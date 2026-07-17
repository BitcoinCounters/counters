"""Unit tests for the BIP39 restore diagnostics.

`_bip39_problem` must accept a real BIP39 phrase and, when it fails, explain
*why* — especially the common old-Counterparty (Electrum-v1) case — instead of
a bare 'checksum failed'.
"""

from __future__ import annotations

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import io  # noqa: E402

from mnemonic import Mnemonic  # noqa: E402

from counters.commands import wallet  # noqa: E402
from counters.commands.wallet import _bip39_problem  # noqa: E402
from counters.config import Config  # noqa: E402

MN = Mnemonic("english")

ELECTRUM_SEED = ("powerful random nobody notice nothing important anyway look away "
                 "hidden message over")


def test_valid_bip39_passes():
    phrase = MN.generate(strength=128)   # a real 12-word BIP39 seed
    assert _bip39_problem(MN, phrase) is None


def test_counterwallet_style_phrase_flagged():
    # Electrum-v1 / Counterwallet phrases contain words outside the BIP39 list.
    phrase = "blabber wobble spatula know never want time out there make look eye"
    msg = _bip39_problem(MN, phrase)
    assert msg is not None
    assert "Counterwallet" in msg and "BIP39 word list" in msg


def test_wrong_word_count():
    msg = _bip39_problem(MN, "abandon abandon abandon")   # 3 valid words
    assert msg is not None and "12, 15, 18, 21, or 24 words" in msg


def test_all_valid_words_but_bad_checksum():
    # every word is a valid BIP39 word, but this ordering fails the checksum
    phrase = ("abandon abandon abandon abandon abandon abandon "
              "abandon abandon abandon abandon abandon zoo")
    msg = _bip39_problem(MN, phrase)
    assert msg is not None and "checksum failed" in msg and "out of order" in msg


def test_autodetects_counterwallet_without_flag(monkeypatch, capsys):
    # A Counterwallet-wordlist seed (not valid BIP39) must route to the
    # Counterwallet path automatically. --dry-run derives without a node/import.
    # The primary derivation is BIP32 m/0'/0/i (compressed 1... P2PKH).
    monkeypatch.setattr(sys, "stdin", io.StringIO(ELECTRUM_SEED + "\n"))
    rc = wallet.cmd_wallet_restore(Config(), "recover", dry_run=True, addresses=1)
    out = capsys.readouterr().out
    assert rc == 0
    assert "m/0'/0/" in out                              # BIP32 Counterwallet path
    assert "168MK3wF9dKN988povk5NFSX2SmqyTTii6" in out   # known first address


def test_counterwallet_restore_no_rescan_imports_with_timestamp_now(monkeypatch, capsys):
    # --no-rescan must import the legacy keys with timestamp="now" (no chain
    # scan) and never trigger the rescan progress runner.
    monkeypatch.setattr(sys, "stdin", io.StringIO(ELECTRUM_SEED + "\n"))
    seen = {}

    class FakeBtc:
        def __init__(self, config):
            pass

        def _call(self, method, params=None):
            return {"checksum": "abcd1234"} if method == "getdescriptorinfo" else None

        def wallet_call(self, name, method, params=None, timeout=-1.0):
            if method == "importdescriptors":
                seen["timestamps"] = [r["timestamp"] for r in params[0]]
                seen["timeout"] = timeout
                return [{"success": True} for _ in params[0]]
            raise AssertionError(f"unexpected RPC {method}")

    monkeypatch.setattr(wallet, "BitcoindClient", FakeBtc)
    # If it tried to rescan, this would be invoked; fail loudly if so.
    monkeypatch.setattr(wallet, "_run_rescan_with_progress",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("rescanned")))
    rc = wallet.cmd_wallet_restore(Config(), "rp", counterwallet=True, no_rescan=True,
                                   addresses=2)
    out = capsys.readouterr().out
    assert rc == 0
    assert set(seen["timestamps"]) == {"now"}   # no timestamp=0 (no scan)
    assert seen["timeout"] == -1.0              # normal timeout, not None
    assert "no rescan" in out


def test_bip39_dry_run_previews_all_accounts(monkeypatch, capsys):
    # --dry-run on a BIP39 seed previews one address per account type, offline,
    # and imports nothing. Uses the canonical 'abandon...about' vector.
    seed = ("abandon abandon abandon abandon abandon abandon "
            "abandon abandon abandon abandon abandon about")
    monkeypatch.setattr(sys, "stdin", io.StringIO(seed + "\n"))
    rc = wallet.cmd_wallet_restore(Config(), "w", dry_run=True)
    out = capsys.readouterr().out
    assert rc == 0
    # authoritative first addresses (iancoleman / BIP86 spec)
    assert "1LqBGSKuX5yYUonjxT5qGfpUsXKYYWeabA" in out          # legacy  (BIP44)
    assert "37VucYSaXLCAsxYyAPfbSi9eh4iEcbShgf" in out          # nested  (BIP49)
    assert "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu" in out  # segwit  (BIP84)
    assert ("bc1p5cyxnuxmeuwuvkwfem96lqzszd02n6xdcjrs20cac6yqjjwudpxqkedrcr"
            in out)                                             # taproot (BIP86)


def test_autodetects_electrum2_segwit(monkeypatch, capsys):
    # An Electrum 2.x segwit seed (not BIP39, not Electrum v1) routes to the
    # Electrum path automatically; --dry-run derives offline.
    seed = "bitter grass shiver impose acquire brush forget axis eager alone wine silver"
    monkeypatch.setattr(sys, "stdin", io.StringIO(seed + "\n"))
    rc = wallet.cmd_wallet_restore(Config(), "e2", dry_run=True, addresses=1)
    out = capsys.readouterr().out
    assert rc == 0
    assert "segwit" in out
    assert "bc1q3g5tmkmlvxryhh843v4dz026avatc0zzr6h3af" in out


def test_balance_no_rescan_derives_and_queries_counterparty(monkeypatch, capsys):
    # --no-rescan must derive addresses from the wallet descriptors (pure key
    # math, no chain) and query Counterparty directly — not read Core's scanned
    # UTXO/receive list.
    class FakeBtc:
        def __init__(self, config):
            pass

        def wallet_call(self, name, method, params=None, timeout=-1.0):
            assert method == "listdescriptors"  # never listunspent/listreceived
            return {"descriptors": [
                {"desc": "tr(xpub.../0/*)#aaa"},   # ranged -> expanded
                {"desc": "pkh(cWIF...)#bbb"},        # flat WIF -> single addr
            ]}

        def _call(self, method, params=None, wallet=None, timeout=-1.0):
            assert method == "deriveaddresses"
            desc = params[0]
            if "*" in desc:
                lo, hi = params[1]
                assert [lo, hi] == [0, 2]  # addresses=3 -> range [0, 2]
                return [f"bc1p_recv_{i}" for i in range(lo, hi + 1)]
            return ["1FlatKeyAddr"]

    class FakeCp:
        def __init__(self, config):
            pass

        def get_address_balances(self, addr):
            if addr == "1FlatKeyAddr":
                return [{"asset": "XCP", "quantity": 500000000,
                         "asset_longname": None}]
            return []

        def get_address_owned_assets(self, addr):
            return []

    monkeypatch.setattr(wallet, "BitcoindClient", FakeBtc)
    monkeypatch.setattr(wallet, "CounterpartyClient", FakeCp)
    rc = wallet.cmd_wallet_balance(Config(), "w", no_rescan=True, addresses=3)
    out = capsys.readouterr().out
    assert rc == 0
    assert "skipped" in out                 # BTC balance unavailable
    assert "4 derived addresses" in out      # 3 ranged + 1 flat
    assert "XCP" in out and "500000000" in out


def test_balance_reports_ownership_rights_assets(monkeypatch, capsys):
    # Assets the wallet owns (ownership rights) must appear in their own
    # section, whether or not any supply is held, and without a held-flag.
    from counters.commands.wallet import _report_cp_balances

    class FakeCp:
        def get_address_balances(self, addr):
            return [{"asset": "HELDPEPE", "quantity": 100, "asset_longname": None}]

        def get_address_owned_assets(self, addr):
            # One asset also held (HELDPEPE), one owned-but-not-held (GHOSTPEPE).
            return [
                {"asset": "HELDPEPE", "asset_longname": None},
                {"asset": "GHOSTPEPE", "asset_longname": None},
            ]

    rc = _report_cp_balances(FakeCp(), ["1SomeAddr"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Ownership rights assets" in out
    # Both owned assets listed; no held/quantity flags in the ownership section.
    assert "GHOSTPEPE" in out and "HELDPEPE" in out
    assert "(0 held)" not in out


def test_rescan_calls_rescanblockchain_with_range(monkeypatch, capsys):
    # `wallet rescan` must invoke rescanblockchain scoped to the one wallet,
    # forwarding the height range, and report the scanned span.
    monkeypatch.setattr(wallet.time, "sleep", lambda *_: None)
    seen = {}

    class FakeBtc:
        def __init__(self, config):
            pass

        def get_block_count(self):
            return 850000

        def wallet_call(self, name, method, params=None, timeout=-1.0):
            if method == "rescanblockchain":
                seen["name"] = name
                seen["params"] = params
                return {"start_height": params[0], "stop_height": params[1]}
            if method == "getwalletinfo":
                return {"scanning": False}
            raise AssertionError(f"unexpected RPC {method}")

    monkeypatch.setattr(wallet, "BitcoindClient", FakeBtc)
    rc = wallet.cmd_wallet_rescan(Config(), "w", start_height=800000, stop_height=800100)
    out = capsys.readouterr().out
    assert rc == 0
    assert seen["name"] == "w"
    assert seen["params"] == [800000, 800100]
    assert "800000" in out and "800100" in out


def test_rescan_monitors_existing_scan_instead_of_failing(monkeypatch, capsys):
    # If a rescan is already in flight, `wallet rescan` must attach and monitor
    # it to completion rather than calling rescanblockchain (which would error
    # 'Wallet is currently rescanning').
    monkeypatch.setattr(wallet.time, "sleep", lambda *_: None)
    state = {"polls": 0}

    class FakeBtc:
        def __init__(self, config):
            pass

        def get_block_count(self):
            return 850000

        def wallet_call(self, name, method, params=None, timeout=-1.0):
            if method == "getwalletinfo":
                state["polls"] += 1
                # scanning for the first two polls, then done
                if state["polls"] <= 2:
                    return {"scanning": {"progress": 0.7, "duration": 30}}
                return {"scanning": False}
            raise AssertionError(f"must not call {method} while a scan is running")

    monkeypatch.setattr(wallet, "BitcoindClient", FakeBtc)
    rc = wallet.cmd_wallet_rescan(Config(), "w")
    cap = capsys.readouterr()
    assert rc == 0
    assert "already in progress" in cap.err
    assert "rescan complete" in cap.out
    # progress 0.7 of tip 850000 -> estimated block ~595,000/850,000
    assert "block ~595,000/850,000" in cap.err


def test_rescan_whole_chain_uses_no_params(monkeypatch, capsys):
    monkeypatch.setattr(wallet.time, "sleep", lambda *_: None)
    seen = {}

    class FakeBtc:
        def __init__(self, config):
            pass

        def get_block_count(self):
            return 850000

        def wallet_call(self, name, method, params=None, timeout=-1.0):
            if method == "rescanblockchain":
                seen["params"] = params
                return {"start_height": 0, "stop_height": 850000}
            return {"scanning": False}

    monkeypatch.setattr(wallet, "BitcoindClient", FakeBtc)
    rc = wallet.cmd_wallet_rescan(Config(), "w")
    assert rc == 0
    assert seen["params"] == []


def test_fmt_eta():
    assert wallet._fmt_eta(45) == "45s"
    assert wallet._fmt_eta(372) == "6m 12s"
    assert wallet._fmt_eta(3780) == "1h 3m"
    assert wallet._fmt_eta(-5) == "0s"


def test_run_rescan_with_progress_runs_import_and_polls(monkeypatch):
    # The runner must execute the import on a worker thread and poll
    # getwalletinfo.scanning until the worker finishes. We release the worker
    # after a few polls and assert it completed and polling occurred.
    monkeypatch.setattr(wallet.time, "sleep", lambda *_: None)
    release = threading.Event()
    calls = {"poll": 0, "import": False}

    class FakePoll:
        def __init__(self, config):
            pass

        def get_block_count(self):
            return 850000

        def wallet_call(self, name, method, params=None, timeout=-1.0):
            assert method == "getwalletinfo"
            calls["poll"] += 1
            if calls["poll"] >= 3:
                release.set()  # let the worker finish
            return {"scanning": {"progress": 0.5, "duration": 10}}

    monkeypatch.setattr(wallet, "BitcoindClient", FakePoll)

    def do_import():
        release.wait(timeout=5)
        calls["import"] = True

    wallet._run_rescan_with_progress(Config(), "w", do_import)
    assert calls["import"] is True
    assert calls["poll"] >= 3


def test_run_rescan_with_progress_surfaces_errors(monkeypatch):
    monkeypatch.setattr(wallet.time, "sleep", lambda *_: None)

    class FakePoll:
        def __init__(self, config):
            pass

        def get_block_count(self):
            return 850000

        def wallet_call(self, name, method, params=None, timeout=-1.0):
            return {"scanning": False}

    monkeypatch.setattr(wallet, "BitcoindClient", FakePoll)

    def do_import():
        raise wallet.BitcoindError("boom")

    try:
        wallet._run_rescan_with_progress(Config(), "w", do_import)
    except wallet.BitcoindError as e:
        assert "boom" in str(e)
    else:
        raise AssertionError("expected BitcoindError to propagate")


if __name__ == "__main__":
    import inspect
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            if inspect.signature(fn).parameters:
                print(f"SKIP {name} (needs pytest fixtures)")
                continue
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    print(f"\n{'OK' if failures == 0 else f'{failures} FAILED'}")
    raise SystemExit(1 if failures else 0)
