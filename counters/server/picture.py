"""A counter's own picture, re-encoded for a link crawler.

Chat apps are fussy about `og:image` in ways the explorer is not. WhatsApp
gives up on a file much over 300 KB, Telegram and Facebook shrink anything
under about 600 px to a small thumbnail beside the text, and nothing renders
SVG at all. So the picture a counter *is* can't always be handed over as it
stands. This module turns it into one that previews: vector drawings get
rasterized, big files get shrunk, and small ones get scaled up by whole
pixels so pixel art stays sharp.

It is the only part of the server that needs imaging libraries: Pillow reads
every raster format a counter has used, and resvg renders SVG. Each input is
bounded before it is decoded, because these are untrusted bytes read during
a request.
"""

from __future__ import annotations

import io
import re

# Bump on any change that alters the output, so cached images are rebuilt.
VERSION = 1

# WhatsApp is the strictest mainstream crawler about bytes.
MAX_BYTES = 300 * 1024
# og:image is displayed at a few hundred pixels wide; past this we are only
# spending the crawler's bandwidth.
MAX_DIM = 1200
# Below roughly this on the short side, Telegram renders a small square thumb
# and Facebook a side icon; from here up, the image gets the full-width layout.
LARGE_MIN = 600
# Stop shrinking here even if still over budget, rather than serve a thumbnail.
MIN_DIM = 320
# Decoding is fast, but a crafted header can claim a huge canvas. Nothing is
# lost when this trips: the counter gets its card, and /content has the file.
MAX_PIXELS = 50_000_000
# The formats every mainstream crawler displays as they are. WebP and the rest
# are re-encoded, since support for them in link previews varies by app.
CRAWLER_SAFE = ("image/jpeg", "image/png", "image/gif")

# Alpha is flattened onto black: several crawlers composite a transparent
# og:image onto white, which inverts the explorer's dark palette.
BACKGROUND = (0, 0, 0)

_JPEG_QUALITIES = (85, 70)
# An SVG's `href`s may only point inside the document. resvg reads any other
# href from the local disk, and those bytes would end up in a public image.
_HREF_RE = re.compile(rb"""href\s*=\s*(["'])(.*?)\1""", re.IGNORECASE | re.DOTALL)
# A script or foreignObject means the SVG draws itself in a browser; resvg
# would render an empty frame or a loading screen instead of the picture.
# An entity could build an href this check never sees.
_SVG_DECLINE_RE = re.compile(rb"<\s*(?:script|foreignObject)\b|<!ENTITY", re.IGNORECASE)


def is_svg(ctype: str) -> bool:
    return ctype.split(";")[0].strip().lower() == "image/svg+xml"


def svg_renderable(blob: bytes) -> bool:
    """True for an SVG this module can faithfully rasterize: self-contained,
    and drawn by its markup rather than by scripts at load time."""
    if _SVG_DECLINE_RE.search(blob):
        return False
    for _q, value in _HREF_RE.findall(blob):
        value = value.strip()
        if not (value.startswith(b"#") or value[:5].lower() == b"data:"):
            return False
    return True


def dimensions(blob: bytes) -> tuple[int, int, bool] | None:
    """(width, height, animated) from a raster image's header, without
    decoding its pixels; None when Pillow can't identify it."""
    from PIL import Image
    try:
        with Image.open(io.BytesIO(blob)) as im:
            return im.width, im.height, getattr(im, "n_frames", 1) > 1
    except Exception:
        return None


def serve_as_is(blob: bytes, ctype: str) -> bool:
    """Whether a crawler can take this file untouched: a format every app
    shows, within the byte budget, and either big enough for the full-width
    layout or animated (re-encoding would freeze it to one frame)."""
    if ctype.split(";")[0].strip().lower() not in CRAWLER_SAFE:
        return False
    if len(blob) > MAX_BYTES:
        return False
    dims = dimensions(blob)
    if dims is None:
        return False
    w, h, animated = dims
    return animated or min(w, h) >= LARGE_MIN


def render(blob: bytes, ctype: str) -> bytes | None:
    """PNG or JPEG bytes a crawler will fetch and show large, or None when the
    picture can't be decoded (the caller then draws the card)."""
    img = _svg(blob) if is_svg(ctype) else _raster(blob)
    if img is None:
        return None
    return _encode_for_crawler(img)


def _raster(blob: bytes):
    from PIL import Image
    try:
        im = Image.open(io.BytesIO(blob))
        if im.width * im.height > MAX_PIXELS:
            return None
        im.seek(0)   # the first frame of an animation
        im.load()
        return _flatten(im)
    except Exception:
        return None


def _svg(blob: bytes):
    """Rasterize with the long side at MAX_DIM. Vectors have no natural pixel
    size worth keeping, and the largest allowed render previews best."""
    if not svg_renderable(blob):
        return None
    import resvg_py
    from PIL import Image
    try:
        text = blob.decode("utf-8")
        # A 64 px wide render costs next to nothing and gives the aspect ratio
        # without trusting the declared width/height/viewBox.
        probe = Image.open(io.BytesIO(bytes(resvg_py.svg_to_bytes(
            svg_string=text, width=64))))
        pw, ph = probe.size
        if not pw or not ph or max(pw, ph) > 64 * 20:
            return None
        if pw >= ph:
            size = {"width": MAX_DIM}
        else:
            size = {"height": MAX_DIM}
        out = Image.open(io.BytesIO(bytes(resvg_py.svg_to_bytes(
            svg_string=text, **size, **_FONTS))))
        out.load()
        return _flatten(out)
    except Exception:
        return None


# resvg's generic families default to Windows font names. These are the DejaVu
# fonts the Docker image installs, the same ones a Linux browser falls back to.
_FONTS = {
    "font_family": "DejaVu Sans",
    "serif_family": "DejaVu Serif",
    "sans_serif_family": "DejaVu Sans",
    "monospace_family": "DejaVu Sans Mono",
}


def _flatten(im):
    from PIL import Image
    im = im.convert("RGBA")
    bg = Image.new("RGBA", im.size, BACKGROUND + (255,))
    return Image.alpha_composite(bg, im).convert("RGB")


def _candidates(img):
    """Sizes to try, best first: whole-pixel enlargements that reach the
    full-width layout, then the image fitted to MAX_DIM, then smaller steps
    down to MIN_DIM."""
    from PIL import Image
    w, h = img.size
    if max(w, h) > MAX_DIM:
        scale = MAX_DIM / max(w, h)
        img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))),
                         Image.LANCZOS)
        w, h = img.size
    if min(w, h) < LARGE_MIN:
        top = -(-LARGE_MIN // min(w, h))
        for factor in range(top, 1, -1):
            if max(w, h) * factor <= MAX_DIM:
                yield img.resize((w * factor, h * factor), Image.NEAREST)
    yield img
    while min(w, h) > MIN_DIM:
        w, h = max(1, w * 3 // 4), max(1, h * 3 // 4)
        yield img.resize((w, h), Image.LANCZOS)


def _encode_for_crawler(img) -> bytes:
    """The first candidate that fits MAX_BYTES, as PNG (lossless, and small
    for flat or pixel art) or else JPEG (small for photos). If nothing fits,
    the smallest attempt."""
    smallest = None
    for cand in _candidates(img):
        for data in _encodings(cand):
            if len(data) <= MAX_BYTES:
                return data
            if smallest is None or len(data) < len(smallest):
                smallest = data
    return smallest


def _encodings(img):
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=False, compress_level=9)
    yield buf.getvalue()
    for quality in _JPEG_QUALITIES:
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality, optimize=True)
        yield buf.getvalue()


def mime_of(data: bytes) -> str:
    """The Content-Type of bytes `render` produced."""
    return "image/jpeg" if data.startswith(b"\xff\xd8") else "image/png"
