"""Unit tests for the DEX order commands (no network/Core needed).

The order message has consensus rules its compose API does not enforce — the
1000-sat BTC leg minimum above all — and BTC legs carry obligations (pay-order
within ~20 blocks) rather than escrow. These pin the unit conversions, the
client-side guards, the payer-side selection for pay-order, and that BTC is
allowed on either side even though `send` treats it as reserved.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import counters.commands.order as O  # noqa: E402
from counters.config import Config  # noqa: E402
from counters.counterparty import CounterpartyClient, CounterpartyError  # noqa: E402

SOURCE = "bc1pSourcexxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
OTHER = "bc1pOtherxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
HASH0 = "a" * 64
HASH1 = "b" * 64
MATCH_ID = f"{HASH0}_{HASH1}"


class FakeBtc:
    def __init__(self):
        self.sent = None

    def _call(self, method, params=None):
        if method == "testmempoolaccept":
            return [{"allowed": True}]
        if method == "sendrawtransaction":
            self.sent = params[0]
            return "ordertxid"
        raise AssertionError(f"unexpected _call {method}")

    def wallet_call(self, wallet, method, params=None, timeout=-1.0):
        if method == "listunspent":   # ensure_funded: the source pays its own fee
            return [{"address": SOURCE, "amount": 0.01, "spendable": True}]
        assert method == "signrawtransactionwithwallet"
        return {"complete": True, "hex": "signed00"}


class FakeCp:
    """BTC must never trigger an asset lookup — get_asset raises on it."""

    def __init__(self, assets=None, orders=None, matches=None, height=960_000,
                 quote_raises=False):
        self.assets = assets or {}
        self.orders = orders or {}
        self.matches = matches or []
        self.height = height
        self.quote_raises = quote_raises
        self.compose_kwargs = None
        self.cancel_kwargs = None
        self.btcpay_kwargs = None

    def get_asset(self, asset):
        assert asset.upper() != "BTC", "BTC is not a Counterparty asset to look up"
        return self.assets.get(asset) or self.assets.get(asset.upper())

    def get_pool_quote(self, a1, a2, quantity):
        if self.quote_raises:
            raise CounterpartyError("no pool")
        return None

    def compose_order(self, source, give_asset, give_quantity, get_asset,
                      get_quantity, expiration, fee_required=0, sat_per_vbyte=None):
        self.compose_kwargs = dict(
            source=source, give_asset=give_asset, give_quantity=give_quantity,
            get_asset=get_asset, get_quantity=get_quantity,
            expiration=expiration, fee_required=fee_required,
            sat_per_vbyte=sat_per_vbyte)
        return {"rawtransaction": "aa", "btc_fee": 492}

    def compose_cancel_order(self, source, offer_hash, sat_per_vbyte=None):
        self.cancel_kwargs = dict(source=source, offer_hash=offer_hash)
        return {"rawtransaction": "aa", "btc_fee": 492}

    def compose_btcpay(self, source, order_match_id, sat_per_vbyte=None):
        self.btcpay_kwargs = dict(source=source, order_match_id=order_match_id)
        return {"rawtransaction": "aa", "btc_fee": 492}

    def get_order(self, order_hash):
        return self.orders.get(order_hash)

    def get_order_matches(self, status="pending"):
        return list(self.matches)

    def counterparty_height(self):
        return self.height


def _patch(btc, cp):
    orig = (O.BitcoindClient, O.CounterpartyClient, O._wallet_addresses,
            O._find_source, O._pick_source)
    O.BitcoindClient = lambda cfg: btc
    O.CounterpartyClient = lambda cfg: cp
    O._wallet_addresses = lambda b, w: [SOURCE]
    O._find_source = lambda b, c, w, a, need: (SOURCE, 10**15)
    O._pick_source = lambda b, w, need: (SOURCE, 10**15)
    return orig


def _restore(orig):
    (O.BitcoindClient, O.CounterpartyClient, O._wallet_addresses,
     O._find_source, O._pick_source) = orig


_XCP = {"XCP": {"asset": "XCP", "divisible": True}}
_PEPE = {"PEPE": {"asset": "PEPE", "divisible": False}}


# --- open-order -------------------------------------------------------------

def test_btc_give_is_allowed_and_quoted_in_satoshis():
    # BTC in the give slot despite RESERVED_ASSETS, treated as divisible, and
    # never looked up as a Counterparty asset (the fake raises if it is).
    btc, cp = FakeBtc(), FakeCp(assets=_XCP)
    orig = _patch(btc, cp)
    try:
        rc = O.cmd_open_order(Config(), "me", "BTC", "0.001", "XCP", "5",
                              assume_yes=True)
        assert rc == 0
        k = cp.compose_kwargs
        assert k["give_asset"] == "BTC" and k["give_quantity"] == 100_000
        assert k["get_asset"] == "XCP" and k["get_quantity"] == 500_000_000
        assert k["expiration"] == 0                     # default: never expires
        assert btc.sent == "signed00"
    finally:
        _restore(orig)


def test_btc_leg_below_1000_sats_is_refused():
    # Compose would happily build it; consensus later invalidates it.
    btc, cp = FakeBtc(), FakeCp(assets=_XCP)
    orig = _patch(btc, cp)
    try:
        rc = O.cmd_open_order(Config(), "me", "BTC", "0.00000999", "XCP", "5",
                              assume_yes=True)
        assert rc == 1 and cp.compose_kwargs is None
        rc = O.cmd_open_order(Config(), "me", "XCP", "5", "BTC", "0.00001",
                              assume_yes=True)
        assert rc == 0 and cp.compose_kwargs["get_quantity"] == 1000
    finally:
        _restore(orig)


def test_fee_required_only_for_orders_receiving_btc():
    btc, cp = FakeBtc(), FakeCp(assets={**_XCP, **_PEPE})
    orig = _patch(btc, cp)
    try:
        rc = O.cmd_open_order(Config(), "me", "XCP", "1", "PEPE", "10",
                              fee_required=100, assume_yes=True)
        assert rc == 1 and cp.compose_kwargs is None
        rc = O.cmd_open_order(Config(), "me", "XCP", "1", "BTC", "0.001",
                              fee_required=100, assume_yes=True)
        assert rc == 0 and cp.compose_kwargs["fee_required"] == 100
    finally:
        _restore(orig)


def test_expiration_bounds():
    btc, cp = FakeBtc(), FakeCp(assets={**_XCP, **_PEPE})
    orig = _patch(btc, cp)
    try:
        for bad in (-1, 65536):
            rc = O.cmd_open_order(Config(), "me", "XCP", "1", "PEPE", "10",
                                  expiration=bad, assume_yes=True)
            assert rc == 1 and cp.compose_kwargs is None
        rc = O.cmd_open_order(Config(), "me", "XCP", "1", "PEPE", "10",
                              expiration=65535, assume_yes=True)
        assert rc == 0 and cp.compose_kwargs["expiration"] == 65535
    finally:
        _restore(orig)


def test_divisibility_converts_each_side_independently():
    btc, cp = FakeBtc(), FakeCp(assets={**_XCP, **_PEPE})
    orig = _patch(btc, cp)
    try:
        rc = O.cmd_open_order(Config(), "me", "XCP", "1.5", "PEPE", "10",
                              assume_yes=True)
        assert rc == 0
        k = cp.compose_kwargs
        assert k["give_quantity"] == 150_000_000       # divisible
        assert k["get_quantity"] == 10                 # indivisible
    finally:
        _restore(orig)


def test_cannot_trade_an_asset_for_itself():
    btc, cp = FakeBtc(), FakeCp(assets=_XCP)
    orig = _patch(btc, cp)
    try:
        rc = O.cmd_open_order(Config(), "me", "XCP", "1", "XCP", "2",
                              assume_yes=True)
        assert rc == 1 and cp.compose_kwargs is None
    finally:
        _restore(orig)


def test_asset_give_uses_find_source_and_checks_escrow_balance():
    btc, cp = FakeBtc(), FakeCp(assets={**_XCP, **_PEPE})
    orig = _patch(btc, cp)
    O._find_source = lambda b, c, w, a, need: (SOURCE, 50_000_000)  # holds 0.5
    try:
        rc = O.cmd_open_order(Config(), "me", "XCP", "1", "PEPE", "10",
                              assume_yes=True)
        assert rc == 1 and cp.compose_kwargs is None
    finally:
        _restore(orig)


def test_pool_quote_failure_is_not_fatal():
    btc, cp = FakeBtc(), FakeCp(assets={**_XCP, **_PEPE}, quote_raises=True)
    orig = _patch(btc, cp)
    try:
        rc = O.cmd_open_order(Config(), "me", "XCP", "1", "PEPE", "10",
                              assume_yes=True)
        assert rc == 0 and cp.compose_kwargs is not None
    finally:
        _restore(orig)


def test_declining_the_prompt_places_nothing():
    btc, cp = FakeBtc(), FakeCp(assets={**_XCP, **_PEPE})
    orig, orig_confirm = _patch(btc, cp), O._confirm
    O._confirm = lambda q: False
    try:
        rc = O.cmd_open_order(Config(), "me", "XCP", "1", "PEPE", "10")
        assert rc == 0 and btc.sent is None            # composed, never broadcast
    finally:
        O._confirm = orig_confirm
        _restore(orig)


# --- cancel-order -----------------------------------------------------------

def _order(status="open", source=SOURCE, give_asset="XCP", give_remaining=100_000_000):
    return {"tx_hash": HASH0, "status": status, "source": source,
            "give_asset": give_asset, "give_remaining": give_remaining,
            "give_asset_info": {"divisible": True}}


def test_cancel_order_happy_path():
    btc, cp = FakeBtc(), FakeCp(orders={HASH0: _order()})
    orig = _patch(btc, cp)
    try:
        rc = O.cmd_cancel_order(Config(), "me", HASH0.upper(), assume_yes=True)
        assert rc == 0
        assert cp.cancel_kwargs == {"source": SOURCE, "offer_hash": HASH0}
        assert btc.sent == "signed00"
    finally:
        _restore(orig)


def test_cancel_order_refuses_a_non_open_order():
    btc, cp = FakeBtc(), FakeCp(orders={HASH0: _order(status="filled")})
    orig = _patch(btc, cp)
    try:
        rc = O.cmd_cancel_order(Config(), "me", HASH0, assume_yes=True)
        assert rc == 1 and cp.cancel_kwargs is None
    finally:
        _restore(orig)


def test_cancel_order_refuses_someone_elses_order():
    btc, cp = FakeBtc(), FakeCp(orders={HASH0: _order(source=OTHER)})
    orig = _patch(btc, cp)
    try:
        rc = O.cmd_cancel_order(Config(), "me", HASH0, assume_yes=True)
        assert rc == 1 and cp.cancel_kwargs is None
    finally:
        _restore(orig)


def test_cancel_order_refuses_an_unknown_order():
    btc, cp = FakeBtc(), FakeCp()
    orig = _patch(btc, cp)
    try:
        rc = O.cmd_cancel_order(Config(), "me", HASH0, assume_yes=True)
        assert rc == 1 and cp.cancel_kwargs is None
    finally:
        _restore(orig)


# --- pay-order --------------------------------------------------------------

def _match(expire=960_010, payer_is_tx1=True):
    """A pending match where the wallet owes 40,000 sat of BTC."""
    if payer_is_tx1:
        return {"tx0_hash": HASH0, "tx1_hash": HASH1,
                "tx0_address": OTHER, "tx1_address": SOURCE,
                "forward_asset": "XCP", "forward_quantity": 5,
                "backward_asset": "BTC", "backward_quantity": 40_000,
                "match_expire_index": expire}
    return {"tx0_hash": HASH0, "tx1_hash": HASH1,
            "tx0_address": SOURCE, "tx1_address": OTHER,
            "forward_asset": "BTC", "forward_quantity": 40_000,
            "backward_asset": "XCP", "backward_quantity": 5,
            "match_expire_index": expire}


def test_pay_order_selects_the_btc_owing_side():
    # Wallet is tx1 and the backward asset is BTC -> it pays backward_quantity.
    btc, cp = FakeBtc(), FakeCp(matches=[_match()])
    orig = _patch(btc, cp)
    try:
        rc = O.cmd_pay_order(Config(), "me", assume_yes=True)
        assert rc == 0
        assert cp.btcpay_kwargs == {"source": SOURCE, "order_match_id": MATCH_ID}
        assert btc.sent == "signed00"
    finally:
        _restore(orig)


def test_pay_order_works_when_wallet_is_tx0():
    btc, cp = FakeBtc(), FakeCp(matches=[_match(payer_is_tx1=False)])
    orig = _patch(btc, cp)
    try:
        rc = O.cmd_pay_order(Config(), "me", assume_yes=True)
        assert rc == 0 and cp.btcpay_kwargs["source"] == SOURCE
    finally:
        _restore(orig)


def test_pay_order_with_nothing_pending_is_a_no_op():
    btc, cp = FakeBtc(), FakeCp(matches=[])
    orig = _patch(btc, cp)
    try:
        rc = O.cmd_pay_order(Config(), "me", assume_yes=True)
        assert rc == 0 and cp.btcpay_kwargs is None and btc.sent is None
    finally:
        _restore(orig)


def test_pay_order_ambiguity_requires_an_explicit_match_id():
    m2 = _match()
    m2 = {**m2, "tx0_hash": "c" * 64}
    btc, cp = FakeBtc(), FakeCp(matches=[_match(), m2])
    orig = _patch(btc, cp)
    try:
        rc = O.cmd_pay_order(Config(), "me", assume_yes=True)
        assert rc == 1 and cp.btcpay_kwargs is None
        # ...and naming the match resolves it
        rc = O.cmd_pay_order(Config(), "me", match_id=MATCH_ID, assume_yes=True)
        assert rc == 0 and cp.btcpay_kwargs["order_match_id"] == MATCH_ID
    finally:
        _restore(orig)


def test_pay_order_refuses_a_malformed_match_id():
    btc, cp = FakeBtc(), FakeCp(matches=[_match()])
    orig = _patch(btc, cp)
    try:
        rc = O.cmd_pay_order(Config(), "me", match_id="not-a-match-id",
                             assume_yes=True)
        assert rc == 1 and cp.btcpay_kwargs is None
    finally:
        _restore(orig)


def test_pay_order_refuses_an_expired_match():
    btc, cp = FakeBtc(), FakeCp(matches=[_match(expire=960_000)])   # height 960,000
    orig = _patch(btc, cp)
    try:
        rc = O.cmd_pay_order(Config(), "me", assume_yes=True)
        assert rc == 1 and cp.btcpay_kwargs is None
    finally:
        _restore(orig)


def test_pay_order_confirm_names_the_full_cost():
    # Prompt total = 40,000 sat owed + 492 sat miner fee = 0.00040492 BTC.
    btc, cp = FakeBtc(), FakeCp(matches=[_match()])
    orig, orig_confirm = _patch(btc, cp), O._confirm
    asked = []
    O._confirm = lambda q: asked.append(q) or False
    try:
        rc = O.cmd_pay_order(Config(), "me")
        assert rc == 0 and btc.sent is None
        assert asked and "0.00040492" in asked[0]
    finally:
        O._confirm = orig_confirm
        _restore(orig)


# --- match-id parsing -------------------------------------------------------

def test_parse_match_id():
    assert O._parse_match_id(MATCH_ID) == MATCH_ID
    assert O._parse_match_id(MATCH_ID.upper()) == MATCH_ID   # normalized
    assert O._parse_match_id(HASH0) is None                  # a lone tx hash
    assert O._parse_match_id("zz_yy") is None


# --- the client methods -----------------------------------------------------

class _CapCp(CounterpartyClient):
    def __init__(self):
        self.captured = None
        self.get_captured = None

    def _post(self, path, params=None):
        self.captured = (path, params)
        return {"result": {"rawtransaction": "00"}}

    def _get(self, path, params=None):
        self.get_captured = (path, params)
        return {"result": [], "next_cursor": None}


def test_compose_order_posts_to_the_order_endpoint():
    cp = _CapCp()
    cp.compose_order("src", "XCP", 1, "BTC", 1000, 0, fee_required=7)
    path, params = cp.captured
    assert path == "/v2/addresses/src/compose/order"
    assert params["give_asset"] == "XCP" and params["get_quantity"] == 1000
    assert params["expiration"] == 0 and params["fee_required"] == 7
    assert "sat_per_vbyte" not in params
    cp.compose_order("src", "XCP", 1, "BTC", 1000, 0, sat_per_vbyte=2.0)
    assert isinstance(cp.captured[1]["sat_per_vbyte"], int)   # 2, not 2.0


def test_compose_cancel_and_btcpay_endpoints():
    cp = _CapCp()
    cp.compose_cancel_order("src", HASH0)
    assert cp.captured[0] == "/v2/addresses/src/compose/cancel"
    assert cp.captured[1]["offer_hash"] == HASH0
    cp.compose_btcpay("src", MATCH_ID)
    assert cp.captured[0] == "/v2/addresses/src/compose/btcpay"
    assert cp.captured[1]["order_match_id"] == MATCH_ID


def test_paginate_carries_status_filters():
    cp = _CapCp()
    cp.get_address_orders("addr", status="open")
    path, params = cp.get_captured
    assert path == "/v2/addresses/addr/orders"
    assert params["status"] == "open" and params["limit"] == 1000
    cp.get_order_matches()
    assert cp.get_captured[1]["status"] == "pending"


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
