"""Signing an inscription reveal with the source's own key.

Since Counterparty Core v11.5.0 (`require_reveal_source_signature`, mainnet
block 969,320) a taproot-envelope reveal is attributed to the address that
funded its commit ONLY when the envelope leaf is closed by a key of that
address and the reveal is a tapscript spend proving a signature by that key.
A reveal closed by any other key is not invalid, it is invisible: the node
writes no ledger rows and only logs "ignored: not signed by its source". Ten
DEGENT subassets and five other assets were lost that way in the first days.

Core's `compose` therefore no longer signs the reveal. It returns the unsigned
`reveal_rawtransaction` together with `envelope_script`, `reveal_control_block`
and `reveal_pubkey` (the x-only key it closed the envelope with, which must be
the source's) and the wallet signs the leaf. Bitcoin Core cannot: its tapscript
signer only satisfies miniscript shapes, and `OP_FALSE OP_IF ... OP_ENDIF <key>
OP_CHECKSIG` is not one. So this module

  1. finds which key of the source address Core expects (`source_pubkey`),
  2. exports that one child key from Bitcoin Core's descriptors
     (`export_secret`, via `listdescriptors true` + BIP32 derivation),
  3. computes the BIP341 script-path sighash and signs it (BIP340),
  4. assembles the witness `<signature> <envelope_script> <control_block>`,
  5. and re-checks the finished reveal against a mirror of Core's rule
     (`reveal_source_error`), so a reveal the network would ignore is never
     broadcast.

Everything here is pure Python over bytes Core already handed out; the only
RPCs are `getaddressinfo` and `listdescriptors`. The secret lives in one local
variable for one signature and is not written anywhere.
"""

from __future__ import annotations

import hashlib
import re
import struct
from dataclasses import dataclass

from . import bip32, tap

SIGHASH_DEFAULT = 0x00
SIGHASH_ALL = 0x01
SIGHASH_NONE = 0x02
SIGHASH_SINGLE = 0x03
SIGHASH_ANYONECANPAY = 0x80

OP_0, OP_PUSHDATA1, OP_PUSHDATA2, OP_PUSHDATA4 = 0x00, 0x4C, 0x4D, 0x4E
OP_1NEGATE, OP_1, OP_16 = 0x4F, 0x51, 0x60
OP_IF, OP_ENDIF, OP_CHECKSIG = 0x63, 0x68, 0xAC


class RevealSignError(Exception):
    pass


# --- transactions -------------------------------------------------------------

@dataclass
class TxIn:
    prev_txid: bytes      # 32 bytes, serialization order (little-endian)
    prev_vout: int
    script_sig: bytes
    sequence: int


@dataclass
class TxOut:
    value: int
    script_pubkey: bytes


@dataclass
class Tx:
    version: int
    vins: list
    vouts: list
    locktime: int
    witnesses: list | None = None   # one list of stack items per input, or None

    def txid(self) -> str:
        return hashlib.sha256(hashlib.sha256(serialize_tx(self, with_witness=False)).digest()).digest()[::-1].hex()


def _read_varint(b: bytes, i: int) -> tuple[int, int]:
    first = b[i]
    if first < 0xFD:
        return first, i + 1
    if first == 0xFD:
        return struct.unpack_from("<H", b, i + 1)[0], i + 3
    if first == 0xFE:
        return struct.unpack_from("<I", b, i + 1)[0], i + 5
    return struct.unpack_from("<Q", b, i + 1)[0], i + 9


def _read_bytes(b: bytes, i: int) -> tuple[bytes, int]:
    n, i = _read_varint(b, i)
    return b[i:i + n], i + n


def parse_tx(raw: bytes) -> Tx:
    """Parse a raw transaction, with or without the segwit marker."""
    i = 0
    version = struct.unpack_from("<i", raw, i)[0]
    i += 4
    segwit = raw[i] == 0 and raw[i + 1] == 1
    if segwit:
        i += 2
    n_in, i = _read_varint(raw, i)
    vins = []
    for _ in range(n_in):
        prev = raw[i:i + 32]
        vout = struct.unpack_from("<I", raw, i + 32)[0]
        i += 36
        script_sig, i = _read_bytes(raw, i)
        sequence = struct.unpack_from("<I", raw, i)[0]
        i += 4
        vins.append(TxIn(prev, vout, script_sig, sequence))
    n_out, i = _read_varint(raw, i)
    vouts = []
    for _ in range(n_out):
        value = struct.unpack_from("<q", raw, i)[0]
        i += 8
        spk, i = _read_bytes(raw, i)
        vouts.append(TxOut(value, spk))
    witnesses = None
    if segwit:
        witnesses = []
        for _ in range(n_in):
            n_items, i = _read_varint(raw, i)
            items = []
            for _ in range(n_items):
                item, i = _read_bytes(raw, i)
                items.append(item)
            witnesses.append(items)
    locktime = struct.unpack_from("<I", raw, i)[0]
    i += 4
    if i != len(raw):
        raise RevealSignError("trailing bytes after transaction")
    return Tx(version, vins, vouts, locktime, witnesses)


def _ser_outpoint(vin: TxIn) -> bytes:
    return vin.prev_txid + struct.pack("<I", vin.prev_vout)


def _ser_output(out: TxOut) -> bytes:
    return struct.pack("<q", out.value) + tap.compact_size(len(out.script_pubkey)) + out.script_pubkey


def serialize_tx(tx: Tx, with_witness: bool = True) -> bytes:
    has_witness = with_witness and tx.witnesses is not None and any(tx.witnesses)
    out = struct.pack("<i", tx.version)
    if has_witness:
        out += b"\x00\x01"
    out += tap.compact_size(len(tx.vins))
    for vin in tx.vins:
        out += _ser_outpoint(vin) + tap.compact_size(len(vin.script_sig)) + vin.script_sig
        out += struct.pack("<I", vin.sequence)
    out += tap.compact_size(len(tx.vouts))
    for vout in tx.vouts:
        out += _ser_output(vout)
    if has_witness:
        for items in tx.witnesses:
            out += tap.compact_size(len(items))
            for item in items:
                out += tap.compact_size(len(item)) + item
    out += struct.pack("<I", tx.locktime)
    return out


def tx_weight(tx: Tx) -> int:
    base = len(serialize_tx(tx, with_witness=False))
    total = len(serialize_tx(tx, with_witness=True))
    return base * 3 + total


# --- BIP341 signature hash ------------------------------------------------------

def _sha256(b: bytes) -> bytes:
    return hashlib.sha256(b).digest()


def taproot_sighash(tx: Tx, input_index: int, spent: list[tuple[bytes, int]], *,
                    hash_type: int = SIGHASH_DEFAULT, leaf_hash: bytes | None = None,
                    annex: bytes | None = None, codesep_pos: int = 0xFFFFFFFF) -> bytes:
    """The BIP341 signature message hash for input `input_index`.

    `spent` lists (scriptPubKey, amount) for EVERY input of `tx`, in order:
    taproot commits to all of them. `leaf_hash` set means a script-path spend
    (BIP342 extension, ext_flag 1); None is a key-path spend.
    """
    if len(spent) != len(tx.vins):
        raise RevealSignError("need the scriptPubKey and amount of every input")
    if hash_type not in (0x00, 0x01, 0x02, 0x03, 0x81, 0x82, 0x83):
        raise RevealSignError(f"invalid sighash type {hash_type:#x}")
    out_type = SIGHASH_ALL if hash_type == SIGHASH_DEFAULT else hash_type & 3
    anyone_can_pay = bool(hash_type & SIGHASH_ANYONECANPAY)
    if out_type == SIGHASH_SINGLE and input_index >= len(tx.vouts):
        raise RevealSignError("SIGHASH_SINGLE without a matching output")

    msg = bytes([hash_type]) + struct.pack("<i", tx.version) + struct.pack("<I", tx.locktime)
    if not anyone_can_pay:
        msg += _sha256(b"".join(_ser_outpoint(v) for v in tx.vins))
        msg += _sha256(b"".join(struct.pack("<q", amt) for _, amt in spent))
        msg += _sha256(b"".join(tap.compact_size(len(spk)) + spk for spk, _ in spent))
        msg += _sha256(b"".join(struct.pack("<I", v.sequence) for v in tx.vins))
    if out_type not in (SIGHASH_NONE, SIGHASH_SINGLE):
        msg += _sha256(b"".join(_ser_output(o) for o in tx.vouts))
    ext_flag = 0 if leaf_hash is None else 1
    spend_type = ext_flag * 2 + (1 if annex is not None else 0)
    msg += bytes([spend_type])
    if anyone_can_pay:
        vin = tx.vins[input_index]
        spk, amt = spent[input_index]
        msg += _ser_outpoint(vin) + struct.pack("<q", amt)
        msg += tap.compact_size(len(spk)) + spk + struct.pack("<I", vin.sequence)
    else:
        msg += struct.pack("<I", input_index)
    if annex is not None:
        msg += _sha256(tap.compact_size(len(annex)) + annex)
    if out_type == SIGHASH_SINGLE:
        msg += _sha256(_ser_output(tx.vouts[input_index]))
    if leaf_hash is not None:
        msg += leaf_hash + b"\x00" + struct.pack("<I", codesep_pos)
    return tap._tagged_hash("TapSighash", b"\x00" + msg)


# --- the envelope and the control block -------------------------------------------

def _is_pushnum(op: int) -> bool:
    return op == OP_1NEGATE or OP_1 <= op <= OP_16


def envelope_leaf_key(script: bytes) -> bytes:
    """The 32-byte key closing a CANONICAL envelope, or RevealSignError.

    Canonical, as Core's `reveal.rs` defines it: `OP_FALSE OP_IF <pushes only>
    OP_ENDIF <32-byte key> OP_CHECKSIG` and nothing else. Anything looser could
    let the spend validate without `OP_CHECKSIG` ever running.
    """
    i, n = 0, len(script)
    if n < 2 or script[0] != OP_0 or script[1] != OP_IF:
        raise RevealSignError("leaf is not a canonical envelope (must start with OP_FALSE OP_IF)")
    i = 2
    while True:
        if i >= n:
            raise RevealSignError("leaf is not a canonical envelope (missing OP_ENDIF)")
        op = script[i]
        if op == OP_ENDIF:
            i += 1
            break
        if op <= 0x4B:
            i += 1 + op
        elif op == OP_PUSHDATA1:
            if i + 1 >= n:
                raise RevealSignError("unparsable script")
            i += 2 + script[i + 1]
        elif op == OP_PUSHDATA2:
            if i + 2 >= n:
                raise RevealSignError("unparsable script")
            i += 3 + struct.unpack_from("<H", script, i + 1)[0]
        elif op == OP_PUSHDATA4:
            if i + 4 >= n:
                raise RevealSignError("unparsable script")
            i += 5 + struct.unpack_from("<I", script, i + 1)[0]
        elif _is_pushnum(op):
            i += 1
        else:
            raise RevealSignError("leaf is not a canonical envelope (non-push opcode inside)")
        if i > n:
            raise RevealSignError("unparsable script")
    if i >= n or script[i] != 0x20 or i + 33 > n:
        raise RevealSignError("leaf is not a canonical envelope (OP_ENDIF must be followed by a 32-byte key)")
    key = script[i + 1:i + 33]
    i += 33
    if i >= n or script[i] != OP_CHECKSIG:
        raise RevealSignError("leaf is not a canonical envelope (key must be followed by OP_CHECKSIG)")
    if i + 1 != n:
        raise RevealSignError("leaf is not a canonical envelope (trailing bytes after OP_CHECKSIG)")
    if tap._lift_x(tap._int(key)) is None:
        raise RevealSignError("invalid envelope public key")
    return key


def _taproot_output_key(control_block: bytes, leaf: bytes) -> tuple[int, bytes]:
    """(parity, output key) the control block commits `leaf` to."""
    if len(control_block) < 33 or (len(control_block) - 33) % 32 != 0 or len(control_block) > 33 + 32 * 128:
        raise RevealSignError("invalid taproot control block")
    if control_block[0] & 0xFE != tap.TAPSCRIPT_LEAF_VERSION:
        raise RevealSignError(f"unsupported taproot leaf version 0x{control_block[0] & 0xFE:02x}")
    internal = control_block[1:33]
    if tap._lift_x(tap._int(internal)) is None:
        raise RevealSignError("invalid internal key in control block")
    node = tap.tapleaf_hash(leaf)
    path = control_block[33:]
    for j in range(0, len(path), 32):
        sibling = path[j:j + 32]
        pair = node + sibling if node < sibling else sibling + node
        node = tap._tagged_hash("TapBranch", pair)
    return tap.taproot_tweak_pubkey(internal, node)


def _hash160(b: bytes) -> bytes:
    return bip32._hash160(b)


def source_controls_key(source_spk: bytes, key: bytes) -> bool:
    """Mirror of Core's `source_controls_key`: is `key` a key of this address?"""
    point = tap._lift_x(tap._int(key))
    if point is None:
        return False
    even = b"\x02" + key
    odd = b"\x03" + key
    if len(source_spk) == 34 and source_spk[:2] == b"\x51\x20":          # P2TR
        output_key = source_spk[2:]
        if key == output_key:
            return True
        return tap.taproot_tweak_pubkey(key, b"")[1] == output_key
    if len(source_spk) == 22 and source_spk[:2] == b"\x00\x14":          # P2WPKH
        return source_spk[2:] in (_hash160(even), _hash160(odd))
    if len(source_spk) == 25 and source_spk[:3] == b"\x76\xa9\x14" and source_spk[23:] == b"\x88\xac":  # P2PKH
        x, y = point
        uncompressed = [b"\x04" + key + y.to_bytes(32, "big"), b"\x04" + key + (tap._p - y).to_bytes(32, "big")]
        return source_spk[3:23] in [_hash160(k) for k in (even, odd, *uncompressed)]
    if len(source_spk) == 23 and source_spk[:2] == b"\xa9\x14" and source_spk[22] == 0x87:  # P2SH-P2WPKH
        return source_spk[2:22] in (_hash160(b"\x00\x14" + _hash160(even)), _hash160(b"\x00\x14" + _hash160(odd)))
    return False


def reveal_source_error(commit_spk: bytes, source_spk: bytes, witness: list[bytes]) -> str | None:
    """Why a reveal would NOT be attributed to its source by Core v11.5.0, or None.

    A Python mirror of `counterparty-rs/src/reveal.rs`, run on the finished
    reveal before broadcast: the four conditions are a P2TR commit, a tapscript
    spend whose control block commits the leaf to the commit's output key, a
    canonical envelope, and a leaf key that belongs to `source_spk`.
    """
    if len(witness) != 3:
        return f"witness has {len(witness)} elements, a tapscript reveal has exactly 3"
    if len(commit_spk) != 34 or commit_spk[:2] != b"\x51\x20":
        return "the spent commit output is not P2TR"
    if tap._lift_x(tap._int(commit_spk[2:])) is None:
        return "invalid P2TR output key"
    leaf, control_block = witness[1], witness[2]
    try:
        parity, output_key = _taproot_output_key(control_block, leaf)
    except RevealSignError as e:
        return str(e)
    if output_key != commit_spk[2:] or parity != (control_block[0] & 1):
        return "control block does not commit the leaf to the output key"
    try:
        key = envelope_leaf_key(leaf)
    except RevealSignError as e:
        return str(e)
    if not source_controls_key(source_spk, key):
        return "envelope key does not belong to the source address"
    return None


# --- signing --------------------------------------------------------------------

def sign_reveal(unsigned_reveal_hex: str, commit_script_pubkey_hex: str, commit_value: int,
                envelope_script_hex: str, control_block_hex: str, reveal_pubkey_hex: str,
                secret: int, *, aux_rand: bytes | None = None) -> tuple[str, str]:
    """Sign Core's unsigned reveal with `secret`; (signed hex, txid).

    Checks before signing: the reveal spends exactly one input with no witness
    yet, the envelope is canonical and closed by `reveal_pubkey`, `secret` is
    that key's secret, and the control block commits the envelope to the
    commit output's key. The signature is SIGHASH_DEFAULT (64 bytes), the
    size Core budgeted the reveal's fee for.
    """
    tx = parse_tx(bytes.fromhex(unsigned_reveal_hex))
    if len(tx.vins) != 1:
        raise RevealSignError(f"reveal has {len(tx.vins)} inputs, expected the commit output only")
    if tx.witnesses and any(tx.witnesses):
        raise RevealSignError("reveal is already signed")
    leaf = bytes.fromhex(envelope_script_hex)
    control_block = bytes.fromhex(control_block_hex)
    reveal_pubkey = bytes.fromhex(reveal_pubkey_hex)
    commit_spk = bytes.fromhex(commit_script_pubkey_hex)
    if len(reveal_pubkey) != 32:
        raise RevealSignError("reveal_pubkey must be a 32-byte x-only key")
    if envelope_leaf_key(leaf) != reveal_pubkey:
        raise RevealSignError("the envelope is not closed by reveal_pubkey")
    if tap.xonly_from_secret(secret) != reveal_pubkey:
        raise RevealSignError("the exported key is not reveal_pubkey; refusing to sign")
    parity, output_key = _taproot_output_key(control_block, leaf)
    if len(commit_spk) != 34 or commit_spk[:2] != b"\x51\x20" or commit_spk[2:] != output_key \
            or parity != (control_block[0] & 1):
        raise RevealSignError("control block does not commit the envelope to the commit output")
    if commit_value <= 0:
        raise RevealSignError("commit value must be positive")

    digest = taproot_sighash(tx, 0, [(commit_spk, commit_value)],
                             hash_type=SIGHASH_DEFAULT, leaf_hash=tap.tapleaf_hash(leaf))
    sig = tap.schnorr_sign(secret, digest, aux_rand)
    tx.witnesses = [[sig, leaf, control_block]]
    return serialize_tx(tx).hex(), tx.txid()


# --- Bitcoin Core: which key, and the key itself ---------------------------------------

@dataclass
class SourceKey:
    address: str
    kind: str                   # "p2tr" | "p2wpkh" | "p2sh-p2wpkh" | "p2pkh"
    compressed: bytes           # 33-byte SEC key; for P2TR the BIP86 internal key (even-y form)
    hdkeypath: str | None
    fingerprint: str | None

    @property
    def xonly(self) -> bytes:
        return self.compressed[1:]


_TR_DESC_KEY = re.compile(r"^tr\((?:\[[^\]]*\])?([0-9a-fA-F]{64})\)")


def source_pubkey(btc, wallet: str, source: str) -> SourceKey:
    """The key Core must close the envelope with: the source's own.

    From `getaddressinfo`: `pubkey` for P2WPKH / P2SH-P2WPKH / P2PKH, and the
    internal key out of the `tr(...)` descriptor for P2TR (Core v11.5.0 accepts
    either the output key or the BIP86 internal key; the internal key is the
    one the wallet actually holds, so its signature needs no tweak).
    """
    info = btc.wallet_call(wallet, "getaddressinfo", [source])
    if not info.get("ismine"):
        raise RevealSignError(f"{source} does not belong to wallet {wallet!r}")
    wv = info.get("witness_version")
    if wv == 1:
        kind = "p2tr"
        m = _TR_DESC_KEY.match(info.get("desc") or "")
        if not m:
            raise RevealSignError(f"cannot read the internal key of {source} from its descriptor")
        compressed = b"\x02" + bytes.fromhex(m.group(1))
    else:
        pub = info.get("pubkey")
        if not pub:
            raise RevealSignError(f"Bitcoin Core reports no public key for {source}")
        compressed = bytes.fromhex(pub)
        if len(compressed) != 33:
            raise RevealSignError(f"{source} uses an uncompressed key; taproot encoding needs P2WPKH or P2TR")
        if wv == 0:
            kind = "p2wpkh"
        elif info.get("isscript") and info.get("embedded", {}).get("iswitness"):
            kind = "p2sh-p2wpkh"
        else:
            kind = "p2pkh"
    return SourceKey(source, kind, compressed, info.get("hdkeypath"), info.get("hdmasterfingerprint"))


_XPRV_IN_DESC = re.compile(r"((?:\[[^\]]*\])?)((?:xprv|tprv)[1-9A-HJ-NP-Za-km-z]{20,})((?:/[0-9]+[h']?)*)(/\*)?")
_WIF_IN_DESC = re.compile(r"[(,]\s*(?:\[[^\]]*\])?([5KLc9][1-9A-HJ-NP-Za-km-z]{50,51})\s*[),]")


def export_secret(btc, wallet: str, key: SourceKey) -> int:
    """The secret scalar behind `key`, read from the wallet's private descriptors.

    Walks every descriptor `listdescriptors true` returns: a ranged xprv
    descriptor is derived along the address's own `hdkeypath` (past the depth
    the xprv already sits at), a bare WIF is decoded as is. The first secret
    whose public key equals `key.compressed` wins; anything else is rejected,
    so a wrong derivation can never sign. Requires an unlocked wallet.
    """
    try:
        listing = btc.wallet_call(wallet, "listdescriptors", [True])
    except Exception as e:  # noqa: BLE001 - the RPC error text is the useful part
        raise RevealSignError(f"cannot read private descriptors of wallet {wallet!r} (locked?): {e}") from e
    full_path = bip32.parse_path(key.hdkeypath) if key.hdkeypath else None

    def matches(secret: int) -> bool:
        pub = bip32.pubkey_from_secret(secret)
        # A P2TR address only pins the x coordinate of its internal key.
        return pub[1:] == key.compressed[1:] if key.kind == "p2tr" else pub == key.compressed
    for entry in listing.get("descriptors", []):
        desc = entry.get("desc", "").split("#", 1)[0]
        for m in _XPRV_IN_DESC.finditer(desc):
            if full_path is None:
                continue
            try:
                node = bip32.node_from_xprv(m.group(2))
            except ValueError:
                continue
            fixed = bip32.parse_path(m.group(3)) if m.group(3) else []
            remainder = full_path[node.depth:]
            if remainder[:len(fixed)] != fixed:
                continue
            try:
                child = bip32.derive(node, remainder)
            except ValueError:
                continue
            if matches(child.secret):
                return child.secret
        for m in _WIF_IN_DESC.finditer(desc):
            try:
                secret = bip32.wif_to_secret(m.group(1))
            except ValueError:
                continue
            if matches(secret):
                return secret
    raise RevealSignError(f"no private key for {key.address} found in wallet {wallet!r}'s descriptors")
