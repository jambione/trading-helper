"""tools/pick_ledger.py: picks are stamped and chained before the outcome, scored open to open, one verdict on the first 30."""
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import pick_ledger as P

ET = ZoneInfo("America/New_York")
DAYS = [f"2026-01-{d:02d}" for d in (5, 6, 7, 8, 9, 12, 13, 14, 15, 16)]
UNI = [f"C{i:03d}" for i in range(60)]


def _add(tmp_path, sym="ABCD", when=datetime(2026, 1, 5, 12, 0, tzinfo=ET), **kw):
    return P.add_pick(sym, kw.pop("thesis", "small reactors get their first licence"), 6, "licence is refused", now=when,
                      path=tmp_path / "l.jsonl", universe=UNI, **kw)


def test_add_validates_and_allows_one_pick_per_symbol_per_quarter(tmp_path):
    r = _add(tmp_path)
    assert len(r["controls"]) == 30 and len(set(r["controls"])) == 30 and r["prev"] == ""
    with pytest.raises(ValueError, match="already recorded"):
        _add(tmp_path, when=datetime(2026, 3, 20, 12, 0, tzinfo=ET))
    r2 = _add(tmp_path, when=datetime(2026, 4, 20, 12, 0, tzinfo=ET))
    assert r2["prev"] == r["id"]
    for bad in ({"sym": "SPY"}, {"sym": "not a symbol"}, {"thesis": "moon"}):
        with pytest.raises(ValueError):
            _add(tmp_path, **bad)
    assert len(P.read_ledger(tmp_path / "l.jsonl")) == 2


def test_chain_catches_an_edit_a_removal_and_a_reorder(tmp_path):
    for i, s in enumerate(("AAA", "BBB", "CCC")):
        _add(tmp_path, sym=s, when=datetime(2026, 1, 5 + i, 12, 0, tzinfo=ET))
    picks = P.read_ledger(tmp_path / "l.jsonl")
    assert P.chain_problem(picks) is None
    assert "removed" in P.chain_problem([picks[0], picks[2]])
    assert "removed" in P.chain_problem([picks[1], picks[0], picks[2]])
    edited = [dict(picks[0], recorded_at="2025-12-01T12:00:00-05:00"), *picks[1:]]          # backdating a line
    assert "edited" in P.chain_problem(edited)
    (tmp_path / "l.jsonl").write_text("\n".join(json.dumps(p) for p in edited) + "\n")
    with pytest.raises(ValueError, match="damaged"):
        _add(tmp_path, sym="DDD", when=datetime(2026, 2, 1, 12, 0, tzinfo=ET))


def test_entry_is_the_first_open_after_the_record_time():
    assert P.entry_index(DAYS, "2026-01-05T09:29:00-05:00") == 0
    assert P.entry_index(DAYS, "2026-01-05T09:31:00-05:00") == 1
    assert P.entry_index(DAYS, "2026-01-09T16:30:00-05:00") == 5          # Friday evening: Monday
    assert P.entry_index(DAYS, "2026-01-05T14:31:00+00:00") == 1          # the same instant written in UTC
    assert P.entry_index(DAYS, "2026-01-16T10:00:00-05:00") is None


def test_span_sells_a_vanished_name_at_its_last_close():
    bars = {"2026-01-05": (10.0, 11.0), "2026-01-06": (11.0, 5.0)}
    assert P.span(bars, DAYS, 0, 1) == pytest.approx(0.10)
    assert P.span(bars, DAYS, 0, 4) == pytest.approx(-0.50)


def _bars():
    b = {"ABCD": {d: (100.0 + 10 * i, 100.0 + 10 * i) for i, d in enumerate(DAYS)}, "SPY": {d: (100.0 + 5 * i, 100.0 + 5 * i) for i, d in enumerate(DAYS)}}
    for c in UNI:
        b[c] = {d: (100.0 + 2 * i, 100.0 + 2 * i) for i, d in enumerate(DAYS)}
    return b


def test_score_needs_a_commit_before_the_entry_open(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "HORIZONS", (2, 20))
    pick = _add(tmp_path)
    before, after = P.open_ts("2026-01-06") - 60, P.open_ts("2026-01-06") + 60
    assert "never committed" in P.score_pick(pick, DAYS, _bars())["status"]
    assert "AFTER" in P.score_pick(pick, DAYS, _bars(), commit_ts=after)["status"]
    assert "could not be checked" in P.score_pick(pick, DAYS, _bars(), commit_ts=before, is_fund=None)["status"]
    fund = P.score_pick(pick, DAYS, _bars(), commit_ts=before, is_fund=True)
    assert not fund["counts"]
    s = P.score_pick(pick, DAYS, _bars(), commit_ts=before)
    h = s["h"][2]                                                          # 110 -> 130 vs SPY 105 -> 115, controls 102 -> 106
    assert s["status"] == "scored" and s["entry_day"] == "2026-01-06" and h["closed"]
    assert h["vs_spy"] == pytest.approx((130 / 110) / (115 / 105) - 1)
    assert h["vs_random"] == pytest.approx((130 / 110) / (106 / 102) - 1) and h["rank"] == 1.0
    assert not s["h"][20]["closed"] and s["h"][20]["sessions"] == 8


def test_retro_picks_never_count(tmp_path):
    r = P.score_pick(_add(tmp_path, retro=True), DAYS, {})
    assert r["status"].startswith("retro") and not r["counts"]


def _scored(lifts, closed=True, status="scored", month=None):
    return [{"symbol": f"S{i}", "recorded_at": f"2026-{1 + i // 5:02d}-{1 + i % 5:02d}T12:00:00-05:00", "status": status, "counts": True,
             "entry_day": f"2026-{1 + i // 5:02d}-10" if month is None else month,
             "h": {63: {"closed": closed, "vs_spy": v, "vs_random": v, "rank": 0.8 if v > 0 else 0.2, "beta_excess": v}}} for i, v in enumerate(lifts)]


def test_verdict_waits_then_reads_only_the_first_thirty():
    good = [0.04 + 0.002 * i for i in range(30)]
    assert "NOT YET" in P.verdict(_scored(good[:29]))["verdict"]
    assert "UNPROVEN" in P.verdict(_scored(good[:29]), today="2027-12-01")["verdict"]
    assert "NOT YET" in P.verdict(_scored(good, closed=False))["verdict"]
    assert P.verdict(_scored(good))["verdict"] == "PASS"
    assert P.verdict(_scored(good + [-0.9] * 10))["verdict"] == "PASS"     # picks 31-40 cannot change it


def test_an_unscoreable_pick_blocks_and_is_not_replaced_by_pick_31():
    s = _scored([0.05] * 31)
    s[3]["status"] = "never committed to git (BLOCKS the verdict)"
    v = P.verdict(s)
    assert v["verdict"] == "BLOCKED" and "S3" in v["why"][0]


def test_a_missing_random_control_blocks():
    s = _scored([0.05 + 0.001 * i for i in range(30)])
    s[7]["h"][63]["vs_random"] = None
    assert P.verdict(s)["verdict"] == "BLOCKED"


def test_one_lucky_pick_does_not_pass():
    v = P.verdict(_scored([-0.02] * 29 + [3.0]))                           # mean is positive, 29 of 30 trail SPY
    assert v["mean_vs_spy"] > 0 and v["verdict"] == "FAIL"


def test_picks_bunched_in_one_month_cannot_reach_the_t_bar():
    v = P.verdict(_scored([0.04 + 0.002 * i for i in range(30)], month="2026-03-10"))
    assert v["verdict"] == "FAIL" and v["t"] != v["t"]                      # one cluster: no t at all
