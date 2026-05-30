"""
test_alert_engine.py — offline tests for STRATEGY_MODE=alert.

The alert engine's thesis: the catalyst (a mention burst → is_hot) is the buy
signal, and the exit is the backtest-validated trailing + hard stop. These tests
drive _eval_alert through every entry/exit transition with NO network and NO
Alpaca — log_buy/log_sell are stubbed so nothing is placed or written.

Run:
    venv/bin/python -m pytest tests/test_alert_engine.py -q
"""

import os
import sys
import types

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import signal_engine as se   # noqa: E402


@pytest.fixture
def engine(monkeypatch):
    """A minimal SignalEngine stand-in plus a capture of buy/sell calls."""
    calls = []
    monkeypatch.setattr(se, "log_buy",
                        lambda **k: calls.append(("BUY", k["ticker"], round(k["price"], 4))))
    monkeypatch.setattr(se, "log_sell",
                        lambda **k: calls.append(("SELL", k["ticker"], round(k["price"], 4), k["reason"])))
    # Deterministic risk limits regardless of signal_engine.env
    monkeypatch.setattr(se, "MAX_PRICE", 5.0)
    monkeypatch.setattr(se, "MAX_TOTAL_EXPOSURE", 0.0)
    monkeypatch.setattr(se, "TRADE_AMOUNT", 500.0)
    monkeypatch.setattr(se, "ALERT_HARD_STOP", 8.0)
    monkeypatch.setattr(se, "ALERT_TRAIL_STOP", 15.0)
    monkeypatch.setattr(se, "ALERT_MIN_HOLD", 0)
    monkeypatch.setattr(se, "ALERT_REQUIRE_GREEN", False)

    fake = types.SimpleNamespace(active={})
    fake.calls = calls
    fake.eval = lambda ts: se.SignalEngine._eval_alert(fake, ts)
    return fake


def _hot(sym="AAA", price=2.0, vel=7):
    ts = se.TickerState(sym)
    ts.last_price = price
    ts.is_hot = True
    ts.mention_velocity = vel
    return ts


# ── Entry ─────────────────────────────────────────────────────────────────────

def test_no_entry_when_not_hot(engine):
    ts = se.TickerState("AAA")
    ts.last_price = 2.0
    ts.is_hot = False
    engine.eval(ts)
    assert not ts.in_position
    assert engine.calls == []


def test_entry_on_mention_burst(engine):
    ts = _hot(price=2.0)
    engine.eval(ts)
    assert ts.in_position
    assert ts.buy_price == 2.0
    assert ts.high_since_buy == 2.0
    assert ts.priority_buy is True
    assert engine.calls[0] == ("BUY", "AAA", 2.0)


def test_max_price_vetoes_entry(engine):
    ts = _hot(price=9.0)   # above MAX_PRICE=5
    engine.eval(ts)
    assert not ts.in_position
    assert engine.calls == []


def test_exposure_cap_vetoes_entry(engine, monkeypatch):
    monkeypatch.setattr(se, "MAX_TOTAL_EXPOSURE", 500.0)  # one position fills the cap
    held = _hot("HELD", price=2.0)
    held.in_position = True
    engine.active = {"HELD": held}
    ts = _hot("NEW", price=2.0)
    engine.eval(ts)
    assert not ts.in_position


def test_require_green_vetoes_falling_entry(engine, monkeypatch):
    monkeypatch.setattr(se, "ALERT_REQUIRE_GREEN", True)
    ts = _hot(price=2.0)
    ts.cached_df = pd.DataFrame({"close": [2.5, 2.4]})  # prior close 2.4 > price 2.0
    engine.eval(ts)
    assert not ts.in_position


# ── Exit ──────────────────────────────────────────────────────────────────────

def test_peak_ratchets_and_holds(engine):
    ts = _hot(price=2.0)
    engine.eval(ts)            # enter
    ts.last_price = 3.0
    engine.eval(ts)            # rise
    assert ts.high_since_buy == 3.0
    assert ts.in_position      # 15% trail of 3.0 = 2.55, price 3.0 is fine


def test_trailing_stop_fires(engine):
    ts = _hot(price=2.0)
    engine.eval(ts)
    ts.last_price = 3.0        # peak ratchets
    engine.eval(ts)
    ts.last_price = 2.54       # < 3.0 * 0.85 = 2.55
    engine.eval(ts)
    assert not ts.in_position
    assert engine.calls[-1][0] == "SELL"
    assert "trail" in engine.calls[-1][3]


def test_trailing_stop_holds_just_above(engine):
    ts = _hot(price=2.0)
    engine.eval(ts)
    ts.last_price = 3.0
    engine.eval(ts)
    ts.last_price = 2.56       # > 2.55 trail level
    engine.eval(ts)
    assert ts.in_position


def test_hard_stop_fires(engine):
    ts = _hot(price=4.0)
    engine.eval(ts)
    ts.last_price = 3.67       # < 4.0 * 0.92 = 3.68
    engine.eval(ts)
    assert not ts.in_position
    assert "hard" in engine.calls[-1][3]


def test_min_hold_blocks_immediate_stop(engine, monkeypatch):
    monkeypatch.setattr(se, "ALERT_MIN_HOLD", 60)
    ts = _hot(price=4.0)
    engine.eval(ts)
    ts.last_price = 3.0        # would hard-stop, but we're inside min-hold
    engine.eval(ts)
    assert ts.in_position      # held — min hold not elapsed


# ── Dashboard state ───────────────────────────────────────────────────────────

def test_alert_state_in_position_reports_pnl(engine):
    ts = _hot(price=2.0)
    engine.eval(ts)
    ts.last_price = 3.0
    engine.eval(ts)
    st = ts.alert_state()
    assert st["strategy"] == "alert"
    assert st["in_position"] is True
    assert st["pnl_pct"] == pytest.approx(50.0, abs=0.01)
    assert st["peak_gain_pct"] == pytest.approx(50.0, abs=0.01)
