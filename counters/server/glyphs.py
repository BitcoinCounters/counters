"""The bitmap font behind the social card images. Generated; do not hand-edit.

X11 ``misc-fixed`` 10x20 (``-Misc-Fixed-Medium-R-Normal--20-200-75-75-C-100``),
whose PCF ``COPYRIGHT`` reads *"Public domain font.  Share and enjoy."* — so the
glyphs ship in-tree rather than depending on a font being installed, and the
server needs no font or imaging library at all.

``_BLOB`` is zlib+base64 of Latin-1 32..255 packed as 20 big-endian uint16
rows per glyph, MSB = leftmost of the 10px cell. Cells are pre-normalized to a
fixed 10x20 box (each glyph shifted by its left bearing and baseline), so
rendering is a plain bit test — see ``card.Canvas.text``. Codes 32..255 are
stored contiguously; the unassigned 127..159 range is simply blank.
"""

from __future__ import annotations

import base64
import zlib

CELL_W = 10
CELL_H = 20
FIRST = 32
LAST = 255

# Punctuation the explorer uses that Latin-1 has no glyph for, folded to the
# nearest thing the font does have rather than degrading to '?'.
_FOLD = {
    "\u2026": "...", "\u2014": "-", "\u2013": "-", "\u2212": "-",
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    "\u2192": "->", "\u2190": "<-", "\u2022": "\u00b7", "\u00a0": " ",
    "\u2009": " ", "\u200b": "",
}


def fold(s: str) -> str:
    """Rewrite a string into characters this font can actually draw."""
    out = []
    for ch in s:
        ch = _FOLD.get(ch, ch)
        for c in ch:
            out.append(c if FIRST <= ord(c) <= LAST else "?")
    return "".join(out)


def rows(ch: str) -> list[int]:
    """The 20 bitmask rows for ``ch``; anything unrenderable falls back to '?'."""
    code = ord(ch)
    if not FIRST <= code <= LAST:
        code = ord("?")
    off = (code - FIRST) * CELL_H * 2
    return [int.from_bytes(_ROWS[off + i * 2: off + i * 2 + 2], "big")
            for i in range(CELL_H)]


_BLOB = (
    "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"
    "zehurb62ZXhefl6t7z/ATgjL"
)

# Latin-1 32..255, 20 rows each. Decoded once at import (~8960 bytes).
_ROWS = zlib.decompress(base64.b64decode(_BLOB))
