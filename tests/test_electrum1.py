"""Verify Electrum-v1 (Counterwallet) recovery against authoritative vectors.

Ground truth is Electrum's own published test seed:
  seed : powerful random nobody notice nothing important anyway look away hidden message over
  hex  : acb740e454c3134901d7c8f16497cc1c
  mpk  : e9d4b786...c442b3            (electrum tests/test_wallet_vertical.py)
  addr : 1FJEEB8ihPMbzs2SkLmr37dHyRFzakqUmo   (first receiving addr; electrum#7082)
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counters import electrum1  # noqa: E402

SEED = "powerful random nobody notice nothing important anyway look away hidden message over"
HEX_SEED = "acb740e454c3134901d7c8f16497cc1c"
MPK = ("e9d4b7866dd1e91c862aebf62a49548c7dbf7bcc6e4b7b8c9da820c7737968df"
       "9c09d5a3e271dc814a29981f81b3faaf2737b551ef5dcc6189cf0f8252c442b3")
FIRST_ADDRESS = "1FJEEB8ihPMbzs2SkLmr37dHyRFzakqUmo"


def test_wordlist_is_1626():
    assert len(electrum1._WORDS) == 1626
    assert len(set(electrum1._WORDS)) == 1626   # no dupes → index is unambiguous


def test_mn_decode_matches_electrum():
    assert electrum1.mn_decode(SEED.split()) == HEX_SEED


def test_mpk_matches_electrum_vector():
    assert electrum1.mpk_from_phrase(SEED) == MPK


def test_first_receiving_address_matches():
    mpk, keys = electrum1.derive(SEED, count=1)
    assert mpk == MPK
    first = next(k for k in keys if k["for_change"] == 0 and k["n"] == 0)
    assert first["address"] == FIRST_ADDRESS
    assert first["wif"].startswith("5")          # uncompressed mainnet WIF


def test_phrase_detection():
    assert electrum1.is_electrum_v1_phrase(SEED)
    assert not electrum1.is_electrum_v1_phrase("not real electrum words here at all ok")
    # a BIP39 phrase with words outside the 1626 list must NOT be misdetected
    assert not electrum1.is_electrum_v1_phrase(
        "zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo")


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
