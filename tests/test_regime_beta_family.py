"""regime_beta_family: HAC t, the R5 evaluation order, B2 evaluation and the hedged-trade math."""
from __future__ import annotations

import random
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.studies import regime_beta_family as rb  # noqa: E402


def test_hac_t_sees_a_planted_dummy_effect():
    rng = np.random.default_rng(1)
    x = (rng.random(120) > 0.5).astype(float)
    y = 5 * x + rng.normal(0, 4, 120)
    assert rb.hac_t(y, x) > 4
    assert abs(rb.hac_t(rng.normal(0, 4, 120), x)) < 3


def _r5(n, planted, seed):
    rng = random.Random(seed)
    out = []
    for i in range(n):
        m = rng.gauss(0, 10)
        net = rng.gauss(-6 + (12 if (planted and m > 0) else 0), 8)
        out.append({"d": f"2026-{5 + i // 28:02d}-{1 + i % 28:02d}", "M": m, "net": net, "hedged": net - 1, "net10": net - 4, "lift": net + 6 + rng.gauss(0, 2), "n_morning": 40, "n_aft": 30})
    return out


def test_r5_pass_fail_underpowered():
    assert rb.evaluate_r5(_r5(120, True, 1))["status"] == "PASS"
    assert rb.evaluate_r5(_r5(120, False, 2))["status"] == "FAIL"
    assert rb.evaluate_r5(_r5(25, True, 3))["status"] == "UNDERPOWERED"


def test_hold_trade_hedged_math():
    day = {"o": np.full((2, 390), 10.0), "c": np.full((2, 390), 10.0), "idx": {"SPY": 0, "AAA": 1}}
    day["c"][1, 111] = 10.10            # stock +100 bp over the hold
    day["c"][0, 111] = 10.0 * 1.001     # SPY +10 bp
    net, hed = rb.hold_trade(day, 1, 100, 10, beta=1.5)
    assert abs(net - (100 - 6)) < 1e-6 and abs(hed - (100 - 1.5 * 10 - 6)) < 1e-6


def _b2(n, planted, seed):
    rng = random.Random(seed)
    out = []
    for i in range(n):
        net = rng.gauss(14 if planted else -6, 25)
        out.append({"d": f"2026-{5 + (i // 12) % 5:02d}-{1 + (i // 12) % 28:02d}", "sym": f"S{i % 70}", "net": net, "hedged": net - 2 if planted else net, "net10": net - 4,
                    "ca": rng.gauss(-4, 6), "cb": rng.gauss(-4, 6), "spy_up": i % 2 == 0})
    return out


def test_b2_pass_fail_and_void():
    assert rb.evaluate_b2(_b2(1200, True, 1), 0)["status"] == "PASS"
    assert rb.evaluate_b2(_b2(1200, False, 2), 0)["status"] == "FAIL"
    assert rb.evaluate_b2(_b2(1200, True, 1), 400)["status"] == "VOID"
