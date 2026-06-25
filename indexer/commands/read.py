"""Read-side `counter` commands: status, info, list, validate.

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


def _current_owner(config: Config, asset: str, fallback: str | None) -> str | None:
    """Current holder per Counterparty (ownership can change after the mint).
    Falls back to the mint-time owner if Core is unreachable."""
    try:
        info = CounterpartyClient(config).get_asset(asset) or {}
        return info.get("owner") or fallback
    except CounterpartyError:
        return fallback


# --- status ----------------------------------------------------------------

def cmd_status(config: Config) -> int:
    btc = BitcoindClient(config)
    cp = CounterpartyClient(config)
    store = Store(config)
    try:
        try:
            st = cp.status()
        except CounterpartyError:
            st = {}
        index_h = store.get_last_height(config.start_height)
        print(f"bitcoind height     : {btc.get_block_count()}")
        print(f"counterparty height : {st.get('counterparty_height', '?')}")
        print(f"counterparty state  : {st.get('ledger_state', '?')}")
        print(f"index height        : {index_h}")
        print(f"counters indexed    : {store.count()}")
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

        owner = _current_owner(config, row["asset"], row["owner"])

        if as_json:
            record = {k: row[k] for k in row.keys()}
            record["current_owner"] = owner
            print(json.dumps(record, indent=2))
            return 0

        print(f"number       : {row['number']}")
        print(f"asset        : {_display_name(row)}")
        print(f"asset_id     : {row['asset_id']}")
        print(f"owner        : {owner}")
        print(f"content_type : {row['content_type'] or '(none)'}")
        print(f"size         : {row['content_length']} bytes")
        print(f"sha256       : {row['content_sha256']}")
        print(f"mint_txid    : {row['mint_txid']}")
        print(f"block        : {row['block_index']} (position {row['block_position']})")
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

    # Issuance lookup reuses the proven per-block join (keyed by tx_hash).
    issuance = None
    any_valid_issuance = False
    blockhash = tx.get("blockhash")
    confirmed = bool(blockhash)
    if confirmed:
        try:
            height = btc.get_block_header(blockhash).get("height")
            tx_issuances = cp.get_block_issuances(height).get(txid, [])
        except (BitcoindError, CounterpartyError) as e:
            print(f"cannot read issuance from Core: {e}", file=sys.stderr)
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
