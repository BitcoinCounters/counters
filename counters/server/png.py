"""A tiny pure-stdlib PNG codec, used to build the social preview images.

The explorer has to hand link crawlers (Telegram, WhatsApp, Twitter, …) a real
raster image for every counter — a rendered card for the ones whose content is
not a picture, and a downscaled copy of the ones whose picture is too big for a
crawler to fetch. That is the *only* imaging this server does, so it does it
here rather than pulling in an imaging library: `zlib` + `struct` already ship
with Python.

Scope is deliberately the minimum that job needs:

  * `encode` writes 8-bit RGB, non-interlaced, one filter per row.
  * `decode` reads 8-bit and 16-bit greyscale/RGB/palette/alpha, non-interlaced.
    Adam7-interlaced input returns None (rare, and not worth the code) — the
    caller then falls back to serving the original bytes.
  * `fit` box-downsamples to fit a bounding box.

Nothing here is a general-purpose image library, and it is not meant to grow
into one.
"""

from __future__ import annotations

import struct
import zlib

SIG = b"\x89PNG\r\n\x1a\n"


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def encode(width: int, height: int, rgb: bytes) -> bytes:
    """Serialize `width * height * 3` RGB bytes as an 8-bit truecolour PNG."""
    if len(rgb) != width * height * 3:
        raise ValueError(f"expected {width * height * 3} bytes, got {len(rgb)}")
    stride = width * 3
    # Filter 2 ("Up") costs one pass and turns the flat fills and repeated rows
    # a card is mostly made of into runs of zero bytes, which deflate to
    # almost nothing. Row 0 has no predecessor, so it stays filter 0 ("None").
    out = bytearray(b"\x00") + rgb[:stride]
    prev = rgb[:stride]
    for y in range(1, height):
        row = rgb[y * stride:(y + 1) * stride]
        if row == prev:
            out += b"\x02" + bytes(stride)
        else:
            out += b"\x02" + bytes((a - b) & 0xFF for a, b in zip(row, prev))
        prev = row
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (SIG + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(bytes(out), 9))
            + _chunk(b"IEND", b""))


# Bytes per pixel for each PNG colour type, at 8 bits per channel.
_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


def _unfilter(raw: bytes, height: int, stride: int, bpp: int) -> bytearray:
    """Reverse the per-row filters (PNG spec §9.2) into raw scanline bytes."""
    out = bytearray(height * stride)
    pos = 0
    for y in range(height):
        ft = raw[pos]
        pos += 1
        line = bytearray(raw[pos:pos + stride])
        pos += stride
        up_at = (y - 1) * stride
        if ft == 0:
            pass
        elif ft == 1:  # Sub
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif ft == 2:  # Up
            if y:
                for i in range(stride):
                    line[i] = (line[i] + out[up_at + i]) & 0xFF
        elif ft == 3:  # Average
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = out[up_at + i] if y else 0
                line[i] = (line[i] + ((a + b) >> 1)) & 0xFF
        elif ft == 4:  # Paeth
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = out[up_at + i] if y else 0
                c = out[up_at + i - bpp] if (y and i >= bpp) else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF
        else:
            raise ValueError(f"bad PNG filter type {ft}")
        out[y * stride:(y + 1) * stride] = line
    return out


def decode(data: bytes, max_pixels: int | None = None
           ) -> tuple[int, int, bytes] | None:
    """Decode to `(width, height, rgb)`, or None if unsupported/damaged.

    Alpha is flattened to black rather than kept, since the result is headed
    for an `og:image` and several crawlers composite transparent PNGs onto
    white, which would invert the explorer's dark palette.

    Unfiltering is per-byte Python, so cost is linear in pixel count with a
    large constant. `max_pixels` declines an image too big to decode inside a
    request instead of occupying a worker thread with it; the caller falls back
    to serving the original bytes.
    """
    if not data.startswith(SIG):
        return None
    width = height = depth = ctype = interlace = 0
    idat = bytearray()
    palette = b""
    trns = b""
    pos = len(SIG)
    try:
        while pos + 8 <= len(data):
            (length,) = struct.unpack_from(">I", data, pos)
            tag = data[pos + 4:pos + 8]
            body = data[pos + 8:pos + 8 + length]
            pos += 12 + length
            if tag == b"IHDR":
                (width, height, depth, ctype, _comp, _filt,
                 interlace) = struct.unpack(">IIBBBBB", body)
            elif tag == b"PLTE":
                palette = body
            elif tag == b"tRNS":
                trns = body
            elif tag == b"IDAT":
                idat += body
            elif tag == b"IEND":
                break
        if interlace or ctype not in _CHANNELS or depth not in (8, 16):
            return None
        if ctype == 3 and depth != 8:
            return None
        if not width or not height or not idat:
            return None
        if max_pixels is not None and width * height > max_pixels:
            return None
        chans = _CHANNELS[ctype]
        sample = depth // 8
        # Palette indices are always 8-bit regardless of the declared depth.
        bits = 8 if ctype == 3 else depth
        stride = (width * chans * bits + 7) // 8
        bpp = max(1, chans * bits // 8)
        raw = zlib.decompress(bytes(idat))
        if len(raw) < height * (stride + 1):
            return None
        flat = _unfilter(raw, height, stride, bpp)
    except (struct.error, zlib.error, ValueError):
        return None

    rgb = bytearray(width * height * 3)
    if ctype == 2 and depth == 8:
        rgb[:] = flat
    elif ctype == 3:
        if len(palette) < 3:
            return None
        # An index past the end of PLTE is malformed; clamp instead of failing.
        last = len(palette) // 3 - 1
        alpha = trns or b""
        for i, idx in enumerate(flat[:width * height]):
            idx = min(idx, last)
            r, g, b = palette[idx * 3:idx * 3 + 3]
            if idx < len(alpha):
                a = alpha[idx]
                r, g, b = (r * a) // 255, (g * a) // 255, (b * a) // 255
            rgb[i * 3:i * 3 + 3] = bytes((r, g, b))
    else:
        px = chans * sample
        grey = ctype in (0, 4)
        has_a = ctype in (4, 6)
        for i in range(width * height):
            o = i * px
            if grey:
                v = flat[o]
                r = g = b = v
            else:
                r, g, b = flat[o], flat[o + sample], flat[o + 2 * sample]
            if has_a:
                a = flat[o + px - sample]
                r, g, b = (r * a) // 255, (g * a) // 255, (b * a) // 255
            rgb[i * 3:i * 3 + 3] = bytes((r, g, b))
    return width, height, bytes(rgb)


def factor_for(width: int, height: int, max_w: int, max_h: int) -> int:
    """The smallest integer box factor that fits `width` x `height` in the box."""
    return max(1, -(-width // max_w), -(-height // max_h))


def fit(width: int, height: int, rgb: bytes, max_w: int, max_h: int
        ) -> tuple[int, int, bytes]:
    """Box-downsample so the image fits `max_w` x `max_h`, preserving aspect."""
    return shrink(width, height, rgb, factor_for(width, height, max_w, max_h))


def shrink(width: int, height: int, rgb: bytes, factor: int
           ) -> tuple[int, int, bytes]:
    """Average each `factor` x `factor` block down to one pixel.

    Integer box averaging only — `factor` 1 is a no-op, so images are never
    enlarged and anything already small enough comes back untouched.
    """
    if factor <= 1:
        return width, height, rgb
    ow, oh = max(1, width // factor), max(1, height // factor)
    out = bytearray(ow * oh * 3)
    stride = width * 3
    n = factor * factor
    for oy in range(oh):
        base = oy * factor
        row_out = oy * ow * 3
        for ox in range(ow):
            r = g = b = 0
            for dy in range(factor):
                o = (base + dy) * stride + ox * factor * 3
                for _ in range(factor):
                    r += rgb[o]
                    g += rgb[o + 1]
                    b += rgb[o + 2]
                    o += 3
            i = row_out + ox * 3
            out[i] = r // n
            out[i + 1] = g // n
            out[i + 2] = b // n
    return ow, oh, bytes(out)
