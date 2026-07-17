"""envelope_style() — ord vs generic carrier detection (enrichment only).

The fixtures mirror counterparty-rs's classifier: ord-style iff the
tapscript's third instruction pushes b"ord" and the fourth pushes 0x07.
"""

from counters.reveal import envelope_style

_MARKER_VOUT = {"scriptPubKey": {"hex": "6a08434e545250525459"}}

# OP_FALSE OP_IF "ord" 0x07 "xcp" OP_0 <data> OP_ENDIF <pubkey> OP_CHECKSIG
_ORD = (
    "0063" + "036f7264" + "0107" + "03786370" + "00" + "0401020304"
    + "68" + "20" + "22" * 32 + "ac"
)
# OP_FALSE OP_IF <data> OP_ENDIF <pubkey> OP_CHECKSIG (Counterparty generic)
_GENERIC = "0063" + "0401020304" + "68" + "20" + "22" * 32 + "ac"


def _tx(tapscript_hex: str) -> dict:
    return {
        "vout": [_MARKER_VOUT],
        "vin": [{"txinwitness": ["00" * 64, tapscript_hex, "c0" + "11" * 32]}],
    }


def test_ord_envelope_detected():
    assert envelope_style(_tx(_ORD)) == "ord"


def test_generic_envelope_detected():
    assert envelope_style(_tx(_GENERIC)) == "generic"


def test_non_reveal_returns_none():
    tx = _tx(_ORD)
    tx["vout"] = [{"scriptPubKey": {"hex": "6a04deadbeef"}}]
    assert envelope_style(tx) is None


def test_two_item_witness_returns_none():
    tx = _tx(_ORD)
    tx["vin"][0]["txinwitness"] = ["00" * 64, _ORD]
    assert envelope_style(tx) is None


def test_unparseable_tapscript_falls_back_to_generic():
    assert envelope_style(_tx("4c")) == "generic"  # truncated OP_PUSHDATA1
