"""CLI command handlers.

Each module implements the logic behind a `counters` subcommand; argument
parsing and dispatch live in `counters.__main__`.

- read.py      status / info / list (read-only index queries)
- wallet.py    create / restore / receive / balance / inscriptions
- inscribe.py  the mint flow (compose issuance + build/sign commit & reveal)
- send.py      transfer a counter (compose Counterparty send + sign + broadcast)
- issue.py     issue / lock-supply / lock-description / transfer-ownership
- burn.py      permanently destroy a quantity of an asset (Counterparty destroy)
- dispenser.py buy-from-dispenser, and the operator side: open/refill/close/list
- order.py     DEX: open-order / cancel-order / pay-order / orders
- bump.py      CPFP-accelerate an unconfirmed transaction
- cancel.py    RBF-abandon an unconfirmed transaction
- serve.py     the `server` command entry point
"""
