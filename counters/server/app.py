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
import re
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from ..bitcoind import BitcoindClient
from ..config import Config
from ..counterparty import CounterpartyClient, CounterpartyError
from ..store import Store

log = logging.getLogger("counters")

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


def _current_owner(config: Config, asset: str, fallback: str | None) -> str | None:
    """Live holder per Counterparty (ownership can change after the mint);
    fall back to the mint-time owner if Core is unreachable."""
    try:
        info = CounterpartyClient(config).get_asset(asset) or {}
        return info.get("owner") or fallback
    except CounterpartyError:
        return fallback


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
        "sha256": row["content_sha256"],
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
            owner = _current_owner(self.config, row["asset"], row["owner"])
            rec = record_dict(store, row, owner=owner)
            if rec["fee"] is None:
                rec["fee"], rec["tx_size"] = self._ensure_fee(store, row)
            if rec["xcp_burned"] is None:
                rec["xcp_burned"] = self._ensure_xcp_burned(store, row)
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
            self._send(200, ctype, blob, immutable=True)
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

    def _send(self, status: int, ctype: str, body: bytes, *, immutable: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        if immutable:
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:  # quiet by default; -v shows it
        log.debug("%s %s", self.address_string(), fmt % args)


def run(config: Config, host: str = "127.0.0.1", port: int = 81) -> int:
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.config = config  # type: ignore[attr-defined]
    url = f"http://{host}:{port}"
    print(f"counters explorer + API on {url}  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping…")
    finally:
        httpd.server_close()
    return 0
