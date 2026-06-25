"""The indexer pipeline.

For each block (ascending), join txs that carry exactly one COUNT envelope
against Counterparty's successful first/creation issuances, assign the next
global number, and store the record + file content.

Validity rules enforced here (MVP):
  1. tx contains exactly one valid COUNT envelope (tx-wide, all inputs)
  2. tx has a Counterparty issuance with status == "valid"
  3. that issuance is the asset's first/creation issuance
  4. minted asset is not BTC/XCP

Reorg renumbering and the read/serve API are intentionally out of scope.
"""

from __future__ import annotations

import logging
import signal
import time

from ..bitcoind import BitcoindClient
from ..config import Config, RESERVED_ASSETS
from ..counterparty import CounterpartyClient
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
        )
        self.store.add_counter(number, rec)
        self._notify(
            f"counter #{number}: {asset} "
            f"({content_type or 'no content_type'}, {len(env.body)} bytes) @ {txid}"
        )
        return True

    # --- run loops ---------------------------------------------------------

    def sync_to_tip(self, stop_at: int | None = None) -> int:
        start = self.store.get_last_height(self.config.start_height) + 1
        start = max(start, self.config.start_height)
        tip = self.btc.get_block_count() - self.config.confirmations
        if stop_at is not None:
            tip = min(tip, stop_at)

        floor = self.config.start_height
        base = self.store.count()
        span = tip - start + 1

        # The daemon (run()) installs a PERSISTENT bar that is reused across
        # polls and always rendered, so it sits at 100% while caught up. A
        # one-shot `sync` of a multi-block range instead gets a transient bar
        # that is closed when the pass ends. The bar tracks ABSOLUTE chain
        # position (height/tip) so the count never resets on resume.
        bar = self._progress
        own_bar = False
        if bar is None and span >= 2:
            bar = ProgressBar(tip - floor + 1, desc="Indexing", initial=start - floor)
            self._progress = bar
            own_bar = True
        if bar is not None:
            bar.total = max(tip - floor + 1, 1)  # keep up with a moving tip

        total = 0
        try:
            if start > tip:
                # Caught up: pin the bar at the current tip (100%) and idle.
                if bar is not None:
                    bar.update(tip - floor + 1, postfix=f"{base} counters")
                return 0
            for height in range(start, tip + 1):
                total += self.process_block(height)
                if bar is not None:
                    bar.update(height - floor + 1, postfix=f"{base + total} counters")
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
        floor = self.config.start_height
        try:
            tip = self.btc.get_block_count() - self.config.confirmations
        except Exception:
            tip = resume
        self._progress = ProgressBar(
            max(tip - floor + 1, 1), desc="Indexing", initial=max(resume - floor, 0)
        )
        try:
            while not self._stop:
                try:
                    self.sync_to_tip()
                except Exception:  # keep the loop alive; log and retry
                    log.exception("sync pass failed; retrying after poll interval")
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
