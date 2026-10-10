"""hist_sim_read.py pure parts: costing with the pooled-median substitute, frozen exclusions, per-day legs."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))
import hist_sim_read as H


def test_cost_day_median_substitute_and_share():
    raw = {"base": [{"symbol": "A", "entry_ts": 1, "ret": 0.001}, {"symbol": "B", "entry_ts": 2, "ret": 0.0}],
           "st": [{"symbol": "A", "entry_ts": 1, "ret": 0.002}, {"symbol": "C", "entry_ts": 3, "ret": None}]}
    sp = {"A": 4.0, "B": None}
    out, info = H.cost_day(raw, lambda s, ts: sp.get(s))
    assert [round(t["net_bp"], 6) for t in out["base"]] == [6.0, -4.0]      # B charged the pooled median (4)
    assert info["no_nbbo"] == {"base": 1, "st": 0} and info["no_nbbo_share"]["base"] == 0.5


def test_exclusions_frozen_order():
    ok = {"no_nbbo_share": {"base": 0.0}}
    assert H.excluded("2026-04-01", {"spy_missing_share": 0.0, "universe": 100, "fetch_failed": 0}, ok) is None
    assert H.excluded("2026-04-01", None, ok) == "no_dayinfo"
    assert H.excluded("2026-04-01", {"spy_missing_share": 0.06, "universe": 100}, ok) == "b_spy_bars"
    assert H.excluded("2026-04-01", {"spy_missing_share": 0.0, "universe": 100, "fetch_failed": 11}, ok) == "c_fetch_failed"
    assert H.excluded("2026-04-01", {"spy_missing_share": 0.0, "universe": 100, "fetch_failed": 0},
                      {"no_nbbo_share": {"base": 0.0, "st": 0.11}}) == "d_no_nbbo"


def test_paired_bp_needs_both_variants_that_day():
    a = [{"day": "d1", "net_bp": 5.0}, {"day": "d2", "net_bp": 1.0}]
    b = [{"day": "d1", "net_bp": 2.0}]
    assert H.paired_bp(a, b, ["d1", "d2"]) == {"d1": 3.0}
    assert H.dollars(a, ["d1", "d2", "d3"]) == {"d1": 0.5, "d2": 0.1, "d3": 0.0}
