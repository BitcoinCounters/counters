"""Regenerate counters/server/glyphs.py from the X11 misc-fixed 10x20 font.

The explorer draws its social cards with an embedded bitmap font so the server
needs no font or imaging library. This is the build step that produced that
blob; it is not imported at runtime and nothing depends on it at request time.

Source: the ``xfonts-base`` package's ``10x20-ISO8859-1.pcf.gz``, whose PCF
``COPYRIGHT`` property reads "Public domain font.  Share and enjoy." The script
asserts that, so it cannot silently bake in a differently-licensed font.

Usage (Debian/Ubuntu):

    sudo apt-get install xfonts-base
    python tools/gen_glyphs.py

Only the tables needed to lay out fixed-pitch ASCII/Latin-1 are parsed
(properties, accelerators, metrics, encodings, bitmaps); PCF is documented in
the X11 ``pcf(5)`` format notes.
"""

import base64
import gzip
import pathlib
import struct
import textwrap
import zlib

PROPERTIES, ACCEL, METRICS, BITMAPS, ENCODINGS = 1, 2, 4, 8, 0x20
BDF_ACCEL = 0x100


class R:
    def __init__(self, buf, off, msb):
        self.b, self.o, self.msb = buf, off, msb

    def i32(self):
        v = struct.unpack_from(">i" if self.msb else "<i", self.b, self.o)[0]
        self.o += 4
        return v

    def i16(self):
        v = struct.unpack_from(">h" if self.msb else "<h", self.b, self.o)[0]
        self.o += 2
        return v

    def u8(self):
        v = self.b[self.o]
        self.o += 1
        return v


def load(path):
    buf = gzip.open(path, "rb").read() if path.endswith(".gz") else open(path, "rb").read()
    assert buf[:4] == b"\x01fcp", buf[:4]
    n = struct.unpack_from("<i", buf, 4)[0]
    tables = {}
    for i in range(n):
        t, fmt, size, off = struct.unpack_from("<4i", buf, 8 + 16 * i)
        tables[t] = (fmt, size, off)
    return buf, tables


def table_fmt(buf, off):
    return struct.unpack_from("<i", buf, off)[0]


def read_props(buf, tables):
    _, _, off = tables[PROPERTIES]
    fmt = table_fmt(buf, off)
    msb = bool(fmt & 4)
    r = R(buf, off + 4, msb)
    nprops = r.i32()
    entries = []
    for _ in range(nprops):
        name_off = r.i32()
        is_str = r.u8()
        val = r.i32()
        entries.append((name_off, is_str, val))
    pad = 0 if (nprops & 3) == 0 else 4 - (nprops & 3)
    r.o += pad
    strsize = r.i32()
    strings = buf[r.o:r.o + strsize]

    def s(o):
        return strings[o:strings.index(b"\0", o)].decode("latin-1")

    out = {}
    for name_off, is_str, val in entries:
        out[s(name_off)] = s(val) if is_str else val
    return out


def read_accel(buf, tables):
    _, _, off = tables.get(BDF_ACCEL) or tables[ACCEL]
    fmt = table_fmt(buf, off)
    msb = bool(fmt & 4)
    r = R(buf, off + 4, msb)
    r.o += 8  # 8 uint8 flags
    return r.i32(), r.i32()  # fontAscent, fontDescent


def read_metrics(buf, tables):
    _, _, off = tables[METRICS]
    fmt = table_fmt(buf, off)
    msb = bool(fmt & 4)
    r = R(buf, off + 4, msb)
    out = []
    if fmt & 0x100:  # compressed
        count = r.i16()
        for _ in range(count):
            out.append(tuple(r.u8() - 0x80 for _ in range(5)))
    else:
        count = r.i32()
        for _ in range(count):
            m = tuple(r.i16() for _ in range(5))
            r.i16()  # attributes
            out.append(m)
    return out


def read_encodings(buf, tables):
    _, _, off = tables[ENCODINGS]
    fmt = table_fmt(buf, off)
    msb = bool(fmt & 4)
    r = R(buf, off + 4, msb)
    min2, max2, min1, max1, default = (r.i16() for _ in range(5))
    enc = {}
    for b1 in range(min1, max1 + 1):
        for b2 in range(min2, max2 + 1):
            idx = struct.unpack_from(">H" if msb else "<H", buf, r.o)[0]
            r.o += 2
            if idx != 0xFFFF:
                enc[(b1 << 8 | b2) if max1 else b2] = idx
    return enc


def read_bitmaps(buf, tables):
    _, _, off = tables[BITMAPS]
    fmt = table_fmt(buf, off)
    msb = bool(fmt & 4)
    msbit = bool(fmt & 8)
    pad = 1 << (fmt & 3)
    r = R(buf, off + 4, msb)
    count = r.i32()
    offsets = [r.i32() for _ in range(count)]
    sizes = [r.i32() for _ in range(4)]
    data_start = r.o
    data = buf[data_start:data_start + sizes[fmt & 3]]
    return data, offsets, pad, msbit


def glyph_rows(data, off, width, height, pad, msbit):
    """Return `height` ints, each a `width`-bit row, MSB = leftmost pixel."""
    row_bytes = ((width + 7) // 8 + pad - 1) // pad * pad
    rows = []
    for y in range(height):
        chunk = data[off + y * row_bytes: off + (y + 1) * row_bytes]
        bits = 0
        for x in range(width):
            byte = chunk[x >> 3]
            bit = (byte >> (7 - (x & 7))) & 1 if msbit else (byte >> (x & 7)) & 1
            bits = (bits << 1) | bit
        rows.append(bits)
    return rows


def extract(path, lo=32, hi=126):
    buf, tables = load(path)
    props = read_props(buf, tables)
    metrics = read_metrics(buf, tables)
    enc = read_encodings(buf, tables)
    data, offsets, pad, msbit = read_bitmaps(buf, tables)

    ascent, descent = read_accel(buf, tables)
    cell_h = ascent + descent
    widths = {metrics[enc[c]][2] for c in range(lo, hi + 1) if c in enc}
    assert len(widths) == 1, widths
    cell_w = widths.pop()

    glyphs = {}
    for code in range(lo, hi + 1):
        gi = enc.get(code)
        cell = [0] * cell_h
        if gi is not None:
            lsb, rsb, adv, asc, desc = metrics[gi]
            w, h = rsb - lsb, asc + desc
            if w > 0 and h > 0:
                rows = glyph_rows(data, offsets[gi], w, h, pad, msbit)
                top = ascent - asc
                for i, bits in enumerate(rows):
                    y = top + i
                    if 0 <= y < cell_h:
                        # left-align into the cell at lsb, MSB = leftmost
                        cell[y] = (bits << (cell_w - w - lsb)) & ((1 << cell_w) - 1)
        glyphs[code] = cell
    return props, cell_w, cell_h, glyphs


SRC = "/usr/share/fonts/X11/misc/10x20-ISO8859-1.pcf.gz"
LO, HI = 32, 255

props, w, h, glyphs = extract(SRC, LO, HI)
assert (w, h) == (10, 20), (w, h)
assert props["COPYRIGHT"].startswith("Public domain"), props["COPYRIGHT"]

raw = bytearray()
for code in range(LO, HI + 1):
    for row in glyphs[code]:
        raw += row.to_bytes(2, "big")
assert len(raw) == (HI - LO + 1) * h * 2

blob = base64.b64encode(zlib.compress(bytes(raw), 9)).decode()
lines = "\n".join(f'    "{c}"' for c in textwrap.wrap(blob, 72))

print(f"raw={len(raw)}B  compressed+b64={len(blob)}B")

out = f'''"""The bitmap font behind the social card images. Generated; do not hand-edit.

X11 ``misc-fixed`` 10x20 (``-Misc-Fixed-Medium-R-Normal--20-200-75-75-C-100``),
whose PCF ``COPYRIGHT`` reads *"{props["COPYRIGHT"]}"* — so the
glyphs ship in-tree rather than depending on a font being installed, and the
server needs no font or imaging library at all.

``_BLOB`` is zlib+base64 of Latin-1 {LO}..{HI} packed as {h} big-endian uint16
rows per glyph, MSB = leftmost of the {w}px cell. Cells are pre-normalized to a
fixed {w}x{h} box (each glyph shifted by its left bearing and baseline), so
rendering is a plain bit test — see ``card.Canvas.text``. Codes {LO}..{HI} are
stored contiguously; the unassigned 127..159 range is simply blank.
"""

from __future__ import annotations

import base64
import zlib

CELL_W = {w}
CELL_H = {h}
FIRST = {LO}
LAST = {HI}

# Punctuation the explorer uses that Latin-1 has no glyph for, folded to the
# nearest thing the font does have rather than degrading to '?'.
_FOLD = {{
    "\\u2026": "...", "\\u2014": "-", "\\u2013": "-", "\\u2212": "-",
    "\\u2018": "'", "\\u2019": "'", "\\u201c": '"', "\\u201d": '"',
    "\\u2192": "->", "\\u2190": "<-", "\\u2022": "\\u00b7", "\\u00a0": " ",
    "\\u2009": " ", "\\u200b": "",
}}


def fold(s: str) -> str:
    """Rewrite a string into characters this font can actually draw."""
    out = []
    for ch in s:
        ch = _FOLD.get(ch, ch)
        for c in ch:
            out.append(c if FIRST <= ord(c) <= LAST else "?")
    return "".join(out)


def rows(ch: str) -> list[int]:
    """The {h} bitmask rows for ``ch``; anything unrenderable falls back to '?'."""
    code = ord(ch)
    if not FIRST <= code <= LAST:
        code = ord("?")
    off = (code - FIRST) * CELL_H * 2
    return [int.from_bytes(_ROWS[off + i * 2: off + i * 2 + 2], "big")
            for i in range(CELL_H)]


_BLOB = (
{lines}
)

# Latin-1 {LO}..{HI}, {h} rows each. Decoded once at import (~{len(raw)} bytes).
_ROWS = zlib.decompress(base64.b64decode(_BLOB))
'''

path = pathlib.Path(__file__).resolve().parent.parent / "counters" / "server" / "glyphs.py"
path.write_text(out)
print("wrote", path)
