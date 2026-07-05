"""Counterparty Core v2 API client (the oracle).

We never reimplement Counterparty consensus. We ask Core for:
  - the issuances in a block (to join against COUNTER txs),
  - whether an issuance is valid and is the asset's first/creation issuance,
  - asset identity (asset_id, longname, owner).
"""

from __future__ import annotations

from typing import Any

import requests

from .config import Config, CREATION_EVENTS


class CounterpartyError(Exception):
    """A Counterparty API call failed. `kind` classifies why so callers can
    report a specific reason: 'unreachable' (nothing listening on the port),
    'timeout' (reachable but slow/busy), 'http' (non-200 response), or the
    default 'error' (anything else)."""

    def __init__(self, message: str, kind: str = "error"):
        super().__init__(message)
        self.kind = kind


class CounterpartyClient:
    def __init__(self, config: Config):
        self.base = config.cp_api_url.rstrip("/")
        self.timeout = config.http_timeout
        self._session = requests.Session()
        self._asset_cache: dict[str, dict | None] = {}

    def _get(self, path: str, params: dict | None = None) -> Any:
        url = f"{self.base}{path}"
        try:
            resp = self._session.get(url, params=params, timeout=self.timeout)
        except requests.ConnectionError as e:
            raise CounterpartyError(
                f"could not reach Counterparty at {self.base} — is counterparty-server "
                f"running with its API enabled on that port?",
                kind="unreachable",
            ) from e
        except requests.Timeout as e:
            raise CounterpartyError(
                f"Counterparty API timed out at {self.base}; the server may still be "
                f"starting up or catching up",
                kind="timeout",
            ) from e
        except requests.RequestException as e:
            raise CounterpartyError(f"Counterparty API request failed: {e}") from e
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise CounterpartyError(
                f"Counterparty API HTTP {resp.status_code}: {resp.text[:200]}", kind="http"
            )
        return resp.json()

    # --- server / chain ----------------------------------------------------

    def status(self) -> dict:
        data = self._get("/v2/")
        return data["result"] if data else {}

    def counterparty_height(self) -> int:
        return int(self.status().get("counterparty_height", 0))

    # --- issuances ---------------------------------------------------------

    def get_block_issuances(self, height: int) -> dict[str, list[dict]]:
        """Return {tx_hash: [issuance, ...]} for a block.

        Paginates via Counterparty's cursor until exhausted.
        """
        by_tx: dict[str, list[dict]] = {}
        cursor: Any = None
        while True:
            params: dict[str, Any] = {"limit": 1000, "verbose": "true"}
            if cursor is not None:
                params["cursor"] = cursor
            data = self._get(f"/v2/blocks/{height}/issuances", params=params)
            if not data:
                break
            for row in data.get("result", []):
                by_tx.setdefault(row["tx_hash"], []).append(row)
            cursor = data.get("next_cursor")
            if cursor is None:
                break
        return by_tx

    def get_issuances_by_tx(self, txid: str) -> list[dict]:
        """Issuance row(s) for a single transaction via /v2/issuances/<tx_hash>.

        A tx carries at most one issuance message, so this is 0 or 1 rows;
        returned as a list to mirror get_block_issuances(). `verbose=true` is
        required so the row includes `asset_events` (needed by is_creation()).
        """
        data = self._get(f"/v2/issuances/{txid}", params={"verbose": "true"})
        if not data:
            return []
        result = data.get("result")
        if result is None:
            return []
        return result if isinstance(result, list) else [result]

    @staticmethod
    def is_creation(issuance: dict) -> bool:
        """True if this issuance record is the asset's first/creation issuance.

        `asset_events` is a space-separated list and a creation can carry extra
        events (e.g. "creation lock_quantity" when issued with --locked). Split
        on whitespace AND commas so any of those forms is recognised.
        """
        events = str(issuance.get("asset_events") or "").replace(",", " ")
        return any(ev in CREATION_EVENTS for ev in events.split())

    @staticmethod
    def is_valid(issuance: dict) -> bool:
        return issuance.get("status") == "valid"

    # --- assets ------------------------------------------------------------

    def get_asset(self, asset: str) -> dict | None:
        if asset not in self._asset_cache:
            data = self._get(f"/v2/assets/{asset}")
            self._asset_cache[asset] = data.get("result") if data else None
        return self._asset_cache[asset]

    # --- compose -----------------------------------------------------------

    def compose_issuance(
        self,
        source: str,
        asset: str,
        quantity: int,
        divisible: bool,
        inputs_set: str,
        description: str = "",
        transfer_destination: str | None = None,
        lock: bool = False,
    ) -> dict:
        """Compose an OP_RETURN issuance and return Core's result dict (includes
        `rawtransaction`). `inputs_set` pins the first UTXO so the RC4 key
        (= first input's txid) matches the reveal's vin[0]; the issuance message
        is keyed on it (composer.py: arc4_key = unspent_list[0]["txid"]).

        `lock=True` locks the asset's supply (no future issuance can change it).
        """
        params: dict[str, Any] = {
            "asset": asset,
            "quantity": quantity,
            "divisible": "true" if divisible else "false",
            "lock": "true" if lock else "false",
            "description": description,
            "encoding": "opreturn",
            "inputs_set": inputs_set,
            "disable_utxo_locks": "true",
            "allow_unconfirmed_inputs": "true",
            "verbose": "true",
        }
        if transfer_destination:
            params["transfer_destination"] = transfer_destination
        data = self._get(f"/v2/addresses/{source}/compose/issuance", params=params)
        if not data or "result" not in data:
            raise CounterpartyError(f"compose issuance failed: {data}")
        return data["result"]

    def compose_send(
        self, source: str, asset: str, quantity: int, destination: str
    ) -> dict:
        """Compose a Counterparty asset *send* (OP_RETURN) from `source` to
        `destination`. `quantity` is in raw units (sats for divisible assets).
        Returns Core's result dict including `rawtransaction` (unsigned).
        """
        params: dict[str, Any] = {
            "asset": asset,
            "quantity": quantity,
            "destination": destination,
            "encoding": "opreturn",
            "allow_unconfirmed_inputs": "true",
            "verbose": "true",
        }
        data = self._get(f"/v2/addresses/{source}/compose/send", params=params)
        if not data or "result" not in data:
            raise CounterpartyError(f"compose send failed: {data}")
        return data["result"]

    # --- addresses ---------------------------------------------------------

    def get_address_balances(self, address: str) -> list[dict]:
        """All Counterparty (XCP + asset) balances held by an address.

        Paginates the cursor-based endpoint. Each row has at least
        {asset, asset_longname, quantity, quantity_normalized}.
        """
        out: list[dict] = []
        cursor: Any = None
        while True:
            params: dict[str, Any] = {"limit": 1000, "verbose": "true"}
            if cursor is not None:
                params["cursor"] = cursor
            data = self._get(f"/v2/addresses/{address}/balances", params=params)
            if not data:
                break
            out.extend(data.get("result", []))
            cursor = data.get("next_cursor")
            if cursor is None:
                break
        return out

    def get_xcp_balance(self, address: str) -> int:
        """XCP balance of an address, in satoshis (XCP is divisible). 0 if none."""
        data = self._get(f"/v2/addresses/{address}/balances/XCP")
        if not data:
            return 0
        result = data.get("result")
        if isinstance(result, list):
            return sum(int(row.get("quantity", 0)) for row in result)
        if isinstance(result, dict):
            return int(result.get("quantity", 0))
        return 0
