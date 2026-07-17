"""`counters server` — serve the explorer SPA + read-only JSON API, and (by
default) run the indexer alongside it so the explorer stays live.

Thin wrapper: argument parsing/dispatch lives in counters.__main__; the actual
HTTP server (routes, static assets, record shaping) lives in counters.server.
"""

from __future__ import annotations

import logging
import sys
import threading

from ..config import Config
from ..indexer import Indexer
from ..server import make_server, run

log = logging.getLogger("counters")


def cmd_server(config: Config, host: str, port: int, with_index: bool = True) -> int:
    # Serve-only: the HTTP server blocks the main thread and owns Ctrl+C.
    if not with_index:
        return run(config, host=host, port=port)

    # Index + serve: the indexer owns the main thread (it installs the SIGINT
    # handler and drives the live progress bar), while the HTTP server runs on a
    # background daemon thread. They share the SQLite file via WAL; the server
    # opens its own read connection per request, so nothing is shared unsafely.
    httpd = make_server(config, host, port)
    server_thread = threading.Thread(
        target=httpd.serve_forever, name="counters-http", daemon=True
    )
    server_thread.start()
    print(f"counters explorer + API on http://{host}:{port}  (Ctrl+C to stop)")

    indexer = Indexer(config)
    try:
        indexer.run()
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
    finally:
        httpd.shutdown()
        httpd.server_close()
        indexer.close()
    return 0
