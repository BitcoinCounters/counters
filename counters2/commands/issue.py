"""`counters wallet lock` and `counters wallet issue` — Counterparty asset ops.

Both are plain Counterparty *issuance* messages: Counterparty Core composes the
OP_RETURN, the Bitcoin Core wallet (which holds the keys) signs it, we validate
against the mempool, then broadcast. Custody stays in Core — we never touch keys.

An issuance can only be made by the asset's current OWNER (the issuance-rights
holder, which moves on transfer), so both commands source the transaction from
the owner address, which must be in this wallet and hold a little BTC for the
fee.

  lock-supply       ASSET  -> freeze the supply (no future issuance changes it)
  lock-description  ASSET  -> freeze the description (the image/metadata ref)
  issue ASSET QUANTITY     -> mint additional supply of an existing asset
                             (--lock to lock the supply in the same transaction)

Counterparty has two independent locks. A SUPPLY lock (issuance `lock=true`)
stops further minting; it does NOT freeze the description. A DESCRIPTION lock is
a separate flag, set by issuing the literal description "LOCK_DESCRIPTION", which
keeps the current description and forbids any future change to it. Tokenscan-style
explorers render an asset's image from its description, so lock-description is
what pins the artwork/metadata reference in place.
"""

from __future__ import annotations

import sys

from ..bitcoind import BitcoindClient, BitcoindError
from ..config import Config, RESERVED_ASSETS
from ..counterparty import CounterpartyClient, CounterpartyError
from .send import _fmt_raw, _sign_and_broadcast, _to_raw_quantity
from .wallet import _wallet_addresses


def _resolve_owned_asset(btc, cp, wallet: str, asset: str):
    """Resolve `asset` to (canonical_name, asset_info, owner) when this wallet
    holds its issuance rights. Prints the reason and returns None otherwise."""
    if asset in RESERVED_ASSETS:
        print(f"{asset} is a reserved asset, not an issuable counter", file=sys.stderr)
        return None
    info = cp.get_asset(asset) or cp.get_asset(asset.upper())
    if not info:
        print(f"unknown asset {asset!r} (Counterparty has no record)", file=sys.stderr)
        return None
    canonical = info.get("asset") or asset
    # Ownership (issuance rights) moves on transfer; `issuer` is the original,
    # immutable creator. Use `owner`, falling back to `issuer` for never-
    # transferred assets.
    owner = info.get("owner") or info.get("issuer")
    if not owner:
        print(f"could not determine the issuance-rights owner of {canonical}", file=sys.stderr)
        return None
    if owner not in set(_wallet_addresses(btc, wallet)):
        print(f"wallet {wallet!r} does not hold the issuance rights of {canonical} "
              f"(owner {owner}); only the owner can lock or reissue it.", file=sys.stderr)
        return None
    return canonical, info, owner


def _compose(cp, owner: str, asset: str, quantity: int, divisible: bool,
             lock: bool, description) -> str | None:
    """Compose the issuance from the owner address; return its unsigned raw tx,
    or None after printing the failure (with a funding hint when relevant)."""
    try:
        composed = cp.compose_issuance(
            source=owner, asset=asset, quantity=quantity, divisible=divisible,
            description=description, lock=lock,
        )
    except CounterpartyError as e:
        msg = str(e)
        print(f"compose failed: {msg}", file=sys.stderr)
        if "No UTXOs" in msg or "inputs_set" in msg:
            print(f"hint: {owner} owns {asset} but has no spendable BTC. The issuance is "
                  f"sourced from the owner address, so fund it with a little BTC (for the "
                  f"tx fee), then retry.", file=sys.stderr)
        return None
    rawtx = composed.get("rawtransaction")
    if not rawtx:
        print(f"compose returned no rawtransaction: {composed}", file=sys.stderr)
        return None
    return rawtx


def cmd_lock_supply(config: Config, wallet: str, asset: str, dry_run: bool = False) -> int:
    btc = BitcoindClient(config)
    cp = CounterpartyClient(config)

    resolved = _resolve_owned_asset(btc, cp, wallet, asset)
    if resolved is None:
        return 1
    asset, info, owner = resolved

    if info.get("locked"):
        print(f"{asset} supply is already locked", file=sys.stderr)
        return 1

    divisible = bool(info.get("divisible"))
    # A supply lock is a zero-quantity issuance with lock=true. The description
    # MUST be omitted (None): under v3 it is the counter's file content, and
    # re-sending it in an OP_RETURN issuance would fail for large content or
    # rewrite the stored content's MIME classification. Omitted, Counterparty
    # preserves it.
    rawtx = _compose(cp, owner, asset, quantity=0, divisible=divisible,
                     lock=True, description=None)
    if rawtx is None:
        return 1

    print(f"lock-supply {asset}")
    print(f"  owner     : {owner}")
    return _sign_and_broadcast(btc, wallet, owner, rawtx, dry_run)


def cmd_lock_description(config: Config, wallet: str, asset: str, dry_run: bool = False) -> int:
    btc = BitcoindClient(config)
    cp = CounterpartyClient(config)

    resolved = _resolve_owned_asset(btc, cp, wallet, asset)
    if resolved is None:
        return 1
    asset, info, owner = resolved

    # A description lock is a zero-quantity issuance whose description is the
    # literal "LOCK_DESCRIPTION": Counterparty keeps the CURRENT description and
    # sets description_locked, so the image/metadata reference can never change.
    # (The asset API doesn't expose description_locked, so a double-lock is left
    # for Counterparty to reject — "Cannot update a locked description".)
    divisible = bool(info.get("divisible"))
    rawtx = _compose(cp, owner, asset, quantity=0, divisible=divisible,
                     lock=False, description="LOCK_DESCRIPTION")
    if rawtx is None:
        return 1

    print(f"lock-description {asset}")
    print(f"  owner     : {owner}")
    print(f"  freezing  : {info.get('description') or '(empty description)'}")
    return _sign_and_broadcast(btc, wallet, owner, rawtx, dry_run)


def cmd_issue(config: Config, wallet: str, asset: str, amount: str,
              lock: bool = False, dry_run: bool = False) -> int:
    btc = BitcoindClient(config)
    cp = CounterpartyClient(config)

    resolved = _resolve_owned_asset(btc, cp, wallet, asset)
    if resolved is None:
        return 1
    asset, info, owner = resolved

    if info.get("locked"):
        print(f"{asset} supply is locked; no further issuance is possible", file=sys.stderr)
        return 1

    # Divisibility is fixed at creation and cannot change on reissue, so the
    # quantity is interpreted with the asset's existing divisibility.
    divisible = bool(info.get("divisible"))
    try:
        raw = _to_raw_quantity(amount, divisible)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    # description omitted (None): preserve the asset's content — see lock-supply.
    rawtx = _compose(cp, owner, asset, quantity=raw, divisible=divisible,
                     lock=lock, description=None)
    if rawtx is None:
        return 1

    print(f"issue +{_fmt_raw(raw, divisible)} {asset}{' (and LOCK)' if lock else ''}")
    print(f"  owner     : {owner}")
    return _sign_and_broadcast(btc, wallet, owner, rawtx, dry_run)
