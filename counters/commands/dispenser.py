"""`counters wallet buy-from-dispenser` — buy from a Counterparty dispenser.

A dispenser is a vending machine: pay its address the listed satoshi price and
it releases a lot of an asset to you. What is NOT enough, since Counterparty's
`disable_vanilla_btc_dispense` activated at block 866,000, is simply sending
that BTC. A payment with no Counterparty data is discarded before the dispenser
logic runs (`gettxinfo.py`: "no data and not unspendable"), so the coins land in
the operator's address and nothing is dispensed — a silent, unrecoverable loss.

A purchase is therefore its own message: the same payment output, plus an
OP_RETURN carrying a `dispense` instruction. That is what this composes.

You say how much of the ASSET you want; the satoshi price comes from the
dispenser, so there is no amount to mistype and no way to underpay. A dispenser
sells in fixed lots, so the request must be a whole number of them — asking for
a part-lot would have the dispenser keep the remainder, so we refuse instead.
"""

from __future__ import annotations

import sys
from decimal import Decimal

from ..bitcoind import COIN, BitcoindClient
from ..config import Config
from ..counterparty import CounterpartyClient, CounterpartyError
from .inscribe import _spendable_addresses
from .send import _fmt_raw, _is_valid_address, _sign_and_broadcast, _to_raw_quantity

# counterparty-core lib/messages/dispenser.py
STATUS_OPEN = 0
STATUS_OPEN_EMPTY_ADDRESS = 1
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
    if not sys.stdin.isatty():
        print("not a terminal: pass --yes to confirm non-interactively", file=sys.stderr)
        return False
    prompt = f"\nbuy {what} for {_fmt_btc_sat(total_sat)} BTC total? [y/N] "
    try:
        return input(prompt).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _fmt_btc_sat(sats: int) -> str:
    return format(Decimal(sats) / COIN, "f")
