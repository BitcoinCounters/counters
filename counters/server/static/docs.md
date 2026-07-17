<!-- Explorer docs. Edit freely; reload the page to see changes.
     Conventions: "## Title {#anchor}" makes a section (the TOC is generated
     automatically); "> **Bold lead.** text" makes a callout; fenced code
     blocks highlight trailing # comments; numbered/bulleted lists get the
     explorer's copper markers. -->

## What a counter is {#what}

A **Bitcoin counter** - A.K.A. Counter Inscription - is a file permanently inscribed on the Bitcoin Blockchain, linked to a counterparty asset, and assigned a number in order of inscription. 

To be a valid counter, a file must be written inside the description of a **Counterparty asset** via a Counterparty message written into the **witness data** of a Bitcoin transaction. 

The Counterparty protocol takes care of asset naming, ownership, and transfer. The counters protocol defines what is a valid counter, and the counters indexer scans the blockchain and finds all valid counters, and assigns inscription numbers.  

All Counterparty asset types are valid, including named, unnamed, subassets, divisible, and so on. Unlimited counters can be reissued on the same asset. 


## Why Witness Data

Counters are intended to make maximum use of the Segwit discount. Witness data is the cheapest place to inscribe files (4x cheaper than usual). 

## Ordinals & Stamps comparison

Bitcoin Stamps use Counterparty Assets, but the data lives in transaction outputs. Ordinal inscriptions live in witness data, but they are bound to individual sats instead of counterparty assets. The Counters protocol is designed to make full use of counterparty functionality while also using the cheapest way to inscribe onto the Bitcoin Blockchain. 

| Protocol | File stored in | Identity / asset layer |
|---|---|---|
| **Counters** | witness data (cheap) | Counterparty asset |
| Ordinals | witness data (cheap) | individual sats (rare sats) |
| Bitcoin Stamps | transaction outputs (full price) | Counterparty asset |
| Cursed Stamps | witness data (cheap) | Counterparty asset |

*Cursed stamps* are `STAMP:` image payloads inscribed in witness data rather than the classic output encodings — mechanically the same as a counter, which is why a `STAMP:` counter is also a cursed stamp.

## Technical Notes

A Counter can be inscribed via 2 counterparty messages. Issue, and Fairminter Deploy. 

Files written in the description of a **Counterparty asset** via a counterparty message written into Op return are not valid

▎ An asset's description can change — the owner can reissue with new content. That never invalidates anything, because a counter is not the asset's current description; it is the event of inscribing one. Each counter is pinned forever to its own transaction: the file sits in that transaction's witness data, and the number records its place in history. Updating a description writes a new page — a new counter — it never erases an old one.

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

Any Counterparty asset can carry counters — named, numeric, divisible, new, or existing — and an unlocked asset can hold many (see Reissuance below).

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

## Ownership {#own}

Whoever holds the Counterparty asset balance owns the counter. Transfer is an ordinary Counterparty send of that asset; the witness file never moves — it stays as permanent provenance pinned to the mint transaction.

## Numbering {#number}

Counters are numbered globally, gap-free, starting at **0**, ordered by `(block, tx_index, msg_index)` — block height, then Counterparty's global transaction index, then its intra-transaction message index. Only *valid* counters get a number, and numbers are permanent: later locks, transfers, or destroys never renumber anything. Numbers in the newest few blocks are provisional until buried by confirmations.

> **Genesis is the taproot activation.** The scan floor is block 902,000 — Counterparty v11's `taproot_support` activation. Counter #0 is XDUALS at block 902,005, the first qualifying event.

## Setup {#setup}

The indexer is the only part that needs backends; the explorer alone runs without them. To index, run two fully-synced nodes and point Counters at them.

**1 · Bitcoin Core.** A synced `bitcoind` with a transaction index and RPC enabled:

```
# bitcoin.conf
txindex=1     # required: look up any tx by id
server=1      # enable JSON-RPC
# auth via the cookie file (default) or rpcuser/rpcpassword
```

**2 · Counterparty Core.** The oracle that decides issuance validity, asset identity, and ownership. Run and sync it (v2 API, default port `4000`) per its official docs: [github.com/CounterpartyXCP/counterparty-core ↗](https://github.com/CounterpartyXCP/counterparty-core). Counters only reads from it — it never reimplements consensus.

**3 · Wire them up.** Tell Counters where the backends are (env vars, or copy `.env.example` to `.env`), then verify both are detected:

```
# point at your nodes (defaults shown)
BTC_RPC_URL=http://127.0.0.1:8332
CP_API_URL=http://127.0.0.1:4000

# confirm bitcoind + Counterparty + index heights line up
counters status

# then index, and serve the explorer
counters index
counters server
```

> **Both nodes must be fully synced.** The indexer never advances past Counterparty's height, so a lagging Core simply slows indexing rather than producing wrong results.

## Minting {#mint}

A mint is a Counterparty issuance composed with `encoding=taproot`: Counterparty Core builds the commit/reveal pair, the reveal carrying the file (the asset's description) in its witness envelope and an `OP_RETURN` holding only the literal `CNTRPRTY` marker. The wallet just signs the commit and broadcasts both.

```
# create a wallet (prints a seed phrase once)
counters wallet create --name me

# inscribe a file as a free numeric asset…
counters wallet --name me inscribe --file cat.png

# …or as a named asset (costs 0.5 XCP)
counters wallet --name me inscribe --file cat.png --asset MYCOUNTER
```

## Validity {#valid}

A Counterparty message records a counter when all hold:

1. it is **valid Counterparty state** — an issuance with `status = valid` (any variant), or a fairminter deploy. Fairmints never qualify (the collection gets one counter at deploy); broadcasts are excluded;
2. its **description is non-empty** — the content is exactly what Counterparty consensus stores as the asset's description (1 byte is enough);
3. the description is **carried in a taproot envelope** — the transaction is a reveal showing the literal, unencrypted `CNTRPRTY` marker in its `OP_RETURN`. Classic `OP_RETURN`-carried descriptions never count.

Non-rules: MIME type never gates validity, duplicate content is allowed, and there is no minimum size. Sweeps and ownership transfers copy descriptions rather than create them — Counterparty refuses taproot encoding for them, so they can never count. Validity is Counterparty's verdict, not an explorer's listing.

## New content on an existing asset {#reinscribe}

Numbering is **per event**, not per asset: an unlocked asset accumulates a new counter for every qualifying issuance it produces. To attach new content to an asset you own, issue a **reissuance with a fresh taproot-carried description** — it earns its own permanent number, and the asset's page lists all of its counters (the lowest-numbered is the *original*). Locks, ownership transfers, and destroys never renumber anything.

```
# attach new content to an asset you own (a reissuance; supply unchanged)
counters wallet --name me inscribe --file v2.png --asset MYCOUNTER
```

## Server API {#api}

This explorer reads these endpoints from `counters server`:

- `GET /status` — latest synced block + total counter count
- `GET /counters?before=N&limit=K` — recent counters
- `GET /counter/<number|asset>` — one counter's record
- `GET /block/<height>` — counters minted in a block
- `GET /content/<number>` — the raw file, served with its stored MIME
- `GET /preview/<number>` — the sandboxed render used by this explorer's cards
- `GET /stamp/<number>` — the decoded image of a `STAMP:` counter

Served over HTTP by `counters server`, this explorer talks to its own origin; opened straight from disk it falls back to a bundled sample.

## Source {#source}

Counters is open source — the indexer, CLI, and this explorer all live in one repository: [github.com/BitcoinCounters/counters ↗](https://github.com/BitcoinCounters/counters). The original protocol, Counters Proto, lives on at [proto.bitcoincounters.com ↗](https://proto.bitcoincounters.com).

## Community {#community}

Join the conversation on Telegram: [t.me/BitcoinCounters ↗](https://t.me/BitcoinCounters).
