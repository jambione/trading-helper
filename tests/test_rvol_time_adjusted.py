"""T2.1 — time-adjusted RVOL. Pure math only; never touches the network.

Context for the fixed numbers below: `expected_fraction` interpolates a
U-shaped cumulative volume curve keyed on minutes since the 9:30 ET open, and
`CURVE` ends at (390, 1.0) — 390 minutes after 9:30 is exactly 16:00.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools.morning_funnel import (  # noqa: E402
    OPEN_MIN,
    expected_fraction,
    rvol_pair,
)

AVG = 1_000_000.0


def _mins(hh: int, mm: int) -> float:
    """Minutes since the 9:30 ET open for a wall-clock ET time."""
    return (hh * 60 + mm) - OPEN_MIN


# ── the curve itself ─────────────────────────────────────────────────────────

def test_curve_anchors():
    assert expected_fraction(_mins(9, 30)) == 0.05
    assert round(expected_fraction(_mins(9, 45)), 2) == 0.13
    assert expected_fraction(_mins(16, 0)) == 1.0


def test_curve_is_monotonic_through_the_session():
    prev = -1.0
    for m in range(0, 391, 5):
        f = expected_fraction(m)
        assert f >= prev, f"curve dipped at {m} min"
        prev = f


# ── the headline cases from the ticket ───────────────────────────────────────

def test_at_0945_thirteen_percent_of_average_is_normal_pace():
    """13% of an average day done 15 min in is exactly on pace -> rvol ~1."""
    rvol, raw = rvol_pair(0.13 * AVG, AVG, _mins(9, 45))
    assert round(rvol, 2) == 1.0
    # The naive full-day ratio is what made this misleading: same stock,
    # trading exactly normally, looked like it was nearly dead.
    assert round(raw, 2) == 0.13


def test_at_0945_five_times_pace_reads_as_five_times():
    rvol, _ = rvol_pair(5 * 0.13 * AVG, AVG, _mins(9, 45))
    assert 4.8 <= rvol <= 5.2


def test_at_1600_adjusted_equals_raw():
    rvol, raw = rvol_pair(0.9 * AVG, AVG, _mins(16, 0))
    assert round(rvol, 4) == round(raw, 4) == 0.9


def test_lunch_lull_is_not_read_as_weakness():
    """Midday the curve is flat, so an ordinary stock still reads ~1."""
    rvol, _ = rvol_pair(expected_fraction(_mins(12, 0)) * AVG, AVG,
                        _mins(12, 0))
    assert round(rvol, 2) == 1.0


# ── pre-market: the tranche windows ──────────────────────────────────────────

def test_premarket_does_not_divide_by_zero_or_explode():
    for hh, mm in ((4, 0), (7, 0), (8, 30), (9, 29)):
        rvol, raw = rvol_pair(1000.0, AVG, _mins(hh, mm))
        assert rvol is not None and raw is not None
        assert rvol > 0
        assert rvol < 1e6, f"absurd rvol at {hh}:{mm:02d}"


def test_premarket_floor_on_the_divisor():
    """The ramp floors at 1% so 04:00 cannot produce an infinite pace."""
    assert expected_fraction(_mins(4, 0)) >= 0.01


def test_at_0700_a_normal_premarket_pace_reads_about_one():
    """This is the tranche-1 window. A stock that has done its usual share of
    volume by 07:00 must read ~1x, not ~37x."""
    frac = expected_fraction(_mins(7, 0))
    rvol, _ = rvol_pair(frac * AVG, AVG, _mins(7, 0))
    assert round(rvol, 2) == 1.0


def test_at_0830_a_genuine_premarket_surge_is_visible():
    frac = expected_fraction(_mins(8, 30))
    rvol, _ = rvol_pair(8 * frac * AVG, AVG, _mins(8, 30))
    assert 7.5 <= rvol <= 8.5


def test_yesterdays_full_volume_premarket_would_have_been_absurd():
    """Regression guard on the actual defect. yfinance fast_info.last_volume
    is the last DAILY bar, so pre-market it is yesterday's completed total.
    Feeding a full day's volume into the 07:00 divisor reports a wild pace for
    a perfectly ordinary stock — which is why the source had to change, and
    why flooring the divisor alone could never fix it."""
    rvol, _ = rvol_pair(AVG, AVG, _mins(7, 0))     # a whole day's volume
    assert rvol > 30, "if this is small, the curve changed; revisit the note"


# ── refusing to answer ───────────────────────────────────────────────────────

def test_no_volume_yet_returns_none_not_zero():
    assert rvol_pair(0, AVG, _mins(9, 45)) == (None, None)
    assert rvol_pair(None, AVG, _mins(9, 45)) == (None, None)


def test_no_average_returns_none():
    """A brand-new listing has no history to compare against. None, not 0.0x."""
    assert rvol_pair(500_000, 0, _mins(9, 45)) == (None, None)
    assert rvol_pair(500_000, None, _mins(9, 45)) == (None, None)


def test_non_numeric_inputs_return_none():
    assert rvol_pair("junk", AVG, 10) == (None, None)
    assert rvol_pair(500_000, "junk", 10) == (None, None)


def test_negative_volume_returns_none():
    assert rvol_pair(-5, AVG, 10) == (None, None)


# ── the flag ─────────────────────────────────────────────────────────────────

def test_time_adjust_off_restores_the_naive_ratio():
    rvol, raw = rvol_pair(0.13 * AVG, AVG, _mins(9, 45), time_adjusted=False)
    assert rvol == raw == 0.13


def test_raw_is_always_the_naive_ratio_regardless_of_time():
    for hh, mm in ((7, 0), (9, 45), (12, 0), (16, 0)):
        _, raw = rvol_pair(0.5 * AVG, AVG, _mins(hh, mm))
        assert round(raw, 4) == 0.5


def test_adjusted_is_never_below_raw_during_the_session():
    """expected_fraction <= 1 always, so the adjustment can only raise pace."""
    for m in range(0, 391, 15):
        rvol, raw = rvol_pair(0.2 * AVG, AVG, m)
        assert rvol >= raw - 1e-9


# ── average-volume cache under a churning watchlist ──────────────────────────

class _FakeFunnel:
    """Stand-in for tools.morning_funnel: counts daily-bar fetches."""

    OPEN_MIN = OPEN_MIN

    def __init__(self, frames):
        self.frames = frames
        self.fetches = []

    def fetch_daily(self, client, tickers, cfg):
        self.fetches.append(list(tickers))
        return {t: self.frames[t] for t in tickers if t in self.frames}

    def knobs_from_cfg(self, cfg):
        return {"avg_days": 10}

    @staticmethod
    def _et_index(df):
        return df.index


def _daily(n_sessions, volume=AVG, today="2026-07-24"):
    import pandas as pd
    end = pd.Timestamp(today) - pd.Timedelta(days=1)
    idx = pd.DatetimeIndex([end - pd.Timedelta(days=d)
                            for d in range(n_sessions - 1, -1, -1)])
    return pd.DataFrame({"volume": [volume] * n_sessions}, index=idx)


def _reset_cache(d):
    d._VOL_AVG_CACHE.clear()
    d._VOL_AVG_DATE = ""


def test_average_volume_is_fetched_once_per_symbol_per_day():
    import dashboard as d
    _reset_cache(d)
    mf = _FakeFunnel({"AAAA": _daily(20)})
    for _ in range(4):
        d._vol_avg_volumes(mf, object(), ["AAAA"], {}, "2026-07-24")
    assert len(mf.fetches) == 1
    assert d._VOL_AVG_CACHE["AAAA"] == AVG


def test_a_symbol_with_no_history_is_not_refetched_every_loop():
    """The watchlist is different names every day and turns over intraday, so
    a fresh listing with under five completed sessions is routine. Without a
    negative cache entry it would be re-requested every 60s all session."""
    import dashboard as d
    _reset_cache(d)
    mf = _FakeFunnel({"NEWCO": _daily(2)})          # only 2 completed sessions
    for _ in range(5):
        d._vol_avg_volumes(mf, object(), ["NEWCO"], {}, "2026-07-24")
    assert len(mf.fetches) == 1, mf.fetches
    assert d._VOL_AVG_CACHE["NEWCO"] is None


def test_a_symbol_absent_from_the_response_is_also_negatively_cached():
    import dashboard as d
    _reset_cache(d)
    mf = _FakeFunnel({})                            # provider returns nothing
    for _ in range(3):
        d._vol_avg_volumes(mf, object(), ["GHOST"], {}, "2026-07-24")
    assert len(mf.fetches) == 1
    assert d._VOL_AVG_CACHE["GHOST"] is None


def test_only_newly_seen_symbols_are_fetched_as_the_list_churns():
    import dashboard as d
    _reset_cache(d)
    mf = _FakeFunnel({s: _daily(20) for s in ("AAAA", "BBBB", "CCCC")})
    d._vol_avg_volumes(mf, object(), ["AAAA", "BBBB"], {}, "2026-07-24")
    d._vol_avg_volumes(mf, object(), ["BBBB", "CCCC"], {}, "2026-07-24")
    assert mf.fetches == [["AAAA", "BBBB"], ["CCCC"]]


def test_cache_resets_on_a_new_session_date():
    import dashboard as d
    _reset_cache(d)
    mf = _FakeFunnel({"AAAA": _daily(20)})
    d._vol_avg_volumes(mf, object(), ["AAAA"], {}, "2026-07-24")
    d._vol_avg_volumes(mf, object(), ["AAAA"], {}, "2026-07-27")
    assert len(mf.fetches) == 2


def test_an_all_halted_history_caches_none_not_zero():
    """A zero average would make rvol_pair divide by zero; it returns None."""
    import dashboard as d
    _reset_cache(d)
    mf = _FakeFunnel({"HALT": _daily(20, volume=0.0)})
    d._vol_avg_volumes(mf, object(), ["HALT"], {}, "2026-07-24")
    assert d._VOL_AVG_CACHE["HALT"] is None
    assert rvol_pair(500_000, None, 10) == (None, None)
