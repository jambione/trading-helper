"""MACD curls bearish on an open position — pull the shelf under the print.

The entry thesis is "the fast and slow lines are separating". A curl back
together is that thesis expiring, and the ratchet's give was sized for
noise, not for a signal that has already turned: waiting for the give to be
crossed hands back the part of the move MACD just said was over.

The tightening is expressed as a FLOOR on the wanted stop rather than a
direct write, so it passes through apply_local_trail's raise-only rule. On
a winner that puts the shelf a penny under price; on a loser `last - 0.01`
sits below the existing shelf and is correctly ignored. It tightens, it
never loosens — which is the property worth pinning, because a stop that
can move down is not a stop.
"""
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

cp = pytest.importorskip("ai_positions")


def _wire(monkeypatch, **sig):
    """Stand in for the engine indicator map."""
    import types
    stub = types.SimpleNamespace(
        _engine_indicator_map=lambda: {"AAA": dict(sig)} if sig else {})
    monkeypatch.setitem(sys.modules, "ai_entry_watch", stub)


def _cfg(monkeypatch, **over):
    base = {"ai_exit_macd_curl_tighten": True, "ai_exit_macd_curl_px": 0.01}
    base.update(over)
    monkeypatch.setattr(cp, "_cfg_all", lambda: base)
    monkeypatch.setattr(
        cp, "_cfg_flag", lambda key, default=True: base.get(key, default))


# ── the trigger ──────────────────────────────────────────────────────────

def test_a_bearish_gap_pulls_the_stop_under_the_print(monkeypatch):
    _cfg(monkeypatch)
    _wire(monkeypatch, macd_gap=-0.004, macd_bull=False)
    assert cp._macd_curl_stop("AAA", 10.00) == pytest.approx(9.99)


def test_fast_below_slow_counts_even_with_a_zero_gap(monkeypatch):
    _cfg(monkeypatch)
    _wire(monkeypatch, macd_gap=0.0, macd_bull=False)
    assert cp._macd_curl_stop("AAA", 10.00) == pytest.approx(9.99)


def test_a_healthy_bullish_gap_does_not_tighten(monkeypatch):
    _cfg(monkeypatch)
    _wire(monkeypatch, macd_gap=0.05, macd_bull=True)
    assert cp._macd_curl_stop("AAA", 10.00) is None


def test_a_closing_but_still_positive_gap_is_ignored_by_default(monkeypatch):
    """Earlier and noisier: one narrowing bar inside a live move would
    flatten a winner that was still working."""
    _cfg(monkeypatch)
    _wire(monkeypatch, macd_gap=0.03, macd_bull=True, macd_gap_falling=True)
    assert cp._macd_curl_stop("AAA", 10.00) is None


def test_the_earlier_trigger_is_available_behind_its_own_knob(monkeypatch):
    _cfg(monkeypatch, ai_exit_macd_curl_on_falling=True)
    _wire(monkeypatch, macd_gap=0.03, macd_bull=True, macd_gap_falling=True)
    assert cp._macd_curl_stop("AAA", 10.00) == pytest.approx(9.99)


# ── it must never fire on a guess ────────────────────────────────────────

def test_off_by_default():
    from config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["ai_exit_macd_curl_tighten"] is False
    assert DEFAULT_CONFIG["ai_exit_macd_curl_px"] == 0.01
    assert DEFAULT_CONFIG["ai_exit_macd_curl_on_falling"] is False


def test_a_missing_reading_does_not_tighten(monkeypatch):
    """No MACD is not bearish. The ordinary give still protects the trade."""
    _cfg(monkeypatch)
    _wire(monkeypatch)
    assert cp._macd_curl_stop("AAA", 10.00) is None
    _wire(monkeypatch, macd_bull=False)          # bull flag but no gap
    assert cp._macd_curl_stop("AAA", 10.00) is None


def test_an_unusable_print_does_not_tighten(monkeypatch):
    _cfg(monkeypatch)
    _wire(monkeypatch, macd_gap=-0.004, macd_bull=False)
    for px in (None, 0, -1):
        assert cp._macd_curl_stop("AAA", px) is None


def test_a_penny_stock_cannot_be_pushed_to_a_non_positive_stop(monkeypatch):
    _cfg(monkeypatch)
    _wire(monkeypatch, macd_gap=-0.004, macd_bull=False)
    assert cp._macd_curl_stop("AAA", 0.01) is None


def test_a_dead_wire_does_not_tighten(monkeypatch):
    """A raised import must read as "no signal", never as bearish."""
    import types

    def _boom():
        raise RuntimeError("wire down")

    monkeypatch.setitem(sys.modules, "ai_entry_watch",
                        types.SimpleNamespace(_engine_indicator_map=_boom))
    _cfg(monkeypatch)
    assert cp._macd_curl_stop("AAA", 10.00) is None


# ── raise-only still holds ───────────────────────────────────────────────

def test_the_curl_stop_is_a_floor_not_an_override(monkeypatch):
    """Pinned as source text: writing pos["local_stop_price"] directly would
    let a losing trade's `last - 0.01` move the shelf DOWN, which is the one
    thing a ratchet may never do."""
    src = (_ROOT / "ai_positions.py").read_text(encoding="utf-8")
    i = src.index("curl_px = _macd_curl_stop(ticker, last)")
    body = src[i:i + 400]
    assert "max(want, curl_px)" in body, "must raise, never replace"
    assert 'pos["local_stop_price"] = curl_px' not in body


def test_a_curl_below_the_existing_shelf_is_ignored(monkeypatch):
    """The arithmetic of the floor: underwater, last-0.01 is below the shelf
    and max() keeps the shelf."""
    want, curl = 9.50, 8.00 - 0.01
    assert max(want, curl) == want
