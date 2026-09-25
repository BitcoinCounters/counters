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
import sqlite3
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counters import __version__  # noqa: E402
from counters.config import Config  # noqa: E402
from counters.server import app as appmod  # noqa: E402
from counters.store import CounterRecord, Store  # noqa: E402


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
    appmod._asset_burned = lambda config, asset: 100 if asset == "TESTASSET" else None
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
        # Release identity the footer renders: our version (which mirrors the
        # Counterparty Core series we target), the build's commit, and when the
        # deployed code last changed (null when no git dir / CI stamp exists).
        assert st["version"] == __version__
        assert st["commit"]
        assert "updated" in st

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
        assert c0["burned"] == 100    # total destroyed, shown next to supply
        assert rec["burned"] is None  # list read BEFORE any view: no snapshot yet
        # The view persisted the asset snapshot (for the crawler-facing
        # preview, which never queries the backends). Supply stays 1: the
        # live lookup is stubbed offline, and None never wipes a value.
        srow = Store(httpd.config).get_counter(0)
        assert srow["burned"] == 100 and srow["supply"] == 1
        # ... so lists now report the stored value, still without lookups.
        relisted = json.loads(_get(base, "/counters?limit=5")[2])["counters"]
        assert {r["number"]: r["burned"] for r in relisted}[0] == 100
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


def test_store_migrates_pre_burned_db():
    # A DB created before the `burned` column existed gets it added on open
    # (SCHEMA is IF NOT EXISTS, so it alone never upgrades an old file).
    cfg = _seed_store(tempfile.mkdtemp())
    db = sqlite3.connect(str(cfg.db_path))
    db.execute("ALTER TABLE counters DROP COLUMN burned")
    db.commit()
    db.close()
    store = Store(cfg)
    try:
        assert store.get_counter(0)["burned"] is None
        # the snapshot updates every counter on the asset, keeps supply (None)
        store.set_asset_snapshot("TESTASSET", None, 7)
        assert store.get_counter(0)["burned"] == 7
        assert store.get_counter(1)["burned"] == 7      # sibling, same asset
        assert store.get_counter(2)["burned"] is None   # other asset untouched
        assert store.get_counter(0)["supply"] == 1      # None never wipes
    finally:
        store.close()


if __name__ == "__main__":
    test_api_and_static()
    test_store_migrates_pre_burned_db()
    print("ok")


def test_inscription_id_addressing():
    """§6.1: every record carries its inscription id, and the per-counter
    endpoints accept one wherever they accept a number. A bare txid is never
    an identifier — the index is load-bearing."""
    httpd, base = _run_server()
    try:
        status, _, body = _get(base, "/counter/0")
        assert status == 200
        rec = json.loads(body)
        iid = rec["id"]
        assert iid == "aa" * 32 + "i0"

        # /counter/<id>, with uppercase hex normalised on input
        for token in (iid, ("aa" * 32).upper() + "i0"):
            status, _, body = _get(base, f"/counter/{token}")
            assert status == 200 and json.loads(body)["number"] == 0

        # /content/<id> serves the very same bytes and type as /content/<n>
        by_num = _get(base, "/content/0")
        by_id = _get(base, f"/content/{iid}")
        assert by_num == by_id and by_id[0] == 200 and by_id[2] == b"hi"

        # /preview/<id> renders; /stamp/<id> decodes the stamp counter
        status, ctype, _ = _get(base, f"/preview/{iid}")
        assert status == 200 and "text/html" in ctype
        status, _, body = _get(base, "/counter/2")
        stamp_id = json.loads(body)["id"]
        assert stamp_id == "cc" * 32 + "i0"
        status, ctype, body = _get(base, f"/stamp/{stamp_id}")
        assert status == 200 and ctype == "image/gif" and body == GIF

        # unknown event: a valid ID shape that names nothing → 404, not 500
        status, _, _ = _get(base, "/counter/" + "ee" * 32 + "i0")
        assert status == 404
        # a bare txid matches no route (content) and no identifier (counter)
        status, _, _ = _get(base, "/content/" + "aa" * 32)
        assert status == 404
        status, _, _ = _get(base, "/counter/" + "aa" * 32)
        assert status == 404
    finally:
        httpd.shutdown()


def _seed_delegate_store(data_dir: str) -> Config:
    """A target counter plus one delegate of each §5.5 form, an unresolved
    one, and a delegate-of-a-delegate for the single-hop rule."""
    cfg = Config()
    cfg.data_dir = data_dir
    store = Store(cfg)
    token = "aa" * 32 + "i0"

    def rec(n, asset, body, txid):
        sha = store.store_blob(body)
        store.add_counter(n, CounterRecord(
            asset=asset, asset_id=str(900 + n), asset_longname=None,
            kind="issuance", content_type="text/plain", content_type_raw=None,
            content_sha256=sha, content_length=len(body),
            is_pointer_like=False, mint_txid=txid, msg_index=0,
            block_index=902005 + n, cp_tx_index=n + 1, source="bc1pstored",
            divisible=False, supply=1,
        ))

    rec(0, "TARGET", b"hello-target-bytes", "aa" * 32)
    rec(1, "EDBARE", token.encode(), "b1" * 32)
    rec(2, "EDTAG", f"Delegate: {token}".encode(), "b2" * 32)
    rec(3, "EDJSON",
        json.dumps({"delegate": token, "name": "Ed 3"}).encode(), "b3" * 32)
    rec(4, "EDLOST", ("ee" * 32 + "i0").encode(), "b4" * 32)
    rec(5, "EDHOP", ("b1" * 32 + "i0").encode(), "b5" * 32)  # → the delegate #1
    rec(6, "EDFRAG", f"DELEGATE:{token}#edition-69".encode(), "b6" * 32)
    rec(7, "EDIMG", json.dumps({
        "delegate": token,
        "image": f"https://ordinals.com/content/{token}#edition-7",
    }).encode(), "b7" * 32)
    store.set_last_height(902011, None)
    store.commit()
    store.close()
    return cfg


def test_delegation():
    """§5.5 + rule 9: all three body forms resolve inside the index, one hop,
    display-only — /content keeps the token, /delegate serves the target,
    /preview renders through the hop and ?raw=1 shows the canonical bytes."""
    tmp = tempfile.mkdtemp()
    cfg = _seed_delegate_store(tmp)
    appmod._live_asset = lambda config, asset: {}
    appmod._asset_burned = lambda config, asset: None
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), appmod.Handler)
    httpd.config = cfg
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    token = "aa" * 32 + "i0"
    try:
        # every form resolves to the target in the API record
        for n in (1, 2, 3):
            status, _, body = _get(base, f"/counter/{n}")
            d = json.loads(body)["delegate"]
            assert d == {"id": token, "number": 0, "content_type": "text/plain",
                         "fragment": None}, n
        assert json.loads(_get(base, "/counter/0")[2])["delegate"] is None

        # display fragment: on the reference itself, or inherited from the
        # JSON `image` member (ordinals-marketplace convention)
        d = json.loads(_get(base, "/counter/6")[2])["delegate"]
        assert (d["number"], d["fragment"]) == (0, "edition-69")
        d = json.loads(_get(base, "/counter/7")[2])["delegate"]
        assert (d["number"], d["fragment"]) == (0, "edition-7")

        # /delegate/<n>: the target's bytes; /content/<n>: the token, always
        for n in (1, 2, 3):
            status, _, body = _get(base, f"/delegate/{n}")
            assert (status, body) == (200, b"hello-target-bytes"), n
        assert _get(base, "/content/1")[2] == token.encode()
        status, _, body = _get(base, "/delegate/0")
        assert status == 404 and b"not a delegate" in body

        # unresolved: named event isn't indexed — token shows, nothing serves
        d = json.loads(_get(base, "/counter/4")[2])["delegate"]
        assert d["number"] is None and d["id"] == "ee" * 32 + "i0"
        status, _, body = _get(base, "/delegate/4")
        assert status == 404 and b"not an indexed counter" in body

        # /preview renders through the hop; ?raw=1 is the canonical bytes
        status, _, body = _get(base, "/preview/1")
        assert status == 200 and b"hello-target-bytes" in body
        status, _, body = _get(base, "/preview/1?raw=1")
        assert status == 200 and token.encode() in body
        assert b"hello-target-bytes" not in body

        # single hop: a delegate's delegate resolves ONCE — #5 renders #1's
        # canonical bytes (the aa… token, as text), never the target behind it
        status, _, body = _get(base, "/preview/5")
        assert status == 200 and token.encode() in body
        assert b"hello-target-bytes" not in body
        assert json.loads(_get(base, "/counter/5")[2])["delegate"]["number"] == 1
    finally:
        httpd.shutdown()
