"""Counterparty dispensers — the buyer side and the operator side.

A dispenser is a vending machine: pay its address the listed satoshi price and
it releases a lot of an asset to you. What is NOT enough, since Counterparty's
`disable_vanilla_btc_dispense` activated at block 866,000, is simply sending
that BTC. A payment with no Counterparty data is discarded before the dispenser
logic runs (`gettxinfo.py`: "no data and not unspendable"), so the coins land in
the operator's address and nothing is dispensed — a silent, unrecoverable loss.

A purchase is therefore its own message: the same payment output, plus an
OP_RETURN carrying a `dispense` instruction. That is what `buy-from-dispenser`
composes. Called with nothing to buy it lists what there is instead — every
open dispenser on a counter, or one asset's, or one address's — ordered by
price PER UNIT, because that is the only figure that compares two dispensers:
a lot of ten at 98,000 sats is 9,800 each, not the expensive one. You say how much of the ASSET you want; the satoshi price comes from
the dispenser, so there is no amount to mistype and no way to underpay. A
dispenser sells in fixed lots, so the request must be a whole number of them —
asking for a part-lot would have the dispenser keep the remainder, so we refuse
instead.

The operator side (`open-dispenser`, `refill-dispenser`, `close-dispenser`,
`dispensers`) composes the `dispenser` message itself. Its ground rules, all
enforced by Counterparty consensus:

  - One open dispenser per (address, asset), created by that address itself
    (opening on another address has been invalid since block 866,000).
  - The escrow is debited up front; the source must hold it all.
  - The price can never be changed. A refill is a second open with IDENTICAL
    give_quantity and satoshirate (at most 5 refills; each resets the
    1000-dispense auto-close counter). To reprice: close, wait, reopen.
  - A close takes ~5 blocks (status CLOSING) during which the dispenser still
    vends; the unsold stock then returns to whoever sent the close.
  - Buyers who overpay a part-lot forfeit the excess, and escrow that is not a
    whole number of lots sits unvendable until it returns at depletion.
"""

from __future__ import annotations

import sys
from decimal import Decimal

from ..bitcoind import COIN, BitcoindClient
from ..config import Config
from ..counterparty import CounterpartyClient, CounterpartyError
from .inscribe import _spendable_addresses
from .read import _dispenser_unit_price, _fmt_qty
from .send import (
    _confirm_prompt,
    _find_source,
    _fmt_raw,
    _is_valid_address,
    _sign_and_broadcast,
    _to_raw_quantity,
)
from .funding import compose_retrying, ensure_funded
from .wallet import _wallet_addresses
from ..store import Store

# counterparty-core lib/messages/dispenser.py
STATUS_OPEN = 0
STATUS_OPEN_EMPTY_ADDRESS = 1
STATUS_CLOSED = 10
STATUS_CLOSING = 11
_OPEN = (STATUS_OPEN, STATUS_OPEN_EMPTY_ADDRESS)


def _open_dispensers(cp, address: str, asset: str | None) -> list[dict]:
    """The address's open dispensers, optionally narrowed to one asset."""
    rows = [d for d in cp.get_address_dispensers(address) if d.get("status") in _OPEN]
    if asset:
        want = asset.upper()
        rows = [d for d in rows
                if (d.get("asset") or "").upper() == want
                or ((d.get("asset_info") or {}).get("asset_longname") or "").upper() == want]
    return rows


def _describe(d: dict) -> str:
    """`1 XCP for 2780 sat, 28 remaining` — a dispenser's terms in one line.

    The satoshis are the ones payable now: an oracle dispenser's `satoshirate`
    is a fiat price, and quoting it would name a number nobody can pay with.
    """
    divisible = bool((d.get("asset_info") or {}).get("divisible"))
    give = _fmt_raw(int(d["give_quantity"]), divisible)
    left = _fmt_raw(int(d["give_remaining"]), divisible)
    oracle = " at today's oracle price" if d.get("oracle_address") else ""
    return (f"{give} {d['asset']} for {_sats_per_lot(d)} sat{oracle} "
            f"({left} {d['asset']} remaining)")


def _pick_source(btc, wallet: str, need_sat: int) -> tuple[str | None, int]:
    """The wallet's richest address that can cover the payment plus a fee.
    Returns (address_or_None, best_balance_seen) — a dispense is composed from a
    single source, so one address must hold enough on its own."""
    spendable = _spendable_addresses(btc, wallet)
    if not spendable:
        return None, 0
    best = max(spendable, key=lambda a: spendable[a])
    return (best if spendable[best] >= need_sat else None), spendable[best]


def _sats_per_lot(d: dict) -> int:
    """What one lot costs in satoshis, right now.

    Not `satoshirate`, which for an ORACLE dispenser is a fiat figure — the
    888 on `1F6zw…`'s XCP dispenser is $8.88, and the satoshis it actually
    wants are 12,846. Core resolves the feed for us and reports the result as
    `satoshi_price`, which equals `satoshirate` when there is no oracle. Using
    the wrong one sorts an oracle dispenser to the top of a cheapest-first list
    at a fourteenth of its price, and then underpays it: Counterparty refuses
    the dispense with "not enough BTC to trigger dispenser".
    """
    price = d.get("satoshi_price")
    return int(price if price is not None else d["satoshirate"])


def _unit_price(d: dict, divisible: bool) -> float:
    """Sats per whole unit — what makes dispensers comparable, and what they
    are sorted by. A lot of ten at 98,000 sats is 9,800 each, not the cheap
    one."""
    lot = int(d["give_quantity"]) / (10**8 if divisible else 1)
    return _sats_per_lot(d) / lot if lot else float("inf")


def _divisible(d: dict) -> bool:
    return bool((d.get("asset_info") or {}).get("divisible"))


def _by_price(rows: list[dict]) -> list[dict]:
    """Cheapest per unit first. Sorted on the number, never on its rendering:
    '12,000' sorts before '9,800' as text."""
    return sorted(rows, key=lambda d: _unit_price(d, _divisible(d)))


def _terms(d: dict) -> str:
    """`5,500 sats each (lots of 10) — 1,000 left`, in `info --trading`'s idiom.

    An oracle dispenser says so, because its price is only today's: the feed
    moves and the satoshis move with it.
    """
    divisible = _divisible(d)
    lot = int(d["give_quantity"])
    per_unit = _unit_price(d, divisible)
    shown = f"{int(per_unit):,}" if per_unit == int(per_unit) else f"{per_unit:,.8f}".rstrip("0").rstrip(".")
    notes = []
    if lot != (10**8 if divisible else 1):
        notes.append(f"lots of {_fmt_qty(lot, divisible)}")
    if d.get("oracle_address"):
        notes.append("oracle-priced")
    note = f" ({', '.join(notes)})" if notes else ""
    return (f"{shown} sats each{note} "
            f"— {_fmt_qty(int(d['give_remaining']), divisible)} left")


def _looks_like_asset(token: str) -> bool:
    """Asset names are upper-case letters and digits, a dot for a subasset, and
    never begin with a digit — so a token can be told from an address without
    asking anything."""
    name = token.upper()
    return bool(name) and not name[0].isdigit() and all(
        c.isalnum() or c in ".-_@!" for c in name)


def resolve_target(config: Config, token: str) -> tuple[str, str] | None:
    """Is this an address or an asset? Returns ('address'|'asset', value).

    `buy-from-dispenser xcp` is the obvious thing to type when you want XCP,
    and answering it with "not a valid Bitcoin address" is a refusal to read.
    An address is checked first because bitcoind can settle it outright; only
    then is Counterparty asked whether the token names an asset it knows.
    """
    btc = BitcoindClient(config)
    if _is_valid_address(btc, token):
        return "address", token
    if not _looks_like_asset(token):
        return None
    try:
        info = CounterpartyClient(config).get_asset(token.upper())
    except CounterpartyError:
        return None
    if not info:
        return None
    return "asset", (info.get("asset") or token.upper())


def _cheapest_for(cp, asset: str, payout: int) -> tuple[dict | None, str]:
    """The cheapest open dispenser that can actually fill `payout` of `asset`.

    "Cheapest" is per unit, and "can fill" is two things: it holds enough, and
    the request is a whole number of its lots — a part-lot payment is kept by
    the dispenser rather than refunded. Returns (dispenser, reason_if_none).
    """
    rows, _ = cp.get_asset_dispensers(asset, limit=50)
    if not rows:
        return None, f"no open dispenser sells {asset}"
    usable = [d for d in _by_price(rows)
              if int(d["give_remaining"]) >= payout and payout % int(d["give_quantity"]) == 0]
    if usable:
        return usable[0], ""

    divisible = _divisible(rows[0])
    best = _by_price(rows)[0]
    stock = max(int(d["give_remaining"]) for d in rows)
    if stock < payout:
        return None, (f"no dispenser has {_fmt_raw(payout, divisible)} {asset} left — "
                      f"the largest holds {_fmt_raw(stock, divisible)}")
    lot = int(best["give_quantity"])
    return None, (f"{asset} is dispensed in lots of {_fmt_raw(lot, divisible)} — ask for a "
                  f"multiple of that, not {_fmt_raw(payout, divisible)}")


def cmd_browse_dispensers(config: Config, asset: str | None = None, limit: int = 25) -> int:
    """What there is to buy, cheapest first — the command called by itself.

    Named for the buyer's side: `cmd_list_dispensers` below is the operator's
    `wallet dispensers`, which lists the ones this wallet runs.

    Without an asset this is every open dispenser selling a counter, which is
    the shelf this tool is about: the index already knows every counter's
    asset, so the question is asked once per asset and answered locally.
    `--asset` widens it to any Counterparty asset, counter or not.
    """
    cp = CounterpartyClient(config)

    if asset:
        try:
            rows, total = cp.get_asset_dispensers(asset.upper(), limit=limit)
        except CounterpartyError as e:
            print(f"cannot reach Counterparty: {e}", file=sys.stderr)
            return 1
        if not rows:
            print(f"no open dispensers selling {asset.upper()}")
            return 0
        more = f" (showing the {len(rows)} cheapest)" if total > len(rows) else ""
        print(f"{asset.upper()} — {total} open dispenser{'s' if total != 1 else ''}, "
              f"cheapest first{more}")
        for d in _by_price(rows):
            print(f"  {_terms(d)} @ {d.get('source') or '?'}")
        print()
        print(f"  counters wallet buy-from-dispenser <ADDRESS> <AMOUNT> --asset {asset.upper()}")
        return 0

    store = Store(config)
    try:
        assets = [r["asset"] for r in store.db.execute(
            "SELECT DISTINCT asset FROM counters ORDER BY number DESC")]
    finally:
        store.close()

    found: list[tuple[float, dict, str]] = []
    for name in assets:
        try:
            rows, _ = cp.get_asset_dispensers(name, limit=5)
        except CounterpartyError as e:
            print(f"cannot reach Counterparty: {e}", file=sys.stderr)
            return 1
        for d in rows:
            found.append((_unit_price(d, _divisible(d)), d, name))

    if not found:
        print("no open dispensers on any counter")
        print()
        print("  counters wallet buy-from-dispenser <ADDRESS> <AMOUNT>   buys from any dispenser")
        print("  counters wallet buy-from-dispenser --asset XCP          lists one asset's")
        return 0

    found.sort(key=lambda t: t[0])
    shown = found[:limit]
    more = f" (showing the {limit} cheapest)" if len(found) > limit else ""
    print(f"{len(found)} open dispenser{'s' if len(found) != 1 else ''} selling counters, "
          f"cheapest first{more}")
    print()
    for _, d, name in shown:
        print(f"  {name:<20} {_terms(d)} @ {d.get('source') or '?'}")
    print()
    print("  counters wallet buy-from-dispenser <ADDRESS> <AMOUNT> [--asset ASSET]")
    return 0


def cmd_browse_address_dispensers(config: Config, address: str, asset: str | None = None) -> int:
    """One address's shelf, cheapest first — `buy-from-dispenser ADDRESS` with
    no amount. A listing, not a mistake, so it succeeds."""
    btc = BitcoindClient(config)
    cp = CounterpartyClient(config)
    if not _is_valid_address(btc, address):
        print(f"{address!r} is not a valid Bitcoin address", file=sys.stderr)
        return 1
    try:
        rows = _open_dispensers(cp, address, asset)
    except CounterpartyError as e:
        print(f"cannot reach Counterparty: {e}", file=sys.stderr)
        return 1
    if not rows:
        which = f" for {asset.upper()}" if asset else ""
        print(f"no open dispenser{which} at {address}")
        return 0
    print(f"{address} — {len(rows)} open dispenser{'s' if len(rows) != 1 else ''}, "
          f"cheapest first")
    for d in _by_price(rows):
        print(f"  --asset {d['asset']:<16} {_terms(d)}")
    print()
    print(f"  counters wallet buy-from-dispenser {address} <AMOUNT> [--asset ASSET]")
    return 0


def cmd_buy_from_dispenser(
    config: Config,
    wallet: str,
    address: str | None,
    amount: str,
    asset: str | None = None,
    source: str | None = None,
    fee_rate: float | None = None,
    assume_yes: bool = False,
    dry_run: bool = False,
) -> int:
    btc = BitcoindClient(config)
    cp = CounterpartyClient(config)

    # Naming the asset instead of an address buys from the cheapest dispenser
    # selling it. The confirmation names which one, so the choice is reviewed
    # before it costs anything rather than taken on trust.
    chosen_note = ""
    if address is None:
        if not asset:
            print("say which dispenser to buy from — an address, or an asset "
                  "to take the cheapest", file=sys.stderr)
            return 1
        try:
            rows, _ = cp.get_asset_dispensers(asset.upper(), limit=1)
        except CounterpartyError as e:
            print(f"cannot reach Counterparty: {e}", file=sys.stderr)
            return 1
        if not rows:
            print(f"no open dispenser sells {asset.upper()}", file=sys.stderr)
            return 1
        try:
            payout = _to_raw_quantity(amount, _divisible(rows[0]))
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        pick, why = _cheapest_for(cp, asset.upper(), payout)
        if pick is None:
            print(why, file=sys.stderr)
            return 1
        address = pick["source"]
        chosen_note = f"cheapest of the open {asset.upper()} dispensers"

    if not _is_valid_address(btc, address):
        print(f"{address!r} is not a valid Bitcoin address", file=sys.stderr)
        return 1

    dispensers = _open_dispensers(cp, address, asset)
    if not dispensers:
        which = f" for {asset}" if asset else ""
        print(f"no open dispenser{which} at {address}", file=sys.stderr)
        return 1
    if len(dispensers) > 1:
        print(f"{address} runs {len(dispensers)} open dispensers — pick one with "
              f"--asset:", file=sys.stderr)
        for d in _by_price(dispensers):
            print(f"  --asset {d['asset']:<16} {_describe(d)}", file=sys.stderr)
        return 1

    d = dispensers[0]
    divisible = bool((d.get("asset_info") or {}).get("divisible"))
    lot = int(d["give_quantity"])
    remaining = int(d["give_remaining"])

    try:
        payout = _to_raw_quantity(amount, divisible)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    # The dispenser sells whole lots: a part-lot payment would be kept, not
    # refunded, so name the multiples the buyer can actually have.
    if payout % lot:
        print(f"{address} dispenses {_fmt_raw(lot, divisible)} {d['asset']} per lot — "
              f"ask for a multiple of that ("
              f"{', '.join(_fmt_raw(lot * n, divisible) for n in (1, 2, 3))}, …), "
              f"not {_fmt_raw(payout, divisible)}", file=sys.stderr)
        return 1
    lots = payout // lot
    pay = _sats_per_lot(d) * lots
    if payout > remaining:
        print(f"dispenser only has {_fmt_raw(remaining, divisible)} {d['asset']} left, "
              f"less than the {_fmt_raw(payout, divisible)} asked for", file=sys.stderr)
        return 1

    if source is None:
        source, best = _pick_source(btc, wallet, pay)
        if source is None:
            print(f"no wallet address holds the {pay} sat payment (plus fee) on its "
                  f"own; the richest has {best} sat. A dispense is composed from a "
                  f"single source, so consolidate funds and retry.", file=sys.stderr)
            return 1

    try:
        composed = cp.compose_dispense(source, address, pay, sat_per_vbyte=fee_rate)
    except CounterpartyError as e:
        print(f"compose failed: {e}", file=sys.stderr)
        return 1
    rawtx = composed.get("rawtransaction")
    if not rawtx:
        print(f"compose returned no rawtransaction: {composed}", file=sys.stderr)
        return 1

    # Counterparty reports the miner fee it built in, so the buyer sees the
    # whole cost — price plus fee — before committing, not just the price.
    miner_fee = int(composed.get("btc_fee") or 0)
    total = pay + miner_fee
    what = f"{_fmt_raw(payout, divisible)} {d['asset']}"

    print(f"buy {what}")
    print(f"  dispenser : {address}{f' ({chosen_note})' if chosen_note else ''}")
    print(f"  terms     : {_describe(d)}")
    print(f"  receiving : {what}")
    print(f"  price     : {pay} sat ({_fmt_btc_sat(pay)} BTC)"
          f"{f' — {lots} lots' if lots > 1 else ''}")
    if miner_fee:
        print(f"  miner fee : {miner_fee} sat"
              f"{f' ({fee_rate} sat/vB)' if fee_rate is not None else ''}")
        print(f"  TOTAL     : {total} sat ({_fmt_btc_sat(total)} BTC)")
    print(f"  from      : {source}")

    if not (dry_run or assume_yes or _confirm(what, total)):
        print("nothing bought")
        return 0
    return _sign_and_broadcast(btc, wallet, source, rawtx, dry_run)


def _confirm(what: str, total_sat: int) -> bool:
    """Last word before real money moves: the buyer sees what they get and the
    total BTC it costs, in one sentence."""
    return _confirm_prompt(f"\nbuy {what} for {_fmt_btc_sat(total_sat)} BTC total?")


def _confirm_admin(question: str) -> bool:
    """Confirmation gate for the operator commands (open/refill/close), kept
    separate from the buyer's `_confirm` so each can be stubbed in tests."""
    return _confirm_prompt(f"\n{question}")


def _fmt_btc_sat(sats: int) -> str:
    return format(Decimal(sats) / COIN, "f")


# --- operator side ---------------------------------------------------------


def _resolve_dispensable_asset(cp, asset: str):
    """Canonical (asset, divisible) for an asset the wallet may dispense, or
    None after printing why. Only BTC is off-limits — XCP dispensers are legal
    and common, so the RESERVED_ASSETS send-guard deliberately does not apply."""
    if asset.upper() == "BTC":
        print("a dispenser vends Counterparty assets for BTC — it cannot vend "
              "BTC itself", file=sys.stderr)
        return None
    info = cp.get_asset(asset) or cp.get_asset(asset.upper())
    if not info:
        print(f"unknown asset {asset!r} (Counterparty has no record)", file=sys.stderr)
        return None
    return info.get("asset") or asset, bool(info.get("divisible"))


def _status_word(d: dict) -> str:
    s = int(d.get("status") or 0)
    if s in _OPEN:
        return "open"
    if s == STATUS_CLOSING:
        close_at = d.get("close_block_index")
        return f"closing (stock returns at block {close_at})" if close_at else "closing"
    return "closed"


def _locate_dispenser(btc, cp, wallet: str, asset: str, source: str | None):
    """The wallet's dispenser for `asset` — the (address, row) a refill or
    close should act on — or (None, None) after printing why. `source` pins
    the address; otherwise every wallet address is checked, and an ambiguous
    result (dispensers for the asset on several addresses) asks for --source."""
    if source is not None:
        row = cp.get_dispenser(source, asset)
        if row is None or int(row.get("status") or 0) == STATUS_CLOSED:
            print(f"{source} has no open dispenser for {asset}", file=sys.stderr)
            return None, None
        return source, row
    found: list[tuple[str, dict]] = []
    for addr in _wallet_addresses(btc, wallet):
        row = cp.get_dispenser(addr, asset)
        if row is not None and int(row.get("status") or 0) != STATUS_CLOSED:
            found.append((addr, row))
    if not found:
        print(f"wallet {wallet!r} has no open dispenser for {asset} — "
              f"see `open-dispenser`", file=sys.stderr)
        return None, None
    if len(found) > 1:
        print(f"{len(found)} wallet addresses run a {asset} dispenser — pick one "
              f"with --source:", file=sys.stderr)
        for addr, row in found:
            print(f"  --source {addr}  {_describe(row)}", file=sys.stderr)
        return None, None
    return found[0]


def _lot_multiple_note(escrow_raw: int, lot_raw: int, asset: str, divisible: bool) -> None:
    """Escrow that is not a whole number of lots is not lost, just stranded —
    say so once, before the confirmation."""
    rem = escrow_raw % lot_raw
    if rem:
        print(f"  note      : {_fmt_raw(escrow_raw, divisible)} is not a whole number "
              f"of {_fmt_raw(lot_raw, divisible)}-lots; the "
              f"{_fmt_raw(rem, divisible)} {asset} remainder cannot vend and only "
              f"returns when the dispenser depletes")


def cmd_open_dispenser(
    config: Config,
    wallet: str,
    asset: str,
    amount: str,
    price: int,
    lot: str | None = None,
    source: str | None = None,
    fee_rate: float | None = None,
    assume_yes: bool = False,
    dry_run: bool = False,
    fund_from: str | None = None,
    no_fund: bool = False,
) -> int:
    btc = BitcoindClient(config)
    cp = CounterpartyClient(config)

    resolved = _resolve_dispensable_asset(cp, asset)
    if resolved is None:
        return 1
    asset, divisible = resolved

    if price <= 0:
        print("--price must be a positive number of satoshis", file=sys.stderr)
        return 1
    try:
        escrow_raw = _to_raw_quantity(amount, divisible)
        # One unit per purchase, unless told otherwise.
        #
        # The lot decides what --price means, since a dispenser's price is per
        # lot: with a lot of one, `--price 5000` reads as "5,000 sats each",
        # which is what almost every listing intends. Defaulting to the whole
        # escrow made the same command mean "5,000 sats for all 100" — an
        # all-or-nothing sale at a per-unit price, and the terms can never be
        # changed once open. `--lot` still says otherwise for a machine that
        # really does vend in tens.
        #
        # An escrow below one whole unit cannot vend in whole ones, so there
        # the escrow is the lot and the dispenser stays openable.
        lot_raw = (_to_raw_quantity(lot, divisible) if lot is not None
                   else min(_to_raw_quantity("1", divisible), escrow_raw))
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if lot_raw > escrow_raw:
        print(f"--lot {_fmt_raw(lot_raw, divisible)} is more than the "
              f"{_fmt_raw(escrow_raw, divisible)} escrowed", file=sys.stderr)
        return 1

    if source is None:
        source, have = _find_source(btc, cp, wallet, asset, escrow_raw)
        if source is None or have <= 0:
            print(f"wallet {wallet!r} holds no {asset}", file=sys.stderr)
            return 1
        if have < escrow_raw:
            print(f"insufficient balance: escrowing {_fmt_raw(escrow_raw, divisible)} "
                  f"{asset}, largest single-address balance is "
                  f"{_fmt_raw(have, divisible)} (the escrow is debited from one "
                  f"address)", file=sys.stderr)
            return 1
    else:
        have = _address_asset_balance(cp, source, asset)
        if have < escrow_raw:
            print(f"{source} holds {_fmt_raw(have, divisible)} {asset}, less than "
                  f"the {_fmt_raw(escrow_raw, divisible)} to escrow", file=sys.stderr)
            return 1

    existing = cp.get_dispenser(source, asset)
    if existing is not None and int(existing.get("status") or 0) != STATUS_CLOSED:
        if int(existing.get("status") or 0) == STATUS_CLOSING:
            print(f"{source} already has a {asset} dispenser closing — no action "
                  f"is possible until block {existing.get('close_block_index')}",
                  file=sys.stderr)
        else:
            print(f"{source} already runs a {asset} dispenser "
                  f"({_describe(existing)}). Counterparty allows one per address "
                  f"per asset: `refill-dispenser` adds stock on the same terms, "
                  f"`close-dispenser` (then ~5 blocks) frees it for a new price.",
                  file=sys.stderr)
        return 1

    fund = ensure_funded(btc, cp, wallet, source, fee_rate=fee_rate,
                         fund_from=fund_from, no_fund=no_fund, dry_run=dry_run)
    if fund.code is not None:
        return fund.code
    try:
        composed = compose_retrying(lambda: cp.compose_dispenser(
            source, asset, lot_raw, escrow_raw, price, status=STATUS_OPEN,
            sat_per_vbyte=fee_rate,
        ), fund.funded)
    except CounterpartyError as e:
        return _report_compose_failure(e, source, asset)
    rawtx = composed.get("rawtransaction")
    if not rawtx:
        print(f"compose returned no rawtransaction: {composed}", file=sys.stderr)
        return 1

    lots = escrow_raw // lot_raw
    proceeds = lots * price
    miner_fee = int(composed.get("btc_fee") or 0)

    print(f"open dispenser: {asset}")
    print(f"  source    : {source}")
    print(f"  terms     : {_fmt_raw(lot_raw, divisible)} {asset} for {price} sat per lot")
    print(f"  escrow    : {_fmt_raw(escrow_raw, divisible)} {asset} "
          f"({lots} lot{'s' if lots != 1 else ''})")
    print(f"  sold out  : {proceeds} sat ({_fmt_btc_sat(proceeds)} BTC)")
    if miner_fee:
        print(f"  miner fee : {miner_fee} sat"
              f"{f' ({fee_rate} sat/vB)' if fee_rate is not None else ''}")
    _lot_multiple_note(escrow_raw, lot_raw, asset, divisible)
    print(f"  note      : the escrow leaves {source} now; closing returns unsold "
          f"stock after ~5 blocks, and the price cannot be changed while open")

    if not (dry_run or assume_yes or _confirm_admin(
            f"open the dispenser, escrowing {_fmt_raw(escrow_raw, divisible)} {asset}?")):
        print("no dispenser opened")
        return 0
    return _sign_and_broadcast(btc, wallet, source, rawtx, dry_run)


def cmd_refill_dispenser(
    config: Config,
    wallet: str,
    asset: str,
    amount: str,
    source: str | None = None,
    fee_rate: float | None = None,
    assume_yes: bool = False,
    dry_run: bool = False,
    fund_from: str | None = None,
    no_fund: bool = False,
) -> int:
    btc = BitcoindClient(config)
    cp = CounterpartyClient(config)

    resolved = _resolve_dispensable_asset(cp, asset)
    if resolved is None:
        return 1
    asset, _ = resolved

    source, d = _locate_dispenser(btc, cp, wallet, asset, source)
    if source is None:
        return 1
    if int(d.get("status") or 0) == STATUS_CLOSING:
        print(f"the {asset} dispenser at {source} is closing — wait for block "
              f"{d.get('close_block_index')}, then open a fresh one", file=sys.stderr)
        return 1

    # A refill is a second open with the SAME terms — Counterparty rejects any
    # deviation, so the live row is the only source of truth for them.
    lot_raw = int(d["give_quantity"])
    rate = int(d["satoshirate"])
    divisible = bool((d.get("asset_info") or {}).get("divisible"))

    try:
        escrow_raw = _to_raw_quantity(amount, divisible)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    have = _address_asset_balance(cp, source, asset)
    if have < escrow_raw:
        print(f"{source} holds {_fmt_raw(have, divisible)} {asset}, less than the "
              f"{_fmt_raw(escrow_raw, divisible)} to add", file=sys.stderr)
        return 1

    fund = ensure_funded(btc, cp, wallet, source, fee_rate=fee_rate,
                         fund_from=fund_from, no_fund=no_fund, dry_run=dry_run)
    if fund.code is not None:
        return fund.code
    try:
        composed = compose_retrying(lambda: cp.compose_dispenser(
            source, asset, lot_raw, escrow_raw, rate, status=STATUS_OPEN,
            sat_per_vbyte=fee_rate,
        ), fund.funded)
    except CounterpartyError as e:
        return _report_compose_failure(e, source, asset)
    rawtx = composed.get("rawtransaction")
    if not rawtx:
        print(f"compose returned no rawtransaction: {composed}", file=sys.stderr)
        return 1

    miner_fee = int(composed.get("btc_fee") or 0)
    print(f"refill dispenser: {asset}")
    print(f"  source    : {source}")
    print(f"  terms     : {_describe(d)}")
    print(f"  adding    : {_fmt_raw(escrow_raw, divisible)} {asset}")
    if miner_fee:
        print(f"  miner fee : {miner_fee} sat"
              f"{f' ({fee_rate} sat/vB)' if fee_rate is not None else ''}")
    _lot_multiple_note(escrow_raw, lot_raw, asset, divisible)
    print(f"  note      : a dispenser can be refilled at most 5 times; each refill "
          f"resets its 1000-dispense auto-close counter")

    if not (dry_run or assume_yes or _confirm_admin(
            f"refill with {_fmt_raw(escrow_raw, divisible)} {asset}?")):
        print("nothing refilled")
        return 0
    return _sign_and_broadcast(btc, wallet, source, rawtx, dry_run)


def cmd_close_dispenser(
    config: Config,
    wallet: str,
    asset: str,
    source: str | None = None,
    fee_rate: float | None = None,
    assume_yes: bool = False,
    dry_run: bool = False,
    fund_from: str | None = None,
    no_fund: bool = False,
) -> int:
    btc = BitcoindClient(config)
    cp = CounterpartyClient(config)

    resolved = _resolve_dispensable_asset(cp, asset)
    if resolved is None:
        return 1
    asset, _ = resolved

    source, d = _locate_dispenser(btc, cp, wallet, asset, source)
    if source is None:
        return 1
    if int(d.get("status") or 0) == STATUS_CLOSING:
        print(f"the {asset} dispenser at {source} is already closing — stock "
              f"returns at block {d.get('close_block_index')}", file=sys.stderr)
        return 1

    divisible = bool((d.get("asset_info") or {}).get("divisible"))
    remaining = int(d.get("give_remaining") or 0)

    # A close still carries the three quantity fields; zeros are the protocol's
    # close convention.
    fund = ensure_funded(btc, cp, wallet, source, fee_rate=fee_rate,
                         fund_from=fund_from, no_fund=no_fund, dry_run=dry_run)
    if fund.code is not None:
        return fund.code
    try:
        composed = compose_retrying(lambda: cp.compose_dispenser(
            source, asset, 0, 0, 0, status=STATUS_CLOSED, sat_per_vbyte=fee_rate,
        ), fund.funded)
    except CounterpartyError as e:
        return _report_compose_failure(e, source, asset)
    rawtx = composed.get("rawtransaction")
    if not rawtx:
        print(f"compose returned no rawtransaction: {composed}", file=sys.stderr)
        return 1

    miner_fee = int(composed.get("btc_fee") or 0)
    print(f"close dispenser: {asset}")
    print(f"  source    : {source}")
    print(f"  terms     : {_describe(d)}")
    print(f"  returns   : {_fmt_raw(remaining, divisible)} {asset} (to {source})")
    if miner_fee:
        print(f"  miner fee : {miner_fee} sat"
              f"{f' ({fee_rate} sat/vB)' if fee_rate is not None else ''}")
    print(f"  note      : the dispenser keeps vending for ~5 more blocks (status "
          f"CLOSING), then the unsold stock returns")

    if not (dry_run or assume_yes or _confirm_admin(f"close the {asset} dispenser?")):
        print("dispenser left open")
        return 0
    return _sign_and_broadcast(btc, wallet, source, rawtx, dry_run)


def cmd_list_dispensers(config: Config, wallet: str) -> int:
    """Every dispenser run by a wallet address, any status. Read-only."""
    btc = BitcoindClient(config)
    cp = CounterpartyClient(config)
    rows: list[tuple[str, dict]] = []
    for addr in _wallet_addresses(btc, wallet):
        for d in cp.get_address_dispensers(addr):
            rows.append((addr, d))
    if not rows:
        print(f"wallet {wallet!r} runs no dispensers")
        return 0
    for addr, d in rows:
        print(f"{addr}  {d.get('asset')}")
        print(f"  terms     : {_describe(d)}")
        print(f"  status    : {_status_word(d)}")
    return 0


def _address_asset_balance(cp, address: str, asset: str) -> int:
    """The address's raw balance of one asset (0 if none)."""
    try:
        rows = cp.get_address_balances(address)
    except CounterpartyError:
        return 0
    return sum(int(r.get("quantity") or 0) for r in rows
               if r.get("asset") == asset or r.get("asset_longname") == asset)


def _report_compose_failure(e: CounterpartyError, source: str, asset: str) -> int:
    msg = str(e)
    print(f"compose failed: {msg}", file=sys.stderr)
    if "No UTXOs" in msg or "inputs_set" in msg or "Insufficient funds" in msg:
        print(f"hint: {source} holds {asset} but has no spendable BTC for the tx "
              f"fee — it pays its own way, so drop --no-fund to top it up "
              f"automatically.", file=sys.stderr)
    return 1
