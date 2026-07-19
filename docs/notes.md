

## Technical Notes

A Counter can be inscribed via 2 counterparty messages. Issue, and Fairminter Deploy. 

Files written in the description of a **Counterparty asset** via a counterparty message written into Op return are not valid

▎ An asset's description can change — the owner can reinscribe with new content. That never invalidates anything, because a counter is not the asset's current description; it is the event of inscribing one. Each counter is pinned forever to its own transaction: the file sits in that transaction's witness data, and the number records its place in history. Updating a description writes a new page — a new counter — it never erases an old one.

And if you want the one-line contrast that makes it click:

▎ Counterparty state answers "what is the description now?" Counters answer "what was ever inscribed, and in what order?"

If you want something warmer, the profile-picture analogy lands well with non-technical readers: changing your profile picture doesn't delete the old photos — the asset's description is the current picture; counters are the album.

Two placement thoughts: this belongs right after your two-conditions sentence (it preempts the obvious "but descriptions can change?!" objection), and it dovetails with the existing "New content on an existing asset" section — which already says numbering is per-event and that the asset page lists all its counters with the lowest number as the original. You might end the new paragraph with "(see New content on an existing asset)" so the two link up.


- numbered inscription
- stored inside bitcoin blockchain
- carried in counterparty asset description
- inscribed on top of counterparty asset. 
- forever associated with counterparty asset
- in witness data
- counterparty protocol is used for asset identity, ownership, naming, transfer
- assigned a number in order of inscription


  as a **Counterparty asset description** carried in Counterparty's taproot envelope, numbered in order of creation.

- a file commited on chain, 


Counterparty carries identity, ownership, naming, transfer — and the content itself. A counter's file is exactly what Counterparty consensus stores as the asset's description. The protocol adds nothing on chain; it defines only which Counterparty events qualify and how they are numbered.

Any Counterparty asset can carry counters — named, numeric, divisible, new, or existing — and an unlocked asset can hold many (see Reinscription below).

## The taproot envelope {#envelope}

The carrier is Counterparty v11's taproot envelope. Every mint is a commit/reveal pair; the counter is keyed to the **reveal**, which has exactly this shape:

```
reveal transaction
├─ input 0 witness: 3 items         # taproot script-path spend
│    [ signature,
│      tapscript,                   # OP_FALSE OP_IF … OP_ENDIF envelope
│      control block ]              #   carrying the description bytes
└─ output: OP_RETURN "CNTRPRTY"     # literal, unencrypted marker
```

The file travels as data pushes inside the tapscript's never-executed `OP_FALSE OP_IF … OP_ENDIF` block. Two envelope styles exist — Counterparty's size-optimised generic envelope and the ordinals-compatible `inscription=true` style — and both count equally.

The indexer never parses envelope contents. It checks only the carrier — the literal `CNTRPRTY` `OP_RETURN` plus the three-item input-0 witness — and reads the content from Counterparty's parsed state. Counterparty is the oracle; the indexer is a numbering lens over it.

## Why counters only became possible when Counterparty enabled taproot {#why-taproot}

*Short version: counters are the description of a Counterparty asset that reached
the chain inside witness data. Until Counterparty could write descriptions into
witness data, that intersection was empty — so counters could not exist before it.*

**The old ceiling.** Bitcoin activated Taproot in November 2021 (block 709,632),
which gave the chain cheap, effectively unbounded *witness*-side data: tapscript
carried in a commit/reveal spend, discounted 4× relative to output bytes. Ordinals
exploited this immediately. **Counterparty did not.** Its messages stayed
*output-side* and small — `OP_RETURN` (~80 bytes), bare multisig, and P2SH data
encoding. An asset's `description` was a short string by construction; there was
nowhere to put a *file*. A counter — a numbered on-chain file — was simply not
expressible.

**What v11 changed.** Counterparty Core **v11.0.0** added a **taproot envelope**
encoding (`encoding=taproot`), activating at **block 902,000** (`taproot_support`,
~20 June 2025; all nodes had to upgrade by then). The same release removed P2SH
encoding ("strictly worse than taproot") and added ordinals-compatible inscription
creation (`inscription=true`, `mime_type`). Under the new encoding a message —
including the asset `description` — travels as data pushes inside an
`OP_FALSE OP_IF … OP_ENDIF` tapscript in the **reveal transaction's input-0
witness**; on the output side there is only the literal `CNTRPRTY` `OP_RETURN`
marker. This is structurally the ordinals inscription envelope, brought inside
Counterparty consensus (chunks ≤520 B, up to ~400 KB per envelope, at witness
discount).

**Why that is exactly what a counter needs.** A counter must pass two tests at
once: (1) be the description of a Counterparty asset, and (2) have those
description bytes reach the chain *in witness data*. Under taproot encoding the
description-write and the witness-write are **the same bytes in the same
transaction** — one payload satisfies both tests. Under the old output-side
encodings a description could exist, but never in witness, so test 2 always
failed. The taproot envelope is the *only* carrier where the intersection is
non-empty.

**Consequences that fall out of this.**

- **Data-defined genesis.** No qualifying event can predate the encoding, so the
  scan floor is block 902,000 and counter **#0 (XDUALS)** is the first qualifying
  reveal, five blocks later at 902,005. There is nothing to retro-number.
- **Witness-side only, by definition.** Descriptions delivered by classic
  `OP_RETURN` (or multisig/P2SH) are valid Counterparty state but are **not**
  counters — which is also why counter numbering never collides with output-side
  schemes like classic Bitcoin Stamps.
- **Convergent identity.** Because the carrier *is* the ordinals envelope, an
  ord-style counter is simultaneously an ordinals inscription, and a `STAMP:`
  payload minted this way is also a (cursed) Bitcoin Stamp. Same witness bytes,
  three lenses.
- **No new on-chain format for us.** Counterparty already supplied identity,
  ownership, naming, and transfer; taproot added the *content* channel. The
  counters protocol invents nothing on chain — it is a deterministic numbering
  lens over "asset description, written into witness data via the taproot
  envelope."

Sources: Counterparty Core v11.0.0 release notes and the
[Taproot Envelope spec](https://docs.counterparty.io/docs/advanced/specifications/taproot-envelope/);
activation height and genesis mapping per this repo's
[`build-reference-v3.md`](build-reference-v3.md) (§1, §4, N3, §13).

