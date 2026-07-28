"""The wallet commands accept their values positionally or as flags.

`send 1NN... BONPARTY 100` and `send --destination 1NN... --asset BONPARTY
--amount 100` must dispatch identically; a value given both ways, or a
required one given neither way, is a usage error (exit 2, argparse-style).
These drive `main()` with the command modules' cmd_* functions stubbed out,
so nothing touches bitcoind or Counterparty.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import counters.__main__ as M  # noqa: E402

DEST = "1NNde3CtwjT6bFdX5JRDeZSDrtadCszstB"
HASH0 = "a" * 64
HASH1 = "b" * 64


def _stub(module, fn_name):
    """Swap a cmd_* for a recorder returning 0; give back (calls, restore)."""
    calls = []
    orig = getattr(module, fn_name)

    def fake(*a, **kw):
        calls.append((a, kw))
        return 0

    setattr(module, fn_name, fake)
    return calls, lambda: setattr(module, fn_name, orig)


def test_send_positional_and_flag_forms_dispatch_identically():
    calls, restore = _stub(M.send, "cmd_send")
    try:
        rc = M.main(["wallet", "--name", "counts", "send",
                     DEST, "BONPARTY", "100", "--fee-rate", "2"])
        assert rc == 0
        rc = M.main(["wallet", "--name", "counts", "send",
                     "--destination", DEST, "--asset", "BONPARTY",
                     "--amount", "100", "--fee-rate", "2"])
        assert rc == 0
        assert calls[0] == calls[1]
        (config, name, dest, asset, amount), kw = calls[0]
        assert (name, dest, asset, amount) == ("counts", DEST, "BONPARTY", "100")
        assert kw["fee_rate"] == 2.0
    finally:
        restore()


def test_send_mixed_forms_work_when_each_value_is_given_once():
    calls, restore = _stub(M.send, "cmd_send")
    try:
        rc = M.main(["wallet", "send", DEST, "--asset", "BONPARTY",
                     "--amount", "100"])
        assert rc == 0
        (_, _, dest, asset, amount), _ = calls[0]
        assert (dest, asset, amount) == (DEST, "BONPARTY", "100")
    finally:
        restore()


def test_send_value_given_twice_is_a_usage_error():
    calls, restore = _stub(M.send, "cmd_send")
    try:
        try:
            M.main(["wallet", "send", DEST, "BONPARTY", "100",
                    "--asset", "BONPARTY"])
            raise AssertionError("expected a usage error")
        except SystemExit as e:
            assert e.code == 2
        assert calls == []
    finally:
        restore()


def test_send_missing_value_is_a_usage_error():
    calls, restore = _stub(M.send, "cmd_send")
    try:
        try:
            M.main(["wallet", "send", "--destination", DEST, "--asset", "BONPARTY"])
            raise AssertionError("expected a usage error")
        except SystemExit as e:
            assert e.code == 2
        assert calls == []
    finally:
        restore()


def test_open_order_flag_form():
    calls, restore = _stub(M.order, "cmd_open_order")
    try:
        rc = M.main(["wallet", "open-order",
                     "--give-asset", "XCP", "--give-amount", "10",
                     "--get-asset", "BTC", "--get-amount", "0.001"])
        assert rc == 0
        (_, _, ga, gam, ta, tam), _ = calls[0]
        assert (ga, gam, ta, tam) == ("XCP", "10", "BTC", "0.001")
    finally:
        restore()


def test_dispenser_admin_flag_forms():
    calls, restore = _stub(M.dispenser, "cmd_open_dispenser")
    try:
        rc = M.main(["wallet", "open-dispenser", "--asset", "PEPECASH",
                     "--amount", "100", "--price", "5000"])
        assert rc == 0
        (_, _, asset, amount, price), _ = calls[0]
        assert (asset, amount, price) == ("PEPECASH", "100", 5000)
    finally:
        restore()
    calls, restore = _stub(M.dispenser, "cmd_close_dispenser")
    try:
        assert M.main(["wallet", "close-dispenser", "--asset", "PEPECASH"]) == 0
        assert calls[0][0][2] == "PEPECASH"
    finally:
        restore()


def test_cancel_order_txhash_flag():
    calls, restore = _stub(M.order, "cmd_cancel_order")
    try:
        assert M.main(["wallet", "cancel-order", "--txhash", HASH0]) == 0
        assert M.main(["wallet", "cancel-order", HASH0]) == 0
        assert calls[0][0][2] == calls[1][0][2] == HASH0
    finally:
        restore()


def test_pay_order_match_id_stays_optional():
    calls, restore = _stub(M.order, "cmd_pay_order")
    try:
        assert M.main(["wallet", "pay-order"]) == 0
        assert calls[0][1]["match_id"] is None
        assert M.main(["wallet", "pay-order", "--match-id", f"{HASH0}_{HASH1}"]) == 0
        assert calls[1][1]["match_id"] == f"{HASH0}_{HASH1}"
    finally:
        restore()


def test_buy_from_dispenser_keeps_its_selector_asset_flag():
    # --asset on buy-from-dispenser is the WHICH-dispenser selector, not a
    # dual of a positional — it must still pass through as the keyword.
    calls, restore = _stub(M.dispenser, "cmd_buy_from_dispenser")
    try:
        rc = M.main(["wallet", "buy-from-dispenser", "--address", DEST,
                     "--amount", "1", "--asset", "PEPE"])
        assert rc == 0
        (_, _, address, amount), kw = calls[0]
        assert (address, amount, kw["asset"]) == (DEST, "1", "PEPE")
    finally:
        restore()


def test_every_tx_creating_command_accepts_fee_rate():
    # Anything that ends in a signed Bitcoin transaction must take --fee-rate
    # and hand it to its cmd_* as fee_rate.
    cases = [
        (M.inscribe, "cmd_inscribe", ["inscribe", "--file", "x.png"]),
        (M.send, "cmd_send", ["send", DEST, "BONPARTY", "1"]),
        (M.issue, "cmd_lock_supply", ["lock-supply", "BONPARTY"]),
        (M.issue, "cmd_lock_description", ["lock-description", "BONPARTY"]),
        (M.issue, "cmd_issue", ["issue", "BONPARTY", "10"]),
        (M.issue, "cmd_transfer_ownership", ["transfer-ownership", "BONPARTY", DEST]),
        (M.burn, "cmd_burn", ["burn", "BONPARTY", "10"]),
        (M.dispenser, "cmd_buy_from_dispenser", ["buy-from-dispenser", DEST, "1"]),
        (M.dispenser, "cmd_open_dispenser",
         ["open-dispenser", "PEPECASH", "100", "--price", "5000"]),
        (M.dispenser, "cmd_refill_dispenser", ["refill-dispenser", "PEPECASH", "100"]),
        (M.dispenser, "cmd_close_dispenser", ["close-dispenser", "PEPECASH"]),
        (M.order, "cmd_open_order", ["open-order", "XCP", "10", "BTC", "0.001"]),
        (M.order, "cmd_cancel_order", ["cancel-order", HASH0]),
        (M.order, "cmd_pay_order", ["pay-order"]),
        (M.bump, "cmd_bump", ["bump"]),
        (M.cancel, "cmd_cancel", ["cancel"]),
    ]
    for module, fn_name, argv in cases:
        calls, restore = _stub(module, fn_name)
        try:
            rc = M.main(["wallet"] + argv + ["--fee-rate", "0.5"])
            assert rc == 0, f"{argv[0]}: exit {rc}"
            assert calls and calls[0][1].get("fee_rate") == 0.5, \
                f"{argv[0]}: fee_rate not passed through ({calls})"
        finally:
            restore()


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
