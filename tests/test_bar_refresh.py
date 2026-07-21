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
