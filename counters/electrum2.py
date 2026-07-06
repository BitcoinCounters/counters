"""Electrum 2.x seed recovery (standard & segwit).

Electrum's post-2.0 seeds are NOT BIP39: the words carry a version number in a
hash prefix (no external checksum wordlist), and the binary seed is
`PBKDF2-HMAC-SHA512(mnemonic, "electrum"+passphrase, 2048)` rather than BIP39's
`"mnemonic"+passphrase`. Wallets then derive BIP32 keys with Electrum's own path
and script type:

  * standard (prefix 01)  -> derivation "m",    p2pkh  (legacy 1...)
  * segwit   (prefix 100) -> derivation "m/0'", p2wpkh (bc1q...)

Addresses are at `<node>/0/i` (receive) and `<node>/1/i` (change). This module
reproduces that exactly (verified against Electrum's own test vectors) so we can
import the resulting WIF keys into Bitcoin Core. 2FA seeds are not supported.

Reuses the BIP32 + address primitives in bip32.py; algorithm mirrors
electrum/mnemonic.py + electrum/keystore.py (MIT).
"""

from __future__ import annotations

import hashlib
import hmac
import unicodedata

from . import bip32

# Electrum seed-version prefixes (hex of hmac-sha512("Seed version", seed)).
_PREFIXES = {"standard": "01", "segwit": "100"}

# Per seed type: (bip32 account kind for address/descriptor, Core descriptor fn).
_TYPES = {
    "standard": ("legacy", "pkh({wif})"),    # p2pkh, 1...
    "segwit":   ("segwit", "wpkh({wif})"),   # p2wpkh, bc1q...
}


def normalize_text(text: str) -> str:
    """Electrum's seed normalisation (NFKD, lowercase, drop combining marks,
    collapse whitespace). Sufficient for the Latin seeds we handle."""
    text = unicodedata.normalize("NFKD", text)
    text = text.lower()
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(text.split())


def seed_type(phrase: str) -> str | None:
    """'standard' / 'segwit' if the phrase is a supported Electrum 2.x seed,
    else None (covers old/2fa/non-Electrum phrases)."""
    s = normalize_text(phrase).encode("utf-8")
    h = hmac.new(b"Seed version", s, hashlib.sha512).hexdigest()
    for t, prefix in _PREFIXES.items():
        if h.startswith(prefix):
            return t
    return None


def is_electrum2_phrase(phrase: str) -> bool:
    return seed_type(phrase) is not None


def _bip32_seed(phrase: str, passphrase: str = "") -> bytes:
    s = normalize_text(phrase).encode("utf-8")
    salt = b"electrum" + normalize_text(passphrase).encode("utf-8")
    return hashlib.pbkdf2_hmac("sha512", s, salt, 2048, 64)


def _wif_compressed(secret: int) -> str:
    return bip32._b58check(b"\x80" + secret.to_bytes(32, "big") + b"\x01")


def derive(phrase: str, count: int = 20, passphrase: str = "") -> tuple[str, list[dict]]:
    """Return (seed_type, keys). `keys` holds the first `count` receive + change
    addresses with their address and compressed WIF for Core import."""
    t = seed_type(phrase)
    if t not in _TYPES:
        raise ValueError("not a supported Electrum 2.x seed (standard/segwit only)")
    kind, desc_tmpl = _TYPES[t]
    root = bip32._master_from_seed(_bip32_seed(phrase, passphrase))
    node = root if t == "standard" else root.ckd_priv(0 + bip32._HARDENED)  # m or m/0'
    keys: list[dict] = []
    for for_change in (0, 1):
        for n in range(count):
            child = node.ckd_priv(for_change).ckd_priv(n)
            pub = bip32._ser_pubkey(child.secret)  # compressed
            keys.append({
                "for_change": for_change,
                "n": n,
                "address": bip32.address_from_pubkey(kind, pub),
                "wif": _wif_compressed(child.secret),
                "desc": desc_tmpl,
            })
    return t, keys
