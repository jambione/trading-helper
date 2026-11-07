"""board_structure_family H1/H2 pieces: slow %R, trade return, refractory, H2 trigger on a synthetic day, evaluation order."""
from __future__ import annotations

import random
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.studies import board_structure_family as bs  # noqa: E402


def test_slow_wr_bounds_and_rising_close_reads_high():
    c = np.linspace(10, 11, 390)
    w = bs.slow_wr(c, c + 0.01, c - 0.01, c + 1, c + 1.01, c + 0.99)
    assert np.nanmax(w) <= 0 and np.nanmin(w) >= -100 and w[-1] > -20


def test_trade_return_and_missing_bars():
    day = {"o": np.full((1, 390), 10.0), "c": np.full((1, 390), 10.0)}
    day["c"][0, 100 + 1 + bs.HOLD] = 10.1
    assert abs(bs.trade_ret(day, 0, 100, 6.0) - (100.0 - 6.0)) < 1e-6
    day["o"][0, 201] = np.nan
    assert bs.trade_ret(day, 0, 200, 6.0) is None


def test_refractory_keeps_one_per_30_bars():
    assert bs.refract([10, 15, 39, 40, 75]) == [10, 40, 75]


def _day(syms, n=390):
    m = {k: np.full((len(syms), n), np.nan) for k in ("o", "h", "l", "c", "v")}
    return {"syms": syms, "idx": {s: i for i, s in enumerate(syms)}, **m}


def test_h2_triggers_on_undercut_and_reclaim_when_green_and_above_vwap():
    syms = ["SPY", "AAA", "BBB"]
    prev, day = _day(syms), _day(syms)
    for i in range(3):
        prev["c"][i] = 9.0
        day["o"][i] = 10.0; day["h"][i] = 10.02; day["l"][i] = 9.99; day["c"][i] = 10.0; day["v"][i] = 1000
    i = 1
    day["c"][i, 30:] = 10.10; day["o"][i, 30:] = 10.10; day["h"][i, 30:] = 10.11; day["l"][i, 30:] = 10.09
    t = 60
    day["l"][i, t - 1] = 10.09 * (1 - 0.0010)       # undercuts the prior 15-bar low by 10 bp
    day["v"][i, t - 1] = 5000                       # on 5x volume
    day["c"][i, t] = 10.10                          # the next bar closes back above the prior low
    tr = bs.score_h2(day, prev, ["AAA", "BBB"], "2026-10-01")
    assert any(x["sym"] == "AAA" for x in tr)


def test_evaluation_underpowered_and_pass_fail():
    rng = random.Random(3)
    few = [{"d": f"2026-0{5 + i % 4}-{1 + i % 28:02d}", "sym": f"S{i % 30}", "net": 5.0, "net10": 1.0, "ca": 0.0, "cb": 0.0} for i in range(100)]
    assert bs.evaluate(few)["status"] == "UNDERPOWERED"
    many = [{"d": f"2026-0{5 + (i // 100) % 5}-{1 + i % 28:02d}", "sym": f"S{i % 80}", "net": rng.gauss(12, 20), "net10": rng.gauss(8, 20),
             "ca": rng.gauss(-4, 5), "cb": rng.gauss(-4, 5)} for i in range(900)]
    assert bs.evaluate(many)["status"] == "PASS"
    null = [{**x, "net": rng.gauss(-5, 20), "net10": -9.0} for x in many]
    assert bs.evaluate(null)["status"] == "FAIL"
