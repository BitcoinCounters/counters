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
  describe ASSET TEXT      -> set the description to text (or to a file's text)
  issue ASSET QUANTITY     -> mint additional supply of an existing asset
                             (--lock to lock the supply in the same transaction)
  transfer-ownership ASSET ADDRESS
                           -> hand the issuance rights to another address

Counterparty splits what English calls "owning" a counter in two. The TOKENS
(the asset balance) travel by `send`; the ISSUANCE RIGHTS — the power to
reissue, lock, and reinscribe — travel by `transfer-ownership`. Moving one
does not move the other.

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
from ..content import classify_mime_type
from ..counterparty import CounterpartyClient, CounterpartyError
from .funding import compose_retrying, ensure_funded
from .send import (
    _confirm_prompt,
    _fmt_raw,
    _is_valid_address,
    _sign_and_broadcast,
    _to_raw_quantity,
)
from .wallet import _wallet_addresses

_ORDER_HINT = "note: the argument order is  transfer-ownership <ASSET> <ADDRESS>"


def _resolve_owned_asset(btc, cp, wallet: str, asset: str):
    """Resolve `asset` to (canonical_name, asset_info, owner) when this wallet
    holds its issuance rights. Prints the reason and returns None otherwise."""
    if asset.upper() in RESERVED_ASSETS:
        print(f"{asset.upper()} is a reserved asset, not an issuable "
              "Counterparty asset", file=sys.stderr)
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
             lock: bool, description, transfer_destination: str | None = None,
             fee_rate: float | None = None, funded: bool = False,
             oversize_hint: str | None = None) -> str | None:
    """Compose the issuance from the owner address; return its unsigned raw tx,
    or None after printing the failure (with a funding hint when relevant).

    `oversize_hint` is printed when Counterparty refuses the message for not
    fitting its single OP_RETURN — only `describe` can hit that, since it is the
    one command here that puts a payload of the caller's choosing on the wire.
    """
    try:
        composed = compose_retrying(lambda: cp.compose_issuance(
            source=owner, asset=asset, quantity=quantity, divisible=divisible,
            description=description, lock=lock,
            transfer_destination=transfer_destination,
            sat_per_vbyte=fee_rate,
        ), funded)
    except CounterpartyError as e:
        msg = str(e)
        print(f"compose failed: {msg}", file=sys.stderr)
        if oversize_hint and "OP_RETURN" in msg:
            print(oversize_hint, file=sys.stderr)
        if "No UTXOs" in msg or "inputs_set" in msg or "Insufficient funds" in msg:
            print(f"hint: {owner} owns {asset} but has no spendable BTC. The issuance is "
                  f"sourced from the owner address, so it pays its own fee. Drop "
                  f"--no-fund to top it up automatically.", file=sys.stderr)
        return None
    rawtx = composed.get("rawtransaction")
    if not rawtx:
        print(f"compose returned no rawtransaction: {composed}", file=sys.stderr)
        return None
    return rawtx


def cmd_lock_supply(config: Config, wallet: str, asset: str,
                    fee_rate: float | None = None, dry_run: bool = False,
                    fund_from: str | None = None, no_fund: bool = False) -> int:
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
    fund = ensure_funded(btc, cp, wallet, owner, fee_rate=fee_rate,
                         fund_from=fund_from, no_fund=no_fund, dry_run=dry_run)
    if fund.code is not None:
        return fund.code
    rawtx = _compose(cp, owner, asset, quantity=0, divisible=divisible,
                     lock=True, description=None, fee_rate=fee_rate,
                     funded=fund.funded)
    if rawtx is None:
        return 1

    print(f"lock-supply {asset}")
    print(f"  owner     : {owner}")
    if fee_rate is not None:
        print(f"  fee rate  : {fee_rate} sat/vB")
    return _sign_and_broadcast(btc, wallet, owner, rawtx, dry_run)


def cmd_lock_description(config: Config, wallet: str, asset: str,
                         fee_rate: float | None = None, dry_run: bool = False,
                         fund_from: str | None = None, no_fund: bool = False) -> int:
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
    fund = ensure_funded(btc, cp, wallet, owner, fee_rate=fee_rate,
                         fund_from=fund_from, no_fund=no_fund, dry_run=dry_run)
    if fund.code is not None:
        return fund.code
    rawtx = _compose(cp, owner, asset, quantity=0, divisible=divisible,
                     lock=False, description="LOCK_DESCRIPTION", fee_rate=fee_rate,
                     funded=fund.funded)
    if rawtx is None:
        return 1

    print(f"lock-description {asset}")
    print(f"  owner     : {owner}")
    print(f"  freezing  : {info.get('description') or '(empty description)'}")
    if fee_rate is not None:
        print(f"  fee rate  : {fee_rate} sat/vB")
    return _sign_and_broadcast(btc, wallet, owner, rawtx, dry_run)


# Counterparty reads two literal descriptions as commands rather than as text
# (issuance.py, parse): they keep the CURRENT description and set a flag instead.
# The comparison there is on `description.lower()`, so match it exactly — a
# padded " lock " really is stored as text.
_LOCK_LITERALS = {
    "lock": ("lock-supply", "locks the SUPPLY instead, keeping the current description"),
    "lock_description": ("lock-description",
                         "locks the DESCRIPTION instead, keeping the current one"),
}


def _new_description(text: str | None, file_path: str | None,
                     clear: bool) -> tuple[str | None, int]:
    """The description to set, from exactly one of --text/--file/--clear.

    Returns (description, exit_code); the code is non-zero once the reason has
    been printed. A file must be UTF-8 text: a traditional description is a
    string in Counterparty's message, and file BYTES need a taproot envelope
    (`inscribe`), which is a different transaction and mints a counter. One
    trailing newline is dropped, since text files carry one and descriptions
    do not.
    """
    given = [name for name, on in (("<text>/--text", text is not None),
                                   ("--file", file_path is not None),
                                   ("--clear", clear)) if on]
    if len(given) != 1:
        print(f"give exactly one of <text>/--text, --file or --clear"
              f"{' (got ' + ', '.join(given) + ')' if given else ''}", file=sys.stderr)
        return None, 1

    if clear:
        return "", 0
    if text is not None:
        return text, 0

    try:
        with open(file_path, "rb") as fh:
            raw = fh.read()
    except OSError as e:
        print(f"cannot read {file_path}: {e}", file=sys.stderr)
        return None, 1
    try:
        description = raw.decode("utf-8")
    except UnicodeDecodeError:
        print(f"{file_path} is not UTF-8 text ({len(raw):,} bytes)", file=sys.stderr)
        print(f"hint: a description carries text. To commit file bytes, inscribe them "
              f"in a taproot envelope instead — that mints a NEW counter:\n"
              f"  counters wallet inscribe --file {file_path} --asset <ASSET>",
              file=sys.stderr)
        return None, 1
    if description.endswith("\r\n"):
        description = description[:-2]
    elif description.endswith("\n"):
        description = description[:-1]
    return description, 0


def _render_description(description: str, mime_type: str | None,
                        block_index: int) -> str:
    """One line naming a description for the confirmation summary."""
    if not description:
        return "(empty)"
    mime = mime_type or "text/plain"
    if classify_mime_type(mime, block_index) == "binary":
        # Binary content comes back from Counterparty hex-encoded (§5.1).
        return f"{len(description) // 2:,} bytes of {mime} (file content)"
    size = len(description.encode("utf-8"))
    shown = description if len(description) <= 60 else description[:57] + "..."
    return f"{shown!r} ({size:,} B, {mime})"


def cmd_describe(config: Config, wallet: str, asset: str,
                 text: str | None = None, file_path: str | None = None,
                 clear: bool = False, fee_rate: float | None = None,
                 assume_yes: bool = False, dry_run: bool = False,
                 fund_from: str | None = None, no_fund: bool = False) -> int:
    """Set an asset's description the traditional way: a zero-quantity issuance
    whose text rides in the OP_RETURN.

    This is the pre-taproot description — a tagline or, more usefully, a URL to
    the metadata (ZOMBIEPEPES reads "BURN THEM ALL"; HONDACIVIC reads
    "https://xcp.fun/HONDACIVIC.json"). The carrier is an OP_RETURN, so under
    v3 the event is NOT a counter (R4 wants a taproot reveal) and nothing is
    numbered — `inscribe --asset` is the command that commits content and mints.

    The whole Counterparty message must fit one 80-byte OP_RETURN. After the
    CNTRPRTY prefix and the CBOR-framed issuance fields (asset id, quantity,
    flags, mime type) that leaves 54-58 bytes of text — 58 for a small asset
    id, 54 for the largest, measured against Core. Core enforces it; the hint
    on failure says so.
    """
    description, code = _new_description(text, file_path, clear)
    if code:
        return code

    btc = BitcoindClient(config)
    cp = CounterpartyClient(config)

    resolved = _resolve_owned_asset(btc, cp, wallet, asset)
    if resolved is None:
        return 1
    asset, info, owner = resolved

    if info.get("description_locked"):
        print(f"{asset} has a locked description; Counterparty rejects any change "
              f"to it (\"Cannot update a locked description\")", file=sys.stderr)
        return 1

    literal = _LOCK_LITERALS.get(description.lower())
    if literal:
        command, effect = literal
        print(f"refusing: Counterparty reads the description {description!r} as a "
              f"command, not as text — it {effect}.", file=sys.stderr)
        print(f"hint: run `counters wallet --name {wallet} {command} {asset}` if that "
              f"is what you meant.", file=sys.stderr)
        return 1

    current = info.get("description") or ""
    if description == current:
        print(f"{asset} already reads exactly that; no transaction needed",
              file=sys.stderr)
        return 1

    # The height of the issuance that set the CURRENT description: how
    # Counterparty classified its mime type is a function of that block.
    last_block = int(info.get("last_issuance_block_index") or 0)
    was_binary = bool(current) and classify_mime_type(
        info.get("mime_type") or "text/plain", last_block) == "binary"

    print(f"describe {asset}")
    print(f"  owner     : {owner}")
    print(f"  now       : {_render_description(current, info.get('mime_type'), last_block)}")
    print(f"  new       : {_render_description(description, 'text/plain', last_block)}")
    print(f"  carrier   : OP_RETURN issuance, quantity 0 — mints no counter")
    if fee_rate is not None:
        print(f"  fee rate  : {fee_rate} sat/vB")
    if was_binary:
        print(f"  WARNING   : {asset} currently carries file content; this replaces it "
              f"with text. Counters already numbered keep their content — the index "
              f"stores it — but explorers reading the asset will show the text.")

    if not (dry_run or assume_yes or _confirm_prompt(
            f"\nset the {asset} description?")):
        print("aborted", file=sys.stderr)
        return 1

    fund = ensure_funded(btc, cp, wallet, owner, fee_rate=fee_rate,
                         fund_from=fund_from, no_fund=no_fund, dry_run=dry_run)
    if fund.code is not None:
        return fund.code

    size = len(description.encode("utf-8"))
    hint = (f"hint: the message must fit one 80-byte OP_RETURN — 54 to 58 bytes of "
            f"text, depending on the asset id, and this description is {size:,} B. "
            f"Shorten it (the traditional "
            f"trick is a URL to the metadata, as HONDACIVIC does), or carry the "
            f"content in a taproot envelope with `inscribe --asset {asset} --file ...`, "
            f"which mints a NEW counter.")
    rawtx = _compose(cp, owner, asset, quantity=0, divisible=bool(info.get("divisible")),
                     lock=False, description=description, fee_rate=fee_rate,
                     funded=fund.funded, oversize_hint=hint)
    if rawtx is None:
        return 1

    return _sign_and_broadcast(btc, wallet, owner, rawtx, dry_run)


def cmd_transfer_ownership(config: Config, wallet: str, asset: str, destination: str,
                           fee_rate: float | None = None, dry_run: bool = False,
                           fund_from: str | None = None, no_fund: bool = False) -> int:
    """Hand an asset's issuance rights to another address.

    A zero-quantity issuance carrying `transfer_destination`: the recipient
    becomes the asset's `owner` and inherits the power to reissue, lock, and
    reinscribe. TOKEN BALANCES DO NOT MOVE — those travel by `send`, and a
    counter transferred without this command leaves the sender still able to
    reinscribe over it.

    Counterparty forbids `transfer_destination` under taproot encoding, so this
    is always its own opreturn transaction, never bundled with an inscription.
    """
    btc = BitcoindClient(config)
    cp = CounterpartyClient(config)

    # Validate the destination first, so a swapped argument fails here with the
    # order hint instead of downstream as a confusing "unknown asset".
    if not _is_valid_address(btc, destination):
        print(f"destination {destination!r} is not a valid Bitcoin address", file=sys.stderr)
        print(_ORDER_HINT, file=sys.stderr)
        return 1

    resolved = _resolve_owned_asset(btc, cp, wallet, asset)
    if resolved is None:
        if _is_valid_address(btc, asset):
            print(_ORDER_HINT, file=sys.stderr)
        return 1
    asset, info, owner = resolved

    if destination == owner:
        print(f"{owner} already holds the issuance rights of {asset}", file=sys.stderr)
        return 1

    # description omitted (None): preserve the asset's content — see lock-supply.
    # lock=False leaves the supply lock as it is; locks are one-way, so this
    # can never unlock an already-locked asset.
    fund = ensure_funded(btc, cp, wallet, owner, fee_rate=fee_rate,
                         fund_from=fund_from, no_fund=no_fund, dry_run=dry_run)
    if fund.code is not None:
        return fund.code
    rawtx = _compose(cp, owner, asset, quantity=0, divisible=bool(info.get("divisible")),
                     lock=False, description=None, transfer_destination=destination,
                     fee_rate=fee_rate, funded=fund.funded)
    if rawtx is None:
        return 1

    print(f"transfer-ownership {asset}")
    print(f"  from      : {owner}")
    print(f"  to        : {destination}")
    print(f"  moves     : issuance rights — reissue, lock, reinscribe")
    print(f"  stays     : token balances (move those with `send`)")
    if fee_rate is not None:
        print(f"  fee rate  : {fee_rate} sat/vB")
    return _sign_and_broadcast(btc, wallet, owner, rawtx, dry_run)


def cmd_issue(config: Config, wallet: str, asset: str, amount: str,
              lock: bool = False, fee_rate: float | None = None,
              dry_run: bool = False,
              fund_from: str | None = None, no_fund: bool = False) -> int:
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
    fund = ensure_funded(btc, cp, wallet, owner, fee_rate=fee_rate,
                         fund_from=fund_from, no_fund=no_fund, dry_run=dry_run)
    if fund.code is not None:
        return fund.code
    rawtx = _compose(cp, owner, asset, quantity=raw, divisible=divisible,
                     lock=lock, description=None, fee_rate=fee_rate,
                     funded=fund.funded)
    if rawtx is None:
        return 1

    print(f"issue +{_fmt_raw(raw, divisible)} {asset}{' (and LOCK)' if lock else ''}")
    print(f"  owner     : {owner}")
    if fee_rate is not None:
        print(f"  fee rate  : {fee_rate} sat/vB")
    return _sign_and_broadcast(btc, wallet, owner, rawtx, dry_run)
