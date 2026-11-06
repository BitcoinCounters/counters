"""`counters teleburn-address` and `counters wallet teleburn`.

Every Counterparty asset has one unspendable Bitcoin address, derived from
its name (counters/teleburn.py; build reference v3 §10.3). `teleburn-address`
prints it for an asset or a counter number — read-only, no wallet, and any
Counterparty asset, counter or not. `teleburn` burns a counter you own into
it, in the two Counterparty messages the state needs:

  1. a `send` of every circulating unit this wallet holds to the address.
     The destination travels in the OP_RETURN, so no sats are burned with
     it. Skipped when nothing circulates (an asset that never had supply, or
     whose supply is already there or destroyed).
  2. an `issuance` from the owner with `quantity=0`, `lock=true` (unless the
     supply is already locked) and `transfer_destination=<the address>`, no
     description. One OP_RETURN message, so rule R4 never sees a counter in
     it. Core puts 330 sats of transfer dust on the address; they are burned
     with it. Lock travels in the same message as the transfer because after
     the transfer nobody can lock.

Both are the ordinary messages `send` and `transfer-ownership` compose, with
the same checks and funding; this command only fixes the destination and
puts the two in order. The wallet must hold the asset's issuance rights (its
`owner`), like `lock-supply`. By default it must also hold every circulating
unit: a burn that leaves units with someone else is a partial state the
indexer shows as partial, and it is asked for explicitly with `--partial`.

IT IS IRREVERSIBLE in every part. The y/N prompt names everything that will
happen before anything is signed; `--dry-run` composes and signs both
transactions and broadcasts neither.
"""

from __future__ import annotations

import json
import sys

from .. import teleburn
from ..bitcoind import BitcoindClient
from ..config import Config
from ..counterparty import CounterpartyClient, CounterpartyError
from ..store import Store
from .funding import compose_retrying, ensure_funded
from .issue import _compose as _compose_issuance, _resolve_owned_asset
from .send import _confirm_prompt, _find_source, _fmt_raw, _sign_and_broadcast


def _counters_of(config: Config, identifier: str) -> tuple[str | None, list[int]]:
    """(asset, counter numbers) for a counter number, inscription id, asset
    name or longname that the index knows; (None, []) when it does not."""
    store = Store(config)
    try:
        row = store.find(identifier.strip().lstrip("#"))
        if row is None:
            return None, []
        return row["asset"], [r["number"] for r in store.get_counters_by_asset(row["asset"])]
    finally:
        store.close()


def _resolve(config: Config, cp, identifier: str) -> tuple[str, list[int]] | None:
    """The canonical asset name behind `identifier` — a counter number first,
    then the index's spelling, then Counterparty's (which turns a longname
    into its numeric asset) — with its counter numbers. None after printing
    why."""
    token = identifier.strip()
    asset, numbers = _counters_of(config, token)
    if asset is not None:
        return asset, numbers
    if token.lstrip("#").isdigit():
        print(f"no counter #{token.lstrip('#')} in the index", file=sys.stderr)
        return None
    info = None
    try:
        info = cp.get_asset(token) or cp.get_asset(token.upper())
    except CounterpartyError as e:
        print(f"cannot reach Counterparty: {e}", file=sys.stderr)
    if info and info.get("asset"):
        return info["asset"], []
    try:
        return teleburn.canonical_asset_name(token), []
    except ValueError as e:
        print(f"{e}", file=sys.stderr)
        return None


def cmd_teleburn_address(config: Config, identifier: str, network: str = "mainnet",
                         as_json: bool = False) -> int:
    """Print the teleburn address of an asset or counter. Read-only."""
    try:
        hrp = teleburn.hrp_for(network)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    resolved = _resolve(config, CounterpartyClient(config), identifier)
    if resolved is None:
        return 1
    asset, numbers = resolved
    try:
        address = teleburn.teleburn_address(asset, hrp)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    spk = teleburn.teleburn_spk(asset)
    if as_json:
        print(json.dumps({"asset": asset, "counters": numbers, "network": network,
                          "address": address, "scriptPubKey": spk.hex(),
                          "xonly": spk[2:].hex(), "tag": teleburn.TAG}, indent=2))
        return 0
    print(f"asset        : {asset}")
    if numbers:
        shown = ", ".join(f"#{n}" for n in numbers[:12])
        more = f", ... (+{len(numbers) - 12} more)" if len(numbers) > 12 else ""
        print(f"counters     : {shown}{more}")
    print(f"teleburn     : {address}")
    print(f"scriptPubKey : {spk.hex()}")
    print(f"derivation   : tagged_hash({teleburn.TAG!r}, {asset!r}) as a BIP341 tweak "
          f"of the NUMS point; nothing can spend from it")
    return 0


def cmd_teleburn(config: Config, wallet: str, identifier: str,
                 partial: bool = False, fee_rate: float | None = None,
                 assume_yes: bool = False, dry_run: bool = False,
                 fund_from: str | None = None, no_fund: bool = False) -> int:
    """Burn an asset this wallet owns into its teleburn address: send the
    circulating supply there, then lock and transfer ownership there."""
    btc = BitcoindClient(config)
    cp = CounterpartyClient(config)

    resolved = _resolve(config, cp, identifier)
    if resolved is None:
        return 1
    asset, numbers = resolved
    try:
        address = teleburn.teleburn_address(asset)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    info = cp.get_asset(asset)
    if not info:
        print(f"unknown asset {asset!r} (Counterparty has no record)", file=sys.stderr)
        return 1
    if teleburn.is_teleburn_address(info.get("owner") or "", asset):
        print(f"{asset} is already teleburned: its owner is {address}")
        return 0

    owned = _resolve_owned_asset(btc, cp, wallet, asset)
    if owned is None:
        return 1
    asset, info, owner = owned
    divisible = bool(info.get("divisible"))
    locked = bool(info.get("locked"))
    supply = int(info.get("supply") or 0)

    try:
        sink = cp.get_address_balance(address, asset)
    except CounterpartyError as e:
        print(f"cannot read the balance at {address}: {e}", file=sys.stderr)
        return 1
    circulating = max(supply - sink, 0)

    source, have = (None, 0)
    send_raw = 0
    if circulating > 0:
        source, have = _find_source(btc, cp, wallet, asset, circulating)
        have = max(have, 0)
        if have < circulating:
            held = _fmt_raw(have, divisible)
            print(f"{_fmt_raw(circulating, divisible)} {asset} circulates outside "
                  f"{address}, and this wallet's largest single-address balance is "
                  f"{held}. A burn that leaves units with someone else is partial: "
                  f"the indexer will show ownership burned and supply not.",
                  file=sys.stderr)
            if not partial:
                print("pass --partial to burn ownership and what this wallet holds anyway",
                      file=sys.stderr)
                return 1
        send_raw = min(have, circulating)

    tag = f" (counter{'s' if len(numbers) > 1 else ''} "
    tag += ", ".join(f"#{n}" for n in numbers[:6]) + ")" if numbers else ""
    print(f"teleburn {asset}{tag if numbers else ''}")
    print(f"  into      : {address}")
    print(f"  owner     : {owner}")
    if supply:
        print(f"  supply    : {_fmt_raw(supply, divisible)} — "
              f"{_fmt_raw(sink, divisible)} already there, "
              f"{_fmt_raw(circulating, divisible)} circulating")
    else:
        print(f"  supply    : none")
    step = 1
    if send_raw:
        print(f"  step {step}    : send {_fmt_raw(send_raw, divisible)} {asset} "
              f"from {source} to the address")
        step += 1
        if send_raw < circulating:
            print(f"            ({_fmt_raw(circulating - send_raw, divisible)} stays with "
                  f"other holders — PARTIAL)")
    print(f"  step {step}    : issuance from the owner — "
          f"{'lock the supply and ' if not locked else ''}transfer ownership to the address")
    if fee_rate is not None:
        print(f"  fee rate  : {fee_rate} sat/vB")
    print(f"  WARNING   : nothing can ever spend from the address, and once ownership "
          f"is there nobody can issue, describe or reinscribe {asset} again")

    if not (dry_run or assume_yes or _confirm_prompt(f"\nteleburn {asset} FOREVER?")):
        print("aborted", file=sys.stderr)
        return 1

    if send_raw:
        fund = ensure_funded(btc, cp, wallet, source, fee_rate=fee_rate,
                             fund_from=fund_from, no_fund=no_fund, dry_run=dry_run,
                             outputs=1)
        if fund.code is not None:
            return fund.code
        try:
            composed = compose_retrying(lambda: cp.compose_send(
                source, asset, send_raw, address, sat_per_vbyte=fee_rate), fund.funded)
        except CounterpartyError as e:
            print(f"compose send failed: {e}", file=sys.stderr)
            return 1
        rawtx = composed.get("rawtransaction")
        if not rawtx:
            print(f"compose returned no rawtransaction: {composed}", file=sys.stderr)
            return 1
        print(f"\nsend {_fmt_raw(send_raw, divisible)} {asset} -> {address}")
        rc = _sign_and_broadcast(btc, wallet, source, rawtx, dry_run)
        if rc != 0:
            print("stopping before the ownership transfer; the supply is unchanged. "
                  "Run the command again to retry.", file=sys.stderr)
            return rc

    fund = ensure_funded(btc, cp, wallet, owner, fee_rate=fee_rate,
                         fund_from=fund_from, no_fund=no_fund, dry_run=dry_run)
    if fund.code is not None:
        return fund.code
    rawtx = _compose_issuance(cp, owner, asset, quantity=0, divisible=divisible,
                              lock=not locked, description=None,
                              transfer_destination=address, fee_rate=fee_rate,
                              funded=fund.funded)
    if rawtx is None:
        return 1
    print(f"\n{'lock + ' if not locked else ''}transfer-ownership {asset} -> {address}")
    rc = _sign_and_broadcast(btc, wallet, owner, rawtx, dry_run)
    if rc == 0 and not dry_run:
        print(f"\nteleburned {asset}: it will show as burned once both transactions confirm")
    return rc
