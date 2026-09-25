"""Inscription IDs (build reference v3 §6.1): format/parse and resolution."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counters.ids import format_id, parse_id  # noqa: E402

TXID = "5dfbc6ffaae2939838c411edcb99952c768f9375a3fa090f42ebe6ecfef4d464"


def test_format_is_lowercase_txid_i_index():
    assert format_id(TXID) == TXID + "i0"
    assert format_id(TXID.upper(), 7) == TXID + "i7"


def test_parse_round_trips_and_normalizes():
    assert parse_id(TXID + "i0") == (TXID, 0)
    assert parse_id(TXID.upper() + "i12") == (TXID, 12)
    assert parse_id("  " + TXID + "i1 \n") == (TXID, 1)   # surrounding ws only
    txid, n = parse_id(format_id(TXID, 3))
    assert format_id(txid, n) == TXID + "i3"


def test_parse_refuses_everything_else():
    bad = [
        TXID,                    # bare txid: the index is load-bearing
        TXID + "i",              # no index
        TXID + "i01",            # leading zero
        TXID + "i-1",
        TXID[:-1] + "i0",        # 63 hex
        TXID + "0i0",            # 65 hex
        TXID + "i0x",            # trailing junk
        "counter:" + TXID + "i0",
        TXID.replace("5", "g", 1) + "i0",   # non-hex
        "42",                    # a number is not an ID
        "XDUALS",                # nor an asset name
        "",
    ]
    for token in bad:
        assert parse_id(token) is None, token
