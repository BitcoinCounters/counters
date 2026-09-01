"""Unit tests for `counters wallet describe` — the traditional OP_RETURN
description (a tagline, or a URL to the metadata).

No network/Core: Bitcoin Core and Counterparty clients are faked, and the
wallet-address lookup is monkeypatched. Covers where the text comes from
(--text / --file / --clear), the guards (locked description, the literals
Counterparty reads as commands, a no-op change), the composed message, and
the hint printed when the text does not fit the OP_RETURN.
"""

from __future__ import annotations

import io
import os
import sys
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import counters.commands.issue as I  # noqa: E402
from counters.config import Config  # noqa: E402
from counters.counterparty import CounterpartyError  # noqa: E402

OWNER = "bc1pOwnerAddrxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"


class FakeBtc:
    def __init__(self):
        self.sent = None

    def wallet_call(self, wallet, method, params=None, timeout=-1.0):
        if method == "listunspent":   # ensure_funded: the source pays its own fee
            return [{"address": OWNER, "amount": 0.01, "spendable": True}]
        assert method == "signrawtransactionwithwallet"
        return {"complete": True, "hex": "signed00"}

    def _call(self, method, params=None):
        if method == "validateaddress":
            return {"isvalid": params[0].startswith("bc1")}
        if method == "testmempoolaccept":
            return [{"allowed": True, "txid": "tt"}]
        if method == "sendrawtransaction":
            self.sent = params[0]
            return "broadcasttxid"
        raise AssertionError(f"unexpected _call {method}")


class FakeCp:
    def __init__(self, info, error: Exception | None = None):
        self.info = info
        self.error = error
        self.compose_kwargs = None

    def get_asset(self, asset):
        if self.info and asset.upper() == self.info["asset"].upper():
            return self.info
        return None

    def compose_issuance(self, **kwargs):
        self.compose_kwargs = kwargs
        if self.error:
            raise self.error
        return {"rawtransaction": "aa"}


def _patch(info, addresses, error=None):
    fake_btc, fake_cp = FakeBtc(), FakeCp(info, error)
    orig = (I.BitcoindClient, I.CounterpartyClient, I._wallet_addresses)
    I.BitcoindClient = lambda cfg: fake_btc
    I.CounterpartyClient = lambda cfg: fake_cp
    I._wallet_addresses = lambda btc, wallet: addresses
    return fake_btc, fake_cp, orig


def _restore(orig):
    I.BitcoindClient, I.CounterpartyClient, I._wallet_addresses = orig


def _asset(name="MYASSET", description="old text", mime_type="text/plain",
           description_locked=False, divisible=False, block=960000):
    return {"asset": name, "asset_id": "123", "owner": OWNER, "issuer": "1Creator",
            "divisible": divisible, "locked": False, "description": description,
            "description_locked": description_locked, "mime_type": mime_type,
            "supply": 100, "asset_longname": None,
            "last_issuance_block_index": block}


def _run(info, addresses=(OWNER,), error=None, **kwargs):
    """Run cmd_describe with everything faked; return (rc, stdout, stderr)."""
    fake_btc, fake_cp, orig = _patch(info, list(addresses), error)
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            rc = I.cmd_describe(Config(), "me", kwargs.pop("asset", "MYASSET"), **kwargs)
    finally:
        _restore(orig)
    return rc, out.getvalue(), err.getvalue(), fake_btc, fake_cp


# --- where the text comes from ---------------------------------------------

def test_text_composes_zero_quantity_issuance():
    rc, out, _err, btc, cp = _run(_asset(), text="https://xcp.fun/MYASSET.json",
                                  dry_run=True)
    assert rc == 0
    k = cp.compose_kwargs
    assert k["source"] == OWNER and k["asset"] == "MYASSET"
    # A description change is a reissuance of NOTHING: no new supply, no lock,
    # and the whole point — a description of our own choosing.
    assert k["quantity"] == 0 and k["lock"] is False
    assert k["description"] == "https://xcp.fun/MYASSET.json"
    assert k["divisible"] is False
    # opreturn is the client default; the taproot envelope is `inscribe`'s job.
    assert k.get("encoding", "opreturn") == "opreturn"
    assert "mints no counter" in out
    assert btc.sent is None                      # dry-run: nothing broadcast


def test_divisibility_follows_the_asset():
    _rc, _out, _err, _btc, cp = _run(_asset(divisible=True), text="hi", dry_run=True)
    assert cp.compose_kwargs["divisible"] is True


def test_file_supplies_the_text_and_one_trailing_newline_is_dropped(tmp_path):
    path = tmp_path / "desc.txt"
    path.write_text("https://xcp.fun/MYASSET.json\n")
    _rc, _out, _err, _btc, cp = _run(_asset(), file_path=str(path), dry_run=True)
    assert cp.compose_kwargs["description"] == "https://xcp.fun/MYASSET.json"


def test_file_keeps_interior_newlines(tmp_path):
    path = tmp_path / "desc.txt"
    path.write_text("one\ntwo\n")
    _rc, _out, _err, _btc, cp = _run(_asset(), file_path=str(path), dry_run=True)
    assert cp.compose_kwargs["description"] == "one\ntwo"


def test_binary_file_is_refused_and_points_at_inscribe(tmp_path):
    path = tmp_path / "cat.gif"
    path.write_bytes(b"GIF89a\x00\xff\xfe\x01binary")
    rc, _out, err, _btc, cp = _run(_asset(), file_path=str(path), dry_run=True)
    assert rc == 1
    assert cp.compose_kwargs is None             # nothing composed
    assert "not UTF-8 text" in err and "inscribe" in err


def test_missing_file_is_reported(tmp_path):
    rc, _out, err, _btc, _cp = _run(_asset(), file_path=str(tmp_path / "nope.txt"))
    assert rc == 1 and "cannot read" in err


def test_clear_sets_an_empty_description():
    rc, _out, _err, _btc, cp = _run(_asset(), clear=True, dry_run=True)
    assert rc == 0 and cp.compose_kwargs["description"] == ""


def test_exactly_one_source_is_required():
    rc, _out, err, _btc, cp = _run(_asset(), dry_run=True)
    assert rc == 1 and cp.compose_kwargs is None and "exactly one" in err


def test_two_sources_are_refused(tmp_path):
    path = tmp_path / "desc.txt"
    path.write_text("from the file")
    rc, _out, err, _btc, cp = _run(_asset(), text="from the flag",
                                   file_path=str(path), dry_run=True)
    assert rc == 1 and cp.compose_kwargs is None and "exactly one" in err


# --- guards -----------------------------------------------------------------

def test_locked_description_is_refused():
    rc, _out, err, _btc, cp = _run(_asset(description_locked=True), text="new",
                                   dry_run=True)
    assert rc == 1 and cp.compose_kwargs is None
    assert "locked description" in err


def test_lock_literals_are_refused():
    # Counterparty reads these as commands and KEEPS the current description,
    # so composing them would silently do something else entirely.
    for literal, pointer in (("lock", "lock-supply"),
                             ("LOCK_DESCRIPTION", "lock-description")):
        rc, _out, err, _btc, cp = _run(_asset(), text=literal, dry_run=True)
        assert rc == 1 and cp.compose_kwargs is None
        assert pointer in err


def test_unchanged_description_costs_no_transaction():
    rc, _out, err, _btc, cp = _run(_asset(description="old text"), text="old text",
                                   dry_run=True)
    assert rc == 1 and cp.compose_kwargs is None
    assert "already reads exactly that" in err


def test_not_the_owner_is_refused():
    rc, _out, err, _btc, cp = _run(_asset(), addresses=["bc1pSomeoneElse"], text="new",
                                   dry_run=True)
    assert rc == 1 and cp.compose_kwargs is None
    assert "issuance rights" in err


def test_replacing_file_content_warns():
    # The asset's description is currently a counter's file: say so, loudly.
    rc, out, _err, _btc, _cp = _run(_asset(description="ff" * 2168, mime_type="image/gif"),
                                    text="https://xcp.fun/MYASSET.json", dry_run=True)
    assert rc == 0
    assert "WARNING" in out and "2,168 bytes of image/gif" in out


# --- confirmation, broadcast, and the size cap ------------------------------

def test_confirmation_refused_composes_nothing():
    orig_confirm = I._confirm_prompt
    I._confirm_prompt = lambda prompt: False
    try:
        rc, _out, err, btc, cp = _run(_asset(), text="new text")
    finally:
        I._confirm_prompt = orig_confirm
    assert rc == 1 and cp.compose_kwargs is None and btc.sent is None
    assert "aborted" in err


def test_yes_broadcasts():
    rc, out, _err, btc, _cp = _run(_asset(), text="BURN THEM ALL", assume_yes=True)
    assert rc == 0 and btc.sent == "signed00"
    assert "broadcast: broadcasttxid" in out


def test_oversize_description_hints_at_the_cap_and_at_inscribe():
    err_obj = CounterpartyError("One `OP_RETURN` output per transaction")
    rc, _out, err, _btc, _cp = _run(_asset(), text="x" * 200, assume_yes=True,
                                    error=err_obj)
    assert rc == 1
    assert "80-byte OP_RETURN" in err and "200 B" in err
    assert "inscribe --asset MYASSET" in err
