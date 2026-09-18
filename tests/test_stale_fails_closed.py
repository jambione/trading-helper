"""Staleness guards must fail CLOSED — an undatable price is not a fresh one.

``live_print`` states the contract in its own docstring: "Age None means the
desk has a number but cannot prove it is live — callers must not treat that as
fresh." Two consumers in ai_positions treated it as fresh anyway.

That was not a corner case. Sampled 2026-09-18 on the mini, ``price_age_sec``
was absent from **20,000 of 20,000** watched shadow rows while ``price_src``
was present on all of them — the source label survives the hop and the clock
does not, so the unprovable branch was the only branch these guards ever took.

It matters in both directions, which is why "it would just exit early" is not a
defence:
  • a phantom LOW is a flatten trigger — any print at or below the shelf
    market-closes the position;
  • a phantom HIGH inflates MFE, so the shelf ratchets up off a price that
    never traded and then flattens against the real market.

Dropping an undatable print costs nothing: the broker mark still feeds the
tick, the 1s book tick still owns the REST fallback, and the blind-book flatten
still owns total data loss — and that one already fails closed via
``quote_is_live``.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import ai_positions as cp


@pytest.fixture
def _tape(monkeypatch):
    """Install a fake live_print returning (price, age)."""
    import ai_entry_watch as ew

    def _set(price, age):
        monkeypatch.setattr(ew, "live_print",
                            lambda _s: (price, age) if price else None)
    return _set


@pytest.fixture(autouse=True)
def _stale_window(monkeypatch):
    monkeypatch.setattr(cp, "_cfg_all",
                        lambda: {"ai_stale_data_max_age_sec": 15.0})


# ── _fresh_tape_px ────────────────────────────────────────────────────────────

def test_fresh_tape_px_takes_a_young_dated_print(_tape):
    _tape(10.00, 2.0)
    assert cp._fresh_tape_px("VEEA") == 10.00


def test_fresh_tape_px_drops_an_old_print(_tape):
    _tape(10.00, 99.0)
    assert cp._fresh_tape_px("VEEA") is None


def test_fresh_tape_px_drops_an_undated_print(_tape):
    """The 100%-of-rows case."""
    _tape(10.00, None)
    assert cp._fresh_tape_px("VEEA") is None


def test_fresh_tape_px_drops_an_unparseable_age(_tape):
    """An age that will not parse is not a young age.

    The except branch returned the price, which undid the None check three
    lines above it for every malformed clock.
    """
    _tape(10.00, "not-a-number")
    assert cp._fresh_tape_px("VEEA") is None


# ── _tick_prints ──────────────────────────────────────────────────────────────

def test_tick_prints_uses_a_young_dated_tape(_tape):
    _tape(9.50, 1.0)
    hi, lo = cp._tick_prints("VEEA", {"current": 10.00})
    assert (hi, lo) == (10.00, 9.50)


def test_tick_prints_ignores_an_undated_tape(_tape):
    """The flatten trigger must not come from a price we cannot date."""
    _tape(9.50, None)
    hi, lo = cp._tick_prints("VEEA", {"current": 10.00})
    assert (hi, lo) == (10.00, 10.00), "undated tape leaked into the tick"


def test_tick_prints_ignores_an_unparseable_age(_tape):
    _tape(9.50, "garbage")
    hi, lo = cp._tick_prints("VEEA", {"current": 10.00})
    assert (hi, lo) == (10.00, 10.00)


def test_tick_prints_ignores_an_old_tape(_tape):
    _tape(9.50, 600.0)
    hi, lo = cp._tick_prints("VEEA", {"current": 10.00})
    assert (hi, lo) == (10.00, 10.00)


def test_an_undated_low_cannot_trigger_a_flatten(_tape):
    """A phantom low is a market-close order. It must not come from nowhere."""
    _tape(4.00, None)   # far below the broker mark — would trip any shelf
    _hi, lo = cp._tick_prints("VEEA", {"current": 10.00})
    assert lo == 10.00, "an undatable print became the flatten trigger"


def test_an_undated_high_cannot_inflate_mfe(_tape):
    """A phantom high ratchets the shelf above the real market."""
    _tape(18.00, None)
    hi, _lo = cp._tick_prints("VEEA", {"current": 10.00})
    assert hi == 10.00, "an undatable print became the excursion high"


def test_no_broker_mark_and_an_undated_tape_yields_nothing(_tape):
    """Absence beats a plausible wrong number — the caller skips this pass."""
    _tape(9.50, None)
    assert cp._tick_prints("VEEA", {}) == (None, None)


def test_the_broker_mark_alone_still_works(_tape):
    """Dropping the tape must not blind the tick when the mark is present."""
    _tape(0, None)   # live_print returns None
    hi, lo = cp._tick_prints("VEEA", {"current_price": 7.25})
    assert (hi, lo) == (7.25, 7.25)


# ── The contract these guards implement ───────────────────────────────────────

def test_live_print_still_documents_the_contract():
    """If this docstring changes, the guards above need re-reading."""
    import ai_entry_watch as ew
    doc = (ew.live_print.__doc__ or "")
    assert "must not treat that as fresh" in doc
