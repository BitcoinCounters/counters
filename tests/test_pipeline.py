"""End-to-end pipeline test with synthetic data (no live nodes).

Proves the full path: COUNT envelope in a witness -> join with a Core
"valid creation" issuance -> assign number 0 -> store record + blob.
Also checks the negative cases that gate validity.

Run: python tests/test_pipeline.py   (or via pytest)
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counters.config import Config  # noqa: E402
from counters.counterparty import CounterpartyClient  # noqa: E402
from counters.indexer import Indexer  # noqa: E402
from counters.store import Store  # noqa: E402


# --- script/witness builders (canonical COUNT envelope) --------------------

def push(data: bytes) -> bytes:
    n = len(data)
    if n == 0:
        return b"\x00"
    if n < 0x4C:
        return bytes([n]) + data
    if n <= 0xFF:
        return b"\x4c" + bytes([n]) + data
    if n <= 0xFFFF:
        return b"\x4d" + n.to_bytes(2, "little") + data
    raise ValueError("too big")


def counter_leaf_script(content_type: bytes, body: bytes, asset: bytes = b"") -> bytes:
    s = (
        b"\x00\x63"                       # OP_FALSE OP_IF
        + push(b"COUNT")                  # marker
        + push(b"\x01") + push(content_type)  # tag 1 = content_type
    )
    if asset:
        s += push(b"\x02") + push(asset)  # tag 2 = reinscription target asset
    s += (
        b"\x00"                            # empty-push separator
        + push(body)                       # body
        + b"\x68"                          # OP_ENDIF
        + push(b"\x11" * 32) + b"\xac"    # x-only pubkey + OP_CHECKSIG
    )
    return s


def taproot_witness(leaf_script: bytes) -> list[str]:
    # [signature, leaf script, control block] -- script-path spend
    sig = b"\x30" * 64
    control = b"\xc0" + b"\x11" * 32
    return [sig.hex(), leaf_script.hex(), control.hex()]


def make_block(height: int, txid: str, content_type: bytes, body: bytes,
               asset: bytes = b"") -> dict:
    leaf = counter_leaf_script(content_type, body, asset)
    return {
        "hash": f"blockhash{height}",
        "height": height,
        "tx": [
            {"txid": "deadbeef_no_witness", "vin": [{"coinbase": "00"}], "vout": []},
            {
                "txid": txid,
                "vin": [{"txid": "prev", "vout": 0, "txinwitness": taproot_witness(leaf)}],
                "vout": [],
            },
        ],
    }


# --- fakes -----------------------------------------------------------------

class FakeBitcoind:
    def __init__(self, blocks: dict[int, dict], tip: int, input_addresses=None):
        self._blocks = blocks
        self._tip = tip
        self._input_addresses = set(input_addresses or [])

    def get_input_addresses(self, tx: dict) -> set[str]:
        return set(self._input_addresses)

    def get_block_count(self) -> int:
        return self._tip

    def get_block_hash(self, height: int) -> str:
        return self._blocks[height]["hash"]

    def get_block(self, block_hash: str, verbosity: int = 2) -> dict:
        for b in self._blocks.values():
            if b["hash"] == block_hash:
                return b
        raise KeyError(block_hash)

    def get_fee_and_size(self, txid: str, tx: dict | None = None):
        return 1000, 200   # fixed enrichment; real client queries prevouts

    def get_inscription_cost(self, reveal_txid: str, reveal_tx: dict | None = None):
        return 1500, 300   # commit + reveal total (fee, raw size)


class FakeCounterparty:
    """Mirrors the real client's interface used by the indexer."""

    def __init__(self, issuances_by_block: dict[int, dict], assets: dict[str, dict],
                 issuers: dict[str, str] | None = None):
        self._iss = issuances_by_block
        self._assets = assets
        self._issuers = issuers or {}

    def issuer_at_height(self, asset: str, height: int) -> str | None:
        return self._issuers.get(asset)

    def get_block_issuances(self, height: int) -> dict[str, list[dict]]:
        return self._iss.get(height, {})

    def get_asset(self, asset: str) -> dict | None:
        return self._assets.get(asset)

    # reuse the real validity/creation logic
    is_valid = staticmethod(CounterpartyClient.is_valid)
    is_creation = staticmethod(CounterpartyClient.is_creation)


def build_indexer(block: dict, issuances: dict, assets: dict, tmp: str) -> Indexer:
    cfg = Config()
    cfg.data_dir = tmp
    store = Store(cfg)
    btc = FakeBitcoind({block["height"]: block}, tip=block["height"])
    cp = FakeCounterparty({block["height"]: issuances}, assets)
    return Indexer(cfg, btc=btc, cp=cp, store=store)


# --- tests -----------------------------------------------------------------

def test_records_a_valid_counter():
    height, txid = 800000, "a" * 64
    body = b"\x89PNG\r\n\x1a\n fake png bytes"
    block = make_block(height, txid, b"image/png", body)
    issuances = {txid: [{"asset": "RAREPEPE", "status": "valid", "asset_events": "creation",
                          "tx_index": 12345, "asset_longname": None, "issuer": "1IssuerAddr",
                          "fee_paid": 50000000}]}
    assets = {"RAREPEPE": {"asset_id": "9876543210", "owner": "1OwnerAddr", "asset_longname": None,
                            "divisible": True, "supply": 1000000000}}

    with tempfile.TemporaryDirectory() as tmp:
        idx = build_indexer(block, issuances, assets, tmp)
        recorded = idx.process_block(height)
        assert recorded == 1, f"expected 1 recorded, got {recorded}"

        row = idx.store.get_counter(0)
        assert row is not None, "counter #0 not stored"
        assert row["asset"] == "RAREPEPE"
        assert row["asset_id"] == "9876543210"
        assert row["content_type"] == "image/png"
        assert row["content_length"] == len(body)
        assert row["mint_txid"] == txid
        assert row["block_index"] == height
        assert row["block_position"] == 1  # second tx in block
        assert row["owner"] == "1OwnerAddr"
        assert row["divisible"] == 1  # stored as int
        assert row["supply"] == 1000000000
        assert row["fee"] == 1500 and row["tx_size"] == 300  # commit + reveal cost captured
        assert row["xcp_burned"] == 50000000  # 0.5 XCP burned for the named asset

        # blob content round-trips by sha256
        expected_sha = hashlib.sha256(body).hexdigest()
        assert row["content_sha256"] == expected_sha
        assert idx.store.read_blob(expected_sha) == body

        assert idx.store.get_last_height(0) == height
        idx.close()


def test_skips_invalid_issuance():
    height, txid = 800001, "b" * 64
    block = make_block(height, txid, b"text/plain", b"hi")
    issuances = {txid: [{"asset": "FOO", "status": "invalid", "asset_events": "creation",
                          "tx_index": 1, "asset_longname": None, "issuer": "x"}]}
    with tempfile.TemporaryDirectory() as tmp:
        idx = build_indexer(block, issuances, {"FOO": {"asset_id": "5"}}, tmp)
        assert idx.process_block(height) == 0
        assert idx.store.count() == 0
        idx.close()


def test_skips_non_creation_reissuance():
    height, txid = 800002, "c" * 64
    block = make_block(height, txid, b"text/plain", b"hi")
    issuances = {txid: [{"asset": "FOO", "status": "valid", "asset_events": "change_description",
                          "tx_index": 1, "asset_longname": None, "issuer": "x"}]}
    with tempfile.TemporaryDirectory() as tmp:
        idx = build_indexer(block, issuances, {"FOO": {"asset_id": "5"}}, tmp)
        assert idx.process_block(height) == 0
        idx.close()


def test_skips_when_no_issuance_in_tx():
    height, txid = 800003, "d" * 64
    block = make_block(height, txid, b"text/plain", b"hi")
    with tempfile.TemporaryDirectory() as tmp:
        idx = build_indexer(block, {}, {}, tmp)  # envelope present, but no issuance
        assert idx.process_block(height) == 0
        idx.close()


def test_skips_reserved_asset():
    height, txid = 800004, "e" * 64
    block = make_block(height, txid, b"text/plain", b"hi")
    issuances = {txid: [{"asset": "XCP", "status": "valid", "asset_events": "creation",
                          "tx_index": 1, "asset_longname": None, "issuer": "x"}]}
    with tempfile.TemporaryDirectory() as tmp:
        idx = build_indexer(block, issuances, {"XCP": {"asset_id": "1"}}, tmp)
        assert idx.process_block(height) == 0
        idx.close()


def test_empty_body_counter_is_recorded():
    height, txid = 800005, "f" * 64
    block = make_block(height, txid, b"image/png", b"")  # empty body
    issuances = {txid: [{"asset": "EMPTYONE", "status": "valid", "asset_events": "creation",
                          "tx_index": 7, "asset_longname": None, "issuer": "x"}]}
    with tempfile.TemporaryDirectory() as tmp:
        idx = build_indexer(block, issuances, {"EMPTYONE": {"asset_id": "42", "owner": "o"}}, tmp)
        assert idx.process_block(height) == 1
        row = idx.store.get_counter(0)
        assert row["content_length"] == 0
        assert row["content_sha256"] == hashlib.sha256(b"").hexdigest()
        idx.close()


def build_reinscribe_indexer(block, assets, issuers, input_addresses, tmp) -> Indexer:
    cfg = Config()
    cfg.data_dir = tmp
    store = Store(cfg)
    btc = FakeBitcoind({block["height"]: block}, tip=block["height"],
                       input_addresses=input_addresses)
    cp = FakeCounterparty({}, assets, issuers=issuers)
    return Indexer(cfg, btc=btc, cp=cp, store=store)


REINSC_ASSET = {"asset": "RAREPEPE", "asset_id": "1234", "owner": "bc1pOwner",
                "divisible": False, "supply": 100, "asset_longname": None}


def test_reinscription_authorized_records_counter():
    height, txid = 800100, "1a" * 32
    owner = "bc1pOwner"
    block = make_block(height, txid, b"image/png", b"reinscribed!", asset=b"RAREPEPE")
    with tempfile.TemporaryDirectory() as tmp:
        # tx spends an input from the asset owner -> authorised
        idx = build_reinscribe_indexer(block, {"RAREPEPE": REINSC_ASSET},
                                       {"RAREPEPE": owner}, {owner}, tmp)
        assert idx.process_block(height) == 1
        row = idx.store.get_counter(0)
        assert row["asset"] == "RAREPEPE"
        assert row["reinscription"] == 0        # first counter on the asset = original
        assert row["cp_tx_index"] is None       # no Counterparty message
        assert row["xcp_burned"] is None
        assert row["owner"] == owner
        idx.close()


def test_reinscription_unauthorized_is_skipped():
    height, txid = 800101, "2b" * 32
    block = make_block(height, txid, b"image/png", b"x", asset=b"RAREPEPE")
    with tempfile.TemporaryDirectory() as tmp:
        # tx spends from someone who is NOT the owner -> rejected
        idx = build_reinscribe_indexer(block, {"RAREPEPE": REINSC_ASSET},
                                       {"RAREPEPE": "bc1pOwner"}, {"bc1pAttacker"}, tmp)
        assert idx.process_block(height) == 0
        assert idx.store.count() == 0
        idx.close()


def test_reinscription_unknown_asset_is_skipped():
    height, txid = 800102, "3c" * 32
    block = make_block(height, txid, b"image/png", b"x", asset=b"NOTREAL")
    with tempfile.TemporaryDirectory() as tmp:
        idx = build_reinscribe_indexer(block, {}, {}, {"bc1pWhoever"}, tmp)
        assert idx.process_block(height) == 0
        idx.close()


def test_reinscription_owner_with_no_valid_issuance_is_skipped():
    height, txid = 800103, "4d" * 32
    block = make_block(height, txid, b"image/png", b"x", asset=b"RAREPEPE")
    with tempfile.TemporaryDirectory() as tmp:
        # issuer_at_height returns None (no issuers map) -> cannot authorise
        idx = build_reinscribe_indexer(block, {"RAREPEPE": REINSC_ASSET},
                                       {}, {"bc1pOwner"}, tmp)
        assert idx.process_block(height) == 0
        idx.close()


def test_multiple_counters_per_asset_flags_later_as_reinscription():
    owner = "bc1pOwner"
    with tempfile.TemporaryDirectory() as tmp:
        h1, t1 = 800200, "aa" * 32
        b1 = make_block(h1, t1, b"image/png", b"one", asset=b"RAREPEPE")
        idx = build_reinscribe_indexer(b1, {"RAREPEPE": REINSC_ASSET},
                                       {"RAREPEPE": owner}, {owner}, tmp)
        assert idx.process_block(h1) == 1       # #0, original

        # a second reinscription of the SAME asset in a later block
        h2, t2 = 800201, "bb" * 32
        b2 = make_block(h2, t2, b"image/png", b"two", asset=b"RAREPEPE")
        idx.btc._blocks[h2] = b2
        idx.btc._tip = h2
        assert idx.process_block(h2) == 1       # #1, reinscription

        rows = idx.store.get_counters_by_asset("RAREPEPE")
        assert [r["number"] for r in rows] == [0, 1]
        assert rows[0]["reinscription"] == 0
        assert rows[1]["reinscription"] == 1
        idx.close()


def test_pepeme_reinscription_from_real_builder_is_indexed():
    """End-to-end: the EXACT tapscript `wallet inscribe --reinscribe --asset
    PEPEME` emits (via builder.build_inscription, not a hand-rolled script) must
    be parsed and recorded by the indexer, authorised by the PEPEME owner's
    input. This proves the builder -> envelope -> indexer contract for the real
    reinscription path, so the PEPEME counter is picked up once its reveal
    confirms and the owner is verified at the block height."""
    from counters import builder

    height, txid = 800300, "ee" * 32
    owner = "bc1pPepemeOwner"
    body = b"\xff\xd8\xff\xe0 fake jpeg bytes for PEPEME"
    # The same call the reinscribe command makes: asset tag = PEPEME.
    insc = builder.build_inscription(b"image/jpeg", body, asset=b"PEPEME")
    # Reveal's script-path witness = [sig, leaf tapscript, control block].
    witness = [(b"\x30" * 64).hex(), insc.leaf.hex(), insc.control_block.hex()]
    block = {
        "hash": f"blockhash{height}",
        "height": height,
        "tx": [
            {"txid": "coinbase", "vin": [{"coinbase": "00"}], "vout": []},
            {"txid": txid,
             "vin": [{"txid": "commit", "vout": 0, "txinwitness": witness}],
             "vout": []},
        ],
    }
    asset_info = {"asset": "PEPEME", "asset_id": "777", "owner": owner,
                  "divisible": False, "supply": 10, "asset_longname": None}
    with tempfile.TemporaryDirectory() as tmp:
        idx = build_reinscribe_indexer(block, {"PEPEME": asset_info},
                                       {"PEPEME": owner}, {owner}, tmp)
        assert idx.process_block(height) == 1, "PEPEME reinscription not recorded"
        row = idx.store.get_counter(0)
        assert row["asset"] == "PEPEME"
        assert row["content_type"] == "image/jpeg"
        assert row["content_length"] == len(body)
        assert row["content_sha256"] == hashlib.sha256(body).hexdigest()
        assert idx.store.read_blob(row["content_sha256"]) == body  # blob round-trips
        assert row["owner"] == owner
        assert row["reinscription"] == 0        # first counter on PEPEME = original
        assert row["cp_tx_index"] is None and row["xcp_burned"] is None  # no CP message
        idx.close()


def test_inscription_cost_sums_commit_and_reveal():
    """get_inscription_cost = reveal fee/vsize + the commit's, where the commit
    is the prevout of the envelope-bearing (script-path) input."""
    from counters.bitcoind import BitcoindClient

    leaf = counter_leaf_script(b"text/plain", b"hi")
    reveal_tx = {
        "vin": [
            {"txid": "change", "vout": 0, "txinwitness": ["aa" * 32]},        # not the envelope
            {"txid": "commitabc", "vout": 1, "txinwitness": taproot_witness(leaf)},  # envelope
        ],
        "vout": [],
        "size": 233,
    }

    class DuckBtc:
        def get_fee_and_size(self, txid, tx=None):
            return (200, 100) if txid == "commitabc" else (932, 233)

    fee, size = BitcoindClient.get_inscription_cost(DuckBtc(), "revealxyz", reveal_tx=reveal_tx)
    assert fee == 932 + 200 and size == 233 + 100


def test_height_lines_show_all_backend_heights():
    """The backend heights print above the bar, one per line: bitcoind's tip,
    then Counterparty's validated height against that tip."""
    idx = Indexer.__new__(Indexer)
    idx._btc_down, idx._cp_down = False, False
    idx._btc_tip, idx._cp_tip = 957_090, 957_063
    assert idx._height_lines() == [
        "bitcoin - 957090",
        "counterparty - 957063/957090",
    ]
    # Heights not yet known (backend never reached) are omitted, never crash.
    idx._btc_tip, idx._cp_tip = None, None
    assert idx._height_lines() == []
    idx._btc_tip, idx._cp_tip = 957_090, None
    assert idx._height_lines() == ["bitcoin - 957090"]


def test_height_lines_show_down_backends():
    """A backend whose last poll failed reads `down`, never a stale height."""
    idx = Indexer.__new__(Indexer)
    idx._btc_tip, idx._cp_tip = 957_090, 957_063

    idx._btc_down, idx._cp_down = True, False
    assert idx._height_lines() == ["bitcoin - down", "counterparty - 957063"]

    idx._btc_down, idx._cp_down = False, True
    assert idx._height_lines() == ["bitcoin - 957090", "counterparty - down"]

    idx._btc_down, idx._cp_down = True, True
    assert idx._height_lines() == ["bitcoin - down", "counterparty - down"]


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    print(f"\n{'OK' if failures == 0 else f'{failures} FAILED'}")
    raise SystemExit(1 if failures else 0)
