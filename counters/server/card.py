"""The social preview image for a counter — the `og:image` a link crawler gets.

Crawlers (Telegram, WhatsApp, Twitter, Discord, …) do not run JavaScript, so
they never see the explorer render a counter; all they can do is fetch one URL
and show the picture at the end of it. For counters whose content *is* a
picture that URL is just `/content/<n>`. For the rest — plain text, pointers,
HTML, audio, binary — this module draws a 1200x630 card that carries the same
information the detail page shows: the content itself on the left, and the
number, asset, badges and facts on the right.

The drawing is deliberately primitive: flat fills, 1px strokes, and a bitmap
font (`glyphs`) blitted at integer scales onto a raw RGB buffer, encoded by
`png`. No imaging or font library is involved. The pixel-font look is not a
compromise dressed up as a style — it matches the odometer the explorer already
uses for counter numbers.

Card images are pure functions of (content, display fields, `VERSION`), so the
server caches them on disk keyed by that tuple; bump `VERSION` on any visual
change to invalidate the cache.
"""

from __future__ import annotations

import math

from . import glyphs, png

# Bump on any change that alters rendered output, so cached cards are rebuilt.
VERSION = 1

WIDTH, HEIGHT = 1200, 630

# The explorer's palette (static/index.html `:root`), so a shared link looks
# like the page it points at.
BG = (0x14, 0x11, 0x0D)
PANEL = (0x1B, 0x17, 0x12)
CARD = (0x1E, 0x1A, 0x14)
LINE = (0x33, 0x2A, 0x1F)
INK = (0xEC, 0xE3, 0xD4)
DIM = (0xA4, 0x95, 0x7F)
FAINT = (0x6E, 0x62, 0x53)
COPPER = (0xE0, 0x68, 0x2F)
COPPER2 = (0xF3, 0x90, 0x5A)
PATINA = (0x5B, 0xB3, 0x94)

PAD = 40
GUTTER = 40
HEAD_Y = 34
BODY_Y = 96
BODY_H = HEIGHT - BODY_Y - PAD
LEFT_W = 534
RIGHT_X = PAD + LEFT_W + GUTTER
RIGHT_W = WIDTH - PAD - RIGHT_X


class Canvas:
    """A flat RGB pixel buffer with just enough drawing to compose the card."""

    def __init__(self, width: int, height: int, bg: tuple[int, int, int]):
        self.w, self.h = width, height
        self.buf = bytearray(bytes(bg) * (width * height))

    def to_png(self) -> bytes:
        return png.encode(self.w, self.h, bytes(self.buf))

    def fill(self, x: int, y: int, w: int, h: int, color: tuple[int, int, int]) -> None:
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(self.w, x + w), min(self.h, y + h)
        if x1 <= x0 or y1 <= y0:
            return
        run = bytes(color) * (x1 - x0)
        for yy in range(y0, y1):
            o = (yy * self.w + x0) * 3
            self.buf[o:o + len(run)] = run

    def blit(self, x: int, y: int, w: int, h: int, rgb: bytes) -> None:
        """Paste a raw RGB block, clipped to the canvas."""
        for row in range(h):
            yy = y + row
            if not 0 <= yy < self.h:
                continue
            x0 = max(0, x)
            x1 = min(self.w, x + w)
            if x1 <= x0:
                continue
            src = (row * w + (x0 - x)) * 3
            dst = (yy * self.w + x0) * 3
            self.buf[dst:dst + (x1 - x0) * 3] = rgb[src:src + (x1 - x0) * 3]

    def _rows(self, w: int, h: int, radius: int):
        """Yield (row, inset) for a rounded rect: how far each row is cut in.

        The inset traces a quarter circle (integer `isqrt`), so a radius of
        h // 2 gives a true pill rather than a hexagon.
        """
        r = max(0, min(radius, w // 2, h // 2))
        for row in range(h):
            if row < r:
                dy = r - row - 1
            elif row >= h - r:
                dy = r - (h - row)
            else:
                yield row, 0
                continue
            yield row, r - math.isqrt(max(0, r * r - dy * dy))

    def box(self, x: int, y: int, w: int, h: int, *, radius: int = 0,
            fill: tuple[int, int, int] | None = None,
            stroke: tuple[int, int, int] | None = None) -> None:
        """A 1px-stroked, optionally filled rectangle with cut corners.

        Corners are cut on the diagonal rather than arced: at these sizes the
        two are indistinguishable, and it keeps the whole thing integer math.
        `fill` and `stroke` are independent, so a pill can be outlined without
        painting over whatever it sits on.
        """
        if fill is not None:
            for row, d in self._rows(w, h, radius):
                self.fill(x + d, y + row, w - 2 * d, 1, fill)
        if stroke is None:
            return
        prev = None
        for row, d in self._rows(w, h, radius):
            if row in (0, h - 1):
                self.fill(x + d, y + row, w - 2 * d, 1, stroke)
            else:
                # Widen the edge across a corner step so the diagonal stays
                # connected instead of breaking into dots.
                run = abs(prev - d) + 1 if prev is not None else 1
                self.fill(x + d, y + row, run, 1, stroke)
                self.fill(x + w - d - run, y + row, run, 1, stroke)
            prev = d

    def text(self, x: int, y: int, s: str, scale: int,
             color: tuple[int, int, int], tracking: int = 0) -> int:
        """Draw `s` with its top-left at (x, y); returns the width drawn."""
        s = glyphs.fold(s)
        cw = glyphs.CELL_W * scale + tracking
        px = bytes(color)
        for ch in s:
            if ch != " ":
                for ry, bits in enumerate(glyphs.rows(ch)):
                    if not bits:
                        continue
                    for cx in range(glyphs.CELL_W):
                        if (bits >> (glyphs.CELL_W - 1 - cx)) & 1:
                            self.fill(x + cx * scale, y + ry * scale,
                                      scale, scale, px)
            x += cw
        return len(s) * cw


def text_width(s: str, scale: int, tracking: int = 0) -> int:
    return len(glyphs.fold(s)) * (glyphs.CELL_W * scale + tracking)


def _wrap(s: str, cols: int, max_lines: int) -> list[str]:
    """Hard-wrap to `cols` columns, honouring existing newlines.

    Content is arbitrary inscribed bytes, so this wraps mid-word rather than on
    word boundaries — a 64-character hash or URI has no spaces to break at.
    """
    s = glyphs.fold(s)
    out: list[str] = []
    for para in s.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not para:
            out.append("")
        while para and len(out) <= max_lines:
            out.append(para[:cols])
            para = para[cols:]
        if len(out) > max_lines:
            break
    if len(out) > max_lines:
        out = out[:max_lines]
        if out:
            out[-1] = out[-1][:max(0, cols - 3)] + "..."
    return out


def fmt_size(n: int) -> str:
    """Byte counts exactly as the explorer's `fmtSize` prints them."""
    if n < 1024:
        return f"{n} B"
    if n < 1048576:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1048576:.2f} MB"


def _meter(c: Canvas, x: int, y: int, number: int, scale: int) -> None:
    """The explorer's odometer: each digit in its own recessed box."""
    text = f"{number:,}"
    dw = glyphs.CELL_W * scale
    bw, bh = dw + 2 * scale + 8, glyphs.CELL_H * scale + 12
    gap = 5
    for ch in text:
        if ch == ",":
            c.text(x, y + bh - glyphs.CELL_H * scale - 6, ",", scale, FAINT)
            x += dw // 2 + gap
            continue
        c.box(x, y, bw, bh, radius=4, fill=CARD, stroke=LINE)
        c.text(x + (bw - dw) // 2, y + 6, ch, scale, COPPER2)
        x += bw + gap


BADGE_SCALE = 2
BADGE_H = glyphs.CELL_H * BADGE_SCALE + 10
BADGE_GAP = 10


def _badge_width(label: str) -> int:
    return text_width(label, BADGE_SCALE, 2) + 46


def _badge(c: Canvas, x: int, y: int, label: str,
           color: tuple[int, int, int]) -> None:
    """A pill like the detail page's `.vbadge`, dot and all."""
    w = _badge_width(label)
    c.box(x, y, w, BADGE_H, radius=BADGE_H // 2, stroke=color)
    c.fill(x + 16, y + BADGE_H // 2 - 3, 6, 6, color)
    c.text(x + 32, y + 5, label, BADGE_SCALE, color, 2)


def _badges(row: dict) -> list[tuple[str, tuple[int, int, int]]]:
    out = [("VALID", PATINA)]
    if row.get("kind") == "fairminter":
        out.append(("FAIRMINTER", COPPER2))
    if row.get("original") is False:
        out.append(("REINSCRIBE", COPPER2))
    if row.get("is_pointer_like"):
        out.append(("POINTER", COPPER2))
    if row.get("envelope") == "ord":
        out.append(("ORDINAL", COPPER2))
    return out


def _stage(c: Canvas, info: dict) -> None:
    """The left panel — the counter's own content, as the detail page shows it."""
    x, y, w, h = PAD, BODY_Y, LEFT_W, BODY_H
    c.box(x, y, w, h, radius=16, fill=PANEL, stroke=LINE)

    # The detail page's `.cap` strip: content type left, size right.
    cap_y = y + h - 40
    c.text(x + 24, cap_y, info["content_type"] or "unknown", 2, FAINT)
    size = fmt_size(info["size"] or 0)
    c.text(x + w - 24 - text_width(size, 2), cap_y, size, 2, FAINT)
    h -= 56  # keep the content clear of the caption

    body = info.get("body")
    if body:
        # Like the explorer's `sampleRender`: set the content as large as it
        # can be drawn without truncating, so a four-character counter reads
        # from across the room and a long one still fits the frame.
        inner_w, inner_h = w - 48, h - 48
        shown = glyphs.fold(body)
        for scale in (5, 4, 3, 2):
            cols = max(1, inner_w // (glyphs.CELL_W * scale))
            line_h = glyphs.CELL_H * scale + 6
            lines = _wrap(shown, cols, max(1, inner_h // line_h))
            if sum(len(line) for line in lines) >= len(shown) or scale == 2:
                break
        ty = y + (h - len(lines) * line_h) // 2
        for line in lines:
            c.text(x + (w - text_width(line, scale)) // 2, ty, line, scale, INK)
            ty += line_h
        return

    # No displayable text: name the format, the way the grid's `.ext` chip does.
    ct = info["content_type"] or "application/octet-stream"
    label = (ct.split("/")[-1] or ct).upper()[:12]
    kind = ct.split("/")[0].upper()
    scale = 5 if len(label) <= 8 else 3
    c.text(x + (w - text_width(label, scale)) // 2,
           y + h // 2 - glyphs.CELL_H * scale // 2 - 20, label, scale, COPPER)
    c.text(x + (w - text_width(kind, 2, 4)) // 2,
           y + h // 2 + glyphs.CELL_H * scale // 2 - 4, kind, 2, FAINT, 4)


def _facts(c: Canvas, x: int, y: int, w: int, bottom: int, info: dict) -> None:
    """Key/value lines from the detail page, as many as the space left allows.

    Content type and size are already in the panel caption, so these are the
    next most useful: who holds it, when it landed, how much of it there is.
    """
    rows = [("OWNER", _short(info.get("owner") or "")),
            ("BLOCK", f"{info['block']:,}")]
    if info.get("supply") is not None:
        supply = (f"{info['supply'] / 1e8:,.8f}".rstrip("0").rstrip(".")
                  if info.get("divisible") else f"{info['supply']:,}")
        rows.append(("SUPPLY", supply))
    if info.get("sha256"):
        rows.append(("SHA-256", _short(info["sha256"])))

    step = glyphs.CELL_H * 2 + 14
    for label, value in rows:
        if not value or y + glyphs.CELL_H * 2 > bottom:
            break
        used = c.text(x, y, f"{label} ", 2, FAINT)
        c.text(x + used, y, value, 2, DIM)
        y += step


def _short(s: str) -> str:
    # Folds to 19 rendered characters, since '…' expands to '...'.
    return s if len(s) <= 20 else f"{s[:8]}…{s[-8:]}"


def render(info: dict) -> bytes:
    """Draw the 1200x630 card for one counter and return PNG bytes.

    `info` carries the display fields the detail page shows: number, asset,
    content_type, size, block, owner, kind, envelope, is_pointer_like,
    original, and `body` (the decoded text content, if it has one).
    """
    c = Canvas(WIDTH, HEIGHT, BG)

    # Header: wordmark left, domain right, hairline under both.
    c.text(PAD, HEAD_Y, "BITCOIN COUNTERS", 2, DIM, 6)
    domain = "bitcoincounters.com"
    c.text(WIDTH - PAD - text_width(domain, 2), HEAD_Y, domain, 2, FAINT)
    c.fill(PAD, BODY_Y - 22, WIDTH - 2 * PAD, 1, LINE)

    _stage(c, info)

    x, y = RIGHT_X, BODY_Y
    _meter(c, x, y, info["number"], 3)
    y += glyphs.CELL_H * 3 + 12 + 22

    asset = info["asset"] or ""
    scale = 4 if len(asset) <= 13 else 3 if len(asset) <= 18 else 2
    cols = max(1, RIGHT_W // (glyphs.CELL_W * scale))
    c.text(x, y, _wrap(asset, cols, 1)[0] if asset else "", scale, COPPER2)
    y += glyphs.CELL_H * scale + 20

    bx = x
    for label, color in _badges(info):
        w = _badge_width(label)
        if bx > x and bx + w > RIGHT_X + RIGHT_W:
            bx, y = x, y + BADGE_H + 12
        _badge(c, bx, y, label, color)
        bx += w + BADGE_GAP
    y += BADGE_H + 26

    _facts(c, x, y, RIGHT_W, BODY_Y + BODY_H, info)
    return c.to_png()
