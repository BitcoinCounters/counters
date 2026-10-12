"""Is the Counterparty node new enough to compose for the current chain?

Core checks its own version against the published `protocol_changes.json`
only when its process starts. A node left running across a release never
looks again, keeps composing with the old rules, and parses the results as
valid, so nothing warns until a newer node re-parses the blocks. That is how
ten DEGENT subassets were composed on a v11.4.0 node a day after
`require_reveal_source_signature` activated (mainnet block 969,320), each
costing about a million sats for a reveal the network ignores.

So the wallet checks before every inscription instead. Two layers:

  * a floor compiled in: `MIN_CORE_VERSION`, the oldest Core whose compose
    output this code knows how to sign; and
  * the live list of protocol changes, fetched from counterparty.io when
    reachable: the node must satisfy the minimum version of every change
    that is already active, or activates within `LOOKAHEAD_BLOCKS` (a reveal
    composed today can confirm days from now).

The check never phones anywhere but counterparty.io, and a failed fetch is a
warning, not a block: the compiled floor still applies.
"""

from __future__ import annotations

import re
import sys

import requests

MIN_CORE_VERSION = (11, 5, 0)
PROTOCOL_CHANGES_URL = "https://www.counterparty.io/protocol_changes.json"
LOOKAHEAD_BLOCKS = 4320          # ~30 days: a commit/reveal pair can take that long to land
_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def parse_version(text: str | None) -> tuple[int, int, int] | None:
    """'11.5.0', 'v11.5.0-rc.1' -> (11, 5, 0); None when unreadable."""
    m = _VERSION_RE.search(text or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def fetch_protocol_changes(timeout: float = 5.0) -> dict | None:
    """The published protocol_changes.json, or None when it cannot be read."""
    try:
        resp = requests.get(PROTOCOL_CHANGES_URL, headers={"cache-control": "no-cache"}, timeout=timeout)
        data = resp.json()
    except Exception:  # noqa: BLE001 - any failure means "unknown", handled by the caller
        return None
    return data if isinstance(data, dict) else None


def _activation(change: dict, network: str) -> int | None:
    key = "block_index" if network == "mainnet" else f"{network}_block_index"
    value = change.get(key)
    return value if isinstance(value, int) else None


def version_problems(node_version: str | None, height: int, changes: dict | None, *,
                     network: str = "mainnet", lookahead: int = LOOKAHEAD_BLOCKS) -> list[str]:
    """Reasons the node must NOT compose right now; empty when it may.

    `changes` is the parsed protocol_changes.json (None skips that layer).
    """
    version = parse_version(node_version)
    if version is None:
        return [f"cannot read the node's version ({node_version!r}); refusing to compose blind"]
    problems = []
    if version < MIN_CORE_VERSION:
        problems.append(
            f"Counterparty Core v{'.'.join(map(str, version))} is older than "
            f"v{'.'.join(map(str, MIN_CORE_VERSION))}, the first release whose reveals the "
            f"network still accepts (require_reveal_source_signature, block 969,320)")
    for name, change in (changes or {}).items():
        if not isinstance(change, dict):
            continue
        try:
            minimum = (int(change["minimum_version_major"]), int(change["minimum_version_minor"]),
                       int(change["minimum_version_revision"]))
        except (KeyError, TypeError, ValueError):
            continue
        if version >= minimum:
            continue
        activation = _activation(change, network)
        if activation is None or activation > height + lookahead:
            continue
        when = "already active" if activation <= height else f"activates in {activation - height:,} blocks"
        problems.append(
            f"protocol change '{name}' needs Core v{'.'.join(map(str, minimum))} "
            f"(block {activation:,}, {when}); the node runs v{'.'.join(map(str, version))}")
    return problems


def require_current_node(cp, *, network: str = "mainnet", out=sys.stderr) -> bool:
    """Print the problems and return False when the node is too old to compose."""
    status = cp.status()
    version = status.get("version")
    height = int(status.get("counterparty_height") or 0)
    changes = fetch_protocol_changes()
    if changes is None:
        print("note: could not fetch counterparty.io/protocol_changes.json; checking only the "
              f"built-in floor (Core v{'.'.join(map(str, MIN_CORE_VERSION))}).", file=out)
    problems = version_problems(version, height, changes, network=network)
    for p in problems:
        print(f"refusing to compose: {p}", file=out)
    if problems:
        print("hint: upgrade counterparty-server, let it re-parse, then retry. A reveal composed by an "
              "outdated node is ignored by the network and its fees are lost.", file=out)
    return not problems
