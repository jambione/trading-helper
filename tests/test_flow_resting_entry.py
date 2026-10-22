"""Fill rule, simulation, drops and verdicts for flow_resting_entry. No market data."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))

import flow_resting_entry as F  # noqa: E402

O = F.O


def test_round_lot_rule():
    assert F.counts_for_fill([" "]) and F.counts_for_fill(["@", "F"])
    assert not F.counts_for_fill(["@", "I"]) and not F.counts_for_fill(["F", "I"])     # any 'I' disqualifies
    assert not F.counts_for_fill(["@", "4"]) and not F.counts_for_fill([])


def test_fill_time_back_of_queue():
    tr = [(101.0, 20.00, 300, ["@"], "Q"),      # AT the limit: queue ahead of us, no fill
          (105.0, 19.99, 50, ["@"], "D"),       # below, 50 of 100
          (106.0, 19.99, 40, ["@", "I"], "Q"),  # odd lot: does not count
          (110.0, 19.98, 60, ["@"], "Q")]       # reaches 110 >= 100
    assert F.fill_time(tr, 20.00, 100, 100.0, 30) == 110.0
    assert F.fill_time(tr, 20.00, 100, 100.0, 8) is None                     # window ends before enough size
    assert F.fill_time(tr, 20.00, 100, 100.0, 30, lit_only=True) is None     # the TRF print (D) excluded -> 60 only
    assert F.fill_time(tr, 20.00, 60, 100.0, 30, min_below=0.015) == 110.0   # only the 19.98 print is >= 1.5c below


def _e(day="2026-06-02", m=600):
    return {"day": day, "sym": "AAA", "m": m, "half": "A", "groups": {"G1": "IN"}}


def test_simulate_fill_unfilled_and_drop():
    e = _e()
    T = O.decision_ts(e["day"], e["m"])
    fq = [(T - 5, 20.00, 20.01, 5, 5)]
    oq = [(T - 5, 20.00, 20.01, 5, 5), (T + 1 + 180, 20.05, 20.06, 5, 5), (T + 20 + 180, 20.10, 20.11, 5, 5)]
    tr = [(T + 20, 19.99, 200, ["@"], "Q")]
    r = F.simulate(e, fq, oq, tr)
    assert r["status"] == "ok" and r["filled"] and r["r_net"] == pytest.approx(20.10 / 20.00 - 1)
    assert r["m_net"] == pytest.approx(20.05 / 20.01 - 1) and r["r_mid"] == pytest.approx(20.105 / 20.00 - 1)
    assert r["spread_ticks"] == 1
    r2 = F.simulate(e, fq, oq, [])
    assert r2["filled"] is False and r2["r_net"] == 0.0
    r3 = F.simulate(e, fq, oq[:1], tr)                    # no quote within 30 s of the exits: the signal is dropped
    assert r3["status"] == "no_exit_quote"
    assert F.simulate(e, [(T - 40, 20.0, 20.01, 5, 5)], oq, tr)["status"] == "no_entry_quote"


def test_population_spread_cap_and_size():
    assert F.in_population((0, 20.00, 20.01, 1, 1)) and not F.in_population((0, 20.00, 20.02, 1, 1))
    assert F.shares_for(20.0) == 112 and F.shares_for(3000.0) == 0


def _row(day, sym, g1, filled, r_net, m_net=-0.0010, r_mid=None, ticks=1, half="A"):
    return {"day": day, "sym": sym, "g1": g1, "half": half, "filled": filled, "r_net": r_net if filled else 0.0,
            "m_net": m_net, "r_mid": (r_mid if r_mid is not None else r_net), "spread_ticks": ticks}


def test_grade_and_verdict():
    import random as _r
    rng = _r.Random(4)
    rows = []
    for d in range(25):
        day = f"2026-06-{d + 1:02d}"
        for i in range(30):
            rows.append(_row(day, f"N{i}", "IN", i % 2 == 0, 0.0009 + rng.gauss(0, 0.0004), ticks=1 + (i // 2) % 2))
            rows.append(_row(day, f"O{i}", "OUT", i % 2 == 0, -0.0012 + rng.gauss(0, 0.0004), ticks=1 + (i // 2) % 2))
    g = F.grade_half(rows, "A")
    assert g["fills"] == 375 and g["fill_rate"] == pytest.approx(0.5) and g["per_fill"] > F.BAR
    assert g["gap"] > 0 and all(v > 0 for v in g["gap_strata"].values())
    assert F.half_verdict(g) == "PASS"
    weak = [dict(r, r_net=r["r_net"] * 0.2) for r in rows]
    assert F.half_verdict(F.grade_half(weak, "A")) == "NOT_PASSED"            # per-fill below +5 bp
    assert F.verdict(["PASS", "PASS"], [0.002, 0.002]) == "PASS"
    assert F.verdict(["NOT_PASSED", "NOT_PASSED"], [0.0003, 0.0004]) == "FAIL"  # upper bound < 5 bp both halves
    assert F.verdict(["PASS", "UNDERPOWERED"], [0.002, None]) == "UNDERPOWERED"


def test_six_field_cache_rows_and_one_cent_cell():
    tr = [(105.0, 19.99, 200, ["@"], "Q", "C")]                           # the fetch stores (ts, px, size, codes, exch, tape)
    assert F.fill_time(tr, 20.00, 100, 100.0, 30) == 105.0
    assert F.fill_time(tr, 20.00, 100, 100.0, 30, min_below=0.01) == 105.0   # exactly 1c below counts (amended_2 g)


def test_fail_routes_ignore_floors():
    g = {"per_signal_t": -2.3, "fills": 40, "days": 25}
    assert F.half_verdict(g) == "FAIL"                                     # t <= -2 before the floors
    assert F.verdict(["UNDERPOWERED", "NOT_PASSED"], [0.0002, 0.0003], [10, 30]) == "FAIL"
    assert F.verdict(["UNDERPOWERED", "NOT_PASSED"], [0.0002, 0.0003], [1, 30]) == "UNDERPOWERED"
