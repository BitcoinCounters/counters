"""`counters wallet bump` — pay more to get an unconfirmed transaction mined.

The lever is **CPFP** (child pays for parent): we spend one of the transaction's
own outputs with a new, richly-paying child. Miners score a transaction by its
whole unconfirmed ancestry, so a fat child drags its parent in with it.

Why not RBF, which `cancel` uses? A replacement has to be re-signed, and the
transactions this wallet gets stuck on are composed by Counterparty with
sequence 0xffffffff — Bitcoin Core's own `bumpfee` refuses those outright ("not
BIP 125 replaceable"). CPFP needs no cooperation from the original: it is a
brand-new transaction that only has to spend an output we control. It also
never invalidates anything, where a replacement destroys the original.

The catch, and the reason this command reports rather than just charges ahead:
CPFP only lifts transactions that are ANCESTORS of the child. A stuck
inscription is the sharp case — its reveal spends the commit's envelope output
and emits a single 0-value OP_RETURN, so it has no output to attach a child to,
and a child on the commit's change is the reveal's *sibling*, not its
descendant. Such a child speeds up the commit and leaves the reveal exactly as
stuck as before. When that is the situation, we say so before taking the money.
"""

from __future__ import annotations

import math
import sys
from decimal import Decimal

from ..bitcoind import COIN, BitcoindClient, BitcoindError
from ..config import Config
from .cancel import DUST_SAT, _reclaim_address, _sats, _unconfirmed_txids
from .send import _check_mempool, _fmt_btc

# A child must at least pay its own way at the incremental relay fee.
MIN_CHILD_SAT_VB = 1
_HISTORY = 200


def _pending(btc, wallet: str) -> list[dict]:
    """Unconfirmed wallet transactions in the mempool, each with the outputs we
    could attach a child to. A transaction with no spendable output cannot be
    bumped by CPFP at all — it is listed anyway, so the reason is visible."""
    attachable: dict[str, list[dict]] = {}
    for u in btc.wallet_call(wallet, "listunspent", [0, 9999999]):
        if u.get("confirmations", 0) == 0 and u.get("spendable", True):
            attachable.setdefault(u["txid"], []).append(u)

    out = []
    for txid in _unconfirmed_txids(btc, wallet):
        try:
            entry = btc._call("getmempoolentry", [txid])
        except BitcoindError:
            continue
        out.append({
            "txid": txid,
            "vsize": entry["vsize"],
            "fee": _sats(entry["fees"]["base"]),
            # Ancestors include this transaction: the package a child inherits.
            "anc_vsize": entry["ancestorsize"],
            "anc_fee": _sats(entry["fees"]["ancestor"]),
            "descendants": entry["descendantcount"] - 1,
            "utxos": attachable.get(txid, []),
        })
    return out


def _child_fee(target_rate: float, anc_vsize: int, anc_fee: int, child_vsize: int) -> int:
    """What the child must pay so that (ancestors + child) reaches `target_rate`.

    The ancestors' own fees count toward the package, so the child pays the
    shortfall — and never less than its own bandwidth at the relay minimum."""
    needed = math.ceil(target_rate * (anc_vsize + child_vsize)) - anc_fee
    return max(needed, child_vsize * MIN_CHILD_SAT_VB)


def cmd_bump(config: Config, wallet: str, txid: str | None = None,
             fee_rate: float | None = None, assume_yes: bool = False,
             dry_run: bool = False) -> int:
    btc = BitcoindClient(config)

    pending = _pending(btc, wallet)
    if not pending:
        print(f"wallet {wallet!r} has no unconfirmed transactions to bump")
        return 0

    if txid:
        chosen = next((t for t in pending if t["txid"] == txid), None)
        if chosen is None:
            print(f"{txid} is not an unconfirmed transaction of wallet {wallet!r}",
                  file=sys.stderr)
            return 1
    else:
        chosen = _choose(pending)
        if chosen is None:
            return 1

    if not chosen["utxos"]:
        print(f"\n{chosen['txid']} has no spendable output, so no child can be "
              f"attached to it — nothing to bump.", file=sys.stderr)
        print("hint: an inscription reveal spends its whole input to fee and emits "
              "only an OP_RETURN. Its fee is fixed by Counterparty's signature and "
              "cannot be raised; `counters wallet cancel` on the COMMIT is the only "
              "way out, then re-mint at a workable rate.", file=sys.stderr)
        return 1

    target = fee_rate if fee_rate is not None else _ask_rate(chosen)
    if target is None:
        return 1
    current = chosen["anc_fee"] / chosen["anc_vsize"]
    if target <= current:
        print(f"target {target} sat/vB is not above the package's current "
              f"{current:.2f} sat/vB — nothing to do", file=sys.stderr)
        return 1

    return _attach_child(btc, wallet, chosen, target, assume_yes, dry_run)


def _choose(pending: list[dict]) -> dict | None:
    print(f"unconfirmed transactions ({len(pending)}):\n")
    for i, tx in enumerate(pending, 1):
        rate = tx["fee"] / tx["vsize"]
        pkg = tx["anc_fee"] / tx["anc_vsize"]
        print(f"  [{i}] {tx['txid']}")
        print(f"      {tx['fee']} sat at {rate:.2f} sat/vB, {tx['vsize']} vB")
        if tx["anc_vsize"] != tx["vsize"]:
            print(f"      with unconfirmed parents: {tx['anc_vsize']} vB at "
                  f"{pkg:.2f} sat/vB — a child lifts all of it")
        if not tx["utxos"]:
            print(f"      NOT bumpable: no spendable output to attach a child to")
        if tx["descendants"]:
            print(f"      note: {tx['descendants']} transaction(s) spend its outputs; "
                  f"a child does NOT speed those up")
        print()

    if not sys.stdin.isatty():
        print("not a terminal: pass --txid TXID to choose non-interactively",
              file=sys.stderr)
        return None
    try:
        answer = input(f"bump which? [1-{len(pending)}, or q to quit] ").strip()
    except EOFError:
        return None
    if not answer.isdigit() or not 1 <= int(answer) <= len(pending):
        print("nothing bumped")
        return None
    return pending[int(answer) - 1]


def _ask_rate(tx: dict) -> float | None:
    """Ask what fee rate the package should end up at."""
    current = tx["anc_fee"] / tx["anc_vsize"]
    if not sys.stdin.isatty():
        print("not a terminal: pass --fee-rate SAT_VB", file=sys.stderr)
        return None
    try:
        answer = input(f"\ncurrently {current:.2f} sat/vB — bump to what "
                       f"rate? [sat/vB] ").strip()
    except EOFError:
        return None
    try:
        rate = float(answer)
    except ValueError:
        print(f"{answer!r} is not a fee rate", file=sys.stderr)
        return None
    if rate <= 0:
        print("fee rate must be positive", file=sys.stderr)
        return None
    return rate


def _attach_child(btc, wallet: str, tx: dict, target: float,
                  assume_yes: bool, dry_run: bool) -> int:
    inputs = [{"txid": u["txid"], "vout": u["vout"], "sequence": 0xFFFFFFFD}
              for u in tx["utxos"]]
    total_in = sum(_sats(u["amount"]) for u in tx["utxos"])
    address = _reclaim_address(btc, wallet)

    # Measure the child by signing a placeholder: its size sets its fee, and the
    # output amount does not change its size.
    probe = _sign(btc, wallet, inputs, address, total_in // 2)
    if not probe.get("complete"):
        print(f"cannot sign a child for {tx['txid']}: {probe.get('errors')}",
              file=sys.stderr)
        return 1
    child_vsize = btc._call("decoderawtransaction", [probe["hex"]])["vsize"]

    fee = _child_fee(target, tx["anc_vsize"], tx["anc_fee"], child_vsize)
    left = total_in - fee
    if left < DUST_SAT:
        print(f"cannot reach {target} sat/vB: the child would owe {fee} sat but only "
              f"{total_in} sat is attachable. Bump to a lower rate, or fund the "
              f"wallet and retry.", file=sys.stderr)
        return 1

    pkg_vsize = tx["anc_vsize"] + child_vsize
    pkg_fee = tx["anc_fee"] + fee
    print(f"\nbump {tx['txid']}")
    print(f"  method    : CPFP — a child pays for it")
    print(f"  now       : {tx['anc_fee']} sat over {tx['anc_vsize']} vB "
          f"({tx['anc_fee'] / tx['anc_vsize']:.2f} sat/vB)")
    print(f"  child     : {fee} sat over {child_vsize} vB")
    print(f"  package   : {pkg_fee} sat over {pkg_vsize} vB "
          f"({pkg_fee / pkg_vsize:.2f} sat/vB)")
    print(f"  costs you : {fee} sat ({_fmt_btc(Decimal(fee) / COIN)} BTC) extra")
    print(f"  change    : {left} sat -> {address}")
    if tx["descendants"]:
        print(f"  WARNING   : {tx['descendants']} transaction(s) spend this one's "
              f"outputs and are NOT sped up — a child only lifts its own ancestors")

    if not (dry_run or assume_yes or _confirm(fee, target)):
        print("nothing bumped")
        return 0

    signed = _sign(btc, wallet, inputs, address, left)
    if not signed.get("complete"):
        print(f"signing the child failed: {signed.get('errors')}", file=sys.stderr)
        return 1
    tx_hex = signed["hex"]

    ok, _ = _check_mempool(btc, tx_hex)
    if dry_run:
        print(f"\n[dry-run] not broadcast. raw tx:\n{tx_hex}")
        return 0 if ok else 1
    if not ok:
        print("not broadcasting: the child was not accepted", file=sys.stderr)
        print(f"raw tx: {tx_hex}", file=sys.stderr)
        return 1

    try:
        child_txid = btc._call("sendrawtransaction", [tx_hex])
    except BitcoindError as e:
        print(f"broadcast failed: {e}", file=sys.stderr)
        return 1
    print(f"\nbumped. child broadcast: {child_txid}")
    print(f"{tx['txid']} now confirms as a package with it.")
    return 0


def _sign(btc, wallet: str, inputs: list[dict], address: str, value_sat: int):
    raw = btc._call("createrawtransaction",
                    [inputs, {address: _fmt_btc(Decimal(value_sat) / COIN)}])
    return btc.wallet_call(wallet, "signrawtransactionwithwallet", [raw])


def _confirm(fee: int, target: float) -> bool:
    if not sys.stdin.isatty():
        print("not a terminal: pass --yes to confirm non-interactively", file=sys.stderr)
        return False
    try:
        return input(f"\npay {fee} sat to reach {target} sat/vB? [y/N] "
                     ).strip().lower() in ("y", "yes")
    except EOFError:
        return False
