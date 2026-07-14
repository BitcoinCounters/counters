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

    def get_asset_issuances(self, asset: str) -> list[dict]:
        """Every issuance event for an asset (creation, reissuances, and
        ownership transfers), oldest-to-newest as returned by the API.

        Each row carries `block_index`, `tx_index`, `issuer`, `transfer`, and
        `status`; used to reconstruct who held the issuance rights at any block.
        """
        out: list[dict] = []
        cursor: Any = None
        while True:
            params: dict[str, Any] = {"limit": 1000, "verbose": "true"}
            if cursor is not None:
                params["cursor"] = cursor
            data = self._get(f"/v2/assets/{asset}/issuances", params=params)
            if not data:
                break
            out.extend(data.get("result", []))
            cursor = data.get("next_cursor")
            if cursor is None:
                break
        return out

    def issuer_at_height(self, asset: str, height: int) -> str | None:
        """The owner (issuance-rights holder) of `asset` as of `height`.

        Ownership only changes via valid issuance messages — creation, a
        reissuance (issuer unchanged), or a transfer that sets a new `issuer` —
        each stamped with a block_index. So the owner as of `height` is the
        `issuer` of the most recent VALID issuance at or before `height`,
        ordered by (block_index, tx_index). Returns None if the asset has no
        valid issuance by then.
        """
        rows = [
            r
            for r in self.get_asset_issuances(asset)
            if self.is_valid(r) and r.get("block_index") is not None
            and int(r["block_index"]) <= height
        ]
        if not rows:
            return None
        rows.sort(key=lambda r: (int(r["block_index"]), int(r.get("tx_index") or 0)))
        return rows[-1].get("issuer")

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
        inputs_set: str | None = None,
        description: str | None = None,
        transfer_destination: str | None = None,
        lock: bool = False,
    ) -> dict:
        """Compose an OP_RETURN issuance and return Core's result dict (includes
        `rawtransaction`).

        `inputs_set`, when given, pins the first UTXO so the RC4 key (= first
        input's txid) matches the reveal's vin[0]; the issuance message is keyed
        on it (composer.py: arc4_key = unspent_list[0]["txid"]). This is needed
        for the inscribe reveal but not for a standalone issuance (lock/reissue),
        where Counterparty funds normally from `source`.

        `description`, when given, is set on the asset. Omit it (None) to keep
        the asset's current description on a reissue/lock — passing "" would
        WIPE it. `lock=True` locks the supply (no future issuance can change it).
        """
        params: dict[str, Any] = {
            "asset": asset,
            "quantity": quantity,
            "divisible": "true" if divisible else "false",
            "lock": "true" if lock else "false",
            "encoding": "opreturn",
            "disable_utxo_locks": "true",
            "allow_unconfirmed_inputs": "true",
            "verbose": "true",
        }
        if inputs_set is not None:
            params["inputs_set"] = inputs_set
        if description is not None:
            params["description"] = description
        if transfer_destination:
            params["transfer_destination"] = transfer_destination
        data = self._get(f"/v2/addresses/{source}/compose/issuance", params=params)
        if not data or "result" not in data:
            raise CounterpartyError(f"compose issuance failed: {data}")
        return data["result"]

    def compose_send(
        self, source: str, asset: str, quantity: int, destination: str,
        sat_per_vbyte: float | int | None = None,
    ) -> dict:
        """Compose a Counterparty asset *send* (OP_RETURN) from `source` to
        `destination`. `quantity` is in raw units (sats for divisible assets).
        Returns Core's result dict including `rawtransaction` (unsigned).

        `sat_per_vbyte`, when given, sets the BTC fee rate; otherwise Counterparty
        estimates one from its confirmation target.
        """
        params: dict[str, Any] = {
            "asset": asset,
            "quantity": quantity,
            "destination": destination,
            "encoding": "opreturn",
            "allow_unconfirmed_inputs": "true",
            "verbose": "true",
        }
        if sat_per_vbyte is not None:
            # Send whole rates as ints so the API doesn't see "1.0" for `1`.
            if isinstance(sat_per_vbyte, float) and sat_per_vbyte.is_integer():
                sat_per_vbyte = int(sat_per_vbyte)
            params["sat_per_vbyte"] = sat_per_vbyte
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

    def get_address_owned_assets(self, address: str) -> list[dict]:
        """Assets whose issuance rights this address currently OWNS — i.e. it can
        reissue, lock, or transfer ownership — even if it holds zero of the token.

        This is distinct from get_address_balances (tokens held). Hits
        /v2/addresses/<address>/assets/owned (get_valid_assets_by_owner). Each row
        carries at least {asset, asset_longname, owner, supply/supply_normalized}.
        """
        out: list[dict] = []
        cursor: Any = None
        while True:
            params: dict[str, Any] = {"limit": 1000, "verbose": "true"}
            if cursor is not None:
                params["cursor"] = cursor
            data = self._get(f"/v2/addresses/{address}/assets/owned", params=params)
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
