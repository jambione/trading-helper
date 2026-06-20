"""
test_pinned_compare.py — pinned compare tickers + 3-indicator param overrides.

Pinned tickers (COMPARE_TICKERS) exist for side-by-side validation against
TradingView: they must never expire and must not consume active-list slots.
Param overrides (THREE_IND_<PARAM>) sync the live engine to the exact settings
on the TV chart.

Run:
    venv/bin/python -m pytest tests/test_pinned_compare.py -q
"""

import os
import sys


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import signal_engine as se  # noqa: E402


# ── Pinned ticker expiry ──────────────────────────────────────────────────────

def test_pinned_ticker_never_expires(monkeypatch):
    ts = se.TickerState("AAPL", pinned=True)
    ts.added_ts -= 100_000          # ancient — far past EXPIRY_COLD/WARM
    assert not ts.is_expired()


def test_unpinned_ticker_expires_normally():
    ts = se.TickerState("AAPL")
    ts.added_ts -= 100_000
    assert ts.is_expired()


def test_pinned_default_off():
    assert se.TickerState("AAPL").pinned is False


def test_pinned_surfaced_in_three_indicator_state():
    ts = se.TickerState("AAPL", pinned=True)
    assert ts.three_indicator_state()["pinned"] is True


# ── THREE_IND_* env overrides ─────────────────────────────────────────────────

def test_env_overrides_parse_types(monkeypatch):
    monkeypatch.setenv("THREE_IND_CM_RSI_BUY_MAX", "35.5")   # float param
    monkeypatch.setenv("THREE_IND_CONFIRM_WINDOW", "12")     # int param
    monkeypatch.setenv("THREE_IND_EXIT_MODE", "ALL")         # str param (lowered)
    p = se._three_ind_env_params()
    assert p["cm_rsi_buy_max"] == 35.5
    assert p["confirm_window"] == 12
    assert isinstance(p["confirm_window"], int)
    assert p["exit_mode"] == "all"


def test_env_override_int_accepts_float_string(monkeypatch):
    monkeypatch.setenv("THREE_IND_MACD_FAST", "12.0")
    assert se._three_ind_env_params()["macd_fast"] == 12


def test_invalid_override_ignored(monkeypatch):
    monkeypatch.setenv("THREE_IND_MACD_SEP_MULT", "not-a-number")
    p = se._three_ind_env_params()
    assert "macd_sep_mult" not in p   # falls back to DEFAULT_PARAMS


def test_no_env_means_defaults_only(monkeypatch):
    for k in list(os.environ):
        if k.startswith("THREE_IND_"):
            monkeypatch.delenv(k)
    p = se._three_ind_env_params()
    assert set(p) == {"exit_mode"}    # only the always-present exit_mode

    import strategy_three_indicator as three_ind
    full = three_ind.params(**p)
    assert full["cm_rsi_buy_max"] == three_ind.DEFAULT_PARAMS["cm_rsi_buy_max"]


# ── Guarded buys: kill switch blocks strategy entries ─────────────────────────

def test_three_indicator_buy_blocked_by_guard(monkeypatch, tmp_path):
    """A 3IND buy signal must be vetoed when the guard kill switch is on."""
    import types
    import pandas as pd
    import numpy as np
    import trade_guard

    calls = []
    monkeypatch.setattr(se, "log_buy", lambda **k: calls.append(("BUY", k["ticker"])))

    # Tripped guard: daily loss already past the limit
    g = trade_guard.TradeGuard(daily_loss_limit=10, state_file=tmp_path / "g.json")
    g.record_close(-50, "2099-01-01T00:00:00Z")
    g._realized_pnl = -50  # ensure today's counters regardless of date math
    g._date = trade_guard._today_et()
    monkeypatch.setattr(se, "GUARD", g)

    # Force the strategy to scream BUY
    monkeypatch.setattr(se.three_ind, "compute_indicators", lambda df, p: df)
    monkeypatch.setattr(se.three_ind, "to_arrays",
                        lambda df: {"close": np.zeros(60)})
    monkeypatch.setattr(se.three_ind, "evaluate_state",
                        lambda a, i, p: {"buy": True, "sell": False, "buy_pct": 100})
    monkeypatch.setattr(se, "MAX_PRICE", 0.0)
    monkeypatch.setattr(se, "MAX_TOTAL_EXPOSURE", 0.0)

    fake = types.SimpleNamespace(active={})
    ts = se.TickerState("AAA")
    ts.last_price = 2.0
    df = pd.DataFrame({"close": [2.0] * 60, "open": [2.0] * 60,
                       "high": [2.0] * 60, "low": [2.0] * 60,
                       "volume": [1.0] * 60})

    se.SignalEngine._eval_three_indicator(fake, ts, df)
    assert calls == []           # buy vetoed
    assert not ts.in_position


def test_two_of_three_conditions_extend_expiry(monkeypatch):
    """A 2/3-aligned setup must earn the EXPIRY_WARM window, not die at 3 min."""
    import types
    import numpy as np
    import pandas as pd

    monkeypatch.setattr(se.three_ind, "compute_indicators", lambda df, p: df)
    monkeypatch.setattr(se.three_ind, "to_arrays", lambda df: {"close": np.zeros(60)})
    monkeypatch.setattr(se.three_ind, "evaluate_state",
                        lambda a, i, p: {"buy": False, "sell": False, "buy_pct": 67})

    fake = types.SimpleNamespace(active={})
    ts = se.TickerState("AAA")
    ts.last_price = 2.0
    df = pd.DataFrame({"close": [2.0] * 60, "open": [2.0] * 60,
                       "high": [2.0] * 60, "low": [2.0] * 60,
                       "volume": [1.0] * 60})

    assert ts.expiry_seconds() == se.EXPIRY_COLD
    se.SignalEngine._eval_three_indicator(fake, ts, df)
    assert ts.expiry_seconds() == se.EXPIRY_WARM


def test_catalyst_gate_blocks_cold_buys(monkeypatch):
    """THREE_IND_REQUIRE_HOT: a 3IND buy without a mention burst must not fire."""
    import types
    import numpy as np
    import pandas as pd

    calls = []
    monkeypatch.setattr(se, "log_buy", lambda **k: calls.append(k["ticker"]))
    monkeypatch.setattr(se, "THREE_IND_REQUIRE_HOT", True)
    monkeypatch.setattr(se, "MAX_PRICE", 0.0)
    monkeypatch.setattr(se, "MAX_TOTAL_EXPOSURE", 0.0)
    monkeypatch.setattr(se.three_ind, "compute_indicators", lambda df, p: df)
    monkeypatch.setattr(se.three_ind, "to_arrays", lambda df: {"close": np.zeros(60)})
    monkeypatch.setattr(se.three_ind, "evaluate_state",
                        lambda a, i, p: {"buy": True, "sell": False, "buy_pct": 100})

    fake = types.SimpleNamespace(active={})
    df = pd.DataFrame({"close": [2.0] * 60, "open": [2.0] * 60,
                       "high": [2.0] * 60, "low": [2.0] * 60,
                       "volume": [1.0] * 60})

    cold = se.TickerState("AAA")
    cold.last_price = 2.0
    se.SignalEngine._eval_three_indicator(fake, cold, df)
    assert calls == [] and not cold.in_position      # no burst → no buy

    hot = se.TickerState("BBB")
    hot.last_price = 2.0
    hot.is_hot = True
    se.SignalEngine._eval_three_indicator(fake, hot, df)
    assert calls == ["BBB"] and hot.in_position      # burst → buy fires
