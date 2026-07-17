"""`counters server` orchestration.

By default the command runs the indexer on the main thread AND the HTTP server
on a background thread; `--no-index` (with_index=False) serves only.
"""

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counters.commands import serve  # noqa: E402
from counters.config import Config  # noqa: E402


class _FakeHttpd:
    def __init__(self):
        self.served = threading.Event()
        self.shutdown_called = False
        self.closed = False

    def serve_forever(self):
        self.served.set()
        # Block until shutdown() is called, like the real server.
        while not self.shutdown_called:
            threading.Event().wait(0.01)

    def shutdown(self):
        self.shutdown_called = True

    def server_close(self):
        self.closed = True


class _FakeIndexer:
    last = None

    def __init__(self, config):
        self.ran = False
        self.closed = False
        _FakeIndexer.last = self

    def run(self):
        self.ran = True

    def close(self):
        self.closed = True


def test_server_runs_indexer_and_http_by_default(monkeypatch, capsys):
    httpd = _FakeHttpd()
    monkeypatch.setattr(serve, "make_server", lambda cfg, host, port: httpd)
    monkeypatch.setattr(serve, "Indexer", _FakeIndexer)

    rc = serve.cmd_server(Config(), "127.0.0.1", 8081, with_index=True)

    assert rc == 0
    assert httpd.served.wait(timeout=2), "HTTP server thread never started"
    idx = _FakeIndexer.last
    assert idx.ran and idx.closed, "indexer must run then close"
    assert httpd.shutdown_called and httpd.closed, "HTTP server must be torn down"
    assert "explorer + API on http://127.0.0.1:8081" in capsys.readouterr().out


def test_server_no_index_serves_only(monkeypatch):
    calls = {}

    def fake_run(config, host, port):
        calls["run"] = (host, port)
        return 0

    monkeypatch.setattr(serve, "run", fake_run)
    monkeypatch.setattr(
        serve, "Indexer",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("indexer must not run")),
    )

    rc = serve.cmd_server(Config(), "0.0.0.0", 9000, with_index=False)

    assert rc == 0
    assert calls["run"] == ("0.0.0.0", 9000)
