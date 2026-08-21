"""Desk SIP optimizer — scoring, grid, folds, book cap. No Alpaca."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from tools.optimize_rstop import (  # noqa: E402
    VERDICT_CANDIDATE,
    VERDICT_DO_NOT,
    VERDICT_HYPOTHESIS,
    VERDICT_SKIP,
    apply_overlay,
    filter_windows_tod,
    in_admit_window,
    iter_grid,
    live_cfg,
    load_admit_windows,
    loo_folds,
    parse_tod_range,
    rr_ok,
    score_trades,
    verdict,
    walk_book,
)

ET = ZoneInfo("America/New_York")


def _ts(hour: int, minute: int, day: str = "2026-08-14") -> float:
    return datetime.fromisoformat(f"{day}T{hour:02d}:{minute:02d}:00").replace(
        tzinfo=ET).timestamp()


def _bars(pxs: list[float], *, start_h: int = 10, start_m: int = 0,
          day: str = "2026-08-14") -> list:
    out = []
    for i, c in enumerate(pxs):
        m = start_m + i
        ts = _ts(start_h + m // 60, m % 60, day)
        o = pxs[i - 1] if i else c
        h = max(o, c) + 0.01
        low = min(o, c) - 0.01
        out.append((ts, o, h, low, c))
    return out


def test_apply_overlay_does_not_mutate_base():
    base = {"ai_local_trail_give_r": 0.10, "ai_watch_synth_rr": 0.6}
    out = apply_overlay(base, {"give_r": 0.20, "synth_rr": 1.0})
    assert base["ai_local_trail_give_r"] == 0.10
    assert out["ai_local_trail_give_r"] == 0.20
    assert out["ai_local_trail_give_open_r"] == 0.20
    assert out["ai_watch_synth_rr"] == 1.0


def test_grid_size_is_product():
    g = iter_grid({"a": [1, 2], "b": [True, False], "c": [10, 20, 30]})
    assert len(g) == 12


def test_loo_folds_hold_each_day_once():
    days = ["2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14"]
    folds = loo_folds(days)
    assert len(folds) == 5
    held = [f[1][0] for f in folds]
    assert held == days
    for train, test in folds:
        assert set(train + test) == set(days)
        assert not set(train) & set(test)


def test_verdict_loser_is_not_candidate():
    assert verdict(
        held_dollar=-10, base_held=-5, fold_wins=5, n_folds=5, n=40, min_n=30,
    ) == VERDICT_DO_NOT


def test_verdict_candidate_needs_majority_and_n():
    assert verdict(
        held_dollar=1, base_held=0, fold_wins=3, n_folds=5, n=40, min_n=30,
    ) == VERDICT_CANDIDATE
    assert verdict(
        held_dollar=1, base_held=0, fold_wins=3, n_folds=5, n=10, min_n=30,
    ) == VERDICT_HYPOTHESIS
    assert verdict(
        held_dollar=1, base_held=0, fold_wins=2, n_folds=5, n=40, min_n=30,
    ) == VERDICT_HYPOTHESIS
    assert verdict(
        held_dollar=1, base_held=0, fold_wins=0, n_folds=0, n=40, min_n=30,
        skipped=True,
    ) == VERDICT_SKIP


def test_rr_below_min_is_not_ok():
    assert rr_ok({"ai_watch_synth_rr": 0.25, "ai_min_reward_risk": 0.5}) is False
    assert rr_ok({"ai_watch_synth_rr": 0.6, "ai_min_reward_risk": 0.5}) is True


def test_score_empty():
    s = score_trades([])
    assert s["n"] == 0
    assert s["total_dollar"] == 0.0


def test_book_cap_limits_concurrent_and_per_minute():
    cfg = {
        "ai_watch_arm_mode": "last",
        "ai_watch_exhaustion_rules": False,
        "ai_watch_require_exhaustion_data": False,
        "ai_watch_synth_stop_pct": 5.0,
        "ai_watch_synth_rr": 0.6,
        "ai_min_reward_risk": 0.5,
        "ai_local_trail_enabled": True,
        "ai_local_trail_give_r": 0.10,
        "ai_local_trail_give_open_r": 0.10,
        "ai_local_trail_min_give_px": 0.0,
        "ai_dead_trade_min": 0,
        "ai_reentry_cooldown_sec": 0,
        "ai_max_positions": 2,
        "ai_max_buys_per_poll": 1,
        "ai_watch_require_db_zone": False,
        "ai_fill_abort_r": 0,
        "ai_trade_amount": 1000.0,
    }
    # Three names, same minutes, grind up so they all want to arm.
    pxs = [10.0 + 0.02 * i for i in range(20)]
    book = {
        "AAA": _bars(pxs),
        "BBB": _bars(pxs),
        "CCC": _bars(pxs),
    }
    res = walk_book(book, cfg, enforce_book=True)
    # First minute only 1 buy; seats max 2 — never 3 concurrent names.
    syms_open_same_ts = {}
    for t in res["trades"]:
        syms_open_same_ts.setdefault(t["entry_ts"], set()).add(t["symbol"])
    assert res["trades"]
    assert max(len(s) for s in syms_open_same_ts.values()) <= 2
    assert res["refuse"].get("book_full", 0) > 0


def test_admit_windows_skip_wash_and_wrong_day(tmp_path):
    p = tmp_path / "shadow.jsonl"
    t0 = _ts(10, 0)
    t1 = _ts(11, 0)
    rows = [
        {"symbol": "AAA", "admit_ts": t0, "ts": t0, "look_reason": "EXT"},
        {"symbol": "AAA", "admit_ts": t0, "ts": t1, "look_reason": "EXT"},
        {"symbol": "BBB", "admit_ts": t0, "ts": t1, "look_reason": "WASH"},
        {"symbol": "CCC", "admit_ts": _ts(10, 0, "2026-08-10"), "ts": t1,
         "look_reason": ""},
    ]
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    w = load_admit_windows(p, ["2026-08-14"])
    assert ("AAA", "2026-08-14") in w
    assert ("BBB", "2026-08-14") not in w
    assert ("CCC", "2026-08-14") not in w
    lo, hi = w[("AAA", "2026-08-14")][0]
    assert lo == t0 and hi == t1


def test_in_admit_window_none_is_unrestricted():
    assert in_admit_window("AAA", 1.0, None) is True
    assert in_admit_window("AAA", 1.0, {"AAA": [(10.0, 20.0)]}) is False
    assert in_admit_window("AAA", 15.0, {"AAA": [(10.0, 20.0)]}) is True


def test_walk_book_does_not_arm_before_admit():
    cfg = {
        "ai_watch_arm_mode": "last",
        "ai_watch_exhaustion_rules": False,
        "ai_watch_require_exhaustion_data": False,
        "ai_watch_synth_stop_pct": 5.0,
        "ai_watch_synth_rr": 0.6,
        "ai_min_reward_risk": 0.5,
        "ai_local_trail_enabled": True,
        "ai_local_trail_give_r": 0.10,
        "ai_local_trail_give_open_r": 0.10,
        "ai_local_trail_min_give_px": 0.0,
        "ai_dead_trade_min": 0,
        "ai_reentry_cooldown_sec": 0,
        "ai_max_positions": 8,
        "ai_max_buys_per_poll": 2,
        "ai_watch_require_db_zone": False,
        "ai_fill_abort_r": 0,
        "ai_trade_amount": 1000.0,
    }
    pxs = [10.0 + 0.02 * i for i in range(20)]
    bars = _bars(pxs)
    late = bars[10][0]
    res = walk_book(
        {"AAA": bars}, cfg, enforce_book=True,
        admit_windows={"AAA": [(late, bars[-1][0])]},
    )
    assert res["refuse"].get("not_admitted", 0) > 0
    if res["trades"]:
        assert min(t["entry_ts"] for t in res["trades"]) >= late - 60


def test_parse_tod_range_and_filter():
    assert parse_tod_range("14:00-15:30") == (14 * 60, 15 * 60 + 30)
    t_am = _ts(10, 0)
    t_pm = _ts(14, 10)
    w = {
        ("AAA", "2026-08-14"): [(t_am, t_am + 600)],
        ("BBB", "2026-08-14"): [(t_pm, t_pm + 600)],
    }
    late = filter_windows_tod(w, 14 * 60, 15 * 60 + 30)
    assert ("AAA", "2026-08-14") not in late
    assert ("BBB", "2026-08-14") in late


def _hold_cfg(**over):
    cfg = {
        "ai_watch_arm_mode": "last",
        "ai_watch_exhaustion_rules": True,
        "ai_watch_require_exhaustion_data": True,
        "ai_watch_synth_stop_pct": 5.0,
        "ai_watch_synth_rr": 1.0,
        "ai_min_reward_risk": 0.5,
        "ai_local_trail_enabled": True,
        "ai_local_trail_give_r": 0.10,
        "ai_local_trail_give_open_r": 0.10,
        "ai_local_trail_min_give_px": 0.0,
        "ai_dead_trade_min": 0,
        "ai_reentry_cooldown_sec": 0,
        "ai_max_positions": 8,
        "ai_max_buys_per_poll": 8,
        "ai_watch_require_db_zone": False,
        "ai_fill_abort_r": 0,
        "ai_trade_amount": 1000.0,
    }
    cfg.update(over)
    return cfg


def test_arm_at_admit_skips_heat_gate():
    """Live heat/RSI would refuse; arm-at-admit still buys the first window bar."""
    pxs = [10.0] * 30
    bars = _bars(pxs, start_h=14, start_m=0)
    t0 = bars[0][0]
    res = walk_book(
        {"AAA": bars}, _hold_cfg(), enforce_book=False,
        admit_windows={"AAA": [(t0, bars[-1][0])]},
        arm_at_admit=True,
    )
    assert res["trades"], res["refuse"]
    assert res["trades"][0]["why_arm"] == "admit"


def test_arm_at_admit_once_per_window():
    pxs = [10.0 + 0.01 * i for i in range(40)]
    bars = _bars(pxs, start_h=14, start_m=0)
    t0 = bars[0][0]
    res = walk_book(
        {"AAA": bars}, _hold_cfg(ai_dead_trade_min=0, ai_local_trail_enabled=False),
        enforce_book=False,
        admit_windows={"AAA": [(t0, bars[-1][0])]},
        arm_at_admit=True,
    )
    assert len(res["trades"]) == 1


def test_trail_off_uses_hard_stop_not_working_shelf():
    """A 0.8% dip kills the 0.10R shelf and must NOT kill a 5% hard stop."""
    pxs = [10.00, 9.92, 10.20, 10.20, 10.20]
    bars = _bars(pxs, start_h=14, start_m=0)
    t0 = bars[0][0]
    windows = {"AAA": [(t0, bars[-1][0])]}
    live = walk_book(
        {"AAA": bars}, _hold_cfg(), enforce_book=False,
        admit_windows=windows, arm_at_admit=True,
    )
    hold = walk_book(
        {"AAA": bars}, _hold_cfg(ai_local_trail_enabled=False),
        enforce_book=False,
        admit_windows=windows, arm_at_admit=True,
    )
    assert live["trades"]
    assert live["trades"][0]["reason"] == "local_trail"
    assert hold["trades"]
    assert hold["trades"][0]["reason"] != "local_trail"


def test_live_cfg_keeps_heat_when_present(monkeypatch):
    monkeypatch.setattr(
        "tools.optimize_rstop.load_config",
        lambda: {"ai_watch_exhaustion_heat_min_pct": 65.0, "ai_local_trail_give_r": 0.10},
    )
    cfg = live_cfg()
    assert cfg["ai_watch_exhaustion_heat_min_pct"] == 65.0
