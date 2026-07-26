"""Unit tests for `counters wallet bump` (no network/Core needed).

Covers the CPFP fee arithmetic, which transactions can be bumped at all, and
the refusals — including the one that matters most: a transaction with no
spendable output (an inscription reveal) cannot be bumped by any child.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import counters.commands.bump as B  # noqa: E402
from counters.bitcoind import BitcoindError  # noqa: E402
from counters.config import Config  # noqa: E402

TXID = "ee" * 32
RECLAIM = "bc1pReclaimxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"


class FakeBtc:
    """Bitcoin Core stand-in: one unconfirmed transaction (154 vB at 0.56
    sat/vB) with a 9692-sat spendable change output."""

    def __init__(self, rows=None, entries=None, utxos=None,
                 sign_complete=True, allowed=True):
        self.rows = rows if rows is not None else [{"txid": TXID, "confirmations": 0}]
        self.entries = entries if entries is not None else {
            TXID: {"vsize": 154, "fees": {"base": 0.00000086, "ancestor": 0.00000086},
                   "ancestorsize": 154, "descendantcount": 2},
        }
        self.utxos = utxos if utxos is not None else [
            {"txid": TXID, "vout": 1, "amount": 0.00009692, "confirmations": 0,
             "spendable": True},
        ]
        self.sign_complete = sign_complete
        self.allowed = allowed
        self.created = []
        self.sent = None

    def wallet_call(self, wallet, method, params=None, timeout=-1.0):
        if method == "listunspent":
            return list(self.utxos)
        if method == "listtransactions":
            return list(self.rows)
        if method == "listdescriptors":
            return {"descriptors": [{"desc": "tr(xpub.../1/*)", "active": True,
                                     "internal": True}]}
        if method == "getrawchangeaddress":
            return RECLAIM
        if method == "signrawtransactionwithwallet":
            return {"complete": self.sign_complete, "hex": "signedchild", "errors": []}
        raise AssertionError(f"unexpected wallet_call {method}")

    def _call(self, method, params=None):
        if method == "getmempoolentry":
            if params[0] not in self.entries:
                raise BitcoindError("not in mempool")
            return self.entries[params[0]]
        if method == "decoderawtransaction":
            return {"vsize": 111}
        if method == "createrawtransaction":
            self.created.append(params)
            return "rawchild"
        if method == "testmempoolaccept":
            return [{"allowed": self.allowed, "reject-reason": "min relay fee not met"}]
        if method == "sendrawtransaction":
            self.sent = params[0]
            return "childtxid"
        raise AssertionError(f"unexpected _call {method}")


def _patch(btc):
    orig = B.BitcoindClient
    B.BitcoindClient = lambda cfg: btc
    return orig


def _restore(orig):
    B.BitcoindClient = orig


# --- fee arithmetic ---------------------------------------------------------

def test_child_pays_the_package_shortfall():
    # The real case: 154 vB parent at 86 sat, 111 vB child, target 3 sat/vB.
    # ceil(3 * 265) - 86 = 709
    assert B._child_fee(3, 154, 86, 111) == 709


def test_child_never_pays_less_than_its_own_bandwidth():
    # Ancestors already exceed the target: the shortfall is negative, but the
    # child still has to pay its own relay minimum.
    assert B._child_fee(0.5, 1000, 900, 100) == 100


# --- which transactions are offered ----------------------------------------

def test_pending_finds_a_change_only_transaction():
    # No listtransactions row (Core omits details for change-only outputs): the
    # transaction must still be found through its unspent output.
    btc = FakeBtc(rows=[])
    assert [t["txid"] for t in B._pending(btc, "me")] == [TXID]


def test_pending_attaches_the_spendable_outputs():
    btc = FakeBtc()
    got = B._pending(btc, "me")
    assert len(got) == 1
    assert got[0]["anc_vsize"] == 154 and got[0]["anc_fee"] == 86
    assert got[0]["descendants"] == 1
    assert [u["vout"] for u in got[0]["utxos"]] == [1]


def test_pending_skips_confirmed_and_dropped():
    btc = FakeBtc(utxos=[], rows=[
        {"txid": "cc" * 32, "confirmations": 2},     # confirmed
        {"txid": "dd" * 32, "confirmations": 0},     # not in mempool
    ])
    assert B._pending(btc, "me") == []


# --- bumping ----------------------------------------------------------------

def test_dry_run_builds_the_child_without_broadcasting():
    btc = FakeBtc()
    orig = _patch(btc)
    try:
        rc = B.cmd_bump(Config(), "me", txid=TXID, fee_rate=3, assume_yes=True,
                        dry_run=True)
        assert rc == 0 and btc.sent is None
        inputs, outputs = btc.created[-1]
        assert inputs == [{"txid": TXID, "vout": 1, "sequence": 0xFFFFFFFD}]
        assert outputs == {RECLAIM: "0.00008983"}       # 9692 - 709
    finally:
        _restore(orig)


def test_bump_broadcasts_the_child():
    btc = FakeBtc()
    orig = _patch(btc)
    try:
        rc = B.cmd_bump(Config(), "me", txid=TXID, fee_rate=3, assume_yes=True)
        assert rc == 0 and btc.sent == "signedchild"
    finally:
        _restore(orig)


def test_refuses_a_transaction_with_no_spendable_output():
    # The inscription-reveal case: nothing to attach a child to.
    btc = FakeBtc(utxos=[])
    orig = _patch(btc)
    try:
        rc = B.cmd_bump(Config(), "me", txid=TXID, fee_rate=3, assume_yes=True)
        assert rc == 1 and btc.created == [] and btc.sent is None
    finally:
        _restore(orig)


def test_refuses_a_target_at_or_below_the_current_rate():
    btc = FakeBtc()
    orig = _patch(btc)
    try:
        rc = B.cmd_bump(Config(), "me", txid=TXID, fee_rate=0.5, assume_yes=True)
        assert rc == 1 and btc.sent is None
    finally:
        _restore(orig)


def test_refuses_when_the_attachable_coin_cannot_cover_the_fee():
    btc = FakeBtc(utxos=[{"txid": TXID, "vout": 1, "amount": 0.00000400,
                          "confirmations": 0, "spendable": True}])
    orig = _patch(btc)
    try:
        rc = B.cmd_bump(Config(), "me", txid=TXID, fee_rate=50, assume_yes=True)
        assert rc == 1 and btc.sent is None
    finally:
        _restore(orig)


def test_does_not_broadcast_a_rejected_child():
    btc = FakeBtc(allowed=False)
    orig = _patch(btc)
    try:
        rc = B.cmd_bump(Config(), "me", txid=TXID, fee_rate=3, assume_yes=True)
        assert rc == 1 and btc.sent is None
    finally:
        _restore(orig)


def test_rejects_an_unknown_txid():
    btc = FakeBtc()
    orig = _patch(btc)
    try:
        assert B.cmd_bump(Config(), "me", txid="ff" * 32, fee_rate=3,
                          assume_yes=True) == 1
    finally:
        _restore(orig)


def test_nothing_pending_is_a_no_op():
    btc = FakeBtc(rows=[], utxos=[])
    orig = _patch(btc)
    try:
        assert B.cmd_bump(Config(), "me", fee_rate=3, assume_yes=True) == 0
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
