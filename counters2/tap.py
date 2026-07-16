"""Taproot address primitives for the wallet's key derivation.

Self-contained (no libsecp256k1) secp256k1 point math + the BIP341 key tweak
and bech32/bech32m (BIP173/350) address encoding that bip32.py uses to derive
wallet addresses. Nothing here signs anything: since v3, Counterparty Core
composes AND signs the taproot reveal itself (see commands/inscribe.py), so
the old envelope-building/signing machinery is gone.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

# --- secp256k1 ---------------------------------------------------------------

_p = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
_n = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_Gx = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
_Gy = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8
_G = (_Gx, _Gy)

Point = tuple  # (x, y) or None for the point at infinity


def _tagged_hash(tag: str, msg: bytes) -> bytes:
    t = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(t + t + msg).digest()


def _point_add(p1, p2):
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    if p1[0] == p2[0] and (p1[1] != p2[1]):
        return None
    if p1 == p2:
        lam = (3 * p1[0] * p1[0] * pow(2 * p1[1] % _p, _p - 2, _p)) % _p
    else:
        lam = ((p2[1] - p1[1]) * pow((p2[0] - p1[0]) % _p, _p - 2, _p)) % _p
    x3 = (lam * lam - p1[0] - p2[0]) % _p
    y3 = (lam * (p1[0] - x3) - p1[1]) % _p
    return (x3, y3)


def _point_mul(p, n):
    r = None
    while n:
        if n & 1:
            r = _point_add(r, p)
        p = _point_add(p, p)
        n >>= 1
    return r


def _has_even_y(p) -> bool:
    return p[1] % 2 == 0


def _lift_x(x: int):
    if x >= _p:
        return None
    y_sq = (pow(x, 3, _p) + 7) % _p
    y = pow(y_sq, (_p + 1) // 4, _p)
    if pow(y, 2, _p) != y_sq:
        return None
    return (x, y if y % 2 == 0 else _p - y)


def _bytes32(x: int) -> bytes:
    return x.to_bytes(32, "big")


def _int(b: bytes) -> int:
    return int.from_bytes(b, "big")


# --- BIP341 taproot ----------------------------------------------------------

def taproot_tweak_pubkey(internal_xonly: bytes, merkle_root: bytes) -> tuple[int, bytes]:
    """Return (parity_bit, tweaked_xonly_pubkey)."""
    t = _int(_tagged_hash("TapTweak", internal_xonly + merkle_root))
    if t >= _n:
        raise ValueError("invalid tweak")
    P = _lift_x(_int(internal_xonly))
    Q = _point_add(P, _point_mul(_G, t))
    return (Q[1] & 1, _bytes32(Q[0]))


# --- bech32 / bech32m (BIP173/350) ------------------------------------------

_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _bech32_polymod(values):
    generator = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        b = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ v
        for i in range(5):
            chk ^= generator[i] if ((b >> i) & 1) else 0
    return chk


def _hrp_expand(hrp):
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def _convertbits(data, frombits, tobits, pad=True):
    acc = 0
    bits = 0
    ret = []
    maxv = (1 << tobits) - 1
    for value in data:
        acc = (acc << frombits) | value
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad and bits:
        ret.append((acc << (tobits - bits)) & maxv)
    return ret


def encode_segwit_address(hrp: str, witver: int, witprog: bytes) -> str:
    const = 0x2BC830A3 if witver else 1  # bech32m for v1+, bech32 for v0
    data = [witver] + _convertbits(list(witprog), 8, 5)
    values = _hrp_expand(hrp) + data
    polymod = _bech32_polymod(values + [0, 0, 0, 0, 0, 0]) ^ const
    checksum = [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(_CHARSET[d] for d in data + checksum)


def p2tr_address(output_xonly: bytes, hrp: str = "bc") -> str:
    return encode_segwit_address(hrp, 1, output_xonly)
