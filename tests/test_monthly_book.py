"""monthly_book: price-only monthly rules. Synthetic panels only."""
import os
import random
import sys
from datetime import date, timedelta

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))
import monthly_book as M  # noqa: E402


def test_month_ends():
    cal = ["2021-01-28", "2021-01-29", "2021-02-01", "2021-02-26", "2021-03-01"]
    assert M.month_ends(cal) == ["2021-01-29", "2021-02-26", "2021-03-01"]


def test_scores_read_only_the_history_given():
    c = np.linspace(50, 100, 300)
    s = M.scores(c)
    assert s["R2_near_52w_high"] == 1.0 and s["R4_trend_low_vol"] is not None
    assert abs(s["R5_one_month_reversal"] + (c[-1] / c[-22] - 1)) < 1e-12
    assert M.scores(c[:200]) == dict.fromkeys(M.RULES)
    down = np.linspace(100, 50, 300)
    assert M.scores(down)["R4_trend_low_vol"] is None


def test_pick_and_turnover():
    assert M.pick({"A": 1.0, "B": 3.0, "C": None, "D": 2.0}, n=2) == ["B", "D"]
    assert M.turnover([], ["A", "B"]) == 1.0
    assert M.turnover(["A", "B"], ["A", "B"]) == 0.0
    assert abs(M.turnover(["A", "B"], ["A", "C"]) - 1.0) < 1e-12


def test_ols_recovers_alpha_and_beta():
    rng = np.random.default_rng(0)
    x = rng.normal(0.01, 0.04, 200)
    y = 0.005 + 1.5 * x + rng.normal(0, 0.001, 200)
    r = M.ols(list(y), list(x))
    assert abs(r["alpha"] - 0.005) < 5e-4 and abs(r["beta"] - 1.5) < 0.02 and r["t"] > 10


def test_words():
    a = {"alpha": 0.01}
    assert M.word(a, a, {"t": 3.0}, 0.005, 0.01, 0.01) == "PASS"
    assert M.word(a, a, {"t": 2.0}, 0.005, 0.01, 0.01) == "PROMISING"
    assert M.word(a, a, {"t": 3.0}, -0.001, 0.01, 0.01) == "FAIL"
    assert M.word(a, {"alpha": -0.001}, {"t": 3.0}, 0.005, 0.01, 0.01) == "FAIL"


def _panel(plant: float, n_names=120, seed=1):
    """Random-walk stocks on a weekday calendar 2015-12..2026-10. With plant > 0, the 20 names nearest their
    52-week high at each month-end (from closes before that day) earn `plant` extra over the next month."""
    rng = random.Random(seed)
    cal, d = [], date(2015, 12, 1)
    while d <= date(2026, 10, 15):
        if d.weekday() < 5:
            cal.append(d.isoformat())
        d += timedelta(days=1)
    ends = set(M.month_ends(cal))
    px = {f"S{i:03d}": [50.0 + rng.random() * 50] for i in range(n_names)}
    spy = [100.0]
    boost: dict[str, float] = {}
    for k in range(1, len(cal)):
        m = rng.gauss(0.0003, 0.01)
        spy.append(spy[-1] * (1 + m))
        for s, p in px.items():
            p.append(p[-1] * (1 + m + rng.gauss(0, 0.015) + boost.get(s, 0.0)))
        if cal[k] in ends and plant and k > M.HIST:
            sc = {s: M.scores(np.array(p[:k]))["R2_near_52w_high"] for s, p in px.items()}
            top = set(M.pick(sc))
            boost = {s: (plant / 21 if s in top else 0.0) for s in px}
    adj = {s: {cal[i]: (p[i], p[i]) for i in range(len(cal))} for s, p in px.items()}
    adj["SPY"] = {cal[i]: (spy[i], spy[i]) for i in range(len(cal))}
    liquid = {s: list(cal) for s in px}
    return M.Panel({"adj": adj, "liquid": liquid, "log": []})


def test_a_planted_52_week_high_edge_reads_pass_and_noise_does_not():
    md = M.report(M.run(_panel(0.02))[0])
    head = next(x for x in md.splitlines() if x.startswith("R1_"))
    assert "R2_near_52w_high: PASS" in head, head
    md = M.report(M.run(_panel(0.0))[0])
    head = next(x for x in md.splitlines() if x.startswith("R1_"))
    assert "PASS" not in head, head


def test_the_pick_does_not_see_a_name_that_stops_trading():
    p = _panel(0.0, n_names=40)
    ends = M.month_ends(p.cal)
    t = next(x for x in ends if x >= M.A_FIRST)
    # Make one name the best R2 pick at t and then never print again.
    s = "S000"
    for d in list(p.adj[s]):
        if d < t:
            p.adj[s][d] = (1.0 + 1e-6 * p.cal.index(d), 1.0 + 1e-6 * p.cal.index(d))
        elif d > t:
            del p.adj[s][d]
    p.dates[s] = sorted(p.adj[s])
    p.closes[s] = np.array([p.adj[s][x][1] for x in p.dates[s]])
    out, _ = M.run(p)
    first = next(m for m in out["R2_near_52w_high"] if m["t"] == t)
    assert s in first["book"] and first["delisted"] >= 1


def test_a_series_with_a_long_hole_is_excluded():
    p = _panel(0.0, n_names=10)
    s = "S001"
    for d in p.cal[400:440]:
        p.adj[s].pop(d, None)
    q = M.Panel({"adj": p.adj, "liquid": {k: sorted(v) for k, v in p.liquid.items()}, "log": []})
    assert s in q.joined and "S002" not in q.joined


def test_stop_sells_at_the_next_open_not_the_trigger_close():
    path = [("d1", 100.0, 95.0), ("d2", 94.0, 89.0), ("d3", 85.0, 86.0), ("d4", 86.0, 120.0)]
    r, hit = M.stop_return(path, 100.0, "stop10")
    assert hit and abs(r - (85.0 / 100 - 1)) < 1e-12
    r, hit = M.stop_return([("d1", 100.0, 105.0), ("d2", 105.0, 110.0)], 100.0, "stop10", final=111.0)
    assert not hit and abs(r - 0.11) < 1e-12


def test_trailing_stop_follows_the_best_close():
    path = [("d1", 100.0, 120.0), ("d2", 119.0, 107.0), ("d3", 106.0, 108.0)]
    r, hit = M.stop_return(path, 100.0, "trail10")
    assert hit and abs(r - 0.06) < 1e-12           # 107 < 108 (= 120 x 0.9): sold at d3's open, 106
    r, hit = M.stop_return(path, 100.0, "stop10", final=108.0)
    assert not hit and abs(r - 0.08) < 1e-12
