"""
test_signal_shadow.py — the counterfactual price track behind Discord signals.

Discord-side signals (Bob's call-outs, scanner price spikes, mention bursts)
reached no outcome log at all: shadow.jsonl and rejects.jsonl only carry
`trending` and `momentum`, because those are the sources the entry watch draws
from. signal_shadow.jsonl is what makes "was the call worth taking?" answerable,
and tools/signal_report.py turns it into forward returns.

Two properties matter most and are pinned here: the sampler must never cost an
API call (it reads prices already merged into STATE), and a signal with no price
must be recorded as unmeasurable rather than dropped — a silently short file is
exactly how a dead feed looks healthy.

Run:
    venv/bin/python -m pytest tests/test_signal_shadow.py -q
"""

import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dashboard as d          # noqa: E402
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "tools"))
import signal_report as sr     # noqa: E402


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(d, "add_ticker_to_log", lambda t: (True, True))
    with d.STATE.lock:
        d.STATE.signal_watch.clear()
        d.STATE.tickers.clear()
        d.STATE.bb_live.clear()
    _path().unlink(missing_ok=True)
    yield


def _path():
    import ai_paths
    return ai_paths.report_file("signal_shadow.jsonl")


def _rows():
    p = _path()
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()]


# ── sampling ─────────────────────────────────────────────────────────────────

def test_a_signal_is_sampled_on_its_cadence():
    now = time.time()
    with d.STATE.lock:
        d.STATE.tickers["AAA"] = {"price": 10.0}
    d.note_signal("AAA", "bb_live", now, {"price": 10.0}, now)

    assert d._sample_signal_shadow(now) == 1          # first sample fires
    assert d._sample_signal_shadow(now + 1) == 0      # too soon
    with d.STATE.lock:
        d.STATE.tickers["AAA"]["price"] = 11.0
    assert d._sample_signal_shadow(now + d._SIGNAL_SAMPLE_SEC + 1) == 1

    rows = _rows()
    assert [r["price"] for r in rows] == [10.0, 11.0]
    assert all(r["entry_price"] == 10.0 for r in rows)
    assert rows[1]["elapsed_sec"] >= d._SIGNAL_SAMPLE_SEC


def test_sampling_stops_after_the_window():
    now = time.time()
    d.note_signal("AAA", "bb_live", now, {"price": 10.0}, now)
    assert d._sample_signal_shadow(now + d._SIGNAL_WINDOW_SEC + 1) == 0
    with d.STATE.lock:
        assert ("AAA", "bb_live") not in d.STATE.signal_watch


def test_a_signal_with_no_price_is_recorded_as_unmeasurable():
    """Call-outs are kept out of the watchlist, so some never get a quote.
    Dropping them would hide the coverage gap instead of measuring it."""
    now = time.time()
    d.note_signal("ZZZ", "bb_live", now, {}, now)
    assert d._sample_signal_shadow(now) == 1
    row = _rows()[0]
    assert row["entry_price"] is None and row["price"] is None
    assert row["price_src"] is None


def test_scanner_seed_is_labelled_not_passed_off_as_a_quote():
    now = time.time()
    entry = {"scanner_price": 2.50, "scanner_price_ts": now - 20}
    with d.STATE.lock:
        d.STATE.tickers["OTC"] = dict(entry)
    d.note_signal("OTC", "price_spike", now, entry, now)
    d._sample_signal_shadow(now)
    row = _rows()[0]
    assert (row["entry_price"], row["entry_price_src"]) == (2.50, "scanner")
    assert row["price_src"] == "scanner"


def test_the_same_name_keeps_its_signals_separate():
    """A call-out and a scanner spike on one name must stay distinguishable —
    telling them apart is the point of measuring them."""
    now = time.time()
    with d.STATE.lock:
        d.STATE.tickers["AAA"] = {"price": 5.0}
    d.note_signal("AAA", "bb_live", now, {"price": 5.0}, now)
    d.note_signal("AAA", "price_spike", now, {"price": 5.0}, now)
    d._sample_signal_shadow(now)
    assert sorted(r["signal"] for r in _rows()) == ["bb_live", "price_spike"]


def test_re_signalling_restarts_the_window():
    """A second call on a name is a fresh opinion, measured from when it was
    made rather than from the first one."""
    now = time.time()
    d.note_signal("AAA", "bb_live", now, {"price": 5.0}, now)
    later = now + 600
    with d.STATE.lock:
        d.STATE.tickers["AAA"] = {"price": 7.0}
    d.note_signal("AAA", "bb_live", later, {"price": 7.0}, later)
    with d.STATE.lock:
        rec = d.STATE.signal_watch[("AAA", "bb_live")]
    assert rec["at"] == later and rec["entry_price"] == 7.0


def test_sampler_is_cheap_when_nothing_is_tracked():
    assert d._sample_signal_shadow(time.time()) == 0
    assert _rows() == []


# ── the ingest hooks ─────────────────────────────────────────────────────────

def test_a_call_out_starts_a_track_at_its_said_time():
    now = time.time()
    d.ingest_discord_alerts([], [], {}, [
        {"ticker": "NRXP", "text": "NRXP pop", "ts": now, "said": ""},
    ])
    with d.STATE.lock:
        assert ("NRXP", "bb_live") in d.STATE.signal_watch


def test_a_price_spike_starts_a_track():
    d.ingest_discord_alerts([
        {"ticker": "PN", "line": "PN Price Volatility Spike! >>>>> x",
         "alert_type": "Price Volatility Spike", "price": 3.1},
    ])
    with d.STATE.lock:
        assert ("PN", "price_spike") in d.STATE.signal_watch


def test_a_mention_burst_starts_a_track_without_deadlocking():
    """_track_mention runs holding STATE.lock; the hook must use the locked
    variant or the price loop and ingest deadlock against each other."""
    threshold = int(d.STATE.cfg.get("mention_alert_threshold", 5))
    with d.STATE.lock:
        d.STATE.push_notified.discard("BUR")
        d.STATE.mention_ts.pop("BUR", None)
        for _ in range(threshold):
            d._track_mention("BUR")
    with d.STATE.lock:
        assert ("BUR", "mention_burst") in d.STATE.signal_watch


# ── the report maths ─────────────────────────────────────────────────────────

def _sample(t, sig, at, elapsed, entry, price):
    return {"ts": at + elapsed, "ticker": t, "signal": sig, "signal_at": at,
            "elapsed_sec": elapsed, "entry_price": entry, "entry_price_src": "quote",
            "price": price, "price_src": "quote"}


def test_forward_return_is_measured_from_the_signal_price():
    at = 1_700_000_000
    rows = [_sample("AAA", "bb_live", at, 0, 10.0, 10.0),
            _sample("AAA", "bb_live", at, 300, 10.0, 11.0)]      # +5m, +10%
    eps = sr.group_episodes(rows)
    er  = sr.episode_returns(list(eps.values())[0], [5.0])
    assert round(er["rets"][5.0], 4) == 0.1


def test_a_horizon_with_no_nearby_sample_is_absent_not_guessed():
    """A 3-minute-old price must not be reported as the 30-minute one."""
    at = 1_700_000_000
    rows = [_sample("AAA", "bb_live", at, 0, 10.0, 10.0),
            _sample("AAA", "bb_live", at, 180, 10.0, 12.0)]
    er = sr.episode_returns(list(sr.group_episodes(rows).values())[0], [30.0])
    assert 30.0 not in er["rets"]


def test_unmeasurable_episodes_are_counted_not_dropped():
    at = 1_700_000_000
    rows = [_sample("AAA", "bb_live", at, 0, 10.0, 10.0),
            _sample("ZZZ", "bb_live", at, 0, None, None)]
    by = sr.summarise(rows, [5.0])
    assert by["bb_live"]["n"] == 2
    assert by["bb_live"]["measurable"] == 1


def test_episodes_are_grouped_per_signal_instance():
    at1, at2 = 1_700_000_000, 1_700_000_600
    rows = [_sample("AAA", "bb_live", at1, 0, 10.0, 10.0),
            _sample("AAA", "bb_live", at2, 0, 12.0, 12.0),
            _sample("AAA", "price_spike", at1, 0, 10.0, 10.0)]
    assert len(sr.group_episodes(rows)) == 3


def test_report_runs_on_an_empty_file(capsys):
    sr.print_report(sr.summarise([], [5.0]), [5.0], None)
    assert "No signal samples recorded" in capsys.readouterr().out
