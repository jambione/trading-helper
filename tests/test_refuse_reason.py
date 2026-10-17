"""Refusal-minute signals and the two-cell verdict. No market data."""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))

import refuse_reason as R  # noqa: E402

ET = ZoneInfo("America/New_York")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _row(sym, why, hour, minute, arm_ok=False, stage="arm", day="2026-09-08"):
    y, m, d = (int(x) for x in day.split("-"))
    return {
        "symbol": sym,
        "arm_why": why,
        "arm_ok": arm_ok,
        "stage": stage,
        "ts": datetime(y, m, d, hour, minute, tzinfo=ET).timestamp(),
    }


def test_earliest_minute_wins_regardless_of_order():
    rows = [
        _row("AAA", "exh_falling", 11, 0),
        _row("aaa", "exh_falling", 10, 5),
        _row("AAA", "exh_falling", 10, 15),
    ]
    assert R.first_signals(rows) == {("AAA", "exh_falling"): 10 * 60 + 5}


def test_each_reason_keeps_its_own_first_minute():
    rows = [
        _row("AAA", "exh_falling", 10, 0),
        _row("AAA", "exh_not_tight", 11, 0),
        _row("AAA", "exh_not_tight", 10, 30),
    ]
    got = R.first_signals(rows)
    assert got[("AAA", "exh_falling")] == 10 * 60
    assert got[("AAA", "exh_not_tight")] == 10 * 60 + 30


def test_arm_ok_true_is_ignored():
    assert R.first_signals([_row("AAA", "exh_falling", 10, 0, arm_ok=True)]) == {}
    assert R.signal_of(_row("AAA", "exh_falling", 10, 0, arm_ok=False)) is not None


def test_other_reasons_and_stages_are_ignored():
    rows = [
        _row("AAA", "above_max_price", 10, 0),
        _row("BBB", "exh_falling", 10, 0, stage="seed"),
        _row("CCC", "EXH_FALLING", 10, 0),
    ]
    assert R.first_signals(rows) == {}
    spaced = _row("AAA", "exh_falling", 10, 0)
    spaced["arm_why"] = "exh_falling "
    assert R.signal_of(spaced) is None


def test_window_bounds():
    assert R.signal_of(_row("AAA", "exh_falling", 9, 49)) is None
    assert R.signal_of(_row("AAA", "exh_falling", 9, 50))[2] == 9 * 60 + 50
    assert R.signal_of(_row("AAA", "exh_not_tight", 15, 30))[2] == 15 * 60 + 30
    assert R.signal_of(_row("AAA", "exh_falling", 15, 31)) is None


def test_ledger_stream_keeps_the_earliest_line(tmp_path):
    path = tmp_path / "2026-09-08.jsonl"
    rows = [
        _row("AAA", "exh_falling", 12, 0),
        _row("AAA", "exh_falling", 10, 11),
        _row("BBB", "exh_not_tight", 10, 0, arm_ok=True),
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    assert R.read_ledger(str(path)) == {("AAA", "exh_falling"): 10 * 60 + 11}


def test_fill_is_the_next_minute_open():
    sip = {m: (100.0, 100.0) for m in range(600, 700)}
    sip[601] = (50.0, 50.0)
    sip[610] = (55.0, 55.0)
    assert abs(R.fill_gross(sip, 600) - (55.0 / 50.0 - 1.0)) < 1e-12
    assert R.fill_gross({}, 600) is None


def test_a_missing_board_minute_is_still_inside_the_benchmark():
    import pandas as pd
    start = pd.Timestamp("2026-09-08 14:00", tz="UTC")  # 10:00 ET
    n = 40
    frame = pd.DataFrame({
        "time": [start + pd.Timedelta(minutes=i) for i in range(n)],
        "open": [10.0] * n,
        "close": [10.0] * n,
    })
    # 10:00 ET is minute 600. Entry 601 needs a later control inside this frame.
    data = {"day": "2026-09-08", "board": {"AAA": set()}, "sip": {"AAA": frame}}
    row = R.score_day("2026-09-08", {("AAA", "exh_falling"): 600, ("ZZZ", "exh_not_tight"): 600}, data)
    assert len(row["cells"]["exh_falling"]["gross"]) == 1
    assert row["board"] == row["cells"]["exh_falling"]["gross"]
    assert row["dropped"] == {"no_sip": 1, "no_markout": 0, "no_control": 0}


def test_board_mean_keeps_every_window_minute():
    sip = {m: (10.0, 10.0) for m in range(580, 700)}
    sip[610] = (11.0, 11.0)
    sip[611] = (12.0, 12.0)
    got = R.board_grosses({"AAA": {600, 601, 9 * 60 + 49}}, {"AAA": sip})
    assert len(got) == 2
    assert {round(x, 6) for x in got} == {round(11.0 / 10.0 - 1.0, 6), round(12.0 / 10.0 - 1.0, 6)}


def _block(n=200, days=20, gross=0.002, t_gross=3.0, later=0.0008, later_t=2.5,
           board=0.0004, net3=0.0017, net10=0.001):
    return {
        "n": n, "days": days, "gross": gross, "t_gross": t_gross,
        "net3": net3, "net10": net10, "later": later, "later_t": later_t,
        "board": board, "board_t": 2.0,
    }


def test_verdict_pass_needs_both_lifts_in_both_halves():
    good = _block()
    assert R.verdict({"A": good, "B": good, "ALL": good}) == "PASS"
    weak = _block(board=-0.0001)
    assert R.verdict({"A": good, "B": weak, "ALL": good}) == "FAIL"


def test_verdict_thin_half_that_misses_pass_is_fail():
    misses = _block(later=0.0002, later_t=0.8, net10=0.0, net3=0.0007, gross=0.001, t_gross=1.2)
    thin = _block(n=50, days=4)
    assert R.verdict({"A": misses, "B": thin, "ALL": misses}) == "FAIL"


def test_verdict_thin_half_when_the_other_would_pass_is_underpowered():
    good = _block()
    thin = _block(n=50, days=4)
    assert R.verdict({"A": good, "B": thin, "ALL": good}) == "UNDERPOWERED"


def test_verdict_promising_needs_both_lifts_and_the_combined_t():
    soft = _block(gross=0.001, t_gross=1.2, later=0.0002, later_t=0.8,
                  board=0.0001, net3=0.0007, net10=0.0)
    both = {"A": soft, "B": soft, "ALL": _block(gross=0.001, t_gross=3.0, net3=0.0007, net10=0.0)}
    assert R.verdict(both) == "PROMISING"
    no_board = _block(gross=0.001, t_gross=1.2, later=0.0002, later_t=0.8,
                      board=0.0, net3=0.0007, net10=0.0)
    assert R.verdict({"A": no_board, "B": no_board, "ALL": both["ALL"]}) == "FAIL"


def test_pin_matches_the_prereg():
    path = os.path.join(ROOT, "docs", "studies", "refuse_reason_prereg.json")
    prereg = json.load(open(path, encoding="utf-8"))
    assert re.findall(r"2026-\d{2}-\d{2}", prereg["data"]) == list(R.PIN)
    assert R.CLOSED_SATURDAY not in R.PIN
    assert datetime.strptime(R.CLOSED_SATURDAY, "%Y-%m-%d").strftime("%A") == "Saturday"
    assert [d for d in R.PIN if d <= R.CUT][-1] == "2026-09-24"
    assert sum(d <= R.CUT for d in R.PIN) == 13
    assert sum(d > R.CUT for d in R.PIN) == 16
    assert R.scored_days(list(R.PIN) + [R.CLOSED_SATURDAY]) == list(R.PIN)
    try:
        R.scored_days(list(R.PIN) + ["2026-09-04"])
    except SystemExit as exc:
        assert "REFUSING" in str(exc)
    else:
        raise AssertionError("an extra session must abort")


def test_a_thin_sip_day_aborts_and_is_not_scored():
    board = {f"S{i}": set() for i in range(100)}
    sip = {f"S{i}": object() for i in range(94)}
    assert R.sip_miss_share({"board": board, "sip": sip}) > 0.05
    assert R.sip_miss_share({"board": {"A": set()}, "sip": {"A": object()}}) == 0.0
    try:
        R.require_sip_coverage("2026-09-08", {"board": board, "sip": sip})
    except SystemExit as exc:
        assert "REFUSING" in str(exc)
    else:
        raise AssertionError("sip miss over 5% must abort")
    try:
        R.require_sip_coverage("2026-09-08", {"board": {}, "sip": {}})
    except SystemExit as exc:
        assert "REFUSING" in str(exc)
    else:
        raise AssertionError("an empty board must abort")


def test_refuse_stays_one_process():
    text = open(R.__file__, encoding="utf-8").read()
    assert "ProcessPoolExecutor" not in text
