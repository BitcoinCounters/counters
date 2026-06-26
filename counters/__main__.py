"""CLI entry point.

Invoke as `counters <command>` (after `pip install -e .`) or, equivalently,
`python -m counters <command>`.
"""

from __future__ import annotations

import argparse
import logging
import sys

from .commands import inscribe, read, send, serve, wallet
from .bitcoind import BitcoindError
from .config import COUNTERS_GENESIS_HEIGHT, TAPROOT_ACTIVATION_HEIGHT, Config
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
        description="Counterparty Inscriptions (Bitcoin Counters) — CLI + indexer",
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
    # Where a FIRST-TIME scan begins (ignored once the DB has stored progress).
    # Default is block 0 (exhaustive); these flags raise the floor.
    startfrom = argparse.ArgumentParser(add_help=False)
    g_start = startfrom.add_mutually_exclusive_group()
    g_start.add_argument(
        "--from-taproot", action="store_true",
        help=f"scan from taproot activation (block {TAPROOT_ACTIVATION_HEIGHT}); "
             f"skips blocks that cannot carry a taproot reveal",
    )
    g_start.add_argument(
        "--from-genesis", action="store_true",
        help=f"scan from the counters genesis block ({COUNTERS_GENESIS_HEIGHT}, #0); "
             f"trusts that no valid counter precedes it",
    )

    # `run` is a backward-compatible alias for `index`.
    sub.add_parser(
        "index",
        parents=[common, startfrom],
        aliases=["run"],
        help="continuously sync to tip and follow new blocks",
    )

    p_sync = sub.add_parser(
        "sync", parents=[common, startfrom], help="sync once up to the tip and exit"
    )
    p_sync.add_argument("--stop-at", type=int, default=None, help="stop at this block height")

    p_server = sub.add_parser(
        "server", parents=[common], help="serve the web explorer + read-only JSON API"
    )
    p_server.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    p_server.add_argument("--port", type=int, default=8080, help="port (default: 8080)")

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
    g_list.add_argument("--owner", metavar="ADDR", help="counters held at mint by ADDR")
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
    wsub.add_parser("restore", parents=[common, wname], help="restore from a seed phrase (via stdin)")
    wsub.add_parser("receive", parents=[common, wname], help="new taproot (bc1p) receive address")
    wsub.add_parser("balance", parents=[common, wname], help="BTC + Counterparty balances")
    wsub.add_parser("inscriptions", parents=[common, wname], help="counters held by this wallet")
    p_insc = wsub.add_parser(
        "inscribe", parents=[common, wname], help="mint a counter from a file"
    )
    p_insc.add_argument("--file", required=True, help="file to inscribe")
    p_insc.add_argument("--asset", help="named asset or PARENT.CHILD subasset; omit for free numeric")
    p_insc.add_argument("--fee-rate", type=float, default=5.0, help="reveal fee rate (sat/vB)")
    p_insc.add_argument("--commit-fee-rate", type=float, default=None,
                        help="commit fee rate (sat/vB); defaults to --fee-rate")
    p_insc.add_argument("--destination", help="address to own the minted counter (default: wallet)")
    p_insc.add_argument("--supply", type=int, default=1, help="issued quantity (default 1)")
    p_insc.add_argument("--divisible", action="store_true", help="make the asset divisible")
    p_insc.add_argument("--locked", action="store_true",
                        help="lock the asset's supply (no future issuance can change it)")
    p_insc.add_argument("--dry-run", action="store_true",
                        help="build + sign both txs but do not broadcast; print raw hex")

    p_send = wsub.add_parser(
        "send", parents=[common, wname], help="transfer a counter (asset) to an address"
    )
    p_send.add_argument("asset", help="asset name or longname of the counter")
    p_send.add_argument("amount", help="quantity to send (e.g. 1, or 0.5 for a divisible asset)")
    p_send.add_argument("destination", help="recipient Bitcoin address")
    p_send.add_argument("--dry-run", action="store_true",
                        help="compose + sign + validate but do not broadcast; print raw hex")

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    if not getattr(args, "command", None):
        parser.print_help()
        return 0

    config = Config()

    # First-time scan floor (the DB's stored height wins on later runs).
    if getattr(args, "from_taproot", False):
        config.start_height = TAPROOT_ACTIVATION_HEIGHT
    elif getattr(args, "from_genesis", False):
        config.start_height = COUNTERS_GENESIS_HEIGHT

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
        return read.cmd_list(config, recent=args.recent, owner=args.owner, block=args.block)

    if args.command == "validate":
        return read.cmd_validate(config, args.txid)

    if args.command == "server":
        return serve.cmd_server(config, args.host, args.port)

    if args.command == "wallet":
        if not getattr(args, "wallet_command", None):
            p_wallet.print_help()
            return 0
        try:
            if args.wallet_command == "inscribe":
                return inscribe.cmd_inscribe(
                    config, args.name, args.file,
                    asset=args.asset, fee_rate=args.fee_rate,
                    commit_fee_rate=args.commit_fee_rate, destination=args.destination,
                    supply=args.supply, divisible=args.divisible, lock=args.locked,
                    dry_run=args.dry_run,
                )
            if args.wallet_command == "send":
                return send.cmd_send(
                    config, args.name, args.asset, args.amount, args.destination,
                    dry_run=args.dry_run,
                )
            dispatch = {
                "create": wallet.cmd_wallet_create,
                "restore": wallet.cmd_wallet_restore,
                "receive": wallet.cmd_wallet_receive,
                "balance": wallet.cmd_wallet_balance,
                "inscriptions": wallet.cmd_wallet_inscriptions,
            }
            return dispatch[args.wallet_command](config, args.name)
        except (BitcoindError, CounterpartyError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
