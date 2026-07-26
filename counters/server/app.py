"""Read-only HTTP server for the Bitcoin Counters explorer.

Serves two things from one origin:

  1. The bundled single-page explorer (the static/ directory: index.html + logos).
  2. A small JSON API backed by the index Store:

       GET /status                      -> {"indexed": H, "count": N, "genesis": 0}
       GET /counters?before=N&limit=K   -> {"counters": [record, ...]}  newest-first
       GET /counter/<number|asset>      -> a single record (404 if unknown)
       GET /block/<height>              -> {"block": H, "count": K, "counters": [...]}
       GET /content/<number>            -> the raw file bytes, with its stored MIME
       GET /social/<number>.png         -> the `og:image` for a shared link

A "record" is the index row reshaped to the field names the frontend expects
(number, asset, asset_id, content_type, size, body, owner, txid, block,
position, sha256). Textual content (small text/*, JSON, SVG) is inlined as
`body`; everything else is fetched lazily from /content/<number>.

The server is intentionally dependency-free (stdlib http.server). Each request
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import card, png, preview
from ..bitcoind import BitcoindClient
from ..config import Config
from ..content import classify_mime_type, sniff_media, stamp_image
from ..counterparty import CounterpartyClient, CounterpartyError
from ..reveal import envelope_style
from ..store import Store

log = logging.getLogger("counters")

def _read_git_commit(git_dir: str = "/app/.git") -> str | None:
    """Resolve the short HEAD commit by reading a git dir directly (no git
    binary). Used when the repo's .git is bind-mounted into the container, so a
    plain `docker compose up -d` shows the real revision without a build arg."""
    head_path = Path(git_dir, "HEAD")
    try:
        head = head_path.read_text().strip()
    except OSError:
        return None
    if not head.startswith("ref:"):
        return head[:7] or None  # detached HEAD holds the sha directly
    ref = head[4:].strip()  # e.g. "refs/heads/main"
    try:  # loose ref
        return (Path(git_dir, ref).read_text().strip()[:7]) or None
    except OSError:
        pass
    try:  # packed-refs fallback
        for line in Path(git_dir, "packed-refs").read_text().splitlines():
            if line and not line.startswith(("#", "^")):
                sha, _, name = line.partition(" ")
                if name.strip() == ref:
                    return sha.strip()[:7]
    except OSError:
        pass
    return None


def _resolve_commit() -> str:
    # A real build-time stamp (CI's Dockerfile GIT_COMMIT arg) wins; otherwise
    # read a bind-mounted .git at runtime; finally fall back to "dev".
    env = os.environ.get("COUNTER_GIT_COMMIT")
    if env and env != "dev":
        return env
    return _read_git_commit() or env or "dev"


# Deployed build revision, surfaced on /status and in the explorer footer.
GIT_COMMIT = _resolve_commit()

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
}

# Inline only small textual blobs in JSON responses; larger or binary content
# is served on demand from /content/<number>.
BODY_MAX_BYTES = 256 * 1024

# Derived views (/preview, /stamp) are functions of the rendering rules, not of
# immutable content, so they must never be cached `immutable` (rule 7): a rules
# change has to take effect promptly. /content stays immutable (content-addressed).
DERIVED_MAX_AGE = 300
INLINE_TYPES = ("text/", "application/json", "image/svg+xml")

# Link crawlers fetch og:image on a short budget and several (WhatsApp being
# the strictest of the mainstream ones) simply give up on a large file, which
# is why counter #95 — a 1.4 MB PNG — previewed as nothing. Images above this
# are re-served downscaled from /social/<n>.png.
SOCIAL_MAX_BYTES = 300 * 1024
# og:image is displayed at a few hundred pixels wide; past this we are only
# spending the crawler's bandwidth.
SOCIAL_MAX_DIM = 1200
# Stop shrinking here even if still over budget, rather than serving a thumbnail.
SOCIAL_MIN_DIM = 320
# Decoding is per-byte Python (~1s per megapixel), so anything larger is left
# alone rather than parked on a worker thread; #95, the biggest counter so far,
# is 1.6 MP. Nothing is lost when this trips — /content still serves the file.
SOCIAL_MAX_PIXELS = 4_000_000


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


def _downscaled(blob: bytes) -> bytes | None:
    """A PNG of `blob` small enough for a crawler, or None if it can't decode.

    Shrinks by whole box factors until it is under the byte budget — one step
    at a time, because how much a photo deflates is not predictable from its
    dimensions.
    """
    decoded = png.decode(blob, max_pixels=SOCIAL_MAX_PIXELS)
    if decoded is None:
        return None
    width, height, rgb = decoded
    factor = png.factor_for(width, height, SOCIAL_MAX_DIM, SOCIAL_MAX_DIM)
    while True:
        ow, oh, small = png.shrink(width, height, rgb, factor)
        out = png.encode(ow, oh, small)
        if len(out) <= SOCIAL_MAX_BYTES or min(ow, oh) <= SOCIAL_MIN_DIM:
            return out
        factor += 1


def render_social(store: Store, row: sqlite3.Row) -> bytes:
    """The og:image bytes for one counter: its own picture, downscaled to fit a
    crawler's budget, or the rendered card when it hasn't got one."""
    if _is_raster(_served_type(store, row)):
        blob = store.read_blob(row["content_sha256"])
        if blob is not None:
            shrunk = _downscaled(blob)
            if shrunk is not None:
                return shrunk
    return card.render(_card_info(store, row))


def record_dict(store: Store, row: sqlite3.Row, *, owner: str | None = None,
                with_body: bool = True) -> dict:
    stamp = _stamp_payload(store, row)
    return {
        "number": row["number"],
        "asset": _display_name(row),
        "asset_id": row["asset_id"],
        "kind": row["kind"],  # 'issuance' | 'fairminter'
        "content_type": row["content_type"],
        "content_type_raw": row["content_type_raw"],
        "size": row["content_length"],
        "is_pointer_like": bool(row["is_pointer_like"]),
        "stamp_mime": stamp[1] if stamp else None,
        # Envelope style is computed from the reveal tx (a bitcoind fetch), so
        # it is filled only on the single-counter endpoint; null in lists
        # (unknown, not "no"). Server-determined, never indexed.
        "envelope": None,  # 'ord' | 'generic'
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
        "fee": row["fee"],
        "tx_size": row["tx_size"],
        "xcp_burned": row["xcp_burned"],
        "body": _inline_body(store, row) if with_body else None,
    }


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
            m = re.fullmatch(r"/preview/(\d+)", path)
            if m:
                return self._preview(int(m.group(1)))
            m = re.fullmatch(r"/content/(\d+)", path)
            if m:
                return self._content(int(m.group(1)))
            m = re.fullmatch(r"/stamp/(\d+)", path)
            if m:
                return self._stamp(int(m.group(1)))
            m = re.fullmatch(r"/social/(\d+)\.png", path)
            if m:
                return self._social(int(m.group(1)))
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
                "commit": GIT_COMMIT,
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
            rec["block_time"] = _block_time(self.config, row["block_index"])
            # Envelope style (ord/generic) from the reveal tx — server-side,
            # serve-time; never indexed; never affects validity or numbering.
            tx = self._reveal_tx(row)
            if tx is not None:
                rec["envelope"] = envelope_style(tx)      # 'ord' | 'generic'
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

    def _content(self, number: int) -> None:
        store = Store(self.config)
        try:
            row = store.get_counter(number)
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
            self._send(200, ctype, blob, immutable=True, extra_headers=CONTENT_HEADERS)
        finally:
            store.close()

    def _stamp(self, number: int) -> None:
        """The decoded image of a stamp-like counter (`STAMP:<base64>` body).
        /content/<n> stays the raw consensus bytes; this is display-only."""
        store = Store(self.config)
        try:
            row = store.get_counter(number)
            if row is None:
                return self._send(404, "text/plain; charset=utf-8", b"counter not found")
            stamp = _stamp_payload(store, row)
            if stamp is None:
                return self._send(404, "text/plain; charset=utf-8", b"not stamp-like")
            raw, mime = stamp
            self._send(200, mime, raw, max_age=DERIVED_MAX_AGE, extra_headers=CONTENT_HEADERS)
        finally:
            store.close()

    def _social(self, number: int) -> None:
        """The og:image for /c/<number> — see `render_social`.

        Rendering costs real CPU (a 1.4 MB PNG has to be inflated, box-filtered
        and re-deflated), and crawlers refetch, so results are cached on disk
        under the data dir. The key pins the content hash and the renderer
        version, so a re-inscription or a change to `card` rebuilds it and a
        stale card can never be served.
        """
        store = Store(self.config)
        try:
            row = store.get_counter(number)
            if row is None:
                return self._send(404, "text/plain; charset=utf-8", b"counter not found")
            name = f"{number}-{row['content_sha256'][:16]}-v{card.VERSION}.png"
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
            self._send(200, "image/png", body, max_age=DERIVED_MAX_AGE,
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

    def _preview(self, number: int) -> None:
        """ord-style preview: raw content for HTML/SVG (rendered as a document
        inside the sandboxed iframe), else a confined same-origin wrapper page
        that loads /content/<n> via a native element."""
        store = Store(self.config)
        try:
            row = store.get_counter(number)
            if row is None:
                return self._send(404, "text/html; charset=utf-8",
                                  b"<!doctype html><meta charset=utf-8><title>404</title>not found")
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
                    doc = preview.wrapper(kind, number, stamp[1], extra,
                                          src=f"/stamp/{number}")
                    return self._send(
                        200, "text/html; charset=utf-8", doc.encode("utf-8"),
                        max_age=DERIVED_MAX_AGE,
                        extra_headers=[
                            ("Content-Security-Policy", preview.csp_for(kind)),
                            ("X-Content-Type-Options", "nosniff"),
                        ],
                    )
                text = (blob or b"").decode("utf-8", "replace")
            doc = preview.wrapper(kind, number, ctype, extra, text)
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
                host = self.headers.get("Host") or "www.bitcoincounters.com"
                proto = self.headers.get("X-Forwarded-Proto") or "https"
                base = f"{proto}://{host}"
                n = row["number"]
                name = _display_name(row)
                ct = _served_type(store, row)
                dims: tuple[int, int] | None = None
                itype: str | None = None
                alt = f"Counter #{n} — {name}"
                # og:image has to be a real raster image, and one the crawler
                # will actually finish fetching.
                if _stamp_payload(store, row):
                    # A stamp's displayable image is its decoded base64, which
                    # is bounded by BODY_MAX_BYTES and so always small enough.
                    image = f"{base}/stamp/{n}"
                elif _is_raster(ct):
                    if row["content_length"] <= SOCIAL_MAX_BYTES:
                        image, itype = f"{base}/content/{n}", ct
                    elif ct == "image/png":
                        # Oversized, and PNG is the one format we can decode,
                        # so serve it downscaled. Its final size depends on how
                        # far it had to shrink, so no dimensions are claimed.
                        image, itype = f"{base}/social/{n}.png", "image/png"
                    else:
                        # Oversized JPEG/GIF: no decoder for those, so this is
                        # the best available. Telegram and Twitter cope; the
                        # strictest crawlers may still skip it.
                        image, itype = f"{base}/content/{n}", ct
                else:
                    # Text, pointers, HTML, audio, binary: a rendered card
                    # carrying what the detail page shows.
                    image = f"{base}/social/{n}.png"
                    dims, itype = (card.WIDTH, card.HEIGHT), "image/png"
                    alt = f"Counter #{n} — {name} ({ct})"
                block = _social_meta(
                    title=f"Counter #{n} · {name} — Bitcoin Counters",
                    description=f"Counter #{n}: {name} — a file inscribed on "
                                f"Bitcoin, owned through Counterparty, numbered from zero.",
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
        self._send(200, STATIC_TYPES[target.suffix], target.read_bytes())

    # --- response helpers --------------------------------------------------

    def _json(self, obj: dict, status: int = 200) -> None:
        self._send(status, "application/json; charset=utf-8", json.dumps(obj).encode())

    def _send(self, status: int, ctype: str, body: bytes, *, immutable: bool = False,
              max_age: int | None = None,
              extra_headers: list[tuple[str, str]] | None = None) -> None:
        self.send_response(status)
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
