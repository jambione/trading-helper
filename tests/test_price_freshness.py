"""Price freshness: observation age, freshest-source-wins, staleness display.

The defect these cover: `price` fed the Price and Chg% columns and the journal,
but nothing recorded when the underlying print actually happened. `price_ts`
looked like it did — it is rewritten on every 10Hz loop pass whether or not the
price changed, so it always reads fresh and detects nothing.
"""
import os
import sys

from conftest import column_cells  # noqa: E402

_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "momentum-monitor"))

from journal import journal_record  # noqa: E402
from momentum_signal import DEFAULTS, Feed, _cell_price, momentum_table  # noqa: E402

from dashboard import freshest_prices, stream_covered  # noqa: E402

T0 = 1753449600.0
CFG = {**DEFAULTS, "alert_new": False, "alert_burst": False,
       "alert_buy": False, "focus_age_enabled": False,
       "setup_distance_enabled": False, "mention_trend_enabled": False}


# ── the merge picks the freshest source, not a favoured one ──────────────────

# The real functions the price loop calls — not reimplementations of them,
# which could pass while the loop diverged.
_merge = freshest_prices


def test_a_fresh_alpaca_print_beats_a_stale_finnhub_tick():
    """The bug: Finnhub won unconditionally, so a stream tick from 90s ago beat
    an Alpaca print from 5s ago. Pre-market, where the stream idles, that was
    the normal case."""
    merged = _merge({"AAAA": (10.50, T0 - 5)}, {"AAAA": (10.00, T0 - 90)})
    assert merged["AAAA"][0] == 10.50


def test_a_fresh_finnhub_tick_still_beats_an_older_alpaca_print():
    merged = _merge({"AAAA": (10.00, T0 - 30)}, {"AAAA": (10.50, T0 - 1)})
    assert merged["AAAA"][0] == 10.50


def test_either_source_alone_is_used():
    assert _merge({"AAAA": (10.0, T0)}, {})["AAAA"][0] == 10.0
    assert _merge({}, {"BBBB": (2.0, T0)})["BBBB"][0] == 2.0


def test_equal_timestamps_do_not_flap():
    """Identical observation times must resolve deterministically, not swap
    between polls — jitter on a 2s refresh is worse than a stale tie."""
    seen = {_merge({"AAAA": (10.0, T0)}, {"AAAA": (11.0, T0)})["AAAA"][0]
            for _ in range(20)}
    assert len(seen) == 1


# ── a stale stream price must not suppress the fallback poll ─────────────────

_covered = stream_covered


def test_a_symbol_that_printed_once_and_went_quiet_is_not_covered():
    """It used to be covered forever, which switched off the 5s Alpaca poll for
    the rest of the session."""
    fh = {"AAAA": {"price": 10.0, "ts_unix": T0 - 300}}
    assert _covered(fh, T0, 20.0) == set()


def test_a_currently_ticking_symbol_is_covered():
    fh = {"AAAA": {"price": 10.0, "ts_unix": T0 - 2}}
    assert _covered(fh, T0, 20.0) == {"AAAA"}


def test_a_price_with_no_timestamp_is_not_treated_as_current():
    for d in ({"price": 10.0}, {"price": 10.0, "ts_unix": 0},
              {"price": 10.0, "ts_unix": None}):
        assert _covered({"AAAA": d}, T0, 20.0) == set()


# ── monitor display ─────────────────────────────────────────────────────────

def test_a_current_price_renders_plainly():
    assert _cell_price({"price": 3.41, "price_age_sec": 2.0},
                       {"cfg": CFG}) == "3.41"


def test_a_stale_price_is_dimmed_and_carries_its_age():
    cell = _cell_price({"price": 3.41, "price_age_sec": 47.0}, {"cfg": CFG})
    assert "dim" in cell
    assert "3.41" in cell
    assert "47s" in cell


def test_the_number_is_never_withheld():
    """A 40s-old price is still roughly right and is what you have; it just
    must not read as live."""
    cell = _cell_price({"price": 3.41, "price_age_sec": 400.0}, {"cfg": CFG})
    assert "3.41" in cell


def test_a_missing_age_renders_plainly_rather_than_claiming_staleness():
    assert _cell_price({"price": 3.41}, {"cfg": CFG}) == "3.41"
    assert _cell_price({"price": 3.41, "price_age_sec": None},
                       {"cfg": CFG}) == "3.41"


def test_a_missing_price_is_still_a_dash():
    assert _cell_price({}, {"cfg": CFG}) == "—"
    assert _cell_price({"price_age_sec": 90.0}, {"cfg": CFG}) == "—"


def test_the_staleness_threshold_is_configurable():
    row = {"price": 3.41, "price_age_sec": 15.0}
    assert "dim" not in _cell_price(row, {"cfg": {**CFG,
                                                 "price_stale_sec": 20.0}})
    assert "dim" in _cell_price(row, {"cfg": {**CFG, "price_stale_sec": 5.0}})


def test_flag_off_renders_exactly_as_before():
    row = {"price": 3.41, "price_age_sec": 400.0}
    assert _cell_price(row, {"cfg": {**CFG,
                                     "price_age_enabled": False}}) == "3.41"


def test_rendered_column_marks_only_the_stale_rows():
    f = Feed(CFG)
    f.rows = [{"ticker": "LIVE", "price": 3.41, "price_age_sec": 1.0},
              {"ticker": "QUIET", "price": 9.90, "price_age_sec": 62.0}]
    cells = column_cells(momentum_table(f, T0, 0.5, True, cfg=CFG), "Price")
    assert cells[0] == "3.41"
    assert "dim" in cells[1] and "62s" in cells[1]


# ── journal ─────────────────────────────────────────────────────────────────

def test_journal_records_the_price_age():
    """T4.2 has to be able to exclude events whose entry reference was stale —
    a forward return measured from a 60s-old price starts in the wrong place."""
    rec = journal_record("focus", "ABCD",
                         {"ticker": "ABCD", "price": 3.41,
                          "price_age_sec": 47.0}, T0)
    assert rec["price_age_sec"] == 47.0


def test_journal_omits_the_age_when_the_server_did_not_publish_it():
    rec = journal_record("focus", "ABCD",
                         {"ticker": "ABCD", "price": 3.41}, T0)
    assert "price_age_sec" not in rec
