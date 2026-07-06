"""Configuration for the Counterparty Inscriptions indexer.

All values are overridable via environment variables so the same code runs
against a local node now and a different backend later. Defaults match the
local setup discovered on this machine (native bitcoind + Core on :4000).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


# --- Protocol constants -----------------------------------------------------

# Marker pushed as the first data element inside the envelope: the literal
# ASCII "COUNT" (OP_PUSHBYTES_5 434f554e54), per the build reference §4/§13.
COUNT_MARKER = b"COUNT"

# Field tag for the content (MIME) type, pushed as a single byte 0x01. The
# legacy OP_1 (0x51) pushnum form also appears on-chain and is accepted on
# parse (build reference §4).
CONTENT_TYPE_TAG = 0x01

# Field tag for a *reinscription* target asset, pushed as a single byte 0x02
# followed by the asset's name/longname (UTF-8). Its presence marks the envelope
# as a reinscription onto an EXISTING asset: there is NO Counterparty message in
# the tx; the indexer binds the counter to this named asset and authorises it by
# checking the tx spent an input from the asset's owner (issuance rights) as of
# that block. Absent tag => creation-style counter bound via a same-tx issuance.
ASSET_TAG = 0x02

# Counterparty `asset_events` values that mark an asset's first (creation)
# issuance. Re-issuances use values like "change_description".
CREATION_EVENTS = frozenset({"creation"})

# Assets that can never back a counter.
RESERVED_ASSETS = frozenset({"BTC", "XCP"})

# Taproot (BIP341) activation height on mainnet. A counter's envelope lives in a
# taproot script-path witness, so a witness-based counter cannot exist before
# this block. `--from-taproot` uses it to skip ~709k blocks that cannot carry a
# taproot reveal. (A fully exhaustive scan still starts at 0, since a COUNT
# marker could in principle appear in other scripts the rules may later cover.)
TAPROOT_ACTIVATION_HEIGHT = 709632

# Block of the counters genesis transaction (COUNTERZERO, #0). `--from-genesis`
# starts here: by protocol there is no valid counter before #0, so a scan that
# trusts the genesis point can skip everything earlier.
COUNTERS_GENESIS_HEIGHT = 955251


@dataclass
class Config:
    # bitcoind JSON-RPC
    btc_rpc_url: str = field(default_factory=lambda: _env("BTC_RPC_URL", "http://127.0.0.1:8332"))
    btc_cookie_file: str = field(
        default_factory=lambda: _env("BTC_COOKIE_FILE", str(Path.home() / ".bitcoin" / ".cookie"))
    )
    btc_rpc_user: str = field(default_factory=lambda: _env("BTC_RPC_USER", ""))
    btc_rpc_password: str = field(default_factory=lambda: _env("BTC_RPC_PASSWORD", ""))

    # Counterparty Core v2 API
    cp_api_url: str = field(default_factory=lambda: _env("CP_API_URL", "http://127.0.0.1:4000"))

    # Storage
    data_dir: str = field(
        default_factory=lambda: _env(
            "COUNTER_DATA_DIR",
            str(Path(__file__).resolve().parent.parent / "data"),
        )
    )

    # Indexing range / behaviour
    # A first-time scan starts at block 0 by default (exhaustive). The CLI's
    # --from-taproot / --from-genesis flags (or COUNTER_START_HEIGHT) raise this
    # floor; stored sync progress always takes precedence on later runs.
    start_height: int = field(
        default_factory=lambda: _env_int("COUNTER_START_HEIGHT", 0)
    )
    confirmations: int = field(default_factory=lambda: _env_int("COUNTER_CONFIRMATIONS", 0))
    poll_interval: float = field(default_factory=lambda: _env_float("COUNTER_POLL_INTERVAL", 15.0))

    # HTTP
    http_timeout: float = field(default_factory=lambda: _env_float("COUNTER_HTTP_TIMEOUT", 30.0))

    @property
    def db_path(self) -> Path:
        return Path(self.data_dir) / "counters.db"

    @property
    def blobs_dir(self) -> Path:
        return Path(self.data_dir) / "blobs"

    def ensure_dirs(self) -> None:
        Path(self.data_dir).mkdir(parents=True, exist_ok=True)
        self.blobs_dir.mkdir(parents=True, exist_ok=True)
