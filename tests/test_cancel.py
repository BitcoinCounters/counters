"""Unit tests for `counters wallet cancel` (no network/Core needed).

Covers the replacement-fee arithmetic, which transactions are offered as
cancellable, and the three ways a cancel is refused: unknown txid, inputs the
wallet cannot re-sign (an inscription's reveal), and inputs too small to cover
the replacement fee.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import counters.commands.cancel as C  # noqa: E402
from counters.bitcoind import BitcoindError  # noqa: E402
from counters.config import Config  # noqa: E402

TXID = "aa" * 32
PREV = "bb" * 32
RECLAIM = "bc1pReclaimxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"


class FakeBtc:
    """Bitcoin Core stand-in. One unconfirmed wallet-funded transaction by
    default, spending a single `input_sat` UTXO."""

    def __init__(self, rows=None, entries=None, input_sat=1_000_000,
                 sign_complete=True, allowed=True, utxos=None):
        self.utxos = utxos if utxos is not None else []
        self.rows = rows if rows is not None else [
            {"txid": TXID, "confirmations": 0, "category": "send"},
        ]
        self.entries = entries if entries is not None else {
            TXID: {"vsize": 154, "fees": {"base": 0.00000109, "descendant": 0.00011263},
                   "descendantsize": 16088, "descendantcount": 2},
        }
        self.input_sat = input_sat
        self.sign_complete = sign_complete
        self.allowed = allowed
        self.created = []          # every createrawtransaction call
        self.sent = None

    def wallet_call(self, wallet, method, params=None, timeout=-1.0):
        if method == "listunspent":
            return list(self.utxos)
        if method == "listtransactions":
            return list(self.rows)
        if method == "gettransaction":
            txid = params[0]
            row = next((r for r in self.rows if r["txid"] == txid), None)
            info = {"hex": "orighex", "bip125-replaceable": "no"}
            if row is None or "fee" in row:
                info["fee"] = (row or {}).get("fee", -0.00000109)
            return info
        if method == "listdescriptors":
            return {"descriptors": [{"desc": "tr(xpub.../1/*)", "active": True,
                                     "internal": True}]}
        if method == "getrawchangeaddress":
            assert params == ["bech32m"]
            return RECLAIM
        if method == "signrawtransactionwithwallet":
            return {"complete": self.sign_complete, "hex": "signedhex", "errors": []}
        raise AssertionError(f"unexpected wallet_call {method}")

    def _call(self, method, params=None):
        if method == "getmempoolentry":
            if params[0] not in self.entries:
                raise BitcoindError("transaction not in mempool")
            return self.entries[params[0]]
        if method == "decoderawtransaction":
            if params[0] == "orighex":
                return {"vsize": 154,
                        "vin": [{"txid": PREV, "vout": 0}],
                        "vout": [{"value": 0.00988737,
                                  "scriptPubKey": {"address": "bc1pSomewhere"}}]}
            return {"vsize": 111, "vin": [], "vout": []}     # the signed replacement
        if method == "getrawtransaction":
            return {"vout": [{"value": self.input_sat / 1e8}]}
        if method == "createrawtransaction":
            self.created.append(params)
            return "rawhex"
        if method == "estimatesmartfee":
            return {"feerate": 0.0000271}          # 2.71 sat/vB
        if method == "testmempoolaccept":
            return [{"allowed": self.allowed, "reject-reason": "insufficient fee"}]
        if method == "sendrawtransaction":
            self.sent = params[0]
            return "replacementtxid"
        raise AssertionError(f"unexpected _call {method}")


def _patch(btc):
    orig = C.BitcoindClient
    C.BitcoindClient = lambda cfg: btc
    return orig


def _restore(orig):
    C.BitcoindClient = orig


# --- fee arithmetic ---------------------------------------------------------

def test_required_fee_out_pays_everything_evicted_plus_bandwidth():
    # The real case: 11263 sat of commit+reveal fees, a 111 vB replacement.
    assert C._required_fee(111, 11263, 1.71) == 11263 + 111


def test_required_fee_uses_the_rate_rule_when_it_dominates():
    # A cheap original but a high requested rate: rate wins.
    assert C._required_fee(100, 100, 50) == 5001


# --- which transactions are offered ----------------------------------------

def test_pending_lists_the_unconfirmed_wallet_transaction():
    btc = FakeBtc(rows=[{"txid": TXID, "confirmations": 0, "fee": -0.00000109}])
    got = C._pending(btc, "me")
    assert len(got) == 1
    assert got[0]["fee"] == 109 and got[0]["evicted_fee"] == 11263
    assert got[0]["evicted_count"] == 2 and got[0]["replaceable"] is False


def test_pending_skips_receives_confirmed_and_dropped():
    btc = FakeBtc(rows=[
        {"txid": TXID, "confirmations": 0},                      # no fee: a receive
        {"txid": "cc" * 32, "confirmations": 3, "fee": -0.001},   # confirmed
        {"txid": "dd" * 32, "confirmations": 0, "fee": -0.001},   # not in mempool
    ])
    assert C._pending(btc, "me") == []


# --- cancelling -------------------------------------------------------------

def _rows():
    return [{"txid": TXID, "confirmations": 0, "fee": -0.00000109}]


def test_cancel_dry_run_builds_a_replacement_but_does_not_broadcast():
    btc = FakeBtc(rows=_rows())
    orig = _patch(btc)
    try:
        rc = C.cmd_cancel(Config(), "me", txid=TXID, assume_yes=True, dry_run=True)
        assert rc == 0 and btc.sent is None
        # Final build: same input, one output, reclaiming input - required fee.
        inputs, outputs = btc.created[-1]
        assert inputs == [{"txid": PREV, "vout": 0, "sequence": 0xFFFFFFFD}]
        assert outputs == {RECLAIM: "0.00988626"}       # 1000000 - (11263 + 111)
    finally:
        _restore(orig)


def test_cancel_broadcasts_when_confirmed():
    btc = FakeBtc(rows=_rows())
    orig = _patch(btc)
    try:
        rc = C.cmd_cancel(Config(), "me", txid=TXID, assume_yes=True)
        assert rc == 0 and btc.sent == "signedhex"
    finally:
        _restore(orig)


def test_cancel_refuses_a_transaction_the_wallet_cannot_re_sign():
    # An inscription's reveal: its input is signed with Counterparty's key.
    btc = FakeBtc(rows=_rows(), sign_complete=False)
    orig = _patch(btc)
    try:
        rc = C.cmd_cancel(Config(), "me", txid=TXID, assume_yes=True)
        assert rc == 1 and btc.sent is None
    finally:
        _restore(orig)


def test_cancel_refuses_when_the_fee_would_eat_the_inputs():
    # Inputs worth less than the replacement fee: nothing to reclaim.
    btc = FakeBtc(rows=_rows(), input_sat=11_000)
    orig = _patch(btc)
    try:
        rc = C.cmd_cancel(Config(), "me", txid=TXID, assume_yes=True)
        assert rc == 1 and btc.sent is None
    finally:
        _restore(orig)


def test_cancel_does_not_broadcast_a_rejected_replacement():
    btc = FakeBtc(rows=_rows(), allowed=False)
    orig = _patch(btc)
    try:
        rc = C.cmd_cancel(Config(), "me", txid=TXID, assume_yes=True)
        assert rc == 1 and btc.sent is None
    finally:
        _restore(orig)


def test_direct_to_miner_prices_by_size_not_by_what_it_evicts():
    # Relay policy would demand 11263 + 111; a miner only needs the replacement
    # to pay its own way, because replacing frees ~16 kvB of block space.
    btc = FakeBtc(rows=_rows())
    orig = _patch(btc)
    try:
        rc = C.cmd_cancel(Config(), "me", txid=TXID, fee_rate=3, assume_yes=True,
                          no_mempool_check=True)
        assert rc == 0
        assert btc.sent is None                  # never broadcast: relay would reject
        _inputs, outputs = btc.created[-1]
        # 111 vB at 3 sat/vB = 334 sat, vs 11374 under relay policy.
        assert outputs == {RECLAIM: "0.00999666"}          # 1000000 - 334
    finally:
        _restore(orig)


def test_direct_to_miner_defaults_to_the_next_block_rate():
    btc = FakeBtc(rows=_rows())
    orig = _patch(btc)
    try:
        rc = C.cmd_cancel(Config(), "me", txid=TXID, assume_yes=True,
                          no_mempool_check=True)
        assert rc == 0 and btc.sent is None
        _inputs, outputs = btc.created[-1]
        # estimatesmartfee -> 2.71 sat/vB; 111 vB -> int(300.81)+1 = 301 sat
        assert outputs == {RECLAIM: "0.00999699"}
    finally:
        _restore(orig)


def test_miner_fee_floors_at_one_sat_per_vbyte():
    assert C._miner_fee(111, 0.1) == 111


def test_pending_finds_a_change_only_transaction():
    # Bitcoin Core emits no listtransactions row for a transaction whose only
    # output pays the wallet's own change address (empty `details`), so it must
    # be discovered through its unspent output instead.
    btc = FakeBtc(rows=[], utxos=[{"txid": TXID, "vout": 0, "confirmations": 0}])
    btc.rows = []                                   # nothing in listtransactions
    got = C._pending(btc, "me")
    assert [t["txid"] for t in got] == [TXID]


def test_cancel_rejects_an_unknown_txid():
    btc = FakeBtc(rows=_rows())
    orig = _patch(btc)
    try:
        rc = C.cmd_cancel(Config(), "me", txid="ff" * 32, assume_yes=True)
        assert rc == 1 and btc.created == []
    finally:
        _restore(orig)


def test_cancel_with_nothing_pending_is_a_no_op():
    btc = FakeBtc(rows=[])
    orig = _patch(btc)
    try:
        assert C.cmd_cancel(Config(), "me", assume_yes=True) == 0
    finally:
        _restore(orig)


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    print(f"\n{'OK' if failures == 0 else f'{failures} FAILED'}")
    raise SystemExit(1 if failures else 0)
