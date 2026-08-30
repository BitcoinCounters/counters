"""Following Counterparty while it catches up: the ledger-db oracle.

Core's API answers 503 "Counterparty not ready" to every ledger question
while the node trails bitcoind, so an indexer that only knows the API parks
until Core is fully caught up. counters/ledger.py answers the same questions
from Core's SQLite ledger directly. Pinned here:

  1. rows read from the ledger come out shaped like the API's rows (hex tx
     hash, asset NAME not asset_index, address STRING not address_id, bools);
  2. a block Core has not finished (no ledger_hash) raises — never [] — so
     the pass retries instead of advancing past real events;
  3. the parsed height is the last block WITH a ledger_hash;
  4. asset identity/supply mirror /v2/assets/{asset} (issued - destroyed);
  5. the indexer reads the ledger while /v2/ says server_ready=false, and
     the API again once it is ready — and indexes the SAME counters either
     way (same numbering, same rolling hash);
  6. without a ledger db, a not-ready API is a retry, never a skipped block.

The ledger here is a tiny SQLite file with the v11 schema subset the reader
touches (blocks, issuances, fairminters, assets, address_list, destructions).
Nothing talks to a real Core.

Zero-dependency runner: python tests/test_ledger.py   (or via pytest)
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counters.config import GENESIS_HEIGHT, Config  # noqa: E402
from counters.counterparty import CounterpartyError  # noqa: E402
from counters.indexer import Indexer  # noqa: E402
from counters.ledger import CounterpartyLedger  # noqa: E402
from counters.store import Store  # noqa: E402

from test_pipeline import FakeBTC, FakeCP, classic_tx, issuance, reveal_tx  # noqa: E402

G = GENESIS_HEIGHT
TXID = "8a" * 32
FM_TXID = "df" * 32
ADDR = "bc1pary3645xyj927jtm3dh5gsc43ukw8e3nw54x6eekxywd05vajl5systdel"


def make_ledger(path: str, *, parsed_through: int = G + 1) -> None:
    """A miniature v11 ledger: block G carries one valid issuance (asset and
    addresses stored as compact integer ids, tx_hash as a BLOB), block G+1 a
    fairminter deploy and an invalid issuance; block G+2 the same fairminter's
    `closed` status-change row (the ledger logs one row per change). Blocks
    above `parsed_through` exist but have no ledger_hash (Core mid-parse /
    rolling back)."""
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE blocks (block_index INTEGER PRIMARY KEY, block_hash TEXT,
                             block_time INTEGER, ledger_hash TEXT);
        CREATE TABLE assets (asset_index INTEGER PRIMARY KEY, asset_id TEXT,
                             asset_name TEXT, block_index INTEGER, asset_longname TEXT);
        CREATE TABLE address_list (address_id INTEGER PRIMARY KEY, address TEXT);
        CREATE TABLE issuances (tx_index INTEGER, tx_hash BLOB, msg_index INTEGER,
                                block_index INTEGER, asset INTEGER, quantity INTEGER,
                                divisible BOOL, source INTEGER, issuer INTEGER,
                                description TEXT, fee_paid INTEGER, status TEXT,
                                asset_longname TEXT, fair_minting BOOL, locked BOOL,
                                mime_type TEXT);
        CREATE TABLE fairminters (tx_hash BLOB, tx_index INTEGER, block_index INTEGER,
                                  source INTEGER, asset INTEGER, asset_parent INTEGER,
                                  asset_longname TEXT, description TEXT, divisible BOOL,
                                  status TEXT, mime_type TEXT);
        CREATE TABLE destructions (tx_index INTEGER, tx_hash BLOB, block_index INTEGER,
                                   source INTEGER, asset INTEGER, quantity INTEGER,
                                   tag TEXT, status TEXT);
        CREATE TABLE transactions (tx_index INTEGER, tx_hash BLOB, block_index INTEGER);
    """)
    db.execute("INSERT INTO transactions VALUES (3155632, ?, ?)", (bytes.fromhex(TXID), G))
    db.execute("INSERT INTO transactions VALUES (3155640, ?, ?)", (bytes.fromhex("ee" * 32), G + 1))
    db.execute("INSERT INTO transactions VALUES (3155641, ?, ?)", (bytes.fromhex(FM_TXID), G + 1))
    db.execute("INSERT INTO assets VALUES (1, '0', 'BTC', NULL, NULL)")
    db.execute("INSERT INTO assets VALUES (261993, '12517955578', 'BONPARTY', ?, NULL)", (G,))
    db.execute("INSERT INTO assets VALUES (261994, '9', 'A9', ?, 'BONPARTY.SUB')", (G + 1,))
    db.execute("INSERT INTO address_list VALUES (467009, ?)", (ADDR,))
    for h in range(G, G + 4):
        db.execute("INSERT INTO blocks VALUES (?, ?, 1785239195, ?)",
                   (h, f"hash-{h}", f"ledger-{h}" if h <= parsed_through else None))
    db.execute(
        "INSERT INTO issuances VALUES (3155632, ?, 0, ?, 261993, 2000, 0, 467009, 467009, "
        "'89504e47', 50000000, 'valid', NULL, 0, 1, 'image/png')",
        (bytes.fromhex(TXID), G))
    db.execute(
        "INSERT INTO issuances VALUES (3155640, ?, 0, ?, 261993, 5, 0, 467009, 467009, "
        "'', 0, 'invalid: locked', NULL, 0, 1, NULL)",
        (bytes.fromhex("ee" * 32), G + 1))
    db.execute(
        "INSERT INTO fairminters VALUES (?, 3155641, ?, 467009, 261994, 261993, "
        "'BONPARTY.SUB', 'ipfs:bafy', 0, 'open', NULL)",
        (bytes.fromhex(FM_TXID), G + 1))
    db.execute(
        "INSERT INTO fairminters VALUES (?, 3155641, ?, 467009, 261994, 261993, "
        "'BONPARTY.SUB', 'ipfs:bafy', 0, 'closed', NULL)",
        (bytes.fromhex(FM_TXID), G + 2))   # status change at G+2: NOT a deploy
    db.execute("INSERT INTO destructions VALUES (3155650, ?, ?, 467009, 261993, 221, '', 'valid')",
               (bytes.fromhex("dd" * 32), G + 1))
    db.commit()
    db.close()


def _cfg(tmp: str, ledger_path: str | None) -> Config:
    cfg = Config()
    cfg.data_dir = tmp
    cfg.cp_db_path = ledger_path or os.path.join(tmp, "no-such-ledger.db")
    return cfg


# --- 1-4. the reader --------------------------------------------------------

def test_rows_are_decoded_to_api_shape():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "counterparty.db")
        make_ledger(path)
        ledger = CounterpartyLedger(path)

        rows = ledger.get_block_issuances(G)
        assert len(rows) == 1
        row = rows[0]
        assert row["tx_hash"] == TXID                 # BLOB -> hex
        assert row["asset"] == "BONPARTY"             # asset_index -> name
        assert row["source"] == ADDR and row["issuer"] == ADDR  # address_id -> string
        assert row["divisible"] is False and row["fair_minting"] is False
        assert row["locked"] is True
        assert row["status"] == "valid"
        assert row["description"] == "89504e47" and row["mime_type"] == "image/png"
        assert row["tx_index"] == 3155632 and row["msg_index"] == 0
        assert row["fee_paid"] == 50000000

        # Invalid rows are returned too (the indexer applies R1 itself).
        assert [r["status"] for r in ledger.get_block_issuances(G + 1)] == ["invalid: locked"]

        fm = ledger.get_block_fairminters(G + 1)
        assert len(fm) == 1
        assert fm[0]["tx_hash"] == FM_TXID
        assert fm[0]["asset"] == "A9" and fm[0]["asset_parent"] == "BONPARTY"
        assert fm[0]["asset_longname"] == "BONPARTY.SUB"
        assert fm[0]["source"] == ADDR
        assert ledger.get_block_fairminters(G) == []
        ledger.close()


def test_fairminter_status_changes_are_not_deploys():
    """The ledger stamps every status change with its own block; only the
    row in the deploy transaction's block is the event. (The API's state db
    instead MOVES its single row to the latest change — which is why an
    after-the-fact API index numbers a deploy at the wrong block.)"""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "counterparty.db")
        make_ledger(path, parsed_through=G + 2)
        ledger = CounterpartyLedger(path)
        assert [r["tx_hash"] for r in ledger.get_block_fairminters(G + 1)] == [FM_TXID]
        assert ledger.get_block_fairminters(G + 2) == []
        ledger.close()


def test_unparsed_block_raises_never_empty():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "counterparty.db")
        make_ledger(path, parsed_through=G + 1)
        ledger = CounterpartyLedger(path)
        for height in (G + 2, G + 3, G + 99):   # no ledger_hash / no row at all
            for call in (ledger.get_block_issuances, ledger.get_block_fairminters):
                try:
                    call(height)
                except CounterpartyError as e:
                    assert e.kind == "unparsed"
                else:
                    raise AssertionError(f"block {height} must not read as parsed")
        ledger.close()


def test_height_is_last_block_with_ledger_hash():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "counterparty.db")
        make_ledger(path, parsed_through=G + 1)
        ledger = CounterpartyLedger(path)
        assert ledger.counterparty_height() == G + 1   # G+2, G+3 exist but unhashed
        assert ledger.status() == {"counterparty_height": G + 1, "server_ready": True}
        ledger.close()


def test_asset_mirrors_api():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "counterparty.db")
        make_ledger(path)
        ledger = CounterpartyLedger(path)
        info = ledger.get_asset("BONPARTY")
        assert info["asset_id"] == "12517955578"
        assert info["asset_longname"] is None
        assert info["divisible"] is False
        assert info["supply"] == 2000 - 221            # valid issued - valid destroyed
        assert ledger.get_asset("bonparty.sub")["asset"] == "A9"   # by longname, any case
        assert ledger.get_asset("NOSUCH") is None
        ledger.close()


def test_open_ignores_missing_or_foreign_files():
    with tempfile.TemporaryDirectory() as tmp:
        assert CounterpartyLedger.open(_cfg(tmp, None)) is None
        foreign = os.path.join(tmp, "other.db")
        sqlite3.connect(foreign).execute("CREATE TABLE t (x)").connection.commit()
        assert CounterpartyLedger.open(_cfg(tmp, foreign)) is None
        real = os.path.join(tmp, "counterparty.db")
        make_ledger(real)
        ledger = CounterpartyLedger.open(_cfg(tmp, real))
        assert ledger is not None and ledger.path.name == "counterparty.db"
        ledger.close()


# --- 5-6. the indexer chooses its oracle ------------------------------------

def _api_rows():
    """The API's view of the same events the ledger holds."""
    row = issuance(TXID, 3155632, desc="89504e47", asset="BONPARTY", mime="image/png")
    row["issuer"] = row["source"] = ADDR
    row["fee_paid"] = 50000000
    return {G: [row]}


class _API(FakeCP):
    def __init__(self, *a, ready=True, **kw):
        super().__init__(*a, **kw)
        self.ready = ready
        self.block_reads = 0

    def get_block_issuances(self, height):
        if not self.ready:
            raise CounterpartyError("Counterparty API HTTP 503: Counterparty not ready",
                                    kind="not_ready")
        self.block_reads += 1
        return super().get_block_issuances(height)

    def get_block_fairminters(self, height):
        if not self.ready:
            raise CounterpartyError("Counterparty API HTTP 503: Counterparty not ready",
                                    kind="not_ready")
        return super().get_block_fairminters(height)

    def get_asset(self, asset):
        return {"asset": asset, "asset_id": "12517955578", "divisible": False, "supply": 1779}


def _run_once(tmp, ledger_path, *, ready):
    cfg = _cfg(tmp, ledger_path)
    store = Store(cfg)
    # The fairminter deploy is classic-carried: R4 drops it on either path.
    btc = FakeBTC({TXID: reveal_tx(TXID), FM_TXID: classic_tx(FM_TXID)}, tip=G + 100)
    cp = _API(_api_rows(), tip=G + 1, ready=ready)
    idx = Indexer(cfg, btc=btc, cp=cp, store=store)
    idx._notify = lambda m: None
    return idx, cp, store


def test_indexer_reads_ledger_while_api_not_ready_then_api_again():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "counterparty.db")
        make_ledger(path, parsed_through=G + 1)

        # API closed: the pass comes from the ledger and records the counter.
        idx, cp, store = _run_once(tmp, path, ready=False)
        assert idx.sync_to_tip() == 1
        assert idx._oracle is idx.ledger and idx.ledger is not None
        assert cp.block_reads == 0
        assert store.count() == 1 and store.get_last_height(G) == G + 1
        rec = store.get_counter(0)
        assert rec["mint_txid"] == TXID and rec["asset"] == "BONPARTY"
        assert rec["source"] == ADDR
        assert rec["supply"] == 2000 - 221
        assert "indexing from ledger db" in idx._height_lines()[1]
        via_ledger_hash = store.last_rolling_hash()

        # Core catches up: the next poll goes back to the API.
        cp.ready = True
        cp.tip = G + 3
        assert idx.sync_to_tip() == 0
        assert idx._oracle is idx.cp
        assert cp.block_reads > 0
        assert "ledger db" not in idx._height_lines()[1]
        assert store.get_last_height(G) == G + 3

        # Same events through the API alone produce the same numbering + hash.
        with tempfile.TemporaryDirectory() as tmp2:
            idx2, _, store2 = _run_once(tmp2, None, ready=True)
            idx2.sync_to_tip()
            assert store2.count() == 1
            assert store2.get_counter(0)["mint_txid"] == TXID
            assert store2.last_rolling_hash() == via_ledger_hash


def test_indexer_follows_ledger_only_as_far_as_it_is_parsed():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "counterparty.db")
        make_ledger(path, parsed_through=G + 1)   # G+2.. exist, unhashed
        idx, _, store = _run_once(tmp, path, ready=False)
        idx.cp.tip = G + 3                        # the API's (stale) claim is ignored
        idx.sync_to_tip()
        assert store.get_last_height(G) == G + 1  # stopped at the ledger's real height
        assert idx._cp_tip == G + 1


def test_not_ready_without_ledger_is_a_retry_not_a_skip():
    with tempfile.TemporaryDirectory() as tmp:
        idx, cp, store = _run_once(tmp, None, ready=False)
        try:
            idx.sync_to_tip()
        except CounterpartyError as e:
            assert e.kind == "not_ready"
        else:
            raise AssertionError("a not-ready API with no ledger must abort the pass")
        assert idx._oracle is idx.cp and idx.ledger is None
        assert store.count() == 0
        assert store.get_last_height(G) == G - 1  # cursor untouched (fresh store)
        # ...and the reason lands on the counterparty line.
        idx._cp_down = True
        idx._set_wait_note(CounterpartyError("x", kind="not_ready"))
        assert "set CP_DB_PATH" in idx._height_lines()[1]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all ledger tests passed")
