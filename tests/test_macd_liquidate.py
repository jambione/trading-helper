"""MACD withdraws the entry thesis → liquidate the position.

The entry claim is "fast is above slow AND the gap is opening". The operator
asked for both ways that expires to flatten the trade:

  NEGATIVE  the gap is at or below zero — the lines crossed, the claim is
            simply false now.
  CURLED    the gap is still positive but has turned from rising to falling.

CURLED is an EDGE, not a level. `macd_gap_falling` on its own would fire on a
position that was never opening to begin with — a name admitted mid-narrowing
would be liquidated on the first tick it was ever looked at. So the turn only
counts once THIS position has been seen rising, which is what macd_dir_seen
records.

Provenance is required rather than preferred, and that is most of what these
tests are about. macd_src flips per ticker mid-session; a curl computed on the
Alpaca REST fallback is a curl in older bars. With no proof the reading is
live, this must not fire — the ordinary trail still protects the position, and
refusing to act on an unprovable indicator is the same rule the entry gate
applies as macd_src_unknown.
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


_RT = {"macd_src": "realtime"}


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


def test_a_curl_after_rising_liquidates(monkeypatch):
    _on(monkeypatch)
    pos = {}
    _wire(monkeypatch, macd_gap=0.05, macd_gap_rising=True, **_RT)
    assert cp.macd_thesis_broken("AAA", pos)[0] is False, "still opening"
    assert pos["macd_dir_seen"] == "up"

    _wire(monkeypatch, macd_gap=0.04, macd_gap_falling=True, **_RT)
    fire, why = cp.macd_thesis_broken("AAA", pos)
    assert fire and why == "macd_curl"


def test_falling_without_ever_rising_does_not_liquidate(monkeypatch):
    """The edge is the point. A name admitted mid-narrowing would otherwise be
    flattened on the first tick it was ever looked at."""
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


# ── provenance: absence is not a pass ────────────────────────────────────

def test_the_rest_fallback_cannot_liquidate(monkeypatch):
    """A curl drawn on Alpaca bars is a curl in older bars."""
    _on(monkeypatch)
    _wire(monkeypatch, macd_gap=-0.05, macd_src="alpaca")
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
    body = src[i:i + 1400]
    assert "close_out(ticker)" in body, "liquidate means flatten"
    assert "cancel_open_orders(ticker)" in body, "pull resting legs first"
    assert "soft_exit_held_back(pos, now)" in body, "min-hold applies"
    # And it only runs on a position that actually exists.
    assert 'pos.get("entry_confirmed")' in src[i - 400:i]
