"""dashboard.snapshot() stale-while-revalidate — never block readers on rebuild.

Mid-RTH /api/state was timing out because snapshot() held _SNAP_LOCK around a
full _snapshot() rebuild. These tests pin the SWR contract: return stale while
a rebuild is in flight, single-flight the refresh, and honor TTL.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dashboard as d  # noqa: E402


def _reset_snap_cache():
    with d._SNAP_LOCK:
        d._SNAP_CACHE = (0.0, {})
        d._SNAP_REBUILDING = False
        d._SNAP_LAST_TIMING_LOG = 0.0


def test_fresh_ttl_returns_cache_without_rebuild(monkeypatch):
    _reset_snap_cache()
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        raise AssertionError("_snapshot must not run while TTL is fresh")

    monkeypatch.setattr(d, "_snapshot", boom)
    monkeypatch.setattr(d, "_SNAP_TTL", 1.0)
    cached = {"tickers": [{"ticker": "AAA"}], "tag": "fresh"}
    with d._SNAP_LOCK:
        d._SNAP_CACHE = (time.monotonic(), cached)

    assert d.snapshot() is cached
    assert calls["n"] == 0


def test_expired_ttl_returns_stale_immediately_while_rebuild_runs(monkeypatch):
    _reset_snap_cache()
    monkeypatch.setattr(d, "_SNAP_TTL", 0.05)
    stale = {"tickers": [{"ticker": "OLD"}], "tag": "stale"}
    with d._SNAP_LOCK:
        # Age the cache past TTL.
        d._SNAP_CACHE = (time.monotonic() - 1.0, stale)

    started = threading.Event()
    release = threading.Event()
    calls = {"n": 0}

    def slow_rebuild():
        calls["n"] += 1
        started.set()
        assert release.wait(2.0), "test timed out waiting to release rebuild"
        return {"tickers": [{"ticker": "NEW"}], "tag": "fresh"}

    monkeypatch.setattr(d, "_snapshot", slow_rebuild)

    t0 = time.monotonic()
    got = d.snapshot()
    elapsed = time.monotonic() - t0

    assert got is stale
    assert got["tag"] == "stale"
    assert elapsed < 0.2, f"reader blocked on rebuild ({elapsed:.3f}s)"
    assert started.wait(1.0), "background rebuild never started"

    # Concurrent readers also get stale without kicking a second rebuild.
    assert d.snapshot() is stale
    time.sleep(0.05)
    assert calls["n"] == 1, "expected single-flight rebuild"

    release.set()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        with d._SNAP_LOCK:
            rebuilding = d._SNAP_REBUILDING
            tag = (d._SNAP_CACHE[1] or {}).get("tag")
        if not rebuilding and tag == "fresh":
            break
        time.sleep(0.02)
    else:
        raise AssertionError("background rebuild did not publish fresh cache")

    assert d.snapshot()["tag"] == "fresh"


def test_single_flight_under_concurrent_expired_readers(monkeypatch):
    _reset_snap_cache()
    monkeypatch.setattr(d, "_SNAP_TTL", 0.01)
    stale = {"tickers": [], "tag": "stale"}
    with d._SNAP_LOCK:
        d._SNAP_CACHE = (time.monotonic() - 1.0, stale)

    gate = threading.Event()
    calls = {"n": 0}
    lock = threading.Lock()

    def slow_rebuild():
        with lock:
            calls["n"] += 1
        assert gate.wait(2.0)
        return {"tickers": [], "tag": "fresh"}

    monkeypatch.setattr(d, "_snapshot", slow_rebuild)

    results = []
    barrier = threading.Barrier(8)

    def reader():
        barrier.wait(2.0)
        results.append(d.snapshot())

    threads = [threading.Thread(target=reader) for _ in range(8)]
    for t in threads:
        t.start()
    # Let all readers hit the expired path before releasing the rebuild.
    time.sleep(0.1)
    gate.set()
    for t in threads:
        t.join(2.0)
        assert not t.is_alive()

    assert all(r is stale or r.get("tag") in ("stale", "fresh") for r in results)
    assert calls["n"] == 1, f"expected one rebuild, got {calls['n']}"


def test_cold_start_builds_synchronously_once(monkeypatch):
    _reset_snap_cache()
    monkeypatch.setattr(d, "_SNAP_TTL", 1.0)
    calls = {"n": 0}

    def build():
        calls["n"] += 1
        time.sleep(0.05)
        return {"tickers": [{"ticker": "AAA"}], "tag": "cold"}

    monkeypatch.setattr(d, "_snapshot", build)
    got = d.snapshot()
    assert got["tag"] == "cold"
    assert calls["n"] == 1
    # Second call within TTL must not rebuild.
    assert d.snapshot() is got
    assert calls["n"] == 1
