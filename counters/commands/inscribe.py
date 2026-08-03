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

New content on an EXISTING asset is a reinscription — a Counterparty reissuance with a fresh
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
from decimal import Decimal

from ..bitcoind import COIN, BitcoindClient, BitcoindError
from ..config import RESERVED_ASSETS, Config
from ..content import classify_mime_type
from ..counterparty import CounterpartyClient, CounterpartyError
from .funding import (
    _fund_source,
    compose_retrying,
    ensure_funded,
    spendable_by_address as _spendable_addresses,
)
from .wallet import _derived_addresses, _wallet_addresses

NUMERIC_MIN = 26 ** 12 + 1     # Counterparty numeric-asset range
NUMERIC_MAX = 2 ** 64 - 1
NAMED_ISSUANCE_FEE_XCP = 50_000_000   # 0.5 XCP burned to register a named asset
DUST_SAT = 330                        # below this an output is unspendable dust


def guess_content_type(path: str) -> str:
    ct, _ = mimetypes.guess_type(path)
    return ct or "application/octet-stream"


def random_numeric_asset() -> str:
    return "A" + str(random.randint(NUMERIC_MIN, NUMERIC_MAX))


def _is_segwit_address(addr: str) -> bool:
    """True if `addr` can be the source of a taproot-encoded compose. Counterparty
    checks the source's scriptPubKey is a witness program (composer.py
    is_segwit_address), so only NATIVE segwit qualifies: bc1q (P2WPKH) and bc1p
    (P2TR). Legacy 1... AND nested-segwit 3... are both rejected — a 3...
    address's scriptPubKey is P2SH, not a witness program, even though its
    coins spend with a witness."""
    return addr.startswith(("bc1", "tb1", "bcrt1"))


def _xcp_holders(cp: CounterpartyClient, addresses: list[str],
                 min_xcp: int = NAMED_ISSUANCE_FEE_XCP) -> tuple[list[str], int]:
    """Addresses holding >= min_xcp XCP, in the order given (Counterparty
    balances are per-address), plus a count of addresses we could NOT ask.

    That count matters: while Counterparty is catching up its API answers
    "Counterparty not ready", and treating that as a zero balance would report
    "no XCP anywhere" about a wallet that is holding plenty."""
    holders: list[str] = []
    unreachable = 0
    for addr in addresses:
        try:
            if cp.get_xcp_balance(addr) >= min_xcp:
                holders.append(addr)
        except CounterpartyError:
            unreachable += 1
    return holders, unreachable


def _estimate_source_need(content_len: int, fee_rate: float) -> int:
    """Roughly what the source address must hold to fund an inscription.

    The commit pays the reveal's fee forward as an output, so the source funds
    both. A reveal is content/4 vB (the witness discount) plus envelope
    overhead; the commit is a 1-in-2-out taproot spend. Deliberately generous —
    this only decides how much to move, and change comes back to the wallet."""
    reveal_vb = content_len / 4 + 210
    commit_vb = 200
    return int((reveal_vb + commit_vb) * fee_rate * 1.15) + DUST_SAT


def _pick_source(cp: CounterpartyClient, wallet_addrs: set[str],
                 spendable: dict[str, int], *, named: bool,
                 inputs_set: str | None,
                 funding: bool = False) -> tuple[str | None, str | None]:
    """Auto-select a Counterparty source that can actually fund a TAPROOT
    inscription. The commit is funded from the source's OWN coins, so a working
    source needs spendable NATIVE-SEGWIT BTC — neither a legacy 1... nor a
    nested 3... address can be a taproot-encoding source (build ref v3 §11).
    A NAMED asset additionally needs >= 0.5 XCP on
    that SAME address, since issuance is single-source. The richest eligible
    address wins, so the commit has the most room to fund. When --inputs-set
    pins the funding UTXOs, the spendable-BTC requirement is relaxed (the caller
    vouches for the pinned coins). Returns (source, error): exactly one is set."""
    addrs = sorted(wallet_addrs)
    funded = sorted(
        (a for a in addrs if spendable.get(a, 0) > 0 and _is_segwit_address(a)),
        key=lambda a: spendable[a], reverse=True,
    )
    pinned = inputs_set is not None

    if named:
        holders, unreachable = _xcp_holders(cp, addrs)
        if not holders and unreachable:
            return None, (
                f"could not ask Counterparty about {unreachable} address(es) — it "
                f"is probably still catching up (check `counters status`). Refusing "
                f"rather than reporting an XCP balance we could not read."
            )
        if not holders:
            return None, (
                f"no wallet address holds the {NAMED_ISSUANCE_FEE_XCP / COIN:.1f} "
                f"XCP required to register a named asset. Fund a wallet address "
                f"with XCP first, then retry (or omit --asset for a free numeric "
                f"asset)."
            )
        if pinned or funding:
            # --inputs-set vouches for the coins; --fund-from will move coins to
            # the XCP holder before composing. Either way it need not be funded yet.
            return holders[0], None
        holder_set = set(holders)
        for a in funded:  # richest segwit address that also holds the XCP
            if a in holder_set:
                return a, None
        return None, (
            f"the 0.5 XCP for a named asset sits on {holders[0]}, but no "
            f"XCP-holding address also has spendable segwit BTC to fund the "
            f"taproot commit (a named issuance is single-source). Move the XCP "
            f"onto a segwit (bc1q/bc1p) address that has BTC — or pass --source "
            f"with --inputs-set to fund it explicitly."
        )

    # Free numeric asset: only needs spendable segwit BTC.
    if funded:
        return funded[0], None
    if pinned and addrs:
        return addrs[0], None
    return None, (
        "wallet has no spendable native-segwit BTC to fund the commit (taproot "
        "encoding can't compose from legacy 1... or nested 3... addresses); "
        "fund a bc1q/bc1p address and retry."
    )


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


def _reveal_fee_sat(commit_dec: dict, reveal_dec: dict) -> int | None:
    """Fee of the reveal transaction, in sats. The reveal is a CPFP child that
    spends the commit's envelope output(s), so its inputs all come from the
    commit — which isn't confirmed yet, hence we read their values from the
    decoded commit rather than the chain. fee = spent commit outputs − reveal
    outputs. Returns None if a reveal input can't be resolved from the commit
    (unexpected shape), so the caller can just omit the line."""
    commit_txid = commit_dec["txid"]
    outs = {o["n"]: o["value"] for o in commit_dec["vout"]}
    total_in = 0.0
    for vin in reveal_dec.get("vin", []):
        if vin.get("txid") == commit_txid and vin.get("vout") in outs:
            total_in += outs[vin["vout"]]
        else:
            return None
    total_out = sum(o.get("value", 0) for o in reveal_dec.get("vout", []))
    return round((total_in - total_out) * COIN)


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
    no_mempool_check: bool = False,
    fund_from: str | None = None,
    no_fund: bool = False,
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

    # Union the on-chain view with descriptor-derived addresses: Core omits
    # CHANGE addresses from listreceivedbyaddress, and XCP parked on one whose
    # coins are spent would otherwise look like "no XCP anywhere".
    wallet_addrs = set(_wallet_addresses(btc, wallet))
    try:
        wallet_addrs.update(_derived_addresses(btc, wallet, 20))
    except BitcoindError:
        pass
    try:
        spendable = _spendable_addresses(btc, wallet)
    except BitcoindError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    # Resolve the asset. Three shapes:
    #   - existing asset you own: reinscribe with the new description (quantity 0)
    #   - new named asset (0.5 XCP burn): source must hold the XCP
    #   - no asset given: free numeric asset
    if supply < 1:
        print(f"--supply must be a positive whole number, got {supply}", file=sys.stderr)
        return 1
    reinscribe = False
    named = False
    quantity = supply * COIN if divisible else supply
    if asset is not None:
        asset = asset if "." in asset else asset.upper()
        if asset in RESERVED_ASSETS:
            print(f"cannot inscribe on reserved asset {asset}", file=sys.stderr)
            return 1
        info = cp.get_asset(asset)
        if info:
            # Existing asset -> reinscription (a Counterparty reissuance) carrying new content.
            owner = info.get("owner") or info.get("issuer")
            if owner not in wallet_addrs:
                print(f"asset {asset} exists and its issuance rights are held by "
                      f"{owner}, which is not in wallet {wallet!r}. Only the owner "
                      f"can attach new content (a reinscription).", file=sys.stderr)
                return 1
            if info.get("description_locked"):
                print(f"{asset}'s description is locked; no new content can ever "
                      f"be attached to it.", file=sys.stderr)
                return 1
            reinscribe = True
            asset = info.get("asset") or asset
            divisible = bool(info.get("divisible"))
            quantity = 0  # keep the supply; the event is the description change
            source = source or owner
        else:
            named = True
    else:
        asset = random_numeric_asset()

    # Pick the source: it funds the commit from its own coins, so it needs
    # spendable segwit BTC (and, for a named asset, the 0.5 XCP burn) on the
    # SAME address. Auto-selection skips addresses that can't fund a taproot
    # commit — notably legacy 1... addresses holding only XCP.
    if source is None:
        source, err = _pick_source(
            cp, wallet_addrs, spendable, named=named, inputs_set=inputs_set,
            funding=not no_fund,
        )
        if source is None:
            print(err, file=sys.stderr)
            return 1
    else:
        if source not in wallet_addrs:
            print(f"--source {source} is not an address of wallet {wallet!r}",
                  file=sys.stderr)
            return 1
        if not _is_segwit_address(source) and inputs_set is None:
            print(f"note: --source {source} is not a native-segwit address; "
                  f"taproot encoding needs a bc1q/bc1p source (legacy 1... and "
                  f"nested 3... are rejected), so compose will likely fail. Use "
                  f"a bc1q/bc1p source (or --inputs-set).", file=sys.stderr)
    # Counterparty cannot take the fee from a second address — the first input
    # IS the issuer — so the source has to own its coins, and topping it up is
    # the DEFAULT rather than a flag you have to know about. The source is
    # whichever address holds the asset's rights or XCP, which is rarely where
    # the wallet keeps its BTC: an owner sitting on nothing but the dust output
    # that carries the asset is the normal case, not the exception.
    # --fund-from pins who pays; --no-fund leaves the source untouched.
    need = _estimate_source_need(len(body), fee_rate or 1.0)
    fund = ensure_funded(btc, cp, wallet, source, fee_rate=fee_rate,
                         fund_from=fund_from, no_fund=no_fund or inputs_set is not None,
                         dry_run=dry_run, need=need)
    if fund.code is not None:
        return fund.code
    if fund.funded:
        spendable = _spendable_addresses(btc, wallet)

    # Enough, not merely non-empty: an address holding the asset's dust output
    # has BTC on record and still cannot pay for a commit that carries the
    # reveal's fee forward.
    if inputs_set is None and spendable.get(source, 0) < need:
        remedy = ("drop --no-fund to top it up automatically" if no_fund
                  else "fund it, or pass --fund-from ADDRESS")
        print(f"note: source {source} holds {spendable.get(source, 0)} sat but the "
              f"commit and reveal need about {need}; compose will likely fail — "
              f"{remedy}, or pass --inputs-set TXID:VOUT", file=sys.stderr)

    # Compose the commit/reveal pair via Counterparty Core.
    try:
        composed = compose_retrying(lambda: cp.compose_issuance(
            source=source, asset=asset, quantity=quantity, divisible=divisible,
            description=description, lock=lock, encoding="taproot",
            mime_type=mime_type, sat_per_vbyte=fee_rate, inputs_set=inputs_set,
        ), fund.funded)
    except CounterpartyError as e:
        msg = str(e)
        print(f"compose failed: {msg}", file=sys.stderr)
        if "No UTXOs" in msg or "inputs_set" in msg or "Insufficient funds" in msg:
            print(f"hint: the source address {source} needs spendable BTC — the "
                  f"commit is funded from its coins. Drop --no-fund to move it "
                  f"there automatically, or pin the payer with --fund-from "
                  f"ADDRESS.", file=sys.stderr)
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
    reveal_dec = btc._call("decoderawtransaction", [reveal_hex])
    reveal_txid = reveal_dec["txid"]

    # Validate BOTH transactions as a package without broadcasting — unless the
    # caller opts out. An oversized inscription is a VALID transaction that
    # simply exceeds Bitcoin's standard-relay weight (400k WU), so
    # testmempoolaccept rejects it with `tx-size` and the local node won't relay
    # it; --no-mempool-check skips the check so a --dry-run just emits the signed
    # commit/reveal hex for you to submit directly to a miner.
    checks: list = []
    all_ok = True
    if not no_mempool_check:
        try:
            checks = btc._call("testmempoolaccept", [[commit_hex, reveal_hex]])
        except BitcoindError as e:
            print(f"testmempoolaccept failed to run: {e}", file=sys.stderr)
            checks = []
        all_ok = bool(checks) and all(c.get("allowed") for c in checks)

    # report
    if reinscribe:
        kind = " (reinscription — new content on your existing asset)"
    elif named:
        kind = " (named)"
    else:
        kind = " (numeric, free)"
    print(f"asset            : {asset}{kind}")
    print(f"content_type     : {mime_type}  ({len(body)} bytes)")
    if not reinscribe:
        print(f"supply           : {supply}{' divisible' if divisible else ''}"
              f"{' (LOCKED)' if lock else ''}")
    print(f"source           : {source}")
    print(f"commit txid      : {unsigned_txid}")
    print(f"reveal txid      : {reveal_txid}")
    commit_fee = composed.get("btc_fee")
    reveal_fee = _reveal_fee_sat(commit_dec, reveal_dec)
    if commit_fee is not None:
        print(f"commit fee       : {commit_fee} sat")
    if reveal_fee is not None:
        print(f"reveal fee       : {reveal_fee} sat")
    if commit_fee is not None and reveal_fee is not None:
        print(f"total fee        : {commit_fee + reveal_fee} sat")
    if named:
        print("XCP cost         : 0.5 XCP (named-asset issuance burn)")

    if no_mempool_check:
        print("\npackage validity : skipped (--no-mempool-check); submit the hex "
              "below directly to a miner")
    else:
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
