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

from typing import Any

import requests

from .config import Config

# Slipstream's published mempool policy caps a transaction (and its OP_RETURN)
# at this weight. A block is 4,000,000 WU, so this is ~99.8% of one: the real
# ceiling on how large a single inscription can ever be. Exceeding it is a
# hard rejection, so we check locally before spending anything.
MAX_WEIGHT = 3_991_000

# The standard-relay weight cap. At or below it, the public network would take
# the transaction for free — Slipstream is for what lies beyond.
STANDARD_MAX_WEIGHT = 400_000


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
        headers = {"Content-Type": "application/json"}
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
