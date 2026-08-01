"""T4.1 — session journal: rising-edge dedupe, flush, failure containment.

The journal is the input to threshold tuning (T4.2), so these tests care as
much about what is NOT written as what is.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "momentum-monitor"))

from journal import MAX_BUFFER, Journal, journal_record  # noqa: E402
from momentum_signal import DEFAULTS, Feed  # noqa: E402

# 2025-07-25 09:47:12 ET
TS = 1753451232.0

CFG = {**DEFAULTS, "alert_new": False, "alert_burst": False,
       "alert_buy": False}

FOCUS_SP = {"cm_rsi": 22.0, "pctr": -91.0, "pctr_slow": -88.0,
            "pctr_deep_os": True, "proximity_pct": 100,
            "mention_velocity": 7}
IDLE_SP = {"cm_rsi": 44.0, "pctr": -40.0, "pctr_slow": -30.0}


class _NullAlerter:
    def __init__(self):
        self.fired = []

    def fire(self, kind, sym, detail=""):
        self.fired.append((kind, sym))


def _row(**kw):
    base = {"ticker": "ABCD", "price": 3.41, "pct_change": 12.4,
            "mention_window": 9, "mention_count": 31}
    base.update(kw)
    return base


def _ingest(feed, rows, now, alerter=None):
    feed.ingest({"tickers": rows}, now, alerter or _NullAlerter(), CFG)


# ── record shape ─────────────────────────────────────────────────────────────

def test_record_has_the_documented_fields():
    rec = journal_record("focus", "ABCD", _row(signal_proximity=FOCUS_SP), TS,
                         st_rank=4, rvol=6.2)
    assert rec["kind"] == "focus"
    assert rec["sym"] == "ABCD"
    assert rec["ts"] == TS
    assert rec["price"] == 3.41
    assert rec["pct_change"] == 12.4
    assert rec["rvol"] == 6.2
    assert rec["mention_window"] == 9
    assert rec["mention_count"] == 31
    assert rec["cm_rsi"] == 22.0
    assert rec["pctr"] == -91.0
    assert rec["pctr_slow"] == -88.0
    assert rec["proximity_pct"] == 100
    assert rec["st_rank"] == 4


def test_et_and_session_window_come_from_the_event_timestamp():
    """Not from the wall clock — a buffered record must carry when the event
    happened, not when it reached disk."""
    rec = journal_record("focus", "ABCD", _row(), TS)
    assert rec["et"] == "09:47:12"
    assert rec["session_window"] == "TRANCHE 3"


def test_symbol_is_upper_cased():
    assert journal_record("new", "abcd", _row(), TS)["sym"] == "ABCD"


def test_timestamps_are_et_regardless_of_host_timezone():
    """The desk also runs on a Windows box that need not be in ET. Falling
    back to naive local time would stamp `et` and the daily filename in the
    host's zone — invisible, and wrong for every downstream slice."""
    import time as _time

    old = os.environ.get("TZ")
    try:
        for tz in ("UTC", "Asia/Tokyo", "America/Los_Angeles"):
            os.environ["TZ"] = tz
            if hasattr(_time, "tzset"):
                _time.tzset()
            rec = journal_record("focus", "ABCD", _row(), TS)
            assert rec["et"] == "09:47:12", tz
            assert rec["session_window"] == "TRANCHE 3", tz
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        if hasattr(_time, "tzset"):
            _time.tzset()


# ── omission, not fabrication ────────────────────────────────────────────────

def test_unknown_fields_are_omitted_not_zero_filled():
    """A 0 that really means "no data" would bias every bucket it lands in."""
    rec = journal_record("new", "ABCD", {"ticker": "ABCD"}, TS)
    for k in ("price", "pct_change", "rvol", "cm_rsi", "pctr", "pctr_slow",
              "proximity_pct", "mention_window", "mention_count", "st_rank"):
        assert k not in rec, f"{k} should be absent, got {rec.get(k)!r}"


def test_absent_rvol_is_omitted():
    rec = journal_record("focus", "ABCD", _row(), TS, rvol=None)
    assert "rvol" not in rec


def test_confluence_absent_is_silence_not_an_empty_list():
    """"confluence": [] would assert "no confirming sources"; the truth is
    that the server did not tell us. The dashboard only publishes the field
    at count >= 2."""
    rec = journal_record("focus", "ABCD", _row(), TS)
    assert "confluence" not in rec
    assert "confluence_count" not in rec


def test_confluence_recorded_when_present():
    rec = journal_record("focus", "ABCD", _row(
        confluence={"sources": ["alert", "squeeze"], "count": 2}), TS)
    assert rec["confluence"] == ["alert", "squeeze"]
    assert rec["confluence_count"] == 2


def test_non_numeric_and_nan_values_are_dropped():
    rec = journal_record("new", "ABCD", _row(price="junk",
                                             pct_change=float("nan")), TS)
    assert "price" not in rec
    assert "pct_change" not in rec


def test_boolean_flags_only_appear_when_true():
    plain = journal_record("new", "ABCD", _row(), TS)
    assert "find_it_first" not in plain
    assert "mention_burst" not in plain
    flagged = journal_record("new", "ABCD",
                             _row(find_it_first=True, mention_burst=True), TS)
    assert flagged["find_it_first"] is True
    assert flagged["mention_burst"] is True


def test_extra_fields_pass_through_and_skip_nones():
    rec = journal_record("focus", "ABCD", _row(), TS,
                         extra={"tier": 3, "nothing": None})
    assert rec["tier"] == 3
    assert "nothing" not in rec


# ── rising edges from Feed ───────────────────────────────────────────────────

def test_edges_are_empty_on_the_seeding_poll():
    """Nothing was observed to transition; the state was simply discovered."""
    f = Feed(CFG)
    _ingest(f, [_row(signal_proximity=FOCUS_SP)], TS)
    assert f.edges == []


def test_new_and_focus_edges_fire_once():
    f = Feed(CFG)
    _ingest(f, [_row(ticker="SEED", signal_proximity=IDLE_SP)], TS)
    _ingest(f, [_row(signal_proximity=FOCUS_SP)], TS + 2.0)
    kinds = [k for k, _ in f.edges]
    assert kinds.count("new") == 1
    assert kinds.count("focus") == 1


def test_no_duplicate_edges_while_the_condition_holds():
    f = Feed(CFG)
    _ingest(f, [_row(ticker="SEED", signal_proximity=IDLE_SP)], TS)
    _ingest(f, [_row(signal_proximity=FOCUS_SP)], TS + 2.0)
    for i in range(2, 8):
        _ingest(f, [_row(signal_proximity=FOCUS_SP)], TS + i * 2.0)
        assert f.edges == [], f"poll {i} re-fired: {f.edges}"


def test_focus_edge_fires_again_after_dropping_out():
    f = Feed(CFG)
    _ingest(f, [_row(ticker="SEED", signal_proximity=IDLE_SP)], TS)
    _ingest(f, [_row(signal_proximity=FOCUS_SP)], TS + 2.0)
    _ingest(f, [_row(signal_proximity=IDLE_SP)], TS + 4.0)
    _ingest(f, [_row(signal_proximity=FOCUS_SP)], TS + 6.0)
    assert [k for k, _ in f.edges] == ["focus"]


def test_burst_edge_fires_on_the_rising_edge_only():
    f = Feed(CFG)
    _ingest(f, [_row(ticker="SEED")], TS)
    _ingest(f, [_row(mention_burst=True)], TS + 2.0)
    assert "burst" in [k for k, _ in f.edges]
    _ingest(f, [_row(mention_burst=True)], TS + 4.0)
    assert "burst" not in [k for k, _ in f.edges]


def test_buy_edge_is_recorded_even_though_alert_buy_is_off():
    """The alert flags control the speaker, not the evidence. alert_buy
    defaults false, so hooking the journal to alerter.fire() call sites would
    silently never record a buy."""
    assert DEFAULTS["alert_buy"] is False
    # Shipped defaults: new audible, buy silent.
    cfg = {**DEFAULTS, "alert_new": True, "alert_buy": False}
    f = Feed(cfg)
    alerter = _NullAlerter()
    f.ingest({"tickers": [_row(ticker="SEED")]}, TS, alerter, cfg)
    f.ingest({"tickers": [_row(signal_proximity={"status": "buy_zone"})]},
             TS + 2.0, alerter, cfg)
    assert "buy" in [k for k, _ in f.edges]
    assert [k for k, _ in alerter.fired] == ["new"]     # buy stayed silent


def test_edges_are_cleared_between_polls():
    f = Feed(CFG)
    _ingest(f, [_row(ticker="SEED")], TS)
    _ingest(f, [_row()], TS + 2.0)
    assert f.edges
    _ingest(f, [_row()], TS + 4.0)
    assert f.edges == []


# ── buffering and flush ──────────────────────────────────────────────────────

def _journal(tmp_path, **kw):
    return Journal(tmp_path / "journal", **kw)


def test_records_are_buffered_until_flush(tmp_path):
    j = _journal(tmp_path, flush_sec=5.0)
    j.record("focus", "ABCD", _row(), TS)
    assert not (tmp_path / "journal").exists()
    assert j.flush() == 1
    assert (tmp_path / "journal" / "2025-07-25.jsonl").exists()


def test_flush_is_rate_limited(tmp_path):
    j = _journal(tmp_path, flush_sec=5.0)
    j.record("new", "ABCD", _row(), TS)
    assert j.maybe_flush(TS) == 1            # first flush: last_flush is 0
    j.record("new", "BBBB", _row(ticker="BBBB"), TS + 1)
    assert j.maybe_flush(TS + 1) == 0        # too soon
    assert j.maybe_flush(TS + 6) == 1        # window elapsed


def test_due_is_false_with_an_empty_buffer(tmp_path):
    j = _journal(tmp_path, flush_sec=0.0)
    assert j.due(TS) is False
    j.record("new", "ABCD", _row(), TS)
    assert j.due(TS) is True


def test_close_flushes_pending_records(tmp_path):
    j = _journal(tmp_path, flush_sec=999.0)
    j.record("focus", "ABCD", _row(), TS)
    assert j.close() == 1
    lines = (tmp_path / "journal" / "2025-07-25.jsonl").read_text().splitlines()
    assert len(lines) == 1


def test_one_file_per_session_date(tmp_path):
    j = _journal(tmp_path, flush_sec=999.0)
    j.record("new", "ABCD", _row(), TS)
    j.record("new", "BBBB", _row(ticker="BBBB"), TS + 86400)
    j.flush()
    names = sorted(p.name for p in (tmp_path / "journal").iterdir())
    assert names == ["2025-07-25.jsonl", "2025-07-26.jsonl"]


def test_appends_rather_than_truncating(tmp_path):
    j = _journal(tmp_path, flush_sec=999.0)
    for i in range(3):
        j.record("new", f"S{i}", _row(ticker=f"S{i}"), TS + i)
        j.flush()
    lines = (tmp_path / "journal" / "2025-07-25.jsonl").read_text().splitlines()
    assert len(lines) == 3


def test_disabled_journal_writes_nothing(tmp_path):
    j = _journal(tmp_path, flush_sec=0.0, enabled=False)
    j.record("focus", "ABCD", _row(), TS)
    assert j.flush() == 0
    assert not (tmp_path / "journal").exists()


# ── round trip ───────────────────────────────────────────────────────────────

def test_every_line_is_valid_json(tmp_path):
    j = _journal(tmp_path, flush_sec=999.0)
    rows = [_row(signal_proximity=FOCUS_SP),
            _row(ticker="BBBB", price=None, pct_change=None),
            {"ticker": "CCCC"}]
    for i, r in enumerate(rows):
        j.record("focus", r["ticker"], r, TS + i, rvol=6.2)
    j.flush()
    text = (tmp_path / "journal" / "2025-07-25.jsonl").read_text()
    recs = [json.loads(ln) for ln in text.splitlines()]
    assert len(recs) == 3
    assert [r["sym"] for r in recs] == ["ABCD", "BBBB", "CCCC"]
    assert recs[0]["cm_rsi"] == 22.0


def test_round_trip_preserves_absence(tmp_path):
    j = _journal(tmp_path, flush_sec=999.0)
    j.record("new", "ABCD", {"ticker": "ABCD"}, TS)
    j.flush()
    rec = json.loads(
        (tmp_path / "journal" / "2025-07-25.jsonl").read_text().splitlines()[0])
    assert rec.get("rvol") is None
    assert "rvol" not in rec


# ── failure containment ──────────────────────────────────────────────────────

def test_write_failure_does_not_raise_and_logs_once(tmp_path):
    """A read-only volume or a full disk must degrade one feature, not stop
    the desk."""
    logged = []
    target = tmp_path / "blocked"
    target.write_text("i am a file, not a directory")
    j = Journal(target / "journal", flush_sec=0.0, log=logged.append)
    for i in range(5):
        j.record("focus", "ABCD", _row(), TS + i)
        assert j.flush() == 0                # no exception escapes
    assert len(logged) == 1, logged
    assert j.dropped >= 5


def test_close_after_a_failure_still_does_not_raise(tmp_path):
    target = tmp_path / "blocked"
    target.write_text("not a directory")
    j = Journal(target / "journal", flush_sec=0.0)
    j.record("focus", "ABCD", _row(), TS)
    assert j.close() == 0


def test_a_malformed_row_does_not_break_recording(tmp_path):
    j = _journal(tmp_path, flush_sec=999.0)
    j.record("focus", "ABCD", {"ticker": "ABCD",
                               "signal_proximity": "not a dict"}, TS)
    assert j.flush() == 1


def test_buffer_is_bounded_so_a_failing_disk_cannot_leak(tmp_path):
    j = _journal(tmp_path, flush_sec=999.0)
    for i in range(MAX_BUFFER + 50):
        j.record("new", "ABCD", _row(), TS + i)
    assert len(j._buf) == MAX_BUFFER
    assert j.dropped == 50


def test_unserialisable_extra_does_not_lose_the_line(tmp_path):
    j = _journal(tmp_path, flush_sec=999.0)
    j.record("focus", "ABCD", _row(), TS, extra={"obj": object()})
    assert j.flush() == 1
    rec = json.loads(
        (tmp_path / "journal" / "2025-07-25.jsonl").read_text().splitlines()[0])
    assert isinstance(rec["obj"], str)     # coerced by default=str
