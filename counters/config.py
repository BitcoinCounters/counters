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

# The Bitcoin network this process talks to. Counterparty Core's own
# activation table (protocol_changes.json) has no per-feature regtest height:
# every protocol change, taproot_support and extended_mime_types_support
# included, is active from block 0 on regtest (confirmed against a live
# regtest node: counterpartycore.lib.parser.protocol.enabled() short-circuits
# `if config.REGTEST: return True`). So unlike testnet3/testnet4, regtest
# needs no activation height of its own — genesis is simply 0.
NETWORK = _env("COUNTER_NETWORK", "mainnet")
if NETWORK not in ("mainnet", "regtest"):
    raise ValueError(f"unsupported COUNTER_NETWORK {NETWORK!r} (expected 'mainnet' or 'regtest')")

# Counterparty `taproot_support` activation (v11.0.0). No qualifying event can
# exist before it (N3); the scan floor and the protocol genesis. On mainnet,
# counter #0 = XDUALS at block 902,005; on regtest, every change is active
# from genesis, so the floor is 0.
_GENESIS_HEIGHTS = {"mainnet": 902000, "regtest": 0}
GENESIS_HEIGHT = _GENESIS_HEIGHTS[NETWORK]

# Counterparty `extended_mime_types_support` activation (v11.1.0). Gates the
# MIME classifier used to derive content bytes (build ref v3 §5.1).
_EXTENDED_MIME_GATES = {"mainnet": 952800, "regtest": 0}
EXTENDED_MIME_GATE = _EXTENDED_MIME_GATES[NETWORK]

# Seed of the rolling consensus-hash chain (build ref v3 §7). Network-tagged
# so a regtest index's hash chain can never collide with / be mistaken for a
# mainnet one.
ROLLING_HASH_GENESIS_TAG = f"counters:v3:bitcoin-{NETWORK}:{GENESIS_HEIGHT}".encode()

# Default local endpoints per network. bitcoind's regtest RPC port (18443)
# and Counterparty Core's regtest API port (24000) both differ from mainnet's
# — verified against a live `counterparty/counterparty` regtest container.
_DEFAULT_BTC_RPC_URLS = {
    "mainnet": "http://127.0.0.1:8332",
    "regtest": "http://127.0.0.1:18443",
}
_DEFAULT_CP_API_URLS = {
    "mainnet": "http://127.0.0.1:4000",
    "regtest": "http://127.0.0.1:24000",
}

# Assets the wallet refuses to operate on (they cannot be issued anyway).
RESERVED_ASSETS = frozenset({"BTC", "XCP"})


@dataclass
class Config:
    # Bitcoin network: 'mainnet' or 'regtest'. Governs the protocol genesis /
    # MIME gate heights and the default local RPC endpoints below; see NETWORK
    # at module level. Stored per-instance too, since the wallet's address
    # encoding (bech32 HRP, WIF/xprv version bytes) needs it at call time.
    network: str = field(default_factory=lambda: NETWORK)

    # bitcoind JSON-RPC
    btc_rpc_url: str = field(
        default_factory=lambda: _env("BTC_RPC_URL", _DEFAULT_BTC_RPC_URLS[NETWORK])
    )
    btc_cookie_file: str = field(
        default_factory=lambda: _env("BTC_COOKIE_FILE", str(Path.home() / ".bitcoin" / ".cookie"))
    )
    btc_rpc_user: str = field(default_factory=lambda: _env("BTC_RPC_USER", ""))
    btc_rpc_password: str = field(default_factory=lambda: _env("BTC_RPC_PASSWORD", ""))

    # Counterparty Core v2 API
    cp_api_url: str = field(
        default_factory=lambda: _env("CP_API_URL", _DEFAULT_CP_API_URLS[NETWORK])
    )
    # Counterparty Core's ledger database, read directly (read-only) while
    # Core is catching up and its API answers "not ready" (see ledger.py).
    # Default: Core's own mainnet location. Used only if the file exists, so a
    # remote Core (docker, another host) simply never has one. Not
    # network-scoped like the RPC/API defaults above: a regtest Core's ledger
    # lives wherever its container/data-dir puts it, so there's no equally
    # standard default to guess — CP_DB_PATH is the way to point at it.
    cp_db_path: str = field(
        default_factory=lambda: _env(
            "CP_DB_PATH", str(Path.home() / ".local" / "share" / "counterparty" / "counterparty.db")
        )
    )

    # MARA Slipstream — out-of-band submission for oversized inscriptions.
    # The key is OPTIONAL: submission works unauthenticated, and a key only
    # applies whatever fee discount MARA has assigned it.
    slipstream_api_url: str = field(
        default_factory=lambda: _env("SLIPSTREAM_API_URL", "https://slipstream.mara.com")
    )
    slipstream_api_key: str = field(default_factory=lambda: _env("SLIPSTREAM_API_KEY", ""))

    # Storage. Network-scoped by default (data/, data-regtest/) so a stray
    # COUNTER_NETWORK switch can never read or write another network's DB —
    # the two use incompatible genesis heights and hash chains and would
    # silently corrupt each other's index if they shared one file.
    data_dir: str = field(
        default_factory=lambda: _env(
            "COUNTER_DATA_DIR",
            str(
                Path(__file__).resolve().parent.parent
                / ("data" if NETWORK == "mainnet" else f"data-{NETWORK}")
            ),
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

    @property
    def social_dir(self) -> Path:
        """Rendered `og:image` cache. Derived, not consensus data: safe to wipe,
        and rebuilt on demand (keyed by content hash + renderer version)."""
        return Path(self.data_dir) / "social"

    def ensure_dirs(self) -> None:
        Path(self.data_dir).mkdir(parents=True, exist_ok=True)
        self.blobs_dir.mkdir(parents=True, exist_ok=True)
