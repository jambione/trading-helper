"""ai_exit_min_hold_sec: hold back discretionary exits, never protection.

Realized R is monotone in how long a trade was allowed to live (hold <10s
returns -0.088 R at a 2% win rate; hold >10m returns +0.055 R at 52%).
Survival is an outcome and cannot be chosen — the delay can.

The whole trade being made is that max loss becomes the 1R disaster stop
instead of the 0.10R shelf. So the two things that make the wait survivable
— the hard stop and the 15:50 flatten — must never be gated, and the
default must be off.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

cp = pytest.importorskip("ai_positions")

NOW = 1_787_000_000.0


def _pos(age_sec):
    return {"entry_time": NOW - age_sec}


def test_off_by_default():
    """Shipped behaviour: every exit armed from the first tick."""
    from config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["ai_exit_min_hold_sec"] == 0
    assert cp.soft_exit_held_back(_pos(1), NOW, {}) is False


def test_young_fill_is_held_back():
    cfg = {"ai_exit_min_hold_sec": 1800}
    assert cp.soft_exit_held_back(_pos(60), NOW, cfg) is True
    assert cp.soft_exit_held_back(_pos(1799), NOW, cfg) is True


def test_matured_fill_is_released():
    cfg = {"ai_exit_min_hold_sec": 1800}
    assert cp.soft_exit_held_back(_pos(1800), NOW, cfg) is False
    assert cp.soft_exit_held_back(_pos(3600), NOW, cfg) is False


def test_zero_and_negative_never_hold():
    for v in (0, 0.0, -1, "", None):
        assert cp.soft_exit_held_back(_pos(1), NOW, {"ai_exit_min_hold_sec": v}) is False


def test_unknown_age_fails_open():
    """No entry_time must not read as 'young' and pin a position open."""
    cfg = {"ai_exit_min_hold_sec": 1800}
    assert cp.soft_exit_held_back({}, NOW, cfg) is False
    assert cp.soft_exit_held_back({"entry_time": None}, NOW, cfg) is False
    assert cp.soft_exit_held_back({"entry_time": "junk"}, NOW, cfg) is False
    assert cp.soft_exit_held_back(None, NOW, cfg) is False


def test_garbage_config_fails_open():
    assert cp.soft_exit_held_back(_pos(1), NOW, {"ai_exit_min_hold_sec": "soon"}) is False


def test_it_gates_the_three_discretionary_exits():
    """Shelf, dead-trade and left-overbought — the desk's opinions.

    Asserted as a property rather than as three literal lines: the first
    version of this test pinned the exact one-line form of each condition
    and broke the moment the held-back branch was split out to be counted,
    which is a test failing on formatting rather than on behaviour.
    """
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "ai_positions.py"), encoding="utf-8").read()
    # 5 mentions total: the definition, three gates, and one observational
    # read in the shadow logger. The logger reads the state and decides
    # nothing, which is the distinction worth pinning — if that count moves,
    # something new is either gating on the delay or has stopped recording it.
    assert src.count("soft_exit_held_back(pos") == 5
    assert src.count('"min_hold_active": soft_exit_held_back(pos, now)') == 1
    for which in ("local_trail", "left_overbought", "dead_trade"):
        assert f'_note_min_hold(pos, "{which}"' in src, (
            f"{which} suppression must be counted or GATE 1 has no mechanism")


def test_every_gated_exit_also_records_the_block():
    """A suppressed exit that logs nothing is an experiment with no readout."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "ai_positions.py"), encoding="utf-8").read()
    assert src.count("_note_min_hold(") == 4      # 3 call sites + the def


def test_noting_a_block_counts_and_labels_without_deciding():
    pos = {"entry_time": NOW}
    cp._note_min_hold(pos, "local_trail", NOW)
    cp._note_min_hold(pos, "dead_trade", NOW + 1)
    assert pos["min_hold_blocks"] == 2
    assert pos["min_hold_last"] == "dead_trade"
    assert pos["min_hold_last_ts"] == NOW + 1


def test_noting_a_block_survives_a_corrupt_counter():
    pos = {"min_hold_blocks": "lots"}
    cp._note_min_hold(pos, "local_trail", NOW)
    assert pos["min_hold_blocks"] == 1


def test_noting_a_block_on_a_non_position_is_a_no_op():
    cp._note_min_hold(None, "local_trail", NOW)      # must not raise


def test_the_disaster_stop_is_never_gated():
    """1R stop and the 15:50 flatten are what make the wait survivable.

    They are placed with the broker and run through liquidate_all, neither
    of which consults soft_exit_held_back — pinned here because gating one
    of them later would turn a bounded loss into an unbounded one.
    """
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "ai_positions.py"), encoding="utf-8").read()
    i = src.index("def soft_exit_held_back")
    j = src.index("def _note_min_hold")
    body = src[i:j]
    for forbidden in ("entry_stop_price", "liquidate_all", "eod"):
        assert forbidden not in body, (
            f"{forbidden} must stay outside the min-hold gate")
