"""Auth handling for the bitcoind RPC client.

bitcoind rewrites its .cookie with a fresh random password on every restart, so
the client must re-read the cookie when it changes; caching it once would 401
forever after a node restart (the "restart didn't show up" bug).
"""

import time

import pytest

from counters.bitcoind import BitcoindClient, BitcoindError
from counters.config import Config


def _config(cookie_path, user="", password="") -> Config:
    return Config(
        btc_cookie_file=str(cookie_path),
        btc_rpc_user=user,
        btc_rpc_password=password,
    )


def test_cookie_auth_reread_after_bitcoind_restart(tmp_path):
    cookie = tmp_path / ".cookie"
    cookie.write_text("__cookie__:oldpass")
    client = BitcoindClient(_config(cookie))
    assert client._resolve_auth() == ("__cookie__", "oldpass")

    # bitcoind restart: same file, brand-new password + a newer mtime.
    time.sleep(0.01)
    cookie.write_text("__cookie__:newpass")
    import os

    future = time.time() + 5
    os.utime(cookie, (future, future))

    assert client._resolve_auth() == ("__cookie__", "newpass")


def test_cookie_auth_cached_between_calls_when_unchanged(tmp_path):
    cookie = tmp_path / ".cookie"
    cookie.write_text("__cookie__:pw")
    client = BitcoindClient(_config(cookie))
    first = client._resolve_auth()
    second = client._resolve_auth()
    assert first == second == ("__cookie__", "pw")


def test_falls_back_to_static_auth_when_no_cookie(tmp_path):
    missing = tmp_path / "does-not-exist.cookie"
    client = BitcoindClient(_config(missing, user="rpcuser", password="rpcpass"))
    assert client._resolve_auth() == ("rpcuser", "rpcpass")


def test_missing_cookie_reappears_and_is_picked_up(tmp_path):
    cookie = tmp_path / ".cookie"
    client = BitcoindClient(_config(cookie, user="rpcuser", password="rpcpass"))
    assert client._resolve_auth() == ("rpcuser", "rpcpass")
    # Cookie file appears (e.g. bitcoind starts): prefer it over static creds.
    cookie.write_text("__cookie__:live")
    assert client._resolve_auth() == ("__cookie__", "live")


def test_no_auth_at_all_fails_fast(tmp_path):
    missing = tmp_path / "does-not-exist.cookie"
    with pytest.raises(BitcoindError):
        BitcoindClient(_config(missing))
