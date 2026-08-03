"""A wallet on disk but not loaded should just get loaded.

Bitcoin Core unloads wallets on restart, and its -18 error ("does not exist or
is not loaded") used to be handed straight to the user as homework. The client
now tells them it is loading and does it.
"""

from __future__ import annotations

import json as jsonlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counters.bitcoind import BitcoindClient, BitcoindError  # noqa: E402
from counters.config import Config  # noqa: E402

NOT_LOADED = {"code": -18, "message": "Requested wallet does not exist or is not loaded"}


class FakeResponse:
    def __init__(self, payload: dict):
        self.status_code = 200
        self._payload = payload
        self.text = jsonlib.dumps(payload)

    def json(self) -> dict:
        return self._payload


class FakeNode:
    """Bitcoin Core with `on_disk` wallets, of which `loaded` are loaded."""

    def __init__(self, on_disk=("mywallet",), loaded=(), load_says_already=False):
        self.on_disk = list(on_disk)
        self.loaded = set(loaded)
        self.load_says_already = load_says_already
        self.calls: list[tuple[str, str | None]] = []

    def post(self, url, json=None, auth=None, timeout=None):  # noqa: A002
        method, params = json["method"], json["params"]
        wallet = url.rsplit("/wallet/", 1)[1] if "/wallet/" in url else None
        self.calls.append((method, wallet))

        if method == "listwalletdir":
            return FakeResponse(
                {"result": {"wallets": [{"name": n} for n in self.on_disk]}, "error": None}
            )
        if method == "loadwallet":
            name = params[0]
            if self.load_says_already:
                # Someone else loaded it between our -18 and this call.
                self.loaded.add(name)
                return FakeResponse(
                    {"result": None, "error": {"code": -35, "message": f"Wallet {name} is already loaded."}}
                )
            self.loaded.add(name)
            return FakeResponse({"result": {"name": name, "warning": ""}, "error": None})
        if wallet is not None and wallet not in self.loaded:
            return FakeResponse({"result": None, "error": NOT_LOADED})
        return FakeResponse({"result": "ok", "error": None})


def _client(tmp_path, node) -> BitcoindClient:
    cookie = tmp_path / ".cookie"
    cookie.write_text("__cookie__:pw")
    client = BitcoindClient(Config(btc_cookie_file=str(cookie)))
    client._session = node
    return client


def test_unloaded_wallet_is_loaded_and_the_call_succeeds(tmp_path, capsys):
    node = FakeNode()
    client = _client(tmp_path, node)

    assert client.wallet_call("mywallet", "getwalletinfo") == "ok"

    methods = [m for m, _ in node.calls]
    assert methods == ["getwalletinfo", "listwalletdir", "loadwallet", "getwalletinfo"]
    # The wait is announced on stderr, so stdout stays parseable.
    assert "loading wallet 'mywallet'" in capsys.readouterr().err


def test_a_wallet_loaded_by_someone_else_is_not_an_error(tmp_path):
    node = FakeNode(load_says_already=True)
    client = _client(tmp_path, node)

    assert client.wallet_call("mywallet", "getwalletinfo") == "ok"


def test_missing_wallet_still_says_how_to_create_it(tmp_path):
    node = FakeNode(on_disk=())
    client = _client(tmp_path, node)

    with pytest.raises(BitcoindError) as e:
        client.wallet_call("ghost", "getwalletinfo")
    assert "does not exist" in str(e.value)
    assert "counters wallet create --name ghost" in str(e.value)
    assert "loadwallet" not in [m for m, _ in node.calls]


def test_load_is_attempted_only_once(tmp_path):
    """A wallet that loads but still answers -18 must not loop."""
    class StubbornNode(FakeNode):
        """loadwallet reports success but the wallet never becomes usable."""

        def post(self, url, json=None, auth=None, timeout=None):  # noqa: A002
            if json["method"] == "loadwallet":
                self.calls.append(("loadwallet", None))
                return FakeResponse({"result": {"name": json["params"][0]}, "error": None})
            return super().post(url, json=json, auth=auth, timeout=timeout)

    node = StubbornNode()
    client = _client(tmp_path, node)

    with pytest.raises(BitcoindError) as e:
        client.wallet_call("mywallet", "getwalletinfo")
    assert [m for m, _ in node.calls].count("loadwallet") == 1
    assert "will not keep it loaded" in str(e.value)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
