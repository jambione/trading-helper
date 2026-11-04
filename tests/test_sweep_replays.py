"""tools/sweep_replays.py: scoring, verdict bars, command shape, report."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import sweep_replays as S  # noqa: E402


def t(sym, ts, ret_bp, peak_bp=50.0, px=100.0):
    return {"symbol": sym, "entry_ts": ts, "entry_px": px, "ret": ret_bp / 1e4, "peak_px": px * (1 + peak_bp / 1e4)}


def test_compare_day_delta_and_never_green():
    base = [t("A", 1, -20, peak_bp=0), t("B", 2, 10)]
    arm = [t("A", 1, -5), t("B", 2, 10)]
    c = S.compare({"2026-10-01": (base, arm)})
    assert c["per_day"]["2026-10-01"] == pytest.approx(15.0)
    assert c["never_green_n"] == 1 and c["never_green_change_bp"] == pytest.approx(15.0)


def test_screen_bar():
    good = {d: ([t("A", 1, 0)], [t("A", 1, 5)]) for d in ("d1", "d2", "d3", "d4", "d5")}
    assert S.screen_verdict(S.compare(good)) == "KEEP"
    mixed = dict(good, d4=([t("A", 1, 0)], [t("A", 1, -50)]), d5=([t("A", 1, 0)], [t("A", 1, -1)]))
    c = S.compare(mixed)
    assert c["share_days_better"] == pytest.approx(0.6) and S.screen_verdict(c) == "DROP"  # mean < 0


def test_confirm_bar_needs_t_and_both_halves():
    days = [f"2026-09-{i:02d}" for i in range(10, 20)]
    up = {d: ([t("A", 1, 0)], [t("A", 1, 4 + (i % 3))]) for i, d in enumerate(days)}
    assert S.confirm_verdict(S.compare(up)) == "CONFIRMED"
    first_half_bad = dict(up, **{d: ([t("A", 1, 0)], [t("A", 1, -30)]) for d in days[:5]})
    assert S.confirm_verdict(S.compare(first_half_bad)) == "NOT_CONFIRMED"
    assert S.confirm_verdict(S.compare({d: up[d] for d in days[:3]})) == "NO_DATA"


def test_set_args_formats_values():
    assert S._set_args({"a": False, "b": 10, "c": "09:35"}) == ["--set", "a=false", "--set", "b=10", "--set", "c=09:35"]


def test_cmd_and_report(tmp_path):
    spec = {"name": "t1", "sha": "HEAD", "baseline": {"live_book": True},
            "variants": {"v": {"live_book": False, "set": {"ai_dead_trade_min": 10}}},
            "screen_days": ["2026-10-01"], "confirm_days": []}
    p = tmp_path / "spec.json"
    p.write_text(json.dumps(spec))
    sw = S.Sweep(p, out_root=tmp_path)
    c = sw.cmd("2026-10-01", "v")
    assert "--live-book" not in c and "ai_dead_trade_min=10" in c and c[c.index("--day") + 1] == "2026-10-01"
    assert "--live-book" in sw.cmd("2026-10-01", "baseline")
    for arm, r in (("baseline", -10), ("v", 5)):
        sw.path("2026-10-01", arm).write_text(json.dumps({"closed": [t("A", 1, r)], "vacuity": {}}))
    rep = sw.report()
    assert rep["variants"]["v"]["screen"]["verdict"] == "KEEP"
    md = (sw.out / "REPORT.md").read_text()
    assert "| v | screen | **KEEP**" in md and "| 1 -> 1 |" in md
