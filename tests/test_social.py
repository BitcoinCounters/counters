"""Tests for the social preview images (`og:image`) served for /c/<id>.

Covers the layers: the PNG encoder, a counter's own picture re-encoded for a
crawler, the card renderer, and the server's choice of which image a link
crawler is pointed at.
"""

from __future__ import annotations

import io
import os
import random
import re
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image  # noqa: E402

from counters.config import Config  # noqa: E402
from counters.server import app as appmod, card, glyphs, picture, png  # noqa: E402
from counters.store import CounterRecord, Store  # noqa: E402


# --- PNG encoder ----------------------------------------------------------

def _decode(data: bytes) -> tuple[int, int, bytes]:
    with Image.open(io.BytesIO(data)) as im:
        return im.width, im.height, im.convert("RGB").tobytes()


def test_png_roundtrip():
    rnd = random.Random(7)
    for w, h in [(1, 1), (3, 2), (17, 5), (64, 40)]:
        rgb = bytes(rnd.randrange(256) for _ in range(w * h * 3))
        assert _decode(png.encode(w, h, rgb)) == (w, h, rgb)


def test_png_encode_rejects_wrong_length():
    try:
        png.encode(2, 2, b"\x00" * 11)
    except ValueError:
        return
    raise AssertionError("expected ValueError on a short pixel buffer")


def test_png_encode_gradient_roundtrips():
    # A horizontal ramp: Sub filters it to near-zero, Up does not — the
    # adaptive choice must still decode to the exact pixels.
    row = bytes(v for x in range(64) for v in (x * 4, x * 4, x * 4))
    rgb = row * 40
    assert _decode(png.encode(64, 40, rgb)) == (64, 40, rgb)


# --- a counter's own picture ----------------------------------------------

def _image(fmt: str, w: int, h: int, *, noise: bool = False, **save) -> bytes:
    rnd = random.Random(w * 7919 + h)
    if noise:
        im = Image.frombytes("RGB", (w, h),
                             rnd.randbytes(w * h * 3))
    else:
        im = Image.new("RGB", (w, h))
        for y in range(h):
            for x in range(w):
                im.putpixel((x, y), ((x * 37) % 256, (y * 91) % 256, 120))
    buf = io.BytesIO()
    im.save(buf, fmt, **save)
    return buf.getvalue()


def _animated_gif(w: int, h: int) -> bytes:
    frames = [Image.new("RGB", (w, h), c) for c in ((255, 0, 0), (0, 0, 255))]
    buf = io.BytesIO()
    frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:],
                   duration=100, loop=0)
    return buf.getvalue()


SVG_FLAT = (b"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 300 420'>"
            b"<rect width='300' height='420' fill='#8000ff'/>"
            b"<rect x='100' width='100' height='420' fill='#ffffff'/></svg>")
SVG_SCRIPTED = (b"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 10 10'>"
                b"<script>document.title='x'</script></svg>")


def test_tiny_pixel_art_is_enlarged_by_whole_pixels():
    """#200 is 2x2, #90 24x24: previewed as a thumb unless scaled up."""
    for w, h in [(2, 2), (24, 24), (48, 30)]:
        src = _image("PNG", w, h)
        out = picture.render(src, "image/png")
        ow, oh, rgb = _decode(out)
        assert min(ow, oh) >= picture.LARGE_MIN and max(ow, oh) <= picture.MAX_DIM
        assert ow % w == 0 and ow // w == oh // h
        # Nearest-neighbour: shrinking back by the same factor is exact.
        with Image.open(io.BytesIO(out)) as im:
            back = im.convert("RGB").resize((w, h), Image.NEAREST).tobytes()
        assert back == _decode(src)[2]


def test_extreme_aspect_is_enlarged_as_far_as_the_long_side_allows():
    """#123 is 218x24: the short side can't reach 600 inside MAX_DIM."""
    ow, oh, _ = _decode(picture.render(_image("PNG", 218, 24), "image/png"))
    assert (ow, oh) == (218 * 5, 24 * 5)


def test_oversized_photo_fits_the_byte_budget():
    """#30 (474 KB JPEG) and #135 (3.9 MB GIF) were served raw and too big."""
    for fmt, ctype in [("JPEG", "image/jpeg"), ("PNG", "image/png"),
                       ("WEBP", "image/webp")]:
        src = _image(fmt, 1400, 1000, noise=True, **({"quality": 100}
                                                    if fmt != "PNG" else {}))
        assert len(src) > picture.MAX_BYTES          # premise
        out = picture.render(src, ctype)
        assert len(out) <= picture.MAX_BYTES
        w, h, _ = _decode(out)
        assert max(w, h) <= picture.MAX_DIM and min(w, h) >= picture.MIN_DIM
        assert picture.mime_of(out) in ("image/png", "image/jpeg")


def test_animated_gif_renders_its_first_frame():
    out = picture.render(_animated_gif(40, 40), "image/gif")
    w, h, rgb = _decode(out)
    assert rgb[:3] == bytes((255, 0, 0))


def test_serve_as_is():
    # Big enough, in budget, a format every crawler takes: untouched.
    assert picture.serve_as_is(_image("JPEG", 700, 700), "image/jpeg")
    # Small but animated: re-encoding would freeze it.
    assert picture.serve_as_is(_animated_gif(40, 40), "image/gif")
    # Small and still: enlarged instead.
    assert not picture.serve_as_is(_image("PNG", 48, 48), "image/png")
    # WebP previews unevenly across apps.
    assert not picture.serve_as_is(_image("WEBP", 700, 700), "image/webp")
    assert not picture.serve_as_is(b"not an image", "image/png")


def test_svg_is_rasterized_at_full_size():
    out = picture.render(SVG_FLAT, "image/svg+xml")
    w, h, rgb = _decode(out)
    assert h == picture.MAX_DIM and abs(w - picture.MAX_DIM * 300 / 420) <= 1
    assert rgb[:3] == bytes((0x80, 0x00, 0xFF))
    mid = (h // 2 * w + w // 2) * 3
    assert rgb[mid:mid + 3] == bytes((255, 255, 255))


def test_svg_text_is_drawn():
    """The MEMETICX SVGs are a word on a fill; without fonts, just the fill."""
    svg = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256">'
           b'<rect width="256" height="256" fill="#8000ff"/><text x="128" '
           b'y="145" text-anchor="middle" font-family="monospace" '
           b'font-size="36" fill="white">MEMETICX</text></svg>')
    _, _, rgb = _decode(picture.render(svg, "image/svg+xml"))
    assert bytes((255, 255, 255)) in {rgb[i:i + 3] for i in range(0, len(rgb), 3)}


def test_svg_that_is_not_self_contained_is_declined():
    """#203 scripts its own art; a static render is a blank or a loading
    screen. And resvg reads non-data hrefs off the local disk."""
    here = os.path.abspath(__file__).encode()
    declined = [
        SVG_SCRIPTED,
        b"<svg xmlns='http://www.w3.org/2000/svg'><foreignObject/></svg>",
        b"<svg xmlns='http://www.w3.org/2000/svg'><image href='" + here + b"'/></svg>",
        b"<svg xmlns='http://www.w3.org/2000/svg' xmlns:xlink='http://www.w3.org/1999/xlink'>"
        b"<image xlink:href = \"red.png\"/></svg>",
        b"<svg xmlns='http://www.w3.org/2000/svg'><image href='file:///etc/x.png'/></svg>",
        b"<!DOCTYPE svg [<!ENTITY e '/etc/x.png'>]><svg xmlns='http://www.w3.org/2000/svg'/>",
    ]
    for svg in declined:
        assert not picture.svg_renderable(svg), svg
        assert picture.render(svg, "image/svg+xml") is None
    inline = (b"<svg xmlns='http://www.w3.org/2000/svg' width='10' height='10'>"
              b"<defs><rect id='r' width='10' height='10' fill='red'/></defs>"
              b"<use href='#r'/><image href='data:image/png;base64,AAAA'/></svg>")
    assert picture.svg_renderable(inline)


def test_undecodable_picture_is_none():
    assert picture.render(b"GIF89a garbage", "image/gif") is None
    assert picture.render(b"<svg", "image/svg+xml") is None


# --- font + card ----------------------------------------------------------

def test_glyph_fold():
    # Latin-1 is drawable as-is; the explorer's typography folds to it.
    assert glyphs.fold("café") == "café"
    assert glyphs.fold("a … b") == "a ... b"
    assert glyphs.fold("‘q’ “q”") == "'q' \"q\""
    # Anything with no Latin-1 equivalent degrades rather than crashing.
    assert glyphs.fold("✓中") == "??"
    assert glyphs.rows("中") == glyphs.rows("?")
    assert len(glyphs.rows("A")) == glyphs.CELL_H


def _info(**over) -> dict:
    base = dict(number=87, asset="NIFTYFIFTY", content_type="text/plain",
                size=64, block=959264, owner="bc1q" + "x" * 38,
                kind="issuance", is_pointer_like=True, original=True,
                supply=25, divisible=False, sha256="ab" * 32,
                body="ipfs:bafkreifhnc7xsedjkrmr2a3agmyy3wu54kbbilvq")
    base.update(over)
    return base


def test_card_renders_a_decodable_png():
    out = card.render(_info())
    decoded = _decode(out)
    assert decoded is not None
    assert decoded[:2] == (card.WIDTH, card.HEIGHT)
    # Deterministic, so the on-disk cache key can be content-derived.
    assert card.render(_info()) == out


def test_card_survives_awkward_content():
    """Nothing on a counter is under our control, so none of it may raise."""
    cases = [
        _info(body=None, content_type="application/octet-stream"),
        _info(body="", asset="", supply=None, owner=None, sha256=None),
        _info(body="x" * 20000, size=20000),          # far more than fits
        _info(body="\n\n\n\r\nline\n", number=0),
        _info(body="中文 — unicode", asset="A" * 40),
        _info(number=1234567, kind="fairminter", original=False,
              content_type=None, supply=10**8, divisible=True),
    ]
    for info in cases:
        out = card.render(info)
        assert _decode(out)[:2] == (card.WIDTH, card.HEIGHT)


def test_card_wraps_without_dropping_content():
    lines = card._wrap("abcdefghij", 4, 9)
    assert lines == ["abcd", "efgh", "ij"]
    # Over the line budget, the tail is marked as truncated.
    assert card._wrap("a" * 100, 4, 2) == ["aaaa", "a..."]
    assert card.fmt_size(64) == "64 B"
    assert card.fmt_size(2048) == "2.0 KB"
    assert card.fmt_size(1433209) == "1.43 MB"
    # Decimal, like mempool.space — and like the explorer's own size badges,
    # whose thresholds are decimal. Binary divisors rendered these as
    # "390.6 KB" and "3.34 MB", contradicting the "over 400 KB" / "over 3.5 MB"
    # badge sitting beside them.
    assert card.fmt_size(400_000) == "400.0 KB"
    assert card.fmt_size(3_500_000) == "3.50 MB"


# --- server ---------------------------------------------------------------

def _noise_png(side: int) -> bytes:
    """An incompressible PNG, so it lands over MAX_BYTES like #95 does."""
    rnd = random.Random(11)
    return png.encode(side, side,
                      rnd.randbytes(side * side * 3))


BIG_PNG = _noise_png(600)
SMALL_GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04"
    b"\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D"
    b"\x01\x00;"
)
STAMP_TEXT = b"STAMP:" + __import__("base64").b64encode(SMALL_GIF)
ANIM_GIF = _animated_gif(40, 40)
BIG_STAMP = b"STAMP:" + __import__("base64").b64encode(ANIM_GIF)
FIT_JPEG = _image("JPEG", 700, 700)


def _seed(data_dir: str) -> Config:
    cfg = Config()
    cfg.data_dir = data_dir
    cfg.ensure_dirs()
    store = Store(cfg)
    rows = [
        ("TEXTONLY", "text/plain", b"ipfs:bafkrei" + b"a" * 40, True),
        ("SMALLGIF", "image/gif", SMALL_GIF, False),
        ("BIGPNG", "image/png", BIG_PNG, False),
        ("STAMPED", "text/plain", STAMP_TEXT, False),
        ("SVGONE", "image/svg+xml", SVG_FLAT, False),
        ("SVGSCRIPT", "image/svg+xml", SVG_SCRIPTED, False),
        ("ANIMGIF", "image/gif", ANIM_GIF, False),
        ("FITJPEG", "image/jpeg", FIT_JPEG, False),
        ("ANIMSTAMP", "text/plain", BIG_STAMP, False),
    ]
    for n, (asset, ctype, content, pointer) in enumerate(rows):
        sha = store.store_blob(content)
        store.add_counter(n, CounterRecord(
            asset=asset, asset_id=str(n), asset_longname=None, kind="issuance",
            content_type=ctype, content_type_raw=None, content_sha256=sha,
            content_length=len(content), is_pointer_like=pointer,
            mint_txid=f"{n:064x}", msg_index=0, block_index=902005 + n,
            cp_tx_index=n, source="bc1pstored", divisible=False, supply=1))
    store.commit()
    store.close()
    return cfg


def _run_server():
    cfg = _seed(tempfile.mkdtemp())
    appmod._live_asset = lambda config, asset: {}
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), appmod.Handler)
    httpd.config = cfg
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, cfg, f"http://127.0.0.1:{httpd.server_address[1]}"


def _get(base: str, path: str):
    try:
        with urllib.request.urlopen(base + path, timeout=30) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read()


def _og(base: str, number: int) -> dict[str, str]:
    status, ctype, body = _get(base, f"/c/{number}")
    assert status == 200 and "text/html" in ctype
    page = body.decode()
    tags = dict(re.findall(
        r'<meta (?:property|name)="((?:og|twitter):[^"]*)" content="([^"]*)"', page))
    # The page a human gets is still the SPA, tags swapped in place.
    assert "<!DOCTYPE html>" in page and "counters-icon.svg" in page
    return tags


def test_og_description_carries_supply_and_burned():
    httpd, cfg, base = _run_server()
    try:
        # An asset with a recorded snapshot previews "supply · burned 🔥" —
        # read from the store alone (the server here has no backends at all).
        store = Store(cfg)
        store.set_asset_snapshot("TEXTONLY", 2000, 100)
        store.close()
        d = _og(base, 0)["og:description"]
        assert "2,000 · 🔥 100" in d
        # No recorded burn: supply alone, no flame.
        d = _og(base, 1)["og:description"]
        assert "🔥" not in d and "SMALLGIF — 1 — a file inscribed" in d
    finally:
        httpd.shutdown()
        httpd.server_close()


def _path(url: str) -> str:
    return urllib.parse.urlparse(url).path


def test_og_image_points_at_the_counters_own_picture():
    httpd, cfg, base = _run_server()
    try:
        # A picture crawlers already show large is handed over untouched...
        tags = _og(base, 7)
        assert _path(tags["og:image"]) == "/content/7"
        assert tags["og:image:type"] == "image/jpeg"
        assert tags["twitter:card"] == "summary_large_image"
        # ...and so is a small animated GIF, which re-encoding would freeze.
        tags = _og(base, 6)
        assert _path(tags["og:image"]) == "/content/6"
        assert tags["og:image:type"] == "image/gif"
        # A stamp previews as its decoded image, not its base64 text.
        assert _path(_og(base, 8)["og:image"]) == "/stamp/8"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_small_pictures_are_enlarged_for_crawlers():
    """A 1x1 GIF, raw or as a stamp, would preview as a thumb or not at all."""
    httpd, cfg, base = _run_server()
    try:
        for number in (1, 3):
            tags = _og(base, number)
            path = _path(tags["og:image"])
            assert path == f"/social/{number}.png"
            # Its size depends on the picture, so none is claimed.
            assert "og:image:width" not in tags and "og:image:type" not in tags
            status, ctype, body = _get(base, path)
            assert status == 200 and ctype == "image/png"
            w, h, _ = _decode(body)
            assert (w, h) == (600, 600)
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_svg_previews_as_its_picture():
    httpd, cfg, base = _run_server()
    try:
        tags = _og(base, 4)
        assert _path(tags["og:image"]) == "/social/4.png"
        status, ctype, body = _get(base, "/social/4.png")
        assert status == 200 and ctype == "image/png"
        w, h, rgb = _decode(body)
        assert h == picture.MAX_DIM and rgb[:3] == bytes((0x80, 0x00, 0xFF))
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_og_image_renders_a_card_when_there_is_no_picture():
    httpd, cfg, base = _run_server()
    try:
        tags = _og(base, 0)             # plain text
        path = _path(tags["og:image"])
        assert path == "/social/0.png"
        assert tags["og:image:width"] == str(card.WIDTH)
        assert tags["og:image:height"] == str(card.HEIGHT)
        assert tags["og:image:type"] == "image/png"
        assert "0" in tags["og:image:alt"]
        # Plain text, and an SVG that scripts its own art: both get the card.
        for number in (0, 5):
            status, ctype, body = _get(base, f"/social/{number}.png")
            assert status == 200 and ctype == "image/png"
            assert _decode(body)[:2] == (card.WIDTH, card.HEIGHT)
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_oversized_image_is_downscaled_for_crawlers():
    httpd, cfg, base = _run_server()
    try:
        assert len(BIG_PNG) > picture.MAX_BYTES     # premise of the test
        tags = _og(base, 2)
        path = _path(tags["og:image"])
        assert path == "/social/2.png"

        status, ctype, body = _get(base, path)
        assert status == 200 and ctype == picture.mime_of(body)
        assert len(body) <= picture.MAX_BYTES
        # /content still serves the exact consensus bytes.
        assert _get(base, "/content/2")[2] == BIG_PNG
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_social_images_are_cached_and_reused():
    httpd, cfg, base = _run_server()
    try:
        first = _get(base, "/social/0.png")[2]
        cached = list(cfg.social_dir.glob("0-*.png"))
        assert len(cached) == 1 and cached[0].read_bytes() == first
        # A second request is served from that file, byte for byte.
        assert _get(base, "/social/0.png")[2] == first
        # The key pins both renderer versions, so a redesign invalidates it.
        assert f"-v{card.VERSION}.{picture.VERSION}.png" in cached[0].name
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_social_unknown_counter_is_404():
    httpd, cfg, base = _run_server()
    try:
        assert _get(base, "/social/999.png")[0] == 404
        assert _get(base, "/social/abc.png")[0] == 404
    finally:
        httpd.shutdown()
        httpd.server_close()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
