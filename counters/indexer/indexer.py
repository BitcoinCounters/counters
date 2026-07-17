"""The indexer pipeline (build reference v3 §8).

Oracle-first: for each block (ascending), ask Counterparty for the block's
issuances and fairminter deploys, filter them to the qualifying events
(R1-R3), verify each survivor's transaction is a taproot REVEAL against
bitcoind (R4), then number and store in (block, tx_index, msg_index) order.

Validity rules enforced here:
  R1  valid Counterparty state only (issuance status == "valid"; a fairminter
      deploy's presence in the fairminters table IS its validity)
  R2  issuances + fairminter deploys; fairmints (fair_minting) excluded
  R3  non-null, non-empty description — content defers to Counterparty state
  R4  taproot envelope carrier only (reveal.py)
  R5  permissive content — MIME never gates validity
  R6  duplicates allowed; dedup is metadata-only
  N1-N6 numbering per the build reference; N4 reorgs are handled by a
      log-structured rollback before each sync pass.
"""

from __future__ import annotations

import logging
import signal
import time

from ..bitcoind import BitcoindClient, BitcoindError
from ..config import GENESIS_HEIGHT, Config
from ..content import classify_mime_type, content_bytes, is_pointer_like, normalize_mime
from ..counterparty import CounterpartyClient, CounterpartyError
from ..progress import ProgressBar
from ..reveal import is_taproot_reveal
from ..store import CounterRecord, Store

log = logging.getLogger("counters")


def is_qualifying_issuance(row: dict) -> bool:
    """R1+R2 for an issuance row: valid per Counterparty, and not a fairmint
    (mints carry no content; the collection's counter lands on the deploy).
    Shared by the indexer and `counters validate` so the CLI verdict is
    definitionally the indexer's verdict."""
    return row.get("status") == "valid" and not row.get("fair_minting")


def has_content(row: dict) -> bool:
    """R3: non-null, non-empty description (1 byte qualifies)."""
    return row.get("description") not in (None, "")


class Indexer:
    def __init__(self, config: Config, btc=None, cp=None, store=None):
        # Clients are injectable for testing; default to real implementations.
        self.config = config
        self.btc = btc if btc is not None else BitcoindClient(config)
        self.cp = cp if cp is not None else CounterpartyClient(config)
        self.store = store if store is not None else Store(config)
        self._progress: ProgressBar | None = None
        self._stop = False  # set by SIGINT for graceful shutdown
        # Latest heights seen by _target_tip(), so run() can tell "caught up"
        # from "waiting for the oracle to catch up".
        self._btc_tip: int | None = None
        self._cp_tip: int | None = None
        # Whether the last poll of each backend failed, so the height lines
        # can say "down" instead of silently showing a stale height.
        self._btc_down = False
        self._cp_down = False
        # Non-TTY only: last height lines logged, so a piped log reprints them
        # on change instead of every poll (a TTY updates them in place instead).
        self._shown_heights: list[str] = []
        # Concise, in-place status note for a backend that is currently
        # unavailable (e.g. "starting up · retrying"), shown ON its height line
        # and redrawn in place rather than scrolling a fresh message each poll.
        # None while the backend is healthy.
        self._btc_note: str | None = None
        self._cp_note: str | None = None

    # --- signal handling ---------------------------------------------------

    def _install_signal_handler(self) -> None:
        """First Ctrl+C requests a graceful stop (finish current block + save);
        a second Ctrl+C forces an immediate exit."""

        def handler(signum, frame):
            if self._stop:
                # Second interrupt: restore default and abort now.
                signal.signal(signal.SIGINT, signal.SIG_DFL)
                self._notify("forced exit")
                raise KeyboardInterrupt
            self._stop = True
            self._notify("shutting down after current block… (Ctrl+C again to force)")

        signal.signal(signal.SIGINT, handler)

    def _interruptible_sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while not self._stop:
            remaining = end - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.2, remaining))

    def _notify(self, msg: str) -> None:
        """Emit an important message, printing above the progress bar if active."""
        if self._progress is not None and self._progress.enabled:
            self._progress.write(msg)
        else:
            log.info(msg)

    def _set_wait_note(self, err: Exception) -> None:
        """Reflect WHY a backend is unavailable as a concise note on its height
        line, tailored to the failure. Shown in place (redrawn each poll) rather
        than scrolling a fresh 'waiting' message. _target_tip() has already set
        the matching down flag; here we only annotate the reason."""
        if isinstance(err, CounterpartyError):
            kind = getattr(err, "kind", "error")
            self._cp_note = {
                # API comes online only after startup DB migrations finish.
                "unreachable": "API not up yet — server starting/migrating · retrying",
                # Busy applying migrations or catching up.
                "timeout": "not responding — server busy · retrying",
            }.get(kind, "API error · retrying")
        else:  # BitcoindError (or other backend RPC failure)
            self._btc_note = "Core RPC unreachable — is bitcoind running? · retrying"

    def close(self) -> None:
        self.store.close()

    # --- candidate selection (R1-R3) ----------------------------------------

    @staticmethod
    def _qualifying_events(issuances: list[dict], fairminters: list[dict]) -> list[tuple[str, dict]]:
        """Filter the block's Counterparty messages down to qualifying events
        and return them in N1 order: (tx_index, msg_index). Deduplication by
        (tx_hash, msg_index) keeps the first occurrence."""
        candidates: list[tuple[str, dict]] = []
        for row in issuances:
            if is_qualifying_issuance(row) and has_content(row):  # R1-R3
                candidates.append(("issuance", row))
        for row in fairminters:
            if has_content(row):  # R3 (presence in the table IS validity, R1)
                candidates.append(("fairminter", row))

        seen: set[tuple[str, int]] = set()
        unique: list[tuple[str, dict]] = []
        for kind, row in candidates:
            key = (row.get("tx_hash"), int(row.get("msg_index") or 0))
            if key in seen:
                continue
            seen.add(key)
            unique.append((kind, row))

        # tx_index is consensus ordering input (N1): a missing value must fail
        # loudly (KeyError aborts the pass for a retry), never default to 0.
        unique.sort(
            key=lambda kr: (int(kr[1]["tx_index"]), int(kr[1].get("msg_index") or 0))
        )
        return unique

    # --- block processing ----------------------------------------------------

    def process_block(self, height: int) -> int:
        """Process a single block; returns the number of counters recorded."""
        block_hash = self.btc.get_block_hash(height)

        issuances = self.cp.get_block_issuances(height)
        fairminters = self.cp.get_block_fairminters(height)
        candidates = self._qualifying_events(issuances, fairminters)

        recorded = 0
        for kind, row in candidates:
            if self._maybe_record(height, kind, row):
                recorded += 1

        self.store.set_last_height(height, block_hash)
        self.store.commit()
        return recorded

    def _maybe_record(self, height: int, kind: str, row: dict) -> bool:
        txid = row.get("tx_hash")
        msg_index = int(row.get("msg_index") or 0)
        if not txid or self.store.has_event(txid, msg_index):
            return False

        # R4: the message's transaction must be a taproot REVEAL. The raw tx
        # comes from bitcoind (txindex=1); content is NOT read from it. A
        # fetch failure must ABORT the block (the run loop retries it):
        # skipping the event while the cursor advances would permanently fork
        # this indexer's numbering and rolling hash.
        tx = self.btc.get_raw_transaction(txid, verbose=True)
        if not is_taproot_reveal(tx):
            log.debug("block %d tx %s: %s description not taproot-carried -> no event",
                      height, txid, kind)
            return False

        # Content: invert Core's description serialization at this height (§5.1).
        description = row.get("description") or ""
        mime_raw = row.get("mime_type")
        body, clean = content_bytes(description, mime_raw or "text/plain", height)
        if not clean:
            log.warning("block %d tx %s: claimed-binary description is not valid hex; "
                        "stored UTF-8 fallback bytes", height, txid)
        content_type, content_type_raw = normalize_mime(mime_raw or "text/plain")

        asset = row.get("asset")
        asset_info = self.cp.get_asset(asset) or {}

        # Inscription cost (commit + reveal) is enrichment, never a blocker.
        try:
            fee, tx_size = self.btc.get_inscription_cost(txid, reveal_tx=tx)
        except (BitcoindError, KeyError, IndexError, TypeError):
            fee, tx_size = None, None

        number = self.store.next_number()
        sha = self.store.store_blob(body)
        rec = CounterRecord(
            asset=asset,
            asset_id=(str(asset_info["asset_id"]) if asset_info.get("asset_id") is not None else None),
            asset_longname=row.get("asset_longname") or asset_info.get("asset_longname"),
            kind=kind,
            content_type=content_type,
            content_type_raw=content_type_raw,
            content_sha256=sha,
            content_length=len(body),
            # Textual per the CONSENSUS classifier (the one that decoded the
            # bytes), not the display prefix: application/json etc. count.
            is_pointer_like=is_pointer_like(
                body,
                textual=classify_mime_type(mime_raw or "text/plain", height) == "text",
            ),
            mint_txid=txid,
            msg_index=msg_index,
            block_index=height,
            cp_tx_index=int(row["tx_index"]),
            source=row.get("issuer") or row.get("source"),
            divisible=(bool(row["divisible"]) if row.get("divisible") is not None
                       else asset_info.get("divisible")),
            supply=asset_info.get("supply"),
            fee=fee,
            tx_size=tx_size,
            xcp_burned=row.get("fee_paid"),
        )
        self.store.add_counter(number, rec)
        self._notify(
            f"counter #{number}: {rec.asset_longname or asset} [{kind}] "
            f"({content_type or 'no content_type'}, {len(body)} bytes) @ {txid}"
        )
        return True

    # --- reorg handling (N4) --------------------------------------------------

    def check_reorg(self) -> None:
        """Detect a reorg at the stored tip and roll back to the fork point.

        If the chain's hash at our last indexed height no longer matches what
        we stored, walk back through the tracked block hashes to the highest
        height that still agrees, delete everything above it, and rewind the
        cursor. Numbering re-derives identically on re-index (N4)."""
        last = self.store.get_last_height(self.config.start_height)
        if last < self.config.start_height:
            return
        stored = self.store.get_last_block_hash()
        if stored is None:
            return
        try:
            if self.btc.get_block_hash(last) == stored:
                return
        except BitcoindError:
            return  # backend unavailable; the sync pass will surface it

        lowest = self.store.lowest_tracked_height()
        floor = max(lowest or self.config.start_height, self.config.start_height)
        fork = last - 1
        while fork >= floor:
            ours = self.store.get_indexed_block_hash(fork)
            if ours is not None and ours == self.btc.get_block_hash(fork):
                break
            fork -= 1
        if fork < floor:
            # The fork is deeper than the tracked block-hash window: rows below
            # it may belong to the abandoned chain and reorg detection would be
            # blind after a partial rollback. Refuse to guess — this needs a
            # rescan from genesis (delete the data dir).
            raise SystemExit(
                f"reorg deeper than the {last - floor}-block tracked window at "
                f"block {last}: cannot find the fork point. Rebuild the index "
                f"from genesis (remove the data directory) and restart."
            )
        removed = self.store.rollback_to(fork)
        self._notify(
            f"reorg detected at block {last}: rolled back to {fork} "
            f"({removed} counter(s) removed; re-indexing)"
        )

    # --- run loops ---------------------------------------------------------

    def _height_lines(self) -> list[str]:
        """The two backend status lines shown above the bar — ALWAYS both, so the
        live block is a stable three rows (bitcoin, counterparty, bar) that update
        in place instead of scrolling. Each line shows the backend's height, or,
        whenever something happens, the reason in its place: a wait note when a
        backend is unreachable (falling back to `down`), `connecting…` before the
        first poll, and a `catching up` tag on Counterparty while it trails
        bitcoind (`957063/957090 · catching up`)."""
        btc, cp = self._btc_tip, self._cp_tip
        btc_note = getattr(self, "_btc_note", None)
        cp_note = getattr(self, "_cp_note", None)

        if self._btc_down:
            btc_line = f"bitcoin - {btc_note or 'down'}"
        elif btc is not None:
            btc_line = f"bitcoin - {btc}"
        else:
            btc_line = "bitcoin - connecting…"

        if self._cp_down:
            cp_line = f"counterparty - {cp_note or 'down'}"
        elif cp is not None:
            if btc is not None and not self._btc_down:
                tag = " · catching up" if cp < btc else ""
                cp_line = f"counterparty - {cp}/{btc}{tag}"
            else:
                cp_line = f"counterparty - {cp}"
        else:
            cp_line = "counterparty - connecting…"

        return [btc_line, cp_line]

    def _show_heights(self, bar: ProgressBar) -> None:
        """Surface the backend heights as *current status*, not history.

        On a TTY the heights are shown on their own lines just above the bar
        and redrawn in place, so a moving bitcoind tip (or a backend flapping
        up/down) updates the same rows instead of scrolling a fresh pair every
        poll. When output is not a TTY (piped to a log) there is no in-place
        redraw, so fall back to printing the lines only when they change."""
        lines = self._height_lines()
        if not lines:
            return
        if bar.enabled:
            bar.set_status_lines(lines)
        elif lines != self._shown_heights:
            self._shown_heights = lines
            for line in lines:
                bar.write(line)

    def _target_tip(self) -> int:
        """Highest block height safe to index.

        Counterparty (the oracle) can only validate blocks it has already
        parsed, so we never index past its height: clamp to the LOWER of
        Bitcoin Core's tip and Counterparty's parsed height, then apply the
        confirmation buffer. Without this, when Counterparty lags behind
        bitcoind the indexer would walk blocks the oracle hasn't seen, record
        nothing for them, advance its cursor, and silently skip any counters
        minted in that gap (only recoverable by a full rescan).
        """
        # Poll both backends even if the first one fails, so the height lines
        # can report each one's up/down state independently.
        btc_err: Exception | None = None
        try:
            self._btc_tip = self.btc.get_block_count()
            self._btc_down = False
        except BitcoindError as e:
            self._btc_down = True
            btc_err = e
        try:
            self._cp_tip = self.cp.counterparty_height()
            self._cp_down = False
        except CounterpartyError:
            self._cp_down = True
            if btc_err is None:
                raise
        if btc_err is not None:
            raise btc_err
        return min(self._btc_tip, self._cp_tip) - self.config.confirmations

    def sync_to_tip(self, stop_at: int | None = None) -> int:
        self.check_reorg()
        start = self.store.get_last_height(self.config.start_height) + 1
        start = max(start, self.config.start_height)
        if start > GENESIS_HEIGHT and self.store.count() == 0:
            # Consensus warning: an empty index starting above genesis will
            # number its first event #0 even though earlier events exist.
            self._notify(
                f"WARNING: fresh index starting at block {start} > genesis "
                f"{GENESIS_HEIGHT}: numbering will NOT match spec-conformant "
                f"indexers (counter #0 is at block 902005). Unset "
                f"COUNTER_START_HEIGHT unless you know what you are doing."
            )
        tip = self._target_tip()
        if stop_at is not None:
            tip = min(tip, stop_at)

        base = self.store.count()
        span = tip - start + 1

        # The daemon (run()) installs a PERSISTENT bar that is reused across
        # polls and always rendered, so it sits at 100% while caught up. A
        # one-shot `sync` of a multi-block range instead gets a transient bar
        # that is closed when the pass ends. The bar shows the REAL block height
        # (n/tip), so the displayed number is the actual chain position and the
        # count never resets on resume; rate/ETA track work done this session.
        bar = self._progress
        own_bar = False
        if bar is None and span >= 2:
            bar = ProgressBar(tip, desc="Indexing", initial=start - 1)
            self._progress = bar
            own_bar = True
        if bar is not None:
            bar.total = max(tip, 1)  # keep up with a moving tip
            self._show_heights(bar)

        total = 0
        try:
            if start > tip:
                # Caught up: pin the bar at the current tip (100%) and idle.
                if bar is not None:
                    bar.update(tip, postfix=f"{base} counters")
                return 0
            for height in range(start, tip + 1):
                total += self.process_block(height)
                if bar is not None:
                    bar.update(height, postfix=f"{base + total} counters")
                if self._stop:
                    break
        finally:
            if own_bar:
                bar.close()
                self._progress = None
        return total

    def run(self) -> None:
        self._install_signal_handler()
        resume = self.store.get_last_height(self.config.start_height) + 1
        resume = max(resume, self.config.start_height)
        log.debug(
            "starting indexer: resuming from block %d (poll=%.1fs, confirmations=%d)",
            resume,
            self.config.poll_interval,
            self.config.confirmations,
        )
        # One persistent progress bar for the whole daemon: it stays on screen,
        # updates in place, and shows 100% whenever the index is caught up to
        # the chain tip. sync_to_tip() reuses it and keeps its total current.
        try:
            tip = self._target_tip()
        except Exception:
            tip = resume
        self._progress = ProgressBar(
            max(tip, 1), desc="Indexing", initial=max(resume - 1, 0)
        )
        try:
            while not self._stop:
                retry = f"retrying in {self.config.poll_interval:.0f}s"
                ok = False
                try:
                    self.sync_to_tip()
                    ok = True
                except (CounterpartyError, BitcoindError) as e:
                    # Expected/transient: a backend is down, restarting, or still
                    # running startup migrations. Reflect the reason ON the
                    # affected backend's height line and redraw it in place —
                    # never scroll a fresh "waiting" message every poll.
                    self._set_wait_note(e)
                    if self._progress is not None:
                        self._show_heights(self._progress)
                except Exception:  # genuinely unexpected: keep the loop alive but log fully
                    log.exception("sync pass failed; %s", retry)
                if ok:
                    # Backends reachable: drop any stale wait note and refresh the
                    # lines. A Counterparty tip below bitcoind's (catching up) is
                    # already visible in the `cp/btc` numbers, so nothing scrolls.
                    if self._btc_note or self._cp_note:
                        self._btc_note = self._cp_note = None
                    if self._progress is not None:
                        self._show_heights(self._progress)
                if self._stop:
                    break
                self._interruptible_sleep(self.config.poll_interval)
        finally:
            if self._progress is not None:
                self._progress.close()
                self._progress = None
        log.info(
            "stopped gracefully at block %d (%d counters indexed)",
            self.store.get_last_height(self.config.start_height),
            self.store.count(),
        )
