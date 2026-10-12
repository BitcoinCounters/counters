# Counterparty Core v11.5.0 upgrade and hardfork — notes

_Started 2026-10-12. Carried over from earlier sessions in ~/counters (Oct 7–11)._

## 1. The consensus change

- **Rule:** `require_reveal_source_signature`, active from mainnet block **969,320**.
- A taproot-envelope reveal is only attributed to its source if the envelope leaf
  is closed with a key belonging to the **source address**. Reveals signed with a
  throwaway key are **ignored**, not marked invalid.
- Motivation: fix for a sweep attack (anyone could reveal someone else's commit).
- Ignored reveals leave **no ledger rows at all**, only a log warning in Core.

## 2. Observed impact on counters (as of 2026-10-11)

| Source | Core version | Counter count |
|---|---|---|
| local node | 11.5.0 | 544 |
| counters.gallery | 11.2.0 | 560 |

Fifteen reveals ignored since activation (table from 2026-10-11 session). Fees are commit + reveal, sats.
Gallery # is the counters.gallery number that does not exist on consensus.

| Gallery # | Block | Date (UTC) | Asset | Content | Source | Fees paid |
|---|---|---|---|---|---|---|
| 532 | 969,525 | Oct 2 | PEPEMAXXER | jpeg 61 KB | bc1q4j2gh7… | 4,654 |
| 533 | 969,683 | Oct 3 | DEGENT.3 | jpeg 3.95 MB | bc1qznsu8… | 1,004,164 |
| 534 | 969,697 | Oct 3 | A13034237042982401926 | png 395 KB | bc1qcx8cju… | 59,762 |
| 540 | 969,919 | Oct 4 | DEGENT.5 | jpeg 3.96 MB | bc1qznsu8… | 1,002,861 |
| 541 | 969,958 | Oct 5 | DEGENT.4 | jpeg 3.95 MB | bc1qznsu8… | 1,002,550 |
| 542 | 969,970 | Oct 5 | NINESIX | jpeg 352 KB | bc1prx0khu… | 35,493 |
| 543 | 969,970 | Oct 5 | NINESIXNINE | jpeg 366 KB | bc1prx0khu… | 36,901 |
| 547 | 970,195 | Oct 6 | DEGENT.6 | jpeg 3.96 MB | bc1qznsu8… | 1,002,102 |
| 548 | 970,297 | Oct 7 | RAREBLOCKS | svg 48 KB | bc1p050y4c… | 18,808 |
| 552 | 970,578 | Oct 9 | DEGENT.7 | jpeg 3.95 MB | bc1qznsu8… | 1,000,173 |
| 554 | 970,705 | Oct 10 | DEGENT.8 | jpeg 3.96 MB | bc1qznsu8… | 1,000,098 |
| 555 | 970,765 | Oct 10 | DEGENT.10 | jpeg 3.96 MB | bc1qznsu8… | 997,982 |
| 556 | 970,842 | Oct 10 | DEGENT.9 | jpeg 3.95 MB | bc1qznsu8… | 997,972 |
| 557 | 970,843 | Oct 10 | DEGENT.11 | jpeg 3.96 MB | bc1qznsu8… | 997,266 |
| 558 | 970,858 | Oct 11 | DEGENT.12 | jpeg 3.96 MB | bc1qznsu8… | 996,569 |

Total: 10,157,355 sats (~0.1016 BTC) for 10.26 MB of block space. The DEGENT issuer is ten of
fifteen and 99% of the fees; all ten DEGENT commits were made Oct 1 (blocks 969,488 / 969,497),
more than a day after activation, i.e. composed with an old node.

DEGENT timeline (commit → reveal):

| Asset | Commit block | Commit time | Reveal block | Reveal time | Status |
|---|---|---|---|---|---|
| DEGENT.0 | 967,378 | Sep 17, 07:30 | 967,440 | Sep 17, 16:35 | valid |
| DEGENT.1 | 967,380 | Sep 17, 07:45 | 967,766 | Sep 20, 00:39 | valid |
| DEGENT.2 | 967,787 | Sep 20, 03:03 | 967,926 | Sep 21, 02:09 | valid |
| **Activation** | | | **969,320** | **Sep 30, 16:48** | `require_reveal_source_signature` |
| DEGENT.3 | 969,488 | Oct 1, 19:59 | 969,683 | Oct 3, 05:51 | ignored |
| DEGENT.4 | 969,488 | Oct 1, 19:59 | 969,958 | Oct 5, 04:59 | ignored |
| DEGENT.5 | 969,497 | Oct 1, 22:34 | 969,919 | Oct 4, 23:28 | ignored |
| DEGENT.6 | 969,497 | Oct 1, 22:34 | 970,195 | Oct 6, 15:57 | ignored |
| DEGENT.7 | 969,497 | Oct 1, 22:34 | 970,578 | Oct 9, 03:37 | ignored |
| DEGENT.8 | 969,497 | Oct 1, 22:34 | 970,705 | Oct 10, 01:29 | ignored |
| DEGENT.10 | 969,497 | Oct 1, 22:34 | 970,765 | Oct 10, 12:34 | ignored |
| DEGENT.9 | 969,497 | Oct 1, 22:34 | 970,842 | Oct 10, 23:17 | ignored |
| DEGENT.11 | 969,497 | Oct 1, 22:34 | 970,843 | Oct 10, 23:19 | ignored |
| DEGENT.12 | 969,497 | Oct 1, 22:34 | 970,858 | Oct 11, 01:57 | ignored |

Counter-examples that succeeded after activation (envelope key matched source): UNITESSTATES,
MMCXXXI, FAKEBANG.420 and twelve HONDACIVIC subassets.

Who agrees with whom:
- **Say they exist:** tokenscan.io, counters.gallery (both on old Core).
- **Say not found:** xcp.io, api.counterparty.io, local node.
- Treat the **local node as consensus**, not counters.gallery.

## 3. API / wallet changes

- Compose no longer returns `signed_reveal_rawtransaction`.
- It now returns an **unsigned reveal** plus `envelope_script`, `reveal_control_block`,
  `reveal_pubkey`.
- The wallet must sign the reveal leaf itself (BIP342 script-path spend) with the
  source key. Taproot encoding requires a **P2WPKH or P2TR** source.
- `counters/commands/inscribe.py` still expects the old signed reveal → **broken until updated**.

## 4. Local node / infra state

- Core v11.5.0 built from `~/counterparty-core` (tag v11.5.0) into `~/.venvs/counterparty`,
  run natively via `~/service-manager/svc` since 2026-10-07. No Docker.
- Core aborts on startup if any pid holds `counterparty.db-wal` open. counters'
  `ledger.py` must open a sqlite connection per call and never hold one.
- Do **not** open counterparty.db with sqlite `immutable=1` (skips the WAL, hides recent rows).
  Query the local API on port 4000 instead.
- Custom bitcoind (v29.99, /usr/local/bin) lacks ZMQ → Core's follower retries recursively
  and dies with "maximum recursion depth exceeded" after ~967 retries (~85 min per run).
  Crashes seen Oct 9 17:06, Oct 11 09:28 and 14:57.
- svc monitor has been dead since the 2026-10-10 22:02 suspend; counterparty is started by hand.
  Stop any hand-started instance before `svc up` so two never share the db.

## 5. Open decisions / TODO

- [ ] Update `inscribe.py` to sign the reveal leaf (BIP342) with the source key.
- [ ] Decide: reindex local numbering from the public API for parity with counters.gallery,
      or keep local (consensus) numbering and let the public site catch up.
- [ ] Ledger-door gaps: `burned` / `xcp_burned` never filled, supply diffs, fairminter
      block choice, multi-message tx decoding.
- [ ] Rebuild bitcoind with `-DWITH_ZMQ=ON`; add zmqpub* lines (tcp://127.0.0.1:9332 / :9333).
      Needs sudo to replace /usr/local/bin/bitcoind.
- [ ] Restart the svc monitor.
- [ ] Consider reporting Core's recursive ZMQ retry upstream.
- [ ] Decide what to tell affected users about the 15 ignored reveals (DEGENT.3–12 etc.).
- [ ] counters indexer parity: gallery counts LP token A18189972090142917414 (#163), local does not; gallery missed CRAZYDANK (970,988). Neither is a fork issue.


## 7. Full explorer diff (started 2026-10-12)

Goal: every asset that exists on old-consensus explorers (tokenscan.io, counters.gallery, Core ≤11.2)
but not on v11.5.0 nodes (local node, api.counterparty.io, xcp.io).

Method:
- tokenscan `GET /api/issuances/<block>/<page>` for every block 969,320 → tip (971,049), vs
  local Core `GET /v2/issuances` (tx-hash match; tokenscan truncates tx_hash to 40 hex chars).
- counters.gallery `GET /counters?limit=500&before=N` (561 rows) vs local `counters.db` (546 rows).
- tokenscan rate-limits at ~4 req/s (Cloudflare 1015 / HTTP 429); one worker at ~1 req/s is fine.

### counters.gallery vs local counters (done)

16 gallery counters are not in the local index; 1 local counter is not in the gallery.

- 15 of the 16 are exactly the ignored-reveal table above (#532–#558). Consensus difference.
- The 16th, **#163 A18189972090142917414** (block 965,043, pre-activation), is an AMM LP token
  ("LP token for MEMENOME/XCP pool", source 1CounterpartyXXXX…, envelope `counterparty/ord`,
  msg_index 1). It exists on the local v11.5.0 Core, so this is an **indexer rule difference**
  in counters (gallery counts it, local does not), not a fork effect. This is the "one more
  numbering shift" from the Oct 11 session.
- Local-only: **CRAZYDANK** (#544 locally, block 970,988, bc1p050y4c…, 10 units, locked,
  inline-JSON description, 50 XCP issuance fee). Valid on local v11.5.0 Core; the gallery (indexed
  to 971,049) has no record of it. Reason not yet established, see tokenscan check below.

### tokenscan vs local Core (done via tx_index proof, 2026-10-12)

Tokenscan and Core both number Counterparty transactions sequentially. Every reveal that
v11.5.0 ignores adds one to tokenscan's `tx_index` but not the local node's, so the gap
between the two for the same tx counts the extra transactions of **any** type:

| Anchor tx | Block | Local tx_index | Tokenscan tx_index | Delta |
|---|---|---|---|---|
| DEGENT.2 reveal (pre-fork) | 967,926 | 3,188,431 | 3,188,431 | 0 |
| issuance at 970,096 | 970,096 | 3,192,998 | 3,193,005 | 7 |
| CRAZYDANK issuance | 970,988 | 3,195,998 | 3,196,013 | 15 |
| newest local issuance | 971,044 | 3,196,035 | 3,196,050 | 15 |

The delta of 7 at block 970,096 matches the seven ignored reveals up to then (PEPEMAXXER,
DEGENT.3, A1303…, DEGENT.5, DEGENT.4, NINESIX, NINESIXNINE), and 15 at 970,988 matches the
full table. Since the delta is exactly 15 and we know 15 extra txs, there are also **zero**
transactions the local node has that tokenscan lacks.

**Result: as of block 971,049 the fifteen assets in the table above are the complete set that
exists on tokenscan.io and counters.gallery but not on v11.5.0 nodes.** No other asset, and no
non-issuance message, is affected.

Partial block sweep (368 of 1,730 blocks fetched before tokenscan's rate limit made it
impractical) agreed: PEPEMAXXER and RAREBLOCKS were the only tokenscan-only issuance txs in
the fetched blocks, and no local-only txs. Raw data kept in the session scratchpad only.

CRAZYDANK follow-up: tokenscan **does** have it (tx_index 3,196,013), so its absence from
counters.gallery is a gallery indexer miss, not a consensus difference.

## 8. Master table: assets on old-consensus explorers but not on v11.5.0 nodes

Complete as of block 971,049 (see §7 for the proof). Rows in reveal order. Times are 2026, UTC.
Commit blocks come from bitcoind (the reveal's first input is the commit output); gallery
numbers from counters.gallery; sources from tokenscan.

| # | Asset | Gallery # | Source | Commit block | Commit time (UTC) | Reveal block | Reveal time (UTC) | Commit tx | Reveal tx |
|---|---|---|---|---|---|---|---|---|---|
| | **Activation: `require_reveal_source_signature`** | | | | | **969,320** | **Sep 30 16:48** | | |
| 1 | PEPEMAXXER | 532 | bc1q4j2gh7… | 969,518 | Oct 02 01:35 | 969,525 | Oct 02 02:42 | `0a97410e2a2c…` | `afa122ca8aea…` |
| 2 | DEGENT.3 | 533 | bc1qznsu8j… | 969,488 | Oct 01 19:59 | 969,683 | Oct 03 05:51 | `2ef8f20ac575…` | `7d640bf0a0d3…` |
| 3 | A13034237042982401926 | 534 | bc1qcx8cju… | 969,697 | Oct 03 07:26 | 969,697 | Oct 03 07:26 | `2e3f4616391f…` | `bd1f913c4419…` |
| 4 | DEGENT.5 | 540 | bc1qznsu8j… | 969,497 | Oct 01 22:34 | 969,919 | Oct 04 23:28 | `35960a2ddbfa…` | `f34bf01e2fb0…` |
| 5 | DEGENT.4 | 541 | bc1qznsu8j… | 969,488 | Oct 01 19:59 | 969,958 | Oct 05 04:59 | `fcff198e110d…` | `ea72e53473ce…` |
| 6 | NINESIX | 542 | bc1prx0khu… | 969,970 | Oct 05 06:46 | 969,970 | Oct 05 06:46 | `df57eec1083a…` | `19d2e817f4b3…` |
| 7 | NINESIXNINE | 543 | bc1prx0khu… | 969,970 | Oct 05 06:46 | 969,970 | Oct 05 06:46 | `3449ab122780…` | `b27384b62258…` |
| 8 | DEGENT.6 | 547 | bc1qznsu8j… | 969,497 | Oct 01 22:34 | 970,195 | Oct 06 15:57 | `7855cfdd4237…` | `b02f497d20ed…` |
| 9 | RAREBLOCKS | 548 | bc1p050y4c… | 970,297 | Oct 07 05:41 | 970,297 | Oct 07 05:41 | `f92ff0e3297f…` | `b1d677c671f9…` |
| 10 | DEGENT.7 | 552 | bc1qznsu8j… | 969,497 | Oct 01 22:34 | 970,578 | Oct 09 03:37 | `4bd00f72a412…` | `c1730efbdfd1…` |
| 11 | DEGENT.8 | 554 | bc1qznsu8j… | 969,497 | Oct 01 22:34 | 970,705 | Oct 10 01:29 | `91ba64217c7d…` | `8383fd5a0f02…` |
| 12 | DEGENT.10 | 555 | bc1qznsu8j… | 969,497 | Oct 01 22:34 | 970,765 | Oct 10 12:34 | `1fcae0c9c489…` | `c63b570221c5…` |
| 13 | DEGENT.9 | 556 | bc1qznsu8j… | 969,497 | Oct 01 22:34 | 970,842 | Oct 10 23:17 | `2ebf1cd07213…` | `3d7a29c39d89…` |
| 14 | DEGENT.11 | 557 | bc1qznsu8j… | 969,497 | Oct 01 22:34 | 970,843 | Oct 10 23:19 | `75b11754da71…` | `24720cfeab13…` |
| 15 | DEGENT.12 | 558 | bc1qznsu8j… | 969,497 | Oct 01 22:34 | 970,858 | Oct 11 01:57 | `7d03b4aa7631…` | `92fd95caccd7…` |

Observations:
- Every commit was made **after** activation; nothing was in flight across the boundary. The
  earliest (DEGENT.3/.4, block 969,488) came 168 blocks / 27 hours after block 969,320.
- Five of the fifteen were commit-and-reveal in the **same block** (A1303…, NINESIX,
  NINESIXNINE, RAREBLOCKS) or 7 blocks apart (PEPEMAXXER): normal wallet flow. The ten DEGENT
  reveals were batched-committed on Oct 1 and revealed one at a time over ten days.
- The DEGENT.3 and DEGENT.4 commits share block 969,488; DEGENT.5–.12 share block 969,497.

Full txids:
- PEPEMAXXER: commit `0a97410e2a2c2bb5ce6c8cfd361c8e441aabc1e6886f748826581850650e2ad8` reveal `afa122ca8aeaaed9fcf40619b0b949d6037ffcf8ebcbcceb8efd216bc9c48e83`
- DEGENT.3: commit `2ef8f20ac575f2d4c4288c67f7f0fac6392af9836cdf07c83c597d08e9984247` reveal `7d640bf0a0d3a99042af72010f58c2d13d1a3e039fa08101d8bde6bca17dd93c`
- A13034237042982401926: commit `2e3f4616391f089ac46f5ed43a5262468dc179fffcbd0673c7abff0f344a53a8` reveal `bd1f913c44197452e8c03d581684bf710c3e0d3fb94843cd0789b5651f7eea1c`
- DEGENT.5: commit `35960a2ddbfa0faf89c81a6ea420d849ed0139941558d1206370bb8ae98356da` reveal `f34bf01e2fb093669f577bb3868c57d991ca4e1988fc1662436e0eb06c664201`
- DEGENT.4: commit `fcff198e110dc97cfb2e02d1c2bdc2cd2ba349d9a903856297bf1f8f156286fb` reveal `ea72e53473ce46f14fb2bcabc6d2c1bce888ec4219623daba1be6b8d771f746c`
- NINESIX: commit `df57eec1083a15f29dfcb6cc71615060204602e28946925b239c0c145fca8ea0` reveal `19d2e817f4b3c855004e216477ecbdd3356bf7d80a0e794758946da1cd21cb28`
- NINESIXNINE: commit `3449ab122780efdf846d8df6a23c3d78b812a86c9a62c4d51ecd94d507ff7056` reveal `b27384b622582c47efdea7b650fa8392848411a7dbea0bc57dadc8b6532ccc91`
- DEGENT.6: commit `7855cfdd4237ac032eca4f036d5a5cd7c8e824eebc79250ecc2eef48690b1eda` reveal `b02f497d20ed488a9427d941ba9695c5e10fd8473c8b206c0b6bb60c98cba0e6`
- RAREBLOCKS: commit `f92ff0e3297f75a88a53e92aaf51bfebdb0759a107d1b5e80e004c7ce47721c2` reveal `b1d677c671f9e073d7f47bba8ba453b93a334a2d90a4ab0b5c7d63880a9c8d70`
- DEGENT.7: commit `4bd00f72a412cf088256587e65c970ea97a1375d81101e5d8233c0f08fd9f604` reveal `c1730efbdfd1c783c2c0d2033b8d263a2df16acd637f45e56b697bd217eb8973`
- DEGENT.8: commit `91ba64217c7daa6249a84bc3bdbe2c4b9905db78d5e7bdd87a70559a92263d91` reveal `8383fd5a0f02b51ad9d31704ae8117e94de6ad19ba6d81118e19ddfb330912e1`
- DEGENT.10: commit `1fcae0c9c4891f9e6809526f2a742cd3a79fcaba5c8ccbc53e73c789dc8276ea` reveal `c63b570221c5ae05706aee479d175aee960098c4e7a1176470ecdd9a83b15000`
- DEGENT.9: commit `2ebf1cd0721324a07d2f2d5f74f2e1406eb03ee9485acaaec0ceeaf02ec6f570` reveal `3d7a29c39d89b1839f9e625d2ef3cedff79c04dedda97e0cb2eb6536926ae5ed`
- DEGENT.11: commit `75b11754da7115f40dbed4e179f42e82742da2bb38532d3ab356cb6cb8e4e078` reveal `24720cfeab130156fc32d095c2dd7b64d544160226a9d231e7d95e10757b26e8`
- DEGENT.12: commit `7d03b4aa7631b6ada85ce36180aa2cb2d07deb6a7db138d48641f94ae09992a4` reveal `92fd95caccd78d8baff153354c8d0e61763e57914765ac344e5551c14ad720a5`

### DEGENT.x master table (2026-10-12, from bitcoind + gallery)

All thirteen subassets from bc1qznsu8jwtas…, each a ~3.95 MB jpeg. Reveal order. Fees are commit + reveal.

| Asset | Gallery/Counter # | Commit block | Commit time | Reveal block | Reveal time | Wait (blocks) | Size | Fees (sats) | v11.5.0 status | Commit tx | Reveal tx |
|---|---|---|---|---|---|---|---|---|---|---|---|
| DEGENT.0 | 204 | 967,378 | Sep 17 07:30 | 967,440 | Sep 17 16:35 | 62 | 3.95 MB | 994,379 | valid (Counter #204) | `3157874eb892…` | `6ee76abd8d26…` |
| DEGENT.1 | 216 | 967,380 | Sep 17 07:45 | 967,766 | Sep 20 00:39 | 386 | 3.95 MB | 993,322 | valid (Counter #216) | `d21054e96b51…` | `86fcf12d18ae…` |
| DEGENT.2 | 217 | 967,787 | Sep 20 03:03 | 967,926 | Sep 21 02:09 | 139 | 3.95 MB | 993,494 | valid (Counter #217) | `4d68308319ad…` | `6557c3f6a1aa…` |
| **Activation** `require_reveal_source_signature` | | | | **969,320** | **Sep 30 16:48** | | | | | | |
| DEGENT.3 | 533 | 969,488 | Oct 01 19:59 | 969,683 | Oct 03 05:51 | 195 | 3.95 MB | 1,004,164 | ignored (gallery #533) | `2ef8f20ac575…` | `7d640bf0a0d3…` |
| DEGENT.5 | 540 | 969,497 | Oct 01 22:34 | 969,919 | Oct 04 23:28 | 422 | 3.96 MB | 1,002,861 | ignored (gallery #540) | `35960a2ddbfa…` | `f34bf01e2fb0…` |
| DEGENT.4 | 541 | 969,488 | Oct 01 19:59 | 969,958 | Oct 05 04:59 | 470 | 3.95 MB | 1,002,550 | ignored (gallery #541) | `fcff198e110d…` | `ea72e53473ce…` |
| DEGENT.6 | 547 | 969,497 | Oct 01 22:34 | 970,195 | Oct 06 15:57 | 698 | 3.96 MB | 1,002,102 | ignored (gallery #547) | `7855cfdd4237…` | `b02f497d20ed…` |
| DEGENT.7 | 552 | 969,497 | Oct 01 22:34 | 970,578 | Oct 09 03:37 | 1,081 | 3.95 MB | 1,000,173 | ignored (gallery #552) | `4bd00f72a412…` | `c1730efbdfd1…` |
| DEGENT.8 | 554 | 969,497 | Oct 01 22:34 | 970,705 | Oct 10 01:29 | 1,208 | 3.96 MB | 1,000,098 | ignored (gallery #554) | `91ba64217c7d…` | `8383fd5a0f02…` |
| DEGENT.10 | 555 | 969,497 | Oct 01 22:34 | 970,765 | Oct 10 12:34 | 1,268 | 3.96 MB | 997,982 | ignored (gallery #555) | `1fcae0c9c489…` | `c63b570221c5…` |
| DEGENT.9 | 556 | 969,497 | Oct 01 22:34 | 970,842 | Oct 10 23:17 | 1,345 | 3.95 MB | 997,972 | ignored (gallery #556) | `2ebf1cd07213…` | `3d7a29c39d89…` |
| DEGENT.11 | 557 | 969,497 | Oct 01 22:34 | 970,843 | Oct 10 23:19 | 1,346 | 3.96 MB | 997,266 | ignored (gallery #557) | `75b11754da71…` | `24720cfeab13…` |
| DEGENT.12 | 558 | 969,497 | Oct 01 22:34 | 970,858 | Oct 11 01:57 | 1,361 | 3.96 MB | 996,569 | ignored (gallery #558) | `7d03b4aa7631…` | `92fd95caccd7…` |

Fees: 12,982,932 sats on all thirteen, 10,001,737 sats on the ten ignored ones. Longest commit→reveal wait: 1,361 blocks (DEGENT.12).

Full DEGENT txids:
- DEGENT.0: commit `3157874eb8927afc90fb25bd1f533ff4ff2c6314eba1fd995d9e238c3edd5eed` reveal `6ee76abd8d268c32b9e9fa2ae7e0faa7775ca38bd76223e6dd4c79008d73ed2a`
- DEGENT.1: commit `d21054e96b510bf1134b8f4cc0b5aaaa7238f19512c336ca2590c94e62c89a63` reveal `86fcf12d18ae860d6e5db08c3162ed12c010ab5e50498e7db3d7ce6932820817`
- DEGENT.2: commit `4d68308319added24213db48fa2da8da19d27d48ad5120eca46e344617cd0546` reveal `6557c3f6a1aa4778e62297328f353bcbf1a61568e85ece531fb4ec2a1d91b1d5`
- DEGENT.3: commit `2ef8f20ac575f2d4c4288c67f7f0fac6392af9836cdf07c83c597d08e9984247` reveal `7d640bf0a0d3a99042af72010f58c2d13d1a3e039fa08101d8bde6bca17dd93c`
- DEGENT.5: commit `35960a2ddbfa0faf89c81a6ea420d849ed0139941558d1206370bb8ae98356da` reveal `f34bf01e2fb093669f577bb3868c57d991ca4e1988fc1662436e0eb06c664201`
- DEGENT.4: commit `fcff198e110dc97cfb2e02d1c2bdc2cd2ba349d9a903856297bf1f8f156286fb` reveal `ea72e53473ce46f14fb2bcabc6d2c1bce888ec4219623daba1be6b8d771f746c`
- DEGENT.6: commit `7855cfdd4237ac032eca4f036d5a5cd7c8e824eebc79250ecc2eef48690b1eda` reveal `b02f497d20ed488a9427d941ba9695c5e10fd8473c8b206c0b6bb60c98cba0e6`
- DEGENT.7: commit `4bd00f72a412cf088256587e65c970ea97a1375d81101e5d8233c0f08fd9f604` reveal `c1730efbdfd1c783c2c0d2033b8d263a2df16acd637f45e56b697bd217eb8973`
- DEGENT.8: commit `91ba64217c7daa6249a84bc3bdbe2c4b9905db78d5e7bdd87a70559a92263d91` reveal `8383fd5a0f02b51ad9d31704ae8117e94de6ad19ba6d81118e19ddfb330912e1`
- DEGENT.10: commit `1fcae0c9c4891f9e6809526f2a742cd3a79fcaba5c8ccbc53e73c789dc8276ea` reveal `c63b570221c5ae05706aee479d175aee960098c4e7a1176470ecdd9a83b15000`
- DEGENT.9: commit `2ebf1cd0721324a07d2f2d5f74f2e1406eb03ee9485acaaec0ceeaf02ec6f570` reveal `3d7a29c39d89b1839f9e625d2ef3cedff79c04dedda97e0cb2eb6536926ae5ed`
- DEGENT.11: commit `75b11754da7115f40dbed4e179f42e82742da2bb38532d3ab356cb6cb8e4e078` reveal `24720cfeab130156fc32d095c2dd7b64d544160226a9d231e7d95e10757b26e8`
- DEGENT.12: commit `7d03b4aa7631b6ada85ce36180aa2cb2d07deb6a7db138d48641f94ae09992a4` reveal `92fd95caccd78d8baff153354c8d0e61763e57914765ac344e5551c14ad720a5`

Both tables as a standalone page: `ignored-reveals.html` in this folder.

## 9. The protocol change itself: `require_reveal_source_signature`

Sources: `~/counterparty-core` at tag v11.5.0 (merge 8aab989, 2026-09-30 13:34 UTC):
`counterpartycore/protocol_changes.json`, `lib/parser/gettxinfo.py`, `lib/utils/script.py`,
`counterparty-rs/src/reveal.rs`, `lib/config.py`, `release-notes/release-notes-v11.5.0.md`.
The GitHub advisory GHSA-q27c-r246-f6qw is still private (404 on github.com and the advisory API).

### protocol_changes.json entry

```json
"require_reveal_source_signature": {
  "minimum_version_major": 11, "minimum_version_minor": 5, "minimum_version_revision": 0,
  "block_index": 969320,            // mainnet
  "testnet3_block_index": 5151805,
  "testnet4_block_index": 154564,
  "signet_block_index": 324359      // regtest: block 0
}
```

Its `_comment` says the heights are where each chain was expected to be at 2026-09-30 15:00 UTC,
about two hours after release, "so operators have a window to upgrade". Mainnet block 969,320
was actually mined 16:48 UTC. The release notes say a node still on v11.4.0 at that block
diverges from the network.

Neighbouring activations for context: `taproot_support` 902,000 (v11.0, when reveals became
possible), `correct_transaction_fee` 966,200, and the next batch at **971,700**
(`mpma_taproot_support`, `ordinals_metadata_support`, `reject_non_finite_broadcast`,
`reject_negative_fee_fraction`) which is ~650 blocks after today's tip.

### The vulnerability (why)

A taproot-envelope message is a commit (P2TR output whose script tree holds the envelope leaf)
plus a reveal (spends it through the leaf). Before 11.5.0 the parser recognised a reveal by
**shape only**: an `OP_RETURN CNTRPRTY` output and a three-element witness on input 0. It then
attributed the message to whoever **funded the commit**, one hop back. Nothing tied that funder
to the envelope.

Attack: publish a P2TR address whose hidden leaf carries a `sweep` of all balances and asset
ownerships; get a victim to pay plain BTC to it; spend that payment in a reveal. Counterparty
attributes the sweep to the victim, who never signed anything Counterparty-related. A P2WSH
output with a three-element witness worked too, with no real signature at all. Confirmed on
regtest against v11.3.0; exposed on mainnet since block 902,000.

### The rule (what)

From the activation height a reveal is attributed to its commit funder only when **all four** hold
(`reveal.rs::check_reveal_source_signature`, pure function of three byte strings):

1. the output the reveal spends is **P2TR** (`OP_1 <32-byte key>`);
2. the witness is a **tapscript spend** of it: exactly 3 elements `<sig> <leaf> <control block>`,
   control block decodes, leaf version is `0xc0`, and the control block commits the leaf to
   the output key (unknown leaf versions would succeed without executing anything);
3. the leaf is a **canonical envelope** and nothing else:
   `OP_FALSE OP_IF <data pushes / OP_1NEGATE..OP_16 only> OP_ENDIF <32-byte key> OP_CHECKSIG`.
   Any other opcode inside the IF (even never-executed `OP_SUCCESSx`), a non-32-byte key, or
   trailing bytes after `OP_CHECKSIG` are rejected, because each could let the spend validate
   without `OP_CHECKSIG` actually running;
4. that 32-byte key **belongs to the source address** (`source_controls_key`):
   - P2TR source: the output key itself, or an internal key whose BIP86 tweak equals it;
   - P2WPKH: compressed key (either parity) hash160s to the witness program;
   - P2SH-P2WPKH: nested `0014<hash160(key)>` hash160s to the script hash;
   - P2PKH: compressed or uncompressed key hash160s to the pubkey hash;
   - anything else (bare multisig, P2WSH, other P2SH) never authorises a reveal.

Under 1–3 Bitcoin consensus guarantees a genuine Schnorr signature by the leaf key over this
transaction; under 4 that key is the source's, so the source provably signed the message.
The SIGHASH flag is checked separately (`check_signatures_sighash_flag`).

### Enforcement path (where)

- `gettxinfo.get_transaction_sources()` → for input 0, when not composing →
  `check_reveal_source_signature(decoded_tx, source_script_pubkey, block_index)`.
- That function returns early unless the tx is a reveal (`parsed_vouts[5]`) and the protocol
  change is enabled at `block_index`; otherwise fetches the commit output's scriptPubKey from
  bitcoind (`backend.bitcoind.get_reveal_commit_script_pubkey`) and calls the Rust check via
  `script.reveal_source_signature_error`.
- On failure it logs `Reveal transaction <txid> ignored: not signed by its source (<reason>)`
  (WARNING for blocks, DEBUG for mempool) and raises `RevealSourceSignatureError(DecodeError)`,
  so the tx is treated as **non-Counterparty**: no transaction row, no issuance row, nothing.
  This is why the fifteen leave no trace in the ledger and why the log is the only record.
- Only the Python parser enforces it; the Rust deserializer still detects reveals by shape.
  All fifteen on this node failed with reason `envelope key does not belong to the source
  address` (`SourceKeyMismatch`), i.e. the old throwaway-key flow.

### Upgrade mechanics

- `config.py` `UPGRADE_ACTIONS["mainnet"]["11.5.0"] = [("rollback", 969320)]`. For a node that
  upgraded before the height this is a no-op (rollback target above tip). For a node that
  stayed on 11.4.0 past it, it rolls back to 969,320 and re-parses (~30 min, API down).
- Not retroactive: every commit/reveal composed before 11.5.0 closed its envelope with a
  throwaway key, so the rule could not be applied to older blocks without erasing history.

### API / wallet side (the breaking change)

- `compose` with `encoding=taproot` no longer returns `signed_reveal_rawtransaction` (the node
  does not hold the source key). It returns `reveal_rawtransaction` (unsigned) plus
  `envelope_script`, `reveal_control_block`, `reveal_pubkey` (x-only key closing the envelope),
  `reveal_lock_scripts`, `reveal_inputs_values`.
- Wallet must add witness `<signature> <envelope_script> <reveal_control_block>` to the reveal,
  signing the leaf (BIP342 script path, `SIGHASH_DEFAULT` or `SIGHASH_ALL`) with the private key
  of `reveal_pubkey`, then broadcast commit then reveal.
- The envelope is closed by the source key: pass it as `multisig_pubkey` (compressed or x-only)
  or the node looks it up from the source's past transactions; a P2TR source with no known key
  falls back to its output key (wallet signs with BIP86-tweaked private key). `taproot` encoding
  now **requires a P2WPKH or P2TR source**. `compose` rejects a `multisig_pubkey` that is not the
  source's and cross-checks commit/envelope/reveal before returning.
- This is the change `counters/commands/inscribe.py` still needs.

### Is CRAZYDANK a counter? (asked 2026-10-12)

Yes, by the explorer's validity rules (`counters/server/static/docs.md` §Validity Rules):
1. valid issuance authored by its own message (`asset_events` = `creation lock_quantity`);
2. non-empty description (37,018 bytes, inline `data:application/json,…`);
3. carried in a taproot envelope: bitcoind shows `OP_RETURN 434e545250525459` (CNTRPRTY),
   a 3-element witness on input 0, a P2TR commit in the same block 970,988, and a leaf ending
   `<32-byte key> OP_CHECKSIG` with key `33da8880…bf5ad`. v11.5.0 accepted it, so the key is the
   source's.

Local index numbers it #544. counters.gallery `GET /block/970988` returns zero counters even
though its indexer reports height 971,049 and it has #560 from the same source address four
days later, so the gallery simply missed it (indexer gap, not a rule difference). Note the
source bc1p050y4c… is the wallet that lost RAREBLOCKS on Oct 7 (old flow) and succeeded with
CRAZYDANK on Oct 11 and PEPEMEMECOIN.S1N7 on Oct 11: that wallet was updated between Oct 7 and 11.

The other gallery/local gap, LP token A18189972090142917414 (#163 on the gallery), is excluded
locally by rule 1 (`pool_deposit_mint` is a derived row, not message-authored); the gallery runs
an older indexer that predates that rule.

### Why nothing warned on 1 Oct when the DEGENT.3–.12 commits were made (asked 2026-10-12)

Claim to test: "Core should have thrown a warning that it was out of date". The node was
Core v11.4.0 (Docker), and the inscriptions were made with `counters wallet inscribe`.

**What v11.4.0's version check actually does** (`lib/parser/check.py`, identical in v11.5.0):
- `software_version()` downloads `https://counterparty.io/protocol_changes.json` (301 → www;
  `requests` follows it) and runs `check_change()` on every entry.
- For an entry whose `minimum_version` is newer than the running node:
  - if `current_block_index >= activation block` → **raise `VersionUpdateRequiredError`**
    ("Your version is v11.4.0, but, as of block 969320, the minimum version is v11.5.0.
    Reason: require_reveal_source_signature. Please upgrade…");
  - otherwise → `logger.warning(...)` with the same text.
- Nobody catches that error (git grep: no handler outside check.py). At startup it aborts the
  server; inside the follower it kills the `handle()` task.
- **It runs only twice per process**: once in `server.py` at startup, once at the top of the
  follower's `handle()` before its loop. `check_software_version_if_needed()` has a 24-hour
  throttle but is called exactly once, so the "re-check daily" intent is dead code. A node that
  stays up never looks again.
- `--force` skips it entirely; a failed download raises `VersionCheckError` (also fatal
  without `--force`).
- The compose API (`lib/api/composer.py`) never calls it. In v11.4.0 compose still built the
  reveal itself with a throwaway key and returned `signed_reveal_rawtransaction`.

**Timeline**
- Sep 30 13:34 UTC: PR #3531 merged to master (one squash commit, 8aab989): the fix, the
  `protocol_changes.json` entry and the v11.5.0 tag. counterparty.io serves that file; its
  deploy lag is unknown.
- Sep 30 16:48 UTC: block 969,320, rule active.
- Oct 1 19:59 and 22:34 UTC: DEGENT.3/.4 then .5–.12 commits (blocks 969,488 / 969,497),
  composed by the v11.4.0 node, signed and broadcast by counters.
- Oct 3–11: the reveals went out one by one. v11.4.0 parsed each one as a **valid** issuance
  under the old rule, so the local node and local counters index showed DEGENT.3+ as real.
- Oct 6 23:26 local: svc created; Docker image switched to `counterparty/counterparty:v11.5.0`
  (services.toml.bak-docker). Oct 7 02:24 local: native v11.5.0. `UPGRADE_ACTIONS["11.5.0"]`
  = rollback to 969,320, re-parse; the "ignored: not signed by its source" warnings in
  `~/.cache/counterparty/log` are dated Oct 8 13:18–14:14 local (the re-parse) and later.

**So what happened on Oct 1.** The node was up and composing, so it had **not** hit
`VersionUpdateRequiredError`. Given the code, one of:
1. the v11.4.0 process had been running since before the remote file gained the entry
   (any time before Sep 30 ~13:30 UTC) and never re-checked, which is the designed behaviour;
2. it was (re)started after that but with `--force`, which silences the check; or
3. counterparty.io had not yet been updated when it last started.
No evidence survives to pick between them: the Docker container, image and its logs are gone
(`docker ps -a` / `docker images` show nothing; the surviving container log under
`~/.local/share/counterparty/.cache/counterparty/log` starts 2026-10-07 04:26 UTC on v11.5.0),
shell history has no counterparty docker commands, and the counters index was rebuilt from
2026-10-07 04:27 so its old DEGENT.3+ rows are gone too. Case 1 is the most likely for a node
that had been left running.

Even in case 1 there was **no warning path at all**: the only pre-activation warning Core emits
is the `logger.warning` in `check_change`, and that fires only during a version check, which
only happens at process start. There is no check at compose time, no check on each block, and
mempool/blocks parsed by an old node look valid to it.

**And counters?** `counters/commands/inscribe.py` calls compose with `encoding="taproot"`,
reads `signed_reveal_rawtransaction`, signs the commit and broadcasts both. It only reads
`/v2/` for `counterparty_height` (`counters/counterparty.py::status`) and never looks at
`version`. It could not know a rule change existed. On a v11.5.0 node the same code would
have failed loudly (no `signed_reveal_rawtransaction` in the response), which is the
"inscribe is broken until updated" item in the TODO list.

**Takeaways / follow-ups**
- [ ] counters: before composing, compare `/v2/` `version` against the newest
      `minimum_version` in the node's own `protocol_changes.json` (or the remote one) and
      refuse to inscribe on a node older than an already-activated change. Cheap, would have
      stopped all ten DEGENT commits.
- [ ] Core upstream: `check_software_version_if_needed()` should be called inside the follower
      loop (the 24 h throttle already exists) so a long-running node learns about a new
      activation; and compose could refuse when the node is below the minimum version for the
      current block. Worth an issue alongside the recursive-ZMQ-retry one.
- [ ] Watch block **971,700** (next activation batch: `mpma_taproot_support`,
      `ordinals_metadata_support`, …): the local node is v11.5.0 so it is fine, but the same
      "no re-check" behaviour applies to any node left on 11.5.0 past a future change.

## 11. Fix applied to counters and counters.fun (2026-10-12)

Verified first that both tools really had the gap:
- `counters/commands/inscribe.py` read `signed_reveal_rawtransaction` and only let Bitcoin Core
  sign the commit; no version check anywhere (`counters/counterparty.py` only reads the height).
- counters.fun `apps/web/src/lib/inscribe/mint.ts` also required `signed_reveal_rawtransaction`
  (so it breaks against any v11.5.0 node) and, for XCP Wallet + native envelope
  (`signsOrdEnvelopeOnly`), put a **random page-held key** in the leaf → ignored under 11.5.0.
  Decoding the witnesses confirmed which tool made what: RAREBLOCKS and PEPEMAXXER carry the
  NUMS internal key + 65-byte sig (counters.fun self-signed path); NINESIX and all DEGENT.x carry
  Core's ephemeral key (CLI path). CRAZYDANK / S1N7 are valid because their leaf key is the
  source's BIP86 internal key.

Why the CLI must sign the reveal itself: Bitcoin Core's tapscript signer
(`SignTaprootScript`) only satisfies `<key> OP_CHECKSIG`, multi_a and miniscript leaves; the
envelope `OP_FALSE OP_IF … OP_ENDIF <key> OP_CHECKSIG` is none of those, so `walletprocesspsbt`
leaves it unsigned (checked on regtest and in the source). Same reason @scure/btc-signer needs
the app's own `finalize()` on the web side.

### counters (CLI), ~/counters/counters
- `counters/tap.py`: BIP340 `schnorr_sign`/`schnorr_verify`, `tapleaf_hash`, `compact_size`,
  `xonly_from_secret` (pure Python, BIP340 vectors in tests).
- `counters/bip32.py`: `node_from_xprv`, `parse_path`, `derive`, `wif_to_secret`,
  `pubkey_from_secret` (read keys back out of Core's descriptors).
- `counters/revealsign.py` (new): `source_pubkey()` (getaddressinfo: `pubkey` for wpkh,
  tr([..]KEY) internal key for bc1p), `export_secret()` (`listdescriptors true` → derive the
  address's hdkeypath from the account xprv, or decode a bare WIF; must reproduce the pubkey),
  BIP341 `taproot_sighash` (all hash types; Core's bip341 vectors pass), `sign_reveal()`
  (SIGHASH_DEFAULT, 64-byte sig, checks envelope/key/control block before signing), and
  `reveal_source_error()`, a Python mirror of `reveal.rs` run on the finished reveal.
- `counters/nodecheck.py` (new): refuses to compose when the node is below `MIN_CORE_VERSION`
  (11.5.0) or below the minimum version of any change in counterparty.io's
  protocol_changes.json that is active or activates within 4,320 blocks. Runs before any
  funding moves.
- `counters/counterparty.py`: `compose_issuance(multisig_pubkey=…)`.
- `counters/commands/inscribe.py`: node check → look up the source key → compose with it →
  require the v11.5.0 fields (refuse `signed_reveal_rawtransaction`, refuse a `reveal_pubkey`
  that is not the source's) → Core signs the commit → export secret, sign reveal, mirror-rule
  check → existing testmempoolaccept / slipstream / broadcast flow unchanged. Docstrings and
  `cancel.py` / README no longer claim the reveal is signed by an ephemeral key.
- Tests: `tests/test_revealsign.py`, `tests/test_nodecheck.py` (41 new); full suite 538 pass.
- Verification beyond unit tests: regtest end-to-end with the real bitcoind (export key from a
  descriptor wallet, sign a Core-style `P2TR(source,[envelope])` reveal, `testmempoolaccept`
  allowed, for bc1p and bc1q sources); mainnet `--dry-run` with wallet `counts` against the
  local v11.5.0 node: compose with source key, commit + reveal both `allowed` at 2 sat/vB.
  (At the default 0.99 sat/vB the local node rejects with "152 < 153": its minrelaytxfee is the
  29.x default 1 sat/vB; sub-1 relay needs `minrelaytxfee`/`incrementalrelayfee` lowered.)

### counters.fun, ~/counters/counters.fun
- `lib/inscribe/node-version.ts` (new): `nodeVersionProblem()`, `MIN_CORE_VERSION`,
  `OutdatedNodeError`.
- `app/api/cp/[...path]/route.ts`: every compose POST first checks the node's `/v2/` version
  (cached 60 s) and answers 503 `compose refused: …` for a node older than 11.5.0.
- `lib/inscribe/psbt.ts`: `ComposeResult` gains the v11.5.0 fields; `coreReveal()` reads
  `reveal_rawtransaction` and throws `OutdatedNodeError` on the legacy shape;
  `revealWeightFor()` sizes the signed reveal exactly from the unsigned template (base×4 + 2 +
  witness); `coreCommitScript()` takes the internal key from `reveal_control_block`;
  `commitTopUp`/`buildRevealPsbt` use the template.
- `lib/inscribe/mint.ts`: passes `multisig_pubkey = sourceXOnly` to compose; checks
  `reveal_pubkey` equals the wallet's key; **removes the self-signed random key** and throws
  `WalletCannotSignNativeRevealError` instead; `finishReveal` refuses a pending mint that
  stored a page key (it would be ignored). `mint-form.tsx` greys the button for that combination
  and the copy says to pick the ord envelope.
- Tests: `tests/reveal-source-signature.test.ts` (exact weight vs a really signed reveal,
  refusals, version gate). Related suites pass; `tsc --noEmit` passes. `npm run check` then
  fails on eslint because the repo has no eslint.config (pre-existing, unrelated).

Not done / follow-ups:
- [ ] Rebuild-and-re-sign a stranded reveal from the source key (now possible in principle).
- [ ] counters.fun: production node behind `COUNTERPARTY_API_BASE` must be ≥ 11.5.0 or the
      proxy now refuses every compose (by design).
- [ ] Commit both repos (nothing committed yet). `counters/ledger.py` carries the older
      uncommitted WAL patch, not part of this change.

## 10. Log

- 2026-10-12: created this file; recovered tables from Oct 8/11 sessions; gallery-vs-local diff done; tx_index proof shows the 15 are the complete tokenscan/gallery-only set at block 971,049; master table with commit/reveal blocks (§8); protocol change write-up (§9); CRAZYDANK confirmed a counter; DEGENT.x table added; both tables published as an artifact page; investigated why v11.4.0 gave no warning on Oct 1 (§9); updated counters CLI and counters.fun mint for v11.5.0 reveal signing + node version gate (§11).
