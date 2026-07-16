"""Content derivation — build ref v3 §5 (mirrors Counterparty's consensus
classifier byte-for-byte, including the extended_mime_types_support gate).

Zero-dependency runner: python tests/test_content.py   (or via pytest)
"""

from __future__ import annotations

import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counters2.config import EXTENDED_MIME_GATE, GENESIS_HEIGHT  # noqa: E402
from counters2.content import (  # noqa: E402
    classify_mime_type,
    content_bytes,
    is_pointer_like,
    normalize_mime,
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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
