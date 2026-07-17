"""Unit tests for `counters wallet send` helpers (no network/Core needed).

Covers the two bits of real logic: human->raw quantity conversion and picking
a single source address that holds enough of the asset.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import counters.commands.send as S  # noqa: E402
from counters.commands.send import _find_source, _to_raw_quantity  # noqa: E402
from counters.config import Config  # noqa: E402
from counters.counterparty import CounterpartyClient  # noqa: E402

DEST = "bc1pdestxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
ADDR_IN_WRONG_SLOT = "1FfZErPEuKK613V3CjViQfECQKmGsby7nR"
SOURCE = "bc1psourcexxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"


def test_to_raw_quantity_divisible():
    assert _to_raw_quantity("1", True) == 100_000_000
    assert _to_raw_quantity("0.5", True) == 50_000_000
    assert _to_raw_quantity("0.0003131", True) == 31_310


def test_to_raw_quantity_indivisible():
    assert _to_raw_quantity("1", False) == 1
    assert _to_raw_quantity("10", False) == 10


def test_to_raw_quantity_rejects_bad_input():
    for bad in ("0", "-1", "abc"):
        try:
            _to_raw_quantity(bad, True)
            assert False, f"{bad!r} should have raised"
        except ValueError:
            pass
    # fractional amount of an indivisible asset is invalid
    try:
        _to_raw_quantity("1.5", False)
        assert False, "fractional indivisible should have raised"
    except ValueError:
        pass


class _DuckCp:
    def __init__(self, balances):
        self._balances = balances

    def get_address_balances(self, addr):
        return self._balances.get(addr, [])


def _patch_addresses(addrs):
    S._wallet_addresses = lambda btc, wallet: addrs


def test_find_source_returns_first_address_with_enough():
    cp = _DuckCp({
        "bc1pA": [{"asset": "RAREPEPE", "asset_longname": None, "quantity": 3}],
        "bc1pB": [{"asset": "RAREPEPE", "asset_longname": None, "quantity": 10}],
    })
    orig = S._wallet_addresses
    _patch_addresses(["bc1pA", "bc1pB"])
    try:
        addr, have = _find_source(object(), cp, "me", "RAREPEPE", 5)
        assert addr == "bc1pB" and have == 10
        # When none has enough, returns the richest so the caller can report it.
        addr2, have2 = _find_source(object(), cp, "me", "RAREPEPE", 50)
        assert addr2 == "bc1pB" and have2 == 10
        # Unknown asset -> nothing found.
        none_addr, none_have = _find_source(object(), cp, "me", "NOPE", 1)
        assert none_addr is None and none_have == 0
    finally:
        S._wallet_addresses = orig


class _FakeBtc:
    def __init__(self, valid_addresses):
        self.valid = set(valid_addresses)
        self.sent = None

    def _call(self, method, params=None):
        if method == "validateaddress":
            return {"isvalid": params[0] in self.valid}
        if method == "testmempoolaccept":
            return [{"allowed": True, "txid": "tt"}]
        if method == "sendrawtransaction":
            self.sent = params[0]
            return "broadcasttxid"
        raise AssertionError(f"unexpected _call {method}")

    def wallet_call(self, wallet, method, params=None, timeout=-1.0):
        assert method == "signrawtransactionwithwallet"
        return {"complete": True, "hex": "signed00"}


class _FakeCp:
    def __init__(self, assets, balances):
        self.assets = assets           # {CANONICAL_NAME: info}
        self.balances = balances       # {address: [balance rows]}
        self.compose_kwargs = None

    def get_asset(self, asset):
        return self.assets.get(asset) or self.assets.get(asset.upper())

    def get_address_balances(self, addr):
        return self.balances.get(addr, [])

    def compose_send(self, source, asset, quantity, destination, sat_per_vbyte=None):
        self.compose_kwargs = dict(source=source, asset=asset, quantity=quantity,
                                   destination=destination, sat_per_vbyte=sat_per_vbyte)
        return {"rawtransaction": "aa"}


def _patch(btc, cp, addresses):
    orig = (S.BitcoindClient, S.CounterpartyClient, S._wallet_addresses)
    S.BitcoindClient = lambda cfg: btc
    S.CounterpartyClient = lambda cfg: cp
    S._wallet_addresses = lambda b, w: addresses
    return orig


def _restore(orig):
    S.BitcoindClient, S.CounterpartyClient, S._wallet_addresses = orig


def _rare(divisible=False):
    return {"RAREPEPE": {"asset": "RAREPEPE", "divisible": divisible,
                         "asset_longname": None}}


def test_send_rejects_invalid_destination_with_order_hint():
    # An asset name in the ADDRESS slot -> fails fast on address validation.
    btc = _FakeBtc(valid_addresses={DEST, SOURCE})
    cp = _FakeCp(_rare(), {SOURCE: [{"asset": "RAREPEPE", "quantity": 5}]})
    orig = _patch(btc, cp, [SOURCE])
    try:
        rc = S.cmd_send(Config(), "me", "RAREPEPE", "COUNTERZERO", "1")
        assert rc == 1
        assert cp.compose_kwargs is None and btc.sent is None
    finally:
        _restore(orig)


def test_send_flags_address_in_asset_slot():
    btc = _FakeBtc(valid_addresses={DEST, ADDR_IN_WRONG_SLOT, SOURCE})
    cp = _FakeCp(_rare(), {})
    orig = _patch(btc, cp, [SOURCE])
    try:
        # dest valid, but the ASSET slot holds an address -> unknown asset.
        rc = S.cmd_send(Config(), "me", DEST, ADDR_IN_WRONG_SLOT, "1")
        assert rc == 1 and cp.compose_kwargs is None
    finally:
        _restore(orig)


def test_send_flags_address_in_amount_slot():
    btc = _FakeBtc(valid_addresses={DEST, ADDR_IN_WRONG_SLOT, SOURCE})
    cp = _FakeCp(_rare(), {SOURCE: [{"asset": "RAREPEPE", "quantity": 5}]})
    orig = _patch(btc, cp, [SOURCE])
    try:
        # dest + asset valid, but AMOUNT is an address -> invalid amount.
        rc = S.cmd_send(Config(), "me", DEST, "RAREPEPE", ADDR_IN_WRONG_SLOT)
        assert rc == 1 and cp.compose_kwargs is None
    finally:
        _restore(orig)


def test_send_happy_path_passes_fee_rate_and_dry_run():
    btc = _FakeBtc(valid_addresses={DEST, SOURCE})
    cp = _FakeCp(_rare(), {SOURCE: [{"asset": "RAREPEPE", "quantity": 5}]})
    orig = _patch(btc, cp, [SOURCE])
    try:
        rc = S.cmd_send(Config(), "me", DEST, "RAREPEPE", "1", fee_rate=2.0, dry_run=True)
        assert rc == 0
        k = cp.compose_kwargs
        assert k["source"] == SOURCE and k["destination"] == DEST
        assert k["asset"] == "RAREPEPE" and k["quantity"] == 1
        assert k["sat_per_vbyte"] == 2.0
        assert btc.sent is None            # dry-run: nothing broadcast
    finally:
        _restore(orig)


class _CapCp(CounterpartyClient):
    def __init__(self):
        self.captured = None

    def _get(self, path, params=None):
        self.captured = (path, params)
        return {"result": {"rawtransaction": "00"}}


def test_compose_send_normalises_whole_fee_rate_to_int():
    cp = _CapCp()
    cp.compose_send("src", "FOO", 1, "dst", sat_per_vbyte=1.0)
    assert isinstance(cp.captured[1]["sat_per_vbyte"], int)   # 1, not 1.0
    cp.compose_send("src", "FOO", 1, "dst", sat_per_vbyte=1.5)
    assert cp.captured[1]["sat_per_vbyte"] == 1.5
    cp.compose_send("src", "FOO", 1, "dst")
    assert "sat_per_vbyte" not in cp.captured[1]       # omitted by default


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("ok")
