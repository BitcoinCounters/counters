"""Tests for the PDF preview and the byte-range serving it depends on.

A PDF is the one media kind with no native element to hand the bytes to — the
browser's own viewer is a plugin, and the preview frames forbid plugins — so it
is rendered by the vendored pdf.js. These cover the parts the server owns: the
wrapper it emits, the policy that wrapper runs under, the vendored assets being
reachable, and `Range` on /content, which is what keeps a whole-block PDF from
being downloaded in full just to show its first page.
"""

from __future__ import annotations

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
from counters.server import preview  # noqa: E402
from counters.store import CounterRecord, Store  # noqa: E402


def _minimal_pdf() -> bytes:
    """A valid, one-page PDF — small enough to keep the test honest about
    ranges (every offset below is inside it)."""
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += (b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objs) + 1, xref))
    return bytes(out)


PDF = _minimal_pdf()


def _seed(data_dir: str) -> Config:
    cfg = Config()
    cfg.data_dir = data_dir
    store = Store(cfg)
    sha = store.store_blob(PDF)
    store.add_counter(
        0,
        CounterRecord(
            asset="PDFTEST", asset_id="123", asset_longname=None,
            kind="issuance", content_type="application/pdf", content_type_raw=None,
            content_sha256=sha, content_length=len(PDF), is_pointer_like=False,
            mint_txid="aa" * 32, msg_index=0, block_index=902005,
            cp_tx_index=1, source="bc1pstored", divisible=False, supply=1,
        ),
    )
    store.set_last_height(902005, None)
    store.commit()
    store.close()
    return cfg


def _run_server():
    cfg = _seed(tempfile.mkdtemp())
    appmod._live_asset = lambda config, asset: {}
    appmod._asset_burned = lambda config, asset: None
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), appmod.Handler)
    httpd.config = cfg
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def _get(base: str, path: str, headers: dict | None = None):
    req = urllib.request.Request(base + path, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_classify_pdf():
    assert preview.classify("application/pdf") == ("pdf", None)
    assert preview.classify("application/pdf; charset=binary") == ("pdf", None)


def test_pdf_preview_wrapper_and_policy():
    httpd, base = _run_server()
    try:
        status, headers, body = _get(base, "/preview/0")
        assert status == 200
        page = body.decode()

        # The viewer's container, and both scripts it needs — the vendored
        # library and our own renderer, as classic scripts.
        assert "id=pdfdoc" in page and "data-src=/content/0" in page
        assert "<script src=/pdfjs.min.js></script>" in page
        assert "<script src=/preview-pdf.js></script>" in page

        # pdf.js runs its decoder in a worker it builds as a blob, so the policy
        # has to permit blob workers and blob scripts — and nothing off-site.
        csp = headers["Content-Security-Policy"]
        directives = {
            d.strip().split(" ", 1)[0]: d.strip().split(" ", 1)[1]
            for d in csp.split(";") if d.strip() and " " in d.strip()
        }
        assert directives["default-src"] == "'self'"
        assert "blob:" in directives["script-src"]
        assert "blob:" in directives["worker-src"]
        # Everything the viewer needs is vendored, so the policy names no host.
        assert "http://" not in csp and "https://" not in csp
    finally:
        httpd.shutdown()


def test_vendored_assets_are_served():
    httpd, base = _run_server()
    try:
        for path, want_type in (
            ("/pdfjs.min.js", "text/javascript"),
            ("/pdfjs.worker.min.js", "text/javascript"),
            ("/preview-pdf.js", "text/javascript"),
            # The standard 14 fonts, for PDFs that name a base font rather than
            # embedding one — without these such a file renders with no text.
            ("/pdfjs-standard-fonts/FoxitSerif.pfb", "application/x-font-type1"),
            ("/pdfjs-standard-fonts/LiberationSans-Regular.ttf", "font/ttf"),
        ):
            status, headers, body = _get(base, path)
            assert status == 200, path
            assert want_type in headers["Content-Type"], path
            assert body, path
    finally:
        httpd.shutdown()


def test_content_serves_byte_ranges():
    httpd, base = _run_server()
    try:
        # Whole file: still advertises that ranges are available.
        status, headers, body = _get(base, "/content/0")
        assert status == 200
        assert headers["Accept-Ranges"] == "bytes"
        assert body == PDF
        # A scripted preview reads these from an opaque-origin fetch, so they
        # have to be exposed across origins or pdf.js cannot page the file.
        assert "Content-Range" in headers["Access-Control-Expose-Headers"]

        # A span.
        status, headers, body = _get(base, "/content/0", {"Range": "bytes=10-19"})
        assert status == 206
        assert headers["Content-Range"] == f"bytes 10-19/{len(PDF)}"
        assert body == PDF[10:20]

        # Open-ended, and the suffix form the trailer read uses.
        status, headers, body = _get(base, "/content/0", {"Range": "bytes=10-"})
        assert status == 206 and body == PDF[10:]
        status, headers, body = _get(base, "/content/0", {"Range": "bytes=-20"})
        assert status == 206 and body == PDF[-20:]

        # An end past the file is clamped, not refused.
        status, headers, body = _get(base, "/content/0",
                                     {"Range": f"bytes=5-{len(PDF) + 500}"})
        assert status == 206 and body == PDF[5:]

        # Anything we do not honour falls back to the whole file, which is
        # always a valid answer to a range request.
        for bad in ("bytes=abc-def", "items=0-10", "bytes=0-5,10-20", "bytes=99999-"):
            status, _, body = _get(base, "/content/0", {"Range": bad})
            assert status == 200 and body == PDF, bad
    finally:
        httpd.shutdown()
