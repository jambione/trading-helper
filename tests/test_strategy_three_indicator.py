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

def _exhausted_then_recovering(n_down=400, n_up=400, seed=2) -> np.ndarray:
    """A sustained decline into exhaustion, then a recovery.

    A pure sine cannot exercise the entry any more, and that is the gate
    working rather than a bug: %R exhaustion on the LONG scale asks whether a
    move is spent, and a clean oscillator has no spent move — on _sine(800, 90)
    the slow line bottoms at -78.1 and never reaches the -80 band. The strategy
    is a pullback-in-trend entry, so the fixture has to contain a trend.
    """
    rng = np.random.default_rng(seed)
    decline = np.linspace(60, 42, n_down)
    recover = (42 + 8 * (1 - np.cos(2 * np.pi * np.arange(n_up) / 120)) / 2
               + rng.normal(0, 0.06, n_up))
    return np.concatenate([decline, recover])


def test_simulate_produces_round_trips():
    df = _frame(_exhausted_then_recovering())
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


# ── RSI-2 buy/sell levels: dip-then-turn, peak-then-roll ─────────────────────

def test_rsi_dip_and_turn_are_sequential_not_same_bar():
    """RSI-2 is fast: while it is under 30 it is usually still falling, and once
    it turns it clears 30 within a bar or two. Requiring "<30 AND rising" on the
    SAME bar produced 0 buys on this fixture despite 288 bars printing under 30.
    """
    import numpy as np
    p = strat.params(macd_sep_mult=0.4, confirm_window=12, cm_rsi_buy_max=30.0)
    a = strat.to_arrays(strat.compute_indicators(_frame(_sine()), p))

    rsi = a["cm_rsi"][np.isfinite(a["cm_rsi"])]
    assert (rsi < 30).sum() > 0, "fixture must actually reach oversold"

    n = len(a["close"])
    fired = [i for i in range(n - 1) if strat.buy_signal(a, i, p)]
    assert fired, "a dip below 30 followed by a turn up must be able to buy"


def test_published_cm_ok_matches_the_buy_rule():
    """evaluate_state's cm_ok / pctr_ok still mirror buy_signal's filter legs.

    macd_ok is now *state* (bullish + separation), while buy_signal still
    requires a recent macd_cross event. So all-three-ok no longer implies buy —
    only the cm/pctr legs stay locked to the buy rule here.
    """
    p = strat.params(macd_sep_mult=0.4, confirm_window=12, cm_rsi_buy_max=30.0)
    a = strat.to_arrays(strat.compute_indicators(_frame(_sine()), p))
    for i in range(30, len(a["close"]) - 1):
        st = strat.evaluate_state(a, i, p)
        # A real buy still needs all three published ok flags.
        if strat.buy_signal(a, i, p):
            assert st["cm_ok"] and st["pctr_ok"] and st["macd_ok"], (
                f"buy at {i} but state legs incomplete"
            )


def test_macd_ok_is_state_not_cross_event():
    """macd_ok stays true mid-move after the cross window expires.

    ASST-class: bullish with enough hist/sep must publish macd_ok even when
    macd_cross is False. Cross remains its own ranking/preference flag.
    """
    import numpy as np
    p = strat.params(macd_sep_mult=0.5, macd_min_gap=0.005, confirm_window=3)
    # Long enough for MACD + hist std; a sustained uptrend keeps line>signal
    # with wide hist long after any early cross falls outside confirm_window.
    closes = np.concatenate([
        np.linspace(50, 40, 80),   # decline to set up a cross later
        np.linspace(40, 70, 120),  # strong sustained rally
    ])
    a = strat.to_arrays(strat.compute_indicators(_frame(closes), p))
    found_ok_without_cross = False
    for i in range(60, len(a["close"]) - 1):
        st = strat.evaluate_state(a, i, p)
        if st["macd_ok"]:
            assert st["macd_bull"], f"macd_ok without bullish state at {i}"
            assert st["macd_gap"] is not None and st["macd_gap"] >= p["macd_min_gap"]
            if not st["macd_cross"]:
                found_ok_without_cross = True
                break
    assert found_ok_without_cross, (
        "expected at least one bar with macd_ok True and macd_cross False"
    )


def test_macd_cross_still_exported_separately():
    """Cross event flag is still computed; it just does not gate macd_ok."""
    p = strat.params(macd_sep_mult=0.4, confirm_window=12)
    a = strat.to_arrays(strat.compute_indicators(_frame(_sine()), p))
    saw_cross = False
    for i in range(40, len(a["close"]) - 1):
        st = strat.evaluate_state(a, i, p)
        assert "macd_cross" in st and "macd_ok" in st
        if st["macd_cross"]:
            saw_cross = True
            # A published cross still implies current bullish state.
            assert st["macd_bull"]
    assert saw_cross, "fixture should still produce some macd_cross events"


def test_sell_needs_a_peak_above_the_level_first():
    """Straight after a buy at RSI<30 the reading is already under 90; a bare
    'below the level' test would exit on the very next bar."""
    import numpy as np
    p = strat.params(exit_signals=("cm",), cm_rsi_sell_min=90.0)
    n = 20

    never_peaked = {"macd_bear": np.zeros(n, bool),
                    "cm_rsi": np.linspace(60.0, 40.0, n),   # falling, never >90
                    "s_percentR": np.full(n, -60.0)}
    assert strat.sell_signal(never_peaked, 19, p) is False

    # The peak must land INSIDE the confirm_window (the last 8 bars), not
    # merely somewhere in the array — the window is what sell_signal scans.
    peaked_then_rolled = {"macd_bear": np.zeros(n, bool),
                          "cm_rsi": np.linspace(99.0, 91.0, n),  # >90 then down
                          "s_percentR": np.full(n, -60.0)}
    assert strat.sell_signal(peaked_then_rolled, 19, p) is True


def test_pctr_ok_requires_actual_exhaustion_not_just_a_rise():
    """It was a bare any(_rising(...)) with no level — an indicator called
    "exhaustion" that never checked exhaustion, true on ~90% of bars.

    Now split by timeframe: the LONG scale carries the exhaustion level (the
    setup), the SHORT scale carries the turn (the trigger).
    """
    import numpy as np
    p = strat.params()
    tl, cw = int(p["trend_lookback"]), int(p["confirm_window"])
    n, i = 20, 19
    lo = max(tl, i - cw + 1)
    exhausted = np.full(n, -90.0)      # long scale deep in the band

    # Rising, but neither scale oversold -> not exhaustion.
    mid = {"s_percentR": np.linspace(-60.0, -40.0, n),
           "l_percentR": np.full(n, -50.0)}
    assert strat._pctr_ok(mid, i, p, lo, tl) is False

    # Long scale exhausted and the short scale turning up -> exhaustion.
    deep = {"s_percentR": np.concatenate([
        np.full(12, -90.0), np.linspace(-90.0, -70.0, 8)]),
        "l_percentR": exhausted}
    assert strat._pctr_ok(deep, i, p, lo, tl) is True


def test_the_long_scale_must_agree_before_the_short_one_can_trigger():
    """The whole point of two timeframes: a fast twitch is not a setup.

    Both lines used to run on the native series (~21m vs ~112m), so the desk
    was really gating on one short-scale oscillator sampled twice. A short-scale
    bounce with the long scale mid-range is exactly the entry that should not
    fire.
    """
    import numpy as np
    p = strat.params()
    tl, cw = int(p["trend_lookback"]), int(p["confirm_window"])
    n, i = 20, 19
    lo = max(tl, i - cw + 1)
    turning = np.concatenate([np.full(12, -90.0), np.linspace(-90.0, -70.0, 8)])

    # Short scale exhausted and turning, long scale NOT -> refuse.
    a = {"s_percentR": turning, "l_percentR": np.full(n, -45.0)}
    assert strat._pctr_ok(a, i, p, lo, tl) is False

    # Same short scale, long scale exhausted -> allow.
    a["l_percentR"] = np.full(n, -85.0)
    assert strat._pctr_ok(a, i, p, lo, tl) is True

    # rte_require_slow=False restores the old single-line behaviour.
    loose = strat.params(rte_require_slow=False)
    a["l_percentR"] = np.full(n, -45.0)
    assert strat._pctr_ok(a, i, loose, lo, tl) is True


def test_a_missing_long_scale_refuses_rather_than_falling_back():
    """Absence is not a pass.

    The long scale IS the setup; without it there is only a fast oscillator
    twitching, which is what this replaced. Silently degrading to the old
    single-line test would run a different, looser strategy without saying so.
    """
    import numpy as np
    p = strat.params()
    tl, cw = int(p["trend_lookback"]), int(p["confirm_window"])
    n, i = 20, 19
    lo = max(tl, i - cw + 1)
    turning = np.concatenate([np.full(12, -90.0), np.linspace(-90.0, -70.0, 8)])

    assert strat._pctr_ok({"s_percentR": turning}, i, p, lo, tl) is False
    assert strat._pctr_ok(
        {"s_percentR": turning, "l_percentR": np.full(n, np.nan)},
        i, p, lo, tl) is False


def test_the_turn_must_be_current_not_merely_somewhere_in_the_window():
    """Accepting a rise anywhere in the window made cm_ok+pctr_ok true on
    67-79% of bars across DKNG/UBER/SNAP/AMD/SOFI. Pinning the turn to bar i
    brings the pair to ~8%."""
    import numpy as np
    p = strat.params()
    tl, cw = int(p["trend_lookback"]), int(p["confirm_window"])
    n, i = 20, 19
    lo = max(tl, i - cw + 1)

    # Dipped below 30, rose early in the window, but is falling again at bar i.
    rose_then_fell = {"cm_rsi": np.concatenate([
        np.full(12, 10.0), np.array([20.0, 35.0, 45.0, 40.0, 30.0, 20.0, 15.0, 10.0])])}
    assert strat._cm_ok(rose_then_fell, i, p, lo, tl) is False

    still_rising = {"cm_rsi": np.concatenate([
        np.full(12, 10.0), np.linspace(10.0, 45.0, 8)])}
    assert strat._cm_ok(still_rising, i, p, lo, tl) is True


# ── the slow line must survive the shape production actually uses ────────────

def test_resample_works_on_a_range_indexed_frame_with_a_time_column():
    """The engine's fetch_bars returns a RangeIndex + a `time` column.

    Requiring a DatetimeIndex made _resampled_percent_r return None on every
    live symbol, so the long scale silently fell back to the native 112-bar
    lookback — the resample would have run in tests and never once in
    production. That is the same silent-fallback failure this whole strategy
    change exists to remove.
    """
    import signals
    closes = _exhausted_then_recovering()
    df = _frame(closes)
    idx = df.index
    flat = df.reset_index(drop=True)          # RangeIndex, keeps `time`
    assert not isinstance(flat.index, pd.DatetimeIndex)

    out = signals._resampled_percent_r(flat, "15min", 21)
    assert out is not None, "must resample off the time column"
    assert list(out.index) == list(flat.index), "aligned to the caller's index"
    assert out.notna().any()

    # And it must agree with the DatetimeIndex path.
    ref = signals._resampled_percent_r(df.set_axis(idx), "15min", 21)
    assert np.allclose(out.to_numpy(dtype=float),
                       ref.to_numpy(dtype=float), equal_nan=True)


def test_no_time_information_falls_back_rather_than_crashing():
    import signals
    df = _frame(_exhausted_then_recovering()).reset_index(drop=True)
    assert signals._resampled_percent_r(df.drop(columns=["time"]),
                                        "15min", 21) is None


def test_the_slow_line_does_not_peek_into_its_own_bar():
    """A 15-minute bar is not complete until its close.

    Using its value inside itself lets a signal depend on prices later than the
    moment being evaluated — the lookahead trap buy_signal guards against on
    the native series, and much easier to introduce when resampling.
    """
    import signals
    df = _frame(_exhausted_then_recovering())
    out = signals._resampled_percent_r(df, "15min", 21)
    # Truncating the frame must not change values that were already emitted.
    cut = len(df) - 30
    part = signals._resampled_percent_r(df.iloc[:cut], "15min", 21)
    a = out.to_numpy(dtype=float)[:cut - 15]
    b = part.to_numpy(dtype=float)[:cut - 15]
    assert np.allclose(a, b, equal_nan=True), "past values changed with future bars"


def test_macd_only_entry():
    p_macd = strat.params(
        require_macd=True,
        require_cm_rsi=False,
        require_pctr=False,
        macd_sep_mult=0.4,
        confirm_window=12,
    )
    df = strat.compute_indicators(_frame(_sine()), p_macd)
    a = strat.to_arrays(df)
    n = len(a["close"])
    buys = [i for i in range(n - 1) if strat.buy_signal(a, i, p_macd)]
    assert buys, "expected MACD-only entries on sine upswings"
    st = strat.evaluate_state(a, buys[0], p_macd)
    assert st["buy"] is True
    assert "macd_gap" in st and st["macd_gap"] is not None
    assert "macd_bull" in st and st["macd_bull"] is True

