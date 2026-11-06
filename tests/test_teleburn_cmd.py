"""Unit tests for `counters teleburn-address` and `counters wallet teleburn`.

No network/Core: bitcoind, Counterparty and the index are faked, and the
wallet-address lookup is monkeypatched. Covers identifier resolution, the
already-burned and not-owner guards, the circulating-supply rule and
--partial, the two composed messages and their order, lock handling, the
confirmation gate, and dry-run vs broadcast.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import counters.commands.teleburn as T  # noqa: E402
from counters import teleburn  # noqa: E402
from counters.config import Config  # noqa: E402

OWNER = "bc1pOwnerAddrxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
HOLDER = "bc1pHolderAddrxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
SINK = teleburn.teleburn_address("PEPECASH")


class FakeBtc:
    def __init__(self):
        self.sent = []

    def wallet_call(self, wallet, method, params=None, timeout=-1.0):
        if method == "listunspent":
            return [{"address": a, "amount": 0.01, "spendable": True} for a in (OWNER, HOLDER)]
        assert method == "signrawtransactionwithwallet"
        return {"complete": True, "hex": "signed-" + params[0]}

    def _call(self, method, params=None):
        if method == "validateaddress":
            return {"isvalid": params[0].startswith("bc1")}
        if method == "testmempoolaccept":
            return [{"allowed": True, "txid": "tt"}]
        if method == "sendrawtransaction":
            self.sent.append(params[0])
            return "txid-" + params[0]
        raise AssertionError(f"unexpected _call {method}")


class FakeCp:
    def __init__(self, info, balances, sink=0):
        self.info, self.balances, self.sink = info, balances, sink
        self.sends, self.issuances = [], []

    def get_asset(self, asset):
        return self.info if self.info and asset.upper() == self.info["asset"] else None

    def get_address_balances(self, address):
        q = self.balances.get(address, 0)
        return [{"asset": "PEPECASH", "quantity": q}] if q else []

    def get_address_balance(self, address, asset):
        assert address == SINK and asset == "PEPECASH"
        return self.sink

    def compose_send(self, source, asset, quantity, destination, sat_per_vbyte=None):
        self.sends.append(dict(source=source, asset=asset, quantity=quantity,
                               destination=destination, sat_per_vbyte=sat_per_vbyte))
        return {"rawtransaction": "sendtx"}

    def compose_issuance(self, **kw):
        self.issuances.append(kw)
        return {"rawtransaction": "issuetx"}


class FakeStore:
    def __init__(self, rows):
        self.rows = rows                  # [{"number", "asset"}]

    def find(self, identifier):
        for r in self.rows:
            if identifier == str(r["number"]) or identifier == r["asset"]:
                return r
        return None

    def get_counters_by_asset(self, asset):
        return [r for r in self.rows if r["asset"] == asset]

    def close(self):
        pass


def _info(owner=OWNER, supply=100, locked=False):
    return {"asset": "PEPECASH", "owner": owner, "issuer": OWNER, "supply": supply,
            "divisible": False, "locked": locked}


def _patch(monkeypatch, info, balances=None, sink=0, rows=None, addresses=(OWNER, HOLDER)):
    btc, cp = FakeBtc(), FakeCp(info, balances or {}, sink)
    rows = rows if rows is not None else [{"number": 7, "asset": "PEPECASH"}]
    monkeypatch.setattr(T, "BitcoindClient", lambda cfg: btc)
    monkeypatch.setattr(T, "CounterpartyClient", lambda cfg: cp)
    monkeypatch.setattr(T, "Store", lambda cfg: FakeStore(rows))
    import counters.commands.issue as I
    monkeypatch.setattr(I, "_wallet_addresses", lambda b, w: list(addresses))
    import counters.commands.send as S
    monkeypatch.setattr(S, "_wallet_addresses", lambda b, w: list(addresses))
    import counters.commands.funding as F
    monkeypatch.setattr(F, "_wallet_addresses", lambda b, w: list(addresses), raising=False)
    return btc, cp


CFG = Config()


# --- teleburn-address --------------------------------------------------------

def test_address_by_counter_number_and_by_name(monkeypatch, capsys):
    _patch(monkeypatch, _info())
    assert T.cmd_teleburn_address(CFG, "#7") == 0
    out = capsys.readouterr().out
    assert SINK in out and "#7" in out and "PEPECASH" in out
    assert T.cmd_teleburn_address(CFG, "PEPECASH", as_json=True) == 0
    j = json.loads(capsys.readouterr().out)
    assert j["address"] == SINK and j["counters"] == [7]
    assert j["scriptPubKey"] == teleburn.teleburn_spk("PEPECASH").hex()
    assert j["tag"] == "CountersTeleburn"


def test_address_for_an_asset_that_is_not_a_counter_comes_from_core(monkeypatch, capsys):
    _patch(monkeypatch, {"asset": "XCP", "owner": None}, rows=[])
    assert T.cmd_teleburn_address(CFG, "xcp") == 0
    assert teleburn.teleburn_address("XCP") in capsys.readouterr().out


def test_address_on_testnet_and_bad_network(monkeypatch, capsys):
    _patch(monkeypatch, _info())
    assert T.cmd_teleburn_address(CFG, "PEPECASH", network="testnet") == 0
    assert teleburn.teleburn_address("PEPECASH", "tb") in capsys.readouterr().out
    assert T.cmd_teleburn_address(CFG, "PEPECASH", network="dogecoin") == 1


def test_unknown_counter_number_fails(monkeypatch, capsys):
    _patch(monkeypatch, _info())
    assert T.cmd_teleburn_address(CFG, "99") == 1
    assert "no counter #99" in capsys.readouterr().err


# --- wallet teleburn ---------------------------------------------------------

def test_full_burn_sends_then_locks_and_transfers(monkeypatch, capsys):
    btc, cp = _patch(monkeypatch, _info(), balances={HOLDER: 100})
    assert T.cmd_teleburn(CFG, "w", "7", assume_yes=True, fee_rate=2.0) == 0
    assert [s["destination"] for s in cp.sends] == [SINK]
    assert cp.sends[0]["source"] == HOLDER and cp.sends[0]["quantity"] == 100
    assert cp.sends[0]["sat_per_vbyte"] == 2.0
    iss = cp.issuances[0]
    assert iss["source"] == OWNER and iss["quantity"] == 0
    assert iss["lock"] is True and iss["transfer_destination"] == SINK
    assert iss["description"] is None
    assert btc.sent == ["signed-sendtx", "signed-issuetx"]      # supply first
    out = capsys.readouterr().out
    assert "step 1" in out and "step 2" in out and "teleburned PEPECASH" in out


def test_already_locked_does_not_lock_again(monkeypatch):
    _, cp = _patch(monkeypatch, _info(locked=True), balances={OWNER: 100})
    assert T.cmd_teleburn(CFG, "w", "PEPECASH", assume_yes=True) == 0
    assert cp.issuances[0]["lock"] is False


def test_nothing_circulating_skips_the_send(monkeypatch, capsys):
    btc, cp = _patch(monkeypatch, _info(supply=0))
    assert T.cmd_teleburn(CFG, "w", "PEPECASH", assume_yes=True) == 0
    assert cp.sends == [] and len(cp.issuances) == 1
    assert btc.sent == ["signed-issuetx"]
    assert "supply    : none" in capsys.readouterr().out


def test_supply_already_at_the_sink_skips_the_send(monkeypatch):
    _, cp = _patch(monkeypatch, _info(supply=100), sink=100)
    assert T.cmd_teleburn(CFG, "w", "PEPECASH", assume_yes=True) == 0
    assert cp.sends == [] and len(cp.issuances) == 1


def test_units_held_elsewhere_need_partial(monkeypatch, capsys):
    btc, cp = _patch(monkeypatch, _info(supply=100), balances={HOLDER: 60})
    assert T.cmd_teleburn(CFG, "w", "PEPECASH", assume_yes=True) == 1
    err = capsys.readouterr().err
    assert "--partial" in err and cp.sends == [] and btc.sent == []
    assert T.cmd_teleburn(CFG, "w", "PEPECASH", assume_yes=True, partial=True) == 0
    assert cp.sends[0]["quantity"] == 60
    assert "PARTIAL" in capsys.readouterr().out
    assert btc.sent == ["signed-sendtx", "signed-issuetx"]


def test_already_teleburned_is_a_no_op(monkeypatch, capsys):
    btc, cp = _patch(monkeypatch, _info(owner=SINK))
    assert T.cmd_teleburn(CFG, "w", "PEPECASH", assume_yes=True) == 0
    assert "already teleburned" in capsys.readouterr().out
    assert cp.sends == [] and cp.issuances == [] and btc.sent == []


def test_not_the_owner_is_refused(monkeypatch, capsys):
    btc, cp = _patch(monkeypatch, _info(owner="bc1pSomebodyElse"), balances={HOLDER: 100})
    assert T.cmd_teleburn(CFG, "w", "PEPECASH", assume_yes=True) == 1
    assert "issuance rights" in capsys.readouterr().err
    assert cp.sends == [] and btc.sent == []


def test_dry_run_signs_both_and_broadcasts_neither(monkeypatch, capsys):
    btc, cp = _patch(monkeypatch, _info(), balances={OWNER: 100})
    assert T.cmd_teleburn(CFG, "w", "PEPECASH", dry_run=True) == 0
    assert len(cp.sends) == 1 and len(cp.issuances) == 1 and btc.sent == []
    assert capsys.readouterr().out.count("[dry-run]") == 2


def test_declined_confirmation_composes_nothing(monkeypatch, capsys):
    btc, cp = _patch(monkeypatch, _info(), balances={OWNER: 100})
    monkeypatch.setattr(T, "_confirm_prompt", lambda prompt: False)
    assert T.cmd_teleburn(CFG, "w", "PEPECASH") == 1
    assert cp.sends == [] and cp.issuances == [] and btc.sent == []


def test_a_failed_send_stops_before_the_transfer(monkeypatch, capsys):
    btc, cp = _patch(monkeypatch, _info(), balances={OWNER: 100})
    monkeypatch.setattr(T, "_sign_and_broadcast", lambda *a, **k: 1)
    assert T.cmd_teleburn(CFG, "w", "PEPECASH", assume_yes=True) == 1
    assert len(cp.sends) == 1 and cp.issuances == []
    assert "stopping before the ownership transfer" in capsys.readouterr().err
