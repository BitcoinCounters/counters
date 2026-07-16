"""Electrum-v1 recovery for old Counterparty wallets (Counterwallet / Freewallet).

Old Counterparty wallets predate BIP39/BIP32 and use the **Electrum v1** scheme:

  * a 12-word mnemonic over a **1626-word** list encodes a 128-bit hex seed
    (`mn_decode`, see electrum1_words.txt);
  * the ASCII of that hex seed is key-stretched (100 000 rounds of SHA-256) into
    a master secret; the master *public* key (mpk) is its uncompressed point,
    64 bytes of X||Y with no prefix;
  * the key for address (for_change, n) is
    `(master_secret + H) mod order`, where `H = sha256d("n:for_change:" + mpk_bytes)`;
  * addresses are legacy, **uncompressed** P2PKH (`1...`).

This reproduces the derivation EXACTLY — verified against Electrum's own test
vector — so we can hand Bitcoin Core the resulting WIF keys and let it hold and
sign them. The algorithm and 1626-word list mirror electrum/old_mnemonic.py and
electrum/keystore.py (Old_KeyStore), MIT-licensed.

We never sign here (mirroring bip32.py); we only derive keys/addresses to import.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from ecdsa import SECP256k1

from .bip32 import _b58check, _hash160

_G = SECP256k1.generator
_ORDER = int(SECP256k1.order)
_N = 1626  # size of the Electrum-v1 word list

_WORDS = Path(__file__).with_name("electrum1_words.txt").read_text().split()
assert len(_WORDS) == _N, f"electrum1 wordlist must be {_N} words, got {len(_WORDS)}"
_INDEX = {w: i for i, w in enumerate(_WORDS)}


def _sha256(b: bytes) -> bytes:
    return hashlib.sha256(b).digest()


def _sha256d(b: bytes) -> bytes:
    return _sha256(_sha256(b))


def _to_num(b: bytes) -> int:
    return int.from_bytes(b, "big")


def is_electrum_v1_phrase(phrase: str) -> bool:
    """True if every word is in the 1626-word list and the count is a multiple
    of three (Counterwallet uses 12). Used to route a failed BIP39 restore."""
    words = phrase.split()
    return (
        len(words) >= 3
        and len(words) % 3 == 0
        and all(w.lower() in _INDEX for w in words)
    )


def mn_decode(words: list[str]) -> str:
    """Electrum-v1 mnemonic -> hex seed string (8 hex chars per 3 words)."""
    out = ""
    for i in range(len(words) // 3):
        w1 = _INDEX[words[3 * i]]
        w2 = _INDEX[words[3 * i + 1]]
        w3 = _INDEX[words[3 * i + 2]]
        x = w1 + _N * ((w2 - w1) % _N) + _N * _N * ((w3 - w2) % _N)
        out += "%08x" % x
    return out


def _stretch_key(hex_seed: str) -> int:
    """100 000 rounds of SHA-256 over the ASCII hex seed -> master secret int."""
    enc = hex_seed.encode("ascii")
    x = enc
    for _ in range(100_000):
        x = _sha256(x + enc)
    return _to_num(x)


def _mpk_bytes(master_secret: int) -> bytes:
    p = master_secret * _G
    return p.x().to_bytes(32, "big") + p.y().to_bytes(32, "big")  # 64 bytes, no prefix


def _sequence(mpk: bytes, for_change: int, n: int) -> int:
    return _to_num(_sha256d(("%d:%d:" % (n, for_change)).encode("ascii") + mpk))


def _address_secret(master_secret: int, mpk: bytes, for_change: int, n: int) -> int:
    return (master_secret + _sequence(mpk, for_change, n)) % _ORDER


def _p2pkh_address(secret: int) -> str:
    p = secret * _G
    uncompressed = b"\x04" + p.x().to_bytes(32, "big") + p.y().to_bytes(32, "big")
    return _b58check(b"\x00" + _hash160(uncompressed))  # mainnet legacy 1...


def _wif_uncompressed(secret: int) -> str:
    return _b58check(b"\x80" + secret.to_bytes(32, "big"))  # mainnet, uncompressed


def mpk_from_phrase(phrase: str) -> str:
    """Master public key hex (128 chars) for a phrase — the value Electrum
    stores and the strongest check that decode+stretch+curve are correct."""
    words = [w.lower() for w in phrase.split()]
    master = _stretch_key(mn_decode(words))
    return _mpk_bytes(master).hex()


def derive(phrase: str, count: int = 20) -> tuple[str, list[dict]]:
    """Return (mpk_hex, keys). `keys` holds the first `count` addresses of both
    the receive (for_change=0) and change (for_change=1) chains, each with its
    legacy `1...` address and uncompressed WIF private key for Core import."""
    words = [w.lower() for w in phrase.split()]
    if not is_electrum_v1_phrase(phrase):
        raise ValueError("not a valid Electrum-v1 / Counterwallet mnemonic")
    master = _stretch_key(mn_decode(words))
    mpk = _mpk_bytes(master)
    keys: list[dict] = []
    for for_change in (0, 1):
        for n in range(count):
            sec = _address_secret(master, mpk, for_change, n)
            keys.append({
                "for_change": for_change,
                "n": n,
                "address": _p2pkh_address(sec),
                "wif": _wif_uncompressed(sec),
            })
    return mpk.hex(), keys
