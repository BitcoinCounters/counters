# Teleburn addresses — implementation plan

Plan, 2026-11-03; **built 2026-11-05** in the Python repo (§4.1–4.6 below).
`teleburn.md` is now the self-contained statement of the scheme. Taproot was
confirmed over a decodable legacy form on 2026-11-05: a legacy payload with
the asset id in the clear would drop the "any asset" attack to 2^96 (grind a
key whose hash160 carries the fixed prefix, then issue the numeric asset
that falls out), against 2^128 for the NUMS tweak. Still open: §4.7
(counters.fun, which syncs from this server into its own database) and the
throwaway-asset check in §5, step 2.

## 0. The one-paragraph version

Every counter's Counterparty asset gets one Bitcoin address, derived from
the asset name alone, that nobody can ever spend from. It is the asset's
**teleburn address**: anything sent there — the asset's own supply, its
ownership, other assets, sats, inscriptions — is burned *into* that asset.
The counters indexer derives the address for each counter, watches what sits
at it, and shows a counter as burned when its asset's ownership and supply
have gone there and the asset is locked.

This is a **counters protocol** rule: it is specified in the build reference,
implemented by this indexer, and scoped to counter assets. The derivation is
written so that it works for *any* Counterparty asset and needs no change to
Counterparty Core (the address is an ordinary P2TR address to every wallet
and to Core). That is deliberate: the intent is that Counterparty adopts the
same derivation later, unchanged, so a counter burned under the counters rule
is already burned under the Counterparty one. Until then, Counterparty is
not asked for anything.

## 1. Derivation (the protocol part)

```
m    = tagged_hash("CountersTeleburn", asset_name)       # ASCII, as Core stores it
t    = tagged_hash("TapTweak", H.x || m)                 # BIP341 tweak
Q    = H + t·G                                           # H = BIP341 NUMS point
spk  = OP_1 <Q.x>                                        # 5120 || Q.x
addr = bech32m(hrp, 1, Q.x)                              # bc / tb / bcrt
```

- `H.x = 50929b74c1a04954b78b4b6035e97a5e078a5a0f28ec96d547bfee9ace803ac0`
  (the BIP341 "nothing up my sleeve" point: sha256 of G's x, lifted).
- `asset_name` is the canonical `asset` field: `XCP`, `PEPECASH`,
  `A16756519242061643639`. For a subasset that is the numeric name, never the
  longname — the longname is a label, the numeric name is the ledger key.
  Uppercase ASCII only; Core already enforces that.
- Unspendable by construction: the key path needs the discrete log of H
  (unknown), the script path needs `m` to be a Merkle root someone can open
  (it is the hash of a non-script). Always a valid point, so no nonce loop
  (ord's "sha256 as x-only key" fails about half the time).
- Same scriptPubKey on every network; only the bech32m prefix differs.
- One address per **asset**, so counters that share an asset (N6 siblings)
  share it. That is correct: burning the asset burns every counter event on
  it, and once ownership is gone no further counter can be minted on it.

Changes from the October draft, and why:

| draft | now | reason |
|---|---|---|
| tag `CountersTeleburn` | unchanged | it is the counters protocol's tag. If Counterparty adopts the scheme it adopts the tag with it: the addresses are the protocol, and renaming the tag would orphan every burn made before adoption |
| message `asset:NAME` with a prefix grammar | bare `NAME` | one asset, one address, derived from the asset name and nothing else; the tag already domain-separates. Other target kinds (`counter:`, `inscription:`, `eth:`) get their own tag if ever wanted, not a prefix |
| no version in the tag | still none | the tag *is* the version: a changed scheme is a new tag and a new address set, never a silent edit |

The tag and message are the two things to fix before the first real burn.
After that they cannot move — by us or by Counterparty.

### Test vectors (mainnet, `CountersTeleburn`, bare asset name)

| asset | address |
|---|---|
| `XCP` | `bc1pj76jq96xhq25jcxl8qgrecz5sv2sfphj247emczw3pw6smhah6sqalf224` |
| `A16756519242061643639` | `bc1py6gs0j0duqtyu0xr8mp3ewjhu3l0tlhdfptv8zcqyf38qvuryhsseel6e2` |
| `PEPECASH` | `bc1p6c47mk09xk42u8aslc4c6u503n6s635st6hxmxj6p06nntjqxg2qj5wqch` |
| `XCP` on testnet | `tb1pj76jq96xhq25jcxl8qgrecz5sv2sfphj247emczw3pw6smhah6sq2hl9s6` |

Derived with `counters/tap.py` (`taproot_tweak_pubkey` + `p2tr_address`),
which reproduces the draft's `asset:A167…` vector bit for bit, and that
vector was checked against `bitcoin-cli decodescript`. The vectors above
differ from the draft's only because the message lost its `asset:` prefix.
They get re-checked the same two ways once §6 is confirmed.

## 2. What "burned" means

All read from Counterparty, none from the counters index. Per asset:

| flag | true when |
|---|---|
| `owner_burned` | `assets.owner == teleburn address` |
| `supply_burned` | every issued unit is at the teleburn address or destroyed: `balance_at_sink + destroyed == issued` (so `destroy` still counts) |
| `locked` | `assets.locked` (already tracked) |
| `burned` | all three |

Partial states are shown as partial (ownership gone but supply circulating,
etc.). Once `owner_burned`, the description is also frozen in effect: no
owner can sign a `change_description` or a lock. The order to do it in is
therefore **lock, then transfer** — after the transfer nobody can lock.

What is *not* in scope now, and left as a later phase: sats, inscriptions
and runes parked at the address (a bitcoind `scantxoutset` or ord lookup),
and other assets burned into this one. The address makes all of that
enumerable later without any change to the derivation.

## 3. Non-consensus, by design

The address is a pure function of a column the index already has. Burn flags
are live Counterparty state, like the supply and description locks. So:

- nothing is hashed, nothing renumbers, no reindex — the same tier as
  `asset_locks` (build reference §10, "enrichment");
- a reorg that undoes a burn simply flips the flag back on the next check;
- `build-reference-v3.md` is the normative home: a new enrichment
  subsection (§10.3) with the derivation and the three flags, the constants
  in §13. Validity rules R1–R4 are untouched.

## 4. Work, in order

### 4.1 `counters/teleburn.py` — the derivation (small)

- `teleburn_xonly(asset) -> bytes`, `teleburn_spk(asset) -> bytes`,
  `teleburn_address(asset, hrp="bc") -> str`, `is_teleburn_spk(spk, asset)`.
- Rejects anything that is not a canonical asset name (so a longname or
  lowercase slip cannot derive a wrong, plausible-looking address).
- Tests: the vectors above; the old draft vector under the old tag (proves
  the primitive); hrp variants; rejection cases.
- `docs/teleburn_address.py` becomes a thin wrapper or is deleted — one
  implementation, not two.

### 4.2 Counterparty client — two reads

- `get_address_balance(address, asset) -> int` via
  `/v2/addresses/{addr}/balances/{asset}`.
- `get_asset_issued(asset)` if `supply + destroyed` is not already exact
  (`get_asset_destroyed` exists; confirm `supply` excludes destroyed as its
  docstring says).

### 4.3 Store — live-state columns, same refresher

- Add to `asset_locks` (rename in comments to "live asset state", keep the
  table name): `owner_burned INTEGER`, `sink_balance INTEGER`,
  `destroyed INTEGER`, with `_migrate` adding them.
- `set_locked` grows into `set_asset_state(...)` taking the new fields; the
  cadence in `assets_due_lock_check` already re-asks open assets often and
  settled ones rarely, which is the right cadence for burns too (a burned
  asset stays burned except across a reorg).
- No change to the `counters` table or the rolling hash.

### 4.4 Server / API

- `record_dict` always includes `teleburn_address` (pure, free).
- `_api_counter` and the background refresher fill
  `teleburn: {address, owner_burned, supply_burned, locked, burned,
  sink_balance, destroyed}` from the live read, and persist it.
- `/c/<n>` preview reads the persisted state (never queries backends — keep
  that rule).
- Explorer SPA: the address on the counter page next to the lock line, a
  `burned` badge, and the partial states spelled out. Follow the
  description-lock pattern from commit `cb30f42`.

### 4.5 CLI

- `counters teleburn-address <ASSET | #n>` — read-only, no wallet, prints the
  address, scriptPubKey and the x-only key. Also for assets that are not
  counters (it is a Counterparty-wide function).
- `counters wallet teleburn <ASSET | #n>` — the burn, two Counterparty
  transactions, each with the usual summary and y/N, `--dry-run`, `--yes`:
  1. `send` the full balance to the teleburn address (enhanced send: the
     destination travels in the OP_RETURN, no dust). Refuses if the wallet
     does not hold the full supply, unless `--partial`.
  2. `issuance` with `quantity=0`, `lock=true`,
     `transfer_destination=<teleburn address>`, no description (an
     OP_RETURN message, so R4 never sees a new counter). Composes 330 sats
     of transfer dust to the address, burned with it.
  - Interactive when bare, over the same `_held` menu as `market.py`.
  - Hard guards carried over from `burn_ordinal.py`: never spend an
    inscription UTXO or a UTXO carrying attached balances as an input; pass
    `inputs_set`.
- The existing `burn` (destroy) command stays as it is; its result is
  recognised by the `destroyed` term in `supply_burned`.

### 4.6 Docs

- `build-reference-v3.md`: §10.3 "Teleburn (enrichment)", constants in §13.
- `rules.md`: a display rule for the burned badge and partial states.
- `teleburn.md` rewritten from session notes into a self-contained
  statement of the scheme — motivation, derivation, vectors, the three
  flags, the burn procedure, and recognising legacy burns
  (`1CounterpartyXXXX…`, all-zero bech32) as "burned, untargeted" — so that
  it can be lifted into a Counterparty proposal later without rewriting.
  Written as a counters document; it does not presume adoption.
- README: the two commands.

### 4.7 counters.fun

Reads the API, so: show the address and the badge on the counter page, a
"burned" facet if the list endpoint exposes the flag. After 4.4 ships.

## 5. Verification before the first real burn

1. Freeze the tag. Regenerate vectors from `teleburn.py` and from the
   standalone script independently; check each with `bitcoin-cli
   decodescript` and `validateaddress`.
2. On a throwaway asset (regtest first, then mainnet with a worthless one):
   send to the address, confirm Core credits the balance; issuance with
   `lock=true` + `transfer_destination`, confirm Core applies lock *and*
   transfer from one message (open item from the draft). If it does not,
   the CLI sends two issuances.
3. Confirm the flags flip in the explorer, and flip back on a regtest
   `invalidateblock`.
4. Confirm `ord` and a couple of wallets display the address as plain P2TR
   and that nothing composes a spend from it.

## 6. Decisions taken in this plan (say if any should change)

- A counters protocol rule, specified in the build reference; Counterparty
  adoption is a goal, not a dependency. Nothing waits on Counterparty.
- Tag `CountersTeleburn` (kept from the draft), message = bare canonical
  asset name.
- Supply burn is a `send` to the address (what makes the address the point),
  with `destroy` recognised as equivalent.
- Live-state cache in the existing `asset_locks` table rather than a new one.
- Bitcoin-layer contents of the address (sats, inscriptions) deferred.
