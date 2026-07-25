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


def _seeded_feed(now=T0 - 2.0):
    """A Feed past its first poll, so later FOCUS transitions are observed
    rising edges. On the seeding poll we cannot know how long a setup has
    already been lit, so ages there are deliberately unknown."""
    f = Feed(CFG)
    _ingest(f, {"SEED": IDLE_SP}, now)
    return f


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
    f = _seeded_feed()
    _ingest(f, {"AAAA": FOCUS_SP}, T0)
    assert f.focus_age("AAAA", T0) == 0.0
    assert _focus_age_str(f.focus_age("AAAA", T0)) == "0:00"


def test_age_accumulates_while_focus_holds():
    f = _seeded_feed()
    for i in range(4):
        _ingest(f, {"AAAA": FOCUS_SP}, T0 + i * 2.0)
    assert f.focus_age("AAAA", T0 + 6.0) == 6.0


# ── already lit when we started ───────────────────────────────────────────────

def test_focus_lit_on_the_seeding_poll_has_no_age():
    """A setup already lit before the monitor started may have been lit for
    ten minutes. Claiming a fresh 0:00 would be a wrong number that looks
    like information."""
    f = Feed(CFG)
    _ingest(f, {"AAAA": FOCUS_SP}, T0)          # first poll ever
    assert f.in_focus("AAAA") is True
    assert f.focus_age("AAAA", T0) is None
    assert f.focus_age("AAAA", T0 + 600.0) is None


def test_unknown_age_stays_unknown_while_it_holds():
    f = Feed(CFG)
    _ingest(f, {"AAAA": FOCUS_SP}, T0)
    for i in range(1, 6):
        _ingest(f, {"AAAA": FOCUS_SP}, T0 + i * 2.0)
    assert f.focus_age("AAAA", T0 + 10.0) is None


def test_unknown_age_becomes_measured_after_a_real_refire():
    f = Feed(CFG)
    _ingest(f, {"AAAA": FOCUS_SP}, T0)          # lit at startup, age unknown
    _ingest(f, {"AAAA": IDLE_SP}, T0 + 10.0)    # dropped — now observable
    _ingest(f, {"AAAA": FOCUS_SP}, T0 + 20.0)   # genuine rising edge
    assert f.focus_age("AAAA", T0 + 20.0) == 0.0


def test_unknown_age_renders_a_blank_chip_not_zero():
    f = Feed(CFG)
    _ingest(f, {"AAAA": FOCUS_SP}, T0)
    f.rows = [{"ticker": "AAAA", "signal_proximity": FOCUS_SP}]
    t = momentum_table(f, T0 + 300.0, 0.5, True,
                       cfg={**CFG, "setup_distance_enabled": False})
    cell = next(iter(list(t.columns)[6].cells))
    assert "FOCUS" in cell
    assert "0:00" not in cell
    assert "5:00" not in cell


def test_in_focus_distinguishes_unlit_from_unknown_age():
    f = Feed(CFG)
    _ingest(f, {"AAAA": FOCUS_SP, "BBBB": IDLE_SP}, T0)
    assert f.in_focus("AAAA") is True
    assert f.in_focus("BBBB") is False
    assert f.focus_age("AAAA", T0) is None      # same render, different meaning
    assert f.focus_age("BBBB", T0) is None


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
    f = _seeded_feed()
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
    f = _seeded_feed()
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
