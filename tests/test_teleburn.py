"""Offline tests for teleburn.py: the per-asset unspendable address.

The vectors are the protocol: a change here is a change to every burn ever
made. They were derived with this module, re-derived with the standalone
reference script (docs/teleburn_address.py, its own EC math), and checked
with `bitcoin-cli decodescript` on 2026-11-05.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counters import tap, teleburn  # noqa: E402

VECTORS = {
    "XCP": "bc1pj76jq96xhq25jcxl8qgrecz5sv2sfphj247emczw3pw6smhah6sqalf224",
    "A16756519242061643639": "bc1py6gs0j0duqtyu0xr8mp3ewjhu3l0tlhdfptv8zcqyf38qvuryhsseel6e2",
    "PEPECASH": "bc1p6c47mk09xk42u8aslc4c6u503n6s635st6hxmxj6p06nntjqxg2qj5wqch",
}


def test_mainnet_vectors():
    for asset, address in VECTORS.items():
        assert teleburn.teleburn_address(asset) == address


def test_testnet_and_regtest_share_the_key_and_differ_only_in_prefix():
    x = teleburn.teleburn_xonly("XCP")
    assert teleburn.teleburn_address("XCP", "tb") == tap.p2tr_address(x, "tb")
    assert teleburn.teleburn_address("XCP", "tb") == \
        "tb1pj76jq96xhq25jcxl8qgrecz5sv2sfphj247emczw3pw6smhah6sq2hl9s6"
    assert teleburn.teleburn_address("XCP", "bcrt").startswith("bcrt1p")
    assert teleburn.hrp_for("mainnet") == "bc"
    assert teleburn.hrp_for("Regtest") == "bcrt"
    with pytest.raises(ValueError):
        teleburn.hrp_for("litecoin")


def test_spk_is_op1_push32_of_the_key():
    spk = teleburn.teleburn_spk("XCP")
    assert len(spk) == 34 and spk[:2] == b"\x51\x20"
    assert spk[2:] == teleburn.teleburn_xonly("XCP")
    assert teleburn.is_teleburn_spk(spk, "XCP")
    assert not teleburn.is_teleburn_spk(spk, "PEPECASH")


def test_address_recognition_folds_case_and_network():
    a = VECTORS["PEPECASH"]
    assert teleburn.is_teleburn_address(a, "PEPECASH")
    assert teleburn.is_teleburn_address(a.upper(), "PEPECASH")
    assert teleburn.is_teleburn_address(teleburn.teleburn_address("PEPECASH", "tb"), "PEPECASH")
    assert not teleburn.is_teleburn_address(a, "XCP")


def test_the_primitive_reproduces_the_october_draft_vector():
    # docs/teleburn.md (2026-10-16) derived `asset:A167…` under the same tag,
    # before the `asset:` prefix was dropped. Same primitive, different message.
    x = teleburn._derive("CountersTeleburn", b"asset:A16756519242061643639")
    assert tap.p2tr_address(x) == \
        "bc1p49j4g7s6dyu5f7ynlnj6ttjdm84lmum4p3fa2drsv4tnyapwe7xs9kdnpv"


def test_derivation_matches_the_formula_step_by_step():
    # m = tagged(TAG, name); t = tagged("TapTweak", H.x || m); Q = H + t*G.
    name = b"PEPECASH"
    m = tap._tagged_hash(teleburn.TAG, name)
    assert teleburn.commitment("PEPECASH") == m
    t = tap._int(tap._tagged_hash("TapTweak", teleburn.NUMS_X + m))
    H = tap._lift_x(tap._int(teleburn.NUMS_X))
    Q = tap._point_add(H, tap._point_mul(tap._G, t))
    assert tap._bytes32(Q[0]) == teleburn.teleburn_xonly("PEPECASH")


def test_nums_point_is_bip341s():
    # H.x = sha256(uncompressed G), per BIP341's "nothing up my sleeve" construction.
    import hashlib
    G_uncompressed = b"\x04" + tap._bytes32(tap._Gx) + tap._bytes32(tap._Gy)
    assert teleburn.NUMS_X == hashlib.sha256(G_uncompressed).digest()
    assert tap._lift_x(tap._int(teleburn.NUMS_X)) is not None


def test_canonical_names_accepted():
    for ok in ("XCP", "PEPECASH", "PEPE", "BBBB", "ZZZZZZZZZZZZ",
               f"A{teleburn.NUMERIC_MIN}", f"A{teleburn.NUMERIC_MAX}"):
        assert teleburn.canonical_asset_name(ok) == ok


@pytest.mark.parametrize("bad", [
    "", "BTC", "pepecash", "Pepecash", "PEP", "ABC", "AAAAA", "TOOLONGASSETNAME",
    "PARENT.child", "A0", f"A{teleburn.NUMERIC_MIN - 1}", f"A{teleburn.NUMERIC_MAX + 1}",
    "A016756519242061643639", "XCP ", " XCP", "A", "123",
])
def test_non_canonical_names_rejected(bad):
    with pytest.raises(ValueError):
        teleburn.canonical_asset_name(bad)
    with pytest.raises(ValueError):
        teleburn.teleburn_address(bad)


def test_no_case_folding_so_a_slip_cannot_derive_a_plausible_address():
    # PEPE is too short to be named, so "pepecash" must not quietly become PEPECASH.
    with pytest.raises(ValueError):
        teleburn.teleburn_address("pepecash")
