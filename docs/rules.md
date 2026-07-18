# Counters server — rendering rules

**Status: current. Audited against counters #0–#86 on 2026-07-17.**

These are the rules a **counters server** follows to decide what to *show* for
a counter — in the explorer grid, on a counter's page, and at the `/preview`
and `/stamp` endpoints. They are deliberately **simple and deterministic** so
that any independent implementation renders the same counter the same way. A
clever recovery that only one server does is worse than a plain rule every
server follows: two explorers must never disagree about what a counter *is*.

Rendering is **display only**. Nothing here affects validity (R1–R4),
numbering (N1–N6), the canonical content bytes ([build ref §5](build-reference-v3.md)),
content hashes, or the rolling consensus hash. Stored blobs are never
rewritten, and `/content/<n>` is always the exact canonical bytes.

> **A counters server only ever shows on-chain files.**
> Every byte it renders comes from the counter's own on-chain content —
> nothing is fetched from a URL, gateway, or any other server, and nothing is
> reconstructed or guessed. Show what is cleanly on chain, and only that.

This is the whole point. A counter's file lives in Bitcoin witness data; the
server displays *that*, and never anything else. When a counter's content is
only a *pointer* to an off-chain file (rule 4), the server shows the pointer
text — it does **not** follow it, because the file it points to is not on
chain and is not the counter.

## The rules

**1 — Rendering is not consensus.** Everything here happens at serve time. A
change to these rules never requires a reindex or renumber, and never changes
`/content/<n>`.

**2 — Sniff the bytes; the declared type is only a hint.** Check the leading
bytes against a small fixed table of signatures (PNG, GIF, JPEG, WebP, Ogg,
…). A recognized signature **wins** over the mint-time `mime_type`; if no
signature matches, use the declared type. Then render with the native element
for that type — image as `<img>`, audio as `<audio>`, video as `<video>`,
etc. The API still reports the declared `content_type`; the sniffed type is
display metadata beside it.

**3 — `STAMP:` is decode-or-text.** A text body of the form `STAMP:<base64>`
renders as an image **only if** the base64 is well-formed (strict base64
alphabet, padding only at the very end) **and** decodes to bytes carrying a
recognized image signature. Then the image is served at `/stamp/<n>` and shown
everywhere an image would be; `/content/<n>` stays the raw text. Anything else
— malformed base64, non-image bytes — is shown as plain text. **No character
substitution, no prefix surgery, no repair of damaged data.**

**4 — Pointers are text, and never fetched.** A body that is a single URI
token (`ipfs:`, `ipfs://`, `ar://`, `http(s)://`) is a *reference*, not an
on-chain file — only the pointer is on chain. Show the pointer text (with a
pointer badge); **never** dereference it — no gateway fetches, no remote
thumbnails, no caching of off-chain bytes.

**5 — HTML and SVG render sandboxed.** `text/html` and `image/svg+xml` are
live documents, rendered inside a sandboxed iframe (`sandbox=allow-scripts`,
opaque origin, confining CSP — the ord model). Same-origin sub-resources
resolve against this server only; any external or cross-protocol reference
simply does not load (rule 4 — never fetched from a gateway). The document
still renders; a missing dependency is the minter's bet, not a server bug.

**6 — Everything else is text, or a download.** Text, JSON, and code render
**verbatim** — escaped, in full, with no percent-decoding, no trimming, no
linkifying, no "looks like hex, let's decode it." Bytes that match no
signature and aren't textual get a download link, not a guess.

**7 — Cache content forever, derived views briefly.** `/content/<n>` is
content-addressed, so a long/immutable cache is fine (away from the reorg
window). But `/preview/<n>` and `/stamp/<n>` are *functions of these rules* —
they must be served with a short-lived cache (or a cache key that includes the
build revision), **never `immutable`**, so a rules change takes effect at once.

## What each counter renders (#0–#86)

Renders-as — **image / audio / html / text / pointer**. Status — **OK**:
server already does this · **FIX**: server needs the rule below.

| # | Asset | Declared type | Renders as | By rule | Status |
|---|-------|---------------|-----------|---------|--------|
| 0 | XDUALS | text/plain | text `testdual` | 6 | OK |
| 1–2 | DUALNAKA | image/gif | GIF image | 2 | OK |
| 3 | DUALNAKA | image/gif | image (bytes are **PNG**; sniff labels it) | 2 | OK |
| 4 | DUALPEPE | image/gif | GIF image | 2 | OK |
| 5–6 | A163…/A131… | text/html | live HTML, sandboxed | 5 | OK |
| 7–11 | A826…→A474… | text/plain | `ipfs:` pointer, never fetched | 4 | OK |
| 12–13 | A179…/A472… | text/html | live HTML, sandboxed | 5 | OK |
| 14 | LINKMISSING | text/plain | pointer | 4 | OK |
| 15 | A123… | application/json | JSON (text) | 6 | OK |
| 16–17 | FADEDNJADED/LIBBITCOIN | text/plain | pointer | 4 | OK |
| 18–21 | A987…/DERPDERP/DEGENORDINAL | text/plain | text | 6 | OK |
| 22–23 | FAIRBITCOIN/XCPFORTWENY | text/plain | pointer | 4 | OK |
| 24 | A106… | text/plain | text (`%20`s and all) | 6 | OK |
| 25 | A148… | image/jpeg | JPEG image | 2 | OK |
| 26 | SSETONET | text/plain | text | 6 | OK |
| 27 | A160… | image/jpeg | JPEG image | 2 | OK |
| 28–29 | A167…/A562… | text/plain | literal text `620a` / `620A` | 6 | OK |
| 30 | A915… | image/jpeg | JPEG image (474 KB) | 2 | OK |
| 31–42 | (12 assets) | text/plain | pointer | 4 | OK |
| 43 | ORDINALMINT | image/jpeg | JPEG image | 2 | OK |
| 44 | TRUDEEPEPE | text/plain | pointer | 4 | OK |
| 45 | OPUSKHE | application/octet-stream | **audio** — bytes are Ogg Opus | 2 | **FIX** |
| 46 | OPUSAUDIO | audio/opus | audio | 2 | OK |
| 47 | A171… | text/plain | text | 6 | OK |
| 48–50 | A171…×3 | image/jpeg | JPEG image | 2 | OK |
| 51 | A171… | image/jpeg | **audio** — declared jpeg, bytes are Ogg Opus | 2 | **FIX** |
| 52–53 | HUNGRYPARTY/NINJAFREN | text/plain | pointer | 4 | OK |
| 54 | MAGICEGG | text/plain | **text** — `STAMP:` base64 is damaged (stray space) | 3 | **FIX** |
| 55–56 | PEPEPANIC/FROGSOUPYUM | text/plain | pointer | 4 | OK |
| 57–58 | A967…×2 | text/plain | decoded PNG (`STAMP:`) | 3 | OK |
| 59 | XCPFTW | text/plain | text — `STAMP:` base64 is damaged (stray prefix) | 3 | OK |
| 60–61 | XCPFTW/XSTARS | text/plain | decoded PNG (`STAMP:`) | 3 | OK |
| 62–77 | (16 assets) | text/plain | pointer | 4 | OK |
| 78–81 | *TESTCASE assets | text/plain | text | 6 | OK |
| 82 | OTFS | text/plain | pointer | 4 | OK |
| 83–84 | A120…/STAMPINAL | text/plain | decoded GIF (`STAMP:`; also cursed stamps) | 3 | OK |
| 85–86 | XCPBULL/THEFIVEOFUS | text/plain | pointer | 4 | OK |

Totals: 87 counters — 46 pointers, 19 text (incl. #54/#59 damaged stamps →
text), 11 images, 3 audio, 4 HTML, 6 shown-image stamps.

## Gaps between these rules and today's server

Serve-time changes only (`content.py` / `preview.py` / `app.py`) — no reindex.

1. **Sniffing (rule 2) isn't wired past images.** #45 shows a Download page
   and #51 a broken `<img>`, though both are Ogg Opus audio. Add `OggS` (and
   any other on-chain signatures) to the sniffer and let a recognized
   signature override the declared type when building the preview.
2. **`STAMP:` decode is too lax (rule 3).** `stamp_image()` strips whitespace
   before decoding, so #54's damaged base64 yields a corrupt PNG that the
   server serves as a broken image. Require strict base64 (reject stray
   whitespace/characters); #54 then falls back to text, matching rule 3.
   (#59 already falls back to text — no change.)
3. **`/preview` and `/stamp` are cached `immutable` (rule 7).** `app.py` sends
   `max-age=31536000, immutable` on derived views, so a browser that visited
   before a rules change keeps the old rendering for a year (seen live on #84
   after the stamp feature shipped). Drop `immutable` there; use a short
   max-age or a `?v=<commit>` key.

Cosmetic: #3 is declared `image/gif` but is a PNG — browsers render it either
way; rule 2's sniffed type just makes the displayed label honest.

## Deliberately excluded

- **No base64 repair.** #54's and #59's images are *technically* recoverable
  (space→`+`; drop a stray prefix), but recovery is a per-server guess, not a
  rule others can reproduce. Under rule 3 they show as text. We render what is
  cleanly on chain, not what we can reconstruct.
- **No pointer dereferencing** (rule 4) and **no gateway fetches for recursive
  HTML** (rule 5): a server only ever serves bytes that are on chain.
