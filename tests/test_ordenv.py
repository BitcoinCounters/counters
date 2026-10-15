"""ordenv: ord inscription envelopes read out of a reveal's witness."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from counters import ordenv  # noqa: E402


def push(data: bytes) -> bytes:
    n = len(data)
    if n == 0:
        return b"\x00"
    if n < 0x4C:
        return bytes([n]) + data
    if n <= 0xFF:
        return b"\x4c" + bytes([n]) + data
    return b"\x4d" + n.to_bytes(2, "little") + data


def envelope(content_type: bytes, body: bytes, extra: dict[bytes, bytes] | None = None,
             chunk: int = 520) -> bytes:
    s = b"\x00\x63" + push(b"ord") + push(b"\x01") + push(content_type)
    for tag, value in (extra or {}).items():
        s += push(tag) + push(value)
    s += b"\x00"
    for i in range(0, len(body), chunk):
        s += push(body[i:i + chunk])
    return s + b"\x68"


def tx_with(*scripts: bytes) -> dict:
    """One input per script, each a 3-item script-path witness."""
    return {"vin": [{"txinwitness": ["aa" * 64, s.hex(), "c0" + "bb" * 32]}
                    for s in scripts]}


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40


def test_one_envelope():
    script = b"\x20" + b"\x11" * 32 + b"\xac" + envelope(b"image/png", PNG)
    found = ordenv.inscriptions(tx_with(script))
    assert [(i.index, i.content_type, i.body) for i in found] == [(0, "image/png", PNG)]
    assert ordenv.inscription(tx_with(script), 0).body == PNG
    assert ordenv.inscription(tx_with(script), 1) is None


def test_many_envelopes_number_by_input_then_order():
    """ord's numbering: `<txid>i<n>` counts envelopes input by input — the
    way one reveal carries seventy RARE.PEPE icons."""
    body = bytes(range(256)) * 5                      # chunks past 520 bytes rejoin
    first = envelope(b"image/png", PNG) + envelope(b"text/plain", b"second")
    second = b"\x20" + b"\x11" * 32 + b"\xac" + envelope(b"image/png", body,
                                                           {b"\x05": b"\xa1\x00"})
    found = ordenv.inscriptions(tx_with(first, second))
    assert [(i.index, i.content_type) for i in found] == [
        (0, "image/png"), (1, "text/plain"), (2, "image/png")]
    assert found[1].body == b"second" and found[2].body == body


def test_no_envelope_and_unparsable_witnesses():
    assert ordenv.inscriptions({"vin": [{"txinwitness": ["aa" * 64]}]}) == []
    assert ordenv.inscriptions({"vin": [{}]}) == []
    # A truncated push is not a script: contributes nothing, raises nothing.
    assert ordenv.inscriptions(tx_with(b"\x4c\x10abc")) == []
    # OP_FALSE OP_IF without "ord" is some other protocol's envelope.
    assert ordenv.inscriptions(tx_with(b"\x00\x63" + push(b"cp") + b"\x00" + push(b"x") + b"\x68")) == []


def test_body_without_content_type():
    script = b"\x00\x63" + push(b"ord") + b"\x00" + push(b"raw") + b"\x68"
    found = ordenv.inscriptions(tx_with(script))
    assert len(found) == 1 and found[0].content_type is None and found[0].body == b"raw"
