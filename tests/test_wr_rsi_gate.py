"""RSI direction gate (ai_watch_wr_rsi_min_rise, 10/10 counterfactual; default off)."""
from datetime import datetime
from zoneinfo import ZoneInfo

import ai_entry_watch as ew
import ob_observe
from tools.studies.rsi_history_fills import wilder_rsi

ET = ZoneInfo("America/New_York")
OPEN = datetime(2026, 10, 9, 9, 30, tzinfo=ET).timestamp()
CFG = {"ai_watch_wr_rsi_min_rise": 10}


def load(sym, closes, skip=()):
    ob_observe.reset()
    rows = [(OPEN + 60 * k, c, c + 0.01, c - 0.01, c) for k, c in enumerate(closes) if k not in skip]
    ob_observe.absorb_rows(sym, rows)


def test_off_by_default():
    assert ew.wr_rsi_refusal("AAA", OPEN + 3600, {}) is None


def test_wilder_matches_the_quick_look():
    xs = [10 + (k % 7) * 0.03 + k * 0.001 for k in range(80)]
    assert ew._wilder_rsi(xs) == wilder_rsi(xs)


def test_too_few_bars_refused():
    load("FEW", [10.0 + 0.01 * k for k in range(30)])
    assert ew.wr_rsi_refusal("FEW", OPEN + 31 * 60, CFG) == "wr_rsi_no_bars"


def test_turning_up_passes_falling_refused():
    down_then_up = [20 - 0.02 * k for k in range(50)] + [19 + 0.03 * k for k in range(15)]
    load("UP", down_then_up)
    assert ew.wr_rsi_refusal("UP", OPEN + 65 * 60, CFG) is None
    load("DN", [20 - 0.02 * k for k in range(65)])
    assert ew.wr_rsi_refusal("DN", OPEN + 65 * 60, CFG) == "wr_rsi_not_trending"


def test_forming_bar_ignored_and_gaps_filled_flat():
    closes = [20 - 0.02 * k for k in range(50)] + [19 + 0.03 * k for k in range(15)]
    load("GAP", closes, skip=(55, 56))
    filled = closes[:55] + [closes[54], closes[54]] + closes[57:64]   # bar 64 is still forming at OPEN + 64.5 min
    r = wilder_rsi(filled)
    want = None if r[-1] - r[-11] >= 10 else "wr_rsi_not_trending"
    assert ew.wr_rsi_refusal("GAP", OPEN + 64.5 * 60, CFG) == want
