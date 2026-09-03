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


def test_the_default_is_above_every_plausible_reading():
    d = _config.DEFAULT_CONFIG["ai_watch_arm_rvol_sane_max"]
    assert d > 19.5, "must not refuse the plausible 8-20x band"
    assert d < 26.8, "must refuse the broken cluster"
