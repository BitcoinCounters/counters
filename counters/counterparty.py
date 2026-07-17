"""Counterparty Core v2 API client (the oracle).

We never reimplement Counterparty consensus. We ask Core for:
  - the issuances and fairminter deploys in a block (the qualifying-event
    candidates, rules R1-R3),
  - asset identity (asset_id, longname, owner) and balances,
  - composed transactions (issuance / send), including the taproot-envelope
    inscription flow (encoding=taproot).
"""

from __future__ import annotations

from typing import Any

import requests

from .config import Config


class CounterpartyError(Exception):
    """A Counterparty API call failed. `kind` classifies why so callers can
    report a specific reason: 'unreachable' (nothing listening on the port),
    'timeout' (reachable but slow/busy), 'http' (non-200 response), or the
    default 'error' (anything else)."""

    def __init__(self, message: str, kind: str = "error"):
        super().__init__(message)
        self.kind = kind



def _fee_rate_param(sat_per_vbyte: float | int) -> float | int:
    """Send whole rates as ints so the API doesn't see "1.0" for `1`."""
    if isinstance(sat_per_vbyte, float) and sat_per_vbyte.is_integer():
        return int(sat_per_vbyte)
    return sat_per_vbyte


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

    def _paginate(self, path: str, missing_ok: bool = True) -> list[dict]:
        """Exhaust a cursor-paginated endpoint (verbose=true) into one list.

        missing_ok=False turns a 404 into an error instead of an empty list:
        the block-scoped candidate endpoints use it so a block Counterparty
        has not parsed (restart, rollback) aborts the sync pass for a retry —
        silently treating it as "no events" would advance the cursor past
        real events and fork the numbering.
        """
        out: list[dict] = []
        cursor: Any = None
        while True:
            params: dict[str, Any] = {"limit": 1000, "verbose": "true"}
            if cursor is not None:
                params["cursor"] = cursor
            data = self._get(path, params=params)
            if data is None:
                if missing_ok:
                    break
                raise CounterpartyError(
                    f"{path} returned 404 — has Counterparty parsed this block?",
                    kind="http",
                )
            if not data:
                break
            out.extend(data.get("result", []))
            cursor = data.get("next_cursor")
            if cursor is None:
                break
        return out

    # --- server / chain ----------------------------------------------------

    def status(self) -> dict:
        data = self._get("/v2/")
        return data["result"] if data else {}

    def counterparty_height(self) -> int:
        return int(self.status().get("counterparty_height", 0))

    # --- qualifying-event candidates (build ref v3 §8 step 1) ---------------

    def get_block_issuances(self, height: int) -> list[dict]:
        """All issuance rows parsed in a block, in API order. Each row carries
        tx_hash, tx_index, msg_index, status, description, mime_type,
        fair_minting, asset, asset_longname, issuer/source, fee_paid, ...
        Raises (rather than returning []) for a block Counterparty has not
        parsed, so the sync pass retries instead of skipping events."""
        return self._paginate(f"/v2/blocks/{height}/issuances", missing_ok=False)

    def get_block_fairminters(self, height: int) -> list[dict]:
        """All fairminter deploys parsed in a block. Rows carry tx_hash,
        tx_index, block_index, source, asset, asset_longname, description,
        mime_type, ... (no msg_index — deploys are one message per tx).
        Raises for an unparsed block, like get_block_issuances."""
        return self._paginate(f"/v2/blocks/{height}/fairminters", missing_ok=False)

    def get_issuances_by_tx(self, txid: str) -> list[dict]:
        """Issuance row(s) for a single transaction via /v2/issuances/<tx_hash>.
        `verbose=true` so rows include every field the validity checks read."""
        data = self._get(f"/v2/issuances/{txid}", params={"verbose": "true"})
        if not data:
            return []
        result = data.get("result")
        if result is None:
            return []
        return result if isinstance(result, list) else [result]

    @staticmethod
    def is_valid(issuance: dict) -> bool:
        return issuance.get("status") == "valid"

    # --- blocks --------------------------------------------------------------

    def get_block(self, height: int) -> dict | None:
        """Block metadata (block_hash, block_time, ledger_hash, …) via
        /v2/blocks/<height>; None for a block Counterparty has not parsed."""
        data = self._get(f"/v2/blocks/{height}")
        return data.get("result") if data else None

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
        encoding: str = "opreturn",
        mime_type: str | None = None,
        sat_per_vbyte: float | int | None = None,
    ) -> dict:
        """Compose an issuance and return Core's result dict.

        encoding="opreturn" (default) composes the classic single transaction
        (`rawtransaction`), used for lock/reissue/transfer where no new content
        rides along. encoding="taproot" composes the commit/reveal inscription
        pair: `rawtransaction` is the UNSIGNED commit and
        `signed_reveal_rawtransaction` the reveal, whose envelope input Core
        signs itself with the ephemeral envelope key (build ref v3 §11).
        `mime_type` labels the description content; binary content is passed
        as hex per Core's content encoding (§5.1).

        `description=None` keeps the asset's current description on a
        reissue/lock — passing "" would WIPE it. `lock=True` locks the supply.
        """
        params: dict[str, Any] = {
            "asset": asset,
            "quantity": quantity,
            "divisible": "true" if divisible else "false",
            "lock": "true" if lock else "false",
            "encoding": encoding,
            "disable_utxo_locks": "true",
            "allow_unconfirmed_inputs": "true",
            "verbose": "true",
        }
        if inputs_set is not None:
            params["inputs_set"] = inputs_set
        if description is not None:
            params["description"] = description
        if mime_type is not None:
            params["mime_type"] = mime_type
        if sat_per_vbyte is not None:
            params["sat_per_vbyte"] = _fee_rate_param(sat_per_vbyte)
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
            params["sat_per_vbyte"] = _fee_rate_param(sat_per_vbyte)
        data = self._get(f"/v2/addresses/{source}/compose/send", params=params)
        if not data or "result" not in data:
            raise CounterpartyError(f"compose send failed: {data}")
        return data["result"]

    # --- addresses ---------------------------------------------------------

    def get_address_balances(self, address: str) -> list[dict]:
        """All Counterparty (XCP + asset) balances held by an address."""
        return self._paginate(f"/v2/addresses/{address}/balances")

    def get_address_owned_assets(self, address: str) -> list[dict]:
        """Assets whose issuance rights this address currently OWNS — i.e. it can
        reissue, lock, or transfer ownership — even if it holds zero of the token."""
        return self._paginate(f"/v2/addresses/{address}/assets/owned")

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
