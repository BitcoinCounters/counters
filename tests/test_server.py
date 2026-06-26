"""Integration tests for `counters server`.

Spin up the real request handler on an ephemeral port against a throwaway
index, then exercise the JSON API, raw content serving, and static assets.
The live-owner lookup (the only thing that would touch Counterparty Core) is
stubbed so the test is fully offline.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counters.config import Config  # noqa: E402
from counters.server import app as appmod  # noqa: E402
from counters.store import CounterRecord, Store  # noqa: E402


def _seed_store(data_dir: str) -> Config:
    cfg = Config()
    cfg.data_dir = data_dir
    store = Store(cfg)
    sha = store.store_blob(b"hi")
    store.add_counter(
        0,
        CounterRecord(
            asset="TESTASSET", asset_id="123", asset_longname=None,
            content_type="text/plain", content_sha256=sha, content_length=2,
            mint_txid="aa" * 32, block_index=800000, block_position=3,
            cp_tx_index=1, owner="bc1pstored", divisible=False, supply=1,
        ),
    )
    store.set_last_height(800000, None)   # so /status reports a synced height
    store.set_fee(0, 333, 111)            # mint fee/size (no bitcoind needed in tests)
    store.set_xcp_burned(0, 50000000)     # 0.5 XCP burned (no Counterparty needed)
    store.commit()
    store.close()
    return cfg


def _get(base: str, path: str):
    try:
        with urllib.request.urlopen(base + path, timeout=5) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read()


def _run_server():
    tmp = tempfile.mkdtemp()
    cfg = _seed_store(tmp)
    # Keep the test offline: never call Counterparty for live asset info.
    appmod._live_asset = lambda config, asset: {}
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), appmod.Handler)
    httpd.config = cfg
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def test_api_and_static():
    httpd, base = _run_server()
    try:
        # --- /counters list ---
        status, ctype, body = _get(base, "/counters?limit=5")
        assert status == 200 and "application/json" in ctype
        data = json.loads(body)
        assert len(data["counters"]) == 1
        rec = data["counters"][0]
        assert rec["number"] == 0
        assert rec["asset"] == "TESTASSET"
        assert rec["size"] == 2
        assert rec["body"] == "hi"           # small text inlined
        assert rec["block"] == 800000 and rec["position"] == 3
        assert rec["fee"] == 333 and rec["tx_size"] == 111
        assert rec["xcp_burned"] == 50000000
        assert rec["supply"] == 1 and rec["divisible"] is False

        # --- /status: latest synced height + total count ---
        status, ctype, body = _get(base, "/status")
        assert status == 200 and "application/json" in ctype
        st = json.loads(body)
        assert st["count"] == 1 and st["indexed"] == 800000

        # --- /block/<height>: counters minted in a block ---
        status, _, body = _get(base, "/block/800000")
        assert status == 200
        blk = json.loads(body)
        assert blk["block"] == 800000 and blk["count"] == 1
        assert blk["counters"][0]["number"] == 0
        # an empty block reports zero, not an error
        empty = json.loads(_get(base, "/block/123456")[2])
        assert empty["count"] == 0 and empty["counters"] == []

        # --- /counter/<number> and /counter/<asset> ---
        c0 = json.loads(_get(base, "/counter/0")[2])
        assert c0["fee"] == 333 and c0["tx_size"] == 111   # already stored, no backfill
        assert c0["xcp_burned"] == 50000000
        assert c0["supply"] == 1 and c0["divisible"] is False
        assert c0["locked"] is None   # live lookup stubbed offline
        assert _get(base, "/counter/TESTASSET")[0] == 200
        assert _get(base, "/counter/999")[0] == 404

        # --- /content/<number> serves raw bytes with stored MIME ---
        status, ctype, body = _get(base, "/content/0")
        assert status == 200 and body == b"hi" and ctype.startswith("text/plain")

        # --- static SPA + asset ---
        status, ctype, body = _get(base, "/")
        assert status == 200 and "text/html" in ctype and b"<!DOCTYPE html>" in body
        assert _get(base, "/counters-icon.svg")[0] == 200

        # --- source files are not served (extension allowlist) ---
        assert _get(base, "/app.py")[0] == 404
    finally:
        httpd.shutdown()
        httpd.server_close()


if __name__ == "__main__":
    test_api_and_static()
    print("ok")
