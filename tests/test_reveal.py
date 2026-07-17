"""Taproot-reveal (carrier) detection — rule R4, build ref v3 §4.

Zero-dependency runner: python tests/test_reveal.py   (or via pytest)
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counters.config import REVEAL_OP_RETURN_SCRIPT  # noqa: E402
from counters.reveal import (  # noqa: E402
    ScriptParseError,
    commit_txid,
    is_marker_op_return,
    is_taproot_reveal,
    parse_script,
)

MARKER_SCRIPT_HEX = "6a08434e545250525459"  # OP_RETURN PUSH8 "CNTRPRTY"


def _tx(vout_scripts: list[str], witness: list[str] | None = None,
        prev_txid: str = "cc" * 32) -> dict:
    return {
        "vout": [{"scriptPubKey": {"hex": h}} for h in vout_scripts],
        "vin": [{"txid": prev_txid, "vout": 0,
                 "txinwitness": witness if witness is not None else []}],
    }


REVEAL_WITNESS = ["aa" * 64, "0063036f7264", "c0" + "bb" * 32]  # sig, script, control


def test_parse_script_push_forms():
    # direct push, OP_PUSHDATA1, OP_PUSHDATA2 all tokenize to the same data
    data = b"CNTRPRTY"
    direct = bytes([8]) + data
    pd1 = bytes([0x4C, 8]) + data
    pd2 = bytes([0x4D, 8, 0]) + data
    for script in (direct, pd1, pd2):
        ops = parse_script(script)
        assert len(ops) == 1 and ops[0][1] == data


def test_parse_script_truncation_raises():
    for bad in (bytes([10, 1, 2]), bytes([0x4C]), bytes([0x4D, 5])):
        try:
            parse_script(bad)
        except ScriptParseError:
            continue
        raise AssertionError(f"{bad!r} should have raised")


def test_marker_op_return_exact_shape():
    assert is_marker_op_return(bytes.fromhex(MARKER_SCRIPT_HEX))
    assert bytes.fromhex(MARKER_SCRIPT_HEX) == REVEAL_OP_RETURN_SCRIPT
    # any push encoding of the marker matches (mirrors Counterparty's rule,
    # which compares the pushed bytes, not the encoding)
    assert is_marker_op_return(bytes.fromhex("6a4c08434e545250525459"))


def test_marker_op_return_rejects_wrong_shapes():
    # marker followed by a payload push: NOT the reveal shape (classic data
    # after the prefix is ARC4-encrypted; the literal form is marker-ONLY)
    assert not is_marker_op_return(bytes.fromhex(MARKER_SCRIPT_HEX + "04deadbeef"))
    # marker as a PREFIX of a longer single push
    assert not is_marker_op_return(bytes.fromhex("6a0c434e545250525459deadbeef"))
    # ordinary (encrypted-looking) OP_RETURN data
    assert not is_marker_op_return(bytes.fromhex("6a08" + "de" * 8))
    # not an OP_RETURN at all — P2WPKH-shaped
    assert not is_marker_op_return(bytes.fromhex("0014" + "ab" * 20))
    # truncated garbage
    assert not is_marker_op_return(bytes.fromhex("6a4c"))


def test_is_taproot_reveal_requires_marker_and_witness_shape():
    # marker OP_RETURN + 3-item witness on input 0 -> reveal
    assert is_taproot_reveal(_tx([MARKER_SCRIPT_HEX], REVEAL_WITNESS))
    # marker output may sit after other outputs
    assert is_taproot_reveal(
        _tx(["0014" + "ab" * 20, MARKER_SCRIPT_HEX], REVEAL_WITNESS)
    )
    # no marker output -> no reveal, whatever the witness looks like
    assert not is_taproot_reveal(_tx(["6a08" + "de" * 8], REVEAL_WITNESS))
    # marker but a key-path (1-item) witness -> not the script-path shape
    assert not is_taproot_reveal(_tx([MARKER_SCRIPT_HEX], ["aa" * 64]))
    # marker but no witness at all (legacy input)
    assert not is_taproot_reveal(_tx([MARKER_SCRIPT_HEX], []))
    # marker but 2-item witness
    assert not is_taproot_reveal(_tx([MARKER_SCRIPT_HEX], ["aa", "bb"]))


def test_commit_txid_is_input0_prevout():
    tx = _tx([MARKER_SCRIPT_HEX], REVEAL_WITNESS, prev_txid="11" * 32)
    assert commit_txid(tx) == "11" * 32
    assert commit_txid(_tx([MARKER_SCRIPT_HEX], ["aa"])) is None


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
