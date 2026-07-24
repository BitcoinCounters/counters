"""Unit tests for `counters wallet inscribe` source selection (no network/Core).

The one bit of real logic worth pinning: auto-picking a source that can
actually fund a TAPROOT commit — spendable segwit BTC, plus >= 0.5 XCP on the
same address for a named asset. The historical bug was choosing a legacy 1...
address that held the XCP but no (segwit) BTC, which always failed at compose.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counters.commands.inscribe import (  # noqa: E402
    NAMED_ISSUANCE_FEE_XCP,
    _is_segwit_address,
    _pick_source,
    _reveal_fee_sat,
)

XCP = NAMED_ISSUANCE_FEE_XCP        # 0.5 XCP in sats
LEGACY = "17mYxHSR2G9LVsPkmTjHRjK8TCiWwmPXxT"
TAPROOT = "bc1px9kxjuc9f8fz5lnnlca0wgl86suncxwjhw80eq7nx3c2asldrjhssneteg"
TAPROOT2 = "bc1phha2586ft7dw5teul5r4l3y8zra7nc8d74g999avtaqcm9zpxdqqgfhl83"
SEGWIT = "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu"
NESTED = "37VucYSaXLCAsxYyAPfbSi9eh4iEcbShgf"


class _DuckCp:
    def __init__(self, xcp):  # {address: sats}
        self._xcp = xcp

    def get_xcp_balance(self, addr):
        return self._xcp.get(addr, 0)


def test_is_segwit_address():
    assert _is_segwit_address(TAPROOT)
    assert _is_segwit_address(SEGWIT)
    assert _is_segwit_address(NESTED)      # nested segwit spends with a witness
    assert not _is_segwit_address(LEGACY)  # legacy P2PKH can't fund taproot


def test_named_skips_legacy_xcp_for_funded_segwit():
    # The real scenario: XCP sits on a legacy address with no BTC, but another
    # segwit address holds both XCP and spendable BTC. Pick the segwit one.
    cp = _DuckCp({LEGACY: 100_000_000, TAPROOT: 100_000_000})
    spendable = {TAPROOT: 100_000}  # legacy has no spendable BTC
    src, err = _pick_source(cp, {LEGACY, TAPROOT}, spendable,
                            named=True, inputs_set=None)
    assert src == TAPROOT and err is None


def test_named_prefers_richest_eligible_holder():
    cp = _DuckCp({TAPROOT: XCP, TAPROOT2: XCP})
    spendable = {TAPROOT: 5_000, TAPROOT2: 100_000}
    src, err = _pick_source(cp, {TAPROOT, TAPROOT2}, spendable,
                            named=True, inputs_set=None)
    assert src == TAPROOT2 and err is None  # more BTC to fund the commit


def test_named_no_xcp_anywhere_errors():
    cp = _DuckCp({})
    src, err = _pick_source(cp, {TAPROOT}, {TAPROOT: 100_000},
                            named=True, inputs_set=None)
    assert src is None and "XCP" in err


def test_named_xcp_only_on_legacy_reports_split():
    # XCP is stranded on legacy with no segwit-BTC co-located: unfundable.
    cp = _DuckCp({LEGACY: 100_000_000})
    spendable = {TAPROOT: 100_000}  # segwit BTC exists but holds no XCP
    src, err = _pick_source(cp, {LEGACY, TAPROOT}, spendable,
                            named=True, inputs_set=None)
    assert src is None
    assert LEGACY in err and "single-source" in err


def test_named_inputs_set_relaxes_btc_requirement():
    cp = _DuckCp({LEGACY: 100_000_000})
    src, err = _pick_source(cp, {LEGACY}, {}, named=True, inputs_set="abcd:0")
    assert src == LEGACY and err is None


def test_numeric_skips_legacy_even_when_richer():
    # Free numeric asset: no XCP needed, but the source still must be segwit.
    # A legacy address with MORE BTC must not be chosen over a segwit one.
    cp = _DuckCp({})
    spendable = {LEGACY: 1_000_000, TAPROOT: 50_000}
    src, err = _pick_source(cp, {LEGACY, TAPROOT}, spendable,
                            named=False, inputs_set=None)
    assert src == TAPROOT and err is None


def test_numeric_no_segwit_btc_errors():
    cp = _DuckCp({})
    src, err = _pick_source(cp, {LEGACY}, {LEGACY: 1_000_000},
                            named=False, inputs_set=None)
    assert src is None and "segwit" in err


# --- reveal fee ------------------------------------------------------------

def test_reveal_fee_whole_envelope_output_is_fee():
    # Reveal spends the commit's 330-sat envelope output and has no value out:
    # the whole thing is fee (the common case).
    commit = {"txid": "aa", "vout": [{"n": 0, "value": 0.0000033},
                                     {"n": 1, "value": 0.001}]}
    reveal = {"vin": [{"txid": "aa", "vout": 0}], "vout": []}
    assert _reveal_fee_sat(commit, reveal) == 330


def test_reveal_fee_with_change_output():
    commit = {"txid": "aa", "vout": [{"n": 0, "value": 0.00001}]}       # 1000 sat
    reveal = {"vin": [{"txid": "aa", "vout": 0}],
              "vout": [{"value": 0.000006}]}                            # 600 sat out
    assert _reveal_fee_sat(commit, reveal) == 400


def test_reveal_fee_unresolvable_input_returns_none():
    # An input not sourced from the commit can't be valued offline -> None.
    commit = {"txid": "aa", "vout": [{"n": 0, "value": 0.001}]}
    reveal = {"vin": [{"txid": "bb", "vout": 0}], "vout": []}
    assert _reveal_fee_sat(commit, reveal) is None
