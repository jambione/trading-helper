"""Premarket and RTH must arm on the same thesis.

Phase B carried its own exhaustion band (ai_phase_b_exh_min/max = 40-70) and
its own direction-only RSI rule. When the square arm shipped (e5339b9) that
became a direct contradiction: RTH enters on the dual-%R red square, which
needs BOTH lines overbought — exhaustion >= 100 - rte_threshold, i.e. 80 — and
Phase B refused anything over 70 as ``phase_b_exh_band``. The premarket lane
was gated to reject exactly the state the thesis says to buy.

The indicator legs now call RTH's own functions rather than reimplementing
them, so the lanes cannot drift apart again. Session mechanics — clock, print
freshness, seats, confirm ticks — stay Phase B's, because those genuinely
differ premarket.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import ai_entry_watch as ew
import phase_b as pb


def _cfg(**over):
    """Phase B on, clock open, with the live RTH arm knobs."""
    cfg = {
        "ai_phase_b_enabled": True,
        "ai_phase_b_start_time": "04:00",
        "ai_phase_b_entry_cutoff": "09:20",
        "ai_phase_b_print_max_age_sec": 15.0,
        # RTH arm, as configured live on the mini 2026-09-18.
        "ai_watch_exh_square_arm": True,
        "rte_threshold": 20,
        "rte_confluence_max": 15.0,
        "rte_require_tight": True,
        "ai_watch_exhaustion_rules": True,
        "ai_watch_require_exh_rising": True,
    }
    cfg.update(over)
    return cfg


def _rec(**ind):
    base = {"pctr": -5.0, "pctr_slow": -8.0,      # both OB, gap 3 -> tight
            "pctr_rising": True, "pctr_falling": False,
            "cm_rsi": 35.0, "cm_rsi_rising": True}
    base.update(ind)
    return {"symbol": "AAA", "indicator": base}


@pytest.fixture(autouse=True)
def _fresh_print(monkeypatch):
    """Premarket dies on missing_print before any indicator runs; supply one."""
    monkeypatch.setattr(pb, "fresh_stream_last",
                        lambda sym, now=None, cfg=None: (10.0, None))


def _arm(rec, cfg):
    # 06:00 ET on a weekday — inside the premarket entry window.
    import datetime as dt
    from zoneinfo import ZoneInfo
    now = dt.datetime(2026, 9, 17, 6, 0, tzinfo=ZoneInfo("America/New_York")).timestamp()
    return pb.phase_b_arm_allows(rec, cfg=cfg, now=now)


# ── The conflict this fixes ───────────────────────────────────────────────────

def test_a_red_square_now_arms_premarket():
    """The exact state the old band refused as phase_b_exh_band."""
    ok, why = _arm(_rec(), _cfg())
    assert ok is True, why


def test_the_old_band_would_have_refused_that_same_square():
    """Proves the conflict was real, not theoretical.

    exhaustion 95 (pctr -5) is far above ai_phase_b_exh_max = 70.
    """
    ok, why = _arm(_rec(), _cfg(ai_phase_b_legacy_arm=True))
    assert ok is False
    assert why == "phase_b_exh_band"


def test_premarket_and_rth_agree_on_the_same_record():
    """Not 'both happen to pass' — the same function decides both."""
    cfg, rec = _cfg(), _rec()
    rth_ok, _ = ew.exhaustion_allows_buy(rec, cfg)
    pb_ok, _ = _arm(rec, cfg)
    assert rth_ok is pb_ok is True

    wide = _rec(pctr_slow=-45.0)        # gap 40 -> not tight, not a square
    rth_ok2, rth_why = ew.exhaustion_allows_buy(wide, cfg)
    pb_ok2, pb_why = _arm(wide, cfg)
    assert rth_ok2 is pb_ok2 is False
    assert pb_why == rth_why, "the lanes must refuse for the SAME reason"


# ── The square's own gates now apply premarket ────────────────────────────────

def test_a_wide_gap_is_refused_premarket():
    ok, why = _arm(_rec(pctr_slow=-45.0), _cfg())
    assert ok is False and why == "exh_not_tight"


def test_only_one_line_overbought_is_not_a_square():
    ok, why = _arm(_rec(pctr=-5.0, pctr_slow=-60.0), _cfg())
    assert ok is False


def test_a_missing_slow_line_refuses_premarket():
    """The premarket data problem, surfaced as a refusal rather than a guess."""
    rec = _rec()
    del rec["indicator"]["pctr_slow"]
    ok, why = _arm(rec, _cfg(ai_watch_require_exhaustion_data=True))
    assert ok is False and why == "no_exhaustion_data"


def test_falling_exhaustion_is_refused():
    ok, why = _arm(_rec(pctr_rising=False, pctr_falling=True), _cfg())
    assert ok is False and why == "exh_falling"


# ── Session mechanics stay Phase B's ──────────────────────────────────────────

def test_the_entry_clock_still_gates(monkeypatch):
    import datetime as dt
    from zoneinfo import ZoneInfo
    late = dt.datetime(2026, 9, 17, 9, 45,
                       tzinfo=ZoneInfo("America/New_York")).timestamp()
    ok, why = pb.phase_b_arm_allows(_rec(), cfg=_cfg(), now=late)
    assert ok is False and why == "phase_b_outside_entries"


def test_a_missing_print_still_refuses_before_any_indicator(monkeypatch):
    """Premarket's real blocker runs first and is unchanged."""
    monkeypatch.setattr(pb, "fresh_stream_last",
                        lambda sym, now=None, cfg=None: (None, "phase_b_missing_print"))
    ok, why = _arm(_rec(), _cfg())
    assert ok is False and why == "phase_b_missing_print"


def test_disabled_still_refuses():
    ok, why = _arm(_rec(), _cfg(ai_phase_b_enabled=False))
    assert ok is False and why == "phase_b_disabled"


# ── The rollback knob ─────────────────────────────────────────────────────────

def test_legacy_arm_is_off_by_default():
    from config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["ai_phase_b_legacy_arm"] is False


def test_legacy_arm_restores_the_old_band():
    """Rollback without a deploy — and it really is the old behaviour."""
    cfg = _cfg(ai_phase_b_legacy_arm=True)
    ok, _why = _arm(_rec(pctr=-45.0, pctr_slow=-45.0), cfg)   # exh 55, in band
    assert ok is True
    # And the square that the shared arm accepts is refused again.
    back, why = _arm(_rec(), cfg)
    assert back is False and why == "phase_b_exh_band"
