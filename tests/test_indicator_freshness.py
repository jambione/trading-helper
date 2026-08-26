"""How stale were EXH and RSI when the desk armed? The record could not say.

The desk gates arming on the age of the PRINT (`_row_tape_stale`, 8s via
`ai_watch_decision_max_age_sec`) while %R and CM RSI-2 come from bars it
never times. Across 2026-08-24..26 `cm_rsi_age_sec` was absent from all
17,585 RTH shadow rows and `pctr_age_sec` did not exist as a field, so the
question had to be answered by reading a live process — which cannot be
done for a session already over.

That mattered more than it looked. 15% of RTH `cm_rsi` readings come from
the `alpaca` fallback (bars up to 60s old) and 26% of `pctr` from degraded
paths, nothing in the arm gate distinguishes them, and RSI is the dominant
lever: `rsi_extended` and `rsi_not_rising` are 91% of all blocks.

Two ages are logged rather than one because they fail independently and
add: engine-side (the tape the reading was computed on) and transport-side
(the age of this process's cached /api/state copy). A sub-second realtime
bar read through a stalled transport is not a realtime decision.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import ai_entry_watch as ew  # noqa: E402


def _rec(**ind):
    return {
        "symbol": "AAA",
        "status": "watching",
        "structure": {"entry_low": 9.0, "entry_high": 10.0,
                      "stop_price": 8.5, "target_1": 11.0},
        "indicator": ind,
    }


def _row(rec):
    return ew._shadow_row(rec, price=9.5, price_src="quote",
                          arm_ok=False, arm_why="wait_rsi", now=1_000_000.0)


# ── transport age ────────────────────────────────────────────────────────

def test_snapshot_age_is_none_before_anything_is_fetched(monkeypatch):
    """Unknown age must not read as fresh. 0.0 would mean 'just fetched'."""
    monkeypatch.setattr(ew, "_DASH_CACHE", (0.0, {}))
    assert ew.dashboard_state_age_sec() is None


def test_snapshot_age_is_none_when_the_last_fetch_failed(monkeypatch):
    """dashboard_state caches an empty dict on failure — that is not a read."""
    import time
    monkeypatch.setattr(ew, "_DASH_CACHE", (time.monotonic(), {}))
    assert ew.dashboard_state_age_sec() is None


def test_snapshot_age_measures_a_real_populated_cache(monkeypatch):
    import time
    monkeypatch.setattr(
        ew, "_DASH_CACHE", (time.monotonic() - 4.0, {"tickers": []}))
    got = ew.dashboard_state_age_sec()
    assert got is not None
    assert 3.5 <= got <= 5.0


def test_snapshot_age_never_goes_negative(monkeypatch):
    """A clock that jumps must report 0.0, not a negative age."""
    import time
    monkeypatch.setattr(
        ew, "_DASH_CACHE", (time.monotonic() + 30.0, {"tickers": []}))
    assert ew.dashboard_state_age_sec() == 0.0


# ── engine-side age on the row ───────────────────────────────────────────

def test_row_carries_the_measured_rsi_age():
    row = _row(_rec(cm_rsi=55.0, cm_rsi_age_sec=0.7, cm_rsi_src="realtime"))
    assert row["bars_age_sec"] == 0.7


def test_row_falls_back_to_bars_age_when_rsi_age_is_absent():
    """The engine publishes bars_age_sec; ai_entry_watch remaps it to
    cm_rsi_age_sec. Rows written from either shape must carry the number."""
    row = _row(_rec(cm_rsi=55.0, bars_age_sec=1.4))
    assert row["bars_age_sec"] == 1.4


def test_a_genuinely_fresh_zero_age_is_not_swallowed():
    """0.0 means 'this tick'. Collapsing it to None would delete the best
    rows from every freshness slice and bias the answer toward stale."""
    row = _row(_rec(cm_rsi=55.0, cm_rsi_age_sec=0.0))
    assert row["bars_age_sec"] == 0.0
    assert row["bars_age_sec"] is not None


def test_fallback_bars_report_an_unknown_age_rather_than_a_fake_one():
    """pctr_src=sparse_window is the REST fallback: the age is genuinely not
    known, and inventing one would make a stale reading look timed."""
    row = _row(_rec(cm_rsi=92.9, pctr_src="sparse_window"))
    assert "bars_age_sec" in row
    assert row["bars_age_sec"] is None


def test_the_columns_exist_even_with_no_indicator_at_all():
    """A row that omits the column cannot be distinguished downstream from a
    row that recorded a missing age — that is the bug this file exists for."""
    row = _row(_rec())
    assert "bars_age_sec" in row and row["bars_age_sec"] is None
    assert "ind_snapshot_age_sec" in row


def test_both_ages_are_recorded_separately_and_never_blended():
    """Summing them in the writer would hide which half broke. A slice can
    add two columns; it cannot take one apart."""
    row = _row(_rec(cm_rsi=55.0, cm_rsi_age_sec=0.3))
    assert row["bars_age_sec"] == 0.3
    assert "ind_snapshot_age_sec" in row
    assert row["ind_snapshot_age_sec"] != row["bars_age_sec"] or (
        row["ind_snapshot_age_sec"] is None)


def test_no_pctr_age_column_is_invented():
    """When pctr_src is "live" the %R came off these same bars, so its age IS
    bars_age_sec; on the fallback paths it is unknown. A second column would
    either duplicate the value and drift, or imply a measurement never taken.
    """
    row = _row(_rec(pctr=-12.0, cm_rsi=55.0, cm_rsi_age_sec=0.5,
                    pctr_src="live"))
    assert "pctr_age_sec" not in row
    # Provenance plus one age is what makes the row answerable.
    assert row["pctr_src"] == "live"
    assert row["bars_age_sec"] == 0.5


def test_provenance_still_travels_with_the_age():
    """Age without src cannot separate a realtime reading from a fallback one
    that happens to be young."""
    row = _row(_rec(cm_rsi=55.0, cm_rsi_age_sec=0.2, cm_rsi_src="realtime",
                    pctr_src="live"))
    assert row["cm_rsi_src"] == "realtime"
    assert row["pctr_src"] == "live"
    assert row["bars_age_sec"] == 0.2
