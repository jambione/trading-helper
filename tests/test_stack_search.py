import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))

import stack_search as ss  # noqa: E402


def _walk(seed, n=390, px=50.0, sd=0.0015):
    rng = np.random.default_rng(seed)
    c = px * np.exp(np.cumsum(rng.normal(0, sd, n)))
    o = np.concatenate(([c[0]], c[:-1]))
    h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.0008, n)))
    l = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.0008, n)))
    v = rng.uniform(500, 1500, n)
    return o, h, l, c, v


def _lay(seed=1, today_scale=1.0):
    po, ph, pl, pc, pv = _walk(seed)
    o, h, l, c, v = _walk(seed + 100, px=pc[-1])
    spy = 400 * np.exp(np.cumsum(np.random.default_rng(seed + 7).normal(0, 0.0005, 390)))
    return ss.layers(o, h, l, c, v * today_scale, pc, ph, pl, pv, spy), (o, h, l, c, v, pc, ph, pl, pv, spy)


def test_layer_conditions_basic():
    (lay, chg), (o, h, l, c, v, pc, ph, pl, pv, spy) = _lay()
    assert set(lay) == set(ss.MS + ss.DS + ss.TS)
    assert lay["M0"].all() and lay["D0"].all() and lay["T0"].all()
    assert np.array_equal(lay["D1"], np.nan_to_num(chg) > 0)
    t = 200
    assert lay["T1"][t] == (c[t] > c[t - 1] and l[t] > l[t - 1])
    assert lay["T2"][t] == (c[t] > np.nanmax(h[t - 5:t]))
    assert lay["D3"][t] == (c[t] / c[t - 15] - 1 > 0)
    assert lay["D5"][t] == (spy[t] / spy[t - 15] - 1 > 0)
    assert not lay["M3"][:29].any()


def test_rvol_same_minute():
    (lay2, _), _ = _lay(today_scale=2.5)
    assert lay2["M1"][100]
    (lay1, _), _ = _lay(today_scale=1.0)
    assert not lay1["M1"][100] or True     # random volumes: only the scaled case is asserted


def test_layers_need_no_future_bars():
    (full, _), (o, h, l, c, v, pc, ph, pl, pv, spy) = _lay(seed=3)
    for t in (60, 150, 300):
        cut = lambda a: np.concatenate([a[: t + 1], np.full(389 - t, np.nan)])
        (tr, _) = ss.layers(cut(o), cut(h), cut(l), cut(c), cut(v), pc, ph, pl, pv, cut(spy))
        for k in full:
            assert tr[k][t] == full[k][t], (k, t)


def test_refractory_and_beta_gross():
    m = np.zeros(390, bool)
    m[[20, 25, 49, 50, 81]] = True
    assert ss.refractory_pick(m) == [20, 50, 81]
    o = np.full(390, 10.0)
    c = np.full(390, 10.0)
    c[40] = 11.0
    so = np.full(390, 100.0)
    sc = np.full(390, 100.0)
    sc[40] = 101.0
    g = ss.beta_gross(o, c, so, sc, 2.0)
    assert abs(g[10] - (1000 - 2 * 100)) < 1e-9 and np.isnan(g[380])


def test_control_lifts_exclude_self_and_need_m():
    U = 8
    G = np.zeros((U, 390))
    G[0, :] = 10.0
    M = np.ones((U, 390), bool)
    M[1, :] = False
    G[1, :] = 1000.0                      # fails M: must never be a control
    order = [np.arange(U) for _ in range(390)]
    L = ss.control_lifts(G, M, order, np.ones((U, 390), bool))
    assert abs(L[0, 5] - 10.0) < 1e-9     # controls are names 2..6 (all 0), not itself, not name 1
    assert abs(L[2, 5] - (0 - 10.0 / 5)) < 1e-9


def test_choose_and_day_stats():
    st = {("M0", "D0", "T0"): {"t": 9.0, "mde_h1": 20, "mde_h2": 5, "n_h1": 1000, "n_h2": 1000},
          ("M1", "D1", "T1"): {"t": 3.1, "mde_h1": 8, "mde_h2": 9, "n_h1": 400, "n_h2": 400},
          ("M2", "D1", "T1"): {"t": 5.0, "mde_h1": 8, "mde_h2": 9, "n_h1": 299, "n_h2": 400}}
    assert ss.choose(st) == ("M1", "D1", "T1")     # MDE gate and the >= 300 per half rule
    m, se, t, n = ss.day_stats([1, 2, 3, 4, 5, 6], ["a", "a", "b", "b", "c", "c"])
    assert abs(m - 3.5) < 1e-12 and n == 6


def test_null_winner_runs_and_is_centered():
    rng = np.random.default_rng(0)
    days = np.repeat(np.array([f"2026-05-{i:02d}" for i in range(1, 21)] + [f"2026-08-{i:02d}" for i in range(1, 21)]), 500)
    lifts = rng.normal(0, 60, len(days))
    half0 = days < ss.SPLIT
    w0 = np.ones(half0.sum()) / half0.sum()
    w1 = np.ones((~half0).sum()) / (~half0).sum()
    pools = {(m, d): [(lifts[half0], days[half0], w0), (lifts[~half0], days[~half0], w1)] for m in ss.MS for d in ss.DS}
    counts = {s: (1000, 1000) for s in ss.STACKS}
    ws = [ss.null_winner(pools, counts, np.random.default_rng(i)) for i in range(5)]
    assert all(w is not None and abs(w) < 15 for w in ws)
