"""Counterparty DEX orders — place, cancel, settle, and list.

An order offers GIVE_AMOUNT of one asset for GET_AMOUNT of another on
Counterparty's on-chain order book (which, since block 952,800, also routes
through the native AMM pools — a match can fill from either). The escrow
rules decide everything about how careful each command must be:

  - A non-BTC give asset is escrowed the moment the order confirms, and only
    returns on cancel or expiry. With --expiration 0 (the default: the order
    never expires) the escrow stays locked until an explicit `cancel-order`.
  - BTC is NEVER escrowed. A match against a BTC-give order goes "pending",
    and the BTC must be paid with a separate `pay-order` transaction within
    ~20 blocks. Missing that window expires the match AND every other open
    BTC-give order from the same address — Counterparty's deadbeat penalty.
    On top of that, a BTC-give order's own miner fee doubles as its
    `fee_provided` budget: counterparties whose `fee_required` exceeds it
    will never match, so the fee rate matters beyond confirmation speed.
  - Either BTC leg must be at least 1,000 sat; Core's composer does not check
    this, but consensus later rejects the order, so we refuse up front.

Naming note: `cancel-order` composes Counterparty's *cancel* message and
withdraws a resting DEX order. It is unrelated to `counters wallet cancel`,
which RBF-abandons an unconfirmed Bitcoin transaction.
"""

from __future__ import annotations

import re
import sys
from decimal import Decimal

from ..bitcoind import BitcoindClient
from ..config import Config
from ..counterparty import CounterpartyClient, CounterpartyError
from .dispenser import _address_asset_balance, _fmt_btc_sat, _pick_source, \
    _report_compose_failure
from .send import (
    _confirm_prompt,
    _find_source,
    _fmt_raw,
    _sign_and_broadcast,
    _to_raw_quantity,
)
from .funding import compose_retrying, ensure_funded
from .wallet import _wallet_addresses

# counterparty-core lib/messages/order.py
MAX_EXPIRATION = 65535          # u16 in the wire format; 0 = never expires
MIN_BTC_LEG_SAT = 1000          # btc_order_minimum — consensus, not compose
BTCPAY_WINDOW_BLOCKS = 20       # order matches expire ~20 blocks after matching
SWAP_EXPIRATION = 1             # a market order fills this block or expires
DEFAULT_SLIPPAGE = 1.0          # percent below the live quote a swap accepts
MAX_SLIPPAGE = 100.0

_MATCH_ID_RE = re.compile(r"^[0-9a-f]{64}_[0-9a-f]{64}$")


def _confirm(question: str) -> bool:
    return _confirm_prompt(f"\n{question}")


def _resolve_order_asset(cp, name: str):
    """Canonical (asset, divisible) for an order leg, or None after printing
    why. BTC is a first-class DEX asset (always divisible — quantities are
    satoshis) and never a Counterparty lookup; the RESERVED_ASSETS send-guard
    deliberately does not apply here."""
    if name.upper() == "BTC":
        return "BTC", True
    info = cp.get_asset(name) or cp.get_asset(name.upper())
    if not info:
        print(f"unknown asset {name!r} (Counterparty has no record)", file=sys.stderr)
        return None
    return info.get("asset") or name, bool(info.get("divisible"))


def _parse_match_id(value: str) -> str | None:
    """An order-match id is '<tx0_hash>_<tx1_hash>' (129 chars); normalize or
    return None."""
    v = value.strip().lower()
    return v if _MATCH_ID_RE.match(v) else None


def _unit_price(give_raw: int, give_div: bool, get_raw: int, get_div: bool) -> str:
    """Human get-per-give unit price for display."""
    give_h = Decimal(_fmt_raw(give_raw, give_div))
    get_h = Decimal(_fmt_raw(get_raw, get_div))
    price = (get_h / give_h).quantize(Decimal("0.00000001"))
    return format(price.normalize(), "f")


def _market_quote(cp, give_asset: str, get_asset: str, give_raw: int) -> dict | None:
    """The combined AMM+book estimate for selling `give_raw`, or None. Raises
    nothing — callers decide whether a missing quote is fatal."""
    try:
        return cp.get_pool_quote(give_asset, get_asset, give_raw)
    except (CounterpartyError, ValueError, TypeError):
        return None


def _print_market(quote: dict, give_asset: str, get_asset: str, get_div: bool,
                  indent: str = "  ") -> None:
    """Where a fill would come from right now, and at what cost. The quote
    splits the output by venue, so the user can see before signing whether the
    pool, the book, or both would fill this — and what the pool charges."""
    total = int(quote.get("estimated_output") or 0)
    pool_out = int(quote.get("pool_output") or 0)
    book_out = int(quote.get("book_output") or 0)
    orders = int(quote.get("book_orders_matched") or 0)
    unfilled = int(quote.get("give_remaining") or 0)
    print(f"{indent}market    : ~{_fmt_raw(total, get_div)} {get_asset} at current state")
    venues = []
    if pool_out:
        venues.append(f"{_fmt_raw(pool_out, get_div)} from the AMM pool")
    if book_out:
        venues.append(f"{_fmt_raw(book_out, get_div)} from {orders} book "
                      f"order{'s' if orders != 1 else ''}")
    if venues:
        print(f"{indent}            {' + '.join(venues)}")
    if quote.get("price_impact") is not None:
        print(f"{indent}impact    : {quote['price_impact']}% price impact")
    if quote.get("fee_bps"):
        print(f"{indent}pool fee  : {quote['fee_bps']} bps")
    if unfilled > 0:
        print(f"{indent}unfilled  : {unfilled} raw {give_asset} would find no "
              f"liquidity at all")


def cmd_open_order(
    config: Config,
    wallet: str,
    give_asset: str,
    give_amount: str,
    get_asset: str,
    get_amount: str,
    expiration: int = 0,
    fee_required: int = 0,
    source: str | None = None,
    fee_rate: float | None = None,
    assume_yes: bool = False,
    dry_run: bool = False,
    fund_from: str | None = None,
    no_fund: bool = False,
) -> int:
    btc = BitcoindClient(config)
    cp = CounterpartyClient(config)

    give = _resolve_order_asset(cp, give_asset)
    if give is None:
        return 1
    get = _resolve_order_asset(cp, get_asset)
    if get is None:
        return 1
    give_asset, give_div = give
    get_asset, get_div = get
    if give_asset == get_asset:
        print(f"cannot trade {give_asset} for itself", file=sys.stderr)
        return 1

    try:
        give_raw = _to_raw_quantity(give_amount, give_div)
        get_raw = _to_raw_quantity(get_amount, get_div)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if not 0 <= expiration <= MAX_EXPIRATION:
        print(f"--expiration must be 0 (never) to {MAX_EXPIRATION} blocks",
              file=sys.stderr)
        return 1
    if fee_required < 0:
        print("--fee-required cannot be negative", file=sys.stderr)
        return 1
    if fee_required > 0 and get_asset != "BTC":
        print("--fee-required only applies to an order that RECEIVES BTC (it is "
              "the miner fee a matching BTC payer must have provided)",
              file=sys.stderr)
        return 1
    for leg_asset, leg_raw in ((give_asset, give_raw), (get_asset, get_raw)):
        if leg_asset == "BTC" and leg_raw < MIN_BTC_LEG_SAT:
            print(f"the BTC side is {leg_raw} sat — Counterparty rejects BTC "
                  f"order legs under {MIN_BTC_LEG_SAT} sat (the compose API "
                  f"does not check this, but consensus invalidates the order)",
                  file=sys.stderr)
            return 1

    if give_asset == "BTC":
        # The BTC is not escrowed, but the source must be able to pay it (plus
        # fees) when matches arrive — pick the address best placed to.
        if source is None:
            source, best = _pick_source(btc, wallet, give_raw)
            if source is None:
                print(f"no wallet address holds the {give_raw} sat this order "
                      f"promises (plus fee) on its own; the richest has {best} "
                      f"sat", file=sys.stderr)
                return 1
    else:
        if source is None:
            source, have = _find_source(btc, cp, wallet, give_asset, give_raw)
            if source is None or have <= 0:
                print(f"wallet {wallet!r} holds no {give_asset}", file=sys.stderr)
                return 1
            if have < give_raw:
                print(f"insufficient balance: offering "
                      f"{_fmt_raw(give_raw, give_div)} {give_asset}, largest "
                      f"single-address balance is {_fmt_raw(have, give_div)} "
                      f"(the escrow is debited from one address)", file=sys.stderr)
                return 1
        else:
            have = _address_asset_balance(cp, source, give_asset)
            if have < give_raw:
                print(f"{source} holds {_fmt_raw(have, give_div)} {give_asset}, "
                      f"less than the {_fmt_raw(give_raw, give_div)} offered",
                      file=sys.stderr)
                return 1

    # Best-effort market context: what the live book + AMM pools would pay for
    # the give side right now. Purely informational — a limit order is still
    # meaningful without it, so any failure is ignored.
    market = _market_quote(cp, give_asset, get_asset, give_raw)

    fund = ensure_funded(btc, cp, wallet, source, fee_rate=fee_rate,
                         fund_from=fund_from, no_fund=no_fund, dry_run=dry_run)
    if fund.code is not None:
        return fund.code
    try:
        composed = compose_retrying(lambda: cp.compose_order(
            source, give_asset, give_raw, get_asset, get_raw, expiration,
            fee_required=fee_required, sat_per_vbyte=fee_rate,
        ), fund.funded)
    except CounterpartyError as e:
        return _report_compose_failure(e, source, give_asset)
    rawtx = composed.get("rawtransaction")
    if not rawtx:
        print(f"compose returned no rawtransaction: {composed}", file=sys.stderr)
        return 1

    give_h = _fmt_raw(give_raw, give_div)
    get_h = _fmt_raw(get_raw, get_div)
    miner_fee = int(composed.get("btc_fee") or 0)

    print(f"open order: {give_h} {give_asset} for {get_h} {get_asset}")
    print(f"  source    : {source}")
    if give_asset == "BTC":
        print(f"  give      : {give_h} BTC — NOT escrowed; each match must be "
              f"paid via `pay-order`")
    else:
        print(f"  give      : {give_h} {give_asset} (escrowed until match, "
              f"cancel, or expiry)")
    print(f"  get       : {get_h} {get_asset}")
    print(f"  price     : {_unit_price(give_raw, give_div, get_raw, get_div)} "
          f"{get_asset} per {give_asset}")
    if market and int(market.get("estimated_output") or 0) > 0:
        _print_market(market, give_asset, get_asset, get_div)
    if expiration == 0:
        print(f"  expires   : never — it rests until filled or `cancel-order`")
    else:
        print(f"  expires   : in {expiration} blocks")
    if fee_required:
        print(f"  fee req.  : matching BTC payers must have provided {fee_required} sat")
    if miner_fee:
        print(f"  miner fee : {miner_fee} sat"
              f"{f' ({fee_rate} sat/vB)' if fee_rate is not None else ''}")
    if give_asset == "BTC":
        print(f"  WARNING   : each match must be settled with `pay-order` within "
              f"~{BTCPAY_WINDOW_BLOCKS} blocks, or the match expires and ALL "
              f"your open BTC-give orders are expired with it (the deadbeat "
              f"penalty). This transaction's miner fee is also the order's "
              f"fee_provided budget — makers demanding more fee_required than "
              f"that will never match it; raise --fee-rate to raise the budget.")

    if not (dry_run or assume_yes or _confirm(
            f"place the order: {give_h} {give_asset} for {get_h} {get_asset}?")):
        print("no order placed")
        return 0
    return _sign_and_broadcast(btc, wallet, source, rawtx, dry_run)


def cmd_swap(
    config: Config,
    wallet: str,
    give_asset: str,
    give_amount: str,
    get_asset: str,
    slippage: float = DEFAULT_SLIPPAGE,
    expiration: int = SWAP_EXPIRATION,
    source: str | None = None,
    fee_rate: float | None = None,
    assume_yes: bool = False,
    dry_run: bool = False,
    fund_from: str | None = None,
    no_fund: bool = False,
) -> int:
    """A market order: sell a fixed amount at whatever the market pays now.

    This composes the SAME `order` message as `open-order` — Counterparty has
    only one trade message, and `compose/order` has no way to prefer the AMM
    pool over the book or the other way round. Consensus alone decides where
    the fill comes from. The difference is entirely in how the order is shaped:
    the price comes from a live quote rather than from you, and `--expiration`
    defaults to 1 block, so anything the market cannot fill immediately expires
    instead of resting on the book. That is what the ecosystem already does —
    the overwhelming majority of pool-filled orders on chain use expiration 1.
    """
    btc = BitcoindClient(config)
    cp = CounterpartyClient(config)

    if not 0 <= slippage <= MAX_SLIPPAGE:
        print(f"--slippage must be between 0 and {MAX_SLIPPAGE:g} percent",
              file=sys.stderr)
        return 1
    for leg in (give_asset, get_asset):
        if leg.upper() == "BTC":
            print("swap cannot trade BTC: an AMM pool never holds BTC, and a "
                  "BTC leg on the book is not settled by the order at all — it "
                  "needs the counterparty to `pay-order` within ~20 blocks. "
                  "Use `open-order` for BTC.", file=sys.stderr)
            return 1

    give = _resolve_order_asset(cp, give_asset)
    if give is None:
        return 1
    get = _resolve_order_asset(cp, get_asset)
    if get is None:
        return 1
    give_asset, give_div = give
    get_asset, get_div = get
    if give_asset == get_asset:
        print(f"cannot trade {give_asset} for itself", file=sys.stderr)
        return 1

    try:
        give_raw = _to_raw_quantity(give_amount, give_div)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if give_raw <= 0:
        print(f"{give_amount} is below the smallest unit {give_asset} can "
              f"represent — it would round to zero", file=sys.stderr)
        return 1
    if not 0 <= expiration <= MAX_EXPIRATION:
        print(f"--expiration must be 0 (never) to {MAX_EXPIRATION} blocks",
              file=sys.stderr)
        return 1

    # A market order has no price of its own, so unlike open-order a missing
    # quote is fatal: there would be nothing to set the limit from.
    market = _market_quote(cp, give_asset, get_asset, give_raw)
    estimated = int((market or {}).get("estimated_output") or 0)
    if not market or estimated <= 0:
        print(f"no live market for {give_asset} -> {get_asset}: neither an AMM "
              f"pool nor a resting order would fill this. Use `open-order` to "
              f"post a limit order and wait.", file=sys.stderr)
        return 1

    get_raw = (estimated if slippage <= 0 else
               int(Decimal(estimated) * (Decimal(100) - Decimal(str(slippage))) / 100))
    if get_raw <= 0:
        print(f"a {slippage:g}% tolerance on ~{_fmt_raw(estimated, get_div)} "
              f"{get_asset} floors to zero — lower --slippage or swap more",
              file=sys.stderr)
        return 1

    if source is not None:
        if source not in set(_wallet_addresses(btc, wallet)):
            print(f"--source {source} is not an address of wallet {wallet!r}",
                  file=sys.stderr)
            return 1
        have = _address_asset_balance(cp, source, give_asset)
    else:
        source, have = _find_source(btc, cp, wallet, give_asset, give_raw)
    if source is None or have <= 0:
        print(f"wallet {wallet!r} holds no {give_asset}", file=sys.stderr)
        return 1
    if have < give_raw:
        print(f"insufficient balance: swapping {_fmt_raw(give_raw, give_div)} "
              f"{give_asset}, largest single-address balance is "
              f"{_fmt_raw(have, give_div)} (the escrow is debited from one "
              f"address)", file=sys.stderr)
        return 1

    give_h = _fmt_raw(give_raw, give_div)
    get_h = _fmt_raw(get_raw, get_div)

    print(f"swap {give_h} {give_asset} for {get_asset}")
    print(f"  source    : {source}")
    print(f"  give      : {give_h} {give_asset} (escrowed until filled or expired)")
    _print_market(market, give_asset, get_asset, get_div)
    print(f"  limit     : at least {get_h} {get_asset}"
          f"{f' ({slippage:g}% tolerance)' if slippage > 0 else ' — NO slippage guard'}")
    print(f"  price     : {_unit_price(give_raw, give_div, get_raw, get_div)} "
          f"{get_asset} per {give_asset} (worst accepted)")
    if expiration == 0:
        print(f"  expires   : NEVER — an unfilled remainder rests on the book "
              f"until `cancel-order`")
    else:
        print(f"  expires   : in {expiration} block{'s' if expiration != 1 else ''} "
              f"— whatever the market cannot fill expires instead of resting")
    if fee_rate is not None:
        print(f"  fee rate  : {fee_rate} sat/vB")
    impact = market.get("price_impact")
    if impact is not None and slippage > 0 and float(impact) > slippage:
        print(f"  WARNING   : the {impact}% price impact of this size already "
              f"exceeds your {slippage:g}% tolerance, so this order is likely "
              f"to fill only partly or not at all. Swap less, or raise "
              f"--slippage deliberately.")

    if not (dry_run or assume_yes or _confirm(
            f"swap {give_h} {give_asset} for at least {get_h} {get_asset}?")):
        print("no swap placed")
        return 0

    fund = ensure_funded(btc, cp, wallet, source, fee_rate=fee_rate,
                         fund_from=fund_from, no_fund=no_fund, dry_run=dry_run)
    if fund.code is not None:
        return fund.code
    try:
        composed = compose_retrying(lambda: cp.compose_order(
            source, give_asset, give_raw, get_asset, get_raw, expiration,
            sat_per_vbyte=fee_rate,
        ), fund.funded)
    except CounterpartyError as e:
        return _report_compose_failure(e, source, give_asset)
    rawtx = composed.get("rawtransaction")
    if not rawtx:
        print(f"compose returned no rawtransaction: {composed}", file=sys.stderr)
        return 1
    miner_fee = int(composed.get("btc_fee") or 0)
    if miner_fee:
        print(f"  miner fee : {miner_fee} sat")
    return _sign_and_broadcast(btc, wallet, source, rawtx, dry_run)


def cmd_cancel_order(
    config: Config,
    wallet: str,
    order_hash: str,
    fee_rate: float | None = None,
    assume_yes: bool = False,
    dry_run: bool = False,
    fund_from: str | None = None,
    no_fund: bool = False,
) -> int:
    btc = BitcoindClient(config)
    cp = CounterpartyClient(config)

    order_hash = order_hash.strip().lower()
    order = cp.get_order(order_hash)
    if order is None:
        print(f"no such order: {order_hash}", file=sys.stderr)
        return 1
    status = order.get("status")
    if status != "open":
        print(f"order {order_hash} is {status!r}, not open — only an open order "
              f"can be cancelled", file=sys.stderr)
        return 1
    source = order.get("source")
    if source not in set(_wallet_addresses(btc, wallet)):
        print(f"order {order_hash} was made by {source}, which is not in wallet "
              f"{wallet!r}", file=sys.stderr)
        return 1

    give_asset = order.get("give_asset")
    give_div = (give_asset == "BTC") or bool(
        (order.get("give_asset_info") or {}).get("divisible"))
    give_remaining = int(order.get("give_remaining") or 0)

    fund = ensure_funded(btc, cp, wallet, source, fee_rate=fee_rate,
                         fund_from=fund_from, no_fund=no_fund, dry_run=dry_run)
    if fund.code is not None:
        return fund.code
    try:
        composed = compose_retrying(lambda: cp.compose_cancel_order(
            source, order_hash, sat_per_vbyte=fee_rate), fund.funded)
    except CounterpartyError as e:
        return _report_compose_failure(e, source, give_asset or "the order")
    rawtx = composed.get("rawtransaction")
    if not rawtx:
        print(f"compose returned no rawtransaction: {composed}", file=sys.stderr)
        return 1

    miner_fee = int(composed.get("btc_fee") or 0)
    print(f"cancel order {order_hash}")
    print(f"  source    : {source}")
    if give_asset == "BTC":
        print(f"  returns   : nothing was escrowed (BTC never is) — this just "
              f"stops future matches")
    else:
        print(f"  returns   : {_fmt_raw(give_remaining, give_div)} {give_asset} "
              f"un-escrows when the cancel confirms")
    if miner_fee:
        print(f"  miner fee : {miner_fee} sat"
              f"{f' ({fee_rate} sat/vB)' if fee_rate is not None else ''}")

    if not (dry_run or assume_yes or _confirm("cancel the order?")):
        print("order left open")
        return 0
    return _sign_and_broadcast(btc, wallet, source, rawtx, dry_run)


def _wallet_btc_debts(cp, addrs: set) -> list[dict]:
    """Pending order matches where a wallet address owes the BTC side. Each
    entry: {id, payer, counterparty, owed_sat, expire_index}."""
    debts = []
    for m in cp.get_order_matches("pending"):
        if m.get("forward_asset") == "BTC" and m.get("tx0_address") in addrs:
            payer, other = m.get("tx0_address"), m.get("tx1_address")
            owed = int(m.get("forward_quantity") or 0)
        elif m.get("backward_asset") == "BTC" and m.get("tx1_address") in addrs:
            payer, other = m.get("tx1_address"), m.get("tx0_address")
            owed = int(m.get("backward_quantity") or 0)
        else:
            continue
        debts.append({
            "id": m.get("id") or f"{m.get('tx0_hash')}_{m.get('tx1_hash')}",
            "payer": payer,
            "counterparty": other,
            "owed_sat": owed,
            "expire_index": int(m.get("match_expire_index") or 0),
        })
    return debts


def cmd_pay_order(
    config: Config,
    wallet: str,
    match_id: str | None = None,
    fee_rate: float | None = None,
    assume_yes: bool = False,
    dry_run: bool = False,
) -> int:
    btc = BitcoindClient(config)
    cp = CounterpartyClient(config)

    if match_id is not None:
        normalized = _parse_match_id(match_id)
        if normalized is None:
            print(f"{match_id!r} is not an order-match id — it looks like "
                  f"<tx0_hash>_<tx1_hash> (two 64-char hashes joined by '_'); "
                  f"`counters wallet orders` lists yours", file=sys.stderr)
            return 1
        match_id = normalized

    addrs = set(_wallet_addresses(btc, wallet))
    debts = _wallet_btc_debts(cp, addrs)

    if match_id is None:
        if not debts:
            print("nothing to pay: no pending order match owes BTC from this wallet")
            return 0
        if len(debts) > 1:
            height = cp.counterparty_height()
            print(f"{len(debts)} pending matches owe BTC — pick one:", file=sys.stderr)
            for d in debts:
                left = d["expire_index"] - height
                print(f"  {d['id']}  {d['owed_sat']} sat, {left} blocks left",
                      file=sys.stderr)
            print("re-run as: counters wallet pay-order <MATCH_ID>", file=sys.stderr)
            return 1
        debt = debts[0]
    else:
        debt = next((d for d in debts if d["id"] == match_id), None)
        if debt is None:
            print(f"no pending match {match_id} owes BTC from wallet {wallet!r} "
                  f"(already paid, expired, or not this wallet's debt)",
                  file=sys.stderr)
            return 1

    height = cp.counterparty_height()
    blocks_left = debt["expire_index"] - height
    if blocks_left <= 0:
        print(f"match {debt['id']} already expired at block {debt['expire_index']} "
              f"(now {height}) — the BTC can no longer be paid", file=sys.stderr)
        return 1

    try:
        composed = cp.compose_btcpay(debt["payer"], debt["id"], sat_per_vbyte=fee_rate)
    except CounterpartyError as e:
        return _report_compose_failure(e, debt["payer"], "BTC")
    rawtx = composed.get("rawtransaction")
    if not rawtx:
        print(f"compose returned no rawtransaction: {composed}", file=sys.stderr)
        return 1

    miner_fee = int(composed.get("btc_fee") or 0)
    total = debt["owed_sat"] + miner_fee
    print(f"pay order match")
    print(f"  match     : {debt['id']}")
    print(f"  paying    : {debt['owed_sat']} sat ({_fmt_btc_sat(debt['owed_sat'])} "
          f"BTC) to {debt['counterparty']}")
    print(f"  from      : {debt['payer']}")
    print(f"  deadline  : {blocks_left} blocks (match expires at block "
          f"{debt['expire_index']})")
    if blocks_left <= 3:
        print(f"  WARNING   : only {blocks_left} blocks left — if this does not "
              f"confirm in time the match expires and your open BTC-give orders "
              f"expire with it")
    if miner_fee:
        print(f"  miner fee : {miner_fee} sat"
              f"{f' ({fee_rate} sat/vB)' if fee_rate is not None else ''}")
        print(f"  TOTAL     : {total} sat ({_fmt_btc_sat(total)} BTC)")
    print(f"  note      : the payment is all-or-nothing; partial BTC settlement "
          f"is not a thing")

    if not (dry_run or assume_yes or _confirm(
            f"pay {_fmt_btc_sat(total)} BTC total to settle the match?")):
        print("nothing paid")
        return 0
    return _sign_and_broadcast(btc, wallet, debt["payer"], rawtx, dry_run)


def cmd_list_orders(config: Config, wallet: str) -> int:
    """The wallet's open orders and its BTC obligations/receivables from
    pending matches. Read-only."""
    btc = BitcoindClient(config)
    cp = CounterpartyClient(config)
    addrs = list(_wallet_addresses(btc, wallet))

    shown = 0
    for addr in addrs:
        for o in cp.get_address_orders(addr, status="open"):
            give_div = (o.get("give_asset") == "BTC") or bool(
                (o.get("give_asset_info") or {}).get("divisible"))
            get_div = (o.get("get_asset") == "BTC") or bool(
                (o.get("get_asset_info") or {}).get("divisible"))
            give_rem = int(o.get("give_remaining") or 0)
            get_rem = int(o.get("get_remaining") or 0)
            expire = o.get("expire_index")
            print(f"{o.get('tx_hash')}")
            print(f"  maker     : {addr}")
            print(f"  offering  : {_fmt_raw(give_rem, give_div)} {o.get('give_asset')} "
                  f"remaining, for {_fmt_raw(max(get_rem, 0), get_div)} "
                  f"{o.get('get_asset')}")
            print(f"  expires   : {f'at block {expire}' if expire else 'never'}")
            shown += 1
    if not shown:
        print(f"wallet {wallet!r} has no open orders")

    debts = _wallet_btc_debts(cp, set(addrs))
    receivables = [
        m for m in cp.get_order_matches("pending")
        if (m.get("forward_asset") == "BTC" and m.get("tx1_address") in addrs)
        or (m.get("backward_asset") == "BTC" and m.get("tx0_address") in addrs)
    ]
    if debts:
        height = cp.counterparty_height()
        print(f"\nyou owe BTC on {len(debts)} pending match"
              f"{'es' if len(debts) != 1 else ''} — settle with `pay-order`:")
        for d in debts:
            left = d["expire_index"] - height
            print(f"  {d['id']}")
            print(f"    {d['owed_sat']} sat from {d['payer']}, {left} blocks left")
    if receivables:
        print(f"\n{len(receivables)} pending match"
              f"{'es' if len(receivables) != 1 else ''} where the counterparty "
              f"owes you BTC (they must pay; you just wait):")
        for m in receivables:
            mid = m.get("id") or f"{m.get('tx0_hash')}_{m.get('tx1_hash')}"
            owed = int((m.get("forward_quantity")
                        if m.get("forward_asset") == "BTC"
                        else m.get("backward_quantity")) or 0)
            print(f"  {mid}")
            print(f"    {owed} sat incoming, match expires at block "
                  f"{m.get('match_expire_index')}")
    return 0
