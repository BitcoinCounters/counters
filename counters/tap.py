"""Taproot primitives: secp256k1 point math, the BIP341 key tweak, BIP340
Schnorr signatures, tapleaf hashing and bech32/bech32m (BIP173/350) addresses.

Self-contained (no libsecp256k1). bip32.py uses the address side to derive
wallet addresses; revealsign.py uses the signing side to close an inscription
reveal with the source's own key. Since Counterparty Core v11.5.0
(`require_reveal_source_signature`, mainnet block 969,320) a reveal only counts
when its envelope leaf is signed by a key of the source address, and Bitcoin
Core's wallet cannot sign that leaf (its tapscript signer only understands
miniscript shapes), so the signature is produced here, over a sighash computed
here, with a key exported from Bitcoin Core for exactly that one signature.
"""

from __future__ import annotations

import hashlib
import os
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


def xonly_from_secret(secret: int) -> bytes:
    """The x-only public key of a secret scalar."""
    if not 1 <= secret < _n:
        raise ValueError("secret out of range")
    return _bytes32(_point_mul(_G, secret)[0])


def compact_size(n: int) -> bytes:
    """Bitcoin's CompactSize encoding."""
    if n < 0xFD:
        return bytes([n])
    if n <= 0xFFFF:
        return b"\xfd" + n.to_bytes(2, "little")
    if n <= 0xFFFFFFFF:
        return b"\xfe" + n.to_bytes(4, "little")
    return b"\xff" + n.to_bytes(8, "little")


TAPSCRIPT_LEAF_VERSION = 0xC0


def tapleaf_hash(script: bytes, leaf_version: int = TAPSCRIPT_LEAF_VERSION) -> bytes:
    """BIP341 leaf hash: tagged_hash("TapLeaf", version || compact_size(len) || script)."""
    return _tagged_hash("TapLeaf", bytes([leaf_version]) + compact_size(len(script)) + script)


# --- BIP340 Schnorr ----------------------------------------------------------

def schnorr_verify(pubkey_xonly: bytes, msg: bytes, sig: bytes) -> bool:
    """BIP340 verification. False (never an exception) for anything malformed."""
    if len(pubkey_xonly) != 32 or len(sig) != 64:
        return False
    P = _lift_x(_int(pubkey_xonly))
    if P is None:
        return False
    r, s = _int(sig[:32]), _int(sig[32:])
    if r >= _p or s >= _n:
        return False
    e = _int(_tagged_hash("BIP0340/challenge", sig[:32] + pubkey_xonly + msg)) % _n
    R = _point_add(_point_mul(_G, s), _point_mul(P, _n - e))
    return R is not None and _has_even_y(R) and R[0] == r


def schnorr_sign(secret: int, msg: bytes, aux_rand: bytes | None = None) -> bytes:
    """BIP340 signature of `msg` by `secret`, 64 bytes.

    `aux_rand` is the 32 bytes of auxiliary randomness; fresh `os.urandom(32)`
    when None, fixed only by tests replaying the BIP340 vectors. The result is
    verified before it is returned, so a wrong signature cannot leave here.
    """
    if not 1 <= secret < _n:
        raise ValueError("secret out of range")
    if aux_rand is None:
        aux_rand = os.urandom(32)
    if len(aux_rand) != 32:
        raise ValueError("aux_rand must be 32 bytes")
    P = _point_mul(_G, secret)
    d = secret if _has_even_y(P) else _n - secret
    px = _bytes32(P[0])
    t = _bytes32(d ^ _int(_tagged_hash("BIP0340/aux", aux_rand)))
    k0 = _int(_tagged_hash("BIP0340/nonce", t + px + msg)) % _n
    if k0 == 0:
        raise ValueError("nonce derivation failed")
    R = _point_mul(_G, k0)
    k = k0 if _has_even_y(R) else _n - k0
    rx = _bytes32(R[0])
    e = _int(_tagged_hash("BIP0340/challenge", rx + px + msg)) % _n
    sig = rx + _bytes32((k + e * d) % _n)
    if not schnorr_verify(px, msg, sig):
        raise ValueError("produced an invalid signature")
    return sig


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
