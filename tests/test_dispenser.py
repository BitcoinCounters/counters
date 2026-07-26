"""Unit tests for `counters wallet buy-from-dispenser` (no network/Core needed).

The point of the command is that a dispenser purchase must carry a `dispense`
message — a bare BTC payment has done nothing since block 866,000 — so these
pin the composed quantity, the lot arithmetic, and every refusal that stops a
payment going out when it would not buy anything.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import counters.commands.dispenser as D  # noqa: E402
from counters.config import Config  # noqa: E402

DISP = "bc1q44s7vmurwks9vf50txdj04uvujedy6f37z3yk5"
SOURCE = "bc1pSourcexxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"


def _dispenser(asset="XCP", rate=2780, give=100_000_000, remaining=2_800_000_000,
               status=0, divisible=True):
    return {"asset": asset, "satoshirate": rate, "give_quantity": give,
            "give_remaining": remaining, "status": status,
            "asset_info": {"divisible": divisible, "asset_longname": None}}


class FakeBtc:
    def __init__(self):
        self.sent = None

    def _call(self, method, params=None):
        if method == "validateaddress":
            return {"isvalid": params[0].startswith("bc1")}
        if method == "testmempoolaccept":
            return [{"allowed": True}]
        if method == "sendrawtransaction":
            self.sent = params[0]
            return "dispensetxid"
        raise AssertionError(f"unexpected _call {method}")

    def wallet_call(self, wallet, method, params=None, timeout=-1.0):
        assert method == "signrawtransactionwithwallet"
        return {"complete": True, "hex": "signed00"}


class FakeCp:
    def __init__(self, dispensers):
        self.dispensers = dispensers
        self.compose_kwargs = None

    def get_address_dispensers(self, address):
        return list(self.dispensers)

    def compose_dispense(self, source, dispenser, quantity, sat_per_vbyte=None):
        self.compose_kwargs = dict(source=source, dispenser=dispenser,
                                   quantity=quantity, sat_per_vbyte=sat_per_vbyte)
        return {"rawtransaction": "aa", "btc_fee": 492}


def _patch(btc, cp, spendable):
    orig = (D.BitcoindClient, D.CounterpartyClient, D._spendable_addresses)
    D.BitcoindClient = lambda cfg: btc
    D.CounterpartyClient = lambda cfg: cp
    D._spendable_addresses = lambda b, w: spendable
    return orig


def _restore(orig):
    D.BitcoindClient, D.CounterpartyClient, D._spendable_addresses = orig


# --- dispenser selection ----------------------------------------------------

def test_open_dispensers_skips_closed_ones():
    cp = FakeCp([_dispenser(status=0), _dispenser(asset="PEPE", status=10)])
    assert [d["asset"] for d in D._open_dispensers(cp, DISP, None)] == ["XCP"]


def test_open_dispensers_filters_by_asset():
    cp = FakeCp([_dispenser(), _dispenser(asset="PEPE", divisible=False)])
    assert [d["asset"] for d in D._open_dispensers(cp, DISP, "pepe")] == ["PEPE"]


def test_describe_reads_as_terms():
    assert D._describe(_dispenser()) == "1 XCP for 2780 sat (28 XCP remaining)"


# --- buying -----------------------------------------------------------------

def test_dry_run_composes_the_listed_price_and_does_not_broadcast():
    btc, cp = FakeBtc(), FakeCp([_dispenser()])
    orig = _patch(btc, cp, {SOURCE: 100_000})
    try:
        rc = D.cmd_buy_from_dispenser(Config(), "me", DISP, "1", fee_rate=2.0, dry_run=True)
        assert rc == 0
        k = cp.compose_kwargs
        assert k["dispenser"] == DISP and k["source"] == SOURCE
        assert k["quantity"] == 2780 and k["sat_per_vbyte"] == 2.0
        assert btc.sent is None
    finally:
        _restore(orig)


def test_buying_several_lots_multiplies_the_payment():
    btc, cp = FakeBtc(), FakeCp([_dispenser()])
    orig = _patch(btc, cp, {SOURCE: 100_000})
    try:
        rc = D.cmd_buy_from_dispenser(Config(), "me", DISP, "3", assume_yes=True)
        assert rc == 0 and cp.compose_kwargs["quantity"] == 3 * 2780
        assert btc.sent == "signed00"
    finally:
        _restore(orig)


def test_refuses_more_than_the_dispenser_has_left():
    btc, cp = FakeBtc(), FakeCp([_dispenser(remaining=100_000_000)])   # 1 XCP left
    orig = _patch(btc, cp, {SOURCE: 100_000})
    try:
        rc = D.cmd_buy_from_dispenser(Config(), "me", DISP, "2")
        assert rc == 1 and cp.compose_kwargs is None and btc.sent is None
    finally:
        _restore(orig)


def test_refuses_a_part_lot():
    # 1 XCP per lot: 0.5 would be paid for but only 1 whole lot ever dispensed.
    btc, cp = FakeBtc(), FakeCp([_dispenser()])
    orig = _patch(btc, cp, {SOURCE: 100_000})
    try:
        rc = D.cmd_buy_from_dispenser(Config(), "me", DISP, "0.5")
        assert rc == 1 and cp.compose_kwargs is None and btc.sent is None
    finally:
        _restore(orig)


def test_amount_is_in_asset_units_not_satoshis():
    # An indivisible 10-per-lot dispenser: "20" means 20 tokens = 2 lots.
    btc, cp = FakeBtc(), FakeCp([_dispenser(asset="PEPE", give=10, remaining=100,
                                            divisible=False)])
    orig = _patch(btc, cp, {SOURCE: 100_000})
    try:
        rc = D.cmd_buy_from_dispenser(Config(), "me", DISP, "20", assume_yes=True)
        assert rc == 0 and cp.compose_kwargs["quantity"] == 2 * 2780
    finally:
        _restore(orig)


def test_declining_the_prompt_buys_nothing():
    btc, cp = FakeBtc(), FakeCp([_dispenser()])
    orig, orig_confirm = _patch(btc, cp, {SOURCE: 100_000}), D._confirm
    asked = []
    D._confirm = lambda what, total: asked.append((what, total)) or False
    try:
        rc = D.cmd_buy_from_dispenser(Config(), "me", DISP, "1")
        # The prompt states the payout and the FULL cost: price + miner fee.
        assert asked == [("1 XCP", 2780 + 492)]
        assert rc == 0 and btc.sent is None          # composed, never broadcast
    finally:
        D._confirm = orig_confirm
        _restore(orig)


def test_confirming_the_prompt_broadcasts():
    btc, cp = FakeBtc(), FakeCp([_dispenser()])
    orig, orig_confirm = _patch(btc, cp, {SOURCE: 100_000}), D._confirm
    D._confirm = lambda what, total: True
    try:
        assert D.cmd_buy_from_dispenser(Config(), "me", DISP, "1") == 0
        assert btc.sent == "signed00"
    finally:
        D._confirm = orig_confirm
        _restore(orig)


def test_refuses_when_no_open_dispenser():
    btc, cp = FakeBtc(), FakeCp([_dispenser(status=10)])
    orig = _patch(btc, cp, {SOURCE: 100_000})
    try:
        rc = D.cmd_buy_from_dispenser(Config(), "me", DISP, "1")
        assert rc == 1 and cp.compose_kwargs is None
    finally:
        _restore(orig)


def test_ambiguous_address_requires_asset():
    btc, cp = FakeBtc(), FakeCp([_dispenser(), _dispenser(asset="PEPE")])
    orig = _patch(btc, cp, {SOURCE: 100_000})
    try:
        rc = D.cmd_buy_from_dispenser(Config(), "me", DISP, "1")
        assert rc == 1 and cp.compose_kwargs is None
        # ...and naming the asset resolves it
        assert D.cmd_buy_from_dispenser(Config(), "me", DISP, "1", asset="PEPE", assume_yes=True) == 0
        assert cp.compose_kwargs["quantity"] == 2780
    finally:
        _restore(orig)


def test_refuses_an_invalid_dispenser_address():
    btc, cp = FakeBtc(), FakeCp([_dispenser()])
    orig = _patch(btc, cp, {SOURCE: 100_000})
    try:
        rc = D.cmd_buy_from_dispenser(Config(), "me", "not-an-address", "1")
        assert rc == 1 and cp.compose_kwargs is None
    finally:
        _restore(orig)


def test_refuses_when_no_single_address_can_cover_the_payment():
    # Funds split across addresses: a dispense is composed from one source.
    btc, cp = FakeBtc(), FakeCp([_dispenser()])
    orig = _patch(btc, cp, {SOURCE: 1000, "bc1pOther": 1500})
    try:
        rc = D.cmd_buy_from_dispenser(Config(), "me", DISP, "1")
        assert rc == 1 and cp.compose_kwargs is None and btc.sent is None
    finally:
        _restore(orig)


def test_refuses_a_zero_amount():
    btc, cp = FakeBtc(), FakeCp([_dispenser()])
    orig = _patch(btc, cp, {SOURCE: 100_000})
    try:
        assert D.cmd_buy_from_dispenser(Config(), "me", DISP, "0") == 1
        assert cp.compose_kwargs is None
    finally:
        _restore(orig)


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
