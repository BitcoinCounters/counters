# XCP Wallet & transactions — field guide

How to compose, sign, and broadcast Counterparty transactions through the XCP Wallet
extension (`window.xcpwallet`), distilled from the mint-app, counters.fun, and the
RARE.PEPE dispenser app. Every rule here was learned by hitting its failure mode.

## The provider

Injected at `document_start` as `window.xcpwallet`, EIP-1193 style:
`request({ method, params }) → Promise`, plus `on`/`removeListener`. A page that loads
late should listen for `xcp-wallet#initialized` and fire `xcp-wallet#discover` to ask
for re-announcement (see `mint-app/lib/provider.ts::detectProvider`).

### Methods and their REAL return shapes

| method | returns | trap |
|---|---|---|
| `xcp_accounts` | `string[]` (empty when locked/not connected) | silent — no popup |
| `xcp_requestAccounts` | **`{ accounts: string[], proof }`** — an object! | treating it like the `xcp_accounts` array prints `[object Object]` |
| `xcp_signPsbt` | `{ hex }` — a **signed PSBT**, *not* a broadcastable tx | key is **`hex`**, not `psbt` |
| `xcp_broadcastTransaction` | `{ txid }` | wants a **finalized raw tx**, never a PSBT |
| `xcp_signMessage` | `{ signature }` or raw string | handle both |
| `xcp_disconnect` | `boolean` | |

Events: `accountsChanged` (string[]), `disconnect`. Normalize account extraction in one
helper that accepts array, `{accounts}`, and `{address}` items. Re-query `xcp_accounts`
immediately before composing anything — never trust a cached address.

Error codes (`err.code`): 4001 user rejected · 4100 locked/unauthorized ·
4200 unsupported · 4900 unavailable. Branch on codes, never message text.
**Caveat**: the wallet also reports some of its own refusals as "User cancelled" —
see the symptom table.

## The PSBT contract

The wallet will only sign a PSBT that fully explains itself. Counterparty Core's
`compose/*?return_psbt=true` output does **not** — its input maps are empty.
Requirements per input:

1. **`witness_utxo`** — prevout script + amount. Core's compose gives you both as
   `lock_scripts[i]` and `inputs_values[i]`; rebuild the PSBT (scure:
   `buildPlainPsbt`, see `counters.fun/apps/web/src/lib/inscribe/psbt.ts`) or patch
   Core's PSBT with the records. Missing → generic `Request failed`.
2. **`sighashType: SIGHASH_ALL`** — *the only flag XCP Wallet accepts.* Missing →
   the wallet refuses and reports it as a **user cancel**.
3. **`signInputs: { [address]: [0, 1, …] }`** in the signPsbt params — every input
   index the wallet should sign.
4. For inscription **commits** only: the `inscription: { revealScript, tapInternalKey }`
   context. It turns "BTC leaving to an unexplained address" into a described
   "Inscription Commit" the wallet verifies field-by-field and will render.
   Mutually exclusive with plain-payment intent signing.

After signing: **finalize before broadcasting.** The wallet deliberately returns an
unfinalized signed PSBT. `Transaction.fromPSBT(hex).finalize(); tx.extract()` (scure),
or for a taproot key-path input: take the `tap_key_sig` (PSBT input key `0x13`, or a
ready `final_scriptwitness`, `0x08`) and build the raw segwit tx yourself. Broadcasting
the signed PSBT raw → `Request failed`.

Base64 vs hex: Core emits base64 PSBTs (`cHNidP8…`); the wallet speaks hex
(`70736274ff…`). Convert.

## Composing against Core — the traps

- `return_psbt=true` + `verbose=true`; use `exact_fee=0` and re-fee the rebuilt tx
  yourself, or accept integer `sat_per_vbyte`.
- **Never let Core pick coins on an address that holds inscriptions or token-bearing
  postage** (330/546/1000-sat coins). Pass `inputs_set` explicitly, or at minimum
  refuse to sign when any `inputs_values[i]` is dust-sized. `exclude_utxos_with_balances=true`
  guards Counterparty balances but knows nothing about ord inscriptions.
- `inputs_set` short form (`txid:vout`) requires the coin to be visible on-network;
  the 4-part form (`txid:vout:value:script_pub_key`) works for unbroadcast chains.
- Multisig-encoded data is ARC4-keyed to the **first input's txid**: never swap
  inputs after composing. Appending extra inputs after input 0 is fine.
- A signed tx's txid must match the pre-signing computation — check it
  (`unsignedRevealTxid`) to catch a wallet that altered more than the witness.

## Symptom → cause

| symptom | actual cause |
|---|---|
| `connected [object Object]` | `xcp_requestAccounts` returns `{accounts,proof}`, not an array |
| `Request failed` at signing | PSBT inputs missing `witness_utxo` |
| "User cancelled" nobody cancelled | missing `SIGHASH_ALL`; or Chrome closed the popup on focus loss |
| `Request failed` at broadcast | you broadcast a signed PSBT instead of a finalized raw tx |
| works on your machine, fails for others | they're on a cached page — serve `Cache-Control: no-store` |
| wallet shows a scary raw-BTC warning on a commit | missing `inscription` context |

## Reference implementations

- `RARE/mint-app/lib/provider.ts` — provider wrapper, error codes, detection race
- `RARE/mint-app/lib/launch.ts` — `buildPlainPsbt`, `runPlainMessage`, commit/reveal with inscription context
- `counters/counters.fun/apps/web/src/lib/inscribe/psbt.ts` — `buildPlainPsbt`, `finalize`, local reveal signing
- `RARE/RARE/RARE.PEPE/editions/dispenser-app/index.html` — the no-build-tools version: raw PSBT
  enrichment (`enrichPsbt`) and finalization (`finalizePsbt`) in ~90 lines of vanilla JS,
  each byte-verified against `bitcoin-cli decodepsbt` / scure's `finalize()`
