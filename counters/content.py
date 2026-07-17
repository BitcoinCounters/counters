"""Deterministic content derivation — build reference v3 §5.

Counterparty's API returns `description` as a string whose encoding follows
Core's consensus helper `bytes_to_content` (counterpartycore lib/utils/
helpers.py): textual MIME types are returned as the UTF-8 text itself, binary
MIME types as the hex encoding of the stored bytes. This module inverts that
rule byte-for-byte — including the `extended_mime_types_support` height gate —
so content hashes are identical across indexers and match what Counterparty
consensus stores.

Nothing here ever gates validity (R5): MIME handling is derivation and display
metadata only.
"""

from __future__ import annotations

import binascii
import re

from .config import EXTENDED_MIME_GATE

# Counterparty's fixed textual application/* list (helpers.py
# TEXTUAL_APPLICATION_MIME_TYPES, verbatim). Post-gate classification also
# accepts *+json; pre-gate uses the shorter explicit list below.
TEXTUAL_APPLICATION_MIME_TYPES = frozenset(
    [
        "application/xml",
        "application/javascript",
        "application/ecmascript",
        "application/x-javascript",
        "application/json",
        "application/manifest+json",
        "application/x-python-code",
        "application/x-sh",
        "application/x-csh",
        "application/x-tex",
        "application/x-latex",
        "application/postscript",
        "application/yaml",
        "application/x-yaml",
        "application/sql",
    ]
)

# The pre-gate classifier's explicit textual application/* list (helpers.py
# classify_mime_type, legacy branch). Shorter than the post-gate set — e.g.
# application/yaml classified as binary before block 952,800.
_PRE_GATE_TEXTUAL_APPLICATION = frozenset(
    [
        "application/xml",
        "application/javascript",
        "application/json",
        "application/manifest+json",
        "application/x-python-code",
        "application/x-sh",
        "application/x-csh",
        "application/x-tex",
        "application/x-latex",
    ]
)


def strip_mime_parameters(mime_type: str) -> str:
    """`audio/ogg;codecs=opus` -> `audio/ogg`."""
    if not isinstance(mime_type, str):
        return ""
    return mime_type.split(";")[0].strip()


def classify_mime_type(mime_type: str, block_index: int) -> str:
    """'text' or 'binary', exactly as Counterparty consensus classifies it at
    this height (helpers.classify_mime_type)."""
    if block_index >= EXTENDED_MIME_GATE:
        if not isinstance(mime_type, str):
            return "binary"
        target = strip_mime_parameters(mime_type)
        if (
            target.startswith("text/")
            or target.startswith("message/")
            or target.endswith("+xml")
            or target.endswith("+json")
        ):
            return "text"
        if target in TEXTUAL_APPLICATION_MIME_TYPES:
            return "text"
        return "binary"

    # Pre-gate (blocks 902,000–952,799): no parameter stripping, no +json rule.
    if (
        mime_type.startswith("text/")
        or mime_type.startswith("message/")
        or mime_type.endswith("+xml")
    ):
        return "text"
    if mime_type in _PRE_GATE_TEXTUAL_APPLICATION:
        return "text"
    return "binary"


def content_bytes(description: str, mime_type: str, block_index: int) -> tuple[bytes, bool]:
    """The canonical content bytes of an event (build ref v3 §5.1).

    Inverts Core's bytes_to_content: UTF-8 for textual types, unhexlify for
    binary. Returns (bytes, clean): `clean` is False on the defensive fallback
    where a claimed-binary description is not valid hex (should be unreachable
    for valid consensus state) and the UTF-8 bytes of the string are used.
    """
    mime = mime_type or "text/plain"
    if classify_mime_type(mime, block_index) == "text":
        return description.encode("utf-8"), True
    try:
        return binascii.unhexlify(description), True
    except (binascii.Error, ValueError):
        return description.encode("utf-8"), False


# Very light MIME well-formedness check for DISPLAY normalization only (R5:
# never a validity condition). Counterparty already consensus-validates
# mime_type against a fixed allow-list, so this is defense in depth.
_MIME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+/[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


def normalize_mime(mime_type: str | None) -> tuple[str, str | None]:
    """(display content_type, raw-or-None).

    Parameters are stripped for display; an unparseable type normalizes to
    application/octet-stream. The verbatim original is returned as `raw` only
    when it differs from the normalized form.
    """
    raw = mime_type if mime_type is not None else None
    base = strip_mime_parameters(mime_type or "") or "text/plain"
    if not _MIME_RE.match(base):
        base = "application/octet-stream"
    return base, (raw if raw is not None and raw != base else None)


# Pointer-like content (build ref v3 §5.3): a single URI-ish token. Display
# metadata only — never affects validity or numbering.
_POINTER_RE = re.compile(r"^(?:ipfs:(?://)?|ar://|https?://)\S+$", re.IGNORECASE)


def is_pointer_like(content: bytes, textual: bool) -> bool:
    if not textual:
        return False
    try:
        text = content.decode("utf-8").strip()
    except UnicodeDecodeError:
        return False
    return bool(_POINTER_RE.match(text)) and len(text.split()) == 1


# Stamp-like content (build ref v3 §5.4): a Bitcoin Stamps payload —
# `STAMP:<base64 image>` in a textual description. Like §5.3 this is display
# metadata only; it never affects validity, numbering, content bytes, or the
# rolling hash. Decoding mirrors stamps indexers: case-insensitive prefix,
# whitespace-tolerant base64 (mints in the wild carry stray spaces), and the
# decoded bytes must carry a known image magic.
_STAMP_PREFIX = "stamp:"
_BASE64_JUNK_RE = re.compile(r"\s+")

# (magic prefix, mime). WebP is RIFF-framed and checked separately.
_IMAGE_MAGICS = (
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
)


def sniff_image(data: bytes) -> str | None:
    for magic, mime in _IMAGE_MAGICS:
        if data.startswith(magic):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def stamp_image(content: bytes, textual: bool) -> tuple[bytes, str] | None:
    """Decode a stamp-like payload to `(image bytes, sniffed mime)`, or None.

    None means "display as-is": not textual, no STAMP: prefix, undecodable
    base64, or decoded bytes that are not a recognized image (e.g. counter #59
    XCPFTW, whose base64 decodes to a mangled PNG)."""
    if not textual:
        return None
    try:
        text = content.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    if not text[: len(_STAMP_PREFIX)].lower() == _STAMP_PREFIX:
        return None
    b64 = _BASE64_JUNK_RE.sub("", text[len(_STAMP_PREFIX):])
    b64 += "=" * (-len(b64) % 4)
    try:
        raw = binascii.a2b_base64(b64.encode("ascii"))
    except (binascii.Error, ValueError, UnicodeEncodeError):
        return None
    mime = sniff_image(raw)
    if mime is None:
        return None
    return raw, mime
