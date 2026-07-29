"""Read-side `counters` commands: status, info, list.

These are public and need only a synced index DB plus the two backends as
oracles (bitcoind for the carrier check, Counterparty Core for message
validity and current ownership). They never write to the index — except the
lazy enrichment backfills (fee, xcp burned), which are metadata-only.
"""

from __future__ import annotations

import json
import sqlite3
import sys

from ..bitcoind import BitcoindClient, BitcoindError
from ..config import Config
from ..counterparty import CounterpartyClient, CounterpartyError
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
        print(f"rolling hash        : {store.last_rolling_hash().hex()}")

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
    full: bool = False,
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
        owner = info.get("owner") or row["source"]
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

        if as_json:
            record = {k: row[k] for k in row.keys()}
            record["current_owner"] = owner
            record["fee"] = fee
            record["tx_size"] = tx_size
            record["locked"] = locked
            print(json.dumps(record, indent=2))
            return 0

        # Extra facts for --full: live burned/holders from Counterparty, the
        # commit txid from the reveal's first input, sibling counters from the
        # store. Each degrades to its stored value (or is omitted) if a backend
        # is unreachable.
        burned, holders, commit_txid = row["burned"], None, None
        asset_numbers: list[int] = []
        if full:
            cp = CounterpartyClient(config)
            try:
                burned = cp.get_asset_destroyed(row["asset"])
            except CounterpartyError:
                pass
            try:
                holders = cp.get_asset_holders_count(row["asset"])
            except CounterpartyError:
                pass
            try:
                commit_txid = BitcoindClient(config).get_raw_transaction(
                    row["mint_txid"], verbose=True)["vin"][0]["txid"]
            except (BitcoindError, KeyError, IndexError, TypeError):
                pass
            asset_numbers = [r["number"] for r in store.get_counters_by_asset(row["asset"])]

        print(f"number       : {row['number']}")
        print(f"asset        : {_display_name(row)}")
        if full:
            print(f"asset_id     : {row['asset_id']}")
            print(f"kind         : {row['kind']}")
        if supply_raw is not None:
            s = f"{supply_raw / 1e8:g}" if divisible else f"{int(supply_raw):,}"
            suffix = "" if full else (" (divisible)" if divisible else "")
            print(f"supply       : {s}{suffix}")
        if full and burned:
            b = f"{burned / 1e8:g}" if divisible else f"{int(burned):,}"
            print(f"burned       : {b}")
        if full and divisible is not None:
            print(f"divisible    : {'yes' if divisible else 'no'}")
        if locked is not None:
            print(f"locked       : {'yes' if locked else 'no'}")
        if full and holders is not None:
            print(f"holders      : {holders}")
        ct = row["content_type"] or "(none)"
        raw_ct = row["content_type_raw"]
        print(f"content_type : {ct}{f'  (raw: {raw_ct})' if raw_ct else ''}")
        print(f"size         : {row['content_length']} bytes")
        if full:
            if row["is_pointer_like"]:
                print("pointer-like : yes (content is a URI; metadata only)")
            print(f"block        : {row['block_index']} (cp tx_index {row['cp_tx_index']})")
        if fee is not None:
            if full:
                # Mirrors the explorer card: "fee paid" and "fee/B" as separate facts.
                print(f"fee          : {fee:,} sats")
                if tx_size:
                    print(f"fee/B        : {fee / tx_size:.1f} sats")
            else:
                rate = f" ({fee / tx_size:.1f} sat/B)" if tx_size else ""
                print(f"fee          : {fee:,} sats{rate}")
        if full and row["xcp_burned"] is not None:
            print(f"xcp_burned   : {row['xcp_burned'] / 1e8:g} XCP")
        if full and asset_numbers:
            others = [n for n in asset_numbers if n != row["number"]]
            if not others:
                print("reinscribed  : no")
            else:
                original = min(asset_numbers)
                tag = (" (this is the original)" if row["number"] == original
                       else f" (original #{original})")
                print(f"reinscribed  : {', '.join(f'#{n}' for n in others)}{tag}")
        if full:
            # Addresses, hashes, and txids last — long opaque strings that
            # bury the readable facts when interleaved above.
            print(f"owner        : {owner}")
            print(f"source       : {row['source']}")
            if commit_txid:
                print(f"commit_txid  : {commit_txid}")
            print(f"reveal_txid  : {row['mint_txid']}"
                  + (f" (msg {row['msg_index']})" if row["msg_index"] else ""))
            print(f"sha256       : {row['content_sha256']}")
            print(f"rolling hash : {row['rolling_hash']}")
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
    source: str | None = None,
    block: str | None = None,
) -> int:
    store = Store(config)
    try:
        if source:
            rows = store.list_by_source(source)
        elif block:
            start, end = _parse_block_range(block)
            rows = store.list_by_block_range(start, end)
        else:
            rows = store.list_recent(recent or 20)

        if not rows:
            print("no counters")
            return 0

        print(f"{'#':>8}  {'asset':<26} {'kind':<10} {'content_type':<22} {'size':>9}  block")
        for r in rows:
            print(
                f"{r['number']:>8}  {_display_name(r)[:26]:<26} "
                f"{r['kind']:<10} {(r['content_type'] or '-')[:22]:<22} "
                f"{r['content_length']:>9}  {r['block_index']}"
            )
    finally:
        store.close()
    return 0
