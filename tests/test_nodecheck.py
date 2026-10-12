"""nodecheck.py: refuse to compose on a node behind the protocol changes."""

from __future__ import annotations

import io

from counters import nodecheck

CHANGES = {
    "taproot_support": {"minimum_version_major": 11, "minimum_version_minor": 0,
                        "minimum_version_revision": 0, "block_index": 902000,
                        "testnet4_block_index": 85000},
    "require_reveal_source_signature": {"minimum_version_major": 11, "minimum_version_minor": 5,
                                        "minimum_version_revision": 0, "block_index": 969320,
                                        "testnet4_block_index": 154564},
    "mpma_taproot_support": {"minimum_version_major": 11, "minimum_version_minor": 4,
                             "minimum_version_revision": 0, "block_index": 971700,
                             "testnet4_block_index": 155500},
    "far_future": {"minimum_version_major": 12, "minimum_version_minor": 0,
                   "minimum_version_revision": 0, "block_index": 1_500_000},
    "junk": "not a dict",
    "incomplete": {"minimum_version_major": 99},
}


def test_parse_version():
    assert nodecheck.parse_version("11.5.0") == (11, 5, 0)
    assert nodecheck.parse_version("v11.4.0-rc.1") == (11, 4, 0)
    assert nodecheck.parse_version(None) is None
    assert nodecheck.parse_version("Unknown") is None


def test_the_degent_case_is_refused():
    # 2026-10-01, block 969,488, a v11.4.0 node: the rule was active for 168 blocks
    problems = nodecheck.version_problems("11.4.0", 969_488, CHANGES)
    assert any("require_reveal_source_signature" in p and "already active" in p for p in problems)
    assert any("older than v11.5.0" in p for p in problems)


def test_an_upcoming_activation_within_the_lookahead_is_refused():
    # v11.5.0 is fine for the reveal rule, but suppose it were below the next batch
    changes = dict(CHANGES)
    changes["next"] = {"minimum_version_major": 11, "minimum_version_minor": 6,
                       "minimum_version_revision": 0, "block_index": 972_000}
    problems = nodecheck.version_problems("11.5.0", 971_049, changes)
    assert len(problems) == 1 and "activates in 951 blocks" in problems[0]
    # ...and ignored when it is further out than the lookahead
    assert nodecheck.version_problems("11.5.0", 971_049, changes, lookahead=500) == []


def test_a_current_node_passes():
    assert nodecheck.version_problems("11.5.0", 971_049, CHANGES) == []
    assert nodecheck.version_problems("11.5.0", 971_049, None) == []
    assert nodecheck.version_problems("12.0.0", 1_600_000, CHANGES) == []


def test_the_built_in_floor_applies_without_the_remote_list():
    problems = nodecheck.version_problems("11.4.0", 969_488, None)
    assert len(problems) == 1 and "older than v11.5.0" in problems[0]


def test_an_unreadable_version_is_refused():
    assert nodecheck.version_problems(None, 1, CHANGES) == [
        "cannot read the node's version (None); refusing to compose blind"]


def test_per_network_activation_heights():
    # testnet4 at height 150,000: the reveal rule (154,564) is within the lookahead
    assert any("require_reveal_source_signature" in p
               for p in nodecheck.version_problems("11.4.0", 150_000, CHANGES, network="testnet4"))
    # a change without a height for that network cannot be judged and is skipped
    assert nodecheck.version_problems("11.5.0", 10, {"x": CHANGES["far_future"]}, network="testnet4") == []


class FakeCp:
    def __init__(self, version, height):
        self._status = {"version": version, "counterparty_height": height}

    def status(self):
        return self._status


def test_require_current_node_reports_and_refuses(monkeypatch):
    monkeypatch.setattr(nodecheck, "fetch_protocol_changes", lambda: CHANGES)
    out = io.StringIO()
    assert nodecheck.require_current_node(FakeCp("11.4.0", 969_488), out=out) is False
    text = out.getvalue()
    assert "refusing to compose" in text and "upgrade counterparty-server" in text
    out = io.StringIO()
    assert nodecheck.require_current_node(FakeCp("11.5.0", 971_049), out=out) is True
    assert out.getvalue() == ""


def test_require_current_node_without_the_remote_list_warns_but_keeps_the_floor(monkeypatch):
    monkeypatch.setattr(nodecheck, "fetch_protocol_changes", lambda: None)
    out = io.StringIO()
    assert nodecheck.require_current_node(FakeCp("11.5.0", 971_049), out=out) is True
    assert "could not fetch" in out.getvalue()
    out = io.StringIO()
    assert nodecheck.require_current_node(FakeCp("11.4.0", 971_049), out=out) is False
