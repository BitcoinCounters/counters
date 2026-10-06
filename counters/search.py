"""Search — how a typed string becomes counters (the explorer's box, GET /search).

One identifier, many spellings. A reader types a counter number, an asset
name, a subasset family ("DEGENT" for DEGENT.0 … DEGENT.6), a transaction
hash with or without ordinals' `i0`, a content hash, a minting address, or
pastes a URL from this explorer, ordinals.com, xchain or a block explorer.
`classify` decides what the string is without touching the database; `run`
asks the store and ranks what comes back: an exact hit first, then a family,
then names beginning with the text, then names containing it.

Case: Counterparty asset names are upper-case and subasset long names are
case-sensitive on chain, but a reader does not know that, so every lookup
here is case-insensitive and the exact spelling wins the tie.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import unquote, urlsplit

from .ids import parse_id

_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
_ADDRESS = re.compile(r"^(?:bc1[02-9ac-hj-np-z]{11,87}|[13][a-km-zA-HJ-NP-Z1-9]{25,34})$", re.IGNORECASE)
# Counter numbers are small integers; a longer digit string is not one (and
# would overflow SQLite's INTEGER if asked).
MAX_NUMBER_DIGITS = 12
_BLOCK = re.compile(r"^(?:block|blk|height)\s*[:#]?\s*(\d+)$", re.IGNORECASE)
# What the explorer, ordinals.com, xchain, tokenscan and the block explorers
# put after their hostnames; the last group of each is the identifier.
_URL_PATHS = (
    re.compile(r"/(?:inscription|c|counter|content|preview|stamp)/([^/?#]+)"),
    re.compile(r"/tx/([0-9a-fA-F]{64})"),
    re.compile(r"/asset/([^/?#]+)"),
    re.compile(r"/block/(\d+)"),
)

ADDRESS_NOTE = ("counters this address minted — the index records the minting source; "
                "today's holder is read live on each counter's page")


@dataclass
class Query:
    kind: str                 # empty | number | id | hex | address | block | name
    token: str                # the identifier as the lookup will use it
    variants: list[str] = field(default_factory=list)   # name queries: spellings to try
    msg_index: int | None = None                        # id queries


def _from_url(raw: str) -> str | None:
    """The identifier inside a pasted URL, or None when it is not one."""
    text = raw if "://" in raw else ("https://" + raw if raw.lower().startswith("www.") else None)
    if text is None:
        return None
    parts = urlsplit(text)
    # The explorer's own hash routes: #/c/<id>, #/b/<n>, #/a/<name>, #/s/<q>.
    frag = parts.fragment
    m = re.match(r"^/?(c|b|a|s)/(.+)$", frag)
    if m:
        kind, val = m.group(1), unquote(m.group(2))
        return f"block {val}" if kind == "b" else val
    for rx in _URL_PATHS:
        m = rx.search(parts.path)
        if m:
            val = unquote(m.group(1))
            return f"block {val}" if rx.pattern.startswith("/block") else val
    tail = parts.path.rstrip("/").rsplit("/", 1)[-1]
    return unquote(tail) or None


def classify(raw: str) -> Query:
    token = " ".join((raw or "").split())          # trim + collapse whitespace
    if not token:
        return Query("empty", "")
    inner = _from_url(token)
    if inner is not None:
        token = " ".join(inner.split())
    if token[:1] in "#$":                           # "#204", "$DEGENT"
        token = token[1:].strip()
    if not token:
        return Query("empty", "")
    # 64 hex characters are a hash even when every one of them is a digit,
    # so the hash shapes are read before the number shape.
    event = parse_id(token)
    if event is not None:
        return Query("id", event[0], msg_index=event[1])
    if _HEX64.match(token):
        return Query("hex", token.lower())
    if token.isdigit():
        return Query("number", str(int(token)) if len(token) <= MAX_NUMBER_DIGITS else token)
    m = _BLOCK.match(token)
    if m:
        return Query("block", str(int(m.group(1))))
    if _ADDRESS.match(token):
        return Query("address", token if token[:3].lower() != "bc1" else token.lower())
    upper = token.upper()
    variants = [upper]
    dotted = upper.replace(" ", ".")                # "DEGENT 6" -> "DEGENT.6"
    if dotted != upper:
        variants.append(dotted)
    return Query("name", upper, variants=variants)


def parent_of(name: str | None) -> str | None:
    """The family a subasset long name belongs to: 'DEGENT' for 'DEGENT.6'."""
    if not name or "." not in name:
        return None
    return name.split(".", 1)[0]


def run(store, raw: str, limit: int, serialize: Callable) -> dict:
    """Answer a search: the classified query, the exact hit if there is one,
    the ranked results (up to `limit`), how many matched in all, and the
    family the text names when it is one."""
    q = classify(raw)
    exact = None
    rows: list = []
    total = 0
    collection = None
    note = None

    if q.kind == "number":
        row = store.get_counter(int(q.token)) if len(q.token) <= MAX_NUMBER_DIGITS else None
        exact = row
        rows = [row] if row is not None else []
        total = len(rows)
    elif q.kind == "id":
        row = store.get_counter_by_event(q.token, q.msg_index)
        exact = row
        rows = [row] if row is not None else []
        total = len(rows)
    elif q.kind == "hex":
        rows = store.get_counters_by_txid(q.token)
        if rows:
            exact = rows[0]
        else:
            rows = store.get_counters_by_sha(q.token)
            if rows:
                note = "counters whose content has this sha256"
        total = len(rows)
    elif q.kind == "address":
        rows = store.list_by_source(q.token, limit=limit)
        total = store.count_by_source(q.token)
        note = ADDRESS_NOTE
    elif q.kind == "name":
        rows, total = store.search_names(q.variants, limit)
        for v in q.variants:
            n = store.family_count(v)
            if n:
                collection = {"name": v, "count": n}
                break
        # An exact spelling (asset or long name) is the hit the box jumps to.
        for row in rows:
            if (row["asset"] or "").upper() in q.variants or (row["asset_longname"] or "").upper() in q.variants:
                exact = row
                break
    # 'block' and 'empty' carry no rows: the explorer routes them itself.

    return {
        "query": raw,
        "kind": q.kind,
        "token": q.token,
        "exact": serialize(exact) if exact is not None else None,
        "results": [serialize(r) for r in rows[:limit]],
        "total": total,
        "collection": collection,
        "note": note,
    }
