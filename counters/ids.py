"""Inscription IDs — build reference v3 §6.1.

The canonical text form of the event key N1 defines:
`<reveal_txid>i<msg_index>`, ordinals' inscription-ID syntax carrying
counters' event index. The index is Counterparty's per-transaction event
index (`msg_index`), NOT ord's envelope index: the two coincide at 0 — since
A1 a counter is always its transaction's own message — and for a
counterparty + ord counter the string is byte-identical to the ordinals
inscription ID of the same reveal.

An ID is pure formatting of data the rolling hash chain already commits to
(`…|mint_txid|msg_index|…`, store.py): no schema, no consensus, no reindex.
"""

from __future__ import annotations

import re

# Strict: exactly 64 hex chars + 'i' + a decimal index with no leading
# zeros. Uppercase hex is accepted on input and canonicalised to lowercase;
# nothing else is repaired. No asset name matches this shape (named assets
# are A-Z, numeric display is 'A'+digits, subassets contain '.'), and a
# digit string never does, so the three identifier forms cannot collide.
_ID_RE = re.compile(r"^([0-9a-fA-F]{64})i(0|[1-9][0-9]*)$")


def format_id(txid: str, msg_index: int = 0) -> str:
    """The inscription ID of an event: `<txid>i<msg_index>`, lowercase."""
    return f"{txid.lower()}i{msg_index}"


def parse_id(token: str) -> tuple[str, int] | None:
    """(txid, msg_index) for an inscription-ID token, else None."""
    m = _ID_RE.match(token.strip())
    if m is None:
        return None
    return m.group(1).lower(), int(m.group(2))
