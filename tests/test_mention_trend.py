"""T1.3 — mention trend arrow: the four cases, sample floor, engine source."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "momentum-monitor"))

from momentum_signal import (  # noqa: E402
    DEFAULTS,
    Feed,
    mention_trend,
    momentum_table,
    push_history,
)
from symbol_history import SymbolHistory  # noqa: E402

T0 = 1753449600.0
CFG = {**DEFAULTS, "alert_new": False, "alert_burst": False,
       "alert_buy": False, "focus_age_enabled": False,
       "setup_distance_enabled": False}


# ── the four arrow cases ─────────────────────────────────────────────────────

def test_strictly_increasing_series_rises():
    assert mention_trend([1, 2, 3, 4, 5, 6, 7, 8]) in ("↑", "↑↑")


def test_sharply_accelerating_series_double_rises():
    assert mention_trend([1, 1, 1, 1, 8, 9, 10, 11]) == "↑↑"


def test_mild_rise_is_a_single_arrow():
    # recent mean / older mean = 1.6 -> over rise (1.5), under strong (2.25)
    assert mention_trend([5, 5, 5, 5, 8, 8, 8, 8]) == "↑"


def test_flat_series_is_sideways():
    assert mention_trend([4, 4, 4, 4, 4, 4, 4, 4]) == "→"


def test_decreasing_series_falls():
    assert mention_trend([10, 9, 8, 7, 3, 2, 1, 1]) == "↓"


def test_mild_drift_stays_sideways():
    # ratio 0.9 -> above fall (0.6), below rise (1.5)
    assert mention_trend([10, 10, 10, 10, 9, 9, 9, 9]) == "→"


# ── sample floor ─────────────────────────────────────────────────────────────

def test_below_min_samples_returns_empty():
    """An arrow off two data points is noise, not information."""
    assert mention_trend([1, 5], min_samples=8) == ""
    assert mention_trend([1, 2, 3, 4, 5, 6, 7], min_samples=8) == ""


def test_exactly_min_samples_emits():
    assert mention_trend([1, 1, 1, 1, 9, 9, 9, 9], min_samples=8) != ""


def test_empty_series_returns_empty():
    assert mention_trend([]) == ""
    assert mention_trend(None) == ""


def test_min_samples_is_configurable():
    assert mention_trend([1, 1, 9, 9], min_samples=4) != ""
    assert mention_trend([1, 1, 9, 9], min_samples=8) == ""


# ── thresholds ───────────────────────────────────────────────────────────────

def test_rise_and_fall_thresholds_are_honored():
    series = [10, 10, 10, 10, 13, 13, 13, 13]     # ratio 1.3
    assert mention_trend(series, rise=1.2, fall=0.6) == "↑"
    assert mention_trend(series, rise=1.5, fall=0.6) == "→"


def test_zero_baseline_coming_alive_reads_as_a_strong_rise():
    assert mention_trend([0, 0, 0, 0, 3, 4, 5, 6]) == "↑↑"


def test_all_zero_series_is_sideways_not_a_rise():
    assert mention_trend([0, 0, 0, 0, 0, 0, 0, 0]) == "→"


# ── engine value ─────────────────────────────────────────────────────────────

def test_engine_velocity_is_appended_as_the_newest_sample():
    """It is a level, not a direction: it contributes an observation."""
    base = [5, 5, 5, 5, 5, 5, 5]
    assert mention_trend(base, None, min_samples=8) == ""        # 7 samples
    assert mention_trend(base, 5, min_samples=8) == "→"          # 8th arrives


def test_engine_velocity_can_flip_the_verdict():
    rising = [1, 1, 1, 1, 2, 2, 2]
    assert mention_trend(rising, 40.0, min_samples=8) == "↑↑"


def test_none_engine_velocity_is_ignored():
    series = [1, 2, 3, 4, 5, 6, 7, 8]
    assert mention_trend(series, None) == mention_trend(series)


def test_non_numeric_engine_velocity_is_ignored():
    series = [1, 2, 3, 4, 5, 6, 7, 8]
    assert mention_trend(series, "junk") == mention_trend(series)


def test_none_entries_in_the_series_are_skipped():
    """Skipped, not zero-filled — and they do not count toward min_samples."""
    assert mention_trend([1, None, 1, 1, 1, 9, 9, None, 9, 9]) == "↑↑"
    # Same list minus one real sample: 7 valid -> below the floor.
    assert mention_trend([1, None, 1, 1, 9, 9, None, 9, 9]) == ""


# ── cell rendering ───────────────────────────────────────────────────────────

def _mentions_cells(rows, hist, **over):
    f = Feed(CFG)
    f.rows = rows
    t = momentum_table(f, T0, 0.5, True, cfg={**CFG, **over}, history=hist)
    return list(list(t.columns)[5].cells)


def _hist_with(sym, field, values):
    h = SymbolHistory(maxlen=120)
    for i, v in enumerate(values):
        h.push(sym, T0 + i * 2.0, **{field: v})
    return h


def test_arrow_is_appended_to_the_mentions_cell():
    hist = _hist_with("AAAA", "mention_window", [1, 1, 1, 1, 8, 9, 10, 11])
    rows = [{"ticker": "AAAA", "mention_window": 11, "mention_count": 47}]
    cell = _mentions_cells(rows, hist)[0]
    assert cell.startswith("11/47 ")
    assert "↑↑" in cell
    assert "green" in cell


def test_falling_arrow_is_red():
    hist = _hist_with("AAAA", "mention_window", [10, 9, 8, 7, 2, 1, 1, 1])
    rows = [{"ticker": "AAAA", "mention_window": 1, "mention_count": 47}]
    cell = _mentions_cells(rows, hist)[0]
    assert "↓" in cell
    assert "red" in cell


def test_flat_arrow_is_dim():
    hist = _hist_with("AAAA", "mention_window", [4] * 8)
    rows = [{"ticker": "AAAA", "mention_window": 4, "mention_count": 47}]
    cell = _mentions_cells(rows, hist)[0]
    assert "→" in cell
    assert "dim" in cell


def test_cell_is_exactly_as_today_below_min_samples():
    hist = _hist_with("AAAA", "mention_window", [1, 2])
    rows = [{"ticker": "AAAA", "mention_window": 12, "mention_count": 47}]
    assert _mentions_cells(rows, hist)[0] == "12/47"


def test_cell_is_exactly_as_today_when_flag_off():
    hist = _hist_with("AAAA", "mention_window", [1, 1, 1, 1, 8, 9, 10, 11])
    rows = [{"ticker": "AAAA", "mention_window": 11, "mention_count": 47}]
    assert _mentions_cells(rows, hist,
                           mention_trend_enabled=False)[0] == "11/47"


def test_no_history_object_renders_as_today():
    rows = [{"ticker": "AAAA", "mention_window": 12, "mention_count": 47}]
    f = Feed(CFG)
    f.rows = rows
    t = momentum_table(f, T0, 0.5, True, cfg=CFG, history=None)
    assert next(iter(list(t.columns)[5].cells)) == "12/47"


def test_a_row_with_no_mentions_gets_no_arrow():
    """"— →" would dress an absence up as a reading."""
    hist = _hist_with("AAAA", "mention_window", [0] * 10)
    rows = [{"ticker": "AAAA", "mention_window": 0, "mention_count": 0}]
    assert _mentions_cells(rows, hist)[0] == "—"


# ── source precedence ────────────────────────────────────────────────────────

def test_engine_velocity_series_wins_over_mention_window_series():
    """Both series are present and point opposite ways; the engine's
    higher-resolution velocity series must decide."""
    h = SymbolHistory(maxlen=120)
    for i in range(10):
        h.push("AAAA", T0 + i * 2.0,
               mention_window=10 - i,        # falling
               mention_velocity=i + 1)       # rising
    rows = [{"ticker": "AAAA", "mention_window": 1, "mention_count": 47,
             "signal_proximity": {"mention_velocity": 11}}]
    cell = _mentions_cells(rows, h)[0]
    assert "↑" in cell
    assert "↓" not in cell


def test_falls_back_to_mention_window_when_engine_series_is_short():
    """The engine only tracks symbols it has picked up, so most rows have
    no velocity samples at all."""
    h = SymbolHistory(maxlen=120)
    for i in range(10):
        h.push("AAAA", T0 + i * 2.0, mention_window=i + 1)   # rising
    rows = [{"ticker": "AAAA", "mention_window": 10, "mention_count": 47}]
    assert "↑" in _mentions_cells(rows, h)[0]


def test_push_history_records_engine_velocity_from_signal_proximity():
    h = SymbolHistory(maxlen=10)
    push_history(h, [{"ticker": "AAAA", "mention_window": 4,
                      "signal_proximity": {"mention_velocity": 7}}], T0)
    assert h.series("AAAA", "mention_velocity") == [7.0]
    assert h.series("AAAA", "mention_window") == [4.0]
