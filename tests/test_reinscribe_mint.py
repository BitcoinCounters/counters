"""Unit test for the reinscription mint helper.

`_prepare_reinscribe` must route the commit's change to the asset OWNER address
(so the reveal spends from it, proving issuance rights) and produce a reveal
shape with NO Counterparty OP_RETURN and no destination outputs.

Run: python tests/test_reinscribe_mint.py   (or via pytest)
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counters import builder  # noqa: E402
from counters.commands.inscribe import _prepare_reinscribe  # noqa: E402

COIN = 100_000_000
OWNER = "bc1powneraddressxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"


class FakeBtc:
    """Just enough of BitcoindClient for _build_commit + _prepare_reinscribe."""

    def __init__(self, commit_address: str, owner_addr: str):
        self.commit_address = commit_address
        self.owner_addr = owner_addr
        self.change_address_seen = None

    def wallet_call(self, wallet, method, params=None, timeout=-1.0):
        if method == "createrawtransaction":
            return "00"
        if method == "fundrawtransaction":
            # record the pinned change address the caller requested
            self.change_address_seen = params[1].get("changeAddress")
            return {"hex": "01"}
        if method == "signrawtransactionwithwallet":
            return {"complete": True, "hex": "02"}
        raise AssertionError(f"unexpected wallet_call {method}")

    def _call(self, method, params=None):
        if method == "decoderawtransaction":
            return {
                "txid": "ab" * 32,
                "vout": [
                    {"n": 0, "value": 546 / COIN,
                     "scriptPubKey": {"hex": "5120" + "11" * 32,
                                      "address": self.commit_address}},
                    {"n": 1, "value": 100_000 / COIN,
                     "scriptPubKey": {"hex": "5120" + "22" * 32,
                                      "address": self.owner_addr}},
                ],
            }
        raise AssertionError(f"unexpected _call {method}")


def test_prepare_reinscribe_routes_change_to_owner_no_op_return():
    insc = builder.build_inscription(b"image/png", b"hello", asset=b"RAREPEPE")
    btc = FakeBtc(insc.commit_address, OWNER)
    change_spk = bytes.fromhex("5120" + "33" * 32)

    built = _prepare_reinscribe(btc, "w", insc, OWNER, 5.0, change_spk)

    # commit change was pinned to the owner address -> reveal spends from owner
    assert btc.change_address_seen == OWNER
    assert built["source_vout"] == 1              # the change (owner) output
    assert built["source_value"] == 100_000
    # a pure inscription: no Counterparty message, no destination outputs
    assert built["op_return_spk"] is None
    assert built["dest_outs"] == []
    assert built["reveal_vsize"] > 0


def test_reinscribe_envelope_carries_asset_tag():
    # the mint must embed the target asset so the indexer can bind it
    insc = builder.build_inscription(b"text/plain", b"x", asset=b"PARENT.CHILD")
    from counters.envelope import find_counter_envelopes
    envs = find_counter_envelopes(insc.leaf)
    assert len(envs) == 1 and envs[0].asset == b"PARENT.CHILD"


# P2PKH scripts for the tests below.
_P2PKH_SPK = bytes.fromhex("76a914" + "55" * 20 + "88ac")   # legacy 1...
_P2TR_SPK = bytes.fromhex("5120" + "44" * 32)               # taproot bc1p


def test_estimate_reveal_vsize_accounts_for_legacy_p2pkh_source():
    """A Counterwallet owner address is P2PKH: its ~107-byte signature sits in
    the full-weight scriptSig, so the reveal is larger than for a witness
    (taproot) source. The estimate must reflect that, or the fee is too low."""
    from counters import tap
    from counters.commands.inscribe import _estimate_reveal_vsize

    insc = builder.build_inscription(b"image/png", b"x" * 400, asset=b"PEPEME")

    def fresh():
        return tap.Tx(vin=[tap.TxIn("aa" * 32, 0), tap.TxIn("bb" * 32, 1)],
                      vout=[tap.TxOut(1000, bytes.fromhex("5120" + "33" * 32))])

    v_segwit = _estimate_reveal_vsize(fresh(), insc.leaf, insc.control_block,
                                      source_spk=_P2TR_SPK)
    v_legacy = _estimate_reveal_vsize(fresh(), insc.leaf, insc.control_block,
                                      source_spk=_P2PKH_SPK)
    assert v_legacy > v_segwit


def test_sign_reveal_accepts_legacy_p2pkh_scriptsig_source():
    """A legacy P2PKH owner address (Counterwallet reinscription source) is
    signed by Core into the SCRIPTSIG, not the witness. _sign_reveal must accept
    that instead of erroring 'Core did not sign the source input'."""
    from counters import tap
    from counters.commands.inscribe import _sign_reveal

    insc = builder.build_inscription(b"image/png", b"hello", asset=b"PEPEME")
    reveal = tap.Tx(
        vin=[tap.TxIn("aa" * 32, 1), tap.TxIn("bb" * 32, 0)],
        vout=[tap.TxOut(1000, bytes.fromhex("5120" + "33" * 32))],
    )
    scriptsig_hex = "47" + "30" * 71 + "21" + "02" * 33  # push sig(71) + push pubkey(33)

    class SigBtc:
        def wallet_call(self, wallet, method, params=None, timeout=-1.0):
            assert method == "signrawtransactionwithwallet"
            return {"complete": True, "hex": "00"}

        def _call(self, method, params=None):
            assert method == "decoderawtransaction"
            # vin[0] signed via scriptSig (legacy P2PKH), NO witness.
            return {"vin": [{"scriptSig": {"hex": scriptsig_hex}},
                            {"scriptSig": {"hex": ""}}]}

    out = _sign_reveal(SigBtc(), "w", reveal, insc,
                       prevouts=[(100_000, _P2PKH_SPK),
                                 (546, insc.commit_script_pubkey)])
    assert isinstance(out, str) and out
    assert reveal.vin[0].script_sig == bytes.fromhex(scriptsig_hex)
    assert reveal.vin[0].witness == []           # legacy input: empty witness stack
    assert len(reveal.vin[1].witness) == 3       # our taproot script-path spend


def test_sign_reveal_errors_when_core_signs_nothing():
    from counters import tap
    from counters.commands.inscribe import InscribeError, _sign_reveal

    insc = builder.build_inscription(b"image/png", b"hi", asset=b"PEPEME")
    reveal = tap.Tx(vin=[tap.TxIn("aa" * 32, 1), tap.TxIn("bb" * 32, 0)],
                    vout=[tap.TxOut(1000, bytes.fromhex("5120" + "33" * 32))])

    class UnsignedBtc:
        def wallet_call(self, wallet, method, params=None, timeout=-1.0):
            return {"complete": False, "errors": [{"error": "no key"}], "hex": "00"}

        def _call(self, method, params=None):
            return {"vin": [{"scriptSig": {"hex": ""}}, {"scriptSig": {"hex": ""}}]}

    try:
        _sign_reveal(UnsignedBtc(), "w", reveal, insc,
                     prevouts=[(100_000, _P2PKH_SPK), (546, insc.commit_script_pubkey)])
    except InscribeError as e:
        assert "did not sign" in str(e)
    else:
        raise AssertionError("expected InscribeError when Core signs nothing")


class _RecordingBtc:
    """Records every wallet_call so we can assert which wallet funded the commit
    and what inputs/options were used."""

    def __init__(self, commit_address, owner_addr, owner_spk):
        self.commit_address = commit_address
        self.owner_addr = owner_addr
        self.owner_spk = owner_spk
        self.calls = []

    def wallet_call(self, wallet, method, params=None, timeout=-1.0):
        self.calls.append((wallet, method, params))
        if method == "createrawtransaction":
            return "00"
        if method == "fundrawtransaction":
            return {"hex": "01"}
        if method == "signrawtransactionwithwallet":
            return {"complete": True, "hex": "02"}
        raise AssertionError(f"unexpected wallet_call {method}")

    def _call(self, method, params=None):
        assert method == "decoderawtransaction"
        return {
            "txid": "ab" * 32,
            "vout": [
                {"n": 0, "value": 546 / COIN,
                 "scriptPubKey": {"hex": "5120" + "11" * 32, "address": self.commit_address}},
                {"n": 1, "value": 100_000 / COIN,
                 "scriptPubKey": {"hex": self.owner_spk.hex(), "address": self.owner_addr}},
            ],
        }


def test_prepare_reinscribe_funds_from_pinned_inputs():
    """--fund-from-address/--fund-from-input: the commit is funded from the pinned
    coins, but change still routes to the owner so the reveal proves ownership."""
    insc = builder.build_inscription(b"image/png", b"hi", asset=b"PEPEME")
    owner_spk = bytes.fromhex("76a914" + "55" * 20 + "88ac")   # legacy P2PKH owner
    change_spk = bytes.fromhex("5120" + "33" * 32)
    btc = _RecordingBtc(insc.commit_address, OWNER, owner_spk)

    built = _prepare_reinscribe(btc, "legacypepe", insc, OWNER, 2.0, change_spk,
                                fund_inputs=[{"txid": "dd" * 32, "vout": 3}])

    # Funded/signed by the --name wallet, from ONLY the pinned coin.
    assert {w for (w, m, p) in btc.calls} == {"legacypepe"}
    create = next(p for (w, m, p) in btc.calls if m == "createrawtransaction")
    assert create[0] == [{"txid": "dd" * 32, "vout": 3}]
    fund = next(p for (w, m, p) in btc.calls if m == "fundrawtransaction")
    assert fund[1]["add_inputs"] is False
    # Change still goes to the owner address -> reveal can prove ownership.
    assert fund[1]["changeAddress"] == OWNER
    assert built["source_spk"] == owner_spk


class _UtxoBtc:
    """Serves listunspent for a fixed address -> UTXO set."""

    def __init__(self, by_address):
        self._by_address = by_address

    def wallet_call(self, wallet, method, params=None, timeout=-1.0):
        assert method == "listunspent"
        address = params[2][0]
        return self._by_address.get(address, [])


def test_collect_fund_inputs_combines_and_dedupes():
    """--fund-from-address and --fund-from-input can be COMBINED: the coins are
    unioned and deduped (an explicit input that also sits at the address appears
    once). Order preserved: the explicit input first, then the address's UTXOs."""
    from counters.commands.inscribe import _collect_fund_inputs

    dup = ("dd" * 32, 0)   # this UTXO is BOTH the explicit input and at the address
    other = ("ee" * 32, 1)
    btc = _UtxoBtc({"1FundAddr": [
        {"txid": dup[0], "vout": dup[1]},
        {"txid": other[0], "vout": other[1]},
    ]})
    out = _collect_fund_inputs(btc, "w", "1FundAddr", f"{dup[0]}:{dup[1]}")
    assert out == [
        {"txid": dup[0], "vout": dup[1]},     # explicit input first, once
        {"txid": other[0], "vout": other[1]},
    ]


def test_collect_fund_inputs_none_when_unspecified():
    from counters.commands.inscribe import _collect_fund_inputs
    assert _collect_fund_inputs(_UtxoBtc({}), "w", None, None) is None


def test_collect_fund_inputs_rejects_bad_input_and_empty_address():
    from counters.commands.inscribe import InscribeError, _collect_fund_inputs

    for bad in ("notxidvout", "abc:xyz"):
        try:
            _collect_fund_inputs(_UtxoBtc({}), "w", None, bad)
        except InscribeError:
            pass
        else:
            raise AssertionError(f"expected InscribeError for --fund-from-input {bad!r}")

    try:
        _collect_fund_inputs(_UtxoBtc({"1Empty": []}), "w", "1Empty", None)
    except InscribeError as e:
        assert "no spendable UTXO" in str(e)
    else:
        raise AssertionError("expected InscribeError for an address with no UTXOs")


def test_reinscribe_targets_current_owner_not_original_issuer():
    """Rare Pepes are usually ACQUIRED: assets_info.issuer is the original
    creator (immutable), .owner is the current holder. The reinscribe owner
    check must use .owner (matching the indexer's issuer_at_height), else an
    asset you legitimately own is wrongly rejected."""
    import io
    import tempfile
    from contextlib import redirect_stderr

    from counters.commands import inscribe as m
    from counters.config import Config

    ISSUER = "1OriginalCreatorAAAAAAAAAAAAAAAAAAA"
    OWNER_NOW = "1CurrentOwnerBBBBBBBBBBBBBBBBBBBBBB"

    class FakeBtc:
        def wallet_call(self, wallet, method, params=None, timeout=-1.0):
            if method == "getrawchangeaddress":
                return "bc1qchange00000000000000000000000000000000"
            if method == "listreceivedbyaddress":
                return [{"address": "1SomeOtherWalletAddr"}]   # holds neither
            if method == "listunspent":
                return []
            raise AssertionError(f"unexpected wallet_call {method}")

        def _call(self, method, params=None):
            if method == "validateaddress":
                return {"scriptPubKey": "0014" + "11" * 20}
            raise AssertionError(f"unexpected _call {method}")

    class FakeCp:
        def get_asset(self, asset):
            return {"asset": "PEPEME", "issuer": ISSUER, "owner": OWNER_NOW,
                    "asset_longname": None, "asset_id": "12345"}

    orig_btc, orig_cp = m.BitcoindClient, m.CounterpartyClient
    m.BitcoindClient = lambda cfg: FakeBtc()
    m.CounterpartyClient = lambda cfg: FakeCp()
    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg") as fh:
            fh.write(b"jpegbytes")
            fh.flush()
            err = io.StringIO()
            with redirect_stderr(err):
                rc = m.cmd_inscribe(Config(), "legacypepe", fh.name,
                                    asset="PEPEME", reinscribe=True, fee_rate=1.0)
    finally:
        m.BitcoindClient, m.CounterpartyClient = orig_btc, orig_cp

    out = err.getvalue()
    assert rc == 1
    # The rejection must name the CURRENT owner, proving we checked .owner.
    assert OWNER_NOW in out
    assert ISSUER not in out


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    print(f"\n{'OK' if failures == 0 else f'{failures} FAILED'}")
    raise SystemExit(1 if failures else 0)
