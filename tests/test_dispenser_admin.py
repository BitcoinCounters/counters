"""Unit tests for the dispenser operator commands (no network/Core needed).

open-dispenser / refill-dispenser / close-dispenser compose the `dispenser`
message, whose consensus quirks are easy to violate silently: quantities are
raw units, a refill must restate the live terms EXACTLY, and a close still
carries the three quantity fields (as zeros). These pin all of that, plus
every refusal that stops an escrow leaving the wallet for nothing.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import counters.commands.dispenser as D  # noqa: E402
from counters.config import Config  # noqa: E402
from counters.counterparty import CounterpartyClient, CounterpartyError  # noqa: E402

SOURCE = "bc1pSourcexxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
OTHER = "bc1pOtherxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"


def _row(asset="PEPECASH", rate=2780, give=100_000_000, remaining=2_800_000_000,
         status=0, divisible=True, close_block=None):
    row = {"asset": asset, "satoshirate": rate, "give_quantity": give,
           "give_remaining": remaining, "status": status,
           "asset_info": {"divisible": divisible, "asset_longname": None}}
    if close_block is not None:
        row["close_block_index"] = close_block
    return row


class FakeBtc:
    def __init__(self):
        self.sent = None

    def _call(self, method, params=None):
        if method == "testmempoolaccept":
            return [{"allowed": True}]
        if method == "sendrawtransaction":
            self.sent = params[0]
            return "dispensertxid"
        raise AssertionError(f"unexpected _call {method}")

    def wallet_call(self, wallet, method, params=None, timeout=-1.0):
        if method == "listunspent":   # ensure_funded: the source pays its own fee
            return [{"address": SOURCE, "amount": 0.01, "spendable": True}]
        assert method == "signrawtransactionwithwallet"
        return {"complete": True, "hex": "signed00"}


class FakeCp:
    def __init__(self, asset_info=None, rows=None, balance=10**12):
        self.asset_info = asset_info if asset_info is not None else {
            "asset": "PEPECASH", "divisible": True}
        self.rows = rows or {}          # address -> dispenser row
        self.balance = balance          # raw units of the asset, any address
        self.compose_kwargs = None

    def get_asset(self, asset):
        return self.asset_info

    def get_dispenser(self, address, asset):
        return self.rows.get(address)

    def get_address_dispensers(self, address):
        return [self.rows[address]] if address in self.rows else []

    def get_address_balances(self, address):
        return [{"asset": (self.asset_info or {}).get("asset"),
                 "quantity": self.balance}]

    def compose_dispenser(self, source, asset, give_quantity, escrow_quantity,
                          mainchainrate, status=0, sat_per_vbyte=None):
        self.compose_kwargs = dict(
            source=source, asset=asset, give_quantity=give_quantity,
            escrow_quantity=escrow_quantity, mainchainrate=mainchainrate,
            status=status, sat_per_vbyte=sat_per_vbyte)
        return {"rawtransaction": "aa", "btc_fee": 492}


def _patch(btc, cp):
    orig = (D.BitcoindClient, D.CounterpartyClient, D._wallet_addresses,
            D._find_source)
    D.BitcoindClient = lambda cfg: btc
    D.CounterpartyClient = lambda cfg: cp
    D._wallet_addresses = lambda b, w: [SOURCE]
    D._find_source = lambda b, c, w, a, need: (SOURCE, cp.balance)
    return orig


def _restore(orig):
    (D.BitcoindClient, D.CounterpartyClient, D._wallet_addresses,
     D._find_source) = orig


# --- open-dispenser ---------------------------------------------------------

def test_open_converts_human_amounts_to_raw_units():
    # Divisible asset: escrow "28", lot "1", price 2780 sat.
    btc, cp = FakeBtc(), FakeCp()
    orig = _patch(btc, cp)
    try:
        rc = D.cmd_open_dispenser(Config(), "me", "PEPECASH", "28", 2780,
                                  lot="1", assume_yes=True)
        assert rc == 0
        k = cp.compose_kwargs
        assert k["give_quantity"] == 100_000_000
        assert k["escrow_quantity"] == 2_800_000_000
        assert k["mainchainrate"] == 2780 and k["status"] == 0
        assert k["source"] == SOURCE and btc.sent == "signed00"
    finally:
        _restore(orig)


def test_open_default_lot_is_the_whole_escrow():
    btc, cp = FakeBtc(), FakeCp()
    orig = _patch(btc, cp)
    try:
        rc = D.cmd_open_dispenser(Config(), "me", "PEPECASH", "5", 9999,
                                  assume_yes=True)
        assert rc == 0
        k = cp.compose_kwargs
        assert k["give_quantity"] == k["escrow_quantity"] == 500_000_000
    finally:
        _restore(orig)


def test_open_refuses_btc():
    btc, cp = FakeBtc(), FakeCp()
    orig = _patch(btc, cp)
    try:
        rc = D.cmd_open_dispenser(Config(), "me", "BTC", "1", 2780, assume_yes=True)
        assert rc == 1 and cp.compose_kwargs is None
    finally:
        _restore(orig)


def test_open_allows_xcp():
    # XCP is in RESERVED_ASSETS for `send`, but XCP dispensers are legal —
    # regression guard against blindly reusing that filter here.
    btc, cp = FakeBtc(), FakeCp(asset_info={"asset": "XCP", "divisible": True})
    orig = _patch(btc, cp)
    try:
        rc = D.cmd_open_dispenser(Config(), "me", "XCP", "1", 2780, assume_yes=True)
        assert rc == 0 and cp.compose_kwargs["asset"] == "XCP"
    finally:
        _restore(orig)


def test_open_refuses_lot_larger_than_escrow():
    btc, cp = FakeBtc(), FakeCp()
    orig = _patch(btc, cp)
    try:
        rc = D.cmd_open_dispenser(Config(), "me", "PEPECASH", "1", 2780,
                                  lot="2", assume_yes=True)
        assert rc == 1 and cp.compose_kwargs is None
    finally:
        _restore(orig)


def test_open_refuses_a_non_positive_price():
    btc, cp = FakeBtc(), FakeCp()
    orig = _patch(btc, cp)
    try:
        assert D.cmd_open_dispenser(Config(), "me", "PEPECASH", "1", 0,
                                    assume_yes=True) == 1
        assert cp.compose_kwargs is None
    finally:
        _restore(orig)


def test_open_warns_but_composes_a_non_multiple_escrow():
    # 2.5 escrowed in 1-lots: the 0.5 remainder is stranded, not lost — warn
    # and proceed rather than refuse.
    btc, cp = FakeBtc(), FakeCp()
    orig = _patch(btc, cp)
    try:
        rc = D.cmd_open_dispenser(Config(), "me", "PEPECASH", "2.5", 2780,
                                  lot="1", assume_yes=True)
        assert rc == 0 and cp.compose_kwargs["escrow_quantity"] == 250_000_000
    finally:
        _restore(orig)


def test_open_refuses_when_a_dispenser_is_already_open():
    btc, cp = FakeBtc(), FakeCp(rows={SOURCE: _row()})
    orig = _patch(btc, cp)
    try:
        rc = D.cmd_open_dispenser(Config(), "me", "PEPECASH", "1", 2780,
                                  assume_yes=True)
        assert rc == 1 and cp.compose_kwargs is None
    finally:
        _restore(orig)


def test_open_refuses_while_a_close_is_pending():
    btc, cp = FakeBtc(), FakeCp(rows={SOURCE: _row(status=11, close_block=960_005)})
    orig = _patch(btc, cp)
    try:
        rc = D.cmd_open_dispenser(Config(), "me", "PEPECASH", "1", 2780,
                                  assume_yes=True)
        assert rc == 1 and cp.compose_kwargs is None
    finally:
        _restore(orig)


def test_open_refuses_an_explicit_source_with_too_little_balance():
    btc, cp = FakeBtc(), FakeCp(balance=50_000_000)   # 0.5, wanting 1
    orig = _patch(btc, cp)
    try:
        rc = D.cmd_open_dispenser(Config(), "me", "PEPECASH", "1", 2780,
                                  source=SOURCE, assume_yes=True)
        assert rc == 1 and cp.compose_kwargs is None
    finally:
        _restore(orig)


def test_open_declining_the_prompt_broadcasts_nothing():
    btc, cp = FakeBtc(), FakeCp()
    orig, orig_confirm = _patch(btc, cp), D._confirm_admin
    D._confirm_admin = lambda q: False
    try:
        rc = D.cmd_open_dispenser(Config(), "me", "PEPECASH", "1", 2780)
        assert rc == 0 and btc.sent is None          # composed, never broadcast
    finally:
        D._confirm_admin = orig_confirm
        _restore(orig)


# --- refill-dispenser -------------------------------------------------------

def test_refill_copies_the_live_terms_verbatim():
    # The user names only the amount; give_quantity and satoshirate come from
    # the on-chain row (Counterparty rejects a refill with different terms).
    btc, cp = FakeBtc(), FakeCp(rows={SOURCE: _row(rate=41, give=25_000_000)})
    orig = _patch(btc, cp)
    try:
        rc = D.cmd_refill_dispenser(Config(), "me", "PEPECASH", "5", assume_yes=True)
        assert rc == 0
        k = cp.compose_kwargs
        assert k["give_quantity"] == 25_000_000 and k["mainchainrate"] == 41
        assert k["escrow_quantity"] == 500_000_000 and k["status"] == 0
    finally:
        _restore(orig)


def test_refill_refuses_without_an_open_dispenser():
    btc, cp = FakeBtc(), FakeCp()
    orig = _patch(btc, cp)
    try:
        rc = D.cmd_refill_dispenser(Config(), "me", "PEPECASH", "5", assume_yes=True)
        assert rc == 1 and cp.compose_kwargs is None
    finally:
        _restore(orig)


def test_refill_refuses_a_closing_dispenser():
    btc, cp = FakeBtc(), FakeCp(rows={SOURCE: _row(status=11, close_block=960_005)})
    orig = _patch(btc, cp)
    try:
        rc = D.cmd_refill_dispenser(Config(), "me", "PEPECASH", "5", assume_yes=True)
        assert rc == 1 and cp.compose_kwargs is None
    finally:
        _restore(orig)


# --- close-dispenser --------------------------------------------------------

def test_close_composes_the_zeros_convention():
    btc, cp = FakeBtc(), FakeCp(rows={SOURCE: _row()})
    orig = _patch(btc, cp)
    try:
        rc = D.cmd_close_dispenser(Config(), "me", "PEPECASH", assume_yes=True)
        assert rc == 0
        k = cp.compose_kwargs
        assert (k["give_quantity"], k["escrow_quantity"], k["mainchainrate"]) == (0, 0, 0)
        assert k["status"] == 10 and btc.sent == "signed00"
    finally:
        _restore(orig)


def test_close_refuses_an_already_closing_dispenser():
    btc, cp = FakeBtc(), FakeCp(rows={SOURCE: _row(status=11, close_block=960_005)})
    orig = _patch(btc, cp)
    try:
        rc = D.cmd_close_dispenser(Config(), "me", "PEPECASH", assume_yes=True)
        assert rc == 1 and cp.compose_kwargs is None
    finally:
        _restore(orig)


# --- the client method ------------------------------------------------------

class _CapCp(CounterpartyClient):
    def __init__(self):
        self.captured = None

    def _post(self, path, params=None):
        self.captured = (path, params)
        return {"result": {"rawtransaction": "00"}}


def test_compose_dispenser_posts_to_the_dispenser_endpoint():
    cp = _CapCp()
    cp.compose_dispenser("src", "FOO", 1, 10, 2780, status=0)
    path, params = cp.captured
    assert path == "/v2/addresses/src/compose/dispenser"
    assert params["give_quantity"] == 1 and params["escrow_quantity"] == 10
    assert params["mainchainrate"] == 2780 and params["status"] == 0
    assert "sat_per_vbyte" not in params               # omitted by default


def test_compose_dispenser_normalises_whole_fee_rate_to_int():
    cp = _CapCp()
    cp.compose_dispenser("src", "FOO", 1, 10, 2780, sat_per_vbyte=1.0)
    assert isinstance(cp.captured[1]["sat_per_vbyte"], int)   # 1, not 1.0
    cp.compose_dispenser("src", "FOO", 1, 10, 2780, sat_per_vbyte=1.5)
    assert cp.captured[1]["sat_per_vbyte"] == 1.5


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
