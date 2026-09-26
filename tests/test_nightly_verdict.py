"""The nightly replay check: SKIP on no session, one verdict word, segments
around restarts, and the export overlay whose absence crashed 2026-09-26."""
import gzip
import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)

import nightly  # noqa: E402
import replay_session as rs  # noqa: E402

ET = ZoneInfo("America/New_York")


def _ts(day, hh, mm):
    return datetime.strptime(day, "%Y-%m-%d").replace(hour=hh, minute=mm, tzinfo=ET).timestamp()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(nightly, "ROOT", tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return tmp_path


def _decisions(repo, day, rows):
    d = repo / "ai_reports" / "sessions" / day
    d.mkdir(parents=True, exist_ok=True)
    with gzip.open(d / "decisions.jsonl.gz", "wt") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def test_weekend_is_skip(repo):
    assert nightly.session_recorded("2026-09-26") == (False, "weekend")


def test_weekday_with_polls_is_a_session(repo):
    _decisions(repo, "2026-09-28", [{"ts": _ts("2026-09-28", 10, 0), "ev": "poll"}])
    assert nightly.session_recorded("2026-09-28") == (True, "")


def test_weekday_without_rth_polls_is_skip(repo):
    _decisions(repo, "2026-09-28", [{"ts": _ts("2026-09-28", 8, 0), "ev": "poll"},
                                    {"ts": _ts("2026-09-28", 10, 0), "ev": "sync"}])
    ok, why = nightly.session_recorded("2026-09-28")
    assert not ok and "holiday" in why


def test_pre_recorder_day_with_fills_is_legacy(repo):
    snap = repo / "home" / "session_snapshots" / "2026-09-25"
    snap.mkdir(parents=True)
    (snap / "fills.jsonl").write_text('{"event": "fill"}\n')
    assert nightly.session_recorded("2026-09-25") == (True, "legacy")


def test_verdict_words():
    assert nightly.verdict({"skip": "weekend"}) == ("SKIP", "weekend")
    assert nightly.verdict({"exact": {"ok": True, "verdict": "PASS"}})[0] == "PASS"
    drift = {"exact": {"ok": True, "verdict": "FAIL", "misses": 0, "errors": {},
                       "decision_agreement": 0.9, "buy_recall": 0.5}}
    assert nightly.verdict(drift)[0] == "DRIFT"
    broken = {"exact": {"ok": True, "verdict": "FAIL", "misses": 12, "errors": {}}}
    assert nightly.verdict(broken)[0] == "FAIL"
    assert nightly.verdict({"exact": {"ok": False, "why": "no boot"}})[0] == "FAIL"
    legacy = {"legacy": True, "fidelity_json": {"recall": 0.62, "precision": 0.2}}
    assert nightly.verdict(legacy)[0] == "DRIFT"


def test_overlay_replaces_an_old_desk_io(tmp_path):
    """Regression for 2026-09-26: an export of 7dc1682 kept that commit's
    desk_io.py, which could not read a list of wire files (TypeError)."""
    here = tmp_path / "here"
    (here / "tools").mkdir(parents=True)
    (here / "tools" / "replay_session.py").write_text("# driver\n")
    (here / "desk_io.py").write_text("# current desk_io\n")
    code = tmp_path / "code"
    code.mkdir()
    (code / "desk_io.py").write_text("# old desk_io\n")
    rs.overlay_harness(code, here=here)
    assert (code / "desk_io.py").read_text() == "# current desk_io\n"
    assert (code / "tools" / "replay_session.py").read_text() == "# driver\n"


def test_desk_io_recording_takes_a_list(tmp_path):
    import desk_io
    rec = desk_io.Recording([tmp_path / "a.jsonl.gz", tmp_path / "b.jsonl.gz"])
    assert rec.alpaca == {}


def test_live_segments_split_at_restarts(tmp_path):
    day = "2026-09-25"
    snap = tmp_path / "snap"
    snap.mkdir()
    rows = [(10, 39, "aaa"), (10, 50, "bbb"), (11, 5, "bbb"), (11, 52, "ccc")]
    with gzip.open(snap / "recorder_config.jsonl.gz", "wt") as f:
        for hh, mm, sha in rows:
            f.write(json.dumps({"ts": _ts(day, hh, mm), "git_sha": sha}) + "\n")
    segs = rs.live_segments(day, snap, "09:30", "16:00")
    assert [(s["sha"], s["from"], s["to"]) for s in segs] == [
        ("aaa", "10:39", "10:50"), ("bbb", "10:50", "11:52"), ("ccc", "11:52", "16:00")]


def test_exact_score_reports_the_quote_path(tmp_path):
    wire = tmp_path / "wire.jsonl.gz"
    with gzip.open(wire, "wt") as f:
        f.write("")
    events = [(100.0, "poll")]
    live = {100.0: [{"s": "AAA", "st": "watching", "b": None, "src": "quote"},
                    {"s": "BBB", "st": "watching", "b": "stale_quote", "src": "stale_tape"}]}
    rep = {100.0: [{"s": "AAA", "st": "watching", "b": None, "src": "quote"},
                   {"s": "BBB", "st": "watching", "b": "stale_quote", "src": "stale_tape"}]}
    o = rs.exact_score(events, live, rep, [], wire, 0.0, 1e9)
    assert o["live_price_src"] == {"quote": 1, "stale_tape": 1}
    assert (o["quote_checks"], o["quote_agree"]) == (1, 1)


def test_weekday_without_any_recording_says_so(repo):
    ok, why = nightly.session_recorded("2026-09-22")
    assert not ok
    assert why.startswith("no session recording for this day")


def test_not_run_reason_is_never_none():
    # --only fidelity on a pre-recorder day: no "exact" key at all (the 9/25 case)
    legacy = {"legacy": True, "fidelity_json": {"recall": 0.5, "precision": 0.2}}
    assert nightly.exact_not_run_reason(legacy) == nightly.LEGACY_WHY
    md = nightly.summary_md("2026-09-25", legacy)
    assert "**Exact replay: not run** — day predates the desk_io recorder" in md
    assert "None" not in md

    # recorded day, exact not requested
    only_fid = {"fidelity_json": {"recall": 0.9, "precision": 0.9}}
    v, why = nightly.verdict(only_fid)
    assert v == "FAIL" and "None" not in why and "not requested" in why
    assert "None" not in nightly.summary_md("2026-09-28", only_fid)

    # a literal None / "None" reason falls back to words
    for bad in (None, "", "None"):
        r = {"exact": {"ok": False, "why": bad}}
        assert nightly.exact_not_run_reason(r) == "no reason recorded"
        assert "None" not in nightly.verdict(r)[1]

    # a real reason passes through
    r = {"exact": {"ok": False, "why": "no desk_io boot marker before 09:30"}}
    assert "no desk_io boot marker" in nightly.summary_md("2026-09-28", r)


def test_skip_summary_for_unrecorded_weekday(repo):
    _, why = nightly.session_recorded("2026-09-22")
    res = {"skip": why, "exact": {"ok": True, "verdict": "SKIP", "why": why}}
    md = nightly.summary_md("2026-09-22", res)
    assert "VERDICT: SKIP — no session recording for this day" in md
    assert "None" not in md
    assert md.count("no session recording for this day") == 1
    assert "((" not in md
