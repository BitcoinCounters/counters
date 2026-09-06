"""`counters wallet inscribe --envelope counterparty|ord` — the taproot envelope choice.

Two styles exist and both count equally (build ref v3 §13): Counterparty's
**counterparty** envelope, and the ordinals-compatible **ord** one Core
emits for `inscription=true`. Three things are worth pinning:

  1. the flag reaches cmd_inscribe, defaulting to counterparty (today's behaviour);
  2. `inscription` is sent to Core ONLY for ord — an older Core rejects
     parameters it does not know, so counterparty must stay a bare compose;
  3. `ord` is refused, not accepted, when compose ignores it — Core silently
     falls back to counterparty for message types it cannot inscribe, and the style
     is committed to by the commit address, so a fallback is unfixable later.

Nothing here touches bitcoind or Counterparty Core.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import counters.__main__ as M  # noqa: E402
from counters.counterparty import CounterpartyClient  # noqa: E402
from counters.reveal import envelope_style  # noqa: E402

SOURCE = "bc1px9kxjuc9f8fz5lnnlca0wgl86suncxwjhw80eq7nx3c2asldrjhssneteg"

# The two tapscripts Core builds, as they appear in a reveal's input-0 witness.
# ord: OP_FALSE OP_IF "ord" 07 "xcp" 01 <mime> OP_0 <body> OP_ENDIF <pk> OP_CHECKSIG
_ORD_SCRIPT = (
    "0063" + "036f7264" + "0107" + "03786370" + "0107" + "0a696d6167652f6a706567"
    + "00" + "0401020304" + "68" + "20" + "22" * 32 + "ac"
)
# counterparty: OP_FALSE OP_IF <cbor message chunks> OP_ENDIF <pk> OP_CHECKSIG
_GENERIC_SCRIPT = "0063" + "0401020304" + "68" + "20" + "22" * 32 + "ac"


def _reveal(tapscript_hex: str) -> dict:
    """A decoderawtransaction-shaped reveal carrying `tapscript_hex`."""
    return {
        "txid": "c" * 64,
        "weight": 1000,
        "vout": [{"scriptPubKey": {"hex": "6a08434e545250525459"}}],
        "vin": [{"txinwitness": ["00" * 64, tapscript_hex, "c0" + "11" * 32]}],
    }


def _stub_inscribe():
    """Swap cmd_inscribe for a recorder; give back (calls, restore)."""
    calls = []
    orig = M.inscribe.cmd_inscribe

    def fake(*a, **kw):
        calls.append((a, kw))
        return 0

    M.inscribe.cmd_inscribe = fake
    return calls, lambda: setattr(M.inscribe, "cmd_inscribe", orig)


# --- 1. the flag reaches the command ----------------------------------------

def test_envelope_defaults_to_native():
    calls, restore = _stub_inscribe()
    try:
        assert M.main(["wallet", "--name", "counts", "inscribe",
                       "--file", "cat.png"]) == 0
    finally:
        restore()
    assert calls[0][1]["envelope"] == "counterparty"


def test_envelope_ord_is_passed_through():
    calls, restore = _stub_inscribe()
    try:
        assert M.main(["wallet", "--name", "counts", "inscribe",
                       "--file", "cat.png", "--envelope", "counterparty/ord"]) == 0
    finally:
        restore()
    assert calls[0][1]["envelope"] == "counterparty/ord"


def test_unknown_envelope_is_a_usage_error():
    calls, restore = _stub_inscribe()
    try:
        M.main(["wallet", "--name", "counts", "inscribe",
                "--file", "cat.png", "--envelope", "stamp"])
    except SystemExit as e:
        assert e.code == 2
    else:
        raise AssertionError("an unknown envelope style must be rejected")
    finally:
        restore()
    assert not calls


# --- 2. what compose_issuance sends Core ------------------------------------

class _RecordingClient(CounterpartyClient):
    """A client that captures compose params instead of calling Core."""

    def __init__(self):
        self.params = None

    def _post(self, path, params=None):
        self.params = params
        return {"result": {"rawtransaction": "00", "signed_reveal_rawtransaction": "01"}}


def _compose(**kw):
    cp = _RecordingClient()
    cp.compose_issuance(source=SOURCE, asset="MYCOUNTER", quantity=1,
                        divisible=False, description="hi", encoding="taproot", **kw)
    return cp.params


def test_native_sends_no_inscription_param():
    # An older Core errors on parameters it does not know: absent, not false.
    assert "inscription" not in _compose()
    assert "inscription" not in _compose(inscription=False)


def test_ord_sends_inscription_true():
    assert _compose(inscription=True)["inscription"] == "true"


# --- 3. the composed reveal is classified before anything is broadcast ------

def test_classifier_agrees_with_each_composed_style():
    assert envelope_style(_reveal(_ORD_SCRIPT)) == "counterparty/ord"
    assert envelope_style(_reveal(_GENERIC_SCRIPT)) == "counterparty"


def test_silent_fallback_is_detectable():
    # Core answering an --envelope ord request with a counterparty envelope is the
    # case the command must catch: the styles differ, so the mismatch is visible.
    requested = "counterparty/ord"
    assert envelope_style(_reveal(_GENERIC_SCRIPT)) != requested


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all envelope-option tests passed")
