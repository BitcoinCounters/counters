

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

