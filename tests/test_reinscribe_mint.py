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
