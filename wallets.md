# Wallets & seed-phrase support

How `counters wallet` turns a seed phrase into addresses, which wallet types it
can import today, and which are candidates. **Keys are always held by Bitcoin
Core** — this tool derives the keys/descriptors, hands them to Core, and never
signs itself (see `counters/bip32.py`, `counters/electrum1.py`, `counters/electrum2.py`).

Restore is automatic: `counters wallet --name <name> restore` reads the phrase
from stdin and **detects the seed type** (see [Detection](#how-detection-works)).

## At a glance

| Wallet(s) | Words | Seed standard | Derivation | Address | Status |
| --- | --- | --- | --- | --- | --- |
| **Any BIP39 wallet** (incl. this tool's `create`) | 12/24 | BIP39 | BIP44/49/84/86 — **all imported** | `1…`, `3…`, `bc1q…`, `bc1p…` | ✅ |
| **Counterwallet / Freewallet / Rare Pepe Wallet** | 12 | Electrum v1 | flat `(chain, n)` sequence | `1…` legacy P2PKH (uncompressed) | ✅ |
| **Electrum 2.x** (standard) | 12 | Electrum v2 | `m` | `1…` P2PKH | ✅ |
| **Electrum 2.x** (segwit) | 12 | Electrum v2 | `m/0'` | `bc1q…` P2WPKH | ✅ |
| Electrum 2.x 2FA / multisig seeds | 12–24 | Electrum v2 | varies | varies | ⛔ not yet |

> **Why this matters for Counterparty.** Counterparty assets can live on *any*
> address type, and a great deal of historical activity sits on **legacy `1…`**
> (Counterwallet, BIP44, Electrum). So a BIP39 restore imports **all four**
> standard accounts at once (legacy/nested/segwit/taproot) and lets the rescan
> find funds wherever they are — no need to know which path your old wallet used.

---

## Supported today

### 1. BIP39 — all standard accounts

`create` generates a 12-word **BIP39** mnemonic (new wallets receive to `bc1p…`
taproot by default); `restore` re-imports one. For a restore we import **all four**
standard accounts into one Core descriptor wallet, so a single rescan finds funds
under any of them (Core allows one active descriptor per output type):

| Account | Path | Core descriptor | Address |
| --- | --- | --- | --- |
| legacy (BIP44) | `m/44'/0'/0'` | `pkh(…/{0,1}/*)` | `1…` |
| nested (BIP49) | `m/49'/0'/0'` | `sh(wpkh(…))` | `3…` |
| segwit (BIP84) | `m/84'/0'/0'` | `wpkh(…)` | `bc1q…` |
| taproot (BIP86) | `m/86'/0'/0'` | `tr(…)` | `bc1p…` |

```bash
counters wallet --name me create      # prints a 12-word seed ONCE
counters wallet --name me restore --dry-run   # preview one address per account type (offline)
counters wallet --name me restore             # import all four accounts → rescan
```

- **Backup:** the 12/24-word phrase. Any BIP39 wallet regenerates the same
  addresses for the matching account.
- **Verified** (canonical `abandon abandon … about` seed): legacy
  `1LqBGSKuX5yYUonjxT5qGfpUsXKYYWeabA`, nested `37VucYSaXLCAsxYyAPfbSi9eh4iEcbShgf`,
  segwit `bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu`, taproot
  `bc1p5cyxnuxmeuwuvkwfem96lqzszd02n6xdcjrs20cac6yqjjwudpxqkedrcr` (BIP86 spec).

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

### 3. Electrum 2.x (standard & segwit)

Electrum's post-2.0 seeds are **not** BIP39 (`counters/electrum2.py`): the words
carry a version number in a hash prefix, and the binary seed is
`PBKDF2-HMAC-SHA512(phrase, "electrum"+passphrase, 2048)`. A BIP32 root is then
derived with Electrum's own path + script type, addresses at `<node>/0/i` and
`<node>/1/i`:

- **standard** (prefix `01`) → path `m`, **p2pkh** `1…`
- **segwit** (prefix `100`) → path `m/0'`, **p2wpkh** `bc1q…`

We import the keys as single-key `pkh(WIF)` / `wpkh(WIF)` descriptors.

- **Verified** against Electrum's own vectors, e.g. standard seed
  `cycle rocket west magnet parrot shuffle foot correct salt library feed song`
  → `1NNkttn1YvVGdqBW4PR6zvc3Zx3H5owKRf`, and segwit seed
  `bitter grass shiver impose acquire brush forget axis eager alone wine silver`
  → `bc1q3g5tmkmlvxryhh843v4dz026avatc0zzr6h3af`.
- **2FA and multisig** Electrum seeds are **not** supported yet.

---

## Not yet supported (candidates)

- **Electrum 2.x 2FA seeds** (version prefix `101`/`102`) and **multisig**
  wallets — different derivation/script; detectable but unhandled.
- **Hardware-wallet BIP39 passphrases** (the optional 25th word) — restore
  currently assumes an empty passphrase.

---

## How detection works

`restore` routes without a flag:

1. **Valid BIP39 checksum** → BIP39 restore (imports all four accounts).
2. **Not BIP39, but every word is in the Electrum-v1 1626-word list** →
   Counterwallet restore.
3. **Not BIP39, but the Electrum seed-version hash prefix matches** (`01`/`100`)
   → Electrum 2.x restore.
4. **None of the above** → a diagnostic error (typo, wrong word count, unknown word).

`--counterwallet` forces path 2 for the rare phrase that is valid under multiple
schemes. We deliberately do **not** brute-force by scanning the chain for
history (accurate but slow); the checksum + word-list + version-prefix tests are
what Electrum itself uses and are sufficient in practice.

## Notes

- **Seeds are read from stdin**, locally, and never leave your machine.
- Restores that import into Core trigger a **full rescan** (`timestamp=0`); this
  can take several minutes. Use Counterwallet `--dry-run` to preview addresses
  first without importing or rescanning.
- `--addresses N` applies to the flat-sequence imports (Counterwallet, Electrum);
  BIP39 uses Core's ranged descriptors with a 1000-address keypool per chain.
- **`--dry-run` works for every seed type** and needs no node: it derives and
  prints the addresses (one per account type for BIP39; the `1…`/`bc1q…` list
  for the flat schemes) and imports nothing.
