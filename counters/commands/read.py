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
from datetime import datetime, timezone

from ..bitcoind import BitcoindClient, BitcoindError
from ..config import Config
from ..content import classify_mime_type, stamp_image
from ..counterparty import CounterpartyClient, CounterpartyError
from ..reveal import commit_txid, envelope_style
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

def _emit_content(store: Store, row: sqlite3.Row, save: str | None) -> int:
    """--raw / --save: the counter's file itself (for an asset, the original's)."""
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


def _ensure_fee(config: Config, store: Store, row: sqlite3.Row) -> tuple[int | None, int | None]:
    """Inscription cost (commit + reveal), computed on demand and cached."""
    fee, tx_size = row["fee"], row["tx_size"]
    if fee is None:
        try:
            fee, tx_size = BitcoindClient(config).get_inscription_cost(row["mint_txid"])
            store.set_fee(row["number"], fee, tx_size)
        except (BitcoindError, KeyError, IndexError, TypeError):
            pass
    return fee, tx_size


def _fmt_qty(qty: int, divisible: bool) -> str:
    if not divisible:
        return f"{int(qty):,}"
    whole, frac = divmod(int(qty), 10**8)
    return f"{whole:,}" + (f".{frac:08d}".rstrip("0") if frac else "")


def _asset_live(config: Config, name: str, last: sqlite3.Row):
    """(supply, divisible, locked, burned, holders, owner) — live per
    Counterparty, falling back to the stored snapshot where unreachable."""
    info = _live_asset(config, name)
    supply = info["supply"] if info.get("supply") is not None else last["supply"]
    divisible = info["divisible"] if info.get("divisible") is not None else last["divisible"]
    locked = info.get("locked")
    owner = info.get("owner") or last["source"]
    burned, holders = last["burned"], None
    cp = CounterpartyClient(config)
    try:
        burned = cp.get_asset_destroyed(name)
    except CounterpartyError:
        pass
    try:
        holders = cp.get_asset_holders_count(name)
    except CounterpartyError:
        pass
    return supply, divisible, locked, burned, holders, owner


def _counter_info(config: Config, store: Store, row: sqlite3.Row,
                  as_json: bool, full: bool) -> int:
    """One counter: the inscription event. Asset-level facts stay in the asset
    view — except supply and burned, useful enough to repeat here."""
    fee, tx_size = _ensure_fee(config, store, row)

    if as_json:
        info = _live_asset(config, row["asset"])
        record = {k: row[k] for k in row.keys()}
        record["current_owner"] = info.get("owner") or row["source"]
        record["fee"] = fee
        record["tx_size"] = tx_size
        record["locked"] = info.get("locked")
        print(json.dumps(record, indent=2))
        return 0

    info = _live_asset(config, row["asset"])
    divisible = info["divisible"] if info.get("divisible") is not None else row["divisible"]
    supply = info["supply"] if info.get("supply") is not None else row["supply"]

    commit = envelope = block_time = stamp_mime = None
    burned = row["burned"]
    asset_numbers: list[int] = []
    if full:
        cp = CounterpartyClient(config)
        try:
            burned = cp.get_asset_destroyed(row["asset"])
        except CounterpartyError:
            pass
        try:
            blk = cp.get_block(row["block_index"])
            block_time = blk.get("block_time") if blk else None
        except CounterpartyError:
            pass
        try:
            tx = BitcoindClient(config).get_raw_transaction(row["mint_txid"], verbose=True)
        except BitcoindError:
            tx = None
        if tx is not None:
            commit = commit_txid(tx)
            envelope = envelope_style(tx)  # 'ord' | 'generic'
        # Stamp tag mirrors the explorer: a textual counter whose payload
        # decodes as a STAMP: image (display metadata only, §5.4).
        ct_class = classify_mime_type(row["content_type"] or "text/plain",
                                      row["block_index"])
        if ct_class == "text" and row["content_length"] <= 256 * 1024:
            blob = store.read_blob(row["content_sha256"])
            decoded = stamp_image(blob, textual=True) if blob else None
            stamp_mime = decoded[1] if decoded else None
        asset_numbers = [r["number"] for r in store.get_counters_by_asset(row["asset"])]

    print(f"number       : {row['number']}")
    print(f"asset        : {_display_name(row)}")
    if full:
        print(f"kind         : {row['kind']}")
    if supply is not None:
        print(f"supply       : {_fmt_qty(supply, divisible)}"
              f"{' (divisible)' if divisible else ''}")
    if full and burned:
        print(f"burned       : {_fmt_qty(burned, divisible)}")
    ct = row["content_type"] or "(none)"
    raw_ct = row["content_type_raw"]
    print(f"content_type : {ct}{f'  (raw: {raw_ct})' if raw_ct else ''}")
    if full and envelope:
        print(f"envelope     : {'ord/xcp — also an ordinals inscription' if envelope == 'ord' else 'generic taproot'}")
    if full and stamp_mime:
        print(f"stamp        : {stamp_mime} (decodes as a stamp image)")
    print(f"size         : {row['content_length']} bytes")
    if full:
        if row["is_pointer_like"]:
            print("pointer-like : yes (content is a URI; metadata only)")
        print(f"block        : {row['block_index']} (cp tx_index {row['cp_tx_index']})")
        if block_time:
            created = datetime.fromtimestamp(block_time, tz=timezone.utc)
            print(f"created      : {created:%Y-%m-%d %H:%M} UTC")
    if fee is not None:
        if full:
            # Mirrors the explorer card: "fee paid" and "fee/B" as separate facts.
            print(f"fee          : {fee:,} sats")
            if tx_size:
                print(f"fee/B        : {fee / tx_size:.1f} sats/B")
        else:
            rate = f" ({fee / tx_size:.1f} sats/B)" if tx_size else ""
            print(f"fee          : {fee:,} sats{rate}")
    if full and row["xcp_burned"] is not None:
        print(f"xcp_burned   : {row['xcp_burned'] / 1e8:g} XCP")
    if full and asset_numbers:
        others = [n for n in asset_numbers if n != row["number"]]
        original = min(asset_numbers)
        if not others:
            print("reinscribed  : no")
        elif row["number"] == original:
            shown = ", ".join(f"#{n}" for n in others[:12])
            more = f", ... (+{len(others) - 12} more)" if len(others) > 12 else ""
            print(f"reinscribed  : yes — {shown}{more}")
        else:
            print(f"reinscribed  : yes — original #{original}")
    if full:
        # Addresses, txids, and hashes last — long opaque strings that bury
        # the readable facts when interleaved above.
        print(f"source       : {row['source']}")
        if commit:
            print(f"commit_txid  : {commit}")
        print(f"reveal_txid  : {row['mint_txid']}"
              + (f" (msg {row['msg_index']})" if row["msg_index"] else ""))
        print(f"sha256       : {row['content_sha256']}")
        print(f"rolling hash : {row['rolling_hash']}")
    return 0


def _asset_info(config: Config, store: Store, name: str,
                rows: list[sqlite3.Row], as_json: bool, full: bool) -> int:
    """One asset: its counters and asset-level facts, with per-event detail
    left to the counter view. `rows` is every counter on the asset, oldest
    first (the original is rows[0]) — and may be empty: any Counterparty
    asset answers here, counterless ones straight from the oracle."""
    last = rows[-1] if rows else None
    if last is not None:
        asset, longname = last["asset"], last["asset_longname"]
        asset_id = last["asset_id"]
        supply, divisible, locked, burned, holders, owner = _asset_live(
            config, asset, last)
    else:
        try:
            info = CounterpartyClient(config).get_asset(name) or {}
        except CounterpartyError as e:
            print(f"cannot reach Counterparty: {e}", file=sys.stderr)
            return 1
        if not info:
            print(f"no counter or Counterparty asset for {name!r}", file=sys.stderr)
            return 1
        asset, longname = info["asset"], info.get("asset_longname")
        asset_id = info.get("asset_id")
        supply, divisible = info.get("supply"), info.get("divisible")
        locked, owner = info.get("locked"), info.get("owner")
        burned = holders = None
        cp = CounterpartyClient(config)
        try:
            burned = cp.get_asset_destroyed(asset)
        except CounterpartyError:
            pass
        try:
            holders = cp.get_asset_holders_count(asset)
        except CounterpartyError:
            pass

    numbers = [r["number"] for r in rows]
    total_size = sum(r["content_length"] for r in rows)
    fees = [_ensure_fee(config, store, r) for r in rows]
    total_fee = sum(f for f, _ in fees if f is not None)
    total_tx_size = sum(s for _, s in fees if s is not None)
    unknown_fees = sum(1 for f, _ in fees if f is None)
    total_xcp = sum(r["xcp_burned"] or 0 for r in rows)

    if as_json:
        print(json.dumps({
            "asset": asset,
            "asset_longname": longname,
            "asset_id": asset_id,
            "supply": supply,
            "burned": burned,
            "divisible": divisible,
            "locked": locked,
            "holders": holders,
            "owner": owner,
            "counters": numbers,
            "counter_count": len(numbers),
            "total_size": total_size if rows else None,
            "total_fee": total_fee if rows and not unknown_fees else None,
            "total_xcp_burned": total_xcp if rows else None,
        }, indent=2))
        return 0

    print(f"asset        : {longname or asset}")
    if longname:
        print(f"asset_name   : {asset}")
    if numbers:
        shown = ", ".join(f"#{n}" for n in numbers[:12])
        more = f", ... (+{len(numbers) - 12} more)" if len(numbers) > 12 else ""
        print(f"counters     : {len(numbers)} — {shown}{more}")
    else:
        print("counters     : 0")
    if supply is not None:
        print(f"supply       : {_fmt_qty(supply, divisible)}")
    if burned:
        print(f"burned       : {_fmt_qty(burned, divisible)}")
    if holders is not None:
        print(f"holders      : {holders}")
    if full and divisible is not None:
        print(f"divisible    : {'yes' if divisible else 'no'}")
    if locked is not None:
        print(f"locked       : {'yes' if locked else 'no'}")
    if rows:
        print(f"total size   : {total_size:,} bytes")
        xcp = f" + {total_xcp / 1e8:g} XCP" if total_xcp else ""
        unknown = f" ({unknown_fees} unknown)" if unknown_fees else ""
        print(f"total fees   : {total_fee:,} sats{xcp}{unknown}")
        if full and total_tx_size:
            print(f"fee/B        : {total_fee / total_tx_size:.1f} sats/B")
    if full:
        print(f"asset_id     : {asset_id}")
        print(f"owner        : {owner}")
    return 0


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
        # A number names one counter (the event); an asset name gets the
        # asset summary. Numeric Counterparty assets are A-prefixed, so a
        # digit string is never ambiguous.
        if identifier.isdigit():
            row = store.get_counter(int(identifier))
            if row is None:
                print(f"no counter for {identifier!r}", file=sys.stderr)
                return 1
            if raw or save:
                return _emit_content(store, row, save)
            return _counter_info(config, store, row, as_json, full)

        rows = store.get_counters_by_asset(identifier)
        if raw or save:
            if not rows:
                print(f"no counter content for {identifier!r}", file=sys.stderr)
                return 1
            return _emit_content(store, rows[0], save)
        return _asset_info(config, store, identifier, rows, as_json, full)
    finally:
        store.close()


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
