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
every qualifying issuance (e.g. a reinscription — a Counterparty reissuance —
with fresh taproot-carried content). Reorgs roll back log-structured (the fork point is found from
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

# plain BTC: put BTC in the ASSET slot (amount in BTC; Bitcoin Core picks the inputs)
counters wallet --name mywallet send bc1p... BTC 0.001
counters wallet --name mywallet send bc1p... BTC 0.001 --fee-rate 3 --dry-run

# abandon an UNCONFIRMED transaction by replacing it (RBF): pick from the pending
# list, confirm, and the inputs come back to the wallet
counters wallet --name mywallet cancel
counters wallet --name mywallet cancel --txid <txid> --dry-run
# ...or price it for a MINER rather than for relay policy — far cheaper when the
# package being cancelled is large; prints hex to hand to a miner directly
counters wallet --name mywallet cancel --no-mempool-check --fee-rate 3

# make an unconfirmed transaction confirm faster, by CPFP: pick it, name the fee
# rate the whole package should reach, confirm, and a child is broadcast
counters wallet --name mywallet bump
counters wallet --name mywallet bump --txid <txid> --fee-rate 5

# buy from a dispenser. A plain BTC send to a dispenser address does NOTHING
# (see disable_vanilla_btc_dispense below) — a purchase needs this message.
# You say how much of the ASSET to buy; the price comes from the dispenser, and
# the total cost in BTC is shown for a y/n confirmation before anything is sent.
counters wallet --name mywallet buy-from-dispenser bc1q... 1          # buy 1 XCP
counters wallet --name mywallet buy-from-dispenser bc1q... 3 --fee-rate 3
counters wallet --name mywallet buy-from-dispenser bc1q... 1 --yes    # no prompt

# mint a counter from a file. Counterparty Core composes the taproot
# commit/reveal pair and signs the reveal itself; the wallet signs the commit.
# --dry-run validates the package via testmempoolaccept WITHOUT broadcasting.
counters wallet --name mywallet inscribe --file cat.png --dry-run
counters wallet --name mywallet inscribe --file cat.png                     # free numeric asset
counters wallet --name mywallet inscribe --file cat.png --asset MYCOUNTER   # named (0.5 XCP)
counters wallet --name mywallet inscribe --file v2.png --asset MYCOUNTER    # EXISTING asset you own: reinscribe with new content (a new counter)
counters wallet --name mywallet inscribe --file cat.png --fee-rate 8
# XCP on one address, BTC on another? Counterparty takes the issuance fee from the
# FIRST INPUT's address, so the source must own its coins — this moves them there first
counters wallet --name mywallet inscribe --file cat.png --asset MYCOUNTER --fund-from auto
counters wallet --name mywallet inscribe --file cat.png --asset MYCOUNTER --fund-from bc1p...

# --- asset management (owner-sourced Counterparty issuances) ---
counters wallet --name mywallet lock-supply MYCOUNTER         # freeze the supply
counters wallet --name mywallet lock-description MYCOUNTER    # freeze the content reference forever
counters wallet --name mywallet issue MYCOUNTER 100           # mint more supply (no new counter — no new content)
counters wallet --name mywallet transfer-ownership MYCOUNTER bc1p...   # hand over the issuance rights (ASSET ADDRESS)
```

> The 12-word seed is the only backup and is shown once at create time. The
> keys are imported into a Bitcoin Core descriptor wallet, which holds them and
> does all signing; this tool never touches private keys after derivation.
> `--name` defaults to `counter`.

> **A named mint is single-source.** Counterparty derives an issuance's source
> from the transaction's FIRST INPUT (`first_input_is_source`) and burns the
> 0.5 XCP from exactly that address; the composer enforces it ("source address
> does not match the first input address"). Extra inputs may come from other
> addresses, but since the composer orders inputs by value, the source's own
> coin has to be the largest — so funding a poor XCP address from a rich one
> does not work directly. `--fund-from` moves the shortfall to the source first
> and then inscribes.

> Constraints inherited from Counterparty: taproot encoding cannot be combined
> with a destination output (so no `transfer_destination` on an inscription
> mint — an ownership transfer is always its own transaction), and attaching
> new content to an existing asset requires its description to be unlocked.

> **Dispensers cannot be paid with a plain BTC send.** Since Counterparty's
> `disable_vanilla_btc_dispense` (block 866,000) a payment carrying no
> Counterparty data is discarded before the dispenser is consulted: the coins
> reach the operator's address and nothing is dispensed, with no error and no
> way back. `buy-from-dispenser` composes the `dispense` message the purchase
> actually requires. (The "never pay a dispenser from taproot" advice you may
> see elsewhere predates `taproot_support` at block 902,000; bc1p sources are
> fine now.)

> **Cancelling costs what relay policy demands, not what a miner needs.** A
> replacement must out-pay every transaction it evicts (BIP125 rule 3) — so
> cancelling a stuck inscription must beat commit + reveal *combined*, which is
> most of what the mint committed. That floor is anti-DoS relay policy, not
> economics: the replacement also hands back the package's block space, which a
> miner resells for far more than the fees given up. `--no-mempool-check` prices
> the replacement at its own size and a competitive rate instead, and prints the
> signed hex for direct submission, since no ordinary node will relay it.

> **An inscription's reveal cannot be sped up or replaced.** Counterparty signs
> it with an ephemeral envelope key, so it cannot be re-signed (no RBF), and it
> spends its whole input to fee, emitting only an `OP_RETURN` — so it has no
> output to attach a CPFP child to. A child on the *commit* is the reveal's
> sibling, not its ancestor, and does not lift it; replacing the commit changes
> its txid and merely invalidates the reveal. A reveal broadcast too cheaply can
> only be waited out, or abandoned with `cancel` on the commit and re-minted.

> Counterparty splits what English calls "owning" a counter in two. `send`
> moves the **tokens** (the asset balance); `transfer-ownership` moves the
> **issuance rights** — the power to reissue, lock, and reinscribe. Moving one
> does not move the other, so handing a counter over in full means doing both.

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
    cancel.py       abandon an unconfirmed transaction by RBF replacement
    bump.py         speed up an unconfirmed transaction by CPFP child
    dispenser.py    buy from a dispenser (composes the `dispense` message)
    serve.py        explorer + JSON API orchestration
  server/           stdlib HTTP server + the bundled explorer SPA
docs/
  build-reference-v3.md   the authoritative protocol spec (v3)
  build-reference-v2.md   superseded COUNT-envelope spec (historical)
```
