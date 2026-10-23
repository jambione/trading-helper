"""Units, windows, board control and the reading rule for gate_counterfactual. No market data."""
from __future__ import annotations

import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))

import gate_counterfactual as G  # noqa: E402

ET = ZoneInfo("America/New_York")


def ts(day: str, hm: str, sec: int = 30) -> float:
    h, m = map(int, hm.split(":"))
    return datetime.strptime(day, "%Y-%m-%d").replace(hour=h, minute=m, second=sec, tzinfo=ET).timestamp()


D = "2026-10-01"


def test_units_first_refusal_overlap_and_admission():
    board = {D: {"BRD": [ts(D, "10:00")], "LATE": [ts(D, "11:00")]}}
    rej = [{"ts": ts(D, "09:40"), "symbol": "BIG", "reason": "float_too_big"},     # before 09:45: ignored for timing
           {"ts": ts(D, "10:05"), "symbol": "BIG", "reason": "float_too_big"},
           {"ts": ts(D, "10:20"), "symbol": "BIG", "reason": "float_too_big"},
           {"ts": ts(D, "10:30"), "symbol": "BIG", "reason": "spread_wide"},
           {"ts": ts(D, "10:10"), "symbol": "LATE", "reason": "float_too_big"},
           {"ts": ts(D, "10:10"), "symbol": "X", "reason": "not_uptrend"}]           # untested gate
    u = G.build_units(rej, board)
    big = [x for x in u["G1_float"] if x["sym"] == "BIG"][0]
    late = [x for x in u["G1_float"] if x["sym"] == "LATE"][0]
    assert abs(big["ts"] - ts(D, "10:05")) < 1 and big["other_gates"] == ["spread_wide"] and big["admit_ts"] is None
    assert late["admit_ts"] == ts(D, "11:00") and not late["other_gates"]          # admitted later: kept (no hindsight)
    assert "G6" not in u and all(x["sym"] != "X" for us in u.values() for x in us)


def test_window_entry_exit_and_cut():
    bars = {m: (10.0, 10.0 + 0.01 * (m - 600)) for m in range(600, 700)}
    e, x, cut = G.window(bars, ts(D, "10:05", 10))
    assert (e, x, cut) == (606, 615, False)                                        # next-minute open, 10 bars
    e, x, cut = G.window(bars, ts(D, "10:05", 10), admit_ts=ts(D, "10:10"))
    assert (e, x, cut) == (606, 609, True)
    assert G.window(bars, ts(D, "10:05", 10), admit_ts=ts(D, "10:06")) == "admitted_before_entry"
    assert G.window({m: v for m, v in bars.items() if m > 610}, ts(D, "10:05")) is None   # no bar within 2 minutes
    assert abs(G.ret(bars, 606, 615) - (10.15 / 10.0 - 1)) < 1e-12


def test_board_at_and_control():
    bd = {"A": [ts(D, "10:04", 0)], "B": [ts(D, "10:01")], "C": [ts(D, "10:06")], "SELF": [ts(D, "10:04")]}
    assert G.board_at(bd, ts(D, "10:05", 0), "SELF") == ["A"]                     # within 120 s before, not after
    db = {"A": {606: (10.0, 10.0), 615: (10.0, 10.1)}}
    assert abs(G.control_ret(db, ["A", "Z"], 606, 615) - 0.01) < 1e-12 and G.control_ret(db, ["Z"], 606, 615) is None


def _h(diff, level, t=None):
    return {"diff": {"mean": diff, "t": t, "t_crit": 2.3}, "level_net": level}


def test_reading_rule():
    assert G.reading({"A": _h(0.0008, 0.0001), "B": _h(0.0006, 0.0002)}) == "COSTLY"
    assert G.reading({"A": _h(0.0008, -0.0003), "B": _h(0.0006, 0.0002)}) == "NO EVIDENCE"   # refused names still lose
    assert G.reading({"A": _h(-0.0008, 0, -3.0), "B": _h(-0.0006, 0, -2.5)}) == "PROTECTIVE"
    assert G.reading({"A": _h(-0.0008, 0, -1.0), "B": _h(-0.0006, 0, -2.5)}) == "NO EVIDENCE"


def test_day_minimum_and_missing_rates():
    rows = [{"day": "d1", "diff": 0.001}] * 3 + [{"day": "d2", "diff": 0.5}] * 2 + [{"day": "d3", "diff": 0.003}] * 3
    s = G.day_stats(rows, "diff")
    assert s["days"] == 2 and abs(s["mean"] - 0.002) < 1e-12                       # d2 (2 units) is left out
    st = G.Counter({"G1_float_refused_missing": 2, "G1_float_ctrl_missing": 30, "G1_float_ctrl_drawn": 100,
                    "G1_float_no_board_120s": 1})
    m = G.missing_rates({"G1_float": [{"on_board_before": False}] * 20 + [{"on_board_before": True}]}, st)
    assert abs(m["G1_float"]["refused"] - 0.1) < 1e-12 and abs(m["G1_float"]["board"] - 0.3) < 1e-12
