"""tools/pick_positions.py: buy on the entry day only, sell on the 21st session, never buy late, report against SPY."""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import pick_positions as PP

# weekdays from Mon 2026-11-16
SESS = [d.isoformat() for d in (date(2026, 11, 16) + timedelta(days=i) for i in range(60)) if d.weekday() < 5]


def _pick(pid, sym, stamp):
    return {"id": pid, "symbol": sym, "recorded_at": stamp, "retro": False}


def test_entry_is_the_first_open_after_the_record():
    assert PP.entry_day("2026-11-16T08:45:00-05:00", SESS) == "2026-11-16"       # recorded before Monday's open
    assert PP.entry_day("2026-11-16T09:30:00-05:00", SESS) == "2026-11-17"       # at or after the open: next session
    assert PP.entry_day("2026-11-19T17:10:00-05:00", SESS) == "2026-11-20"       # the nightly catch-up enters next morning
    assert PP.entry_day("2026-11-20T17:10:00-05:00", SESS) == "2026-11-23"       # over a weekend
    assert PP.exit_day("2026-11-16", SESS) == SESS[21] and PP.exit_day(SESS[-5], SESS) is None


def test_plan_buys_today_only_never_late_and_sells_when_due():
    picks = [_pick("a", "AAA", "2026-11-16T08:45:00-05:00"), _pick("b", "BBB", "2026-11-23T08:45:00-05:00"),
             {**_pick("r", "RRR", "2026-11-16T08:45:00-05:00"), "retro": True}]
    assert PP.plan(picks, {}, SESS, "2026-11-16") == [("buy", "a")]
    assert PP.plan(picks, {}, SESS, "2026-11-17") == [("missed", "a")]            # the machine was off on the entry day
    st = {"a": {"symbol": "AAA", "status": "open", "entry_day": "2026-11-16"}}
    assert PP.plan(picks, st, SESS, "2026-11-23") == [("buy", "b")]
    assert PP.plan(picks, st, SESS, SESS[20]) == [("missed", "b")] and PP.plan(picks, st, SESS, SESS[21])[0] == ("sell", "a")
    assert ("sell", "a") in PP.plan(picks, st, SESS, SESS[25])                    # a missed sell day is caught up
    done = {"a": {"symbol": "AAA", "status": "closed", "entry_day": "2026-11-16"}, "b": {"symbol": "BBB", "status": "missed"}}
    assert PP.plan(picks, done, SESS, SESS[30]) == []


def test_size_is_equal_dollars_without_borrowing():
    assert PP.size(9900.0, 9900.0) == 660.0 and PP.size(9900.0, 300.0) == 300.0 and PP.size(9900.0, -5.0) == 0.0


def test_rows_compare_each_holding_with_spy_over_the_same_days():
    days = SESS[:10]
    bars = {"SPY": {d: (100.0, 100.0 + i) for i, d in enumerate(days)}, "AAA": {d: (50.0, 50.0 + i) for i, d in enumerate(days)},
            "BBB": {d: (20.0, 20.0) for d in days}}
    state = {"a": {"symbol": "AAA", "status": "open", "entry_day": days[0], "dollars": 600.0, "buy_px": 50.0},
             "b": {"symbol": "BBB", "status": "closed", "entry_day": days[0], "exit_day": days[5], "dollars": 600.0, "buy_px": 20.0, "sell_px": 19.0},
             "c": {"symbol": "CCC", "status": "missed", "entry_day": days[1]}}
    rows = {r["symbol"]: r for r in PP.rows_for(state, bars, days)}
    assert abs(rows["AAA"]["ret"] - 0.18) < 1e-9 and abs(rows["AAA"]["spy"] - 0.09) < 1e-9 and rows["AAA"]["held"] == 9
    assert abs(rows["AAA"]["vs_spy"] - (1.18 / 1.09 - 1)) < 1e-9 and abs(rows["AAA"]["pnl"] - 108.0) < 1e-9
    assert abs(rows["BBB"]["ret"] + 0.05) < 1e-9 and rows["BBB"]["spy"] == 0.0 and rows["BBB"]["held"] == 5   # closed: open to open
    assert "ret" not in rows["CCC"]
    text = PP.render(list(rows.values()), 9950.0, "2026-11-27 17:00 ET")
    assert "OPEN (1)" in text and "CLOSED (1)" in text and "NOT HELD: CCC (missed)" in text and "1 of 1 ahead of the S&P" in text
    assert "no positions yet" in PP.render([], None, "x")
