"""Bitcoin script tokenizer + COUNT envelope parser.

Canonical COUNT envelope (inside a tapscript revealed in the witness):

    OP_FALSE                      # 0x00
    OP_IF                         # 0x63
      PUSH "COUNT"               # 5-byte marker (OP_PUSHBYTES_5 434f554e54)
      PUSH 0x01                   # field tag 1 = content_type (or OP_1 0x51)
      PUSH <content_type>         # MIME bytes (may be empty)
      OP_0                        # empty push: separator, body begins
      PUSH <body chunk 1>         # file bytes, <= 520 per push
      ...
    OP_ENDIF                      # 0x68
    <32-byte x-only pubkey>
    OP_CHECKSIG                   # 0xac

Identity rule (per design): an envelope is a COUNT envelope iff its first push
equals b"COUNT". content_type and body are both optional (empty-body counters
are valid). The empty-push separator switches field-section parsing to body
collection; body is the concatenation of all pushes after it.

This module never decides validity of the *issuance* — only whether a
well-formed COUNT envelope is present and what it contains.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import ASSET_TAG, CONTENT_TYPE_TAG, COUNT_MARKER

# Opcodes we care about.
OP_0 = 0x00
OP_PUSHDATA1 = 0x4C
OP_PUSHDATA2 = 0x4D
OP_PUSHDATA4 = 0x4E
OP_IF = 0x63
OP_NOTIF = 0x64
OP_ENDIF = 0x68
OP_1 = 0x51  # OP_PUSHNUM_1; ord's legacy content-type tag form, also accepted

# A parsed script op: (opcode, data). `data` is the pushed bytes for push ops
# (b"" for OP_0), or None for non-push opcodes.
Op = tuple[int, "bytes | None"]


class ScriptParseError(Exception):
    pass


def parse_script(script: bytes) -> list[Op]:
    """Tokenize a Bitcoin script into a list of (opcode, data) ops.

    Tolerant of truncation at the end: a push that runs past the end raises,
    so callers parsing untrusted witness data should catch ScriptParseError.
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


@dataclass(frozen=True)
class CounterEnvelope:
    content_type: bytes
    body: bytes
    # Reinscription target (tag 0x02): the existing asset this counter attaches
    # to. Empty => no target, i.e. a creation-style counter bound via a same-tx
    # Counterparty issuance. Non-empty => reinscription (no Counterparty message;
    # the indexer authorises it against the asset's owner as of the block).
    asset: bytes = b""


def _skip_to_endif(ops: list[Op], start: int) -> int:
    """Return index just past the OP_ENDIF that closes the block opened before
    `start`, accounting for nesting. If none found, returns len(ops)."""
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


def _parse_envelope(ops: list[Op], start: int) -> tuple[CounterEnvelope | None, int]:
    """Parse one OP_IF...OP_ENDIF block beginning at `start` (the op right
    after OP_IF). Returns (envelope_or_None, index_after_endif)."""
    j = start
    if j >= len(ops):
        return None, j

    # First element must be the marker push.
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
            return CounterEnvelope(content_type, b"".join(body_chunks), asset), j + 1
        if op in (OP_IF, OP_NOTIF):
            # Nested conditional inside the envelope is malformed for us.
            return None, _skip_to_endif(ops, j + 1)

        if in_body:
            if data is not None:
                body_chunks.append(data)
            j += 1
            continue

        # Field section.
        if op == OP_0 and data == b"":
            # Empty-push separator: body begins.
            in_body = True
            j += 1
            continue
        # content_type tag: canonical 0x01 data push, OR the legacy OP_1 (0x51)
        # pushnum form that also appears on-chain — accept both (build ref §4).
        is_content_type_tag = (
            data is not None and len(data) == 1 and data[0] == CONTENT_TYPE_TAG
        ) or op == OP_1
        if is_content_type_tag:
            # Tag 1 = content_type; value is the next push.
            j += 1
            if j < len(ops):
                _, ct = ops[j]
                content_type = ct if ct is not None else b""
                j += 1
            continue
        # Tag 2 = reinscription target asset; value is the next push (UTF-8
        # asset name/longname). Marks the envelope as a reinscription.
        is_asset_tag = data is not None and len(data) == 1 and data[0] == ASSET_TAG
        if is_asset_tag:
            j += 1
            if j < len(ops):
                _, av = ops[j]
                asset = av if av is not None else b""
                j += 1
            continue
        # Unknown field element: skip it (provisional "ignore unknown" policy;
        # the strict it's-okay-to-be-odd ruleset is deferred until the marker
        # and tags are frozen).
        j += 1

    # Reached end without OP_ENDIF: malformed.
    return None, j


def find_counter_envelopes(script: bytes) -> list[CounterEnvelope]:
    """Find all COUNT envelopes in a single script (e.g. a tapscript)."""
    # Fast reject: any real envelope must contain the marker bytes verbatim
    # (it is pushed as a literal). This skips full tokenization of the vast
    # majority of witness items (signatures, control blocks, etc.).
    if COUNT_MARKER not in script:
        return []
    try:
        ops = parse_script(script)
    except ScriptParseError:
        return []

    envelopes: list[CounterEnvelope] = []
    i = 0
    while i < len(ops) - 1:
        op, data = ops[i]
        nxt = ops[i + 1][0]
        # Envelope opener: OP_FALSE (empty push) immediately followed by OP_IF.
        if op == OP_0 and data == b"" and nxt == OP_IF:
            env, end = _parse_envelope(ops, i + 2)
            if env is not None:
                envelopes.append(env)
            i = max(end, i + 1)
        else:
            i += 1
    return envelopes


def find_counter_envelopes_in_witness(witness_items_hex: list[str]) -> list[CounterEnvelope]:
    """Scan every item of one input's witness for COUNT envelopes.

    We scan all items rather than only the canonical tapscript position
    (second-to-last, minus optional annex) for robustness: a signature or
    control block will not contain the OP_FALSE OP_IF "COUNT" pattern, so
    scanning everything is safe and avoids position/annex edge cases.
    """
    found: list[CounterEnvelope] = []
    for item_hex in witness_items_hex:
        try:
            item = bytes.fromhex(item_hex)
        except ValueError:
            continue
        found.extend(find_counter_envelopes(item))
    return found


def find_counter_envelopes_in_tx(vin: list[dict]) -> list[CounterEnvelope]:
    """Scan all inputs of a tx (bitcoind verbosity-2 vin list) for COUNT
    envelopes. 'Exactly one' is enforced by the caller, tx-wide."""
    found: list[CounterEnvelope] = []
    for txin in vin:
        witness = txin.get("txinwitness")
        if witness:
            found.extend(find_counter_envelopes_in_witness(witness))
    return found


def find_commit_txid(vin: list[dict]) -> str | None:
    """The commit transaction's txid, given a reveal's inputs.

    The COUNT envelope is revealed by the input that script-path-spends the
    commit output, so that input's prevout txid *is* the commit transaction.
    Returns None if no envelope-bearing input is present.
    """
    for txin in vin:
        witness = txin.get("txinwitness")
        if witness and find_counter_envelopes_in_witness(witness):
            return txin.get("txid")
    return None
