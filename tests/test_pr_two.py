"""pr_two: the shape of the last two bars. No market returns."""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))
import pr_two as T  # noqa: E402


def test_features_read_bars_m_m1_m2_only():
    bars = {600: (100.0, 102.0, 99.0, 101.0), 599: (99.0, 100.0, 98.0, 100.0), 598: (98.0, 99.0, 97.0, 98.0)}
    r, rp, l2 = T.features2(bars, 600)
    assert abs(r - 0.01) < 1e-12 and abs(rp - (100 / 99 - 1)) < 1e-12 and abs(l2 - (101 / 98 - 1)) < 1e-12
    first = T.features2(bars, 600)
    bars[601] = (1.0, 1.0, 1.0, 1.0)
    assert T.features2(bars, 600) == first
    del bars[598]
    assert T.features2(bars, 600)[2] is None
    del bars[599]
    assert T.features2(bars, 600) is None


def test_cutoffs_do_not_see_the_return():
    import inspect
    sig = inspect.signature(T.cutoffs2)
    assert list(sig.parameters) == ["r", "line2"]
    rng = np.random.default_rng(0)
    c = T.cutoffs2(rng.normal(0, 1e-3, 5000), rng.normal(0, 2e-3, 5000))
    assert c["r"][0] < 0 < c["r"][1] and c["floor"] > 0


def test_the_rows():
    cuts = {"floor": 1.0, "r": (-3.0, 3.0), "line2": (-5.0, 5.0)}
    r = np.array([2.0, 1.5, -2.0, -1.5, 4.0, -4.0, 0.5])
    rp = np.array([1.5, 2.0, -1.5, -2.0, -4.0, 4.0, 0.5])
    l2 = np.array([6.0, np.nan, -6.0, 0.0, 0.0, 0.0, 0.0])
    m = T.masks(r, rp, l2, cuts)
    assert list(np.flatnonzero(m["up2"][0])) == [0, 1]
    assert list(np.flatnonzero(m["accel_up"][0])) == [0] and list(np.flatnonzero(m["slow_up"][0])) == [1]
    assert list(np.flatnonzero(m["accel_down"][0])) == [2] and list(np.flatnonzero(m["slow_down"][0])) == [3]
    assert list(np.flatnonzero(m["turn_up"][0])) == [4] and list(np.flatnonzero(m["turn_down"][0])) == [5]
    assert list(np.flatnonzero(m["line2_hi"][0])) == [0] and not m["line2_hi"][1][1]


def _cache(tmp, plant):
    rng = np.random.default_rng(1)
    days = [f"2026-{mo:02d}-{d:02d}" for mo in (5, 8) for d in range(1, 31)]
    for d in days:
        n = 4000
        r, rp = rng.normal(0, 1e-3, n), rng.normal(0, 1e-3, n)
        l2 = r + rp
        y = rng.normal(0, 2e-3, n)
        if plant:
            acc = (r > 5e-4) & (rp > 5e-4) & (r > rp)
            y[acc] += 1.5e-3
        np.savez(os.path.join(tmp, f"{d}.npz"), r=r.astype(np.float32), rp=rp.astype(np.float32),
                 l2=l2.astype(np.float32), y=y.astype(np.float32), yc=y.astype(np.float32))
    fit = [d for d in days if d <= T.CUT]
    cols = [T._load(tmp, d) for d in fit]
    cuts = T.cutoffs2(np.concatenate([c[0] for c in cols]), np.concatenate([c[2] for c in cols]))
    per = {d: T._packs(tmp, d, cuts) for d in days}
    return T.report(per, days, cuts)


def test_a_planted_speed_up_reads_higher_and_noise_does_not(tmp_path):
    a, b = tmp_path / "plant", tmp_path / "noise"
    a.mkdir()
    b.mkdir()
    md = _cache(str(a), plant=True)
    words = [x for x in md.splitlines() if x.startswith("up2:")][0]
    assert "accel_up: HIGHER" in words, words
    md = _cache(str(b), plant=False)
    words = [x for x in md.splitlines() if x.startswith("up2:")][0]
    assert "HIGHER" not in words, words
