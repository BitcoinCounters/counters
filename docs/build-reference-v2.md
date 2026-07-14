# Bitcoin Counters — Build Reference v2

**Status:** current implementation. This document is the authoritative,
human-readable description of the Counters protocol as implemented in this
repository. It supersedes `build-reference.pdf` (v1); see [§12](#12-changes-from-v1)
for the delta.

A **counter** is a numbered inscription: a file committed permanently to Bitcoin
in taproot witness data, bound to a Counterparty asset, and assigned a global,
gap-free number in order of creation. Counterparty carries identity, ownership,
naming, and transfer; the counters protocol only defines the **COUNT envelope**,
the **asset-binding** rule, the **validity** rules, and the **numbering**.

---

## 1. Design principles

- **Small surface.** The protocol adds only an envelope format and a binding
  rule on top of Bitcoin + Counterparty. Everything about ownership, naming, and
  transfer is delegated to the Counterparty asset.
- **Reuse ord.** The envelope is byte-for-byte an ordinals-style inscription
  envelope with a different marker (`COUNT`), so existing parsers work unchanged.
- **Counterparty is the oracle.** Asset existence, validity, and ownership are
  whatever Counterparty says they are. The indexer never overrides Counterparty.
- **Data-defined genesis.** There is no activation flag. Counter `#0` is simply
  the first valid counter on-chain.

---

## 2. Terminology

| Term | Meaning |
|------|---------|
| **Envelope** | The `OP_FALSE OP_IF … OP_ENDIF` COUNT data structure inside a tapscript. |
| **Commit tx** | Transaction that pays to the P2TR address committing to the tapscript. |
| **Reveal tx** | Transaction that script-path-spends the commit output, exposing the envelope in its witness. |
| **Creation** *(binding path)* | A counter bound to an asset that is *created* by a Counterparty issuance in the same (reveal) transaction. |
| **Reinscription** *(binding path)* | A counter bound to a *pre-existing* asset, with no Counterparty message, authorised by the owner's signature. |
| **Original** *(ordinal)* | The lowest-numbered counter on a given asset. |
| **`reinscription` flag** *(ordinal)* | Stored boolean: `true` iff the asset already had a counter when this one was recorded — i.e. "not the original". Independent of the binding path (see [§8](#8-numbering)). |

---

## 3. The COUNT envelope

### 3.1 Structure

```
OP_FALSE OP_IF
  PUSH "COUNT"                 # 5-byte marker (OP_PUSHBYTES_5 0x434f554e54)
  PUSH 0x01  PUSH <content_type>   # tag 1 = content type (MIME); optional
  PUSH 0x02  PUSH <asset>          # tag 2 = target asset; optional
  OP_0                         # empty push: field section ends, body begins
  PUSH <body chunk 1>          # file bytes, ≤ 520 bytes per push
  PUSH <body chunk 2>
  ...
OP_ENDIF
```

The `OP_FALSE OP_IF … OP_ENDIF` block never executes, so its contents are inert
data. The bytes ride in a section the script interpreter reads past but never
runs.

### 3.2 Fields

- **Marker** — the ASCII bytes `COUNT`. An envelope is a COUNT envelope **iff**
  its first push equals `COUNT`. The legacy 7-byte `COUNTER` marker and ord's
  `ord` marker are **not** recognised.
- **Tag 1 — content type** (`0x01`): the next push is the MIME type (e.g.
  `image/png`). Optional; may be empty. For compatibility, the legacy `OP_1`
  (`0x51`) pushnum form of the tag is also accepted on parse.
- **Tag 2 — target asset** (`0x02`): the next push is the asset's canonical
  name or longname as UTF-8 (e.g. `RAREPEPE`, `A95428956661682177`, or a
  subasset longname `PARENT.CHILD`). Names the asset the counter binds to (see
  [§6](#6-asset-binding)). Optional.
- **Separator** — a single empty push (`OP_0`) ends the field section; every
  push after it is body.
- **Body** — the file, as the concatenation of all pushes after the separator.
  Each push is at most **520 bytes** (`MAX_PUSH`). May be empty (an empty-body
  counter is valid).

Unknown field elements before the separator are skipped (a provisional
"ignore unknown fields" policy; a strict ruleset is deferred until the tag set
is frozen).

### 3.3 Parsing (identity)

To locate envelopes in a script, scan for the opener `OP_FALSE OP_IF` followed
immediately by a push of `COUNT`, then read fields until the `OP_0` separator
and collect body pushes until `OP_ENDIF`. Parsing is tolerant of truncated
pushes (a malformed tail yields no envelope rather than an error).

---

## 4. The tapscript leaf

The envelope is embedded in a **taproot script-path leaf** that also carries a
key-path signature check:

```
<32-byte x-only pubkey> OP_CHECKSIG      # the real spend condition
OP_FALSE OP_IF … OP_ENDIF                # the COUNT envelope (inert data)
```

The reference builder emits the key check **first**, then the envelope
(ord-style). Because the parser locates the envelope by its `OP_FALSE OP_IF
"COUNT"` opener regardless of position, the traditional ordinals layout
(envelope first, key check last) is equally valid.

- **Leaf version:** `0xc0` (BIP342 tapscript).
- **Reveal key:** an ephemeral x-only key; it is both the taproot internal key
  and the key checked by `OP_CHECKSIG`.

---

## 5. On-chain construction (commit / reveal)

A tapscript is only revealed when it is *spent*, so minting takes two
transactions.

### 5.1 Commit

1. Build the leaf (§4) and compute the tapleaf hash:
   `merkle_root = TapLeaf(0xc0 ‖ ser_script(leaf))`.
2. Tweak the internal key: `Q = P + TapTweak(P ‖ merkle_root)·G`, where `P` is
   the reveal x-only key.
3. The commit address is the P2TR output for `Q`. Broadcasting a transaction
   that pays this address is the **commit**. On-chain it looks like an ordinary
   `bc1p…` payment; the envelope is hidden inside the commitment.

### 5.2 Reveal

The **reveal** transaction script-path-spends the commit output. Its
envelope-bearing input witness is:

```
[ schnorr_signature , leaf_script , control_block ]
```

where `control_block = (0xc0 | parity_bit) ‖ internal_xonly`. This is the only
point at which the envelope bytes appear on-chain (in witness data, which
receives the segwit weight discount).

The reveal also carries the Counterparty message when one is needed:

- **Creation:** a Counterparty issuance is composed as a legacy `OP_RETURN`
  (destinations before it, change after it). The reveal's `vin[0]` is the
  Counterparty *source*.
- **Reinscription:** **no** Counterparty message and no destination outputs. The
  reveal's `vin[0]` spends an input from the asset's owner address, proving
  issuance rights (see [§7](#7-reinscriptions)).

---

## 6. Asset binding

> **This is the core of v2.** In v1 the presence of the tag-2 asset name *was*
> the signal that distinguished a reinscription from a creation. In v2 the
> envelope **always** names its asset, and binding follows a priority rule.

Given a transaction with exactly one COUNT envelope, the counter binds to an
asset as follows:

1. **Envelope asset (authoritative).** If the envelope names an asset `A`
   (tag 2), use it if the binding is valid:
   1. **Creation** — a Counterparty issuance in the **same transaction** that is
      `valid` and a `creation`, whose `asset` **or** `asset_longname` equals `A`.
      Recorded as a creation (`reinscription = false`).
   2. **Reinscription** — otherwise, if `A` already exists and the transaction
      spends an input from `A`'s owner (issuance-rights holder) **as of this
      block**, record a reinscription.
2. **Same-tx issuance (fallback).** If the envelope names no asset, or the named
   asset does not resolve to a valid binding, bind to whatever asset the
   transaction validly *creates* (a `valid` `creation` issuance).
3. **Otherwise** it is **not** a valid counter.

Minting always writes the asset name into the envelope, so real creations take
path 1.i. The fallback (path 2) exists for backward compatibility with legacy
counters whose envelopes omit the asset name.

### 6.1 Disallowed bindings

A binding is rejected — regardless of path — when:

- the asset is **reserved** (`BTC` or `XCP`); or
- the asset's `asset_id` is `0` or `1` (i.e. BTC / XCP).

---

## 7. Reinscriptions

A reinscription attaches a new counter to a **pre-existing** asset without any
Counterparty message. Because there is no on-chain message binding it, authority
is proven cryptographically:

- The **owner** of the asset as of the block is the `issuer` of the most recent
  `valid` issuance at or before that height (ordered by `block_index`, then
  `tx_index`). This is the current issuance-rights holder, which may differ from
  the original `issuer` after a transfer.
- The reveal transaction must **spend an input from that owner address**.
  Producing that input's signature requires the owner's key, proving control.
- If the owner cannot be determined, or is not among the transaction's input
  addresses, or the input prevouts cannot be read (e.g. `txindex=1` is not
  enabled), the reinscription is **not** recorded.

Each reinscription is itself a new, permanently numbered counter. One asset can
therefore back many counters.

---

## 8. Numbering

- Counters are numbered **globally, gap-free, starting at `0`**.
- Order is **(block height, then position within the block)** — the position is
  the transaction's index in the block. This matches the Ordinals scheme.
- Only **valid** counters receive a number. The next number is `MAX(number) + 1`.
- **Original vs `reinscription` flag:** the first counter recorded on a given
  asset is its *original* (`reinscription = false`); any later counter on the
  same asset is stored with `reinscription = true`. This flag is **ordinal**, not
  a record of the binding path: it means "the asset already had a counter", and
  is set purely from `store.has_asset(asset)` at record time.
- Consequently a **creation** is always the first counter on its (newly created)
  asset, hence always an original (`false`). A **reinscription-path** counter is
  an original (`false`) when it is the first counter on a pre-existing asset, and
  `true` only for the second and later counters on that asset.

---

## 9. Validity — checklist

A transaction records a counter iff **all** hold:

1. Its witness contains a **well-formed COUNT envelope** (§3).
2. There is **exactly one** COUNT envelope in the transaction. Transactions with
   more than one envelope are skipped.
3. The transaction has **not** already produced a counter (dedup by `mint_txid`).
4. The asset **binding resolves** per [§6](#6-asset-binding).
5. The bound asset is **not reserved** and its `asset_id ∉ {0, 1}`.
6. For a **creation**: the matching issuance is `status == "valid"` and its
   `asset_events` includes `creation`.
7. For a **reinscription**: the owner as of the block is among the transaction's
   input addresses.

Validity ultimately depends on Counterparty's verdict, not on any explorer's
listing.

---

## 10. Indexing algorithm

For each block, in order:

1. Scan every input's witness for COUNT envelopes. Collect transactions with
   **exactly one** envelope as candidates (log and skip any with more than one).
2. If there are candidates, fetch the block's Counterparty issuances once.
3. For each candidate, apply the binding rule (§6) and, on success, store the
   counter: persist the body blob (content-addressed by SHA-256), assign the
   next number, and record the row.
4. Advance the sync cursor (`last_height`, `last_block_hash`) and commit.

### 10.1 Chain-tip safety

The indexer **never indexes past Counterparty's parsed height**. The target tip
is `min(bitcoind_height, counterparty_height) − confirmations`. Counterparty is
the oracle: walking blocks it has not parsed would record nothing for them,
advance the cursor, and silently skip any counters in the gap.

### 10.2 Scan range

- A first-time scan starts at block `0` (exhaustive) by default.
- `--from-taproot` starts at the taproot activation height (`709632`); no
  witness-based counter can exist before it.
- `--from-genesis` starts at the counters genesis block (`955251`); by protocol
  there is no valid counter before `#0`.
- Stored sync progress always takes precedence on later runs.

---

## 11. Enrichment (non-consensus)

Each stored counter also carries best-effort metadata that never gates validity:
inscription cost (commit + reveal fee and serialized size), `asset_id`,
`asset_longname`, `owner`, `divisible`, `supply`, `cp_tx_index`, and
`xcp_burned`. A failure to fetch enrichment must not prevent a valid counter
from being recorded.

---

## 12. Changes from v1

| | v1 | v2 |
|---|----|----|
| **Envelope asset tag** | Present **only** for reinscriptions. | Present for **all** counters (creations included). |
| **Creation signal** | *Absence* of the tag. | A `valid` `creation` issuance in the same tx for the envelope's asset. |
| **Reinscription signal** | *Presence* of the tag. | The envelope's asset pre-exists and no same-tx issuance creates it; owner signature authorises. |
| **Binding** | Tag present → reinscription; absent → same-tx issuance. | Envelope asset first (creation, else reinscription); **fallback** to the same-tx issuance; else invalid. |

**Backward compatibility.** Both legacy shapes still index correctly:

- A **legacy creation** (no tag) resolves via the same-tx issuance fallback
  (§6, path 2) — unchanged behaviour.
- An **existing reinscription** (tag + no message) fails the creation check and
  authorises via the owner's signature (§6, path 1.ii) — unchanged behaviour.

---

## 13. Constants

| Name | Value |
|------|-------|
| `COUNT_MARKER` | ASCII `COUNT` (`0x434f554e54`) |
| `CONTENT_TYPE_TAG` | `0x01` (legacy `OP_1` `0x51` also accepted on parse) |
| `ASSET_TAG` | `0x02` |
| `RESERVED_ASSETS` | `{ BTC, XCP }` |
| `CREATION_EVENTS` | `{ creation }` |
| `MAX_PUSH` | `520` bytes per data push |
| `LEAF_VERSION_TAPSCRIPT` | `0xc0` |
| `TAPROOT_ACTIVATION_HEIGHT` | `709632` |
| `COUNTERS_GENESIS_HEIGHT` | `955251` (block of counter `#0`, asset `COUNTERZERO`) |

---

*Generated from the reference implementation in this repository. Where this
document and the code disagree, the code is authoritative — please file a fix.*
