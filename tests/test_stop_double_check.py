"""The shelf-plus-double-check walk. The checker is injected so a wick can be held."""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import stop_double_check as sdc  # noqa: E402

CFG = {
    "ai_local_trail_enabled": True,
    "ai_local_trail_give_r": 0.12,
    "ai_local_trail_arm_r": 0.06,
    "ai_local_trail_peak_give_pct": 0.35,
    "ai_local_trail_time_decay_enabled": False,
    "ai_local_trail_print_ring": 2,
    "ai_local_trail_be_at_r": 0.0,
    "ai_local_trail_be_at_pct": 0.0,
    "ai_local_trail_entry_catchup_sec": 0.0,
}


def _bars(start_ts, prices):
    """One minute per close. High/low sit 0.05 around the close."""
    out = []
    for i, px in enumerate(prices):
        out.append((start_ts + i * 60, px, px + 0.05, px - 0.05, px))
    return out


def test_a_sell_yes_hit_matches_the_stop_arm():
    # 09:30 ET on 2026-10-12 is 13:30 UTC.
    start = 1760275800.0
    bars = _bars(start, [20.0 + 0.01 * i for i in range(20)] + [19.4])
    entry_ts = start
    stop = sdc.simulate(bars, entry_ts, 20.0, 1.0, CFG, use_gate=False, sell_yes=lambda rows, px: True)
    gated = sdc.simulate(bars, entry_ts, 20.0, 1.0, CFG, use_gate=True, sell_yes=lambda rows, px: True)
    assert stop["reason"] == "stop"
    assert gated["reason"] == "stop"
    assert gated["exit_px"] == stop["exit_px"]
    assert gated["declined"] == 0


def test_a_sell_no_hit_holds_and_the_later_rally_is_kept():
    start = 1760275800.0
    # Rise, one wick through the shelf, then a higher grind. The injected
    # checker declines every touch, so only the 15:50 flatten ends the gate arm.
    up = [20.0 + 0.02 * i for i in range(30)]
    wick = [up[-1] - 0.80]
    resume = [up[-1] + 0.05 * i for i in range(1, 15)]
    bars = _bars(start, up + wick + resume)
    # Force the wick bar's low through any shelf the trail could have.
    w = 30
    ts, op, hi, lo, cl = bars[w]
    bars[w] = (ts, op, hi, lo - 2.0, cl)
    entry_ts = start
    stop = sdc.simulate(bars, entry_ts, 20.0, 1.0, CFG, use_gate=False, sell_yes=lambda rows, px: False)
    gated = sdc.simulate(bars, entry_ts, 20.0, 1.0, CFG, use_gate=True, sell_yes=lambda rows, px: False)
    assert stop["reason"] == "stop"
    assert gated["declined"] >= 1
    assert gated["exit_px"] > stop["exit_px"]


def test_missing_history_is_sell_yes():
    assert sdc.checker_sell_yes([], 20.0) is True
    assert sdc.checker_sell_yes([(20, 19, 20)] * 5, 19.5) is True
    assert sdc.checker_one_line_sell_yes([], 20.0) is True


def test_one_line_sells_when_either_line_is_down():
    # The both-lines utility call is sell no. Either line down still sells.
    one_down = {
        "trend": "sideways", "sell": "no",
        "fast_trend": "down", "slow_trend": "up",
    }
    assert sdc.one_line_sell(one_down) is True
    neither = {
        "trend": "sideways", "sell": "no",
        "fast_trend": "sideways", "slow_trend": "up",
    }
    assert sdc.one_line_sell(neither) is False
    both_up = {
        "trend": "up", "sell": "no",
        "fast_trend": "up", "slow_trend": "up",
    }
    assert sdc.one_line_sell(both_up) is False
    blind = {
        "trend": "no_lookback", "sell": "yes",
        "fast_trend": "no_lookback", "slow_trend": "no_lookback",
    }
    assert sdc.one_line_sell(blind) is True
    both_down = {
        "trend": "down", "sell": "yes",
        "fast_trend": "down", "slow_trend": "down",
    }
    assert sdc.one_line_sell(both_down) is True


def test_bad_plan_stop_is_excluded():
    assert sdc._valid_risk(20.0, 20.0) is None
    assert sdc._valid_risk(20.0, 21.0) is None
    assert sdc._valid_risk(20.0, 19.0) == 1.0
