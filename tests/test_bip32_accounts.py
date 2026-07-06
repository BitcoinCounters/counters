"""BIP39 multi-account address derivation, checked against authoritative vectors.

Ground truth for the canonical 'abandon abandon ... about' seed (passphrase ''):
  legacy  (BIP44 m/44'/0'/0'/0/0): 1LqBGSKuX5yYUonjxT5qGfpUsXKYYWeabA  (iancoleman)
  nested  (BIP49 m/49'/0'/0'/0/0): 37VucYSaXLCAsxYyAPfbSi9eh4iEcbShgf  (iancoleman)
  segwit  (BIP84 m/84'/0'/0'/0/0): bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu (iancoleman)
  taproot (BIP86 m/86'/0'/0'/0/0): bc1p5cyxnuxmeuwuvkwfem96lqzszd02n6xdcjrs20cac6yqjjwudpxqkedrcr (BIP86 spec)
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mnemonic import Mnemonic  # noqa: E402

from counters import bip32  # noqa: E402

SEED = Mnemonic("english").to_seed(
    "abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon abandon abandon about"
)

FIRST = {
    "legacy":  "1LqBGSKuX5yYUonjxT5qGfpUsXKYYWeabA",
    "nested":  "37VucYSaXLCAsxYyAPfbSi9eh4iEcbShgf",
    "segwit":  "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu",
    "taproot": "bc1p5cyxnuxmeuwuvkwfem96lqzszd02n6xdcjrs20cac6yqjjwudpxqkedrcr",
}

# BIP86 spec also publishes the second receive + first change taproot addresses.
TAPROOT_RECV_1 = "bc1p4qhjn9zdvkux4e44uhx8tc55attvtyu358kutcqkudyccelu0was9fqzwh"
TAPROOT_CHANGE_0 = "bc1p3qkhfews2uk44qtvauqyr2ttdsw7svhkl9nkm9s9c3x4ax5h60wqwruhk7"


def test_first_address_each_account_type():
    for kind, want in FIRST.items():
        assert bip32.first_address(SEED, kind) == want, kind


def test_taproot_more_indices_match_bip86_spec():
    assert bip32.first_address(SEED, "taproot", change=0, index=1) == TAPROOT_RECV_1
    assert bip32.first_address(SEED, "taproot", change=1, index=0) == TAPROOT_CHANGE_0


def test_account_descriptors_shape():
    for kind in bip32.ACCOUNT_TYPES:
        recv, change = bip32.account_descriptors(SEED, kind)
        assert "/0/*" in recv and "/1/*" in change
        assert recv.startswith({"legacy": "pkh(", "nested": "sh(wpkh(",
                                 "segwit": "wpkh(", "taproot": "tr("}[kind])


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
