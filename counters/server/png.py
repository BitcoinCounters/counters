"""A tiny pure-stdlib PNG encoder for the social preview card.

`card` draws onto a raw RGB buffer, and `encode` writes it as an 8-bit RGB,
non-interlaced PNG with one filter per row: `zlib` + `struct` already ship
with Python. A counter's own picture is re-encoded by `picture` instead, which
has a real imaging library to decode what minters inscribe.
"""

from __future__ import annotations

import struct
import zlib

SIG = b"\x89PNG\r\n\x1a\n"


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def _cost(filtered: bytes) -> int:
    """The spec's minimum-sum-of-absolute-differences filter heuristic."""
    return sum(x if x < 128 else 256 - x for x in filtered)


def encode(width: int, height: int, rgb: bytes) -> bytes:
    """Serialize `width * height * 3` RGB bytes as an 8-bit truecolour PNG."""
    if len(rgb) != width * height * 3:
        raise ValueError(f"expected {width * height * 3} bytes, got {len(rgb)}")
    stride = width * 3
    # Each row takes filter 2 ("Up") or 1 ("Sub"), whichever scores better: Up
    # turns the flat fills and repeated rows a card is mostly made of into runs
    # of zero bytes, Sub does the same for horizontal runs within a row. Row 0's predecessor is all zeros (per spec), so Up there
    # degenerates to "None" and the choice stays honest.
    out = bytearray()
    prev = bytes(stride)
    for y in range(height):
        row = rgb[y * stride:(y + 1) * stride]
        if row == prev:
            out += b"\x02" + bytes(stride)
        else:
            up = bytes((a - b) & 0xFF for a, b in zip(row, prev))
            sub = bytes((row[i] - (row[i - 3] if i >= 3 else 0)) & 0xFF
                        for i in range(stride))
            if _cost(sub) < _cost(up):
                out += b"\x01" + sub
            else:
                out += b"\x02" + up
        prev = row
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (SIG + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(bytes(out), 9))
            + _chunk(b"IEND", b""))
