"""The v3 indexer pipeline end-to-end against fake backends.

Covers the validity rules (R1-R4), numbering (N1/N2/N6), dedup, the rolling
consensus-hash chain (§7), and reorg rollback (N4).

Zero-dependency runner: python tests/test_pipeline.py   (or via pytest)
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counters.config import GENESIS_HEIGHT, Config  # noqa: E402
from counters.indexer import Indexer  # noqa: E402
from counters.store import Store  # noqa: E402

MARKER_SCRIPT = "6a08434e545250525459"
REVEAL_WITNESS = ["aa" * 64, "0063036f7264", "c0" + "bb" * 32]
G = GENESIS_HEIGHT


def reveal_tx(txid: str) -> dict:
    return {
        "txid": txid,
        "vout": [{"scriptPubKey": {"hex": MARKER_SCRIPT}}],
        "vin": [{"txid": "cc" * 32, "vout": 0, "txinwitness": REVEAL_WITNESS}],
    }


def classic_tx(txid: str) -> dict:
    """A classic OP_RETURN-encoded Counterparty tx (encrypted data, no marker)."""
    return {
        "txid": txid,
        "vout": [{"scriptPubKey": {"hex": "6a1c" + "de" * 28}}],
        "vin": [{"txid": "cc" * 32, "vout": 0, "txinwitness": []}],
    }


def issuance(txid, tx_index, desc="hello", *, status="valid", fair_minting=False,
             asset="TESTASSET", mime=None, msg_index=0):
    return {
        "tx_hash": txid, "tx_index": tx_index, "msg_index": msg_index,
        "status": status, "fair_minting": fair_minting, "asset": asset,
        "asset_longname": None, "description": desc, "mime_type": mime,
        "issuer": "bc1qissuer", "source": "bc1qissuer", "fee_paid": 0,
        "divisible": False,
    }


def fairminter(txid, tx_index, desc="ipfs:bafytest", asset="FAIRTEST", mime=None):
    return {
        "tx_hash": txid, "tx_index": tx_index, "asset": asset,
        "asset_longname": None, "description": desc, "mime_type": mime,
        "source": "bc1qdeployer",
    }


class FakeBTC:
    def __init__(self, txs: dict[str, dict], tip: int = G + 10):
        self.txs = txs
        self.tip = tip
        # height -> hash; mutate to simulate a reorg
        self.hashes: dict[int, str] = {}

    def get_block_count(self):
        return self.tip

    def get_block_hash(self, height):
        return self.hashes.get(height, f"hash-{height}")

    def get_raw_transaction(self, txid, verbose=True):
        return self.txs[txid]

    def get_inscription_cost(self, txid, reveal_tx=None):
        return 1000, 500


class FakeCP:
    def __init__(self, issuances: dict[int, list], fairminters: dict[int, list] | None = None,
                 tip: int = G + 10):
        self.issuances = issuances
        self.fairminters = fairminters or {}
        self.tip = tip

    def counterparty_height(self):
        return self.tip

    def get_block_issuances(self, height):
        return list(self.issuances.get(height, []))

    def get_block_fairminters(self, height):
        return list(self.fairminters.get(height, []))

    def get_asset(self, asset):
        return {"asset": asset, "asset_id": "42", "divisible": False, "supply": 1}


def make_indexer(tmp, btc, cp):
    cfg = Config()
    cfg.data_dir = tmp
    store = Store(cfg)
    return Indexer(cfg, btc=btc, cp=cp, store=store)


# --- validity rules ----------------------------------------------------------

def test_rules_filter_candidates():
    """R1 invalid, R2 fairmint, R3 empty description, R4 classic carrier are
    all rejected; the valid taproot-carried issuance is recorded."""
    txs = {
        "t-valid": reveal_tx("t-valid"),
        "t-invalid": reveal_tx("t-invalid"),
        "t-fairmint": reveal_tx("t-fairmint"),
        "t-empty": reveal_tx("t-empty"),
        "t-classic": classic_tx("t-classic"),
    }
    rows = [
        issuance("t-invalid", 1, status="invalid: bad"),      # R1
        issuance("t-fairmint", 2, fair_minting=True),         # R2
        issuance("t-empty", 3, desc=""),                      # R3
        issuance("t-none", 4, desc=None),                     # R3
        issuance("t-classic", 5),                             # R4
        issuance("t-valid", 6),                               # records
    ]
    with tempfile.TemporaryDirectory() as tmp:
        idx = make_indexer(tmp, FakeBTC(txs), FakeCP({G: rows}))
        recorded = idx.process_block(G)
        assert recorded == 1
        row = idx.store.get_counter(0)
        assert row["mint_txid"] == "t-valid"
        assert row["kind"] == "issuance"
        assert row["cp_tx_index"] == 6
        idx.close()


def test_fairminter_deploy_qualifies_and_pointer_flagged():
    txs = {"t-fm": reveal_tx("t-fm")}
    with tempfile.TemporaryDirectory() as tmp:
        idx = make_indexer(tmp, FakeBTC(txs),
                           FakeCP({}, fairminters={G: [fairminter("t-fm", 7)]}))
        assert idx.process_block(G) == 1
        row = idx.store.get_counter(0)
        assert row["kind"] == "fairminter"
        assert row["asset"] == "FAIRTEST"
        assert row["is_pointer_like"] == 1     # ipfs: pointer, metadata only
        assert row["source"] == "bc1qdeployer"
        idx.close()


def test_dedupe_same_message_across_tables():
    """A message surfacing as both an issuance row and a fairminter row (same
    tx_hash) is counted once."""
    txs = {"t-both": reveal_tx("t-both")}
    with tempfile.TemporaryDirectory() as tmp:
        idx = make_indexer(
            tmp, FakeBTC(txs),
            FakeCP({G: [issuance("t-both", 9, asset="FAIRTEST")]},
                   fairminters={G: [fairminter("t-both", 9)]}),
        )
        assert idx.process_block(G) == 1
        assert idx.store.count() == 1
        idx.close()


def test_binary_content_hashed_over_decoded_bytes():
    gif_hex = "474946383761" + "00" * 10
    txs = {"t-gif": reveal_tx("t-gif")}
    rows = [issuance("t-gif", 1, desc=gif_hex, mime="image/gif")]
    with tempfile.TemporaryDirectory() as tmp:
        idx = make_indexer(tmp, FakeBTC(txs), FakeCP({G: rows}))
        idx.process_block(G)
        row = idx.store.get_counter(0)
        raw = bytes.fromhex(gif_hex)
        assert row["content_sha256"] == hashlib.sha256(raw).hexdigest()
        assert row["content_length"] == len(raw)
        assert idx.store.read_blob(row["content_sha256"]) == raw
        idx.close()


# --- numbering ----------------------------------------------------------------

def test_n1_ordering_within_block_and_across_blocks():
    txs = {t: reveal_tx(t) for t in ("t-a", "t-b", "t-c")}
    blocks = {
        G: [issuance("t-b", 20, asset="B"), issuance("t-a", 10, asset="A")],
        G + 1: [issuance("t-c", 30, asset="C")],
    }
    with tempfile.TemporaryDirectory() as tmp:
        idx = make_indexer(tmp, FakeBTC(txs), FakeCP(blocks))
        idx.process_block(G)
        idx.process_block(G + 1)
        # within the block, cp tx_index orders: t-a (10) before t-b (20)
        assert [idx.store.get_counter(n)["mint_txid"] for n in (0, 1, 2)] == \
            ["t-a", "t-b", "t-c"]
        idx.close()


def test_n6_per_event_numbering_same_asset():
    txs = {"t-1": reveal_tx("t-1"), "t-2": reveal_tx("t-2")}
    blocks = {
        G: [issuance("t-1", 1, desc="v1")],
        G + 1: [issuance("t-2", 2, desc="v2")],   # same asset, new content
    }
    with tempfile.TemporaryDirectory() as tmp:
        idx = make_indexer(tmp, FakeBTC(txs), FakeCP(blocks))
        idx.process_block(G)
        idx.process_block(G + 1)
        rows = idx.store.get_counters_by_asset("TESTASSET")
        assert [r["number"] for r in rows] == [0, 1]
        idx.close()


# --- rolling hash (§7) ---------------------------------------------------------

def _index_two_blocks(tmp):
    txs = {"t-1": reveal_tx("t-1"), "t-2": reveal_tx("t-2")}
    blocks = {G: [issuance("t-1", 1)], G + 1: [issuance("t-2", 2, desc="two")]}
    idx = make_indexer(tmp, FakeBTC(txs), FakeCP(blocks))
    idx.process_block(G)
    idx.process_block(G + 1)
    return idx


def test_rolling_hash_deterministic_across_runs():
    with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
        ia, ib = _index_two_blocks(a), _index_two_blocks(b)
        assert ia.store.last_rolling_hash() == ib.store.last_rolling_hash()
        # and each row chains off the previous
        h0 = ia.store.get_counter(0)["rolling_hash"]
        h1 = ia.store.get_counter(1)["rolling_hash"]
        assert h0 != h1 and ia.store.last_rolling_hash().hex() == h1
        ia.close()
        ib.close()


# --- reorg rollback (N4) --------------------------------------------------------

def test_reorg_rolls_back_and_reindexes_identically():
    txs = {"t-1": reveal_tx("t-1"), "t-2": reveal_tx("t-2"), "t-3": reveal_tx("t-3")}
    blocks = {
        G: [issuance("t-1", 1, asset="A")],
        G + 1: [issuance("t-2", 2, asset="B")],
        G + 2: [issuance("t-3", 3, asset="C")],
    }
    with tempfile.TemporaryDirectory() as tmp:
        btc = FakeBTC(txs)
        idx = make_indexer(tmp, btc, FakeCP(blocks))
        for h in (G, G + 1, G + 2):
            idx.process_block(h)
        assert idx.store.count() == 3
        tip_hash_before = idx.store.last_rolling_hash()

        # Reorg: blocks G+1 and G+2 are replaced on the chain.
        btc.hashes[G + 1] = "reorged-1"
        btc.hashes[G + 2] = "reorged-2"
        idx._notify = lambda m: None
        idx.check_reorg()
        assert idx.store.count() == 1                      # rolled back to G
        assert idx.store.get_last_height(G) == G

        # Re-index the replaced blocks: numbering + hash chain re-derive.
        idx.process_block(G + 1)
        idx.process_block(G + 2)
        assert idx.store.count() == 3
        assert idx.store.last_rolling_hash() == tip_hash_before
        idx.close()


def test_no_reorg_is_a_noop():
    with tempfile.TemporaryDirectory() as tmp:
        idx = _index_two_blocks(tmp)
        before = idx.store.count()
        idx.check_reorg()
        assert idx.store.count() == before
        idx.close()


# --- tip clamping ---------------------------------------------------------------

def test_sync_never_passes_the_oracle():
    """sync_to_tip clamps to min(bitcoind, counterparty) - confirmations."""
    txs = {"t-1": reveal_tx("t-1")}
    blocks = {G: [issuance("t-1", 1)]}
    with tempfile.TemporaryDirectory() as tmp:
        btc = FakeBTC(txs, tip=G + 100)
        cp = FakeCP(blocks, tip=G + 1)          # oracle lags far behind
        idx = make_indexer(tmp, btc, cp)
        idx.config.confirmations = 1
        idx.sync_to_tip()
        assert idx.store.get_last_height(G) == G  # (G+1) - 1 confirmation
        idx.close()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
