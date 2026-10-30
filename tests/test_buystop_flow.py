"""Pure checks for the buystop_flow code-review fixes. No network, no warehouse, no outcomes."""
from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))

import buystop_flow as B  # noqa: E402
import leash_replay as L  # noqa: E402


def test_prereg_records_the_count_and_leaves_the_window_frozen():
    """The 10/24 count is on the record. The power-amendment sessions are not in the prereg yet."""
    prereg = json.loads((Path(__file__).resolve().parent.parent / "docs" / "studies"
                         / "buystop_flow_prereg.json").read_text())
    assert prereg["count_2026_10_24"]["A"]["mde_bp"] == 3.026
    assert prereg["count_2026_10_24"]["A"]["floors_met"] is False
    assert prereg["count_2026_10_24"]["B"]["floors_met"] is True
    assert prereg["count_2026_10_24"]["result"].startswith("UNDERPOWERED")
    assert "2026-05-11" in prereg["population"]
    assert "2026-04-27" not in json.dumps(prereg)


def test_empty_conditions_are_round_lots_and_odd_lots_are_not():
    assert B.round_lot([]) and B.round_lot(None) and B.round_lot([" "]) and B.round_lot(["@", "F"])
    assert not B.round_lot(["I"]) and not B.round_lot([" ", "I"]) and not B.round_lot(["Z"])


def test_trigger_takes_the_empty_condition_print_and_skips_the_odd_lot():
    k0 = 1_000_000.0
    trades = [(k0 + 1, 10.05, 100, [], "C"), (k0 + 2, 10.50, 100, ["I"], "C")]
    assert B.trigger(trades, k0, 10.0) == (k0 + 1, 10.05)


def test_merge_secs_keeps_the_lowest_low_and_the_later_last():
    earlier = [(100, 10.0, 9.5), (101, 10.2, 10.1)]
    later = [(101, 10.4, 10.0), (102, 10.5, 10.3)]
    assert B.merge_secs([earlier, later]) == [(100, 10.0, 9.5), (101, 10.4, 10.0), (102, 10.5, 10.3)]


def test_last_chunk_clears_a_time_cap_and_the_old_end_does_not():
    assert B.arm_b_horizons()[-1] == L.MAX_HOLD + 60
    t0 = 1_700_000_000.4
    eod = t0 + 8 * 3600
    fetched = B.chunk_end(t0, L.MAX_HOLD + 60, eod)
    xs = int(t0) + L.MAX_HOLD
    assert not B.exit_beyond_fetch(xs, fetched, eod)
    old = B.chunk_end(t0, L.MAX_HOLD, eod)
    assert B.exit_beyond_fetch(xs, old, eod)
    # a late entry is capped at 15:50, and that exit is not "beyond the fetch"
    late_end = B.chunk_end(t0, L.MAX_HOLD + 60, t0 + 100)
    assert late_end == t0 + 100
    assert not B.exit_beyond_fetch(t0 + 100, late_end, t0 + 100)


def test_upper_bound_quantile_is_98_75_percent(monkeypatch):
    seen = []
    monkeypatch.setattr(B.W.E, "t_ppf", lambda p, df: seen.append(p) or 2.0)
    rows = [{"day": "d1", "x": 0.02}, {"day": "d2", "x": 0.00}, {"day": "d3", "x": 0.01}]
    assert B.ub_at(rows, lambda r: r["x"], B.UB_P) is not None
    assert seen[-1] == 0.9875
    assert "0.99375" not in open(B.__file__).read()


def test_select_trades_keeps_each_days_prefix_and_ignores_later_rows():
    trades = []
    for h, days in (("A", ("d1", "d2")), ("B", ("d3", "d4"))):
        for d in days:
            for i in range(10):
                trades.append({"half": h, "day": d, "i": i, "bar_gross": 1.0 if i < 3 else -1.0})
    got = B.select_trades(trades, 5)
    assert len([t for t in got if t["half"] == "A"]) == 5
    assert len([t for t in got if t["half"] == "B"]) == 5
    assert all(t["i"] <= 2 for t in got)
    assert all(t["bar_gross"] == 1.0 for t in got)


def test_nine_hour_budget_keeps_the_full_sample_under_the_bar():
    assert B.BUDGET_H == 9.0
    out = B.budget_n_half(0.8355, 2.5065, 7975)
    assert out["hours_at_full_sample"] < B.BUDGET_H
    assert out["n_half"] == 7975
    assert out["projected_under_bar"]
    assert out["projected_mde"]["A"] == B.PREREG_MDE_AT_8000["A"] * math.sqrt(8000 / 7975)
    assert out["projected_mde"]["A"] <= B.MDE_BAR
    # Pace measured by the 2026-10-24 probe. Full sample is 8.82 hours, under 9.
    measured = B.budget_n_half(1.612563891844316, 1.1371237754821777, 7975)
    assert measured["n_half"] == 7975 and measured["projected_under_bar"]
    thin = B.budget_n_half(30.0, 30.0, 7975)
    assert thin["n_half"] == 405
    assert not thin["projected_under_bar"]


def test_probe_indexes_are_spread_across_the_sample():
    idx = B.probe_indexes(15_950)
    assert idx[0] == 0 and idx[-1] > 10_000 and len(idx) == B.PROBE_CAP
    assert idx[1] - idx[0] > 1


def test_probe_blocks_span_the_whole_sample():
    blocks = B.probe_blocks(15_950)
    assert len(blocks) == B.PROBE_MIN
    assert all(blocks)
    assert blocks[0][0] == 0
    assert blocks[-1][0] > 12_000
    assert blocks[B.PROBE_MIN // 2][0] > 15_950 * 0.4


def test_spy_return_is_open_to_open_and_counts_a_missing_bar():
    opens = {600: 100.0, 630: 101.0}
    assert math.isclose(B.spy_return(opens, 600, 630), 0.01)
    assert B.spy_return(opens, 600, 601) is None
    assert B.spy_return({}, 600, 630) is None


def test_quoted_spread_uses_the_touch():
    assert math.isclose(B.spread_bp(10.0, 10.02), (0.02 / 10.01) * 1e4)
    assert B.spread_bp(0, 10) is None


def test_arm_b_below_the_floor_is_underpowered_not_a_pass():
    assert B.arm_half_verdict(0.01, 5.0, 0.01, 0.01, 0.01, 10, 10) == "UNDERPOWERED"
    assert B.arm_half_verdict(0.01, 5.0, 0.01, 0.01, 0.01, 300, 20) == "PASS"
    assert B.arm_half_verdict(-0.01, -2.5, 0.0, 0.0, 0.0, 300, 20) == "FAIL"
    assert B.rollup(["UNDERPOWERED", "NOT_PASSED"], [-0.01, -0.01]) == "UNDERPOWERED"
    assert B.rollup(["NOT_PASSED", "NOT_PASSED"], [-0.01, -0.01]) == "FAIL"


def test_module_shas_cover_the_exit_stack():
    shas = B.module_shas()
    for name in ("ai_positions", "bars", "config", "leash_replay", "eod_reversal",
                 "indicator_confirm_hist", "exit_capture", "order_flow_pilot",
                 "sell_double_check", "ai_entry_watch"):
        assert len(shas[name]) == 64


def test_stale_sample_is_refused():
    with pytest.raises(SystemExit, match="rerun --sample"):
        B.assert_sample_current({"module_sha256": {}})
    with pytest.raises(SystemExit, match="rerun --sample"):
        B.assert_sample_current(None)
    B.assert_sample_current({"module_sha256": B.module_shas()})


def test_arm_b_fetches_bars_through_req(monkeypatch):
    seen = {}

    def fake_req(fn, *a, **k):
        seen.setdefault("fns", []).append(getattr(fn, "__name__", str(fn)))
        return []

    monkeypatch.setattr(B, "_req", fake_req)
    monkeypatch.setattr(B, "pace", lambda: None)
    monkeypatch.setattr(B, "arm_b_walk", lambda *a, **k: (a[0]["T"] + 10, 1.0, "trail", 0))
    row = {"sym": "AAA", "day": "2026-06-02", "T": B.et_epoch("2026-06-02", 600),
           "ask_in": 20.0, "ask_raw": 20.0, "bid_in": 19.9}
    B.fetch_arm_b(object(), row, {}, 3)
    assert "fetch_bars" in seen["fns"]
