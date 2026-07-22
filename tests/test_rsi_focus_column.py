"""FOCUS column — CM RSI green-long AND both %R lines deep OS toward -100."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "momentum-monitor"))

from momentum_signal import (  # noqa: E402
    rsi_focus_trigger,
    rsi_focus_empty_reason,
    _rsi_focus_cell,
    _rsi_leg_ok,
    _pctr_leg_ok,
)


def _row(**sp):
    return {"signal_proximity": sp}


def test_focus_requires_both_legs():
    # RSI in band + engine deep-OS flag
    row = _row(cm_rsi=22.0, pctr_deep_os=True)
    rsi, hit = rsi_focus_trigger(row)
    assert rsi == 22.0
    assert hit is True


def test_rsi_only_not_enough():
    row = _row(cm_rsi=8.0, pctr_deep_os=False)
    rsi, hit = rsi_focus_trigger(row)
    assert rsi == 8.0
    assert hit is False


def test_pctr_only_not_enough():
    # deep OS true but RSI high
    row = _row(cm_rsi=48.0, pctr_deep_os=True)
    _, hit = rsi_focus_trigger(row)
    assert hit is False


def test_pctr_leg_from_values_and_falling():
    row = _row(
        pctr=-88.0, pctr_slow=-92.0,
        pctr_falling=True, pctr_slow_falling=True,
    )
    assert _pctr_leg_ok(row) is True


def test_pctr_leg_in_band_but_not_falling():
    row = _row(
        pctr=-88.0, pctr_slow=-92.0,
        pctr_falling=False, pctr_slow_falling=True,
    )
    assert _pctr_leg_ok(row) is False


def test_pctr_leg_outside_band():
    row = _row(
        pctr=-60.0, pctr_slow=-92.0,
        pctr_falling=True, pctr_slow_falling=True,
    )
    assert _pctr_leg_ok(row) is False


def test_pctr_deep_os_flag_wins():
    # Flag true even if values look wrong (engine is source of truth)
    row = _row(pctr_deep_os=True, pctr=-10.0, pctr_slow=-10.0)
    assert _pctr_leg_ok(row) is True


def test_combined_from_values():
    row = _row(
        cm_rsi=12.0,
        pctr=-80.0, pctr_slow=-85.0,
        pctr_falling=True, pctr_slow_falling=True,
    )
    _, hit = rsi_focus_trigger(row)
    assert hit is True


def test_rsi_leg_band():
    assert _rsi_leg_ok(_row(cm_rsi=0.0)) is True
    assert _rsi_leg_ok(_row(cm_rsi=34.9)) is True
    assert _rsi_leg_ok(_row(cm_rsi=35.0)) is False
    assert _rsi_leg_ok(_row(cm_rsi=48.0)) is False


def test_boundary_35_not_rsi_leg():
    row = _row(cm_rsi=35.0, pctr_deep_os=True)
    _, hit = rsi_focus_trigger(row)
    assert hit is False


def test_missing_proximity():
    rsi, hit = rsi_focus_trigger({})
    assert rsi is None
    assert hit is False


def test_empty_reason_untracked():
    assert rsi_focus_empty_reason({}) == "untracked"
    assert rsi_focus_empty_reason({"signal_proximity": None}) == "untracked"


def test_empty_reason_pending():
    row = _row(bars_fetched=False, cm_rsi=None)
    assert rsi_focus_empty_reason(row) == "pending"


def test_cell_markup_focus():
    cell = _rsi_focus_cell(_row(cm_rsi=8.0, pctr_deep_os=True))
    assert "FOCUS" in cell
    assert "8" in cell


def test_cell_markup_idle_rsi_only():
    cell = _rsi_focus_cell(_row(cm_rsi=8.0, pctr_deep_os=False))
    assert "FOCUS" not in cell
    assert "8" in cell


def test_cell_markup_untracked():
    cell = _rsi_focus_cell({})
    assert "—" in cell
    assert "…" not in cell


def test_cell_markup_pending_bars():
    cell = _rsi_focus_cell(_row(bars_fetched=False))
    assert "…" in cell
