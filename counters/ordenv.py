"""Ordinals inscription envelopes, read out of a reveal transaction.

An ordinals inscription is a tapscript envelope on a taproot script-path
spend:

    OP_FALSE OP_IF "ord" <tag> <value> ... OP_0 <body chunk> ... OP_ENDIF

Tags are single pushes followed by their value (tag 1 is the content type);
the body starts at the first OP_0 and is the concatenation of every push up
to OP_ENDIF. One reveal can carry many envelopes — across inputs and within
one script — and ord numbers them by input order, then envelope order, as
`<txid>i<n>`.

This is NOT the counters carrier (reveal.py checks that one and never reads
its contents). It exists so a counter that NAMES an inscription — the icon
entry of an asset-info JSON body, content.py icon_ref — can be shown from
the chain through this node, never through a hosted gateway.
"""
from __future__ import annotations

from dataclasses import dataclass

from .reveal import ScriptParseError, parse_script

OP_0 = 0x00
OP_IF = 0x63
OP_ENDIF = 0x68
_ORD = b"ord"
_TAG_CONTENT_TYPE = b"\x01"


@dataclass
class Inscription:
    index: int
    content_type: str | None
    body: bytes


def inscriptions(tx: dict) -> list[Inscription]:
    """Every ord envelope in a verbose transaction, in ord's numbering.
    A witness that does not parse as script contributes nothing."""
    out: list[Inscription] = []
    for vin in tx.get("vin", []):
        witness = vin.get("txinwitness") or []
        if len(witness) < 2:
            continue
        try:
            ops = parse_script(bytes.fromhex(witness[-2]))
        except (ScriptParseError, ValueError):
            continue
        i, n = 0, len(ops)
        while i < n:
            if (ops[i] == (OP_0, b"") and i + 2 < n
                    and ops[i + 1][0] == OP_IF and ops[i + 2][1] == _ORD):
                j = i + 3
                fields: dict[bytes, bytes] = {}
                chunks: list[bytes] = []
                in_body = False
                while j < n and ops[j][0] != OP_ENDIF:
                    code, data = ops[j]
                    if not in_body:
                        if code == OP_0:
                            in_body = True
                        elif data is not None and j + 1 < n:
                            fields[data] = fields.get(data, b"") + (ops[j + 1][1] or b"")
                            j += 1
                    elif data is not None:
                        chunks.append(data)
                    j += 1
                ctype = fields.get(_TAG_CONTENT_TYPE)
                out.append(Inscription(
                    index=len(out),
                    content_type=(ctype.decode("utf-8", "replace") if ctype else None),
                    body=b"".join(chunks),
                ))
                i = j
            i += 1
    return out


def inscription(tx: dict, index: int) -> Inscription | None:
    """The `index`-th inscription of a reveal, or None."""
    found = inscriptions(tx)
    return found[index] if 0 <= index < len(found) else None
