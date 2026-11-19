import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "studies"))
import signal_cache as SC  # noqa: E402


def test_plan_and_load_ranges(tmp_path, monkeypatch):
    monkeypatch.setattr(SC, "OUT", tmp_path)
    for d, syms in (("2026-05-01", ["A", "B"]), ("2026-05-04", ["C"]), ("2026-06-01", ["D"])):
        (tmp_path / f"{d}.json").write_text(json.dumps([{"day": d, "sym": s, "t": 20} for s in syms]))
    assert [p[2] for p in SC.plan_range("2026-05-01..2026-05-31")] == ["A", "B", "C"]
    assert [p[2] for p in SC.plan_range("2026-06-01")] == ["D"]
    import news_source as ns
    q = tmp_path / "quotes.json"
    q.write_text(json.dumps({ns.qkey("2026-05-01", "A", 20): 3.5}))
    monkeypatch.setattr(ns, "QUOTES", q)
    rows = SC.load("2026-05-01", "2026-05-04")
    assert len(rows) == 3 and [r["spread"] for r in rows if r["sym"] == "A"] == [3.5]
    assert [r["spread"] for r in rows if r["sym"] == "B"] == [None]
