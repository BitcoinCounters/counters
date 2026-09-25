<!-- Explorer docs. Edit freely; reload the page to see changes.
     Conventions: "## Title {#anchor}" makes a section (the TOC is generated
     automatically); "> **Bold lead.** text" makes a callout; fenced code
     blocks highlight trailing # comments; numbered/bulleted lists get the
     explorer's copper markers. -->

## What are Counters {#what}

**Bitcoin Counters** - A.K.A. Counter Inscriptions - are numbered on chain NFTs using counterparty assets. 

Every Counter is a file inscription permanently stored on the Bitcoin Blockchain, linked to a counterparty asset, and assigned a number in order of inscription. 

## The Counters Protocol
The counters protocol defines what is a valid counter, and the counters indexer scans the blockchain and finds all valid counters, and assigns inscription numbers.  

To be a valid counter, a file must be written inside the description of a **Counterparty asset** via a Counterparty message written into the **witness data** of a Bitcoin transaction. Unlimited counters can be reinscribed on the same asset.

## Numbering {#number}
Counters are numbered one by one, starting at **0**, in order of creation, by block height, then block position — same as Ordinals. Only *valid* counters get a number.

Valid counters only became possible after Counterparty activated taproot support at block 902,000 — therefore the counters indexer starts at block 902,000

## Ownership
Ownership, asset names and transfers are handled by the Counterparty protocol. Every Counterparty asset type is valid, including named, unnamed, subassets, divisible, locked and so on.  

The holders of the Counterparty asset balance own the counter. Asset transfer happens via ordinary Counterparty send of that asset balance; the counter file never moves — it stays permanently pinned to the asset.


## Why Witness Data

Counters are intended to make maximum use of the Segwit discount. Witness data is the cheapest place to inscribe on Bitcoin,(4x cheaper with the SegWit discount). 

Witness data holds can hold 400kb with a standard transaction and 4MB with non standard transactions, where `OP_RETURN` caps at ~80 bytes for standard transactions and 1MB for non standard. 

Witness data also never enters the UTXO set, so counters don't bloat the UTXO set. 

## Ordinals & Stamps comparison

Bitcoin Stamps use Counterparty Assets, but the data lives in transaction outputs. Ordinal inscriptions live in witness data, but they are bound to individual sats instead of counterparty assets. The Counters protocol is designed to make full use of counterparty functionality while also using the cheapest way to inscribe onto the Bitcoin Blockchain. 

| Protocol | File stored in | Identity / asset layer |
|---|---|---|
| **Counters** | witness data (cheap) | Counterparty asset |
| Ordinals | witness data (cheap) | individual sats (rare sats) |
| Bitcoin Stamps | transaction outputs (full price) | Counterparty asset |
| Cursed Stamps | witness data (cheap) | Counterparty asset |

*Cursed stamps* are `STAMP:` image payloads inscribed in witness data rather than the classic output encodings — mechanically the same as a counter, which is why a `STAMP:` counter is also a cursed stamp.

## Setup {#setup}

Only the indexer needs backends; the explorer runs on its own. To index, point Counters at two fully-synced nodes — a `bitcoind` with `txindex=1`, and Counterparty Core (the oracle for asset validity, identity, and ownership).

```
# .env (defaults shown)
BTC_RPC_URL=http://127.0.0.1:8332   # bitcoind: txindex=1, server=1
CP_API_URL=http://127.0.0.1:4000    # Counterparty Core, v2 API
```

Then confirm the heights line up and run:

```
counters status   # check bitcoind + Counterparty + index heights
counters index    # follow the tip
counters server   # serve this explorer
```

> **Both nodes must be fully synced.** The indexer never advances past Counterparty's height — a lagging Core only slows indexing, never produces wrong results.

## Inscription {#inscribe}

A counter is inscribed with one command: Counterparty Core composes the taproot commit/reveal pair carrying your file as the asset's description, and the wallet signs and broadcasts both. Inscribe onto a free numeric asset, or a named asset for 0.5 XCP.

```
# create a wallet (prints a seed phrase once)
counters wallet create --name me

# inscribe a file as a free numeric asset…
counters wallet --name me inscribe --file cat.png

# …or as a named asset (costs 0.5 XCP)
counters wallet --name me inscribe --file cat.png --asset MYCOUNTER
```

## Validity Rules {#valid}

A Counterparty message records a counter when all hold:

1. it is **valid Counterparty state, authored by the transaction's own message** — an issuance with `status = valid` (any variant), or a fairminter deploy. Fairmints never qualify (the collection gets one counter at deploy); broadcasts are excluded; and rows Counterparty *derives* during block processing — an LP token issued at a fairminter close or pool deposit, lifecycle rows copying the deploy's description — never qualify: every `asset_events` tag on the row must be message-authored (`creation`, `reissuance`, `transfer`, `change_description`, `lock_quantity`, `lock_description`, `reset`), with unknown tags failing closed;
2. its **description is non-empty** — the content is exactly what Counterparty consensus stores as the asset's description (1 byte is enough);
3. the description is **carried in a taproot envelope** — the transaction is a reveal showing the literal, unencrypted `CNTRPRTY` marker in its `OP_RETURN`. Classic `OP_RETURN`-carried descriptions never count.

Non-rules: MIME type never gates validity, duplicate content is allowed, and there is no minimum size. Sweeps and ownership transfers copy descriptions rather than create them — Counterparty refuses taproot encoding for them, so they can never count. Validity is Counterparty's verdict, not an explorer's listing.

## Delegation {#delegate}

A counter whose body **names another counter's event** renders that counter's content in its place — one file inscribed once, referenced by any number of cheap (~30–90 byte) counters. The reference is an **inscription id**, never a counter number (numbers are this explorer's handle, not on-chain data). Three body shapes count, all strict: the bare id (`<txid>i<msg_index>`), the tagged form `DELEGATE:<txid>i<msg_index>`, or a JSON object whose `delegate` member holds the id — every other JSON member is the edition's own metadata (traits, a name), carried untouched. Resolution is display-only and happens inside this index: the target either is an indexed counter (rendered, badged, linked) or the body shows as text. One hop only, and `/content/<n>` always returns the delegate's own bytes; the **raw** toggle on the counter page shows them in place of the rendered view.

The reference may carry a **display fragment** — `<id>#edition-69` — appended to the rendered document's URL, so an SVG that styles itself by `:target` (one file, many editions) shows the named variant. In the JSON form, a reference without a fragment inherits the fragment of an `image` member (the ordinals-marketplace convention), so a dual counter+ordinal edition JSON renders its edition here with no extra field.

```
# delegate to a counter you saw in the explorer (number resolves locally;
# only the inscription id goes on chain)
counters wallet --name me inscribe --delegate 121
counters wallet --name me inscribe --delegate <txid>i0 --asset MYEDITION
# edition 69 of a :target-styled SVG counter
counters wallet --name me inscribe --delegate '218#edition-69' --asset RARE.PEPE.69
```

## Reinscription {#reinscribe}

There is one counter per inscription event, not per asset. To attach new content to an asset you own, reinscribe it with a fresh taproot-carried description — the reinscription earns its own permanent number. One asset can hold many counters; the lowest-numbered is the *original*, and the asset's page lists them all. Locks, transfers, and destroys never renumber anything.

```
# attach new content to an asset you own (supply unchanged)
counters wallet --name me inscribe --file v2.png --asset MYCOUNTER
```

## Server API {#api}

This explorer reads these endpoints from `counters server`:

- `GET /status` — latest synced block + total counter count
- `GET /counters?before=N&limit=K` — recent counters
- `GET /counter/<number|asset|inscription id>` — one counter's record
- `GET /block/<height>` — counters minted in a block
- `GET /content/<number|inscription id>` — the raw file, served with its stored MIME. Honours `Range`, so a reader can pull one piece of a large inscription (a PDF page, a seek in a long audio file) instead of the whole thing
- `GET /preview/<number|inscription id>` — the sandboxed render used by this explorer's cards. PDFs get a page-by-page viewer that scrolls the whole document and paints pages as they come into view
- `GET /stamp/<number|inscription id>` — the decoded image of a `STAMP:` counter
- `GET /delegate/<number|inscription id>` — the bytes a delegate's target committed (one hop, from this index only); 404 for a non-delegate or an unresolved target
- `GET /preview/<number|inscription id>?raw=1` — the canonical on-chain bytes as inert text (the raw toggle) instead of any derived view

Every counter record carries its **inscription id**: `<reveal txid>i<msg_index>` — the event's on-chain identity, in ordinals' syntax. The index is Counterparty's per-transaction event index (0 for every counter to date); for a counterparty + ord counter the string is byte-identical to the ordinals inscription id of the same reveal, so ord tooling can dereference it too. Numbers are this lens's handle; the inscription id means the same thing to any reader.

Served over HTTP by `counters server`, this explorer talks to its own origin; opened straight from disk it falls back to a bundled sample.

## Source {#source}

Counters is open source — the indexer, CLI, and this explorer all live in one repository: [github.com/BitcoinCounters/counters ↗](https://github.com/BitcoinCounters/counters). The original protocol, Counters Proto, lives on at [proto.bitcoincounters.com ↗](https://proto.bitcoincounters.com).

## Community {#community}

Join the conversation on Telegram: [t.me/BitcoinCounters ↗](https://t.me/BitcoinCounters).



