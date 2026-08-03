"""`counters wallet burn` — permanently destroy a quantity of an asset.

Counterparty has two things called "burn", and only one of them is alive:

  - The PROOF-OF-BURN (`compose/burn`): January 2014's genesis event, where
    sending BTC to the unspendable 1CounterpartyXXXXXXXXXXXXXXXUWLpVr minted
    XCP. On mainnet it was consensus-valid only between blocks 278,310 and
    283,810; composing one today is pointless, so no command exposes it.

  - The DESTROY message (`compose/destroy`): the live primitive, and what this
    command composes. An OP_RETURN instruction that removes `quantity` of
    `asset` from the sender's own balance — supply drops, and the event is
    recorded in Counterparty's destructions table (auditable via
    /v2/assets/<asset>/destructions). An optional TAG (a short free-text note,
    e.g. a reason) travels in the message; Counterparty requires the field, so
    we always send it and default it to empty.

A destroy beats the folk method (sending tokens to a burn address) because it
is protocol-native: explorers show it as a destruction rather than a balance
parked at an address, and it cannot be faked with a spendable lookalike.

Like a send, a destroy is debited from a single address, so the burn is
sourced from one wallet address holding enough of the asset. IT IS
IRREVERSIBLE — hence the y/N prompt (--yes to skip).

BTC cannot be burned this way (it is not a Counterparty asset). XCP can:
destroying XCP is an ordinary, legitimate use of the message.
"""

from __future__ import annotations

import sys

from ..bitcoind import BitcoindClient
from ..config import Config
from ..counterparty import CounterpartyClient, CounterpartyError
from .funding import compose_retrying, ensure_funded
from .send import (
    _confirm_prompt,
    _fmt_raw,
    _sign_and_broadcast,
    _to_raw_quantity,
)
from .wallet import _wallet_addresses


def _source_balance(cp, address: str, asset: str) -> int:
    """Raw balance of `asset` held at `address` (0 when absent)."""
    try:
        rows = cp.get_address_balances(address)
    except CounterpartyError:
        return 0
    for r in rows:
        if r.get("asset") == asset or r.get("asset_longname") == asset:
            return int(r.get("quantity") or 0)
    return 0


def _find_source(btc, cp, wallet: str, asset: str, need_raw: int):
    """A wallet address holding the asset (a destroy is per-address). Returns
    the first address with >= need_raw; otherwise the richest address found.
    Result is (address_or_None, raw_balance)."""
    best = (None, 0)
    for addr in _wallet_addresses(btc, wallet):
        q = _source_balance(cp, addr, asset)
        if q >= need_raw:
            return addr, q
        if q > best[1]:
            best = (addr, q)
    return best


def cmd_burn(
    config: Config,
    wallet: str,
    asset: str,
    amount: str,
    tag: str = "",
    source: str | None = None,
    fee_rate: float | None = None,
    assume_yes: bool = False,
    dry_run: bool = False,
    fund_from: str | None = None,
    no_fund: bool = False,
) -> int:
    btc = BitcoindClient(config)
    cp = CounterpartyClient(config)

    if asset.upper() == "BTC":
        print("BTC is not a Counterparty asset and cannot be destroyed this way",
              file=sys.stderr)
        return 1

    info = cp.get_asset(asset) or cp.get_asset(asset.upper())
    if not info:
        print(f"unknown asset {asset!r} (Counterparty has no record)", file=sys.stderr)
        return 1
    asset = info.get("asset") or asset          # canonical name Core expects
    divisible = bool(info.get("divisible"))

    try:
        need = _to_raw_quantity(amount, divisible)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if source is not None:
        if source not in set(_wallet_addresses(btc, wallet)):
            print(f"--source {source} is not an address of wallet {wallet!r}",
                  file=sys.stderr)
            return 1
        have = _source_balance(cp, source, asset)
    else:
        source, have = _find_source(btc, cp, wallet, asset, need)
    if source is None or have <= 0:
        print(f"wallet {wallet!r} holds no {asset}", file=sys.stderr)
        return 1
    if have < need:
        print(
            f"insufficient balance: burning {_fmt_raw(need, divisible)} {asset}, "
            f"but {source} holds {_fmt_raw(have, divisible)} "
            f"(a destroy is debited from a single address)",
            file=sys.stderr,
        )
        return 1

    print(f"burn {_fmt_raw(need, divisible)} {asset}")
    print(f"  from      : {source}")
    print(f"  remaining : {_fmt_raw(have - need, divisible)} {asset} at this address")
    if tag:
        print(f"  tag       : {tag}")
    if fee_rate is not None:
        print(f"  fee rate  : {fee_rate} sat/vB")
    print(f"  WARNING   : a destroy is irreversible — the tokens are gone forever")

    if not (dry_run or assume_yes or _confirm_prompt(
            f"\nburn {_fmt_raw(need, divisible)} {asset} FOREVER?")):
        print("aborted", file=sys.stderr)
        return 1

    fund = ensure_funded(btc, cp, wallet, source, fee_rate=fee_rate,
                         fund_from=fund_from, no_fund=no_fund, dry_run=dry_run)
    if fund.code is not None:
        return fund.code

    try:
        composed = compose_retrying(lambda: cp.compose_destroy(
            source, asset, need, tag=tag, sat_per_vbyte=fee_rate), fund.funded)
    except CounterpartyError as e:
        msg = str(e)
        print(f"compose failed: {msg}", file=sys.stderr)
        if "No UTXOs" in msg or "inputs_set" in msg or "Insufficient funds" in msg:
            print(f"hint: {source} holds {asset} but has no spendable BTC. A destroy "
                  f"is sourced from the asset-holding address, so it pays its own "
                  f"fee. Drop --no-fund to top it up automatically.", file=sys.stderr)
        return 1
    rawtx = composed.get("rawtransaction")
    if not rawtx:
        print(f"compose returned no rawtransaction: {composed}", file=sys.stderr)
        return 1

    return _sign_and_broadcast(btc, wallet, source, rawtx, dry_run)
