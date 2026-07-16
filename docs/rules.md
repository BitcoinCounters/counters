# Display rules

**Status: draft for discussion — audited against counters #0–#86 on 2026-07-16.**

This document specifies what the explorer *shows* for each counter. It is a
pure display layer: nothing here ever changes validity (R1–R4), numbering
(N1–N6), canonical content bytes (§5.1), content hashes, or the rolling
consensus hash. Stored blobs are never rewritten. See
`build-reference-v3.md` §5 for the consensus-side derivation these rules sit
on top of.

## The prime directive

> **If the artifact's bytes are on chain, render the artifact.**
> **If only a pointer is on chain, render the pointer — never fetch what it points to.**

Everything below is a consequence of this. In particular: packaging
(`STAMP:` wrappers), wrong or generic declared MIME types, and repairable
encoding damage are never a reason to hide an on-chain file behind raw text
or a download link.

## Rules

### D1 — Display is not consensus

Every rule in this file operates at serve time only. No DB migration, no
reindex, no effect on `content_sha256` or the rolling hash. A change to
these rules must never require re-numbering.

### D2 — Render by artifact kind

On-chain artifacts render with the native element for what they *are*:
images as `<img>`, audio as `<audio controls>`, video as `<video>`, PDF in
an iframe, fonts as a specimen, HTML/SVG as a live document (see D7). Text
— including JSON, code, markdown — is an artifact too, and renders verbatim
(D9). The "unknown → Download link" page is a last resort, reached only
when D3's sniffing also comes up empty.

### D3 — Bytes beat the declared MIME type

The mint-time `mime_type` is a hint supplied by the minter; the bytes are
the truth. Sniff magic numbers (GIF, PNG, JPEG, WebP, Ogg, …) and let the
sniffed type drive display when:

* the declared type is generic (`application/octet-stream`) or unknown, **or**
* the declared type contradicts the sniffed magic.

The declared type still drives display when the bytes carry no recognized
magic (text, HTML, JSON have none). The API keeps reporting the declared
`content_type` as-is; the sniffed type is display metadata alongside it.

*Why:* #45 is byte-identical to #46 (Ogg Opus audio) but declared
`application/octet-stream` — it must play, not offer a download. #51 is
declared `image/jpeg` but is Ogg Opus audio — a broken `<img>` is wrong;
a player is right. #3 is declared `image/gif` but is a PNG (renders either
way; the sniffed type is still the honest label).

### D4 — `STAMP:` is packaging, not content

A textual body of the form `STAMP:<base64>` whose payload decodes to a
recognized image **is an on-chain image** and renders as one — in the grid,
in `/preview/<n>`, everywhere an image would show. That other indexers file
these as (cursed) Bitcoin Stamps is irrelevant here: the bytes are in our
envelope, on chain, so we show them. `/content/<n>` stays the raw text
(canonical bytes, D1); `/stamp/<n>` serves the decoded image.

### D5 — Deterministic base64 repair ladder

Mints in the wild carry transport damage in their base64. Repair is
attempted in a fixed order, entirely at serve time, and every step is
deterministic — same input, same output, on every indexer:

1. **Space is a mangled `+`.** Replace `' '` → `'+'`, then strip CR/LF/TAB.
   Form/URL encoding turns `+` into a space; *stripping* spaces instead
   deletes real payload characters and silently corrupts the image.
2. **Pad** to a multiple of 4 with `=`.
3. **Decode**, then require a recognized image magic (D3's sniffer).
4. **Stray-prefix rescue.** If step 3 finds no magic, scan the base64 text
   for a known base64-encoded image signature (`iVBORw0KGgo` PNG,
   `R0lGODlh`/`R0lGODdh` GIF, `/9j/` JPEG, `UklGR` WebP) and retry the
   decode from the **first** such signature. A `=` can only legally appear
   as terminal padding, so text before a mid-string `=` is corruption, not
   payload.
5. **Fail closed.** If no step yields a recognized image, fall back to raw
   text (D12). Repair never touches the stored blob or any hash.

*Why:* #54 MAGICEGG has `+` → space damage; step 1 recovers the exact image
minted as #57/#58 (sha-verified), while whitespace-stripping yields a PNG
with a bad PLTE CRC. #59 XCPFTW is #60's body with a stray leading
`iVBOR=`; step 4 recovers the identical, CRC-valid PNG.

### D6 — Pointers render as pointers

A body that is a single URI token (`ipfs:`, `ipfs://`, `ar://`,
`http(s)://`) is **not** an on-chain file — only the reference is on chain.
Display the pointer text verbatim with the pointer badge. The explorer
**never** dereferences it: no gateway fetches, no remote thumbnails, no
caching of off-chain bytes. What the pointer resolves to can change or
disappear; the counter's on-chain truth is the pointer string itself.

### D7 — Active content runs sandboxed

`text/html` and `image/svg+xml` are live documents and render as such —
inside a sandboxed iframe (`sandbox=allow-scripts`, opaque origin,
confining CSP), exactly the ord model. Being scriptable is not a reason to
show source instead of the artifact; being untrusted is a reason to
sandbox.

### D8 — Recursive references resolve locally or not at all

HTML counters may reference other on-chain resources (e.g. #6 pulls
`/content/<64-hex>i<n>`, an ordinals-style inscription id for a JS
library). Same-server paths resolve against this explorer only. An
ordinals-id reference 404s unless an optional read-only proxy to a local
`ord` server is configured — the dependency *is* on chain, just outside the
counters corpus, so proxying it is legitimate; fetching it from a public
gateway is not (D6). Until such a proxy exists, the document still renders;
its missing dependency is the minter's recursion bet, not a display bug.

### D9 — Text is shown verbatim

Raw canonical bytes, escaped, in full. No percent-decoding (#24/#26 render
as `This%20is%20my%20numeric%20asset`), no trimming, no linkifying, no
"looks like hex, let's decode it" (#28/#29 show the literal text `620a`).
The bytes are the artifact.

### D10 — Duplicates render independently

Identical content under multiple numbers (#19/#20, #25/#27, #48/#49,
#57/#58, #59/#60) is not deduplicated in display. Every counter shows its
own content; sameness is discoverable via `content_sha256`.

### D11 — Cache content forever, derived views briefly

`/content/<n>` bytes are on-chain and content-addressed — long/immutable
caching is fine (away from the reorg window). But `/preview/<n>` and
`/stamp/<n>` are **functions of the display code**: every rule in this file
changes their output. They must be served with short-lived caching (or a
cache key that includes the build commit), never `immutable`. *Why:* the
stamp feature shipped while browsers held year-long immutable copies of the
old text previews — #84 kept showing raw `STAMP:` text after the server was
already serving the image.

### D12 — Fail closed to raw text

Content that claims to be a file but can't be recovered by D3+D5 renders as
raw text (or the unknown/download page for undecodable binary), ideally
with a "claims X, doesn't decode" note. Never render a known-broken
artifact as if it were fine.

## Counter-by-counter audit (#0–#86)

Class key — **img**: native on-chain image · **audio**: native on-chain
audio · **html**: on-chain active document · **text**: on-chain text
artifact · **stamp**: `STAMP:`-wrapped on-chain image · **ptr**: off-chain
pointer.

Status — **OK**: current behavior already matches the rules ·
**FIX**: current behavior violates the rules (see gaps below).

| # | Asset | Declared type | Class | Rule | Should show | Status |
|---|-------|---------------|-------|------|-------------|--------|
| 0 | XDUALS | text/plain | text | D9 | text `testdual` | OK |
| 1 | DUALNAKA | image/gif | img | D2 | GIF image | OK |
| 2 | DUALNAKA | image/gif | img | D2 | GIF image | OK |
| 3 | DUALNAKA | image/gif | img | D2, D3 | image — bytes are **PNG**, sniffed type labels it | OK (label: FIX) |
| 4 | DUALPEPE | image/gif | img | D2 | GIF image (native envelope GIF — invisible to stamps indexers) | OK |
| 5 | A16364538941481093000 | text/html | html | D7 | live HTML in sandbox | OK |
| 6 | A13174135439704705000 | text/html | html | D7, D8 | live HTML; JS-library dependency is an ordinals id → 404 until ord proxy | OK |
| 7–11 | A826…/A937…/A255…/A150…/A474… | text/plain | ptr | D6 | `ipfs:` pointer text, never fetched | OK |
| 12 | A17996848539542727000 | text/html | html | D7 | live HTML in sandbox | OK |
| 13 | A4727332526279217000 | text/html | html | D7 | live HTML — embeds its image as an inline data-URI, fully on chain | OK |
| 14 | LINKMISSING | text/plain | ptr | D6 | pointer text | OK |
| 15 | A12323633654417476000 | application/json | text | D9 | JSON, code view | OK |
| 16 | FADEDNJADED | text/plain | ptr | D6 | pointer text | OK |
| 17 | LIBBITCOIN | text/plain | ptr | D6 | pointer text | OK |
| 18 | A987654321098765432 | text/plain | text | D9 | text | OK |
| 19 | A987654321098765432 | text/plain | text | D9, D10 | text (same body as #20) | OK |
| 20 | DERPDERP | text/plain | text | D9, D10 | text | OK |
| 21 | DEGENORDINAL | text/plain | text | D9 | text `Ordinal degen.jpeg` — text *about* a file is still text | OK |
| 22 | FAIRBITCOIN | text/plain | ptr | D6 | pointer text | OK |
| 23 | XCPFORTWENY | text/plain | ptr | D6 | pointer text | OK |
| 24 | A106242056811263620 | text/plain | text | D9 | text, `%20`s and all | OK |
| 25 | A14809898470940682177 | image/jpeg | img | D2 | JPEG image | OK |
| 26 | SSETONET | text/plain | text | D9 | text | OK |
| 27 | A16079936798727682177 | image/jpeg | img | D2, D10 | JPEG (same bytes as #25, different asset) | OK |
| 28 | A16756519242061643639 | text/plain | text | D9 | literal text `620a` | OK |
| 29 | A5626872973035810444 | text/plain | text | D9 | literal text `620A` | OK |
| 30 | A9150645658417709715 | image/jpeg | img | D2 | JPEG image (474 KB) | OK |
| 31–42 | (12 assets) | text/plain | ptr | D6 | pointer text | OK |
| 43 | ORDINALMINT | image/jpeg | img | D2 | JPEG image | OK |
| 44 | TRUDEEPEPE | text/plain | ptr | D6 | pointer text | OK |
| 45 | OPUSKHE | application/octet-stream | audio | **D3** | **audio player** — bytes are Ogg Opus, identical to #46 | **FIX** (shows Download page) |
| 46 | OPUSAUDIO | audio/opus | audio | D2 | audio player | OK |
| 47 | A171770237737159042 | text/plain | text | D9 | text | OK |
| 48 | A171770243778123456 | image/jpeg | img | D2, D10 | JPEG image | OK |
| 49 | A171770243778123456 | image/jpeg | img | D2, D10 | JPEG (re-mint of #48) | OK |
| 50 | A171770248226422651 | image/jpeg | img | D2 | JPEG image | OK |
| 51 | A171770323249123456 | image/jpeg | audio | **D3** | **audio player** — declared image/jpeg, bytes are Ogg Opus | **FIX** (broken `<img>`) |
| 52 | HUNGRYPARTY | text/plain | ptr | D6 | pointer text | OK |
| 53 | NINJAFREN | text/plain | ptr | D6 | pointer text | OK |
| 54 | MAGICEGG | text/plain | stamp | D4, **D5.1** | **decoded PNG** — space→`+` repair recovers the exact image of #57/#58 | **FIX** (serves CRC-corrupt PNG) |
| 55 | PEPEPANIC | text/plain | ptr | D6 | pointer text | OK |
| 56 | FROGSOUPYUM | text/plain | ptr | D6 | pointer text | OK |
| 57 | A9670224954228772833 | text/plain | stamp | D4 | decoded PNG | OK |
| 58 | A9670224954228772834 | text/plain | stamp | D4, D10 | decoded PNG (same bytes as #57) | OK |
| 59 | XCPFTW | text/plain | stamp | D4, **D5.4** | **decoded PNG** — stray `iVBOR=` prefix; signature-scan recovers #60's exact image | **FIX** (falls back to text) |
| 60 | XCPFTW | text/plain | stamp | D4 | decoded PNG (the corrected re-mint of #59) | OK |
| 61 | XSTARS | text/plain | stamp | D4 | decoded PNG | OK |
| 62–77 | (16 assets) | text/plain | ptr | D6 | pointer text | OK |
| 78 | BASECASETEST | text/plain | text | D9 | text | OK |
| 79 | BURNCASETEST | text/plain | text | D9 | text | OK |
| 80 | PROPTESTCASE | text/plain | text | D9 | text | OK |
| 81 | PROPCASETEST | text/plain | text | D9 | text | OK |
| 82 | OTFS | text/plain | ptr | D6 | pointer text | OK |
| 83 | A12001200120012001200 | text/plain | stamp | D4 | decoded GIF | OK |
| 84 | STAMPINAL | text/plain | stamp | D4 | decoded GIF (stampchain's cursed stamp #-1841) | OK |
| 85 | XCPBULL | text/plain | ptr | D6 | pointer text | OK |
| 86 | THEFIVEOFUS | text/plain | ptr | D6 | pointer text | OK |

Totals: 87 counters — 46 pointers, 17 text, 11 native images, 3 native
audio (one mislabeled, one generic-labeled), 4 HTML, 9 stamps (2 needing
repair), 0 unrecoverable.

**Under these rules every counter that carries an on-chain file displays
it. Nothing falls through to the download page, and only genuine text and
pointers render as text.**

## Current implementation gaps

Ordered by user-visible severity; all are serve-time changes in
`content.py` / `preview.py` / `app.py`, no reindex.

1. **G1 — #54 serves a corrupt image today.** `stamp_image()` strips
   whitespace; per D5.1 it must substitute `' '`→`'+'` first (then strip
   only CR/LF/TAB). Verified: substitution yields sha-identical bytes to
   #57/#58; stripping yields a PNG with a bad PLTE CRC.
2. **G2 — #45 hides on-chain audio behind a Download page.** Needs D3
   magic-sniffing beyond images (at least `OggS`) wired into preview
   classification when the declared type is generic.
3. **G3 — #51 renders on-chain audio as a broken image.** Same D3 sniffing
   applied when declared type *contradicts* the magic.
4. **G4 — #59 shows raw text though its image is recoverable.** D5.4
   stray-prefix rescue in `stamp_image()`.
5. **G5 (cosmetic) — #3 is labeled image/gif but is a PNG.** Browsers
   render it regardless; expose the sniffed type as display metadata so the
   label is honest.
6. **G6 — `/preview/<n>` and `/stamp/<n>` are cached
   `max-age=31536000, immutable` (app.py `_send(..., immutable=True)`).**
   Violates D11: browsers that visited before a display-rule change keep the
   old rendering for a year (observed live on #84 after the stamp feature
   shipped). Drop `immutable` on derived views; short max-age or a
   `?v=<commit>` cache key instead.

## Open questions (to settle before implementing)

* **D5 scope:** repair ladder as specified stops at space-substitution +
  stray-prefix rescue. Any appetite for more (e.g. truncated-tail images)?
  Recommendation: no — these two are exact, verified recoveries; anything
  further is guesswork and violates "deterministic".
* **D8 ord proxy:** worth wiring `/content/<inscription-id>` passthrough to
  the local ord server so #6 renders fully? Optional, off by default.
* **D3 sniff set:** images + Ogg cover everything on chain today. Extend to
  a broader magic table (PDF, WOFF2, MP4/ISO-BMFF, WAV/RIFF) now or as
  needed?
