"""Content derivation — build ref v3 §5 (mirrors Counterparty's consensus
classifier byte-for-byte, including the extended_mime_types_support gate).

Zero-dependency runner: python tests/test_content.py   (or via pytest)
"""

from __future__ import annotations

import json

import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counters.config import EXTENDED_MIME_GATE, GENESIS_HEIGHT  # noqa: E402
from counters.content import (  # noqa: E402
    classify_mime_type,
    content_bytes,
    is_pointer_like,
    normalize_mime,
    sniff_image,
    sniff_media,
    stamp_image,
    asset_info_image,
    decoded_image,
)

PRE = GENESIS_HEIGHT          # 902000: before the extended-MIME gate
POST = EXTENDED_MIME_GATE     # 952800: at/after the gate


def test_classify_stable_types_both_gates():
    for h in (PRE, POST):
        assert classify_mime_type("text/plain", h) == "text"
        assert classify_mime_type("text/html", h) == "text"
        assert classify_mime_type("application/json", h) == "text"
        assert classify_mime_type("image/svg+xml", h) == "text"   # *+xml
        assert classify_mime_type("image/gif", h) == "binary"
        assert classify_mime_type("audio/opus", h) == "binary"
        assert classify_mime_type("application/octet-stream", h) == "binary"


def test_classify_gate_differences():
    # +json structured suffix: textual only after the gate
    assert classify_mime_type("application/ld+json", PRE) == "binary"
    assert classify_mime_type("application/ld+json", POST) == "text"
    # application/yaml: only in the post-gate textual application list
    assert classify_mime_type("application/yaml", PRE) == "binary"
    assert classify_mime_type("application/yaml", POST) == "text"
    # MIME parameters are stripped only after the gate; either way the base
    # type here is binary
    assert classify_mime_type("audio/ogg;codecs=opus", POST) == "binary"
    # but a parameterised TEXTUAL type is only recognised post-gate
    assert classify_mime_type("text/plain;charset=utf-8", POST) == "text"


def test_content_bytes_text_and_binary():
    body, clean = content_bytes("testdual", "text/plain", GENESIS_HEIGHT)
    assert body == b"testdual" and clean
    gif = bytes.fromhex("474946383761")  # "GIF87a"
    body, clean = content_bytes("474946383761", "image/gif", GENESIS_HEIGHT)
    assert body == gif and clean
    # sha256 is over the DECODED bytes, never the API string
    assert hashlib.sha256(body).hexdigest() != hashlib.sha256(b"474946383761").hexdigest()


def test_content_bytes_bad_hex_falls_back():
    body, clean = content_bytes("not-hex!", "image/gif", GENESIS_HEIGHT)
    assert body == b"not-hex!" and not clean


def test_normalize_mime():
    assert normalize_mime("text/plain") == ("text/plain", None)
    # parameters stripped for display, verbatim kept as raw
    ct, raw = normalize_mime("audio/ogg;codecs=opus")
    assert ct == "audio/ogg" and raw == "audio/ogg;codecs=opus"
    # unparseable -> octet-stream, raw kept (R5: display only, never validity)
    ct, raw = normalize_mime("garbage")
    assert ct == "application/octet-stream" and raw == "garbage"
    # absent -> text/plain default
    assert normalize_mime(None) == ("text/plain", None)


def test_is_pointer_like():
    assert is_pointer_like(b"ipfs:bafkreihpndr5w57vznshu3rsgbic6wsldusktyi56cg6esyfvykbub5yhe", True)
    assert is_pointer_like(b"ipfs://bafybeigdyrzt5s", True)
    assert is_pointer_like(b"https://example.com/x.png", True)
    assert is_pointer_like(b"ar://abc123", True)
    assert not is_pointer_like(b"just some text", True)
    assert not is_pointer_like(b"ipfs: two tokens", True)
    assert not is_pointer_like(b"\xff\xfe", True)      # not UTF-8
    assert not is_pointer_like(b"https://x", False)    # binary content never flagged


# A real 1x1 transparent GIF89a, as minted stamps carry (magic + trailer).
_GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04"
    b"\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D"
    b"\x01\x00;"
)


def test_sniff_image():
    assert sniff_image(_GIF) == "image/gif"
    assert sniff_image(b"\x89PNG\r\n\x1a\n" + b"\0" * 8) == "image/png"
    assert sniff_image(b"\xff\xd8\xff\xe0rest") == "image/jpeg"
    assert sniff_image(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert sniff_image(b"\x89PNF corrupted") is None      # counter #59 XCPFTW
    assert sniff_image(b"") is None


def test_sniff_media():
    # superset of sniff_image: images still match
    assert sniff_media(_GIF) == "image/gif"
    assert sniff_media(b"\x89PNG\r\n\x1a\n" + b"\0" * 8) == "image/png"
    # Ogg container (Opus/Vorbis) -> audio (counters #45/#51, minted as
    # application/octet-stream / image/jpeg but actually Ogg Opus)
    assert sniff_media(b"OggS\x00\x02" + b"\0" * 20) == "audio/ogg"
    # no signature -> None, so the declared type is used
    assert sniff_media(b"just some text") is None
    assert sniff_media(b"") is None


def test_stamp_image():
    import base64

    b64 = base64.b64encode(_GIF).decode()
    assert stamp_image(f"STAMP:{b64}".encode(), True) == (_GIF, "image/gif")
    # prefix is case-insensitive; whitespace around the whole body tolerated
    assert stamp_image(f"stamp:{b64}\n".encode(), True) == (_GIF, "image/gif")


def test_stamp_image_rejects():
    import base64

    b64 = base64.b64encode(_GIF).decode()
    assert stamp_image(f"STAMP:{b64}".encode(), False) is None   # binary content
    assert stamp_image(b64.encode(), True) is None               # no prefix
    assert stamp_image(b"STAMP:!!!not-base64!!!", True) is None  # undecodable
    # Rule 3 (strict, no repair): whitespace INSIDE the base64 -> text, not a
    # repaired image (counter #54 MAGICEGG's stray space).
    spaced = b64[:10] + " " + b64[10:]
    assert stamp_image(f"STAMP:{spaced}".encode(), True) is None
    # missing/damaged padding is not reconstructed -> text
    assert stamp_image(f"STAMP:{b64.rstrip('=')}".encode(), True) is None
    # decodes fine but not a recognized image -> display as text (§5.4)
    text_b64 = base64.b64encode(b"hello world, not an image").decode()
    assert stamp_image(f"STAMP:{text_b64}".encode(), True) is None
    assert stamp_image(b"STAMP:\xff\xfe", True) is None          # not UTF-8


# --- inline asset-info JSON (§5.4, second shape; counter #560) --------------

def _asset_info(image: object = None, images: object = None, **extra) -> bytes:
    info: dict = {"asset": "PEPEMEMECOIN.S1N7", "title": "21 A Kek Odyssey"}
    if image is not None:
        info["image"] = image
    if images is not None:
        info["images"] = images
    info.update(extra)
    return b"data:application/json," + json.dumps(info).encode()


def test_asset_info_image():
    import base64

    b64 = base64.b64encode(_GIF).decode()
    uri = f"data:image/gif;base64,{b64}"
    assert asset_info_image(_asset_info(image=uri), True) == (_GIF, "image/gif")
    # the picture's own bytes decide the type, not the URI's label
    assert asset_info_image(_asset_info(image=f"data:image/webp;base64,{b64}"), True) == (_GIF, "image/gif")
    # scheme and mime are case-insensitive; a charset parameter is allowed;
    # whitespace around the whole body is tolerated (as for STAMP:)
    body = b"DATA:APPLICATION/JSON;charset=utf-8," + json.dumps({"asset": "X", "image": uri}).encode()
    assert asset_info_image(body + b"\n", True) == (_GIF, "image/gif")
    # #560's real shape: `image` inline, `images` entries hosted (ignored)
    hosted = [{"type": "icon", "size": "48x48", "data": "https://x.ar.io/icon.png"},
              {"type": "high_def", "data": "https://x.ar.io/hd.png"}]
    assert asset_info_image(_asset_info(image=uri, images=hosted), True) == (_GIF, "image/gif")
    # `image` hosted, `images[].data` inline: the array entry is used
    assert asset_info_image(
        _asset_info(image="https://xcp.fun/icon.png",
                    images=[{"type": "icon", "data": "https://x/i.png"},
                            {"type": "standard", "data": uri}]),
        True) == (_GIF, "image/gif")
    # the pretty-printed JSON with \r\n and spaces that #560 actually carries
    body = (b'data:application/json,{"asset":"PEPEMEMECOIN.S1N7", "title":"21 A Kek Odyssey",\r\n'
            b'"artist":"Memeticx",  "image":"' + uri.encode() + b'"}')
    assert asset_info_image(body, True) == (_GIF, "image/gif")
    # decoded_image is the union: STAMP: first, then asset info
    assert decoded_image(f"STAMP:{b64}".encode(), True) == (_GIF, "image/gif")
    assert decoded_image(_asset_info(image=uri), True) == (_GIF, "image/gif")
    assert decoded_image(b"just text", True) is None


def test_asset_info_image_rejects():
    import base64

    b64 = base64.b64encode(_GIF).decode()
    uri = f"data:image/gif;base64,{b64}"
    assert asset_info_image(_asset_info(image=uri), False) is None       # binary content
    assert asset_info_image(b"STAMP:" + b64.encode(), True) is None       # not this shape
    assert asset_info_image(json.dumps({"image": uri}).encode(), True) is None  # bare JSON, no data: wrapper
    # a hosted picture is a pointer (rule 4): never fetched, shown as text
    assert asset_info_image(_asset_info(image="https://xcp.fun/full/X.png"), True) is None
    assert asset_info_image(_asset_info(images=[{"type": "standard", "data": "ipfs://Qm"}]), True) is None
    # no picture at all
    assert asset_info_image(_asset_info(), True) is None
    # base64 without the data: prefix is not a URI we recognize (no guessing)
    assert asset_info_image(_asset_info(image=b64), True) is None
    # a data: image that is not base64-encoded
    assert asset_info_image(_asset_info(image="data:image/svg+xml,<svg/>"), True) is None
    # JSON that is not an object, or does not parse
    assert asset_info_image(b"data:application/json,[1,2]", True) is None
    assert asset_info_image(b"data:application/json,{not json", True) is None
    # other data: mimes are not asset info
    assert asset_info_image(b"data:text/plain,hello", True) is None
    assert asset_info_image(b"data:application/json;base64," + base64.b64encode(b"{}"), True) is None
    # Rule 3 strictness carries over: damaged base64 is text, not repaired
    spaced = b64[:10] + " " + b64[10:]
    assert asset_info_image(_asset_info(image=f"data:image/gif;base64,{spaced}"), True) is None
    assert asset_info_image(_asset_info(image=f"data:image/gif;base64,{b64.rstrip('=')}"), True) is None
    # the first candidate decides: a damaged `image` is not rescued by `images`
    assert asset_info_image(
        _asset_info(image=f"data:image/gif;base64,{spaced}",
                    images=[{"type": "standard", "data": uri}]), True) is None
    # decodes, but not a recognized image -> text
    text_b64 = base64.b64encode(b"hello world, not an image").decode()
    assert asset_info_image(_asset_info(image=f"data:image/gif;base64,{text_b64}"), True) is None
    assert asset_info_image(b"data:application/json,\xff\xfe", True) is None  # not UTF-8


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")


# --- delegate-like content (§5.5) -------------------------------------------

def test_delegate_event_accepts_all_three_forms():
    from counters.content import delegate_event
    t = "ab" * 32 + "i0"
    ev = ("ab" * 32, 0)
    assert delegate_event(t.encode(), True) == ev                       # bare
    assert delegate_event(f"  {t}\n".encode(), True) == ev
    assert delegate_event(f"DELEGATE:{t}".encode(), True) == ev         # tagged
    assert delegate_event(f"Delegate: {t}".encode(), True) == ev        # case/ws
    assert delegate_event(("AB" * 32 + "i0").encode(), True) == ev      # normalised
    body = json.dumps({"delegate": t, "name": "Ed 3",
                       "traits": {"eyes": "laser"}}).encode()
    assert delegate_event(body, True) == ev                             # json
    assert delegate_event(json.dumps({"delegate": "cd" * 32 + "i7"}).encode(),
                          True) == ("cd" * 32, 7)


def test_delegate_event_refuses_everything_else():
    from counters.content import DELEGATE_MAX_BYTES, delegate_event
    t = "ab" * 32 + "i0"
    bad = [
        ("ab" * 32).encode(),                          # bare txid: no index
        b"42",                                         # a number is not on chain
        f'"{t}"'.encode(),                             # top-level JSON string
        json.dumps([t]).encode(),                      # array
        json.dumps({"delegate": 42}).encode(),         # non-string member
        json.dumps({"meta": {"delegate": t}}).encode(),  # nested only
        b"DELEGATE:" + ("ab" * 32).encode(),           # tagged bare txid
        b"{delegate:" + t.encode() + b"}",             # not JSON
        b"\xff\xfe",                                   # not UTF-8
    ]
    for body in bad:
        assert delegate_event(body, True) is None, body
    assert delegate_event(t.encode(), False) is None   # binary MIME never
    huge = b"{" + b" " * DELEGATE_MAX_BYTES + b"}"
    assert delegate_event(huge, True) is None          # over the parse cap


def test_delegate_display_fragment():
    from counters.content import delegate_ref
    t = "ab" * 32 + "i0"
    ev = ("ab" * 32, 0)
    # on the reference itself, in every shape
    assert delegate_ref(f"{t}#edition-69".encode(), True) == (*ev, "edition-69")
    assert delegate_ref(f"DELEGATE:{t}#edition-69".encode(), True) == (*ev, "edition-69")
    assert delegate_ref(json.dumps({"delegate": f"{t}#edition-69"}).encode(),
                        True) == (*ev, "edition-69")
    # absent → None; JSON inherits the `image` member's fragment
    assert delegate_ref(t.encode(), True) == (*ev, None)
    body = json.dumps({"delegate": t,
                       "image": f"https://ordinals.com/content/{t}#edition-69"})
    assert delegate_ref(body.encode(), True) == (*ev, "edition-69")
    # the reference's own fragment wins over image's
    body = json.dumps({"delegate": f"{t}#edition-1",
                       "image": f"https://x.example/{t}#edition-2"})
    assert delegate_ref(body.encode(), True) == (*ev, "edition-1")
    # malformed on the reference: not a delegate at all (strict, no repair)
    assert delegate_ref(f'{t}#bad"frag'.encode(), True) is None
    assert delegate_ref(f"{t}#".encode(), True) is None
    assert delegate_ref((t + "#" + "x" * 256).encode(), True) is None
    # malformed on image: ignored — foreign metadata, best-effort
    body = json.dumps({"delegate": t, "image": 'https://x.example/f#bad"frag'})
    assert delegate_ref(body.encode(), True) == (*ev, None)
    # non-string image: ignored
    body = json.dumps({"delegate": t, "image": ["https://x.example/f#e-1"]})
    assert delegate_ref(body.encode(), True) == (*ev, None)
    # `edition` member: a positive integer becomes edition-<n>
    body = json.dumps({"delegate": t, "edition": 69})
    assert delegate_ref(body.encode(), True) == (*ev, "edition-69")
    # ...but only as the last fallback: image's fragment wins over it
    body = json.dumps({"delegate": t, "edition": 69,
                       "image": f"https://x.example/{t}#edition-2"})
    assert delegate_ref(body.encode(), True) == (*ev, "edition-2")
    # non-conforming editions are ignored, never repaired
    for edition in (0, -1, 69.0, "69", True, 10**9 + 1, None):
        body = json.dumps({"delegate": t, "edition": edition})
        assert delegate_ref(body.encode(), True) == (*ev, None), edition
