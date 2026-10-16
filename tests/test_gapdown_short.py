"""Gap-down short selection and scoring (prereg amended_1+2). Pure; no data."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))
import gapdown_short as G  # noqa: E402


def test_liquid_dates_use_prior_day_raw():
    rows = [(f"d{k:02d}", 20.0, 2e6) for k in range(25)]          # $40M/day
    rows[21] = ("d21", 9.0, 2e6)                                   # prior close under $10 -> d22 out
    ld = G.liquid_dates(rows)
    assert "d20" in ld and "d22" not in ld and "d21" in ld and "d19" not in ld   # needs 20 prior rows


def test_pick_day_filters_ssr_floor_and_caps():
    names = [{"sym": f"S{i}", "gap": -0.011 - i * 0.001, "prior_ret": 0.0, "ret": 0.0} for i in range(30)]
    names += [{"sym": "HUGE", "gap": -0.15, "prior_ret": 0.0, "ret": 0.0},      # <= -10%: SSR, out
              {"sym": "SSR", "gap": -0.03, "prior_ret": -0.12, "ret": 0.0},     # fell 10% yesterday: out
              {"sym": "FLAT", "gap": 0.002, "prior_ret": 0.0, "ret": 0.0},
              {"sym": "UP", "gap": 0.03, "prior_ret": 0.0, "ret": 0.0}]
    picks, ctrl = G.pick_day(names)
    syms = [p["sym"] for p in picks]
    assert len(picks) == 20 and "HUGE" not in syms and "SSR" not in syms and syms[0] == "S29"
    assert [c["sym"] for c in ctrl] == ["FLAT"]


def test_score_half_paired_and_robust():
    days = []
    for k in range(600):
        m = f"2017-{1 + k % 12:02d}-{1 + k % 28:02d}"
        days.append({"d": m, "picks": [{"sym": f"A{k % 50}", "ret": 0.004}], "ctrl_ret": 0.001 + (k % 3) * 1e-4,
                     "spy_ret": 0.0})
    r = G.score_half(days, 20, 25)
    assert abs(r["paired_bp"] - (40 - 11)) < 1e-6 and r["pass_a"] and r["pass_d"] and not r["underpowered"]
    assert abs(r["hedged_stress_bp"] - (40 - 25 - 2)) < 1e-6 and r["pass_b"] is False   # zero variance -> no t
    assert G.score_half(days[:100], 20, 25)["underpowered"]
