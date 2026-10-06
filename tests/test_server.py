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
        # body=0: the same records without the inlined text (the grid's form)
        lean = json.loads(_get(base, "/counters?limit=5&body=0")[2])["counters"]
        assert [r["number"] for r in lean] == [r["number"] for r in data["counters"]]
        assert all(r["body"] is None for r in lean)
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
        # Previews come from this origin unless a second one is configured.
        assert st["preview_origin"] is None
        httpd.config.preview_origin = "https://frames.example"
        assert json.loads(_get(base, "/status")[2])["preview_origin"] == "https://frames.example"
        httpd.config.preview_origin = ""

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
    endpoints accept one wherever they accept a number. The index is
    load-bearing on the byte endpoints (/content, /preview, /stamp): a bare
    txid matches no route there. /counter/ and /c/ are where a reader
    types, so a bare txid resolves to the transaction's first event."""
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
        # a bare txid matches no byte route; on /counter/ it is the first event
        status, _, _ = _get(base, "/content/" + "aa" * 32)
        assert status == 404
        status, _, body = _get(base, "/counter/" + "aa" * 32)
        assert status == 200 and json.loads(body)["number"] == 0
        status, _, _ = _get(base, "/counter/" + "ee" * 32)      # a txid that minted nothing
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
                         "size": len(b"hello-target-bytes"), "fragment": None}, n
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


def test_preview_origin_is_a_bare_origin():
    """COUNTER_PREVIEW_ORIGIN lands in every preview frame's src, so only a
    bare origin is accepted — anything else is dropped, never repaired."""
    import os
    from counters.config import Config

    def read(value):
        os.environ["COUNTER_PREVIEW_ORIGIN"] = value
        try:
            return Config().preview_origin
        finally:
            del os.environ["COUNTER_PREVIEW_ORIGIN"]

    assert read("https://frames.example") == "https://frames.example"
    assert read("https://frames.example/") == "https://frames.example"
    assert read("http://localhost:8099") == "http://localhost:8099"
    for bad in ("frames.example", "https://frames.example/path",
                "https://frames.example?x=1", 'https://a.example" onload="x',
                "javascript:alert(1)", "https://a.example b.example", ""):
        assert read(bad) == "", bad
    assert Config().preview_origin == ""


def _reveal(ord_style: bool) -> dict:
    """A bitcoind-verbose taproot reveal whose tapscript opens the way each
    envelope style does (reveal.envelope_style reads ops 2 and 3)."""
    script = (b"\x00\x63\x03ord\x01\x07\x03xcp" if ord_style
              else b"\x00\x63\x04data\x04more")
    return {
        "vout": [{"scriptPubKey": {"hex": "6a08434e545250525459"}}],
        "vin": [{"txid": "ff" * 32,
                 "txinwitness": ["00" * 64, script.hex(), "c0" + "00" * 32]}],
    }


def test_lists_carry_the_cached_envelope_and_original():
    """A list never calls bitcoind: it carries the envelope verdict the
    background pass cached (null while unknown), and marks reinscriptions."""
    from counters.reveal import ENVELOPE_VERSION

    httpd, base = _run_server()
    try:
        def listed():
            recs = json.loads(_get(base, "/counters?limit=5&body=0")[2])["counters"]
            return {r["number"]: r for r in recs}

        recs = listed()
        assert [recs[n]["envelope"] for n in (0, 1, 2)] == [None, None, None]
        # N6: #1 is a later event on #0's asset
        assert [recs[n]["original"] for n in (0, 1, 2)] == [True, False, True]

        class FakeBtc:
            def __init__(self):
                self.asked = []

            def get_raw_transaction(self, txid, verbose=True):
                self.asked.append(txid)
                if txid == "cc" * 32:
                    raise OSError("node down")
                return _reveal(ord_style=(txid == "aa" * 32))

        # A failure stops the pass where it is; what was classified is kept.
        btc = FakeBtc()
        try:
            appmod.warm_envelopes_once(httpd.config, btc=btc)
        except OSError:
            pass
        assert btc.asked == ["aa" * 32, "bb" * 32, "cc" * 32]
        recs = listed()
        assert recs[0]["envelope"] == "counterparty/ord"
        assert recs[1]["envelope"] == "counterparty"
        assert recs[2]["envelope"] is None

        # The next pass asks only for what is still unknown.
        class Healthy(FakeBtc):
            def get_raw_transaction(self, txid, verbose=True):
                self.asked.append(txid)
                return _reveal(ord_style=False)

        btc = Healthy()
        assert appmod.warm_envelopes_once(httpd.config, btc=btc) == 1
        assert btc.asked == ["cc" * 32]
        assert listed()[2]["envelope"] == "counterparty"
        assert appmod.warm_envelopes_once(httpd.config, btc=Healthy()) == 0

        # A verdict from an older version of the rule is not trusted.
        store = Store(httpd.config)
        try:
            assert store.get_envelope("aa" * 32, ENVELOPE_VERSION) == "counterparty/ord"
            assert store.get_envelope("aa" * 32, ENVELOPE_VERSION + 1) is None
            assert len(store.reveals_without_envelope(ENVELOPE_VERSION + 1)) == 3
        finally:
            store.close()
    finally:
        httpd.shutdown()


def test_lists_carry_the_cached_supply_lock():
    """`locked` in a list is what Counterparty last said, kept by the
    background pass: null until checked, re-asked soon while unlocked (an
    owner can lock at any moment), rarely once locked."""
    httpd, base = _run_server()
    try:
        def locks():
            recs = json.loads(_get(base, "/counters?limit=5&body=0")[2])["counters"]
            return {r["number"]: r["locked"] for r in recs}

        assert locks() == {0: None, 1: None, 2: None}

        class FakeCp:
            def __init__(self, state):
                self.state, self.asked = state, []

            def get_asset(self, asset):
                self.asked.append(asset)
                return {"locked": self.state[asset]} if asset in self.state else None

        cp = FakeCp({"TESTASSET": False, "STAMPTEST": True})
        assert appmod.warm_locks_once(httpd.config, cp=cp, now=1000) == 2
        assert sorted(cp.asked) == ["STAMPTEST", "TESTASSET"]   # once per ASSET
        assert locks() == {0: False, 1: False, 2: True}          # siblings share it

        # Fresh answers are not re-asked...
        cp = FakeCp({"TESTASSET": True, "STAMPTEST": True})
        assert appmod.warm_locks_once(httpd.config, cp=cp, now=1000 + 60) == 0
        # ...an unlocked asset goes stale quickly, and its lock is picked up...
        assert appmod.warm_locks_once(
            httpd.config, cp=cp, now=1000 + appmod.UNLOCKED_MAX_AGE) == 1
        assert cp.asked == ["TESTASSET"]
        assert locks() == {0: True, 1: True, 2: True}
        # ...and a locked one only after a long while.
        cp = FakeCp({"TESTASSET": True, "STAMPTEST": True})
        assert appmod.warm_locks_once(
            httpd.config, cp=cp, now=1000 + appmod.LOCKED_MAX_AGE) == 1
        assert cp.asked == ["STAMPTEST"]

        # An asset Core does not know stays unknown rather than guessed.
        store = Store(httpd.config)
        try:
            store.db.execute("DELETE FROM asset_locks")
            store.db.commit()
        finally:
            store.close()
        assert appmod.warm_locks_once(httpd.config, cp=FakeCp({}), now=5000) == 0
        assert locks() == {0: None, 1: None, 2: None}
    finally:
        httpd.shutdown()


def test_facets_is_the_whole_index_in_one_response():
    """/facets carries every counter, oldest first, as the fields a card and
    a filter need — cached until the index or a tag cache changes."""
    import gzip
    import urllib.request
    from counters.reveal import ENVELOPE_VERSION

    httpd, base = _run_server()
    try:
        def fetch(headers=None):
            req = urllib.request.Request(base + "/facets", headers=headers or {})
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status, dict(r.headers), r.read()
            except urllib.error.HTTPError as e:
                return e.code, dict(e.headers), e.read()

        status, headers, body = fetch()
        assert status == 200 and "application/json" in headers["Content-Type"]
        data = json.loads(body)
        assert data["count"] == 3 and data["fields"] == list(appmod.FACET_FIELDS)
        recs = [dict(zip(data["fields"], row)) for row in data["rows"]]
        assert [r["number"] for r in recs] == [0, 1, 2]           # oldest first
        assert recs[0]["asset"] == "TESTASSET" and recs[0]["size"] == 2
        assert [r["original"] for r in recs] == [True, False, True]
        assert recs[2]["stamp_mime"] == "image/gif"
        assert all(r["envelope"] is None and r["locked"] is None for r in recs)
        assert all(r["delegate"] is None for r in recs)

        # Unchanged index: the same ETag, and a conditional request is a 304.
        etag = headers["ETag"]
        status, headers2, body2 = fetch({"If-None-Match": etag})
        assert status == 304 and body2 == b"" and headers2["ETag"] == etag

        # gzip when the client takes it; the same JSON underneath.
        status, headers3, zipped = fetch({"Accept-Encoding": "gzip"})
        assert headers3["Content-Encoding"] == "gzip"
        assert gzip.decompress(zipped) == body

        # A tag cache filling in changes the index, so the ETag moves.
        store = Store(httpd.config)
        try:
            store.set_envelope("aa" * 32, "counterparty", ENVELOPE_VERSION)
            store.set_locked("TESTASSET", False, 1000)
        finally:
            store.close()
        status, headers4, body4 = fetch({"If-None-Match": etag})
        assert status == 200 and headers4["ETag"] != etag
        recs = [dict(zip(data["fields"], row)) for row in json.loads(body4)["rows"]]
        assert recs[0]["envelope"] == "counterparty" and recs[1]["envelope"] is None
        assert [r["locked"] for r in recs] == [False, False, None]
    finally:
        httpd.shutdown()


def test_facets_carries_a_delegate_as_its_target():
    tmp = tempfile.mkdtemp()
    cfg = _seed_delegate_store(tmp)
    store = Store(cfg)
    try:
        data = json.loads(appmod.build_facets(store)[1])
    finally:
        store.close()
    recs = {row[0]: dict(zip(data["fields"], row)) for row in data["rows"]}
    assert recs[0]["delegate"] is None
    # [target number, target type, target size, display fragment]
    assert recs[1]["delegate"] == [0, "text/plain", len(b"hello-target-bytes"), None]
    assert recs[6]["delegate"][0] == 0 and recs[6]["delegate"][3] == "edition-69"
    assert recs[4]["delegate"][:3] == [None, None, None]      # unresolved


def test_a_file_is_sandboxed_wherever_it_is_opened():
    """/content, /stamp, /delegate and a raw /preview carry the `sandbox`
    directive, so a counter followed as a plain link is confined as it is in
    the explorer's frame — except a PDF, which a sandbox would blank."""
    import urllib.request

    def csp(base, path):
        with urllib.request.urlopen(base + path, timeout=5) as r:
            return r.headers.get_all("Content-Security-Policy") or []

    httpd, base = _run_server()
    try:
        for path in ("/content/0", "/content/" + "aa" * 32 + "i0", "/stamp/2"):
            policies = csp(base, path)
            assert "sandbox allow-scripts" in policies, path
            # the confining policies it joins are still there
            assert any(p.startswith("default-src 'self'") for p in policies), path
    finally:
        httpd.shutdown()

    tmp = tempfile.mkdtemp()
    cfg = _seed_delegate_store(tmp)
    store = Store(cfg)
    try:
        def add(number, asset, body, ctype, txid):
            store.add_counter(number, CounterRecord(
                asset=asset, asset_id=str(900 + number), asset_longname=None,
                kind="issuance", content_type=ctype, content_type_raw=None,
                content_sha256=store.store_blob(body), content_length=len(body),
                is_pointer_like=False, mint_txid=txid, msg_index=0,
                block_index=902100 + number, cp_tx_index=100 + number,
                source="bc1pstored", divisible=False, supply=1))
        n = store.count()
        add(n, "LIVEPAGE", b"<!doctype html><script>1</script>", "text/html", "d1" * 32)
        add(n + 1, "ABOOK", b"%PDF-1.4 tiny", "application/pdf", "d2" * 32)
        store.commit()
    finally:
        store.close()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), appmod.Handler)
    httpd.config = cfg
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        assert "sandbox allow-scripts" in csp(base, f"/content/{n}")      # html
        assert "sandbox allow-scripts" in csp(base, f"/preview/{n}")      # the raw document
        assert "sandbox allow-scripts" in csp(base, "/delegate/1")        # a delegate's target
        assert "sandbox allow-scripts" not in csp(base, f"/content/{n + 1}")   # pdf
    finally:
        httpd.shutdown()
