"""MARA Slipstream client — out-of-band submission for transactions the public
relay network refuses.

An oversized taproot inscription is a perfectly VALID Bitcoin transaction that
merely exceeds the 400k WU standard-relay cap, so no ordinary node will relay
it and `sendrawtransaction` is a dead end. Slipstream takes such transactions
directly into MARA's own mempool for mining. Four facts shape this client:

  - **No API key is required.** `/api/rates`, `/api/transactions` and
    `/api/transactions/status` all answer unauthenticated (verified
    2026-08-05: an unauthenticated submit of bad hex returns HTTP 400
    "Failed to deserialize transaction", not 401). A key, where one exists,
    travels as `client_code` and only buys whatever fee discount MARA has
    assigned it — so it is strictly optional and never required to mint.

  - **Two live rates, and they are not interchangeable.** `/api/rates`
    publishes `submit_fee_rate` — the floor a submission must meet to be
    ACCEPTED — and `effective_rate` (= `market_rate` x `multiplier`), the rate
    MARA is currently mining at. The web UI labels them "Minimum submission
    rate" and "Current mineable rate". They diverge whenever the market moves:
    a submission at the floor is accepted and then simply waits. Gate on
    `submit_fee_rate`; treat `effective_rate` as the confirm-soon advice it is.
    Read both at compose time — the multiplier is a knob MARA turns (it has
    not always been 1.0).

  - **No package submission.** Unlike Bitcoin Core's `submitpackage`,
    Slipstream accepts one transaction at a time ("slipstream does not support
    packaged transactions as defined in Bitcoin Core"). A commit/reveal pair
    therefore goes up in order, and the commit must be accepted first.

  - **Submitted transactions stay private until mined.** They are not relayed
    to the public network until they have at least one confirmation, so
    bitcoind and every block explorer are blind to them in the meantime.
    Status can only come from Slipstream itself.
"""

from __future__ import annotations

import json
import time
from typing import Any, NamedTuple

import requests

from .config import Config

# Slipstream's published mempool policy caps a transaction (and its OP_RETURN)
# at this weight. A block is 4,000,000 WU, so this is ~99.8% of one: the real
# ceiling on how large a single inscription can ever be. Exceeding it is a
# hard rejection, so we check locally before spending anything.
MAX_WEIGHT = 3_991_000

# Sent on every request. Not a workaround for anything observed here — an
# unauthenticated probe with requests' default UA answers 400 normally
# (checked 2026-09-15) — but MARA's edge has rejected generic clients before,
# and naming ourselves costs nothing.
USER_AGENT = "counters/1.0"

# The standard-relay weight cap. At or below it, the public network would take
# the transaction for free — Slipstream is for what lies beyond.
STANDARD_MAX_WEIGHT = 400_000

# --- Submitting a multi-megabyte transaction ---------------------------------
#
# Slipstream's origin sits behind Cloudflare and flaps. A large upload finishes
# and then dies with a 5xx while the origin is still validating it, and THAT
# ANSWER IS AMBIGUOUS: the submission may well have landed. Treating it as a
# failure is how a reveal that Slipstream actually accepted gets reported as
# rejected. So a submission is classified, never simply raised on:
#
#   accepted   200/201, or 400 "already known" — it has the transaction
#   rejected   400 for any other reason — a verdict, stop
#   ambiguous  5xx after the whole body went up — probably accepted; the CHAIN
#              decides, and a re-upload is cheap only compared to losing it
#   no-answer  timeout, reset, 408 — the body never arrived; retry freely
#
# The probe exists because uploading megabytes into a dead origin wastes the
# one thing that cannot be repeated cheaply. A throwaway body draws a fast 400
# ("Failed to deserialize transaction") from the ORIGIN itself — proof it is
# alive right now — and the real submission follows immediately.
PROBE_BODY = "00"
PROBE_ALIVE_SECONDS = 2.0     # a 400 slower than this is Cloudflare, not the origin
PROBE_INTERVAL = 6            # seconds between probes
PROBE_STREAK = 2              # consecutive live answers before a real submit
PROBE_CAP = 1200              # ~2 h of probing before giving up
SUBMIT_TIMEOUT = 300.0        # a multi-MB upload plus the origin's think time
AMBIGUOUS_AFTER_SECONDS = 90  # a 5xx later than this means the body DID go up


class SubmitVerdict(NamedTuple):
    """What a submission attempt actually established. `state` is one of
    'accepted', 'rejected', 'ambiguous', 'no-answer' (see above)."""

    state: str
    code: int | None
    message: str
    seconds: float

    @property
    def submitted(self) -> bool:
        """True when Slipstream may hold the transaction — so the next move is
        to watch the chain, never to assume failure."""
        return self.state in ("accepted", "ambiguous")

    @property
    def final(self) -> bool:
        """True when retrying cannot change the outcome."""
        return self.state in ("accepted", "rejected")


class SlipstreamError(Exception):
    """A Slipstream API call failed. `kind` classifies why: 'unreachable',
    'timeout', 'http' (the API answered with an error), or 'error'."""

    def __init__(self, message: str, kind: str = "error"):
        super().__init__(message)
        self.kind = kind


class SlipstreamClient:
    def __init__(self, config: Config):
        self.base = config.slipstream_api_url.rstrip("/")
        self.timeout = config.http_timeout
        # Optional: buys a discount multiplier when MARA has issued one. Its
        # absence never blocks a submission.
        self.client_code = config.slipstream_api_key or None
        self._session = requests.Session()

    # -- transport ---------------------------------------------------------

    def _request(self, method: str, path: str, params: dict | None = None,
                 body: dict | None = None, timeout: float | None = None) -> Any:
        url = f"{self.base}{path}"
        headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
        if self.client_code:
            headers["Authorization"] = f"Bearer {self.client_code}"
        try:
            resp = self._session.request(
                method, url, params=params, json=body, headers=headers,
                timeout=self.timeout if timeout is None else timeout,
            )
        except requests.ConnectionError as e:
            raise SlipstreamError(
                f"could not reach Slipstream at {self.base}", kind="unreachable"
            ) from e
        except requests.Timeout as e:
            raise SlipstreamError(
                f"Slipstream timed out at {self.base}", kind="timeout"
            ) from e

        try:
            parsed = resp.json()
        except ValueError:
            parsed = resp.text

        if not resp.ok:
            # Errors come back as {"status": "error", "message": "..."}.
            detail = parsed.get("message") if isinstance(parsed, dict) else parsed
            raise SlipstreamError(
                f"Slipstream {method} {path} failed ({resp.status_code}): {detail}",
                kind="http",
            )
        return parsed

    # -- endpoints ---------------------------------------------------------

    def rates(self) -> dict:
        """Current pricing. `submit_fee_rate` is the minimum sat/vB a submission
        must pay to be accepted; `effective_rate` is the rate being mined now,
        with `market_rate` and `multiplier` explaining how it was derived."""
        result = self._request("GET", "/api/rates")
        if not isinstance(result, dict):
            raise SlipstreamError(f"unexpected /api/rates response: {result!r}")
        return result

    def fee_floors(self) -> tuple[float, float]:
        """`(submit_floor, mineable_rate)` from a single /api/rates call.

        Only the first is a hard gate. Paying between the two is legitimate —
        the transaction is accepted and waits for the market to come down —
        so nothing here may silently round one up to the other.
        """
        rates = self.rates()
        mineable = rates.get("effective_rate")
        if mineable is None:
            raise SlipstreamError(f"/api/rates carried no effective_rate: {rates!r}")
        # Older deployments published only effective_rate; fall back to it so a
        # missing field errs toward overpaying rather than toward rejection.
        floor = rates.get("submit_fee_rate", mineable)
        return float(floor), float(mineable)

    def min_fee_rate(self) -> float:
        """The rate a submission must meet or beat, straight from the API."""
        return self.fee_floors()[0]

    def submit(self, raw_hex: str) -> dict:
        """Submit ONE signed transaction. Slipstream has no package endpoint, so
        a commit/reveal pair is two calls, commit first."""
        body: dict[str, Any] = {"tx_hex": raw_hex}
        if self.client_code:
            body["client_code"] = self.client_code
        # A multi-megabyte reveal takes real time to upload; don't let the
        # ordinary HTTP timeout abort a submission we may not be able to repeat.
        result = self._request("POST", "/api/transactions", body=body,
                               timeout=max(self.timeout, 300.0))
        if isinstance(result, dict) and result.get("status") not in (None, "success"):
            raise SlipstreamError(f"Slipstream rejected the transaction: {result}")
        return result if isinstance(result, dict) else {"message": result}

    # -- classified submission ---------------------------------------------

    def _post_unraised(self, body: dict, timeout: float) -> tuple[int | None, str, float]:
        """POST /api/transactions and report what happened. Never raises: how a
        submission failed is the whole signal here, so it is data, not an
        exception. Returns (http_code_or_None, body_text, seconds)."""
        headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
        if self.client_code:
            headers["Authorization"] = f"Bearer {self.client_code}"
        started = time.time()
        try:
            resp = self._session.request(
                "POST", f"{self.base}/api/transactions", params=None, json=body,
                headers=headers, timeout=timeout,
            )
        except Exception as e:                  # timeout, reset, DNS — all "no answer"
            return None, f"{type(e).__name__}: {e}", time.time() - started
        return resp.status_code, (resp.text or "")[:600], time.time() - started

    def probe(self, timeout: float = 20.0) -> tuple[bool, str]:
        """Is the ORIGIN answering right now? A throwaway body should draw a
        fast 400 "Failed to deserialize transaction" — Cloudflare alone cannot
        produce that. Returns (alive, one-line detail)."""
        code, msg, secs = self._post_unraised({"tx_hex": PROBE_BODY}, timeout)
        alive = (code == 400 and secs < PROBE_ALIVE_SECONDS)
        return alive, f"http={code} {secs:.2f}s"

    def submit_classified(self, raw_hex: str,
                          timeout: float | None = None) -> SubmitVerdict:
        """Submit one transaction and CLASSIFY the answer rather than raising.

        The distinction `submit()` cannot draw: a 5xx arriving after the whole
        body went up is not a rejection. Every large submission MARA has
        accepted looked exactly like that — Cloudflare gives up at ~100 s while
        the origin is still validating — so it is reported as ambiguous and the
        caller settles it against the chain.
        """
        body: dict[str, Any] = {"tx_hex": raw_hex}
        if self.client_code:
            body["client_code"] = self.client_code
        code, msg, secs = self._post_unraised(
            body, SUBMIT_TIMEOUT if timeout is None else timeout)
        low = (msg or "").lower()

        if code in (200, 201):
            # A 200 can still carry {"status": "error"} — the API does that, so
            # the body is parsed rather than pattern-matched.
            try:
                parsed = json.loads(msg)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict) and parsed.get("status") == "error":
                return SubmitVerdict("rejected", code, msg, secs)
            return SubmitVerdict("accepted", code, msg, secs)
        if code == 400 and ("already" in low or "known" in low):
            return SubmitVerdict("accepted", code, msg, secs)
        if code == 400 and "not found" not in low:
            return SubmitVerdict("rejected", code, msg, secs)
        if code is not None and code >= 500 and secs > AMBIGUOUS_AFTER_SECONDS:
            return SubmitVerdict("ambiguous", code, msg, secs)
        return SubmitVerdict("no-answer", code, msg, secs)

    def status(self, txid: str) -> dict:
        """Confirmation state, weight/vsize/fee, and mining odds for a txid.
        The only way to watch a submission: it is invisible to bitcoind until
        it has a confirmation."""
        result = self._request("GET", "/api/transactions/status",
                               params={"tx_id": txid})
        if not isinstance(result, dict):
            raise SlipstreamError(f"unexpected status response: {result!r}")
        return result


def describe_status(st: dict) -> str:
    """One human line from a status payload, for the CLI to echo."""
    tx = st.get("transaction") or {}
    state = tx.get("status") or {}
    if state.get("confirmed"):
        return f"confirmed in block {state.get('block_height')}"
    msg = st.get("message")
    if st.get("is_next_block"):
        return f"{msg} (in the next block template)" if msg else "in the next block template"
    return msg or "pending"
