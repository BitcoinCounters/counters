"""Unit tests for `wallet balance --detailed` per-address reporting.

The detailed view lists every funded address — one holding BTC, Counterparty
assets, or issuance rights — and omits empty ones. An unreachable Counterparty
never reads as "you own nothing": funded-by-BTC addresses still print with an
UNKNOWN note, and a run that showed nothing while queries failed exits 1.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counters.commands import wallet  # noqa: E402
from counters.config import Config  # noqa: E402
from counters.counterparty import CounterpartyError  # noqa: E402


def _fake_btc(unspent):
    class FakeBtc:
        def __init__(self, config):
            pass

        def wallet_call(self, name, method, params=None, timeout=-1.0):
            if method == "getbalances":
                return {"mine": {"trusted": 0.001, "untrusted_pending": 0,
                                 "immature": 0}}
            if method == "listreceivedbyaddress":
                return [{"address": "bc1pfirst"}, {"address": "1Aused"},
                        {"address": "bc1pempty"}]
            if method == "listunspent":
                return unspent
            if method == "listdescriptors":
                return {"descriptors": []}
            raise AssertionError(f"unexpected RPC {method}")

    return FakeBtc


def _fake_cp(balances, owned=None, broken=(), dispensers=None, orders=None):
    class FakeCp:
        def __init__(self, config):
            pass

        def get_address_balances(self, address):
            if address in broken:
                raise CounterpartyError("Counterparty not ready")
            return balances.get(address, [])

        def get_address_owned_assets(self, address):
            return (owned or {}).get(address, [])

        def get_address_dispensers(self, address):
            return (dispensers or {}).get(address, [])

        def get_address_orders(self, address, status=None):
            assert status == "open"
            return (orders or {}).get(address, [])

    return FakeCp


def test_detailed_lists_funded_addresses_and_skips_empty(monkeypatch, capsys):
    monkeypatch.setattr(wallet, "BitcoindClient", _fake_btc(
        [{"address": "bc1pfirst", "amount": 0.0006},
         {"address": "bc1pfirst", "amount": 0.0004}]))
    monkeypatch.setattr(wallet, "CounterpartyClient", _fake_cp(
        {"bc1pfirst": [{"asset": "MYTOKEN", "quantity": 5,
                        "asset_info": {"divisible": False}}]},
        owned={"1Aused": [{"asset": "A95428956661682177",
                           "asset_longname": None}]}))
    rc = wallet.cmd_wallet_balance(Config(), "w", detailed=True)
    cap = capsys.readouterr()
    assert rc == 0
    assert "BTC confirmed : 0.00100000" in cap.out
    # bc1pfirst: two UTXOs summed, plus its asset.
    assert "0.00100000" in cap.out.split("bc1pfirst")[1]
    assert "MYTOKEN" in cap.out
    # 1Aused holds only issuance rights; still shown.
    assert "A95428956661682177" in cap.out and "(ownership rights)" in cap.out
    # bc1pempty holds nothing at all: omitted.
    assert "bc1pempty" not in cap.out


def test_detailed_shows_open_dispensers_and_orders(monkeypatch, capsys):
    # 1Aused's only activity is an open dispenser: its escrowed stock is not an
    # address balance, so without the dispenser line it would be omitted.
    monkeypatch.setattr(wallet, "BitcoindClient", _fake_btc(
        [{"address": "bc1pfirst", "amount": 0.0005}]))
    monkeypatch.setattr(wallet, "CounterpartyClient", _fake_cp(
        {},
        dispensers={"1Aused": [
            {"asset": "MYTOKEN", "status": 0, "give_quantity": 1,
             "give_remaining": 28, "satoshirate": 2780,
             "asset_info": {"divisible": False}},
            {"asset": "GONE", "status": 10, "give_quantity": 1,
             "give_remaining": 0, "satoshirate": 1000,
             "asset_info": {"divisible": False}},
        ]},
        orders={"bc1pfirst": [
            {"give_asset": "XCP", "give_remaining": 100000000,
             "give_asset_info": {"divisible": True},
             "get_asset": "BTC", "get_remaining": 500000,
             "expire_index": 961000},
        ]}))
    rc = wallet.cmd_wallet_balance(Config(), "w", detailed=True)
    cap = capsys.readouterr()
    assert rc == 0
    assert "open dispenser: 1 MYTOKEN for 2780 sat (28 MYTOKEN remaining)" in cap.out
    assert "GONE" not in cap.out  # closed dispensers are not listed
    assert "open order: 1 XCP for 0.005 BTC, expires at block 961000" in cap.out


def test_detailed_unreachable_cp_still_shows_btc(monkeypatch, capsys):
    monkeypatch.setattr(wallet, "BitcoindClient", _fake_btc(
        [{"address": "bc1pfirst", "amount": 0.0005}]))
    monkeypatch.setattr(wallet, "CounterpartyClient", _fake_cp(
        {}, broken={"bc1pfirst", "1Aused", "bc1pempty"}))
    rc = wallet.cmd_wallet_balance(Config(), "w", detailed=True)
    cap = capsys.readouterr()
    assert rc == 0
    assert "bc1pfirst" in cap.out
    assert "UNKNOWN" in cap.out
    assert "could not be queried" in cap.out


def test_detailed_nothing_shown_and_unreachable_exits_1(monkeypatch, capsys):
    monkeypatch.setattr(wallet, "BitcoindClient", _fake_btc([]))
    monkeypatch.setattr(wallet, "CounterpartyClient", _fake_cp(
        {}, broken={"bc1pfirst", "1Aused", "bc1pempty"}))
    rc = wallet.cmd_wallet_balance(Config(), "w", detailed=True)
    cap = capsys.readouterr()
    assert rc == 1
    assert "could not be queried" in cap.out


def test_detailed_no_rescan_derives_and_omits_btc_lines(monkeypatch, capsys):
    class FakeBtc:
        def __init__(self, config):
            pass

        def wallet_call(self, name, method, params=None, timeout=-1.0):
            assert method == "listdescriptors"
            return {"descriptors": [
                {"desc": "tr([305f614d/86h/0h/0h]xpub6C.../0/*)#aaaaaaaa",
                 "internal": False}]}

        def _call(self, method, params=None):
            assert method == "deriveaddresses"
            _, (lo, hi) = params
            return [f"bc1paddr{i}" for i in range(lo, hi + 1)]

    monkeypatch.setattr(wallet, "BitcoindClient", FakeBtc)
    monkeypatch.setattr(wallet, "CounterpartyClient", _fake_cp(
        {"bc1paddr1": [{"asset": "XCP", "quantity": 150000000,
                        "asset_info": {"divisible": True}}]}))
    rc = wallet.cmd_wallet_balance(Config(), "w", no_rescan=True, detailed=True,
                                   addresses=3)
    cap = capsys.readouterr()
    assert rc == 0
    assert "needs a rescan" in cap.out
    assert "bc1paddr1" in cap.out
    assert "XCP" in cap.out and "1.50000000" in cap.out
    assert "BTC " not in cap.out.split("bc1paddr1")[1]
    assert "bc1paddr0" not in cap.out and "bc1paddr2" not in cap.out
