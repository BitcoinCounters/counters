"""`counters wallet inscribe` — mint a counter from a file (build ref v3 §11).

Counterparty Core does the heavy lifting: composing the issuance with
`encoding=taproot` returns the commit/reveal pair — `rawtransaction` is the
UNSIGNED commit (funded from the source address's coins) and
`signed_reveal_rawtransaction` the reveal, whose envelope input Core has
already signed with its ephemeral envelope key. Our job is only to:

  1. derive the description string from the file per Core's content encoding
     (text -> UTF-8, binary -> hex; content.py mirrors the consensus rule),
  2. pick the Counterparty *source* address (it funds the commit, receives
     the issued tokens, and pays the 0.5 XCP burn for a named asset),
  3. have Bitcoin Core sign the commit — all inputs are segwit (the composer
     enforces this) so signing cannot change the txid the reveal commits to,
  4. package-validate [commit, reveal] with testmempoolaccept and broadcast.

Key custody stays in Bitcoin Core. There is no local envelope construction.

New content on an EXISTING asset is a plain reissuance with a fresh
taproot-carried description (quantity 0 keeps the supply): under per-event
numbering (N6) it earns a new counter. The wallet must hold the asset's
issuance rights. Constraints inherited from Counterparty: no
transfer_destination with taproot encoding, and reissuance requires the
description to be unlocked.
"""

from __future__ import annotations

import mimetypes
import os
import random
import sys

from ..bitcoind import COIN, BitcoindClient, BitcoindError
from ..config import RESERVED_ASSETS, Config
from ..content import classify_mime_type
from ..counterparty import CounterpartyClient, CounterpartyError
from .wallet import _wallet_addresses

NUMERIC_MIN = 26 ** 12 + 1     # Counterparty numeric-asset range
NUMERIC_MAX = 2 ** 64 - 1
NAMED_ISSUANCE_FEE_XCP = 50_000_000   # 0.5 XCP burned to register a named asset


def guess_content_type(path: str) -> str:
    ct, _ = mimetypes.guess_type(path)
    return ct or "application/octet-stream"


def random_numeric_asset() -> str:
    return "A" + str(random.randint(NUMERIC_MIN, NUMERIC_MAX))


def _spendable_addresses(btc: BitcoindClient, wallet: str) -> dict[str, int]:
    """{address: total sats} of confirmed+unconfirmed spendable coins, so the
    source can be chosen among addresses that can actually fund the commit."""
    totals: dict[str, int] = {}
    for u in btc.wallet_call(wallet, "listunspent", [0, 9999999]):
        addr = u.get("address")
        if addr and u.get("spendable", True):
            totals[addr] = totals.get(addr, 0) + int(round(u.get("amount", 0) * COIN))
    return totals


def _find_xcp_address(cp: CounterpartyClient, addresses: list[str],
                      min_xcp: int = NAMED_ISSUANCE_FEE_XCP) -> str | None:
    """First address holding >= min_xcp XCP (balances are address-level)."""
    for addr in addresses:
        try:
            if cp.get_xcp_balance(addr) >= min_xcp:
                return addr
        except CounterpartyError:
            continue
    return None


def _description_for(body: bytes, mime_type: str, height: int) -> str | None:
    """The `description` compose parameter for this content: UTF-8 text for
    textual MIME types, hex for binary — the same classification Counterparty
    consensus applies (§5.1). None if a textual type isn't valid UTF-8."""
    if classify_mime_type(mime_type, height) == "text":
        try:
            return body.decode("utf-8")
        except UnicodeDecodeError:
            return None
    return body.hex()


def cmd_inscribe(
    config: Config,
    wallet: str,
    file_path: str,
    asset: str | None = None,
    fee_rate: float | None = None,
    supply: int = 1,
    divisible: bool = False,
    lock: bool = False,
    source: str | None = None,
    inputs_set: str | None = None,
    dry_run: bool = False,
) -> int:
    btc = BitcoindClient(config)
    cp = CounterpartyClient(config)

    if not os.path.isfile(file_path):
        print(f"file not found: {file_path}", file=sys.stderr)
        return 1
    with open(file_path, "rb") as fh:
        body = fh.read()
    if not body:
        print("refusing to inscribe an empty file (an empty description is no "
              "event — rule R3)", file=sys.stderr)
        return 1
    mime_type = guess_content_type(file_path)

    try:
        height = btc.get_block_count()
    except BitcoindError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    description = _description_for(body, mime_type, height)
    if description is None:
        print(f"{file_path} is detected as {mime_type} (textual) but is not valid "
              f"UTF-8 — rename/convert the file or use a binary MIME type.",
              file=sys.stderr)
        return 1

    wallet_addrs = set(_wallet_addresses(btc, wallet))
    try:
        spendable = _spendable_addresses(btc, wallet)
    except BitcoindError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    # Resolve the asset. Three shapes:
    #   - existing asset you own: reissue with the new description (quantity 0)
    #   - new named asset (0.5 XCP burn): source must hold the XCP
    #   - no asset given: free numeric asset
    if supply < 1:
        print(f"--supply must be a positive whole number, got {supply}", file=sys.stderr)
        return 1
    reissue = False
    named = False
    quantity = supply * COIN if divisible else supply
    if asset is not None:
        asset = asset if "." in asset else asset.upper()
        if asset in RESERVED_ASSETS:
            print(f"cannot inscribe on reserved asset {asset}", file=sys.stderr)
            return 1
        info = cp.get_asset(asset)
        if info:
            # Existing asset -> reissuance carrying new content.
            owner = info.get("owner") or info.get("issuer")
            if owner not in wallet_addrs:
                print(f"asset {asset} exists and its issuance rights are held by "
                      f"{owner}, which is not in wallet {wallet!r}. Only the owner "
                      f"can attach new content (a reissuance).", file=sys.stderr)
                return 1
            if info.get("description_locked"):
                print(f"{asset}'s description is locked; no new content can ever "
                      f"be attached to it.", file=sys.stderr)
                return 1
            reissue = True
            asset = info.get("asset") or asset
            divisible = bool(info.get("divisible"))
            quantity = 0  # keep the supply; the event is the description change
            source = source or owner
        else:
            named = True
    else:
        asset = random_numeric_asset()

    # Pick the source: it funds the commit from its own coins, so it needs BTC
    # (and the XCP burn for a named asset).
    if source is None:
        if named:
            source = _find_xcp_address(cp, sorted(wallet_addrs))
            if source is None:
                print(f"no wallet address holds the {NAMED_ISSUANCE_FEE_XCP / COIN:.1f} "
                      f"XCP required to register a named asset. Fund a wallet address "
                      f"with XCP first, then retry (or omit --asset for a free "
                      f"numeric asset).", file=sys.stderr)
                return 1
        else:
            # Address with the most spendable BTC.
            source = max(spendable, key=spendable.get) if spendable else None
            if source is None:
                print(f"wallet {wallet!r} has no spendable BTC to fund the commit; "
                      f"fund it and retry.", file=sys.stderr)
                return 1
    elif source not in wallet_addrs:
        print(f"--source {source} is not an address of wallet {wallet!r}",
              file=sys.stderr)
        return 1
    if not spendable.get(source) and inputs_set is None:
        print(f"note: source {source} has no spendable BTC on record; compose may "
              f"fail — fund it or pass --inputs-set TXID:VOUT", file=sys.stderr)

    # Compose the commit/reveal pair via Counterparty Core.
    try:
        composed = cp.compose_issuance(
            source=source, asset=asset, quantity=quantity, divisible=divisible,
            description=description, lock=lock, encoding="taproot",
            mime_type=mime_type, sat_per_vbyte=fee_rate, inputs_set=inputs_set,
        )
    except CounterpartyError as e:
        msg = str(e)
        print(f"compose failed: {msg}", file=sys.stderr)
        if "No UTXOs" in msg or "inputs_set" in msg:
            print(f"hint: the source address {source} needs spendable BTC — the "
                  f"commit is funded from its coins.", file=sys.stderr)
        if "legacy inputs" in msg:
            print("hint: taproot encoding needs segwit coins on the source; move "
                  "funds off legacy 1... addresses first.", file=sys.stderr)
        return 1
    commit_unsigned = composed.get("rawtransaction")
    reveal_hex = composed.get("signed_reveal_rawtransaction")
    if not commit_unsigned or not reveal_hex:
        print(f"compose returned no commit/reveal pair — is Counterparty Core v11+? "
              f"keys: {sorted(composed)}", file=sys.stderr)
        return 1

    # Bitcoin Core signs the commit. All composer-selected inputs are segwit,
    # so signing cannot change the txid the pre-signed reveal spends — but
    # verify anyway before anything is broadcast.
    unsigned_txid = btc._call("decoderawtransaction", [commit_unsigned])["txid"]
    signed = btc.wallet_call(wallet, "signrawtransactionwithwallet", [commit_unsigned])
    if not signed.get("complete"):
        print(f"commit signing failed (does {source} belong to wallet {wallet!r}?): "
              f"{signed.get('errors')}", file=sys.stderr)
        return 1
    commit_hex = signed["hex"]
    commit_dec = btc._call("decoderawtransaction", [commit_hex])
    if commit_dec["txid"] != unsigned_txid:
        print("internal error: commit txid changed on signing; the pre-signed "
              "reveal would be orphaned. Nothing was broadcast.", file=sys.stderr)
        return 1
    reveal_txid = btc._call("decoderawtransaction", [reveal_hex])["txid"]

    # Validate BOTH transactions as a package without broadcasting.
    try:
        checks = btc._call("testmempoolaccept", [[commit_hex, reveal_hex]])
    except BitcoindError as e:
        print(f"testmempoolaccept failed to run: {e}", file=sys.stderr)
        checks = []
    all_ok = bool(checks) and all(c.get("allowed") for c in checks)

    # report
    if reissue:
        kind = " (reissuance — new content on your existing asset)"
    elif named:
        kind = " (named)"
    else:
        kind = " (numeric, free)"
    print(f"asset            : {asset}{kind}")
    print(f"content_type     : {mime_type}  ({len(body)} bytes)")
    if not reissue:
        print(f"supply           : {supply}{' divisible' if divisible else ''}"
              f"{' (LOCKED)' if lock else ''}")
    print(f"source           : {source}")
    print(f"commit txid      : {unsigned_txid}")
    print(f"reveal txid      : {reveal_txid}")
    if composed.get("btc_fee") is not None:
        print(f"commit fee       : {composed['btc_fee']} sat")
    if named:
        print("XCP cost         : 0.5 XCP (named-asset issuance burn)")

    print("\npackage validity (testmempoolaccept):")
    for c in checks:
        verdict = "allowed" if c.get("allowed") else f"REJECTED: {c.get('reject-reason')}"
        print(f"  {c.get('txid', '?')[:16]}…  {verdict}")

    if dry_run:
        print("\n--- DRY RUN (nothing broadcast) ---")
        print(f"commit_raw: {commit_hex}")
        print(f"reveal_raw: {reveal_hex}")
        return 0 if all_ok else 1

    if not all_ok:
        print("\nrefusing to broadcast: package failed validation (see above).", file=sys.stderr)
        print(f"commit_raw: {commit_hex}\nreveal_raw: {reveal_hex}", file=sys.stderr)
        return 1

    # broadcast commit then reveal (the reveal is a CPFP child of the commit)
    try:
        ctxid = btc._call("sendrawtransaction", [commit_hex])
        rtxid = btc._call("sendrawtransaction", [reveal_hex])
    except BitcoindError as e:
        print(f"broadcast failed: {e}", file=sys.stderr)
        print(f"commit_raw: {commit_hex}\nreveal_raw: {reveal_hex}", file=sys.stderr)
        return 1
    print(f"\nbroadcast OK\n  commit: {ctxid}\n  reveal: {rtxid}")
    print("the counter is numbered once the reveal confirms and Counterparty "
          "parses the issuance.")
    return 0
