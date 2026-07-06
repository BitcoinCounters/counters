"""Unit tests for the COUNT envelope parser.

Run: python -m pytest  (from the indexer/ dir), or `python tests/test_envelope.py`.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counters.envelope import (  # noqa: E402
    CounterEnvelope,
    find_counter_envelopes,
    parse_script,
)


# --- script-building helpers (canonical minimal pushes) --------------------

def push(data: bytes) -> bytes:
    """Encode a minimal data push for payloads up to 0xffff bytes."""
    n = len(data)
    if n == 0:
        return b"\x00"  # OP_0
    if n < 0x4C:
        return bytes([n]) + data
    if n <= 0xFF:
        return b"\x4c" + bytes([n]) + data
    if n <= 0xFFFF:
        return b"\x4d" + n.to_bytes(2, "little") + data
    raise ValueError("too big for test helper")


OP_FALSE = b"\x00"
OP_IF = b"\x63"
OP_ENDIF = b"\x68"
OP_CHECKSIG = b"\xac"
XONLY = b"\x11" * 32


def envelope(content_type: bytes | None, body_chunks: list[bytes], marker: bytes = b"COUNT",
             asset: bytes | None = None) -> bytes:
    s = OP_FALSE + OP_IF + push(marker)
    if content_type is not None:
        s += push(b"\x01") + push(content_type)
    if asset is not None:
        s += push(b"\x02") + push(asset)
    s += b"\x00"  # empty-push separator
    for chunk in body_chunks:
        s += push(chunk)
    s += OP_ENDIF + push(XONLY) + OP_CHECKSIG
    return s


# --- tests -----------------------------------------------------------------

def test_simple_counter():
    script = envelope(b"image/png", [b"\xde\xad\xbe\xef"])
    envs = find_counter_envelopes(script)
    assert envs == [CounterEnvelope(b"image/png", b"\xde\xad\xbe\xef")]


def test_multi_chunk_body_is_concatenated():
    script = envelope(b"text/plain", [b"hello ", b"world"])
    envs = find_counter_envelopes(script)
    assert len(envs) == 1
    assert envs[0].body == b"hello world"
    assert envs[0].content_type == b"text/plain"


def test_empty_body_is_valid():
    script = envelope(b"image/png", [])
    envs = find_counter_envelopes(script)
    assert envs == [CounterEnvelope(b"image/png", b"")]


def test_empty_content_type_allowed():
    script = envelope(b"", [b"data"])
    envs = find_counter_envelopes(script)
    assert envs == [CounterEnvelope(b"", b"data")]


def test_marker_only_no_content_type_no_body():
    # marker + separator only -> still a valid (empty) counter
    script = OP_FALSE + OP_IF + push(b"COUNT") + b"\x00" + OP_ENDIF + push(XONLY) + OP_CHECKSIG
    envs = find_counter_envelopes(script)
    assert envs == [CounterEnvelope(b"", b"")]


def test_non_counter_marker_ignored():
    script = envelope(b"image/png", [b"data"], marker=b"ord")
    assert find_counter_envelopes(script) == []


def test_legacy_count_seven_byte_marker_ignored():
    # The old 7-byte "COUNTER" marker is NOT the protocol marker; must be skipped.
    script = envelope(b"image/png", [b"data"], marker=b"COUNTER")
    assert find_counter_envelopes(script) == []


def test_content_type_tag_op1_form():
    # tag 1 encoded as the legacy OP_1 (0x51) pushnum instead of a 0x01 push.
    op1 = b"\x51"
    script = (
        OP_FALSE + OP_IF + push(b"COUNT")
        + op1 + push(b"image/png")
        + b"\x00" + push(b"data")
        + OP_ENDIF + push(XONLY) + OP_CHECKSIG
    )
    envs = find_counter_envelopes(script)
    assert envs == [CounterEnvelope(b"image/png", b"data")]


def test_reinscription_asset_tag_parsed():
    # tag 2 = target asset; marks the envelope as a reinscription.
    script = envelope(b"image/png", [b"data"], asset=b"RAREPEPE")
    envs = find_counter_envelopes(script)
    assert envs == [CounterEnvelope(b"image/png", b"data", b"RAREPEPE")]
    assert envs[0].asset == b"RAREPEPE"


def test_no_asset_tag_means_empty_asset():
    # A creation-style envelope (no tag 2) leaves asset empty.
    envs = find_counter_envelopes(envelope(b"image/png", [b"data"]))
    assert envs[0].asset == b""


def test_asset_tag_subasset_longname():
    script = envelope(b"text/plain", [b"x"], asset=b"PARENT.CHILD")
    assert find_counter_envelopes(script)[0].asset == b"PARENT.CHILD"


def test_builder_reinscription_roundtrips_through_parser():
    # The real builder must produce an envelope the parser reads back exactly.
    from counters import builder

    body = b"\x89PNG reinscribed"
    leaf = builder.build_envelope(b"image/png", body, asset=b"A95428956661682177")
    envs = find_counter_envelopes(leaf)
    assert len(envs) == 1
    assert envs[0].content_type == b"image/png"
    assert envs[0].body == body
    assert envs[0].asset == b"A95428956661682177"


def test_builder_creation_has_no_asset():
    from counters import builder

    leaf = builder.build_envelope(b"text/plain", b"hi")
    assert find_counter_envelopes(leaf)[0].asset == b""


def test_no_envelope():
    # a plausible signature-ish blob: a single 64-byte push, no OP_IF
    script = push(b"\x07" * 64)
    assert find_counter_envelopes(script) == []


def test_truncated_script_does_not_raise():
    # declared push longer than available bytes -> parser returns []
    script = b"\x05ab"  # says push 5 bytes, only 2 present
    assert find_counter_envelopes(script) == []


def test_large_body_pushdata2():
    body = b"A" * 1000  # forces OP_PUSHDATA2 in the helper
    script = envelope(b"application/octet-stream", [body])
    envs = find_counter_envelopes(script)
    assert envs[0].body == body


def test_parse_script_roundtrip_basic():
    ops = parse_script(push(b"hi") + b"\x63" + b"\x68")
    assert ops[0] == (2, b"hi")
    assert ops[1] == (0x63, None)
    assert ops[2] == (0x68, None)


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    print(f"\n{'OK' if failures == 0 else f'{failures} FAILED'}")
    raise SystemExit(1 if failures else 0)
