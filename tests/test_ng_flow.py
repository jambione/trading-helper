from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools" / "studies"))
import ng_flow as N  # noqa: E402


def test_label_fixed_horizon_and_codes():
    t, e = 1000.0, 50.0
    fwd = [(1010.0, 50.01, [" "]), (1100.0, 50.02, [" "])]          # 50.02 < 50.025 -> never green
    assert N.ng_from_forward(fwd, t, e) is True
    assert N.ng_from_forward(fwd + [(1200.0, 50.03, [" "])], t, e) is False
    assert N.ng_from_forward(fwd + [(1400.0, 60.0, [" "])], t, e) is True        # after the 300 s window
    assert N.ng_from_forward([(1010.0, 60.0, ["Z"])], t, e) is None             # only a dropped (odd) condition code
    assert N.ng_from_forward([], t, e) is None


def test_score_needs_both_halves_and_does_not_leak_the_cut():
    import random
    rng = random.Random(1)
    rows = []
    for i in range(900):
        d = "2026-10-%02d" % (1 + i % 28 if i % 28 < 13 else 14 + i % 17 if 14 + i % 17 <= 30 else 30)
        ofi = rng.uniform(-1, 1)
        rows.append({"d": d, "sym": "A", "ofi": ofi, "ng": rng.random() < (0.55 if ofi < -0.33 else 0.38), "ng_peak": False,
                     "bp": rng.gauss(-8, 30), "t": float(i), "e": 50.0, "qi": 0.0, "micro": 0.0, "key": str(i)})
    res = N.score(rows)
    assert res["verdict"] in ("PASS_NG_ONLY", "PASS_NG_AND_PAYOFF", "FAIL", "UNDERPOWERED")
    assert res["ALL"]["ng_weak"] > res["ALL"]["ng_rest"]
    flat = [dict(r, ng=False) for r in rows]
    assert N.score(flat)["verdict"] == "FAIL"
