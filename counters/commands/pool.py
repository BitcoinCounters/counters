"""Counterparty AMM liquidity pools — provide liquidity, withdraw it, inspect it.

A pool is a constant-product reserve pair (`asset_a`/`asset_b`) with its own
**LP token**: a numeric asset Counterparty mints to represent a share of the
reserves. Depositing mints LP tokens; withdrawing burns them and returns both
assets pro rata. Trading against a pool is not done here at all — a pool fill
is an ordinary DEX order that consensus routes to the pool, so it lives in
`order.py` (`swap` and `open-order`).

Three things about the protocol shape every command below:

  - **The first deposit creates the pool** and its two quantities set the
    opening price. There is no separate "create pool" message, and nothing
    validates that price against the wider market: get it wrong and the first
    trade takes the difference. Later deposits are quoted against the existing
    reserves instead.
  - **On a later deposit the quantities are MAXIMUMS.** Core debits only the
    proportional amounts, so over-stating one side is harmless and
    under-stating it silently caps the deposit. That is why omitting the
    second amount (and letting the quote fill it in) is the normal path.
  - **Slippage protection is opt-in in the protocol** (`min_lp_quantity`,
    `min_quantity_a`, `min_quantity_b` all default to 0 = none). It is opt-out
    here: a deposit or withdrawal that lands in the same block as a large
    trade settles against reserves that moved after you signed. The ledger
    already carries real `invalid: slippage protection` withdrawals, so the
    guard is doing work in production.

Pools hold Counterparty assets only — never BTC.
"""

from __future__ import annotations

import sys
from decimal import Decimal, InvalidOperation

from ..bitcoind import BitcoindClient
from ..config import Config
from ..counterparty import CounterpartyClient, CounterpartyError
from .dispenser import _address_asset_balance, _report_compose_failure
from .funding import compose_retrying, ensure_funded
from .send import (
    _confirm_prompt,
    _find_source,
    _fmt_raw,
    _sign_and_broadcast,
    _to_raw_quantity,
    cmd_send,
)
from .wallet import _wallet_addresses

DEFAULT_SLIPPAGE = 1.0          # percent, applied to every quoted minimum
MAX_SLIPPAGE = 100.0


def _confirm(question: str) -> bool:
    return _confirm_prompt(f"\n{question}")


def _resolve_pool_asset(cp, name: str):
    """Canonical (asset, divisible) for a pool leg, or None after printing why.

    BTC is refused explicitly: it is a first-class DEX asset but it has no
    Counterparty balance to escrow, so it can never sit in a pool."""
    if name.upper() == "BTC":
        print("pools hold Counterparty assets only — BTC can never be pooled. "
              "Trade BTC on the order book with `open-order`.", file=sys.stderr)
        return None
    info = cp.get_asset(name) or cp.get_asset(name.upper())
    if not info:
        print(f"unknown asset {name!r} (Counterparty has no record)", file=sys.stderr)
        return None
    return info.get("asset") or name, bool(info.get("divisible"))


def _raw(amount: str, divisible: bool, label: str) -> int:
    """`_to_raw_quantity` plus the zero guard it lacks: a positive amount below
    one satoshi rounds to 0 there, which would compose a do-nothing message."""
    raw = _to_raw_quantity(amount, divisible)
    if raw <= 0:
        raise ValueError(
            f"{label} {amount} is below the smallest unit this asset can "
            f"represent — it would round to zero")
    return raw


def _slippage_floor(estimate: int, slippage: float) -> int:
    """The minimum to demand of a quoted `estimate`. `slippage` 0 means no
    guard at all, which is Counterparty's own default."""
    if slippage <= 0:
        return 0
    return int(Decimal(estimate) * (Decimal(100) - Decimal(str(slippage))) / 100)


def _check_slippage(slippage: float) -> bool:
    if not 0 <= slippage <= MAX_SLIPPAGE:
        print(f"--slippage must be between 0 and {MAX_SLIPPAGE:g} percent "
              f"(0 disables the guard entirely)", file=sys.stderr)
        return False
    return True


def _source_in_wallet(btc, wallet: str, source: str) -> bool:
    """An explicit --source must belong to the wallet. Without this check the
    automatic top-up happily pays BTC to a stranger's address before the
    compose fails."""
    if source in set(_wallet_addresses(btc, wallet)):
        return True
    print(f"--source {source} is not an address of wallet {wallet!r}",
          file=sys.stderr)
    return False


def _find_source_two(btc, cp, wallet: str, asset_a: str, need_a: int,
                     asset_b: str, need_b: int):
    """A wallet address holding BOTH assets — Counterparty debits a message
    from the first input's address, so a balance split across two of your own
    addresses cannot fund one deposit.

    Returns (address_or_None, have_a, have_b, candidates), where `candidates`
    is every address holding either asset, best first, for an error message
    that can tell you which one to consolidate onto.

    Ranking never compares raw quantities of the two assets against each other
    — they are different units, and one asset's satoshis would swamp the
    other's whole units. Candidates are ranked by how many legs they cover,
    then by the fraction of each requirement they meet."""
    rows = []
    for addr in _wallet_addresses(btc, wallet):
        try:
            balances = cp.get_address_balances(addr)
        except CounterpartyError:
            continue
        have_a = have_b = 0
        for r in balances:
            name, longname = r.get("asset"), r.get("asset_longname")
            if name == asset_a or longname == asset_a:
                have_a += int(r.get("quantity") or 0)
            elif name == asset_b or longname == asset_b:
                have_b += int(r.get("quantity") or 0)
        if have_a >= need_a and have_b >= need_b:
            return addr, have_a, have_b, [(addr, have_a, have_b)]
        if have_a or have_b:
            rows.append((addr, have_a, have_b))

    def _score(row):
        _, ha, hb = row
        legs = (ha >= need_a) + (hb >= need_b)
        frac = (min(ha / need_a, 1.0) if need_a else 1.0) + \
               (min(hb / need_b, 1.0) if need_b else 1.0)
        return (legs, frac)

    rows.sort(key=_score, reverse=True)
    if not rows:
        return None, 0, 0, []
    addr, have_a, have_b = rows[0]
    return addr, have_a, have_b, rows


def _pending_credit(cp, address: str, asset: str) -> dict | None:
    """A credit of `asset` to `address` sitting unconfirmed in the mempool.

    This is the only way to see a consolidating send that has been broadcast
    but not yet mined — Counterparty credits balances when a block is parsed,
    so nothing in the confirmed state knows about it. Without this check, a
    second run of the command would happily broadcast a duplicate transfer."""
    try:
        events = cp.get_address_mempool_events(address, "CREDIT")
    except CounterpartyError:
        return None            # best-effort: never block a deposit on this
    for event in events:
        params = event.get("params") or {}
        if params.get("address") == address and params.get("asset") == asset:
            return event
    return None


def _consolidation_plan(candidates, asset_a: str, need_a: int, div_a: bool,
                        asset_b: str, need_b: int, div_b: bool):
    """(target, missing_asset, gap, divisible) when ONE send would make some
    address able to fund the deposit, else None.

    `candidates` is best-first, so the first address short on exactly one leg
    is the one to consolidate onto. An address short on BOTH legs needs two
    sends and two confirmations, which is not worth offering as a single y/n."""
    for addr, have_a, have_b in candidates:
        short_a, short_b = have_a < need_a, have_b < need_b
        if short_a and short_b:
            continue
        if short_a:
            return addr, asset_a, need_a - have_a, div_a
        if short_b:
            return addr, asset_b, need_b - have_b, div_b
    return None


def _price_str(value: Decimal) -> str:
    """A price at readable precision. A pool ratio divides two reserves and
    runs to 30-odd digits otherwise; sub-1 prices keep more places because
    that is where the significant figures live."""
    places = Decimal("0.00000001") if value >= 1 else Decimal("0.000000000001")
    try:
        return format(value.quantize(places).normalize(), "f")
    except InvalidOperation:
        return format(value, "f")


def _ratio(numer: int, div_n: bool, denom: int, div_d: bool) -> Decimal | None:
    n = Decimal(_fmt_raw(numer, div_n))
    d = Decimal(_fmt_raw(denom, div_d))
    return (n / d) if n > 0 and d > 0 else None


def _implied_price(reserve_a: int, div_a: bool, reserve_b: int, div_b: bool,
                   asset_a: str, asset_b: str) -> str:
    """'X B per A' with the reciprocal, from raw reserves."""
    fwd = _ratio(reserve_b, div_b, reserve_a, div_a)
    rev = _ratio(reserve_a, div_a, reserve_b, div_b)
    if fwd is None or rev is None:
        return "n/a"
    return (f"{_price_str(fwd)} {asset_b} per {asset_a}"
            f"  ·  {_price_str(rev)} {asset_a} per {asset_b}")


def _resolve_split_balance(
    config, btc, cp, wallet: str, source: str, candidates,
    asset_a: str, raw_a: int, div_a: bool,
    asset_b: str, raw_b: int, div_b: bool, *,
    amount_a: str, amount_b: str | None,
    fee_rate: float | None, dry_run: bool, consolidate: bool,
    fund_from: str | None, no_fund: bool,
) -> int:
    """Explain a deposit that no single address can fund, and offer the one
    send that would fix it.

    Always returns 1: whatever happens here, the deposit did not happen, and a
    script must not read this as success."""
    print(f"no single address can fund this deposit. A Counterparty message is "
          f"debited from the ONE address that pays for it, so a balance split "
          f"across your addresses cannot fund one deposit.", file=sys.stderr)
    print(f"  needs      : {_fmt_raw(raw_a, div_a)} {asset_a} + "
          f"{_fmt_raw(raw_b, div_b)} {asset_b} at a single address",
          file=sys.stderr)
    if candidates:
        print(f"  your {asset_a}/{asset_b} addresses:", file=sys.stderr)
        for addr, have_a, have_b in candidates[:6]:
            short = f"{addr[:12]}…{addr[-6:]}" if len(addr) > 24 else addr
            print(f"    {short}  {_fmt_raw(have_a, div_a)} {asset_a}"
                  f"  /  {_fmt_raw(have_b, div_b)} {asset_b}", file=sys.stderr)

    plan = _consolidation_plan(candidates, asset_a, raw_a, div_a,
                               asset_b, raw_b, div_b)
    if plan is None:
        print(f"  no single transfer fixes this — every address is short on "
              f"both assets. Consolidate manually with `counters wallet "
              f"--name {wallet} send`.", file=sys.stderr)
        return 1
    target, missing_asset, gap, gap_div = plan
    gap_h = _fmt_raw(gap, gap_div)

    # Already broadcast? Counterparty credits a balance only when the block is
    # parsed, so a pending send is invisible to every balance query — without
    # this the next run would send a second time.
    pending = _pending_credit(cp, target, missing_asset)
    if pending:
        params = pending.get("params") or {}
        txid = pending.get("tx_hash") or params.get("event") or "?"
        print(f"\na transfer of {missing_asset} to {target} is already in the "
              f"mempool, unconfirmed:", file=sys.stderr)
        print(f"  txid       : {txid}", file=sys.stderr)
        print(f"  pending    : {_fmt_raw(int(params.get('quantity') or 0), gap_div)} "
              f"{missing_asset}", file=sys.stderr)
        print(f"  wait for it to confirm — Counterparty credits the balance "
              f"only once the block is parsed — then run this command again. "
              f"Nothing was sent now.", file=sys.stderr)
        return 1

    # Is there anywhere to send it from? A send is itself single-address.
    from_addr, from_bal = _find_source(btc, cp, wallet, missing_asset, gap)
    if from_addr is None or from_addr == target or from_bal < gap:
        print(f"  no address holds the {gap_h} {missing_asset} still needed at "
              f"{target}, so no single transfer fixes this.", file=sys.stderr)
        return 1

    redo = (f"counters wallet --name {wallet} add-liquidity "
            f"{asset_a} {amount_a} {asset_b}"
            f"{f' {amount_b}' if amount_b is not None else ''}"
            f"{f' --fee-rate {fee_rate}' if fee_rate is not None else ''}")

    print(f"\n{target} already holds the rest. Sending it {gap_h} "
          f"{missing_asset} from {from_addr} would let the deposit go ahead.",
          file=sys.stderr)
    if dry_run:
        print(f"[dry-run] would send {gap_h} {missing_asset} to {target}, then "
              f"stop — the deposit needs that transfer to CONFIRM first.",
              file=sys.stderr)
        return 1

    if not (consolidate or _confirm(
            f"send {gap_h} {missing_asset} to {target} now? "
            f"(the deposit needs it to confirm first)")):
        print("nothing sent, no liquidity added", file=sys.stderr)
        return 1

    print()
    rc = cmd_send(config, wallet, target, missing_asset, gap_h,
                  fee_rate=fee_rate, fund_from=fund_from, no_fund=no_fund)
    if rc != 0:
        print(f"the transfer failed — no liquidity was added", file=sys.stderr)
        return 1
    print(f"\nNO LIQUIDITY ADDED YET. That transfer must confirm before the "
          f"deposit can be composed: Counterparty credits a balance only when "
          f"the block is parsed, so it cannot be chained the way a BTC top-up "
          f"can.")
    print(f"Once it confirms, run:\n  {redo}")
    return 1


# --- add liquidity ---------------------------------------------------------


def cmd_add_liquidity(
    config: Config,
    wallet: str,
    asset_a: str,
    amount_a: str,
    asset_b: str,
    amount_b: str | None = None,
    lp_asset: str | None = None,
    slippage: float = DEFAULT_SLIPPAGE,
    source: str | None = None,
    fee_rate: float | None = None,
    assume_yes: bool = False,
    dry_run: bool = False,
    fund_from: str | None = None,
    no_fund: bool = False,
    consolidate: bool = False,
) -> int:
    btc = BitcoindClient(config)
    cp = CounterpartyClient(config)

    if not _check_slippage(slippage):
        return 1
    a = _resolve_pool_asset(cp, asset_a)
    if a is None:
        return 1
    b = _resolve_pool_asset(cp, asset_b)
    if b is None:
        return 1
    asset_a, div_a = a
    asset_b, div_b = b
    if asset_a == asset_b:
        print(f"cannot pool {asset_a} against itself", file=sys.stderr)
        return 1

    try:
        raw_a = _raw(amount_a, div_a, asset_a)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    pool = cp.get_pool(asset_a, asset_b)
    first_deposit = pool is None

    # Canonical pair order is Counterparty's, not the user's: the pool endpoint
    # normalizes either argument order and always answers in (asset_a, asset_b).
    if not first_deposit:
        canon_a = pool.get("asset_a")
        canon_b = pool.get("asset_b")
    else:
        canon_a, canon_b = asset_a, asset_b

    quote = None
    if first_deposit:
        if amount_b is None:
            print(f"{asset_a}/{asset_b} has no pool yet, so this deposit CREATES "
                  f"it and both amounts set the opening price — give the second "
                  f"amount explicitly.", file=sys.stderr)
            return 1
    else:
        try:
            quote = cp.get_pool_deposit_quote(asset_a, asset_b, raw_a)
        except CounterpartyError as e:
            print(f"could not quote the pool: {e}", file=sys.stderr)
            return 1
        if not quote:
            print(f"no deposit quote for {asset_a}/{asset_b}", file=sys.stderr)
            return 1

    try:
        if amount_b is not None:
            raw_b = _raw(amount_b, div_b, asset_b)
        else:
            # The quote answers in canonical order; take the side that is not
            # the asset we priced from.
            raw_b = int(quote["quantity_b_required"] if asset_a == canon_a
                        else quote["quantity_a_required"])
            if raw_b <= 0:
                print(f"the pool ratio puts the {asset_b} side of this deposit "
                      f"below one unit — deposit more {asset_a}", file=sys.stderr)
                return 1
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    # Map the user's (asset, quantity) pairs onto Counterparty's canonical order.
    if asset_a == canon_a:
        q_a, q_b = raw_a, raw_b
        div_ca, div_cb = div_a, div_b
    else:
        q_a, q_b = raw_b, raw_a
        div_ca, div_cb = div_b, div_a

    candidates: list[tuple[str, int, int]] = []
    if source is not None:
        if not _source_in_wallet(btc, wallet, source):
            return 1
        have_a = _address_asset_balance(cp, source, asset_a)
        have_b = _address_asset_balance(cp, source, asset_b)
    else:
        source, have_a, have_b, candidates = _find_source_two(
            btc, cp, wallet, asset_a, raw_a, asset_b, raw_b)
        if source is None:
            print(f"wallet {wallet!r} holds neither {asset_a} nor {asset_b}",
                  file=sys.stderr)
            return 1
    if have_a < raw_a or have_b < raw_b:
        return _resolve_split_balance(
            config, btc, cp, wallet, source, candidates,
            asset_a, raw_a, div_a, asset_b, raw_b, div_b,
            amount_a=amount_a, amount_b=amount_b,
            fee_rate=fee_rate, dry_run=dry_run, consolidate=consolidate,
            fund_from=fund_from, no_fund=no_fund,
        )

    min_lp = 0
    minted = 0
    if quote:
        minted = int(quote.get("quantity_minted_estimate") or 0)
        min_lp = _slippage_floor(minted, slippage)

    print(f"add liquidity to {canon_a}/{canon_b}")
    print(f"  source    : {source}")
    print(f"  deposit   : {_fmt_raw(q_a, div_ca)} {canon_a}")
    print(f"              {_fmt_raw(q_b, div_cb)} {canon_b}")
    if first_deposit:
        print(f"  pool      : NONE YET — this deposit CREATES the pool")
        print(f"  price     : {_implied_price(q_a, div_ca, q_b, div_cb, canon_a, canon_b)}")
        print(f"  LP token  : {lp_asset or 'generated by Counterparty'}")
        print(f"  slippage  : n/a on a first deposit — there is no ratio to "
              f"slip against")
        print(f"  WARNING   : these two amounts SET the opening price and "
              f"nothing checks it against the wider market. If the ratio is "
              f"off, the first trade takes the difference from you.")
    else:
        print(f"  reserves  : {_fmt_raw(int(pool['reserve_a']), div_ca)} {canon_a}"
              f" / {_fmt_raw(int(pool['reserve_b']), div_cb)} {canon_b}")
        print(f"  price     : {_implied_price(int(pool['reserve_a']), div_ca, int(pool['reserve_b']), div_cb, canon_a, canon_b)}")
        print(f"  LP token  : {pool.get('lp_asset')} — ~{_fmt_raw(minted, True)} minted")
        if min_lp:
            print(f"  slippage  : {slippage:g}% — the deposit is invalid below "
                  f"{_fmt_raw(min_lp, True)} LP")
        else:
            print(f"  slippage  : NONE — the deposit settles at whatever ratio "
                  f"the pool holds when it confirms")
        if amount_b is None:
            print(f"  note      : the {asset_b} side came from the live pool "
                  f"ratio; quantities are maximums, so only the proportional "
                  f"amount is debited")
    if fee_rate is not None:
        print(f"  fee rate  : {fee_rate} sat/vB")

    if not (dry_run or assume_yes or _confirm(
            f"deposit {_fmt_raw(q_a, div_ca)} {canon_a} and "
            f"{_fmt_raw(q_b, div_cb)} {canon_b}?")):
        print("no liquidity added")
        return 0

    fund = ensure_funded(btc, cp, wallet, source, fee_rate=fee_rate,
                         fund_from=fund_from, no_fund=no_fund, dry_run=dry_run)
    if fund.code is not None:
        return fund.code
    try:
        composed = compose_retrying(lambda: cp.compose_pooldeposit(
            source, canon_a, canon_b, q_a, q_b,
            min_lp_quantity=min_lp,
            lp_asset=lp_asset if first_deposit else None,
            sat_per_vbyte=fee_rate,
        ), fund.funded)
    except CounterpartyError as e:
        return _report_compose_failure(e, source, canon_a)
    rawtx = composed.get("rawtransaction")
    if not rawtx:
        print(f"compose returned no rawtransaction: {composed}", file=sys.stderr)
        return 1
    miner_fee = int(composed.get("btc_fee") or 0)
    if miner_fee:
        print(f"  miner fee : {miner_fee} sat")
    return _sign_and_broadcast(btc, wallet, source, rawtx, dry_run)


# --- remove liquidity ------------------------------------------------------


def cmd_remove_liquidity(
    config: Config,
    wallet: str,
    asset_a: str,
    asset_b: str,
    amount: str,
    slippage: float = DEFAULT_SLIPPAGE,
    source: str | None = None,
    fee_rate: float | None = None,
    assume_yes: bool = False,
    dry_run: bool = False,
    fund_from: str | None = None,
    no_fund: bool = False,
) -> int:
    btc = BitcoindClient(config)
    cp = CounterpartyClient(config)

    if not _check_slippage(slippage):
        return 1
    a = _resolve_pool_asset(cp, asset_a)
    if a is None:
        return 1
    b = _resolve_pool_asset(cp, asset_b)
    if b is None:
        return 1
    asset_a, div_a = a
    asset_b, div_b = b

    pool = cp.get_pool(asset_a, asset_b)
    if not pool:
        print(f"{asset_a}/{asset_b} has no pool", file=sys.stderr)
        return 1
    canon_a, canon_b = pool["asset_a"], pool["asset_b"]
    div_ca, div_cb = (div_a, div_b) if asset_a == canon_a else (div_b, div_a)
    lp_asset = pool.get("lp_asset")
    if not lp_asset:
        print(f"pool {canon_a}/{canon_b} reports no LP asset", file=sys.stderr)
        return 1

    # The LP token is always divisible (Counterparty mints it that way), but
    # ask rather than assume — a wrong divisibility misreads the amount by 1e8.
    lp_info = cp.get_asset(lp_asset) or {}
    lp_div = bool(lp_info.get("divisible", True))

    if source is not None:
        if not _source_in_wallet(btc, wallet, source):
            return 1
        have = _address_asset_balance(cp, source, lp_asset)
    else:
        source, have = None, 0
        for addr in _wallet_addresses(btc, wallet):
            bal = _address_asset_balance(cp, addr, lp_asset)
            if bal > have:
                source, have = addr, bal
    if not source or have <= 0:
        print(f"wallet {wallet!r} holds no {lp_asset} (the {canon_a}/{canon_b} "
              f"LP token) — nothing to withdraw", file=sys.stderr)
        return 1

    if str(amount).strip().lower() == "all":
        raw_lp = have
    else:
        try:
            raw_lp = _raw(amount, lp_div, "LP amount")
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
    if raw_lp > have:
        print(f"{source} holds {_fmt_raw(have, lp_div)} {lp_asset}, less than "
              f"the {_fmt_raw(raw_lp, lp_div)} being burned", file=sys.stderr)
        return 1

    try:
        quote = cp.get_pool_withdraw_quote(canon_a, canon_b, raw_lp)
    except CounterpartyError as e:
        print(f"could not quote the pool: {e}", file=sys.stderr)
        return 1
    if not quote:
        print(f"no withdrawal quote for {canon_a}/{canon_b}", file=sys.stderr)
        return 1
    est_a = int(quote.get("quantity_a_estimate") or 0)
    est_b = int(quote.get("quantity_b_estimate") or 0)
    min_a = _slippage_floor(est_a, slippage)
    min_b = _slippage_floor(est_b, slippage)

    share = ""
    supply = int(quote.get("supply") or 0)
    if supply > 0:
        share = f" ({Decimal(raw_lp) * 100 / supply:.4f}% of the pool)"

    print(f"remove liquidity from {canon_a}/{canon_b}")
    print(f"  source    : {source}")
    print(f"  burn      : {_fmt_raw(raw_lp, lp_div)} {lp_asset}{share}")
    print(f"  receive   : ~{_fmt_raw(est_a, div_ca)} {canon_a}")
    print(f"              ~{_fmt_raw(est_b, div_cb)} {canon_b}")
    if min_a or min_b:
        print(f"  slippage  : {slippage:g}% — invalid below "
              f"{_fmt_raw(min_a, div_ca)} {canon_a} or "
              f"{_fmt_raw(min_b, div_cb)} {canon_b}")
    else:
        print(f"  slippage  : NONE — you take whatever the reserves hold when "
              f"this confirms")
    print(f"  remaining : {_fmt_raw(have - raw_lp, lp_div)} {lp_asset}")
    if fee_rate is not None:
        print(f"  fee rate  : {fee_rate} sat/vB")

    if not (dry_run or assume_yes or _confirm(
            f"burn {_fmt_raw(raw_lp, lp_div)} {lp_asset} and withdraw?")):
        print("no liquidity removed")
        return 0

    fund = ensure_funded(btc, cp, wallet, source, fee_rate=fee_rate,
                         fund_from=fund_from, no_fund=no_fund, dry_run=dry_run)
    if fund.code is not None:
        return fund.code
    try:
        composed = compose_retrying(lambda: cp.compose_poolwithdraw(
            source, canon_a, canon_b, raw_lp,
            min_quantity_a=min_a, min_quantity_b=min_b, sat_per_vbyte=fee_rate,
        ), fund.funded)
    except CounterpartyError as e:
        return _report_compose_failure(e, source, lp_asset)
    rawtx = composed.get("rawtransaction")
    if not rawtx:
        print(f"compose returned no rawtransaction: {composed}", file=sys.stderr)
        return 1
    miner_fee = int(composed.get("btc_fee") or 0)
    if miner_fee:
        print(f"  miner fee : {miner_fee} sat")
    return _sign_and_broadcast(btc, wallet, source, rawtx, dry_run)


# --- read-only view --------------------------------------------------------


def _divisible(cp, asset: str) -> bool:
    if asset.upper() == "XCP":
        return True
    info = cp.get_asset(asset) or {}
    return bool(info.get("divisible", True))


def cmd_pools(
    config: Config,
    asset_a: str | None = None,
    asset_b: str | None = None,
    limit: int = 20,
) -> int:
    """List pools, or show one pair in detail. Read-only — no wallet."""
    cp = CounterpartyClient(config)

    if asset_a and not asset_b:
        print("give both assets of the pair, or neither", file=sys.stderr)
        return 1

    if not asset_a:
        rows, total = cp.list_pools(limit)
        if not rows:
            print("no pools")
            return 0
        print(f"{len(rows)} of {total} pools")
        print(f"  {'PAIR':<34} {'RESERVES':<44} PRICE")
        for p in rows:
            a, b = p["asset_a"], p["asset_b"]
            da, db = _divisible(cp, a), _divisible(cp, b)
            ra, rb = int(p["reserve_a"]), int(p["reserve_b"])
            pair = f"{a}/{b}"
            res = f"{_fmt_raw(ra, da)} / {_fmt_raw(rb, db)}"
            fwd = _ratio(rb, db, ra, da)
            price = _price_str(fwd) if fwd is not None else "n/a"
            print(f"  {pair[:34]:<34} {res[:44]:<44} {price} {b}/{a}")
        return 0

    pool = cp.get_pool(asset_a, asset_b)
    if not pool:
        print(f"{asset_a}/{asset_b} has no pool")
        return 0
    a, b = pool["asset_a"], pool["asset_b"]
    da, db = _divisible(cp, a), _divisible(cp, b)
    ra, rb = int(pool["reserve_a"]), int(pool["reserve_b"])
    print(f"pool {a}/{b}")
    print(f"  reserves  : {_fmt_raw(ra, da)} {a}")
    print(f"              {_fmt_raw(rb, db)} {b}")
    print(f"  price     : {_implied_price(ra, da, rb, db, a, b)}")
    print(f"  LP token  : {pool.get('lp_asset')}")
    print(f"  created   : block {pool.get('block_index')} by {pool.get('source')}")

    matches = cp.get_pool_matches(a, b, limit=5)
    if matches:
        print(f"  recent swaps:")
        for m in matches:
            fwd, back = m.get("forward_asset"), m.get("backward_asset")
            fq, bq = int(m.get("forward_quantity") or 0), int(m.get("backward_quantity") or 0)
            print(f"    block {m.get('block_index')}  "
                  f"{_fmt_raw(fq, _divisible(cp, fwd))} {fwd} -> "
                  f"{_fmt_raw(bq, _divisible(cp, back))} {back}  "
                  f"({m.get('fee_bps')} bps)")
    deposits = cp.get_pool_deposits(a, b, limit=3)
    if deposits:
        print(f"  recent deposits:")
        for d in deposits:
            print(f"    block {d.get('block_index')}  "
                  f"{_fmt_raw(int(d.get('quantity_a') or 0), da)} {a} + "
                  f"{_fmt_raw(int(d.get('quantity_b') or 0), db)} {b}  "
                  f"[{d.get('status')}]")
    withdrawals = cp.get_pool_withdrawals(a, b, limit=3)
    if withdrawals:
        print(f"  recent withdrawals:")
        for w in withdrawals:
            print(f"    block {w.get('block_index')}  "
                  f"{_fmt_raw(int(w.get('quantity_a') or 0), da)} {a} + "
                  f"{_fmt_raw(int(w.get('quantity_b') or 0), db)} {b}  "
                  f"[{str(w.get('status'))[:48]}]")
    return 0
