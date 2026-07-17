"""SQLite metadata store + content-addressed blob store.

Files live on disk under blobs/<aa>/<sha256>, keyed by sha256(content), so
identical content is de-duplicated (R6: dedupe is metadata-only). SQLite holds
counter records, sync state, and a window of recent block hashes for reorg
detection (N4). Numbering is a gap-free global sequence starting at 0, and
every row extends the rolling consensus-hash chain (build ref v3 §7).
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .config import ROLLING_HASH_GENESIS_TAG, Config

SCHEMA = """
CREATE TABLE IF NOT EXISTS counters (
    number           INTEGER PRIMARY KEY,
    asset            TEXT    NOT NULL,
    asset_id         TEXT,
    asset_longname   TEXT,
    kind             TEXT    NOT NULL,             -- 'issuance' | 'fairminter'
    content_type     TEXT,                         -- normalized display MIME
    content_type_raw TEXT,                         -- verbatim, when it differs
    content_sha256   TEXT    NOT NULL,
    content_length   INTEGER NOT NULL,
    is_pointer_like  INTEGER NOT NULL DEFAULT 0,
    mint_txid        TEXT    NOT NULL,
    msg_index        INTEGER NOT NULL DEFAULT 0,
    block_index      INTEGER NOT NULL,
    cp_tx_index      INTEGER NOT NULL,
    source           TEXT,                         -- mint-time issuer/source
    divisible        INTEGER,
    supply           INTEGER,
    fee              INTEGER,
    tx_size          INTEGER,
    xcp_burned       INTEGER,
    rolling_hash     TEXT    NOT NULL,
    created_at       TEXT    DEFAULT (datetime('now')),
    UNIQUE (mint_txid, msg_index)
);
CREATE INDEX IF NOT EXISTS idx_counters_asset ON counters(asset);
CREATE INDEX IF NOT EXISTS idx_counters_block ON counters(block_index);
CREATE INDEX IF NOT EXISTS idx_counters_sha   ON counters(content_sha256);

CREATE TABLE IF NOT EXISTS sync_state (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    last_height     INTEGER NOT NULL,
    last_block_hash TEXT
);

-- Recent block hashes, for detecting where a reorg forked (N4). Pruned to a
-- sliding window; a reorg deeper than the window needs a rescan from genesis.
CREATE TABLE IF NOT EXISTS indexed_blocks (
    height     INTEGER PRIMARY KEY,
    block_hash TEXT    NOT NULL
);
"""

# Block hashes kept for reorg detection. Far deeper than any plausible reorg.
BLOCK_HASH_WINDOW = 1000


@dataclass
class CounterRecord:
    asset: str
    asset_id: str | None
    asset_longname: str | None
    kind: str                      # 'issuance' | 'fairminter'
    content_type: str | None
    content_type_raw: str | None
    content_sha256: str
    content_length: int
    is_pointer_like: bool
    mint_txid: str
    msg_index: int
    block_index: int
    cp_tx_index: int
    source: str | None
    divisible: bool | None = None
    supply: int | None = None
    fee: int | None = None
    tx_size: int | None = None
    xcp_burned: int | None = None


class Store:
    def __init__(self, config: Config):
        config.ensure_dirs()
        self.blobs_dir = config.blobs_dir
        self.db = sqlite3.connect(str(config.db_path))
        self.db.row_factory = sqlite3.Row
        # WAL lets the server's read connections and the indexer's writer share
        # the file without blocking each other; busy_timeout waits out the brief
        # exclusive lock at commit instead of raising "database is locked".
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(SCHEMA)
        self.db.commit()

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

    def has_event(self, txid: str, msg_index: int = 0) -> bool:
        """Dedup key: one counter per Counterparty MESSAGE (tx_hash, msg_index)."""
        return (
            self.db.execute(
                "SELECT 1 FROM counters WHERE mint_txid = ? AND msg_index = ? LIMIT 1",
                (txid, msg_index),
            ).fetchone()
            is not None
        )

    def last_rolling_hash(self) -> bytes:
        """The chain tip of the rolling hash: the last row's digest, or the
        seed sha256(GENESIS_TAG) when no counter exists yet (§7)."""
        row = self.db.execute(
            "SELECT rolling_hash FROM counters ORDER BY number DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return hashlib.sha256(ROLLING_HASH_GENESIS_TAG).digest()
        return bytes.fromhex(row["rolling_hash"])

    @staticmethod
    def canonical_event(number: int, rec: CounterRecord) -> bytes:
        """canonical(n) of build ref v3 §7 — the bytes each event contributes
        to the rolling hash. Consensus-critical: never reorder or extend."""
        return (
            f"{number}|{rec.mint_txid}|{rec.msg_index}|{rec.block_index}|"
            f"{rec.cp_tx_index}|{rec.kind}|{rec.asset}|{rec.content_sha256}|"
            f"{rec.content_length}"
        ).encode("utf-8")

    def add_counter(self, number: int, rec: CounterRecord) -> str:
        """Insert a counter, extending the rolling hash chain. Returns the new
        rolling hash (hex).

        `number` must be exactly next_number(): the store owns the gap-free
        sequence (N2) because the rolling hash bakes the number into the
        chain — a stale or gapped number would corrupt it silently."""
        expected = self.next_number()
        if number != expected:
            raise ValueError(
                f"counter number {number} out of sequence (expected {expected}); "
                f"refusing to corrupt the rolling hash chain"
            )
        rolling = hashlib.sha256(
            self.last_rolling_hash() + self.canonical_event(number, rec)
        ).hexdigest()
        self.db.execute(
            """
            INSERT INTO counters (
                number, asset, asset_id, asset_longname, kind, content_type,
                content_type_raw, content_sha256, content_length,
                is_pointer_like, mint_txid, msg_index, block_index, cp_tx_index,
                source, divisible, supply, fee, tx_size, xcp_burned, rolling_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                number,
                rec.asset,
                rec.asset_id,
                rec.asset_longname,
                rec.kind,
                rec.content_type,
                rec.content_type_raw,
                rec.content_sha256,
                rec.content_length,
                int(rec.is_pointer_like),
                rec.mint_txid,
                rec.msg_index,
                rec.block_index,
                rec.cp_tx_index,
                rec.source,
                None if rec.divisible is None else int(rec.divisible),
                rec.supply,
                rec.fee,
                rec.tx_size,
                rec.xcp_burned,
                rolling,
            ),
        )
        return rolling

    def set_asset_meta(self, number: int, divisible: bool | None, supply: int | None) -> None:
        """Backfill divisibility/supply for an existing record (write-through
        cache for enrichment fetched lazily)."""
        self.db.execute(
            "UPDATE counters SET divisible = ?, supply = ? WHERE number = ?",
            (None if divisible is None else int(divisible), supply, number),
        )
        self.db.commit()

    def set_fee(self, number: int, fee: int | None, tx_size: int | None) -> None:
        """Backfill mint fee/size for an existing record (lazy enrichment)."""
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
        counters (N6), so return the lowest-numbered one deterministically."""
        return self.db.execute(
            "SELECT * FROM counters WHERE asset = ? OR asset_longname = ? "
            "ORDER BY number LIMIT 1",
            (name, name),
        ).fetchone()

    def get_counters_by_asset(self, name: str) -> list[sqlite3.Row]:
        """All counters on an asset, oldest first (the original, then any later
        events the asset accumulated under per-event numbering)."""
        return self.db.execute(
            "SELECT * FROM counters WHERE asset = ? OR asset_longname = ? "
            "ORDER BY number",
            (name, name),
        ).fetchall()

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

    def list_by_source(self, source: str, limit: int = 1000) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM counters WHERE source = ? ORDER BY number ASC LIMIT ?",
            (source, limit),
        ).fetchall()

    def list_by_block_range(self, start: int, end: int) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM counters WHERE block_index BETWEEN ? AND ? ORDER BY number ASC",
            (start, end),
        ).fetchall()

    # --- sync state + reorg support (N4) ------------------------------------

    def get_last_height(self, default: int) -> int:
        row = self.db.execute("SELECT last_height FROM sync_state WHERE id = 1").fetchone()
        return row["last_height"] if row else default - 1

    def get_last_block_hash(self) -> str | None:
        row = self.db.execute(
            "SELECT last_block_hash FROM sync_state WHERE id = 1"
        ).fetchone()
        return row["last_block_hash"] if row else None

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
        if block_hash is not None:
            self.db.execute(
                "INSERT INTO indexed_blocks (height, block_hash) VALUES (?, ?) "
                "ON CONFLICT(height) DO UPDATE SET block_hash = excluded.block_hash",
                (height, block_hash),
            )
            self.db.execute(
                "DELETE FROM indexed_blocks WHERE height < ?",
                (height - BLOCK_HASH_WINDOW,),
            )

    def get_indexed_block_hash(self, height: int) -> str | None:
        row = self.db.execute(
            "SELECT block_hash FROM indexed_blocks WHERE height = ?", (height,)
        ).fetchone()
        return row["block_hash"] if row else None

    def lowest_tracked_height(self) -> int | None:
        row = self.db.execute("SELECT MIN(height) AS m FROM indexed_blocks").fetchone()
        return row["m"]

    def rollback_to(self, height: int) -> int:
        """Undo everything above `height` (log-structured rollback, N4):
        delete counters and tracked block hashes with block_index > height and
        rewind the sync cursor. Numbering and the rolling hash chain re-derive
        identically on re-index. Returns the number of counters removed.
        Blobs are content-addressed and shared, so they are left in place."""
        removed = self.db.execute(
            "SELECT COUNT(*) AS c FROM counters WHERE block_index > ?", (height,)
        ).fetchone()["c"]
        self.db.execute("DELETE FROM counters WHERE block_index > ?", (height,))
        self.db.execute("DELETE FROM indexed_blocks WHERE height > ?", (height,))
        self.db.execute(
            "UPDATE sync_state SET last_height = ?, last_block_hash = ? WHERE id = 1",
            (height, self.get_indexed_block_hash(height)),
        )
        self.db.commit()
        return removed

    def commit(self) -> None:
        self.db.commit()
