"""CLI command handlers.

Each module implements the logic behind a `counters` subcommand; argument
parsing and dispatch live in `counters.__main__`.

- read.py      status / info / list (read-only index queries)
- wallet.py    create / restore / receive / balance / inscriptions
- inscribe.py  the mint flow (compose issuance + build/sign commit & reveal)
- send.py      transfer a counter (compose Counterparty send + sign + broadcast)
- issue.py     issue / lock-supply / lock-description / transfer-ownership
- burn.py      permanently destroy a quantity of an asset (Counterparty destroy)
- burn_ordinal.py burn-ordinal-sat: burn the ordinals inscription of a
  counterparty + ord counter (the Counterparty asset is untouched)
- dispenser.py buy-from-dispenser (and, called bare, what is for sale —
  cheapest per unit first), plus the operator side: open/refill/close/list
- order.py     DEX: swap / open-order / cancel-order / pay-order / orders
- pool.py      AMM liquidity: add-liquidity / remove-liquidity / pools
- bump.py      CPFP-accelerate an unconfirmed transaction
- cancel.py    RBF-abandon an unconfirmed transaction
- serve.py     the `server` command entry point
"""
