"""Sample draw, signal, grouping, grading and verdicts for closing_imbalance. No market data."""
from __future__ import annotations

import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))

import closing_imbalance as CI  # noqa: E402


def test_draw_sessions_seeded_20_per_half():
    days = [f"2026-{m:02d}-{d:02d}" for m in range(4, 11) for d in range(1, 29)]
    s = CI.draw_sessions(days)
    assert s == CI.draw_sessions(days) and s == sorted(s) and len(s) == 40
    assert sum(CI.half_of(d) == "A" for d in s) == 20 and all(CI.FIRST <= d <= CI.LAST for d in s)


def test_signed_imb_and_groups():
    buy = {"side": "B", "imb": 1000.0, "ref": 50.0}
    assert CI.signed_imb(buy, 1e6) == 0.05
    assert CI.signed_imb(dict(buy, side="A"), 1e6) == -0.05
    assert CI.signed_imb(dict(buy, side="N"), 1e6) == 0.0
    assert CI.signed_imb(buy, None) is None and CI.signed_imb(dict(buy, ref=0.0), 1e6) is None
    bc, sc = CI.cuts([x / 100 for x in range(-50, 51)])
    assert abs(bc - 0.40) < 1e-9 and abs(sc + 0.40) < 1e-9
    assert CI.group(0.45, "B", bc, sc) == "BUY" and CI.group(0.45, "A", bc, sc) == "CTRL"
    assert CI.group(-0.45, "A", bc, sc) == "SELL" and CI.group(0.1, "B", bc, sc) == "CTRL"


def test_close_print_prefers_official():
    tr = [(10.0, 500.0, {"6"}), (10.1, 100.0, {"M"}), (9.9, 900.0, {"@"})]
    assert CI.close_print(tr) == (10.1, "M")
    assert CI.close_print(tr[:1] + tr[2:]) == (10.0, "6")
    assert CI.close_print(tr[2:]) == (None, None)


def test_returns_and_quote_age():
    r = CI.returns({"bid": 9.99, "ask": 10.01, "close": 10.10})
    assert abs(r["gross"] - (10.10 / 10.0 - 1)) < 1e-12 and abs(r["net"] - (10.10 / 10.01 - 1 - CI.EXIT_FEE)) < 1e-12
    assert CI.returns({"bid": 9.99, "ask": 10.01})["gross"] is None
    qs = [(100.0, 1, 2), (103.0, 1, 2)]
    assert CI.last_at(qs, 104.0, 5.0)[0] == 103.0 and CI.last_at(qs, 109.0, 5.0) is None


def _rows(lift, seed=1, days=20, half="A"):
    rng = random.Random(seed)
    rows = []
    for d in range(days):
        day = f"2026-0{6 if half == 'A' else 9}-{d + 1:02d}"
        for k in range(20):
            rows.append({"day": day, "sym": f"B{k}", "group": "BUY", "gross": lift + rng.gauss(0, 0.002),
                         "net": lift - 0.0005 + rng.gauss(0, 0.002)})
        for k in range(150):
            rows.append({"day": day, "sym": f"C{k}", "group": "CTRL", "gross": rng.gauss(0, 0.002), "net": None})
    return rows


def test_grade_and_verdicts():
    g = CI.grade(_rows(0.002))
    assert g["sessions"] == 20 and g["n_buy"] == 400 and g["lift"] > 0.0015 and g["t"] > 5
    assert CI.half_verdict(g) == "PASS"
    assert CI.half_verdict(dict(g, n_buy=299)) == "UNDERPOWERED"
    assert CI.half_verdict(CI.grade(_rows(-0.002))) == "FAIL"
    flat = CI.grade(_rows(0.0, seed=2))
    assert flat["ub"] < CI.LIFT_BAR and CI.half_verdict(flat) in ("NOT_PASSED", "FAIL")
    assert CI.final(["NOT_PASSED", "NOT_PASSED"], [flat["ub"]] * 2, [20, 20]) == "FAIL"
    assert CI.final(["PASS", "PASS"], [0.01, 0.01], [20, 20]) == "PASS"
    assert CI.final(["PASS", "UNDERPOWERED"], [0.01, None], [20, 1]) == "UNDERPOWERED"


def test_sessions_need_three_buys():
    rows = [{"day": "d1", "sym": "B1", "group": "BUY", "gross": 0.01}, {"day": "d1", "sym": "B2", "group": "BUY", "gross": 0.01},
            {"day": "d1", "sym": "C", "group": "CTRL", "gross": 0.0}]
    assert CI.session_diffs(rows) == {}
    rows.append({"day": "d1", "sym": "B3", "group": "BUY", "gross": 0.01})
    assert abs(CI.session_diffs(rows)["d1"] - 0.01) < 1e-12
