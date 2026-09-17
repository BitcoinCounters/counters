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
import counters.slipstream as S  # noqa: E402
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


# --- classified submission ---------------------------------------------------
#
# The distinction the raising `submit()` cannot draw. A 5xx arriving AFTER the
# whole body went up is not a rejection: Cloudflare gives up at ~100 s while the
# origin is still validating, and that is what every accepted multi-MB
# submission has looked like. Reporting it as failure is how a reveal Slipstream
# actually holds gets declared lost — and the reveal is the only transaction
# that can ever spend its commit.

class _SlowSession(_FakeSession):
    """Replays responses and controls the clock each one appears to take."""

    def __init__(self, responses, seconds):
        super().__init__(responses)
        self.seconds = list(seconds)
        self.now = 1000.0

    def request(self, *a, **kw):
        self.now += self.seconds.pop(0)
        if self.responses and isinstance(self.responses[0], Exception):
            raise self.responses.pop(0)
        return super().request(*a, **kw)


def _timed_client(responses, seconds, key=""):
    cfg = Config()
    cfg.slipstream_api_key = key
    c = SlipstreamClient(cfg)
    c._session = _SlowSession(responses, seconds)
    return c


def _at(monkeypatch, client):
    """Make slipstream's clock read the fake session's, so elapsed time is the
    time the response 'took' rather than real seconds."""
    monkeypatch.setattr(S.time, "time", lambda: client._session.now)


def test_524_after_the_full_upload_is_ambiguous_not_rejected(monkeypatch):
    c = _timed_client([_FakeResp("<html>error 524</html>", ok=False, status_code=524)],
                      seconds=[126.0])
    _at(monkeypatch, c)
    v = c.submit_classified("ab" * 500_000)
    assert v.state == "ambiguous"
    assert v.submitted is True       # the chain decides, not this answer
    assert v.final is False          # ...so a slow re-upload stays on the table


def test_502_before_the_body_finished_is_only_no_answer(monkeypatch):
    # Fast 5xx: Cloudflare turned it away before the origin saw anything, so
    # nothing was established and retrying costs nothing.
    c = _timed_client([_FakeResp("bad gateway", ok=False, status_code=502)],
                      seconds=[3.0])
    _at(monkeypatch, c)
    v = c.submit_classified("abcd")
    assert v.state == "no-answer" and v.submitted is False


def test_already_known_is_acceptance(monkeypatch):
    c = _timed_client([_FakeResp("transaction already known", ok=False, status_code=400)],
                      seconds=[1.0])
    _at(monkeypatch, c)
    v = c.submit_classified("abcd")
    assert v.state == "accepted" and v.submitted and v.final


def test_a_real_400_is_a_verdict(monkeypatch):
    c = _timed_client([_FakeResp("Fee rate of 0 is below the threshold",
                                 ok=False, status_code=400)], seconds=[1.0])
    _at(monkeypatch, c)
    v = c.submit_classified("abcd")
    assert v.state == "rejected" and v.submitted is False and v.final is True


def test_error_status_inside_a_200_is_rejected(monkeypatch):
    c = _timed_client([_FakeResp('{"status": "error", "message": "too big"}')],
                      seconds=[1.0])
    _at(monkeypatch, c)
    assert c.submit_classified("abcd").state == "rejected"


def test_success_200_is_accepted(monkeypatch):
    c = _timed_client([_FakeResp('{"status":"success","message":"' + REVEAL_TXID + '"}')],
                      seconds=[31.9])
    _at(monkeypatch, c)
    v = c.submit_classified("abcd")
    assert v.state == "accepted" and v.final


def test_a_connection_failure_never_raises(monkeypatch):
    # How a submission failed IS the signal, so it must arrive as data.
    c = _timed_client([ConnectionError("reset by peer")], seconds=[5.0])
    _at(monkeypatch, c)
    v = c.submit_classified("abcd")
    assert v.state == "no-answer" and "reset by peer" in v.message


def test_probe_is_alive_only_on_a_fast_400(monkeypatch):
    for status, secs, expected in ((400, 0.4, True),     # the origin itself
                                   (400, 9.0, False),    # too slow to be the origin
                                   (502, 0.3, False)):   # the edge, not the origin
        c = _timed_client([_FakeResp("Failed to deserialize transaction",
                                     ok=(status == 200), status_code=status)],
                          seconds=[secs])
        _at(monkeypatch, c)
        alive, detail = c.probe()
        assert alive is expected, (status, secs, detail)


def test_every_request_names_this_client(monkeypatch):
    c = _timed_client([_FakeResp('{"status":"success"}')], seconds=[1.0])
    _at(monkeypatch, c)
    c.submit_classified("abcd")
    assert c._session.calls[0]["headers"]["User-Agent"] == S.USER_AGENT


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


# --- the split path: commit over relay, reveal over Slipstream ---------------
#
# This is the path an oversized reveal actually takes, and the one where a
# misread answer costs the most: the commit is ALREADY CONFIRMED when the reveal
# goes up, so declaring a live submission dead sends the operator looking for a
# recovery that isn't needed — while the funds sit behind a reveal that cannot
# be re-composed, fee-bumped or replaced.

class _ProbingSlip:
    """A Slipstream whose probe answers and submit verdicts are scripted."""

    def __init__(self, verdicts, alive=True):
        self.verdicts = list(verdicts)
        self.alive = alive
        self.submitted = []
        self.probes = 0

    def probe(self, timeout=20.0):
        self.probes += 1
        return self.alive, f"http=400 0.4s"

    def submit_classified(self, raw, timeout=None):
        self.submitted.append(raw)
        return self.verdicts.pop(0)

    def status(self, txid):
        return {"message": "pending", "transaction": {}}


class _ConfirmedBtc:
    """A node whose commit is already confirmed, so no waiting loop runs."""

    def __init__(self):
        self.sent = []

    def _call(self, method, params=None):
        if method == "sendrawtransaction":
            self.sent.append(params[0])
            return COMMIT_TXID
        if method == "getrawtransaction":
            return {"confirmations": 3, "blockhash": "b" * 64}
        raise AssertionError(f"unexpected {method}")


@pytest.fixture(autouse=True)
def _no_probe_sleep(monkeypatch):
    monkeypatch.setattr(I.time, "sleep", lambda _s: None)


def test_split_reports_a_clean_acceptance():
    slip = _ProbingSlip([S.SubmitVerdict("accepted", 200, '{"status":"success"}', 31.9)])
    rc = I._submit_split(_ConfirmedBtc(), slip, "aa", "bb", COMMIT_TXID, REVEAL_TXID,
                         3_900_000)
    assert rc == 0 and slip.submitted == ["bb"]      # only the reveal goes to MARA


def test_split_treats_an_ambiguous_524_as_submitted(capsys):
    # The regression that matters: this used to print "would not take the reveal"
    # and exit 1 for a submission MARA was in the middle of accepting.
    slip = _ProbingSlip([S.SubmitVerdict("ambiguous", 524, "<html>524</html>", 126.0)])
    rc = I._submit_split(_ConfirmedBtc(), slip, "aa", "bb", COMMIT_TXID, REVEAL_TXID,
                         3_900_000)
    out = capsys.readouterr().out
    assert rc == 0                                   # not a failure
    assert "probably accepted" in out
    assert "--watch-only" in out                     # and how to settle it


def test_split_still_fails_loudly_on_a_real_rejection(capsys, _stash_in_tmp):
    slip = _ProbingSlip([S.SubmitVerdict("rejected", 400, "Fee rate of 0", 1.0)])
    rc = I._submit_split(_ConfirmedBtc(), slip, "aa", "beefbeef", COMMIT_TXID,
                         REVEAL_TXID, 3_900_000)
    err = capsys.readouterr().err
    assert rc == 1
    assert "COMMIT IS CONFIRMED ON CHAIN" in err
    stash = _stash_in_tmp / f"reveal-{REVEAL_TXID[:16]}.hex"
    assert str(stash) in err and "beefbeef" in stash.read_text()


def test_probing_retries_when_the_body_never_arrived():
    # 'no-answer' establishes nothing, so it goes back to probing rather than
    # burning the one submission that cannot be cheaply repeated.
    slip = _ProbingSlip([S.SubmitVerdict("no-answer", None, "timeout", 20.0),
                         S.SubmitVerdict("accepted", 200, "ok", 30.0)])
    v = I._submit_probing(slip, "bb", "reveal")
    assert v.state == "accepted" and len(slip.submitted) == 2


def test_probing_gives_up_when_no_window_ever_opens():
    slip = _ProbingSlip([], alive=False)
    v = I._submit_probing(slip, "bb", "reveal")
    assert v.state == "no-answer" and v.submitted is False
    assert slip.submitted == []                      # nothing was ever uploaded
    assert slip.probes == S.PROBE_CAP


def test_max_weight_is_below_a_full_block():
    # Slipstream's cap, and the standard-relay cap it exists to get past.
    assert MAX_WEIGHT < 4_000_000
    assert STANDARD_MAX_WEIGHT < MAX_WEIGHT
