"""Read-side `counters` commands: status, info, list, validate.

These are public and need only a synced index DB plus the two backends as
oracles (bitcoind for raw tx/witness, Counterparty Core for issuance validity
and current ownership). They never write to the index.
"""

from __future__ import annotations

import json
import sqlite3
import sys

from ..bitcoind import BitcoindClient, BitcoindError
from ..config import Config, RESERVED_ASSETS
from ..counterparty import CounterpartyClient, CounterpartyError
from ..envelope import find_counter_envelopes_in_tx
from ..store import Store


def _display_name(row: sqlite3.Row) -> str:
    return row["asset_longname"] or row["asset"]


def _live_asset(config: Config, asset: str) -> dict:
    """Live asset info per Counterparty (owner/lock/supply can change after the
    mint). Empty dict if Core is unreachable so callers fall back to stored data."""
    try:
        return CounterpartyClient(config).get_asset(asset) or {}
    except CounterpartyError:
        return {}


# --- status ----------------------------------------------------------------

def cmd_status(config: Config) -> int:
    btc = BitcoindClient(config)
    cp = CounterpartyClient(config)
    store = Store(config)
    try:
        try:
            btc_h: int | None = btc.get_block_count()
            print(f"bitcoind height     : {btc_h}")
        except BitcoindError as e:
            btc_h = None
            print(f"bitcoind            : UNREACHABLE — {e}")

        try:
            st = cp.status()
        except CounterpartyError as e:
            st = {}
            print(f"counterparty        : UNREACHABLE — {e}")
        cp_h = st.get("counterparty_height")
        print(f"counterparty height : {cp_h if cp_h is not None else '?'}")
        print(f"counterparty state  : {st.get('ledger_state', '?')}")

        index_h = store.get_last_height(config.start_height)
        print(f"index height        : {index_h}")
        print(f"counters indexed    : {store.count()}")

        # Actionable sync warnings.
        warnings = []
        if btc_h is not None and index_h is not None and btc_h - index_h > 0:
            warnings.append(
                f"index is {btc_h - index_h:,} block(s) behind bitcoind — run "
                f"`counters index` (follow tip) or `counters sync` (once) to catch up."
            )
        if btc_h is not None and isinstance(cp_h, int) and btc_h - cp_h > 0:
            warnings.append(
                f"counterparty is {btc_h - cp_h:,} block(s) behind bitcoind — it is "
                f"still processing; recently-minted counters may not appear yet."
            )
        for w in warnings:
            print(f"! {w}")
    finally:
        store.close()
    return 0


# --- info -------------------------------------------------------------------

def cmd_info(
    config: Config,
    identifier: str,
    as_json: bool = False,
    raw: bool = False,
    save: str | None = None,
) -> int:
    store = Store(config)
    try:
        row = store.find(identifier)
        if row is None:
            print(f"no counter for {identifier!r}", file=sys.stderr)
            return 1

        # Content output modes take precedence over metadata.
        if raw or save:
            blob = store.read_blob(row["content_sha256"])
            if blob is None:
                print(f"blob {row['content_sha256']} missing on disk", file=sys.stderr)
                return 1
            if save:
                with open(save, "wb") as fh:
                    fh.write(blob)
                print(f"wrote {len(blob)} bytes to {save}")
            else:
                sys.stdout.buffer.write(blob)
            return 0

        info = _live_asset(config, row["asset"])
        owner = info.get("owner") or row["owner"]
        divisible = info["divisible"] if info.get("divisible") is not None else row["divisible"]
        supply_raw = info["supply"] if info.get("supply") is not None else row["supply"]
        locked = info.get("locked")

        # Inscription cost (commit + reveal) computed on demand and cached.
        fee, tx_size = row["fee"], row["tx_size"]
        if fee is None:
            try:
                fee, tx_size = BitcoindClient(config).get_inscription_cost(row["mint_txid"])
                store.set_fee(row["number"], fee, tx_size)
            except (BitcoindError, KeyError, IndexError, TypeError):
                pass

        # XCP burned for the issuance comes from Counterparty (the issuance row).
        xcp_burned = row["xcp_burned"]
        if xcp_burned is None:
            try:
                cp = CounterpartyClient(config)
                rows = cp.get_block_issuances(row["block_index"]).get(row["mint_txid"], [])
                xcp_burned = next(
                    (int(r["fee_paid"]) for r in rows
                     if cp.is_creation(r) and r.get("fee_paid") is not None),
                    None,
                )
                store.set_xcp_burned(row["number"], xcp_burned)
            except CounterpartyError:
                pass

        if as_json:
            record = {k: row[k] for k in row.keys()}
            record["current_owner"] = owner
            record["fee"] = fee
            record["tx_size"] = tx_size
            record["xcp_burned"] = xcp_burned
            record["locked"] = locked
            print(json.dumps(record, indent=2))
            return 0

        print(f"number       : {row['number']}")
        print(f"asset        : {_display_name(row)}")
        print(f"asset_id     : {row['asset_id']}")
        if supply_raw is not None:
            s = f"{supply_raw / 1e8:g}" if divisible else f"{int(supply_raw):,}"
            print(f"supply       : {s}{' (divisible)' if divisible else ''}")
        if locked is not None:
            print(f"locked       : {'yes' if locked else 'no'}")
        print(f"owner        : {owner}")
        print(f"content_type : {row['content_type'] or '(none)'}")
        print(f"size         : {row['content_length']} bytes")
        print(f"sha256       : {row['content_sha256']}")
        print(f"mint_txid    : {row['mint_txid']}")
        print(f"block        : {row['block_index']} (position {row['block_position']})")
        if fee is not None:
            rate = f" ({fee / tx_size:.1f} sat/B)" if tx_size else ""
            print(f"fee          : {fee:,} sats{rate}")
        if xcp_burned is not None:
            print(f"xcp_burned   : {xcp_burned / 1e8:g} XCP")
    finally:
        store.close()
    return 0


# --- list -------------------------------------------------------------------

def _parse_block_range(spec: str) -> tuple[int, int]:
    sep = "-" if "-" in spec else (":" if ":" in spec else None)
    if sep is None:
        h = int(spec)
        return h, h
    a, _, b = spec.partition(sep)
    return int(a), int(b)


def cmd_list(
    config: Config,
    recent: int | None = None,
    owner: str | None = None,
    block: str | None = None,
) -> int:
    store = Store(config)
    try:
        if owner:
            rows = store.list_by_owner(owner)
        elif block:
            start, end = _parse_block_range(block)
            rows = store.list_by_block_range(start, end)
        else:
            rows = store.list_recent(recent or 20)

        if not rows:
            print("no counters")
            return 0

        print(f"{'#':>8}  {'asset':<26} {'content_type':<22} {'size':>9}  block")
        for r in rows:
            print(
                f"{r['number']:>8}  {_display_name(r)[:26]:<26} "
                f"{(r['content_type'] or '-')[:22]:<22} {r['content_length']:>9}  "
                f"{r['block_index']}"
            )
    finally:
        store.close()
    return 0


# --- validate ---------------------------------------------------------------

def cmd_validate(config: Config, txid: str) -> int:
    """Report whether a transaction is a valid counter, and why or why not."""
    btc = BitcoindClient(config)
    cp = CounterpartyClient(config)

    try:
        tx = btc.get_raw_transaction(txid, verbose=True)
    except BitcoindError as e:
        print(f"cannot fetch tx {txid}: {e}", file=sys.stderr)
        return 1

    vin = tx.get("vin", [])
    envelopes = find_counter_envelopes_in_tx(vin)
    has_envelope = len(envelopes) >= 1
    exactly_one = len(envelopes) == 1

    # Issuance lookup by transaction id (Counterparty's per-tx endpoint).
    issuance = None
    blockhash = tx.get("blockhash")
    confirmed = bool(blockhash)
    try:
        tx_issuances = cp.get_issuances_by_tx(txid)
    except CounterpartyError as e:
        print(f"cannot read issuance from Counterparty Core: {e}", file=sys.stderr)
        tx_issuances = []
    any_valid_issuance = any(cp.is_valid(r) for r in tx_issuances)
    for r in tx_issuances:
        if cp.is_valid(r) and cp.is_creation(r):
            issuance = r
            break

    is_creation_issuance = issuance is not None
    asset = issuance["asset"] if issuance else None
    not_reserved = issuance is not None and asset not in RESERVED_ASSETS

    checks = [
        ("COUNT envelope present", has_envelope),
        ("exactly one envelope", exactly_one),
        ("transaction confirmed", confirmed),
        ("Counterparty issuance valid", any_valid_issuance),
        ("is the asset's first/creation issuance", is_creation_issuance),
        ("asset is not BTC/XCP", not_reserved),
    ]
    is_counter = all(ok for _, ok in checks)

    for label, ok in checks:
        print(f"  [{'x' if ok else ' '}] {label}")
    if is_counter:
        print(f"\n{txid}\n  is a VALID counter (asset {asset}).")
    else:
        print(f"\n{txid}\n  is NOT a counter.")
    return 0 if is_counter else 1
