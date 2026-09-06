"""The burst+RSI logger: fires once, on closed bars, and stamps its latency.

The rule it watches — premarket mention burst, then the first closed bar at
or after 09:30 with CM RSI-2 >= 70 — measured +2.56%/trade at 10/10 sessions
in-sample, and the same entry taken with RSI LOW loses. That polarity is
asserted explicitly below: a sign flip here would quietly collect the losing
half and look identical in the log.

Three properties matter more than the arithmetic:

  * latency_sec is the point. The measured result falls from +2.56% at the
    signal bar to +0.72% one bar later, so how fast this desk reacts decides
    which number it lives at. A row without that field is worthless.
  * closed bars only, once each. RSI-2 moves inside a forming minute, and
    firing on one would be a different signal wearing the same name.
  * it cannot raise. A logging experiment that can take the watch poll down
    is a worse trade than any it could find.
"""
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import strength_signal as ss  # noqa: E402

# 2026-09-08 (a Tuesday): ~10:00 ET inside RTH, and ~09:00 ET premarket.
OPEN_TS = 1788876000.0
PRE_TS = OPEN_TS - 3600.0


class FakeEW:
    """The three accessors strength_signal reads off ai_entry_watch."""

    def __init__(self, rows, stamps, rsi):
        self._rows, self._stamps, self._rsi = rows, stamps, rsi

    def symbol_ohlc(self, sym, cfg, now):
        return self._rows

    def _cached_ohlc_stamps(self, sym, cfg, now):
        return self._stamps

    def cm_rsi_series(self, closes, period):
        return self._rsi[:len(closes)]


def _bars(n=30, end_ts=OPEN_TS):
    rows = [(10.1, 9.9, 10.0) for _ in range(n)]
    stamps = [end_ts - 60.0 * (n - 1 - k) for k in range(n)]
    return rows, stamps


def _burst_file(tmp_path, symbols, ts=PRE_TS):
    with (tmp_path / "signal_shadow.jsonl").open("w") as fh:
        for s in symbols:
            fh.write(json.dumps({"ts": ts, "ticker": s,
                                 "signal": "mention_burst"}) + "\n")


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_REPORT_DIR", str(tmp_path))
    ss.reset_state()
    yield
    ss.reset_state()


def _run(rows, stamps, rsi, cfg=None, now=OPEN_TS):
    return ss.evaluate(["AAA"], cfg or {}, now, ew=FakeEW(rows, stamps, rsi))


# ── the rule ─────────────────────────────────────────────────────────────

def test_a_burst_name_with_strong_rsi_fires(tmp_path):
    _burst_file(tmp_path, ["AAA"])
    rows, stamps = _bars()
    out = _run(rows, stamps, [85.0] * len(rows))
    assert len(out) == 1
    assert out[0]["symbol"] == "AAA"
    assert out[0]["cm_rsi"] >= 70.0
    assert out[0]["rule"] == "premarket_burst_rsi2"


def test_weak_rsi_does_not_fire_it_is_the_losing_half(tmp_path):
    """RSI LOW on this setup was the losing arm. A sign flip must not pass."""
    _burst_file(tmp_path, ["AAA"])
    rows, stamps = _bars()
    assert _run(rows, stamps, [20.0] * len(rows)) == []


def test_a_name_with_no_premarket_burst_is_skipped(tmp_path):
    _burst_file(tmp_path, ["ZZZ"])          # someone else burst, not AAA
    rows, stamps = _bars()
    assert _run(rows, stamps, [85.0] * len(rows)) == []


def test_an_rth_burst_does_not_count_as_premarket(tmp_path):
    _burst_file(tmp_path, ["AAA"], ts=OPEN_TS - 60.0)
    rows, stamps = _bars()
    assert _run(rows, stamps, [85.0] * len(rows)) == []


def test_nothing_fires_before_the_open(tmp_path):
    _burst_file(tmp_path, ["AAA"])
    rows, stamps = _bars(end_ts=PRE_TS)
    assert _run(rows, stamps, [85.0] * len(rows), now=PRE_TS) == []


# ── once, on closed bars ─────────────────────────────────────────────────

def test_it_fires_once_per_name_day(tmp_path):
    _burst_file(tmp_path, ["AAA"])
    rows, stamps = _bars()
    rsi = [85.0] * len(rows)
    assert len(_run(rows, stamps, rsi)) == 1
    assert _run(rows, stamps, rsi) == []          # same bar, no second row


def test_the_forming_bar_is_not_scored(tmp_path):
    """The last row may still be forming, so the reading comes from the one
    before it — the same series the rule was fitted on."""
    _burst_file(tmp_path, ["AAA"])
    rows, stamps = _bars()
    out = _run(rows, stamps, [85.0] * len(rows))
    assert out and out[0]["bars"] == len(rows) - 1


def test_mismatched_stamps_are_refused_not_guessed(tmp_path):
    _burst_file(tmp_path, ["AAA"])
    rows, stamps = _bars()
    assert _run(rows, stamps[:-3], [85.0] * len(rows)) == []


# ── latency is the point ─────────────────────────────────────────────────

def test_latency_is_measured_from_the_closed_bar(tmp_path):
    _burst_file(tmp_path, ["AAA"])
    rows, stamps = _bars()
    out = _run(rows, stamps, [85.0] * len(rows), now=OPEN_TS + 7.0)
    assert out
    assert out[0]["latency_sec"] == pytest.approx(
        (OPEN_TS + 7.0) - out[0]["bar_ts"], abs=0.2)
    assert out[0]["latency_sec"] > 0


def test_the_logged_row_carries_what_a_scorer_needs(tmp_path):
    _burst_file(tmp_path, ["AAA"])
    rows, stamps = _bars()
    _run(rows, stamps, [85.0] * len(rows))
    rec = json.loads(Path(ss.log_path()).read_text().splitlines()[0])
    for k in ("bar_ts", "fired_at", "latency_sec", "cm_rsi", "price",
              "params", "burst_universe",
              "signal_bar_ts", "decision_ts", "fill_model"):
        assert k in rec, k
    assert rec["fill_model"] == "next_open"
    assert rec["signal_bar_ts"] == rec["bar_ts"]
    assert rec["decision_ts"] == rec["fired_at"]


# ── it cannot take the poll down ─────────────────────────────────────────

class Exploding:
    def symbol_ohlc(self, *a, **k):
        raise RuntimeError("no bars")

    def _cached_ohlc_stamps(self, *a, **k):
        raise RuntimeError("no stamps")

    def cm_rsi_series(self, *a, **k):
        raise RuntimeError("no rsi")


def test_a_broken_bar_source_is_swallowed(tmp_path):
    _burst_file(tmp_path, ["AAA"])
    assert ss.evaluate(["AAA"], {}, OPEN_TS, ew=Exploding()) == []


def test_a_missing_burst_file_fires_nothing(tmp_path):
    """No file is no information, not "no bursts". Record less, never more."""
    rows, stamps = _bars()
    assert _run(rows, stamps, [85.0] * len(rows)) == []


def test_disabled_does_nothing(tmp_path):
    _burst_file(tmp_path, ["AAA"])
    rows, stamps = _bars()
    cfg = {"ai_strength_signal_enabled": False}
    assert _run(rows, stamps, [85.0] * len(rows), cfg) == []
