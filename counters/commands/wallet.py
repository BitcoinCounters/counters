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

from .. import bip32, electrum1, electrum2
from ..bitcoind import BitcoindClient, BitcoindError
from ..config import Config
from ..counterparty import CounterpartyClient, CounterpartyError
from ..store import Store

_KEYPOOL = 1000  # addresses Core derives per chain


def _checksummed(btc: BitcoindClient, descriptor: str) -> str:
    info = btc._call("getdescriptorinfo", [descriptor])
    return descriptor + "#" + info["checksum"]


def _import_account(btc: BitcoindClient, name: str, seed: bytes, rescan: bool) -> None:
    """Create a blank descriptor wallet and import ALL standard BIP39 accounts
    (legacy/nested/segwit/taproot). A BIP39 seed can hold coins under any of
    them, so importing all four lets one rescan find funds wherever they are —
    important for Counterparty assets, which mostly sit on legacy 1... paths.
    Core allows one active descriptor per output type, so all four coexist."""
    # blank descriptor wallet, private keys enabled, so we can import our own.
    btc._call("createwallet", [name, False, True, "", False, True, False, False])
    timestamp = 0 if rescan else "now"
    requests = []
    for kind in bip32.ACCOUNT_TYPES:
        recv, change = bip32.account_descriptors(seed, kind)
        for desc, internal in ((recv, False), (change, True)):
            requests.append({
                "desc": _checksummed(btc, desc),
                "active": True,
                "internal": internal,
                "timestamp": timestamp,
                "range": [0, _KEYPOOL],
            })
    # A timestamp=0 import rescans the whole chain and blocks the RPC until
    # done, so disable the client timeout for that case.
    results = btc.wallet_call(
        name, "importdescriptors", [requests], timeout=None if rescan else -1.0
    )
    for r in results:
        if not r.get("success"):
            raise BitcoindError(f"importdescriptors failed: {r.get('error')}")


def _import_wif(btc: BitcoindClient, name: str,
                descriptors_with_labels: list[tuple[str, str]], rescan: bool) -> None:
    """Create a blank descriptor wallet and import single-key WIF descriptors, so
    Core holds the keys and can sign. Used by the legacy imports (Counterwallet /
    Electrum) where addresses are a flat key list, not ranged BIP32 chains."""
    btc._call("createwallet", [name, False, True, "", False, True, False, False])
    timestamp = 0 if rescan else "now"
    requests = [
        {"desc": _checksummed(btc, desc), "timestamp": timestamp, "label": label}
        for desc, label in descriptors_with_labels
    ]
    results = btc.wallet_call(
        name, "importdescriptors", [requests], timeout=None if rescan else -1.0
    )
    for r in results:
        if not r.get("success"):
            raise BitcoindError(f"importdescriptors failed: {r.get('error')}")


def _import_counterwallet(btc: BitcoindClient, name: str, keys: list[dict],
                          rescan: bool) -> None:
    """Import legacy Counterwallet keys as pkh() WIF descriptors — each key is
    uncompressed → a 1... P2PKH address, matching Counterwallet exactly."""
    _import_wif(btc, name,
                [(f"pkh({k['wif']})", f"counterwallet/{k['for_change']}/{k['n']}")
                 for k in keys], rescan)


def _import_electrum2(btc: BitcoindClient, name: str, keys: list[dict],
                      rescan: bool) -> None:
    """Import Electrum 2.x keys as pkh() (standard) or wpkh() (segwit) WIF
    descriptors, per the seed's script type carried on each key."""
    _import_wif(btc, name,
                [(k["desc"].format(wif=k["wif"]), f"electrum/{k['for_change']}/{k['n']}")
                 for k in keys], rescan)


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
            "Electrum-v1 scheme with legacy 1... addresses. Restore those with:\n"
            "  counters wallet --name <name> restore --counterwallet"
        )
    if n not in (12, 15, 18, 21, 24):
        return (f"got {n} words; a BIP39 seed is 12, 15, 18, 21, or 24 words. "
                "Check for a missing or extra word.")
    return ("BIP39 checksum failed although every word is valid — a word is most "
            "likely mistyped, swapped, or out of order (the final word encodes a "
            "checksum). Re-check the phrase and its word order.")


def cmd_wallet_restore(config: Config, name: str, *, counterwallet: bool = False,
                       addresses: int = 20, dry_run: bool = False) -> int:
    print("enter your seed phrase (BIP39, or an old Counterwallet / Freewallet "
          "phrase):", file=sys.stderr)
    phrase = sys.stdin.readline().strip()
    mnemo = Mnemonic("english")
    # Route automatically: a valid BIP39 phrase (it carries a checksum) restores a
    # taproot wallet; a phrase whose words are all in the Electrum-v1 list is an old
    # Counterwallet seed. --counterwallet forces the legacy path for the rare phrase
    # that would satisfy both.
    use_counterwallet = counterwallet or (
        not mnemo.check(phrase) and electrum1.is_electrum_v1_phrase(phrase)
    )
    if use_counterwallet:
        return _restore_counterwallet(config, name, phrase, addresses, dry_run)
    if not mnemo.check(phrase) and electrum2.is_electrum2_phrase(phrase):
        return _restore_electrum2(config, name, phrase, addresses, dry_run)
    problem = _bip39_problem(mnemo, phrase)
    if problem:
        print(problem, file=sys.stderr)
        return 1
    seed = mnemo.to_seed(phrase)
    if dry_run:
        print("BIP39 seed — first receive address of each account type (NOTHING "
              "imported — dry run):")
        for kind in bip32.ACCOUNT_TYPES:
            print(f"  {kind:8} {bip32.first_address(seed, kind)}")
        print("\nre-run without --dry-run to import all four accounts + rescan.")
        return 0
    btc = BitcoindClient(config)
    print(f"importing all account types into wallet {name!r} and rescanning the chain "
          "— this can take several minutes; please wait...", file=sys.stderr)
    try:
        _import_account(btc, name, seed, rescan=True)
    except BitcoindError as e:
        print(f"could not restore wallet: {e}", file=sys.stderr)
        return 1
    print(f"restored wallet {name!r}; rescan complete. Check `counters wallet --name "
          f"{name} balance`.")
    return 0


def _restore_counterwallet(config: Config, name: str, phrase: str, addresses: int,
                           dry_run: bool = False) -> int:
    """Recover an old Counterwallet / Freewallet (Electrum v1) wallet: decode the
    seed, derive its legacy uncompressed 1... keys, and import them into Core so
    it holds and signs them. These are NOT taproot; they live on 1... addresses.
    With dry_run, print the derived addresses and import nothing (no node needed),
    so you can confirm they match your wallet before the long rescan."""
    if not electrum1.is_electrum_v1_phrase(phrase):
        print("that isn't a valid Counterwallet / Electrum-v1 phrase — a word is "
              "unknown or the word count isn't a multiple of 3 (Counterwallet uses "
              "12).", file=sys.stderr)
        return 1
    try:
        mpk, keys = electrum1.derive(phrase, count=max(1, addresses))
    except ValueError as e:
        print(f"could not decode seed: {e}", file=sys.stderr)
        return 1

    if dry_run:
        print(f"Counterwallet (Electrum v1) — mpk {mpk[:16]}…")
        print(f"derived {len(keys)} addresses (NOT imported — dry run):")
        for k in keys:
            chain = "recv" if k["for_change"] == 0 else "chng"
            print(f"  {chain}/{k['n']:<3} {k['address']}")
        print("\nif your address is listed, re-run without --dry-run to import + "
              "rescan; if not, raise --addresses N.")
        return 0

    btc = BitcoindClient(config)
    print(f"deriving {len(keys)} legacy addresses (mpk {mpk[:16]}…) and importing them "
          f"into wallet {name!r}; rescanning the chain — this can take several "
          "minutes, please wait...", file=sys.stderr)
    try:
        _import_counterwallet(btc, name, keys, rescan=True)
    except BitcoindError as e:
        print(f"could not restore wallet: {e}", file=sys.stderr)
        return 1
    print(f"restored Counterwallet keys into wallet {name!r}; rescan complete.")
    print(f"  first address: {keys[0]['address']}")
    print("these are legacy 1... addresses. Run `counters wallet --name "
          f"{name} balance` for BTC + Counterparty balances, and `... send` to move "
          "assets (e.g. to a new taproot wallet created here).")
    return 0


def _restore_electrum2(config: Config, name: str, phrase: str, addresses: int,
                       dry_run: bool = False) -> int:
    """Recover an Electrum 2.x wallet (standard → 1… P2PKH, or segwit → bc1q…
    P2WPKH): decode the seed with Electrum's derivation and import the keys into
    Core. With dry_run, print the addresses and import nothing (no node needed)."""
    try:
        seed_type, keys = electrum2.derive(phrase, count=max(1, addresses))
    except ValueError as e:
        print(f"could not decode seed: {e}", file=sys.stderr)
        return 1
    label = f"Electrum 2.x ({seed_type})"

    if dry_run:
        print(f"{label} — derived {len(keys)} addresses (NOT imported — dry run):")
        for k in keys:
            chain = "recv" if k["for_change"] == 0 else "chng"
            print(f"  {chain}/{k['n']:<3} {k['address']}")
        print("\nif your address is listed, re-run without --dry-run to import + "
              "rescan; if not, raise --addresses N.")
        return 0

    btc = BitcoindClient(config)
    print(f"importing {len(keys)} {label} keys into wallet {name!r}; rescanning the "
          "chain — this can take several minutes, please wait...", file=sys.stderr)
    try:
        _import_electrum2(btc, name, keys, rescan=True)
    except BitcoindError as e:
        print(f"could not restore wallet: {e}", file=sys.stderr)
        return 1
    print(f"restored {label} keys into wallet {name!r}; rescan complete.")
    print(f"  first address: {keys[0]['address']}")
    print(f"run `counters wallet --name {name} balance` for BTC + Counterparty "
          "balances.")
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
