"""Unit tests for the MARA Slipstream submission path (no network).

Three things carry real risk and are pinned here:

  - the API key is OPTIONAL — submission must work with none configured,
  - the fee rate is resolved from the live API BEFORE funding is sized, since
    funding is computed from it,
  - the commit/reveal pair goes up as two separate calls (Slipstream has no
    package endpoint), so a reveal rejected after the commit was accepted must
    surface the reveal hex — that reveal is the only transaction that can ever
    spend the commit.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import counters.commands.inscribe as I  # noqa: E402
from counters.config import Config  # noqa: E402
from counters.slipstream import (  # noqa: E402
    MAX_WEIGHT,
    STANDARD_MAX_WEIGHT,
    SlipstreamClient,
    SlipstreamError,
    describe_status,
)

COMMIT_TXID = "c" * 64
REVEAL_TXID = "r" * 64


# --- client ------------------------------------------------------------------

class _FakeResp:
    def __init__(self, payload, ok=True, status_code=200):
        self._payload = payload
        self.ok = ok
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


class _FakeSession:
    """Records requests and replays queued responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append(dict(method=method, url=url, params=params,
                               json=json, headers=headers, timeout=timeout))
        return self.responses.pop(0)


def _client(responses, key=""):
    cfg = Config()
    cfg.slipstream_api_key = key
    c = SlipstreamClient(cfg)
    c._session = _FakeSession(responses)
    return c


RATES = {"effective_rate": 3.0, "market_rate": 3.0, "multiplier": 1.0,
         "submit_fee_rate": 1.0}


def test_fee_floors_keeps_the_two_rates_apart():
    # They are not interchangeable. submit_fee_rate is the hard gate a
    # submission must meet; effective_rate is what MARA is mining at right now.
    # Paying between them is legitimate — accepted, then waits — so nothing may
    # silently round one up to the other.
    c = _client([_FakeResp(RATES)])
    assert c.fee_floors() == (1.0, 3.0)


def test_min_fee_rate_is_the_submit_floor():
    # The gate, not the mineable rate: quoting effective_rate here would refuse
    # submissions the API would have accepted.
    c = _client([_FakeResp(RATES)])
    assert c.min_fee_rate() == 1.0


def test_no_api_key_is_required():
    # The whole point: minting must not depend on a key existing anywhere.
    c = _client([_FakeResp({"status": "success", "message": REVEAL_TXID})], key="")
    assert c.client_code is None
    c.submit("deadbeef")
    call = c._session.calls[0]
    assert "client_code" not in call["json"]      # nothing invented
    assert "Authorization" not in call["headers"]
    assert call["json"]["tx_hex"] == "deadbeef"


def test_api_key_when_present_is_sent_as_client_code():
    c = _client([_FakeResp({"status": "success"})], key="secret")
    c.submit("deadbeef")
    call = c._session.calls[0]
    assert call["json"]["client_code"] == "secret"
    assert call["headers"]["Authorization"] == "Bearer secret"


def test_submit_raises_on_error_status_in_a_200_body():
    c = _client([_FakeResp({"status": "error", "message": "too big"})])
    try:
        c.submit("deadbeef")
        assert False, "an error status should have raised"
    except SlipstreamError as e:
        assert "too big" in str(e)


def test_http_error_carries_the_api_message():
    c = _client([_FakeResp({"status": "error", "message": "Failed to deserialize"},
                           ok=False, status_code=400)])
    try:
        c.rates()
        assert False, "a 400 should have raised"
    except SlipstreamError as e:
        assert e.kind == "http" and "Failed to deserialize" in str(e)


def test_submit_uses_a_long_timeout():
    # A multi-megabyte reveal takes real time to upload, and it is a request we
    # may not be able to repeat — it must not die on the ordinary HTTP timeout.
    c = _client([_FakeResp({"status": "success"})])
    c.timeout = 30.0
    c.submit("ab" * 1000)
    assert c._session.calls[0]["timeout"] >= 300.0


def test_describe_status_variants():
    assert "959968" in describe_status(
        {"transaction": {"status": {"confirmed": True, "block_height": 959968}}})
    assert describe_status({"message": "pending", "transaction": {}}) == "pending"
    assert "next block" in describe_status(
        {"is_next_block": True, "transaction": {"status": {"confirmed": False}}})


# --- inscribe integration ----------------------------------------------------

class _StubSlip:
    """Stands in for SlipstreamClient inside cmd_inscribe."""

    def __init__(self, floor=3.0, fail_on=None):
        self.floor = floor
        self.fail_on = fail_on          # 'commit' | 'reveal' | None
        self.submitted = []

    def min_fee_rate(self):
        return self.floor

    def submit(self, raw):
        which = "commit" if len(self.submitted) == 0 else "reveal"
        if self.fail_on == which:
            raise SlipstreamError(f"{which} rejected")
        self.submitted.append(raw)
        return {"status": "success", "message": "ok"}

    def status(self, txid):
        return {"message": "pending", "transaction": {}}


@pytest.fixture(autouse=True)
def _stash_in_tmp(tmp_path, monkeypatch):
    """_stash_hex writes beside the cwd, so every submission test runs in its
    own directory rather than dropping reveal-*.hex into the repo."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_submit_order_is_commit_then_reveal():
    slip = _StubSlip()
    rc = I._submit_via_slipstream(slip, "aa", "bb", COMMIT_TXID, REVEAL_TXID, 500_000)
    assert rc == 0
    assert slip.submitted == ["aa", "bb"]      # commit first — the reveal spends it


def test_reveal_hex_is_stashed_before_anything_is_submitted(_stash_in_tmp):
    # The hex reaches disk BEFORE the first broadcast, so a Ctrl-C or a dropped
    # terminal between the two submissions cannot take it down with the process.
    slip = _StubSlip()
    I._submit_via_slipstream(slip, "aa", "beefbeef", COMMIT_TXID, REVEAL_TXID, 500_000)
    stash = _stash_in_tmp / f"reveal-{REVEAL_TXID[:16]}.hex"
    assert "beefbeef" in stash.read_text()


def test_reveal_rejection_after_commit_points_at_the_stashed_hex(_stash_in_tmp, capsys):
    # The unrecoverable case. The commit is on chain and only this exact reveal
    # can ever spend it (ephemeral key, discarded), so losing the hex strands
    # the funds. stdout can be redirected and a terminal can scroll, so the hex
    # lives in a file and the failure path must name it.
    slip = _StubSlip(fail_on="reveal")
    rc = I._submit_via_slipstream(slip, "aa", "beefbeef", COMMIT_TXID, REVEAL_TXID, 500_000)
    err = capsys.readouterr().err
    assert rc == 1
    assert "COMMIT WAS ACCEPTED" in err
    stash = _stash_in_tmp / f"reveal-{REVEAL_TXID[:16]}.hex"
    assert str(stash) in err                   # the operator is told where it is
    assert "beefbeef" in stash.read_text()     # and the hex itself is recoverable


def test_commit_rejection_says_nothing_was_stranded(capsys):
    slip = _StubSlip(fail_on="commit")
    rc = I._submit_via_slipstream(slip, "aa", "bb", COMMIT_TXID, REVEAL_TXID, 500_000)
    err = capsys.readouterr().err
    assert rc == 1
    assert "Nothing reached the chain" in err
    assert slip.submitted == []


def test_max_weight_is_below_a_full_block():
    # Slipstream's cap, and the standard-relay cap it exists to get past.
    assert MAX_WEIGHT < 4_000_000
    assert STANDARD_MAX_WEIGHT < MAX_WEIGHT
