"""`counters wallet send` — transfer a counter (its Counterparty asset), or BTC.

Argument order mirrors `ord wallet send`: DESTINATION first, then ASSET, then
AMOUNT (`send <ADDRESS> <ASSET> <AMOUNT>`). `BTC` in the ASSET slot means a
plain bitcoin payment (`send <ADDRESS> BTC <AMOUNT>`); anything else is a
counter transfer.

A counter is owned by whoever holds its Counterparty asset balance, so
transferring ownership is a plain Counterparty *send*: compose the OP_RETURN
via Core, have the wallet (which holds the keys) sign it, validate against the
mempool, and broadcast. Custody stays in Bitcoin Core — we never touch keys.

Counterparty balances are per-address, so the send is sourced from a single
wallet address that holds enough of the asset (and enough BTC to pay the fee).
"""

from __future__ import annotations

import sys
from decimal import Decimal, InvalidOperation

from ..bitcoind import COIN, BitcoindClient, BitcoindError
from ..config import Config, RESERVED_ASSETS
from ..counterparty import CounterpartyClient, CounterpartyError
from .wallet import _wallet_addresses


def _to_raw_quantity(amount: str, divisible: bool) -> int:
    """Human amount -> Counterparty raw units: sats for a divisible asset,
    whole units otherwise. Raises ValueError on bad input."""
    try:
        dec = Decimal(str(amount))
    except InvalidOperation:
        raise ValueError(f"invalid amount {amount!r}")
    if dec <= 0:
        raise ValueError("amount must be positive")
    if divisible:
        return int((dec * COIN).to_integral_value())
    if dec != dec.to_integral_value():
        raise ValueError(f"{amount} is fractional but the asset is indivisible")
    return int(dec)


def _fmt_raw(raw: int, divisible: bool) -> str:
    """Raw units -> human string (inverse of _to_raw_quantity, for display)."""
    if not divisible:
        return str(raw)
    return format((Decimal(raw) / COIN).normalize(), "f")


def _to_btc_amount(amount: str) -> Decimal:
    """Human BTC amount -> Decimal BTC. Raises ValueError on a non-positive or
    sub-satoshi value (Bitcoin amounts have 8 decimal places)."""
    try:
        dec = Decimal(str(amount))
    except InvalidOperation:
        raise ValueError(f"invalid amount {amount!r}")
    if dec <= 0:
        raise ValueError("amount must be positive")
    sats = dec * COIN
    if sats != sats.to_integral_value():
        raise ValueError(f"{amount} BTC is finer than one satoshi")
    return dec


def _fmt_btc(value) -> str:
    """BTC amount -> plain 8-decimal string, without exponent or trailing zeros."""
    dec = Decimal(str(value)).quantize(Decimal("0.00000001"))
    return format(dec, "f").rstrip("0").rstrip(".") or "0"


def _find_source(btc, cp, wallet: str, asset: str, need_raw: int):
    """A wallet address holding the asset (sends are per-address). Returns the
    first address with >= need_raw; otherwise the richest address found.
    Result is (address_or_None, raw_balance)."""
    best = (None, 0)
    for addr in _wallet_addresses(btc, wallet):
        try:
            rows = cp.get_address_balances(addr)
        except CounterpartyError:
            continue
        for r in rows:
            if r.get("asset") == asset or r.get("asset_longname") == asset:
                q = int(r.get("quantity") or 0)
                if q >= need_raw:
                    return addr, q
                if q > best[1]:
                    best = (addr, q)
    return best


_ORDER_HINT = "note: the argument order is  send <ADDRESS> <ASSET> <AMOUNT>"


def _is_valid_address(btc, value: str) -> bool:
    """True if bitcoind considers `value` a valid Bitcoin address. Used to fail
    fast on a bad destination and to detect a swapped ADDRESS/ASSET/AMOUNT arg."""
    try:
        return bool(btc._call("validateaddress", [value]).get("isvalid"))
    except (BitcoindError, KeyError, TypeError):
        return False


def cmd_send(
    config: Config,
    wallet: str,
    destination: str,
    asset: str,
    amount: str,
    fee_rate: float | None = None,
    dry_run: bool = False,
) -> int:
    # BTC is not a Counterparty asset — it takes the plain-bitcoin path.
    if asset.upper() == "BTC":
        return cmd_send_btc(
            config, wallet, destination, amount, fee_rate=fee_rate, dry_run=dry_run
        )

    btc = BitcoindClient(config)
    cp = CounterpartyClient(config)

    # Order is ADDRESS ASSET AMOUNT: validate the destination first, so a
    # swapped argument (e.g. an asset name where the address belongs) fails
    # fast with a clear message + order hint instead of a confusing downstream
    # "unknown asset" / "invalid amount".
    if not _is_valid_address(btc, destination):
        print(f"destination {destination!r} is not a valid Bitcoin address", file=sys.stderr)
        print(_ORDER_HINT, file=sys.stderr)
        return 1

    if asset in RESERVED_ASSETS:
        print(f"{asset} is a reserved asset, not a counter", file=sys.stderr)
        return 1

    info = cp.get_asset(asset) or cp.get_asset(asset.upper())
    if not info:
        print(f"unknown asset {asset!r} (Counterparty has no record)", file=sys.stderr)
        if _is_valid_address(btc, asset):
            print(_ORDER_HINT, file=sys.stderr)
        return 1
    asset = info.get("asset") or asset          # canonical name Core expects
    divisible = bool(info.get("divisible"))

    try:
        need = _to_raw_quantity(amount, divisible)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        if _is_valid_address(btc, amount):
            print(_ORDER_HINT, file=sys.stderr)
        return 1

    source, have = _find_source(btc, cp, wallet, asset, need)
    if source is None or have <= 0:
        print(f"wallet {wallet!r} holds no {asset}", file=sys.stderr)
        return 1
    if have < need:
        print(
            f"insufficient balance: need {_fmt_raw(need, divisible)} {asset}, "
            f"largest single-address balance is {_fmt_raw(have, divisible)} "
            f"(Counterparty sends cannot span addresses)",
            file=sys.stderr,
        )
        return 1

    try:
        composed = cp.compose_send(source, asset, need, destination, sat_per_vbyte=fee_rate)
    except CounterpartyError as e:
        msg = str(e)
        print(f"compose failed: {msg}", file=sys.stderr)
        if "No UTXOs" in msg or "inputs_set" in msg:
            print(f"hint: {source} holds {asset} but has no spendable BTC. A send is "
                  f"sourced from the asset-holding address, so fund it with a little "
                  f"BTC (for the tx fee), then retry.", file=sys.stderr)
        return 1
    rawtx = composed.get("rawtransaction")
    if not rawtx:
        print(f"compose returned no rawtransaction: {composed}", file=sys.stderr)
        return 1

    print(f"send {_fmt_raw(need, divisible)} {asset}")
    print(f"  from      : {source}")
    print(f"  to        : {destination}")
    if fee_rate is not None:
        print(f"  fee rate  : {fee_rate} sat/vB")
    return _sign_and_broadcast(btc, wallet, source, rawtx, dry_run)


def cmd_send_btc(
    config: Config,
    wallet: str,
    destination: str,
    amount: str,
    fee_rate: float | None = None,
    dry_run: bool = False,
) -> int:
    """`send <ADDRESS> BTC <AMOUNT>` — a plain bitcoin payment, no Counterparty
    involved. Bitcoin Core selects the inputs across the whole wallet, signs,
    and broadcasts. Counterparty balances are bound to *addresses*, not to the
    UTXOs sitting at them, so paying out BTC never moves a counter."""
    btc = BitcoindClient(config)

    if not _is_valid_address(btc, destination):
        print(f"destination {destination!r} is not a valid Bitcoin address", file=sys.stderr)
        print(_ORDER_HINT, file=sys.stderr)
        return 1

    try:
        want = _to_btc_amount(amount)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        if _is_valid_address(btc, amount):
            print(_ORDER_HINT, file=sys.stderr)
        return 1

    # Pre-flight against the wallet's spendable balance, so the common mistake
    # (an amount typed in satoshis) reports what the wallet actually holds
    # rather than a bare "Insufficient funds" from Core.
    balances = btc.wallet_call(wallet, "getbalances")
    spendable = Decimal(str((balances.get("mine") or {}).get("trusted", 0)))
    if want > spendable:
        print(f"insufficient BTC: want {_fmt_btc(want)}, wallet {wallet!r} has "
              f"{_fmt_btc(spendable)} spendable — note the amount is in BTC, "
              f"not satoshis", file=sys.stderr)
        return 1

    print(f"send {_fmt_btc(want)} BTC")
    print(f"  from      : wallet {wallet!r} (inputs picked by Bitcoin Core)")
    print(f"  to        : {destination}")
    if fee_rate is not None:
        print(f"  fee rate  : {fee_rate} sat/vB")

    # `send` with add_to_wallet=false signs but neither broadcasts nor records
    # the tx — exactly the dry-run semantics the other commands have.
    options = {"add_to_wallet": False} if dry_run else {}

    def _send(opts):
        return btc.wallet_call(
            wallet, "send", [{destination: _fmt_btc(want)}, None, "unset", fee_rate, opts]
        )

    try:
        result = _send(options)
    except BitcoindError as e:
        # Core builds change at the node's -changetype, which fails outright on
        # a wallet with no descriptor of that type (e.g. a taproot-only wallet
        # on a node running addresstype=legacy). Retry with a type the wallet
        # can actually derive.
        change_type = _change_type(btc, wallet) if "change address" in str(e) else None
        if not change_type:
            raise
        print(f"  change    : {change_type} (the node's default change type is not "
              f"in this wallet)")
        result = _send({**options, "change_type": change_type})

    if dry_run:
        tx_hex = result.get("hex")
        if not tx_hex:
            print(f"send returned no transaction: {result}", file=sys.stderr)
            return 1
        ok, check = _check_mempool(btc, tx_hex)
        _print_fee(check.get("fees", {}).get("base"), check.get("vsize"))
        print(f"\n[dry-run] not broadcast. raw tx:\n{tx_hex}")
        return 0 if ok else 1

    txid = result.get("txid")
    if not txid:
        print(f"send did not broadcast: {result}", file=sys.stderr)
        return 1
    try:
        info = btc.wallet_call(wallet, "gettransaction", [txid])
        _print_fee(abs(Decimal(str(info.get("fee", 0)))), None)
    except (BitcoindError, InvalidOperation):
        pass
    print(f"\nbroadcast: {txid}")
    return 0


# Change-output types Core accepts, best first — this wallet is taproot-first,
# so prefer taproot change and fall back down the ladder.
_CHANGE_TYPES = (("tr(", "bech32m"), ("wpkh(", "bech32"),
                 ("sh(wpkh(", "p2sh-segwit"), ("pkh(", "legacy"))


def _change_type(btc, wallet: str) -> str | None:
    """A change type this wallet's own descriptors can produce, or None if we
    cannot tell. Read from the active *internal* (change) descriptors."""
    try:
        info = btc.wallet_call(wallet, "listdescriptors", [])
    except BitcoindError:
        return None
    descs = [d.get("desc", "") for d in info.get("descriptors", [])
             if d.get("active") and d.get("internal")]
    for prefix, name in _CHANGE_TYPES:
        if any(d.startswith(prefix) for d in descs):
            return name
    return None


def _print_fee(fee_btc, vsize) -> None:
    """Report the fee actually paid, with the effective rate when vsize is known."""
    if fee_btc is None:
        return
    sats = int((Decimal(str(fee_btc)) * COIN).to_integral_value())
    rate = f", {sats / vsize:.2f} sat/vB" if vsize else ""
    print(f"  fee       : {_fmt_btc(fee_btc)} BTC ({sats} sat{rate})")


def _check_mempool(btc, tx_hex: str) -> tuple[bool, dict]:
    """Ask bitcoind whether the signed tx would be accepted, and print the
    verdict. Returns (accepted, the raw check result)."""
    try:
        checks = btc._call("testmempoolaccept", [[tx_hex]])
    except BitcoindError as e:
        print(f"testmempoolaccept failed to run: {e}", file=sys.stderr)
        checks = []
    ok = bool(checks) and checks[0].get("allowed")
    if checks:
        c = checks[0]
        verdict = "allowed" if c.get("allowed") else f"REJECTED: {c.get('reject-reason')}"
        print(f"  mempool   : {verdict}")
    return bool(ok), (checks[0] if checks else {})


def _sign_and_broadcast(btc, wallet: str, source: str, rawtx: str, dry_run: bool) -> int:
    """Sign (Core), validate against the mempool, and broadcast — the shared
    submit path for send, lock-supply, lock-description, and issue."""
    signed = btc.wallet_call(wallet, "signrawtransactionwithwallet", [rawtx])
    if not signed.get("complete"):
        print(f"signing failed (does {source} have BTC for the fee?): "
              f"{signed.get('errors')}", file=sys.stderr)
        return 1
    tx_hex = signed["hex"]

    ok, _ = _check_mempool(btc, tx_hex)

    if dry_run:
        print(f"\n[dry-run] not broadcast. raw tx:\n{tx_hex}")
        return 0 if ok else 1
    if not ok:
        print("not broadcasting: failed mempool acceptance", file=sys.stderr)
        print(f"raw tx: {tx_hex}", file=sys.stderr)
        return 1

    try:
        txid = btc._call("sendrawtransaction", [tx_hex])
    except BitcoindError as e:
        print(f"broadcast failed: {e}", file=sys.stderr)
        print(f"raw tx: {tx_hex}", file=sys.stderr)
        return 1
    print(f"\nbroadcast: {txid}")
    return 0
