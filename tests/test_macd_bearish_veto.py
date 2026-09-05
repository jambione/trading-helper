"""The direction half of the MACD stack, on its own knob.

``ai_watch_arm_require_macd`` answers three different questions at once:

  DIRECTION      crossed down (macd_bearish), gap closing (macd_gap_narrowing)
  SIZE           macd_min_gap, macd_sep_mult
  AVAILABILITY   no_macd_data, macd_src_unknown, macd_stale_bars

Measured over 2026-08-31..09-04 the bundle refused 84-94% of every arm
decision the desk made, with macd_bearish the largest single reason each
session (2,175-4,846/day). Turning the bundle off to stop SIZE and
AVAILABILITY starving the opens drops DIRECTION with them — and a fast line
under its slow line is the one of the three that says the trade is wrong
rather than merely small or unmeasured.

``ai_watch_macd_block_bearish`` is that test standing alone, fail-open on a
missing reading exactly like the narrowing veto beside it.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import ai_entry_watch as ew  # noqa: E402

# The veto-only arm path: MACD is not required, direction still refuses.
VETO = {"ai_watch_arm_require_macd": False,
        "ai_watch_macd_block_bearish": True}
OFF = {"ai_watch_arm_require_macd": False,
       "ai_watch_macd_block_bearish": False}


def _rec(**ind):
    base = {"macd_fast": 0.10, "macd_slow": 0.05, "macd_gap": 0.05}
    base.update(ind)
    return {"symbol": "AAA", "indicator": base}


# ── the rule ─────────────────────────────────────────────────────────────

def test_crossed_down_is_refused():
    rec = _rec(macd_fast=0.02, macd_slow=0.06, macd_gap=-0.04)
    why = ew.macd_bearish_blocks_buy(rec, VETO)
    assert why == "macd_bearish"
    assert "0.0200" in str(rec.get("block_detail"))


def test_a_negative_histogram_is_bearish_even_with_fast_above_slow():
    # Both halves of the live test, kept identical to macd_allows_buy:
    # `fast <= slow or gap <= 0`. A crossed-up pair with a negative
    # histogram is a contradiction the sim must not resolve differently
    # from the desk.
    why = ew.macd_bearish_blocks_buy(
        _rec(macd_fast=0.10, macd_slow=0.05, macd_gap=-0.01), VETO)
    assert why == "macd_bearish"


def test_an_open_bullish_pair_passes():
    assert ew.macd_bearish_blocks_buy(_rec(), VETO) is None


def test_the_knob_off_is_silent():
    assert ew.macd_bearish_blocks_buy(
        _rec(macd_fast=0.02, macd_slow=0.06, macd_gap=-0.04), OFF) is None


# ── availability is not a verdict ────────────────────────────────────────

def test_missing_macd_fails_open_by_default():
    # The whole point of the split: no_macd_data is 8,090 refusals across
    # the run and says nothing about the trade. A veto that blocks on
    # blindness re-creates the starvation require_macd=false removed.
    assert ew.macd_bearish_blocks_buy({"symbol": "AAA", "indicator": {}},
                                      VETO) is None


def test_missing_macd_can_fail_closed_when_asked():
    rec = {"symbol": "AAA", "indicator": {}}
    why = ew.macd_bearish_blocks_buy(rec, VETO, fail_open_unknown=False)
    assert why == "no_macd_data"


def test_a_record_with_no_indicator_dict_fails_open():
    assert ew.macd_bearish_blocks_buy({"symbol": "AAA"}, VETO) is None


# ── line/signal aliases, as macd_allows_buy reads them ───────────────────

def test_line_and_signal_aliases_are_honoured():
    rec = {"symbol": "AAA",
           "indicator": {"macd_line": 0.02, "macd_signal": 0.06,
                         "macd_hist": -0.04}}
    assert ew.macd_bearish_blocks_buy(rec, VETO) == "macd_bearish"


# ── agreement with the bundled path ──────────────────────────────────────

def test_standalone_veto_matches_the_full_stack_on_direction():
    """Same input, same verdict, whichever path evaluates it."""
    bear = {"macd_fast": 0.02, "macd_slow": 0.06, "macd_gap": -0.04,
            "macd_sep_ratio": 1.5}
    full = {"ai_watch_arm_require_macd": True}
    ok, why = ew.macd_allows_buy({"symbol": "AAA", "indicator": dict(bear)},
                                 full)
    assert (ok, why) == (False, "macd_bearish")
    assert ew.macd_bearish_blocks_buy(
        {"symbol": "AAA", "indicator": dict(bear)}, VETO) == "macd_bearish"


def test_the_veto_does_not_apply_the_size_tests():
    """A bullish gap far too small for macd_min_gap still passes.

    This is the whole reason the knob exists: size is what starves the
    open, direction is what protects it.
    """
    tiny = {"macd_fast": 0.1002, "macd_slow": 0.1000, "macd_gap": 0.0002}
    assert ew.macd_bearish_blocks_buy(
        {"symbol": "AAA", "indicator": dict(tiny)}, VETO) is None
    ok, why = ew.macd_allows_buy(
        {"symbol": "AAA", "indicator": dict(tiny, macd_sep_ratio=1.5)},
        {"ai_watch_arm_require_macd": True})
    assert (ok, why) == (False, "macd_gap_too_close")
