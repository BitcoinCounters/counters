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
            "give_remaining": remaining, "status": status, "source": DISP,
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

    def get_asset(self, asset):
        return {"asset": asset.upper()} if asset.upper() in {"XCP", "PEPE"} else None

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


# --- browsing: the command called with nothing to buy -----------------------

def test_price_sorts_on_the_number_not_its_rendering():
    # 12,000 sorts before 9,800 as text, and a lot of ten at 98,000 sats is
    # 9,800 each rather than the cheapest thing on the shelf.
    cheap = _dispenser(asset="CHEAP", rate=12_000, give=100_000_000)          # 12,000/unit
    lots = _dispenser(asset="LOTS", rate=98_000, give=1_000_000_000)          # 9,800/unit
    assert [d["asset"] for d in D._by_price([cheap, lots])] == ["LOTS", "CHEAP"]


def test_an_oracle_dispensers_rate_is_fiat_and_never_the_price():
    # `satoshirate` 888 on an oracle dispenser is $8.88; the satoshis it wants
    # are what Core resolved from the feed. Sorting on the rate would put this
    # at the top of a cheapest-first list at a fourteenth of its price, and
    # paying it would underpay: "not enough BTC to trigger dispenser".
    oracle = _dispenser(rate=888, give=100_000_000)
    oracle["oracle_address"] = "1BTCUSDupRmeaNferCFoxmF6bYV5cAR2X2"
    oracle["satoshi_price"] = 12_846
    assert D._sats_per_lot(oracle) == 12_846
    assert D._unit_price(oracle, True) == 12_846.0
    assert "oracle-priced" in D._terms(oracle)

    plain = _dispenser(rate=5_000, give=100_000_000)
    assert D._sats_per_lot(plain) == 5_000          # no oracle, no difference
    assert D._unit_price(plain, True) == 5_000.0


def test_an_oracle_dispenser_does_not_undercut_a_cheaper_plain_one():
    oracle = _dispenser(asset="ORACLE", rate=888, give=100_000_000)
    oracle["oracle_address"] = "1BTCUSDupRmeaNferCFoxmF6bYV5cAR2X2"
    oracle["satoshi_price"] = 12_846
    plain = _dispenser(asset="PLAIN", rate=5_200, give=100_000_000)
    assert [d["asset"] for d in D._by_price([oracle, plain])] == ["PLAIN", "ORACLE"]


def test_indivisible_lots_price_per_whole_unit():
    d = _dispenser(asset="PEPE", rate=50_000, give=10, remaining=100, divisible=False)
    assert D._unit_price(d, False) == 5_000.0
    assert D._terms(d) == "5,000 sats each (lots of 10) — 100 left"


def test_terms_omit_the_lot_note_for_single_unit_lots():
    assert D._terms(_dispenser()) == "2,780 sats each — 28 left"


def test_browsing_one_asset_lists_cheapest_first(capsys):
    class Cp(FakeCp):
        def get_asset_dispensers(self, asset, limit=10):
            return [_dispenser(asset=asset, rate=9_000),
                    _dispenser(asset=asset, rate=2_780)], 2

    orig = _patch(FakeBtc(), Cp([]), {})
    try:
        assert D.cmd_browse_dispensers(Config(), asset="xcp") == 0
    finally:
        _restore(orig)
    lines = [l for l in capsys.readouterr().out.splitlines() if "sats each" in l]
    assert lines[0].startswith("  2,780 sats each")
    assert lines[1].startswith("  9,000 sats each")


def test_browsing_an_address_is_a_listing_not_an_error(capsys):
    cp = FakeCp([_dispenser(asset="PEPE", rate=9_000, divisible=False, give=1, remaining=5),
                 _dispenser(asset="XCP", rate=2_780)])
    orig = _patch(FakeBtc(), cp, {})
    try:
        assert D.cmd_browse_address_dispensers(Config(), DISP) == 0
    finally:
        _restore(orig)
    out = capsys.readouterr().out
    assert "2 open dispensers, cheapest first" in out
    # Cheapest per unit first: XCP at 2,780 before PEPE at 9,000.
    assert out.index("--asset XCP") < out.index("--asset PEPE")


def test_browsing_an_address_with_nothing_open_still_succeeds(capsys):
    orig = _patch(FakeBtc(), FakeCp([]), {})
    try:
        assert D.cmd_browse_address_dispensers(Config(), DISP) == 0
    finally:
        _restore(orig)
    assert "no open dispenser at" in capsys.readouterr().out


# --- the first word: an address, or an asset --------------------------------

def test_a_token_that_is_an_address_resolves_as_one():
    orig = _patch(FakeBtc(), FakeCp([]), {})
    try:
        assert D.resolve_target(Config(), DISP) == ("address", DISP)
    finally:
        _restore(orig)


def test_an_asset_name_where_an_address_goes_resolves_as_an_asset():
    # `buy-from-dispenser xcp` is the obvious thing to type; answering it with
    # "not a valid Bitcoin address" is a refusal to read.
    orig = _patch(FakeBtc(), FakeCp([]), {})
    try:
        assert D.resolve_target(Config(), "xcp") == ("asset", "XCP")
    finally:
        _restore(orig)


def test_a_token_that_is_neither_resolves_to_nothing():
    orig = _patch(FakeBtc(), FakeCp([]), {})
    try:
        assert D.resolve_target(Config(), "notathing") is None
        # Numeric assets are 'A' + digits; a bare number names neither.
        assert D.resolve_target(Config(), "12345") is None
    finally:
        _restore(orig)


def test_buying_by_asset_takes_the_cheapest_that_can_fill_it():
    class Cp(FakeCp):
        def get_asset_dispensers(self, asset, limit=10):
            rows = [
                {**_dispenser(asset="XCP", rate=2_000), "source": "bc1qCheapButEmpty",
                 "give_remaining": 0},
                {**_dispenser(asset="XCP", rate=5_200), "source": "bc1qCheapEnough"},
                {**_dispenser(asset="XCP", rate=9_000), "source": "bc1qDearer"},
            ]
            return rows[:limit], len(rows)

        def get_address_dispensers(self, address):
            return [_dispenser(asset="XCP", rate=5_200)]

    btc, cp = FakeBtc(), Cp([])
    orig = _patch(btc, cp, {SOURCE: 100_000})
    try:
        # The 2,000-sat one is cheapest and has nothing left; the 5,200 wins.
        assert D.cmd_buy_from_dispenser(
            Config(), "me", None, "1", asset="xcp", dry_run=True) == 0
        assert cp.compose_kwargs["dispenser"] == "bc1qCheapEnough"
        assert cp.compose_kwargs["quantity"] == 5_200
    finally:
        _restore(orig)


def test_buying_by_asset_says_when_no_dispenser_can_fill_it(capsys):
    class Cp(FakeCp):
        def get_asset_dispensers(self, asset, limit=10):
            return [_dispenser(asset="XCP", rate=5_200, remaining=50_000_000)], 1

    orig = _patch(FakeBtc(), Cp([]), {SOURCE: 100_000})
    try:
        assert D.cmd_buy_from_dispenser(
            Config(), "me", None, "1", asset="xcp", dry_run=True) == 1
    finally:
        _restore(orig)
    assert "the largest holds 0.5" in capsys.readouterr().err
