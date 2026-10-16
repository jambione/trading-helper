"""Operator 2026-10-15: the no-progress cut can wait for the double check's %R reading. Pure; no network."""
import ai_positions as cp


def _patch(monkeypatch, sell):
    import tools.sell_double_check as sdc
    monkeypatch.setattr(cp, "_symbol_bars_for_double_check", lambda t, n: ([1.0] * 30, [1.0] * 30, [1.0] * 30, [1.0] * 30))
    monkeypatch.setattr(sdc, "evaluate_symbol", lambda *a, **k: {"sell": sell})


def test_off_by_default_keeps_the_old_cut(monkeypatch):
    _patch(monkeypatch, "no")
    assert cp.no_progress_trend_says_sell({"last_seen_price": 10.0}, "ABC", 1.0, {})


def test_on_sells_only_when_both_lines_fall(monkeypatch):
    cfg = {"ai_no_progress_require_double_check": True}
    _patch(monkeypatch, "yes")
    assert cp.no_progress_trend_says_sell({"last_seen_price": 10.0}, "ABC", 1.0, cfg)
    _patch(monkeypatch, "no")
    assert not cp.no_progress_trend_says_sell({"last_seen_price": 10.0}, "ABC", 1.0, cfg)


def test_fails_safe_to_sell(monkeypatch):
    cfg = {"ai_no_progress_require_double_check": True}
    assert cp.no_progress_trend_says_sell({}, "ABC", 1.0, cfg)               # no price
    monkeypatch.setattr(cp, "_symbol_bars_for_double_check", lambda t, n: (_ for _ in ()).throw(RuntimeError("x")))
    assert cp.no_progress_trend_says_sell({"last_seen_price": 10.0}, "ABC", 1.0, cfg)


def test_replay_never_fetches_bars_for_the_double_check(monkeypatch):
    import tools.replay_session as rs
    import ob_observe
    monkeypatch.setattr(ob_observe, "bars", lambda t: [(i * 60.0, 1, 2, 0.5, 1.5) for i in range(5)])
    assert rs._dc_store_bars("ABC") is None                                   # < 20 bars: none, no fetch
    monkeypatch.setattr(ob_observe, "bars", lambda t: [(i * 60.0, 1, 2, 0.5, 1.5) for i in range(25)])
    st, hi, lo, cl = rs._dc_store_bars("ABC")
    assert len(st) == 25 and hi[0] == 2.0 and cl[-1] == 1.5


def test_trend_reentry_idle_only_on_reentries_into_names_up_enough():
    cfg = {"ai_trend_reentry_idle_sec": 60, "ai_trend_reentry_min_pct": 2.0}
    up = {"features": {"pct_change": 3.1}}
    assert cp.trend_reentry_idle_sec({**up, "entry_n": 2}, cfg) == 60
    assert cp.trend_reentry_idle_sec({**up, "entry_n": 1}, cfg) == 0          # first entry: normal leash
    assert cp.trend_reentry_idle_sec({"features": {"pct_change": 1.5}, "entry_n": 3}, cfg) == 0
    assert cp.trend_reentry_idle_sec({**up, "entry_n": 2}, {}) == 0           # off by default
    assert cp.trend_reentry_idle_sec({"entry_n": 2}, cfg) == 0                # unknown move: normal leash


def test_trail_exit_trend_hold_only_at_or_under_entry_above_the_hard_stop(monkeypatch):
    cfg = {"ai_trail_exit_trend_hold": True, "ai_trail_exit_trend_hold_band_pct": 0.05}
    monkeypatch.setattr(cp, "_double_check_store_bars", lambda t: ([1.0] * 30, [1.0] * 30, [1.0] * 30, [1.0] * 30))
    pos = {"entry_price": 12.04, "entry_stop_price": 11.50}
    _patch(monkeypatch, "no")                                         # %R not both falling -> hold
    assert cp.trail_exit_trend_hold(pos, "AAL", 12.04, 1.0, cfg)
    assert not cp.trail_exit_trend_hold(pos, "AAL", 12.10, 1.0, cfg)   # green exit: take it
    assert not cp.trail_exit_trend_hold(pos, "AAL", 11.50, 1.0, cfg)   # hard stop is absolute
    assert not cp.trail_exit_trend_hold(pos, "AAL", 12.04, 1.0, {})    # off by default
    _patch(monkeypatch, "yes")                                        # both falling -> sell
    assert not cp.trail_exit_trend_hold(pos, "AAL", 12.04, 1.0, cfg)


def test_trail_hold_sells_on_stale_bars(monkeypatch):
    cfg = {"ai_trail_exit_trend_hold": True, "ai_trail_exit_trend_hold_band_pct": 0.05}
    pos = {"entry_price": 12.04, "entry_stop_price": 11.50}
    _patch(monkeypatch, "no")
    monkeypatch.setattr(cp, "_double_check_store_bars", lambda t: ([1000.0] * 30, [1.0] * 30, [1.0] * 30, [1.0] * 30))
    assert cp.trail_exit_trend_hold(pos, "AAL", 12.04, 1000.0 + 299, cfg)          # fresh enough: hold
    assert not cp.trail_exit_trend_hold(pos, "AAL", 12.04, 1000.0 + 301, cfg)      # name left the watch: sell
    assert cp.trail_exit_trend_hold(pos, "AAL", 12.04, 1000.0 + 9999,
                                    {**cfg, "ai_trail_exit_trend_hold_max_bar_age_sec": 0})   # 0 = guard off


def _trail_pos():
    return {"entry_price": 10.0, "entry_stop_price": 9.5, "stop_price": 9.5, "local_stop_price": 10.0,
            "entry_confirmed": True, "entry_confirmed_at": 1.0, "entry_time": 1.0, "qty": 10,
            "peak_price": 10.05, "risk_per_share": 0.5}


def test_live_trail_hit_at_entry_holds_when_on_and_sells_when_off(monkeypatch):
    import tools.sell_double_check as sdc
    sold = []
    import alpaca_trader as at
    monkeypatch.setattr(at, "cancel_open_orders", lambda t: None)
    monkeypatch.setattr(at, "close_out", lambda t, **k: sold.append(t) or {"order_id": "x1"})
    import time as _t
    fresh = _t.time()
    monkeypatch.setattr(cp, "_double_check_store_bars", lambda t: ([fresh] * 30, [1.0] * 30, [1.0] * 30, [1.0] * 30))
    monkeypatch.setattr(sdc, "evaluate_symbol", lambda *a, **k: {"sell": "no"})
    monkeypatch.setattr(cp, "log_event", lambda *a, **k: None)
    on = {"ai_trail_exit_trend_hold": True, "ai_trail_exit_trend_hold_band_pct": 0.05}
    monkeypatch.setattr(cp, "_cfg_all", lambda: dict(on))
    pos = _trail_pos()
    cp.apply_local_trail("ABC", pos, 9.99, [], {})
    assert sold == [] and pos.get("closing_reason") is None
    monkeypatch.setattr(cp, "_cfg_all", lambda: {})
    pos = _trail_pos()
    cp.apply_local_trail("ABC", pos, 9.99, [], {})
    assert sold == ["ABC"] and pos.get("closing_reason") == "local_trail"


def test_trail_hold_never_fetches_bars(monkeypatch):
    calls = []
    monkeypatch.setattr(cp, "_symbol_bars_for_double_check", lambda *a: calls.append(a))
    import ob_observe
    monkeypatch.setattr(ob_observe, "bars", lambda t: [])
    cfg = {"ai_trail_exit_trend_hold": True}
    assert not cp.trail_exit_trend_hold({"entry_price": 10.0, "stop_price": 9.0}, "ABC", 10.0, 1.0, cfg)
    assert calls == []
