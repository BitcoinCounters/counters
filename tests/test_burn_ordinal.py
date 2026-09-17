"""burn-ordinal-sat: find a wallet's counterparty + ord inscriptions, burn one to a 1-sat OP_RETURN."""

import counters.__main__ as M
from counters.commands import burn_ordinal as B

REVEAL = "77" * 32
PLAIN = "3d" * 32


class _Row(dict):
    pass


class FakeStore:
    def __init__(self, events):
        self.events = events          # txid -> row

    def get_counter_by_event(self, txid, msg_index=0):
        return self.events.get(txid) if msg_index == 0 else None


class FakeCp:
    def __init__(self, attached=None, fail=False):
        self.attached, self.fail = attached or {}, fail

    def get_utxo_balances(self, utxo):
        if self.fail:
            raise B.CounterpartyError("not ready")
        return self.attached.get(utxo, [])


class FakeBtc:
    def __init__(self, utxos, style="counterparty/ord", sizes=None):
        self.utxos, self.style = utxos, style
        self.sizes = sizes or {}      # (n_in, n_out) -> vsize
        self.signed = []

    def wallet_call(self, wallet, method, params=None, timeout=-1.0):
        if method == "listunspent":
            return [u for u in self.utxos if u.get("confirmations", 1) >= params[0]]
        if method == "signrawtransactionwithwallet":
            self.signed.append(params[0])
            return {"complete": True, "hex": params[0]}
        if method == "getrawchangeaddress":
            return "bc1pchange"
        if method == "getaddressinfo":
            return {"scriptPubKey": "5120" + "cc" * 32}
        if method == "listdescriptors":
            return {"descriptors": []}
        raise AssertionError(method)

    def _call(self, method, params=None):
        if method == "getrawtransaction":
            return {"txid": params[0]}
        if method == "decoderawtransaction":
            raw = bytes.fromhex(params[0])
            n_in = raw[4]
            n_out = raw[5 + 41 * n_in]
            return {"vsize": self.sizes.get((n_in, n_out), 82)}
        raise AssertionError(method)


def _patch_style(monkeypatch, style="counterparty/ord"):
    monkeypatch.setattr(B, "envelope_style", lambda tx: style)


ROW = _Row(number=162, asset="EXPLORERTEST", asset_longname=None,
           content_type="image/png", content_length=195430)
INSC_UTXO = {"txid": REVEAL, "vout": 1, "amount": 0.00000546, "address": "bc1pme"}


def test_burn_output_is_ords():
    raw = B.serialize_unsigned([(REVEAL, 1)], [(1, B.BURN_SCRIPT_HEX)])
    b = bytes.fromhex(raw)
    assert len(b) == 65                           # the standardness floor, exactly
    assert b[5:37] == bytes.fromhex(REVEAL)[::-1] and b[37:41] == b"\x01\x00\x00\x00"
    assert raw.endswith("01" + "0100000000000000" + "05" + "6a00020000" + "00000000")


def test_candidates_are_ord_reveal_outputs_with_their_asset(monkeypatch):
    _patch_style(monkeypatch)
    btc = FakeBtc([INSC_UTXO,
                   {"txid": REVEAL, "vout": 0, "amount": 0.001},        # not vout 1
                   {"txid": PLAIN, "vout": 1, "amount": 0.001}])        # not a counter
    (c,) = B.find_candidates(btc, FakeCp(), FakeStore({REVEAL: ROW}), "w")
    assert (c["number"], c["asset"], c["value"]) == (162, "EXPLORERTEST", 546)
    assert c["inscription_id"] == REVEAL + "i0" and c["blocked"] is None


def test_native_envelope_is_not_a_candidate(monkeypatch):
    _patch_style(monkeypatch, "counterparty")
    assert B.find_candidates(FakeBtc([INSC_UTXO]), FakeCp(),
                             FakeStore({REVEAL: ROW}), "w") == []


def test_attached_or_unknown_balances_block_the_burn(monkeypatch):
    _patch_style(monkeypatch)
    cp = FakeCp(attached={f"{REVEAL}:1": [{"asset": "XCP", "quantity": 5}]})
    (c,) = B.find_candidates(FakeBtc([INSC_UTXO]), cp, FakeStore({REVEAL: ROW}), "w")
    assert "attached" in c["blocked"]
    (c,) = B.find_candidates(FakeBtc([INSC_UTXO]), FakeCp(fail=True),
                             FakeStore({REVEAL: ROW}), "w")
    assert "could not ask" in c["blocked"]


def test_target_matches_number_asset_or_inscription_id():
    c = {"number": 162, "asset": "EXPLORERTEST", "inscription_id": REVEAL + "i0",
         "outpoint": REVEAL + ":1", "txid": REVEAL}
    for t in ("162", "#162", "explorertest", REVEAL + "i0", REVEAL + ":1"):
        assert B._matches(c, t)
    assert not B._matches(c, "163")


def _cand():
    return {"txid": REVEAL, "value": 546, "outpoint": REVEAL + ":1"}


def test_postage_pays_the_fee_alone():
    btc = FakeBtc([INSC_UTXO])
    plan, err = B.build_burn(btc, FakeCp(), FakeStore({}), "w", _cand(), 2, set())
    assert err is None and plan["funding"] is None and plan["change_address"] is None
    assert plan["fee"] == 545                     # leftover below dust: all fee
    assert plan["hex"].endswith("01" + "0100000000000000" + "056a00020000" + "00000000")


def test_high_rate_adds_a_plain_coin_and_change():
    utxos = [INSC_UTXO,
             {"txid": "aa" * 32, "vout": 1, "amount": 0.01},    # a counter's reveal
             {"txid": PLAIN, "vout": 0, "amount": 0.001}]
    btc = FakeBtc(utxos, sizes={(2, 2): 183})
    store = FakeStore({"aa" * 32: ROW})
    plan, err = B.build_burn(btc, FakeCp(), store, "w", _cand(), 20,
                             {REVEAL + ":1"})
    assert err is None
    assert plan["funding"]["txid"] == PLAIN
    assert plan["fee"] == 20 * 183
    assert plan["change"] == 546 + 100_000 - 1 - 20 * 183
    raw = bytes.fromhex(plan["hex"])
    assert raw[5:37] == bytes.fromhex(REVEAL)[::-1]        # inscription stays input 0


def test_no_funding_coin_is_an_error():
    plan, err = B.build_burn(FakeBtc([INSC_UTXO]), FakeCp(), FakeStore({}), "w",
                             _cand(), 50, {REVEAL + ":1"})
    assert plan is None and "no plain confirmed coin" in err


def test_cli_routes_target_and_flags(monkeypatch):
    calls = []
    monkeypatch.setattr(B, "cmd_burn_ordinal_sat",
                        lambda *a, **k: calls.append((a, k)) or 0)
    assert M.main(["wallet", "--name", "counts", "burn-ordinal-sat", "162",
                   "--fee-rate", "3", "--dry-run"]) == 0
    (a, k), = calls
    assert a[1:] == ("counts", "162") and k["fee_rate"] == 3.0 and k["dry_run"]
    calls.clear()
    assert M.main(["wallet", "--name", "counts", "burn-ordinal-sat"]) == 0
    assert calls[0][0][2] is None
