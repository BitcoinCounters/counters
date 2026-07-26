"""`counters wallet cancel` — replace an unconfirmed transaction to abandon it.

Bitcoin has no "cancel". The nearest thing is a *replacement*: a new transaction
spending the same inputs, paying a higher fee, that miners prefer over the
original. Ours pays everything back to the wallet, so the original's effect —
a payment, or an inscription's commit — never happens.

Replacement rules this has to satisfy (BIP125, as Bitcoin Core enforces them):

  - the replacement spends the same inputs, so it conflicts with the original;
  - it pays a HIGHER ABSOLUTE FEE than every transaction it evicts. Evicting a
    transaction evicts its unconfirmed descendants too, so cancelling an
    inscription's commit means out-paying commit + reveal COMBINED — for a big
    inscription that is most of what the mint already cost;
  - it pays for its own bandwidth on top, at the incremental relay fee;
  - its fee RATE beats the original's.

A transaction that does not signal replaceability (sequence 0xffffffff) can
still be replaced by nodes running full-RBF, which is Bitcoin Core's default
from v28 — but a node that isn't will not relay the replacement, so a cancel is
best-effort, never a guarantee. Nothing here can undo a CONFIRMED transaction.

Only wallet-funded transactions can be cancelled: replacing one means re-signing
its inputs. An inscription's reveal is not cancellable on its own — its input is
the commit's envelope output, signed with an ephemeral key Counterparty Core
holds, not this wallet. Cancel the commit instead; the reveal dies with it.
"""

from __future__ import annotations

import sys
from decimal import Decimal

from ..bitcoind import COIN, BitcoindClient, BitcoindError
from ..config import Config
from .send import _change_type, _check_mempool, _fmt_btc

# BIP125 rule 4: the replacement pays for its own bandwidth at this rate, on
# top of out-paying everything it evicts.
INCREMENTAL_RELAY_SAT_VB = 1
# Below this a reclaim output is unspendable dust; taproot's threshold is 330.
DUST_SAT = 330
# How far back to look for unconfirmed wallet transactions.
_HISTORY = 200


def _sats(btc_value) -> int:
    return int((Decimal(str(btc_value)) * COIN).to_integral_value())


def _unconfirmed_txids(btc, wallet: str) -> list[str]:
    """Every unconfirmed transaction the wallet knows of, newest first.

    `listtransactions` alone is not enough: its rows come from a transaction's
    `details`, and Bitcoin Core emits no detail for an output paying the
    wallet's own CHANGE address. A transaction whose outputs are all change —
    consolidating coins onto another of your addresses, say — therefore has no
    rows at all and would be invisible. Its unspent outputs still show up in
    `listunspent`, so union the two."""
    seen, txids = set(), []
    for u in btc.wallet_call(wallet, "listunspent", [0, 9999999]):
        txid = u.get("txid")
        if txid and txid not in seen and u.get("confirmations", 0) == 0:
            seen.add(txid)
            txids.append(txid)
    rows = btc.wallet_call(wallet, "listtransactions", ["*", _HISTORY, 0, True])
    for row in reversed(rows):                     # listtransactions is oldest-first
        txid = row.get("txid")
        if txid and txid not in seen and row.get("confirmations", 0) == 0:
            seen.add(txid)
            txids.append(txid)
    return txids


def _pending(btc, wallet: str) -> list[dict]:
    """Wallet-funded transactions still unconfirmed in the mempool, newest first.

    Receives are skipped: with no wallet inputs there is nothing for us to
    re-sign, so they cannot be replaced from here."""
    out = []
    for txid in _unconfirmed_txids(btc, wallet):
        info = btc.wallet_call(wallet, "gettransaction", [txid])
        if info.get("fee") is None:
            continue                               # not ours to re-sign
        try:
            entry = btc._call("getmempoolentry", [txid])
        except BitcoindError:
            continue                               # confirmed or dropped since
        out.append({
            "txid": txid,
            "hex": info["hex"],
            "vsize": entry["vsize"],
            "fee": _sats(entry["fees"]["base"]),
            # "descendant" fees include this transaction: exactly the set a
            # replacement has to out-pay.
            "evicted_fee": _sats(entry["fees"]["descendant"]),
            "evicted_vsize": entry["descendantsize"],
            "evicted_count": entry["descendantcount"],
            "replaceable": info.get("bip125-replaceable") == "yes",
        })
    return out


def _describe(btc, tx: dict) -> str:
    """One line naming where a transaction's money is going."""
    decoded = btc._call("decoderawtransaction", [tx["hex"]])
    parts = []
    for vout in decoded["vout"]:
        addr = vout["scriptPubKey"].get("address") or vout["scriptPubKey"]["type"]
        parts.append(f"{_sats(vout['value'])} sat -> {addr}")
    return "; ".join(parts)


def _reclaim_address(btc, wallet: str) -> str:
    """A wallet address to sweep the inputs back to. Uses a change address of a
    type the wallet can actually derive (see `send`: the node's default type may
    not exist in this wallet)."""
    change_type = _change_type(btc, wallet)
    params = [change_type] if change_type else []
    return btc.wallet_call(wallet, "getrawchangeaddress", params)


def _inputs_of(btc, tx_hex: str) -> tuple[list[dict], int]:
    """The transaction's inputs (re-marked replaceable) and their total value."""
    decoded = btc._call("decoderawtransaction", [tx_hex])
    inputs, total = [], 0
    for vin in decoded["vin"]:
        prev = btc._call("getrawtransaction", [vin["txid"], True])
        total += _sats(prev["vout"][vin["vout"]]["value"])
        # Signal replaceability, so the cancel can itself be bumped if needed.
        inputs.append({"txid": vin["txid"], "vout": vin["vout"], "sequence": 0xFFFFFFFD})
    return inputs, total


def _sign(btc, wallet: str, inputs: list[dict], address: str, value_sat: int):
    raw = btc._call("createrawtransaction",
                    [inputs, {address: _fmt_btc(Decimal(value_sat) / COIN)}])
    return btc.wallet_call(wallet, "signrawtransactionwithwallet", [raw])


def _required_fee(vsize: int, evicted_fee: int, min_rate: float) -> int:
    """The smallest fee that satisfies both replacement rules: out-pay everything
    evicted (plus this transaction's own bandwidth), and beat the original rate."""
    by_absolute = evicted_fee + vsize * INCREMENTAL_RELAY_SAT_VB
    by_rate = int(vsize * min_rate) + 1
    return max(by_absolute, by_rate)


def _miner_fee(vsize: int, rate: float) -> int:
    """What a replacement has to pay to be worth MINING, as opposed to worth
    relaying — simply its own size at a competitive rate.

    Rule 3's absolute-fee floor exists to stop free-relay attacks, not because a
    block producer needs it: replacing a fat low-feerate package hands back its
    block space, which the miner resells at the going rate. Cancelling a stuck
    16 kvB inscription frees ~16 kvB worth tens of thousands of sats, dwarfing
    the few hundred the replacement itself pays. Any miner is better off taking
    it — but ordinary nodes will not relay it, so this fee only makes sense for
    a transaction handed straight to a miner."""
    return max(int(vsize * rate) + 1, vsize * INCREMENTAL_RELAY_SAT_VB)


def _next_block_rate(btc) -> float | None:
    """The node's next-block fee estimate — the rate a miner is currently being
    paid, and so the natural default for a direct-to-miner replacement."""
    try:
        est = btc._call("estimatesmartfee", [1])
    except BitcoindError:
        return None
    rate = est.get("feerate")
    return round(rate * COIN / 1000, 2) if rate else None


def cmd_cancel(config: Config, wallet: str, txid: str | None = None,
               fee_rate: float | None = None, assume_yes: bool = False,
               dry_run: bool = False, no_mempool_check: bool = False) -> int:
    btc = BitcoindClient(config)

    pending = _pending(btc, wallet)
    if not pending:
        print(f"wallet {wallet!r} has no unconfirmed transactions to cancel")
        return 0

    if txid:
        chosen = next((t for t in pending if t["txid"] == txid), None)
        if chosen is None:
            print(f"{txid} is not an unconfirmed transaction of wallet {wallet!r}",
                  file=sys.stderr)
            return 1
    else:
        chosen = _choose(btc, pending, assume_yes)
        if chosen is None:
            return 1

    return _replace(btc, wallet, chosen, fee_rate, assume_yes, dry_run,
                    no_mempool_check)


def _choose(btc, pending: list[dict], assume_yes: bool) -> dict | None:
    """Show the pending transactions and ask which to cancel."""
    print(f"unconfirmed transactions ({len(pending)}):\n")
    for i, tx in enumerate(pending, 1):
        rate = tx["fee"] / tx["vsize"]
        print(f"  [{i}] {tx['txid']}")
        print(f"      {tx['fee']} sat at {rate:.2f} sat/vB, {tx['vsize']} vB"
              f"{'' if tx['replaceable'] else ' (does not signal RBF)'}")
        print(f"      pays: {_describe(btc, tx)}")
        if tx["evicted_count"] > 1:
            print(f"      cancelling it also drops {tx['evicted_count'] - 1} "
                  f"transaction(s) spending its outputs")
        print(f"      cancelling costs over {tx['evicted_fee']} sat in fees")
        print()

    if len(pending) == 1 and assume_yes:
        return pending[0]
    if not sys.stdin.isatty():
        print("not a terminal: pass --txid TXID to choose non-interactively",
              file=sys.stderr)
        return None
    try:
        answer = input(f"cancel which? [1-{len(pending)}, or q to quit] ").strip()
    except EOFError:
        return None
    if not answer.isdigit() or not 1 <= int(answer) <= len(pending):
        print("nothing cancelled")
        return None
    return pending[int(answer) - 1]


def _replace(btc, wallet: str, tx: dict, fee_rate: float | None,
             assume_yes: bool, dry_run: bool, no_mempool_check: bool = False) -> int:
    """Build, confirm, and broadcast the replacement that abandons `tx`."""
    inputs, total_in = _inputs_of(btc, tx["hex"])
    address = _reclaim_address(btc, wallet)

    # Size first: sign a placeholder to measure the replacement's real vsize,
    # then set the fee from it. The output amount does not change the size.
    probe = _sign(btc, wallet, inputs, address, total_in - tx["evicted_fee"] - 1000)
    if not probe.get("complete"):
        print(f"cannot re-sign {tx['txid']}: this wallet does not hold the keys for "
              f"all of its inputs.", file=sys.stderr)
        print("hint: an inscription's reveal is signed with Counterparty's ephemeral "
              "envelope key. Cancel its commit instead — the reveal dies with it.",
              file=sys.stderr)
        return 1
    vsize = btc._call("decoderawtransaction", [probe["hex"]])["vsize"]

    relay_fee = _required_fee(
        vsize, tx["evicted_fee"],
        fee_rate if fee_rate is not None else tx["fee"] / tx["vsize"] + 1,
    )
    if no_mempool_check:
        # Price it for a miner, not for the relay network.
        rate = fee_rate if fee_rate is not None else _next_block_rate(btc)
        if rate is None:
            print("could not estimate a next-block fee rate; pass --fee-rate",
                  file=sys.stderr)
            return 1
        fee = _miner_fee(vsize, rate)
    else:
        fee = relay_fee

    reclaim = total_in - fee
    if reclaim < DUST_SAT:
        print(f"cannot cancel {tx['txid']}: replacing it costs {fee} sat but its "
              f"inputs are only worth {total_in} sat — there is nothing left to "
              f"reclaim.", file=sys.stderr)
        return 1

    print(f"\ncancel {tx['txid']}")
    print(f"  replaces  : {tx['evicted_count']} transaction(s), "
          f"{tx['evicted_fee']} sat of fees already committed")
    print(f"  reclaims  : {_fmt_btc(Decimal(reclaim) / COIN)} BTC -> {address}")
    print(f"  fee       : {fee} sat ({fee / vsize:.2f} sat/vB, {vsize} vB)")
    if no_mempool_check:
        freed = tx["evicted_vsize"] - vsize
        print(f"  direct    : priced for a miner, not for relay — saves "
              f"{relay_fee - fee} sat against the {relay_fee} sat relay policy "
              f"would demand")
        print(f"  why       : replacing frees {freed} vB of block space, worth far "
              f"more to a miner than the {tx['evicted_fee']} sat it gives up")
    if not tx["replaceable"]:
        print(f"  note      : the original does not signal RBF — only nodes running "
              f"full-RBF will relay this")

    if not (assume_yes or _confirm()):
        print("nothing cancelled")
        return 0

    signed = _sign(btc, wallet, inputs, address, reclaim)
    if not signed.get("complete"):
        print(f"signing the replacement failed: {signed.get('errors')}", file=sys.stderr)
        return 1
    tx_hex = signed["hex"]

    if no_mempool_check:
        # The local node enforces the same rule-3 floor this deliberately skips,
        # so there is nothing to broadcast to: hand the hex to a miner.
        print(f"\nnot broadcast — this fee is below what relay policy accepts, so "
              f"submit it directly to a miner:\n{tx_hex}")
        return 0

    ok, _ = _check_mempool(btc, tx_hex)
    if dry_run:
        print(f"\n[dry-run] not broadcast. raw tx:\n{tx_hex}")
        return 0 if ok else 1
    if not ok:
        print("not broadcasting: the replacement was not accepted", file=sys.stderr)
        print(f"raw tx: {tx_hex}", file=sys.stderr)
        return 1

    try:
        new_txid = btc._call("sendrawtransaction", [tx_hex])
    except BitcoindError as e:
        print(f"broadcast failed: {e}", file=sys.stderr)
        return 1
    print(f"\ncancelled. replacement broadcast: {new_txid}")
    print(f"{tx['txid']} is dropped once the replacement propagates; it is only "
          f"truly dead when the replacement confirms.")
    return 0


def _confirm() -> bool:
    if not sys.stdin.isatty():
        print("not a terminal: pass --yes to confirm non-interactively", file=sys.stderr)
        return False
    try:
        return input("\ncancel it? [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False
