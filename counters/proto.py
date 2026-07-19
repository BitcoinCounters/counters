"""Detect a proto-counter (the first-version COUNT-envelope protocol) inside a
v3 counter's transaction — enrichment only, never consensus.

A single reveal transaction can carry BOTH identities at once: the Counterparty
taproot envelope that makes it a (v3) counter, and a COUNT envelope — the
original 'proto-counter' protocol (see the separate counters-proto repo). This
module finds the latter. It is a display flag, like the ord/stamp
dual-identities (build ref v3 §5.4, §10); it never affects v3 validity (R1-R4)
or numbering (N1-N6).

The COUNT envelope lives inside a tapscript revealed in the witness:

    OP_FALSE OP_IF
      PUSH "COUNT"            # 5-byte marker
      PUSH 0x01 PUSH <mime>   # tag 1 = content type (optional)
      PUSH 0x02 PUSH <asset>  # tag 2 = target asset (optional)
      OP_0                    # empty push: separator, body begins
      PUSH <file bytes> ...   # <= 520 bytes per push
    OP_ENDIF
    <x-only pubkey> OP_CHECKSIG

Identity rule (per the proto design): an OP_FALSE OP_IF ... OP_ENDIF block is a
COUNT envelope iff its first push equals b"COUNT". Ported from counters-proto's
envelope.py; reuses reveal.parse_script, which tokenises identically.
"""

from __future__ import annotations

from dataclasses import dataclass

from .reveal import ScriptParseError, parse_script

# Proto-counter envelope constants (counters-proto config.py).
COUNT_MARKER = b"COUNT"
CONTENT_TYPE_TAG = 0x01
ASSET_TAG = 0x02

OP_0 = 0x00
OP_IF = 0x63
OP_NOTIF = 0x64
OP_ENDIF = 0x68
OP_1 = 0x51  # ord's legacy content-type tag form, also accepted


@dataclass(frozen=True)
class CountEnvelope:
    content_type: bytes
    body: bytes
    asset: bytes = b""  # tag 0x02 target asset name (empty => bind to tx issuance)


def _skip_to_endif(ops: list, start: int) -> int:
    depth = 1
    j = start
    while j < len(ops):
        op = ops[j][0]
        if op in (OP_IF, OP_NOTIF):
            depth += 1
        elif op == OP_ENDIF:
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return j


def _parse_envelope(ops: list, start: int):
    """Parse one OP_IF...OP_ENDIF block starting at `start` (op after OP_IF).
    Returns (CountEnvelope|None, index_after_endif)."""
    j = start
    if j >= len(ops):
        return None, j
    _, marker = ops[j]
    if marker != COUNT_MARKER:
        return None, _skip_to_endif(ops, j)
    j += 1

    content_type = b""
    asset = b""
    body_chunks: list[bytes] = []
    in_body = False
    while j < len(ops):
        op, data = ops[j]
        if op == OP_ENDIF:
            return CountEnvelope(content_type, b"".join(body_chunks), asset), j + 1
        if op in (OP_IF, OP_NOTIF):
            return None, _skip_to_endif(ops, j + 1)
        if in_body:
            if data is not None:
                body_chunks.append(data)
            j += 1
            continue
        if op == OP_0 and data == b"":
            in_body = True
            j += 1
            continue
        is_ct_tag = (
            data is not None and len(data) == 1 and data[0] == CONTENT_TYPE_TAG
        ) or op == OP_1
        if is_ct_tag:
            j += 1
            if j < len(ops):
                _, ct = ops[j]
                content_type = ct if ct is not None else b""
                j += 1
            continue
        is_asset_tag = data is not None and len(data) == 1 and data[0] == ASSET_TAG
        if is_asset_tag:
            j += 1
            if j < len(ops):
                _, av = ops[j]
                asset = av if av is not None else b""
                j += 1
            continue
        j += 1  # unknown field element: skip (matches proto's ignore-unknown policy)
    return None, j  # reached end without OP_ENDIF: malformed


def find_in_script(script: bytes) -> list[CountEnvelope]:
    """All COUNT envelopes in one script (e.g. a tapscript)."""
    # Fast reject: a real envelope pushes the marker literally, so the bytes
    # must appear verbatim. Skips full tokenisation of signatures/control blocks.
    if COUNT_MARKER not in script:
        return []
    try:
        ops = parse_script(script)
    except ScriptParseError:
        return []
    out: list[CountEnvelope] = []
    i = 0
    while i < len(ops) - 1:
        op, data = ops[i]
        if op == OP_0 and data == b"" and ops[i + 1][0] == OP_IF:
            env, end = _parse_envelope(ops, i + 2)
            if env is not None:
                out.append(env)
            i = max(end, i + 1)
        else:
            i += 1
    return out


def find_count_envelopes(tx: dict) -> list[CountEnvelope]:
    """Every COUNT envelope across all input witnesses of a (bitcoind
    verbosity-2) transaction."""
    found: list[CountEnvelope] = []
    for vin in tx.get("vin") or []:
        for item_hex in vin.get("txinwitness") or []:
            try:
                item = bytes.fromhex(item_hex)
            except (ValueError, TypeError):
                continue
            found.extend(find_in_script(item))
    return found


def has_count_envelope(tx: dict) -> bool:
    """True iff this counter's transaction also carries a COUNT envelope — i.e.
    it is *also* a proto-counter. Display enrichment; never gates v3 validity."""
    return bool(find_count_envelopes(tx))
