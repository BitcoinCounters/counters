"""Topping a Counterparty source address up, without tripping its dispenser.

Every Counterparty message is debited from ONE address and paid for by that
same address's coins: the first input IS the source, so the fee can never come
from a second address. An asset therefore routinely ends up somewhere holding
no BTC at all — issuance rights transferred in, tokens received, a dispenser
address that only ever collects — and every compose from it fails with "No
UTXOs found" or "Insufficient funds" until a little BTC is moved across. Making
that move is what this module does, and it is the DEFAULT everywhere rather
than a flag the caller has to know exists.

The wrinkle is DISPENSERS. A dispenser fires on any BTC arriving from another
address — that is the entire mechanism — so the obvious top-up doubles as a
purchase: send 5,000 sat to an address dispensing at 1,000 and you buy five of
your own tokens and shrink the dispenser by five. Counterparty has no notion of
a wallet, so "it is my own address" earns no exemption. A dispense needs
floor(sent / satoshirate) >= 1, so anything strictly below the rate moves the
coins and dispenses nothing. We fund under that line and refuse rather than
cross it. Deliberate dispenser use — buying, opening, refilling, closing — is
untouched; only the automatic top-up is fenced.
"""

from __future__ import annotations

import sys
import time
from decimal import Decimal
from typing import Callable, NamedTuple

from ..bitcoind import COIN, BitcoindClient, BitcoindError
from ..counterparty import CounterpartyClient, CounterpartyError

DUST_SAT = 330            # below this an output is unspendable dust
# Counterparty lists the source's coins straight from bitcoind with no cache, so
# a just-broadcast funding output can be invisible for a beat. Long enough to
# cover that, short enough that a genuinely unfunded source still fails fast.
VISIBLE_TRIES = 6
VISIBLE_WAIT = 2.0        # seconds between tries
# A Counterparty OP_RETURN transaction: an input or two, the OP_RETURN, change.
# Generous, but not so generous it clears a dispenser's rate for no reason —
# whatever is not spent simply stays on the source.
TYPICAL_VSIZE = 250


def spendable_by_address(btc: BitcoindClient, wallet: str) -> dict[str, int]:
    """{address: total sats} of confirmed+unconfirmed spendable coins."""
    totals: dict[str, int] = {}
    for u in btc.wallet_call(wallet, "listunspent", [0, 9999999]):
        addr = u.get("address")
        if addr and u.get("spendable", True):
            totals[addr] = totals.get(addr, 0) + int(round(u.get("amount", 0) * COIN))
    return totals


def estimate_need(fee_rate: float | None = None, outputs: int = 0) -> int:
    """What the source must hold to pay for its own transaction. `outputs` counts
    dust-carrying outputs the message adds beyond the OP_RETURN (a send's
    destination, say). Never below dust: the funding output itself has to be
    spendable."""
    est = int(TYPICAL_VSIZE * (fee_rate or 1.0) * 1.15) + DUST_SAT * outputs
    return max(est, DUST_SAT)


def dispense_floor(cp: CounterpartyClient, address: str) -> int | None:
    """The largest payment to `address` that CANNOT trigger a dispense — one sat
    under the cheapest open dispenser's rate. None when nothing is dispensing
    there, meaning no cap applies. A drained dispenser cannot fire, so it does
    not constrain us."""
    try:
        rows = cp.get_address_dispensers(address) or []
    except CounterpartyError:
        return None                      # cannot prove it is safe OR unsafe; do not block
    rates = []
    for d in rows:
        if str(d.get("status", "")) not in ("0", "open"):
            continue
        if int(d.get("give_remaining") or 0) <= 0:
            continue
        rate = int(d.get("satoshirate") or 0)
        if rate > 0:
            rates.append(rate)
    return min(rates) - 1 if rates else None


def _fund_source(btc: BitcoindClient, wallet: str, source: str, amount: int,
                 from_address: str | None, fee_rate: float | None) -> str | None:
    """Move `amount` sats to the source address so it can pay its own way.

    Counterparty derives a message's source from the FIRST input, and it orders
    inputs by value — so an asset-holding address cannot simply be topped up by
    pinning someone else's bigger coin. Consolidating first sidesteps the
    ordering entirely: afterwards the source owns coins outright. Compose allows
    unconfirmed inputs, so the new coin is usable straight away. Returns the
    funding txid, or None on failure."""
    from .send import _change_type, _fmt_btc   # local: send.py imports this module

    options: dict = {}
    if from_address is not None:
        utxos = [u for u in btc.wallet_call(wallet, "listunspent", [0, 9999999])
                 if u.get("address") == from_address and u.get("spendable", True)]
        if not utxos:
            print(f"--fund-from {from_address} has no spendable coins in wallet "
                  f"{wallet!r}", file=sys.stderr)
            return None
        options["inputs"] = [{"txid": u["txid"], "vout": u["vout"]} for u in utxos]
        options["add_inputs"] = False
    change_type = _change_type(btc, wallet)
    if change_type:
        options["change_type"] = change_type

    params = [{source: _fmt_btc(Decimal(amount) / COIN)}, None, "unset", fee_rate, options]
    try:
        result = btc.wallet_call(wallet, "send", params)
    except BitcoindError as e:
        print(f"funding the source failed: {e}", file=sys.stderr)
        return None
    txid = result.get("txid")
    if not txid:
        print(f"funding transaction was not broadcast: {result}", file=sys.stderr)
        return None
    return txid


class Funding(NamedTuple):
    """`code` is None to carry on and compose, otherwise the exit code the
    caller should return immediately. `funded` records whether we just moved
    coins, which is what makes a compose worth retrying."""
    code: int | None = None
    funded: bool = False


def ensure_funded(
    btc: BitcoindClient,
    cp: CounterpartyClient,
    wallet: str,
    source: str,
    *,
    fee_rate: float | None = None,
    fund_from: str | None = None,
    no_fund: bool = False,
    dry_run: bool = False,
    outputs: int = 0,
    need: int | None = None,
) -> Funding:
    """Give `source` enough BTC to pay for its own transaction."""
    if no_fund:
        return Funding()              # compose against what is there, fail on its own terms
    if fund_from == "auto":           # accepted for compatibility
        fund_from = None
    try:
        have = spendable_by_address(btc, wallet).get(source, 0)
    except BitcoindError as e:
        print(f"error: {e}", file=sys.stderr)
        return Funding(1)
    want = estimate_need(fee_rate, outputs) if need is None else need
    short = want - have
    if short <= 0:
        return Funding()

    floor_sat = dispense_floor(cp, source)
    if floor_sat is not None and short > floor_sat:
        bought = short // (floor_sat + 1)
        print(f"{source} has an open dispenser at {floor_sat + 1} sat, and needs "
              f"{short} sat to pay its own fee — sending that would trigger the "
              f"dispenser and buy {bought} of its own token(s), shrinking it. Nothing "
              f"was sent. Close the dispenser first, lower --fee-rate, or move BTC "
              f"there yourself if you accept the dispense.", file=sys.stderr)
        return Funding(1)

    origin = f"from {fund_from}" if fund_from else "wallet-wide"
    if dry_run:
        print(f"[dry-run] would first move {short} sat to {source} ({origin}), then "
              f"continue. Re-run without --dry-run to do it.")
        return Funding(0)
    print(f"funding {source} with {short} sat ({origin}) so it can pay its own fee...")
    txid = _fund_source(btc, wallet, source, short, fund_from, fee_rate)
    if txid is None:
        return Funding(1)
    print(f"  funding txid : {txid}")
    return Funding(None, True)


def compose_retrying(compose: Callable[[], dict], funded: bool) -> dict:
    """Run `compose()`, retrying briefly when Counterparty cannot yet see a
    funding we have just broadcast — its view of the source's coins trails ours,
    so composing on the instant fails for money already sent. Only retried when
    we did the funding; otherwise the shortfall is real and fails at once."""
    attempts = VISIBLE_TRIES if funded else 1
    for attempt in range(attempts):
        try:
            return compose()
        except CounterpartyError as e:
            msg = str(e)
            if attempt + 1 >= attempts or (
                    "Insufficient funds" not in msg and "No UTXOs" not in msg):
                raise
            if attempt == 0:
                print("  waiting for Counterparty to see the funding...")
            time.sleep(VISIBLE_WAIT)
    raise AssertionError("unreachable")
