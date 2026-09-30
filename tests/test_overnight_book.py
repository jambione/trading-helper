"""overnight_book.build_snapshot — what the dashboard's Overnight book strip reads."""
from datetime import datetime
from zoneinfo import ZoneInfo

import overnight_book as ob

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 1, 10, 0, tzinfo=ET)
PLAN = {"day": "2026-09-30", "data_through": "2026-09-29", "n_liquid": 1421,
        "picks": [{"sym": "AAA", "mom": 2.5, "adv20": 9e7, "last_close": 50.0},
                  {"sym": "BBB", "mom": 1.5, "adv20": 8e7, "last_close": 20.0}]}


def test_empty_book_shows_the_plan_and_no_score():
    snap = ob.build_snapshot(PLAN, [], [], [], {}, {}, None, NOW)
    assert snap["book_night"] is None
    assert snap["rows"] == []
    assert snap["totals"] is None
    assert [p["sym"] for p in snap["plan"]["picks"]] == ["AAA", "BBB"]
    assert snap["backtest_bp"] == ob.BACKTEST_BP


def test_book_pairs_buy_and_sell_fills_for_the_latest_night():
    ledger = [
        # an older night must not leak into the book
        {"event": "submit", "night": "2026-09-29", "sym": "OLD", "side": "buy", "qty": 3},
        {"event": "submit", "night": "2026-09-30", "sym": "AAA", "side": "buy", "qty": 20, "ref_price": 50.0},
        {"event": "submit", "night": "2026-09-30", "sym": "BBB", "side": "buy", "qty": 50, "ref_price": 20.0},
        {"event": "fill", "day": "2026-09-30", "leg": "buy", "sym": "AAA", "status": "filled",
         "fill": 50.0, "cross": 50.0, "fill_vs_cross_bp": 0.0, "filled_qty": 20},
        {"event": "fill", "day": "2026-10-01", "leg": "sell", "sym": "AAA", "status": "filled",
         "fill": 50.5, "cross": 50.5, "fill_vs_cross_bp": 0.0},
    ]
    snap = ob.build_snapshot(PLAN, ledger, [], [], {}, {}, None, NOW)
    assert snap["book_night"] == "2026-09-30"
    by = {r["sym"]: r for r in snap["rows"]}
    assert set(by) == {"AAA", "BBB"}
    assert abs(by["AAA"]["night_bp"] - 100.0) < 1e-9  # 50.0 -> 50.5
    assert by["BBB"]["night_bp"] is None           # MOC still pending
    assert by["BBB"]["qty"] == 50


def test_positions_are_attached_and_counted():
    ledger = [{"event": "submit", "night": "2026-09-30", "sym": "AAA", "side": "buy", "qty": 20}]
    pos = [{"symbol": "AAA", "qty": 20.0, "avg_entry_price": 50.0, "market_value": 1004.0}]
    snap = ob.build_snapshot(PLAN, ledger, [], pos, {"equity": 25000.0}, {}, None, NOW)
    assert snap["holding"] == 1
    assert snap["rows"][0]["held_qty"] == 20.0
    assert snap["account"]["equity"] == 25000.0


def test_totals_average_the_scored_nights_and_list_newest_first():
    nights = [
        {"night_end": "2026-10-01", "names": 20, "mean_bp_fills": 30.0, "pnl_usd": 55.0},
        {"night_end": "2026-10-02", "names": 20, "mean_bp_fills": -10.0, "pnl_usd": -20.0},
        {"night_end": "2026-10-03", "names": 0, "mean_bp_fills": None, "pnl_usd": 0.0},
    ]
    snap = ob.build_snapshot(PLAN, [], nights, [], {}, {}, None, NOW)
    t = snap["totals"]
    assert t["nights"] == 2
    assert t["mean_bp"] == 10.0
    assert t["pnl_usd"] == 35.0
    assert t["green"] == 1
    assert [n["night_end"] for n in snap["nights"]] == ["2026-10-02", "2026-10-01"]


def test_only_error_steps_are_surfaced():
    state = {"plan": "ok 06:30", "sell": "error boom", "buy": "skipped late at 15:55"}
    snap = ob.build_snapshot(PLAN, [], [], [], {}, state, {"name": "buy", "at": 1.0}, NOW)
    assert snap["errors"] == {"sell": "error boom"}
    assert snap["next_step"]["name"] == "buy"
