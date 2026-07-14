"""`counters wallet inscribe` — mint a counter from a file (commit + reveal).

Flow (build ref §5, CLI ref §3), designed to work from a single funding UTXO:
  1. Build the COUNT envelope tapscript from the file; derive the P2TR commit
     address (builder.py).
  2. Build + sign the COMMIT (Core funds it): it spends the wallet's BTC and
     produces two outputs — the tiny P2TR commit output (which the reveal will
     script-spend to expose the envelope) and the wallet CHANGE output.
  3. That change output becomes the reveal's vin[0] = the Counterparty *source*
     (so issued tokens + ownership land on a wallet address). Compose the
     issuance as a legacy OP_RETURN pinned to it (RC4 key = its txid) and copy
     the destinations-then-OP_RETURN outputs verbatim.
  4. Build the reveal: vin[0] = the commit's change output (signed by Core),
     vin[1] = the commit output (taproot script path, signed here with the
     ephemeral reveal key); then our own change back to the wallet.
  5. Package-validate [commit, reveal] with testmempoolaccept, then broadcast
     commit + reveal (CPFP) — or with --dry-run stop and print the hex.

Key custody stays in Bitcoin Core; we only sign the ephemeral script-path input.
Numeric (free) assets use the commit's own change as the source. Named assets
burn 0.5 XCP, so they instead use a wallet UTXO whose address already holds the
XCP as the source (reveal vin[0]); the commit is sized to fund the reveal and
the XCP UTXO is locked so funding never spends it.
"""

from __future__ import annotations

import math
import mimetypes
import os
import random
import sys

from .. import builder, tap
from ..bitcoind import BitcoindClient, BitcoindError
from ..config import Config, RESERVED_ASSETS
from ..counterparty import CounterpartyClient, CounterpartyError
from .wallet import _wallet_addresses

COIN = 100_000_000
DUST = 546                     # conservative dust floor for our outputs
NUMERIC_MIN = 26 ** 12 + 1     # Counterparty numeric-asset range
NUMERIC_MAX = 2 ** 64 - 1
NAMED_ISSUANCE_FEE_XCP = 50_000_000   # 0.5 XCP burned to register a named asset


def guess_content_type(path: str) -> bytes:
    ct, _ = mimetypes.guess_type(path)
    return (ct or "application/octet-stream").encode()


def random_numeric_asset() -> str:
    return "A" + str(random.randint(NUMERIC_MIN, NUMERIC_MAX))


def _extract_issuance_outputs(decoded: dict):
    """From Counterparty's composed tx, return (dest_outs, op_return_spk).

    Counterparty's parse rule (gettxinfo.py): destinations come *before* the
    OP_RETURN data output, change comes *after*. So we copy every output up to
    and excluding the OP_RETURN (the transfer destinations, in order) and the
    OP_RETURN itself, and drop everything after it (Counterparty's change) —
    we compute our own change.
    """
    dest_outs: list[tap.TxOut] = []
    for o in decoded["vout"]:
        spk = o["scriptPubKey"]
        h = bytes.fromhex(spk["hex"])
        if spk.get("type") == "nulldata":
            return dest_outs, h
        dest_outs.append(tap.TxOut(int(round(o["value"] * COIN)), h))
    raise CounterpartyError("composed issuance has no OP_RETURN output")


def _is_p2pkh(spk: bytes) -> bool:
    """True for a legacy P2PKH scriptPubKey (OP_DUP OP_HASH160 <20> OP_EQUALVERIFY
    OP_CHECKSIG) — the 1... addresses Counterwallet/Electrum-v1 wallets use."""
    return len(spk) == 25 and spk[:3] == b"\x76\xa9\x14" and spk[-2:] == b"\x88\xac"


def _estimate_reveal_vsize(tx: tap.Tx, leaf: bytes, control_block: bytes,
                           source_spk: bytes | None = None) -> int:
    """Exact vsize by filling placeholders of the real signature sizes.

    vin[1] is always the taproot script-path spend (witness). vin[0] is the
    source: model it by script type so the fee is right whether the source is
    segwit/taproot (signature in the weight-discounted WITNESS) or a legacy
    P2PKH `1...` owner address (signature in the full-weight SCRIPTSIG) — the
    latter is the norm for a Counterwallet reinscription."""
    tx.vin[1].witness = [b"\x00" * 64, leaf, control_block]     # script-path
    if source_spk is not None and _is_p2pkh(source_spk):
        tx.vin[0].witness = []
        tx.vin[0].script_sig = b"\x00" * 107   # ~ push+sig(72) + push+pubkey(33)
    else:
        tx.vin[0].witness = [b"\x00" * 65]                      # P2TR keypath sig
    base = len(tx.serialize(force_witness=False))
    total = len(tx.serialize(force_witness=True))
    weight = base * 3 + total
    return math.ceil(weight / 4)


class InscribeError(Exception):
    pass


def cmd_inscribe(
    config: Config,
    wallet: str,
    file_path: str,
    asset: str | None = None,
    fee_rate: float = 5.0,
    commit_fee_rate: float | None = None,
    destination: str | None = None,
    supply: int = 1,
    divisible: bool = False,
    lock: bool = False,
    reinscribe: bool = False,
    dry_run: bool = False,
    fund_from_address: str | None = None,
    fund_from_input: str | None = None,
) -> int:
    btc = BitcoindClient(config)
    cp = CounterpartyClient(config)
    commit_fee_rate = commit_fee_rate or fee_rate

    if not os.path.isfile(file_path):
        print(f"file not found: {file_path}", file=sys.stderr)
        return 1
    with open(file_path, "rb") as fh:
        body = fh.read()
    content_type = guess_content_type(file_path)

    # --fund-from-address / --fund-from-input choose WHICH of the wallet's coins
    # pay for the commit, for ANY inscription type. The funding coin is decoupled
    # from the issuance source (the commit's change): for a reinscription the
    # change goes to the asset owner, for a named mint to the XCP-holding issuer,
    # for a numeric mint to a fresh address — none of which need to hold BTC
    # themselves, since the chosen funding coins cover the commit. Both flags may
    # be combined (their coins are unioned); every pinned coin must belong to the
    # --name wallet, since Core signs the commit with it.
    try:
        fund_inputs = _collect_fund_inputs(btc, wallet, fund_from_address, fund_from_input)
    except InscribeError as e:
        print(str(e), file=sys.stderr)
        return 1

    change_addr = _wallet_change_address(btc, wallet)
    change_spk = _addr_to_spk(btc, change_addr)

    # Resolve the inscription and build/sign the commit. Three shapes:
    #   - reinscription: attach to an EXISTING asset you own; NO Counterparty
    #     message. The commit's change is routed to the asset owner's address so
    #     the reveal spends from it, proving issuance rights on-chain.
    #   - numeric (free): source is the commit's own change output.
    #   - named (0.5 XCP burn): source must already hold >=0.5 XCP.
    named = False
    if reinscribe:
        if asset is None:
            print("--reinscribe requires --asset (the existing asset to attach the counter to)",
                  file=sys.stderr)
            return 1
        if destination is not None:
            print("--destination is not allowed with --reinscribe: a reinscription attaches a "
                  "counter to an existing asset and moves no Counterparty asset or ownership.",
                  file=sys.stderr)
            return 1
        # Resolve the asset (try the name as given, then upper-cased for named).
        asset_info = cp.get_asset(asset) or cp.get_asset(asset.upper())
        if not asset_info:
            print(f"asset {asset} does not exist — --reinscribe attaches to an EXISTING asset "
                  f"whose issuance rights you hold; omit --reinscribe to create a new asset.",
                  file=sys.stderr)
            return 1
        # Human-readable name for the envelope tag; canonical name for reporting.
        target_name = asset_info.get("asset_longname") or asset_info.get("asset") or asset
        asset = asset_info.get("asset") or target_name
        if asset in RESERVED_ASSETS:
            print(f"cannot reinscribe onto reserved asset {asset}", file=sys.stderr)
            return 1
        # Use the CURRENT owner (issuance-rights holder), not `issuer` (the
        # original, immutable creator): ownership moves on transfer, and Rare
        # Pepe-style assets are usually acquired, not self-issued. This must
        # match the indexer, which authorises `issuer_at_height` = the current
        # owner as of the block.
        owner = asset_info.get("owner") or asset_info.get("issuer")
        if not owner:
            print(f"could not determine the issuance-rights owner of {asset}", file=sys.stderr)
            return 1
        if owner not in set(_wallet_addresses(btc, wallet)):
            print(f"this wallet does not hold the issuance rights of {asset} (owner {owner}); "
                  f"you can only reinscribe assets whose issuance rights you hold.",
                  file=sys.stderr)
            return 1
        insc = builder.build_inscription(content_type, body, asset=target_name.encode())
        try:
            built = _prepare_reinscribe(btc, wallet, insc, owner, commit_fee_rate,
                                        change_spk, fund_inputs=fund_inputs)
        except (BitcoindError, CounterpartyError, InscribeError) as e:
            print(f"reinscribe build failed: {e}", file=sys.stderr)
            return 1
    else:
        named = asset is not None
        if named:
            asset = asset.upper()
            if asset in RESERVED_ASSETS:
                print(f"cannot inscribe onto reserved asset {asset}", file=sys.stderr)
                return 1
            if cp.get_asset(asset):
                print(f"asset {asset} already exists; use --reinscribe to attach to an asset "
                      f"whose issuance rights you hold, or pick an unregistered name.",
                      file=sys.stderr)
                return 1
        else:
            asset = random_numeric_asset()
        quantity = supply * COIN if divisible else supply
        # Name the asset in the envelope too, so the counter is envelope-bound
        # (the indexer prefers the envelope's asset, matched to this same-tx
        # issuance) rather than relying solely on the OP_RETURN issuance.
        insc = builder.build_inscription(content_type, body, asset=asset.encode())
        try:
            if named:
                built = _prepare_named(btc, cp, wallet, insc, asset, quantity,
                                       divisible, destination, fee_rate,
                                       commit_fee_rate, change_spk, lock,
                                       fund_inputs=fund_inputs)
            else:
                built = _prepare_numeric(btc, cp, wallet, insc, asset, quantity,
                                         divisible, destination, fee_rate,
                                         commit_fee_rate, change_spk, lock,
                                         fund_inputs=fund_inputs)
        except (BitcoindError, CounterpartyError, InscribeError) as e:
            print(f"inscribe build failed: {e}", file=sys.stderr)
            return 1
    if built is None:
        return 1

    commit = built["commit"]
    commit_txid = commit["txid"]
    commit_out = commit["commit_out"]
    source_txid = built["source_txid"]
    source_vout = built["source_vout"]
    source_value = built["source_value"]
    source_spk = built["source_spk"]
    dest_outs = built["dest_outs"]
    op_return_spk = built["op_return_spk"]
    reveal_vsize = built["reveal_vsize"]

    # 4. reveal: vin[0] = source (Core-signed), vin[1] = commit output (we sign
    #    the taproot script path); exact fee from vsize -> our change.
    reveal_fee = math.ceil(reveal_vsize * fee_rate)
    dest_value = sum(o.value for o in dest_outs)
    total_in = source_value + commit_out["value"]
    change_value = total_in - reveal_fee - dest_value
    if change_value < DUST:
        print("error: inputs too small to cover the reveal fee + a non-dust change "
              "output; fund the wallet with a bit more BTC", file=sys.stderr)
        return 1

    def build_reveal(cv: int) -> tap.Tx:
        # Reinscriptions carry no Counterparty message, so there is no OP_RETURN
        # and no destination outputs — just the envelope reveal + our change.
        outs = list(dest_outs)
        if op_return_spk is not None:
            outs.append(tap.TxOut(0, op_return_spk))
        outs.append(tap.TxOut(cv, change_spk))
        return tap.Tx(
            vin=[tap.TxIn(source_txid, source_vout),
                 tap.TxIn(commit_txid, commit_out["vout"])],
            vout=outs,
        )

    reveal = build_reveal(change_value)

    # 5. sign: Core signs vin[0] (the change/source), we sign vin[1] (commit)
    try:
        reveal_hex = _sign_reveal(
            btc, wallet, reveal, insc,
            prevouts=[(source_value, source_spk),
                      (commit_out["value"], insc.commit_script_pubkey)],
        )
    except (BitcoindError, InscribeError) as e:
        print(f"reveal signing failed: {e}", file=sys.stderr)
        return 1

    commit_hex = commit["hex"]
    reveal_txid = reveal.txid()
    commit_fee = _commit_fee(btc, commit_hex)

    # Validate BOTH transactions as a package without broadcasting. This proves
    # the taproot script-path signature (our self-signed reveal input) and the
    # Core-signed source input are consensus-valid before any funds move.
    try:
        checks = btc._call("testmempoolaccept", [[commit_hex, reveal_hex]])
    except BitcoindError as e:
        print(f"testmempoolaccept failed to run: {e}", file=sys.stderr)
        checks = []
    all_ok = bool(checks) and all(c.get("allowed") for c in checks)

    # report
    if reinscribe:
        kind = " (reinscription)"
    elif named:
        kind = " (named)"
    else:
        kind = " (numeric, free)"
    print(f"asset            : {asset}{kind}")
    print(f"content_type     : {content_type.decode(errors='replace')}  ({len(body)} bytes)")
    if not reinscribe:
        print(f"supply           : {supply}{' divisible' if divisible else ''}"
              f"{' (LOCKED)' if lock else ''}")
    print(f"commit address   : {insc.commit_address}")
    print(f"commit txid      : {commit_txid}")
    print(f"reveal txid      : {reveal_txid}")
    print(f"commit fee       : {commit_fee} sat")
    print(f"reveal fee       : {reveal_fee} sat  (~{reveal_vsize} vB @ {fee_rate} sat/vB)")
    total = commit_fee + reveal_fee
    print(f"total BTC fees   : {total} sat ({total / COIN:.8f} BTC)")
    if named:
        print("XCP cost         : 0.5 XCP (named-asset issuance burn)")
    if reinscribe:
        print("Counterparty     : none (pure inscription; authorised by spending an "
              "input from the owner address)")
    if fund_from_address or fund_from_input:
        srcs = ", ".join(s for s in (fund_from_address, fund_from_input) if s)
        print(f"funded from      : {srcs}")

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

    # broadcast commit then reveal (reveal is a CPFP child of the commit)
    try:
        ctxid = btc._call("sendrawtransaction", [commit_hex])
        rtxid = btc._call("sendrawtransaction", [reveal_hex])
    except BitcoindError as e:
        print(f"broadcast failed: {e}", file=sys.stderr)
        print(f"commit_raw: {commit_hex}\nreveal_raw: {reveal_hex}", file=sys.stderr)
        return 1
    print(f"\nbroadcast OK\n  commit: {ctxid}\n  reveal: {rtxid}")
    if reinscribe:
        print("the counter mints once the reveal confirms and the indexer verifies the "
              "owner signed it.")
    else:
        print("the counter mints once the reveal confirms and Counterparty processes the issuance.")
    return 0


def _addr_to_spk(btc: BitcoindClient, address: str) -> bytes:
    return bytes.fromhex(btc._call("validateaddress", [address])["scriptPubKey"])


def _address_utxos(btc: BitcoindClient, wallet: str, address: str) -> list[dict]:
    """Spendable UTXOs (as {txid, vout}) held by `address` in `wallet`.

    Used by --fund-from-address so the commit is funded from a specific address's
    coins (e.g. one that actually holds BTC, when the asset owner address is
    empty). include_unsafe=True so still-unconfirmed self-change is usable."""
    utxos = btc.wallet_call(wallet, "listunspent", [0, 9999999, [address], True])
    return [{"txid": u["txid"], "vout": u["vout"]} for u in utxos]


def _collect_fund_inputs(btc: BitcoindClient, wallet: str,
                         fund_from_address: str | None,
                         fund_from_input: str | None) -> list[dict] | None:
    """Resolve --fund-from-input and --fund-from-address into the deduped list of
    coins that must fund the commit, or None to let Core select. The two may be
    combined (union); every coin must be spendable by `wallet` since Core signs
    the commit with it. Raises InscribeError on a malformed input or an address
    with no spendable UTXO."""
    inputs: list[dict] = []
    if fund_from_input:
        parts = fund_from_input.rsplit(":", 1)
        if len(parts) != 2 or not parts[1].isdigit():
            raise InscribeError(f"--fund-from-input must be TXID:VOUT, got {fund_from_input!r}")
        inputs.append({"txid": parts[0], "vout": int(parts[1])})
    if fund_from_address:
        addr_utxos = _address_utxos(btc, wallet, fund_from_address)
        if not addr_utxos:
            raise InscribeError(f"no spendable UTXO at {fund_from_address} in wallet "
                                f"{wallet!r} to fund the commit — pick an address that "
                                f"holds BTC.")
        inputs.extend(addr_utxos)
    seen: set[tuple[str, int]] = set()
    deduped = [u for u in inputs
               if (u["txid"], u["vout"]) not in seen
               and not seen.add((u["txid"], u["vout"]))]
    return deduped or None


def _wallet_change_address(btc: BitcoindClient, wallet: str) -> str:
    """A change address the wallet controls.

    Taproot wallets (from `wallet create` or a BIP39 restore) have an active
    ranged descriptor, so Core hands us a fresh bech32m change address. Legacy
    Counterwallet/Electrum wallets are imported as flat single-key WIF
    descriptors with no active/internal descriptor, so `getrawchangeaddress`
    fails with "no available keys"; fall back to an address the wallet already
    controls (its key is present, so the change stays spendable)."""
    try:
        return btc.wallet_call(wallet, "getrawchangeaddress", ["bech32m"])
    except BitcoindError:
        addrs = _wallet_addresses(btc, wallet)
        if not addrs:
            raise BitcoindError(
                f"wallet {wallet!r} cannot produce a change address and has no known "
                f"addresses to reuse — fund it, or inscribe from a wallet made with "
                f"`counters wallet create`."
            )
        return addrs[0]


def _find_xcp_address(btc, cp, wallet, min_xcp=NAMED_ISSUANCE_FEE_XCP):
    """Return a wallet address holding >= min_xcp XCP, else None.

    XCP balances are address-level. The commit funds from anywhere in the wallet
    and routes its change to this address, making it the named issuance's
    source/issuer — so the address does NOT need its own BTC UTXO. We therefore
    enumerate the wallet's full address set (received + unspent), exactly like
    `balance` does; scanning only `listunspent` would miss XCP held at an address
    with no live UTXO."""
    for addr in _wallet_addresses(btc, wallet):
        try:
            if cp.get_xcp_balance(addr) >= min_xcp:
                return addr
        except CounterpartyError:
            continue
    return None


def _compose_and_size(btc, cp, insc, commit, asset, quantity, divisible,
                      destination, change_spk, lock=False):
    """Shared tail: the commit's change output is the issuance source. Compose the
    OP_RETURN pinned to it, copy the destination outputs, and size the reveal."""
    change_out = commit["change_out"]
    source = change_out["address"]
    transfer_destination = (destination if destination and destination != source
                            else None)
    inputs_set = (f"{commit['txid']}:{change_out['vout']}:{change_out['value']}:"
                  f"{change_out['spk'].hex()}")
    composed = cp.compose_issuance(
        source=source, asset=asset, quantity=quantity, divisible=divisible,
        inputs_set=inputs_set, transfer_destination=transfer_destination,
        lock=lock,
    )
    decoded = btc._call("decoderawtransaction", [composed["rawtransaction"]])
    dest_outs, op_return_spk = _extract_issuance_outputs(decoded)
    struct = tap.Tx(
        vin=[tap.TxIn(commit["txid"], change_out["vout"]),
             tap.TxIn(commit["txid"], commit["commit_out"]["vout"])],
        vout=dest_outs + [tap.TxOut(0, op_return_spk), tap.TxOut(0, change_spk)],
    )
    reveal_vsize = _estimate_reveal_vsize(struct, insc.leaf, insc.control_block,
                                          source_spk=change_out["spk"])
    return {
        "commit": commit,
        "source_txid": commit["txid"],
        "source_vout": change_out["vout"],
        "source_value": change_out["value"],
        "source_spk": change_out["spk"],
        "dest_outs": dest_outs,
        "op_return_spk": op_return_spk,
        "reveal_vsize": reveal_vsize,
    }


def _prepare_numeric(btc, cp, wallet, insc, asset, quantity, divisible,
                     destination, fee_rate, commit_fee_rate, change_spk, lock=False,
                     fund_inputs=None):
    """Build the commit (change -> a fresh wallet address); that change is the
    source for a free numeric asset. `fund_inputs` pins the funding coins."""
    commit = _build_commit(btc, wallet, insc.commit_address, DUST, commit_fee_rate,
                           fund_inputs=fund_inputs)
    return _compose_and_size(btc, cp, insc, commit, asset, quantity, divisible,
                             destination, change_spk, lock)


def _prepare_named(btc, cp, wallet, insc, asset, quantity, divisible,
                   destination, fee_rate, commit_fee_rate, change_spk, lock=False,
                   fund_inputs=None):
    """Route the commit's change back to a wallet address holding >=0.5 XCP, so
    that address is the issuance source/issuer and pays the name-registration
    burn (XCP balances are address-level, so spending its UTXO is fine).
    `fund_inputs` pins which coins pay — the XCP address needs no BTC of its own."""
    xcp_addr = _find_xcp_address(btc, cp, wallet)
    if xcp_addr is None:
        print(f"no wallet address holds the {NAMED_ISSUANCE_FEE_XCP / COIN:.1f} XCP "
              f"required to register a named asset. Fund a counter address with "
              f"XCP first (`counters wallet --name {wallet} receive`), then retry.",
              file=sys.stderr)
        return None
    commit = _build_commit(btc, wallet, insc.commit_address, DUST,
                           commit_fee_rate, change_address=xcp_addr,
                           fund_inputs=fund_inputs)
    return _compose_and_size(btc, cp, insc, commit, asset, quantity, divisible,
                             destination, change_spk, lock)


def _prepare_reinscribe(btc, wallet, insc, owner_addr, commit_fee_rate, change_spk,
                        fund_inputs=None):
    """Reinscription: NO Counterparty message. Route the commit's change to the
    asset OWNER address so the reveal's vin[0] spends from it — that signature
    proves the issuance rights on-chain. The reveal therefore has no OP_RETURN
    and no destination outputs, just the envelope reveal + change to the wallet.

    `fund_inputs` (a list of {txid, vout}) pins which of the wallet's coins pay
    for the commit — e.g. a BTC-holding address when the owner address is empty.
    Change still lands on owner_addr, so ownership is proven regardless of which
    coin funded the commit."""
    commit = _build_commit(btc, wallet, insc.commit_address, DUST,
                           commit_fee_rate, change_address=owner_addr,
                           fund_inputs=fund_inputs)
    change_out = commit["change_out"]
    struct = tap.Tx(
        vin=[tap.TxIn(commit["txid"], change_out["vout"]),
             tap.TxIn(commit["txid"], commit["commit_out"]["vout"])],
        vout=[tap.TxOut(0, change_spk)],
    )
    reveal_vsize = _estimate_reveal_vsize(struct, insc.leaf, insc.control_block,
                                          source_spk=change_out["spk"])
    return {
        "commit": commit,
        "source_txid": commit["txid"],
        "source_vout": change_out["vout"],
        "source_value": change_out["value"],
        "source_spk": change_out["spk"],
        "dest_outs": [],
        "op_return_spk": None,
        "reveal_vsize": reveal_vsize,
    }


def _build_commit(btc, wallet, commit_address, commit_value, commit_fee_rate,
                  change_address=None, fund_inputs=None):
    """Fund + sign a tx paying `commit_value` to `commit_address`. Core adds a
    change output (to `change_address`, or a fresh wallet address) which we
    return so the reveal can use it as its source. For named mints the caller
    passes the XCP-holding address so the change keeps the issuance source.

    `fund_inputs` (a list of {txid, vout}), if given, pins the exact coins that
    fund the commit (add_inputs=False), so a caller can pay from a specific
    address's or UTXO's coins in `wallet`."""
    prevouts = list(fund_inputs) if fund_inputs else []
    raw = btc.wallet_call(
        wallet, "createrawtransaction",
        [prevouts, [{commit_address: f"{commit_value / COIN:.8f}"}]],
    )
    # Pin the change address: this output becomes the reveal's source.
    # include_unsafe lets the commit chain off the wallet's own still-unconfirmed
    # funds, which is safe here since we only spend our own UTXOs.
    if change_address is None:
        change_address = _wallet_change_address(btc, wallet)
    fund_opts = {"fee_rate": commit_fee_rate, "include_unsafe": True,
                 "changeAddress": change_address}
    if fund_inputs:
        fund_opts["add_inputs"] = False   # fund from ONLY the pinned coins
    funded = btc.wallet_call(wallet, "fundrawtransaction", [raw, fund_opts])
    signed = btc.wallet_call(wallet, "signrawtransactionwithwallet", [funded["hex"]])
    if not signed.get("complete"):
        raise BitcoindError(f"commit not fully signed: {signed.get('errors')}")

    commit_hex = signed["hex"]
    dec = btc._call("decoderawtransaction", [commit_hex])
    commit_out = change_out = None
    for o in dec["vout"]:
        spk = o["scriptPubKey"]
        info = {
            "vout": o["n"],
            "value": int(round(o["value"] * COIN)),
            "spk": bytes.fromhex(spk["hex"]),
            "address": spk.get("address"),
        }
        if spk.get("address") == commit_address:
            commit_out = info
        else:
            change_out = info
    if commit_out is None:
        raise BitcoindError("commit output not found after funding")
    if change_out is None:
        raise BitcoindError(
            "commit produced no change output to use as the reveal source; "
            "fund the wallet with a slightly larger UTXO"
        )
    return {"hex": commit_hex, "txid": dec["txid"],
            "commit_out": commit_out, "change_out": change_out}


def _commit_fee(btc, commit_hex):
    dec = btc._call("decoderawtransaction", [commit_hex])
    vin_total = 0
    for vi in dec["vin"]:
        prev = btc.get_raw_transaction(vi["txid"], True)
        vin_total += int(round(prev["vout"][vi["vout"]]["value"] * COIN))
    vout_total = sum(int(round(o["value"] * COIN)) for o in dec["vout"])
    return vin_total - vout_total


def _sign_reveal(btc, wallet, reveal: tap.Tx, insc, prevouts):
    """Core signs vin[0] (its wallet UTXO); we sign vin[1] (the commit output)
    via the taproot script path with the reveal key."""
    unsigned = reveal.serialize().hex()
    # Both inputs come from the (still unbroadcast) commit, so Core needs the
    # prevout scriptPubKey+amount for each to sign vin[0] and compute the
    # taproot sighash that commits to every input.
    prevtxs = [
        {
            "txid": reveal.vin[0].txid,
            "vout": reveal.vin[0].vout,
            "scriptPubKey": prevouts[0][1].hex(),
            "amount": f"{prevouts[0][0] / COIN:.8f}",
        },
        {
            "txid": reveal.vin[1].txid,
            "vout": reveal.vin[1].vout,
            "scriptPubKey": insc.commit_script_pubkey.hex(),
            "amount": f"{prevouts[1][0] / COIN:.8f}",
        },
    ]
    signed = btc.wallet_call(wallet, "signrawtransactionwithwallet", [unsigned, prevtxs])
    dec = btc._call("decoderawtransaction", [signed["hex"]])
    # Copy whatever Core produced for vin[0], regardless of the source's script
    # type: native segwit / taproot (bc1q/bc1p change from a `wallet create`
    # wallet) is signed into the WITNESS; a legacy P2PKH `1...` owner address
    # (e.g. a Counterwallet reinscription source) is signed into the SCRIPTSIG;
    # nested segwit (3...) uses both. Take both so every source type works.
    vin0 = dec["vin"][0]
    vin0_witness = vin0.get("txinwitness") or []
    vin0_script_sig = (vin0.get("scriptSig") or {}).get("hex", "")
    if not vin0_witness and not vin0_script_sig:
        errors = signed.get("errors")
        raise InscribeError(
            "Core did not sign the source input (vin[0]) — the wallet may not hold "
            "that address's key, or the prevout is wrong"
            + (f"; {errors}" if errors else "")
        )
    reveal.vin[0].witness = [bytes.fromhex(w) for w in vin0_witness]
    reveal.vin[0].script_sig = bytes.fromhex(vin0_script_sig) if vin0_script_sig else b""

    sighash = tap.taproot_script_path_sighash(
        reveal, input_index=1,
        prevout_values=[prevouts[0][0], prevouts[1][0]],
        prevout_scripts=[prevouts[0][1], prevouts[1][1]],
        tapleaf=insc.merkle_root,
    )
    sig = tap.schnorr_sign(sighash, insc.reveal_seckey, aux_rand=os.urandom(32))
    reveal.vin[1].witness = [sig, insc.leaf, insc.control_block]
    return reveal.serialize().hex()
