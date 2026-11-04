"""replay_session JsonCache: shared between concurrent replays, never erases another
run's values, and serves misses as unknown under REPLAY_OFFLINE."""
from __future__ import annotations

import json
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import replay_session as rs  # noqa: E402


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(rs, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(rs, "OFFLINE", False)
    rs.OFFLINE_STATS.update(cache_misses=0, blocked_connects=0)
    return tmp_path


def test_two_runs_share_and_do_not_erase(cache_dir):
    a, b = rs.JsonCache("x"), rs.JsonCache("x")
    assert a.get("k1", lambda: 1) == 1
    a.save()
    calls = []
    # b loaded before a saved; its miss rereads the file instead of fetching
    assert b.get("k1", lambda: calls.append(1) or 99) == 1 and not calls
    assert b.get("k2", lambda: 2) == 2
    b.save()
    assert a.get("k3", lambda: 3) == 3
    a.save()                                   # a never saw k2; it must not erase it
    disk = json.loads((cache_dir / "x.json").read_text())
    assert disk == {"k1": 1, "k2": 2, "k3": 3}


def test_offline_miss_is_unknown_and_counted(cache_dir, monkeypatch):
    c = rs.JsonCache("y")
    assert c.get("warm", lambda: 5) == 5
    c.save()
    monkeypatch.setattr(rs, "OFFLINE", True)
    d = rs.JsonCache("y")
    assert d.get("warm", lambda: pytest.fail("fetched")) == 5
    assert d.get("cold", lambda: pytest.fail("fetched")) is None
    assert rs.OFFLINE_STATS["cache_misses"] == 1


def test_offline_bars_refuse_to_download(cache_dir, monkeypatch):
    monkeypatch.setattr(rs, "OFFLINE", True)
    with pytest.raises(SystemExit, match="not cached"):
        rs.day_bars("2026-10-29", {"ABC"}, client=None)


def test_socket_guard_blocks_outbound_only(cache_dir, monkeypatch):
    orig = socket.socket.connect
    monkeypatch.setattr(socket.socket, "connect", orig)
    rs._offline_socket_guard()
    try:
        s = socket.socket()
        with pytest.raises(ConnectionRefusedError, match="REPLAY_OFFLINE"):
            s.connect(("93.184.216.34", 443))
        s.close()
        assert rs.OFFLINE_STATS["blocked_connects"] == 1
    finally:
        socket.socket.connect = orig
