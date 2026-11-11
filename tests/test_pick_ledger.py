"""tools/pick_ledger.py: picks are stamped before the outcome, scored open to open against SPY, verdict on the first 30."""
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import pick_ledger as P

ET = ZoneInfo("America/New_York")
DAYS = [f"2026-01-{d:02d}" for d in (5, 6, 7, 8, 9, 12, 13, 14, 15, 16)]


def _add(tmp_path, sym="OKLO", when=datetime(2026, 1, 5, 12, 0, tzinfo=ET), **kw):
    return P.add_pick(sym, kw.pop("thesis", "small reactors get their first licence"), 6, "licence is refused", now=when,
                      path=tmp_path / "l.jsonl", **kw)


def test_add_validates_and_refuses_a_repeat_within_ten_days(tmp_path):
    _add(tmp_path)
    with pytest.raises(ValueError, match="already recorded"):
        _add(tmp_path, when=datetime(2026, 1, 9, 12, 0, tzinfo=ET))
    _add(tmp_path, when=datetime(2026, 1, 20, 12, 0, tzinfo=ET))          # a new idea later is fine
    for bad in ({"sym": "SPY"}, {"sym": "not a symbol"}, {"thesis": "moon"}):
        with pytest.raises(ValueError):
            _add(tmp_path, **bad)
    assert len(P.read_ledger(tmp_path / "l.jsonl")) == 2


def test_entry_is_the_first_open_after_the_record_time():
    assert P.entry_index(DAYS, "2026-01-05T09:29:00-05:00") == 0          # before that day's open: that open
    assert P.entry_index(DAYS, "2026-01-05T09:31:00-05:00") == 1          # after it: the next session
    assert P.entry_index(DAYS, "2026-01-09T16:30:00-05:00") == 5          # Friday evening: Monday
    assert P.entry_index(DAYS, "2026-01-16T10:00:00-05:00") is None       # not happened yet


def test_span_sells_a_vanished_name_at_its_last_close():
    bars = {"2026-01-05": (10.0, 11.0), "2026-01-06": (11.0, 5.0)}
    assert P.span(bars, DAYS, 0, 1) == pytest.approx(0.10)
    assert P.span(bars, DAYS, 0, 4) == pytest.approx(-0.50)


def test_score_is_a_ratio_against_spy_and_marks_open_windows(monkeypatch):
    monkeypatch.setattr(P, "HORIZONS", (2, 20))
    pick = {"id": "abc", "symbol": "OKLO", "recorded_at": "2026-01-05T12:00:00-05:00"}
    bars = {"OKLO": {d: (100.0 + 10 * i, 0.0) for i, d in enumerate(DAYS)}, "SPY": {d: (100.0 + 5 * i, 0.0) for i, d in enumerate(DAYS)}}
    s = P.score_pick(pick, DAYS, bars, [])
    assert s["entry_day"] == "2026-01-06"
    h = s["h"][2]                                                          # 110 -> 130 vs SPY 105 -> 115
    assert h["closed"] and h["vs_spy"] == pytest.approx((130 / 110) / (115 / 105) - 1) and h["vs_random"] is None
    assert not s["h"][20]["closed"] and s["h"][20]["sessions"] == 8        # marked to the latest open, flagged open


def test_retro_picks_are_never_scored(tmp_path):
    r = _add(tmp_path, retro=True)
    assert P.score_pick(r, DAYS, {}, [])["status"].startswith("retro")


def _scored(n, lifts, closed=True):
    return [{"recorded_at": f"2026-01-{i + 1:02d}", "status": "scored", "h": {63: {"closed": closed, "vs_spy": lifts[i], "vs_random": lifts[i]}}}
            for i in range(n)]


def test_verdict_waits_for_thirty_closed_and_uses_only_the_first_thirty():
    assert "NOT YET" in P.verdict(_scored(29, [0.05] * 29))["verdict"]
    assert "NOT YET" in P.verdict(_scored(30, [0.05] * 30, closed=False))["verdict"]
    good = [0.04 + 0.001 * i for i in range(30)]
    assert P.verdict(_scored(30, good))["verdict"] == "PASS"
    later_disasters = _scored(40, good + [-0.9] * 10)                      # picks 31-40 cannot change the verdict
    assert P.verdict(later_disasters)["verdict"] == "PASS"


def test_one_lucky_pick_does_not_pass():
    lifts = [-0.02] * 29 + [3.0]                                           # mean is positive, but 29 of 30 trail SPY
    v = P.verdict(_scored(30, lifts))
    assert v["mean_vs_spy"] > 0 and v["verdict"] == "FAIL"
