"""Bitcoin Counters v3 (counters) — Counterparty taproot-envelope file-event
indexer, explorer, and wallet.

A counter is a numbered file event: a Counterparty asset description carried
in a v11 taproot envelope, numbered deterministically from #0 (XDUALS, block
902,005). Counterparty Core is the oracle for validity, identity, ownership,
AND content; this package owns only the carrier check (reveal.py), the
numbering, and storage. See docs/build-reference-v3.md.
"""

# Versions mirror Counterparty Core. MAJOR.MINOR name the Counterparty Core
# release this build targets (11.2 => Counterparty Core v11.2.x); PATCH is ours,
# incremented for counters releases against that same upstream minor and reset
# to 0 when we move up to a new one. So a glance at the version answers the only
# compatibility question that matters here: which Counterparty does this speak?
#
# The single source of truth: pyproject reads it from this attribute, the CLI
# prints it for `counters --version`, and the server reports it on /status and
# in the explorer footer.
__version__ = "11.2.0"

#: The Counterparty Core minor series this build targets ("11.2.0" -> "11.2").
CP_SERIES = ".".join(__version__.split(".")[:2])
