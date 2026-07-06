"""Electrum 2.x (standard/segwit) recovery vs Electrum's own test vectors.

From electrum tests/test_wallet_vertical.py (passphrase ''):
  standard 'cycle rocket west magnet parrot shuffle foot correct salt library feed song'
           recv[0]=1NNkttn1YvVGdqBW4PR6zvc3Zx3H5owKRf  change[0]=1KSezYMhAJMWqFbVFB2JshYg69UpmEXR4D
  segwit   'bitter grass shiver impose acquire brush forget axis eager alone wine silver'
           recv[0]=bc1q3g5tmkmlvxryhh843v4dz026avatc0zzr6h3af  change[0]=bc1qdy94n2q5qcp0kg7v9yzwe6wvfkhnvyzje7nx2p
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counters import electrum2  # noqa: E402

STANDARD = "cycle rocket west magnet parrot shuffle foot correct salt library feed song"
SEGWIT = "bitter grass shiver impose acquire brush forget axis eager alone wine silver"


def _addrs(phrase):
    _t, keys = electrum2.derive(phrase, count=1)
    recv = next(k for k in keys if k["for_change"] == 0)["address"]
    chng = next(k for k in keys if k["for_change"] == 1)["address"]
    return recv, chng


def test_seed_type_detection():
    assert electrum2.seed_type(STANDARD) == "standard"
    assert electrum2.seed_type(SEGWIT) == "segwit"
    # a BIP39-style phrase is not an Electrum 2.x seed
    assert electrum2.seed_type("abandon abandon abandon") is None


def test_standard_addresses_match_electrum():
    recv, chng = _addrs(STANDARD)
    assert recv == "1NNkttn1YvVGdqBW4PR6zvc3Zx3H5owKRf"
    assert chng == "1KSezYMhAJMWqFbVFB2JshYg69UpmEXR4D"


def test_segwit_addresses_match_electrum():
    recv, chng = _addrs(SEGWIT)
    assert recv == "bc1q3g5tmkmlvxryhh843v4dz026avatc0zzr6h3af"
    assert chng == "bc1qdy94n2q5qcp0kg7v9yzwe6wvfkhnvyzje7nx2p"


def test_descriptor_kind_per_type():
    _t, keys = electrum2.derive(STANDARD, count=1)
    assert keys[0]["desc"] == "pkh({wif})"
    _t, keys = electrum2.derive(SEGWIT, count=1)
    assert keys[0]["desc"] == "wpkh({wif})"


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
