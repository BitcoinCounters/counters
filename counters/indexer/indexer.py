"""The indexer pipeline.

For each block (ascending), join txs that carry exactly one COUNT envelope
against Counterparty's successful first/creation issuances, assign the next
global number, and store the record + file content.

Validity rules enforced here (MVP):
  1. tx contains exactly one valid COUNT envelope (tx-wide, all inputs)
  2. tx has a Counterparty issuance with status == "valid"
  3. that issuance is the asset's first/creation issuance
  4. issued asset is not BTC/XCP

Reorg renumbering and the read/serve API are intentionally out of scope.
"""

from __future__ import annotations

import logging
import signal
import time

from ..bitcoind import BitcoindClient, BitcoindError
from ..config import Config, RESERVED_ASSETS
from ..counterparty import CounterpartyClient, CounterpartyError
from ..envelope import find_counter_envelopes_in_tx
from ..progress import ProgressBar
from ..store import CounterRecord, Store

log = logging.getLogger("counters")


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
        # De-dup state for transient status lines (see _status): remember the
        # current reason and when it started so we don't reprint every poll.
        self._status_key: str | None = None
        self._status_since = 0.0
        self._status_last = 0.0

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

    def _status(self, key: str, msg: str, repeat_every: float = 60.0) -> None:
        """Emit a transient, de-duplicated status line.

        Prints immediately when the reason (`key`) changes; while the same
        condition persists it reprints at most once per `repeat_every` seconds,
        appending how long it has been waiting. This replaces the old behaviour
        of repeating the identical line on every poll.
        """
        now = time.monotonic()
        if key != self._status_key:
            self._status_key = key
            self._status_since = now
            self._status_last = now
            self._notify(msg)
        elif now - self._status_last >= repeat_every:
            self._status_last = now
            self._notify(f"{msg} (still waiting, {int(now - self._status_since)}s)")

    def _status_clear(self, msg: str | None = None) -> None:
        """Clear any active status; optionally emit a one-off recovery line."""
        if self._status_key is not None:
            self._status_key = None
            if msg:
                self._notify(msg)

    def _backend_wait_reason(self, err: Exception, retry: str) -> tuple[str, str]:
        """Return (dedup_key, message) explaining why a backend is unavailable,
        tailored to the specific failure so the line states the real reason."""
        if isinstance(err, CounterpartyError):
            url = self.config.cp_api_url
            kind = getattr(err, "kind", "error")
            if kind == "unreachable":
                reason = (
                    f"Counterparty API not listening on {url} yet — counterparty-server "
                    f"is starting up or restarting (its API comes online only after the "
                    f"database migrations finish)"
                )
            elif kind == "timeout":
                reason = (
                    f"Counterparty API at {url} is not responding — the server is busy "
                    f"(applying migrations or catching up)"
                )
            else:
                reason = f"Counterparty API error at {url}: {err}"
            return f"cp-{kind}", f"{reason}; {retry}…"
        # BitcoindError (or other backend RPC failure)
        return (
            "btc",
            f"Bitcoin Core RPC not reachable at {self.config.btc_rpc_url} — is bitcoind "
            f"running with RPC enabled? {retry}…",
        )

    def close(self) -> None:
        self.store.close()

    # --- block processing --------------------------------------------------

    def process_block(self, height: int) -> int:
        """Process a single block; returns the number of counters recorded."""
        block_hash = self.btc.get_block_hash(height)
        block = self.btc.get_block(block_hash, verbosity=2)
        txs = block.get("tx", [])

        # Only fetch Counterparty issuances if the block has any candidate
        # COUNT envelopes — avoids an API call per empty block.
        candidates: list[tuple[int, dict, object]] = []
        for position, tx in enumerate(txs):
            envelopes = find_counter_envelopes_in_tx(tx.get("vin", []))
            if len(envelopes) == 1:
                candidates.append((position, tx, envelopes[0]))
            elif len(envelopes) > 1:
                log.debug(
                    "block %d tx %s: %d COUNT envelopes (>1) -> skipped",
                    height,
                    tx.get("txid"),
                    len(envelopes),
                )

        recorded = 0
        if candidates:
            issuances = self.cp.get_block_issuances(height)
            for position, tx, env in candidates:
                if self._maybe_record(height, position, tx, env, issuances):
                    recorded += 1

        self.store.set_last_height(height, block_hash)
        self.store.commit()
        return recorded

    def _maybe_record(self, height, position, tx, env, issuances) -> bool:
        txid = tx.get("txid")
        if self.store.has_txid(txid):
            return False

        tx_issuances = issuances.get(txid)
        if not tx_issuances:
            return False

        # A tx carries one Counterparty message; pick the issuance row that is
        # a valid creation. (Defensive: iterate in case of multiple rows.)
        issuance = None
        for row in tx_issuances:
            if self.cp.is_valid(row) and self.cp.is_creation(row):
                issuance = row
                break
        if issuance is None:
            return False

        asset = issuance["asset"]
        if asset in RESERVED_ASSETS:
            return False

        asset_info = self.cp.get_asset(asset) or {}
        asset_id = asset_info.get("asset_id")
        if asset_id in ("0", "1", 0, 1):
            return False

        sha = self.store.store_blob(env.body)
        number = self.store.next_number()
        content_type = env.content_type.decode("utf-8", errors="replace") if env.content_type else None
        # Inscription cost (commit + reveal) is enrichment, never a blocker: a
        # fetch failure must not stop a counter from being recorded.
        try:
            fee, tx_size = self.btc.get_inscription_cost(txid, reveal_tx=tx)
        except (BitcoindError, KeyError, IndexError, TypeError):
            fee, tx_size = None, None
        rec = CounterRecord(
            asset=asset,
            asset_id=str(asset_id) if asset_id is not None else None,
            asset_longname=issuance.get("asset_longname") or asset_info.get("asset_longname"),
            content_type=content_type,
            content_sha256=sha,
            content_length=len(env.body),
            mint_txid=txid,
            block_index=height,
            block_position=position,
            cp_tx_index=issuance.get("tx_index"),
            owner=asset_info.get("owner") or issuance.get("issuer"),
            divisible=asset_info.get("divisible"),
            supply=asset_info.get("supply"),
            fee=fee,
            tx_size=tx_size,
            xcp_burned=issuance.get("fee_paid"),
        )
        self.store.add_counter(number, rec)
        self._notify(
            f"counter #{number}: {asset} "
            f"({content_type or 'no content_type'}, {len(env.body)} bytes) @ {txid}"
        )
        return True

    # --- run loops ---------------------------------------------------------

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
        self._btc_tip = self.btc.get_block_count()
        self._cp_tip = self.cp.counterparty_height()
        return min(self._btc_tip, self._cp_tip) - self.config.confirmations

    def sync_to_tip(self, stop_at: int | None = None) -> int:
        start = self.store.get_last_height(self.config.start_height) + 1
        start = max(start, self.config.start_height)
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
                    # running startup migrations. State the specific reason once
                    # (no stack trace) and retry; _status de-dups the repeats.
                    key, msg = self._backend_wait_reason(e, retry)
                    self._status(key, msg)
                except Exception:  # genuinely unexpected: keep the loop alive but log fully
                    log.exception("sync pass failed; %s", retry)
                if ok:
                    # Backends reachable. Distinguish "fully caught up" from
                    # "waiting for the oracle to catch up" (cp behind bitcoind),
                    # which would otherwise be a silent delay.
                    cp, btc = self._cp_tip, self._btc_tip
                    if cp is not None and btc is not None and cp < btc:
                        self._status(
                            "catchup",
                            f"Waiting for Counterparty to catch up — it has validated to "
                            f"block {cp:,} of {btc:,}; counters will follow automatically.",
                        )
                    else:
                        self._status_clear("Backends reachable — indexing resumed.")
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
