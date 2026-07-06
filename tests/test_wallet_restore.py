"""Unit tests for the BIP39 restore diagnostics.

`_bip39_problem` must accept a real BIP39 phrase and, when it fails, explain
*why* — especially the common old-Counterparty (Electrum-v1) case — instead of
a bare 'checksum failed'.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mnemonic import Mnemonic  # noqa: E402

from counters.commands.wallet import _bip39_problem  # noqa: E402

MN = Mnemonic("english")


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
