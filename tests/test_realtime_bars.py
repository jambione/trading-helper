"""
test_realtime_bars.py — offline tests for the Finnhub→OHLCV aggregator.

Deterministic, no network: feed synthetic trades and assert correct bar
formation, minute rollover, OHLC/volume math, and seeding.
"""

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from realtime_bars import RealtimeBarAggregator   # noqa: E402

MIN = 60_000   # one minute in ms


def test_single_minute_ohlc():
    agg = RealtimeBarAggregator()
    base = 1_700_000_000_000  # arbitrary ms epoch on a minute boundary-ish
    # Trades within the same minute: prices 10, 12, 9, 11
    for px, t in [(10, 0), (12, 1000), (9, 2000), (11, 3000)]:
        agg.on_trade("X", px, 100, base + t)
    bar = agg.forming_bar("X")
    assert bar["open"] == 10
    assert bar["high"] == 12
    assert bar["low"] == 9
    assert bar["close"] == 11
    assert bar["volume"] == 400


def test_minute_rollover_seals_bar():
    agg = RealtimeBarAggregator()
    base = 1_700_000_000_000
    minute0 = (base // MIN) * MIN
    agg.on_trade("X", 10, 100, minute0 + 1000)
    agg.on_trade("X", 11, 100, minute0 + 2000)
    # Next minute → previous bar should seal
    agg.on_trade("X", 20, 100, minute0 + MIN + 500)

    df = agg.get_bars("X")
    assert len(df) == 2                      # sealed bar + forming bar
    assert df.iloc[0]["open"] == 10
    assert df.iloc[0]["close"] == 11         # sealed at last price of minute 0
    assert df.iloc[-1]["open"] == 20         # forming bar of minute 1


def test_get_bars_appends_forming_to_seed():
    agg = RealtimeBarAggregator()
    seed = pd.DataFrame({
        "time":  [f"2024-01-01T09:{m:02d}:00Z" for m in range(30, 35)],
        "open":  [100.0] * 5, "high": [101.0] * 5, "low": [99.0] * 5,
        "close": [100.0] * 5, "volume": [1000.0] * 5,
    })
    agg.seed("X", seed)
    assert agg.is_seeded("X")

    base = 1_700_000_000_000
    agg.on_trade("X", 105.0, 50, base)
    df = agg.get_bars("X")
    assert len(df) == 6                       # 5 seeded + 1 forming
    assert df.iloc[-1]["close"] == 105.0


def test_volume_accumulates_across_trades():
    agg = RealtimeBarAggregator()
    base = 1_700_000_000_000
    for _ in range(5):
        agg.on_trade("X", 10.0, 200, base)
    assert agg.forming_bar("X")["volume"] == 1000


def test_unknown_ticker_returns_none():
    agg = RealtimeBarAggregator()
    assert agg.get_bars("NOPE") is None
    assert agg.forming_bar("NOPE") is None


def test_maxlen_bounds_history():
    agg = RealtimeBarAggregator(maxlen=10)
    base = 1_700_000_000_000
    minute0 = (base // MIN) * MIN
    # Roll through 50 minutes → only the last 10 sealed bars should remain
    for k in range(50):
        agg.on_trade("X", 10 + k, 100, minute0 + k * MIN + 500)
    df = agg.get_bars("X")
    assert len(df) <= 11                      # <=10 sealed + 1 forming


def test_out_of_order_old_trade_ignored():
    agg = RealtimeBarAggregator()
    base = 1_700_000_000_000
    minute0 = (base // MIN) * MIN
    agg.on_trade("X", 10, 100, minute0 + 1000)
    agg.on_trade("X", 20, 100, minute0 + MIN + 500)   # advance to minute 1
    close_before = agg.forming_bar("X")["close"]
    agg.on_trade("X", 999, 100, minute0 + 2000)       # stale trade from minute 0
    # Forming bar (minute 1) must be unchanged by the stale minute-0 trade
    assert agg.forming_bar("X")["close"] == close_before


def test_indicators_run_on_realtime_frame():
    # The aggregator output must be consumable by the strategy indicators.
    import strategy_three_indicator as strat
    agg = RealtimeBarAggregator()
    seed = pd.DataFrame({
        "time":  [f"2024-01-01T{9 + m // 60:02d}:{m % 60:02d}:00Z" for m in range(200)],
        "open":  [100.0 + (m % 5) for m in range(200)],
        "high":  [101.0 + (m % 5) for m in range(200)],
        "low":   [99.0 + (m % 5) for m in range(200)],
        "close": [100.0 + (m % 5) for m in range(200)],
        "volume": [1000.0] * 200,
    })
    agg.seed("X", seed)
    agg.on_trade("X", 104.0, 100, 1_700_000_000_000)
    df = agg.get_bars("X")
    ind = strat.compute_indicators(df)
    assert "cm_rsi" in ind.columns and "macd_hist" in ind.columns


# ── Freshness ────────────────────────────────────────────────────────────────
# A dropped stream leaves sealed history and a forming bar behind, so the frame
# still looks healthy while its newest bar ages. These guard the fallback.

BASE = 1_700_000_000_000


def test_age_is_none_before_any_trade():
    """Seeded but never fed: unusable, and must be distinguishable from fresh."""
    agg = RealtimeBarAggregator()
    agg.seed("X", pd.DataFrame({
        "time": ["2024-01-01T09:00:00Z"], "open": [10.0], "high": [11.0],
        "low": [9.0], "close": [10.0], "volume": [100.0],
    }))
    assert agg.is_seeded("X")
    assert agg.get_bars("X") is not None      # frame looks healthy…
    assert agg.age_seconds("X") is None       # …but nothing has ever fed it
    assert agg.last_trade_ms("X") is None


def test_age_tracks_newest_trade():
    agg = RealtimeBarAggregator()
    agg.on_trade("X", 10.0, 100, BASE)
    assert agg.age_seconds("X", now_ms=BASE) == 0.0
    assert agg.age_seconds("X", now_ms=BASE + 90_000) == 90.0
    assert agg.last_trade_ms("X") == BASE


def test_frozen_stream_ages_while_frame_stays_full():
    """The exact failure: bars still returned, but they stopped being current."""
    agg = RealtimeBarAggregator()
    for m in range(5):
        agg.on_trade("X", 10.0 + m, 100, BASE + m * MIN)
    # Stream dies here. Ten minutes later the frame is unchanged...
    frame_len = len(agg.get_bars("X"))
    later = BASE + 4 * MIN + 600_000
    assert len(agg.get_bars("X")) == frame_len
    # ...but the age makes the staleness visible to the caller.
    assert agg.age_seconds("X", now_ms=later) == 600.0


def test_out_of_order_trade_never_rewinds_the_clock():
    agg = RealtimeBarAggregator()
    agg.on_trade("X", 10.0, 100, BASE + 5 * MIN)
    agg.on_trade("X", 10.0, 100, BASE)          # late print from an old minute
    assert agg.last_trade_ms("X") == BASE + 5 * MIN


def test_age_is_per_ticker():
    agg = RealtimeBarAggregator()
    agg.on_trade("LIVE", 10.0, 100, BASE)
    agg.on_trade("DEAD", 10.0, 100, BASE - 600_000)
    assert agg.age_seconds("LIVE", now_ms=BASE) == 0.0
    assert agg.age_seconds("DEAD", now_ms=BASE) == 600.0
    assert agg.age_seconds("NEVER", now_ms=BASE) is None


# ── Engine fallback ──────────────────────────────────────────────────────────

def _engine_stub():
    """Minimal stand-in: _strategy_df only touches rt_bars and _rt_stale."""
    import signal_engine as se

    class _Eng:
        pass

    eng = _Eng()
    eng.rt_bars = RealtimeBarAggregator()
    eng._rt_stale = set()
    return se, eng


def _ts_stub(ticker="X"):
    class _TS:
        pass
    t = _TS()
    t.ticker = ticker
    return t


def _wide_frame(n=60, tag=1.0):
    return pd.DataFrame({
        "time":  [f"2024-01-01T09:{m:02d}:00Z" for m in range(n)],
        "open":  [tag] * n, "high": [tag] * n,
        "low":   [tag] * n, "close": [tag] * n, "volume": [1.0] * n,
    })


def test_fresh_realtime_bars_are_preferred(monkeypatch):
    import time as _t
    se, eng = _engine_stub()
    monkeypatch.setattr(se, "REALTIME_BARS", True)
    monkeypatch.setattr(se, "RT_BARS_MAX_STALE", 120.0)

    eng.rt_bars.seed("X", _wide_frame(tag=1.0))
    eng.rt_bars.on_trade("X", 99.0, 10, int(_t.time() * 1000))

    out = se.SignalEngine._strategy_df(eng, _ts_stub(), _wide_frame(tag=2.0))
    assert float(out["close"].iloc[-1]) == 99.0      # realtime frame won


def test_stale_realtime_bars_fall_back_to_alpaca(monkeypatch):
    """The bug: a full-looking frozen frame used to win over fresh Alpaca bars."""
    import time as _t
    se, eng = _engine_stub()
    monkeypatch.setattr(se, "REALTIME_BARS", True)
    monkeypatch.setattr(se, "RT_BARS_MAX_STALE", 120.0)

    eng.rt_bars.seed("X", _wide_frame(tag=1.0))
    # Last print ten minutes ago — stream died, frame still full.
    eng.rt_bars.on_trade("X", 99.0, 10, int(_t.time() * 1000) - 600_000)
    assert len(eng.rt_bars.get_bars("X")) >= 60      # still looks healthy

    out = se.SignalEngine._strategy_df(eng, _ts_stub(), _wide_frame(tag=2.0))
    assert float(out["close"].iloc[-1]) == 2.0       # fell back to Alpaca
    assert "X" in eng._rt_stale                      # and recorded the switch


def test_never_fed_ticker_falls_back(monkeypatch):
    se, eng = _engine_stub()
    monkeypatch.setattr(se, "REALTIME_BARS", True)
    monkeypatch.setattr(se, "RT_BARS_MAX_STALE", 120.0)

    eng.rt_bars.seed("X", _wide_frame(tag=1.0))      # seeded, never traded
    out = se.SignalEngine._strategy_df(eng, _ts_stub(), _wide_frame(tag=2.0))
    assert float(out["close"].iloc[-1]) == 2.0


def test_recovery_clears_the_stale_flag(monkeypatch):
    import time as _t
    se, eng = _engine_stub()
    monkeypatch.setattr(se, "REALTIME_BARS", True)
    monkeypatch.setattr(se, "RT_BARS_MAX_STALE", 120.0)
    eng._rt_stale.add("X")

    eng.rt_bars.seed("X", _wide_frame(tag=1.0))
    eng.rt_bars.on_trade("X", 99.0, 10, int(_t.time() * 1000))
    out = se.SignalEngine._strategy_df(eng, _ts_stub(), _wide_frame(tag=2.0))
    assert float(out["close"].iloc[-1]) == 99.0
    assert "X" not in eng._rt_stale


def test_disabled_flag_always_uses_alpaca(monkeypatch):
    import time as _t
    se, eng = _engine_stub()
    monkeypatch.setattr(se, "REALTIME_BARS", False)
    eng.rt_bars.seed("X", _wide_frame(tag=1.0))
    eng.rt_bars.on_trade("X", 99.0, 10, int(_t.time() * 1000))
    out = se.SignalEngine._strategy_df(eng, _ts_stub(), _wide_frame(tag=2.0))
    assert float(out["close"].iloc[-1]) == 2.0
