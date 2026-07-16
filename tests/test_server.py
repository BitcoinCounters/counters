"""Integration tests for `counters server`.

Spin up the real request handler on an ephemeral port against a throwaway
index, then exercise the JSON API, raw content serving, and static assets.
The live-owner lookup (the only thing that would touch Counterparty Core) is
stubbed so the test is fully offline.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counters2.config import Config  # noqa: E402
from counters2.server import app as appmod  # noqa: E402
from counters2.store import CounterRecord, Store  # noqa: E402


# 1x1 transparent GIF89a — the decoded payload of the stamp-like counter.
GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04"
    b"\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D"
    b"\x01\x00;"
)


def _seed_store(data_dir: str) -> Config:
    cfg = Config()
    cfg.data_dir = data_dir
    store = Store(cfg)
    sha = store.store_blob(b"hi")
    store.add_counter(
        0,
        CounterRecord(
            asset="TESTASSET", asset_id="123", asset_longname=None,
            kind="issuance", content_type="text/plain", content_type_raw=None,
            content_sha256=sha, content_length=2, is_pointer_like=False,
            mint_txid="aa" * 32, msg_index=0, block_index=902005,
            cp_tx_index=1, source="bc1pstored", divisible=False, supply=1,
        ),
    )
    # A later event on the SAME asset (per-event numbering, N6).
    sha2 = store.store_blob(b"v2")
    store.add_counter(
        1,
        CounterRecord(
            asset="TESTASSET", asset_id="123", asset_longname=None,
            kind="issuance", content_type="text/plain", content_type_raw=None,
            content_sha256=sha2, content_length=2, is_pointer_like=False,
            mint_txid="bb" * 32, msg_index=0, block_index=902006,
            cp_tx_index=2, source="bc1pstored", divisible=False, supply=1,
        ),
    )
    # A stamp-like counter: text/plain whose body is STAMP:<base64 gif> (§5.4).
    stamp_text = b"STAMP:" + base64.b64encode(GIF)
    sha3 = store.store_blob(stamp_text)
    store.add_counter(
        2,
        CounterRecord(
            asset="STAMPTEST", asset_id="456", asset_longname=None,
            kind="issuance", content_type="text/plain", content_type_raw=None,
            content_sha256=sha3, content_length=len(stamp_text),
            is_pointer_like=False,
            mint_txid="cc" * 32, msg_index=0, block_index=902006,
            cp_tx_index=3, source="bc1pstored", divisible=False, supply=1,
        ),
    )
    store.set_last_height(902006, None)   # so /status reports a synced height
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
        assert len(data["counters"]) == 3     # original + later event (N6) + stamp
        recs = {r["number"]: r for r in data["counters"]}
        rec = recs[0]
        assert rec["number"] == 0
        assert rec["asset"] == "TESTASSET"
        assert rec["kind"] == "issuance"
        assert rec["size"] == 2
        assert rec["body"] == "hi"           # small text inlined
        assert rec["block"] == 902005 and rec["msg_index"] == 0
        assert rec["tx_index"] == 1   # → tokenscan.io/tx/<tx_index>
        assert rec["fee"] == 333 and rec["tx_size"] == 111
        assert rec["xcp_burned"] == 50000000
        assert rec["supply"] == 1 and rec["divisible"] is False
        assert rec["owner"] == "bc1pstored" and rec["source"] == "bc1pstored"
        assert rec["is_pointer_like"] is False
        assert rec["stamp_mime"] is None
        assert rec["rolling_hash"] and recs[1]["rolling_hash"] != rec["rolling_hash"]

        # --- stamp-like counter (§5.4): flagged, body stays the raw text ---
        stamp_rec = recs[2]
        assert stamp_rec["stamp_mime"] == "image/gif"
        assert stamp_rec["body"] == "STAMP:" + base64.b64encode(GIF).decode()

        # --- /status: latest synced height + total count ---
        status, ctype, body = _get(base, "/status")
        assert status == 200 and "application/json" in ctype
        st = json.loads(body)
        assert st["count"] == 3 and st["indexed"] == 902006

        # --- /block/<height>: counters minted in a block ---
        status, _, body = _get(base, "/block/902005")
        assert status == 200
        blk = json.loads(body)
        assert blk["block"] == 902005 and blk["count"] == 1
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
        # the single-counter endpoint lists every counter on the asset
        assert [(a["number"], a["kind"]) for a in c0["asset_counters"]] == \
            [(0, "issuance"), (1, "issuance")]
        # resolving by asset name returns the ORIGINAL (lowest number)
        by_asset = json.loads(_get(base, "/counter/TESTASSET")[2])
        assert by_asset["number"] == 0
        assert _get(base, "/counter/999")[0] == 404

        # --- /content/<number> serves raw bytes with stored MIME ---
        status, ctype, body = _get(base, "/content/0")
        assert status == 200 and body == b"hi" and ctype.startswith("text/plain")

        # --- /stamp/<number>: decoded image for stamp-like counters only ---
        status, ctype, body = _get(base, "/stamp/2")
        assert status == 200 and ctype == "image/gif" and body == GIF
        assert _get(base, "/stamp/0")[0] == 404      # not stamp-like
        assert _get(base, "/stamp/999")[0] == 404    # unknown counter
        # /content of the stamp counter stays the raw consensus text
        status, ctype, body = _get(base, "/content/2")
        assert status == 200 and body.startswith(b"STAMP:") and ctype.startswith("text/plain")
        # /preview of the stamp counter is the image wrapper, not the text one
        status, ctype, body = _get(base, "/preview/2")
        assert status == 200 and "text/html" in ctype
        assert b"/stamp/2" in body and b"<img" in body
        # a plain-text counter still previews as text
        status, _, body = _get(base, "/preview/0")
        assert status == 200 and b"<pre>hi</pre>" in body

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
