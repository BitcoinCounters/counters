"""The indexing engine: walks blocks, asks Counterparty (the oracle) for each
block's issuances and fairminter deploys, verifies the taproot-envelope
carrier against bitcoind, and writes numbered counter records.
"""

from .indexer import Indexer

__all__ = ["Indexer"]
