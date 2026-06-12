"""
test_strategy_three_indicator.py — offline tests for the 3-indicator strategy.

These run with NO network and NO Alpaca credentials: they build synthetic bar
series engineered to exercise each rule, then assert the signals and the
backtest runner behave. This lets you validate the logic immediately, before
pointing it at real history.

Run:
    venv/bin/python -m pytest tests/test_strategy_three_indicator.py -q
"""

import os
import sys

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "tools"))  # backtest_3ind now lives under tools/

import strategy_three_indicator as strat   # noqa: E402
import backtest_3ind                        # noqa: E402


# ── Synthetic series builders ─────────────────────────────────────────────────

def _frame(closes: np.ndarray) -> pd.DataFrame:
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    idx = pd.date_range("2024-01-01 09:30", periods=n, freq="1min")
    return pd.DataFrame({
        "open":  closes,
        "high":  closes + 0.2,
        "low":   closes - 0.2,
        "close": closes,
        "volume": np.full(n, 1000.0),
        "time":  idx.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }, index=idx)


def _sine(n=600, period=80, amp=10.0, base=50.0, noise=0.05, seed=1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    return base + amp * np.sin(2 * np.pi * t / period) + rng.normal(0, noise, n)


# Looser separation gate for trigger tests — we're testing alignment logic,
# not the magnitude threshold (which has its own dedicated test).
LOOSE = strat.params(macd_sep_mult=0.4, confirm_window=12)


# ── Indicator plumbing ────────────────────────────────────────────────────────

def test_compute_indicators_has_all_columns():
    df = strat.compute_indicators(_frame(_sine()), LOOSE)
    for col in ("cm_rsi", "s_percentR", "l_percentR", "macd_line",
                "macd_signal_line", "macd_hist", "macd_bull", "macd_bear",
                "macd_hist_std"):
        assert col in df.columns


# ── BUY fires on an upswing out of oversold ───────────────────────────────────

def test_buy_fires_on_oscillating_upswings():
    a = strat.to_arrays(strat.compute_indicators(_frame(_sine()), LOOSE))
    n = len(a["close"])
    fired = [i for i in range(n - 1) if strat.buy_signal(a, i, LOOSE)]
    assert fired, "expected at least one BUY across multiple sine upswings"
    # Every buy should sit where MACD is genuinely bullish (sanity on the gate)
    for i in fired:
        assert a["macd_line"][i] > a["macd_signal"][i]


def test_no_buys_in_pure_downtrend():
    # Monotonic decline → no bullish MACD cross → never buy.
    a = strat.to_arrays(strat.compute_indicators(
        _frame(np.linspace(100, 40, 500)), LOOSE))
    n = len(a["close"])
    assert not any(strat.buy_signal(a, i, LOOSE) for i in range(n - 1))


# ── SELL fires on a rollover from overbought ──────────────────────────────────

def test_sell_fires_on_oscillating_downswings():
    a = strat.to_arrays(strat.compute_indicators(_frame(_sine()), LOOSE))
    n = len(a["close"])
    assert any(strat.sell_signal(a, i, LOOSE) for i in range(n - 1))


def test_exit_all_is_subset_of_any():
    # "all" mode requires every reversal signal, so it can only fire on a subset
    # of the bars where "any" fires.
    df = strat.compute_indicators(_frame(_sine()), LOOSE)
    a = strat.to_arrays(df)
    p_any = {**LOOSE, "exit_mode": "any"}
    p_all = {**LOOSE, "exit_mode": "all"}
    n = len(a["close"])
    any_bars = {i for i in range(n - 1) if strat.sell_signal(a, i, p_any)}
    all_bars = {i for i in range(n - 1) if strat.sell_signal(a, i, p_all)}
    assert all_bars.issubset(any_bars)
    assert len(all_bars) <= len(any_bars)


# ── The "wide separation" gate actually gates ─────────────────────────────────

def test_separation_gate_blocks_weak_crosses():
    df = strat.compute_indicators(_frame(_sine()), None)
    a = strat.to_arrays(df)
    n = len(a["close"])
    strict = strat.params(macd_sep_mult=5.0, confirm_window=12)   # absurdly wide req
    loose  = strat.params(macd_sep_mult=0.1, confirm_window=12)
    strict_buys = sum(strat.buy_signal(a, i, strict) for i in range(n - 1))
    loose_buys  = sum(strat.buy_signal(a, i, loose) for i in range(n - 1))
    assert strict_buys <= loose_buys      # higher separation bar ⇒ fewer/equal buys


# ── End-to-end backtest runner (no network) ───────────────────────────────────

def test_simulate_produces_round_trips():
    df = _frame(_sine(n=800, period=90))
    trades = backtest_3ind.simulate(df, LOOSE, slippage_bps=10.0)
    assert len(trades) >= 1
    for t in trades:
        # Round-trip integrity
        assert t["sell_bar"] > t["buy_bar"]
        assert t["buy_price"] > 0 and t["sell_price"] > 0
        assert t["reason"] in {"reversal", "stop_loss", "take_profit", "end_of_data"}
        # pnl_pct matches the recorded fills
        expected = (t["sell_price"] - t["buy_price"]) / t["buy_price"] * 100
        assert abs(expected - t["pnl_pct"]) < 0.02


def test_evaluate_state_breakdown_matches_signals():
    a = strat.to_arrays(strat.compute_indicators(_frame(_sine()), LOOSE))
    n = len(a["close"])
    for i in range(n - 1):
        st = strat.evaluate_state(a, i, LOOSE)
        # buy/sell in the breakdown must equal the authoritative signal functions
        assert st["buy"] == strat.buy_signal(a, i, LOOSE)
        assert st["sell"] == strat.sell_signal(a, i, LOOSE)
        # buy_pct is the fraction of the three conditions met
        expected = round((int(st["cm_ok"]) + int(st["pctr_ok"]) + int(st["macd_ok"])) / 3 * 100)
        assert st["buy_pct"] == expected
        # a real buy means all three conditions are met
        if st["buy"]:
            assert st["cm_ok"] and st["pctr_ok"] and st["macd_ok"]
            assert st["buy_pct"] == 100


def test_evaluate_state_is_json_safe():
    import json
    a = strat.to_arrays(strat.compute_indicators(_frame(_sine()), LOOSE))
    st = strat.evaluate_state(a, len(a["close"]) - 2, LOOSE)
    json.dumps(st)   # must not raise (no numpy bools/floats leaking)


def test_stop_loss_caps_downside():
    # A series that rips up (triggering a buy) then collapses should exit via the
    # protective stop rather than riding all the way down.
    up   = np.linspace(40, 60, 200)
    down = np.linspace(60, 30, 200)
    df = _frame(np.concatenate([_sine(n=200), up, down]))
    trades = backtest_3ind.simulate(df, LOOSE, slippage_bps=10.0,
                                    stop_loss_pct=2.0)
    stops = [t for t in trades if t["reason"] == "stop_loss"]
    # If any stop fired, its loss is bounded near -2% (plus slippage)
    for t in stops:
        assert t["pnl_pct"] <= 0
        assert t["pnl_pct"] > -4.0
