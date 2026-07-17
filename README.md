<p align="center">
  <img src="counters/server/static/counters-logo-512.png" alt="Bitcoin Counters" width="160">
</p>

# Bitcoin Counters v3 — Indexer & Wallet (`counters`)

**Bitcoin Counters** are numbered file events: files committed permanently to
Bitcoin as **Counterparty asset descriptions carried in v11 taproot
envelopes**, numbered deterministically from #0 (XDUALS, block 902,005 — five
blocks after Counterparty's taproot activation). Counterparty carries
identity, ownership, naming, transfer, *and the content itself*; the counters
protocol is a numbering lens over events Counterparty already parses. The
full protocol is specified in [`docs/build-reference-v3.md`](docs/build-reference-v3.md).

This tool **indexes** counters (fetch → filter → carrier-check → number →
store), **mints** and **transfers** them using a taproot (BIP86) wallet kept
inside **Bitcoin Core** (Core holds the keys and signs; this is the same
wallet `bitcoin-cli` manages), and **serves** a web explorer plus a read-only
JSON API.

## How it works

For each block (ascending, from genesis 902,000):

1. **Fetch from the oracle** — the block's issuances and fairminter deploys
   from Counterparty Core (`/v2/blocks/{h}/issuances`, `.../fairminters`).
2. **Filter (R1–R3)** — keep valid issuances (fairmints excluded — a
   fair-minted collection gets one counter at deploy) and fairminter deploys,
   with a **non-null, non-empty description**. The content is exactly what
   Counterparty consensus stores as the description; the indexer never
   re-interprets witness data.
3. **Carrier check (R4)** — the transaction must be a Counterparty taproot
   **reveal**: an `OP_RETURN` holding only the literal, unencrypted
   `CNTRPRTY` marker plus a 3-item script-path witness on input 0. Classic
   `OP_RETURN`-carried descriptions never count.
4. **Number & store** — order by `(block, tx_index, msg_index)`, assign the
   next gap-free number (from 0), write the decoded content to a
   content-addressed blob store, extend the rolling consensus hash, insert
   the record into SQLite.

We never reimplement Counterparty consensus — **Counterparty Core** decides
message validity, asset identity, ownership, and content. ("Bitcoin Core" is
the separate Bitcoin node; the two are always named in full to avoid
confusion.)

Numbering is **per event**: an unlocked asset accumulates a new counter for
every qualifying issuance (e.g. a reissuance with fresh taproot-carried
content). Reorgs roll back log-structured (the fork point is found from
stored block hashes; numbering re-derives identically), and the index never
advances past Counterparty's parsed height.

## Requirements

- Python 3.10+
- A synced **bitcoind** with `txindex=1` (RPC reachable; cookie auth supported)
- A synced **Counterparty Core** v11+ API

```bash
pip install -e .          # installs deps + the `counters` console command
```

## Run with Docker

The repo ships a `Dockerfile` and a `docker-compose.yml` with two services:

- **`counters`** — the web explorer + read-only JSON API on port `8081`.
- **`indexer`** — the indexing engine (runs `index`); needs a reachable
  **bitcoind** and **Counterparty Core**.

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

## Configuration (environment variables)

| Variable | Default | Meaning |
| --- | --- | --- |
| `BTC_RPC_URL` | `http://127.0.0.1:8332` | bitcoind JSON-RPC URL |
| `BTC_COOKIE_FILE` | `~/.bitcoin/.cookie` | bitcoind cookie (preferred auth) |
| `BTC_RPC_USER` / `BTC_RPC_PASSWORD` | — | fallback if no cookie |
| `CP_API_URL` | `http://127.0.0.1:4000` | Counterparty Core v2 API |
| `COUNTER_DATA_DIR` | `data/` | SQLite + blobs location |
| `COUNTER_START_HEIGHT` | `902000` | first block a fresh scan starts at (never below genesis) |
| `COUNTER_CONFIRMATIONS` | `0` | blocks behind tip to stay (6 recommended for near-final numbering) |
| `COUNTER_POLL_INTERVAL` | `15` | seconds between tip polls in `index` |

> A fresh scan starts at the protocol genesis (block **902,000**, Counterparty
> v11's `taproot_support` activation) — by rule N3 nothing can qualify
> earlier, so there is no exhaustive-from-0 mode. Stored progress always wins;
> to rescan, `rm -rf data` first.

## Usage

Invoke as `counters <command>` after `pip install -e .`, or equivalently
`python -m counters <command>`.

```bash
# --- indexing ---
counters index -v                                 # sync from genesis, then follow the tip
counters sync --stop-at 920000                    # one-shot catch-up (bounded for tests)

# --- reads (need only a synced index) ---
counters status                                   # bitcoind / Counterparty / index heights + rolling hash
counters list                                     # 20 most recent
counters list --recent 50
counters list --source bc1q...                    # by mint-time source address
counters list --block 902000-902100               # by block range
counters info 0                                   # metadata by number
counters info XDUALS                              # ...or by asset name / longname
counters info 0 --json                            # metadata as JSON
counters info 0 --raw > file.txt                  # stream the file bytes
counters info 0 --save file.gif                   # write the file to disk
counters validate <txid>                          # does this tx record a counter, and why / why not

# --- web explorer + read-only JSON API ---
counters server                                   # indexer + explorer on http://127.0.0.1:8081
counters server --no-index                        # serve only (index runs elsewhere)
counters server --host 0.0.0.0 --port 8081        # bind publicly / pick a port

# --- wallet (taproot BIP86, bc1p; keys held by Bitcoin Core) ---
counters wallet --name mywallet create            # new wallet; prints a 12-word seed ONCE
counters wallet --name mywallet restore           # re-import from a BIP39 seed (read on stdin) + rescan

# recover an OLD Counterparty wallet (Counterwallet / Freewallet — pre-BIP39 Electrum v1, legacy 1... addresses).
# The seed type is auto-detected; --counterwallet only forces it for a phrase valid as BOTH schemes. See wallets.md.
counters wallet --name old restore --dry-run                  # preview the derived 1... addresses; imports nothing
counters wallet --name old restore                            # import the legacy keys into Core + rescan
counters wallet --name mywallet receive           # next taproot (bc1p) address
counters wallet --name mywallet balance           # BTC + aggregated Counterparty balances
counters wallet --name mywallet inscriptions      # counters held by the wallet
counters wallet --name mywallet send bc1p... XDUALS 1         # transfer a counter (ADDRESS ASSET AMOUNT)
counters wallet --name mywallet send bc1p... XDUALS 1 --dry-run   # compose+sign, no broadcast

# mint a counter from a file. Counterparty Core composes the taproot
# commit/reveal pair and signs the reveal itself; the wallet signs the commit.
# --dry-run validates the package via testmempoolaccept WITHOUT broadcasting.
counters wallet --name mywallet inscribe --file cat.png --dry-run
counters wallet --name mywallet inscribe --file cat.png                     # free numeric asset
counters wallet --name mywallet inscribe --file cat.png --asset MYCOUNTER   # named (0.5 XCP)
counters wallet --name mywallet inscribe --file v2.png --asset MYCOUNTER    # EXISTING asset you own: reissue with new content (a new counter)
counters wallet --name mywallet inscribe --file cat.png --fee-rate 8

# --- asset management (owner-sourced Counterparty issuances) ---
counters wallet --name mywallet lock-supply MYCOUNTER         # freeze the supply
counters wallet --name mywallet lock-description MYCOUNTER    # freeze the content reference forever
counters wallet --name mywallet issue MYCOUNTER 100           # mint more supply (no new counter — no new content)
```

> The 12-word seed is the only backup and is shown once at create time. The
> keys are imported into a Bitcoin Core descriptor wallet, which holds them and
> does all signing; this tool never touches private keys after derivation.
> `--name` defaults to `counter`.

> Constraints inherited from Counterparty: taproot encoding cannot be combined
> with a destination output (so no `transfer_destination` on an inscription
> mint), and attaching new content to an existing asset requires its
> description to be unlocked.

## Tests

```bash
python -m pytest              # if pytest installed
python tests/test_reveal.py   # zero-dependency runners (also: test_content.py, test_pipeline.py)
```

## Layout

```
counters/
  config.py         protocol constants (genesis, marker, MIME gate) + env-driven Config
  reveal.py         script tokenizer + taproot-reveal (carrier) detection — rule R4
  content.py        deterministic content derivation + MIME normalization — §5
  bitcoind.py       JSON-RPC client (cookie auth, raw tx / fee lookups)
  counterparty.py   Core v2 client (the oracle): block issuances/fairminters, compose
  store.py          SQLite schema + blob store + rolling hash + reorg rollback
  tap.py            BIP340/341 primitives (address encoding for the wallet)
  bip32.py          BIP32/BIP86 derivation (pure-Python RIPEMD160 + ecdsa)
  counterwallet.py  Counterwallet/Freewallet legacy recovery
  electrum1.py      Electrum-v1 recovery for old Counterparty seeds
  electrum1_words.txt  the 1626-word Electrum-v1 list (verbatim from Electrum, MIT)
  electrum2.py      Electrum 2.x (standard/segwit) seed recovery
  progress.py       ord-style progress bar
  __main__.py       CLI command tree (parser + dispatch)
  indexer/          the indexing engine
    indexer.py      oracle-first pipeline + reorg rollback + run loops
  commands/         CLI command handlers
    read.py         status / info / list / validate
    wallet.py       create / restore / receive / balance / inscriptions
    inscribe.py     mint flow: compose via Core (encoding=taproot), sign commit, broadcast
    issue.py        lock-supply / lock-description / issue (owner-sourced)
    send.py         transfer a counter (Counterparty send)
    serve.py        explorer + JSON API orchestration
  server/           stdlib HTTP server + the bundled explorer SPA
docs/
  build-reference-v3.md   the authoritative protocol spec (v3)
  build-reference-v2.md   superseded COUNT-envelope spec (historical)
```
