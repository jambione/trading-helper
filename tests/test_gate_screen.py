"""A gate screen that double-counts overlapping fires will promote noise.

Shadow writes a row per poll, so a gate that is true for an hour fires
hundreds of times on one name-day. Counting each as an observation would
inflate n by two orders of magnitude and hand any gate a passing sigma.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

gs = pytest.importorskip("gate_screen")


def _bars(n, day="2026-08-20", start_h=14):
    out = []
    for i in range(n):
        out.append({"t": i * 60.0, "day": day,
                    "hm": (start_h + (i // 60), i % 60),
                    "o": 10.0, "h": 10.1, "l": 9.9, "c": 10.0})
    return out


def _row(sym, ts, day="2026-08-20", **kw):
    r = {"symbol": sym, "ts": ts, "_sym": sym, "_day": day, "_ts": ts}
    r.update(kw)
    return r


def test_overlapping_fires_collapse_to_non_overlapping_samples():
    """60 consecutive firing polls over a 15m horizon is not 60 samples."""
    rows = [_row("AAA", float(i * 60)) for i in range(60)]
    bars = {"AAA": _bars(120)}
    out = gs.samples_for(gs.GATES["all"], rows, bars, horizon=15)
    assert len(out) == 4


def test_each_symbol_day_is_counted_independently():
    rows = [_row("AAA", 0.0), _row("BBB", 0.0)]
    bars = {"AAA": _bars(60), "BBB": _bars(60)}
    out = gs.samples_for(gs.GATES["all"], rows, bars, horizon=15)
    assert len(out) == 2


def test_a_gate_that_never_fires_yields_nothing():
    rows = [_row("AAA", float(i * 60), arm_ok=False) for i in range(30)]
    bars = {"AAA": _bars(60)}
    assert gs.samples_for(gs.GATES["arm_ok"], rows, bars, horizon=15) == []


def test_truncated_window_is_dropped_not_padded():
    """A fire with fewer than horizon bars left must not become a sample."""
    rows = [_row("AAA", 50 * 60.0)]
    bars = {"AAA": _bars(60)}
    assert gs.samples_for(gs.GATES["all"], rows, bars, horizon=30) == []


def test_excursion_measures_from_the_fire_instant():
    bars = [{"t": 0.0, "day": "2026-08-20", "hm": (14, 0),
             "o": 100.0, "h": 100.0, "l": 100.0, "c": 100.0},
            {"t": 60.0, "day": "2026-08-20", "hm": (14, 1),
             "o": 100.0, "h": 104.0, "l": 97.0, "c": 103.0}]
    e = gs.excursion_from(bars, "2026-08-20", 60.0, horizon=1)
    assert e["mfe"] == pytest.approx(4.0)
    assert e["mae"] == pytest.approx(3.0)
    assert e["net"] == pytest.approx(3.0)


def test_freshness_gate_uses_admit_ts():
    admit = 1787330000.0
    fresh = _row("AAA", admit + 300.0, admit_ts=admit)    # 5 min after admit
    stale = _row("AAA", admit + 7200.0, admit_ts=admit)   # 2 h after admit
    assert gs.GATES["fresh_5m"](fresh) is True
    assert gs.GATES["fresh_5m"](stale) is False
    assert gs.GATES["stale_60m"](stale) is True
    assert gs.GATES["stale_60m"](fresh) is False


def test_freshness_gate_is_closed_when_admit_ts_is_missing():
    """No admit_ts must not read as 'fresh' — fail closed.

    0.0 counts as missing: load_shadow_universe uses it as the
    no-constraint sentinel for file universes, so it is never a real
    admission instant.
    """
    assert gs.GATES["fresh_5m"](_row("AAA", 300.0)) is False
    assert gs.GATES["stale_60m"](_row("AAA", 300.0)) is False
    assert gs.GATES["fresh_5m"](_row("AAA", 300.0, admit_ts=0.0)) is False


def test_rvol_gates_treat_missing_as_zero():
    assert gs.GATES["rvol_3"](_row("AAA", 0.0, rvol=5.0)) is True
    assert gs.GATES["rvol_3"](_row("AAA", 0.0, rvol=1.0)) is False
    assert gs.GATES["rvol_3"](_row("AAA", 0.0)) is False


def test_arm_ok_is_available_as_the_incumbent_gate():
    """Every candidate is measured against what the desk runs today."""
    assert "arm_ok" in gs.GATES
    assert gs.GATES["arm_ok"](_row("AAA", 0.0, arm_ok=True)) is True
