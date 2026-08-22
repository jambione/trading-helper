"""Capture ratio: realized R over the move that was actually available.

The outcome row's own ``mfe_r`` spans only the hold, so a shelf that exits
at 53 seconds books a small MFE and the ledger reads a modest win. TEM
2026-08-20 took +0.040 R with 1.40 R still ahead and looked fine. This
metric exists to make that visible, so it must not be flattered by the
same blind spots.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

eod = pytest.importorskip("eod")
bars = pytest.importorskip("bars")


@pytest.fixture
def fake_bars(monkeypatch):
    """(stamps, highs) keyed by symbol; entry_time is always 1000."""
    store = {}

    def _fetch_hl(sym, day, feed="sip"):
        return store.get(str(sym).upper(), (None, None))

    monkeypatch.setattr(bars, "fetch_hl", _fetch_hl)
    monkeypatch.setattr(eod.bars, "fetch_hl", _fetch_hl)
    monkeypatch.setattr(eod.bars, "day_of", lambda ts: "2026-08-20")
    return store


def _trade(sym="AAA", r=0.04, entry=10.0, stop=9.5, ts=1000.0):
    return {"symbol": sym, "realized_r_multiple": r, "entry_price": entry,
            "stop_price": stop, "entry_time": ts}


def test_capture_uses_the_move_after_entry_not_the_hold(fake_bars):
    """1R risk = 0.50. High of 11.0 is +2.0R available; we took +0.04R."""
    fake_bars["AAA"] = ([1000.0, 1060.0, 1120.0], [10.1, 11.0, 10.2])
    s = eod.capture_stats([_trade(r=0.04)])
    assert s["n"] == 1
    assert s["median_available_r"] == pytest.approx(2.0)
    assert s["median_capture"] == pytest.approx(0.02)
    assert s["n_ge_1r_available"] == 1


def test_capture_is_negative_when_a_favorable_move_became_a_loss(fake_bars):
    """The headline case: the name went up, the book came back down."""
    fake_bars["AAA"] = ([1000.0, 1060.0], [10.5, 10.6])
    s = eod.capture_stats([_trade(r=-0.06)])
    assert s["median_capture"] < 0


def test_bars_before_entry_are_ignored(fake_bars):
    """A high printed before the fill was never ours to take."""
    fake_bars["AAA"] = ([500.0, 1000.0, 1060.0], [99.0, 10.1, 10.2])
    s = eod.capture_stats([_trade()])
    assert s["median_available_r"] == pytest.approx(0.4)  # 10.2, not 99.0


def test_never_favorable_is_excluded_not_counted_as_zero(fake_bars):
    """No denominator. Counting it as 0% would drag the median upward."""
    fake_bars["AAA"] = ([1000.0, 1060.0], [10.0, 9.9])
    assert eod.capture_stats([_trade(r=-0.5)])["n"] == 0


def test_missing_bars_are_skipped_not_guessed(fake_bars):
    assert eod.capture_stats([_trade(sym="NOPE")])["n"] == 0


def test_non_positive_risk_is_skipped(fake_bars):
    fake_bars["AAA"] = ([1000.0, 1060.0], [10.1, 11.0])
    assert eod.capture_stats([_trade(entry=10.0, stop=10.0)])["n"] == 0
    assert eod.capture_stats([_trade(entry=10.0, stop=11.0)])["n"] == 0


def test_empty_input_reports_none_not_zero(fake_bars):
    s = eod.capture_stats([])
    assert s["n"] == 0
    assert s["median_capture"] is None
    assert s["median_available_r"] is None


def test_metric_uses_the_desks_own_feed(monkeypatch):
    """SIP would price a high the IEX-driven shelf could never reach."""
    seen = {}

    def _fetch_hl(sym, day, feed="sip"):
        seen["feed"] = feed
        return ([1000.0, 1060.0], [10.1, 11.0])

    monkeypatch.setattr(eod.bars, "fetch_hl", _fetch_hl)
    monkeypatch.setattr(eod.bars, "day_of", lambda ts: "2026-08-20")
    eod.capture_stats([_trade()], feed="iex")
    assert seen["feed"] == "iex"


def test_fetch_hl_has_its_own_cache_and_does_not_disturb_fetch():
    """fetch()'s two-tuple contract has callers; widening it would break them."""
    assert hasattr(bars, "fetch_hl")
    assert bars._HL_CACHE is not bars._CACHE
