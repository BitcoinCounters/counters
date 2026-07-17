"""Bitcoin Counters v3 (counters) — Counterparty taproot-envelope file-event
indexer, explorer, and wallet.

A counter is a numbered file event: a Counterparty asset description carried
in a v11 taproot envelope, numbered deterministically from #0 (XDUALS, block
902,005). Counterparty Core is the oracle for validity, identity, ownership,
AND content; this package owns only the carrier check (reveal.py), the
numbering, and storage. See docs/build-reference-v3.md.
"""

__version__ = "0.2.0"
