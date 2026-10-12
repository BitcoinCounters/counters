# Tables: Counterparty v11.5.0 hardfork

_Saved 2026-10-12. Source data: bitcoind, local Core v11.5.0 API, tokenscan.io, counters.gallery._

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

Shareable page with both tables (private until shared): https://claude.ai/artifact/JJPKNrhAwzrXZ4oJBevshQ

