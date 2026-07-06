"""`counters wallet` commands: create / restore / receive / balance / inscriptions.

Taproot-only (BIP86, bc1p). We generate the BIP39 mnemonic and derive the
account key locally (see bip32.py), then import the tr() descriptors into a
Bitcoin Core descriptor wallet which HOLDS the keys and does all signing. The
mnemonic, printed once at create time, is the only backup.

Counterparty balances are per-address, so for XCP/asset views we aggregate
across the wallet's known addresses (those Core has seen receive funds, plus
unspent), then ask Counterparty Core for each address's balances.
"""

from __future__ import annotations

import sys
from decimal import Decimal

from mnemonic import Mnemonic

from ..bip32 import bip86_account, bip86_descriptors
from ..bitcoind import BitcoindClient, BitcoindError
from ..config import Config
from ..counterparty import CounterpartyClient, CounterpartyError
from ..store import Store

_KEYPOOL = 1000  # addresses Core derives per chain


def _checksummed(btc: BitcoindClient, descriptor: str) -> str:
    info = btc._call("getdescriptorinfo", [descriptor])
    return descriptor + "#" + info["checksum"]


def _import_account(btc: BitcoindClient, name: str, seed: bytes, rescan: bool) -> None:
    """Create a blank descriptor wallet and import the BIP86 tr() chains."""
    acct_xprv, fp = bip86_account(seed)
    recv, change = bip86_descriptors(acct_xprv, fp)
    # blank descriptor wallet, private keys enabled, so we can import our own.
    btc._call("createwallet", [name, False, True, "", False, True, False, False])
    timestamp = 0 if rescan else "now"
    requests = [
        {
            "desc": _checksummed(btc, recv),
            "active": True,
            "internal": False,
            "timestamp": timestamp,
            "range": [0, _KEYPOOL],
        },
        {
            "desc": _checksummed(btc, change),
            "active": True,
            "internal": True,
            "timestamp": timestamp,
            "range": [0, _KEYPOOL],
        },
    ]
    # A timestamp=0 import rescans the whole chain and blocks the RPC until
    # done, so disable the client timeout for that case.
    results = btc.wallet_call(
        name, "importdescriptors", [requests], timeout=None if rescan else -1.0
    )
    for r in results:
        if not r.get("success"):
            raise BitcoindError(f"importdescriptors failed: {r.get('error')}")


def _wallet_addresses(btc: BitcoindClient, name: str) -> list[str]:
    """Addresses the wallet controls that may hold Counterparty balances:
    every address that has received funds (include_empty) plus current UTXOs."""
    addrs: set[str] = set()
    received = btc.wallet_call(name, "listreceivedbyaddress", [0, True, True])
    for r in received:
        if r.get("address"):
            addrs.add(r["address"])
    for u in btc.wallet_call(name, "listunspent", [0, 9999999]):
        if u.get("address"):
            addrs.add(u["address"])
    return sorted(addrs)


# --- commands ---------------------------------------------------------------

def cmd_wallet_create(config: Config, name: str) -> int:
    btc = BitcoindClient(config)
    mnemonic = Mnemonic("english").generate(strength=128)  # 12 words
    seed = Mnemonic("english").to_seed(mnemonic)
    try:
        _import_account(btc, name, seed, rescan=False)
    except BitcoindError as e:
        print(f"could not create wallet: {e}", file=sys.stderr)
        return 1
    first = btc.wallet_call(name, "getnewaddress", ["", "bech32m"])
    print(f"created taproot wallet {name!r}.\n")
    print("=== WRITE DOWN YOUR SEED PHRASE (shown once) ===")
    print(f"  {mnemonic}")
    print("================================================\n")
    print(f"first receive address: {first}")
    return 0


def _bip39_problem(mnemo: Mnemonic, phrase: str) -> str | None:
    """Explain why `phrase` isn't a usable BIP39 mnemonic, or None if it checks
    out. Distinguishes the common old-Counterparty case (pre-BIP39 Electrum-v1
    seeds → legacy 1... addresses) from a plain typo, since 'checksum failed'
    alone sends people down the wrong path."""
    if mnemo.check(phrase):
        return None
    words = phrase.split()
    n = len(words)
    wordset = set(mnemo.wordlist)
    unknown = [w for w in words if w.lower() not in wordset]
    if unknown:
        eg = ", ".join(unknown[:4]) + ("…" if len(unknown) > 4 else "")
        return (
            f"this doesn't look like a BIP39 seed — {len(unknown)} of {n} words aren't "
            f"in the BIP39 word list (e.g. {eg}).\n"
            "Old Counterparty wallets (Counterwallet / Freewallet) use the pre-BIP39 "
            "Electrum-v1 scheme with legacy 1... addresses, which this taproot-only "
            "tool cannot import. Recover those funds/assets in Counterwallet, "
            "Freewallet, or Electrum ('I already have a seed' → options → old-style), "
            "then optionally sweep them to a fresh wallet made here "
            "(`counters wallet --name <name> create`)."
        )
    if n not in (12, 15, 18, 21, 24):
        return (f"got {n} words; a BIP39 seed is 12, 15, 18, 21, or 24 words. "
                "Check for a missing or extra word.")
    return ("BIP39 checksum failed although every word is valid — a word is most "
            "likely mistyped, swapped, or out of order (the final word encodes a "
            "checksum). Re-check the phrase and its word order.")


def cmd_wallet_restore(config: Config, name: str) -> int:
    btc = BitcoindClient(config)
    print("enter your 12/24-word BIP39 seed phrase (this restores a taproot / bc1p "
          "wallet):", file=sys.stderr)
    mnemonic = sys.stdin.readline().strip()
    mnemo = Mnemonic("english")
    problem = _bip39_problem(mnemo, mnemonic)
    if problem:
        print(problem, file=sys.stderr)
        return 1
    seed = mnemo.to_seed(mnemonic)
    print(f"importing into wallet {name!r} and rescanning the chain — this can take "
          "several minutes; please wait...", file=sys.stderr)
    try:
        _import_account(btc, name, seed, rescan=True)
    except BitcoindError as e:
        print(f"could not restore wallet: {e}", file=sys.stderr)
        return 1
    print(f"restored wallet {name!r}; rescan complete. Check `counters wallet --name "
          f"{name} balance`.")
    return 0


def cmd_wallet_receive(config: Config, name: str) -> int:
    btc = BitcoindClient(config)
    try:
        print(btc.wallet_call(name, "getnewaddress", ["", "bech32m"]))
    except BitcoindError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_wallet_balance(config: Config, name: str) -> int:
    btc = BitcoindClient(config)
    cp = CounterpartyClient(config)
    try:
        bal = btc.wallet_call(name, "getbalances", [])
    except BitcoindError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    mine = bal.get("mine", {})
    confirmed = mine.get("trusted", 0)
    pending = mine.get("untrusted_pending", 0) + mine.get("immature", 0)
    print(f"BTC confirmed : {confirmed}")
    if pending:
        print(f"BTC pending   : {pending}")

    # Aggregate Counterparty balances across the wallet's addresses.
    totals: dict[str, dict] = {}
    for addr in _wallet_addresses(btc, name):
        try:
            rows = cp.get_address_balances(addr)
        except CounterpartyError:
            continue
        for r in rows:
            q = int(r.get("quantity") or 0)
            if q <= 0:
                continue
            key = r["asset"]
            agg = totals.setdefault(key, {"qty": 0, "name": r.get("asset_longname") or key})
            agg["qty"] += q
    if totals:
        print("\nCounterparty assets:")
        for asset, agg in sorted(totals.items()):
            print(f"  {agg['name']:<28} {agg['qty']}")
    else:
        print("\nno Counterparty assets")
    return 0


def cmd_wallet_inscriptions(config: Config, name: str) -> int:
    """Counters (inscriptions) held by the wallet: the wallet's Counterparty
    asset balances intersected with the counter index."""
    btc = BitcoindClient(config)
    cp = CounterpartyClient(config)
    store = Store(config)
    try:
        # Sum each asset's normalized balance (Counterparty already accounts
        # for divisibility in quantity_normalized) across the wallet's addresses.
        held: dict[str, Decimal] = {}
        for addr in _wallet_addresses(btc, name):
            try:
                rows = cp.get_address_balances(addr)
            except CounterpartyError:
                continue
            for r in rows:
                q = int(r.get("quantity") or 0)
                if q <= 0:
                    continue
                qn = Decimal(str(r.get("quantity_normalized") or q))
                held[r["asset"]] = held.get(r["asset"], Decimal(0)) + qn

        counters = []
        for asset, qty in held.items():
            row = store.get_counter_by_asset(asset)
            if row is not None:
                counters.append((row, qty))
        counters.sort(key=lambda t: t[0]["number"])

        if not counters:
            print("no counters held by this wallet")
            return 0
        print(f"{'#':>8}  {'asset':<26} {'size':>8} {'held/supply':>16}")
        for r, qty in counters:
            name_ = r["asset_longname"] or r["asset"]
            divisible, supply_raw = r["divisible"], r["supply"]
            if supply_raw is None:
                # Older record (pre-supply column): fetch once and backfill.
                info = cp.get_asset(r["asset"]) or {}
                divisible, supply_raw = info.get("divisible"), info.get("supply")
                if supply_raw is not None:
                    store.set_asset_meta(r["number"], divisible, supply_raw)
            supply = Decimal(int(supply_raw or 0))
            if divisible:
                supply /= Decimal(10**8)
            balance = f"{_fmt_amount(qty)}/{_fmt_amount(supply)}"
            print(
                f"{r['number']:>8}  {name_[:26]:<26} "
                f"{r['content_length']:>8} {balance:>16}"
            )
    finally:
        store.close()
    return 0


def _fmt_amount(d: Decimal) -> str:
    """Trim trailing zeros for display: 10.00000000 -> 10, 0.00031310 -> 0.0003131."""
    return format(d.normalize(), "f")
