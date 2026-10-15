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
