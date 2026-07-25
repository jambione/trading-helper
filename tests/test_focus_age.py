"""T1.1 — FOCUS age: rising/falling edges, re-fire reset, formatter."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "momentum-monitor"))

from momentum_signal import (  # noqa: E402
    DEFAULTS,
    Feed,
    _focus_age_markup,
    _focus_age_str,
    _rsi_focus_cell,
    momentum_table,
)

T0 = 1753449600.0

FOCUS_SP = {"cm_rsi": 22.0, "pctr": -91.0, "pctr_slow": -88.0,
            "pctr_deep_os": True}
IDLE_SP = {"cm_rsi": 44.0, "pctr": -40.0, "pctr_slow": -30.0}


class _NullAlerter:
    def fire(self, *a, **k):
        pass


CFG = {**DEFAULTS, "alert_new": False, "alert_burst": False,
       "alert_buy": False}


def _ingest(feed, sp_by_sym, now):
    rows = [{"ticker": s, "signal_proximity": sp}
            for s, sp in sp_by_sym.items()]
    feed.ingest({"tickers": rows}, now, _NullAlerter(), CFG)


# ── formatter ────────────────────────────────────────────────────────────────

def test_focus_age_str_formats():
    assert _focus_age_str(0) == "0:00"
    assert _focus_age_str(14) == "0:14"
    assert _focus_age_str(62) == "1:02"
    assert _focus_age_str(599) == "9:59"


def test_focus_age_str_caps_so_the_column_cannot_widen():
    assert _focus_age_str(600) == "9:59+"
    assert _focus_age_str(3600) == "9:59+"


def test_focus_age_str_empty_when_not_lit():
    assert _focus_age_str(None) == ""
    assert _focus_age_str(-1) == ""
    assert _focus_age_str("junk") == ""


def test_focus_age_color_tiers():
    """Fresh green, aging yellow, stale dim — a stale FOCUS looks stale."""
    assert "green" in _focus_age_markup(10, 60.0, 180.0)
    assert "yellow" in _focus_age_markup(90, 60.0, 180.0)
    assert "dim" in _focus_age_markup(240, 60.0, 180.0)
    assert _focus_age_markup(None) == ""


def test_focus_age_color_boundaries_are_exclusive_at_the_low_end():
    assert "yellow" in _focus_age_markup(60.0, 60.0, 180.0)
    assert "dim" in _focus_age_markup(180.0, 60.0, 180.0)


# ── edges ────────────────────────────────────────────────────────────────────

def test_age_starts_at_zero_on_the_rising_edge():
    f = Feed(CFG)
    _ingest(f, {"AAAA": FOCUS_SP}, T0)
    assert f.focus_age("AAAA", T0) == 0.0
    assert _focus_age_str(f.focus_age("AAAA", T0)) == "0:00"


def test_age_accumulates_while_focus_holds():
    f = Feed(CFG)
    for i in range(4):
        _ingest(f, {"AAAA": FOCUS_SP}, T0 + i * 2.0)
    assert f.focus_age("AAAA", T0 + 6.0) == 6.0


def test_falling_edge_clears_the_timer():
    f = Feed(CFG)
    _ingest(f, {"AAAA": FOCUS_SP}, T0)
    _ingest(f, {"AAAA": IDLE_SP}, T0 + 2.0)
    assert f.focus_age("AAAA", T0 + 2.0) is None
    assert "AAAA" not in f.focus_since


def test_refire_resets_rather_than_resumes():
    f = Feed(CFG)
    _ingest(f, {"AAAA": FOCUS_SP}, T0)            # lit
    _ingest(f, {"AAAA": IDLE_SP}, T0 + 10.0)      # dropped
    _ingest(f, {"AAAA": FOCUS_SP}, T0 + 20.0)     # re-fired
    assert f.focus_age("AAAA", T0 + 20.0) == 0.0


def test_leaving_and_rejoining_the_feed_resets_the_timer():
    f = Feed(CFG)
    _ingest(f, {"AAAA": FOCUS_SP}, T0)
    _ingest(f, {"BBBB": IDLE_SP}, T0 + 10.0)      # AAAA off the feed
    assert "AAAA" not in f.focus_since
    _ingest(f, {"AAAA": FOCUS_SP}, T0 + 30.0)     # back, still in FOCUS
    assert f.focus_age("AAAA", T0 + 30.0) == 0.0


def test_never_lit_symbol_has_no_age():
    f = Feed(CFG)
    _ingest(f, {"AAAA": IDLE_SP}, T0)
    assert f.focus_age("AAAA", T0) is None
    assert f.focus_age("NOPE", T0) is None


def test_focus_clock_is_per_symbol():
    f = Feed(CFG)
    _ingest(f, {"AAAA": FOCUS_SP, "BBBB": IDLE_SP}, T0)
    _ingest(f, {"AAAA": FOCUS_SP, "BBBB": FOCUS_SP}, T0 + 10.0)
    assert f.focus_age("AAAA", T0 + 10.0) == 10.0
    assert f.focus_age("BBBB", T0 + 10.0) == 0.0


# ── cell rendering ───────────────────────────────────────────────────────────

def test_cell_shows_age_when_supplied():
    cell = _rsi_focus_cell({"signal_proximity": FOCUS_SP}, age=14.0)
    assert "FOCUS" in cell
    assert "0:14" in cell
    assert "22·-91/-88" in cell


def test_cell_without_age_is_unchanged_from_today():
    row = {"signal_proximity": FOCUS_SP}
    assert _rsi_focus_cell(row, age=None) == _rsi_focus_cell(row)


def test_non_focus_rows_never_show_an_age():
    cell = _rsi_focus_cell({"signal_proximity": IDLE_SP}, age=99.0)
    assert "0:" not in cell
    assert "1:" not in cell


def test_flag_off_renders_the_cell_exactly_as_today():
    """Acceptance: focus_age_enabled false => byte-identical Setup cell.

    The expected string is the literal Phase 0 output, not another call's
    output. Calling momentum_table() with no cfg is NOT a pre-Phase-1
    baseline: the flag lookups fall back to their DEFAULTS value, which is
    True, so a bare call legitimately renders the age.
    """
    phase0 = ("[bold black on green] FOCUS [/] "
              "[bold green]22·-91/-88[/bold green]")
    f = Feed(CFG)
    _ingest(f, {"AAAA": FOCUS_SP}, T0)
    f.rows = [{"ticker": "AAAA", "signal_proximity": FOCUS_SP}]

    def setup_cells(**over):
        cfg = {**CFG, "setup_distance_enabled": False, **over}
        t = momentum_table(f, T0 + 30.0, 0.5, True, cfg=cfg)
        return list(list(t.columns)[6].cells)

    assert setup_cells(focus_age_enabled=False) == [phase0]
    on = setup_cells(focus_age_enabled=True)
    assert on != [phase0]
    assert "0:30" in on[0]
