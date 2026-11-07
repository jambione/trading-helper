import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import broker_truth as bt

T = 1_800_000_000.0


def f(sym, side, qty, px, t):
    return {"symbol": sym, "side": side, "qty": qty, "price": px, "t": T + t}


def test_simple_round_trip():
    rts, un, op = bt.round_trips([f("A", "buy", 10, 100.0, 0), f("A", "sell", 10, 101.0, 60)])
    assert len(rts) == 1 and abs(rts[0]["pnl"] - 10.0) < 1e-9 and abs(rts[0]["bp"] - 100.0) < 1e-9 and un == 0 and op == {}


def test_partial_fills_are_volume_weighted_and_close_once():
    rts, _, _ = bt.round_trips([f("A", "buy", 4, 100.0, 0), f("A", "buy", 6, 101.0, 1), f("A", "sell", 5, 102.0, 2), f("A", "sell", 5, 100.0, 3)])
    assert len(rts) == 1
    assert abs(rts[0]["entry"] - 100.6) < 1e-9 and abs(rts[0]["exit"] - 101.0) < 1e-9 and abs(rts[0]["pnl"] - 4.0) < 1e-9


def test_unmatched_sell_is_counted_not_invented():
    rts, un, _ = bt.round_trips([f("A", "sell", 5, 100.0, 0)])
    assert rts == [] and un == 5


def test_open_position_is_reported():
    rts, _, op = bt.round_trips([f("A", "buy", 5, 100.0, 0)])
    assert rts == [] and op == {"A": 5}


def test_weekly_stats():
    rts, _, _ = bt.round_trips([f("A", "buy", 1, 100.0, 0), f("A", "sell", 1, 101.0, 10),
                                f("B", "buy", 1, 100.0, 20), f("B", "sell", 1, 99.5, 30)])
    (wk, mon, s), = bt.weekly(rts)
    assert s["n"] == 2 and s["win"] == 0.5 and abs(s["pnl"] - 0.5) < 1e-9 and abs(s["bp"] - 25.0) < 1e-9


def test_cross_check_matches_order_level_totals():
    alp = [{"order_id": "o1", "symbol": "A", "side": "buy", "qty": 4, "price": 100.0, "t": T}, {"order_id": "o1", "symbol": "A", "side": "buy", "qty": 6, "price": 101.0, "t": T + 1}]
    arc = [{"order_id": "o1", "symbol": "A", "side": "buy", "qty": 10, "price": 100.6, "t": T}]
    cc = bt.cross_check(alp, arc)
    assert cc["matched"] == 1 and cc["qty_mismatch"] == 0 and cc["max_px_diff_bp"] < 1e-6


def test_orphan_lot_does_not_swallow_later_days():
    D = 86400.0
    fills = [f("A", "buy", 10, 100.0, 0),                                    # day 1: orphan lot (account reset, never sold)
             f("A", "buy", 100, 100.0, D), f("A", "sell", 100, 101.0, D + 60),      # day 2: a clean round trip
             f("A", "buy", 100, 100.0, 2 * D), f("A", "sell", 100, 99.0, 2 * D + 60)]  # day 3: another
    whole, _, still = bt.round_trips(fills)
    by_day, orphans = bt.round_trips_by_day(fills)
    assert len(whole) == 0 and still.get("A") == 10                        # the old behaviour: everything swallowed
    assert len(by_day) == 2 and abs(sum(r["pnl"] for r in by_day) - 0.0) < 1e-9
    assert any(o[1] == "A" and o[2] == 10 for o in orphans)                 # and the orphan is reported with its day


def test_unmatched_sell_is_listed_with_its_day():
    _, orphans = bt.round_trips_by_day([f("A", "sell", 5, 100.0, 0)])
    assert orphans and orphans[0][1] == "*" and orphans[0][2] == 5
