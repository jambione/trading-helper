"""relative_strength_scan: pure pieces (blocks, t, bootstrap, verdict) and score_day on a synthetic day with a planted leader effect."""
from __future__ import annotations

import random
import sys
from datetime import date
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.studies import relative_strength_scan as rs  # noqa: E402


def test_prev_weekday_and_blocks():
    assert rs.prev_weekday(date(2026, 10, 12)) == date(2026, 10, 9)          # Monday -> Friday
    assert rs.prev_weekday(date(2026, 10, 14)) == date(2026, 10, 13)
    b = rs.split_blocks([f"d{i:03d}" for i in range(120)])
    assert [len(v) for v in b.values()] == [72, 24, 24] and b["HELD-OUT"][0] == "d096"


def _day(syms, seed, plant=0.0, base_c=50.0):
    rng = np.random.default_rng(seed)
    n = len(syms)
    c = np.full((n, 390), np.nan, dtype=np.float32)
    o = np.full_like(c, np.nan)
    v = np.full_like(c, np.nan)
    for i in range(n):
        r = rng.normal(0, 0.0003, 390)
        if plant and i % 10 == 0:                      # a tenth of the names run early and keep running
            r[:30] += 0.0012
            r[30:120] += plant
        px = base_c * np.cumprod(1 + r)
        c[i] = px
        o[i] = np.concatenate([[base_c], px[:-1]])
        v[i] = 10000.0 + 100 * i
    return {"syms": syms, "idx": {s: i for i, s in enumerate(syms)}, "o": o, "h": c, "l": c, "c": c, "v": v}


def test_score_day_sees_a_planted_effect_and_a_null_does_not():
    syms = ["SPY"] + [f"S{i:03d}" for i in range(450)]
    prev = _day(syms, 1)
    hist = [_day(syms, 10 + k) for k in range(5)]
    day_p = _day(syms, 2, plant=0.0006)
    r = rs.score_day(day_p, prev, hist, set(), "2026-10-01")
    assert r and r["n_leaders"] >= 30 and r["lift"] > 20                         # leaders keep running: large positive lift
    day_n = _day(syms, 3, plant=0.0)
    r0 = rs.score_day(day_n, prev, hist, set(), "2026-10-02")
    assert r0 and abs(r0["lift"]) < 20


def _res(n, planted, seed):
    rng = random.Random(seed)
    out = {}
    for i in range(n):
        d = f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}"
        x = rng.gauss(15 if planted else -6, 25)
        out[d] = {"net_leaders": x, "net_univ": -6, "rnd": -6 + rng.gauss(0, 5), "lift": x + 6, "beta_lift": x + 6 + rng.gauss(0, 2),
                  "n_leaders": 40, "names": [(f"N{rng.randint(0, 80)}", x + rng.gauss(0, 10)) for _ in range(5)]}
    return out


def test_verdict_passes_a_planted_effect_and_fails_a_null():
    assert rs.verdict(_res(120, True, 1), stress=5.0)["pass"]
    assert not rs.verdict(_res(120, False, 2), stress=5.0)["pass"]
    assert not rs.verdict(_res(120, True, 1), stress=-1.0)["pass"]               # fails the entry-lag / 10 bp stress condition
