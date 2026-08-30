"""_wallet_addresses must include descriptor-derived addresses, not just what
Bitcoin Core has seen on-chain: a Counterparty send pays the destination no
bitcoin output (the address rides in the OP_RETURN), so an address holding
only assets has no transaction Core can attribute to it."""

from counters.bitcoind import BitcoindError
from counters.commands import wallet as W

SEEN = "bc1pseen000000000000000000000000000000000000000000000000000000"
DERIVED = "bc1qcx8cjuxyfff84wsymp74lcak8qrq6np7ds7tze"


class FakeBTC:
    def __init__(self, descriptors=True):
        self.descriptors = descriptors

    def wallet_call(self, name, method, params):
        if method == "listreceivedbyaddress":
            return [{"address": SEEN}]
        if method == "listunspent":
            return []
        if method == "listdescriptors":
            if not self.descriptors:
                raise BitcoindError("Only descriptor wallets support this call")
            return {"descriptors": [
                {"desc": "wpkh([0a41a6cd/84h/0h/0h]xpub.../0/*)#x", "internal": False},
            ]}
        raise AssertionError(method)

    def _call(self, method, params):
        assert method == "deriveaddresses"
        desc, (lo, hi) = params
        assert (lo, hi) == (0, W.DERIVED_WINDOW - 1)
        return [DERIVED] + [f"bc1qfiller{i}" for i in range(lo + 1, hi + 1)]


def test_asset_only_address_is_found_via_descriptors():
    addrs = W._wallet_addresses(FakeBTC(), "counts")
    assert SEEN in addrs
    assert DERIVED in addrs  # never received a satoshi, still ours
    assert len(addrs) == 1 + W.DERIVED_WINDOW


def test_legacy_wallet_without_descriptors_keeps_onchain_view():
    assert W._wallet_addresses(FakeBTC(descriptors=False), "old") == [SEEN]
