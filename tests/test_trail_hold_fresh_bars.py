"""Trail trend hold, 11/04 fix: a HELD name's bar store must keep refreshing, and every time the hold
lets an at-entry exit sell, the reason is logged (it sold 37 of 37 in-band exits the double check said to hold)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ai_positions as cp  # noqa: E402

CFG = {"ai_trail_exit_trend_hold": True, "ai_trail_exit_trend_hold_band_pct": 0.05,
       "ai_trail_exit_trend_hold_max_bar_age_sec": 300, "ai_double_check_lookback_min": 10}
T0 = 1_793_800_000.0


def _rising_bars(n=200, end=T0 - 120):
    """Closed 1-minute bars climbing steadily: both %R lines are at new highs."""
    stamps = [end - 60 * (n - 1 - i) for i in range(n)]
    closes = [100.0 + 0.01 * i for i in range(n)]
    return (stamps, [c + 0.005 for c in closes], [c - 0.005 for c in closes], closes)


@pytest.fixture
def logged(monkeypatch):
    rows = []
    monkeypatch.setattr(cp, "log_event", lambda kind, **f: rows.append((kind, f)) or {})
    return rows


def _pos(entry):
    return {"entry_price": entry, "entry_stop_price": entry * 0.95}


def test_fresh_rising_bars_hold_and_log_nothing(monkeypatch, logged):
    b = _rising_bars()
    monkeypatch.setattr(cp, "_double_check_store_bars", lambda t: b)
    px = b[3][-1] + 0.02
    assert cp.trail_exit_trend_hold(_pos(px), "ABC", px, now=T0, cfg=CFG) is True
    assert not [r for r in logged if r[0] == "trail_hold_declined"]


def test_stale_bars_sell_and_log_the_reason(monkeypatch, logged):
    b = _rising_bars(end=T0 - 400)
    monkeypatch.setattr(cp, "_double_check_store_bars", lambda t: b)
    px = b[3][-1] + 0.02
    assert cp.trail_exit_trend_hold(_pos(px), "ABC", px, now=T0, cfg=CFG) is False
    (kind, f), = [r for r in logged if r[0] == "trail_hold_declined"]
    assert f["reason"] == "stale_bars" and f["symbol"] == "ABC" and f["last_bar_age_sec"] == pytest.approx(400.0)


def test_missing_bars_sell_and_log(monkeypatch, logged):
    monkeypatch.setattr(cp, "_double_check_store_bars", lambda t: None)
    assert cp.trail_exit_trend_hold(_pos(50.0), "ABC", 50.0, now=T0, cfg=CFG) is False
    assert [r[1]["reason"] for r in logged if r[0] == "trail_hold_declined"] == ["no_bars"]


def test_green_exit_is_not_logged(monkeypatch, logged):
    b = _rising_bars()
    monkeypatch.setattr(cp, "_double_check_store_bars", lambda t: b)
    assert cp.trail_exit_trend_hold(_pos(100.0), "ABC", 101.0, now=T0, cfg=CFG) is False   # above the band
    assert not logged


def test_keep_held_bars_fresh_enqueues_the_watch_refresh(monkeypatch):
    import ai_entry_watch as ew
    seen = []
    monkeypatch.setattr(ew, "_ob_warm_request", lambda s, now_wall=None: seen.append(s) or True)
    assert cp.keep_held_bars_fresh("CRH") is True and seen == ["CRH"]


def test_keep_held_bars_fresh_never_raises(monkeypatch):
    import ai_entry_watch as ew
    monkeypatch.setattr(ew, "_ob_warm_request", lambda s, now_wall=None: (_ for _ in ()).throw(RuntimeError("x")))
    assert cp.keep_held_bars_fresh("CRH") is False
