"""Unit tests for `wallet receive` address-type handling.

A wallet serves the address types it has descriptors for: taproot (bc1p),
segwit (bc1q), and legacy (1...). Wallets imported before all-account restore
(or from a single key) may lack some types — those are skipped with a stderr
note, not a hard failure. Only a wallet with no served type at all is an error.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counters.commands import wallet  # noqa: E402
from counters.config import Config  # noqa: E402

TR_DESC = "tr([305f614d/86h/0h/0h]xpub6C.../0/*)#aaaaaaaa"
WPKH_DESC = "wpkh([305f614d/84h/0h/0h]xpub6D.../0/*)#dddddddd"
PKH_DESC = "pkh([305f614d/44h/0h/0h]xpub6B.../0/*)#bbbbbbbb"


def _fake_btc(descriptors):
    class FakeBtc:
        def __init__(self, config):
            pass

        def wallet_call(self, name, method, params=None, timeout=-1.0):
            if method == "listdescriptors":
                return {"descriptors": descriptors}
            if method == "getnewaddress":
                _, addr_type = params
                return {"bech32m": "bc1pNEW", "bech32": "bc1qNEW",
                        "legacy": "1NEW"}[addr_type]
            raise AssertionError(f"unexpected RPC {method}")

        def _call(self, method, params=None):
            assert method == "deriveaddresses"
            desc, (lo, hi) = params
            stem = {"tr(": "bc1p", "wpkh(": "bc1q"}.get(desc[:desc.index("(") + 1], "1A")
            return [f"{stem}addr{i}" for i in range(lo, hi + 1)]

    return FakeBtc


def test_receive_serves_all_three_types(monkeypatch, capsys):
    monkeypatch.setattr(wallet, "BitcoindClient", _fake_btc([
        {"desc": TR_DESC, "internal": False},
        {"desc": TR_DESC, "internal": True},
        {"desc": WPKH_DESC, "internal": False},
        {"desc": PKH_DESC, "internal": False},
    ]))
    rc = wallet.cmd_wallet_receive(Config(), "w")
    cap = capsys.readouterr()
    assert rc == 0
    assert "taproot: bc1paddr0" in cap.out
    assert "segwit:  bc1qaddr0" in cap.out
    assert "legacy:  1Aaddr0" in cap.out
    assert cap.err == ""


def test_receive_skips_missing_types_with_notes(monkeypatch, capsys):
    # A taproot-only wallet (imported before all-account restore) still serves
    # its taproot address; the missing segwit and legacy types are notes, not
    # failures.
    monkeypatch.setattr(wallet, "BitcoindClient", _fake_btc([
        {"desc": TR_DESC, "internal": False},
        {"desc": TR_DESC, "internal": True},
    ]))
    rc = wallet.cmd_wallet_receive(Config(), "w", new=True)
    cap = capsys.readouterr()
    assert rc == 0
    assert "taproot: bc1pNEW" in cap.out
    assert "segwit" not in cap.out
    assert "legacy" not in cap.out
    assert "no segwit descriptor" in cap.err
    assert "no legacy descriptor" in cap.err


def test_receive_no_served_types_errors(monkeypatch, capsys):
    # Only internal/unranged descriptors -> nothing to serve.
    monkeypatch.setattr(wallet, "BitcoindClient", _fake_btc([
        {"desc": TR_DESC, "internal": True},
        {"desc": "pkh(cWIFsinglekey)#cccccccc", "internal": False},
    ]))
    rc = wallet.cmd_wallet_receive(Config(), "w")
    cap = capsys.readouterr()
    assert rc == 1
    assert "no receive descriptor for any served address type" in cap.err


def test_receive_number_pairs_by_index(monkeypatch, capsys):
    monkeypatch.setattr(wallet, "BitcoindClient", _fake_btc([
        {"desc": TR_DESC, "internal": False},
        {"desc": WPKH_DESC, "internal": False},
        {"desc": PKH_DESC, "internal": False},
    ]))
    rc = wallet.cmd_wallet_receive(Config(), "w", number=2)
    cap = capsys.readouterr()
    assert rc == 0
    assert "index 0:" in cap.out and "index 1:" in cap.out
    assert "bc1paddr1" in cap.out and "bc1qaddr1" in cap.out and "1Aaddr1" in cap.out
