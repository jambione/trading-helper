"""RSI focus column — green long zone (CM RSI below threshold toward 0)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "momentum-monitor"))

from momentum_signal import rsi_focus_trigger, _rsi_focus_cell  # noqa: E402


def test_trigger_in_green_long_zone():
    row = {"signal_proximity": {"cm_rsi": 22.0}}
    rsi, hit = rsi_focus_trigger(row, max_lvl=35.0)
    assert rsi == 22.0
    assert hit is True


def test_no_trigger_above_threshold():
    row = {"signal_proximity": {"cm_rsi": 48.0}}
    rsi, hit = rsi_focus_trigger(row, max_lvl=35.0)
    assert rsi == 48.0
    assert hit is False


def test_boundary_35_not_triggered():
    # Zone is [0, max) — 35 is the band edge, not the green long floor
    row = {"signal_proximity": {"cm_rsi": 35.0}}
    _, hit = rsi_focus_trigger(row, max_lvl=35.0)
    assert hit is False


def test_zero_is_triggered():
    row = {"signal_proximity": {"cm_rsi": 0.0}}
    _, hit = rsi_focus_trigger(row, max_lvl=35.0)
    assert hit is True


def test_falls_back_to_classic_rsi():
    row = {"signal_proximity": {"rsi": 12.5}}
    rsi, hit = rsi_focus_trigger(row)
    assert rsi == 12.5
    assert hit is True


def test_prefers_cm_rsi_over_rsi():
    row = {"signal_proximity": {"cm_rsi": 10.0, "rsi": 80.0}}
    rsi, hit = rsi_focus_trigger(row)
    assert rsi == 10.0
    assert hit is True


def test_missing_proximity():
    rsi, hit = rsi_focus_trigger({})
    assert rsi is None
    assert hit is False


def test_cell_markup_focus():
    cell = _rsi_focus_cell({"signal_proximity": {"cm_rsi": 8.0}})
    assert "FOCUS" in cell
    assert "8" in cell


def test_cell_markup_idle():
    cell = _rsi_focus_cell({"signal_proximity": {"cm_rsi": 55.0}})
    assert "FOCUS" not in cell
    assert "55" in cell
