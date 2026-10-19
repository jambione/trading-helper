"""Seat test rules. Synthetic records only. No market returns."""
import gzip
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from tools.studies.seat_agent import (
    DAYS_A,
    DAYS_B,
    already_up,
    analyze_day,
    closed_trips,
    day_book,
    grade,
    half_stats,
    morning_list,
    movers_first,
    seat_of,
)

ET = ZoneInfo("America/New_York")
DAY = "2026-09-25"


def _ts(hhmm: str, day: str = DAY) -> float:
    hour, minute = map(int, hhmm.split(":"))
    y, m, d = map(int, day.split("-"))
    return datetime(y, m, d, hour, minute, tzinfo=ET).timestamp()


def _sug(ts: float, name: str, last_ok: float, symbols: list[str]) -> dict:
    return {
        "ts": ts,
        "file": name,
        "data": {
            "last_ok": last_ok,
            "rows": [{"symbol": s, "pct_change": 12.0 if s == "AAA" else 1.0} for s in symbols],
        },
    }


def test_the_first_morning_snapshot_is_the_list_and_the_afternoon_is_not():
    records = [
        _sug(_ts("08:31"), "grok_suggestions.json", _ts("08:31"), ["AAA"]),
        _sug(_ts("10:00"), "grok_suggestions.json", _ts("08:31"), ["AAA", "BBB"]),
        _sug(_ts("14:35"), "grok_suggestions.json", _ts("14:35"), ["CCC"]),
        _sug(_ts("08:32"), "claude_suggestions.json", _ts("08:32"), ["AAA", "DDD"]),
        _sug(_ts("08:00"), "grok_suggestions.json", _ts("08:00"), ["EEE"]),
    ]
    symbols, pct, files = morning_list(records, DAY)
    assert symbols == {"AAA", "DDD"}
    assert pct["AAA"] == 12.0
    assert files == {"grok_suggestions.json", "claude_suggestions.json"}


def test_a_publish_at_0907_counts_and_0930_does_not():
    early = morning_list([_sug(_ts("09:07"), "grok_suggestions.json", _ts("09:07"), ["AAA"])], DAY)
    late = morning_list([_sug(_ts("09:30"), "grok_suggestions.json", _ts("09:30"), ["AAA"])], DAY)
    assert early[0] == {"AAA"}
    assert late[0] == set()


def test_yesterdays_last_ok_does_not_seat_today_and_a_missing_last_ok_uses_the_report():
    stale = morning_list(
        [_sug(_ts("08:40"), "grok_suggestions.json", _ts("08:31", "2026-09-24"), ["AAA"])],
        DAY,
    )
    assert stale[0] == set()
    report = morning_list([{
        "ts": _ts("08:40"),
        "file": "claude_suggestions.json",
        "data": {
            "last_report_path": "/tmp/claude_research_20260925_083200.md",
            "rows": [{"symbol": "GGG"}],
        },
    }], DAY)
    assert report[0] == {"GGG"}


def test_movers_time_is_the_files_own_clock_and_overlap_stays_on_the_agent_arm():
    records = [
        {"ts": _ts("10:05"), "file": "movers_stocks.json",
         "data": {"ts": _ts("09:40"), "rows": [{"symbol": "MMM"}]}},
        {"ts": _ts("11:00"), "file": "movers_stocks.json",
         "data": {"ts": _ts("10:50"), "rows": [{"symbol": "MMM"}, {"symbol": "NNN"}]}},
        {"ts": _ts("08:00"), "file": "movers_stocks.json",
         "data": {"ts": _ts("16:00", "2026-09-24"), "rows": [{"symbol": "OLD"}]}},
    ]
    first = movers_first(records, DAY)
    assert first == {"MMM": _ts("09:40"), "NNN": _ts("10:50")}
    assert "OLD" not in first
    agent = {"AAA"}
    assert seat_of("AAA", _ts("10:00"), agent, {"AAA": _ts("09:50")}) == ("agent", True)
    assert seat_of("MMM", _ts("10:00"), agent, first) == ("movers", False)
    assert seat_of("MMM", _ts("09:30"), agent, first) == (None, False)
    assert seat_of("ZZZ", _ts("10:00"), agent, first) == (None, False)


def test_an_earlier_other_book_buy_cannot_consume_the_desk_sell():
    events = [
        {"event": "fill", "order_id": "on1", "symbol": "AAA", "side": "buy",
         "filled_avg_price": 9.0, "filled_qty": 1, "ts": _ts("09:40"),
         "client_order_id": "on-2026-09-25-AAA-buy"},
        {"event": "fill", "order_id": "desk", "symbol": "AAA", "side": "buy",
         "filled_avg_price": 10.0, "filled_qty": 1, "ts": _ts("10:05"),
         "client_order_id": "desk-1"},
        {"event": "fill", "order_id": "sell", "symbol": "AAA", "side": "sell",
         "filled_avg_price": 11.0, "filled_qty": 1, "ts": _ts("10:20"),
         "client_order_id": ""},
        {"event": "fill", "order_id": "osell", "symbol": "BBB", "side": "sell",
         "filled_avg_price": 4.0, "filled_qty": 1, "ts": _ts("10:25"),
         "client_order_id": "mir-exit"},
        {"event": "fill", "order_id": "bb", "symbol": "BBB", "side": "buy",
         "filled_avg_price": 5.0, "filled_qty": 1, "ts": _ts("10:10"),
         "client_order_id": "desk-2"},
    ]
    trips, open_buys = closed_trips(day_book(events))
    assert [(t["symbol"], t["entry_px"], t["exit_px"]) for t in trips] == [("AAA", 10.0, 11.0)]
    assert open_buys == 1


def test_fifo_uses_the_last_fill_per_order_and_drops_other_books_at_classify_time():
    buy_early = _ts("10:00")
    buy_late = _ts("10:05")
    sell = _ts("10:20")
    events = [
        {"event": "fill", "order_id": "b1", "symbol": "AAA", "side": "buy",
         "filled_avg_price": 10.0, "filled_qty": 10, "filled_at": datetime.fromtimestamp(buy_early, ET).isoformat(),
         "client_order_id": "desk-1"},
        {"event": "fill", "order_id": "b1", "symbol": "AAA", "side": "buy",
         "filled_avg_price": 10.5, "filled_qty": 10, "filled_at": datetime.fromtimestamp(buy_early + 1, ET).isoformat(),
         "client_order_id": "desk-1"},
        {"event": "fill", "order_id": "b2", "symbol": "AAA", "side": "buy",
         "filled_avg_price": 11.0, "filled_qty": 5, "filled_at": datetime.fromtimestamp(buy_late, ET).isoformat(),
         "client_order_id": "on-2026-09-25-AAA-buy"},
        {"event": "fill", "order_id": "s1", "symbol": "AAA", "side": "sell",
         "filled_avg_price": 11.0, "filled_qty": 10, "filled_at": datetime.fromtimestamp(sell, ET).isoformat(),
         "client_order_id": ""},
        {"event": "submit", "order_id": "b9", "symbol": "AAA", "side": "buy", "filled_avg_price": 1},
    ]
    trips, open_buys = closed_trips(events)
    assert len(trips) == 1
    assert trips[0]["entry_px"] == 10.5
    assert trips[0]["bp"] == (11.0 / 10.5 - 1.0) * 1e4
    assert trips[0]["cid"] == "desk-1"
    assert open_buys == 1


def test_a_day_folder_keeps_the_linked_cash_session_trips_only(tmp_path: Path):
    folder = tmp_path / DAY
    folder.mkdir()
    rows = [
        _sug(_ts("08:31"), "grok_suggestions.json", _ts("08:31"), ["AAA"]),
        {"ts": _ts("09:40"), "file": "movers_stocks.json",
         "data": {"ts": _ts("09:40"), "rows": [{"symbol": "MMM"}]}},
    ]
    with gzip.open(folder / "state_snapshots.jsonl.gz", "wt") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
    buy = _ts("10:00")
    sell = _ts("10:30")
    early = _ts("09:00")
    fills = [
        {"event": "fill", "order_id": "a", "symbol": "AAA", "side": "buy",
         "filled_avg_price": 10, "filled_qty": 1, "ts": buy, "client_order_id": "abc"},
        {"event": "fill", "order_id": "as", "symbol": "AAA", "side": "sell",
         "filled_avg_price": 11, "filled_qty": 1, "ts": sell, "client_order_id": ""},
        {"event": "fill", "order_id": "m", "symbol": "MMM", "side": "buy",
         "filled_avg_price": 20, "filled_qty": 1, "ts": buy, "client_order_id": "def"},
        {"event": "fill", "order_id": "ms", "symbol": "MMM", "side": "sell",
         "filled_avg_price": 19, "filled_qty": 1, "ts": sell, "client_order_id": ""},
        {"event": "fill", "order_id": "e", "symbol": "AAA", "side": "buy",
         "filled_avg_price": 10, "filled_qty": 1, "ts": early, "client_order_id": "abc"},
        {"event": "fill", "order_id": "es", "symbol": "AAA", "side": "sell",
         "filled_avg_price": 10, "filled_qty": 1, "ts": buy, "client_order_id": ""},
        {"event": "fill", "order_id": "z", "symbol": "ZZZ", "side": "buy",
         "filled_avg_price": 5, "filled_qty": 1, "ts": buy, "client_order_id": "ghi"},
        {"event": "fill", "order_id": "zs", "symbol": "ZZZ", "side": "sell",
         "filled_avg_price": 5, "filled_qty": 1, "ts": sell, "client_order_id": ""},
    ]
    (folder / "fills.jsonl").write_text("".join(json.dumps(r) + "\n" for r in fills))
    oks = [
        {"event_kind": "entry_ok", "symbol": "AAA", "ts": buy - 5},
        {"event_kind": "entry_ok", "symbol": "MMM", "ts": buy - 5},
        {"event_kind": "entry_ok", "symbol": "ZZZ", "ts": buy - 5},
        {"event_kind": "entry_ok", "symbol": "AAA", "ts": early - 5},
    ]
    (folder / "decision_ledger.jsonl").write_text("".join(json.dumps(r) + "\n" for r in oks))
    out = analyze_day(folder, DAY)
    arms = sorted((t["arm"], t["symbol"]) for t in out["trips"])
    assert arms == [("agent", "AAA"), ("movers", "MMM")]
    assert out["dropped"]["outside_window"] == 1
    assert out["dropped"]["no_seat"] == 1
    assert out["trips"][0]["pct"] == 12.0 or out["trips"][1]["pct"] == 12.0


def test_grade_needs_both_halves_and_the_combined_t():
    def stats(paired, trips, agent, diff, t):
        return {
            "paired_days": paired, "agent_trips": trips, "movers_trips": trips,
            "agent_mean": agent, "diff_mean": diff, "t": t,
        }
    good = stats(6, 20, 5.0, 4.0, 2.0)
    assert grade(good, good, good) == "PROMISING"
    weak_t = stats(6, 20, 5.0, 4.0, 1.49)
    assert grade(weak_t, weak_t, weak_t) == "FAIL"
    wrong = stats(6, 20, -1.0, -2.0, -1.0)
    thin = stats(3, 10, 5.0, 4.0, 2.0)
    no_t = stats(6, 20, 5.0, 4.0, None)
    assert grade(wrong, thin, wrong) == "FAIL"
    assert grade(thin, thin, thin) == "UNDERPOWERED"
    assert grade(good, thin, good) == "UNDERPOWERED"
    assert grade(no_t, no_t, no_t) == "UNDERPOWERED"


def test_half_stats_are_day_equal_and_already_up_ignores_a_missing_pct():
    trips = [
        {"day": DAYS_A[0], "arm": "agent", "bp": 10.0, "pct": 12.0, "overlap": True},
        {"day": DAYS_A[0], "arm": "agent", "bp": 30.0, "pct": 1.0, "overlap": False},
        {"day": DAYS_A[0], "arm": "movers", "bp": 0.0, "pct": None, "overlap": False},
        {"day": DAYS_A[1], "arm": "agent", "bp": 10.0, "pct": None, "overlap": False},
    ]
    stats = half_stats(trips, DAYS_A)
    assert stats["paired_days"] == 1
    assert stats["agent_trips"] == 2
    assert stats["movers_trips"] == 1
    assert stats["unpaired_agent_trips"] == 1
    assert stats["agent_mean"] == 20.0
    assert stats["diff_mean"] == 20.0
    assert stats["t"] is None
    assert already_up(trips) == (1, 2, 1)
    assert len(DAYS_A) == 8 and len(DAYS_B) == 7
    assert DAYS_A[-1] == "2026-10-06" and DAYS_B[0] == "2026-10-07"
