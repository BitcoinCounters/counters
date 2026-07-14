"""Read-only HTTP server for the Bitcoin Counters explorer.

Serves two things from one origin:

  1. The bundled single-page explorer (the static/ directory: index.html + logos).
  2. A small JSON API backed by the index Store:

       GET /status                      -> {"indexed": H, "count": N, "genesis": 0}
       GET /counters?before=N&limit=K   -> {"counters": [record, ...]}  newest-first
       GET /counter/<number|asset>      -> a single record (404 if unknown)
       GET /block/<height>              -> {"block": H, "count": K, "counters": [...]}
       GET /content/<number>            -> the raw file bytes, with its stored MIME

A "record" is the index row reshaped to the field names the frontend expects
(number, asset, asset_id, content_type, size, body, owner, txid, block,
position, sha256). Textual content (small text/*, JSON, SVG) is inlined as
`body`; everything else is fetched lazily from /content/<number>.

The server is intentionally dependency-free (stdlib http.server). Each request
opens its own SQLite connection because ThreadingHTTPServer handles requests on
worker threads and SQLite connections are not shareable across threads.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import preview
from ..bitcoind import BitcoindClient
from ..config import Config
from ..counterparty import CounterpartyClient, CounterpartyError
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
}

# Inline only small textual blobs in JSON responses; larger or binary content
# is served on demand from /content/<number>.
BODY_MAX_BYTES = 256 * 1024
INLINE_TYPES = ("text/", "application/json", "image/svg+xml")


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


def record_dict(store: Store, row: sqlite3.Row, *, owner: str | None = None,
                with_body: bool = True) -> dict:
    return {
        "number": row["number"],
        "asset": _display_name(row),
        "asset_id": row["asset_id"],
        "content_type": row["content_type"],
        "size": row["content_length"],
        "owner": owner if owner is not None else row["owner"],
        "txid": row["mint_txid"],
        "block": row["block_index"],
        "position": row["block_position"],
        "tx_index": row["cp_tx_index"],  # Counterparty tx index → tokenscan.io/tx/<n>
        "sha256": row["content_sha256"],
        "supply": row["supply"],
        "divisible": (bool(row["divisible"]) if row["divisible"] is not None else None),
        "locked": None,  # mutable; filled live on the single-counter endpoint
        "fee": row["fee"],
        "tx_size": row["tx_size"],
        "xcp_burned": row["xcp_burned"],
        "reinscription": bool(row["reinscription"]),
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
                "indexed": store.get_last_height(self.config.start_height),
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
        store = Store(self.config)
        try:
            if before not in (None, "", "null"):
                rows = store.list_before(int(before), limit)
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
            owner = info.get("owner") or row["owner"]
            rec = record_dict(store, row, owner=owner)
            rec["locked"] = info.get("locked")
            if info.get("supply") is not None:
                rec["supply"] = info["supply"]
            if info.get("divisible") is not None:
                rec["divisible"] = bool(info["divisible"])
            if rec["fee"] is None:
                rec["fee"], rec["tx_size"] = self._ensure_fee(store, row)
            if rec["xcp_burned"] is None:
                rec["xcp_burned"] = self._ensure_xcp_burned(store, row)
            # All counters inscribed on this asset (original first, then any
            # reinscriptions) so the explorer can list them together.
            siblings = store.get_counters_by_asset(row["asset"])
            rec["asset_counters"] = [
                {"number": s["number"],
                 "reinscription": bool(s["reinscription"]),
                 "content_type": s["content_type"]}
                for s in siblings
            ]
            self._json(rec)
        finally:
            store.close()

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
            rows = cp.get_block_issuances(row["block_index"]).get(row["mint_txid"], [])
            burned = next(
                (int(r["fee_paid"]) for r in rows
                 if cp.is_creation(r) and r.get("fee_paid") is not None),
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
            ctype = row["content_type"] or "application/octet-stream"
            self._send(200, ctype, blob, immutable=True, extra_headers=CONTENT_HEADERS)
        finally:
            store.close()

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
            ctype = row["content_type"] or "application/octet-stream"
            kind, extra = preview.classify(ctype)
            if kind == preview.IFRAME:
                blob = store.read_blob(row["content_sha256"])
                if blob is None:
                    return self._send(404, "text/html; charset=utf-8",
                                      b"<!doctype html><meta charset=utf-8><title>404</title>content unavailable")
                return self._send(200, ctype, blob, immutable=True, extra_headers=CONTENT_HEADERS)
            text = None
            if kind in ("text", "code", "markdown"):
                blob = store.read_blob(row["content_sha256"]) or b""
                text = blob.decode("utf-8", "replace")
            doc = preview.wrapper(kind, number, ctype, extra, text)
            self._send(
                200, "text/html; charset=utf-8", doc.encode("utf-8"), immutable=True,
                extra_headers=[
                    ("Content-Security-Policy", preview.csp_for(kind)),
                    ("X-Content-Type-Options", "nosniff"),
                ],
            )
        finally:
            store.close()

    # --- static assets -----------------------------------------------------

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
              extra_headers: list[tuple[str, str]] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        if immutable:
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        for name, value in (extra_headers or []):
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:  # quiet by default; -v shows it
        log.debug("%s %s", self.address_string(), fmt % args)


def make_server(config: Config, host: str = "127.0.0.1", port: int = 8081) -> ThreadingHTTPServer:
    """Build (but do not start) the explorer HTTP server. The caller drives it —
    either blocking via run() for a serve-only process, or on a background thread
    when `counters server` also runs the indexer in the foreground."""
    httpd = ThreadingHTTPServer((host, port), Handler)
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
