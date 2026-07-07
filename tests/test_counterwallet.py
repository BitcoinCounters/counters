"""Counterwallet / Freewallet / Rare Pepe derivation.

These wallets use the Electrum-v1 wordlist ONLY for entropy, then derive keys
via BIP32 at m/0'/0/i with COMPRESSED 1... P2PKH addresses — verified against
Counterwallet's own mnemonic.js and the community `bip32utils` recovery helper
(the reference `openWallet`: unhexlify(toHex(mn_decode)) -> HMAC-SHA512("Bitcoin
seed") -> m/0' -> m/0'/0 -> m/0'/0/i).

The vectors below are locked so a future refactor can't silently change the
derivation (which would send users to empty addresses).
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counters import counterwallet  # noqa: E402

# Sample 12-word Counterwallet phrase from the official Counterparty forum
# recovery FAQ (all words are in the 1626-word Electrum-v1 list).
PHRASE = "god stock reply doctor pity ink glare air sport someone matter reach"
EXPECTED = [
    "1Khan4b4A3TzAjy8kQNGpRG9ASFnSnFP8P",  # m/0'/0/0
    "1Jixo5H8EG4P1kaiXdb68c4JCYZLFb9i6Q",  # m/0'/0/1
    "19oa4iBPgxuHNnMcVKSUfqURA3btVmCmnU",  # m/0'/0/2
]


def test_addresses_match_reference_vector():
    keys = counterwallet.derive(PHRASE, count=3)
    assert [k["address"] for k in keys] == EXPECTED
    assert [k["n"] for k in keys] == [0, 1, 2]


def test_addresses_are_legacy_p2pkh():
    for k in counterwallet.derive(PHRASE, count=5):
        assert k["address"].startswith("1")


def test_wifs_are_compressed():
    # Compressed mainnet WIFs start with K or L (uncompressed start with 5).
    for k in counterwallet.derive(PHRASE, count=5):
        assert k["wif"][0] in ("K", "L")


def test_seed_is_16_bytes():
    assert len(counterwallet.seed_from_phrase(PHRASE)) == 16


def test_deterministic():
    assert counterwallet.derive(PHRASE, 3) == counterwallet.derive(PHRASE, 3)


def test_rejects_non_electrum_v1_phrase():
    try:
        counterwallet.derive("clearly not a valid counterwallet phrase at all", 1)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for a non-Electrum-v1 phrase")


if __name__ == "__main__":
    import inspect
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
