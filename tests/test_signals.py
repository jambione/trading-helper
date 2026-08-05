"""
test_signals.py — Unit tests for the indicator math in signals.py.

These functions feed the live signal engine (and, in live mode, real Alpaca
orders), so the goal here is to pin their *behavioural* properties — sign,
bounds, direction, edge cases — rather than exact float values that would be
brittle to refactors.

Run:
    venv/bin/python -m pytest tests/test_signals.py -q
"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import signals  # noqa: E402


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _ohlc(closes, highs=None, lows=None, vols=None) -> pd.DataFrame:
    """Build a minimal OHLCV frame from a close series."""
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    highs = closes + 1.0 if highs is None else np.asarray(highs, dtype=float)
    lows  = closes - 1.0 if lows  is None else np.asarray(lows,  dtype=float)
    vols  = np.full(n, 1000.0) if vols is None else np.asarray(vols, dtype=float)
    idx = pd.date_range("2024-01-01 09:30", periods=n, freq="1min")
    return pd.DataFrame(
        {"open": closes, "high": highs, "low": lows, "close": closes, "volume": vols},
        index=idx,
    )


DEFAULT_CFG = {
    "cm_rsi_length": 2, "cm_rsi_oversold": 25,
    "obv_length": 20, "rte_threshold": 20, "rte_min_boxes": 2,
    "macd_fast": 12, "macd_slow": 26, "macd_signal": 9,
    "volume_surge_mult": 1.5, "wr_length": 14,
    "ema_short": 8, "ema_long": 21, "strategy": "multiple_os",
}


# ── EMA / SMA ─────────────────────────────────────────────────────────────────

def test_ema_of_constant_is_constant():
    s = pd.Series([5.0] * 50)
    out = signals.ema(s, 10)
    assert np.allclose(out.values, 5.0)


def test_sma_matches_window_mean():
    s = pd.Series([1, 2, 3, 4, 5], dtype=float)
    out = signals.sma(s, 3)
    assert out.iloc[2] == pytest.approx(2.0)   # mean(1,2,3)
    assert out.iloc[4] == pytest.approx(4.0)   # mean(3,4,5)
    assert pd.isna(out.iloc[0])                 # not enough data yet


# ── RSI ───────────────────────────────────────────────────────────────────────

def test_rsi_bounds():
    rng = np.random.default_rng(0)
    s = pd.Series(np.cumsum(rng.standard_normal(200)) + 100)
    out = signals.rsi(s, 14).dropna()
    assert (out >= 0).all() and (out <= 100).all()


def test_rsi_uptrend_with_minor_dips_is_high():
    # Mostly rising with small periodic dips so avg_loss > 0 (avoids the
    # pure-uptrend NaN edge pinned below). Should read strongly overbought.
    vals = np.arange(1, 121, dtype=float)
    vals[10::10] -= 2.0                     # real down-bars (step is +1, dip −2)
    out = signals.rsi(pd.Series(vals), 14)
    assert out.iloc[-1] > 90


def test_rsi_strict_downtrend_is_low():
    s = pd.Series(np.arange(100, 0, -1, dtype=float))  # monotonically falling
    out = signals.rsi(s, 14)
    assert out.iloc[-1] == pytest.approx(0.0)


def test_rsi_pure_uptrend_is_nan_edge():
    # Documented quirk: with zero losses the implementation divides by NaN
    # (avg_loss.replace(0, np.nan)) and returns NaN rather than 100. Pinned so
    # a future refactor that changes this is a conscious decision.
    s = pd.Series(np.arange(1, 101, dtype=float))
    assert pd.isna(signals.rsi(s, 14).iloc[-1])


# ── Williams %R ───────────────────────────────────────────────────────────────

def test_williams_pr_bounds_and_extremes():
    df = _ohlc(np.linspace(10, 20, 60))
    pr = signals.williams_pr(df["high"], df["low"], df["close"], 14).dropna()
    # %R lives in [-100, 0]
    assert (pr >= -100.001).all() and (pr <= 0.001).all()
    # Close at the very top of the range → near 0
    assert pr.iloc[-1] > -50


# ── ATR ───────────────────────────────────────────────────────────────────────

def test_atr_positive_and_tracks_range():
    df = _ohlc(np.linspace(10, 20, 60), highs=np.linspace(11, 22, 60),
               lows=np.linspace(9, 18, 60))
    a = signals.atr(df, 14).dropna()
    assert (a > 0).all()


def test_atr_constant_range_converges():
    # high-low fixed at 2.0, no gaps → ATR should converge to ~2.0
    closes = np.full(80, 10.0)
    df = _ohlc(closes, highs=closes + 1.0, lows=closes - 1.0)
    a = signals.atr(df, 14)
    assert a.iloc[-1] == pytest.approx(2.0, abs=1e-6)


# ── MACD ──────────────────────────────────────────────────────────────────────

def test_macd_histogram_positive_in_uptrend():
    df = _ohlc(np.linspace(10, 50, 120))
    out = signals.compute_macd(df, DEFAULT_CFG)
    # In a sustained uptrend the MACD line leads its signal → hist > 0
    assert out["macd_hist"].iloc[-1] > 0


def test_macd_histogram_negative_in_downtrend():
    df = _ohlc(np.linspace(50, 10, 120))
    out = signals.compute_macd(df, DEFAULT_CFG)
    assert out["macd_hist"].iloc[-1] < 0


def test_macd_line_is_fast_minus_slow():
    df = _ohlc(np.linspace(10, 50, 120))
    out = signals.compute_macd(df, DEFAULT_CFG)
    fast = signals.ema(df["close"], DEFAULT_CFG["macd_fast"])
    slow = signals.ema(df["close"], DEFAULT_CFG["macd_slow"])
    assert np.allclose((fast - slow).values, out["macd_line"].values)


# ── OBV oscillator ────────────────────────────────────────────────────────────

def test_obv_rises_when_price_and_volume_up():
    df = _ohlc(np.arange(1, 81, dtype=float))   # every bar closes higher
    out = signals.compute_obv_oscillator(df, DEFAULT_CFG)
    # Raw OBV must be monotonically increasing when every close is an up-bar
    assert out["obv"].is_monotonic_increasing
    assert bool(out["obv_above_zero"].iloc[-1])


# ── VWAP ──────────────────────────────────────────────────────────────────────

def test_vwap_within_price_range():
    df = _ohlc(np.linspace(10, 20, 60))
    vw = signals.vwap_calc(df).dropna()
    assert (vw >= df["low"].min() - 1).all()
    assert (vw <= df["high"].max() + 1).all()


def test_vwap_constant_price_equals_price():
    df = _ohlc(np.full(30, 15.0), highs=np.full(30, 15.0), lows=np.full(30, 15.0))
    vw = signals.vwap_calc(df)
    assert np.allclose(vw.values, 15.0)


# ── compute_signals end-to-end smoke ─────────────────────────────────────────

def test_compute_signals_runs_and_labels():
    rng = np.random.default_rng(7)
    closes = np.cumsum(rng.standard_normal(300)) + 100
    df = _ohlc(closes, highs=closes + 0.5, lows=closes - 0.5,
               vols=rng.uniform(500, 5000, 300))
    out = signals.compute_signals(df, DEFAULT_CFG)
    # Always emits a signal column with only valid labels
    assert "signal" in out.columns
    assert set(out["signal"].unique()).issubset({"BUY", "HOLD"})
    # Core columns the engine reads downstream exist. (Note: compute_signals
    # does NOT compute MACD — the engine derives the histogram separately.)
    for col in ("rsi", "atr", "rvol", "ema_short", "ema_long", "wr",
                "cm_rsi", "obv_trending_up"):
        assert col in out.columns


def test_compute_signals_does_not_mutate_input():
    df = _ohlc(np.linspace(10, 30, 250))
    before = df.copy()
    signals.compute_signals(df, DEFAULT_CFG)
    pd.testing.assert_frame_equal(df, before)


# ── exit signal composition ──────────────────────────────────────────────────

def _exit_arrays(*, macd_down, cm_high_falling, pctr_exhausted_falling):
    """Minimal arrays that switch each reversal condition on/off independently."""
    import numpy as np
    n = 20
    a = {
        "macd_bear": np.array([macd_down] * n, dtype=bool),
        "cm_rsi": np.full(n, 95.0 if cm_high_falling else 50.0),
        "s_percentR": np.full(n, -5.0 if pctr_exhausted_falling else -60.0),
    }
    if cm_high_falling:
        a["cm_rsi"] = np.linspace(99.0, 91.0, n)      # >90 and falling
    if pctr_exhausted_falling:
        # Must still be above rte_sell_from (-10) *inside* the 8-bar window and
        # falling — that is what "pinned near the top, then rolled over" means.
        a["s_percentR"] = np.linspace(-1.0, -12.0, n)
    return a


def test_macd_alone_no_longer_closes_a_position():
    """exit_mode="any" over all three let a lone bearish MACD cross exit a trade
    that CM RSI-2 and %R both still called healthy."""
    import strategy_three_indicator as s

    p = s.params()
    a = _exit_arrays(macd_down=True, cm_high_falling=False,
                     pctr_exhausted_falling=False)
    assert s.sell_signal(a, 19, p) is False


def test_rsi_or_percent_r_each_close_a_position():
    import strategy_three_indicator as s

    p = s.params()
    only_rsi = _exit_arrays(macd_down=False, cm_high_falling=True,
                            pctr_exhausted_falling=False)
    assert s.sell_signal(only_rsi, 19, p) is True

    only_pctr = _exit_arrays(macd_down=False, cm_high_falling=False,
                             pctr_exhausted_falling=True)
    assert s.sell_signal(only_pctr, 19, p) is True


def test_macd_can_be_put_back_in_the_exit_set():
    import strategy_three_indicator as s

    p = s.params(exit_signals=("macd", "cm", "rte"))
    a = _exit_arrays(macd_down=True, cm_high_falling=False,
                     pctr_exhausted_falling=False)
    assert s.sell_signal(a, 19, p) is True
