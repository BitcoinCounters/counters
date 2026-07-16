"""Counterwallet / Freewallet / Rare Pepe Wallet key recovery.

These web wallets (Counterwallet, Freewallet, rarepepewallet.com) borrow the
Electrum-v1 1626-word list ONLY as an entropy encoding — they are NOT genuine
Electrum-v1 wallets. The 12-word phrase is `mn_decode`'d to a 16-byte seed which
then seeds a standard **BIP32** tree; addresses are the **compressed** P2PKH
keys at **m/0'/0/i** (a single chain, indexed by i).

This differs from desktop Electrum v1 (see electrum1.py), which key-stretches
the seed 100 000 rounds and uses uncompressed keys — a completely different set
of addresses. Counterparty holders overwhelmingly have the Counterwallet form,
so this is the derivation we lead with when restoring a legacy phrase.

Verified against Counterwallet's own js/external/mnemonic.js + restore flow:

    seed = Mnemonic(words).toHex()               # == mn_decode(words), 16-byte hex
    hd   = HDPrivateKey.fromSeed(seed)           # BIP32 master ("Bitcoin seed")
    key  = hd.derive("m/0'/0/" + i).privateKey   # compressed WIF

We never sign here (mirroring bip32.py / electrum1.py); we only derive
keys/addresses to import into Bitcoin Core, which holds and signs them.
"""

from __future__ import annotations

from .bip32 import _HARDENED, _b58check, _hash160, _master_from_seed, _ser_pubkey
from .electrum1 import is_electrum_v1_phrase, mn_decode


def seed_from_phrase(phrase: str) -> bytes:
    """The 16-byte BIP32 seed: the Electrum-v1 `mn_decode` of the phrase, taken
    as raw bytes (12 words -> 32 hex chars -> 16 bytes)."""
    words = [w.lower() for w in phrase.split()]
    return bytes.fromhex(mn_decode(words))


def _compressed_wif(secret: int) -> str:
    return _b58check(b"\x80" + secret.to_bytes(32, "big") + b"\x01")  # mainnet, compressed


def _p2pkh_compressed(secret: int) -> str:
    return _b58check(b"\x00" + _hash160(_ser_pubkey(secret)))  # mainnet legacy 1...


def derive(phrase: str, count: int = 20) -> list[dict]:
    """First `count` Counterwallet addresses at m/0'/0/i (compressed P2PKH `1...`),
    each with its compressed WIF private key for Bitcoin Core import."""
    if not is_electrum_v1_phrase(phrase):
        raise ValueError("not a valid Counterwallet / Electrum-v1 mnemonic")
    chain = _master_from_seed(seed_from_phrase(phrase)).ckd_priv(0 + _HARDENED).ckd_priv(0)
    keys: list[dict] = []
    for n in range(count):
        sec = chain.ckd_priv(n).secret  # m/0'/0/n
        keys.append({
            "n": n,
            "address": _p2pkh_compressed(sec),
            "wif": _compressed_wif(sec),
        })
    return keys
