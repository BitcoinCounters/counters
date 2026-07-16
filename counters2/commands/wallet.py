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

import shutil
import sys
import threading
import time
from decimal import Decimal

from mnemonic import Mnemonic

from .. import bip32, counterwallet, electrum1, electrum2
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


def _import_electrum2(btc: BitcoindClient, name: str, keys: list[dict],
                      rescan: bool) -> None:
    """Import Electrum 2.x keys as pkh() (standard) or wpkh() (segwit) WIF
    descriptors, per the seed's script type carried on each key."""
    _import_wif(btc, name,
                [(k["desc"].format(wif=k["wif"]), f"electrum/{k['for_change']}/{k['n']}")
                 for k in keys], rescan)


def _wallet_addresses(btc: BitcoindClient, name: str) -> list[str]:
    """Addresses the wallet controls that may hold Counterparty balances:
    every address that has received funds (include_empty) plus current UTXOs.
    This reflects what Bitcoin Core has SEEN on-chain, so it only returns
    addresses if the wallet has been rescanned."""
    addrs: set[str] = set()
    received = btc.wallet_call(name, "listreceivedbyaddress", [0, True, True])
    for r in received:
        if r.get("address"):
            addrs.add(r["address"])
    for u in btc.wallet_call(name, "listunspent", [0, 9999999]):
        if u.get("address"):
            addrs.add(u["address"])
    return sorted(addrs)


def _derived_addresses(btc: BitcoindClient, name: str, count: int) -> list[str]:
    """Addresses derived from the wallet's descriptors WITHOUT touching the
    chain — pure key math via `deriveaddresses`. Expands each ranged descriptor
    over [0, count-1] (and returns the single address for flat WIF descriptors),
    so we can query Counterparty for balances even when the wallet has never
    been rescanned. This is a bounded window (gap limit), not the whole chain:
    raise `count` to look further down each chain."""
    addrs: set[str] = set()
    info = btc.wallet_call(name, "listdescriptors", [])
    for d in info.get("descriptors", []):
        desc = d["desc"]
        if "*" in desc:
            for a in btc._call("deriveaddresses", [desc, [0, max(0, count - 1)]]):
                addrs.add(a)
        else:
            for a in btc._call("deriveaddresses", [desc]):
                addrs.add(a)
    return sorted(addrs)


def _fmt_eta(seconds: float) -> str:
    """Human ETA: '6m 12s', '45s', or '1h 3m'."""
    s = int(max(0, seconds))
    if s >= 3600:
        return f"{s // 3600}h {(s % 3600) // 60}m"
    if s >= 60:
        return f"{s // 60}m {s % 60}s"
    return f"{s}s"


def _render_scan_bar(scanning: dict, tip: int | None = None) -> None:
    """Render/refresh the rescan progress bar in place (carriage return, no
    newline) from a `getwalletinfo.scanning` object ({'progress': 0..1,
    'duration': secs}). If the chain `tip` is known, also show an estimated
    block height (Core reports only a 0..1 fraction, not a height, so the block
    number is progress*tip — approximate).

    The line is truncated to the terminal width and cleared to end-of-line
    (ESC[K) so it never wraps — a wrapped line defeats the carriage return and
    scrolls, printing a new row each refresh instead of updating in place."""
    prog = float(scanning.get("progress") or 0.0)
    dur = float(scanning.get("duration") or 0.0)
    eta = f", ~{_fmt_eta(dur / prog - dur)} left" if prog > 0 else ""
    blk = f" block ~{int(prog * tip):,}/{tip:,}" if tip else ""
    filled = int(prog * 24)
    bar = "#" * filled + "-" * (24 - filled)
    line = f"  rescanning [{bar}] {prog * 100:5.1f}%{blk}{eta}"
    cols = shutil.get_terminal_size(fallback=(80, 24)).columns
    line = line[: max(0, cols - 1)]  # keep within one row so \r stays put
    sys.stderr.write("\r\033[K" + line)
    sys.stderr.flush()


def _chain_tip(btc: BitcoindClient) -> int | None:
    """Best-effort current block height, for showing block N/total; None if the
    node can't be reached (progress bar then just omits the block number)."""
    try:
        return btc.get_block_count()
    except BitcoindError:
        return None


def _clear_line() -> None:
    sys.stderr.write("\r\033[K")
    sys.stderr.flush()


def _is_scanning(btc: BitcoindClient, name: str) -> dict | None:
    """Return the `scanning` object if a rescan is currently in flight for this
    wallet, else None. Bitcoin Core allows only one rescan per wallet at a time."""
    try:
        info = btc.wallet_call(name, "getwalletinfo", [])
    except BitcoindError:
        return None
    scanning = info.get("scanning")
    return scanning if isinstance(scanning, dict) else None


def _monitor_rescan(config: Config, name: str) -> None:
    """Watch an already-running rescan to completion, rendering the live bar.
    Used when a scan is in flight (ours or another client's) and starting a new
    one would fail with 'Wallet is currently rescanning'."""
    poll = BitcoindClient(config)
    tip = _chain_tip(poll)
    while True:
        scanning = _is_scanning(poll, name)
        if scanning is None:
            break
        _render_scan_bar(scanning, tip)
        time.sleep(2)
    _clear_line()


def _run_rescan_with_progress(config: Config, name: str, do_import) -> None:
    """Run a blocking descriptor-import-with-rescan on a background thread while
    polling `getwalletinfo.scanning` on the main thread to render a live bar.

    The import RPC blocks its connection until the scan finishes, so we hand it
    a thread (using the passed-in client) and poll with a SEPARATE client so the
    two never share a requests.Session. Thread liveness is the source of truth
    for completion; `scanning` is only present while a rescan is in flight."""
    error: dict[str, BaseException] = {}

    def worker():
        try:
            do_import()
        except BaseException as e:  # surface to the caller after join
            error["e"] = e

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    poll = BitcoindClient(config)  # own session for concurrent polling
    tip = _chain_tip(poll)
    while t.is_alive():
        time.sleep(2)
        scanning = _is_scanning(poll, name)
        if scanning is not None:
            _render_scan_bar(scanning, tip)

    _clear_line()
    t.join()
    if "e" in error:
        raise error["e"]


def _import_maybe_rescan(config: Config, name: str, import_fn, no_rescan: bool) -> None:
    """Run a restore's key import either with a live-progress chain rescan, or
    (no_rescan) with timestamp='now' so Core imports the keys WITHOUT scanning.

    `import_fn(rescan: bool)` performs the actual createwallet + importdescriptors.
    With no_rescan the wallet has no BTC/UTXO view yet (a later `wallet rescan`
    backfills that), but Counterparty balances are still visible immediately via
    `balance --no-rescan`, which derives the addresses and queries Counterparty."""
    if no_rescan:
        import_fn(rescan=False)
    else:
        _run_rescan_with_progress(config, name, lambda: import_fn(rescan=True))


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
                       addresses: int = 20, dry_run: bool = False,
                       no_rescan: bool = False) -> int:
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
        return _restore_counterwallet(config, name, phrase, addresses, dry_run, no_rescan)
    if not mnemo.check(phrase) and electrum2.is_electrum2_phrase(phrase):
        return _restore_electrum2(config, name, phrase, addresses, dry_run, no_rescan)
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
    if no_rescan:
        print(f"importing all account types into wallet {name!r} WITHOUT rescanning "
              "(timestamp=now):", file=sys.stderr)
    else:
        print(f"importing all account types into wallet {name!r} and rescanning the "
              "chain — this can take several minutes:", file=sys.stderr)
    try:
        _import_maybe_rescan(
            config, name, lambda rescan: _import_account(btc, name, seed, rescan=rescan),
            no_rescan,
        )
    except BitcoindError as e:
        print(f"could not restore wallet: {e}", file=sys.stderr)
        return 1
    if no_rescan:
        print(f"restored wallet {name!r} (no rescan). Run `counters wallet --name "
              f"{name} rescan` for a BTC/UTXO view; `... balance --no-rescan` shows "
              "Counterparty assets now.")
    else:
        print(f"restored wallet {name!r}; rescan complete. Check `counters wallet "
              f"--name {name} balance`.")
    return 0


def _counterwallet_descriptors(phrase: str, count: int) -> tuple[list[dict], list[tuple[str, str]]]:
    """Build (counterwallet_keys, labeled_descriptors) for a legacy phrase.

    We import TWO derivations so a single rescan finds funds regardless of which
    old wallet the phrase came from:
      * Counterwallet / Freewallet / Rare Pepe — BIP32 m/0'/0/i, compressed
        1... P2PKH (what virtually all Counterparty holders have); and
      * genuine desktop Electrum v1 — 100k-stretch + uncompressed 1... P2PKH
        (rare, but cheap to include as a safety net).
    Descriptor labels distinguish the two so `balance` output stays readable."""
    cw = counterwallet.derive(phrase, count=count)
    ev1 = electrum1.derive(phrase, count=count)[1]
    labeled = (
        [(f"pkh({k['wif']})", f"counterwallet/0/{k['n']}") for k in cw]
        + [(f"pkh({k['wif']})", f"electrumv1/{k['for_change']}/{k['n']}") for k in ev1]
    )
    return cw, labeled


def _restore_counterwallet(config: Config, name: str, phrase: str, addresses: int,
                           dry_run: bool = False, no_rescan: bool = False) -> int:
    """Recover a Counterwallet / Freewallet / Rare Pepe wallet. These borrow the
    Electrum-v1 wordlist only as entropy, then derive keys via BIP32 at m/0'/0/i
    (compressed 1... P2PKH) — see counterwallet.py. We also import the genuine
    Electrum-v1 (uncompressed) addresses as a fallback so one rescan finds funds
    regardless of which scheme the old wallet used. These are NOT taproot; they
    live on 1... addresses. With dry_run, print the derived addresses and import
    nothing (no node needed), so you can confirm a match before the long rescan."""
    if not electrum1.is_electrum_v1_phrase(phrase):
        print("that isn't a valid Counterwallet / Electrum-v1 phrase — a word is "
              "unknown or the word count isn't a multiple of 3 (Counterwallet uses "
              "12).", file=sys.stderr)
        return 1
    try:
        cw, labeled = _counterwallet_descriptors(phrase, count=max(1, addresses))
    except ValueError as e:
        print(f"could not decode seed: {e}", file=sys.stderr)
        return 1

    if dry_run:
        print(f"Counterwallet / Rare Pepe (BIP32 m/0'/0/i) — {len(cw)} addresses "
              "(NOT imported — dry run):")
        for k in cw:
            print(f"  m/0'/0/{k['n']:<3} {k['address']}")
        print("\nif your address is listed, re-run without --dry-run to import + "
              "rescan; if not, raise --addresses N. (Genuine Electrum-v1 addresses "
              "are also imported as a fallback.)")
        return 0

    btc = BitcoindClient(config)
    if no_rescan:
        print(f"importing {len(labeled)} legacy addresses into wallet {name!r} WITHOUT "
              "rescanning (timestamp=now):", file=sys.stderr)
    else:
        print(f"importing {len(labeled)} legacy addresses into wallet {name!r}; "
              "rescanning the chain — this can take several minutes:", file=sys.stderr)
    try:
        _import_maybe_rescan(
            config, name, lambda rescan: _import_wif(btc, name, labeled, rescan=rescan),
            no_rescan,
        )
    except BitcoindError as e:
        print(f"could not restore wallet: {e}", file=sys.stderr)
        return 1
    print(f"restored legacy keys into wallet {name!r}"
          + (" (no rescan)." if no_rescan else "; rescan complete."))
    print(f"  first Counterwallet address: {cw[0]['address']}")
    if no_rescan:
        print("these are legacy 1... addresses. `counters wallet --name "
              f"{name} balance --no-rescan` shows Counterparty assets now; run "
              f"`... rescan` when you want a BTC balance or to move funds.")
    else:
        print("these are legacy 1... addresses. Run `counters wallet --name "
              f"{name} balance` for BTC + Counterparty balances, and `... send` to move "
              "assets (e.g. to a new taproot wallet created here).")
    return 0


def _restore_electrum2(config: Config, name: str, phrase: str, addresses: int,
                       dry_run: bool = False, no_rescan: bool = False) -> int:
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
    if no_rescan:
        print(f"importing {len(keys)} {label} keys into wallet {name!r} WITHOUT "
              "rescanning (timestamp=now):", file=sys.stderr)
    else:
        print(f"importing {len(keys)} {label} keys into wallet {name!r}; rescanning "
              "the chain — this can take several minutes:", file=sys.stderr)
    try:
        _import_maybe_rescan(
            config, name, lambda rescan: _import_electrum2(btc, name, keys, rescan=rescan),
            no_rescan,
        )
    except BitcoindError as e:
        print(f"could not restore wallet: {e}", file=sys.stderr)
        return 1
    print(f"restored {label} keys into wallet {name!r}"
          + (" (no rescan)." if no_rescan else "; rescan complete."))
    print(f"  first address: {keys[0]['address']}")
    if no_rescan:
        print(f"`counters wallet --name {name} balance --no-rescan` shows Counterparty "
              f"assets now; run `... rescan` for a BTC balance or to move funds.")
    else:
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


def cmd_wallet_rescan(config: Config, name: str, *, start_height: int | None = None,
                      stop_height: int | None = None) -> int:
    """Rescan the chain for an existing wallet (Bitcoin Core `rescanblockchain`,
    scoped to this one wallet). Use this to backfill balances after importing
    keys without a scan, or to re-scan a bounded height range. Blocks until the
    scan finishes, showing the same live progress bar as restore."""
    btc = BitcoindClient(config)

    # A wallet allows only one rescan at a time. If one is already running (e.g.
    # left over from a restore/import), don't fail — attach to it and show its
    # progress until it finishes.
    if _is_scanning(btc, name) is not None:
        print(f"a rescan is already in progress for wallet {name!r}; monitoring it:",
              file=sys.stderr)
        _monitor_rescan(config, name)
        print("rescan complete.")
        print(f"Check `counters wallet --name {name} balance`.")
        return 0

    params: list = []
    if start_height is not None:
        params.append(start_height)
        if stop_height is not None:
            params.append(stop_height)
    scope = "the whole chain" if not params else (
        f"from height {start_height}" + (f" to {stop_height}" if stop_height is not None else "")
    )
    print(f"rescanning {scope} for wallet {name!r} — this can take several minutes:",
          file=sys.stderr)
    result: dict = {}
    try:
        _run_rescan_with_progress(
            config, name,
            lambda: result.update(
                btc.wallet_call(name, "rescanblockchain", params, timeout=None) or {}
            ),
        )
    except BitcoindError as e:
        # Lost a race: a scan started between our check and the call. Monitor it.
        if "currently rescanning" in str(e).lower():
            print(f"a rescan is already in progress for wallet {name!r}; monitoring it:",
                  file=sys.stderr)
            _monitor_rescan(config, name)
            print("rescan complete.")
            print(f"Check `counters wallet --name {name} balance`.")
            return 0
        print(f"rescan failed: {e}", file=sys.stderr)
        return 1
    lo, hi = result.get("start_height"), result.get("stop_height")
    if lo is not None:
        print(f"rescan complete (scanned heights {lo}–{hi}).")
    else:
        print("rescan complete.")
    print(f"Check `counters wallet --name {name} balance`.")
    return 0


def cmd_wallet_balance(config: Config, name: str, *, no_rescan: bool = False,
                       addresses: int = 20) -> int:
    btc = BitcoindClient(config)
    cp = CounterpartyClient(config)

    if no_rescan:
        # Skip Bitcoin Core's on-chain view entirely: derive the wallet's
        # addresses from its descriptors (pure key math) and ask Counterparty
        # directly. No rescan needed. BTC balance is unavailable this way
        # because it depends on scanned UTXOs.
        try:
            addrs = _derived_addresses(btc, name, addresses)
        except BitcoindError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print("BTC confirmed : (skipped — needs a rescan)")
        print(f"checking Counterparty for {len(addrs)} derived addresses "
              f"({addresses}/chain)...")
        return _report_cp_balances(cp, addrs)

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
    return _report_cp_balances(cp, _wallet_addresses(btc, name))


def _report_cp_balances(cp: CounterpartyClient, addrs: list[str]) -> int:
    # Aggregate Counterparty balances across the given addresses.
    totals: dict[str, dict] = {}
    owned: dict[str, str] = {}   # asset -> display name (issuance rights held)
    for addr in addrs:
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
        try:
            for a in cp.get_address_owned_assets(addr):
                key = a["asset"]
                owned[key] = a.get("asset_longname") or key
        except CounterpartyError:
            pass
    if totals:
        print("\nCounterparty assets:")
        for asset, agg in sorted(totals.items()):
            print(f"  {agg['name']:<28} {agg['qty']}")
    else:
        print("\nno Counterparty assets")
    if owned:
        # "Ownership rights assets": the transferable control of an asset (its
        # `owner` field), independent of the token supply. The protocol has no
        # dedicated name for it — Counterparty just calls you the `owner` — but
        # it is transferable and tradeable in its own right, so we surface it as
        # a distinct holding.
        print("\nOwnership rights assets (transferable control, independent of supply):")
        for asset, name in sorted(owned.items()):
            print(f"  {name}")
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
