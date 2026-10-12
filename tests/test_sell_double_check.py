"""Sell double-check: %R lookback ending at the live reading.

The windows below are the operator's 2026-10-12 chart-clip reads
(docs/training/chart-clips/). They are the indicator values, two minutes
apart, so the tests judge the call on those readings directly.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import sell_double_check as sdc

# IREN 10:23–10:33. Both lines fell, one brief lift in the middle.
IREN_FAST = [-55.1, -65.5, -65.5, -53.1, -66.2, -80.1]
IREN_SLOW = [-83.7, -86.7, -83.1, -76.1, -83.3, -91.1]
# CLWT 08:32–08:42. Both lines climbed, then eased on the last print.
CLWT_FAST = [-32.8, -36.0, -29.1, -16.9, -15.1, -22.0]
CLWT_SLOW = [-56.3, -56.2, -51.0, -44.1, -35.3, -38.9]
# ONDS 09:27–09:37. Fast eased, slow rose out of the hole. Not a down pair.
ONDS_FAST = [-32.1, -21.9, -45.6, -47.8, -40.0, -45.0]
ONDS_SLOW = [-90.8, -58.0, -67.4, -67.9, -58.1, -59.4]
# DAIC 08:46–08:56. Both lines fell and stayed down on the last print.
DAIC_FAST = [-22.0, -30.1, -46.9, -53.0, -57.8, -57.6]
DAIC_SLOW = [-27.5, -37.5, -57.3, -73.4, -84.4, -86.2]


def _call(fast, slow, lookback=None):
    n = lookback or len(fast)
    return sdc.decide(fast, slow, lookback=n)


def test_iren_both_lines_down_is_sell_yes():
    got = _call(IREN_FAST, IREN_SLOW)
    assert got["sell"] == "yes"
    assert got["trend"] == "down"
    assert got["fast_trend"] == "down"
    assert got["slow_trend"] == "down"
    assert got["fast_now"] == -80.1
    assert got["slow_now"] == -91.1


def test_clwt_last_print_turned_down_is_sell_yes():
    """The push was up, then both lines eased off the high. Any downturn sells."""
    got = _call(CLWT_FAST, CLWT_SLOW)
    assert got["fast_trend"] == "down"
    assert got["slow_trend"] == "down"
    assert got["sell"] == "yes"
    assert got["trend"] == "down"


def test_onds_both_eased_off_the_high_is_sell_yes():
    """Slow rose out of the hole, then both live readings sat under the window high."""
    got = _call(ONDS_FAST, ONDS_SLOW)
    assert got["fast_trend"] == "down"
    assert got["slow_trend"] == "down"
    assert got["sell"] == "yes"


def test_daic_both_lines_down_is_sell_yes():
    got = _call(DAIC_FAST, DAIC_SLOW)
    assert got["sell"] == "yes"
    assert got["trend"] == "down"


def test_modest_rollover_like_vz_is_sell_yes():
    """Both lines bend a couple of points off the high. That is a sell."""
    fast = [-30, -24, -18, -14, -16, -22, -27.2]
    slow = [-34, -30, -26, -22, -23, -26, -28.4]
    got = _call(fast, slow)
    assert got["fast_trend"] == "down"
    assert got["slow_trend"] == "down"
    assert got["sell"] == "yes"
    assert got["trend"] == "down"


def test_still_making_highs_is_sell_no():
    fast = [-40, -36, -32, -28, -24, -20]
    slow = [-60, -55, -50, -46, -42, -38]
    got = _call(fast, slow)
    assert got["trend"] == "up"
    assert got["sell"] == "no"


def test_one_line_still_rising_is_sell_no():
    fast = [-30, -24, -18, -14, -16, -22]
    slow = [-60, -55, -50, -46, -42, -38]
    got = _call(fast, slow)
    assert got["fast_trend"] == "down"
    assert got["slow_trend"] == "up"
    assert got["trend"] == "sideways"
    assert got["sell"] == "no"


def test_flat_pair_is_sideways_sell_no():
    fast = [-40, -40.02, -39.99, -40.01, -40.0, -40.01]
    slow = [-55, -55.01, -54.99, -55.0, -55.02, -55.0]
    got = _call(fast, slow)
    assert got["trend"] == "sideways"
    assert got["sell"] == "no"


def test_missing_lookback_defaults_to_sell_yes():
    got = _call(IREN_FAST[:3], IREN_SLOW, lookback=6)
    assert got["sell"] == "yes"
    assert got["trend"] == "no_lookback"
    assert got["reason"] == "no %R lookback"


def test_hole_in_the_window_is_no_lookback():
    fast = IREN_FAST[:-1] + [None]
    got = sdc.decide(fast, IREN_SLOW, lookback=6)
    assert got["sell"] == "yes"
    assert got["trend"] == "no_lookback"


def test_live_print_is_the_last_point_and_can_flip_the_call():
    """Nine rising closes, then the live reading. A new high stays a hold.
    A live print any amount under the window high is a sell."""
    fast_base = [-50, -48, -46, -44, -42, -40, -38, -36, -34]
    slow_base = [-70, -68, -66, -64, -62, -60, -58, -56, -54]
    holding = _call(fast_base + [-32], slow_base + [-52])
    assert holding["sell"] == "no"
    assert holding["trend"] == "up"
    breaking = _call(fast_base + [-34.2], slow_base + [-54.2])
    assert breaking["sell"] == "yes"
    assert breaking["trend"] == "down"
    assert breaking["fast_now"] == -34.2
    assert breaking["slow_now"] == -54.2


def test_no_realtime_price_is_sell_yes():
    got = sdc.evaluate_symbol("AAPL", ([0], [1], [1], [1]), None)
    assert got["sell"] == "yes"
    assert got["trend"] == "no_realtime"
    assert got["reason"] == "no realtime price"


def test_no_bars_is_sell_yes():
    got = sdc.evaluate_symbol("AAPL", None, 100.0)
    assert got["sell"] == "yes"
    assert got["trend"] == "no_lookback"
    assert got["reason"] == "no bars"


def test_closed_rows_drop_the_forming_minute():
    # 12:00 and 12:01 closed, 12:02 is the current minute.
    now = 1_700_000_000
    minute = (now // 60) * 60
    stamps = [minute - 120, minute - 60, minute + 10]
    rows = sdc.closed_rows(stamps, [3, 4, 9], [1, 2, 8], [2, 3, 8.5], now)
    assert rows == [(3.0, 1.0, 2.0), (4.0, 2.0, 3.0)]


def _tape(n, start, step):
    rows = []
    px = float(start)
    for _ in range(n):
        px += step
        rows.append((px + 0.05, px - 0.05, px))
    return rows


def test_live_reading_matches_the_desk_and_ends_the_window():
    rows = _tape(140, 20.0, 0.04) + _tape(20, 20.0 + 140 * 0.04, -0.25)
    price = rows[-1][2] - 0.5
    fast = sdc.line_window(rows, price, 21, 7.0, 10)
    slow = sdc.line_window(rows, price, 112, 3.0, 10)
    assert fast is not None and slow is not None
    assert len(fast) == 10 and len(slow) == 10
    assert fast[-1] == sdc.live_value(rows, price, 21, 7.0)
    assert slow[-1] == sdc.live_value(rows, price, 112, 3.0)
    hist = sdc.smoothed_at_closes(rows, 21, 7.0)
    assert fast[:-1] == hist[-9:]
    # The live point is not a copy of the last closed bar.
    assert fast[-1] != hist[-1]


def test_rollover_on_bars_is_sell_yes_and_a_climb_is_sell_no():
    # Long rise, then a break. Both lines leave the top of the pane.
    rise = _tape(150, 20.0, 0.05)
    peak = rise[-1][2]
    drop = _tape(15, peak, -0.35)
    bars_down = _as_bars(rise + drop)
    live = drop[-1][2] - 0.2
    down = sdc.evaluate_symbol(
        "DROP", bars_down, live, lookback=10, now=None)
    assert down["sell"] == "yes"
    assert down["trend"] == "down"
    assert down["fast_now"] == round(sdc.live_value(
        sdc.closed_rows(*bars_down, None), live, 21, 7.0), 2)

    trough = _tape(160, 30.0, -0.04)
    bounce = _tape(15, trough[-1][2], 0.40)
    up = sdc.evaluate_symbol(
        "UP", _as_bars(trough + bounce), bounce[-1][2] + 0.2,
        lookback=10, now=None)
    assert up["sell"] == "no"
    assert up["trend"] in ("up", "sideways")


def _as_bars(rows):
    return (
        None,
        [r[0] for r in rows],
        [r[1] for r in rows],
        [r[2] for r in rows],
    )


def test_broker_book_beats_a_stale_managed_name():
    state = {"OLD": {"qty_a": 10, "qty_b": 10, "entry_confirmed": True}}
    broker = {"NEW": {"qty": 5, "current": 12.5}}
    got = sdc.iter_open(state, broker)
    assert [r["symbol"] for r in got] == ["NEW"]
    assert got[0]["price"] == 12.5


def test_flat_broker_is_empty_even_if_state_has_a_name():
    state = {"OLD": {"qty": 10}}
    assert sdc.iter_open(state, {}) == []


def test_managed_book_when_the_broker_does_not_answer():
    state = {
        "AAA": {"qty_a": 3, "qty_b": 2},
        "BBB": {"qty_a": 1, "qty_b": 1, "closing_reason": "flatten"},
        "CCC": {"qty": 0},
    }
    got = sdc.iter_open(state, None)
    assert [r["symbol"] for r in got] == ["AAA"]
    assert got[0]["qty"] == 5
    assert got[0]["price"] is None


def test_report_names_the_rule_and_an_empty_book():
    text = sdc.format_report(
        [], lookback=10, now_et=__import__("datetime").datetime(2026, 10, 12, 10, 41))
    assert "lookback 10m" in text
    assert "no open positions" in text
    assert "sell yes" in text and "sell no" in text
