"""Unit tests for the AMM liquidity commands (no network/Core needed).

What these pin, in order of how much money each would cost to get wrong:

  - Slippage protection is opt-out, not opt-in. Counterparty defaults every
    minimum to 0; the CLI must send a real floor unless asked not to.
  - The confirmation prompt runs BEFORE the automatic top-up, so declining
    cannot leave a broadcast BTC transaction behind.
  - An explicit --source is checked against the wallet before any funding, so
    a mistyped address cannot be paid.
  - A positive amount below one satoshi is refused rather than silently
    composed as a zero-quantity message.
  - Pool messages never ask for taproot encoding: creating a pool mints an
    LP-token issuance under the deposit's own txid, and the counters indexer
    decides its carrier rule on the transaction, so a taproot deposit would
    number Counterparty's generated string as a counter.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import counters.commands.pool as P  # noqa: E402
from counters.config import Config  # noqa: E402
from counters.counterparty import CounterpartyClient  # noqa: E402

SOURCE = "bc1pSourcexxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
OTHER = "bc1pOtherxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
LP = "A692890855536909669"


class FakeBtc:
    def __init__(self):
        self.sent = None
        self.checked = False
        self.wallet_sends = []

    def _call(self, method, params=None):
        if method == "testmempoolaccept":
            self.checked = True
            return [{"allowed": True}]
        if method == "sendrawtransaction":
            self.sent = params[0]
            return "pooltxid"
        raise AssertionError(f"unexpected _call {method}")

    def wallet_call(self, wallet, method, params=None, timeout=-1.0):
        if method == "listunspent":   # ensure_funded: the source pays its own way
            return [{"address": SOURCE, "amount": 0.01, "spendable": True}]
        if method == "send":          # a real BTC top-up broadcast
            self.wallet_sends.append(params)
            return {"txid": "fundtxid", "complete": True}
        assert method == "signrawtransactionwithwallet"
        return {"complete": True, "hex": "signed00"}


_POOL = {
    "asset_a": "FEWGOODMAN", "asset_b": "XCP",
    "reserve_a": 4_000_000_000_000_000, "reserve_b": 50_000_000_000,
    "lp_asset": LP, "block_index": 965_995, "source": SOURCE,
}


class FakeCp:
    def __init__(self, assets=None, pool=None, deposit_quote=None,
                 withdraw_quote=None, balances=None, mempool_events=None):
        self.mempool_events = mempool_events or []
        self.assets = assets or {}
        self.pool = pool
        self.deposit_quote = deposit_quote
        self.withdraw_quote = withdraw_quote
        self.balances = balances or {}
        self.deposit_kwargs = None
        self.withdraw_kwargs = None

    def get_asset(self, asset):
        return self.assets.get(asset) or self.assets.get(asset.upper())

    def get_pool(self, a1, a2):
        return self.pool

    def get_pool_deposit_quote(self, a1, a2, quantity):
        return self.deposit_quote

    def get_pool_withdraw_quote(self, a1, a2, quantity):
        return self.withdraw_quote

    def get_address_balances(self, address):
        return self.balances.get(address, [])

    def get_address_mempool_events(self, address, event_name=None):
        return [e for e in self.mempool_events
                if (e.get("params") or {}).get("address") == address]

    def compose_pooldeposit(self, source, asset_a, asset_b, quantity_a,
                            quantity_b, min_lp_quantity=0, lp_asset=None,
                            sat_per_vbyte=None):
        self.deposit_kwargs = dict(
            source=source, asset_a=asset_a, asset_b=asset_b,
            quantity_a=quantity_a, quantity_b=quantity_b,
            min_lp_quantity=min_lp_quantity, lp_asset=lp_asset,
            sat_per_vbyte=sat_per_vbyte)
        return {"rawtransaction": "aa", "btc_fee": 492}

    def compose_poolwithdraw(self, source, asset_a, asset_b, quantity,
                             min_quantity_a=0, min_quantity_b=0,
                             sat_per_vbyte=None):
        self.withdraw_kwargs = dict(
            source=source, asset_a=asset_a, asset_b=asset_b, quantity=quantity,
            min_quantity_a=min_quantity_a, min_quantity_b=min_quantity_b,
            sat_per_vbyte=sat_per_vbyte)
        return {"rawtransaction": "aa", "btc_fee": 492}


_ASSETS = {
    "FEWGOODMAN": {"asset": "FEWGOODMAN", "divisible": True},
    "XCP": {"asset": "XCP", "divisible": True},
    "PEPE": {"asset": "PEPE", "divisible": False},
    LP: {"asset": LP, "divisible": True},
}

_DEP_QUOTE = {
    "first_deposit": False, "asset_a": "FEWGOODMAN", "asset_b": "XCP",
    "quantity_a_required": 100_000_000_000, "quantity_b_required": 1_250_000,
    "quantity_minted_estimate": 1_000_000,
}
_WD_QUOTE = {
    "pool_exists": True, "asset_a": "FEWGOODMAN", "asset_b": "XCP",
    "supply": 10_000_000_000, "quantity_a_estimate": 400_000_000,
    "quantity_b_estimate": 5_000_000,
}


def _patch(btc, cp, addresses=(SOURCE,), balance=10**15, confirm=True):
    orig = (P.BitcoindClient, P.CounterpartyClient, P._wallet_addresses,
            P._address_asset_balance, P._find_source_two, P._confirm)
    P.BitcoindClient = lambda cfg: btc
    P.CounterpartyClient = lambda cfg: cp
    P._wallet_addresses = lambda b, w: list(addresses)
    P._address_asset_balance = lambda c, addr, asset: balance
    P._find_source_two = lambda b, c, w, a, na, bb, nb: (
        SOURCE, balance, balance, [(SOURCE, balance, balance)])
    P._confirm = lambda q: confirm
    return orig


def _restore(orig):
    (P.BitcoindClient, P.CounterpartyClient, P._wallet_addresses,
     P._address_asset_balance, P._find_source_two, P._confirm) = orig


# --- add-liquidity ----------------------------------------------------------


def test_divisible_amounts_convert_to_satoshis():
    btc, cp = FakeBtc(), FakeCp(assets=_ASSETS, pool=_POOL, deposit_quote=_DEP_QUOTE)
    orig = _patch(btc, cp)
    try:
        rc = P.cmd_add_liquidity(Config(), "me", "FEWGOODMAN", "1000", "XCP",
                                 "0.0125", assume_yes=True)
        assert rc == 0
        k = cp.deposit_kwargs
        assert k["asset_a"] == "FEWGOODMAN" and k["quantity_a"] == 100_000_000_000
        assert k["asset_b"] == "XCP" and k["quantity_b"] == 1_250_000
        assert btc.sent == "signed00"
    finally:
        _restore(orig)


def test_indivisible_amounts_stay_whole_units():
    assets = dict(_ASSETS)
    pool = dict(_POOL, asset_a="PEPE")
    btc, cp = FakeBtc(), FakeCp(assets=assets, pool=pool,
                                deposit_quote=dict(_DEP_QUOTE, asset_a="PEPE"))
    orig = _patch(btc, cp)
    try:
        rc = P.cmd_add_liquidity(Config(), "me", "PEPE", "7", "XCP", "1",
                                 assume_yes=True)
        assert rc == 0
        assert cp.deposit_kwargs["quantity_a"] == 7
    finally:
        _restore(orig)


def test_second_amount_is_taken_from_the_pool_ratio_when_omitted():
    btc, cp = FakeBtc(), FakeCp(assets=_ASSETS, pool=_POOL, deposit_quote=_DEP_QUOTE)
    orig = _patch(btc, cp)
    try:
        rc = P.cmd_add_liquidity(Config(), "me", "FEWGOODMAN", "1000", "XCP",
                                 None, assume_yes=True)
        assert rc == 0
        assert cp.deposit_kwargs["quantity_b"] == _DEP_QUOTE["quantity_b_required"]
    finally:
        _restore(orig)


def test_first_deposit_requires_both_amounts():
    btc, cp = FakeBtc(), FakeCp(assets=_ASSETS, pool=None)
    orig = _patch(btc, cp)
    try:
        rc = P.cmd_add_liquidity(Config(), "me", "FEWGOODMAN", "1000", "XCP",
                                 None, assume_yes=True)
        assert rc == 1
        assert cp.deposit_kwargs is None
    finally:
        _restore(orig)


def test_first_deposit_composes_with_no_slippage_floor_and_names_the_lp_asset():
    btc, cp = FakeBtc(), FakeCp(assets=_ASSETS, pool=None)
    orig = _patch(btc, cp)
    try:
        rc = P.cmd_add_liquidity(Config(), "me", "FEWGOODMAN", "1000", "XCP",
                                 "1", lp_asset="MYLP", assume_yes=True)
        assert rc == 0
        k = cp.deposit_kwargs
        assert k["min_lp_quantity"] == 0     # nothing to slip against yet
        assert k["lp_asset"] == "MYLP"
    finally:
        _restore(orig)


def test_slippage_defaults_to_one_percent_below_the_quoted_mint():
    btc, cp = FakeBtc(), FakeCp(assets=_ASSETS, pool=_POOL, deposit_quote=_DEP_QUOTE)
    orig = _patch(btc, cp)
    try:
        rc = P.cmd_add_liquidity(Config(), "me", "FEWGOODMAN", "1000", "XCP",
                                 "0.0125", assume_yes=True)
        assert rc == 0
        assert cp.deposit_kwargs["min_lp_quantity"] == 990_000  # 99% of 1_000_000
    finally:
        _restore(orig)


def test_slippage_zero_sends_counterpartys_own_no_guard_default():
    btc, cp = FakeBtc(), FakeCp(assets=_ASSETS, pool=_POOL, deposit_quote=_DEP_QUOTE)
    orig = _patch(btc, cp)
    try:
        rc = P.cmd_add_liquidity(Config(), "me", "FEWGOODMAN", "1000", "XCP",
                                 "0.0125", slippage=0, assume_yes=True)
        assert rc == 0
        assert cp.deposit_kwargs["min_lp_quantity"] == 0
    finally:
        _restore(orig)


def test_sub_satoshi_amount_is_refused_not_rounded_to_zero():
    btc, cp = FakeBtc(), FakeCp(assets=_ASSETS, pool=_POOL, deposit_quote=_DEP_QUOTE)
    orig = _patch(btc, cp)
    try:
        rc = P.cmd_add_liquidity(Config(), "me", "FEWGOODMAN", "0.000000004",
                                 "XCP", "1", assume_yes=True)
        assert rc == 1
        assert cp.deposit_kwargs is None
    finally:
        _restore(orig)


def test_btc_can_never_be_pooled():
    for a, b in (("BTC", "XCP"), ("XCP", "BTC")):
        btc, cp = FakeBtc(), FakeCp(assets=_ASSETS, pool=_POOL)
        orig = _patch(btc, cp)
        try:
            rc = P.cmd_add_liquidity(Config(), "me", a, "1", b, "1", assume_yes=True)
            assert rc == 1, f"{a}/{b} was not refused"
            assert cp.deposit_kwargs is None
        finally:
            _restore(orig)


def test_explicit_source_outside_the_wallet_is_refused_before_any_funding():
    btc, cp = FakeBtc(), FakeCp(assets=_ASSETS, pool=_POOL, deposit_quote=_DEP_QUOTE)
    orig = _patch(btc, cp)
    try:
        rc = P.cmd_add_liquidity(Config(), "me", "FEWGOODMAN", "1000", "XCP",
                                 "0.0125", source=OTHER, assume_yes=True)
        assert rc == 1
        assert cp.deposit_kwargs is None
        assert btc.wallet_sends == [], "a top-up was paid to a foreign address"
    finally:
        _restore(orig)


def test_declining_the_prompt_broadcasts_nothing_at_all():
    btc, cp = FakeBtc(), FakeCp(assets=_ASSETS, pool=_POOL, deposit_quote=_DEP_QUOTE)
    orig = _patch(btc, cp, confirm=False)
    try:
        rc = P.cmd_add_liquidity(Config(), "me", "FEWGOODMAN", "1000", "XCP",
                                 "0.0125")
        assert rc == 0
        assert cp.deposit_kwargs is None
        assert btc.sent is None
        # The prompt runs before ensure_funded, so no BTC top-up went out.
        assert btc.wallet_sends == []
    finally:
        _restore(orig)


def test_dry_run_validates_but_does_not_broadcast():
    btc, cp = FakeBtc(), FakeCp(assets=_ASSETS, pool=_POOL, deposit_quote=_DEP_QUOTE)
    orig = _patch(btc, cp)
    try:
        rc = P.cmd_add_liquidity(Config(), "me", "FEWGOODMAN", "1000", "XCP",
                                 "0.0125", dry_run=True)
        assert rc == 0
        assert btc.checked is True
        assert btc.sent is None
    finally:
        _restore(orig)


def test_user_pair_order_is_mapped_onto_counterpartys_canonical_order():
    """Core normalizes the pair; a deposit named XCP-first must still compose
    with the pool's own (asset_a, asset_b) and the amounts on the right sides."""
    btc, cp = FakeBtc(), FakeCp(assets=_ASSETS, pool=_POOL, deposit_quote=_DEP_QUOTE)
    orig = _patch(btc, cp)
    try:
        rc = P.cmd_add_liquidity(Config(), "me", "XCP", "0.0125", "FEWGOODMAN",
                                 "1000", assume_yes=True)
        assert rc == 0
        k = cp.deposit_kwargs
        assert k["asset_a"] == "FEWGOODMAN" and k["quantity_a"] == 100_000_000_000
        assert k["asset_b"] == "XCP" and k["quantity_b"] == 1_250_000
    finally:
        _restore(orig)


def test_partial_candidates_rank_by_legs_covered_not_by_raw_totals():
    """Raw quantities of two different assets are not comparable: XCP is in
    satoshis, an indivisible asset is in whole units, so summing them lets one
    side swamp the other. The address holding ALL of the scarce asset must win
    over one holding none of it."""
    rich_xcp = "bc1qRichXcpxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    has_token = "bc1pHasTokenxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    balances = {
        # 1319 XCP in satoshis dwarfs 1329 whole BONPARTY numerically
        rich_xcp: [{"asset": "XCP", "quantity": 131_900_000_000}],
        has_token: [{"asset": "BONPARTY", "quantity": 1329},
                    {"asset": "XCP", "quantity": 2_300_000_000}],
    }
    cp = FakeCp(assets=_ASSETS, balances=balances)
    orig = (P._wallet_addresses,)
    P._wallet_addresses = lambda b, w: [rich_xcp, has_token]
    try:
        addr, ha, hb, rows = P._find_source_two(
            None, cp, "me", "BONPARTY", 500, "XCP", 10_000_000_000)
        assert addr == has_token, f"picked {addr}, which holds no BONPARTY"
        assert (ha, hb) == (1329, 2_300_000_000)
        assert [r[0] for r in rows] == [has_token, rich_xcp]
    finally:
        (P._wallet_addresses,) = orig


def test_an_address_covering_both_wins_outright():
    both = "bc1pBothxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    thin = "bc1pThinxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    balances = {
        thin: [{"asset": "XCP", "quantity": 999_999_999_999}],
        both: [{"asset": "BONPARTY", "quantity": 500},
               {"asset": "XCP", "quantity": 10_000_000_000}],
    }
    cp = FakeCp(assets=_ASSETS, balances=balances)
    orig = (P._wallet_addresses,)
    P._wallet_addresses = lambda b, w: [thin, both]
    try:
        addr, _, _, rows = P._find_source_two(
            None, cp, "me", "BONPARTY", 500, "XCP", 10_000_000_000)
        assert addr == both
        assert rows == [(both, 500, 10_000_000_000)]
    finally:
        (P._wallet_addresses,) = orig


# --- remove-liquidity -------------------------------------------------------


def test_withdraw_burns_lp_and_floors_both_sides_at_one_percent():
    btc, cp = FakeBtc(), FakeCp(assets=_ASSETS, pool=_POOL, withdraw_quote=_WD_QUOTE)
    orig = _patch(btc, cp, balance=1_000_000_000)
    try:
        rc = P.cmd_remove_liquidity(Config(), "me", "FEWGOODMAN", "XCP", "1",
                                    assume_yes=True)
        assert rc == 0
        k = cp.withdraw_kwargs
        assert k["quantity"] == 100_000_000
        assert k["min_quantity_a"] == 396_000_000    # 99% of 400_000_000
        assert k["min_quantity_b"] == 4_950_000      # 99% of 5_000_000
    finally:
        _restore(orig)


def test_withdraw_all_burns_the_whole_balance():
    btc, cp = FakeBtc(), FakeCp(assets=_ASSETS, pool=_POOL, withdraw_quote=_WD_QUOTE)
    orig = _patch(btc, cp, balance=777_000_000)
    try:
        rc = P.cmd_remove_liquidity(Config(), "me", "FEWGOODMAN", "XCP", "all",
                                    assume_yes=True)
        assert rc == 0
        assert cp.withdraw_kwargs["quantity"] == 777_000_000
    finally:
        _restore(orig)


def test_withdraw_refuses_more_lp_than_held():
    btc, cp = FakeBtc(), FakeCp(assets=_ASSETS, pool=_POOL, withdraw_quote=_WD_QUOTE)
    orig = _patch(btc, cp, balance=50_000_000)
    try:
        rc = P.cmd_remove_liquidity(Config(), "me", "FEWGOODMAN", "XCP", "1",
                                    assume_yes=True)
        assert rc == 1
        assert cp.withdraw_kwargs is None
    finally:
        _restore(orig)


def test_withdraw_from_a_pair_with_no_pool_is_refused():
    btc, cp = FakeBtc(), FakeCp(assets=_ASSETS, pool=None)
    orig = _patch(btc, cp)
    try:
        rc = P.cmd_remove_liquidity(Config(), "me", "FEWGOODMAN", "XCP", "1",
                                    assume_yes=True)
        assert rc == 1
        assert cp.withdraw_kwargs is None
    finally:
        _restore(orig)


def test_withdraw_declined_broadcasts_nothing():
    btc, cp = FakeBtc(), FakeCp(assets=_ASSETS, pool=_POOL, withdraw_quote=_WD_QUOTE)
    orig = _patch(btc, cp, balance=1_000_000_000, confirm=False)
    try:
        rc = P.cmd_remove_liquidity(Config(), "me", "FEWGOODMAN", "XCP", "1")
        assert rc == 0
        assert cp.withdraw_kwargs is None
        assert btc.sent is None and btc.wallet_sends == []
    finally:
        _restore(orig)


def test_out_of_range_slippage_is_refused_on_both_commands():
    for fn, args in ((P.cmd_add_liquidity, ("FEWGOODMAN", "1", "XCP", "1")),
                     (P.cmd_remove_liquidity, ("FEWGOODMAN", "XCP", "1"))):
        btc, cp = FakeBtc(), FakeCp(assets=_ASSETS, pool=_POOL,
                                    deposit_quote=_DEP_QUOTE, withdraw_quote=_WD_QUOTE)
        orig = _patch(btc, cp)
        try:
            assert fn(Config(), "me", *args, slippage=101, assume_yes=True) == 1
            assert fn(Config(), "me", *args, slippage=-1, assume_yes=True) == 1
        finally:
            _restore(orig)


# --- the consolidating-send offer -------------------------------------------
#
# A deposit is one Counterparty message from one address. When the two assets
# sit on different addresses the command offers the single transfer that would
# fix it — and must never broadcast that transfer twice.

TARGET = "bc1pTargetxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
DONOR = "bc1pDonorxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

# TARGET holds all the BONPARTY but only 23 XCP; DONOR holds the XCP.
_SPLIT = [(TARGET, 1329, 2_300_000_000), (DONOR, 0, 131_900_000_000)]
_SPLIT_ASSETS = {"BONPARTY": {"asset": "BONPARTY", "divisible": False},
                 "XCP": {"asset": "XCP", "divisible": True}}


def _patch_split(btc, cp, confirm=True, sends=None):
    """No address covers both legs, so cmd_add_liquidity reaches the offer."""
    orig = (P.BitcoindClient, P.CounterpartyClient, P._wallet_addresses,
            P._find_source_two, P._find_source, P._confirm, P.cmd_send)
    P.BitcoindClient = lambda cfg: btc
    P.CounterpartyClient = lambda cfg: cp
    P._wallet_addresses = lambda b, w: [TARGET, DONOR]
    P._find_source_two = lambda b, c, w, a, na, bb, nb: (
        TARGET, 1329, 2_300_000_000, list(_SPLIT))
    P._find_source = lambda b, c, w, asset, need: (DONOR, 131_900_000_000)
    P._confirm = lambda q: confirm
    if sends is not None:
        P.cmd_send = lambda *a, **kw: (sends.append((a, kw)), 0)[1]
    return orig


def _restore_split(orig):
    (P.BitcoindClient, P.CounterpartyClient, P._wallet_addresses,
     P._find_source_two, P._find_source, P._confirm, P.cmd_send) = orig


def _pending(address, asset, quantity, txid="deadbeef"):
    return [{"event": "CREDIT", "tx_hash": txid,
             "params": {"address": address, "asset": asset, "quantity": quantity}}]


def test_split_balance_offers_the_send_and_accepting_transfers_the_gap():
    sends = []
    btc, cp = FakeBtc(), FakeCp(assets=_SPLIT_ASSETS, pool=None)
    orig = _patch_split(btc, cp, confirm=True, sends=sends)
    try:
        rc = P.cmd_add_liquidity(Config(), "counts", "BONPARTY", "500", "XCP",
                                 "100", fee_rate=1.0)
        assert rc == 1, "the deposit did not happen, so this must not report success"
        assert len(sends) == 1, sends
        args, kwargs = sends[0]
        # cmd_send(config, wallet, destination, asset, amount)
        assert args[2] == TARGET and args[3] == "XCP"
        assert args[4] == "77"          # 100 wanted, 23 already there
        assert kwargs["fee_rate"] == 1.0
        assert cp.deposit_kwargs is None, "no deposit may be composed"
    finally:
        _restore_split(orig)


def test_declining_the_offer_sends_nothing():
    sends = []
    btc, cp = FakeBtc(), FakeCp(assets=_SPLIT_ASSETS, pool=None)
    orig = _patch_split(btc, cp, confirm=False, sends=sends)
    try:
        rc = P.cmd_add_liquidity(Config(), "counts", "BONPARTY", "500", "XCP",
                                 "100", fee_rate=1.0)
        assert rc == 1
        assert sends == []
        assert cp.deposit_kwargs is None
    finally:
        _restore_split(orig)


def test_a_pending_transfer_suppresses_the_offer_entirely():
    """The re-run case. Counterparty credits a balance only when the block is
    parsed, so a broadcast-but-unconfirmed send is invisible to every balance
    query — without the mempool check the second run would send again."""
    sends = []
    btc = FakeBtc()
    cp = FakeCp(assets=_SPLIT_ASSETS, pool=None,
                mempool_events=_pending(TARGET, "XCP", 7_700_000_000))
    orig = _patch_split(btc, cp, confirm=True, sends=sends)
    try:
        rc = P.cmd_add_liquidity(Config(), "counts", "BONPARTY", "500", "XCP",
                                 "100", fee_rate=1.0)
        assert rc == 1
        assert sends == [], "a duplicate transfer was broadcast"
        assert cp.deposit_kwargs is None
    finally:
        _restore_split(orig)


def test_a_pending_credit_of_a_different_asset_does_not_suppress_the_offer():
    sends = []
    btc, cp = FakeBtc(), FakeCp(
        assets=_SPLIT_ASSETS, pool=None,
        mempool_events=_pending(TARGET, "SOMETHINGELSE", 1))
    orig = _patch_split(btc, cp, confirm=True, sends=sends)
    try:
        P.cmd_add_liquidity(Config(), "counts", "BONPARTY", "500", "XCP", "100")
        assert len(sends) == 1
    finally:
        _restore_split(orig)


def test_yes_alone_never_triggers_a_transfer_but_consolidate_does():
    """--yes means 'skip the deposit prompt'. A scripted add-liquidity must not
    broadcast an asset transfer as a side effect of it."""
    sends = []
    btc, cp = FakeBtc(), FakeCp(assets=_SPLIT_ASSETS, pool=None)
    orig = _patch_split(btc, cp, confirm=False, sends=sends)   # prompt says no
    try:
        rc = P.cmd_add_liquidity(Config(), "counts", "BONPARTY", "500", "XCP",
                                 "100", assume_yes=True)
        assert rc == 1 and sends == [], "--yes broadcast a transfer"
    finally:
        _restore_split(orig)

    sends = []
    btc, cp = FakeBtc(), FakeCp(assets=_SPLIT_ASSETS, pool=None)
    orig = _patch_split(btc, cp, confirm=False, sends=sends)
    try:
        rc = P.cmd_add_liquidity(Config(), "counts", "BONPARTY", "500", "XCP",
                                 "100", consolidate=True)
        assert rc == 1 and len(sends) == 1, "--consolidate did not send"
    finally:
        _restore_split(orig)


def test_dry_run_describes_the_transfer_but_never_sends():
    sends = []
    btc, cp = FakeBtc(), FakeCp(assets=_SPLIT_ASSETS, pool=None)
    orig = _patch_split(btc, cp, confirm=True, sends=sends)
    try:
        rc = P.cmd_add_liquidity(Config(), "counts", "BONPARTY", "500", "XCP",
                                 "100", dry_run=True, consolidate=True)
        assert rc == 1 and sends == []
    finally:
        _restore_split(orig)


def test_no_offer_when_every_address_is_short_on_both_assets():
    sends = []
    btc, cp = FakeBtc(), FakeCp(assets=_SPLIT_ASSETS, pool=None)
    orig = _patch_split(btc, cp, confirm=True, sends=sends)
    P._find_source_two = lambda b, c, w, a, na, bb, nb: (
        TARGET, 10, 1, [(TARGET, 10, 1), (DONOR, 5, 2)])
    try:
        rc = P.cmd_add_liquidity(Config(), "counts", "BONPARTY", "500", "XCP",
                                 "100", consolidate=True)
        assert rc == 1 and sends == [], "offered a send that cannot fix it"
    finally:
        _restore_split(orig)


def test_no_offer_when_nothing_can_cover_the_gap():
    sends = []
    btc, cp = FakeBtc(), FakeCp(assets=_SPLIT_ASSETS, pool=None)
    orig = _patch_split(btc, cp, confirm=True, sends=sends)
    P._find_source = lambda b, c, w, asset, need: (None, 0)
    try:
        rc = P.cmd_add_liquidity(Config(), "counts", "BONPARTY", "500", "XCP",
                                 "100", consolidate=True)
        assert rc == 1 and sends == []
    finally:
        _restore_split(orig)


def test_a_failed_transfer_is_reported_as_failure():
    btc, cp = FakeBtc(), FakeCp(assets=_SPLIT_ASSETS, pool=None)
    orig = _patch_split(btc, cp, confirm=True)
    P.cmd_send = lambda *a, **kw: 1
    try:
        rc = P.cmd_add_liquidity(Config(), "counts", "BONPARTY", "500", "XCP",
                                 "100", consolidate=True)
        assert rc == 1
        assert cp.deposit_kwargs is None
    finally:
        _restore_split(orig)


# --- client layer -----------------------------------------------------------


class _CapCp(CounterpartyClient):
    def __init__(self, config):
        super().__init__(config)
        self.captured = []

    def _get(self, path, params=None):
        self.captured.append(("GET", path, params or {}))
        return {"result": {}}

    def _post(self, path, params=None):
        self.captured.append(("POST", path, params or {}))
        return {"result": {"rawtransaction": "aa"}}


def test_mempool_events_endpoint_shape():
    cp = _CapCp(Config())
    cp.get_address_mempool_events("bc1pabc", "CREDIT")
    _, path, params = cp.captured[0]
    assert path == "/v2/addresses/mempool"
    assert params["addresses"] == "bc1pabc" and params["event_name"] == "CREDIT"


def test_withdraw_quote_is_called_without_verbose():
    """Counterparty Core v11.2.0 answers 500 on quote/withdraw when verbose is
    present, though quote/deposit accepts it. Every other call in the client
    sets it, so this asymmetry has to be pinned."""
    cp = _CapCp(Config())
    cp.get_pool_withdraw_quote("FEWGOODMAN", "XCP", 100)
    _, path, params = cp.captured[0]
    assert path == "/v2/pools/FEWGOODMAN/XCP/quote/withdraw"
    assert "verbose" not in params

    cp.get_pool_deposit_quote("FEWGOODMAN", "XCP", 100)
    _, path, params = cp.captured[1]
    assert path == "/v2/pools/FEWGOODMAN/XCP/quote/deposit"
    assert params.get("verbose") == "true"


def test_pool_composes_never_request_taproot_or_inscription():
    """A taproot-encoded pool deposit would make the LP-token issuance Core
    generates inherit a reveal's carrier, minting a phantom counter."""
    cp = _CapCp(Config())
    cp.compose_pooldeposit("src", "A", "B", 1, 2, min_lp_quantity=3)
    cp.compose_poolwithdraw("src", "A", "B", 1, min_quantity_a=2, min_quantity_b=3)
    for method, path, params in cp.captured:
        assert method == "POST"
        assert params.get("encoding") in (None, "opreturn"), params
        assert "inscription" not in params
        assert params["disable_utxo_locks"] == "true"


def test_compose_params_reach_the_endpoint_intact():
    cp = _CapCp(Config())
    cp.compose_pooldeposit("src", "A", "B", 10, 20, min_lp_quantity=5,
                           lp_asset="MYLP", sat_per_vbyte=2.0)
    _, path, params = cp.captured[0]
    assert path == "/v2/addresses/src/compose/pooldeposit"
    assert params["quantity_a"] == 10 and params["quantity_b"] == 20
    assert params["min_lp_quantity"] == 5 and params["lp_asset"] == "MYLP"
    assert isinstance(params["sat_per_vbyte"], int)   # 2.0 must not become "2.0"

    cp.compose_poolwithdraw("src", "A", "B", 9, min_quantity_a=1, min_quantity_b=2)
    _, path, params = cp.captured[1]
    assert path == "/v2/addresses/src/compose/poolwithdraw"
    assert params["quantity"] == 9
    assert params["min_quantity_a"] == 1 and params["min_quantity_b"] == 2
    assert "lp_asset" not in params


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as e:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {e}")
    raise SystemExit(1 if failures else 0)
