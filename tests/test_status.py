"""Indexer status messaging: specific backend-down reasons + de-duplication.

Covers the run-loop UX so a transient outage states *why* it's waiting and
does not reprint the identical line on every poll.

Run: python tests/test_status.py   (or via pytest)
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counters.bitcoind import BitcoindError  # noqa: E402
from counters.config import Config  # noqa: E402
from counters.counterparty import CounterpartyError  # noqa: E402
from counters.indexer import Indexer  # noqa: E402
from counters.indexer import indexer as indexer_mod  # noqa: E402
from counters.store import Store  # noqa: E402


def _make_idx(tmp: str):
    cfg = Config()
    cfg.data_dir = tmp
    store = Store(cfg)
    idx = Indexer(cfg, btc=object(), cp=object(), store=store)
    msgs: list[str] = []
    idx._notify = lambda m: msgs.append(m)  # capture instead of printing
    return idx, msgs


def test_wait_reason_classifies_backend_and_kind():
    with tempfile.TemporaryDirectory() as tmp:
        idx, _ = _make_idx(tmp)
        k, m = idx._backend_wait_reason(CounterpartyError("x", kind="unreachable"), "retrying in 15s")
        assert k == "cp-unreachable" and "not listening" in m

        k, m = idx._backend_wait_reason(CounterpartyError("x", kind="timeout"), "retrying in 15s")
        assert k == "cp-timeout" and "not responding" in m

        k, m = idx._backend_wait_reason(BitcoindError("x"), "retrying in 15s")
        assert k == "btc" and "bitcoind" in m.lower()
        idx.close()


def test_status_dedups_until_reason_changes():
    with tempfile.TemporaryDirectory() as tmp:
        idx, msgs = _make_idx(tmp)
        clock = {"t": 1000.0}
        orig = indexer_mod.time.monotonic
        indexer_mod.time.monotonic = lambda: clock["t"]
        try:
            idx._status("cp-unreachable", "down")
            idx._status("cp-unreachable", "down")   # same reason, within window -> suppressed
            assert msgs == ["down"]

            clock["t"] += 61                          # past the 60s repeat window
            idx._status("cp-unreachable", "down")     # reprints with elapsed
            assert len(msgs) == 2 and "still waiting" in msgs[1]

            idx._status("catchup", "catching up")     # reason changed -> immediate
            assert msgs[-1] == "catching up"
        finally:
            indexer_mod.time.monotonic = orig
        idx.close()


def test_status_clear_emits_once_then_noop():
    with tempfile.TemporaryDirectory() as tmp:
        idx, msgs = _make_idx(tmp)
        idx._status("cp-unreachable", "down")
        idx._status_clear("resumed")
        assert msgs == ["down", "resumed"]

        idx._status_clear("resumed")   # nothing active -> no-op (no duplicate)
        assert msgs == ["down", "resumed"]
        idx.close()


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    print(f"\n{'OK' if failures == 0 else f'{failures} FAILED'}")
    raise SystemExit(1 if failures else 0)
