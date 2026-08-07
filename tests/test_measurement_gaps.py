"""The measurement is the product on a day with no fills — these guard it.

Three gaps found on 2026-08-07, all of the same shape: a record that looked
present and was not.

  • look_reason was written on every row and logged as null on every row,
    because apply_look_highlights writes "" for untagged names and both
    writers used `or None`. The completeness report therefore said "never
    observed" forever, and the EXT gate — the one change that day — could not
    be measured against the feature it gates.

  • the scorecard compared 08-06 against 08-05, a day before shadow.jsonl and
    rejects.jsonl existed, read the missing files as zero, and printed BETTER
    for the logger starting up.

  • reconcile_unmanaged logged 384 rows for one unchanging fact, burying the
    27 rows that explained the session.
"""

import json
import sys
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "tools"))

import ai_entry_watch as ew  # noqa: E402
import ai_positions as cp  # noqa: E402
import desk_report as dr  # noqa: E402


# ── look_reason is a value, not a truthiness test ────────────────────────

def test_untagged_look_reason_records_as_none_not_missing():
    # The producer ran and did not tag this row. That is an observation.
    assert ew._look_reason_value({"look_reason": ""}) == "NONE"


def test_absent_look_reason_stays_missing():
    # The producer never wrote the field. That is NOT the same as untagged,
    # and collapsing the two is what made the feature unmeasurable.
    assert ew._look_reason_value({}) is None


def test_tagged_look_reason_is_carried_verbatim():
    assert ew._look_reason_value({"look_reason": "EXT"}) == "EXT"
    assert ew._look_reason_value({"look_reason": "wash"}) == "WASH"


def test_admission_provenance_is_sticky_across_an_untagged_refresh():
    # A name let on because it was EXT keeps that reason when a later poll
    # arrives untagged — the record is why it was ADMITTED, not what is true
    # now, the same way admit_ts keeps the moment rather than the latest tick.
    assert ew._look_reason_value({"look_reason": ""}, "EXT") == "EXT"
    # …but a prior "NONE" is not a tag and must not shadow a real one.
    assert ew._look_reason_value({"look_reason": "EXT"}, "NONE") == "EXT"


def test_reject_arm_logs_untagged_look_reason(monkeypatch):
    written: list[dict] = []
    monkeypatch.setattr(cp, "log_reject_sample", lambda r: written.append(r))
    ew._reject_last_logged.clear()
    by_symbol = {"AAA": {"symbol": "AAA", "source": "trending", "price": 10.0,
                         "score": 11.0, "look_reason": ""}}
    ew._log_rejects([{"symbol": "AAA", "reason": "not_ext"}],
                    by_symbol, {}, now=1000.0)
    # Rejected FOR not being EXT, so the feature must be present to prove it.
    assert written[0]["look_reason"] == "NONE"


# ── steady-state events log on transition ────────────────────────────────

def _capture_events(monkeypatch):
    """Collect rows, mirroring log_event: None fields are dropped, row returned."""
    rows: list[dict] = []

    def _fake(kind, **f):
        row = {"kind": kind, **{k: v for k, v in f.items() if v is not None}}
        rows.append(row)
        return row

    monkeypatch.setattr(cp, "log_event", _fake)
    return rows


def test_unchanged_condition_is_folded_into_a_repeat_count(monkeypatch):
    rows = _capture_events(monkeypatch)
    cp.clear_state_event("k")
    assert cp.log_state_event("k", ["CELH"], symbols=["CELH"]) is not None
    for _ in range(50):
        assert cp.log_state_event("k", ["CELH"], symbols=["CELH"]) is None
    assert len(rows) == 1
    # First write carries no fold count — nothing was suppressed before it.
    assert "folded" not in rows[0]


def test_changed_condition_logs_and_reports_what_it_folded(monkeypatch):
    rows = _capture_events(monkeypatch)
    cp.clear_state_event("k")
    cp.log_state_event("k", ["CELH"], symbols=["CELH"])
    for _ in range(9):
        cp.log_state_event("k", ["CELH"], symbols=["CELH"])
    cp.log_state_event("k", ["CELH", "SOUN"], symbols=["CELH", "SOUN"])
    assert len(rows) == 2
    # The 9 suppressed polls are compressed, not lost.
    assert rows[1]["folded"] == 9
    assert rows[1]["symbols"] == ["CELH", "SOUN"]


def test_scopes_do_not_evict_each_other(monkeypatch):
    # synth_zone interleaves symbols. Unscoped, A-B-A reads as three changes
    # and nothing is ever suppressed — worse than no dedupe at all.
    rows = _capture_events(monkeypatch)
    for sym in ("AAA", "BBB"):
        cp.clear_state_event("synth_zone", sym)
    for _ in range(5):
        cp.log_state_event("synth_zone", (1.0, 2.0), scope="AAA", symbol="AAA")
        cp.log_state_event("synth_zone", (3.0, 4.0), scope="BBB", symbol="BBB")
    assert len(rows) == 2, "one row per symbol per distinct zone"
    # A genuinely new zone for one symbol still gets written.
    cp.log_state_event("synth_zone", (1.5, 2.5), scope="AAA", symbol="AAA")
    assert len(rows) == 3 and rows[2]["symbol"] == "AAA"


def test_a_cleared_condition_logs_again_when_it_returns(monkeypatch):
    rows = _capture_events(monkeypatch)
    cp.clear_state_event("k")
    cp.log_state_event("k", ["CELH"], symbols=["CELH"])
    cp.clear_state_event("k")            # position got its stop
    cp.log_state_event("k", ["CELH"], symbols=["CELH"])   # …and lost it again
    assert len(rows) == 2, "a recurrence is the event worth seeing"


# ── the scorecard refuses to score an unrecorded day ─────────────────────

def _write(tmp_path, name, ts_days):
    p = tmp_path / name
    p.write_text("".join(
        json.dumps({"ts": d, "symbol": "AAA"}) + "\n" for d in ts_days),
        encoding="utf-8")
    return p


def test_metric_is_none_when_its_source_was_not_recording():
    report = {"funnel": {"zoned": 0, "zone_touched": 0, "armed": 0,
                         "filled": 0, "closed_with_outcome": 0,
                         "symbols_admitted": 0, "symbols_rejected": 0},
              "execution": {}, "instrumented": {"shadow": False,
                                                "rejects": False,
                                                "events": True,
                                                "outcomes": True,
                                                "tradelog": True}}
    m = dr.scorecard_metrics(report)
    assert m["zoned"] is None and m["armed"] is None
    assert m["symbols_admitted"] is None and m["symbols_rejected"] is None
    # Sources that WERE recording still answer — a real zero stays zero.
    assert m["filled"] == 0
    assert m["order_errors"] == 0


def test_first_day_finds_when_a_log_started(tmp_path, monkeypatch):
    import datetime as _dt
    d6 = _dt.datetime(2026, 8, 6, 10, 0).timestamp()
    d7 = _dt.datetime(2026, 8, 7, 10, 0).timestamp()
    p = _write(tmp_path, "shadow.jsonl", [d7, d6, d7])
    dr._first_day.cache_clear()
    assert dr._first_day(p) == date(2026, 8, 6)


def test_a_day_before_the_log_existed_is_not_instrumented(tmp_path,
                                                          monkeypatch):
    import datetime as _dt
    d6 = _dt.datetime(2026, 8, 6, 10, 0).timestamp()
    p = _write(tmp_path, "shadow.jsonl", [d6])
    dr._first_day.cache_clear()
    monkeypatch.setattr(dr, "SHADOW", p)
    monkeypatch.setattr(dr, "REJECTS", tmp_path / "absent.jsonl")
    monkeypatch.setattr(dr, "EVENTS", tmp_path / "absent.jsonl")
    monkeypatch.setattr(dr, "OUTCOMES", tmp_path / "absent.jsonl")
    monkeypatch.setattr(dr, "TRADELOG", tmp_path / "absent.json")

    assert dr._instrumented(date(2026, 8, 5))["shadow"] is False
    assert dr._instrumented(date(2026, 8, 6))["shadow"] is True
    # A log that does not exist at all was never recording on any day.
    assert dr._instrumented(date(2026, 8, 6))["rejects"] is False
