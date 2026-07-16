"""CLI command handlers.

Each module implements the logic behind a `counters` subcommand; argument
parsing and dispatch live in `counters.__main__`.

- read.py      status / info / list / validate (read-only index queries)
- wallet.py    create / restore / receive / balance / inscriptions
- inscribe.py  the mint flow (compose issuance + build/sign commit & reveal)
- send.py      transfer a counter (compose Counterparty send + sign + broadcast)
- serve.py     the `server` command entry point
"""
