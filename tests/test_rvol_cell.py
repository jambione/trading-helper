"""T2.2 — RVOL column: source precedence, thresholds, missing data, layout."""
import os
import sys

from conftest import column_cells  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "momentum-monitor"))

from momentum_signal import (  # noqa: E402
    DEFAULTS,
    MOMENTUM_COLUMNS,
    Feed,
    _rvol_cell,
    momentum_columns,
    momentum_table,
    push_history,
    row_rvol,
)
from symbol_history import SymbolHistory  # noqa: E402

T0 = 1753449600.0
CFG = {**DEFAULTS, "alert_new": False, "alert_burst": False,
       "alert_buy": False, "focus_age_enabled": False,
       "setup_distance_enabled": False, "mention_trend_enabled": False}


# ── source precedence ────────────────────────────────────────────────────────

def test_funnel_value_wins_over_the_top_level_value():
    row = {"ticker": "AAAA", "rvol": 1.2, "funnel": {"rvol": 8.2}}
    assert row_rvol(row) == 8.2


def test_falls_back_to_the_top_level_value():
    assert row_rvol({"ticker": "AAAA", "rvol": 1.4}) == 1.4


def test_funnel_present_but_without_rvol_falls_back():
    row = {"ticker": "AAAA", "rvol": 1.4, "funnel": {"score": 70}}
    assert row_rvol(row) == 1.4


def test_funnel_rvol_none_falls_back():
    row = {"ticker": "AAAA", "rvol": 1.4, "funnel": {"rvol": None}}
    assert row_rvol(row) == 1.4


def test_rvol_raw_is_never_used_as_a_pace():
    """rvol_raw is the naive full-day ratio. Reading it as a pace would
    understate every symbol before 16:00, so it must be ignored here."""
    row = {"ticker": "AAAA", "rvol_raw": 0.17}
    assert row_rvol(row) is None
    assert _rvol_cell(row) == "[dim]—[/dim]"


# ── missing data ─────────────────────────────────────────────────────────────

def test_missing_everything_is_none_never_zero():
    assert row_rvol({"ticker": "AAAA"}) is None
    assert row_rvol({}) is None


def test_zero_and_negative_are_treated_as_absent():
    """The server publishes nothing rather than 0 since T2.1; if a 0 ever
    arrives it means "no data", not "no volume relative to average"."""
    assert row_rvol({"rvol": 0}) is None
    assert row_rvol({"rvol": -1}) is None
    assert row_rvol({"funnel": {"rvol": 0}, "rvol": 2.0}) == 2.0


def test_non_numeric_is_absent():
    assert row_rvol({"rvol": "junk"}) is None
    assert row_rvol({"rvol": None}) is None


def test_cell_renders_a_dash_and_never_a_fabricated_zero():
    cell = _rvol_cell({"ticker": "AAAA"})
    assert cell == "[dim]—[/dim]"
    assert "0.0x" not in cell


# ── thresholds ───────────────────────────────────────────────────────────────

def test_hot_is_bold_green():
    assert _rvol_cell({"rvol": 8.2}, 3.0, 1.5) == "[bold green]8.2x[/bold green]"


def test_warm_is_yellow():
    assert _rvol_cell({"rvol": 2.0}, 3.0, 1.5) == "[yellow]2.0x[/yellow]"


def test_cold_is_dim():
    assert _rvol_cell({"rvol": 1.1}, 3.0, 1.5) == "[dim]1.1x[/dim]"


def test_threshold_boundaries_are_inclusive():
    assert "bold green" in _rvol_cell({"rvol": 3.0}, 3.0, 1.5)
    assert "yellow" in _rvol_cell({"rvol": 1.5}, 3.0, 1.5)
    assert "dim" in _rvol_cell({"rvol": 1.49}, 3.0, 1.5)


def test_thresholds_are_configurable():
    assert "bold green" in _rvol_cell({"rvol": 2.0}, 2.0, 1.0)
    assert "dim" in _rvol_cell({"rvol": 2.0}, 9.0, 5.0)


def test_formatting_is_one_decimal():
    assert "8.2x" in _rvol_cell({"rvol": 8.234}, 3.0, 1.5)
    assert "12.0x" in _rvol_cell({"rvol": 11.96}, 3.0, 1.5)


# ── column placement ─────────────────────────────────────────────────────────

def test_column_is_inserted_after_chg():
    headers = [h for h, _, _ in momentum_columns(CFG)]
    assert headers.index("RVOL") == headers.index("Chg%") + 1


def test_historical_columns_keep_their_relative_order():
    headers = [h for h, _, _ in momentum_columns(CFG)]
    base = [h for h, _, _ in MOMENTUM_COLUMNS]
    assert [h for h in headers if h in base] == base


def test_flag_off_removes_the_column_entirely():
    headers = [h for h, _, _ in momentum_columns(
        {**CFG, "rvol_column_enabled": False})]
    assert "RVOL" not in headers
    assert headers == [h for h, _, _ in MOMENTUM_COLUMNS]


def test_no_cfg_yields_the_historical_set():
    """A cfg-less call is the structural baseline the T0.1 tests rely on."""
    assert momentum_columns(None) == list(MOMENTUM_COLUMNS)


def test_base_spec_constant_is_not_mutated():
    before = list(MOMENTUM_COLUMNS)
    momentum_columns(CFG)
    momentum_columns(CFG)
    assert MOMENTUM_COLUMNS == before
    assert "RVOL" not in [h for h, _, _ in MOMENTUM_COLUMNS]


# ── rendered table ───────────────────────────────────────────────────────────

def _rvol_cells(rows, **over):
    f = Feed(CFG)
    f.rows = rows
    t = momentum_table(f, T0, 0.5, True, cfg={**CFG, **over})
    return column_cells(t, "RVOL")


def test_rendered_column_shows_per_row_values():
    rows = [
        {"ticker": "AAAA", "funnel": {"rvol": 8.2}, "rvol": 1.0},
        {"ticker": "BBBB", "rvol": 1.4},
        {"ticker": "CCCC"},
    ]
    assert _rvol_cells(rows) == ["[bold green]8.2x[/bold green]",
                                 "[dim]1.4x[/dim]",
                                 "[dim]—[/dim]"]


def test_flag_off_means_no_rvol_column_in_the_render():
    f = Feed(CFG)
    f.rows = [{"ticker": "AAAA", "rvol": 8.2}]
    t = momentum_table(f, T0, 0.5, True,
                       cfg={**CFG, "rvol_column_enabled": False})
    assert "RVOL" not in [c.header for c in t.columns]


def test_empty_feed_fallback_row_still_fills_every_column():
    f = Feed(CFG)
    f.rows = []
    t = momentum_table(f, T0, 0.5, True, cfg=CFG)
    cells = [list(c.cells) for c in t.columns]
    assert len(cells) == len(momentum_columns(CFG))
    assert all(len(c) == 1 for c in cells)


# ── history ──────────────────────────────────────────────────────────────────

def test_history_records_the_same_value_the_column_shows():
    h = SymbolHistory(maxlen=10)
    push_history(h, [{"ticker": "AAAA", "rvol": 1.2,
                      "funnel": {"rvol": 8.2}}], T0)
    assert h.series("AAAA", "rvol") == [8.2]


def test_history_records_nothing_when_there_is_no_rvol():
    h = SymbolHistory(maxlen=10)
    push_history(h, [{"ticker": "AAAA", "rvol_raw": 0.17}], T0)
    assert h.series("AAAA", "rvol") == []
