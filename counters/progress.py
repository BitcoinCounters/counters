"""A small, dependency-free progress bar styled after the `ord` indexer.

Renders a single self-updating line to stderr when attached to a TTY, e.g.:

    Indexing ███████████████████░░░░░░░░░░░ 595123/957412 · 0 counters

When stderr is not a TTY (piped/redirected) it degrades to nothing on the bar
itself; callers should still emit periodic log lines in that case.
"""

from __future__ import annotations

import shutil
import sys
import time
from typing import TextIO


class ProgressBar:
    def __init__(
        self,
        total: int,
        desc: str = "Indexing",
        width: int = 30,
        stream: TextIO | None = None,
        min_interval: float = 0.1,
        initial: int = 0,
    ) -> None:
        self.total = max(total, 0)
        self.desc = desc
        self.width = width
        self.stream = stream or sys.stderr
        self.min_interval = min_interval
        self.enabled = bool(getattr(self.stream, "isatty", lambda: False)())
        self.start = time.monotonic()
        self._last_render = 0.0
        # `initial` = absolute position already completed before this session
        # (e.g. blocks indexed in a previous run). The displayed position `n`
        # is absolute so it never resets on resume, while rate/ETA are based on
        # work done THIS session (n - initial).
        self.initial = max(initial, 0)
        self.n = self.initial
        self.postfix = ""
        # Persistent status lines (e.g. backend heights) shown ABOVE the bar,
        # one per line, and redrawn in place via set_status_lines(); they never
        # scroll. `_drawn` is how many terminal lines the current block (status
        # lines + bar) occupies, so the next render can move up and overwrite it.
        self.status_lines: list[str] = []
        self._drawn = 0

    def update(self, n: int, postfix: str = "") -> None:
        self.n = n
        if postfix:
            self.postfix = postfix
        if not self.enabled:
            return
        now = time.monotonic()
        if now - self._last_render < self.min_interval and n < self.total:
            return
        self._last_render = now
        self._render()

    def _bar_line(self) -> str:
        total = self.total or 1
        frac = min(max(self.n / total, 0.0), 1.0)
        # The info section is always the position, with the caller's postfix
        # (e.g. the counter count) appended after it.
        info = f"{self.n}/{self.total}"
        if self.postfix:
            info = f"{info} · {self.postfix}"
        head = self.desc
        # Fit the line to the terminal so the info tail is never cut off:
        # shrink the bar first, then drop it, then drop the description.
        # Wrapping/truncating mid-info would defeat the in-place redraw.
        cols = shutil.get_terminal_size(fallback=(80, 24)).columns
        width = min(self.width, cols - 1 - len(head) - len("  ") - len(info))
        if width >= 8:
            filled = int(round(width * frac))
            bar = "█" * filled + "░" * (width - filled)
            line = f"{head} {bar} {info}"
        elif len(head) + 1 + len(info) < cols:
            line = f"{head} {info}"
        else:
            line = info
        if len(line) > cols:  # last resort on a very narrow terminal
            line = line[: max(cols - 1, 0)]
        return line

    def _erase_block(self) -> None:
        """Move the cursor to the top-left of the currently drawn block and clear
        from there to the end of the screen, so the caller can redraw (or emit a
        permanent line) without leaving a stale copy behind."""
        if self._drawn > 1:
            self.stream.write(f"\033[{self._drawn - 1}A")
        self.stream.write("\r\033[J")
        self._drawn = 0

    def _render(self) -> None:
        if not self.enabled:
            return
        # Truncate the status lines to the terminal width: a wrapped status line
        # would occupy two rows and throw off the cursor-up math on redraw.
        cols = shutil.get_terminal_size(fallback=(80, 24)).columns
        lines = [s[: max(cols - 1, 0)] for s in self.status_lines]
        lines.append(self._bar_line())
        # Overwrite the previous block in place: the status lines sit ABOVE the
        # bar, each on its own line, and the whole block is redrawn without
        # scrolling so a moving chain tip updates the same rows every poll.
        self._erase_block()
        self.stream.write("\n".join(lines))
        self.stream.flush()
        self._drawn = len(lines)

    def set_status_lines(self, lines: list[str]) -> None:
        """Set the persistent status lines shown above the bar and redraw them in
        place. Unlike write(), this never scrolls, so values that change every
        poll (e.g. the backend heights) update the same rows instead of leaving
        a trail."""
        lines = list(lines)
        if lines == self.status_lines:
            return
        self.status_lines = lines
        if self.enabled:
            self._render()

    def write(self, msg: str) -> None:
        """Print a message as permanent scrollback above the live block."""
        if self.enabled:
            self._erase_block()
            self.stream.write(msg + "\n")
            self._render()
        else:
            self.stream.write(msg + "\n")
            self.stream.flush()

    def close(self) -> None:
        if self.enabled:
            self._render()
            self.stream.write("\n")
            self.stream.flush()
