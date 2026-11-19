import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "studies"))

import opening_grid as og  # noqa: E402


def test_bracket_all_matches_desk_bracket():
    from holistic_entry import bracket, COST
    rng = np.random.default_rng(1)
    for _ in range(5):
        c = 50 * np.exp(np.cumsum(rng.normal(0, 0.003, 390)))
        o = np.concatenate(([c[0]], c[:-1]))
        h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.002, 390)))
        l = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.002, 390)))
        for arr in (o, h, l, c):
            arr[rng.integers(0, 390, 20)] = np.nan
        g = og.bracket_all(o, h, l, c)
        for t in range(0, 375, 7):
            b = bracket(o, h, l, c, t)
            if b is None:
                assert not np.isfinite(g[t])
            else:
                assert abs(g[t] - (b + COST)) < 1e-9


def test_orb_cross_first_cross_only():
    h = np.array([10, 10.2, 10.1, 10.0, 10.1] + [10.0] * 10)
    c = np.array([10.0] * 5 + [10.1, 10.3, 10.1, 10.4, 10.0, 10.5, 10, 10, 10, 10])
    assert og.orb_cross(h, c) == 6


def test_pace_same_minute():
    v = np.array([10, 10, 10, 10.0])
    pv = np.array([5, 5, np.nan, 5.0])
    p = og.pace_series(v, pv, n=4)
    assert p[0] == 2.0 and abs(p[2] - 3.0) < 1e-12


def test_neighbours_shapes():
    c = og.make_cell("ORB", 1, 14, 4.0, False, 1.5)
    ids = [n["id"] for n in og.neighbours(c)]
    assert len(ids) == len(set(ids)) and c["id"] not in ids
    assert len(ids) == 7          # lo-5 clips to 1 = itself and is dropped; lo+5, hi-5, hi+5, chg 3/6, pace 1/2
    assert len(og.neighbours(og.make_cell("ORB", 15, 29, 4.0, True, None))) == 6


def test_draw_gate_cap_and_missing():
    cands = [("d", f"S{i}", 1, 10.0) for i in range(10)]
    sp = {f"S{i}": (20.0 if i % 2 else 5.0) for i in range(10)}
    tr, miss = og.draw(cands, lambda d, s, t: sp[s], cap=3)
    assert len(tr) == 3 and all(r["spread"] == 5.0 for r in tr) and not miss
    tr, miss = og.draw(cands, lambda d, s, t: "missing", cap=3)
    assert not tr and len(miss) == 3


def test_choose_needs_min_n_and_picks_t():
    st = {"a": {"n": 400, "t_net": 2.0, "usd_per_day_approx": 1}, "b": {"n": 299, "t_net": 9.0},
          "c": {"n": 350, "t_net": 3.0, "usd_per_day_approx": 0}}
    assert og.choose(st) == "c"
    assert og.choose({"b": {"n": 10, "t_net": 5}}) is None


def _day(chg, sig, gross=5.0):
    n = len(chg)
    return {"syms": [f"S{i}" for i in range(n)], "chg": np.tile(np.array(chg, float)[:, None], (1, og.T_SIG)),
            "px": np.full((n, og.T_SIG), 50.0), "pace": np.full((n, og.T_SIG), 2.0),
            "sig": np.array(sig, np.uint8), "gross": np.full((n, og.T_GROSS), gross), "spy": np.zeros(og.T_GROSS)}


def test_candidates_first_bar_in_window_and_filter():
    sig = np.zeros((2, og.T_SIG), np.uint8)
    sig[0, [3, 8, 20]] = og.BITS["ORB"]
    sig[1, 5] = og.BITS["ORB"]
    d = _day([2.0, 6.0], sig)
    c1 = og.make_cell("ORB", 4, 14, 4.0, False, None)
    assert [(s, t) for _, s, t, _ in og.candidates({"x": d}, c1)] == [("S0", 8)]
    c3 = og.make_cell("ORB", 1, 14, 4.0, True, None)
    assert [(s, t) for _, s, t, _ in og.candidates({"x": d}, c3)] == [("S1", 5)]
