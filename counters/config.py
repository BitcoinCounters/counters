"""Configuration for the counters indexer (Bitcoin Counters, protocol v3).

All values are overridable via environment variables so the same code runs
against a local node now and a different backend later.
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


# --- Protocol constants (build reference v3 §13) -----------------------------

# The literal, unencrypted marker a taproot REVEAL transaction carries in its
# OP_RETURN output. Classic OP_RETURN-encoded Counterparty data is ARC4-
# encrypted with the first input's prevout txid, so it can never show this
# marker in the clear; only taproot reveals do (build ref v3 §4).
CNTRPRTY_MARKER = b"CNTRPRTY"

# The exact reveal OP_RETURN script: OP_RETURN PUSH8 "CNTRPRTY".
REVEAL_OP_RETURN_SCRIPT = bytes.fromhex("6a08434e545250525459")

# Counterparty `taproot_support` activation on mainnet (v11.0.0). No
# qualifying event can exist before it (N3); the scan floor and the protocol
# genesis. Counter #0 = XDUALS at block 902,005.
GENESIS_HEIGHT = 902000

# Counterparty `extended_mime_types_support` activation on mainnet (v11.1.0).
# Gates the MIME classifier used to derive content bytes (build ref v3 §5.1).
EXTENDED_MIME_GATE = 952800

# Seed of the rolling consensus-hash chain (build ref v3 §7).
ROLLING_HASH_GENESIS_TAG = b"counters:v3:bitcoin-mainnet:902000"

# Assets the wallet refuses to operate on (they cannot be issued anyway).
RESERVED_ASSETS = frozenset({"BTC", "XCP"})


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

    # Indexing range / behaviour.
    # A first-time scan starts at the protocol genesis (block 902,000): by rule
    # N3 nothing qualifies earlier, so there is no exhaustive-from-0 mode.
    # Stored sync progress always takes precedence on later runs.
    start_height: int = field(
        default_factory=lambda: _env_int("COUNTER_START_HEIGHT", GENESIS_HEIGHT)
    )
    # Blocks to stay behind the tip. 6 is recommended for near-final numbering
    # (N4); 0 follows the tip and relies on rollback for reorgs.
    confirmations: int = field(default_factory=lambda: _env_int("COUNTER_CONFIRMATIONS", 0))
    poll_interval: float = field(default_factory=lambda: _env_float("COUNTER_POLL_INTERVAL", 15.0))

    # HTTP
    http_timeout: float = field(default_factory=lambda: _env_float("COUNTER_HTTP_TIMEOUT", 30.0))

    def __post_init__(self) -> None:
        # N3: nothing can qualify before genesis, so the floor travels with the
        # object — every constructor (CLI, tests, programmatic embedding) gets
        # the clamp, not just the counters entry point. A start height ABOVE
        # genesis is legal (resuming operators) but consensus-affecting on a
        # fresh DB; the indexer warns loudly in that case (see sync_to_tip).
        self.start_height = max(self.start_height, GENESIS_HEIGHT)

    @property
    def db_path(self) -> Path:
        return Path(self.data_dir) / "counters.db"

    @property
    def blobs_dir(self) -> Path:
        return Path(self.data_dir) / "blobs"

    def ensure_dirs(self) -> None:
        Path(self.data_dir).mkdir(parents=True, exist_ok=True)
        self.blobs_dir.mkdir(parents=True, exist_ok=True)
