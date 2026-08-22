"""The spread-relative trail knobs, and the cap that makes them safe to arm.

``ai_local_trail_give_spread_k`` floors the cushion at k round trips. It is
applied last and outranks every other rule, which is correct on a 0.06R book
and dangerous on the tail: RTH spread_r runs p90 5.56R
(tools/spread_coverage.py, 2026-08-17..21). Uncapped, k=1 there parks the
shelf 5.5R under the peak, which is not a wide stop — it is no stop.

``ai_watch_open_seed_min_pct`` exists because ``ai_watch_min_pct_change``
gates only the big-mover seed while the soft open seed — where most
admissions come from — had no percent gate at all. Default 0.0 must keep
the shipped behaviour exactly.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ai_positions = pytest.importorskip("ai_positions")


def _give(**cfg):
    # min_give_px is the legacy six-cent floor and would mask the knob under
    # test (0.10R x 0.50 = $0.05 sits below it); zero it so these assertions
    # are about the spread floor and nothing else.
    base = {"ai_local_trail_give_r": 0.10, "ai_local_trail_give_max_pct": 0.0,
            "ai_local_trail_min_give_px": 0.0}
    base.update(cfg)
    return ai_positions.local_trail_give(
        last=10.0, risk=0.50, cfg=base, mfe_r=0.0,
        spread_r=cfg.pop("_spread_r", None))


def test_spread_floor_off_by_default():
    """k=0 is shipped: the cushion is give_r x R and nothing else."""
    assert _give(_spread_r=1.0) == pytest.approx(0.05)


def test_spread_floor_widens_a_narrow_cushion():
    """Book 0.20R wide, k=1: 0.10R give is too tight, floor lifts it."""
    g = _give(ai_local_trail_give_spread_k=1.0, _spread_r=0.20)
    assert g == pytest.approx(0.20 * 0.50)


def test_spread_floor_does_not_shrink_a_wide_cushion():
    """A book narrower than the give must leave the give alone."""
    g = _give(ai_local_trail_give_spread_k=1.0, _spread_r=0.02)
    assert g == pytest.approx(0.05)


def test_spread_floor_is_capped_so_it_stays_a_stop():
    """p90 book (5.56R) must not become a 5.56R cushion."""
    g = _give(ai_local_trail_give_spread_k=1.0, _spread_r=5.56)
    assert g == pytest.approx(0.50 * 0.50)   # capped at 0.50R, not 5.56R
    assert g < 5.56 * 0.50


def test_cap_is_configurable_and_can_be_disabled():
    wide = {"ai_local_trail_give_spread_k": 1.0, "_spread_r": 5.56}
    assert _give(ai_local_trail_give_spread_max_r=0.25, **wide) == \
        pytest.approx(0.25 * 0.50)
    # 0 disables the cap — the old, unbounded behaviour, opt-in only.
    assert _give(ai_local_trail_give_spread_max_r=0.0, **wide) == \
        pytest.approx(5.56 * 0.50)


def test_missing_spread_reading_applies_no_floor():
    """No quote is no opinion — it must not be read as a zero spread."""
    assert _give(ai_local_trail_give_spread_k=1.0, _spread_r=None) == \
        pytest.approx(0.05)


def test_open_seed_knob_defaults_to_shipped_behaviour():
    from config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["ai_watch_open_seed_min_pct"] == 0.0


def test_open_seed_knob_is_distinct_from_the_big_mover_knob():
    """They are different gates on different paths; do not collapse them."""
    from config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["ai_watch_min_pct_change"] == 50.0
    assert (DEFAULT_CONFIG["ai_watch_open_seed_min_pct"]
            != DEFAULT_CONFIG["ai_watch_min_pct_change"])


def test_open_seed_gate_is_wired_into_the_soft_seed_path():
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "ai_entry_watch.py"),
        encoding="utf-8").read()
    assert 'cfg.get("ai_watch_open_seed_min_pct", 0.0)' in src
    assert "if open_seed_min_pct > 0:" in src
