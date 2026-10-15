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

import base64
import binascii
import json
import re

from .config import EXTENDED_MIME_GATE
from .ids import parse_id

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


# Delegate-like content (build ref v3 §5.5): the body names another
# counter's event, and a server renders that counter's content in its place
# (display rule 9 — resolved inside the index only, one hop, never fetched).
# Display metadata only: it never affects validity, numbering, content
# bytes, or the rolling hash. Three shapes, all strict (no repair):
#
#   1. bare:    <txid>i<msg_index>                    — the inscription id alone
#   2. tagged:  DELEGATE:<txid>i<msg_index>           — case-insensitive prefix
#   3. json:    {"delegate": "<txid>i<msg_index>", …} — a JSON object; every
#               other member is the counter's own uninterpreted metadata
#
# A body over DELEGATE_MAX_BYTES is never a delegate, so every server parses
# (or refuses) identically without reading unbounded JSON.
#
# A reference may carry a DISPLAY FRAGMENT — `<id>#edition-69` — appended to
# the target's document URL at render time, so a target that styles itself by
# `:target` (an SVG edition selector) shows the named variant. In the JSON
# form a missing fragment falls back to the fragment of an `image` string
# member (the ordinals-marketplace convention), then to an `edition` integer
# member as `edition-<n>`. RFC 3986 fragment characters
# minus quotes and percent-escapes, so the token drops into a URL and an HTML
# attribute untouched; a malformed fragment on the reference itself makes the
# body not a delegate (strict, no repair), while a malformed `image` fragment
# is ignored (foreign metadata, best-effort).
_DELEGATE_PREFIX = "delegate:"
DELEGATE_MAX_BYTES = 65536
_FRAGMENT_RE = re.compile(r"^[A-Za-z0-9!$&()*+,\-./:;=?@_~]{1,255}$")


def split_fragment(token: str) -> tuple[str, str | None] | None:
    """(reference, fragment) — fragment None when absent; None entirely when
    a fragment is present but malformed."""
    base, sep, frag = token.partition("#")
    if not sep:
        return token, None
    if not _FRAGMENT_RE.match(frag):
        return None
    return base.strip(), frag


def delegate_ref(content: bytes, textual: bool) -> tuple[str, int, str | None] | None:
    """(txid, msg_index, display fragment or None) named by a delegate-like
    body, else None. Resolution (is that event an indexed counter?) is the
    caller's business — this only recognises the shape."""
    if not textual or len(content) > DELEGATE_MAX_BYTES:
        return None
    try:
        text = content.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    if text[:1] == "{":
        try:
            obj = json.loads(text)
        except ValueError:
            return None
        target = obj.get("delegate") if isinstance(obj, dict) else None
        if not isinstance(target, str):
            return None
        split = split_fragment(target.strip())
        if split is None:
            return None
        base, frag = split
        event = parse_id(base)
        if event is None:
            return None
        if frag is None:
            image = obj.get("image")
            if isinstance(image, str) and "#" in image:
                cand = image.partition("#")[2]
                if _FRAGMENT_RE.match(cand):
                    frag = cand
        if frag is None:
            # `edition` member: `"edition": 69` → `edition-69`, the anchor
            # naming of :target-styled edition SVGs. A positive integer only
            # (bool is an int in Python — excluded), bounded so the fragment
            # regex's length cap can never trip.
            edition = obj.get("edition")
            if (isinstance(edition, int) and not isinstance(edition, bool)
                    and 0 < edition <= 10**9):
                frag = f"edition-{edition}"
        return (*event, frag)
    if text[:len(_DELEGATE_PREFIX)].lower() == _DELEGATE_PREFIX:
        text = text[len(_DELEGATE_PREFIX):]
    split = split_fragment(text.strip())
    if split is None:
        return None
    base, frag = split
    event = parse_id(base)
    return None if event is None else (*event, frag)


def delegate_event(content: bytes, textual: bool) -> tuple[str, int] | None:
    """(txid, msg_index) alone — see delegate_ref."""
    ref = delegate_ref(content, textual)
    return None if ref is None else ref[:2]


# An ICON (build ref v3 §5.6): a small picture an asset-info JSON body names
# for places too small for the file itself — a row in an order book, a badge
# by a name. XChain's `images` extension carries it as the entry whose `type`
# is "icon"; its `data` names an ordinals inscription, either bare
# (`<txid>i<n>`) or as an ordinals.com content URL, which is the shape in the
# wild (every RARE.PEPE edition). Only the inscription id is taken from the
# URL — the bytes are read from the chain through this node (ordenv.py), so
# the host in the URL is never contacted. A hosted picture of any other shape
# is a pointer and yields no icon. Shape only, like delegate_ref; the same
# body cap applies.
_ORDINALS_CONTENT_RE = re.compile(
    r"^https?://(?:www\.)?ordinals\.com/(?:content|r/undelegated-content)/"
    r"([0-9a-fA-F]{64})i(\d+)(?:[?#].*)?$")


def icon_ref(content: bytes, textual: bool) -> tuple[str, int] | None:
    """(txid, index) of the ordinals inscription an asset-info JSON body names
    as its icon, else None."""
    if not textual or len(content) > DELEGATE_MAX_BYTES:
        return None
    try:
        text = content.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    if text[:1] != "{":
        return None
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    images = obj.get("images") if isinstance(obj, dict) else None
    if not isinstance(images, list):
        return None
    for entry in images:
        if not isinstance(entry, dict) or entry.get("type") != "icon":
            continue
        data = entry.get("data")
        if not isinstance(data, str):
            return None
        data = data.strip()
        m = _ORDINALS_CONTENT_RE.match(data)
        if m:
            return m.group(1).lower(), int(m.group(2))
        return parse_id(data)      # bare id, or None for anything else
    return None


# Stamp-like content (build ref v3 §5.4): a Bitcoin Stamps payload —
# `STAMP:<base64 image>` in a textual description. Like §5.3 this is display
# metadata only; it never affects validity, numbering, content bytes, or the
# rolling hash. Decoding mirrors stamps indexers: case-insensitive prefix,
# whitespace-tolerant base64 (mints in the wild carry stray spaces), and the
# decoded bytes must carry a known image magic.
_STAMP_PREFIX = "stamp:"

# Strict base64: alphabet only, padding (if any) at the very end. No
# whitespace, no stray characters (rule 3 — no repair of damaged data).
_STRICT_BASE64_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")

# (magic prefix, mime). WebP is RIFF-framed and checked separately.
_IMAGE_MAGICS = (
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
)

# Non-image container signatures for display sniffing (rule 2). Deliberately
# tiny and fixed so any server reproduces the same choice. Ogg (Opus/Vorbis)
# is shown with a native <audio> element; a signature never resolves to a
# textual or executable type, so it is always safe to trust over the declared
# MIME.
_MEDIA_MAGICS = ((b"OggS", "audio/ogg"),)


def sniff_image(data: bytes) -> str | None:
    for magic, mime in _IMAGE_MAGICS:
        if data.startswith(magic):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def sniff_media(data: bytes) -> str | None:
    """A recognized image/audio/video signature for `data`, else None
    (rule 2). Superset of `sniff_image`: a match wins over the declared
    `mime_type` when choosing how to render, and is never a textual type."""
    image = sniff_image(data)
    if image is not None:
        return image
    for magic, mime in _MEDIA_MAGICS:
        if data.startswith(magic):
            return mime
    return None


def stamp_image(content: bytes, textual: bool) -> tuple[bytes, str] | None:
    """Decode a stamp-like payload to `(image bytes, sniffed mime)`, or None.

    None means "display as-is" (rule 3): not textual, no STAMP: prefix, base64
    that is not strictly well-formed (e.g. #54 MAGICEGG's stray space, #59
    XCPFTW's stray prefix), or decoded bytes that are not a recognized image.
    Damaged data is never repaired — it falls back to text."""
    if not textual:
        return None
    try:
        text = content.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    if not text[: len(_STAMP_PREFIX)].lower() == _STAMP_PREFIX:
        return None
    b64 = text[len(_STAMP_PREFIX):]
    if not _STRICT_BASE64_RE.match(b64):
        return None
    try:
        raw = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        return None
    mime = sniff_image(raw)
    if mime is None:
        return None
    return raw, mime


# Asset-info-like content (build ref v3 §5.4, second shape): Counterparty's
# Enhanced Asset Info JSON — normally hosted at a URL the description points
# to — carried *in* the description as a `data:application/json,` URI, with
# the picture itself a `data:image/*;base64,` URI inside it (the `image`
# field, or an entry of XChain's `images` array extension). First seen on
# counter #560 PEPEMEMECOIN.S1N7. Like `STAMP:` this is display metadata
# only, and decode-or-text under the same strictness: the base64 must be
# clean and the bytes must carry a known image magic. Hosted URLs in the
# same JSON are references, never fetched (rule 4).
_JSON_DATA_URI_RE = re.compile(
    r"^data:application/json(?:;charset=utf-8)?,", re.IGNORECASE)
_IMAGE_DATA_URI_RE = re.compile(r"^data:image/[\w.+-]+;base64,", re.IGNORECASE)

# Decoding a JSON wrapper this large is cheap; the point of the cap is a
# bound on serve-time work, not a judgement about the content.
DECODE_MAX_BYTES = 4 * 1024 * 1024


def _image_data_uris(info: dict) -> list[str]:
    """Candidate inline-image fields in spec order: `image`, then each
    `images[].data` (XChain extension). Anything that is not a string
    starting `data:image/...;base64,` is skipped — a hosted URL is a pointer."""
    out: list[str] = []
    image = info.get("image")
    if isinstance(image, str):
        out.append(image)
    images = info.get("images")
    if isinstance(images, list):
        for entry in images:
            if isinstance(entry, dict) and isinstance(entry.get("data"), str):
                out.append(entry["data"])
    return [u for u in out if _IMAGE_DATA_URI_RE.match(u)]


def asset_info_image(content: bytes, textual: bool) -> tuple[bytes, str] | None:
    """Decode an inline-asset-info payload to `(image bytes, sniffed mime)`,
    or None (display as text): not textual, not a `data:application/json,`
    URI, JSON that does not parse to an object, no inline image field, base64
    that is not strictly well-formed, or bytes that are not a recognized
    image. The first candidate field decides — no fallthrough to a later
    field after a damaged one, and no repair."""
    if not textual or len(content) > DECODE_MAX_BYTES:
        return None
    try:
        text = content.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    m = _JSON_DATA_URI_RE.match(text)
    if m is None:
        return None
    try:
        info = json.loads(text[m.end():])
    except ValueError:
        return None
    if not isinstance(info, dict):
        return None
    candidates = _image_data_uris(info)
    if not candidates:
        return None
    b64 = candidates[0].split(",", 1)[1]
    if not _STRICT_BASE64_RE.match(b64):
        return None
    try:
        raw = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        return None
    mime = sniff_image(raw)
    if mime is None:
        return None
    return raw, mime


def decoded_image(content: bytes, textual: bool) -> tuple[bytes, str] | None:
    """The image a textual counter *encodes*, if any: a `STAMP:<base64>`
    payload or an inline-asset-info JSON (§5.4). None means display the text
    as-is. This is what `stamp_mime` and `/stamp/<n>` report — "stamp" is the
    name of the channel, after the shape that got there first."""
    return stamp_image(content, textual) or asset_info_image(content, textual)
