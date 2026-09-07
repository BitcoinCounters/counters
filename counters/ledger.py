"""Counterparty Core's ledger database, read directly (the oracle, offline).

Core's API refuses every ledger question with HTTP 503 "Counterparty not
ready" while the node is more than a block behind bitcoind — restart,
reparse, or simply catching up after downtime — even though the ledger is
being written block by block the whole time, each block committed atomically.
An indexer that only knows the API therefore sits idle until Core has caught
up completely, and then has the whole backlog still ahead of it.

This module answers the same four oracle questions the indexer asks of the
API (parsed height, a block's issuances, a block's fairminter deploys, an
asset's identity/supply) straight from Core's SQLite ledger file, read-only,
so the index can follow the ledger while Core is still catching up. It is a
different door into the SAME consensus state — rows are decoded to look
exactly like the API's (hex tx hashes, asset names, address strings), so the
indexer's rules, numbering, and rolling hash see the same events through
either door. The one place the doors differ is deliberate: a fairminter
deploy is read from its deploy row here, whereas the API's derived state db
moves a deploy to the block of its latest status change (see
get_block_fairminters).

Only what the indexer reads is decoded. Nothing here is consensus of its own.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from .config import Config
from .counterparty import CounterpartyError

log = logging.getLogger("counters")

# Ledger columns the indexer reads that Core v11 stores as compact integer
# foreign keys (see Core's database.ASSET_INDEX_COLUMN_NAMES /
# ADDRESS_INDEX_COLUMN_NAMES). Older ledgers keep the names/strings inline;
# the decoders pass a non-int value through unchanged, so both shapes work.
_ASSET_COLUMNS = ("asset", "asset_parent")
_ADDRESS_COLUMNS = ("source", "issuer")
# Booleans the API reports as true/false but the ledger stores as 0/1.
_BOOL_COLUMNS = ("divisible", "fair_minting", "locked")


class CounterpartyLedger:
    """Read-only access to Counterparty Core's ledger database.

    Every query runs inside one explicit read transaction, so the "is this
    block fully parsed?" check and the rows read for it come from a single
    WAL snapshot: a block Core is rolling back or reparsing between the two
    statements cannot leak a partial (or empty) event list to the indexer.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        # isolation_level=None: no implicit transactions; _snapshot() BEGINs
        # explicitly so each oracle call is one consistent read.
        self._db = sqlite3.connect(
            f"file:{self.path}?mode=ro", uri=True, isolation_level=None
        )
        self._db.row_factory = sqlite3.Row

    @classmethod
    def open(cls, config: Config) -> CounterpartyLedger | None:
        """The configured ledger if its file exists and is a Counterparty
        ledger, else None (a remote Core, or a different data dir)."""
        path = config.cp_db_path
        if not path or not Path(path).is_file():
            return None
        try:
            ledger = cls(path)
            ledger._db.execute("SELECT block_index FROM blocks LIMIT 1")
            ledger._db.execute("SELECT tx_hash FROM issuances LIMIT 1")
            ledger._db.execute("SELECT tx_hash FROM transactions LIMIT 1")
        except sqlite3.Error as e:
            log.warning("ignoring ledger db %s: %s", path, e)
            return None
        return ledger

    def close(self) -> None:
        self._db.close()

    # --- one consistent read per call ---------------------------------------

    def _snapshot(self):
        return _Snapshot(self._db)

    # --- the oracle interface (mirrors CounterpartyClient) ------------------

    def counterparty_height(self) -> int:
        """Highest block the ledger has fully parsed. Core stamps a block's
        ledger_hash at the END of parsing it (and NULLs it when rolling the
        block back for a reparse), so `ledger_hash IS NOT NULL` is exactly
        "every message of this block is committed"."""
        with self._snapshot() as db:
            row = db.execute(
                "SELECT MAX(block_index) AS h FROM blocks WHERE ledger_hash IS NOT NULL"
            ).fetchone()
        return int(row["h"] or 0)

    def status(self) -> dict:
        """The subset of /v2/ the indexer reads. The ledger is by definition
        ready to answer for everything it has parsed."""
        return {"counterparty_height": self.counterparty_height(), "server_ready": True}

    def get_block_issuances(self, height: int) -> list[dict]:
        """All issuance rows of a fully parsed block, shaped like the API's.
        Raises (never returns []) for a block the ledger has not finished, so
        the sync pass retries instead of advancing past real events."""
        with self._snapshot() as db:
            self._require_parsed(db, height)
            rows = db.execute(
                "SELECT * FROM issuances WHERE block_index = ? ORDER BY tx_index, msg_index",
                (height,),
            ).fetchall()
            return [self._decode(db, r) for r in rows]

    def get_block_fairminters(self, height: int) -> list[dict]:
        """The fairminter DEPLOYS of a fully parsed block, shaped like the API's.

        The ledger's fairminters table is log-structured: one row per status
        change (pending -> open -> closed), each stamped with the block of
        the change. Only the deploy row — the one in the block of the deploy
        transaction itself — is the event a counter numbers, so the block's
        rows are joined to `transactions` on (tx_hash, block_index). The
        later rows for the same deploy would be dropped by the indexer's
        (tx_hash, msg_index) dedup anyway; excluding them here keeps the
        answer the same whether or not the deploy block was indexed first.

        (The API's view is the derived state db, which keeps ONE row per
        deploy and moves its block_index to the latest status change — so
        `/v2/blocks/{h}/fairminters` only reports a deploy at its deploy
        block while the deploy is still pending. See the README.)
        """
        with self._snapshot() as db:
            self._require_parsed(db, height)
            rows = db.execute(
                "SELECT f.* FROM fairminters AS f "
                "JOIN transactions AS t ON t.tx_hash = f.tx_hash AND t.block_index = f.block_index "
                "WHERE f.block_index = ? ORDER BY f.tx_index",
                (height,),
            ).fetchall()
            return [self._decode(db, r) for r in rows]

    def get_asset(self, asset: str) -> dict | None:
        """Identity + supply of `asset` (by name or longname), the fields the
        indexer reads from /v2/assets/{asset}. Supply is Core's own formula
        (ledger.supplies.asset_supply): valid issuances minus valid destroys."""
        with self._snapshot() as db:
            row = db.execute(
                "SELECT asset_index, asset_id, asset_name, asset_longname FROM assets "
                "WHERE asset_name = ? OR asset_longname = ? COLLATE NOCASE",
                (asset.upper(), asset),
            ).fetchone()
            if row is None:
                return None
            index, name = row["asset_index"], row["asset_name"]
            # Issuance rows reference the asset either by index (v11
            # normalized ledger) or by name (older); match both.
            issued = db.execute(
                "SELECT SUM(quantity) AS t FROM issuances "
                "WHERE status = 'valid' AND asset IN (?, ?)",
                (index, name),
            ).fetchone()["t"] or 0
            destroyed = db.execute(
                "SELECT SUM(quantity) AS t FROM destructions "
                "WHERE status = 'valid' AND asset IN (?, ?)",
                (index, name),
            ).fetchone()["t"] or 0
            last = db.execute(
                "SELECT divisible FROM issuances WHERE status = 'valid' AND asset IN (?, ?) "
                "ORDER BY tx_index DESC, msg_index DESC LIMIT 1",
                (index, name),
            ).fetchone()
        return {
            "asset": name,
            "asset_id": str(row["asset_id"]) if row["asset_id"] is not None else None,
            "asset_longname": row["asset_longname"],
            "divisible": bool(last["divisible"]) if last is not None else None,
            "supply": int(issued) - int(destroyed),
        }

    # --- helpers ---------------------------------------------------------------

    @staticmethod
    def _require_parsed(db: sqlite3.Connection, height: int) -> None:
        row = db.execute(
            "SELECT ledger_hash FROM blocks WHERE block_index = ?", (height,)
        ).fetchone()
        if row is None or row["ledger_hash"] is None:
            raise CounterpartyError(
                f"ledger db has not finished parsing block {height}", kind="unparsed"
            )

    def _decode(self, db: sqlite3.Connection, row: sqlite3.Row) -> dict:
        """One ledger row -> the dict the API would have returned for it."""
        d = dict(row)
        for key in ("tx_hash",):
            if isinstance(d.get(key), (bytes, memoryview)):
                d[key] = bytes(d[key]).hex()
        for key in _ASSET_COLUMNS:
            if isinstance(d.get(key), int):
                d[key] = self._asset_name(db, d[key])
        for key in _ADDRESS_COLUMNS:
            if isinstance(d.get(key), int):
                d[key] = self._address(db, d[key])
        for key in _BOOL_COLUMNS:
            if isinstance(d.get(key), int):
                d[key] = bool(d[key])
        return d

    @staticmethod
    def _asset_name(db: sqlite3.Connection, index: int) -> str | int:
        row = db.execute(
            "SELECT asset_name FROM assets WHERE asset_index = ?", (index,)
        ).fetchone()
        return row["asset_name"] if row is not None else index

    @staticmethod
    def _address(db: sqlite3.Connection, address_id: int) -> str | int:
        row = db.execute(
            "SELECT address FROM address_list WHERE address_id = ?", (address_id,)
        ).fetchone()
        return row["address"] if row is not None else address_id


class _Snapshot:
    """`with ledger._snapshot() as db:` — one deferred read transaction."""

    def __init__(self, db: sqlite3.Connection):
        self._db = db

    def __enter__(self) -> sqlite3.Connection:
        self._db.execute("BEGIN")
        return self._db

    def __exit__(self, *exc) -> None:
        # A read transaction has nothing to commit; ROLLBACK just releases the
        # snapshot (and is what we want on an exception too).
        self._db.execute("ROLLBACK")
