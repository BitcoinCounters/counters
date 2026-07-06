"""Unit tests for the BIP39 restore diagnostics.

`_bip39_problem` must accept a real BIP39 phrase and, when it fails, explain
*why* — especially the common old-Counterparty (Electrum-v1) case — instead of
a bare 'checksum failed'.
"""

from __future__ import annotations

import os
import sys

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
    # An Electrum-v1 seed (not valid BIP39) must route to the Counterwallet path
    # automatically. --dry-run derives without a node/import.
    monkeypatch.setattr(sys, "stdin", io.StringIO(ELECTRUM_SEED + "\n"))
    rc = wallet.cmd_wallet_restore(Config(), "recover", dry_run=True, addresses=1)
    out = capsys.readouterr().out
    assert rc == 0
    assert "1FJEEB8ihPMbzs2SkLmr37dHyRFzakqUmo" in out   # known first address


def test_bip39_dry_run_is_rejected(monkeypatch, capsys):
    # --dry-run only applies to Counterwallet; a BIP39 seed must not silently
    # import. It should error out before touching a node.
    monkeypatch.setattr(sys, "stdin", io.StringIO(MN.generate(strength=128) + "\n"))
    rc = wallet.cmd_wallet_restore(Config(), "w", dry_run=True)
    assert rc == 1
    assert "dry-run" in capsys.readouterr().err.lower()


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
