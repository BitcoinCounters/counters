# Counters teleburn addresses

A counters protocol rule, implemented 2026-11-05. Normative text in
[`build-reference-v3.md` §10.3](build-reference-v3.md); this document is the
self-contained statement of the scheme — motivation, derivation, vectors,
the burn flags and the burn procedure — written so that Counterparty can
adopt it unchanged. It presumes nothing of Counterparty: the addresses work
with Core as it is.

## 1. What it is

Every Counterparty asset has one Bitcoin address, derived from its name
alone, that nothing can ever spend from: its **teleburn address**. Anything
sent there is burned *into* that asset — its own supply, its ownership
(an issuance with `transfer_destination`), other assets, sats, inscriptions,
runes. The address is an ordinary P2TR output to every wallet and to
Counterparty Core, which credits balances to it and accepts it as an `owner`
without any change. It simply has no key.

A counter is a numbered Counterparty issuance event, so a counter is burned
when its asset is: ownership at the teleburn address, every unit there or
destroyed, supply locked. Counters that share an asset (reinscriptions)
share the address and burn together, which is right: once ownership is gone
no further counter can be minted on that asset.

Why an address rather than Counterparty's `destroy`: a destroy removes units
but says nothing about ownership, and ownership is what lets a counter be
reinscribed. An address takes both, through messages Core already has, and
it is enumerable — an indexer can derive every asset's address and recognise
a burn on sight, with no declaration from the burner. A destroy still counts
toward the supply flag (§4).

## 2. Derivation

```
m    = tagged_hash("CountersTeleburn", asset_name)   # ASCII, the canonical `asset` field
t    = tagged_hash("TapTweak", H.x || m)             # BIP341 tweak
Q    = H + t·G                                       # H = BIP341 NUMS point
spk  = OP_1 <Q.x>                                    # 5120 || Q.x
addr = bech32m(hrp, 1, Q.x)                          # bc / tb / bcrt
```

- `tagged_hash(tag, msg) = sha256(sha256(tag) || sha256(tag) || msg)` (BIP340).
- `H.x = 50929b74c1a04954b78b4b6035e97a5e078a5a0f28ec96d547bfee9ace803ac0`:
  BIP341's "nothing up my sleeve" point, the x coordinate of
  sha256(uncompressed G) lifted. Its discrete log is unknown.
- `asset_name` is Counterparty's canonical `asset` field — `XCP`,
  `PEPECASH`, `A16756519242061643639` — as uppercase ASCII. For a subasset
  it is the numeric name, never the longname: the longname is a label, the
  numeric name is what the ledger keys on. Case is not folded; a lowercase
  spelling derives nothing rather than something plausible.
- Same scriptPubKey on every network; only the bech32m prefix differs.
- The tag is the version. A changed scheme is a new tag and a new address
  set, never a silent edit: every burn ever made is bound to this one.

### Test vectors (mainnet)

| asset | address |
|---|---|
| `XCP` | `bc1pj76jq96xhq25jcxl8qgrecz5sv2sfphj247emczw3pw6smhah6sqalf224` |
| `A16756519242061643639` (counter #28) | `bc1py6gs0j0duqtyu0xr8mp3ewjhu3l0tlhdfptv8zcqyf38qvuryhsseel6e2` |
| `PEPECASH` | `bc1p6c47mk09xk42u8aslc4c6u503n6s635st6hxmxj6p06nntjqxg2qj5wqch` |
| `XCP`, testnet | `tb1pj76jq96xhq25jcxl8qgrecz5sv2sfphj247emczw3pw6smhah6sq2hl9s6` |

Derived by `counters/teleburn.py`, re-derived by the standalone
[`teleburn_address.py`](teleburn_address.py) with its own curve arithmetic,
and confirmed by `bitcoin-cli decodescript` / `validateaddress`
(`witness_v1_taproot`). `tests/test_teleburn.py` pins them.

## 3. Why it is unspendable, and why this construction

- **Key path.** Spending needs the discrete log of `Q = H + t·G`, i.e. of
  `H`. Nobody knows it; that is what the NUMS construction is for.
- **Script path.** Spending needs `m` to be the Merkle root of a script tree
  someone can open. It is the hash of a non-script.
- **No "any asset" shortcut.** Every address is the same unknown discrete
  log plus a public tweak, so solving any one of them is solving that one
  128-bit problem — the assumption every Bitcoin signature rests on. This is
  the reason the scheme hashes rather than encodes. A decodable
  construction (a hash160 payload with a fixed 12-byte prefix and the asset
  id in the clear, giving readable `1CountersBurn…` addresses) would let an
  attacker grind keys until one's hash160 carried the prefix, about 2^96
  work, and then *issue* the numeric asset whose id fell out, holding the
  key to its burn address. 96-bit security is below what a permanent burn
  should rest on. Recognition is therefore by derivation from the asset,
  never by decoding the address; for counters the set of assets is known, so
  this costs nothing.
- Always a valid point, so no retry nonce (ord's "sha256 as x-only key"
  fails about half the time).
- The one axis where a hashed legacy payload would win is a post-quantum
  world, where a single discrete-log computation on `H` would open every
  address of this family at once. Not decisive today; recorded.

## 4. Burn state

All read live from Counterparty, none from the counters index:

| flag | true when |
|---|---|
| `owner_burned` | the asset's `owner` is its teleburn address |
| `supply_burned` | no unit circulates outside it: `balance at the address ≥ supply`. Core's `supply` already excludes destroyed units, so a `destroy` counts; an asset that never had supply is trivially there |
| `locked` | the supply lock |
| `burned` | all three. `null` while any is unknown and none is known false |

Partial states are real and are shown as partial. Once `owner_burned`, no
owner can sign another issuance, so the description is frozen in effect and
the counter family on that asset is closed.

Not in scope here, enumerable later without any change to the derivation:
sats, inscriptions and runes parked at the address (a `scantxoutset` or ord
lookup), and other assets burned into this one.

## 5. Burn procedure

Two ordinary Counterparty messages, in this order, both composed via
`/v2/addresses/{address}/compose/…` as plain OP_RETURN transactions — no
description, no taproot envelope, so none of them is a counter (rule R4):

1. **Supply.** `send` every circulating unit to the address. The destination
   travels in the OP_RETURN; no sats are burned with it. Skipped when
   nothing circulates.
2. **Ownership and lock, one message.** `issuance` from the owner with
   `quantity=0`, `divisible=<the asset's current value>`, `lock=true`
   (unless already locked), `transfer_destination=<address>`, no
   description. Core puts 330 sats of transfer dust on the address; they are
   burned with it. **Lock travels with the transfer** because after the
   transfer nobody can lock.

`counters wallet teleburn <ASSET | #N>` does exactly this, with one y/N for
both and `--dry-run` to sign both and broadcast neither. By default the
wallet must hold every circulating unit; `--partial` burns ownership and
what the wallet holds and leaves the rest with its holders, shown as a
partial burn. `counters teleburn-address <ASSET | #N>` prints the address
for any asset, counter or not, with `--network testnet|regtest`.

Still to confirm on a throwaway asset before the first real burn: that Core
applies lock *and* transfer from one combined issuance message. If it does
not, the command will send two issuances, lock first.

### Compose notes (from the October dry runs on Core 11.5.0)

- `divisible` must equal the asset's current value or compose fails with
  `cannot change divisibility`.
- Core locks a UTXO for a while after any compose uses it. Consecutive dry
  runs with the same inputs fail with `No UTXOs found …`; the wallet passes
  `disable_utxo_locks=true`.
- Core's own coin selection will spend a 546-sat inscription output. The
  wallet commands here select from the asset-holding address like `send`
  and `transfer-ownership` do; keep inscription UTXOs out of the funding
  address, or move them first (as was done in October, §7).

## 6. Indexer

Non-consensus throughout: the address is a pure function of a column the
index already has, and the flags are live state kept in the serve-time
cache beside the locks and re-asked on the same cadence (an open asset
every ten minutes; one with both locks, or fully burned, daily). Nothing is
hashed, nothing renumbers, no reindex, and a reorg that undoes a burn flips
the flags back on the next check. The API carries `teleburn: {address,
owner_burned, supply_burned, locked, sink_balance, burned}` on every counter
and the verdict flat as `teleburned` (also in `/facets`); the explorer shows
a `burned` badge and the address as a copyable fact (rules.md, rule 11);
`counters info` prints `teleburned` for an asset and the address under
`--detailed`.

Legacy burns — units at `1CounterpartyXXXXXXXXXXXXXXXUWLpVr` or an all-zero
bech32 — are not recognised as a teleburn of anything: they burn units
without saying into what, and they never take ownership. They remain what
they were, a destroy by other means.

## 7. Session log, 2026-10-16 (preparation)

- 20 inscription UTXOs moved from `xcp_taproot` to
  `bc1qytkvecngwt2a6jc2q4snueyjzzrhdp7puj846v`, one tx each, 0.5 sat/vB,
  100 sats fee each. Counterparty assets, balances and owners unchanged —
  confirming that the ordinals sat and the Counterparty asset are separate
  objects, and that a counter burn is Counterparty-only.
- Later the same day: the 12 xcp inscriptions in `countergents` (≈3.95 MB
  jpegs) and the one in `counts` (366 KB jpeg) were moved to the same bc1q
  address, one tx each. Every UTXO in both wallets was first checked on
  blockspace.to (`/output/<outpoint>`) for inscriptions and runes, and on
  Core (`/v2/utxos/<outpoint>/balances`) for attached balances: none
  attached, fee UTXOs clean. Counterparty state unchanged.
- ord 0.29 index was at block 800k; `ord wallet` refuses to run until the
  index reaches the tip, and refuses any Core wallet that is not exactly
  two `tr()` descriptors. Raw transactions via `bitcoin-cli` were used
  instead. The ord wallet `xcp_taproot_ordinals` exists and is empty.
- Compose checks of the send and the lock+transfer issuance were run
  against Core 11.5.0 (mainnet) on this machine; no burn was broadcast.
