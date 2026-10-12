"""revealsign.py: the reveal is signed by the source key, exactly as Core v11.5.0 demands.

The fixtures that matter are published vectors: BIP340 for the signature,
BIP341 (Bitcoin Core's `bip341_wallet_vectors.json`, key-path case 0) for the
sighash. The script-path extension on top of them is exercised end to end:
a commit built the way Core builds it, `P2TR(source_key, [envelope])`, a
reveal signed by `sign_reveal`, and the mirror of Core's rule accepting it,
while every single deviation Core rejects is rejected here too.
"""

from __future__ import annotations

import hashlib

import pytest

from counters import bip32, revealsign as rs, tap

# --- BIP340 (first vectors of the published CSV) ------------------------------------

BIP340 = [
    # secret, aux, msg, signature
    ("0000000000000000000000000000000000000000000000000000000000000003",
     "0000000000000000000000000000000000000000000000000000000000000000",
     "0000000000000000000000000000000000000000000000000000000000000000",
     "E907831F80848D1069A5371B402410364BDF1C5F8307B0084C55F1CE2DCA821525F66A4A85EA8B71E482A74F382D2CE5EBEEE8FDB2172F477DF4900D310536C0"),
    ("B7E151628AED2A6ABF7158809CF4F3C762E7160F38B4DA56A784D9045190CFEF",
     "0000000000000000000000000000000000000000000000000000000000000001",
     "243F6A8885A308D313198A2E03707344A4093822299F31D0082EFA98EC4E6C89",
     "6896BD60EEAE296DB48A229FF71DFE071BDE413E6D43F917DC8DCF8C78DE33418906D11AC976ABCCB20B091292BFF4EA897EFCB639EA871CFA95F6DE339E4B0A"),
    ("C90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74020BBEA63B14E5C9",
     "C87AA53824B4D7AE2EB035A2B5BBBCCC080E76CDC6D1692C4B0B62D798E6D906",
     "7E2D58D8B3BCDF1ABADEC7829054F90DDA9805AAB56C77333024B9D0A508B75C",
     "5831AAEED7B44BB74E5EAB94BA9D4294C49BCF2A60728D8B4C200F50DD313C1BAB745879A5AD954A72C45A91C3A51D3C7ADEA98D82F8481E0E1E03674A6F3FB7"),
]


@pytest.mark.parametrize("secret,aux,msg,sig", BIP340)
def test_bip340_vectors(secret, aux, msg, sig):
    produced = tap.schnorr_sign(int(secret, 16), bytes.fromhex(msg), bytes.fromhex(aux))
    assert produced.hex().upper() == sig
    assert tap.schnorr_verify(tap.xonly_from_secret(int(secret, 16)), bytes.fromhex(msg), produced)


def test_bip340_verify_rejects_a_flipped_bit():
    secret, aux, msg, sig = BIP340[1]
    bad = bytearray(bytes.fromhex(sig))
    bad[40] ^= 1
    assert not tap.schnorr_verify(tap.xonly_from_secret(int(secret, 16)), bytes.fromhex(msg), bytes(bad))


# --- BIP341 sighash (Bitcoin Core bip341_wallet_vectors.json, keyPathSpending[0]) ---------

BIP341_TX = (
    "02000000097de20cbff686da83a54981d2b9bab3586f4ca7e48f57f5b55963115f3b334e9c010000000000000000d7b7cab57b1393ace2d064f4d4a2cb8af6def61273e127517d44759b6dafdd990000000000fffffffff8e1f583384333689228c5d28eac13366be082dc57441760d957275419a418420000000000fffffffff0689180aa63b30cb162a73c6d2a38b7eeda2a83ece74310fda0843ad604853b0100000000feffffffaa5202bdf6d8ccd2ee0f0202afbbb7461d9264a25e5bfd3c5a52ee1239e0ba6c0000000000feffffff956149bdc66faa968eb2be2d2faa29718acbfe3941215893a2a3446d32acd050000000000000000000e664b9773b88c09c32cb70a2a3e4da0ced63b7ba3b22f848531bbb1d5d5f4c94010000000000000000e9aa6b8e6c9de67619e6a3924ae25696bb7b694bb677a632a74ef7eadfd4eabf0000000000ffffffffa778eb6a263dc090464cd125c466b5a99667720b1c110468831d058aa1b82af10100000000ffffffff0200ca9a3b000000001976a91406afd46bcdfd22ef94ac122aa11f241244a37ecc88ac807840cb0000000020ac9a87f5594be208f8532db38cff670c450ed2fea8fcdefcc9a663f78bab962b0065cd1d"
)
BIP341_SPENT = [
    ("512053a1f6e454df1aa2776a2814a721372d6258050de330b3c6d10ee8f4e0dda343", 420000000),
    ("5120147c9c57132f6e7ecddba9800bb0c4449251c92a1e60371ee77557b6620f3ea3", 462000000),
    ("76a914751e76e8199196d454941c45d1b3a323f1433bd688ac", 294000000),
    ("5120e4d810fd50586274face62b8a807eb9719cef49c04177cc6b76a9a4251d5450e", 504000000),
    ("512091b64d5324723a985170e4dc5a0f84c041804f2cd12660fa5dec09fc21783605", 630000000),
    ("00147dd65592d0ab2fe0d0257d571abf032cd9db93dc", 378000000),
    ("512075169f4001aa68f15bbed28b218df1d0a62cbbcf1188c6665110c293c907b831", 672000000),
    ("5120712447206d7a5238acc7ff53fbe94a3b64539ad291c7cdbc490b7577e4b17df5", 546000000),
    ("512077e30a5522dd9f894c3f8b8bd4c4b2cf82ca7da8a3ea6a239655c39c050ab220", 588000000),
]
BIP341_SIGHASHES = [
    # txinIndex, hashType, sigHash
    (0, 3, "2514a6272f85cfa0f45eb907fcb0d121b808ed37c6ea160a5a9046ed5526d555"),
    (1, 0x83, "325a644af47e8a5a2591cda0ab0723978537318f10e6a63d4eed783b96a71a4d"),
    (3, 1, "bf013ea93474aa67815b1b6cc441d23b64fa310911d991e713cd34c7f5d46669"),
    (4, 0, "4f900a0bae3f1446fd48490c2958b5a023228f01661cda3496a11da502a7f7ef"),
    (6, 2, "15f25c298eb5cdc7eb1d638dd2d45c97c4c59dcaec6679cfc16ad84f30876b85"),
    (7, 0x82, "cd292de50313804dabe4685e83f923d2969577191a3e1d2882220dca88cbeb10"),
    (8, 0x81, "cccb739eca6c13a8a89e6e5cd317ffe55669bbda23f2fd37b0f18755e008edd2"),
]


def test_tx_round_trip():
    tx = rs.parse_tx(bytes.fromhex(BIP341_TX))
    assert len(tx.vins) == 9 and len(tx.vouts) == 2
    assert rs.serialize_tx(tx).hex() == BIP341_TX
    assert tx.txid() == hashlib.sha256(hashlib.sha256(bytes.fromhex(BIP341_TX)).digest()).digest()[::-1].hex()


@pytest.mark.parametrize("index,hash_type,expected", BIP341_SIGHASHES)
def test_bip341_sighash_vectors(index, hash_type, expected):
    tx = rs.parse_tx(bytes.fromhex(BIP341_TX))
    spent = [(bytes.fromhex(spk), amt) for spk, amt in BIP341_SPENT]
    assert rs.taproot_sighash(tx, index, spent, hash_type=hash_type).hex() == expected


def test_sighash_needs_every_prevout():
    tx = rs.parse_tx(bytes.fromhex(BIP341_TX))
    with pytest.raises(rs.RevealSignError):
        rs.taproot_sighash(tx, 0, [(b"", 1)])


# --- the envelope, the commit and the reveal -------------------------------------------

SECRET = 0x1234567890ABCDEF1234567890ABCDEF1234567890ABCDEF1234567890ABCDEF
XONLY = tap.xonly_from_secret(SECRET)
OTHER = tap.xonly_from_secret(SECRET + 1)


def push(data: bytes) -> bytes:
    if len(data) < 0x4C:
        return bytes([len(data)]) + data
    if len(data) <= 0xFF:
        return b"\x4c" + bytes([len(data)]) + data
    return b"\x4d" + len(data).to_bytes(2, "little") + data


def envelope(key: bytes = XONLY, body: bytes = b"\x16" + b"X" * 300, ord_style: bool = False) -> bytes:
    inner = push(b"CNTRPRTY") + push(body)
    if ord_style:
        inner = push(b"ord") + b"\x51" + push(b"text/plain") + b"\x00" + push(body)  # OP_1 and OP_0 are pushes
    return b"\x00\x63" + inner + b"\x68" + b"\x20" + key + b"\xac"


def commit_for(leaf: bytes, internal: bytes = XONLY) -> tuple[bytes, bytes]:
    """(commit scriptPubKey, control block) of P2TR(internal, [leaf]), as Core builds it."""
    parity, output_key = tap.taproot_tweak_pubkey(internal, tap.tapleaf_hash(leaf))
    return b"\x51\x20" + output_key, bytes([0xC0 | parity]) + internal


def unsigned_reveal(commit_txid: bytes = b"\x11" * 32) -> str:
    tx = rs.Tx(2, [rs.TxIn(commit_txid, 0, b"", 0xFFFFFFFD)],
               [rs.TxOut(0, b"\x6a\x08CNTRPRTY")], 0)
    return rs.serialize_tx(tx).hex()


def p2wpkh_spk(xonly: bytes, parity: int = 2) -> bytes:
    return b"\x00\x14" + bip32._hash160(bytes([parity]) + xonly)


def p2tr_bip86_spk(xonly: bytes) -> bytes:
    return b"\x51\x20" + tap.taproot_tweak_pubkey(xonly, b"")[1]


def test_envelope_leaf_key_reads_canonical_envelopes():
    assert rs.envelope_leaf_key(envelope()) == XONLY
    assert rs.envelope_leaf_key(envelope(ord_style=True)) == XONLY
    big = envelope(body=b"\x16" + b"Y" * 60000)       # OP_PUSHDATA2 body
    assert rs.envelope_leaf_key(big) == XONLY


@pytest.mark.parametrize("leaf,reason", [
    (b"\x63" + envelope()[2:], "OP_FALSE"),                                   # no OP_FALSE
    (b"\x00\x63" + push(b"x") + b"\x75\x68\x20" + XONLY + b"\xac", "non-push"),  # OP_DROP inside
    (b"\x00\x63" + push(b"x"), "missing OP_ENDIF"),
    (b"\x00\x63" + push(b"x") + b"\x20" + XONLY + b"\xac", "non-push"),            # 0x20 is itself a push
    (envelope() + b"\x75", "trailing"),                                       # bytes after CHECKSIG
    (envelope()[:-1] + b"\xad", "OP_CHECKSIG"),                               # CHECKSIGVERIFY
    (b"\x00\x63" + push(b"x") + b"\x68" + push(b"\x02" + XONLY) + b"\xac", "32-byte key"),
])
def test_envelope_leaf_key_rejects_non_canonical_shapes(leaf, reason):
    with pytest.raises(rs.RevealSignError) as e:
        rs.envelope_leaf_key(leaf)
    assert reason.split()[0] in str(e.value)


def test_source_controls_key_for_every_supported_source_type():
    even = b"\x02" + XONLY
    assert rs.source_controls_key(p2wpkh_spk(XONLY), XONLY)
    assert rs.source_controls_key(p2tr_bip86_spk(XONLY), XONLY)              # BIP86 internal key
    assert rs.source_controls_key(b"\x51\x20" + XONLY, XONLY)                 # output key itself
    assert rs.source_controls_key(b"\x76\xa9\x14" + bip32._hash160(even) + b"\x88\xac", XONLY)
    assert rs.source_controls_key(b"\xa9\x14" + bip32._hash160(b"\x00\x14" + bip32._hash160(even)) + b"\x87", XONLY)
    assert not rs.source_controls_key(p2wpkh_spk(XONLY), OTHER)
    assert not rs.source_controls_key(p2tr_bip86_spk(XONLY), OTHER)
    assert not rs.source_controls_key(b"\x00\x20" + b"\x00" * 32, XONLY)       # P2WSH: no single key


def test_sign_reveal_produces_a_reveal_core_attributes_to_the_source():
    leaf = envelope()
    commit_spk, control_block = commit_for(leaf)
    signed_hex, txid = rs.sign_reveal(unsigned_reveal(), commit_spk.hex(), 20000,
                                      leaf.hex(), control_block.hex(), XONLY.hex(), SECRET)
    tx = rs.parse_tx(bytes.fromhex(signed_hex))
    sig, wit_leaf, wit_cb = tx.witnesses[0]
    assert len(sig) == 64 and wit_leaf == leaf and wit_cb == control_block
    assert tx.txid() == txid == rs.parse_tx(bytes.fromhex(unsigned_reveal())).txid()
    digest = rs.taproot_sighash(tx, 0, [(commit_spk, 20000)], leaf_hash=tap.tapleaf_hash(leaf))
    assert tap.schnorr_verify(XONLY, digest, sig)
    # Core's rule, for a P2WPKH source and for a BIP86 P2TR source of the same key
    assert rs.reveal_source_error(commit_spk, p2wpkh_spk(XONLY), tx.witnesses[0]) is None
    assert rs.reveal_source_error(commit_spk, p2tr_bip86_spk(XONLY), tx.witnesses[0]) is None
    # and it is deterministic for a fixed aux
    again, _ = rs.sign_reveal(unsigned_reveal(), commit_spk.hex(), 20000, leaf.hex(),
                              control_block.hex(), XONLY.hex(), SECRET, aux_rand=bytes(32))
    twice, _ = rs.sign_reveal(unsigned_reveal(), commit_spk.hex(), 20000, leaf.hex(),
                              control_block.hex(), XONLY.hex(), SECRET, aux_rand=bytes(32))
    assert again == twice


def test_sign_reveal_accepts_a_commit_whose_internal_key_is_not_the_source():
    leaf = envelope()
    nums = bytes.fromhex("50929b74c1a04954b78b4b6035e97a5e078a5a0f28ec96d547bfee9ace803ac0")
    commit_spk, control_block = commit_for(leaf, internal=nums)
    signed_hex, _ = rs.sign_reveal(unsigned_reveal(), commit_spk.hex(), 20000,
                                   leaf.hex(), control_block.hex(), XONLY.hex(), SECRET)
    assert rs.reveal_source_error(commit_spk, p2wpkh_spk(XONLY),
                                  rs.parse_tx(bytes.fromhex(signed_hex)).witnesses[0]) is None


def test_sign_reveal_refuses_what_core_would_ignore():
    leaf = envelope()
    commit_spk, control_block = commit_for(leaf)
    other_leaf = envelope(key=OTHER)
    other_spk, other_cb = commit_for(other_leaf, internal=OTHER)
    # the exported key is not the envelope's key
    with pytest.raises(rs.RevealSignError, match="not reveal_pubkey"):
        rs.sign_reveal(unsigned_reveal(), commit_spk.hex(), 20000, leaf.hex(), control_block.hex(),
                       XONLY.hex(), SECRET + 1)
    # Core closed the envelope with some other key
    with pytest.raises(rs.RevealSignError, match="not closed by reveal_pubkey"):
        rs.sign_reveal(unsigned_reveal(), other_spk.hex(), 20000, other_leaf.hex(), other_cb.hex(),
                       XONLY.hex(), SECRET)
    # control block does not commit this envelope to this commit output
    with pytest.raises(rs.RevealSignError, match="control block"):
        rs.sign_reveal(unsigned_reveal(), other_spk.hex(), 20000, leaf.hex(), control_block.hex(),
                       XONLY.hex(), SECRET)
    # the reveal is already signed
    signed_hex, _ = rs.sign_reveal(unsigned_reveal(), commit_spk.hex(), 20000, leaf.hex(),
                                   control_block.hex(), XONLY.hex(), SECRET)
    with pytest.raises(rs.RevealSignError, match="already signed"):
        rs.sign_reveal(signed_hex, commit_spk.hex(), 20000, leaf.hex(), control_block.hex(),
                       XONLY.hex(), SECRET)


def test_reveal_source_error_mirrors_core():
    leaf = envelope()
    commit_spk, control_block = commit_for(leaf)
    signed_hex, _ = rs.sign_reveal(unsigned_reveal(), commit_spk.hex(), 20000, leaf.hex(),
                                   control_block.hex(), XONLY.hex(), SECRET)
    sig, _, _ = rs.parse_tx(bytes.fromhex(signed_hex)).witnesses[0]
    good = [sig, leaf, control_block]
    source = p2wpkh_spk(XONLY)
    assert rs.reveal_source_error(commit_spk, source, good) is None
    # the reported attack: the leaf names the attacker's key, the victim funded the commit
    assert "does not belong" in rs.reveal_source_error(commit_spk, p2wpkh_spk(OTHER), good)
    # P2WSH-style commit
    assert "not P2TR" in rs.reveal_source_error(b"\x00\x20" + b"\x00" * 32, source, good)
    # wrong witness shape
    assert "exactly 3" in rs.reveal_source_error(commit_spk, source, [sig, leaf])
    # unknown leaf version
    bad_cb = bytes([0xC2 | (control_block[0] & 1)]) + control_block[1:]
    assert "leaf version" in rs.reveal_source_error(commit_spk, source, [sig, leaf, bad_cb])
    # control block that does not commit the leaf
    assert "does not commit" in rs.reveal_source_error(commit_spk, source, [sig, envelope(body=b"\x16zz"), control_block])
    # a leaf that is not a canonical envelope
    plain = b"\x20" + XONLY + b"\xac"
    plain_spk, plain_cb = commit_for(plain)
    assert "canonical envelope" in rs.reveal_source_error(plain_spk, source, [sig, plain, plain_cb])


def test_reveal_source_error_walks_a_merkle_path():
    leaf = envelope()
    sibling = b"\x20" + OTHER + b"\xac"
    h_leaf, h_sib = tap.tapleaf_hash(leaf), tap.tapleaf_hash(sibling)
    root = tap._tagged_hash("TapBranch", h_leaf + h_sib if h_leaf < h_sib else h_sib + h_leaf)
    parity, output_key = tap.taproot_tweak_pubkey(XONLY, root)
    commit_spk = b"\x51\x20" + output_key
    control_block = bytes([0xC0 | parity]) + XONLY + h_sib
    tx = rs.parse_tx(bytes.fromhex(unsigned_reveal()))
    digest = rs.taproot_sighash(tx, 0, [(commit_spk, 1000)], leaf_hash=h_leaf)
    sig = tap.schnorr_sign(SECRET, digest, bytes(32))
    assert rs.reveal_source_error(commit_spk, p2wpkh_spk(XONLY), [sig, leaf, control_block]) is None


# --- Bitcoin Core: which key, and the key itself ----------------------------------------

SEED = bytes(range(64))


class FakeBtc:
    """Just enough of BitcoindClient: getaddressinfo + listdescriptors."""

    def __init__(self, infos: dict, descriptors: list[str]):
        self.infos, self.descriptors = infos, descriptors

    def wallet_call(self, wallet, method, params=None, timeout=-1.0):
        if method == "getaddressinfo":
            return dict(self.infos[params[0]])
        if method == "listdescriptors":
            assert params == [True]
            return {"descriptors": [{"desc": d + "#abcdefgh"} for d in self.descriptors]}
        raise AssertionError(method)


def _account(purpose: int):
    master = bip32._master_from_seed(SEED)
    node = bip32.derive(master, bip32.parse_path(f"m/{purpose}h/0h/0h"))
    return node, bip32._fingerprint(master.secret).hex()


def test_source_pubkey_and_export_secret_for_a_bip84_address():
    acct, fp = _account(84)
    child = bip32.derive(acct, [0, 7])
    pub = bip32.pubkey_from_secret(child.secret)
    addr = bip32.address_from_pubkey("segwit", pub)
    btc = FakeBtc(
        {addr: {"ismine": True, "witness_version": 0, "pubkey": pub.hex(),
                "hdkeypath": "m/84h/0h/0h/0/7", "hdmasterfingerprint": fp}},
        [f"pkh([{fp}/44h/0h/0h]{_account(44)[0].xprv()}/0/*)",
         f"wpkh([{fp}/84h/0h/0h]{acct.xprv()}/0/*)",
         f"wpkh([{fp}/84h/0h/0h]{acct.xprv()}/1/*)"],
    )
    key = rs.source_pubkey(btc, "w", addr)
    assert key.kind == "p2wpkh" and key.compressed == pub and key.xonly == pub[1:]
    assert rs.export_secret(btc, "w", key) == child.secret


def test_source_pubkey_and_export_secret_for_a_bip86_address():
    acct, fp = _account(86)
    child = bip32.derive(acct, [1, 3])                       # a change address
    pub = bip32.pubkey_from_secret(child.secret)
    addr = bip32.address_from_pubkey("taproot", pub)
    btc = FakeBtc(
        {addr: {"ismine": True, "witness_version": 1,
                "desc": f"tr([{fp}/86h/0h/0h/1/3]{pub[1:].hex()})#zzzzzzzz",
                "hdkeypath": "m/86h/0h/0h/1/3", "hdmasterfingerprint": fp}},
        [f"tr([{fp}/86h/0h/0h]{acct.xprv()}/0/*)", f"tr([{fp}/86h/0h/0h]{acct.xprv()}/1/*)"],
    )
    key = rs.source_pubkey(btc, "w", addr)
    assert key.kind == "p2tr" and key.xonly == pub[1:]
    secret = rs.export_secret(btc, "w", key)
    assert tap.xonly_from_secret(secret) == pub[1:]
    # the key Core v11.5.0 accepts for this source: the BIP86 internal key
    assert rs.source_controls_key(p2tr_bip86_spk(pub[1:]), key.xonly)


def test_export_secret_reads_a_bare_wif_descriptor():
    secret = 0xC0FFEE
    pub = bip32.pubkey_from_secret(secret)
    addr = bip32.address_from_pubkey("segwit", pub)
    wif = bip32._b58check(b"\x80" + secret.to_bytes(32, "big") + b"\x01")
    btc = FakeBtc({addr: {"ismine": True, "witness_version": 0, "pubkey": pub.hex()}},
                  [f"wpkh({wif})"])
    key = rs.source_pubkey(btc, "w", addr)
    assert rs.export_secret(btc, "w", key) == secret


def test_export_secret_refuses_a_key_that_does_not_match():
    acct, fp = _account(84)
    child = bip32.derive(acct, [0, 0])
    pub = bip32.pubkey_from_secret(child.secret)
    addr = bip32.address_from_pubkey("segwit", pub)
    other, _ = _account(49)                                   # a different account's xprv
    btc = FakeBtc({addr: {"ismine": True, "witness_version": 0, "pubkey": pub.hex(),
                          "hdkeypath": "m/84h/0h/0h/0/0", "hdmasterfingerprint": fp}},
                  [f"wpkh([{fp}/84h/0h/0h]{other.xprv()}/0/*)"])
    with pytest.raises(rs.RevealSignError, match="no private key"):
        rs.export_secret(btc, "w", rs.source_pubkey(btc, "w", addr))


def test_source_pubkey_rejects_foreign_and_uncompressed_addresses():
    btc = FakeBtc({"x": {"ismine": False}, "y": {"ismine": True, "pubkey": "04" + "00" * 64}}, [])
    with pytest.raises(rs.RevealSignError, match="does not belong"):
        rs.source_pubkey(btc, "w", "x")
    with pytest.raises(rs.RevealSignError, match="uncompressed"):
        rs.source_pubkey(btc, "w", "y")
