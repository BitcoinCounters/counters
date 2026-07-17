"""Indexer status messaging: a transient outage states *why* it's waiting on
the affected backend's height line, redrawn in place, and never scrolls a fresh
message every poll.

Run: python tests/test_status.py   (or via pytest)
"""

from __future__ import annotations

import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counters.bitcoind import BitcoindError  # noqa: E402
from counters.config import Config  # noqa: E402
from counters.counterparty import CounterpartyError  # noqa: E402
from counters.indexer import Indexer  # noqa: E402
from counters.progress import ProgressBar  # noqa: E402
from counters.store import Store  # noqa: E402


class _FakeTTY(io.StringIO):
    """A StringIO that claims to be a TTY so ProgressBar renders in place."""

    def isatty(self) -> bool:
        return True


def _make_idx(tmp: str):
    cfg = Config()
    cfg.data_dir = tmp
    store = Store(cfg)
    idx = Indexer(cfg, btc=object(), cp=object(), store=store)
    msgs: list[str] = []
    idx._notify = lambda m: msgs.append(m)  # capture instead of printing
    return idx, msgs


def test_wait_note_reflects_backend_and_kind():
    """A backend failure annotates ITS height line with a concise reason instead
    of scrolling a message; the kind (starting up / busy / error) is preserved."""
    with tempfile.TemporaryDirectory() as tmp:
        idx, _ = _make_idx(tmp)
        idx._btc_tip, idx._cp_tip = 957_090, None

        # Counterparty API not up yet -> reason on the counterparty line.
        idx._cp_down = True
        idx._set_wait_note(CounterpartyError("x", kind="unreachable"))
        assert idx._cp_note == "API not up yet — server starting/migrating · retrying"
        assert idx._height_lines() == [
            "bitcoin - 957090",
            "counterparty - API not up yet — server starting/migrating · retrying",
        ]

        idx._set_wait_note(CounterpartyError("x", kind="timeout"))
        assert idx._cp_note == "not responding — server busy · retrying"
        idx._set_wait_note(CounterpartyError("x"))   # generic
        assert idx._cp_note == "API error · retrying"

        # bitcoind down annotates the bitcoin line.
        idx._btc_down = True
        idx._set_wait_note(BitcoindError("x"))
        assert idx._btc_note == "Core RPC unreachable — is bitcoind running? · retrying"
        assert idx._height_lines()[0] == "bitcoin - Core RPC unreachable — is bitcoind running? · retrying"
        idx.close()


def test_wait_note_clears_on_recovery_without_scrolling():
    """Once a backend recovers the note disappears and the line shows the plain
    height; the transient status is never emitted as scrollback history."""
    with tempfile.TemporaryDirectory() as tmp:
        idx, msgs = _make_idx(tmp)
        idx._btc_tip, idx._cp_tip = 957_090, 957_063
        idx._cp_down = True
        idx._set_wait_note(CounterpartyError("x", kind="unreachable"))
        assert "API not up yet" in idx._height_lines()[1]

        # Recovery mirrors the run loop: clear the note + down flag on success.
        idx._cp_down = False
        idx._cp_note = None
        assert idx._height_lines() == [
            "bitcoin - 957090",
            "counterparty - 957063/957090 · catching up",   # trailing bitcoind, in place
        ]
        # Nothing was ever pushed to scrollback via _notify.
        assert msgs == []
        idx.close()


def test_heights_update_in_place_on_tty_no_scroll():
    """On a TTY a moving bitcoind tip updates the height rows IN PLACE: the
    heights stay on their own lines above the bar, nothing scrolls (bar.write
    is never used), and the persistent status holds only the newest tip."""
    idx = Indexer.__new__(Indexer)
    idx._btc_down = idx._cp_down = False
    idx._cp_tip = None  # Counterparty down/unknown while bitcoind advances
    idx._shown_heights = []
    idx._cp_down = True

    stream = _FakeTTY()
    bar = ProgressBar(1000, stream=stream)
    assert bar.enabled  # our fake stream is a "TTY"

    scrolls: list[str] = []
    bar.write = lambda msg: scrolls.append(msg)  # the scroll path must be unused

    for tip in (957_457, 957_527, 957_577):
        idx._btc_tip = tip
        idx._show_heights(bar)

    # No scrolling: the heights were redrawn in place, never written as history.
    assert scrolls == []
    # bitcoin and counterparty stay on SEPARATE lines, showing only the latest.
    assert bar.status_lines == ["bitcoin - 957577", "counterparty - down"]
    # An in-place redraw uses cursor-movement escapes (never plain scrollback).
    assert "\033[" in stream.getvalue()


def test_heights_scroll_only_on_change_when_not_tty():
    """Piped to a log (no TTY): height lines print only when they change, not
    on every poll, so a static backend state does not repeat."""
    idx = Indexer.__new__(Indexer)
    idx._btc_down = idx._cp_down = False
    idx._cp_down = True
    idx._cp_tip = None
    idx._shown_heights = []

    stream = io.StringIO()  # not a TTY
    bar = ProgressBar(1000, stream=stream)
    assert not bar.enabled

    idx._btc_tip = 957_457
    idx._show_heights(bar)
    idx._show_heights(bar)  # unchanged -> must NOT reprint
    assert stream.getvalue().count("bitcoin - 957457") == 1

    idx._btc_tip = 957_527  # changed -> reprints once
    idx._show_heights(bar)
    assert stream.getvalue().count("bitcoin - 957527") == 1


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
