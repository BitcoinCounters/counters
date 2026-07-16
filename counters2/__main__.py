"""CLI entry point.

Invoke as `counters <command>` (after `pip install -e .`) or, equivalently,
`python -m counters2 <command>`.
"""

from __future__ import annotations

import argparse
import logging
import sys

from .commands import inscribe, issue, read, send, serve, wallet
from .bitcoind import BitcoindError
from .config import GENESIS_HEIGHT, Config
from .counterparty import CounterpartyError
from .indexer import Indexer


def _setup_logging(verbose: bool) -> None:
    # Keep the console clean for the progress bar: only our logger is verbose;
    # the HTTP client libraries are pinned to WARNING so -v doesn't unleash the
    # urllib3 request firehose.
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    logging.getLogger("counters").setLevel(logging.DEBUG if verbose else logging.INFO)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)


class _OrdStyleHelp(argparse.RawDescriptionHelpFormatter):
    """Render help like `ord`: 'Usage:' prefix, a clean 'Commands:' list (no
    {a,b,c} blob or double indentation), and 'Options:'."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("max_help_position", 40)
        super().__init__(*args, **kwargs)

    def add_usage(self, usage, actions, groups, prefix=None):
        super().add_usage(usage, actions, groups, prefix="Usage: ")

    def _iter_indented_subactions(self, action):
        # Don't add argparse's extra indent level for subcommands; this keeps
        # them flush under 'Commands:' AND keeps the help-column math consistent
        # (otherwise the longest entry wraps).
        if action.nargs == argparse.PARSER:
            try:
                get_subactions = action._get_subactions
            except AttributeError:
                return
            yield from get_subactions()
        else:
            yield from super()._iter_indented_subactions(action)

    def _format_action(self, action):
        text = super()._format_action(action)
        if action.nargs == argparse.PARSER:
            # Drop the auto-generated metavar header line; keep the subcommands.
            text = "\n".join(text.split("\n")[1:])
        return text


def main(argv: list[str] | None = None) -> int:
    # A parent parser carries -v so it is accepted both before AND after the
    # subcommand. SUPPRESS default means an absent flag won't overwrite a value
    # set in the other position.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help="debug logging"
    )

    parser = argparse.ArgumentParser(
        prog="counters",
        description="Bitcoin Counters v3 — Counterparty taproot-envelope "
                    "file-event indexer, CLI, and wallet",
        parents=[common],
        formatter_class=_OrdStyleHelp,
        usage="counters [OPTIONS] <COMMAND>",
    )
    parser.set_defaults(verbose=False)
    parser._optionals.title = "Options"
    sub = parser.add_subparsers(
        dest="command", required=False, title="Commands", metavar="<command>"
    )
    # Show Commands above Options in --help, like ord.
    parser._action_groups.insert(0, parser._action_groups.pop())

    # --- daemon / indexing ---
    # A first-time scan always starts at the protocol genesis (block
    # 902000): rule N3 — nothing can qualify earlier. Stored sync progress
    # takes precedence on later runs; COUNTER_START_HEIGHT can raise the floor.
    # `run` is a backward-compatible alias for `index`.
    sub.add_parser(
        "index",
        parents=[common],
        aliases=["run"],
        help="continuously sync to tip and follow new blocks",
    )

    p_sync = sub.add_parser(
        "sync", parents=[common], help="sync once up to the tip and exit"
    )
    p_sync.add_argument("--stop-at", type=int, default=None, help="stop at this block height")

    p_server = sub.add_parser(
        "server", parents=[common],
        help="run the indexer AND serve the web explorer + read-only JSON API",
    )
    p_server.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    p_server.add_argument("--port", type=int, default=8081, help="port (default: 8081)")
    p_server.add_argument(
        "--no-index", action="store_true",
        help="serve only; do not run the indexer (e.g. when `counters index` "
             "already runs in a separate process)",
    )

    # --- reads ---
    sub.add_parser("status", parents=[common], help="tips/health of all three backends")

    p_info = sub.add_parser("info", parents=[common], help="show a counter by number or asset")
    p_info.add_argument("identifier", help="counter number, asset name, or longname")
    g_info = p_info.add_mutually_exclusive_group()
    g_info.add_argument("--json", action="store_true", help="metadata as JSON")
    g_info.add_argument("--raw", action="store_true", help="stream raw file bytes to stdout")
    g_info.add_argument("--save", metavar="PATH", help="write the counter's file to disk")

    p_list = sub.add_parser("list", parents=[common], help="list counters")
    g_list = p_list.add_mutually_exclusive_group()
    g_list.add_argument("--recent", type=int, metavar="N", help="N most recent (default 20)")
    g_list.add_argument("--source", metavar="ADDR", help="counters minted from ADDR")
    g_list.add_argument("--block", metavar="A-B", help="counters in a block range, e.g. 800000-800100")

    p_val = sub.add_parser("validate", parents=[common], help="is <txid> a valid counter, and why")
    p_val.add_argument("txid")

    # --- wallet (taproot BIP86; keys held by Bitcoin Core) ---
    # --name is a wallet-level option (counters wallet --name abc create); it is
    # also accepted after the subcommand via the SUPPRESS-default parent so it
    # never clobbers the wallet-level value when absent.
    wname = argparse.ArgumentParser(add_help=False)
    wname.add_argument("--name", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    p_wallet = sub.add_parser(
        "wallet",
        parents=[common],
        help="taproot wallet (keys in Bitcoin Core)",
        formatter_class=_OrdStyleHelp,
        usage="counters wallet [--name NAME] <COMMAND>",
    )
    p_wallet.add_argument("--name", default="counter", help="Core wallet name (default: counter)")
    p_wallet._optionals.title = "Options"
    wsub = p_wallet.add_subparsers(
        dest="wallet_command", required=False, title="Commands", metavar="<command>"
    )
    p_wallet._action_groups.insert(0, p_wallet._action_groups.pop())
    wsub.add_parser("create", parents=[common, wname], help="create a wallet; print the seed once")
    p_restore = wsub.add_parser(
        "restore", parents=[common, wname], help="restore from a seed phrase (via stdin)"
    )
    p_restore.add_argument(
        "--counterwallet", action="store_true",
        help="force the old Counterwallet / Freewallet (Electrum v1) path, importing "
             "legacy 1... keys. Normally auto-detected from the phrase; only needed "
             "for a phrase that is valid as both BIP39 and Electrum v1",
    )
    p_restore.add_argument(
        "--addresses", type=int, default=20, metavar="N",
        help="Counterwallet only: how many addresses per chain to import (default 20)",
    )
    p_restore.add_argument(
        "--dry-run", action="store_true",
        help="derive and print the addresses (per account type for BIP39, or the "
             "1... list for Counterwallet) to verify them, but import nothing and "
             "skip the rescan",
    )
    p_restore.add_argument(
        "--no-rescan", action="store_true",
        help="import the keys WITHOUT rescanning the chain (timestamp=now). "
             "Counterparty balances are visible immediately via `balance "
             "--no-rescan`; run `wallet rescan` later for a BTC balance or to spend",
    )
    wsub.add_parser("receive", parents=[common, wname], help="new taproot (bc1p) receive address")
    p_bal = wsub.add_parser("balance", parents=[common, wname],
                            help="BTC + Counterparty balances")
    p_bal.add_argument(
        "--no-rescan", action="store_true",
        help="skip Bitcoin Core's on-chain view: derive addresses from the "
             "wallet descriptors and query Counterparty directly (no rescan). "
             "BTC balance is unavailable this way",
    )
    p_bal.add_argument(
        "--addresses", type=int, default=20, metavar="N",
        help="--no-rescan only: addresses per chain to derive and check (default 20)",
    )
    p_rescan = wsub.add_parser(
        "rescan", parents=[common, wname],
        help="rescan the chain for this wallet (e.g. to backfill balances)",
    )
    p_rescan.add_argument("--start-height", type=int, default=None, metavar="H",
                          help="first block to scan (default: genesis)")
    p_rescan.add_argument("--stop-height", type=int, default=None, metavar="H",
                          help="last block to scan (default: chain tip)")
    wsub.add_parser("inscriptions", parents=[common, wname], help="counters held by this wallet")
    p_insc = wsub.add_parser(
        "inscribe", parents=[common, wname], help="mint a counter from a file"
    )
    p_insc.add_argument("--file", required=True, help="file to inscribe")
    p_insc.add_argument("--asset",
                        help="named asset or PARENT.CHILD subasset; omit for free numeric. "
                             "An EXISTING asset you own gets the content attached via a "
                             "reissuance (new counter, same asset)")
    p_insc.add_argument("--fee-rate", type=float, default=None, metavar="SAT_VB",
                        help="fee rate in sat/vB (default: Counterparty estimates one)")
    p_insc.add_argument("--supply", type=int, default=1, help="issued quantity (default 1)")
    p_insc.add_argument("--divisible", action="store_true", help="make the asset divisible")
    p_insc.add_argument("--locked", action="store_true",
                        help="lock the asset's supply (no future issuance can change it)")
    p_insc.add_argument("--source", metavar="ADDRESS",
                        help="Counterparty source address (funds the commit, receives the "
                             "tokens, pays any XCP burn); default: picked from the wallet")
    p_insc.add_argument("--inputs-set", metavar="TXID:VOUT[,...]",
                        help="pin the exact UTXO(s) that fund the commit (Counterparty "
                             "inputs_set format)")
    p_insc.add_argument("--dry-run", action="store_true",
                        help="compose + sign but do not broadcast; print raw hex")

    p_send = wsub.add_parser(
        "send", parents=[common, wname],
        help="transfer a counter (asset) to an address",
        usage="counters wallet [--name NAME] send <ADDRESS> <ASSET> <AMOUNT>",
    )
    p_send.add_argument("destination", metavar="address", help="recipient Bitcoin address")
    p_send.add_argument("asset", help="asset name or longname of the counter")
    p_send.add_argument("amount", help="quantity to send (e.g. 1, or 0.5 for a divisible asset)")
    p_send.add_argument("--fee-rate", type=float, default=None, metavar="SAT_VB",
                        help="fee rate in sat/vB (default: Counterparty estimates one)")
    p_send.add_argument("--dry-run", action="store_true",
                        help="compose + sign + validate but do not broadcast; print raw hex")

    p_lock_supply = wsub.add_parser(
        "lock-supply", parents=[common, wname], aliases=["lock"],
        help="freeze an asset's supply (no future issuance can change it)",
    )
    p_lock_supply.add_argument("asset", help="asset name or longname whose issuance rights you hold")
    p_lock_supply.add_argument("--dry-run", action="store_true",
                               help="compose + sign + validate but do not broadcast; print raw hex")

    p_lock_desc = wsub.add_parser(
        "lock-description", parents=[common, wname],
        help="freeze an asset's description (the image/metadata reference)",
    )
    p_lock_desc.add_argument("asset", help="asset name or longname whose issuance rights you hold")
    p_lock_desc.add_argument("--dry-run", action="store_true",
                             help="compose + sign + validate but do not broadcast; print raw hex")

    p_issue = wsub.add_parser(
        "issue", parents=[common, wname],
        help="issue additional supply of an existing asset you own",
    )
    p_issue.add_argument("asset", help="asset name or longname whose issuance rights you hold")
    p_issue.add_argument("amount", help="additional quantity to issue (e.g. 100, or 0.5 if divisible)")
    p_issue.add_argument("--lock", action="store_true",
                         help="also lock the supply in the same transaction")
    p_issue.add_argument("--dry-run", action="store_true",
                         help="compose + sign + validate but do not broadcast; print raw hex")

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    if not getattr(args, "command", None):
        parser.print_help()
        return 0

    config = Config()  # start_height is clamped to genesis in Config itself

    if args.command in ("index", "run"):
        indexer = Indexer(config)
        try:
            indexer.run()
        except KeyboardInterrupt:
            print("interrupted", file=sys.stderr)
        finally:
            indexer.close()
        return 0

    if args.command == "sync":
        indexer = Indexer(config)
        try:
            total = indexer.sync_to_tip(stop_at=args.stop_at)
            print(f"done. recorded {total} new counter(s); total {indexer.store.count()}")
        finally:
            indexer.close()
        return 0

    if args.command == "status":
        return read.cmd_status(config)

    if args.command == "info":
        return read.cmd_info(
            config, args.identifier, as_json=args.json, raw=args.raw, save=args.save
        )

    if args.command == "list":
        return read.cmd_list(config, recent=args.recent, source=args.source, block=args.block)

    if args.command == "validate":
        return read.cmd_validate(config, args.txid)

    if args.command == "server":
        return serve.cmd_server(
            config, args.host, args.port, with_index=not args.no_index
        )

    if args.command == "wallet":
        if not getattr(args, "wallet_command", None):
            p_wallet.print_help()
            return 0
        try:
            if args.wallet_command == "inscribe":
                return inscribe.cmd_inscribe(
                    config, args.name, args.file,
                    asset=args.asset, fee_rate=args.fee_rate,
                    supply=args.supply, divisible=args.divisible, lock=args.locked,
                    source=args.source, inputs_set=args.inputs_set,
                    dry_run=args.dry_run,
                )
            if args.wallet_command == "send":
                return send.cmd_send(
                    config, args.name, args.destination, args.asset, args.amount,
                    fee_rate=args.fee_rate, dry_run=args.dry_run,
                )
            if args.wallet_command in ("lock-supply", "lock"):
                return issue.cmd_lock_supply(
                    config, args.name, args.asset, dry_run=args.dry_run,
                )
            if args.wallet_command == "lock-description":
                return issue.cmd_lock_description(
                    config, args.name, args.asset, dry_run=args.dry_run,
                )
            if args.wallet_command == "issue":
                return issue.cmd_issue(
                    config, args.name, args.asset, args.amount,
                    lock=args.lock, dry_run=args.dry_run,
                )
            if args.wallet_command == "restore":
                return wallet.cmd_wallet_restore(
                    config, args.name,
                    counterwallet=args.counterwallet, addresses=args.addresses,
                    dry_run=args.dry_run, no_rescan=args.no_rescan,
                )
            if args.wallet_command == "balance":
                return wallet.cmd_wallet_balance(
                    config, args.name,
                    no_rescan=args.no_rescan, addresses=args.addresses,
                )
            if args.wallet_command == "rescan":
                return wallet.cmd_wallet_rescan(
                    config, args.name,
                    start_height=args.start_height, stop_height=args.stop_height,
                )
            dispatch = {
                "create": wallet.cmd_wallet_create,
                "receive": wallet.cmd_wallet_receive,
                "inscriptions": wallet.cmd_wallet_inscriptions,
            }
            return dispatch[args.wallet_command](config, args.name)
        except (BitcoindError, CounterpartyError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
