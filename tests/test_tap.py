"""Offline tests for tap.py: secp256k1 tweak + bech32m addresses (what the
wallet's BIP86 derivation uses). The signing/serialization machinery was
removed in v3 — Counterparty Core signs reveals now.

Run: python tests/test_tap.py   (or via pytest)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counters import tap  # noqa: E402


def test_taproot_tweak_and_address_bip86_vector():
    # BIP86 test vector: first receive address of the standard test mnemonic.
    # internal x-only pubkey at m/86'/0'/0'/0/0:
    internal = bytes.fromhex(
        "cc8a4bc64d897bddc5fbc2f670f7a8ba0b386779106cf1223c6fc5d7cd6fc115"
    )
    parity, tweaked = tap.taproot_tweak_pubkey(internal, b"")
    assert tap.p2tr_address(tweaked) == (
        "bc1p5cyxnuxmeuwuvkwfem96lqzszd02n6xdcjrs20cac6yqjjwudpxqkedrcr"
    )
    assert parity in (0, 1)


def test_bech32_v0_and_bech32m_v1_encoding():
    # BIP173 P2WPKH example
    assert tap.encode_segwit_address(
        "bc", 0, bytes.fromhex("751e76e8199196d454941c45d1b3a323f1433bd6")
    ) == "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
    # bech32m used for v1+, bech32 for v0 (constants differ)
    v1 = tap.encode_segwit_address("bc", 1, bytes(32))
    assert v1.startswith("bc1p")


if __name__ == "__main__":
    test_taproot_tweak_and_address_bip86_vector()
    test_bech32_v0_and_bech32m_v1_encoding()
    print("ok")
