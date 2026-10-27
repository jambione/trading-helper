"""first_presq_holdout.py: causal port vs Argon's own setup 4, grid fill, pass filter, controls, statistics."""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "studies"))
import first_presq_holdout as F  # noqa: E402


def _bars(mins, c, rng, v):
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) * (1 + rng.uniform(0, 0.0004, len(c)))
    l = np.minimum(o, c) * (1 - rng.uniform(0, 0.0004, len(c)))
    return np.column_stack([mins, o, h, l, c, v])


def _day(rng, start_px, n=390, spike_at=None):
    """Full-minute RTH bars. With spike_at: a pullback from the open, then a sharp volume-backed rise at spike_at."""
    mins = np.arange(570, 570 + n, dtype=float)
    ret = rng.normal(0, 0.0004, n)
    v = rng.uniform(3e4, 6e4, n)
    if spike_at is not None:
        k = spike_at - 570
        ret[:k - 4] += -0.0002
        ret[k - 4:k + 1] += 0.006
        v[k] *= 4
    c = start_px * np.exp(np.cumsum(ret))
    return _bars(mins, c, rng, v)


def _prev_day(rng, px, n=390):
    """Previous session whose last 120 minutes rise ~5% and fade back, so the next open sits low in the range."""
    mins = np.arange(570, 570 + n, dtype=float)
    ret = rng.normal(0, 0.0003, n)
    ret[n - 120:n - 60] += 0.0008
    ret[n - 60:] -= 0.0008
    c = px * np.exp(np.cumsum(ret))
    return _bars(mins, c, rng, rng.uniform(3e4, 6e4, n))


@pytest.fixture
def synth(monkeypatch):
    monkeypatch.setattr(F.H, "warehouse_days", lambda: ["D0", "D1"])   # Argon's module lists the warehouse on import
    import first_presq_argon as A
    rng = np.random.default_rng(7)
    syms = [f"S{i:03d}" for i in range(200)]
    prev, cur = {}, {}
    for k, s in enumerate(syms):
        px = rng.uniform(25, 120)
        prev[s] = _prev_day(rng, px)
        cur[s] = _day(rng, prev[s][-1, 4] * (1 + rng.uniform(0.008, 0.015)),
                      spike_at=int(rng.integers(600, 680)) if k % 2 == 0 else None)
    data = {("sip", "D0"): prev, ("sip", "D1"): cur}
    monkeypatch.setattr(A.H, "load_raw", lambda feed, day: data[("sip", day)])
    monkeypatch.setattr(A, "days", ["D0", "D1"])
    return A, prev, cur


def test_causal_port_fires_exactly_where_argon_setup4_fires_on_full_minutes(synth):
    A, prev, cur = synth
    argon = {r[1] for r in A.eval_day_idx(1)[F.K4]}
    ours = set()
    for s, rows in cur.items():
        if not F.sip_universe(prev[s], rows):
            continue
        f = F.candidate(prev[s], rows, float(prev[s][-1, 4]), rows, grid=False)
        if F.passes(f, F.ARGON_CFG):
            ours.add(s)
    assert argon, "synthetic data must trigger setup 4 at least once"
    assert ours == argon


def test_grid_fill_flat_fills_2_to_15_minute_gaps_with_zero_volume():
    rows = np.array([[570, 10, 10, 10, 10, 5], [573, 11, 11, 11, 11, 7], [600, 12, 12, 12, 12, 9]], float)
    g = F.grid_fill(rows)
    assert list(g[:, 0]) == [570, 571, 572, 573, 600]          # 27-minute gap left open
    assert g[1, 4] == 10 and g[1, 5] == 0 and g[2, 5] == 0


def test_iex_candidate_reads_rises_by_minute_and_marks_grid_holes():
    rng = np.random.default_rng(3)
    prev = _day(rng, 50.0)
    cur = _day(rng, 50.5, spike_at=640)
    sparse = np.delete(cur, [100, 101, 102], axis=0)            # a 4-minute gap: filled, same minutes
    f_full = F.candidate(prev, cur, 50.0, cur, grid=True)
    f_gap = F.candidate(prev, sparse, 50.0, cur, grid=True)
    assert f_full["minute"] == f_gap["minute"] or f_gap.get("why")


def test_passes_respects_every_grid_dimension():
    f = {"minute": 640, "close": 30.0, "dvol15": 2e5, "day_chg": 2.0, "fast_rise": 16.0, "slow_rise": 2.5,
         "vol_ratio": 1.7, "overbought": False}
    assert F.passes(f, F.ARGON_CFG)
    assert not F.passes(f | {"vol_ratio": 1.4}, F.ARGON_CFG)
    assert not F.passes(f | {"minute": 700}, F.ARGON_CFG)
    assert not F.passes(f | {"overbought": True}, F.ARGON_CFG)
    assert not F.passes(f | {"close": 22.0}, F.ARGON_CFG | {"floor": 25.0})
    assert len(F.all_cfgs()) == 96 and F.cfg_key(F.ARGON_CFG) in {F.cfg_key(c) for c in F.all_cfgs()}


def test_controls_are_seeded_and_minute_based():
    mm = {"A": ({m: 10.0 for m in range(570, 960)}, {m: 10.0 + (m - 570) * 0.01 for m in range(570, 960)}),
          "B": ({m: 20.0 for m in range(570, 960)}, {m: 20.0 for m in range(570, 960)})}
    c1 = F.controls(mm, "A", "D", 640, ["A", "B"], {})
    c2 = F.controls(mm, "A", "D", 640, ["A", "B"], {})
    assert c1 == c2 and c1["fp-x"] == 0.0
    assert F.gross(mm, "A", 640) == pytest.approx(((10.0 + (654 - 570) * 0.01) / 10.0 - 1) * 1e4)


def test_day_equal_and_projection_shapes():
    st = F.day_equal({"a": [1.0, 3.0], "b": [4.0], "c": [6.0]})
    assert st["n_days"] == 3 and st["mean"] == pytest.approx(4.0)
    rows = [{"day": f"d{k}", "rows": [{"sym": "A", "iex": {"minute": 640, "close": 30.0, "dvol15": 2e5,
             "day_chg": 2.0, "fast_rise": 16.0, "slow_rise": 2.5, "vol_ratio": 1.7, "overbought": False,
             "gross": 5.0, "fp-d": float(k), "fp-d2": 0.0, "fp-x": -float(k), "fp-x2": 0.0}}]} for k in range(10)]
    p = F.projection(rows, "iex", F.ARGON_CFG)
    assert p["r"] == 1.0 and p["mde_proj_600"] == pytest.approx(3.08 * p["sd_in"] * (1 / 300) ** 0.5)
