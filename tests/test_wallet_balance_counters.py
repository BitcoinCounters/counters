"""Unit tests for the counter column of `wallet balance`.

Each Counterparty asset the wallet holds (or holds the rights to) lists the
counters it carries, read from the local index: `counter #1,4,7`, oldest
first, since an asset accumulates one per qualifying event (N6). An asset with
no counter — XCP, or one whose description never travelled in a witness —
leaves the column empty, and a wallet with no index at all prints as before.
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counters.commands import wallet  # noqa: E402
from counters.config import Config  # noqa: E402
from counters.store import CounterRecord, Store  # noqa: E402


def _seed(data_dir: str, events: list[tuple[str, str | None]]) -> Config:
    """An index holding one counter per (asset, longname), numbered in order."""
    cfg = Config()
    cfg.data_dir = data_dir
    store = Store(cfg)
    for n, (asset, longname) in enumerate(events):
        body = f"file {n}".encode()
        store.add_counter(n, CounterRecord(
            asset=asset, asset_id=str(n), asset_longname=longname,
            kind="issuance", content_type="text/plain", content_type_raw=None,
            content_sha256=store.store_blob(body), content_length=len(body),
            is_pointer_like=False, mint_txid=f"{n:064x}", msg_index=0,
            block_index=902005 + n, cp_tx_index=n, source="bc1psource",
            divisible=False, supply=1,
        ))
    store.commit()
    store.close()
    return cfg


class FakeCp:
    def get_address_balances(self, addr):
        return [
            {"asset": "MULTI", "quantity": 2, "asset_info": {"divisible": False}},
            {"asset": "SINGLE", "quantity": 1, "asset_info": {"divisible": False}},
            {"asset": "A95428956661682177", "asset_longname": "PARENT.CHILD",
             "quantity": 1, "asset_info": {"divisible": False}},
            {"asset": "PLAIN", "quantity": 7, "asset_info": {"divisible": False}},
            {"asset": "XCP", "quantity": 500000000, "asset_info": {"divisible": True}},
        ]

    def get_address_owned_assets(self, addr):
        return [{"asset": "MULTI", "asset_longname": None},
                {"asset": "PLAIN", "asset_longname": None}]

    def get_address_dispensers(self, addr):
        return []

    def get_address_orders(self, addr, status=None):
        return []


class FakeBtc:
    def __init__(self, config):
        pass

    def wallet_call(self, name, method, params=None, timeout=-1.0):
        if method == "getbalances":
            return {"mine": {"trusted": 0.001}}
        if method == "listreceivedbyaddress":
            return [{"address": "bc1pmine"}]
        if method == "listunspent":
            return []
        if method == "listdescriptors":
            return {"descriptors": []}
        raise AssertionError(f"unexpected RPC {method}")


EVENTS = [("OTHER", None), ("MULTI", None), ("SINGLE", None), ("OTHER", None),
          ("MULTI", None), ("A95428956661682177", "PARENT.CHILD"),
          ("OTHER", None), ("MULTI", None)]


def _lines(out: str) -> dict[str, list[str]]:
    """First word of each indented row -> the row's remaining words, per
    occurrence (an asset appears once under each section)."""
    rows: dict[str, list[str]] = {}
    for line in out.splitlines():
        if line.startswith("  ") and line.strip():
            name, *rest = line.split()
            rows.setdefault(name, []).append(" ".join(rest))
    return rows


def test_balance_lists_every_counter_of_an_asset(capsys):
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(_seed(tmp, EVENTS))
        try:
            rc = wallet._report_cp_balances(FakeCp(), ["bc1pmine"], store)
        finally:
            store.close()
    out = capsys.readouterr().out
    rows = _lines(out)
    assert rc == 0
    # Held, then owned: the same counters under both sections.
    assert rows["MULTI"] == ["2 counter #1,4,7", "counter #1,4,7"]
    assert rows["SINGLE"] == ["1 counter #2"]
    # A subasset is shown by longname and looked up by its numeric name.
    assert rows["PARENT.CHILD"] == ["1 counter #5"]


def test_balance_leaves_the_column_empty_without_a_counter(capsys):
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(_seed(tmp, EVENTS))
        try:
            wallet._report_cp_balances(FakeCp(), ["bc1pmine"], store)
        finally:
            store.close()
    out = capsys.readouterr().out
    rows = _lines(out)
    assert rows["PLAIN"] == ["7", ""]
    assert rows["XCP"] == ["5.00000000"]
    # Empty means empty: no padding left dangling at the end of a row.
    assert all(line == line.rstrip() for line in out.splitlines())


def test_balance_counter_column_is_aligned(capsys):
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(_seed(tmp, EVENTS))
        try:
            wallet._report_cp_balances(FakeCp(), ["bc1pmine"], store)
        finally:
            store.close()
    held = capsys.readouterr().out.split("Ownership rights")[0]
    starts = {line.index("counter #") for line in held.splitlines()
              if "counter #" in line}
    assert len(starts) == 1
    # Clear of the widest quantity in the listing (XCP's 5.00000000).
    assert starts.pop() > len("  ") + 28 + len(" 5.00000000")


def test_balance_without_an_index_prints_as_before(capsys):
    rc = wallet._report_cp_balances(FakeCp(), ["bc1pmine"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "counter #" not in out
    assert _lines(out)["MULTI"] == ["2", ""]


def test_balance_does_not_create_an_index(monkeypatch, capsys):
    # A balance is a read: on a machine that never indexed, it must not leave
    # an empty counters.db behind.
    monkeypatch.setattr(wallet, "BitcoindClient", FakeBtc)
    monkeypatch.setattr(wallet, "CounterpartyClient", lambda config: FakeCp())
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config()
        cfg.data_dir = os.path.join(tmp, "never-indexed")
        rc = wallet.cmd_wallet_balance(cfg, "w")
        assert not os.path.exists(cfg.data_dir)
    out = capsys.readouterr().out
    assert rc == 0
    assert "MULTI" in out and "counter #" not in out


def test_balance_reads_the_index_through_the_command(monkeypatch, capsys):
    monkeypatch.setattr(wallet, "BitcoindClient", FakeBtc)
    monkeypatch.setattr(wallet, "CounterpartyClient", lambda config: FakeCp())
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _seed(tmp, EVENTS)
        assert wallet.cmd_wallet_balance(cfg, "w") == 0
        plain = _lines(capsys.readouterr().out)
        assert wallet.cmd_wallet_balance(cfg, "w", detailed=True) == 0
        detailed = _lines(capsys.readouterr().out)
    assert plain["MULTI"] == ["2 counter #1,4,7", "counter #1,4,7"]
    # --detailed: the same column on the holding and on the rights row.
    assert detailed["MULTI"] == ["2 counter #1,4,7",
                                 "(ownership rights) counter #1,4,7"]
    assert detailed["PLAIN"] == ["7", "(ownership rights)"]
