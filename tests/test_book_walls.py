"""Wall features, groups, fixed-effect lift, permutation and verdicts for book_walls. No market data."""
from __future__ import annotations

import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))

import book_walls as BW  # noqa: E402


def _row(bids, asks):
    r = {}
    for k, (p, s) in enumerate(bids):
        r[f"bid_px_{k:02d}"], r[f"bid_sz_{k:02d}"] = p, s
    for k, (p, s) in enumerate(asks):
        r[f"ask_px_{k:02d}"], r[f"ask_sz_{k:02d}"] = p, s
    return r


def test_window_from_touch_notional_wall_and_symmetric_classification():
    # $50 stock, 1-cent levels: 15 bp = 7.5 cents, so levels 50.01..50.08 cover the window from the touch 50.01
    asks = [(50.01 + 0.01 * k, 300.0) for k in range(10)]
    bids = [(50.00 - 0.01 * k, 300.0) for k in range(10)]
    f = BW.snapshot_features(_row(bids, asks))
    assert f["ask"]["classified"] and not f["ask"]["wall"] and f["bid"]["classified"]
    asks2 = list(asks)
    asks2[3] = (50.04, 2000.0)                         # 2000 >= 4 x 300 and $100k notional: a wall at +3 cents
    f2 = BW.snapshot_features(_row(bids, asks2))
    assert f2["ask"]["wall"] and abs(f2["ask"]["room"] - (50.04 / 50.01 - 1)) < 1e-12
    asks3 = list(asks)
    asks3[3] = (50.04, 400.0)                          # only 1.3 x median: not a wall
    assert not BW.snapshot_features(_row(bids, asks3))["ask"]["wall"]
    # $400 stock: 10 penny levels span 2.25 bp < 15 bp -> unclassified on BOTH outcomes (wall or not)
    hi_a = [(400.01 + 0.01 * k, 20.0) for k in range(10)]
    hi_b = [(400.00 - 0.01 * k, 20.0) for k in range(10)]
    assert not BW.snapshot_features(_row(hi_b, hi_a))["ask"]["classified"]
    hi_a[2] = (400.03, 500.0)                          # a $200k wall, still unclassified (window not fully visible)
    assert not BW.snapshot_features(_row(hi_b, hi_a))["ask"]["classified"]
    # odd lots on a high-priced name: 4 x median but under $25k is not a wall
    odd_a = [(400.01 + 0.2 * k, 7.0) for k in range(10)]
    odd_a[1] = (400.21, 40.0)
    f4 = BW.snapshot_features(_row(hi_b, odd_a))
    assert f4["ask"]["classified"] and not f4["ask"]["wall"]
    # thin book (fewer than 10 levels) = whole side visible
    assert BW.snapshot_features(_row(bids[:3], asks[:3]))["ask"]["classified"]


def test_groups_population_and_pulled():
    f = BW.snapshot_features(_row([(50.0, 300.0)] * 1, [(50.01, 300.0)]))
    assert BW.groups(f) == {"H1": "OPEN", "H2": "UNSUPPORTED"} and BW.in_population(f)
    assert not BW.in_population(dict(f, mid=19.99)) and not BW.in_population(dict(f, spread=0.0011))
    asks = [(50.01 + 0.01 * k, 300.0) for k in range(10)]
    asks[2] = (50.03, 3000.0)
    bids = [(50.00 - 0.01 * k, 300.0) for k in range(10)]
    before = BW.snapshot_features(_row(bids, asks))
    now = list(asks)
    now[2] = (50.03, 1000.0)
    assert BW.pulled(before, _row(bids, now)) is True
    assert BW.pulled(before, _row(bids, asks)) is False
    assert BW.pulled(BW.snapshot_features(_row(bids, [(50.01, 300.0)])), _row(bids, asks)) is None


def test_outcome_keys():
    q = {"X|1500000000": [9.99, 10.01], "X|301500000000": [10.09, 10.11]}
    assert abs(BW.outcome(q, "X", 1_000_000_000, 300) - 0.01) < 1e-12
    assert abs(BW.net_outcome(q, "X", 1_000_000_000, 300) - (10.09 / 10.01 - 1)) < 1e-12
    assert BW.outcome(q, "X", 1_000_000_000, 60) is None


def _rows(lift, days=25, seed=1):
    rng = random.Random(seed)
    rows = []
    for d in range(days):
        day = f"2026-06-{d + 1:02d}"
        for n in range(8):
            for k in range(3):
                g = "OPEN" if k == 0 else "WALLED"
                rows.append({"id": f"{day}_{n}_{k}", "day": day, "sym": f"S{n}", "g": {"H1": g, "H2": None},
                             "y": (lift if g == "OPEN" else 0.0) + rng.gauss(0, 0.002), "obi5_dm": rng.gauss(0, 1)})
    return rows


def test_fixed_effect_lift_and_permutation():
    rows = _rows(0.003)
    mu, t, days, ub, n_mixed, per_nd = BW.fe_lift(rows, "H1")
    assert days == 25 and n_mixed == 200 and abs(mu - 0.003) < 0.0006 and t > 5
    perm = BW.permute(rows, "H1", 50, "t")
    assert perm == BW.permute(rows, "H1", 50, "t") and abs(sum(perm) / len(perm)) < 0.002
    # a name-level difference (OPEN names are different stocks) does not enter the fixed-effect lift
    for r in rows:
        r["y"] += 0.01 if r["sym"] in ("S0", "S1") else 0.0
    assert abs(BW.fe_lift(rows, "H1")[0] - mu) < 1e-12


def test_verdicts():
    rows = _rows(0.003)
    g = BW.grade(rows, "H1", (-0.43, 0.43), 100, "t")
    assert BW.half_verdict(g) == "PASS"
    assert BW.half_verdict(dict(g, mixed_namedays=99)) == "UNDERPOWERED"
    assert BW.half_verdict(dict(g, obi_terciles=[0.001, -0.0001, 0.002])) == "NOT_PASSED"
    assert BW.half_verdict(BW.grade(_rows(-0.003), "H1", (-0.43, 0.43), 50, "t")) == "FAIL"
    assert BW.final(["PASS", "PASS"], [0.01, 0.01], [25, 25]) == "PASS (LEAD)"
    assert BW.final(["NOT_PASSED", "NOT_PASSED"], [0.0004, 0.0003], [25, 25]) == "FAIL"
