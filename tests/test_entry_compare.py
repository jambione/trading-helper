import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "studies"))
import entry_compare as m  # noqa: E402


def _q(day, sym, t):
    return f"{day}|{sym}|{t}"


def test_spread_gate_and_split():
    rows = [{"day": "2026-05-01", "sym": "A", "t": 10, "gross": 20.0},
            {"day": "2026-05-01", "sym": "B", "t": 10, "gross": 20.0},
            {"day": "2026-10-01", "sym": "C", "t": 100, "gross": -5.0},
            {"day": "2026-10-01", "sym": "D", "t": 100, "gross": 1.0}]
    q = {_q("2026-05-01", "A", 10): 4.0, _q("2026-05-01", "B", 10): 16.0, _q("2026-10-01", "C", 100): 2.0}
    out = m.with_spread(rows, "gross", q, _q)
    assert [(r["day"], r["net"]) for r in out] == [("2026-05-01", 16.0), ("2026-10-01", -7.0)]
    d, c = m.split(out)
    assert len(d) == 1 and len(c) == 1


def test_summary_and_render():
    rows = [{"day": f"2026-05-{1 + i % 20:02d}", "t": (i * 7) % 390, "net": (i % 5) - 2.0} for i in range(400)]
    s = m.summarize(rows)
    assert s["n"] == 400 and s["per_day"] == 20.0 and "09:30-10:00" in s["tod"]
    md = m.render([("Test rule", "15-min bracket", rows)], [], {"x": "not scored yet"})
    assert "| Test rule | 15-min bracket |" in md and "## Net by time of day" in md
