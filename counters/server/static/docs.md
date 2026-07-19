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

1. it is **valid Counterparty state** — an issuance with `status = valid` (any variant), or a fairminter deploy. Fairmints never qualify (the collection gets one counter at deploy); broadcasts are excluded;
2. its **description is non-empty** — the content is exactly what Counterparty consensus stores as the asset's description (1 byte is enough);
3. the description is **carried in a taproot envelope** — the transaction is a reveal showing the literal, unencrypted `CNTRPRTY` marker in its `OP_RETURN`. Classic `OP_RETURN`-carried descriptions never count.

Non-rules: MIME type never gates validity, duplicate content is allowed, and there is no minimum size. Sweeps and ownership transfers copy descriptions rather than create them — Counterparty refuses taproot encoding for them, so they can never count. Validity is Counterparty's verdict, not an explorer's listing.

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



