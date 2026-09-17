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
    _estimate_source_need,
    _fund_source,
    _is_segwit_address,
    _pick_source,
    _reveal_fee_sat,
    _subasset_parent_owner,
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
    # Nested 3... spends with a witness, but its scriptPubKey is P2SH — not a
    # witness program — so counterparty-core rejects it as a taproot source.
    assert not _is_segwit_address(NESTED)
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


# --- subassets --------------------------------------------------------------
#
# A subasset is not a source CHOICE: counterparty-core rejects any source but
# the parent's owner ("parent asset owned by another address"), and subassets
# have been free since block 866,000. Picking an XCP holder — the named-asset
# rule — funds an address that then cannot issue at all.

class _ParentCp(_DuckCp):
    def __init__(self, assets, xcp=None):
        super().__init__(xcp or {})
        self._assets = assets

    def get_asset(self, asset):
        return self._assets.get(asset.upper())


def test_subasset_source_is_the_parents_owner_not_the_xcp_holder():
    cp = _ParentCp({"DEGENT": {"asset": "DEGENT", "owner": TAPROOT, "issuer": LEGACY}},
                   xcp={TAPROOT2: 100_000_000})   # the XCP lives elsewhere
    owner, err = _subasset_parent_owner(cp, "DEGENT.0", {TAPROOT, TAPROOT2})
    assert owner == TAPROOT and err is None


def test_subasset_owner_falls_back_to_issuer_when_never_transferred():
    cp = _ParentCp({"DEGENT": {"asset": "DEGENT", "issuer": TAPROOT}})
    owner, err = _subasset_parent_owner(cp, "DEGENT.0", {TAPROOT})
    assert owner == TAPROOT and err is None


def test_subasset_parent_outside_the_wallet_is_rejected():
    cp = _ParentCp({"DEGENT": {"asset": "DEGENT", "owner": TAPROOT2}})
    owner, err = _subasset_parent_owner(cp, "DEGENT.0", {TAPROOT})
    assert owner is None and TAPROOT2 in err


def test_subasset_unknown_parent_is_rejected():
    cp = _ParentCp({})
    owner, err = _subasset_parent_owner(cp, "NOSUCH.0", {TAPROOT})
    assert owner is None and "unknown parent asset" in err


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


# --- funding a source that holds the XCP but no BTC -------------------------

class _FundBtc:
    """Bitcoin Core stand-in for the consolidation transfer."""

    def __init__(self, utxos=None):
        self.utxos = utxos if utxos is not None else [
            {"txid": "cc", "vout": 0, "address": "bc1pRich", "amount": 0.01,
             "spendable": True},
        ]
        self.sent = None

    def wallet_call(self, wallet, method, params=None, timeout=-1.0):
        if method == "listunspent":
            return list(self.utxos)
        if method == "listdescriptors":
            return {"descriptors": [{"desc": "tr(xpub.../1/*)", "active": True,
                                     "internal": True}]}
        if method == "send":
            self.sent = params
            return {"complete": True, "txid": "fundtxid"}
        raise AssertionError(f"unexpected wallet_call {method}")


def test_estimate_covers_the_real_inscription_cost():
    # Observed on-chain: a 62919-byte image composed to 15934 vB reveal + 154 vB
    # commit, so 16088 vB. The estimate must exceed that at any rate.
    for rate in (0.55, 1, 3, 10):
        assert _estimate_source_need(62919, rate) > (15934 + 154) * rate


def test_fund_source_pins_the_named_address_coins():
    btc = _FundBtc()
    txid = _fund_source(btc, "me", "bc1pXcpHolder", 5000, "bc1pRich", 3.0)
    assert txid == "fundtxid"
    outputs, _conf, _mode, fee_rate, options = btc.sent
    assert outputs == {"bc1pXcpHolder": "0.00005"}
    assert fee_rate == 3.0
    assert options["inputs"] == [{"txid": "cc", "vout": 0}]
    assert options["add_inputs"] is False          # only the named address pays
    assert options["change_type"] == "bech32m"


def test_fund_source_auto_lets_core_choose():
    btc = _FundBtc()
    assert _fund_source(btc, "me", "bc1pXcpHolder", 5000, None, None) == "fundtxid"
    _outputs, _conf, _mode, _rate, options = btc.sent
    assert "inputs" not in options                 # wallet-wide selection
    assert "add_inputs" not in options


def test_fund_source_refuses_an_address_with_no_coins():
    btc = _FundBtc(utxos=[])
    assert _fund_source(btc, "me", "bc1pXcpHolder", 5000, "bc1pRich", None) is None
    assert btc.sent is None


def test_pick_source_accepts_an_unfunded_xcp_holder_when_funding():
    # The whole point of --fund-from: the XCP holder has no BTC *yet*.
    cp = _DuckCp({"bc1pXcp": NAMED_ISSUANCE_FEE_XCP})
    src, err = _pick_source(cp, {"bc1pXcp", "bc1pRich"}, {"bc1pRich": 100000},
                            named=True, inputs_set=None, funding=True)
    assert src == "bc1pXcp" and err is None
    # ...and without it, the same wallet is refused.
    src2, err2 = _pick_source(cp, {"bc1pXcp", "bc1pRich"}, {"bc1pRich": 100000},
                              named=True, inputs_set=None)
    assert src2 is None and "single-source" in err2


# --- --commit-fee-rate: a CPFP child lifts the commit, the reveal keeps its rate --

from counters.commands.inscribe import _commit_child  # noqa: E402

_SRC = "bc1pSource"


def _commit_dec(change_sat=100_000, change_addr=_SRC, vsize=154):
    vout = [{"n": 0, "value": 0.0005,
             "scriptPubKey": {"address": "bc1pEnvelope", "hex": "5120aa"}}]
    if change_sat is not None:
        vout.append({"n": 1, "value": change_sat / 1e8,
                     "scriptPubKey": {"address": change_addr, "hex": "5120bb"}})
    return {"txid": "c0" * 32, "vsize": vsize, "vout": vout}


class _ChildBtc:
    def __init__(self, child_vsize=111):
        self.child_vsize = child_vsize
        self.signed = []

    def _call(self, method, params=None):
        if method == "createrawtransaction":
            return {"inputs": params[0], "outputs": params[1]}
        if method == "decoderawtransaction":
            return {"vsize": self.child_vsize}
        raise AssertionError(method)

    def wallet_call(self, wallet, method, params=None, timeout=-1.0):
        assert method == "signrawtransactionwithwallet"
        raw, prevtxs = params
        self.signed.append((raw, prevtxs))
        return {"complete": True, "hex": f"child{len(self.signed)}"}


def test_commit_child_brings_the_commit_package_to_the_rate():
    btc = _ChildBtc(child_vsize=111)
    # Composed at 1 sat/vB: 154 sat over 154 vB. Target 3 over 154+111 vB = 795.
    hex_, vsize, fee, err = _commit_child(btc, "w", _commit_dec(), 154, _SRC, 3)
    assert err is None and hex_ == "child2" and vsize == 111
    assert fee == 795 - 154
    raw, prevtxs = btc.signed[-1]
    assert raw["inputs"] == [{"txid": "c0" * 32, "vout": 1, "sequence": 0xFFFFFFFD}]
    assert raw["outputs"] == {_SRC: f"{(100_000 - fee) / 1e8:.8f}"}
    # The commit is unbroadcast, so its output must be handed to the signer.
    assert prevtxs == [{"txid": "c0" * 32, "vout": 1, "scriptPubKey": "5120bb",
                        "amount": 0.001}]


def test_commit_child_not_needed_when_the_commit_already_pays():
    btc = _ChildBtc()
    assert _commit_child(btc, "w", _commit_dec(), 462, _SRC, 3) == (None, 0, 0, None)
    assert not btc.signed


def test_commit_child_needs_a_change_output_to_the_source():
    for dec in (_commit_dec(change_sat=None), _commit_dec(change_addr="bc1pElse")):
        hex_, _v, _f, err = _commit_child(_ChildBtc(), "w", dec, 154, _SRC, 3)
        assert hex_ is None and "no change output" in err


def test_commit_child_refuses_when_change_cannot_pay():
    hex_, _v, _f, err = _commit_child(_ChildBtc(), "w", _commit_dec(change_sat=900),
                                      154, _SRC, 3)
    assert hex_ is None and "cannot pay" in err
