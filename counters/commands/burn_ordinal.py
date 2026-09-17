"""`counters wallet burn-ordinal-sat` — destroy the ordinals half of a counterparty + ord counter.

A `counterparty/ord` reveal creates TWO independently ownable things: the
Counterparty asset, and an ordinals inscription living on the first sat of the
reveal's dust output (vout 1 — vout 0 is the 0-value CNTRPRTY OP_RETURN, which
receives no sats, so offset 0 flows into vout 1). This command burns the second
and leaves the first exactly as it was: no Counterparty message is composed,
and the asset, its supply, its holders and its counter number are untouched.

The burn is ord's own (`ord wallet burn`): the inscribed sat is sent to an
OP_RETURN output worth exactly 1 sat, which ord's indexer marks with the
`burned` charm and which nothing can ever spend again. The script is ord's too —
OP_RETURN, an empty push, and zero padding to 5 bytes, so the transaction's
base size clears the 65-byte standardness minimum. IT IS IRREVERSIBLE.

Picking what to burn comes from the counters index, not an ord server: every
wallet UTXO at `<reveal txid>:1` whose txid is an indexed counter's reveal, and
whose reveal really is a counterparty/ord envelope. Only an inscription still
sitting on its reveal output is found — once the sat has moved, its location is
ord's to know, not ours. The inscription belongs to the reveal's FIRST message
(msg_index 0), so that event's asset is the one it is shown against.

Two guards before anything is signed: a UTXO carrying Counterparty balances
(v11 `attach`) is refused, since spending it would move those; and a funding
input, when the postage cannot pay the fee, is never another inscription or a
counter's reveal output.
"""

from __future__ import annotations

import math
import struct
import sys

from ..bitcoind import BitcoindClient, BitcoindError
from ..config import Config
from ..counterparty import CounterpartyClient, CounterpartyError
from ..reveal import envelope_style
from ..store import Store
from .cancel import _next_block_rate, _reclaim_address, _sats
from .send import _check_mempool, _confirm_prompt

# ord's burn output: OP_RETURN, OP_0 (empty metadata), push of 2 zero bytes.
BURN_SCRIPT_HEX = "6a00020000"
BURN_VALUE_SAT = 1
INSCRIPTION_VOUT = 1
DUST_SAT = 330
CHANGE_OUTPUT_VB = 43        # a P2TR output, the costliest the wallet makes


def _varint(n: int) -> bytes:
    if n < 0xFD:
        return bytes([n])
    if n <= 0xFFFF:
        return b"\xfd" + struct.pack("<H", n)
    return b"\xfe" + struct.pack("<I", n)


def serialize_unsigned(inputs: list[tuple[str, int]],
                       outputs: list[tuple[int, str]]) -> str:
    """A version-2 transaction, unsigned. `inputs` are (txid, vout) and signal
    RBF; `outputs` are (value_sat, script_hex). Built by hand because
    createrawtransaction can only give an OP_RETURN a value of zero."""
    tx = struct.pack("<i", 2) + _varint(len(inputs))
    for txid, vout in inputs:
        tx += bytes.fromhex(txid)[::-1] + struct.pack("<I", vout)
        tx += b"\x00" + struct.pack("<I", 0xFFFFFFFD)
    tx += _varint(len(outputs))
    for value, script in outputs:
        spk = bytes.fromhex(script)
        tx += struct.pack("<q", value) + _varint(len(spk)) + spk
    return (tx + struct.pack("<I", 0)).hex()


def _attached_balances(cp, outpoint: str) -> list[dict] | None:
    """Counterparty balances attached to a UTXO, or None if Core could not say."""
    try:
        return [b for b in cp.get_utxo_balances(outpoint)
                if int(b.get("quantity") or 0) > 0]
    except CounterpartyError:
        return None


def find_candidates(btc, cp, store, wallet: str) -> list[dict]:
    """Every counterparty + ord inscription still on its reveal output in this wallet."""
    out = []
    for u in btc.wallet_call(wallet, "listunspent", [0, 9999999]):
        if u.get("vout") != INSCRIPTION_VOUT or not u.get("spendable", True):
            continue
        row = store.get_counter_by_event(u["txid"], 0)
        if row is None:
            continue
        try:
            tx = btc._call("getrawtransaction", [u["txid"], True])
        except BitcoindError:
            continue
        if envelope_style(tx) != "counterparty/ord":
            continue
        outpoint = f"{u['txid']}:{INSCRIPTION_VOUT}"
        attached = _attached_balances(cp, outpoint)
        blocked = None
        if attached is None:
            blocked = "could not ask Counterparty whether assets are attached to it"
        elif attached:
            held = ", ".join(f"{b.get('asset_longname') or b.get('asset')}"
                             for b in attached)
            blocked = f"Counterparty balances are attached to this UTXO ({held})"
        out.append({
            "number": row["number"],
            "asset": row["asset_longname"] or row["asset"],
            "content_type": row["content_type"],
            "size": row["content_length"],
            "inscription_id": f"{u['txid']}i0",
            "txid": u["txid"],
            "outpoint": outpoint,
            "address": u.get("address"),
            "value": _sats(u["amount"]),
            "blocked": blocked,
        })
    out.sort(key=lambda c: c["number"])
    return out


def _matches(c: dict, target: str) -> bool:
    t = target.strip()
    return (t == str(c["number"]) or t.lstrip("#") == str(c["number"])
            or t.upper() == c["asset"].upper()
            or t.lower() in (c["inscription_id"], c["outpoint"], c["txid"]))


def _show(i: int, c: dict) -> None:
    print(f"  [{i}] #{c['number']}  {c['asset']}  ({c['content_type']}, "
          f"{c['size']:,} bytes)")
    print(f"      inscription {c['inscription_id']}")
    print(f"      on {c['outpoint']}  ({c['value']} sat at {c['address']})")
    if c["blocked"]:
        print(f"      CANNOT BURN: {c['blocked']}")


def _choose(candidates: list[dict], target: str | None) -> dict | None:
    pool = [c for c in candidates if target is None or _matches(c, target)]
    if target is not None and not pool:
        print(f"{target!r} is not a counterparty + ord inscription on its reveal "
              f"output in this wallet", file=sys.stderr)
        return None
    if len(pool) == 1 and target is not None:
        return pool[0]
    print(f"counterparty + ord inscriptions in this wallet ({len(pool)}):\n")
    for i, c in enumerate(pool, 1):
        _show(i, c)
    print(flush=True)
    if not sys.stdin.isatty():
        print("not a terminal: name one (counter number, asset, or inscription id) "
              "to choose non-interactively", file=sys.stderr)
        return None
    try:
        answer = input(f"burn which inscription? [1-{len(pool)}, or q to quit] ").strip()
    except EOFError:
        return None
    if not answer.isdigit() or not 1 <= int(answer) <= len(pool):
        print("nothing burned")
        return None
    return pool[int(answer) - 1]


def _funding_utxo(btc, cp, store, wallet: str, exclude: set[str], need: int) -> dict | None:
    """The smallest plain wallet coin worth at least `need` sat: confirmed, not a
    counter's reveal output, and carrying no Counterparty balances."""
    utxos = sorted(
        (u for u in btc.wallet_call(wallet, "listunspent", [1, 9999999])
         if u.get("spendable", True) and _sats(u["amount"]) >= need
         and f"{u['txid']}:{u['vout']}" not in exclude),
        key=lambda u: _sats(u["amount"]),
    )
    for u in utxos:
        if store.get_counter_by_event(u["txid"], 0) is not None:
            continue
        if _attached_balances(cp, f"{u['txid']}:{u['vout']}") != []:
            continue
        return u
    return None


def _sign(btc, wallet: str, inputs, outputs) -> tuple[str | None, int, str | None]:
    raw = serialize_unsigned(inputs, outputs)
    signed = btc.wallet_call(wallet, "signrawtransactionwithwallet", [raw])
    if not signed.get("complete"):
        return None, 0, f"signing failed: {signed.get('errors')}"
    vsize = btc._call("decoderawtransaction", [signed["hex"]])["vsize"]
    return signed["hex"], vsize, None


def build_burn(btc, cp, store, wallet: str, cand: dict, rate: float,
               exclude: set[str]) -> tuple[dict | None, str | None]:
    """Sign the burn at `rate`. Returns (plan, error): exactly one is set.

    Input 0 is the inscription, so its first sat lands at offset 0 of output 0,
    the 1-sat OP_RETURN. The rest of the postage pays the fee, and goes to
    change only when there is enough left to be worth an output. If the
    postage cannot pay the fee, a plain wallet coin joins as input 1."""
    burn = (BURN_VALUE_SAT, BURN_SCRIPT_HEX)
    inputs = [(cand["txid"], INSCRIPTION_VOUT)]
    total_in = cand["value"]

    hex_, vsize, err = _sign(btc, wallet, inputs, [burn])
    if err:
        return None, err
    funding = None
    if total_in - BURN_VALUE_SAT < math.ceil(rate * vsize):
        # A second input (~58 vB) and a change output: size for both up front.
        need = math.ceil(rate * (vsize + 58 + CHANGE_OUTPUT_VB)) + DUST_SAT
        funding = _funding_utxo(btc, cp, store, wallet, exclude, need)
        if funding is None:
            return None, (f"the {total_in} sat postage cannot pay {rate:g} sat/vB, and "
                          f"the wallet has no plain confirmed coin of {need} sat or "
                          f"more to add. Lower --fee-rate or fund the wallet.")
        inputs.append((funding["txid"], funding["vout"]))
        total_in += _sats(funding["amount"])

    left = total_in - BURN_VALUE_SAT
    est_vsize = vsize + (58 if funding else 0) + CHANGE_OUTPUT_VB
    change_addr, change = None, 0
    if left - math.ceil(rate * est_vsize) >= DUST_SAT:
        change_addr = _reclaim_address(btc, wallet)
        spk = btc.wallet_call(wallet, "getaddressinfo", [change_addr])["scriptPubKey"]
        # Measure with a placeholder amount; the amount does not change the size.
        _h, vsize, err = _sign(btc, wallet, inputs, [burn, (left // 2, spk)])
        if err:
            return None, err
        fee = math.ceil(rate * vsize)
        change = left - fee
        hex_, vsize, err = _sign(btc, wallet, inputs, [burn, (change, spk)])
        if err:
            return None, err
    else:
        # Below dust: the leftover postage is the fee.
        fee = left
        if funding is not None:
            hex_, vsize, err = _sign(btc, wallet, inputs, [burn])
            if err:
                return None, err
    return {"hex": hex_, "vsize": vsize, "fee": fee, "change": change,
            "change_address": change_addr, "funding": funding}, None


def cmd_burn_ordinal_sat(config: Config, wallet: str, target: str | None = None,
                         fee_rate: float | None = None, assume_yes: bool = False,
                         dry_run: bool = False) -> int:
    btc = BitcoindClient(config)
    cp = CounterpartyClient(config)
    store = Store(config)
    try:
        try:
            candidates = find_candidates(btc, cp, store, wallet)
        except BitcoindError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        if not candidates:
            print("no counterparty + ord inscriptions on their reveal outputs in this "
                  "wallet — nothing to burn")
            return 0
        cand = _choose(candidates, target)
        if cand is None:
            return 1
        if cand["blocked"]:
            print(f"refusing to burn {cand['inscription_id']}: {cand['blocked']}",
                  file=sys.stderr)
            return 1

        rate = fee_rate if fee_rate is not None else (_next_block_rate(btc) or 1.0)
        exclude = {c["outpoint"] for c in candidates}
        try:
            plan, err = build_burn(btc, cp, store, wallet, cand, rate, exclude)
        except BitcoindError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        if err:
            print(err, file=sys.stderr)
            return 1

        print(f"\nburn the ordinals inscription of counter #{cand['number']}")
        print(f"  counterparty asset : {cand['asset']}  (NOT touched — only the "
              f"inscription is destroyed)")
        print(f"  inscription        : {cand['inscription_id']}")
        print(f"  spends             : {cand['outpoint']}  ({cand['value']} sat)")
        if plan["funding"]:
            f = plan["funding"]
            print(f"  fee input          : {f['txid']}:{f['vout']}  "
                  f"({_sats(f['amount'])} sat)")
        print(f"  burns              : {BURN_VALUE_SAT} sat to OP_RETURN (ord's "
              f"`burned` charm)")
        print(f"  fee                : {plan['fee']} sat over {plan['vsize']} vB "
              f"({plan['fee'] / plan['vsize']:.2f} sat/vB)")
        if plan["change_address"]:
            print(f"  change             : {plan['change']} sat -> "
                  f"{plan['change_address']}")
        elif not plan["funding"]:
            print(f"                       (the leftover postage is below dust, so "
                  f"it all goes to the fee)")
        ok, _ = _check_mempool(btc, plan["hex"])

        if dry_run:
            print(f"\n[dry-run] not broadcast. raw tx:\n{plan['hex']}")
            return 0 if ok else 1
        if not ok:
            print("not broadcasting: the burn was not accepted", file=sys.stderr)
            return 1
        if not assume_yes and not _confirm_prompt(
                f"\nburn inscription {cand['inscription_id']} of {cand['asset']} "
                f"FOREVER?"):
            print("nothing burned")
            return 0
        try:
            txid = btc._call("sendrawtransaction", [plan["hex"]])
        except BitcoindError as e:
            print(f"broadcast failed: {e}", file=sys.stderr)
            return 1
        print(f"\nburned. txid: {txid}")
        print("ord shows the inscription as burned once this confirms; the "
              f"Counterparty asset {cand['asset']} is unchanged.")
        return 0
    finally:
        store.close()
