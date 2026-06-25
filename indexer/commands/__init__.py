"""CLI command handlers.

Each module implements the logic behind a `counter` subcommand; argument
parsing and dispatch live in `indexer.__main__`.

- read.py      status / info / list / validate (read-only index queries)
- wallet.py    create / restore / receive / balance / inscriptions
- inscribe.py  the mint flow (compose issuance + build/sign commit & reveal)
"""
