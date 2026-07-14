"""SQLite metadata store + content-addressed blob store.

Files live on disk under blobs/<aa>/<sha256>, keyed by sha256(content), so
identical content is de-duplicated. SQLite holds counter records and sync
state. Numbering is a gap-free global sequence starting at 0.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .config import Config

SCHEMA = """
CREATE TABLE IF NOT EXISTS counters (
    number          INTEGER PRIMARY KEY,
    asset           TEXT    NOT NULL,
    asset_id        TEXT,
    asset_longname  TEXT,
    content_type    TEXT,
    content_sha256  TEXT    NOT NULL,
    content_length  INTEGER NOT NULL,
    mint_txid       TEXT    NOT NULL UNIQUE,
    block_index     INTEGER NOT NULL,
    block_position  INTEGER NOT NULL,
    cp_tx_index     INTEGER,
    owner           TEXT,
    divisible       INTEGER,
    supply          INTEGER,
    fee             INTEGER,
    tx_size         INTEGER,
    xcp_burned      INTEGER,
    reinscription   INTEGER DEFAULT 0,
    created_at      TEXT    DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_counters_asset ON counters(asset);
CREATE INDEX IF NOT EXISTS idx_counters_block ON counters(block_index);
CREATE INDEX IF NOT EXISTS idx_counters_sha   ON counters(content_sha256);

CREATE TABLE IF NOT EXISTS sync_state (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    last_height     INTEGER NOT NULL,
    last_block_hash TEXT
);
"""


@dataclass
class CounterRecord:
    asset: str
    asset_id: str | None
    asset_longname: str | None
    content_type: str | None
    content_sha256: str
    content_length: int
    mint_txid: str
    block_index: int
    block_position: int
    cp_tx_index: int | None
    owner: str | None
    divisible: bool | None = None
    supply: int | None = None
    fee: int | None = None
    tx_size: int | None = None
    xcp_burned: int | None = None
    # True when this is NOT the first counter on its asset (a reinscription).
    reinscription: bool = False


class Store:
    def __init__(self, config: Config):
        config.ensure_dirs()
        self.blobs_dir = config.blobs_dir
        self.db = sqlite3.connect(str(config.db_path))
        self.db.row_factory = sqlite3.Row
        # WAL lets the server's read connections and the indexer's writer share
        # the file without blocking each other; busy_timeout waits out the brief
        # exclusive lock at commit instead of raising "database is locked". Both
        # matter now that `counters server` can run the indexer in-process.
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.commit()

    def _migrate(self) -> None:
        """Add/rename columns introduced after a DB was first created (CREATE
        TABLE IF NOT EXISTS never alters an existing table)."""
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(counters)")}
        if "vsize" in cols and "tx_size" not in cols:
            # Fee rate switched from virtual size to raw serialized size.
            self.db.execute("ALTER TABLE counters RENAME COLUMN vsize TO tx_size")
            cols.discard("vsize")
            cols.add("tx_size")
        for col in ("divisible", "supply", "fee", "tx_size", "xcp_burned"):
            if col not in cols:
                self.db.execute(f"ALTER TABLE counters ADD COLUMN {col} INTEGER")
        if "reinscription" not in cols:
            # 0 = original (first counter on its asset), 1 = reinscription.
            self.db.execute(
                "ALTER TABLE counters ADD COLUMN reinscription INTEGER DEFAULT 0"
            )

    def close(self) -> None:
        self.db.close()

    # --- blobs -------------------------------------------------------------

    def store_blob(self, content: bytes) -> str:
        digest = hashlib.sha256(content).hexdigest()
        path = self._blob_path(digest)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(content)
            tmp.replace(path)
        return digest

    def _blob_path(self, digest: str) -> Path:
        return self.blobs_dir / digest[:2] / digest

    def read_blob(self, digest: str) -> bytes | None:
        path = self._blob_path(digest)
        return path.read_bytes() if path.exists() else None

    # --- counters ----------------------------------------------------------

    def next_number(self) -> int:
        row = self.db.execute("SELECT MAX(number) AS m FROM counters").fetchone()
        return 0 if row["m"] is None else row["m"] + 1

    def count(self) -> int:
        return self.db.execute("SELECT COUNT(*) AS c FROM counters").fetchone()["c"]

    def has_txid(self, txid: str) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM counters WHERE mint_txid = ? LIMIT 1", (txid,)
            ).fetchone()
            is not None
        )

    def add_counter(self, number: int, rec: CounterRecord) -> None:
        self.db.execute(
            """
            INSERT INTO counters (
                number, asset, asset_id, asset_longname, content_type,
                content_sha256, content_length, mint_txid, block_index,
                block_position, cp_tx_index, owner, divisible, supply, fee, tx_size,
                xcp_burned, reinscription
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                number,
                rec.asset,
                rec.asset_id,
                rec.asset_longname,
                rec.content_type,
                rec.content_sha256,
                rec.content_length,
                rec.mint_txid,
                rec.block_index,
                rec.block_position,
                rec.cp_tx_index,
                rec.owner,
                None if rec.divisible is None else int(rec.divisible),
                rec.supply,
                rec.fee,
                rec.tx_size,
                rec.xcp_burned,
                int(rec.reinscription),
            ),
        )

    def set_asset_meta(self, number: int, divisible: bool | None, supply: int | None) -> None:
        """Backfill divisibility/supply for an existing record (write-through
        cache for older rows recorded before these columns existed)."""
        self.db.execute(
            "UPDATE counters SET divisible = ?, supply = ? WHERE number = ?",
            (None if divisible is None else int(divisible), supply, number),
        )
        self.db.commit()

    def set_fee(self, number: int, fee: int | None, tx_size: int | None) -> None:
        """Backfill mint fee/size for an existing record (enrichment computed
        lazily the first time a counter is viewed, like set_asset_meta)."""
        self.db.execute(
            "UPDATE counters SET fee = ?, tx_size = ? WHERE number = ?",
            (fee, tx_size, number),
        )
        self.db.commit()

    def set_xcp_burned(self, number: int, xcp_burned: int | None) -> None:
        """Backfill the XCP burned for the issuance (lazy enrichment)."""
        self.db.execute(
            "UPDATE counters SET xcp_burned = ? WHERE number = ?",
            (xcp_burned, number),
        )
        self.db.commit()

    def get_counter(self, number: int) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM counters WHERE number = ?", (number,)
        ).fetchone()

    def get_counter_by_asset(self, name: str) -> sqlite3.Row | None:
        """The canonical (original) counter for an asset: an asset may back many
        counters, so return the lowest-numbered one deterministically."""
        return self.db.execute(
            "SELECT * FROM counters WHERE asset = ? OR asset_longname = ? "
            "ORDER BY number LIMIT 1",
            (name, name),
        ).fetchone()

    def get_counters_by_asset(self, name: str) -> list[sqlite3.Row]:
        """All counters inscribed on an asset, oldest first (original, then any
        reinscriptions)."""
        return self.db.execute(
            "SELECT * FROM counters WHERE asset = ? OR asset_longname = ? "
            "ORDER BY number",
            (name, name),
        ).fetchall()

    def has_asset(self, asset: str) -> bool:
        """True if the asset already backs at least one counter (used to decide
        whether a new counter on it is a reinscription)."""
        return (
            self.db.execute(
                "SELECT 1 FROM counters WHERE asset = ? LIMIT 1", (asset,)
            ).fetchone()
            is not None
        )

    def find(self, identifier: str) -> sqlite3.Row | None:
        """Resolve a counter by number (all-digit) or asset name/longname.

        Asset names never begin with a digit (named are A-Z; numeric display is
        'A'+int; subassets contain a '.'), so an all-digit token is a number.
        """
        token = str(identifier)
        if token.isdigit():
            return self.get_counter(int(token))
        return self.get_counter_by_asset(token)

    def list_recent(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM counters ORDER BY number DESC LIMIT ?", (limit,)
        ).fetchall()

    def list_before(self, before: int, limit: int = 120) -> list[sqlite3.Row]:
        """Newest-first page of counters with number < `before` (for the
        explorer's 'load more' pagination)."""
        return self.db.execute(
            "SELECT * FROM counters WHERE number < ? ORDER BY number DESC LIMIT ?",
            (before, limit),
        ).fetchall()

    def list_by_owner(self, owner: str, limit: int = 1000) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM counters WHERE owner = ? ORDER BY number ASC LIMIT ?",
            (owner, limit),
        ).fetchall()

    def list_by_block_range(self, start: int, end: int) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM counters WHERE block_index BETWEEN ? AND ? ORDER BY number ASC",
            (start, end),
        ).fetchall()

    # --- sync state --------------------------------------------------------

    def get_last_height(self, default: int) -> int:
        row = self.db.execute("SELECT last_height FROM sync_state WHERE id = 1").fetchone()
        return row["last_height"] if row else default - 1

    def set_last_height(self, height: int, block_hash: str | None) -> None:
        self.db.execute(
            """
            INSERT INTO sync_state (id, last_height, last_block_hash)
            VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET last_height = excluded.last_height,
                                          last_block_hash = excluded.last_block_hash
            """,
            (height, block_hash),
        )

    def commit(self) -> None:
        self.db.commit()
