"""Tests for `--restart` (wipe the local index and rebuild from genesis)."""

from __future__ import annotations

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counters.__main__ import _wipe_index, main  # noqa: E402
from counters.config import Config  # noqa: E402


def _seed(cfg: Config) -> None:
    cfg.ensure_dirs()
    db = cfg.db_path
    db.write_text("db")
    (db.parent / f"{db.name}-wal").write_text("wal")
    (db.parent / f"{db.name}-shm").write_text("shm")
    blob_dir = cfg.blobs_dir / "ab"
    blob_dir.mkdir(parents=True, exist_ok=True)
    (blob_dir / "deadbeef").write_text("bytes")


def test_wipe_removes_db_and_blobs():
    cfg = Config()
    cfg.data_dir = tempfile.mkdtemp()
    _seed(cfg)
    db = cfg.db_path
    assert db.exists() and cfg.blobs_dir.exists()

    _wipe_index(cfg)

    assert not db.exists()
    assert not (db.parent / f"{db.name}-wal").exists()
    assert not (db.parent / f"{db.name}-shm").exists()
    assert not cfg.blobs_dir.exists()


def test_wipe_is_idempotent():
    cfg = Config()
    cfg.data_dir = tempfile.mkdtemp()
    _wipe_index(cfg)  # nothing there yet — must not raise
    _seed(cfg)
    _wipe_index(cfg)
    _wipe_index(cfg)  # already gone — still fine


def test_restart_rejected_on_read_commands():
    # --restart is only on index/sync/server; reads must reject it.
    with pytest.raises(SystemExit):
        main(["status", "--restart"])


if __name__ == "__main__":
    test_wipe_removes_db_and_blobs()
    test_wipe_is_idempotent()
    print("ok")
