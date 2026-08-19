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


# ── A failed fetch must not be cached as a day-long verdict ──────────────────

def test_failed_history_fetch_does_not_poison_the_day_cache(monkeypatch):
    """avg_session_volumes caches per ET day and skips anything already cached.
    fetch_minutes_history returns None when it could not ask at all (429,
    network, auth), as distinct from {} meaning "asked, nobody had history".
    Caching a negative for the first promotes one transient rejection into a
    whole session with no rvol — which is what happened on 2026-08-07 and
    emptied the rvol column on half the A/B reject arm.
    """
    import sys, os
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
    import morning_funnel as mf
    from datetime import datetime
    from zoneinfo import ZoneInfo

    mf._AVG_VOL_CACHE.clear()
    mf._AVG_VOL_DATE = ""
    mf._AVG_VOL_RETRY_AT = 0.0
    now_et = datetime.now(ZoneInfo("America/New_York"))

    monkeypatch.setattr(mf, "fetch_minutes_history", lambda *a, **k: None)
    out = mf.avg_session_volumes(object(), ["AAA", "BBB"], {}, now_et)
    assert out == {}, "a failed fetch must cache nothing"
    assert "AAA" not in mf._AVG_VOL_CACHE

    # Backed off, not blacklisted: after the retry floor the symbols are
    # asked for again. Retrying every cycle instead simply 429s forever.
    assert mf._AVG_VOL_RETRY_AT > 0
    mf._AVG_VOL_RETRY_AT = 0.0

    import pandas as pd
    monkeypatch.setattr(mf, "fetch_minutes_history",
                        lambda *a, **k: {"AAA": pd.DataFrame()})
    monkeypatch.setattr(mf, "avg_session_volume", lambda df, d, n: 1234.0)
    out = mf.avg_session_volumes(object(), ["AAA", "BBB"], {}, now_et)
    assert out.get("AAA") == 1234.0
    mf._AVG_VOL_CACHE.clear()
    mf._AVG_VOL_DATE = ""


def test_successful_fetch_still_caches_a_genuine_none(monkeypatch):
    """A symbol absent from a NON-empty result really has no usable history —
    that None is a real answer and must be cached, or a fresh listing is
    re-requested every 60s and never resolves."""
    import sys, os
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
    import morning_funnel as mf
    import pandas as pd
    from datetime import datetime
    from zoneinfo import ZoneInfo

    mf._AVG_VOL_CACHE.clear()
    mf._AVG_VOL_DATE = ""
    mf._AVG_VOL_RETRY_AT = 0.0
    now_et = datetime.now(ZoneInfo("America/New_York"))
    monkeypatch.setattr(mf, "fetch_minutes_history",
                        lambda *a, **k: {"AAA": pd.DataFrame()})
    monkeypatch.setattr(mf, "avg_session_volume",
                        lambda df, d, n: 999.0 if df is not None else None)
    out = mf.avg_session_volumes(object(), ["AAA", "NEWIPO"], {}, now_et)
    assert out["AAA"] == 999.0
    assert "NEWIPO" in mf._AVG_VOL_CACHE and out["NEWIPO"] is None
    mf._AVG_VOL_CACHE.clear()
    mf._AVG_VOL_DATE = ""


# ── the buy rule's own inputs have to be on the row that scores it ───────
#
# Fourth gap, same shape, found 2026-08-19. The desk switched to
# "both %R lines then CM RSI-2" (454ea10) and the shadow row kept recording
# only the fast %R line plus booleans. rte_threshold, rte_confluence_max and
# cm_rsi_buy_max therefore had no recorded input between them, so the three
# thresholds that decide every entry could not be swept at any sample size.

def _rec_with_indicator(**ind):
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


def test_shadow_row_carries_both_pctr_lines_and_the_raw_rsi():
    row = _row(_rec_with_indicator(
        pctr=-12.0, pctr_slow=-18.0, pctr_gap=6.0,
        pctr_ob=True, pctr_tight=True, cm_rsi=7.5,
    ))
    # Levels, not verdicts: cm_ok tells you the gate passed, never what it
    # would have done at a different cutoff.
    assert row["pctr"] == -12.0
    assert row["pctr_slow"] == -18.0
    assert row["pctr_gap"] == 6.0
    assert row["cm_rsi"] == 7.5
    assert row["pctr_ob"] is True
    assert row["pctr_tight"] is True


def test_shadow_row_records_a_missing_rsi_rather_than_omitting_it():
    """cm_rsi None is the reason _tv_exh_rsi_allows_buy refuses with wait_rsi.

    A session that records no cm_rsi is a session the desk could not have
    bought in, so the None is the observation — not something to drop.
    """
    row = _row(_rec_with_indicator(pctr=-12.0))
    assert "cm_rsi" in row and row["cm_rsi"] is None
    assert "pctr_slow" in row and row["pctr_slow"] is None


def test_instrumentation_check_watches_the_buy_rule_inputs():
    import instrumentation_check as ic
    for field in ("pctr", "pctr_slow", "cm_rsi"):
        assert field in ic.DECISION_FIELDS["shadow"], field


# ── the spread gate's own input has to be on disk before it can be set ───
#
# ai_max_spread_r is the one spread gate wired into the fill path, and it is 0
# because nothing recorded what crossing costs. pre_entry_gate's own comment
# says to turn it on "once the server's realized entry_slippage_r says what
# crossing actually costs" — but no bid was ever written, so the round-trip
# figure the gate enforces could not be computed from history at all.

def test_spread_r_matches_the_gate_arithmetic():
    # ask 10.00, bid 9.98, stop 9.50 -> risk 0.50, round trip 2 x 0.02 = 0.04
    assert ew._spread_r(10.00, 9.98, 9.50) == 0.08


def test_spread_r_is_none_rather_than_zero_when_unknowable():
    assert ew._spread_r(10.0, None, 9.5) is None
    assert ew._spread_r(None, 9.98, 9.5) is None
    assert ew._spread_r(10.0, 9.98, None) is None
    # A stop at or above the ask is not a risk unit.
    assert ew._spread_r(10.0, 9.98, 10.0) is None


def test_shadow_row_records_bid_and_spread_r():
    rec = {"symbol": "AAA", "status": "watching",
           "structure": {"entry_low": 9.9, "entry_high": 10.1,
                         "stop_price": 9.50, "target_1": 11.0},
           "indicator": {}}
    row = ew._shadow_row(rec, price=10.00, price_src="quote", bid=9.98,
                         arm_ok=True, arm_why="last_heating", now=1_000_000.0)
    assert row["bid"] == 9.98
    assert row["spread_r"] == 0.08


def test_shadow_row_keeps_the_columns_when_there_is_no_bid():
    rec = {"symbol": "AAA", "status": "watching",
           "structure": {"stop_price": 9.50}, "indicator": {}}
    row = ew._shadow_row(rec, price=10.00, price_src="quote",
                         arm_ok=False, arm_why="wait_exh", now=1_000_000.0)
    assert "bid" in row and row["bid"] is None
    assert "spread_r" in row and row["spread_r"] is None
