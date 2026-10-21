"""Drift transform, the live walk, bid handling and the capture / break-even arithmetic for exit_capture."""
from __future__ import annotations

import os
import sys
from collections import Counter
from datetime import datetime

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))

import exit_capture as X  # noqa: E402

L, cp, bars = X.L, X.cp, X.bars


def _t(day="2026-10-15", hh=11, mm=0, ss=0):
    return datetime(2026, int(day[5:7]), int(day[8:]), hh, mm, ss, tzinfo=bars.ET).timestamp()


def test_g_and_factor():
    assert X.g(-5, 180) == 0 and X.g(90, 180) == 0.5 and X.g(400, 180) == 1.0
    assert X.g(59, 60, "step") == 0 and X.g(60, 60, "step") == 1.0
    assert X.factor(1090, 1000, 20, 180) == pytest.approx(1 + 0.002 * 0.5)


def test_scaling_prints_and_bars():
    secs = [(990, 10.0, 9.9), (1090, 10.0, 9.9), (1180, 10.0, 10.0)]
    sc = X.scale_secs(secs, 1000, 20, 180)
    assert sc[0] == secs[0] and sc[1][1] == pytest.approx(10.01) and sc[2][2] == pytest.approx(10.02)
    assert X.scale_secs(secs, 1000, 0, 180) is secs
    b = X.scale_bars([(1000, 10.0, 10.0, 10.0, 10.0)], 1000, 20, 180)     # closes at 1060 -> g 1/3
    assert b[0][4] == pytest.approx(10.0 * (1 + 0.002 / 3))


def test_dead_trade_live_semantics():
    assert X.dead_trade_params({"ai_dead_trade_min": 0.0}) == (22.0, 0.10)       # 0 means the default, not off
    assert X.dead_trade_params({"ai_dead_trade_min": 15, "ai_dead_trade_mfe_r": 0.2}) == (15.0, 0.2)


def _cfg():
    cfg = dict(L.load_config())
    cfg.update({"ai_trail_exit_trend_hold": False, "ai_dead_trade_min": 22.0, "ai_dead_trade_mfe_r": 0.10})
    return cfg


def _fill(t0):
    return {"sym": "AAA", "day": "2026-10-15", "t0": t0, "entry": 20.0, "risk": 0.4, "spread_r": 0.02}


def _path(t0, n=900):
    import math
    return [(int(t0) + k, 20.0 + 0.08 * math.sin(k / 37.0) + 0.0004 * k, 20.0 + 0.08 * math.sin(k / 37.0) + 0.0004 * k - 0.01)
            for k in range(1, n)]


def test_mu0_walk_reproduces_leash_replay_when_the_trend_hold_is_off():
    t0 = _t()
    f, secs, cfg = _fill(t0), _path(t0), _cfg()
    ring = max(2, int(cfg.get("ai_local_trail_print_ring", 3) or 3))
    mine = X.walk(f, secs, cfg, ring, [])
    theirs = L.walk(f, secs, cfg, ring)
    assert mine[:3] == theirs


def test_trend_hold_defers_a_trail_exit(monkeypatch):
    t0 = _t()
    f, cfg = _fill(t0), _cfg()
    cfg["ai_trail_exit_trend_hold"] = True
    secs = [(int(t0) + k, 20.0, 20.0) for k in range(1, 30)] + [(int(t0) + k, 19.9, 19.9) for k in range(30, 400)]
    ring = 3
    monkeypatch.setattr(cp, "trail_exit_trend_hold", lambda p, sym, px, now=None, cfg=None: now < t0 + 100)
    xs, _trig, why, held = X.walk(f, secs, cfg, ring, [])
    plain = L.walk(f, secs, {**cfg, "ai_trail_exit_trend_hold": False}, ring)
    if plain[2] == "trail":
        assert why == "trail" and xs >= t0 + 100 and held > 0
    assert cp._double_check_store_bars is not X._store_bars          # restored after the walk


def test_bars_known_only_closed_and_needs_20():
    b = [(1000 + 60 * k, 1, 2, 0.5, 1.5) for k in range(30)]
    assert X.bars_known(b, 1000 + 60 * 10) is None                   # 10 closed bars
    kb = X.bars_known(b, 1000 + 60 * 25)
    assert len(kb[0]) == 25 and kb[0][-1] == 1000 + 60 * 24


def test_exit_price_and_hold_second():
    assert X.exit_price(10.0, 9.98, "trail") == 9.98 and X.exit_price(10.0, 10.05, "plan_stop") == 10.0
    assert X.exit_price(10.0, 10.05, "dead_trade") == 10.05 and X.exit_price(10.0, None, "trail") is None
    t0 = _t(hh=15, mm=45)
    assert X.hold_second(t0, 180) == int(t0) + 180
    assert X.hold_second(t0, 600) == int(_t(hh=15, mm=50))           # capped at 15:50
    assert X.hold_second(_t(hh=10), "end") == int(_t(hh=10)) + L.MAX_HOLD


def test_bid_failures_are_not_cached(monkeypatch):
    cache = {"bid": {}}
    monkeypatch.setattr(X, "fetch_bid", lambda cl, s, ts, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert X.bid(object(), cache, "AAA", 5) == "missing" and cache["bid"] == {}
    monkeypatch.setattr(X, "fetch_bid", lambda cl, s, ts, **k: None)
    assert X.bid(object(), cache, "AAA", 5) is None and cache["bid"] == {("AAA", 5): None}
    assert X.bid(None, {"bid": {}}, "AAA", 6, offline=True) == "missing"


def test_capture_and_break_even():
    assert X.capture_per_fill(6.0, -4.0, 10) == pytest.approx(1.0)
    assert X.break_even([(0, -5.0), (5, -2.0), (10, 3.0)]) == pytest.approx(5 + 2 * 5 / 5)
    assert X.break_even([(0, 1.0), (10, 5.0)]) == 0.0
    assert X.break_even([(0, -9.0), (40, -1.0)]) == ">40"


def test_bootstrap_break_even_runs():
    d = {mu: {f"d{i}": [mu * 0.8 - 6 + (i % 3)] for i in range(12)} for mu in (0, 5, 10, 20, 40)}
    lo, hi, never = X.bootstrap_break_even(d, n=200)
    assert lo is not None and lo <= hi and 0 <= never <= 1


def test_replay_drops_a_fill_from_every_cell_when_a_bid_is_missing(monkeypatch):
    t0 = _t()
    fills = [_fill(t0), {**_fill(t0 + 1), "sym": "BBB"}]
    cache = {"trades": {("AAA", int(t0)): _path(t0), ("BBB", int(t0) + 1): _path(t0 + 1)},
             "bars": {("AAA", "2026-10-15"): [], ("BBB", "2026-10-15"): []}, "bid": {}}
    monkeypatch.setattr(X, "save_cache", lambda c: None)
    monkeypatch.setattr(X, "bid", lambda cl, c, sym, ts, off=False: "missing" if sym == "BBB" else 20.0)
    res, st = X.replay(fills, cache, _cfg(), 3, None, True)
    assert set(res) == {0} and set(res[0]) >= set(X.cells())
    assert sum(v for k, v in st.items() if k.startswith("no_bid")) == 1
