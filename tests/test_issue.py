"""Unit tests for `counters wallet lock` and `counters wallet issue`.

No network/Core: Bitcoin Core and Counterparty clients are faked, and the
wallet-address lookup is monkeypatched. Covers owner resolution/authorisation,
the lock guard, quantity conversion, the --lock flag, and dry-run vs broadcast.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import counters.commands.issue as I  # noqa: E402
from counters.config import Config  # noqa: E402
from counters.counterparty import CounterpartyClient  # noqa: E402

OWNER = "bc1pOwnerAddrxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"


class FakeBtc:
    def __init__(self):
        self.sent = None

    def wallet_call(self, wallet, method, params=None, timeout=-1.0):
        assert method == "signrawtransactionwithwallet"
        return {"complete": True, "hex": "signed00"}

    def _call(self, method, params=None):
        if method == "testmempoolaccept":
            return [{"allowed": True, "txid": "tt"}]
        if method == "sendrawtransaction":
            self.sent = params[0]
            return "broadcasttxid"
        raise AssertionError(f"unexpected _call {method}")


class FakeCp:
    def __init__(self, info):
        self.info = info
        self.compose_kwargs = None

    def get_asset(self, asset):
        if self.info and asset.upper() == self.info["asset"].upper():
            return self.info
        return None

    def compose_issuance(self, **kwargs):
        self.compose_kwargs = kwargs
        return {"rawtransaction": "aa"}


def _patch(info, addresses):
    fake_btc, fake_cp = FakeBtc(), FakeCp(info)
    orig = (I.BitcoindClient, I.CounterpartyClient, I._wallet_addresses)
    I.BitcoindClient = lambda cfg: fake_btc
    I.CounterpartyClient = lambda cfg: fake_cp
    I._wallet_addresses = lambda btc, wallet: addresses
    return fake_btc, fake_cp, orig


def _restore(orig):
    I.BitcoindClient, I.CounterpartyClient, I._wallet_addresses = orig


def _asset(name="MYASSET", divisible=False, locked=False, description="hi", owner=OWNER):
    return {"asset": name, "asset_id": "123", "owner": owner, "issuer": "1Creator",
            "divisible": divisible, "locked": locked, "description": description,
            "supply": 100, "asset_longname": None}


# --- owner resolution / authorisation --------------------------------------

def test_resolve_owned_asset_ok():
    _btc, _cp, orig = _patch(_asset(), [OWNER])
    try:
        canonical, info, owner = I._resolve_owned_asset(I.BitcoindClient(None),
                                                        I.CounterpartyClient(None),
                                                        "myasset", "myasset")
        assert canonical == "MYASSET" and owner == OWNER and info["asset"] == "MYASSET"
    finally:
        _restore(orig)


def test_resolve_rejects_reserved():
    _btc, _cp, orig = _patch(_asset("XCP"), [OWNER])
    try:
        assert I._resolve_owned_asset(I.BitcoindClient(None), I.CounterpartyClient(None),
                                      "me", "XCP") is None
    finally:
        _restore(orig)


def test_resolve_rejects_unknown():
    _btc, _cp, orig = _patch(None, [OWNER])
    try:
        assert I._resolve_owned_asset(I.BitcoindClient(None), I.CounterpartyClient(None),
                                      "me", "NOPE") is None
    finally:
        _restore(orig)


def test_resolve_rejects_not_owned():
    # Owner address is NOT among the wallet's addresses -> cannot issue.
    _btc, _cp, orig = _patch(_asset(), ["bc1pSomeoneElse"])
    try:
        assert I._resolve_owned_asset(I.BitcoindClient(None), I.CounterpartyClient(None),
                                      "me", "MYASSET") is None
    finally:
        _restore(orig)


# --- lock -------------------------------------------------------------------

def test_lock_supply_composes_zero_quantity_lock_true_dry_run():
    fake_btc, fake_cp, orig = _patch(_asset(divisible=False, description="keep me"), [OWNER])
    try:
        rc = I.cmd_lock_supply(Config(), "me", "MYASSET", dry_run=True)
        assert rc == 0
        k = fake_cp.compose_kwargs
        assert k["source"] == OWNER and k["asset"] == "MYASSET"
        assert k["quantity"] == 0 and k["lock"] is True
        assert k["divisible"] is False
        # description OMITTED (None): under v3 it is file content; omitting it
        # is how Counterparty preserves it (re-sending would corrupt/fail).
        assert k["description"] is None
        assert fake_btc.sent is None             # dry-run: nothing broadcast
    finally:
        _restore(orig)


def test_lock_supply_broadcasts_when_not_dry_run():
    fake_btc, fake_cp, orig = _patch(_asset(), [OWNER])
    try:
        rc = I.cmd_lock_supply(Config(), "me", "MYASSET")
        assert rc == 0 and fake_btc.sent == "signed00"
    finally:
        _restore(orig)


def test_lock_supply_already_locked_is_rejected():
    fake_btc, fake_cp, orig = _patch(_asset(locked=True), [OWNER])
    try:
        rc = I.cmd_lock_supply(Config(), "me", "MYASSET")
        assert rc == 1 and fake_cp.compose_kwargs is None and fake_btc.sent is None
    finally:
        _restore(orig)


# --- lock-description -------------------------------------------------------

def test_lock_description_issues_lock_description_keyword():
    fake_btc, fake_cp, orig = _patch(_asset(divisible=False, description="ipfs://cid"), [OWNER])
    try:
        rc = I.cmd_lock_description(Config(), "me", "MYASSET", dry_run=True)
        assert rc == 0
        k = fake_cp.compose_kwargs
        assert k["description"] == "LOCK_DESCRIPTION"   # the magic string
        assert k["quantity"] == 0 and k["lock"] is False
        assert k["divisible"] is False
        assert fake_btc.sent is None
    finally:
        _restore(orig)


def test_lock_description_broadcasts_when_not_dry_run():
    fake_btc, fake_cp, orig = _patch(_asset(), [OWNER])
    try:
        rc = I.cmd_lock_description(Config(), "me", "MYASSET")
        assert rc == 0 and fake_btc.sent == "signed00"
    finally:
        _restore(orig)


def test_lock_description_requires_ownership():
    fake_btc, fake_cp, orig = _patch(_asset(), ["bc1pSomeoneElse"])
    try:
        rc = I.cmd_lock_description(Config(), "me", "MYASSET")
        assert rc == 1 and fake_cp.compose_kwargs is None
    finally:
        _restore(orig)


# --- issue ------------------------------------------------------------------

def test_issue_indivisible_quantity_whole_units():
    fake_btc, fake_cp, orig = _patch(_asset(divisible=False), [OWNER])
    try:
        rc = I.cmd_issue(Config(), "me", "MYASSET", "100")
        assert rc == 0
        k = fake_cp.compose_kwargs
        assert k["quantity"] == 100 and k["divisible"] is False and k["lock"] is False
        assert fake_btc.sent == "signed00"
    finally:
        _restore(orig)


def test_issue_divisible_quantity_scaled_and_lock_flag():
    fake_btc, fake_cp, orig = _patch(_asset(divisible=True), [OWNER])
    try:
        rc = I.cmd_issue(Config(), "me", "MYASSET", "0.5", lock=True, dry_run=True)
        assert rc == 0
        k = fake_cp.compose_kwargs
        assert k["quantity"] == 50_000_000 and k["divisible"] is True
        assert k["lock"] is True
        assert fake_btc.sent is None
    finally:
        _restore(orig)


def test_issue_rejects_fractional_indivisible():
    fake_btc, fake_cp, orig = _patch(_asset(divisible=False), [OWNER])
    try:
        rc = I.cmd_issue(Config(), "me", "MYASSET", "1.5")
        assert rc == 1 and fake_cp.compose_kwargs is None
    finally:
        _restore(orig)


def test_issue_on_locked_asset_is_rejected():
    fake_btc, fake_cp, orig = _patch(_asset(locked=True), [OWNER])
    try:
        rc = I.cmd_issue(Config(), "me", "MYASSET", "10")
        assert rc == 1 and fake_cp.compose_kwargs is None and fake_btc.sent is None
    finally:
        _restore(orig)


# --- compose_issuance param handling ----------------------------------------

class _CapCp(CounterpartyClient):
    def __init__(self):
        self.captured = None

    def _get(self, path, params=None):
        self.captured = (path, params)
        return {"result": {"rawtransaction": "00"}}


def test_compose_issuance_omits_inputs_set_and_description_by_default():
    cp = _CapCp()
    cp.compose_issuance(source="addr", asset="FOO", quantity=0, divisible=False, lock=True)
    _path, params = cp.captured
    assert "inputs_set" not in params      # standalone issuance: no RC4 pinning
    assert "description" not in params      # omitted -> Counterparty keeps current
    assert params["lock"] == "true" and params["quantity"] == 0


def test_compose_issuance_includes_them_when_given():
    cp = _CapCp()
    cp.compose_issuance(source="addr", asset="FOO", quantity=5, divisible=True,
                        inputs_set="txid:0:1:aa", description="set this", lock=False)
    _path, params = cp.captured
    assert params["inputs_set"] == "txid:0:1:aa"
    assert params["description"] == "set this"
    assert params["divisible"] == "true" and params["lock"] == "false"


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
