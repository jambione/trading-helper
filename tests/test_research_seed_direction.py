"""Direction is decided once, by ai_watch_require_uptrend — not twice.

The research seed carried its own hardcoded sign test: any name red on the day
was dropped before the inclusion gate ever saw it. Because it was code rather
than config, it survived the operator turning every day-change floor off on
2026-08-28 (open_seed_min_pct 5.0 -> 0, min_pct_change 50 -> 0,
trending_min_pct_change 15 -> 8).

That afternoon's research panel, six of seven negative:

    PURR -5.66%   ASST -4.63%   FIG -3.41%
    SRPT -2.18%   BULL -1.46%   PATH -0.14%   GAP +13.04%

All six were dropped at the seed, leaving one research candidate on a book of
three. Momentum and trending are momentum sources where the sign is part of
the signal; research is a THESIS list — "Q2 EPS $1.38 vs est, guidance raised"
does not stop being a thesis because the stock is red today.
"""
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import ai_entry_watch as ew  # noqa: E402


def _research_block() -> str:
    import inspect
    src = inspect.getsource(ew.desk_candidate_rows)
    i = src.index("seed_research")
    return src[i:i + 4200]


def test_the_research_seed_has_no_sign_test_of_its_own():
    """The regression this file exists for. A `pct <= 0: continue` here is a
    direction rule that config cannot see or switch off."""
    block = _research_block()
    bad = [l.strip() for l in block.split("\n")
           if re.search(r"pct_f\s*(<=|<)\s*0", l) and not l.strip().startswith("#")]
    assert not bad, f"research seed is filtering on direction: {bad}"


def test_direction_still_has_exactly_one_owner():
    """Removing the seed test must not remove direction control — it moves it
    to the single knob that is meant to hold it."""
    import config
    assert "ai_watch_require_uptrend" in config.DEFAULT_CONFIG
    assert "ai_watch_require_uptrend" in config.load_config()


def test_the_price_cap_survived_the_edit():
    """The sign test sat between the price cap and the volume read; a careless
    deletion would take one of its neighbours with it."""
    block = _research_block()
    assert "_price_under_cap(px_src, max_price)" in block
    assert "pct_f = _pct_change_value(pct_src)" in block, (
        "pct_f is still read — it feeds the row, it just no longer refuses it")


def test_a_red_research_name_is_no_longer_dropped_at_the_seed():
    """Behavioural: the seed emits the row and lets the gate decide."""
    block = _research_block()
    i = block.index("pct_f = _pct_change_value(pct_src)")
    after = block[i:i + 700]
    assert "continue" not in after.split("dvol")[0], (
        "something still short-circuits between the pct read and the row")
