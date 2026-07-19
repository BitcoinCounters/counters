"""Tests for proto-counter detection (a v3 counter that is ALSO a proto-counter,
i.e. its transaction carries a COUNT envelope). Enrichment only."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counters.proto import (  # noqa: E402
    find_count_envelopes,
    find_in_script,
    has_count_envelope,
)


def _push(data: bytes) -> bytes:
    """A minimal direct push (<= 75 bytes) of `data`."""
    assert len(data) < 0x4C
    return bytes([len(data)]) + data


OP_FALSE = b"\x00"
OP_IF = b"\x63"
OP_ENDIF = b"\x68"
OP_CHECKSIG = b"\xac"


def _count_envelope(content_type=b"", asset=b"", body=b"") -> bytes:
    """Build a COUNT envelope tapscript: OP_FALSE OP_IF "COUNT" [tags] OP_0 body OP_ENDIF."""
    s = OP_FALSE + OP_IF + _push(b"COUNT")
    if content_type:
        s += _push(b"\x01") + _push(content_type)  # tag 1 = content type
    if asset:
        s += _push(b"\x02") + _push(asset)          # tag 2 = target asset
    s += OP_FALSE  # empty-push separator: body begins
    if body:
        s += _push(body)
    s += OP_ENDIF
    # a realistic tapscript tail (never executed part is above)
    s += _push(b"\x11" * 32) + OP_CHECKSIG
    return s


def _tx(*witness_items: bytes) -> dict:
    return {"vin": [{"txinwitness": [w.hex() for w in witness_items]}]}


def test_detects_count_envelope():
    env = _count_envelope(content_type=b"image/png", asset=b"RAREPEPE", body=b"ABC")
    tx = _tx(b"\x30\x44sig", env, b"\xc0control")   # sig, tapscript, control block
    assert has_count_envelope(tx) is True
    found = find_count_envelopes(tx)
    assert len(found) == 1
    assert found[0].content_type == b"image/png"
    assert found[0].asset == b"RAREPEPE"
    assert found[0].body == b"ABC"


def test_minimal_envelope_no_tags():
    tx = _tx(b"sig", _count_envelope(body=b"hi"), b"ctrl")
    assert has_count_envelope(tx) is True
    assert find_count_envelopes(tx)[0].body == b"hi"


def test_no_envelope():
    # A plain Counterparty-style envelope (marker is NOT "COUNT") must not match.
    other = OP_FALSE + OP_IF + _push(b"CNTRPRTY") + OP_FALSE + _push(b"data") + OP_ENDIF
    tx = _tx(b"sig", other, b"ctrl")
    assert has_count_envelope(tx) is False
    assert find_count_envelopes(tx) == []


def test_marker_bytes_without_structure_dont_match():
    # "COUNT" bytes present, but not as OP_FALSE OP_IF <push "COUNT"> — no match.
    noise = _push(b"xxCOUNTxx") + OP_CHECKSIG
    assert find_in_script(noise) == []
    assert has_count_envelope(_tx(noise)) is False


def test_empty_or_missing_witness():
    assert has_count_envelope({"vin": []}) is False
    assert has_count_envelope({"vin": [{"txinwitness": []}]}) is False
    assert has_count_envelope({}) is False


def test_truncated_script_is_safe():
    # A push that runs past the end must not raise out of find_in_script.
    truncated = OP_FALSE + OP_IF + _push(b"COUNT") + b"\x20\x01\x02"  # says push 32, only 2
    assert find_in_script(truncated) == []


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
