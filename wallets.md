# Wallets & seed-phrase support

How `counters wallet` turns a seed phrase into addresses, which wallet types it
can import today, and which are candidates. **Keys are always held by Bitcoin
Core** — this tool derives the keys/descriptors, hands them to Core, and never
signs itself (see `counters/bip32.py` and `counters/electrum1.py`).

Restore is automatic: `counters wallet --name <name> restore` reads the phrase
from stdin and **detects the seed type** (see [Detection](#how-detection-works)).

## At a glance

| Wallet(s) | Words | Seed standard | Derivation | Address | Status |
| --- | --- | --- | --- | --- | --- |
| **This tool** (`create`) | 12 | BIP39 + BIP86 | `m/86'/0'/0'` | `bc1p…` taproot | ✅ default |
| **Any BIP39 taproot wallet** | 12/24 | BIP39 + BIP86 | `m/86'/0'/0'` | `bc1p…` taproot | ✅ |
| **Counterwallet / Freewallet / Rare Pepe Wallet** | 12 | Electrum v1 | flat `(chain, n)` sequence | `1…` legacy P2PKH (uncompressed) | ✅ |
| BIP39 legacy (BIP44) | 12/24 | BIP39 + BIP44 | `m/44'/0'/0'` | `1…` P2PKH (compressed) | ⛔ not yet |
| BIP39 nested SegWit (BIP49) | 12/24 | BIP39 + BIP49 | `m/49'/0'/0'` | `3…` P2SH-P2WPKH | ⛔ not yet |
| BIP39 native SegWit (BIP84) | 12/24 | BIP39 + BIP84 | `m/84'/0'/0'` | `bc1q…` P2WPKH | ⛔ not yet |
| Electrum 2.x (standard/segwit) | 12–13 | Electrum v2 | `m/0` | `1…` or `bc1q…` | ⛔ not yet |

> **Why this matters for Counterparty.** Counterparty assets can live on *any*
> address type, but a great deal of historical activity sits on **legacy `1…`**
> (Counterwallet) and BIP44/Electrum wallets. Restoring a BIP39 seed to taproot
> only will show an *empty* wallet if the assets are actually on that seed's
> legacy/SegWit paths. Pick the path that matches where your coins are.

---

## Supported today

### 1. BIP39 → taproot (the default)

`create` generates a 12-word **BIP39** mnemonic; `restore` re-imports one. We
derive the **BIP86** account `m/86'/0'/0'` and import Core `tr()` descriptors for
the receive (`/0/*`) and change (`/1/*`) chains, so Core owns/derives `bc1p…`
taproot addresses.

```bash
counters wallet --name me create      # prints a 12-word seed ONCE
counters wallet --name me restore     # paste a BIP39 seed on stdin → rescan
```

- **Backup:** the 12/24-word phrase. Any BIP86 wallet regenerates the same
  taproot addresses.
- **Compatible with:** any wallet that follows BIP39 + BIP86 (Sparrow, recent
  Electrum taproot, hardware wallets set to taproot, etc.).

### 2. Counterwallet / Freewallet (Electrum v1)

Old Counterparty wallets predate BIP39/BIP32. They use the **Electrum v1**
scheme, implemented in `counters/electrum1.py`:

1. a 12-word mnemonic over a **1626-word list** (`electrum1_words.txt`) encodes a
   128-bit hex seed (`mn_decode`);
2. the ASCII of that hex seed is key-stretched (100 000 rounds of SHA-256) into a
   master secret; the master *public* key is its uncompressed point (64 bytes);
3. the key for address `(chain, n)` is `(master_secret + H) mod order`, where
   `H = sha256d("n:chain:" + mpk_bytes)`;
4. addresses are legacy, **uncompressed** `1…` P2PKH.

We derive the first `N` keys of both chains and import them into Core as
single-key `pkh(WIF)` descriptors, so Core holds and signs them.

```bash
counters wallet --name old restore --dry-run          # preview 1… addresses; imports nothing, no node needed
counters wallet --name old restore                    # import legacy keys + rescan
counters wallet --name old restore --addresses 100    # derive 100 per chain (default 20)
counters wallet --name old restore --counterwallet    # force this path (see Detection)
```

- **Verified** against Electrum's own vector: seed
  `powerful random nobody notice nothing important anyway look away hidden message over`
  → mpk `e9d4b786…c442b3` and first address
  `1FJEEB8ihPMbzs2SkLmr37dHyRFzakqUmo`.
- **Same phrase, same addresses across:** Counterwallet, Freewallet, Rare Pepe
  Wallet, and other classic Counterwallet-compatible wallets.
- These are **not** taproot; to use them with the rest of this (taproot-oriented)
  tool, `send` the assets to a fresh `bc1p…` address made with `create`.

---

## Not yet supported (candidates)

These are all feasible with the primitives we already have (`bip32.py` does BIP32
and secp256k1; `tap.py` does bech32/bech32m). Each needs a derivation path and/or
a descriptor template plus a rescan import — the same shape as the two paths above.

### BIP39 with a chosen account (BIP44 / BIP49 / BIP84)

Same BIP39 seed, different account + script type. This is the biggest gap for
Counterparty users, because most non-taproot holdings are here:

| Standard | Path | Core descriptor | Address |
| --- | --- | --- | --- |
| BIP44 | `m/44'/0'/0'` | `pkh([fp/44h/0h/0h]xprv/{0,1}/*)` | `1…` |
| BIP49 | `m/49'/0'/0'` | `sh(wpkh(…))` | `3…` |
| BIP84 | `m/84'/0'/0'` | `wpkh(…)` | `bc1q…` |

A natural interface: `restore --account legacy|nested|segwit|taproot` (default
`taproot`), or import several at once and let the rescan find whichever holds
funds.

### Electrum 2.x seeds

Electrum's post-2.0 "standard"/"segwit" seeds are **not** BIP39: the words carry
a version prefix and the binary seed is `PBKDF2-HMAC-SHA512(phrase, "electrum")`,
then a BIP32 root with Electrum's own path (`m/0`). Detectable via the version
prefix (`is_new_seed`). Distinct from both paths above.

---

## How detection works

`restore` routes without a flag:

1. **Valid BIP39 checksum** → taproot restore (BIP39 phrases carry a checksum).
2. **Not BIP39, but every word is in the Electrum-v1 1626-word list** →
   Counterwallet restore.
3. **Neither** → a diagnostic error (typo, wrong word count, or unknown word).

`--counterwallet` forces path 2 for the rare phrase that is valid under *both*
schemes. We deliberately do **not** brute-force by scanning the chain for
history (accurate but slow); the checksum + word-list test is what Electrum
itself uses and is sufficient in practice.

## Notes

- **Seeds are read from stdin**, locally, and never leave your machine.
- Restores that import into Core trigger a **full rescan** (`timestamp=0`); this
  can take several minutes. Use Counterwallet `--dry-run` to preview addresses
  first without importing or rescanning.
- `--addresses N` applies to the Counterwallet path (flat sequence, no gap-limit
  scanning); BIP39 taproot uses Core's ranged descriptors with a 1000-address
  keypool per chain.
