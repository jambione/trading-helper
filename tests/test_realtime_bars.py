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
    import types

    import signal_engine as se

    class _Eng:
        pass

    eng = _Eng()
    eng.rt_bars = RealtimeBarAggregator()
    eng._rt_stale = set()
    # Bind helpers so promote/ready paths resolve on the stub instance.
    eng._macd_min_bars = types.MethodType(se.SignalEngine._macd_min_bars, eng)
    eng._rt_bars_ready = types.MethodType(se.SignalEngine._rt_bars_ready, eng)
    eng._strategy_df = types.MethodType(se.SignalEngine._strategy_df, eng)
    eng._promote_rt_bars_if_eligible = types.MethodType(
        se.SignalEngine._promote_rt_bars_if_eligible, eng
    )
    eng._eval_three_indicator = types.MethodType(
        se.SignalEngine._eval_three_indicator, eng
    )
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


def test_trade_ts_ms_normalises_both_feeds():
    """Finnhub sends milliseconds, the Alpaca poller sends seconds.

    2026-08-20: feeding seconds straight into on_trade made age_seconds report
    decades (never fresh) and bucketed bars near 1970, so a ticker fed by both
    sources sealed a garbage bar on every alternation. Coverage went DOWN when
    the second feed was enabled.
    """
    import signal_engine as se

    secs = 1_787_240_000.0          # what alpaca_price_poll passes
    ms = 1_787_240_000_000          # what the Finnhub socket passes

    assert se._trade_ts_ms(secs) == ms
    assert se._trade_ts_ms(ms) == ms
    # Both feeds must land in the same minute bucket for the same instant.
    from realtime_bars import _epoch_minute
    assert _epoch_minute(se._trade_ts_ms(secs)) == _epoch_minute(se._trade_ts_ms(ms))
    # Junk is refused rather than turned into a 1970 bar.
    assert se._trade_ts_ms(None) == 0
    assert se._trade_ts_ms(-1) == 0
    assert se._trade_ts_ms("nonsense") == 0


def test_aggregator_stays_fresh_when_fed_seconds_style_input():
    """End to end: a seconds timestamp must not make a ticker look ancient."""
    import time

    import signal_engine as se
    from realtime_bars import RealtimeBarAggregator

    agg = RealtimeBarAggregator()
    now_s = time.time()
    agg.on_trade("TEM", 67.9, 100, se._trade_ts_ms(now_s))
    age = agg.age_seconds("TEM")
    assert age is not None
    assert age < 5, f"seconds input reported as {age}s old"


# ── last_trade(): the price and its clock as one event ──────────────────────
#
# Added 2026-08-27. The desk publishes a price into the dashboard's price
# merge, and that merge picks a winner by recency. It had been pairing the
# engine's `price` (which the engine adopts from the dashboard when the stream
# is quiet) with `bars_age_sec` (the tape's trade clock). A frozen quote came
# back reading 0.3s old and won races it should have lost. last_trade() exists
# so the two cannot be sourced separately.

def test_last_trade_returns_the_price_and_its_own_timestamp():
    agg = RealtimeBarAggregator()
    agg.on_trade("AAA", 10.25, 100, 1_787_000_000_000)
    assert agg.last_trade("AAA") == (10.25, 1_787_000_000_000)


def test_last_trade_is_none_for_a_ticker_that_never_traded():
    """Absent, not fresh — the caller must be able to tell the difference."""
    agg = RealtimeBarAggregator()
    assert agg.last_trade("NOPE") is None


def test_last_trade_is_none_for_a_seeded_but_unfed_ticker():
    """seed() fills sealed history, so get_bars() looks healthy for a ticker
    no trade has touched. The price pair must not inherit that illusion."""
    agg = RealtimeBarAggregator()
    agg.seed("AAA", pd.DataFrame([
        {"time": "2026-08-27T13:30:00Z", "open": 10.0, "high": 10.1,
         "low": 9.9, "close": 10.05, "volume": 1000},
    ]))
    assert agg.get_bars("AAA") is not None
    assert agg.last_trade("AAA") is None


def test_last_trade_advances_with_the_newest_print():
    agg = RealtimeBarAggregator()
    agg.on_trade("AAA", 10.00, 100, 1_787_000_000_000)
    agg.on_trade("AAA", 10.40, 100, 1_787_000_030_000)
    assert agg.last_trade("AAA") == (10.40, 1_787_000_030_000)


def test_an_out_of_order_print_moves_neither_half():
    """on_trade keeps the freshness clock monotonic. The price has to follow
    the same rule or the pair splits: a late print from an earlier minute
    would overwrite the price while the timestamp stayed put, which is the
    borrowed-clock bug in miniature."""
    agg = RealtimeBarAggregator()
    agg.on_trade("AAA", 10.00, 100, 1_787_000_030_000)
    agg.on_trade("AAA", 9.50, 100, 1_787_000_000_000)   # 30s late
    assert agg.last_trade("AAA") == (10.00, 1_787_000_030_000)


def test_the_pair_survives_a_minute_rollover():
    agg = RealtimeBarAggregator()
    agg.on_trade("AAA", 10.00, 100, 1_787_000_000_000)
    agg.on_trade("AAA", 10.75, 100, 1_787_000_000_000 + MIN)
    px, ts = agg.last_trade("AAA")
    assert px == 10.75
    assert ts == 1_787_000_000_000 + MIN


def test_the_pair_agrees_with_age_seconds():
    """Same underlying timestamp, so they can never disagree about which
    print is the newest one."""
    agg = RealtimeBarAggregator()
    now_ms = 1_787_000_000_000
    agg.on_trade("AAA", 10.00, 100, now_ms)
    _px, ts = agg.last_trade("AAA")
    age = agg.age_seconds("AAA", now_ms=now_ms + 4_000)
    assert age == (now_ms + 4_000 - ts) / 1000.0 == 4.0


def test_a_zero_price_print_updates_neither_half():
    """on_trade drops price <= 0 before the clock, so a junk print must not
    advance freshness either."""
    agg = RealtimeBarAggregator()
    agg.on_trade("AAA", 10.00, 100, 1_787_000_000_000)
    agg.on_trade("AAA", 0.0, 100, 1_787_000_060_000)
    assert agg.last_trade("AAA") == (10.00, 1_787_000_000_000)


# ── Seed adequacy + promote-on-eligible (2026-09-02) ─────────────────────────
# Live: VSTM/ASST had young Finnhub asks while bars_src stayed alpaca because
# ALPACA_RT_SKIP_REFRESH and the 0.5¢ price_moved gate skipped _strategy_df.


def test_is_seeded_respects_min_bars():
    agg = RealtimeBarAggregator()
    short = pd.DataFrame({
        "time": [f"2024-01-01T09:{m:02d}:00Z" for m in range(5)],
        "open": [1.0] * 5, "high": [1.0] * 5,
        "low": [1.0] * 5, "close": [1.0] * 5, "volume": [1.0] * 5,
    })
    agg.seed("X", short)
    assert agg.is_seeded("X")                 # default: any sealed history
    assert agg.is_seeded("X", min_bars=1)
    assert not agg.is_seeded("X", min_bars=40)
    assert agg.sealed_count("X") == 5


def test_is_seeded_min_bars_met_after_full_seed():
    agg = RealtimeBarAggregator()
    agg.seed("X", _wide_frame(n=60))
    assert agg.is_seeded("X", min_bars=40)
    assert agg.sealed_count("X") == 60


def test_rt_bars_ready_requires_fresh_and_warmup(monkeypatch):
    import time as _t
    se, eng = _engine_stub()
    monkeypatch.setattr(se, "REALTIME_BARS", True)
    monkeypatch.setattr(se, "RT_BARS_MAX_STALE", 120.0)
    monkeypatch.setattr(se, "MACD_SLOW", 26)
    monkeypatch.setattr(se, "MACD_SIG", 9)

    ready, age, n = eng._rt_bars_ready("X")
    assert ready is False and n == 0

    eng.rt_bars.seed("X", _wide_frame(n=60))
    ready, age, n = eng._rt_bars_ready("X")
    assert ready is False          # seeded but never traded
    assert n >= 60

    eng.rt_bars.on_trade("X", 99.0, 10, int(_t.time() * 1000))
    ready, age, n = eng._rt_bars_ready("X")
    assert ready is True
    assert age is not None and age < 5


def test_promote_flips_bars_src_without_price_move(monkeypatch):
    """The stranded-alpaca bug: rt eligible, bars_src still alpaca, no 0.5¢ move."""
    import time as _t
    import types
    se, eng = _engine_stub()
    monkeypatch.setattr(se, "REALTIME_BARS", True)
    monkeypatch.setattr(se, "RT_BARS_MAX_STALE", 120.0)
    monkeypatch.setattr(se, "STRATEGY_MODE", "three_indicator")
    monkeypatch.setattr(se, "MACD_SLOW", 26)
    monkeypatch.setattr(se, "MACD_SIG", 9)

    ts = _ts_stub("VSTM")
    ts.bars_fetched = True
    ts._bars_src = "alpaca"
    ts.cached_df = _wide_frame(n=60, tag=2.0)
    ts.three_ind_state = {}

    eng.rt_bars.seed("VSTM", _wide_frame(n=60, tag=1.0))
    eng.rt_bars.on_trade("VSTM", 99.0, 10, int(_t.time() * 1000))

    # Avoid full three_ind compute — promote only needs _strategy_df side effect.
    called = {}

    def _fake_eval(self, ts_, df):
        called["n"] = len(df)
        called["src"] = getattr(ts_, "_bars_src", None)

    eng._eval_three_indicator = types.MethodType(_fake_eval, eng)
    ok = eng._promote_rt_bars_if_eligible(ts)
    assert ok is True
    assert ts._bars_src == "realtime"
    assert called["src"] == "realtime"
    assert called["n"] >= 40


def test_promote_noop_when_already_realtime(monkeypatch):
    import time as _t
    se, eng = _engine_stub()
    monkeypatch.setattr(se, "REALTIME_BARS", True)
    monkeypatch.setattr(se, "RT_BARS_MAX_STALE", 120.0)
    monkeypatch.setattr(se, "STRATEGY_MODE", "three_indicator")

    ts = _ts_stub("X")
    ts.bars_fetched = True
    ts._bars_src = "realtime"
    ts.cached_df = _wide_frame(n=60)
    eng.rt_bars.seed("X", _wide_frame(n=60))
    eng.rt_bars.on_trade("X", 99.0, 10, int(_t.time() * 1000))

    def _boom(*a, **k):
        raise AssertionError("should not re-eval when already realtime")

    eng._eval_three_indicator = _boom
    assert eng._promote_rt_bars_if_eligible(ts) is False


def test_promote_noop_when_rt_stale(monkeypatch):
    import time as _t
    se, eng = _engine_stub()
    monkeypatch.setattr(se, "REALTIME_BARS", True)
    monkeypatch.setattr(se, "RT_BARS_MAX_STALE", 120.0)
    monkeypatch.setattr(se, "STRATEGY_MODE", "three_indicator")

    ts = _ts_stub("X")
    ts.bars_fetched = True
    ts._bars_src = "alpaca"
    ts.cached_df = _wide_frame(n=60)
    eng.rt_bars.seed("X", _wide_frame(n=60))
    eng.rt_bars.on_trade("X", 99.0, 10, int(_t.time() * 1000) - 600_000)

    assert eng._promote_rt_bars_if_eligible(ts) is False
    assert ts._bars_src == "alpaca"
