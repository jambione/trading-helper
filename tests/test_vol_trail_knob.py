"""ai_local_trail_give_vol_k: the trail cushion floored at k x 1m volatility.

Off (k = 0) must leave the shipped cushion exactly as it was. On, it widens
the cushion on volatile names, including past the percent ceiling, and does
nothing when the entry had no volatility reading.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ai_positions = pytest.importorskip("ai_positions")
ew = pytest.importorskip("ai_entry_watch")

BASE = {"ai_local_trail_give_r": 0.10, "ai_local_trail_give_max_pct": 1.0,
        "ai_local_trail_min_give_px": 0.0}


def _give(vol_pct, **cfg):
    return ai_positions.local_trail_give(
        last=10.0, risk=0.50, cfg={**BASE, **cfg}, mfe_r=0.0, vol_pct=vol_pct)


def test_off_by_default_is_unchanged():
    assert _give(0.5) == _give(None) == pytest.approx(0.05)


def test_floor_widens_volatile_names_past_the_ceiling():
    # 4 x 0.5% x $10 = $0.20, above both give_r x R ($0.05) and 1% ($0.10).
    assert _give(0.5, ai_local_trail_give_vol_k=4) == pytest.approx(0.20)


def test_floor_never_tightens():
    assert _give(0.05, ai_local_trail_give_vol_k=4) == pytest.approx(0.05)


def test_no_reading_means_no_floor():
    assert _give(None, ai_local_trail_give_vol_k=4) == pytest.approx(0.05)


def test_pos_vol_pct_reads_entry_features():
    assert ai_positions._pos_vol_pct({"features": {"vol_1m_pct": 0.3}}) == 0.3
    assert ai_positions._pos_vol_pct({"features": {"vol_1m_pct": None}}) is None
    assert ai_positions._pos_vol_pct({}) is None


def _seed(sym, closes, t0, step=60.0):
    rows = [(c, c, c) for c in closes]
    stamps = [t0 + i * step for i in range(len(closes))]
    with ew._ohlc_cache_lock:
        ew._ohlc_cache[sym] = (stamps[-1], rows)
        ew._ohlc_ts_cache[sym] = (stamps[-1], stamps)
    return stamps[-1]


def test_vol_1m_pct_consecutive_minutes():
    closes = [10.0, 10.1] * 8  # 16 closes, 15 alternating returns
    last = _seed("VOLT", closes, 1_000_000.0)
    v = ew._vol_1m_pct("VOLT", last + 60)
    assert v is not None and v > 0.9


def test_vol_1m_pct_refuses_gaps_and_stale_bars():
    closes = [10.0, 10.1] * 8
    last = _seed("GAPT", closes, 1_000_000.0, step=120.0)
    assert ew._vol_1m_pct("GAPT", last + 60) is None
    last = _seed("OLDT", closes, 1_000_000.0)
    assert ew._vol_1m_pct("OLDT", last + 3600) is None
    assert ew._vol_1m_pct("NONE", last) is None
