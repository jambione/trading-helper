import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import book_daily_report as R  # noqa: E402


def test_names_section_counts_unique_names_per_blocker():
    rows = [{"leaders": 30, "coverage_pct": 20.0, "purity_pct": 50.0, "flagged": ["X"],
             "missing": [{"symbol": "CRWD", "pct": 9.0, "why": "above_max_price"}, {"symbol": "TJX", "pct": 8.0, "why": "admit_range_pos"}]},
            {"leaders": 30, "coverage_pct": 30.0, "purity_pct": 70.0, "flagged": [],
             "missing": [{"symbol": "CRWD", "pct": 10.0, "why": "above_max_price"}, {"symbol": "TTWO", "pct": 9.0, "why": "above_max_price"}]}]
    n = R.names_section(rows)
    assert n["coverage_avg"] == 25.0 and n["purity_avg"] == 60.0
    assert n["blockers"][0] == ("above_max_price", 2, ["CRWD", "TTWO"])
    assert n["most_missed"][0] == ("CRWD", 2, 10.0) and n["flagged"] == [("X", 1)]
    assert "above_max_price" in R.next_blocker(n)


def test_opens_section_windows():
    iv = [{"t_open": 100, "t_close": 300}, {"t_open": 650, "t_close": None}, {"t_open": 700, "t_close": 800}]
    o = R.opens_section(iv, day_start=0, day_end=1800)
    assert o["opens"] == 3 and o["windows"] == 3 and o["windows_with_open"] == 2


def test_timing_section_and_render():
    cards = [{"range_pos": 95, "min_since_low": 30, "green_3m": False, "result_bp": -40.0},
             {"range_pos": 50, "min_since_low": 10, "green_3m": True, "result_bp": 12.0}]
    t = R.timing_section(cards)
    assert t["top10_range_pct"] == 50.0 and t["green_3m_pct"] == 50.0 and t["mean_result_bp"] == -14.0
    md = R.render("2026-11-19", R.names_section([]), R.opens_section([], 0, 600), t)
    assert "# Book report 2026-11-19" in md and "below" in md
