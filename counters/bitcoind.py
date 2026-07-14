"""Minimal bitcoind JSON-RPC client.

Uses cookie-file auth by default (the local node has no rpcuser/rpcpassword),
falling back to explicit user/password if configured.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import requests

from .config import Config
from .envelope import find_commit_txid

COIN = 100_000_000  # sats per BTC


class BitcoindError(Exception):
    pass


class BitcoindClient:
    def __init__(self, config: Config):
        self.url = config.btc_rpc_url
        self.timeout = config.http_timeout
        self._cookie_file = Path(config.btc_cookie_file)
        self._static_auth: tuple[str, str] | None = (
            (config.btc_rpc_user, config.btc_rpc_password)
            if config.btc_rpc_user
            else None
        )
        # Cookie auth is re-read on demand (see _resolve_auth): bitcoind writes
        # a fresh random password to its .cookie on every restart, so caching
        # it once would 401 forever after a node restart.
        self._cookie_auth: tuple[str, str] | None = None
        self._cookie_mtime: float | None = None
        self._session = requests.Session()
        self._id = 0
        # Fail fast if there is no way to authenticate at all.
        if self._resolve_auth() is None:
            raise BitcoindError(
                f"No bitcoind auth available: cookie file {self._cookie_file} not found "
                "and BTC_RPC_USER not set."
            )

    def _resolve_auth(self) -> tuple[str, str] | None:
        """Current RPC credentials, re-reading the cookie file when it changes.

        bitcoind rewrites its .cookie with a new password on each restart; a
        client that cached the old value would get 401s forever after the node
        restarts. Keying off the file's mtime lets us pick up the new cookie
        automatically (and cheaply — one stat() per call) so the indexer
        reconnects on its own once bitcoind comes back."""
        try:
            mtime = self._cookie_file.stat().st_mtime
        except OSError:
            # No cookie file: fall back to configured user/password, if any.
            return self._static_auth
        if self._cookie_auth is None or mtime != self._cookie_mtime:
            user, _, password = self._cookie_file.read_text().strip().partition(":")
            self._cookie_auth = (user, password)
            self._cookie_mtime = mtime
        return self._cookie_auth

    def _call(
        self,
        method: str,
        params: list[Any] | None = None,
        wallet: str | None = None,
        timeout: float | None = -1.0,
    ) -> Any:
        self._id += 1
        payload = {"jsonrpc": "1.0", "id": self._id, "method": method, "params": params or []}
        # Wallet RPCs are scoped to the /wallet/<name> endpoint.
        url = f"{self.url.rstrip('/')}/wallet/{wallet}" if wallet else self.url
        # timeout=-1 means "use the default"; None means "wait indefinitely"
        # (needed for blocking calls like a full-history importdescriptors rescan).
        effective = self.timeout if timeout == -1.0 else timeout
        try:
            resp = self._session.post(
                url, json=payload, auth=self._resolve_auth(), timeout=effective
            )
        except requests.ConnectionError as e:
            raise BitcoindError(
                f"could not reach Bitcoin Core at {self.url} — is bitcoind running "
                f"with server=1 and the RPC port reachable?"
            ) from e
        except requests.Timeout as e:
            raise BitcoindError(
                f"Bitcoin Core RPC timed out at {self.url} (method {method}); the node "
                f"may be busy (e.g. mid-reindex)"
            ) from e
        except requests.RequestException as e:
            raise BitcoindError(f"bitcoind RPC request failed: {e}") from e
        if resp.status_code not in (200, 500):
            raise BitcoindError(f"bitcoind RPC HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        if data.get("error"):
            err = data["error"]
            code = err.get("code") if isinstance(err, dict) else None
            msg = err.get("message") if isinstance(err, dict) else err
            if code == -18 and wallet:
                # -18 is ambiguous ("does not exist OR is not loaded"); check
                # the wallet dir so we give the right remedy.
                try:
                    on_disk = wallet in self.list_wallet_dir()
                except BitcoindError:
                    on_disk = True  # can't tell: assume it exists, suggest load
                if on_disk:
                    raise BitcoindError(
                        f"wallet {wallet!r} exists but is not loaded in Bitcoin Core. "
                        f"Load it with: bitcoin-cli loadwallet {wallet}"
                    )
                raise BitcoindError(
                    f"wallet {wallet!r} does not exist. "
                    f"Create it with: counters wallet create --name {wallet}"
                )
            if code == -5 and method == "getrawtransaction":
                raise BitcoindError(
                    f"transaction not found by Bitcoin Core: {msg} — non-wallet txs "
                    f"require txindex=1 in bitcoin.conf (and a one-time reindex)."
                )
            if isinstance(msg, str) and "Insufficient funds" in msg:
                raise BitcoindError(
                    f"insufficient BTC in wallet {wallet or '(default)'} to cover the "
                    f"amount plus the transaction fee — fund a wallet address and retry."
                )
            raise BitcoindError(f"bitcoind RPC error for {method}: {msg}")
        return data["result"]

    def wallet_call(
        self,
        wallet: str,
        method: str,
        params: list[Any] | None = None,
        timeout: float | None = -1.0,
    ) -> Any:
        """Invoke a wallet-scoped RPC against /wallet/<name>."""
        return self._call(method, params, wallet=wallet, timeout=timeout)

    def list_wallet_dir(self) -> list[str]:
        """Names of wallets present in Bitcoin Core's wallet directory (whether
        or not they are currently loaded)."""
        data = self._call("listwalletdir")
        return [w.get("name") for w in (data or {}).get("wallets", [])]

    # --- high-level helpers ------------------------------------------------

    def get_block_count(self) -> int:
        return int(self._call("getblockcount"))

    def get_block_hash(self, height: int) -> str:
        return self._call("getblockhash", [height])

    def get_block(self, block_hash: str, verbosity: int = 2) -> dict:
        """Verbosity 2 returns full tx data including vin[].txinwitness."""
        return self._call("getblock", [block_hash, verbosity])

    def get_block_at_height(self, height: int, verbosity: int = 2) -> dict:
        return self.get_block(self.get_block_hash(height), verbosity)

    def get_raw_transaction(self, txid: str, verbose: bool = True) -> dict:
        """Decoded tx (verbose=True) including vin[].txinwitness and, if mined,
        the 'blockhash'. Requires txindex=1 for non-wallet, non-mempool txs."""
        return self._call("getrawtransaction", [txid, verbose])

    def get_block_header(self, block_hash: str) -> dict:
        """Header object including 'height' and 'confirmations'."""
        return self._call("getblockheader", [block_hash])

    def get_fee_and_size(self, txid: str, tx: dict | None = None) -> tuple[int | None, int | None]:
        """Mining fee (sats) and raw serialized size (bytes) for a tx.

        bitcoind doesn't report a confirmed tx's fee, so we sum the outputs and
        subtract the inputs (each input's value comes from its prevout tx, which
        needs txindex=1). `size` is the full serialized byte length (witness
        included, no segwit discount). Pass an already-decoded `tx` to save one
        RPC. Returns (None, size) for coinbase.
        """
        if tx is None:
            tx = self.get_raw_transaction(txid, verbose=True)
        size = tx.get("size")
        out_sats = sum(round(o.get("value", 0) * COIN) for o in tx.get("vout", []))
        in_sats = 0
        for vin in tx.get("vin", []):
            if "txid" not in vin:  # coinbase has no prevout to price
                return None, size
            prev = self.get_raw_transaction(vin["txid"], verbose=True)
            in_sats += round(prev["vout"][vin["vout"]]["value"] * COIN)
        return in_sats - out_sats, size

    def get_inscription_cost(
        self, reveal_txid: str, reveal_tx: dict | None = None
    ) -> tuple[int | None, int | None]:
        """Total fee (sats) and raw size (bytes) to inscribe = commit + reveal.

        The reveal is the mint tx; the commit is the tx whose output the reveal
        script-path-spends (the prevout of the COUNT-envelope input). We sum both
        so the displayed fee/rate reflect the whole inscription, not just the
        reveal. Falls back to reveal-only if the commit can't be identified.
        """
        if reveal_tx is None:
            reveal_tx = self.get_raw_transaction(reveal_txid, verbose=True)
        fee, size = self.get_fee_and_size(reveal_txid, tx=reveal_tx)
        commit_txid = find_commit_txid(reveal_tx.get("vin", []))
        if commit_txid and commit_txid != reveal_txid:
            cfee, csize = self.get_fee_and_size(commit_txid)
            if fee is not None and cfee is not None:
                fee += cfee
            if size is not None and csize is not None:
                size += csize
        return fee, size

    def get_input_addresses(self, tx: dict) -> set[str]:
        """The set of addresses whose UTXOs this tx spends.

        Each input's address is read from its prevout's scriptPubKey (needs
        txindex=1 for non-wallet prevouts). Used to prove reinscription
        authorisation: the tx must spend from the asset's owner address.
        Coinbase inputs and prevouts without a decodable address are skipped.
        """
        addrs: set[str] = set()
        for vin in tx.get("vin", []):
            if "txid" not in vin:  # coinbase: no prevout
                continue
            prev = self.get_raw_transaction(vin["txid"], verbose=True)
            spk = prev["vout"][vin["vout"]].get("scriptPubKey", {})
            addr = spk.get("address")
            if addr:
                addrs.add(addr)
        return addrs
