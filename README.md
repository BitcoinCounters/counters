<p align="center">
  <img src="counters/server/static/counters-logo-512.png" alt="Bitcoin Counters" width="160">
</p>

# Bitcoin Counters — Indexer & Wallet

**Bitcoin Counters** are inscriptions — files stored in Bitcoin witness data (a
`COUNT` envelope) — bound to a Counterparty asset issued in the same transaction.

This tool **indexes** them (parse → join → number → store), **mints** and
**transfers** them using a taproot (BIP86) wallet kept inside **Bitcoin Core**
(Core holds the keys and signs; this is the same wallet `bitcoin-cli` manages),
and **serves** a web explorer plus a read-only JSON API.

## How it works

For each block (ascending):

1. **Parse** — in each transaction, scan the inputs' witness data for a valid
   `COUNT` envelope
   (`OP_FALSE OP_IF "COUNT" <0x01 content_type> [<0x02 asset>] <0x00> <body…> OP_ENDIF …`).
   The optional `0x02` tag names a target asset and marks a *reinscription*
   (see below).
2. **Join** — for each tx with **exactly one** valid envelope (across all its inputs),
   bind it to the Counterparty issuance in the **same transaction** (matched by
   `txid`). The block's issuances are fetched once and each candidate is looked
   up by its `txid`, so the asset is whatever that transaction itself created.
3. **Validate (via Counterparty Core, the oracle)** — the issuance must be
   `status == "valid"`, the asset's **first/creation** issuance
   (`asset_events` contains `creation`), and not `BTC`/`XCP`.
4. **Number & store** — assign the next gap-free number (from 0), write the
   file to a content-addressed blob store, and insert the record into SQLite.

We never reimplement Counterparty consensus — **Counterparty Core** decides
issuance validity, asset identity, and ownership. ("Bitcoin Core" is the
separate Bitcoin node; the two are always named in full to avoid confusion.)

### Reinscriptions

A counter can also be attached to an **existing** asset you already own — a
*reinscription*. Here the `COUNT` envelope carries an extra `0x02` tag naming
the target asset, and the transaction carries **no Counterparty message** at
all. The indexer authorises it by proving the transaction spent an input from
the asset's **owner (issuance-rights holder) as of that block** — reconstructed
from Counterparty's issuance history (creation → reissuances → ownership
transfers). It is *ownership*, not token balance, that grants the right, and
ownership is checked at the height of the inscription, so a later transfer can
neither retroactively authorise nor invalidate it.

Each reinscription is a new, permanently-numbered counter, so one asset can
back many counters. The lowest-numbered counter on an asset is its *original*;
any later ones are reinscriptions (the explorer lists them all on the asset).
Mint one with `inscribe --reinscribe --asset <ASSET>` (see Usage).

## Requirements

- Python 3.10+
- A synced **bitcoind** with `txindex=1` (RPC reachable; cookie auth supported)
- A synced **Counterparty Core** v2 API

```bash
pip install -e .          # installs deps + the `counters` console command
```

## Run with Docker

The repo ships a `Dockerfile` and a `docker-compose.yml` with two services:

- **`counters`** — the web explorer + read-only JSON API on port `8081`.
- **`indexer`** — the indexing engine (runs `index --from-genesis`); needs a
  reachable **bitcoind** and **Counterparty Core**.

```bash
cp .env.example .env             # set your bitcoind / Counterparty Core endpoints
docker compose up -d --build     # build + start both services
docker compose up -d counters    # ...or just the explorer (no backends required)
docker compose logs -f counters  # follow logs
docker compose down              # stop
```

The explorer is then at `http://127.0.0.1:8081`. The index (SQLite + blobs)
persists in the `counters-data` volume, mounted at `/data` inside the
containers. On Linux, `host.docker.internal` resolves to the Docker host (wired
up via `extra_hosts`), so the defaults in `.env.example` point at bitcoind /
Core running on the host.

> Compose forwards the connection and indexer-behaviour variables from `.env`
> (`BTC_RPC_*`, `CP_API_URL`, `COUNTER_POLL_INTERVAL`, `COUNTER_CONFIRMATIONS`).
> The `indexer` service sets its start floor with `--from-genesis` rather than
> `COUNTER_START_HEIGHT`.

## Configuration (environment variables)

| Variable | Default | Meaning |
| --- | --- | --- |
| `BTC_RPC_URL` | `http://127.0.0.1:8332` | bitcoind JSON-RPC URL |
| `BTC_COOKIE_FILE` | `~/.bitcoin/.cookie` | bitcoind cookie (preferred auth) |
| `BTC_RPC_USER` / `BTC_RPC_PASSWORD` | — | fallback if no cookie |
| `CP_API_URL` | `http://127.0.0.1:4000` | Counterparty Core v2 API |
| `COUNTER_DATA_DIR` | `data/` | SQLite + blobs location |
| `COUNTER_START_HEIGHT` | `0` | first block a fresh scan starts at |
| `COUNTER_CONFIRMATIONS` | `0` | blocks behind tip to stay |
| `COUNTER_POLL_INTERVAL` | `15` | seconds between tip polls in `run` |

> A fresh scan starts at **block 0**. Raise the floor with `--from-taproot`
> (block 709632 — no taproot reveal can exist earlier) or `--from-genesis`
> (block 955251 — counter #0; nothing valid precedes it), or set
> `COUNTER_START_HEIGHT`. Stored progress always wins, so this only applies to a
> fresh DB — to rescan, `rm -rf data` first.

## Usage

Invoke as `counters <command>` after `pip install -e .`, or equivalently
`python -m counters <command>`.

```bash
# --- indexing ---
counters index -v                                  # scan from block 0, then follow the tip
counters index --from-taproot                      # skip pre-taproot blocks (fresh DB only)
counters index --from-genesis                      # start at counter #0's block (fresh DB only)
counters sync --stop-at 720000                     # one-shot catch-up (bounded for tests)

# --- reads (need only a synced index) ---
counters status                                    # bitcoind / Counterparty / index heights
counters list                                      # 20 most recent
counters list --recent 50
counters list --owner bc1p...                      # by mint-time owner
counters list --block 800000-800100                # by block range
counters info 0                                    # metadata by number
counters info RAREPEPE                             # ...or by asset name / longname
counters info 0 --json                             # metadata as JSON
counters info 0 --raw > cat.png                     # stream the file bytes
counters info 0 --save cat.png                      # write the file to disk
counters validate <txid>                           # is this tx a counter, and why / why not

# --- web explorer + read-only JSON API ---
counters server                                    # http://127.0.0.1:8081
counters server --host 0.0.0.0 --port 8081         # bind publicly / pick a port

# --- wallet (taproot BIP86, bc1p; keys held by Bitcoin Core) ---
counters wallet --name mywallet create             # new wallet; prints a 12-word seed ONCE
counters wallet --name mywallet restore            # re-import from a BIP39 seed (read on stdin) + rescan

# recover an OLD Counterparty wallet (Counterwallet / Freewallet — pre-BIP39 Electrum v1, legacy 1... addresses)
counters wallet --name old restore --counterwallet --dry-run   # preview the derived 1... addresses; imports nothing
counters wallet --name old restore --counterwallet             # import the legacy keys into Core + rescan
counters wallet --name mywallet receive            # next taproot (bc1p) address
counters wallet --name mywallet balance            # BTC + aggregated Counterparty balances
counters wallet --name mywallet inscriptions       # counters held by the wallet
counters wallet --name mywallet send RAREPEPE 1 bc1p...        # transfer a counter
counters wallet --name mywallet send RAREPEPE 1 bc1p... --dry-run  # compose+sign, no broadcast

# mint a counter from a file (commit + reveal). --dry-run builds, signs, and
# package-validates both txs WITHOUT broadcasting (prints raw hex + cost).
counters wallet --name mywallet inscribe --file cat.png --dry-run
counters wallet --name mywallet inscribe --file cat.png                    # free numeric asset
counters wallet --name mywallet inscribe --file cat.png --asset ZOMBIEPEPES # named (0.5 XCP)
counters wallet --name mywallet inscribe --file v2.png --asset RAREPEPE --reinscribe  # attach to an asset you own (no new asset, no XCP)
counters wallet --name mywallet inscribe --file cat.png --fee-rate 8 --commit-fee-rate 4
```

> The 12-word seed is the only backup and is shown once at create time. The
> keys are imported into a Bitcoin Core descriptor wallet, which holds them and
> does all signing; this tool never touches private keys after derivation.
> `--name` defaults to `counter`.

## Tests

```bash
python -m pytest            # if pytest installed
python tests/test_envelope.py   # zero-dependency runner
```

## Layout

```
counters/
  config.py         protocol constants + env-driven Config
  bitcoind.py       JSON-RPC client (cookie auth, getblock witnesses)
  envelope.py       script tokenizer + COUNT envelope parser
  counterparty.py   Core v2 client (the oracle)
  store.py          SQLite schema + content-hash blob store + queries
  builder.py        COUNT leaf script + P2TR commit-address derivation
  tap.py            BIP340 Schnorr + BIP341/342 taproot + tx serializer
  bip32.py          BIP32/BIP86 derivation (pure-Python RIPEMD160 + ecdsa)
  electrum1.py      Electrum-v1 recovery for old Counterwallet/Freewallet seeds
  electrum1_words.txt  the 1626-word Electrum-v1 list (verbatim from Electrum, MIT)
  progress.py       ord-style progress bar
  __main__.py       CLI command tree (parser + dispatch)
  indexer/          the indexing engine
    indexer.py      pipeline + run loops
  commands/         CLI command handlers
    read.py         status / info / list / validate
    wallet.py       create / restore / receive / balance / inscriptions
    inscribe.py     mint flow: create-issuance or reinscribe; build/sign commit & reveal
    send.py         transfer a counter (compose send + sign + broadcast)
    serve.py        server command entry point
  server/           web explorer + read-only JSON API
    app.py          stdlib HTTP server (static SPA + /counters /counter /content)
    static/         index.html + logos/favicon (served assets)
pyproject.toml      installs the `counters` console command
Dockerfile          container image (entrypoint: the `counters` CLI)
docker-compose.yml  explorer + indexer services, data volume, host networking
.env.example        sample environment (copy to .env)
docs/               protocol + CLI reference PDFs
tests/
  test_envelope.py  parser unit tests
```
