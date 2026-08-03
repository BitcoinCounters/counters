"""Unit tests for automatic source funding and the dispenser fence.

Every Counterparty message is paid for by the address it is debited from, so a
source holding only tokens gets topped up. The load-bearing rule is that the
top-up must never trip a dispenser sitting on that same address: a dispense
fires at floor(sent / satoshirate) >= 1, so funding has to stay strictly under
the rate, and refuse rather than cross it.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counters.commands.funding import (  # noqa: E402
    DUST_SAT,
    Funding,
    compose_retrying,
    dispense_floor,
    ensure_funded,
    estimate_need,
)
from counters.counterparty import CounterpartyError  # noqa: E402

SRC = "bc1pSourcexxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"


class _FakeBtc:
    def __init__(self, sats: int = 0):
        self.sats = sats
        self.sent = None

    def wallet_call(self, wallet, method, params=None, timeout=-1.0):
        if method == "listunspent":
            if not self.sats:
                return []
            return [{"address": SRC, "amount": self.sats / 1e8, "spendable": True}]
        if method == "send":
            self.sent = params
            return {"txid": "fundtxid"}
        if method == "listdescriptors":
            return {"descriptors": []}
        raise AssertionError(f"unexpected wallet call {method}")


class _FakeCp:
    def __init__(self, dispensers=None):
        self.dispensers = dispensers or []

    def get_address_dispensers(self, address):
        return self.dispensers


def _dispenser(rate: int, remaining: int = 10, status=0):
    return {"satoshirate": rate, "give_remaining": remaining, "status": status}


# --- sizing -----------------------------------------------------------------

def test_need_never_falls_below_dust():
    # The funding output itself has to be spendable.
    assert estimate_need(0.1) == DUST_SAT
    assert estimate_need(None) >= DUST_SAT


def test_need_scales_with_fee_rate_and_extra_outputs():
    assert estimate_need(10) > estimate_need(1)
    assert estimate_need(10, outputs=1) == estimate_need(10) + DUST_SAT


# --- the dispenser fence ----------------------------------------------------

def test_no_dispenser_means_no_cap():
    assert dispense_floor(_FakeCp([]), SRC) is None


def test_floor_is_one_satoshi_under_the_rate():
    # 999 sat buys floor(999/1000) = 0 tokens, so it cannot trigger a dispense.
    assert dispense_floor(_FakeCp([_dispenser(1000)]), SRC) == 999


def test_cheapest_open_dispenser_sets_the_floor():
    cp = _FakeCp([_dispenser(5000), _dispenser(800), _dispenser(2000)])
    assert dispense_floor(cp, SRC) == 799


def test_closed_and_drained_dispensers_cannot_fire():
    cp = _FakeCp([_dispenser(1000, status=10), _dispenser(500, remaining=0)])
    assert dispense_floor(cp, SRC) is None


def test_an_unreachable_counterparty_does_not_block():
    class _Broken:
        def get_address_dispensers(self, address):
            raise CounterpartyError("not ready")
    assert dispense_floor(_Broken(), SRC) is None


# --- ensure_funded ----------------------------------------------------------

def test_a_funded_source_is_left_alone():
    btc = _FakeBtc(100_000)
    assert ensure_funded(btc, _FakeCp(), "me", SRC, fee_rate=1) == Funding(None, False)
    assert btc.sent is None


def test_an_empty_source_is_topped_up():
    btc = _FakeBtc(0)
    result = ensure_funded(btc, _FakeCp(), "me", SRC, fee_rate=1)
    assert result.code is None and result.funded is True
    assert btc.sent is not None                      # a funding tx went out


def test_no_fund_leaves_the_source_untouched():
    btc = _FakeBtc(0)
    assert ensure_funded(btc, _FakeCp(), "me", SRC, no_fund=True) == Funding(None, False)
    assert btc.sent is None


def test_dry_run_reports_instead_of_sending():
    btc = _FakeBtc(0)
    assert ensure_funded(btc, _FakeCp(), "me", SRC, fee_rate=1, dry_run=True).code == 0
    assert btc.sent is None


def test_funding_under_the_dispenser_rate_is_allowed():
    # 1 sat/vB needs a few hundred sat; a 1000-sat dispenser leaves room.
    btc = _FakeBtc(0)
    cp = _FakeCp([_dispenser(1000)])
    assert estimate_need(1) < 1000                   # the premise of this test
    result = ensure_funded(btc, cp, "me", SRC, fee_rate=1)
    assert result.funded is True and btc.sent is not None


def test_funding_that_would_trigger_a_dispense_is_refused():
    btc = _FakeBtc(0)
    cp = _FakeCp([_dispenser(50)])                   # any useful top-up exceeds 50 sat
    result = ensure_funded(btc, cp, "me", SRC, fee_rate=1)
    assert result.code == 1 and result.funded is False
    assert btc.sent is None                          # nothing was sent


def test_the_fence_only_applies_when_a_top_up_is_needed():
    # A source that can already pay never gets near the dispenser question.
    btc = _FakeBtc(100_000)
    cp = _FakeCp([_dispenser(1)])                    # would forbid any transfer
    assert ensure_funded(btc, cp, "me", SRC, fee_rate=1).code is None


# --- racing Counterparty's view of a fresh funding --------------------------

def test_compose_is_retried_while_the_funding_becomes_visible():
    calls = []

    def compose():
        calls.append(1)
        if len(calls) < 3:
            raise CounterpartyError("Insufficient funds for the target amount: 330 < 18598")
        return {"rawtransaction": "raw"}

    import counters.commands.funding as F
    F.VISIBLE_WAIT = 0                               # no real sleeping in tests
    assert compose_retrying(compose, funded=True) == {"rawtransaction": "raw"}
    assert len(calls) == 3


def test_an_unfunded_shortfall_fails_at_once():
    calls = []

    def compose():
        calls.append(1)
        raise CounterpartyError("Insufficient funds for the target amount: 0 < 500")

    try:
        compose_retrying(compose, funded=False)
    except CounterpartyError:
        pass
    else:
        raise AssertionError("expected the error to propagate")
    assert len(calls) == 1                           # no waiting on a real shortfall


def test_unrelated_compose_errors_are_not_retried():
    calls = []

    def compose():
        calls.append(1)
        raise CounterpartyError("Cannot update a locked description")

    try:
        compose_retrying(compose, funded=True)
    except CounterpartyError:
        pass
    assert len(calls) == 1
