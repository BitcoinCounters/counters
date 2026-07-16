"""Taproot-reveal (carrier) detection — rule R4 of build reference v3 §4.

A counter's content is whatever Counterparty stores as the description (R3);
this module only decides HOW it travelled. It mirrors Counterparty's own
reveal-transaction rule (counterparty-rs bitcoin_client.rs): a transaction is
a taproot reveal iff

  1. it has an OP_RETURN output of the exact shape OP_RETURN PUSH "CNTRPRTY" —
     the marker literal and unencrypted, with no message payload after it
     (classic OP_RETURN-encoded Counterparty data is ARC4-encrypted with the
     first input's prevout txid, so it can never display the literal marker);
  2. its input 0 witness has exactly 3 items (signature, tapscript, control
     block — a taproot script-path spend), the envelope living in item 1.

The output script is tokenized properly — never substring-searched over raw
hex — so envelope-like bytes elsewhere in a transaction cannot false-positive.
"""

from __future__ import annotations

from .config import CNTRPRTY_MARKER

OP_0 = 0x00
OP_RETURN = 0x6A
OP_PUSHDATA1 = 0x4C
OP_PUSHDATA2 = 0x4D
OP_PUSHDATA4 = 0x4E

# A parsed script op: (opcode, data). `data` is the pushed bytes for push ops
# (b"" for OP_0), or None for non-push opcodes.
Op = tuple[int, "bytes | None"]


class ScriptParseError(Exception):
    pass


def parse_script(script: bytes) -> list[Op]:
    """Tokenize a Bitcoin script into a list of (opcode, data) ops.

    A push that runs past the end raises, so callers parsing untrusted data
    should catch ScriptParseError.
    """
    ops: list[Op] = []
    i = 0
    n = len(script)
    while i < n:
        op = script[i]
        i += 1
        if op == OP_0:
            ops.append((OP_0, b""))
        elif op < OP_PUSHDATA1:  # 0x01..0x4b: push `op` bytes
            data = script[i : i + op]
            if len(data) != op:
                raise ScriptParseError("truncated direct push")
            i += op
            ops.append((op, data))
        elif op == OP_PUSHDATA1:
            if i + 1 > n:
                raise ScriptParseError("truncated OP_PUSHDATA1 length")
            length = script[i]
            i += 1
            data = script[i : i + length]
            if len(data) != length:
                raise ScriptParseError("truncated OP_PUSHDATA1 data")
            i += length
            ops.append((op, data))
        elif op == OP_PUSHDATA2:
            if i + 2 > n:
                raise ScriptParseError("truncated OP_PUSHDATA2 length")
            length = int.from_bytes(script[i : i + 2], "little")
            i += 2
            data = script[i : i + length]
            if len(data) != length:
                raise ScriptParseError("truncated OP_PUSHDATA2 data")
            i += length
            ops.append((op, data))
        elif op == OP_PUSHDATA4:
            if i + 4 > n:
                raise ScriptParseError("truncated OP_PUSHDATA4 length")
            length = int.from_bytes(script[i : i + 4], "little")
            i += 4
            data = script[i : i + length]
            if len(data) != length:
                raise ScriptParseError("truncated OP_PUSHDATA4 data")
            i += length
            ops.append((op, data))
        else:
            ops.append((op, None))
    return ops


def is_marker_op_return(script: bytes) -> bool:
    """True for the exact reveal OP_RETURN: OP_RETURN followed by a single
    push whose bytes are the literal CNTRPRTY marker and nothing else."""
    try:
        ops = parse_script(script)
    except ScriptParseError:
        return False
    return (
        len(ops) == 2
        and ops[0][0] == OP_RETURN
        and ops[0][1] is None
        and ops[1][1] == CNTRPRTY_MARKER
    )


def _has_marker_output(tx: dict) -> bool:
    for vout in tx.get("vout", []):
        spk_hex = (vout.get("scriptPubKey") or {}).get("hex")
        if not spk_hex:
            continue
        try:
            script = bytes.fromhex(spk_hex)
        except ValueError:
            continue
        if is_marker_op_return(script):
            return True
    return False


def is_taproot_reveal(tx: dict) -> bool:
    """Is this (bitcoind-verbose) transaction a Counterparty taproot reveal?

    Both checks of build ref v3 §4: the literal-marker OP_RETURN output, and a
    3-item witness on input 0 (the script-path spend that exposes the
    envelope). Envelope content is NOT extracted — content defers to
    Counterparty's parsed state (R3).
    """
    if not _has_marker_output(tx):
        return False
    vin = tx.get("vin") or []
    if not vin:
        return False
    witness = vin[0].get("txinwitness") or []
    return len(witness) == 3


def commit_txid(tx: dict) -> str | None:
    """The commit transaction's txid for a reveal: the prevout of input 0
    (which script-path-spends the commit output). None for non-reveals."""
    if not is_taproot_reveal(tx):
        return None
    return (tx.get("vin") or [{}])[0].get("txid")
