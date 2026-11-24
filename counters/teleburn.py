"""Teleburn addresses: one provably unspendable Bitcoin address per
Counterparty asset, derived from the asset name and nothing else.

Anything sent to an asset's teleburn address — its own supply, its ownership
(an issuance with `transfer_destination`), other assets, sats, inscriptions —
is burned *into* that asset. The address is an ordinary P2TR output to every
wallet and to Counterparty Core, so no protocol change is needed for Core to
credit balances to it or to accept it as an owner; it simply has no key.

    m    = tagged_hash("CountersTeleburn", asset_name)   # ASCII, as Core stores it
    t    = tagged_hash("TapTweak", H.x || m)             # the BIP341 tweak
    Q    = H + t*G                                       # H = BIP341's NUMS point
    spk  = OP_1 <Q.x>
    addr = bech32m(hrp, 1, Q.x)

Why this is unspendable: the key path needs the discrete log of H, which
BIP341 constructs so that nobody knows it (H.x = sha256(G.x) lifted); the
script path needs `m` to be the Merkle root of a script tree someone can open,
and it is the hash of a non-script. Every address is the same unknown
discrete log plus a public tweak, so there is no cheaper "any asset" attack
than solving that one problem — the same assumption every Bitcoin signature
rests on. A scheme that put the asset id in the clear (a hash160 payload with
a fixed prefix, say) would instead let an attacker grind for ANY address in
the family and then issue the numeric asset that fell out; that is why the
address commits by hashing and is recognised from a table, not decoded.

The commitment is the canonical `asset` field — `XCP`, `PEPECASH`,
`A16756519242061643639` — never a subasset's longname, which is a label on a
numeric asset. The tag is the version: a changed scheme is a new tag and a new
address set, never a silent edit of this one.

This is a counters protocol rule (build reference v3 §10.3). It is written to
hold for any Counterparty asset so that Counterparty can adopt it unchanged.
"""

from __future__ import annotations

import re
from functools import lru_cache

from . import tap

TAG = "CountersTeleburn"

# BIP341's "nothing up my sleeve" internal key: the x coordinate of
# sha256(G.x) lifted to a point. Its discrete log is unknown.
NUMS_X = bytes.fromhex("50929b74c1a04954b78b4b6035e97a5e078a5a0f28ec96d547bfee9ace803ac0")

HRP = {"mainnet": "bc", "main": "bc", "testnet": "tb", "test": "tb",
       "testnet4": "tb", "signet": "tb", "regtest": "bcrt"}

# Counterparty asset names: XCP; four to twelve capitals not starting with A;
# or A followed by an integer above 26^12 that fits in 64 bits (numeric
# assets, which is what every subasset is underneath its longname).
_NAMED = re.compile(r"^[B-Z][A-Z]{3,11}$")
_NUMERIC = re.compile(r"^A[1-9][0-9]*$")
NUMERIC_MIN = 26 ** 12 + 1
NUMERIC_MAX = 2 ** 64 - 1


def canonical_asset_name(name: str) -> str:
    """`name` as Counterparty's canonical `asset` field, or ValueError saying
    why it is not one. Case is NOT folded: a lowercase slip must not derive a
    plausible-looking address for an asset that is spelled differently."""
    if not isinstance(name, str) or not name:
        raise ValueError("empty asset name")
    if name == "BTC":
        raise ValueError("BTC is not a Counterparty asset; it has no balances to burn")
    if name == "XCP" or _NAMED.match(name):
        return name
    if _NUMERIC.match(name):
        n = int(name[1:])
        if NUMERIC_MIN <= n <= NUMERIC_MAX:
            return name
        raise ValueError(f"numeric asset {name} is outside Counterparty's range "
                         f"(A{NUMERIC_MIN} .. A{NUMERIC_MAX})")
    if "." in name:
        raise ValueError(f"{name!r} is a subasset longname; derive from its numeric "
                         f"asset name (the `asset` field), which is what the ledger keys on")
    if name != name.upper():
        raise ValueError(f"{name!r} is not uppercase; Counterparty asset names are")
    raise ValueError(f"{name!r} is not a Counterparty asset name")


@lru_cache(maxsize=4096)
def _derive(tag: str, message: bytes) -> bytes:
    """The tweaked x-only key for any (tag, message) — the primitive. The
    public functions below fix the tag and the message's form. Cached: the
    pure-Python point multiplication costs milliseconds, and a list page
    asks for every counter's address at once."""
    m = tap._tagged_hash(tag, message)
    _, xonly = tap.taproot_tweak_pubkey(NUMS_X, m)
    return xonly


def commitment(asset: str) -> bytes:
    """`m`, the 32-byte commitment the tweak is built from."""
    return tap._tagged_hash(TAG, canonical_asset_name(asset).encode("ascii"))


def teleburn_xonly(asset: str) -> bytes:
    """The 32-byte x-only output key of `asset`'s teleburn address."""
    return _derive(TAG, canonical_asset_name(asset).encode("ascii"))


def teleburn_spk(asset: str) -> bytes:
    """The scriptPubKey: OP_1 PUSH32 <x>."""
    return b"\x51\x20" + teleburn_xonly(asset)


def teleburn_address(asset: str, hrp: str = "bc") -> str:
    """The bech32m address, for `hrp` (bc, tb, bcrt — see HRP)."""
    return tap.p2tr_address(teleburn_xonly(asset), hrp)


def hrp_for(network: str) -> str:
    try:
        return HRP[network.lower()]
    except KeyError:
        raise ValueError(f"unknown network {network!r}; one of {', '.join(sorted(HRP))}")


def burn_state(supply: int | None, locked: bool | None, owner_burned: bool | None,
               sink_balance: int | None) -> dict:
    """The burn flags from their live inputs (build reference v3 §10.3).

    `supply_burned`: no unit circulates outside the teleburn address. Core's
    `supply` already excludes destroyed units, so a `destroy` counts too, and
    an asset that never had supply is trivially there. `burned`: ownership
    and supply at the address and the supply locked — True when all three
    are known true, False when any is known false, None while unknown."""
    supply_burned = (None if supply is None or sink_balance is None
                     else sink_balance >= supply)
    flags = (owner_burned, supply_burned, locked)
    if any(f is False for f in flags):
        burned: bool | None = False
    elif all(f is True for f in flags):
        burned = True
    else:
        burned = None
    return {"owner_burned": owner_burned, "supply_burned": supply_burned,
            "locked": locked, "sink_balance": sink_balance, "burned": burned}


def is_teleburn_spk(spk: bytes, asset: str) -> bool:
    return spk == teleburn_spk(asset)


def is_teleburn_address(address: str, asset: str) -> bool:
    """Whether `address` is `asset`'s teleburn address, on any network.
    bech32 is case-insensitive, so the comparison folds case."""
    a = address.lower()
    return any(a == teleburn_address(asset, h) for h in ("bc", "tb", "bcrt"))
