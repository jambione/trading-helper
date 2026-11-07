"""11/07: two default-off switches narrow trail_exit_trend_hold (initial working-stop floor; off after the trade peaked)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ai_positions as cp  # noqa: E402
from tools import sell_double_check as sdc  # noqa: E402

BASE = {"ai_trail_exit_trend_hold": True, "ai_trail_exit_trend_hold_band_pct": 0.05, "ai_trail_exit_trend_hold_max_bar_age_sec": 0}


def _setup(monkeypatch):
    monkeypatch.setattr(cp, "_double_check_store_bars", lambda t: ([0.0], [1.0], [1.0], [1.0]))
    monkeypatch.setattr(sdc, "evaluate_symbol", lambda *a, **k: {"sell": "no", "trend": "up"})


def pos(**kw):
    p = {"entry_price": 10.0, "entry_stop_price": 9.5, "local_stop_initial": 9.94, "peak_price": 10.0}
    p.update(kw)
    return p


def test_default_holds_as_before(monkeypatch):
    _setup(monkeypatch)
    assert cp.trail_exit_trend_hold(pos(), "X", 9.90, now=1.0, cfg=dict(BASE)) is True        # below the working stop, above the 5% stop: held (old behaviour)
    assert cp.trail_exit_trend_hold(pos(peak_price=10.03), "X", 10.0, now=1.0, cfg=dict(BASE)) is True


def test_floor_switch_never_holds_at_or_below_the_initial_working_stop(monkeypatch):
    _setup(monkeypatch)
    cfg = {**BASE, "ai_trail_exit_trend_hold_floor_initial": True}
    assert cp.trail_exit_trend_hold(pos(), "X", 9.94, now=1.0, cfg=cfg) is False
    assert cp.trail_exit_trend_hold(pos(), "X", 9.90, now=1.0, cfg=cfg) is False
    assert cp.trail_exit_trend_hold(pos(), "X", 9.99, now=1.0, cfg=cfg) is True               # near entry: still held


def test_after_peak_switch_takes_the_exit_once_the_trade_ran_up(monkeypatch):
    _setup(monkeypatch)
    cfg = {**BASE, "ai_trail_exit_trend_hold_off_after_peak_bp": 15.0}
    assert cp.trail_exit_trend_hold(pos(peak_price=10.016), "X", 10.0, now=1.0, cfg=cfg) is False   # peaked +16 bp: breakeven exit taken
    assert cp.trail_exit_trend_hold(pos(peak_price=10.010), "X", 10.0, now=1.0, cfg=cfg) is True    # peaked +10 bp: still held
