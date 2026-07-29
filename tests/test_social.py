"""Tests for the social preview images (`og:image`) served for /c/<id>.

Covers the three layers: the PNG codec, the card renderer, and the server's
choice of which image a link crawler is pointed at.
"""

from __future__ import annotations

import os
import random
import re
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import zlib
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counters.config import Config  # noqa: E402
from counters.server import app as appmod, card, glyphs, png  # noqa: E402
from counters.store import CounterRecord, Store  # noqa: E402


# --- PNG codec ------------------------------------------------------------

def test_png_roundtrip():
    rnd = random.Random(7)
    for w, h in [(1, 1), (3, 2), (17, 5), (64, 40)]:
        rgb = bytes(rnd.randrange(256) for _ in range(w * h * 3))
        assert png.decode(png.encode(w, h, rgb)) == (w, h, rgb)


def test_png_encode_rejects_wrong_length():
    try:
        png.encode(2, 2, b"\x00" * 11)
    except ValueError:
        return
    raise AssertionError("expected ValueError on a short pixel buffer")


def test_png_decode_rejects_junk():
    assert png.decode(b"") is None
    assert png.decode(b"not a png at all") is None
    # Right signature, truncated body.
    assert png.decode(png.SIG + b"\x00" * 12) is None
    import struct
    ihdr = struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0)
    head = png.SIG + png._chunk(b"IHDR", ihdr)
    tail = png._chunk(b"IEND", b"")
    # Well-formed header, but the pixel data is not valid zlib.
    assert png.decode(head + png._chunk(b"IDAT", b"not zlib") + tail) is None
    # Valid zlib, but fewer scanlines than the header promises.
    short = zlib.compress(b"\x00\x01\x02\x03")
    assert png.decode(head + png._chunk(b"IDAT", short) + tail) is None
    # An unsupported bit depth is declined rather than mis-decoded.
    ihdr4 = struct.pack(">IIBBBBB", 2, 2, 4, 2, 0, 0, 0)
    assert png.decode(png.SIG + png._chunk(b"IHDR", ihdr4)
                      + png._chunk(b"IDAT", zlib.compress(b"\x00" * 8))
                      + tail) is None


def test_png_decode_greyscale_and_palette():
    """Colour types the encoder never emits but inscribed content may use."""
    def build(ctype: str, depth: int, rows: bytes, extra: bytes = b"") -> bytes:
        import struct
        ihdr = struct.pack(">IIBBBBB", 2, 2, depth, ctype, 0, 0, 0)
        return (png.SIG + png._chunk(b"IHDR", ihdr) + extra
                + png._chunk(b"IDAT", zlib.compress(rows))
                + png._chunk(b"IEND", b""))

    # 8-bit greyscale, filter 0 per row.
    grey = build(0, 8, b"\x00\x00\xff" + b"\x00\x80\x40")
    assert png.decode(grey) == (2, 2, bytes([0, 0, 0, 255, 255, 255,
                                             128, 128, 128, 64, 64, 64]))
    # Palette: index 0 red, index 1 green.
    plte = png._chunk(b"PLTE", bytes([255, 0, 0, 0, 255, 0]))
    pal = build(3, 8, b"\x00\x00\x01" + b"\x00\x01\x00", plte)
    assert png.decode(pal) == (2, 2, bytes([255, 0, 0, 0, 255, 0,
                                            0, 255, 0, 255, 0, 0]))


def test_png_decode_honours_the_pixel_cap():
    data = png.encode(40, 40, b"\x20" * (40 * 40 * 3))
    assert png.decode(data, max_pixels=1600) is not None
    assert png.decode(data, max_pixels=1599) is None
    assert png.decode(data) is not None      # uncapped by default


def test_png_decode_skips_interlaced():
    import struct
    ihdr = struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 1)   # interlace = Adam7
    data = (png.SIG + png._chunk(b"IHDR", ihdr)
            + png._chunk(b"IDAT", zlib.compress(b"\x00" * 14))
            + png._chunk(b"IEND", b""))
    assert png.decode(data) is None


def test_png_shrink_averages_blocks():
    # Two flat halves: every 2x2 block is uniform, so averaging is exact.
    rgb = bytes([0, 0, 0] * 2 + [255, 255, 255] * 2) * 4
    assert png.shrink(4, 4, rgb, 2) == (
        2, 2, bytes([0, 0, 0, 255, 255, 255, 0, 0, 0, 255, 255, 255]))
    # Never enlarges, and a factor of 1 is the identity.
    assert png.fit(4, 4, rgb, 99, 99) == (4, 4, rgb)
    assert png.factor_for(1254, 1254, 1200, 1200) == 2
    assert png.factor_for(400, 400, 1200, 1200) == 1


def test_png_upscale_is_shrink_inverse():
    rnd = random.Random(3)
    rgb = bytes(rnd.randrange(256) for _ in range(5 * 4 * 3))
    for factor in (2, 3):
        uw, uh, up = png.upscale(5, 4, rgb, factor)
        assert (uw, uh) == (5 * factor, 4 * factor)
        # Every pixel became a uniform factor² block, so box-averaging it
        # back down is exact.
        assert png.shrink(uw, uh, up, factor) == (5, 4, rgb)
    assert png.upscale(5, 4, rgb, 1) == (5, 4, rgb)


def test_png_encode_gradient_roundtrips():
    # A horizontal ramp: Sub filters it to near-zero, Up does not — the
    # adaptive choice must still decode to the exact pixels.
    row = bytes(v for x in range(64) for v in (x * 4, x * 4, x * 4))
    rgb = row * 40
    assert png.decode(png.encode(64, 40, rgb)) == (64, 40, rgb)


def test_enlarged_reaches_the_large_layout():
    # Coarse vertical bands stay cheap at any scale, so the pixel-doubled
    # copy fits the byte budget and crosses the large-layout threshold.
    row = b"".join(([0, 200][x // 10 % 2].to_bytes(1, "big") * 3)
                   for x in range(300))
    big = appmod._enlarged(300, 300, row * 300)
    assert big is not None and len(big) <= appmod.SOCIAL_MAX_BYTES
    w, h, rgb = png.decode(big)
    assert min(w, h) >= appmod.SOCIAL_LARGE_MIN
    assert (w, h, rgb) == png.upscale(300, 300, row * 300, w // 300)


def test_enlarged_declines_when_it_cannot_help():
    rnd = random.Random(5)
    # Already at the large layout: nothing to do.
    flat = bytes(600 * 600 * 3)
    assert appmod._enlarged(600, 600, flat) is None
    # Noise doubles past the byte budget, so the small copy stands.
    noise = bytes(rnd.randrange(256) for _ in range(500 * 500 * 3))
    assert appmod._enlarged(500, 500, noise) is None
    # Any multiple of the long side would blow SOCIAL_MAX_DIM.
    tall = bytes(350 * 700 * 3)
    assert appmod._enlarged(350, 700, tall) is None


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
    decoded = png.decode(out)
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
        assert png.decode(out)[:2] == (card.WIDTH, card.HEIGHT)


def test_card_wraps_without_dropping_content():
    lines = card._wrap("abcdefghij", 4, 9)
    assert lines == ["abcd", "efgh", "ij"]
    # Over the line budget, the tail is marked as truncated.
    assert card._wrap("a" * 100, 4, 2) == ["aaaa", "a..."]
    assert card.fmt_size(64) == "64 B"
    assert card.fmt_size(2048) == "2.0 KB"
    assert card.fmt_size(1433209) == "1.37 MB"


# --- server ---------------------------------------------------------------

def _noise_png(side: int) -> bytes:
    """An incompressible PNG, so it lands over SOCIAL_MAX_BYTES like #95 does."""
    rnd = random.Random(11)
    return png.encode(side, side,
                      bytes(rnd.randrange(256) for _ in range(side * side * 3)))


BIG_PNG = _noise_png(600)
SMALL_GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04"
    b"\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D"
    b"\x01\x00;"
)
STAMP_TEXT = b"STAMP:" + __import__("base64").b64encode(SMALL_GIF)


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
        ("SVGONE", "image/svg+xml", b"<svg xmlns='http://www.w3.org/2000/svg'/>", False),
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


def test_og_image_points_at_the_counters_own_picture():
    httpd, cfg, base = _run_server()
    try:
        # A small raster image is handed over untouched, so an animated GIF
        # still animates in the preview.
        tags = _og(base, 1)
        assert urllib.parse.urlparse(tags["og:image"]).path == "/content/1"
        assert tags["og:image:type"] == "image/gif"
        assert tags["twitter:card"] == "summary_large_image"

        # A stamp previews as its decoded image, not its base64 text.
        assert urllib.parse.urlparse(_og(base, 3)["og:image"]).path == "/stamp/3"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_og_image_renders_a_card_when_there_is_no_picture():
    httpd, cfg, base = _run_server()
    try:
        for number in (0, 4):        # plain text, and SVG (not a raster image)
            tags = _og(base, number)
            path = urllib.parse.urlparse(tags["og:image"]).path
            assert path == f"/social/{number}.png"
            assert tags["og:image:width"] == str(card.WIDTH)
            assert tags["og:image:height"] == str(card.HEIGHT)
            assert tags["og:image:type"] == "image/png"
            assert str(number) in tags["og:image:alt"]

            status, ctype, body = _get(base, path)
            assert status == 200 and ctype == "image/png"
            assert png.decode(body)[:2] == (card.WIDTH, card.HEIGHT)
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_oversized_image_is_downscaled_for_crawlers():
    httpd, cfg, base = _run_server()
    try:
        assert len(BIG_PNG) > appmod.SOCIAL_MAX_BYTES     # premise of the test
        tags = _og(base, 2)
        path = urllib.parse.urlparse(tags["og:image"]).path
        assert path == "/social/2.png"

        status, ctype, body = _get(base, path)
        assert status == 200 and ctype == "image/png"
        assert len(body) < len(BIG_PNG)
        width, height, _ = png.decode(body)
        assert width < 600 and height < 600
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
        # The key pins the renderer version, so a redesign invalidates it.
        assert f"-v{card.VERSION}.png" in cached[0].name
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
