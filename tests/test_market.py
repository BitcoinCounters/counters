"""Unit tests for the interactive `sell`, `buy` and `dispense` (no network).

Each command is a scripted conversation that ends in a call to the existing
command (order.cmd_open_order / dispenser.cmd_open_dispenser), which owns the summary, the y/N
and the broadcast. So the things to pin are the conversation itself: what
the menu shows, which answers are accepted, how the defaults are derived,
how a price per unit becomes an order's get side, how a BTC decimal becomes
sats, and that quitting or losing the terminal never reaches the hand-off.
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import counters.__main__ as M  # noqa: E402
import counters.commands.market as K  # noqa: E402
from counters.config import Config  # noqa: E402

A = "bc1pAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
B = "bc1pBxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"


class FakeCp:
    """Balances per address; get_asset for the get side of an order."""

    def __init__(self, balances, assets=None):
        self.balances = balances
        self.assets = assets or {"XCP": {"asset": "XCP", "divisible": True}}

    def get_address_balances(self, address):
        return self.balances.get(address, [])

    def get_asset(self, name):
        return self.assets.get(name)


class FakeStore:
    def __init__(self, numbers):
        self.numbers = numbers

    def get_counter_by_asset(self, asset):
        n = self.numbers.get(asset)
        return None if n is None else {"number": n}

    def close(self):
        pass


def _bal(asset, qty, divisible=False, longname=None):
    return {"asset": asset, "quantity": qty, "asset_longname": longname,
            "asset_info": {"divisible": divisible}}


class Session:
    """Install fakes and a scripted stdin; record the hand-off call."""

    def __init__(self, monkeypatch, answers, balances, numbers, tty=True, assets=None):
        self.answers = list(answers)
        self.handoff = None
        self.out = []
        monkeypatch.setattr(K, "_tty", lambda: tty)
        monkeypatch.setattr(K, "_input", self._input)
        monkeypatch.setattr(K, "BitcoindClient", lambda config: object())
        monkeypatch.setattr(K, "CounterpartyClient", lambda config: FakeCp(balances, assets))
        monkeypatch.setattr(K, "Store", lambda config: FakeStore(numbers))
        monkeypatch.setattr(K, "_wallet_addresses", lambda btc, w: sorted(balances))
        monkeypatch.setattr(K.order, "cmd_open_order", self._record("order"))
        monkeypatch.setattr(K.dispenser, "cmd_open_dispenser", self._record("dispenser"))

    def _input(self, prompt):
        self.out.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)

    def _record(self, kind):
        def fn(*args, **kwargs):
            self.handoff = (kind, args, kwargs)
            return 0
        return fn


ONE_COUNTER = {A: [_bal("A987654321098765432", 2), _bal("XCP", 350_000_000, True)]}
NUMBERS = {"A987654321098765432": 18}


# --- list ---------------------------------------------------------------------


def test_sell_prices_per_unit_and_hands_the_total_to_open_order(monkeypatch, capsys):
    s = Session(monkeypatch, ["1", "", "", "0.001", ""], ONE_COUNTER, NUMBERS)
    rc = K.cmd_sell(Config(), "w")
    assert rc == 0
    kind, args, kw = s.handoff
    assert kind == "order"
    # (config, wallet, give_asset, give_amount, get_asset, get_amount)
    assert args[1:] == ("w", "A987654321098765432", "2", "BTC", "0.002")
    assert kw["expiration"] == 0
    assert kw["dry_run"] is False
    text = capsys.readouterr().out
    assert "#18" in text and "A987654321098765432" in text        # the menu
    assert "total: 0.002 BTC for 2 A987654321098765432" in text   # the arithmetic


def test_sell_menu_puts_counters_first_then_other_assets(monkeypatch, capsys):
    balances = {A: [_bal("XCP", 10**8, True), _bal("A5", 1), _bal("A9", 3)]}
    Session(monkeypatch, ["q"], balances, {"A9": 30, "A5": 51})
    assert K.cmd_sell(Config(), "w") == 0
    out = capsys.readouterr().out
    rows = [re.match(r"\s*\[\s*\d+\]\s+(#\d+)?\s*(\S+)", l)
            for l in out.splitlines() if l.strip().startswith("[")]
    assert [(m.group(1), m.group(2)) for m in rows] == [
        ("#30", "A9"), ("#51", "A5"), (None, "XCP")]
    assert "nothing sold" in out


def test_sell_default_amount_is_the_largest_single_address_balance(monkeypatch, capsys):
    balances = {A: [_bal("A1", 3)], B: [_bal("A1", 5)]}
    s = Session(monkeypatch, ["1", "", "", "1", ""], balances, {"A1": 7})
    K.cmd_sell(Config(), "w")
    assert s.handoff[1][3] == "5"                       # not the 8 held wallet-wide
    assert "(5 on one address)" in capsys.readouterr().out


def test_sell_re_asks_an_amount_above_one_address(monkeypatch, capsys):
    balances = {A: [_bal("A1", 3)], B: [_bal("A1", 5)]}
    s = Session(monkeypatch, ["1", "8", "4", "", "1", ""], balances, {"A1": 7})
    K.cmd_sell(Config(), "w")
    assert s.handoff[1][3] == "4"
    assert "only 5 A1 is held on one address" in capsys.readouterr().out


def test_sell_re_asks_a_btc_total_under_the_consensus_minimum(monkeypatch, capsys):
    s = Session(monkeypatch, ["1", "1", "", "0.000005", "0.00001", ""], ONE_COUNTER, NUMBERS)
    K.cmd_sell(Config(), "w")
    assert s.handoff[1][5] == "0.00001"
    assert "500 sat in total" in capsys.readouterr().out


def test_sell_re_asks_a_fractional_total_in_an_indivisible_asset(monkeypatch, capsys):
    assets = {"XCP": {"asset": "XCP", "divisible": True},
              "PEPE": {"asset": "PEPE", "divisible": False}}
    # 2 units at 1.25 PEPE is 2.5 PEPE in total, and PEPE has no halves.
    s = Session(monkeypatch, ["1", "", "PEPE", "1.25", "3", ""], ONE_COUNTER, NUMBERS,
                assets=assets)
    K.cmd_sell(Config(), "w")
    assert s.handoff[1][4:] == ("PEPE", "6")
    assert "PEPE is indivisible" in capsys.readouterr().out


def test_sell_refuses_an_unknown_get_asset_and_itself(monkeypatch, capsys):
    s = Session(monkeypatch, ["1", "", "NOPE", "A987654321098765432", "XCP", "2", ""],
                ONE_COUNTER, NUMBERS)
    K.cmd_sell(Config(), "w")
    assert s.handoff[1][4:] == ("XCP", "4")
    out = capsys.readouterr()
    assert "unknown asset 'NOPE'" in out.err
    assert "cannot trade A987654321098765432 for itself" in out.out


def test_sell_passes_expiration_and_flags_through(monkeypatch):
    s = Session(monkeypatch, ["1", "", "", "0.01", "8064"], ONE_COUNTER, NUMBERS)
    K.cmd_sell(Config(), "w", fee_rate=3.0, dry_run=True, fund_from=B, no_fund=False,
               fee_required=200, source=A, assume_yes=True)
    kw = s.handoff[2]
    assert kw == {"expiration": 8064, "fee_required": 200, "source": A, "fee_rate": 3.0,
                  "assume_yes": True, "dry_run": True, "fund_from": B, "no_fund": False}


def test_sell_expiration_flag_is_the_interactive_default(monkeypatch):
    s = Session(monkeypatch, ["1", "", "", "0.01", ""], ONE_COUNTER, NUMBERS)
    K.cmd_sell(Config(), "w", expiration=144)
    assert s.handoff[2]["expiration"] == 144


def test_sell_with_the_asset_named_skips_the_menu(monkeypatch, capsys):
    s = Session(monkeypatch, ["", "", "0.01", ""], ONE_COUNTER, NUMBERS)
    assert K.cmd_sell(Config(), "w", asset="a987654321098765432") == 0
    assert s.handoff[1][2:4] == ("A987654321098765432", "2")
    out = capsys.readouterr().out
    assert "sell which?" not in out and "selling A987654321098765432 (counter #18)" in out


def test_sell_accepts_a_counter_number_for_the_asset(monkeypatch):
    s = Session(monkeypatch, ["1", "", "0.01", ""], ONE_COUNTER, NUMBERS)
    assert K.cmd_sell(Config(), "w", asset="#18") == 0
    assert s.handoff[1][2] == "A987654321098765432"


def test_sell_of_an_asset_not_held_is_an_error(monkeypatch, capsys):
    s = Session(monkeypatch, [], ONE_COUNTER, NUMBERS)
    assert K.cmd_sell(Config(), "w", asset="PEPECASH") == 1
    assert s.handoff is None
    assert "holds no PEPECASH" in capsys.readouterr().err


def test_sell_quits_on_q_without_handing_off(monkeypatch, capsys):
    s = Session(monkeypatch, ["1", "q"], ONE_COUNTER, NUMBERS)
    assert K.cmd_sell(Config(), "w") == 0
    assert s.handoff is None
    assert "nothing sold" in capsys.readouterr().out


def test_sell_quits_on_eof(monkeypatch):
    s = Session(monkeypatch, ["1"], ONE_COUNTER, NUMBERS)
    assert K.cmd_sell(Config(), "w") == 0
    assert s.handoff is None


def test_sell_refuses_without_a_terminal_and_names_the_flag_form(monkeypatch, capsys):
    s = Session(monkeypatch, [], ONE_COUNTER, NUMBERS, tty=False)
    assert K.cmd_sell(Config(), "w") == 1
    assert s.handoff is None
    assert "counters wallet sell <ASSET>" in capsys.readouterr().err


def test_sell_with_nothing_held_says_so(monkeypatch, capsys):
    Session(monkeypatch, [], {A: []}, {})
    assert K.cmd_sell(Config(), "w") == 0
    assert "holds no Counterparty assets" in capsys.readouterr().out


# --- dispense -----------------------------------------------------------------


def test_dispense_defaults_to_all_held_in_lots_of_one(monkeypatch, capsys):
    s = Session(monkeypatch, ["1", "", "", "5000"], ONE_COUNTER, NUMBERS)
    assert K.cmd_dispense(Config(), "w") == 0
    kind, args, kw = s.handoff
    assert kind == "dispenser"
    # (config, wallet, asset, amount, price)
    assert args[1:] == ("w", "A987654321098765432", "2", 5000)
    assert kw["lot"] == "1"
    assert "2 lots at 5000 sat: 0.0001 BTC if sold out" in capsys.readouterr().out


def test_dispense_reads_a_decimal_price_as_btc(monkeypatch, capsys):
    s = Session(monkeypatch, ["1", "", "", "0.0005"], ONE_COUNTER, NUMBERS)
    K.cmd_dispense(Config(), "w")
    assert s.handoff[1][4] == 50_000
    assert "read as 50000 sat (0.0005 BTC)" in capsys.readouterr().out


def test_dispense_lot_default_is_the_whole_escrow_below_one_unit(monkeypatch):
    balances = {A: [_bal("PEPECASH", 40_000_000, divisible=True)]}   # 0.4
    s = Session(monkeypatch, ["1", "", "", "100"], balances, {})
    K.cmd_dispense(Config(), "w")
    assert s.handoff[1][3] == "0.4" and s.handoff[2]["lot"] == "0.4"


def test_dispense_notes_a_part_lot_remainder(monkeypatch, capsys):
    balances = {A: [_bal("A1", 7)]}
    s = Session(monkeypatch, ["1", "", "3", "1000"], balances, {"A1": 1})
    K.cmd_dispense(Config(), "w")
    assert s.handoff[2]["lot"] == "3"
    out = capsys.readouterr().out
    assert "1 A1 would sit unvendable" in out
    assert "2 lots at 1000 sat" in out


def test_dispense_re_asks_a_lot_larger_than_the_escrow(monkeypatch, capsys):
    s = Session(monkeypatch, ["1", "", "5", "2", "100"], ONE_COUNTER, NUMBERS)
    K.cmd_dispense(Config(), "w")
    assert s.handoff[2]["lot"] == "2"
    assert "only 2 A987654321098765432 is held" in capsys.readouterr().out


def test_dispense_re_asks_a_sub_satoshi_btc_price(monkeypatch, capsys):
    s = Session(monkeypatch, ["1", "", "", "0.000000001", "150"], ONE_COUNTER, NUMBERS)
    K.cmd_dispense(Config(), "w")
    assert s.handoff[1][4] == 150
    assert "finer than one satoshi" in capsys.readouterr().out


def test_dispense_with_the_asset_named_skips_the_menu_and_passes_flags(monkeypatch, capsys):
    s = Session(monkeypatch, ["", "", "5000"], ONE_COUNTER, NUMBERS)
    assert K.cmd_dispense(Config(), "w", asset="18", source=A, fee_rate=1.5,
                          assume_yes=True, dry_run=True) == 0
    kind, args, kw = s.handoff
    assert args[1:] == ("w", "A987654321098765432", "2", 5000)
    assert kw == {"lot": "1", "source": A, "fee_rate": 1.5, "assume_yes": True,
                  "dry_run": True, "fund_from": None, "no_fund": False}
    assert "dispense which?" not in capsys.readouterr().out


def test_dispense_quits_on_q(monkeypatch, capsys):
    s = Session(monkeypatch, ["q"], ONE_COUNTER, NUMBERS)
    assert K.cmd_dispense(Config(), "w") == 0
    assert s.handoff is None
    assert "no dispenser opened" in capsys.readouterr().out


def test_dispense_refuses_without_a_terminal(monkeypatch, capsys):
    s = Session(monkeypatch, [], ONE_COUNTER, NUMBERS, tty=False)
    assert K.cmd_dispense(Config(), "w") == 1
    assert s.handoff is None
    assert "dispense <ASSET> <AMOUNT> --price SATS" in capsys.readouterr().err


# --- buy ----------------------------------------------------------------------


class BuyCp(FakeCp):
    def __init__(self, assets, orders=()):
        super().__init__({}, assets)
        self.orders = list(orders)

    def get_asset_orders(self, asset, limit=50):
        return [o for o in self.orders if o["give_asset"] == asset], len(self.orders)


def _ask_row(asset, give_left, get_asset, get_left, status="open"):
    return {"give_asset": asset, "give_remaining": give_left, "get_asset": get_asset,
            "get_remaining": get_left, "status": status}


ASSETS = {"XCP": {"asset": "XCP", "divisible": True},
          "SSETONET": {"asset": "SSETONET", "divisible": False},
          "A987654321098765432": {"asset": "A987654321098765432", "divisible": False}}


class BuySession(Session):
    def __init__(self, monkeypatch, answers, orders=(), numbers=None, tty=True):
        super().__init__(monkeypatch, answers, {}, numbers or {"SSETONET": 26}, tty=tty)
        monkeypatch.setattr(K, "CounterpartyClient", lambda config: BuyCp(ASSETS, orders))
        store = FakeStore(numbers or {"SSETONET": 26})
        store.get_counter = lambda n: ({"asset": "SSETONET", "asset_longname": None}
                                       if n == 26 else None)
        monkeypatch.setattr(K, "Store", lambda config: store)


def test_buy_names_the_asset_and_hands_a_swapped_order_to_open_order(monkeypatch, capsys):
    s = BuySession(monkeypatch, ["SSETONET", "", "", "0.002", ""])
    assert K.cmd_buy(Config(), "w") == 0
    kind, args, kw = s.handoff
    assert kind == "order"
    # give BTC, get the asset: (config, wallet, give_asset, give_amount, get_asset, get_amount)
    assert args[1:] == ("w", "BTC", "0.002", "SSETONET", "1")
    out = capsys.readouterr().out
    assert "buying SSETONET (counter #26)" in out
    assert "total: 0.002 BTC pays for 1 SSETONET" in out


def test_buy_takes_a_counter_number(monkeypatch, capsys):
    s = BuySession(monkeypatch, ["3", "XCP", "2.5", ""])
    assert K.cmd_buy(Config(), "w", asset="#26") == 0
    assert s.handoff[1][2:] == ("XCP", "7.5", "SSETONET", "3")
    assert "buy which asset" not in capsys.readouterr().out


def test_buy_shows_the_cheapest_resting_ask(monkeypatch, capsys):
    orders = [_ask_row("SSETONET", 1, "BTC", 300_000),
              _ask_row("SSETONET", 2, "BTC", 400_000),          # 0.002 each: cheapest
              _ask_row("SSETONET", 1, "BTC", 100_000, status="filled"),
              _ask_row("SSETONET", 1, "XCP", 10**8)]           # other pay asset
    BuySession(monkeypatch, ["SSETONET", "", "", "0.002", ""], orders)
    K.cmd_buy(Config(), "w")
    assert "cheapest resting ask: 0.002 BTC per unit (2 for sale)" in capsys.readouterr().out


def test_buy_re_asks_an_unknown_asset_and_refuses_btc(monkeypatch, capsys):
    s = BuySession(monkeypatch, ["NOPE", "BTC", "99", "SSETONET", "", "", "0.01", ""])
    K.cmd_buy(Config(), "w")
    assert s.handoff[1][4] == "SSETONET"
    out = capsys.readouterr()
    assert "unknown asset 'NOPE'" in out.err
    assert "BTC is what you pay with" in out.out
    assert "no counter #99" in out.out


def test_buy_with_an_unknown_named_asset_is_an_error(monkeypatch):
    s = BuySession(monkeypatch, [])
    assert K.cmd_buy(Config(), "w", asset="NOPE") == 1
    assert s.handoff is None


def test_buy_quits_and_refuses_without_a_terminal(monkeypatch, capsys):
    s = BuySession(monkeypatch, ["q"])
    assert K.cmd_buy(Config(), "w") == 0 and s.handoff is None
    assert "nothing bought" in capsys.readouterr().out
    s = BuySession(monkeypatch, [], tty=False)
    assert K.cmd_buy(Config(), "w") == 1 and s.handoff is None
    assert "counters wallet buy <ASSET> <AMOUNT> <PAY_ASSET> <PAY_AMOUNT>" in capsys.readouterr().err


# --- CLI wiring -----------------------------------------------------------------


def test_cli_dispatches_sell_and_dispense_with_their_flags(monkeypatch):
    calls = []

    def rec(name):
        def fn(*a, **kw):
            calls.append((name, a, kw))
            return 0
        return fn

    monkeypatch.setattr(M.market, "cmd_sell", rec("sell"))
    monkeypatch.setattr(M.market, "cmd_dispense", rec("dispense"))
    assert M.main(["wallet", "--name", "w", "sell", "--fee-rate", "2", "--dry-run"]) == 0
    assert M.main(["wallet", "--name", "w", "dispense", "--no-fund"]) == 0
    assert calls[0][0] == "sell" and calls[0][1][1] == "w"
    assert calls[0][2] == {"asset": None, "expiration": 0, "fee_required": 0, "source": None,
                           "fee_rate": 2.0, "assume_yes": False, "dry_run": True,
                           "fund_from": None, "no_fund": False}
    assert calls[1][0] == "dispense"
    assert calls[1][2] == {"asset": None, "source": None, "fee_rate": None,
                           "assume_yes": False, "dry_run": False, "fund_from": None,
                           "no_fund": True}
