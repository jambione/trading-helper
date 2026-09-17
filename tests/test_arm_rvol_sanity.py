"""An RVOL the feed cannot have produced is not evidence of anything.

The 2026-09-03 audit of 522 at_last entries found 24 taken at rvol >= 8:

    8.2, 8.4, 14.1, 15.7, 19.5, 26.8, 30.4, 50.1, 62.6, 71.7, 103.5,
    117.0, 235.5, 882.2, 1008.4, 1012.2, 1024.9, 1044.9, 1045.8, 1048.0,
    1070.1, 1097.2, 1102.4, 1144.6

The top of that list clusters around 1000, which is an arithmetic fault
rather than a tape. The nineteen readings at 20x and above averaged -0.236R
against -0.035R for the book as a whole and cost -4.48R; the five plausible
8-20 readings are a different question and are deliberately left alone.

This is a credibility bound, not a heat ceiling.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import ai_entry_watch as ew  # noqa: E402
import config as _config  # noqa: E402


def _cfg(**over):
    c = {"ai_watch_arm_rvol_sane_max": 25.0}
    c.update(over)
    return c


def _rec(rvol):
    return {"symbol": "X", "rvol": rvol}


def _armed(rvol, cfg=None):
    """Just the sanity clause — the gate it guards is exercised elsewhere."""
    cfg = cfg if cfg is not None else _cfg()
    try:
        cap = float(cfg.get("ai_watch_arm_rvol_sane_max", 0.0) or 0.0)
    except (TypeError, ValueError):
        cap = 0.0
    rv = ew._arm_rvol(_rec(rvol))
    return not (cap > 0 and rv is not None and rv > cap)


def test_the_thousand_x_readings_are_refused():
    for rv in (882.2, 1008.4, 1044.9, 1144.6):
        assert _armed(rv) is False, rv


def test_a_plausible_hot_name_still_arms():
    """8-20x is a real tape. This clause has no opinion about it."""
    for rv in (8.2, 14.1, 19.5):
        assert _armed(rv) is True, rv


def test_an_ordinary_reading_is_untouched():
    for rv in (0.43, 1.01, 2.8, 4.6):
        assert _armed(rv) is True, rv


def test_a_missing_reading_abstains_rather_than_refusing():
    """Absent is not implausible: _arm_rvol returns None and the clause
    must not turn that into a veto, or every name without an RVOL stops
    arming."""
    assert ew._arm_rvol({"symbol": "X"}) is None
    assert _armed(None) is True


def test_zero_disables_the_bound():
    assert _armed(1044.9, _cfg(ai_watch_arm_rvol_sane_max=0)) is True


def test_the_gate_is_wired_into_should_arm_buy():
    src = (_ROOT / "ai_entry_watch.py").read_text(encoding="utf-8")
    i = src.index("def should_arm_buy")
    body = src[i:src.index("\ndef ", i + 10)]
    assert "ai_watch_arm_rvol_sane_max" in body
    assert 'return False, "rvol_implausible"' in body


def test_the_refusal_has_a_label_that_does_not_say_too_hot():
    label = ew.format_blocker("rvol_implausible")
    assert label and "hot" not in label.lower()


def test_the_default_disables_the_bound():
    """Missing bot_config key must not revive rvol_implausible (was 25)."""
    d = _config.DEFAULT_CONFIG["ai_watch_arm_rvol_sane_max"]
    assert float(d or 0) <= 0.0


def test_should_arm_buy_respects_sane_max_zero_and_twenty_five():
    """Integration: sane_max=0 never rvol_implausible; =25 still can."""
    from tests.test_ai_entry_watch import _armable_rec, _last_cfg

    rec = _armable_rec()
    rec["source"] = "momentum"
    rec["structure"]["zone_kind"] = "at_last"
    rec["structure"]["synthetic"] = True
    rec["structure"]["reward_risk"] = 0.6
    rec["rvol"] = 1044.9
    rec["indicator"]["pctr"] = -30.0
    rec["indicator"]["pctr_rising"] = True
    rec["indicator"]["pctr_falling"] = False

    cfg0 = _last_cfg(
        ai_watch_arm_rvol_sane_max=0.0,
        ai_watch_arm_require_cm_rsi=False,
        ai_watch_soft_ob_enabled=False,
        ai_watch_mistimed_heat_enabled=False,
        ai_watch_exhaustion_heat_min_pct=0.0,
    )
    ok, why = ew.should_arm_buy(rec, ask=32.0, bid=31.9, cfg=cfg0)
    assert ok is True, why
    assert why != "rvol_implausible"

    cfg25 = dict(cfg0, ai_watch_arm_rvol_sane_max=25.0)
    ok, why = ew.should_arm_buy(rec, ask=32.0, bid=31.9, cfg=cfg25)
    assert ok is False and why == "rvol_implausible"
