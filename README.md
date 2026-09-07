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

**Following Counterparty while it catches up.** Core answers every ledger
question with `503 Counterparty not ready` whenever it trails bitcoind by more
than a block (restart, reparse, downtime) — only `/v2/` keeps replying. An
indexer that knows only the API would sit still until Core is completely done
and then start on the whole backlog. So when `/v2/` reports
`server_ready: false` and Core's ledger database is readable locally
(`CP_DB_PATH`), the indexer reads that file directly — read-only, one
snapshot per block, only blocks Core has fully committed — and follows the
ledger block by block as Core parses. Rows are decoded to exactly the API's
shape (hex hashes, asset names, address strings), so the rules, numbering
and rolling hash see the same events through either door; the API takes
over again the moment Core is ready. The status line says
`counterparty - 964131/964249 · catching up · indexing from ledger db` while
this is happening, and both backends are re-polled once a second *during* a
pass, so that line and the bar's target (`y` in `x/y`) tick with Core while
`x` is the block the index has actually reached. With a remote Core there is no file to read: the index
waits, as before (or run Core with `--force`, which disables the not-ready
gate — Core marks that option as not for production).

One difference is deliberate. The API serves fairminters from Core's derived
state db, which keeps a single row per deploy and **moves its `block_index`
to the block of the latest status change** (pending → open → closed): a
deploy shows up under `/v2/blocks/{h}/fairminters` at its deploy block only
while it is still pending, and later under the block it opened or closed in.
The ledger logs one row per change and the deploy row keeps the deploy
block, so the ledger reader returns only deploy rows (joined to
`transactions`). Through the ledger a deploy is therefore always numbered at
its deploy block — which is also what the API path yields when the index is
following live, but not when a stretch of blocks is indexed after the fact.

## Requirements

- Python 3.10+
- A synced **bitcoind** with `txindex=1` (RPC reachable; cookie auth supported)
- A synced **Counterparty Core** v11+ API

```bash
pip install -e .          # installs deps + the `counters` console command
```

## Versioning — we mirror Counterparty Core

Counterparty Core is the oracle this project reads, so the version says which
Counterparty it speaks:

> **MAJOR.MINOR** name the Counterparty Core release a build targets — `11.2.x`
> targets Counterparty Core **v11.2.x**. **PATCH** is ours: it counts counters
> releases against that same upstream minor, and resets to `0` when we move up
> to a new one.

There is no separate "counters version" to reconcile against a compatibility
table — the number *is* the table. `counters/__init__.py` holds the single
source of truth; `pyproject.toml` reads it, and it surfaces in three places:

```bash
counters --version                # counters 11.2.0 (targets Counterparty Core v11.2)
curl -s localhost:8081/status     # {"version": "11.2.0", "commit": "88bb304",
                                  #  "updated": "2026-07-27T05:40:28Z", ...}
```

…and in the explorer footer, as `v11.2.0 · build 88bb304 · updated 2026-07-27`
— the version links to its release tag, the commit to the exact build, and
`updated` is when the deployed code last changed. Releases are tagged
`v<version>`.

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
| `COUNTER_NETWORK` | `mainnet` | `mainnet` or `regtest` — see [Regtest](#regtest) below |
| `BTC_RPC_URL` | `http://127.0.0.1:8332` | bitcoind JSON-RPC URL |
| `BTC_COOKIE_FILE` | `~/.bitcoin/.cookie` | bitcoind cookie (preferred auth) |
| `BTC_RPC_USER` / `BTC_RPC_PASSWORD` | — | fallback if no cookie |
| `CP_API_URL` | `http://127.0.0.1:4000` | Counterparty Core v2 API |
| `CP_DB_PATH` | `~/.local/share/counterparty/counterparty.db` | Core's ledger db, read directly (read-only) while Core is catching up and its API is closed; ignored if the file does not exist |
| `SLIPSTREAM_API_URL` | `https://slipstream.mara.com` | MARA Slipstream endpoint (`--slipstream`) |
| `SLIPSTREAM_API_KEY` | — | optional; only applies a fee discount, never required to submit |
| `COUNTER_DATA_DIR` | `data/` (`data-regtest/` on regtest) | SQLite + blobs location |
| `COUNTER_START_HEIGHT` | `902000` (`0` on regtest) | first block a fresh scan starts at (never below genesis) |
| `COUNTER_CONFIRMATIONS` | `0` | blocks behind tip to stay (6 recommended for near-final numbering) |
| `COUNTER_POLL_INTERVAL` | `15` | seconds between tip polls in `index` |

> A fresh scan starts at the protocol genesis (block **902,000** on mainnet,
> Counterparty v11's `taproot_support` activation) — by rule N3 nothing can
> qualify earlier, so there is no exhaustive-from-0 mode. Stored progress
> always wins; to rescan, `rm -rf data` first.

## Regtest

Setting `COUNTER_NETWORK=regtest` points every default at a local regtest
stack instead of mainnet:

| | mainnet | regtest |
| --- | --- | --- |
| `BTC_RPC_URL` | `http://127.0.0.1:8332` | `http://127.0.0.1:18443` |
| `CP_API_URL` | `http://127.0.0.1:4000` | `http://127.0.0.1:24000` |
| protocol genesis | block `902000` | block `0` |
| `EXTENDED_MIME_GATE` | block `952800` | block `0` |
| `COUNTER_DATA_DIR` | `data/` | `data-regtest/` |
| wallet address encoding | `bc1p...` / `bc1q...` / `1...`, `xprv` | `bcrt1p...` / `bcrt1q...` / regtest legacy, `tprv` |

The genesis/MIME-gate heights collapse to `0` because Counterparty Core's own
regtest activation table has no per-feature height — every protocol change is
active from block 0 on regtest (`counterpartycore.lib.parser.protocol.enabled`
short-circuits to `True` when `config.REGTEST`). `data-regtest/` is a
separate directory from mainnet's `data/` on purpose: the two networks have
incompatible genesis heights and rolling-hash chains, so sharing one index
would silently corrupt it.

```bash
export COUNTER_NETWORK=regtest
counters status                                    # confirms it's talking to your regtest node
counters wallet --name mywallet create              # a regtest (bcrt1p...) wallet, same commands as mainnet
counters index                                      # syncs from block 0, not 902,000
```

Everything under [Usage](#usage) works identically once `COUNTER_NETWORK` is
set — the wallet, indexer, and CLI don't otherwise know which network they're
on. One thing regtest doesn't give you for free: `counters` has no BTC→XCP
burn composer (mainnet's burn window closed at block 283,810, so no command
exposes it — see `commands/burn.py`), but regtest keeps that window open
indefinitely. To get XCP on a fresh regtest wallet, compose and broadcast a
classic burn directly against Counterparty Core's API:

```bash
# fund the wallet first (regtest coinbase needs 100 confirmations to mature)
bitcoin-cli -regtest generatetoaddress 101 <wallet receive address>

# then burn BTC for XCP (needs a UTXO on that address — inputs_set is
# required if your Counterparty regtest node has no address indexer)
curl "http://127.0.0.1:24000/v2/addresses/<address>/compose/burn?quantity=100000000&inputs_set=<txid>:<vout>"
# sign the returned rawtransaction with bitcoin-cli signrawtransactionwithwallet
# and broadcast with sendrawtransaction, then mine a block to confirm it
```

If `counters wallet inscribe` (or `send`/`issue`) fails with `No UTXOs found`,
your Counterparty regtest node has no Electrs/address-indexer configured, so
it can't auto-select inputs for anyone. Pass `--inputs-set TXID:VOUT`
(from `bitcoin-cli listunspent`) to work around it — every compose command
that funds itself accepts the flag.

The bundled `docker-compose.yml` defaults both services to regtest
(`COUNTER_NETWORK=regtest`, ports `18443`/`24000`, `rpcuser`/`rpcpassword`
`mempool`/`mempool` — matching the `counterparty/counterparty` regtest image's
own defaults). Override with `COUNTER_NETWORK=mainnet` plus the mainnet
`BTC_RPC_URL`/`CP_API_URL` (see the comment in the file) for a production
deploy.

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
counters info 0                                   # one counter: the inscription event
counters info 0 --detailed                        # every event field (block, txids, hashes, ...)
counters info XDUALS                              # the ASSET: supply, holders, its counters, totals
counters info XDUALS --trading                    # market state: DEX orders & matches, dispensers & dispenses
counters info 0 --json                            # metadata as JSON (asset name gives asset JSON)
counters info 0 --raw > file.txt                  # stream the file bytes
counters info 0 --save file.gif                   # write the file to disk (asset name: the original's)

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
counters wallet --name mywallet receive           # the wallet's first taproot (bc1p) address
counters wallet --name mywallet receive --new     # ...a fresh unused address instead
counters wallet --name mywallet receive --number 5  # ...the first 5 addresses
counters wallet --name mywallet balance           # BTC + aggregated Counterparty balances
counters wallet --name mywallet inscriptions      # counters held by the wallet
counters wallet --name mywallet send bc1p... XDUALS 1         # transfer a Counterparty asset (ADDRESS ASSET AMOUNT)
counters wallet --name mywallet send bc1p... XDUALS 1 --dry-run   # compose+sign, no broadcast
counters wallet --name mywallet send bc1p... XCP 1            # any Counterparty asset, not just counters
# every positional also has a flag form (here and on the other wallet commands):
counters wallet --name mywallet send --destination bc1p... --asset XDUALS --amount 1

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

# --- run a dispenser (the operator side) ---
# open: escrow an asset and vend it for BTC. The escrow leaves the address
# immediately; the price (satoshis per lot) can NEVER be changed while open —
# to reprice, close, wait ~5 blocks, and reopen.
counters wallet --name mywallet open-dispenser MYCOUNTER 100 --price 5000 --lot 1
counters wallet --name mywallet open-dispenser MYCOUNTER 1 --price 250000  # one lot: all-or-nothing
counters wallet --name mywallet dispensers                    # list yours, with status
# refill: adds stock on the SAME terms (Counterparty rejects any change, so the
# live terms are read from the chain — you only name the amount). Max 5 refills.
counters wallet --name mywallet refill-dispenser MYCOUNTER 50
# close: the dispenser keeps vending for ~5 blocks (status CLOSING), then the
# unsold stock returns to the closer.
counters wallet --name mywallet close-dispenser MYCOUNTER

# --- the DEX (Counterparty's on-chain order book + AMM) ---
# place an order: give one asset, get another; BTC is allowed on either side.
# A non-BTC give is escrowed until the order fills, expires, or is cancelled.
# Default --expiration 0 = the order NEVER expires (rests until cancel-order).
counters wallet --name mywallet open-order XCP 10 BTC 0.001              # sell XCP for BTC
counters wallet --name mywallet open-order BTC 0.001 XCP 10              # buy XCP with BTC
counters wallet --name mywallet open-order MYCOUNTER 1 XCP 5 --expiration 8064
counters wallet --name mywallet orders          # open orders + pending BTC settlements
counters wallet --name mywallet cancel-order <txhash>   # withdraw; escrow returns
# BTC is never escrowed: when a BTC-give order matches, the match is "pending"
# and the BTC must be paid within ~20 blocks — or the match expires and ALL your
# open BTC-give orders expire with it (the deadbeat penalty).
counters wallet --name mywallet pay-order               # settle it (auto-picks if one)

# mint a counter from a file. Counterparty Core composes the taproot
# commit/reveal pair and signs the reveal itself; the wallet signs the commit.
# --dry-run validates the package via testmempoolaccept WITHOUT broadcasting.
counters wallet --name mywallet inscribe --file cat.png --dry-run
counters wallet --name mywallet inscribe --file cat.png                     # free numeric asset
counters wallet --name mywallet inscribe --file cat.png --asset MYCOUNTER   # named (0.5 XCP)
counters wallet --name mywallet inscribe --file v2.png --asset MYCOUNTER    # EXISTING asset you own: reinscribe with new content (a new counter)
counters wallet --name mywallet inscribe --file cat.png --fee-rate 8
# pick the taproot envelope style (default: counterparty, Counterparty's own)
counters wallet --name mywallet inscribe --file cat.png --envelope counterparty/ord  # also an ordinals inscription
# XCP on one address, BTC on another? Counterparty takes the issuance fee from the
# FIRST INPUT's address, so the source must own its coins — this moves them there first
counters wallet --name mywallet inscribe --file cat.png --asset MYCOUNTER --fund-from auto
counters wallet --name mywallet inscribe --file cat.png --asset MYCOUNTER --fund-from bc1p...

# --- large inscriptions: MARA Slipstream ---
# Over 400k WU a reveal is non-standard: no node relays it and sendrawtransaction
# is a dead end. --slipstream submits it straight to MARA's mempool instead.
# No API key required. Pays Slipstream's live minimum rate unless --fee-rate is higher.
counters wallet --name mywallet inscribe --file big.png --asset BIGONE --slipstream
counters wallet --name mywallet inscribe --file big.png --asset BIGONE --slipstream --dry-run
counters wallet inscribe --slipstream-status <TXID>   # the only way to watch it

# --- asset management (owner-sourced Counterparty issuances) ---
counters wallet --name mywallet lock-supply MYCOUNTER         # freeze the supply
counters wallet --name mywallet lock-description MYCOUNTER    # freeze the content reference forever
counters wallet --name mywallet issue MYCOUNTER 100           # mint more supply (no new counter — no new content)
counters wallet --name mywallet transfer-ownership MYCOUNTER bc1p...   # hand over the issuance rights (ASSET ADDRESS)

# --- the traditional description (OP_RETURN text, NOT a counter) ---
# What Counterparty assets carried before taproot: a tagline, or a URL to the
# metadata — ZOMBIEPEPES reads "BURN THEM ALL", HONDACIVIC reads
# "https://xcp.fun/HONDACIVIC.json". It is a zero-quantity issuance whose text
# rides in the OP_RETURN, so nothing is numbered: `inscribe --asset` is what
# commits content and mints a counter.
counters wallet --name mywallet describe MYASSET "https://xcp.fun/MYASSET.json"
counters wallet --name mywallet describe MYASSET --file description.txt   # UTF-8 text; one trailing newline dropped
counters wallet --name mywallet describe MYASSET --clear                  # empty the description
counters wallet --name mywallet describe --asset MYASSET --text "BURN THEM ALL" --dry-run
```

> **How much text fits.** The whole Counterparty message must fit a single
> 80-byte `OP_RETURN`: after the `CNTRPRTY` prefix and the CBOR-framed issuance
> fields (asset id, quantity, flags, mime type), that leaves **54-58 bytes** of
> description — 58 for a small asset id, 54 for the largest, and fewer still if
> the issuance carries a quantity. This is why the tradition is to point at the metadata
> rather than to embed it. Anything larger needs the taproot envelope
> (`inscribe --asset MYASSET --file ...`), which mints a NEW counter.
> `describe` never falls back to it silently.

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

> **`--envelope` — which taproot envelope carries the file.** Counterparty v11
> can build the witness two ways, and both count equally as counters (R4): the
> style is enrichment, never validity or numbering.
>
> - **`counterparty`** (default) — Counterparty's own envelope: `OP_FALSE OP_IF`,
>   the serialized message in 520-byte chunks, `OP_ENDIF`. Nothing but
>   Counterparty reads it.
> - **`counterparty/ord`** — the same Counterparty envelope *plus* the ordinals framing:
>   tagged with the content type (tag 1), the metaprotocol `xcp` (tag 7) and
>   the rest of the issuance as CBOR metadata (tag 5), with the file itself as
>   the body. It is not an alternative to `counterparty` but a superset of it — the
>   reveal creates **two independently ownable assets**, the Counterparty asset
>   and an ordinals inscription on its own UTXO that can be sent away from it.
>   That second asset is why Core adds a dust output here and not for `counterparty`:
>   the inscription needs a sat to live on. Hence the name, and hence the first
>   five counters ever minted — XDUALS, DUALNAKA, DUALPEPE. It costs roughly **+150 WU** over
>   counterparty and does not scale with the file: ~124 WU for the dust output Core
>   adds for an ordinals envelope (31 vB at the 4x output rate) plus ~26 WU of
>   tags — `"ord"`, `0x07`, `"xcp"`, `0x01`, the MIME string, and one `0x05`
>   per metadata chunk. Only the MIME string's length moves the figure.
>
> The style is fixed by the tapscript the commit address commits to, so it can
> never be changed after the fact. Core applies `counterparty/ord` only to a
> content-carrying issuance and otherwise falls back to counterparty *silently* —
> so the composed reveal is classified before anything is broadcast, and a mint
> that did not get the style you asked for is refused rather than sent.
>
> Historically the choice mattered: of the first 87 counters, 34 were minted
> counterparty + ord and 8 as Bitcoin Stamps, while counterparty native was used 53
> times — but 46 of those carried only a pointer. Across all 164 counters to
> date the split is 47 counterparty + ord to 117 counterparty native, with 98 of
> the native ones being pointers and no counterparty + ord counter ever having
> been one.

> Constraints inherited from Counterparty: taproot encoding cannot be combined
> with a destination output (so no `transfer_destination` on an inscription
> mint — an ownership transfer is always its own transaction), and attaching
> new content to an existing asset requires its description to be unlocked.

> **`--slipstream` — minting past the relay limit.** An inscription whose reveal
> exceeds Bitcoin's 400,000 WU standard-relay cap is a perfectly *valid*
> transaction that no node will forward, so the local node cannot publish it.
> MARA Slipstream accepts such transactions directly into its own mempool.
> Four things follow, and the flag handles each:
>
> - **No API key.** `/api/rates`, `/api/transactions` and the status endpoint all
>   answer unauthenticated. `SLIPSTREAM_API_KEY` is honoured if set, but it only
>   applies a fee discount MARA has assigned — it is never needed to mint.
> - **The minimum fee rate is live.** It is the higher of `submit_fee_rate`× the
>   current mempool priority rate or `submit_fee_rate` sat/vB, published resolved
>   as `effective_rate`. `--slipstream` fetches it *before* funding (the source
>   top-up is sized from the rate) and adopts it as the default. A `--fee-rate`
>   *below* it is refused rather than silently raised — on a ~1M vB reveal, one
>   sat/vB is ~1M sats.
> - **A hard 3,991,000 WU ceiling** (~99.8% of a block) caps how large any single
>   inscription can be. It is checked locally before the commit is sent, because
>   past that point the reveal cannot be re-composed (see below).
> - **Submissions stay private until mined** — they are not relayed publicly until
>   they have a confirmation, so bitcoind and every explorer are blind to them.
>   `--slipstream-status TXID` is the only way to watch one.
>
> Slipstream has no package endpoint, so the commit and reveal are submitted
> individually, commit first. If the commit is accepted and the reveal is not,
> the reveal hex is printed loudly: Counterparty signed that reveal with an
> ephemeral key it discarded, making it the only transaction that can ever spend
> the commit output — it cannot be re-composed, fee-bumped, or replaced.

> **Dispensers cannot be paid with a plain BTC send.** Since Counterparty's
> `disable_vanilla_btc_dispense` (block 866,000) a payment carrying no
> Counterparty data is discarded before the dispenser is consulted: the coins
> reach the operator's address and nothing is dispensed, with no error and no
> way back. `buy-from-dispenser` composes the `dispense` message the purchase
> actually requires. (The "never pay a dispenser from taproot" advice you may
> see elsewhere predates `taproot_support` at block 902,000; bc1p sources are
> fine now.)

> **DEX escrow is asymmetric.** An order's non-BTC give side is locked the
> moment the order confirms and only returns on cancel or expiry — with the
> default indefinite expiration that means until an explicit `cancel-order`.
> BTC is never locked: each match against a BTC-give order must be settled with
> `pay-order` within ~20 blocks, and a missed deadline expires the match *and
> every other open BTC-give order from that address*. A BTC-give order's own
> miner fee is also its `fee_provided` matching budget — counterparties whose
> `fee_required` exceeds it will never match, so a dust-fee buy order can rest
> forever untouched.

> **Cancelling costs what relay policy demands, not what a miner needs.** A
> replacement must out-pay every transaction it evicts (BIP125 rule 3) — so
> cancelling a stuck inscription must beat commit + reveal *combined*, which is
> most of what the mint committed. That floor is anti-DoS relay policy, not
> economics: the replacement also hands back the package's block space, which a
> miner resells for far more than the fees given up. `--no-mempool-check` prices
> the replacement at its own size and a competitive rate instead, and prints the
> signed hex for direct submission, since no ordinary node will relay it.

> **A reveal can never be replaced, and only a `counterparty/ord` one can be sped up.**
> Counterparty signs the reveal with an ephemeral envelope key it discards, so
> it can never be re-signed: no RBF, in either style. CPFP, however, depends on
> the envelope. A `counterparty` reveal spends its whole input to fee and emits only
> an `OP_RETURN`, so there is no output to attach a child to. A `counterparty/ord` reveal
> additionally carries the dust output Core adds for an ordinals envelope
> (v11.0.0: "when using an Ordinals envelope script, add a dust output for the
> source address"); it pays the source address, so it *can* anchor a CPFP child.
> All 47 counterparty + ord counters to date have it and all 117 native ones do not. A child
> on the *commit* is the reveal's sibling, not its ancestor, and lifts neither;
> replacing the commit changes its txid and merely invalidates the reveal. A
> cheap `counterparty` reveal can only be waited out, or abandoned with `cancel` on
> the commit and re-minted.

> Counterparty splits what English calls "owning" a counter in two. `send`
> moves the **tokens** (the asset balance); `transfer-ownership` moves the
> **issuance rights** — the power to reissue, lock, and reinscribe. Moving one
> does not move the other, so handing a counter over in full means doing both.

## Link previews

Pasting a counter link — `https://www.bitcoincounters.com/c/95` — into Telegram,
WhatsApp, Twitter, Discord, Slack or iMessage shows that counter, not the site
logo. Crawlers run no JavaScript, so `/c/<id>` is served by the server with the
counter's own Open Graph tags substituted into the SPA shell; the explorer
itself is unchanged and still renders the page from the same URL.

What `og:image` points at depends on the content:

| counter | `og:image` |
|---|---|
| a raster image under 300 KB | `/content/<n>` — the file itself, untouched (animated GIFs keep animating) |
| a raster image over 300 KB | `/social/<n>.png` — the same picture, box-downsampled until a crawler will fetch it, then pixel-doubled back over 600 px (when the bytes allow) so chat apps keep their full-width preview layout |
| a stamp (`STAMP:<base64>`) | `/stamp/<n>` — the decoded image |
| anything else | `/social/<n>.png` — a rendered 1200x630 card |

The card carries what the detail page shows: the content itself on the left
(text, a pointer URI, HTML source, or the format name for audio and binary),
and the number, asset, badges and facts on the right, in the explorer's own
palette and odometer style.

The 300 KB threshold is the practical ceiling for the strictest mainstream
crawler, not a format limit. Oversized **JPEG** and **GIF** are the one gap:
they are served at full size, because downscaling them would mean shipping a
JPEG decoder, and Telegram, Twitter and Discord fetch them fine anyway.

Card images are drawn with no imaging or font library — `png.py` is a small
PNG codec over `zlib`, and `glyphs.py` embeds the public-domain X11
`misc-fixed` 10x20 bitmap font (~1.9 KB). They are cached under
`$COUNTER_DATA_DIR/social`, keyed by content hash and renderer version, so a
redesign invalidates them and the directory is safe to delete at any time.

## PDFs

Counters run to whole blocks, so a counter can be an entire book. A PDF counter
previews as a scrolling document: every page in one column, painted as it comes
into view and released once it is well past, so a 400-page file costs about a
screenful of bitmap rather than hundreds of megabytes. `/content/<n>` serves
`Range`, and the viewer asks for byte ranges as it goes — a card thumbnail
showing page 1 costs a few KB, not the whole inscription.

This is the one media kind whose frame gets a real origin
(`sandbox="allow-scripts allow-same-origin"`), and the reason is worth writing
down, because it is the only place the preview model bends:

* **The browser's own PDF viewer can never be used.** It is a plugin, and the
  sandbox flags that confine every preview frame block plugins outright — a
  PDF in an `<iframe>` renders as a broken-document icon and nothing else.
  So the pages have to be painted by JavaScript, which means pdf.js.
* **pdf.js cannot run on an opaque origin.** A frame without
  `allow-same-origin` cannot start a worker, and cannot load an ES module at
  all — the entry script arrives, its imports are never fetched, and a dynamic
  `import()` hangs instead of failing. Without a worker pdf.js falls back to
  decoding on the main thread, and that fallback paints nothing.

What that frame runs is still only our own wrapper and our own vendored pdf.js;
the inscription is *data* handed to a parser that does not execute a PDF's
embedded JavaScript, with `isEvalSupported` off. That is the difference from an
HTML or SVG counter, which really is executable content and must keep the
opaque origin it has today.

pdf.js is vendored (`static/pdfjs.min.js` + its worker, ~1.4 MB, pinned to
3.11.174 — 4.x ships ES modules only) rather than loaded from a CDN, so the
explorer renders the same offline as online. The standard 14 fonts ship with it
(~0.8 MB): a PDF paying by the on-chain byte has every reason to name a base
font instead of embedding one, and without that data such a file renders with
no text at all.

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
  ledger.py         the same oracle questions answered from Core's ledger db, read-only (API closed while catching up)
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
    read.py         status / info / list
    wallet.py       create / restore / receive / balance / inscriptions
    inscribe.py     mint flow: compose via Core (encoding=taproot), sign commit, broadcast
    issue.py        lock-supply / lock-description / describe / issue (owner-sourced)
    send.py         transfer a counter (Counterparty send) or plain BTC
    cancel.py       abandon an unconfirmed transaction by RBF replacement
    bump.py         speed up an unconfirmed transaction by CPFP child
    dispenser.py    buy from a dispenser, and run one: open / refill / close / list
    order.py        DEX: open-order / cancel-order / pay-order / orders
    serve.py        explorer + JSON API orchestration
  server/           stdlib HTTP server + the bundled explorer SPA
    app.py          routes, JSON API, per-counter Open Graph tags
    preview.py      ord-style sandboxed preview wrappers per media type
    card.py         the 1200x630 og:image drawn for non-image counters
    png.py          minimal PNG encoder/decoder/downscaler (stdlib only)
    glyphs.py       embedded public-domain bitmap font (generated)
    static/
      preview-pdf.js         the scrolling PDF viewer (see "PDFs" below)
      pdfjs.min.js           vendored pdf.js 3.11.174 (classic build)
      pdfjs.worker.min.js    its decoder worker
      pdfjs-standard-fonts/  the standard 14 fonts, for PDFs that embed none
docs/
  build-reference-v3.md   the authoritative protocol spec (v3)
  build-reference-v2.md   superseded COUNT-envelope spec (historical)
```
