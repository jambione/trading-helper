"""
test_alert_integration.py — end-to-end smoke test of the alert flow.

Unlike test_alert_engine.py (which stubs log_buy/log_sell), this drives the REAL
delivery chain — SignalEngine._check_proximity → log_buy → alpaca_trader (off
mode) → on-disk logs — with a simulated Finnhub price feed and TRADER_MODE=off,
so no orders are placed but every real seam is exercised. Closes review item (b):
"one real smoke-test of the alert flow before trusting it."

    venv/bin/python -m pytest tests/test_alert_integration.py -q
"""

import json
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import signal_engine as se          # noqa: E402
import alpaca_trader                # noqa: E402


@pytest.fixture
def engine(monkeypatch, tmp_path):
    # Run the real alert path, but with deterministic config and tmp log files.
    monkeypatch.setattr(se, "STRATEGY_MODE", "alert")
    monkeypatch.setattr(se, "ALERT_HARD_STOP", 8.0)
    monkeypatch.setattr(se, "ALERT_TRAIL_STOP", 15.0)
    monkeypatch.setattr(se, "ALERT_MIN_HOLD", 0)
    monkeypatch.setattr(se, "ALERT_REQUIRE_GREEN", False)
    monkeypatch.setattr(se, "MAX_PRICE", 5.0)
    monkeypatch.setattr(se, "MAX_TOTAL_EXPOSURE", 0.0)
    monkeypatch.setattr(se, "LOG_FILE", tmp_path / "signal_log.json")
    monkeypatch.setattr(alpaca_trader, "_TRADE_LOG", tmp_path / "alpaca_trade_log.json")

    # Real trader, off mode — exercises buy()/sell() without placing orders.
    alpaca_trader.init("off", "", "")

    # Simulated Finnhub price feed.
    prices = {}
    monkeypatch.setattr(se, "get_latest_price", lambda sym: prices.get(sym))

    eng = types.SimpleNamespace(active={})
    eng._eval_alert      = types.MethodType(se.SignalEngine._eval_alert, eng)
    eng._check_proximity = types.MethodType(se.SignalEngine._check_proximity, eng)
    eng.prices   = prices
    eng.log_file = se.LOG_FILE
    eng.trade_log = alpaca_trader._TRADE_LOG
    return eng


def _log(path):
    return json.loads(path.read_text()) if path.exists() else []


def test_full_buy_to_trailing_exit(engine):
    ts = se.TickerState("MNTS")
    engine.active["MNTS"] = ts
    ts.is_hot = True
    ts.mention_velocity = 7

    # 1) catalyst + price → BUY through the real per-second path
    engine.prices["MNTS"] = 2.00
    engine._check_proximity(ts)
    assert ts.in_position and ts.buy_price == 2.00

    sig = _log(engine.log_file)
    assert sig and sig[-1]["action"] == "BUY" and sig[-1]["ticker"] == "MNTS"
    # off-mode trader logged it without ordering
    assert _log(engine.trade_log)[-1]["action"] == "BUY_LOGGED"

    # 2) price rises → peak ratchets, still in position
    engine.prices["MNTS"] = 3.00
    engine._check_proximity(ts)
    assert ts.in_position and ts.high_since_buy == 3.00

    # 3) price falls below trailing level (3.00 * 0.85 = 2.55) → SELL
    engine.prices["MNTS"] = 2.50
    engine._check_proximity(ts)
    assert not ts.in_position

    sig = _log(engine.log_file)
    sells = [e for e in sig if e["action"] == "SELL"]
    assert sells and "trail" in sells[-1]["reason"]
    assert sells[-1]["pnl_pct"] == pytest.approx(25.0, abs=0.1)   # 2.00 → 2.50
    assert _log(engine.trade_log)[-1]["action"] == "SELL_LOGGED"


def test_hard_stop_path(engine):
    ts = se.TickerState("BIYA")
    engine.active["BIYA"] = ts
    ts.is_hot = True
    ts.mention_velocity = 6

    engine.prices["BIYA"] = 4.00
    engine._check_proximity(ts)
    assert ts.in_position

    # drop 8%+ below entry (4.00 * 0.92 = 3.68) → hard stop
    engine.prices["BIYA"] = 3.60
    engine._check_proximity(ts)
    assert not ts.in_position
    sells = [e for e in _log(engine.log_file) if e["action"] == "SELL"]
    assert "hard" in sells[-1]["reason"]


def test_not_hot_never_buys(engine):
    ts = se.TickerState("TEST")
    engine.active["TEST"] = ts
    ts.is_hot = False
    engine.prices["TEST"] = 2.00
    engine._check_proximity(ts)
    assert not ts.in_position
    assert _log(engine.log_file) == []


def test_dashboard_state_tracks_position(engine):
    ts = se.TickerState("MNTS")
    engine.active["MNTS"] = ts
    ts.is_hot = True
    ts.mention_velocity = 7
    engine.prices["MNTS"] = 2.00
    engine._check_proximity(ts)
    engine.prices["MNTS"] = 3.00
    engine._check_proximity(ts)

    st = ts.alert_state()
    assert st["strategy"] == "alert"
    assert st["status"] == "in_position"
    assert st["pnl_pct"] == pytest.approx(50.0, abs=0.1)
    assert st["peak_gain_pct"] == pytest.approx(50.0, abs=0.1)
    assert st["trail_stop_pct"] == 15.0 and st["hard_stop_pct"] == 8.0
