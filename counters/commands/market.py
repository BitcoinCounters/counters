"""Selling and buying, in plain words: `sell` posts an ask on the DEX, `buy`
posts a bid, `dispense` opens a dispenser.

All three are the wallet's front door to two Counterparty messages that
already have full implementations here — the `order` (order.cmd_open_order)
and the `dispenser` (dispenser.cmd_open_dispenser). Those want every figure
up front: the asset's exact name, the quantity in the asset's own units, a
price per lot in satoshis. A counter's asset is usually a twenty-digit
numeric name nobody types from memory, so each command has two shapes:

  - **One-shot.** Every value on the command line, exactly as the old
    `open-order` / `open-dispenser` took them. `sell` and `buy` are the same
    order message with the two sides swapped — `sell X 1 BTC 0.001` gives X
    and gets BTC, `buy X 1 BTC 0.001` gives BTC and gets X — so both read the
    way people think: sell THIS for THAT, buy THIS with THAT.
  - **Interactive.** Called bare (or with just the asset), the command asks.
    `sell` and `dispense` show what the wallet holds, counter numbers first,
    and offer the largest single-address balance as the default amount,
    because both DEX escrow and dispenser escrow are debited from ONE
    address. `buy` has no menu — the asset is named, by name or by counter
    number — and shows the cheapest resting ask before asking for a price.
    Prices are asked PER UNIT and multiplied out, and the total is echoed.

Either way the answers go to the existing command, whose summary and y/N
prompt are the last word before anything is signed. Nothing new reaches the
chain from here: a `sell` or `buy` IS an order and a `dispense` IS a
dispenser, with the same checks and the same escrow rules.

Interactive mode needs a terminal. Without one (a pipe, a script) the
command refuses and names the one-shot form.
"""

from __future__ import annotations

import sys
from decimal import Decimal, InvalidOperation

from ..bitcoind import COIN, BitcoindClient
from ..config import Config
from ..counterparty import CounterpartyClient, CounterpartyError
from ..store import Store
from . import dispenser, order
from .send import _fmt_raw, _to_raw_quantity
from .wallet import _wallet_addresses


class _Quit(Exception):
    """The user typed q (or closed stdin): leave without doing anything."""


# Indirections so tests can script a session without a terminal.
def _tty() -> bool:
    return sys.stdin.isatty()


def _input(prompt: str) -> str:
    return input(prompt)


def _ask(prompt: str, default: str | None = None) -> str:
    """One question. Enter takes the default (when there is one); q quits."""
    shown = f"{prompt} [{default}]: " if default is not None else f"{prompt}: "
    try:
        answer = _input(shown).strip()
    except EOFError:
        raise _Quit()
    if answer.lower() in ("q", "quit"):
        raise _Quit()
    if not answer and default is not None:
        return default
    return answer


def _ask_amount(prompt: str, divisible: bool, default_raw: int | None,
                max_raw: int | None, asset: str) -> int:
    """A quantity of `asset`, in its own units, returned raw. Re-asks on bad
    input and on more than `max_raw`."""
    default = _fmt_raw(default_raw, divisible) if default_raw is not None else None
    while True:
        text = _ask(prompt, default)
        try:
            raw = _to_raw_quantity(text, divisible)
        except ValueError as e:
            print(f"  {e}")
            continue
        if max_raw is not None and raw > max_raw:
            print(f"  only {_fmt_raw(max_raw, divisible)} {asset} is held on one "
                  f"address, and the escrow comes from one address")
            continue
        return raw


def _ask_decimal(prompt: str, default: str | None = None) -> Decimal:
    while True:
        text = _ask(prompt, default)
        try:
            value = Decimal(text)
        except InvalidOperation:
            print(f"  not a number: {text!r}")
            continue
        if value <= 0:
            print("  must be positive")
            continue
        return value


def _ask_int(prompt: str, default: str, lo: int, hi: int) -> int:
    while True:
        text = _ask(prompt, default)
        if not text.lstrip("-").isdigit() or not lo <= int(text) <= hi:
            print(f"  a whole number from {lo} to {hi}")
            continue
        return int(text)


def _ask_sats(prompt: str) -> int:
    """A price in satoshis. A whole number is sats; a number with a decimal
    point is BTC and is converted, with the reading echoed so a slip between
    the two units is seen before the summary."""
    while True:
        text = _ask(prompt)
        try:
            value = Decimal(text.lower().removesuffix("btc").removesuffix("sats")
                            .removesuffix("sat").strip())
        except InvalidOperation:
            print(f"  not a number: {text!r}")
            continue
        if value <= 0:
            print("  must be positive")
            continue
        if "." in text or text.lower().endswith("btc"):
            sats = value * COIN
            if sats != sats.to_integral_value():
                print(f"  {text} BTC is finer than one satoshi")
                continue
            sats = int(sats)
            print(f"  read as {sats} sat ({format(value.normalize(), 'f')} BTC)")
            return sats
        return int(value)


def _ask_pay_asset(cp, asset: str, asset_id: str) -> tuple[str, bool]:
    """The other side of an order: what the counter is priced in. Defaults to
    BTC; refuses the asset itself and anything Counterparty has no record of."""
    while True:
        name = _ask("priced in which asset", "BTC")
        if name.upper() in (asset.upper(), asset_id.upper()):
            print(f"  cannot trade {asset} for itself")
            continue
        got = order._resolve_order_asset(cp, name)
        if got is None:
            continue
        if got[0] == asset_id:
            print(f"  cannot trade {asset} for itself")
            continue
        return got


def _ask_total(asset: str, qty_raw: int, div: bool, pay_asset: str, pay_div: bool,
               verb: str) -> int:
    """A price per unit of `asset`, multiplied out to the order's other side
    (raw). Re-asks when the total is not expressible in the pay asset, or is a
    BTC leg below Counterparty's consensus minimum."""
    qty_h = _fmt_raw(qty_raw, div)
    while True:
        unit = _ask_decimal(f"price per {asset}, in {pay_asset}")
        total = unit * Decimal(qty_h)
        try:
            pay_raw = _to_raw_quantity(str(total), pay_div)
        except ValueError:
            print(f"  {format(total.normalize(), 'f')} {pay_asset} in total is "
                  f"fractional, and {pay_asset} is indivisible")
            continue
        if pay_asset == "BTC" and pay_raw < order.MIN_BTC_LEG_SAT:
            print(f"  that is {pay_raw} sat in total; Counterparty rejects BTC "
                  f"legs under {order.MIN_BTC_LEG_SAT} sat")
            continue
        print(f"  total: {_fmt_raw(pay_raw, pay_div)} {pay_asset} {verb} "
              f"{qty_h} {asset}")
        return pay_raw


def _ask_expiration(default: int) -> int:
    return _ask_int("expires after how many blocks (0 = never, rests until "
                    "cancel-order)", str(default), 0, order.MAX_EXPIRATION)


# --- what the wallet holds ---------------------------------------------------


def _held(btc, cp, store, wallet: str) -> list[dict]:
    """Every Counterparty asset the wallet has a positive balance of, with the
    wallet-wide total, the largest single-address balance (what one escrow
    can take), and the counter number when the asset is a counter."""
    by_asset: dict[str, dict] = {}
    for addr in _wallet_addresses(btc, wallet):
        try:
            rows = cp.get_address_balances(addr)
        except CounterpartyError:
            continue
        for r in rows:
            q = int(r.get("quantity") or 0)
            if q <= 0:
                continue
            key = r["asset"]
            row = by_asset.setdefault(key, {
                "asset": key,
                "name": r.get("asset_longname") or key,
                "divisible": bool((r.get("asset_info") or {}).get("divisible")),
                "total": 0,
                "max": 0,
            })
            row["total"] += q
            row["max"] = max(row["max"], q)
    for row in by_asset.values():
        rec = store.get_counter_by_asset(row["asset"])
        row["number"] = rec["number"] if rec is not None else None
    # Counters first, by number; then everything else by name.
    return sorted(by_asset.values(),
                  key=lambda r: (r["number"] is None, r["number"] or 0, r["name"]))


def _match_held(rows: list[dict], token: str) -> dict | None:
    """The held row a user named — by asset, longname, or counter number
    (`26` or `#26`)."""
    t = token.strip()
    if t.startswith("#"):
        t = t[1:]
    if t.isdigit():
        for r in rows:
            if r["number"] == int(t):
                return r
    for r in rows:
        if t.upper() in (r["name"].upper(), r["asset"].upper()):
            return r
    return None


def _pick_asset(rows: list[dict], wallet: str, verb: str) -> dict:
    print(f"assets in wallet {wallet!r} ({len(rows)}):\n")
    width = max(len(r["name"]) for r in rows)
    for i, r in enumerate(rows, 1):
        num = f"#{r['number']}" if r["number"] is not None else ""
        held = _fmt_raw(r["total"], r["divisible"])
        split = ""
        if r["max"] < r["total"]:
            split = f"  ({_fmt_raw(r['max'], r['divisible'])} on one address)"
        print(f"  [{i:>2}]  {num:>6}  {r['name']:<{width}}  {held:>14}{split}")
    print()
    while True:
        text = _ask(f"{verb} which? [1-{len(rows)}, or q to quit]")
        if text.isdigit() and 1 <= int(text) <= len(rows):
            return rows[int(text) - 1]
        print(f"  a number from 1 to {len(rows)}")


def _refuse_without_terminal(flag_form: str) -> int:
    print(f"not a terminal: this command asks questions. Non-interactively, "
          f"use  {flag_form}", file=sys.stderr)
    return 1


def _load(config: Config, wallet: str):
    btc = BitcoindClient(config)
    cp = CounterpartyClient(config)
    store = Store(config)
    try:
        rows = _held(btc, cp, store, wallet)
    finally:
        store.close()
    return btc, cp, rows


def _choose_held(config: Config, wallet: str, asset: str | None, verb: str):
    """What to sell or dispense: the named asset if it is held, else the menu.
    Returns (cp, row) or (cp, None) after printing why."""
    btc, cp, rows = _load(config, wallet)
    if not rows:
        print(f"wallet {wallet!r} holds no Counterparty assets")
        return cp, None
    if asset is not None:
        row = _match_held(rows, asset)
        if row is None:
            print(f"wallet {wallet!r} holds no {asset}", file=sys.stderr)
            return cp, None
        return cp, row
    return cp, _pick_asset(rows, wallet, verb)


def _headline(verb: str, row: dict) -> None:
    tag = f" (counter #{row['number']})" if row["number"] is not None else ""
    print(f"\n{verb} {row['name']}{tag}")


# --- sell: an ask on the DEX ---------------------------------------------------


def cmd_sell(config: Config, wallet: str, asset: str | None = None,
             expiration: int = 0, fee_required: int = 0, source: str | None = None,
             fee_rate: float | None = None, assume_yes: bool = False,
             dry_run: bool = False, fund_from: str | None = None,
             no_fund: bool = False) -> int:
    """Pick an asset from the wallet (or take the one named), ask amount and
    price, and post the ask through order.cmd_open_order (summary + y/N there)."""
    if not _tty():
        return _refuse_without_terminal(
            "counters wallet sell <ASSET> <AMOUNT> <GET_ASSET> <GET_AMOUNT>")
    try:
        cp, chosen = _choose_held(config, wallet, asset, "sell")
        if chosen is None:
            return 0 if asset is None else 1
        name, div = chosen["name"], chosen["divisible"]
        _headline("selling", chosen)
        give_raw = _ask_amount(f"amount of {name} to sell", div, chosen["max"],
                               chosen["max"], name)
        get_asset, get_div = _ask_pay_asset(cp, name, chosen["asset"])
        get_raw = _ask_total(name, give_raw, div, get_asset, get_div, "for")
        expiration = _ask_expiration(expiration)
    except _Quit:
        print("\nnothing sold")
        return 0

    print()
    return order.cmd_open_order(
        config, wallet, name, _fmt_raw(give_raw, div), get_asset,
        _fmt_raw(get_raw, get_div), expiration=expiration, fee_required=fee_required,
        source=source, fee_rate=fee_rate, assume_yes=assume_yes, dry_run=dry_run,
        fund_from=fund_from, no_fund=no_fund,
    )


# --- buy: a bid on the DEX -----------------------------------------------------


def _resolve_wanted(cp, store, token: str):
    """The asset a buyer named — a name, or a counter number — as the order
    leg (asset_id, divisible) plus its display name; None after printing why."""
    t = token.strip()
    if t.startswith("#"):
        t = t[1:]
    if t.isdigit():
        rec = store.get_counter(int(t))
        if rec is None:
            print(f"  no counter #{t} in the index")
            return None
        name = rec["asset_longname"] or rec["asset"]
    else:
        name = t
    got = order._resolve_order_asset(cp, name)
    if got is None:
        return None
    if got[0] == "BTC":
        print("  BTC is what you pay with, not what you buy here")
        return None
    return got[0], got[1], name


def _cheapest_ask(cp, asset_id: str, pay_asset: str, div: bool, pay_div: bool) -> str | None:
    """The best resting ask for `asset_id` priced in `pay_asset`, as a line,
    or None. Informational only — any failure is swallowed."""
    try:
        rows, _ = cp.get_asset_orders(asset_id, limit=50)
    except (CounterpartyError, TypeError, ValueError):
        return None
    best = None
    for o in rows or []:
        if o.get("status") != "open":
            continue
        if o.get("give_asset") != asset_id or o.get("get_asset") != pay_asset:
            continue
        give_left = int(o.get("give_remaining") or 0)
        get_left = int(o.get("get_remaining") or 0)
        if give_left <= 0 or get_left <= 0:
            continue
        unit = (Decimal(get_left) / (COIN if pay_div else 1)) / \
               (Decimal(give_left) / (COIN if div else 1))
        if best is None or unit < best[0]:
            best = (unit, give_left)
    if best is None:
        return None
    return (f"  cheapest resting ask: {format(best[0].normalize(), 'f')} {pay_asset} "
            f"per unit ({_fmt_raw(best[1], div)} for sale)")


def cmd_buy(config: Config, wallet: str, asset: str | None = None,
            expiration: int = 0, fee_required: int = 0, source: str | None = None,
            fee_rate: float | None = None, assume_yes: bool = False,
            dry_run: bool = False, fund_from: str | None = None,
            no_fund: bool = False) -> int:
    """Name an asset (or counter number), an amount, what to pay with and a
    price per unit; post the bid through order.cmd_open_order (summary + y/N)."""
    if not _tty():
        return _refuse_without_terminal(
            "counters wallet buy <ASSET> <AMOUNT> <PAY_ASSET> <PAY_AMOUNT>")
    cp = CounterpartyClient(config)
    store = Store(config)
    try:
        wanted = None
        if asset is not None:
            wanted = _resolve_wanted(cp, store, asset)
            if wanted is None:
                return 1
        while wanted is None:
            wanted = _resolve_wanted(cp, store, _ask("buy which asset (name, or a counter number)"))
        asset_id, div, name = wanted
        rec = store.get_counter_by_asset(asset_id)
        print(f"\nbuying {name}" + (f" (counter #{rec['number']})" if rec is not None else ""))
        qty_raw = _ask_amount(f"amount of {name} to buy", div, _to_raw_quantity("1", div),
                              None, name)
        pay_asset, pay_div = _ask_pay_asset(cp, name, asset_id)
        line = _cheapest_ask(cp, asset_id, pay_asset, div, pay_div)
        if line:
            print(line)
        pay_raw = _ask_total(name, qty_raw, div, pay_asset, pay_div, "pays for")
        expiration = _ask_expiration(expiration)
    except _Quit:
        print("\nnothing bought")
        return 0
    finally:
        store.close()

    print()
    return order.cmd_open_order(
        config, wallet, pay_asset, _fmt_raw(pay_raw, pay_div), name,
        _fmt_raw(qty_raw, div), expiration=expiration, fee_required=fee_required,
        source=source, fee_rate=fee_rate, assume_yes=assume_yes, dry_run=dry_run,
        fund_from=fund_from, no_fund=no_fund,
    )


# --- dispense: a dispenser ---------------------------------------------------


def cmd_dispense(config: Config, wallet: str, asset: str | None = None,
                 source: str | None = None, fee_rate: float | None = None,
                 assume_yes: bool = False, dry_run: bool = False,
                 fund_from: str | None = None, no_fund: bool = False) -> int:
    """Pick an asset from the wallet (or take the one named), ask escrow, lot
    and price, and open it through dispenser.cmd_open_dispenser (summary + y/N)."""
    if not _tty():
        return _refuse_without_terminal(
            "counters wallet dispense <ASSET> <AMOUNT> --price SATS [--lot AMOUNT]")
    try:
        cp, chosen = _choose_held(config, wallet, asset, "dispense")
        if chosen is None:
            return 0 if asset is None else 1
        name, div = chosen["name"], chosen["divisible"]
        _headline("dispensing", chosen)

        escrow_raw = _ask_amount(f"amount of {name} to put in the dispenser", div,
                                 chosen["max"], chosen["max"], name)
        one = min(_to_raw_quantity("1", div), escrow_raw)
        lot_raw = _ask_amount(f"{name} per purchase (the lot)", div, one, escrow_raw, name)
        if escrow_raw % lot_raw:
            left = escrow_raw % lot_raw
            print(f"  note: {_fmt_raw(escrow_raw, div)} is not a whole number of "
                  f"lots; {_fmt_raw(left, div)} {name} would sit unvendable "
                  f"until the dispenser closes")
        lots = escrow_raw // lot_raw
        price = _ask_sats("price per lot, in sats (a decimal is read as BTC)")
        print(f"  {lots} lot{'s' if lots != 1 else ''} at {price} sat: "
              f"{format((Decimal(lots * price) / COIN).normalize(), 'f')} BTC if "
              f"sold out. The price can never be changed while open.")
    except _Quit:
        print("\nno dispenser opened")
        return 0

    print()
    return dispenser.cmd_open_dispenser(
        config, wallet, name, _fmt_raw(escrow_raw, div), price,
        lot=_fmt_raw(lot_raw, div), source=source, fee_rate=fee_rate,
        assume_yes=assume_yes, dry_run=dry_run, fund_from=fund_from, no_fund=no_fund,
    )
