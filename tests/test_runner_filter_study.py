"""Entry-time traits of the runner filter. No market data."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))
import runner_filter_study as F  # noqa: E402


def test_traits_use_only_bars_before_the_entry():
    bars = {570: (100, 104, 99, 103), 580: (103, 103.5, 101, 101), 600: (101, 110, 100, 109)}   # 600 is the entry bar
    t = F.traits(bars, 600, 101.0, prior_close=95.0)
    assert t["F1_room"] is True                       # 101 <= 104 * 0.9726 = 101.15; the entry bar's 110 is ignored
    assert t["F2_price50"] is True
    assert t["F3_early_up"] is True                   # +6.3% at 10:00
    late = F.traits(bars, 11 * 60, 101.0, prior_close=95.0)
    assert late["F3_early_up"] is False
    assert F.traits(bars, 600, 101.0, prior_close=None)["F3_early_up"] is None
    assert F.traits({}, 600, 40.0, 39.0)["F1_room"] is None and F.traits({}, 600, 40.0, 39.0)["F2_price50"] is False


def test_prior_close_is_strictly_before_the_day():
    daily = {"X": [("2026-10-08", 1, 10.0, 1), ("2026-10-09", 1, 11.0, 1), ("2026-10-12", 1, 12.0, 1)]}
    assert F.prior_close_for(daily, "X", "2026-10-12") == 11.0
    assert F.prior_close_for(daily, "Y", "2026-10-12") is None


def test_prior_close_must_be_the_previous_session():
    daily = {"X": [("2026-10-08", 1, 10.0, 1), ("2026-10-09", 1, 11.0, 1)], "SPY": [("2026-10-08", 1, 1, 1), ("2026-10-09", 1, 1, 1)]}
    prevs = F.prev_sessions(["2026-10-09", "2026-10-12", "2026-10-13"], daily)
    assert prevs["2026-10-13"] == "2026-10-12" and prevs["2026-10-12"] == "2026-10-09"
    assert F.prior_close_for(daily, "X", "2026-10-12", prevs["2026-10-12"]) == 11.0
    assert F.prior_close_for(daily, "X", "2026-10-13", prevs["2026-10-13"]) is None    # cache gap: unknown, not 10/09


def test_robustness_drops_the_best():
    rows = [{"day": f"d{i % 4}", "sym": f"S{i}", "run": (0.05 if i < 5 else -0.001), "half": "A"} for i in range(20)]
    rob = F.robustness([r for r in rows if r["sym"] != "S19"], rows)
    assert rob["drop_top5_trades"]["kept_net"] < 0                     # without its five big winners it loses
