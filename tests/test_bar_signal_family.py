import sys
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "studies"))
import bar_signal_family as b


def test_volume_bars_one_per_minute_with_carry():
    v = [500, 10, 10, 1000, 10]            # minute 0 alone is 5x the size; minute 3 is 10x
    h = [2, 2, 2, 2, 2]; l = [1, 1, 1, 1, 1]; c = [1.5] * 5
    bars = b.volume_bars(v, h, l, c, 100)
    assert [x[0] for x in bars] == [0, 3]           # one bar per crossing minute, overshoot carried


def test_wr_smoothed_nan_until_21():
    x = np.linspace(10, 11, 40)
    w = b.wr_smoothed(x + 0.1, x - 0.1, x)
    assert np.isnan(w[19]) and np.isfinite(w[25])


def test_v1_cond_leaving_oversold():
    assert b.v1_cond([-90, -85, -70, -45])
    assert not b.v1_cond([-60, -55, -45])
    assert not b.v1_cond([-90, -85, -70, -60])


def test_transitions_fire_once():
    cond = np.zeros(390, dtype=bool); cond[100:105] = True
    assert b.transitions(cond) == [100]


def test_refract_keeps_first_and_restarts_from_kept():
    kept, supp = b.refract([100, 110, 129, 130, 160])
    assert kept == [100, 130, 160] and supp == [110, 129]


def _day(n=390):
    z = np.full((1, n), 10.0, dtype=np.float32)
    return {"o": z.copy(), "h": z + 0.01, "l": z - 0.01, "c": z.copy(), "v": np.full((1, n), 1000.0, dtype=np.float32)}


def test_k_absorption_triggers_on_planted_bar():
    day = _day()
    c = day["c"][0]; c[:] = np.linspace(11.0, 10.0, 390)           # about -36 bp per 15 bars
    day["h"][0] = c + 0.10; day["l"][0] = c - 0.10; day["o"][0] = c
    t = 100
    lo = float(np.min(day["l"][0][t - 14:t])) - 0.001
    day["v"][0][t] = 5000; day["l"][0][t] = lo; day["h"][0][t] = lo + 0.01; day["c"][0][t] = lo + 0.008
    full, part = b.k_masks(day, 0)
    assert full[t] and not full[t - 1]


def test_w_info_washout_and_cross():
    day = _day()
    day["o"][0][0] = 10.0
    day["l"][0][5] = 9.85
    day["c"][0][14] = 9.95
    day["c"][0][20] = 10.05
    wash, cross, below = b.w_info(day, 0)
    assert wash and below and cross == 20


def test_w_no_washout_when_recovered_by_0944():
    day = _day()
    day["l"][0][5] = 9.85
    day["c"][0][14] = 10.02
    assert not b.w_info(day, 0)[0]


def test_d_requires_fresh_breakout_and_three_knocks():
    day = _day()
    h = day["h"][0]; l = day["l"][0]; c = day["c"][0]
    h[:] = 10.00; l[:] = 9.95; c[:] = 9.97
    for k in (40, 45, 50):                       # three knocks at 10.10 with pullbacks between
        h[k] = 10.10
    h[40] = 10.10
    c[60] = 10.12; h[60] = 10.13
    ev = dict(b.d_events(day, 0))
    assert ev.get(60, 0) >= 3
    c[59] = 10.12                                # not fresh any more
    assert 60 not in dict(b.d_events(day, 0))


def test_evaluate_not_tested_makes_underpowered():
    tr = [{"d": f"2026-0{1 + i % 9}-1{i % 9}", "sym": f"S{i % 50}", "t": 100, "net": 1.0, "net10": 0.5, "h3": 0.0, "h30": 0.0, "cb": None, "ca": 0.0} for i in range(400)]
    r = b.evaluate(tr, ["ca", "cb"], "cb")
    assert r["status"] == "UNDERPOWERED" and r["not_tested"] == ["cb"]


def test_controls_exclude_minutes_before_a_trigger():
    m = b.eligible_mask([100], [])
    assert not m[85] and not m[99] and not m[100] and not m[111]
    assert m[84] and m[113]
