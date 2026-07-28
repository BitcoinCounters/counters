"""Counterparty dispensers — the buyer side and the operator side.

A dispenser is a vending machine: pay its address the listed satoshi price and
it releases a lot of an asset to you. What is NOT enough, since Counterparty's
`disable_vanilla_btc_dispense` activated at block 866,000, is simply sending
that BTC. A payment with no Counterparty data is discarded before the dispenser
logic runs (`gettxinfo.py`: "no data and not unspendable"), so the coins land in
the operator's address and nothing is dispensed — a silent, unrecoverable loss.

A purchase is therefore its own message: the same payment output, plus an
OP_RETURN carrying a `dispense` instruction. That is what `buy-from-dispenser`
composes. You say how much of the ASSET you want; the satoshi price comes from
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
from .send import (
    _confirm_prompt,
    _find_source,
    _fmt_raw,
    _is_valid_address,
    _sign_and_broadcast,
    _to_raw_quantity,
)
from .wallet import _wallet_addresses

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
    """`1 XCP for 2780 sat, 28 remaining` — a dispenser's terms in one line."""
    divisible = bool((d.get("asset_info") or {}).get("divisible"))
    give = _fmt_raw(int(d["give_quantity"]), divisible)
    left = _fmt_raw(int(d["give_remaining"]), divisible)
    return (f"{give} {d['asset']} for {int(d['satoshirate'])} sat "
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


def cmd_buy_from_dispenser(
    config: Config,
    wallet: str,
    address: str,
    amount: str,
    asset: str | None = None,
    source: str | None = None,
    fee_rate: float | None = None,
    assume_yes: bool = False,
    dry_run: bool = False,
) -> int:
    btc = BitcoindClient(config)
    cp = CounterpartyClient(config)

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
        for d in dispensers:
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
    pay = int(d["satoshirate"]) * lots
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
    print(f"  dispenser : {address}")
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
        lot_raw = _to_raw_quantity(lot, divisible) if lot is not None else escrow_raw
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

    try:
        composed = cp.compose_dispenser(
            source, asset, lot_raw, escrow_raw, price, status=STATUS_OPEN,
            sat_per_vbyte=fee_rate,
        )
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

    try:
        composed = cp.compose_dispenser(
            source, asset, lot_raw, escrow_raw, rate, status=STATUS_OPEN,
            sat_per_vbyte=fee_rate,
        )
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
    try:
        composed = cp.compose_dispenser(
            source, asset, 0, 0, 0, status=STATUS_CLOSED, sat_per_vbyte=fee_rate,
        )
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
    if "No UTXOs" in msg or "inputs_set" in msg:
        print(f"hint: {source} holds {asset} but has no spendable BTC for the tx "
              f"fee — fund it with a little BTC and retry.", file=sys.stderr)
    return 1
