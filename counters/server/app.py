"""Read-only HTTP server for the Bitcoin Counters explorer.

Serves two things from one origin:

  1. The bundled single-page explorer (the static/ directory: index.html + logos).
  2. A small JSON API backed by the index Store:

       GET /status                      -> {"indexed": H, "count": N, "genesis": 0,
                                            "version": V, "commit": SHA, "updated": ISO}
       GET /counters?before=N&limit=K   -> {"counters": [record, ...]}  newest-first
       GET /counter/<number|asset>      -> a single record (404 if unknown)
       GET /block/<height>              -> {"block": H, "count": K, "counters": [...]}
       GET /content/<number>            -> the raw file bytes, with its stored MIME
       GET /social/<number>.png         -> the `og:image` for a shared link

A "record" is the index row reshaped to the field names the frontend expects
(number, asset, asset_id, content_type, size, body, owner, txid, block,
position, sha256). Textual content (small text/*, JSON, SVG) is inlined as
`body`; everything else is fetched lazily from /content/<number>.

The server is built on stdlib http.server, with no web framework. Each request
opens its own SQLite connection because ThreadingHTTPServer handles requests on
worker threads and SQLite connections are not shareable across threads.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import sqlite3
import sys
import threading
import zlib
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import card, picture, preview
from .. import __version__
from ..bitcoind import BitcoindClient
from ..config import Config
from ..ids import format_id
from ..content import classify_mime_type, delegate_event, sniff_media, stamp_image
from ..counterparty import CounterpartyClient, CounterpartyError
from ..reveal import envelope_style
from ..store import Store

log = logging.getLogger("counters")

GIT_DIR = "/app/.git"


def _read_git_head(git_dir: str = GIT_DIR) -> tuple[str, Path] | None:
    """Resolve HEAD by reading a git dir directly (no git binary), returning
    (full sha, the file that named it). Used when the repo's .git is
    bind-mounted into the container, so a plain `docker compose up -d` shows
    the real revision without a build arg. The source file matters for
    `_resolve_updated`: its mtime is when the deploy last moved the ref."""
    head_path = Path(git_dir, "HEAD")
    try:
        head = head_path.read_text().strip()
    except OSError:
        return None
    if not head.startswith("ref:"):
        return (head, head_path) if head else None  # detached HEAD holds the sha
    ref = head[4:].strip()  # e.g. "refs/heads/main"
    ref_path = Path(git_dir, ref)
    try:  # loose ref
        sha = ref_path.read_text().strip()
        if sha:
            return sha, ref_path
    except OSError:
        pass
    packed = Path(git_dir, "packed-refs")
    try:  # packed-refs fallback
        for line in packed.read_text().splitlines():
            if line and not line.startswith(("#", "^")):
                sha, _, name = line.partition(" ")
                if name.strip() == ref:
                    return sha.strip(), packed
    except OSError:
        pass
    return None


def _commit_time(sha: str, git_dir: str = GIT_DIR) -> int | None:
    """Committer timestamp (epoch) of a loose commit object, or None. Objects
    that arrived by fetch live in packfiles, which we don't parse — only
    locally-created commits are reliably loose."""
    try:
        raw = zlib.decompress(Path(git_dir, "objects", sha[:2], sha[2:]).read_bytes())
    except (OSError, zlib.error):
        return None
    header, _, body = raw.partition(b"\0")
    if not header.startswith(b"commit "):
        return None
    for line in body.split(b"\n"):
        if not line:  # end of headers; committer line not found
            return None
        if line.startswith(b"committer "):
            try:  # b"committer Name <email> 1753594828 +0000" — epoch is UTC
                return int(line.rsplit(b">", 1)[1].split()[0])
            except (IndexError, ValueError):
                return None
    return None


def _resolve_commit() -> str:
    # A real build-time stamp (CI's Dockerfile GIT_COMMIT arg) wins; otherwise
    # read a bind-mounted .git at runtime; finally fall back to "dev".
    env = os.environ.get("COUNTER_GIT_COMMIT")
    if env and env != "dev":
        return env
    head = _read_git_head()
    return (head[0][:7] if head else None) or env or "dev"


def _resolve_updated() -> str | None:
    """When the deployed code last changed, ISO 8601 UTC, or None if unknown.

    Best source first: a CI build stamp; then the HEAD commit's own committer
    time (loose object); then the mtime of the file that named HEAD — pulled
    objects arrive packed, but the `git pull` that moved the ref rewrote that
    file, so its mtime is the moment this deployment picked the commit up."""
    env = os.environ.get("COUNTER_GIT_COMMIT_DATE")
    if env:
        return env
    head = _read_git_head()
    if not head:
        return None
    sha, source = head
    ts = _commit_time(sha)
    if ts is None:
        try:
            ts = int(source.stat().st_mtime)
        except OSError:
            return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Deployed build revision + when it last changed, surfaced on /status and in
# the explorer footer.
GIT_COMMIT = _resolve_commit()
GIT_UPDATED = _resolve_updated()

# Headers for untrusted inscription bytes (/content and iframe-media previews),
# mirroring ord's `content_response`. Two CSP headers are sent; the browser
# enforces their intersection: the first confines sub-resources to our own
# origin (+ data:/blob:), the second additionally permits cross-server
# `/content` recursion. Scripts are allowed, but only ever inside the opaque
# origin of the `<iframe sandbox=allow-scripts>` that embeds this content.
CONTENT_HEADERS = [
    ("Content-Security-Policy", "default-src 'self' 'unsafe-eval' 'unsafe-inline' data: blob:"),
    ("Content-Security-Policy", "default-src *:*/content/ 'unsafe-eval' 'unsafe-inline' data: blob:"),
    ("X-Content-Type-Options", "nosniff"),
    # A scripted preview reads these bytes from an opaque origin (the
    # `sandbox=allow-scripts` frame), so its fetches are cross-origin and it can
    # only see the headers named here. The PDF viewer needs them to page through
    # a large inscription by byte range instead of pulling the whole file.
    # `Range` itself is CORS-safelisted, so this costs no preflight.
    ("Access-Control-Expose-Headers", "Content-Range, Accept-Ranges, Content-Length"),
]

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json",
    ".md": "text/markdown; charset=utf-8",
    # pdf.js's copies of the standard 14 fonts, fetched by the PDF preview for
    # documents that name a base font instead of embedding one — which an
    # inscription, paying by the byte, has every reason to do.
    ".pfb": "application/x-font-type1",
    ".ttf": "font/ttf",
}

# Inline only small textual blobs in JSON responses; larger or binary content
# is served on demand from /content/<number>.
BODY_MAX_BYTES = 256 * 1024

# Derived views (/preview, /stamp) are functions of the rendering rules, not of
# immutable content, so they must never be cached `immutable` (rule 7): a rules
# change has to take effect promptly. /content stays immutable (content-addressed).
DERIVED_MAX_AGE = 300
STATIC_MAX_AGE = 3600      # logos, css: temporary — they change only on deploy
INLINE_TYPES = ("text/", "application/json", "image/svg+xml")

# The social/meta block in index.html is wrapped in these markers so a counter
# page can swap in per-counter Open Graph tags without touching anything else.
_SOCIAL_RE = re.compile(r"<!-- social:start.*?social:end -->", re.DOTALL)


def _social_meta(*, title: str, description: str, url: str, image: str,
                 big_image: bool, alt: str,
                 dims: tuple[int, int] | None = None,
                 image_type: str | None = None) -> str:
    """The <title> + canonical + Open Graph + Twitter block for one counter.

    `dims` and `image_type` are emitted only when known without opening the
    image: crawlers use them to reserve layout before the fetch completes, and
    a wrong hint is worse than none.
    """
    t = html.escape(title, quote=True)
    d = html.escape(description, quote=True)
    u = html.escape(url, quote=True)
    i = html.escape(image, quote=True)
    a = html.escape(alt, quote=True)
    twitter_card = "summary_large_image" if big_image else "summary"
    extra = ""
    if dims:
        extra += (f'<meta property="og:image:width" content="{dims[0]}">\n'
                  f'<meta property="og:image:height" content="{dims[1]}">\n')
    if image_type:
        extra += f'<meta property="og:image:type" content="{image_type}">\n'
    return (
        f"<title>{html.escape(title)}</title>\n"
        f'<meta name="description" content="{d}">\n'
        f'<link rel="canonical" href="{u}">\n'
        f'<meta property="og:type" content="article">\n'
        f'<meta property="og:site_name" content="Bitcoin Counters">\n'
        f'<meta property="og:title" content="{t}">\n'
        f'<meta property="og:description" content="{d}">\n'
        f'<meta property="og:url" content="{u}">\n'
        f'<meta property="og:image" content="{i}">\n'
        + extra +
        f'<meta property="og:image:alt" content="{a}">\n'
        f'<meta name="twitter:card" content="{twitter_card}">\n'
        f'<meta name="twitter:title" content="{t}">\n'
        f'<meta name="twitter:description" content="{d}">\n'
        f'<meta name="twitter:image" content="{i}">\n'
        f'<meta name="twitter:image:alt" content="{a}">'
    )


def _display_name(row: sqlite3.Row) -> str:
    return row["asset_longname"] or row["asset"]


def _fmt_qty(raw: int, divisible) -> str:
    """Raw asset units -> human string (mirrors the card's SUPPLY format)."""
    if divisible:
        return f"{raw / 1e8:,.8f}".rstrip("0").rstrip(".")
    return f"{raw:,}"


def _supply_segment(row: sqlite3.Row) -> str:
    """' — <supply>[ · <burned> 🔥]' for the preview text, from the stored
    snapshot only (crawler paths never query the backends); empty when the
    supply has never been recorded."""
    if row["supply"] is None:
        return ""
    seg = f" — {_fmt_qty(row['supply'], row['divisible'])}"
    if row["burned"]:
        seg += f" · 🔥 {_fmt_qty(row['burned'], row['divisible'])}"
    return seg


def _inline_body(store: Store, row: sqlite3.Row) -> str | None:
    ct = row["content_type"] or ""
    if not any(ct == t or ct.startswith(t) for t in INLINE_TYPES):
        return None
    if row["content_length"] > BODY_MAX_BYTES:
        return None
    blob = store.read_blob(row["content_sha256"])
    if blob is None:
        return None
    try:
        return blob.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _live_asset(config: Config, asset: str) -> dict:
    """Live asset info per Counterparty (owner/lock/supply can change after the
    mint); empty dict if Core is unreachable so callers fall back to stored data."""
    try:
        return CounterpartyClient(config).get_asset(asset) or {}
    except CounterpartyError:
        return {}


def _asset_burned(config: Config, asset: str) -> int | None:
    """Total raw units of `asset` destroyed, per Counterparty (best-effort,
    like _live_asset; None when Core is unreachable, so the UI can tell
    "couldn't check" from a true zero)."""
    try:
        return CounterpartyClient(config).get_asset_destroyed(asset)
    except CounterpartyError:
        return None


def _block_time(config: Config, height: int) -> int | None:
    """The counter's creation time = its block's timestamp, per Counterparty
    (best-effort, like _live_asset; None if Core is unreachable)."""
    try:
        blk = CounterpartyClient(config).get_block(height)
    except CounterpartyError:
        return None
    return blk.get("block_time") if blk else None


def _stamp_payload(store: Store, row: sqlite3.Row) -> tuple[bytes, str] | None:
    """Decoded (image bytes, mime) for a stamp-like counter, else None (build
    ref v3 §5.4 — display metadata only, derived at serve time)."""
    ct = row["content_type"] or "text/plain"
    if classify_mime_type(ct, row["block_index"]) != "text":
        return None
    if row["content_length"] > BODY_MAX_BYTES:
        return None
    blob = store.read_blob(row["content_sha256"])
    if blob is None:
        return None
    return stamp_image(blob, textual=True)


def _delegate_info(store: Store, row: sqlite3.Row) -> dict | None:
    """{'id','number','content_type'} for a delegate-like counter (build ref
    v3 §5.5): the named event, resolved against the local index ONLY —
    number/content_type are None while the target is not an indexed counter.
    None for a non-delegate. Serve-time, like the stamp tag; never indexed."""
    ct = row["content_type"] or "text/plain"
    if classify_mime_type(ct, row["block_index"]) != "text":
        return None
    blob = store.read_blob(row["content_sha256"])
    if blob is None:
        return None
    event = delegate_event(blob, textual=True)
    if event is None:
        return None
    target = store.get_counter_by_event(*event)
    return {
        "id": format_id(*event),
        "number": target["number"] if target is not None else None,
        "content_type": target["content_type"] if target is not None else None,
    }


def _effective_row(store: Store, row: sqlite3.Row) -> sqlite3.Row:
    """The row display draws from: a resolved delegate's target (rule 9, ONE
    hop — a delegate's own delegate is not followed, so its token shows as
    text), else the row itself. /content/<n> is never routed through this."""
    info = _delegate_info(store, row)
    if info is None or info["number"] is None or info["number"] == row["number"]:
        return row
    return store.get_counter(info["number"]) or row


# Rendering a social image is seconds of CPU for a large PNG, and crawlers
# fire several requests at once for a freshly shared link. One lock per cache
# key means the first request renders and the rest wait for its file.
_social_locks: dict[str, threading.Lock] = {}
_social_locks_guard = threading.Lock()


def _social_lock(key: str) -> threading.Lock:
    with _social_locks_guard:
        return _social_locks.setdefault(key, threading.Lock())


def _served_type(store: Store, row: sqlite3.Row) -> str:
    """The content type /content/<n> would actually serve (rule 2: sniffed
    signature wins over the declared type), which is what a crawler will see.

    Every signature it looks for sits in the first few bytes, so this reads a
    prefix — /c/<n> is a page view and must not pull a 1.4 MB blob off disk to
    decide one meta tag.
    """
    head = store.read_blob_prefix(row["content_sha256"], 64)
    return ((sniff_media(head) if head else None)
            or row["content_type"] or "application/octet-stream")


def _is_raster(ctype: str) -> bool:
    """True for bitmap images a crawler can show as-is. SVG is excluded: it is
    a document, and no mainstream crawler renders it as an og:image."""
    ct = ctype.split(";")[0].strip().lower()
    return ct.startswith("image/") and ct != "image/svg+xml"


def _card_info(store: Store, row: sqlite3.Row) -> dict:
    """The display fields `card.render` draws, all from the local index.

    Deliberately no bitcoind or Counterparty calls: a link crawler must not be
    able to make this server go out to its backends, and the card stays
    renderable (and testable) with neither reachable. That costs the `ordinal`
    badge, which is a serve-time bitcoind lookup on the detail page.
    """
    siblings = store.get_counters_by_asset(row["asset"])
    first = siblings[0]["number"] if siblings else row["number"]
    return {
        "number": row["number"],
        "asset": _display_name(row),
        "content_type": row["content_type"],
        "size": row["content_length"],
        "block": row["block_index"],
        "owner": row["source"],
        "kind": row["kind"],
        "is_pointer_like": bool(row["is_pointer_like"]),
        "original": row["number"] == first,
        "supply": row["supply"],
        "divisible": bool(row["divisible"]),
        "sha256": row["content_sha256"],
        "body": _inline_body(store, row),
    }


def _picture_source(store: Store, row: sqlite3.Row) -> tuple[bytes, str] | None:
    """(bytes, type) of the picture a counter is — a raster image, an SVG, or
    a stamp's decoded image — or None when its content is not a picture."""
    stamp = _stamp_payload(store, row)
    if stamp:
        return stamp
    ct = _served_type(store, row)
    if not (_is_raster(ct) or picture.is_svg(ct)):
        return None
    blob = store.read_blob(row["content_sha256"])
    return (blob, ct) if blob is not None else None


def render_social(store: Store, row: sqlite3.Row) -> bytes:
    """The og:image bytes for one counter: its own picture, re-encoded to a
    size and format crawlers show large, or the rendered card when it hasn't
    got one that can be drawn (including SVGs that script their own art)."""
    source = _picture_source(store, _effective_row(store, row))
    if source is not None:
        out = picture.render(*source)
        if out is not None:
            return out
    return card.render(_card_info(store, row))


def record_dict(store: Store, row: sqlite3.Row, *, owner: str | None = None,
                with_body: bool = True) -> dict:
    stamp = _stamp_payload(store, row)
    return {
        "number": row["number"],
        # §6.1: <reveal txid>i<msg_index> — the event's on-chain identity,
        # ordinals' syntax; for a counterparty/ord counter it is byte-identical
        # to the ord inscription ID of the same reveal.
        "id": format_id(row["mint_txid"], row["msg_index"]),
        "asset": _display_name(row),
        "asset_id": row["asset_id"],
        "kind": row["kind"],  # 'issuance' | 'fairminter'
        "content_type": row["content_type"],
        "content_type_raw": row["content_type_raw"],
        "size": row["content_length"],
        "is_pointer_like": bool(row["is_pointer_like"]),
        "stamp_mime": stamp[1] if stamp else None,
        # §5.5: the event a delegate body names — {'id','number','content_type'},
        # number None while unresolved — or null for a non-delegate.
        "delegate": _delegate_info(store, row),
        # Envelope style is computed from the reveal tx (a bitcoind fetch), so
        # it is filled only on the single-counter endpoint; null in lists
        # (unknown, not "no"). Server-determined, never indexed.
        "envelope": None,  # 'counterparty/ord' | 'counterparty'
        "owner": owner if owner is not None else row["source"],
        "source": row["source"],
        "txid": row["mint_txid"],
        "msg_index": row["msg_index"],
        "block": row["block_index"],
        "tx_index": row["cp_tx_index"],  # Counterparty tx index → tokenscan.io/tx/<n>
        "sha256": row["content_sha256"],
        "rolling_hash": row["rolling_hash"],
        "supply": row["supply"],
        "divisible": (bool(row["divisible"]) if row["divisible"] is not None else None),
        "locked": None,  # mutable; filled live on the single-counter endpoint
        # Total destroyed: the stored snapshot (refreshed by detail views);
        # the single-counter endpoint overlays the live number.
        "burned": row["burned"],
        "fee": row["fee"],
        "tx_size": row["tx_size"],
        "xcp_burned": row["xcp_burned"],
        "body": _inline_body(store, row) if with_body else None,
    }


# A per-counter endpoint accepts a number or an inscription ID (§6.1); asset
# names stay on /counter/ and /c/, which resolve any identifier via find().
_IDENT = r"\d+|[0-9a-fA-F]{64}i\d+"


class Handler(BaseHTTPRequestHandler):
    server_version = "counters/0.1"
    protocol_version = "HTTP/1.1"

    @property
    def config(self) -> Config:
        return self.server.config  # type: ignore[attr-defined]

    # --- routing -----------------------------------------------------------

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/status":
                return self._status()
            if path == "/counters":
                return self._api_list(parse_qs(parsed.query))
            m = re.fullmatch(r"/counter/(.+)", path)
            if m:
                return self._api_counter(unquote(m.group(1)))
            m = re.fullmatch(r"/block/(\d+)", path)
            if m:
                return self._block(int(m.group(1)))
            m = re.fullmatch(rf"/preview/({_IDENT})", path)
            if m:
                return self._preview(m.group(1),
                                     raw="raw" in parse_qs(parsed.query))
            m = re.fullmatch(rf"/delegate/({_IDENT})", path)
            if m:
                return self._delegate(m.group(1))
            m = re.fullmatch(rf"/content/({_IDENT})", path)
            if m:
                return self._content(m.group(1))
            m = re.fullmatch(rf"/stamp/({_IDENT})", path)
            if m:
                return self._stamp(m.group(1))
            m = re.fullmatch(rf"/social/({_IDENT})\.png", path)
            if m:
                return self._social(m.group(1))
            # A counter's own page: the SPA, but server-rendered with per-counter
            # Open Graph tags so a shared link previews *that counter's* image
            # (crawlers don't run the JS or see the #/c/<id> hash).
            m = re.fullmatch(r"/c/(.+)", path)
            if m:
                return self._counter_page(unquote(m.group(1)))
            return self._static(path)
        except BrokenPipeError:
            pass
        except Exception as e:  # never leak a stack trace to the client
            log.exception("request failed: %s", self.path)
            self._json({"error": str(e)}, status=500)

    do_HEAD = do_GET

    # --- API handlers ------------------------------------------------------

    def _status(self) -> None:
        store = Store(self.config)
        try:
            payload = {
                # 0-default => -1 on a fresh DB, which the frontend treats as
                # "nothing indexed yet" instead of linking a phantom block.
                "indexed": store.get_last_height(0),
                "count": store.count(),
                "genesis": 0,
                # Release identity: the version names the Counterparty Core
                # series this build targets, the commit names the exact build,
                # and updated (ISO 8601 UTC, null if unknown) is when the
                # deployed code last changed. All shown in the explorer footer.
                "version": __version__,
                "commit": GIT_COMMIT,
                "updated": GIT_UPDATED,
            }
        finally:
            store.close()
        self._json(payload)

    def _block(self, height: int) -> None:
        store = Store(self.config)
        try:
            rows = store.list_by_block_range(height, height)
            payload = {
                "block": height,
                "count": len(rows),
                "counters": [record_dict(store, r) for r in rows],
            }
        finally:
            store.close()
        self._json(payload)

    def _api_list(self, qs: dict[str, list[str]]) -> None:
        try:
            limit = max(1, min(int(qs.get("limit", ["120"])[0]), 500))
        except ValueError:
            limit = 120
        before = qs.get("before", [None])[0]
        if before not in (None, "", "null"):
            try:
                before = int(before)
            except ValueError:
                return self._json({"error": "before must be an integer"}, status=400)
        store = Store(self.config)
        try:
            if before not in (None, "", "null"):
                rows = store.list_before(before, limit)
            else:
                rows = store.list_recent(limit)
            payload = {"counters": [record_dict(store, r) for r in rows]}
        finally:
            store.close()
        self._json(payload)

    def _api_counter(self, ident: str) -> None:
        store = Store(self.config)
        try:
            row = store.find(ident)
            if row is None:
                return self._json({"error": "not found"}, status=404)
            info = _live_asset(self.config, row["asset"])
            owner = info.get("owner") or row["source"]
            rec = record_dict(store, row, owner=owner)
            rec["locked"] = info.get("locked")
            if info.get("supply") is not None:
                rec["supply"] = info["supply"]
            if info.get("divisible") is not None:
                rec["divisible"] = bool(info["divisible"])
            live_burned = _asset_burned(self.config, row["asset"])
            if live_burned is not None:
                rec["burned"] = live_burned
            # Persist the asset-level numbers so the crawler-facing preview
            # (/c/<n>, which never queries the backends) shows what the last
            # human viewer saw. COALESCE semantics: None never wipes a value.
            store.set_asset_snapshot(row["asset"], info.get("supply"), live_burned)
            rec["block_time"] = _block_time(self.config, row["block_index"])
            # Envelope style (counterparty vs counterparty/ord) from the reveal tx — server-side,
            # serve-time; never indexed; never affects validity or numbering.
            tx = self._reveal_tx(row)
            if tx is not None:
                rec["envelope"] = envelope_style(tx)      # 'counterparty/ord' | 'counterparty'
            if rec["fee"] is None:
                rec["fee"], rec["tx_size"] = self._ensure_fee(store, row)
            if rec["xcp_burned"] is None:
                rec["xcp_burned"] = self._ensure_xcp_burned(store, row)
            # All counters on this asset (the original first, then the later
            # events it accumulated — N6) so the explorer lists them together.
            siblings = store.get_counters_by_asset(row["asset"])
            first = siblings[0]["number"] if siblings else row["number"]
            rec["original"] = row["number"] == first
            rec["asset_counters"] = [
                {"number": s["number"],
                 "kind": s["kind"],
                 "original": s["number"] == first,
                 "content_type": s["content_type"]}
                for s in siblings
            ]
            self._json(rec)
        finally:
            store.close()

    def _reveal_tx(self, row) -> dict | None:
        """The counter's reveal transaction (bitcoind, verbose) — the source for
        the serve-time envelope-style tag. None if bitcoind is unreachable, so
        the UI can tell "no" from "couldn't check". Display-only; never affects
        validity or numbering."""
        try:
            return BitcoindClient(self.config).get_raw_transaction(row["mint_txid"], verbose=True)
        except Exception:
            log.debug("reveal-tx fetch failed for #%s", row["number"], exc_info=True)
            return None

    def _ensure_fee(self, store: Store, row) -> tuple[int | None, int | None]:
        """Compute the inscription cost (commit + reveal fee/size) from bitcoind
        once and persist it (best effort — null if the node is unreachable)."""
        try:
            fee, tx_size = BitcoindClient(self.config).get_inscription_cost(row["mint_txid"])
            store.set_fee(row["number"], fee, tx_size)
            return fee, tx_size
        except Exception:
            log.debug("fee backfill failed for #%s", row["number"], exc_info=True)
            return None, None

    def _ensure_xcp_burned(self, store: Store, row) -> int | None:
        """Look up the XCP burned for the issuance from Counterparty once and
        persist it (best effort — null if Core is unreachable)."""
        try:
            cp = CounterpartyClient(self.config)
            burned = next(
                (int(r["fee_paid"]) for r in cp.get_issuances_by_tx(row["mint_txid"])
                 if r.get("fee_paid") is not None),
                None,
            )
            store.set_xcp_burned(row["number"], burned)
            return burned
        except Exception:
            log.debug("xcp_burned backfill failed for #%s", row["number"], exc_info=True)
            return None

    def _content(self, ident: str) -> None:
        store = Store(self.config)
        try:
            row = store.find(ident)
            if row is None:
                return self._send(404, "text/plain; charset=utf-8", b"counter not found")
            blob = store.read_blob(row["content_sha256"])
            if blob is None:
                return self._send(404, "text/plain; charset=utf-8", b"content unavailable")
            # Rule 2: a recognized on-chain signature wins over the declared
            # mime_type, so a mislabeled file (e.g. Ogg audio minted as
            # image/jpeg, #51) is served with a type the browser can render.
            # The bytes are still the exact canonical blob; the JSON API keeps
            # reporting the declared content_type. Deterministic in the bytes,
            # so the content-addressed immutable cache still holds.
            ctype = sniff_media(blob) or row["content_type"] or "application/octet-stream"
            self._send(200, ctype, blob, immutable=True, extra_headers=CONTENT_HEADERS,
                       ranged=True)
        finally:
            store.close()

    def _stamp(self, ident: str) -> None:
        """The decoded image of a stamp-like counter (`STAMP:<base64>` body).
        /content/<n> stays the raw consensus bytes; this is display-only."""
        store = Store(self.config)
        try:
            row = store.find(ident)
            if row is None:
                return self._send(404, "text/plain; charset=utf-8", b"counter not found")
            stamp = _stamp_payload(store, row)
            if stamp is None:
                return self._send(404, "text/plain; charset=utf-8", b"not stamp-like")
            raw, mime = stamp
            self._send(200, mime, raw, max_age=DERIVED_MAX_AGE, extra_headers=CONTENT_HEADERS)
        finally:
            store.close()

    def _delegate(self, ident: str) -> None:
        """The bytes a delegate's target committed — the one-hop resolution
        of a §5.5 body, from the local index only. /content/<n> keeps
        returning the delegate's own canonical bytes (rule 1); this is the
        rendered counterpart, cached briefly like every derived view (rule
        7), since an unresolved target can resolve when it gets indexed."""
        store = Store(self.config)
        try:
            row = store.find(ident)
            if row is None:
                return self._send(404, "text/plain; charset=utf-8", b"counter not found")
            info = _delegate_info(store, row)
            if info is None:
                return self._send(404, "text/plain; charset=utf-8", b"not a delegate")
            if info["number"] is None:
                return self._send(404, "text/plain; charset=utf-8",
                                  b"delegate target is not an indexed counter")
            target = store.get_counter(info["number"])
            blob = store.read_blob(target["content_sha256"]) if target is not None else None
            if blob is None:
                return self._send(404, "text/plain; charset=utf-8", b"content unavailable")
            ctype = sniff_media(blob) or target["content_type"] or "application/octet-stream"
            self._send(200, ctype, blob, max_age=DERIVED_MAX_AGE,
                       extra_headers=CONTENT_HEADERS, ranged=True)
        finally:
            store.close()

    def _social(self, ident: str) -> None:
        """The og:image for /c/<number> — see `render_social`.

        Rendering costs real CPU (a 1.4 MB PNG has to be decoded, resized and
        re-encoded, maybe several times over to fit the byte budget), and
        crawlers refetch, so results are cached on disk under the data dir. The
        key pins the content hash and both renderer versions, so a
        re-inscription or a change to `card` or `picture` rebuilds it and a
        stale image can never be served. The body may be PNG or JPEG whatever
        the path says; the Content-Type header is what crawlers go by.
        """
        store = Store(self.config)
        try:
            row = store.find(ident)
            if row is None:
                return self._send(404, "text/plain; charset=utf-8", b"counter not found")
            name = (f"{row['number']}-{row['content_sha256'][:16]}"
                    f"-v{card.VERSION}.{picture.VERSION}.png")
            path = self.config.social_dir / name
            try:
                body = path.read_bytes()
            except OSError:
                with _social_lock(name):
                    # Another thread may have rendered it while we queued.
                    try:
                        body = path.read_bytes()
                    except OSError:
                        body = render_social(store, row)
                        self._cache_social(path, body)
            self._send(200, picture.mime_of(body), body, max_age=DERIVED_MAX_AGE,
                       extra_headers=[("X-Content-Type-Options", "nosniff")])
        finally:
            store.close()

    @staticmethod
    def _cache_social(path: Path, body: bytes) -> None:
        """Persist a rendered image, via a temp file so a concurrent reader
        never sees a half-written one. A read-only or full disk just means we
        re-render next time, so failures are logged and swallowed."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            tmp.write_bytes(body)
            tmp.replace(path)
        except OSError:
            log.debug("could not cache %s", path, exc_info=True)

    def _preview(self, ident: str, raw: bool = False) -> None:
        """ord-style preview: raw content for HTML/SVG (rendered as a document
        inside the sandboxed iframe), else a confined same-origin wrapper page
        that loads /content/<n> via a native element.

        ?raw=1 shows the canonical bytes as inert escaped text instead of any
        derived view — no delegate hop, no stamp decode, no live document —
        for any textual body (the explorer's raw toggle). Non-textual bodies
        have no raw text form and keep the normal preview."""
        store = Store(self.config)
        try:
            row = store.find(ident)
            if row is None:
                return self._send(404, "text/html; charset=utf-8",
                                  b"<!doctype html><meta charset=utf-8><title>404</title>not found")
            if raw:
                declared = row["content_type"] or "text/plain"
                if classify_mime_type(declared, row["block_index"]) == "text":
                    blob = store.read_blob(row["content_sha256"])
                    text = (blob or b"").decode("utf-8", "replace")
                    doc = preview.wrapper("text", row["number"], "text/plain",
                                          None, text)
                    return self._send(
                        200, "text/html; charset=utf-8", doc.encode("utf-8"),
                        max_age=DERIVED_MAX_AGE,
                        extra_headers=[
                            ("Content-Security-Policy", preview.csp_for("text")),
                            ("X-Content-Type-Options", "nosniff"),
                        ],
                    )
            else:
                # Rule 9: a resolved delegate previews as its target.
                row = _effective_row(store, row)
            blob = store.read_blob(row["content_sha256"])
            # Rule 2: sniff the bytes; a recognized signature wins over the
            # declared type when picking the native element — so #51's Ogg
            # renders as <audio>, not a broken <img>. HTML/SVG/text carry no
            # signature, so they keep their declared type.
            declared = row["content_type"] or "application/octet-stream"
            ctype = (sniff_media(blob) if blob else None) or declared
            kind, extra = preview.classify(ctype)
            if kind == preview.IFRAME:
                if blob is None:
                    return self._send(404, "text/html; charset=utf-8",
                                      b"<!doctype html><meta charset=utf-8><title>404</title>content unavailable")
                return self._send(200, ctype, blob, max_age=DERIVED_MAX_AGE,
                                  extra_headers=CONTENT_HEADERS)
            text = None
            if kind in ("text", "code", "markdown"):
                stamp = _stamp_payload(store, row)
                if stamp is not None:
                    # Stamp-like: preview the decoded image instead of the
                    # base64 text (§5.4). Served from /stamp/<n>.
                    kind, extra = preview.classify(stamp[1])
                    n = row["number"]
                    doc = preview.wrapper(kind, n, stamp[1], extra,
                                          src=f"/stamp/{n}")
                    return self._send(
                        200, "text/html; charset=utf-8", doc.encode("utf-8"),
                        max_age=DERIVED_MAX_AGE,
                        extra_headers=[
                            ("Content-Security-Policy", preview.csp_for(kind)),
                            ("X-Content-Type-Options", "nosniff"),
                        ],
                    )
                text = (blob or b"").decode("utf-8", "replace")
            doc = preview.wrapper(kind, row["number"], ctype, extra, text)
            self._send(
                200, "text/html; charset=utf-8", doc.encode("utf-8"),
                max_age=DERIVED_MAX_AGE,
                extra_headers=[
                    ("Content-Security-Policy", preview.csp_for(kind)),
                    ("X-Content-Type-Options", "nosniff"),
                ],
            )
        finally:
            store.close()

    # --- static assets -----------------------------------------------------

    def _counter_page(self, ident: str) -> None:
        """Serve the explorer for /c/<id> with per-counter Open Graph tags, so a
        shared counter link previews that counter's image. Humans get the same
        SPA (its router renders the detail view from the path); crawlers read
        the injected tags. Unknown ids fall back to the default (site) tags."""
        store = Store(self.config)
        try:
            page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
            row = store.find(ident)
            if row is not None:
                host = self.headers.get("Host") or "counters.gallery"
                proto = self.headers.get("X-Forwarded-Proto") or "https"
                base = f"{proto}://{host}"
                n = row["number"]
                name = _display_name(row)
                # Rule 9: a resolved delegate's link preview carries the
                # TARGET's picture; page identity (title, url) stays its own.
                eff = _effective_row(store, row)
                en = eff["number"]
                ct = _served_type(store, eff)
                dims: tuple[int, int] | None = None
                itype: str | None = None
                alt = f"Counter #{n} — {name}"
                # og:image has to be a raster image the crawler will fetch
                # and show large. A picture that already is one is linked
                # as-is (an animated GIF keeps animating); any other picture
                # goes through /social, which re-encodes it. Its final size
                # and format depend on the picture, so none are claimed.
                stamp = _stamp_payload(store, eff)
                if stamp:
                    if picture.serve_as_is(*stamp):
                        image, itype = f"{base}/stamp/{en}", stamp[1]
                    else:
                        image = f"{base}/social/{n}.png"
                elif _is_raster(ct):
                    blob = (store.read_blob(eff["content_sha256"])
                            if eff["content_length"] <= picture.MAX_BYTES else None)
                    if blob is not None and picture.serve_as_is(blob, ct):
                        image, itype = f"{base}/content/{en}", ct
                    else:
                        image = f"{base}/social/{n}.png"
                elif picture.is_svg(ct):
                    # Rasterized there, or a card if it scripts its own art.
                    image = f"{base}/social/{n}.png"
                else:
                    # Text, pointers, HTML, audio, binary: a rendered card
                    # carrying what the detail page shows.
                    image = f"{base}/social/{n}.png"
                    dims, itype = (card.WIDTH, card.HEIGHT), "image/png"
                    alt = f"Counter #{n} — {name} ({ct})"
                block = _social_meta(
                    title=f"Counter #{n} · {name} — Bitcoin Counters",
                    description=f"Counter #{n}: {name}{_supply_segment(row)} — "
                                f"a file inscribed on Bitcoin, owned through "
                                f"Counterparty, numbered from zero.",
                    url=f"{base}/c/{n}", image=image, big_image=True,
                    alt=alt, dims=dims, image_type=itype,
                )
                page = _SOCIAL_RE.sub(lambda _m: block, page, count=1)
            self._send(200, "text/html; charset=utf-8", page.encode("utf-8"),
                       max_age=DERIVED_MAX_AGE)
        finally:
            store.close()

    def _static(self, path: str) -> None:
        rel = "index.html" if path == "/" else path.lstrip("/")
        target = (STATIC_DIR / rel).resolve()
        if (
            not target.is_relative_to(STATIC_DIR)
            or not target.is_file()
            or target.suffix not in STATIC_TYPES
        ):
            return self._send(404, "text/plain; charset=utf-8", b"not found")
        # Assets get a temporary browser cache; the app shell (index.html)
        # stays uncached so a deploy shows up on the next refresh.
        max_age = None if target.suffix == ".html" else STATIC_MAX_AGE
        self._send(200, STATIC_TYPES[target.suffix], target.read_bytes(),
                   max_age=max_age)

    # --- response helpers --------------------------------------------------

    def _json(self, obj: dict, status: int = 200) -> None:
        self._send(status, "application/json; charset=utf-8", json.dumps(obj).encode())

    def _send(self, status: int, ctype: str, body: bytes, *, immutable: bool = False,
              max_age: int | None = None,
              extra_headers: list[tuple[str, str]] | None = None,
              ranged: bool = False) -> None:
        span = _parse_range(self.headers.get("Range"), len(body)) if ranged else None
        if span is not None:
            start, end = span
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(body)}")
            body = body[start:end + 1]
        else:
            self.send_response(status)
        if ranged:
            self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        if immutable:
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        elif max_age is not None:
            self.send_header("Cache-Control", f"public, max-age={max_age}")
        for name, value in (extra_headers or []):
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:  # quiet by default; -v shows it
        log.debug("%s %s", self.address_string(), fmt % args)


def _parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """A single `Range: bytes=…` span as inclusive (start, end), or None to send
    the whole body — which is always a valid answer to a range request.

    Only the single-span form is honoured; a multi-range request gets the entire
    resource rather than a multipart body. What needs this is a reader pulling
    one piece of a large inscription at a time — the PDF viewer asking for the
    few KB that hold page 1 of a whole-block book, or a seek in a long audio
    file — and those ask for one span.
    """
    if not header or size == 0:
        return None
    unit, _, spec = header.partition("=")
    if unit.strip().lower() != "bytes" or "," in spec:
        return None
    start_s, sep, end_s = spec.strip().partition("-")
    if not sep:
        return None
    try:
        if not start_s:                       # "-N": the final N bytes
            n = int(end_s)
            if n <= 0:
                return None
            return max(0, size - n), size - 1
        start = int(start_s)
        end = int(end_s) if end_s else size - 1
    except ValueError:
        return None
    end = min(end, size - 1)
    if start > end or start < 0:
        return None                           # unsatisfiable: fall back to 200
    return start, end


class _QuietThreadingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that doesn't dump a traceback when a client hangs up
    mid-request (a browser tab closing / navigating away). Those
    ConnectionReset/BrokenPipe/Timeout errors are the peer's doing, not a
    server fault — real handler errors still print."""

    def handle_error(self, request, client_address) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, BrokenPipeError, TimeoutError)):
            log.debug("client %s dropped the connection: %r", client_address, exc)
            return
        super().handle_error(request, client_address)


def make_server(config: Config, host: str = "127.0.0.1", port: int = 8081) -> ThreadingHTTPServer:
    """Build (but do not start) the explorer HTTP server. The caller drives it —
    either blocking via run() for a serve-only process, or on a background thread
    when `counters server` also runs the indexer in the foreground."""
    httpd = _QuietThreadingHTTPServer((host, port), Handler)
    httpd.config = config  # type: ignore[attr-defined]
    return httpd


def run(config: Config, host: str = "127.0.0.1", port: int = 8081) -> int:
    httpd = make_server(config, host, port)
    url = f"http://{host}:{port}"
    print(f"counters explorer + API on {url}  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping…")
    finally:
        httpd.server_close()
    return 0
