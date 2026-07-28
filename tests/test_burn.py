"""Unit tests for `counters wallet burn` (the Counterparty destroy message).

No network/Core: Bitcoin Core and Counterparty clients are faked, and the
wallet-address lookup is monkeypatched. Covers asset resolution, the BTC
guard, source selection (auto and --source), balance checks, quantity
conversion, tag/fee-rate pass-through, the confirmation gate, and dry-run vs
broadcast.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import counters.commands.burn as B  # noqa: E402
from counters.config import Config  # noqa: E402
from counters.counterparty import CounterpartyClient  # noqa: E402

HOLDER = "bc1pHolderAddrxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
OTHER = "bc1pOtherAddrxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"


class FakeBtc:
    def __init__(self):
        self.sent = None

    def wallet_call(self, wallet, method, params=None, timeout=-1.0):
        assert method == "signrawtransactionwithwallet"
        return {"complete": True, "hex": "signed00"}

    def _call(self, method, params=None):
        if method == "testmempoolaccept":
            return [{"allowed": True, "txid": "tt"}]
        if method == "sendrawtransaction":
            self.sent = params[0]
            return "broadcasttxid"
        raise AssertionError(f"unexpected _call {method}")


class FakeCp:
    def __init__(self, info, balances):
        self.info = info
        self.balances = balances          # {address: [balance rows]}
        self.destroy_args = None

    def get_asset(self, asset):
        if self.info and asset.upper() == self.info["asset"].upper():
            return self.info
        return None

    def get_address_balances(self, address):
        return self.balances.get(address, [])

    def compose_destroy(self, source, asset, quantity, tag="", sat_per_vbyte=None):
        self.destroy_args = dict(source=source, asset=asset, quantity=quantity,
                                 tag=tag, sat_per_vbyte=sat_per_vbyte)
        return {"rawtransaction": "aa"}


def _patch(info, balances, addresses):
    fake_btc, fake_cp = FakeBtc(), FakeCp(info, balances)
    orig = (B.BitcoindClient, B.CounterpartyClient, B._wallet_addresses)
    B.BitcoindClient = lambda cfg: fake_btc
    B.CounterpartyClient = lambda cfg: fake_cp
    B._wallet_addresses = lambda btc, wallet: addresses
    return fake_btc, fake_cp, orig


def _restore(orig):
    B.BitcoindClient, B.CounterpartyClient, B._wallet_addresses = orig


def _asset(name="MYASSET", divisible=False):
    return {"asset": name, "asset_id": "123", "owner": HOLDER, "issuer": HOLDER,
            "divisible": divisible, "locked": False, "description": "hi",
            "supply": 100, "asset_longname": None}


def _bal(asset="MYASSET", quantity=100):
    return {"asset": asset, "asset_longname": None, "quantity": quantity}


# --- guards -------------------------------------------------------------------

def test_burn_rejects_btc():
    _btc, fake_cp, orig = _patch(_asset(), {}, [HOLDER])
    try:
        rc = B.cmd_burn(Config(), "me", "BTC", "1", assume_yes=True)
        assert rc == 1 and fake_cp.destroy_args is None
    finally:
        _restore(orig)


def test_burn_rejects_unknown_asset():
    _btc, fake_cp, orig = _patch(None, {}, [HOLDER])
    try:
        rc = B.cmd_burn(Config(), "me", "NOPE", "1", assume_yes=True)
        assert rc == 1 and fake_cp.destroy_args is None
    finally:
        _restore(orig)


def test_burn_rejects_fractional_indivisible():
    _btc, fake_cp, orig = _patch(_asset(divisible=False),
                                 {HOLDER: [_bal()]}, [HOLDER])
    try:
        rc = B.cmd_burn(Config(), "me", "MYASSET", "1.5", assume_yes=True)
        assert rc == 1 and fake_cp.destroy_args is None
    finally:
        _restore(orig)


def test_burn_rejects_insufficient_balance():
    _btc, fake_cp, orig = _patch(_asset(), {HOLDER: [_bal(quantity=5)]}, [HOLDER])
    try:
        rc = B.cmd_burn(Config(), "me", "MYASSET", "10", assume_yes=True)
        assert rc == 1 and fake_cp.destroy_args is None
    finally:
        _restore(orig)


def test_burn_refuses_without_confirmation_when_not_a_tty():
    # No --yes and stdin is not a terminal -> abort before composing.
    fake_btc, fake_cp, orig = _patch(_asset(), {HOLDER: [_bal()]}, [HOLDER])
    try:
        rc = B.cmd_burn(Config(), "me", "MYASSET", "10")
        assert rc == 1 and fake_cp.destroy_args is None and fake_btc.sent is None
    finally:
        _restore(orig)


# --- the destroy composed -----------------------------------------------------

def test_burn_composes_destroy_and_broadcasts():
    fake_btc, fake_cp, orig = _patch(_asset(), {HOLDER: [_bal()]}, [HOLDER])
    try:
        rc = B.cmd_burn(Config(), "me", "MYASSET", "10", assume_yes=True)
        assert rc == 0
        d = fake_cp.destroy_args
        assert d["source"] == HOLDER and d["asset"] == "MYASSET"
        assert d["quantity"] == 10 and d["tag"] == ""
        assert fake_btc.sent == "signed00"
    finally:
        _restore(orig)


def test_burn_divisible_quantity_scaled():
    fake_btc, fake_cp, orig = _patch(_asset(divisible=True),
                                     {HOLDER: [_bal(quantity=100_000_000)]}, [HOLDER])
    try:
        rc = B.cmd_burn(Config(), "me", "MYASSET", "0.5", assume_yes=True, dry_run=True)
        assert rc == 0
        assert fake_cp.destroy_args["quantity"] == 50_000_000
        assert fake_btc.sent is None            # dry-run: nothing broadcast
    finally:
        _restore(orig)


def test_burn_xcp_is_allowed():
    # Destroying XCP is a legitimate use of the message (unlike send, which
    # treats reserved assets as non-counters).
    fake_btc, fake_cp, orig = _patch(_asset("XCP", divisible=True),
                                     {HOLDER: [_bal("XCP", 50_000_000)]}, [HOLDER])
    try:
        rc = B.cmd_burn(Config(), "me", "XCP", "0.1", assume_yes=True, dry_run=True)
        assert rc == 0 and fake_cp.destroy_args["asset"] == "XCP"
    finally:
        _restore(orig)


def test_burn_passes_tag_and_fee_rate():
    _btc, fake_cp, orig = _patch(_asset(), {HOLDER: [_bal()]}, [HOLDER])
    try:
        rc = B.cmd_burn(Config(), "me", "MYASSET", "10", tag="bugs!",
                        fee_rate=0.5, assume_yes=True, dry_run=True)
        assert rc == 0
        assert fake_cp.destroy_args["tag"] == "bugs!"
        assert fake_cp.destroy_args["sat_per_vbyte"] == 0.5
    finally:
        _restore(orig)


# --- source selection -----------------------------------------------------------

def test_burn_source_flag_must_be_a_wallet_address():
    _btc, fake_cp, orig = _patch(_asset(), {OTHER: [_bal()]}, [HOLDER])
    try:
        rc = B.cmd_burn(Config(), "me", "MYASSET", "10", source=OTHER, assume_yes=True)
        assert rc == 1 and fake_cp.destroy_args is None
    finally:
        _restore(orig)


def test_burn_source_flag_pins_the_address():
    _btc, fake_cp, orig = _patch(
        _asset(), {HOLDER: [_bal(quantity=50)], OTHER: [_bal(quantity=100)]},
        [HOLDER, OTHER])
    try:
        rc = B.cmd_burn(Config(), "me", "MYASSET", "10", source=OTHER,
                        assume_yes=True, dry_run=True)
        assert rc == 0 and fake_cp.destroy_args["source"] == OTHER
    finally:
        _restore(orig)


def test_burn_auto_picks_an_address_that_can_cover_it():
    _btc, fake_cp, orig = _patch(
        _asset(), {HOLDER: [_bal(quantity=5)], OTHER: [_bal(quantity=100)]},
        [HOLDER, OTHER])
    try:
        rc = B.cmd_burn(Config(), "me", "MYASSET", "10", assume_yes=True, dry_run=True)
        assert rc == 0 and fake_cp.destroy_args["source"] == OTHER
    finally:
        _restore(orig)


# --- compose_destroy param handling ---------------------------------------------

class _CapCp(CounterpartyClient):
    def __init__(self):
        self.captured = None

    def _get(self, path, params=None):
        self.captured = (path, params)
        return {"result": {"rawtransaction": "00"}}


def test_compose_destroy_always_sends_tag_and_omits_fee_by_default():
    cp = _CapCp()
    cp.compose_destroy("addr", "FOO", 1000)
    path, params = cp.captured
    assert path == "/v2/addresses/addr/compose/destroy"
    assert params["tag"] == ""                   # the API requires the field
    assert params["quantity"] == 1000 and params["asset"] == "FOO"
    assert "sat_per_vbyte" not in params
    assert params["encoding"] == "opreturn"


def test_compose_destroy_passes_tag_and_whole_fee_rates_as_ints():
    cp = _CapCp()
    cp.compose_destroy("addr", "FOO", 1000, tag="why", sat_per_vbyte=2.0)
    _path, params = cp.captured
    assert params["tag"] == "why"
    assert params["sat_per_vbyte"] == 2 and isinstance(params["sat_per_vbyte"], int)


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
