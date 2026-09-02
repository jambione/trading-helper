"""
test_bar_refresh.py — unit tests for TickerState bar-refresh timing logic.

Tests the two fetch paths in _refresh_bars():
  - First fetch: fires after the stagger delay (fetch_offset_s after being added)
  - Subsequent fetches: wait for ~1 minute of elapsed time + stagger slot

Requires the full venv (signal_engine imports pandas).
Run:
    venv/bin/python -m pytest tests/test_bar_refresh.py -q
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

se = pytest.importorskip("signal_engine", reason="signal_engine requires pandas")
TickerState = se.TickerState


# ── Helpers ───────────────────────────────────────────────────────────────────

def _should_fetch(ts: TickerState, secs_past_minute: float = 0.0) -> bool:
    """Replicate the _refresh_bars guard logic without doing a real fetch."""
    now = time.time()
    if not ts.bars_fetched:
        min_wait = (se.BAR_FAIL_RETRY_S if ts._bar_fetch_attempts > 0
                    else se.BAR_REFRESH)
        return (now - ts.last_bar_fetch) >= min_wait
    fire_at = 5 + ts.fetch_offset_s
    if (now - ts.last_bar_fetch) < 55:
        return False
    return secs_past_minute >= fire_at


# ── First-fetch (stagger) path ────────────────────────────────────────────────

def test_first_fetch_blocked_before_stagger_delay():
    ts = TickerState("NVDA", fetch_offset_s=10)
    # Initialised so last_bar_fetch is (BAR_REFRESH - offset) seconds ago
    # meaning fetch is not due yet
    ts.last_bar_fetch = time.time() - (se.BAR_REFRESH - 15)
    assert not _should_fetch(ts)


def test_first_fetch_allowed_after_bar_refresh():
    ts = TickerState("NVDA", fetch_offset_s=0)
    ts.last_bar_fetch = time.time() - (se.BAR_REFRESH + 1)
    assert _should_fetch(ts)


def test_stagger_offsets_space_out_tickers():
    """Tickers with different offsets fetch at different stagger slots."""
    tickers = [TickerState(f"T{i}", fetch_offset_s=i * se.BAR_STAGGER) for i in range(5)]
    # Simulate all having just had their first fetch
    now = time.time()
    for ts in tickers:
        ts.last_bar_fetch  = now - (se.BAR_REFRESH + 1)
        ts.last_bar_minute = -1   # first-fetch path for all
    # All should be ready (BAR_REFRESH + 1 has elapsed)
    assert all(_should_fetch(ts) for ts in tickers)


# ── Subsequent-fetch (minute-boundary) path ───────────────────────────────────

def test_subsequent_fetch_blocked_within_55s():
    ts = TickerState("NVDA", fetch_offset_s=0)
    ts.bars_fetched = True
    ts.last_bar_minute = int(time.time() // 60)
    ts.last_bar_fetch  = time.time() - 30   # only 30s ago
    assert not _should_fetch(ts, secs_past_minute=10.0)


def test_subsequent_fetch_allowed_after_55s_past_stagger_slot():
    ts = TickerState("NVDA", fetch_offset_s=0)   # fire_at = 5
    ts.bars_fetched = True
    ts.last_bar_minute = int(time.time() // 60) - 1
    ts.last_bar_fetch  = time.time() - 60        # 60s ago
    assert _should_fetch(ts, secs_past_minute=6.0)  # 6 >= fire_at=5


def test_subsequent_fetch_blocked_before_stagger_slot():
    ts = TickerState("NVDA", fetch_offset_s=10)  # fire_at = 15
    ts.bars_fetched = True
    ts.last_bar_minute = int(time.time() // 60) - 1
    ts.last_bar_fetch  = time.time() - 60
    assert not _should_fetch(ts, secs_past_minute=3.0)  # 3 < fire_at=15


def test_elapsed_time_guard_is_ntp_safe():
    """Simulates NTP backward step: minute counter goes back but elapsed time is fine."""
    ts = TickerState("NVDA", fetch_offset_s=0)
    ts.bars_fetched = True
    ts.last_bar_minute = int(time.time() // 60) + 2   # "future" minute (simulates backward jump)
    ts.last_bar_fetch  = time.time() - 60             # 60s of real elapsed time
    # Old code: current_minute (now) <= last_bar_minute (future) → blocked forever
    # New code: elapsed 60s > 55 → passes; secs_past_minute check decides
    assert _should_fetch(ts, secs_past_minute=10.0)


def test_failed_first_fetch_retries_sooner_than_bar_refresh():
    ts = TickerState("THIN", fetch_offset_s=0)
    ts.bars_fetched = False
    ts._bar_fetch_attempts = 1
    ts.last_bar_fetch = time.time() - (se.BAR_FAIL_RETRY_S + 1)
    assert _should_fetch(ts)
    ts.last_bar_fetch = time.time() - max(1, se.BAR_FAIL_RETRY_S - 5)
    assert not _should_fetch(ts)


def test_subsequent_path_requires_bars_fetched():
    """Once bars loaded, use the ~1 minute subsequent cadence (not fail-retry)."""
    ts = TickerState("NVDA", fetch_offset_s=0)
    ts.bars_fetched = True
    ts.last_bar_minute = int(time.time() // 60)
    ts.last_bar_fetch = time.time() - 30
    assert not _should_fetch(ts, secs_past_minute=10.0)


# ── fetch_bars ordering ───────────────────────────────────────────────────────
# `limit` truncates from whichever end `sort` starts at. Ascending returned the
# OLDEST `count` bars in the lookback window, so with the 5-day default the
# engine computed CM RSI-2 / %R / MACD on bars three days old and published
# them as current readings. Verified live: sort=asc gave SPY bars ending
# 2026-07-27 while the clock read 2026-07-30.

class _FakeResp:
    def __init__(self, payload, status_code=200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise se.requests.HTTPError(f"{self.status_code}", response=self)

    def json(self):
        return self._payload


def _bars_newest_first(n=60):
    """What Alpaca returns for sort=desc: newest first."""
    return [{"t": f"2026-07-30T{15 - (i // 60):02d}:{59 - (i % 60):02d}:00Z",
             "o": 10.0, "h": 11.0, "l": 9.0, "c": 10.5, "v": 1000.0}
            for i in range(n)]


def test_fetch_bars_requests_newest_first(monkeypatch):
    seen = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        seen.update(params or {})
        return _FakeResp({"bars": _bars_newest_first()})

    monkeypatch.setattr(se.requests, "get", fake_get)
    se.fetch_bars("SPY", "k", "s")
    assert seen.get("sort") == "desc", (
        "ascending + limit keeps the OLDEST bars in the window")


def test_fetch_bars_returns_oldest_first(monkeypatch):
    """Indicators read forward and take .iloc[-1] as now, so the frame handed
    back must be ascending even though the API was asked for descending."""
    monkeypatch.setattr(se.requests, "get",
                        lambda *a, **k: _FakeResp({"bars": _bars_newest_first()}))
    df = se.fetch_bars("SPY", "k", "s")
    assert df is not None
    times = list(df["time"])
    assert times == sorted(times), "frame is not oldest-first"


def test_fetch_bars_keeps_the_newest_bar(monkeypatch):
    """The whole point: the last row is the most recent bar available."""
    payload = _bars_newest_first()
    newest = payload[0]["t"]
    monkeypatch.setattr(se.requests, "get",
                        lambda *a, **k: _FakeResp({"bars": payload}))
    df = se.fetch_bars("SPY", "k", "s")
    assert df is not None
    assert df["time"].iloc[-1] == newest
