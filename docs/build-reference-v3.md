# Bitcoin Counters — Build Reference v3

**Status:** current protocol. This document is the authoritative, human-readable
description of the Counters protocol as implemented in this repository (the
`counters` CLI; python package `counters`). It supersedes `build-reference-v2.md`; see
[§12](#12-changes-from-v2) for the delta.

A **counter** is a numbered file event: a file committed permanently to Bitcoin
as a **Counterparty asset description carried in a Counterparty taproot
envelope** (Counterparty Core v11+), and assigned a global, gap-free number in
order of creation. Counterparty carries identity, ownership, naming, transfer,
*and the content itself*; the counters protocol only defines **which
Counterparty events qualify** and **how they are numbered**.

---

## 1. Design principles

- **Counterparty is the oracle — fully.** In v2 Counterparty validated the
  asset while the envelope was our own. In v3 the *content* is Counterparty
  consensus state: a counter's file is exactly what Counterparty stores as the
  asset's `description`. The indexer never re-interprets raw witness data to
  decide content (rule R3); it only checks *how* the description travelled
  (rule R4).
- **No new on-chain format.** The protocol adds nothing to the chain. It is a
  deterministic numbering lens over events Counterparty already parses.
- **Permissive content.** MIME type, content shape, and duplication never gate
  validity (R5, R6).
- **Data-defined genesis.** Counter `#0` is the first qualifying event at or
  after Counterparty's `taproot_support` activation (block **902,000**). No
  pre-v11 scheme is retro-numbered (Bitcoin Stamps has prior claim on
  output-side encodings; this protocol is witness-side only, so *classic*
  stamp numbering and counter numbering never overlap — though a `STAMP:`
  payload minted through the envelope is also picked up by stamps indexers
  as a cursed stamp, see §5.4).

---

## 2. Terminology

| Term | Meaning |
|------|---------|
| **Taproot envelope** | Counterparty v11's witness data encoding: an `OP_FALSE OP_IF … OP_ENDIF`/`OP_CHECKSIG` tapscript revealed by a commit/reveal pair. Two styles exist — the size-optimised **generic** envelope and the ordinals-compatible **"ord/xcp"** envelope (emitted with `inscription=true`). Both count equally. |
| **Commit tx** | Pays to the P2TR address committing to the envelope tapscript. |
| **Reveal tx** | Script-path-spends the commit output, exposing the envelope in **input 0's witness**, and carries an `OP_RETURN` holding only the literal marker `CNTRPRTY`. This is the transaction Counterparty parses and the one a counter is keyed to. |
| **File event** | A qualifying Counterparty message (issuance or fairminter deploy) whose description is non-empty and taproot-carried. One counter per file event. |
| **Kind** | `issuance` or `fairminter` — which message type produced the event. |
| **Content** | The description bytes as stored by Counterparty consensus (see [§5](#5-content)). |

---

## 3. Qualifying events (validity rules)

A Counterparty message records a counter iff **all** of R1–R4 hold. R5 and R6
are explicit non-rules.

- **R1 — Valid Counterparty state only.** The message must be accepted by
  Counterparty consensus: an issuance row with `status == "valid"`, or a
  fairminter deploy present in Counterparty's fairminters table (invalid
  deploys are never recorded there; the fairminter `status` field —
  open/closed — is lifecycle, not validity). The asset exists in Counterparty
  state by construction.
- **R2 — Qualifying message types.** Issuances (all variants: creation,
  reissuance, subasset) and **fairminter deploys**. **Fairmints (mints) never
  qualify** — they carry no content (`fair_minting == true` issuance rows are
  excluded); a fair-minted collection gets one counter at deploy. Broadcasts
  are excluded.
- **R3 — Non-empty description, deferred to the oracle.** The content is what
  Counterparty consensus stores as the description — the indexer never
  re-derives it from the witness. `description` must be non-null and non-empty.
  Null-description issuances (locks, ownership transfers, quantity-only
  reissues) produce no event. **No minimum size** — a 1-byte description
  qualifies.
- **R4 — Taproot envelope carrier only.** The message's transaction must be a
  Counterparty taproot **reveal** (see [§4](#4-carrier-detection)). Both
  envelope styles count equally. Descriptions carried by classic `OP_RETURN`
  (or any other encoding) do **not** count. Unrevealed commits are nothing.
- **R5 — Permissive content (non-rule).** MIME type is never a validity
  condition; there is no content-vs-MIME validation. (Counterparty itself
  validates `mime_type` against a fixed allow-list before a message can be
  `valid`, so malformed MIME cannot normally reach the indexer — but if one
  ever does, it is normalised to `application/octet-stream` for display with
  the raw string kept as metadata, and the event still counts.)
- **R6 — Duplicates allowed (non-rule).** Identical content may be inscribed
  any number of times; each qualifying event gets its own number.
  De-duplication is metadata-only, via `content_sha256`.

---

## 4. Carrier detection

R4 is decided by mirroring Counterparty's own reveal-transaction rule
(counterparty-rs `bitcoin_client.rs`), *not* by re-parsing envelope content:

A transaction is a **taproot reveal** iff:

1. it has an `OP_RETURN` output of the exact shape
   `OP_RETURN PUSH8 "CNTRPRTY"` — script hex `6a08434e545250525459`. The
   marker is **literal and unencrypted**; classic OP_RETURN-encoded
   Counterparty data is ARC4-encrypted with the first input's prevout txid, so
   it can never display the literal marker. The push carries the marker only —
   the message payload lives in the witness; and
2. its **input 0** witness has exactly **3 items** (signature, tapscript,
   control block — a taproot script-path spend), with the envelope in the
   tapscript (witness item 1).

The indexer checks (1) by parsing the output script (never by substring-search
over raw hex) and (2) as a shape check. Envelope *content* is not extracted —
per R3, content comes from Counterparty's parsed state.

Because Counterparty refuses taproot encoding for transactions with a
destination output (and for detach), sweeps and ownership-transfer issuances —
which copy an existing description into fresh issuance rows — can never pass
R4. Copied state is not new content.

---

## 5. Content

### 5.1 Bytes (canonical)

Counterparty's API returns `description` as a string whose encoding follows
Core's consensus helper `bytes_to_content` (`helpers.py`):

- **Textual** MIME types → the UTF-8 text itself.
- **Binary** MIME types → the **hex encoding** of the stored bytes.

A MIME type is *textual* iff (per Core's `classify_mime_type`, gated on the
event's block height):

- always: `text/*`, `message/*`, `*+xml`, or one of Core's fixed textual
  `application/*` list (`application/json`, `application/xml`,
  `application/javascript`, `application/yaml`, `application/sql`, …);
- additionally, from block **952,800** (`extended_mime_types_support`):
  `*+json`, with MIME parameters (e.g. `;codecs=opus`) stripped before
  classification.

The canonical **content bytes** of an event are therefore:

```
content_bytes = utf8(description)            if classify(mime_type, height) == text
                unhexlify(description)       otherwise
```

`content_sha256` and `content_length` are computed over `content_bytes` —
never over the API's string form. If `unhexlify` fails on a claimed-binary
description (defensive; should be unreachable for valid state), the UTF-8
bytes of the string are used and the event is flagged.

### 5.2 MIME metadata

`mime_type` defaults to `text/plain` when absent. The stored, display-facing
`content_type` strips MIME parameters; the verbatim original is kept as
`content_type_raw`. Per R5 none of this affects validity or numbering.

### 5.3 Pointer-like content (informational)

Textual content consisting of a single URI-like token (`ipfs:…`, `ar://…`,
`http(s)://…`) is flagged `is_pointer_like = true` as **display metadata
only**. It never affects validity or numbering (e.g. counter #77 SURREALPEPE,
an `ipfs:` pointer, is a full counter).

### 5.4 Stamp-like content (informational)

Textual content of the form `STAMP:<base64>` (case-insensitive prefix,
whitespace-tolerant base64) whose decoded bytes carry a known image magic
(GIF, PNG, JPEG, WebP) is a **Bitcoin Stamps payload** minted through the
taproot envelope. Stamps indexers pick these up too — as *cursed* (negative-
numbered) stamps, since the data is witness-side rather than in the classic
output-side encodings (bare multisig, OLGA P2WSH) — so such an event is both
a cursed stamp and a full counter (e.g. counter #84 STAMPINAL = stamp #-1841).

Like §5.3 this is display metadata only, derived at serve time (`stamp_mime`
on API records; the decoded image at `/stamp/<n>`). The canonical content
bytes, sha256, and rolling hash remain those of the *text* per §5.1; a
payload that fails to decode to a recognized image simply displays as text.

How content is *rendered* — including the deterministic repair of
transport-damaged stamp base64, magic-number sniffing when the declared MIME
type is generic or wrong, pointer handling, and caching of derived views —
is specified separately in [`rules.md`](rules.md) (display rules D1–D12).
Display rules never affect anything in this document.

---

## 6. Numbering

- **N1 — Order.** Events are ordered by
  `(block_index, tx_index, msg_index)` where `tx_index` is Counterparty's
  global transaction index of the **reveal** tx and `msg_index` is
  Counterparty's intra-transaction message index (today always `0` for
  qualifying messages — the key is future-proof for multi-message bundling;
  fairminter rows carry no `msg_index` and use `0`). One counter per
  Counterparty **message**, keyed `(tx_hash, msg_index)`.
- **N2 — Numbers start at 0**, gap-free; the next number is `MAX(number) + 1`.
- **N3 — Genesis.** The scan floor is block **902,000** (`taproot_support`
  activation; no qualifying event can exist earlier). Counter **#0 is XDUALS**
  at block 902,005. No retro-numbering of any pre-v11 scheme.
- **N4 — Reorgs.** The index is log-structured: on a block-hash mismatch at
  the stored tip, the indexer rolls back — deletes events with
  `block_index` above the fork point and re-walks the chain, reproducing
  identical numbering for identical chains. Waiting `confirmations` blocks
  behind the tip (recommended: 6) makes rollbacks rare; numbers above the
  finality horizon should be treated as provisional.
- **N5 — Append-only.** Later description changes, locks, ownership
  transfers, or destroys never renumber or invalidate an existing counter.
- **N6 — Per-event numbering.** Numbering is per qualifying *event*, not per
  asset: an unlocked asset accumulates a new counter for every qualifying
  reissuance (and fairminter deploy) it produces. The lowest-numbered counter
  on an asset is its *original*; the explorer lists all of an asset's counters
  together.

---

## 7. Consensus hash chain

To let independent indexers verify agreement cheaply, each counter carries a
rolling hash:

```
seed              = sha256( GENESIS_TAG )
rolling_hash(0)   = sha256( seed || canonical(0) )
rolling_hash(n)   = sha256( rolling_hash(n-1) || canonical(n) )

canonical(n) = utf8( "{number}|{tx_hash}|{msg_index}|{block_index}|{tx_index}|{kind}|{asset}|{content_sha256}|{content_length}" )
```

where `GENESIS_TAG = utf8("counters:v3:bitcoin-mainnet:902000")`, `||` is byte
concatenation of the previous digest (raw 32 bytes) with `canonical(n)`, and
`tx_hash`/`content_sha256` are lowercase hex. Two indexers agree on the whole
history iff their latest `rolling_hash` matches. A rollback (N4) truncates the
chain; re-indexing reproduces it.

---

## 8. Indexing algorithm

For each block from `max(sync_height + 1, 902000)` up to
`min(bitcoind_height, counterparty_height) − confirmations`, in order:

1. **Fetch messages from the oracle:** the block's issuances
   (`/v2/blocks/{h}/issuances`) and fairminter deploys
   (`/v2/blocks/{h}/fairminters`).
2. **Filter (R1–R3):** issuances must be `valid`, not `fair_minting`, with
   non-null non-empty `description`; fairminter deploys must have a non-null
   non-empty `description`. De-duplicate by `(tx_hash, msg_index)`
   (an issuance and fairminter row for the same message count once).
3. **Carrier check (R4):** fetch each candidate's raw transaction from
   bitcoind and apply [§4](#4-carrier-detection). Reject non-reveals.
4. **Record** survivors in `(block_index, tx_index, msg_index)` order:
   derive `content_bytes` ([§5](#5-content)), store the blob
   (content-addressed by SHA-256), assign the next number, extend the rolling
   hash, insert the row.
5. Advance the sync cursor (`last_height`, `last_block_hash`), remember the
   block hash for reorg detection, and commit.

The tip clamp (never past Counterparty's parsed height) is unchanged from v2:
walking blocks the oracle has not parsed would silently skip events.

---

## 9. Validity — checklist

A transaction message records a counter iff **all** hold:

1. `block_index ≥ 902,000`.
2. It is a Counterparty **issuance** with `status == "valid"` and
   `fair_minting == false`, **or** a **fairminter deploy**.
3. `description` is non-null and non-empty (as parsed by Counterparty).
4. The transaction is a **taproot reveal** ([§4](#4-carrier-detection)).
5. The message has not already produced a counter (dedup by
   `(tx_hash, msg_index)`).

MIME type, content shape, duplication, asset lock state, and later asset
lifecycle events are never conditions.

---

## 10. Enrichment (non-consensus)

Each stored counter also carries best-effort metadata that never gates
validity: `asset_id`, `asset_longname`, `source` (issuer), `divisible`,
`supply`, inscription cost (commit + reveal fee and serialized size),
`xcp_burned` (`fee_paid`), `content_type_raw`, and `is_pointer_like`. A
failure to fetch enrichment must not prevent a valid counter from being
recorded.

---

## 11. Minting

Minting requires no protocol-specific construction: compose a Counterparty
issuance (or fairminter) with `encoding=taproot` and a `mime_type`, passing
binary content as hex — Counterparty Core builds the commit/reveal pair and
signs the reveal's script-path input itself. The `counters wallet inscribe`
command wraps this: it composes via Core, has Bitcoin Core sign/fund the
commit, package-validates both transactions with `testmempoolaccept`, and
broadcasts. Constraints inherited from Counterparty: taproot encoding is
unavailable for transactions with a destination output (so
`transfer_destination` cannot be combined with an inscription mint) and for
detach.

---

## 12. Changes from v2

| | v2 | v3 |
|---|----|----|
| **Envelope** | Own `COUNT` envelope, parsed by this indexer. | Counterparty's taproot envelope; content deferred to Counterparty state. |
| **Content** | Envelope body bytes. | The asset's `description` as stored by Counterparty ([§5](#5-content)). |
| **Qualifying events** | Asset creations + owner-signed reinscriptions. | Valid issuances (all variants, fairmints excluded) + fairminter deploys ([§3](#3-qualifying-events-validity-rules)). |
| **Reinscriptions** | Message-less, authorised by owner signature. | Removed — every counter is a Counterparty message. A reissuance with a new taproot-carried description is the nearest equivalent (N6). |
| **Genesis** | Block 955,251 (`COUNTERZERO`). | Block 902,000 (`taproot_support` activation); #0 = XDUALS @ 902,005. |
| **Ordering** | (block, position-in-block). | (block, Counterparty `tx_index`, `msg_index`). |
| **Reorgs** | Out of scope. | Log-structured rollback (N4). |
| **Cross-indexer verification** | — | Rolling consensus hash chain ([§7](#7-consensus-hash-chain)). |
| **CLI** | `counters-proto` (originally `counters`) | `counters` (originally `counters2`) |

Counters minted under v2 (COUNT envelopes with `encoding=opreturn` issuances
and no description) do **not** qualify under v3 and are not renumbered — v3
starts a new namespace defined entirely by Counterparty state.

---

## 13. Constants

| Name | Value |
|------|-------|
| `CNTRPRTY_MARKER` | ASCII `CNTRPRTY` (`0x434e545250525459`) |
| Reveal OP_RETURN script | `6a08434e545250525459` |
| `GENESIS_HEIGHT` | `902000` (`taproot_support` activation; counter #0 = XDUALS @ 902005) |
| `EXTENDED_MIME_GATE` | `952800` (`extended_mime_types_support` activation) |
| Rolling-hash genesis tag | `counters:v3:bitcoin-mainnet:902000` |
| Recommended confirmations | `6` |

---

*Generated from the reference implementation in this repository. Where this
document and the code disagree, the code is authoritative — please file a fix.*
