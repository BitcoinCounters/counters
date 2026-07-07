"""`_wallet_change_address` must let legacy (Counterwallet/Electrum) wallets inscribe.

Those wallets are imported as flat single-key WIF descriptors with no active/
internal descriptor, so Bitcoin Core's `getrawchangeaddress` fails with "no
available keys". The inscribe flow must fall back to an address the wallet
already controls instead of dying, so a named inscription / reinscription can
still be funded from a restored Counterwallet.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counters.bitcoind import BitcoindError  # noqa: E402
from counters.commands.inscribe import _wallet_change_address  # noqa: E402


class _TaprootBtc:
    """A create/BIP39 wallet: getrawchangeaddress works."""

    def wallet_call(self, wallet, method, params=None, timeout=-1.0):
        if method == "getrawchangeaddress":
            assert params == ["bech32m"]
            return "bc1pfreshchangeaddr"
        raise AssertionError(f"unexpected wallet_call {method}")


class _LegacyBtc:
    """A Counterwallet WIF import: getrawchangeaddress fails; only known
    addresses are available (via listreceivedbyaddress / listunspent)."""

    def __init__(self, addresses):
        self._addresses = addresses

    def wallet_call(self, wallet, method, params=None, timeout=-1.0):
        if method == "getrawchangeaddress":
            raise BitcoindError("This wallet has no available keys")
        if method == "listreceivedbyaddress":
            return [{"address": a} for a in self._addresses]
        if method == "listunspent":
            return []
        raise AssertionError(f"unexpected wallet_call {method}")


def test_taproot_wallet_uses_fresh_bech32m_change():
    assert _wallet_change_address(_TaprootBtc(), "counter") == "bc1pfreshchangeaddr"


def test_legacy_wallet_falls_back_to_known_address():
    addr = _wallet_change_address(_LegacyBtc(["1AaaPepeAddr", "1BbbPepeAddr"]), "legacypepe")
    # Falls back to a wallet-controlled address (sorted first), not an error.
    assert addr == "1AaaPepeAddr"


def test_legacy_wallet_with_no_addresses_raises_clear_error():
    try:
        _wallet_change_address(_LegacyBtc([]), "empty")
    except BitcoindError as e:
        assert "cannot produce a change address" in str(e)
    else:
        raise AssertionError("expected a BitcoindError when no addresses are available")


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
    sys.exit(1 if failures else 0)
