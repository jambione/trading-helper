"""MACD withdraws the entry thesis → liquidate the position.

The entry claim is "fast is above slow AND the gap is opening". Two hard
sells, neither waits on min-hold:

  NEGATIVE          the gap is at or below zero — the lines crossed, the
                    claim is simply false now. Direction does not matter.
  THIN AND CLOSING  still positive, falling, and macd_sep_ratio under 1.0.

A wide positive gap that is merely falling is not a flatten. The trail owns
that. Provenance is required rather than preferred. macd_src flips per ticker
mid-session; a reading on the Alpaca REST fallback is older bars. With no
proof the reading is live, this must not fire — the ordinary trail still
protects the position, and refusing to act on an unprovable indicator is the
same rule the entry gate applies as macd_src_unknown.
"""
import sys
import types
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

cp = pytest.importorskip("ai_positions")


def _wire(monkeypatch, **sig):
    """Stand in for the engine indicator map."""
    stub = types.SimpleNamespace(
        _engine_indicator_map=lambda: {"AAA": dict(sig)} if sig else {})
    monkeypatch.setitem(sys.modules, "ai_entry_watch", stub)


def _on(monkeypatch, **over):
    cfg = {"ai_exit_macd_liquidate": True}
    cfg.update(over)
    monkeypatch.setattr(cp, "_cfg_flag",
                        lambda k, d=False: bool(cfg.get(k, d)))
    # These tests are about the RULE, so they run at one reading per verdict.
    # The confirmation streak that the live config uses (3 ticks, ~9s of
    # agreement, so a forming-bar flicker cannot liquidate) has its own file:
    # tests/test_churn_guards.py.
    monkeypatch.setattr(cp, "_cfg_all", lambda: {
        "ai_exit_macd_confirm_ticks": 1,
        "ai_exit_macd_hard_sell_sep": 1.0,
        "ai_watch_macd_max_age_sec": 30.0,
    })


_RT = {"macd_src": "realtime", "macd_age_sec": 0.3}


# ── the two triggers ─────────────────────────────────────────────────────

def test_a_negative_gap_liquidates(monkeypatch):
    _on(monkeypatch)
    _wire(monkeypatch, macd_gap=-0.01, **_RT)
    fire, why = cp.macd_thesis_broken("AAA", {})
    assert fire and why == "macd_negative"


def test_a_zero_gap_liquidates(monkeypatch):
    """At the cross, not merely past it."""
    _on(monkeypatch)
    _wire(monkeypatch, macd_gap=0.0, **_RT)
    assert cp.macd_thesis_broken("AAA", {})[1] == "macd_negative"


def test_macd_bull_false_liquidates_even_with_a_positive_gap(monkeypatch):
    """The engine's own verdict outranks a stale gap number."""
    _on(monkeypatch)
    _wire(monkeypatch, macd_gap=0.05, macd_bull=False, **_RT)
    assert cp.macd_thesis_broken("AAA", {})[1] == "macd_negative"


def test_a_wide_gap_that_curls_does_not_liquidate(monkeypatch):
    """Curl is gone. A still-positive gap well clear of 1.0x is the trail's."""
    _on(monkeypatch)
    pos = {}
    _wire(monkeypatch, macd_gap=0.05, macd_sep_ratio=1.7,
          macd_gap_rising=True, **_RT)
    assert cp.macd_thesis_broken("AAA", pos)[0] is False, "still opening"
    _wire(monkeypatch, macd_gap=0.04, macd_sep_ratio=1.7,
          macd_gap_falling=True, **_RT)
    fire, why = cp.macd_thesis_broken("AAA", pos)
    assert fire is False
    assert why != "macd_curl"


def test_falling_without_being_thin_does_not_liquidate(monkeypatch):
    """A name admitted mid-narrowing, still above the 1.0x bar, is left."""
    _on(monkeypatch)
    _wire(monkeypatch, macd_gap=0.04, macd_gap_falling=True, **_RT)
    assert cp.macd_thesis_broken("AAA", {})[0] is False


def test_an_opening_gap_is_left_alone(monkeypatch):
    _on(monkeypatch)
    _wire(monkeypatch, macd_gap=0.05, macd_gap_rising=True, **_RT)
    assert cp.macd_thesis_broken("AAA", {})[0] is False


def test_a_negative_gap_fires_even_with_no_direction_history(monkeypatch):
    """NEGATIVE is a level, not an edge — it needs no prior state."""
    _on(monkeypatch)
    _wire(monkeypatch, macd_gap=-0.02, **_RT)
    assert cp.macd_thesis_broken("AAA", {})[0] is True


def test_a_negative_gap_liquidates_even_while_rising(monkeypatch):
    """No exceptions: an up arrow does not save a crossed gap."""
    _on(monkeypatch)
    _wire(monkeypatch, macd_gap=-0.007, macd_sep_ratio=-0.4,
          macd_gap_rising=True, macd_bull=False, **_RT)
    fire, why = cp.macd_thesis_broken("AAA", {})
    assert fire and why == "macd_negative"


# ── provenance: absence is not a pass ────────────────────────────────────

def test_the_rest_fallback_cannot_liquidate(monkeypatch):
    """A curl drawn on Alpaca bars is a curl in older bars."""
    _on(monkeypatch)
    _wire(monkeypatch, macd_gap=-0.05, macd_src="alpaca")
    assert cp.macd_thesis_broken("AAA", {})[0] is False


def test_stale_realtime_macd_cannot_liquidate(monkeypatch):
    """A 70s-old 'realtime' bar is not a live flatten."""
    _on(monkeypatch)
    monkeypatch.setattr(cp, "_cfg_all",
                        lambda: {"ai_watch_macd_max_age_sec": 30.0})
    _wire(monkeypatch, macd_gap=-0.05, macd_src="realtime", macd_age_sec=71.0)
    assert cp.macd_thesis_broken("AAA", {})[0] is False


def test_unknown_provenance_cannot_liquidate(monkeypatch):
    _on(monkeypatch)
    _wire(monkeypatch, macd_gap=-0.05)
    assert cp.macd_thesis_broken("AAA", {})[0] is False


def test_a_missing_gap_cannot_liquidate(monkeypatch):
    _on(monkeypatch)
    _wire(monkeypatch, macd_gap=None, **_RT)
    assert cp.macd_thesis_broken("AAA", {})[0] is False


def test_an_unknown_symbol_cannot_liquidate(monkeypatch):
    _on(monkeypatch)
    _wire(monkeypatch, macd_gap=-0.05, **_RT)
    assert cp.macd_thesis_broken("ZZZ", {})[0] is False


def test_an_engine_that_raises_cannot_liquidate(monkeypatch):
    """A broken import must not flatten the book."""
    _on(monkeypatch)
    boom = types.SimpleNamespace(
        _engine_indicator_map=lambda: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setitem(sys.modules, "ai_entry_watch", boom)
    assert cp.macd_thesis_broken("AAA", {})[0] is False


# ── the switch ───────────────────────────────────────────────────────────

def test_off_by_default(monkeypatch):
    """It closes positions, so it must be opt-in."""
    from config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["ai_exit_macd_liquidate"] is False
    assert DEFAULT_CONFIG["ai_exit_macd_liquidate_ignore_hold"] is False


def test_disabled_means_disabled(monkeypatch):
    monkeypatch.setattr(cp, "_cfg_flag", lambda k, d=False: False)
    _wire(monkeypatch, macd_gap=-0.05, **_RT)
    assert cp.macd_thesis_broken("AAA", {})[0] is False


def test_the_knobs_reach_the_live_config():
    """Pinned because a knob that load_config() does not pass through is a
    knob that is silently dead — the exact shape of the 30s tape ceiling that
    never took effect."""
    import config
    c = config.load_config()
    assert "ai_exit_macd_liquidate" in c
    assert "ai_exit_macd_liquidate_ignore_hold" in c


# ── where it runs ────────────────────────────────────────────────────────

def test_it_is_wired_into_the_positions_loop_not_the_shelf_tick():
    """Source-pinned. The 0.25s shelf tick's contract is no broker round trip
    unless the stop fires; an indicator lookup every tick breaks it, and an
    earlier attempt that put it there was rightly removed."""
    src = (_ROOT / "ai_positions.py").read_text(encoding="utf-8")
    assert "macd_thesis_broken(ticker, pos)" in src
    i = src.index("_macd_fire, _macd_why = macd_thesis_broken")
    body = src[i:src.index("# Day-scalp dead trade", i)]
    assert "close_out(ticker)" in body, "liquidate means flatten"
    assert "cancel_open_orders(ticker)" in body, "pull resting legs first"
    assert "soft_exit_held_back" not in body, "hard sells skip min-hold"
    assert "_note_min_hold" not in body
    # And it only runs on a position that actually exists.
    assert 'pos.get("entry_confirmed")' in src[i - 400:i]


# ── HARD SELL: thin separation AND closing ─────────────────────────────────
#
# The operator's rule, read off the book: the number in parentheses is
# macd_sep_ratio — the gap measured in standard deviations of its own
# histogram. Under 1.0 the separation is inside the noise the entry was meant
# to clear, and falling on top of that is a move both small and shrinking.
# IBRX showed +0.003 (0.8x) closing while still nominally bullish.
#
# A LEVEL plus a direction: it does not wait to have been seen rising, and
# it outranks min-hold. Rising, even when thin, is left alone.

def test_thin_and_closing_is_a_hard_sell(monkeypatch):
    _on(monkeypatch)
    _wire(monkeypatch, macd_gap=0.003, macd_sep_ratio=0.8,
          macd_gap_falling=True, **_RT)
    fire, why = cp.macd_thesis_broken("AAA", {})
    assert fire and why == "macd_thin_and_closing"


def test_thin_but_still_opening_is_left_alone(monkeypatch):
    """IOVA: +0.001 (0.3x) RISING. Thin, but going the right way."""
    _on(monkeypatch)
    _wire(monkeypatch, macd_gap=0.001, macd_sep_ratio=0.3,
          macd_gap_rising=True, **_RT)
    assert cp.macd_thesis_broken("AAA", {})[0] is False


def test_wide_and_closing_is_not_a_flatten(monkeypatch):
    """SBET: +0.006 (1.7x) falling. Closing, but still well clear of noise.
    Curl is gone; the trail owns this."""
    _on(monkeypatch)
    _wire(monkeypatch, macd_gap=0.006, macd_sep_ratio=1.7,
          macd_gap_falling=True, **_RT)
    fire, why = cp.macd_thesis_broken("AAA", {})
    assert fire is False
    assert why != "macd_curl"


def test_the_threshold_is_configurable(monkeypatch):
    cfg = {"ai_exit_macd_liquidate": True}
    monkeypatch.setattr(cp, "_cfg_flag", lambda k, d=False: bool(cfg.get(k, d)))
    monkeypatch.setattr(cp, "_cfg_all",
                        lambda: {"ai_exit_macd_hard_sell_sep": 0.5})
    _wire(monkeypatch, macd_gap=0.003, macd_sep_ratio=0.8,
          macd_gap_falling=True, **_RT)
    # 0.8 is no longer under the bar, so it is not the hard sell.
    assert cp.macd_thesis_broken("AAA", {})[1] != "macd_thin_and_closing"


def test_a_missing_ratio_cannot_hard_sell(monkeypatch):
    """Absence is not a pass — an unmeasurable separation is not a thin one."""
    _on(monkeypatch)
    _wire(monkeypatch, macd_gap=0.003, macd_gap_falling=True, **_RT)
    assert cp.macd_thesis_broken("AAA", {})[1] != "macd_thin_and_closing"


def test_both_hard_sells_skip_min_hold_and_curl_is_gone():
    """Source-pinned: nothing in the flatten block consults the clock, and
    macd_curl is no longer a reason the thesis function can return."""
    src = (_ROOT / "ai_positions.py").read_text(encoding="utf-8")
    i = src.index("_macd_fire, _macd_why = macd_thesis_broken")
    body = src[i:src.index("# Day-scalp dead trade", i)]
    assert "soft_exit_held_back" not in body
    assert "_note_min_hold" not in body
    assert 'return True, "macd_curl"' not in src
    assert "macd_negative" in src
    assert "macd_thin_and_closing" in src


def test_the_threshold_reaches_the_live_config():
    import config
    assert "ai_exit_macd_hard_sell_sep" in config.load_config()


# ── entry and exit must not overlap ─────────────────────────────────────────
#
# 2026-08-28, live session. Entry admitted at macd_sep_mult >= 0.8 while the
# hard sell fired at macd_sep_ratio < 1.0, so anything bought in the 0.8-1.0
# band was born inside the liquidation zone and flattened the first time its
# gap ticked down. Fourteen hard sells that morning, six of them held under
# thirty seconds:
#
#   09:39:52  PATH  macd_thin_and_closing  age_min=0.3   18 seconds
#   10:20:45  GAP   macd_negative          age_min=-0.0  instant
#   10:25:09  PURR  macd_thin_and_closing  age_min=0.1   6 seconds
#
# Neither rule was wrong on its own — the seam between them was. min-hold at
# 300s had been deferring the sells five minutes and disguising them as
# ordinary trail exits; 30s plus a min-hold-exempt hard sell made it visible
# in a single session.

def test_entry_separation_clears_the_hard_sell_line():
    """A fresh position must not already satisfy its own liquidation rule."""
    import config
    c = config.load_config()
    entry = float(c["macd_sep_mult"])
    hard = float(c["ai_exit_macd_hard_sell_sep"])
    assert entry > hard, (
        f"entry admits at sep >= {entry} but the hard sell fires below "
        f"{hard}: anything bought in between is liquidated on the next "
        f"downtick")


def test_a_position_bought_at_the_entry_bar_is_not_instantly_sellable(monkeypatch):
    """End to end through the real rule, at the exact admission threshold."""
    import config
    c = config.load_config()
    _on(monkeypatch)
    _wire(monkeypatch, macd_gap=0.02,
          macd_sep_ratio=float(c["macd_sep_mult"]),
          macd_gap_falling=True, **_RT)
    fire, why = cp.macd_thesis_broken("AAA", {})
    assert why != "macd_thin_and_closing", (
        "a name admitted at the entry bar is already inside the hard sell")
