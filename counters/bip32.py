"""Self-contained BIP32 / BIP86 (taproot) key derivation.

This exists only to turn a BIP39 mnemonic into the descriptors we import into
Bitcoin Core, which then HOLDS the keys and does all signing. We never sign
here. Pure-Python because the host's OpenSSL 3 has RIPEMD160 disabled and the
installed bitcoinlib/pycryptodome fallbacks are broken; we only depend on the
`ecdsa` package for secp256k1 point math.

The 24-word mnemonic is the real backup: any BIP86-compliant wallet
regenerates the same taproot addresses from it.
"""

from __future__ import annotations

import hashlib
import hmac
import struct

from ecdsa import SECP256k1

from . import tap  # bech32/bech32m + BIP341 taproot tweak for address encoding

_CURVE_ORDER = SECP256k1.order
_G = SECP256k1.generator
_HARDENED = 0x80000000
_XPRV_VERSION = bytes.fromhex("0488ADE4")  # mainnet xprv
_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


# --- RIPEMD160 (pure Python, used only for hash160 fingerprints) -----------

def _ripemd160(msg: bytes) -> bytes:
    # Reference implementation of RIPEMD-160 (ISO/IEC 10118-3).
    def rol(x, n):
        return ((x << n) | (x >> (32 - n))) & 0xFFFFFFFF

    _r1 = [
        0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15,
        7, 4, 13, 1, 10, 6, 15, 3, 12, 0, 9, 5, 2, 14, 11, 8,
        3, 10, 14, 4, 9, 15, 8, 1, 2, 7, 0, 6, 13, 11, 5, 12,
        1, 9, 11, 10, 0, 8, 12, 4, 13, 3, 7, 15, 14, 5, 6, 2,
        4, 0, 5, 9, 7, 12, 2, 10, 14, 1, 3, 8, 11, 6, 15, 13,
    ]
    _r2 = [
        5, 14, 7, 0, 9, 2, 11, 4, 13, 6, 15, 8, 1, 10, 3, 12,
        6, 11, 3, 7, 0, 13, 5, 10, 14, 15, 8, 12, 4, 9, 1, 2,
        15, 5, 1, 3, 7, 14, 6, 9, 11, 8, 12, 2, 10, 0, 4, 13,
        8, 6, 4, 1, 3, 11, 15, 0, 5, 12, 2, 13, 9, 7, 10, 14,
        12, 15, 10, 4, 1, 5, 8, 7, 6, 2, 13, 14, 0, 3, 9, 11,
    ]
    _s1 = [
        11, 14, 15, 12, 5, 8, 7, 9, 11, 13, 14, 15, 6, 7, 9, 8,
        7, 6, 8, 13, 11, 9, 7, 15, 7, 12, 15, 9, 11, 7, 13, 12,
        11, 13, 6, 7, 14, 9, 13, 15, 14, 8, 13, 6, 5, 12, 7, 5,
        11, 12, 14, 15, 14, 15, 9, 8, 9, 14, 5, 6, 8, 6, 5, 12,
        9, 15, 5, 11, 6, 8, 13, 12, 5, 12, 13, 14, 11, 8, 5, 6,
    ]
    _s2 = [
        8, 9, 9, 11, 13, 15, 15, 5, 7, 7, 8, 11, 14, 14, 12, 6,
        9, 13, 15, 7, 12, 8, 9, 11, 7, 7, 12, 7, 6, 15, 13, 11,
        9, 7, 15, 11, 8, 6, 6, 14, 12, 13, 5, 14, 13, 13, 7, 5,
        15, 5, 8, 11, 14, 14, 6, 14, 6, 9, 12, 9, 12, 5, 15, 8,
        8, 5, 12, 9, 12, 5, 14, 6, 8, 13, 6, 5, 15, 13, 11, 11,
    ]
    _K1 = [0x00000000, 0x5A827999, 0x6ED9EBA1, 0x8F1BBCDC, 0xA953FD4E]
    _K2 = [0x50A28BE6, 0x5C4DD124, 0x6D703EF3, 0x7A6D76E9, 0x00000000]

    def f(j, x, y, z):
        if j < 16:
            return x ^ y ^ z
        if j < 32:
            return (x & y) | (~x & z)
        if j < 48:
            return (x | ~y) ^ z
        if j < 64:
            return (x & z) | (y & ~z)
        return x ^ (y | ~z)

    h0, h1, h2, h3, h4 = (
        0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0,
    )
    padded = msg + b"\x80"
    padded += b"\x00" * ((56 - len(padded) % 64) % 64)
    padded += struct.pack("<Q", (len(msg) * 8) & 0xFFFFFFFFFFFFFFFF)

    for off in range(0, len(padded), 64):
        block = padded[off:off + 64]
        x = list(struct.unpack("<16L", block))
        a1, b1, c1, d1, e1 = h0, h1, h2, h3, h4
        a2, b2, c2, d2, e2 = h0, h1, h2, h3, h4
        for j in range(80):
            t = (a1 + f(j, b1, c1, d1) + x[_r1[j]] + _K1[j // 16]) & 0xFFFFFFFF
            t = (rol(t, _s1[j]) + e1) & 0xFFFFFFFF
            a1, e1, d1, c1, b1 = e1, d1, rol(c1, 10), b1, t
            t = (a2 + f(79 - j, b2, c2, d2) + x[_r2[j]] + _K2[j // 16]) & 0xFFFFFFFF
            t = (rol(t, _s2[j]) + e2) & 0xFFFFFFFF
            a2, e2, d2, c2, b2 = e2, d2, rol(c2, 10), b2, t
        t = (h1 + c1 + d2) & 0xFFFFFFFF
        h1 = (h2 + d1 + e2) & 0xFFFFFFFF
        h2 = (h3 + e1 + a2) & 0xFFFFFFFF
        h3 = (h4 + a1 + b2) & 0xFFFFFFFF
        h4 = (h0 + b1 + c2) & 0xFFFFFFFF
        h0 = t
    return struct.pack("<5L", h0, h1, h2, h3, h4)


def _hash160(data: bytes) -> bytes:
    return _ripemd160(hashlib.sha256(data).digest())


def _b58check(payload: bytes) -> str:
    chk = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    num = int.from_bytes(payload + chk, "big")
    out = ""
    while num > 0:
        num, rem = divmod(num, 58)
        out = _B58_ALPHABET[rem] + out
    pad = len(payload + chk) - len((payload + chk).lstrip(b"\x00"))
    return _B58_ALPHABET[0] * pad + out


def _ser_pubkey(secret: int) -> bytes:
    point = secret * _G
    x = point.x().to_bytes(32, "big")
    prefix = b"\x02" if point.y() % 2 == 0 else b"\x03"
    return prefix + x


def _fingerprint(secret: int) -> bytes:
    return _hash160(_ser_pubkey(secret))[:4]


class _Node:
    __slots__ = ("secret", "chain", "depth", "parent_fp", "child_number")

    def __init__(self, secret: int, chain: bytes, depth: int, parent_fp: bytes, child_number: int):
        self.secret = secret
        self.chain = chain
        self.depth = depth
        self.parent_fp = parent_fp
        self.child_number = child_number

    def ckd_priv(self, index: int) -> "_Node":
        if index >= _HARDENED:
            data = b"\x00" + self.secret.to_bytes(32, "big") + struct.pack(">L", index)
        else:
            data = _ser_pubkey(self.secret) + struct.pack(">L", index)
        i = hmac.new(self.chain, data, hashlib.sha512).digest()
        il = int.from_bytes(i[:32], "big")
        child_secret = (il + self.secret) % int(_CURVE_ORDER)
        if il >= int(_CURVE_ORDER) or child_secret == 0:
            raise ValueError("invalid BIP32 child key; pick a new mnemonic")
        return _Node(child_secret, i[32:], self.depth + 1, _fingerprint(self.secret), index)

    def xprv(self) -> str:
        payload = (
            _XPRV_VERSION
            + bytes([self.depth])
            + self.parent_fp
            + struct.pack(">L", self.child_number)
            + self.chain
            + b"\x00"
            + self.secret.to_bytes(32, "big")
        )
        return _b58check(payload)


def _master_from_seed(seed: bytes) -> _Node:
    i = hmac.new(b"Bitcoin seed", seed, hashlib.sha512).digest()
    return _Node(int.from_bytes(i[:32], "big"), i[32:], 0, b"\x00\x00\x00\x00", 0)


def bip86_account(seed: bytes) -> tuple[str, str]:
    """Derive the BIP86 account at m/86'/0'/0'.

    Returns (account_xprv, master_fingerprint_hex). The account xprv is what we
    embed in the tr() descriptor; Core derives the /0/* and /1/* chains from it.
    """
    master = _master_from_seed(seed)
    master_fp = _fingerprint(master.secret).hex()
    account = master.ckd_priv(86 + _HARDENED).ckd_priv(0 + _HARDENED).ckd_priv(0 + _HARDENED)
    return account.xprv(), master_fp


def bip86_descriptors(account_xprv: str, master_fp: str) -> tuple[str, str]:
    """Build (receive, change) tr() descriptors WITHOUT checksums.

    Core's getdescriptorinfo appends the required '#checksum'.
    """
    origin = f"[{master_fp}/86h/0h/0h]"
    receive = f"tr({origin}{account_xprv}/0/*)"
    change = f"tr({origin}{account_xprv}/1/*)"
    return receive, change


# --- generic BIP39 accounts (legacy/nested/segwit/taproot) ------------------
#
# A BIP39 seed can hold coins under several standard accounts. We import all of
# them so a rescan finds funds wherever they are, without the user knowing which
# derivation their old wallet used. Each entry maps a friendly name to the BIP43
# purpose and the Core descriptor template (Core appends the checksum).
_ACCOUNTS = {
    "legacy":  (44, "pkh({inner})"),        # 1...   P2PKH
    "nested":  (49, "sh(wpkh({inner}))"),   # 3...   P2SH-P2WPKH
    "segwit":  (84, "wpkh({inner})"),       # bc1q.. P2WPKH
    "taproot": (86, "tr({inner})"),         # bc1p.. P2TR (BIP86)
}
ACCOUNT_TYPES = ("legacy", "nested", "segwit", "taproot")


def _account_node(seed: bytes, purpose: int) -> tuple["_Node", str]:
    master = _master_from_seed(seed)
    fp = _fingerprint(master.secret).hex()
    node = (master.ckd_priv(purpose + _HARDENED)
                  .ckd_priv(0 + _HARDENED)
                  .ckd_priv(0 + _HARDENED))
    return node, fp


def account_descriptors(seed: bytes, kind: str) -> tuple[str, str]:
    """(receive, change) descriptors (no checksum) for a BIP39 account type:
    'legacy'(44) / 'nested'(49) / 'segwit'(84) / 'taproot'(86)."""
    purpose, template = _ACCOUNTS[kind]
    node, fp = _account_node(seed, purpose)
    xprv = node.xprv()
    origin = f"[{fp}/{purpose}h/0h/0h]"
    return (template.format(inner=f"{origin}{xprv}/0/*"),
            template.format(inner=f"{origin}{xprv}/1/*"))


def address_from_pubkey(kind: str, pub_compressed: bytes) -> str:
    """Encode a compressed pubkey as the address for the given account type."""
    if kind == "legacy":
        return _b58check(b"\x00" + _hash160(pub_compressed))
    if kind == "nested":
        redeem = b"\x00\x14" + _hash160(pub_compressed)  # 0 <20-byte-keyhash>
        return _b58check(b"\x05" + _hash160(redeem))
    if kind == "segwit":
        return tap.encode_segwit_address("bc", 0, _hash160(pub_compressed))
    if kind == "taproot":
        _, tweaked = tap.taproot_tweak_pubkey(pub_compressed[1:], b"")  # BIP86: no script
        return tap.p2tr_address(tweaked)
    raise ValueError(f"unknown account type {kind!r}")


def first_address(seed: bytes, kind: str, *, change: int = 0, index: int = 0) -> str:
    """Offline: the address at m/purpose'/0'/0'/change/index for the account type
    (used to preview a restore without touching Bitcoin Core)."""
    purpose, _ = _ACCOUNTS[kind]
    node, _ = _account_node(seed, purpose)
    child = node.ckd_priv(change).ckd_priv(index)
    return address_from_pubkey(kind, _ser_pubkey(child.secret))
