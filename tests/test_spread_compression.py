"""spread_compression: time-weighted median, the ratio rule, and day-and-hour pairing."""
from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.studies import spread_compression as sc  # noqa: E402


def _qs(a, b, spread_bp, n=40):
    mid = 100.0
    step = (b - a) / n
    h = mid * spread_bp / 2e4
    return [(a + i * step, mid - h, mid + h) for i in range(n)]


def test_time_weighted_median_and_min_quotes():
    q = _qs(0, 60, 10.0)
    assert abs(sc.tw_median(q, 0, 60) - 10.0) < 0.01
    assert sc.tw_median(q[:5], 0, 60) is None                                   # fewer than 20 quotes in the window
    q2 = _qs(0, 30, 10.0, 25) + _qs(30, 60, 4.0, 25)                            # 4 bp for the second half: the median sits at the boundary
    assert sc.tw_median(q2, 0, 60) in (10.0, 4.0) or 3.9 < sc.tw_median(q2, 0, 60) < 10.1


def test_ratio_compressed_vs_flat():
    t = 1000.0
    comp = _qs(t - 300, t - 60, 10.0, 60) + _qs(t - 60, t, 4.0, 40)
    flat = _qs(t - 300, t - 60, 10.0, 60) + _qs(t - 60, t, 10.0, 40)
    assert sc.ratio(comp, t) <= 0.7 < sc.ratio(flat, t)
    assert sc.ratio(_qs(t - 60, t, 4.0, 40), t) is None                         # no earlier window: not measurable


def _rows(n, planted, seed):
    rng = random.Random(seed)
    out = []
    for i in range(n):
        d = f"2026-10-{1 + i * 28 // n:02d}"
        comp = rng.random() < 0.3
        out.append({"d": d, "hr": 10 + rng.randint(0, 4), "comp": comp, "bp": rng.gauss(6 if (comp and planted) else -6, 15), "ng": rng.random() < 0.3})
    return out


def test_planted_effect_passes_null_fails_and_small_is_underpowered():
    assert sc.evaluate(_rows(1500, True, 1))["pass"]
    assert not sc.evaluate(_rows(1500, False, 2))["pass"]
    assert sc.evaluate(_rows(100, True, 3))["under"]
