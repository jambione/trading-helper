"""The admission range-position filter: logs always, refuses only when asked.

Measured 2026-09-05 over 195 momentum admissions: where a name sits in its
own day range at admission grades the fade monotonically, and refusing the
top third took the admission book from -1.81% to +0.34%. In-sample, so the
knob ships at 0 (log only) and these tests pin that default — a filter that
silently started refusing names would be a behaviour change nobody asked
for.

The other property worth pinning is fail-open. Refusing on a range that
cannot be computed is how the MACD availability gates starved the book:
absence is not a verdict.
"""
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import admission_filter as af  # noqa: E402


class FakeEW:
    """The two accessors admission_filter reads off ai_entry_watch."""

    def __init__(self, rows, stamps=None):
        self._rows, self._stamps = rows, stamps

    def symbol_ohlc(self, sym, cfg, now):
        return self._rows

    def _cached_ohlc_stamps(self, sym, cfg, now):
        return self._stamps


def _bars(n=30, lo=10.0, hi=12.0):
    """n bars spanning lo..hi, so a price maps predictably into the range."""
    return [(hi, lo, (hi + lo) / 2) for _ in range(n)]


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_REPORT_DIR", str(tmp_path))
    af.reset_state()
    yield
    af.reset_state()


# ── the reading ──────────────────────────────────────────────────────────

def test_price_at_the_high_reads_100():
    pos, n = af.range_pos("AAA", 12.0, {}, 1.0, ew=FakeEW(_bars()))
    assert pos == pytest.approx(100.0)
    assert n == 30


def test_price_at_the_low_reads_zero():
    pos, _ = af.range_pos("AAA", 10.0, {}, 1.0, ew=FakeEW(_bars()))
    assert pos == pytest.approx(0.0)


def test_a_new_high_extends_the_range_rather_than_exceeding_it():
    """A live print above every closed bar is 100, not 140.

    Clamping would be wrong too — the range has genuinely widened.
    """
    pos, _ = af.range_pos("AAA", 14.0, {}, 1.0, ew=FakeEW(_bars()))
    assert pos == pytest.approx(100.0)


def test_too_few_bars_is_unknown_not_zero():
    pos, n = af.range_pos("AAA", 11.0, {}, 1.0, ew=FakeEW(_bars(3)))
    assert pos is None and n == 3


def test_no_bars_is_unknown():
    assert af.range_pos("AAA", 11.0, {}, 1.0, ew=FakeEW([])) == (None, 0)


# ── the default is log-only ──────────────────────────────────────────────

def test_it_refuses_nothing_by_default():
    """cap 0 = inert. The measurement is in-sample; it gets days first."""
    row = {"symbol": "AAA", "price": 12.0}
    assert af.check(row, {}, 1.0, ew=FakeEW(_bars())) is None
    assert row["admit_range_pos"] == pytest.approx(100.0)


def test_it_refuses_the_top_third_when_the_cap_is_set():
    cfg = {"ai_watch_admit_max_range_pos": 67.0}
    row = {"symbol": "AAA", "price": 12.0}
    assert af.check(row, cfg, 1.0, ew=FakeEW(_bars())) == "admit_range_pos"


def test_a_name_inside_the_cap_is_admitted():
    cfg = {"ai_watch_admit_max_range_pos": 67.0}
    row = {"symbol": "AAA", "price": 10.5}      # 25% of the range
    assert af.check(row, cfg, 1.0, ew=FakeEW(_bars())) is None


# ── fail-open, and never fatal ───────────────────────────────────────────

def test_an_unreadable_range_is_admitted_not_refused():
    cfg = {"ai_watch_admit_max_range_pos": 67.0}
    row = {"symbol": "AAA", "price": 12.0}
    assert af.check(row, cfg, 1.0, ew=FakeEW(_bars(2))) is None
    assert row["admit_range_pos"] is None


def test_a_broken_bar_source_cannot_empty_the_book():
    class Boom:
        def symbol_ohlc(self, *a, **k):
            raise RuntimeError("no bars")

        def _cached_ohlc_stamps(self, *a, **k):
            raise RuntimeError("no stamps")

    cfg = {"ai_watch_admit_max_range_pos": 67.0}
    assert af.check({"symbol": "AAA", "price": 12.0}, cfg, 1.0, ew=Boom()) is None


def test_a_missing_price_is_admitted():
    cfg = {"ai_watch_admit_max_range_pos": 67.0}
    assert af.check({"symbol": "AAA"}, cfg, 1.0, ew=FakeEW(_bars())) is None


# ── the log is the point ─────────────────────────────────────────────────

def test_it_logs_once_per_name_day():
    ew = FakeEW(_bars())
    for _ in range(5):
        af.check({"symbol": "AAA", "price": 12.0}, {}, 1.0, ew=ew)
    lines = [x for x in Path(af.log_path()).read_text().splitlines() if x.strip()]
    assert len(lines) == 1


def test_the_row_records_what_it_would_have_refused_while_inert():
    af.check({"symbol": "AAA", "price": 12.0}, {}, 1.0, ew=FakeEW(_bars()))
    rec = json.loads(Path(af.log_path()).read_text().splitlines()[0])
    assert rec["filter_active"] is False        # shipped default
    assert rec["would_refuse"] is True          # but it would have
    assert rec["range_pos"] == pytest.approx(100.0)
    assert rec["bars_used"] == 30               # partial windows stay visible
