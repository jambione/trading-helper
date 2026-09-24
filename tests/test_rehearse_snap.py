"""Snapshot-archive replay harness + concurrency pass bars."""
import gzip
import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "tools"))
import rehearse_snap as rs  # noqa: E402
import rehearse_open as ro  # noqa: E402

ET = ZoneInfo("America/New_York")


def _ts(hhmm, s=0, day="2026-09-24"):
    h, m = map(int, hhmm.split(":"))
    y, mo, d = map(int, day.split("-"))
    return datetime(y, mo, d, h, m, s, tzinfo=ET).timestamp()


def _write_gz(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with gzip.open(path, "wt") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def test_index_and_replay_book_concurrency(tmp_path):
    snap = tmp_path / "2026-09-24"
    snap.mkdir()
    rows = []
    # Two slots: 10:00 book of 3 with mixed blocks; 1 open.
    # 10:10 book of 2; 2 open.
    for hhmm, book, n_open in (
        ("10:00", {"AAA": {"block_code": "wait_mid_rise"},
                   "BBB": {"block_code": "spread_wide"},
                   "CCC": {"block_code": "stale_quote"}}, 1),
        ("10:10", {"AAA": {"block_code": "wait_mid_rise"},
                   "DDD": {"arm_ok": True, "status": "armed"}}, 2),
    ):
        ts = _ts(hhmm)
        rows.append({"ts": ts, "file": "ai_reports/entry_watch_state.json",
                     "mtime": ts, "sha": "x", "data": book})
        rows.append({"ts": ts, "file": "ai_positions_state.json",
                     "mtime": ts, "sha": "x",
                     "data": {"n_open": n_open, "positions": {}}})
        rows.append({"ts": ts, "file": "ai_reports/admit_funnel.json",
                     "mtime": ts, "sha": "x",
                     "data": {"n_candidates": 10, "kept_symbols": list(book)}})
    _write_gz(str(snap / "state_snapshots.jsonl.gz"), rows)

    res = rs.replay_snapshots(str(snap), "2026-09-24", "10:00", "10:20", 10)
    assert len(res["rows"]) == 2
    assert res["rows"][0]["book"] == 3
    assert res["rows"][0]["armable"] == 1
    assert res["rows"][0]["gate"] == 1
    assert res["rows"][0]["data"] == 1
    assert res["rows"][1]["armable"] >= 1
    c = res["concurrency"]
    assert c["max_open"] >= 2
    assert c["pct_ge1"] is not None and c["pct_ge1"] > 0
    crit = dict((n, ok) for n, ok, _ in rs.criteria_with_concurrency(res))
    assert ">=1 open for >=80% of session" in crit


def test_simulate_one_clock_clears_stale_when_engine_young(tmp_path):
    snap = tmp_path / "2026-09-24"
    snap.mkdir()
    ts = _ts("10:00")
    rows = [
        {"ts": ts, "file": "ai_reports/entry_watch_state.json", "mtime": ts, "sha": "x",
         "data": {"CDNA": {"block_code": "stale_quote", "symbol": "CDNA"}}},
        {"ts": ts, "file": "signal_state.json", "mtime": ts, "sha": "x",
         "data": {"tickers": {"CDNA": {"rt_price": 41.0, "rt_price_age_sec": 3.0}}}},
        {"ts": ts, "file": "ai_positions_state.json", "mtime": ts, "sha": "x",
         "data": {"n_open": 0, "positions": {}}},
    ]
    _write_gz(str(snap / "state_snapshots.jsonl.gz"), rows)
    base = rs.replay_snapshots(str(snap), "2026-09-24", "10:00", "10:10", 10)
    assert base["rows"][0]["data"] == 1
    sim = rs.replay_snapshots(str(snap), "2026-09-24", "10:00", "10:10", 10,
                              simulate_one_clock=True)
    assert sim["rows"][0]["data"] == 0
    assert sim["rows"][0]["armable"] == 1


def test_concurrency_from_fills(tmp_path):
    fills = tmp_path / "fills.jsonl"
    with open(fills, "w") as f:
        for e in (
            {"ts": _ts("10:00"), "event": "fill", "side": "buy", "symbol": "AAA"},
            {"ts": _ts("10:05"), "event": "fill", "side": "buy", "symbol": "BBB"},
            {"ts": _ts("10:15"), "event": "fill", "side": "sell", "symbol": "AAA"},
        ):
            f.write(json.dumps(e) + "\n")
    c = rs.concurrency_from_fills(str(fills), "10:00", "10:20", "2026-09-24")
    assert c["max_open"] == 2
    assert c["pct_ge1"] > 0.5
    assert c["pct_ge2"] > 0.0


def test_rehearse_open_criteria_includes_cadence_when_events(tmp_path):
    rep = str(tmp_path)
    os.makedirs(os.path.join(rep, "decision_ledger"), exist_ok=True)
    os.makedirs(os.path.join(rep, "admit_ledger"), exist_ok=True)
    with open(os.path.join(rep, "events.jsonl"), "w") as f:
        f.write(json.dumps({
            "ts": _ts("09:40"), "kind": "admit_funnel",
            "n_candidates": 12, "kept_symbols": ["A"] * 10,
        }) + "\n")
        f.write(json.dumps({
            "ts": _ts("09:41"), "kind": "entry_ok", "symbol": "A",
        }) + "\n")
    with open(os.path.join(rep, "decision_ledger", "2026-09-24.jsonl"), "w") as f:
        for i, why in enumerate(["wait_mid_rise"] * 6 + ["stale_quote"] * 4):
            f.write(json.dumps({
                "ts": _ts("09:40", i), "stage": "arm", "symbol": f"S{i}",
                "arm_why": why, "arm_ok": False, "tape_age_sec": 3.0,
            }) + "\n")
    open(os.path.join(rep, "admit_ledger", "2026-09-24.jsonl"), "w").close()
    res = ro.replay("2026-09-24", "09:40", "09:50", 5, rep)
    names = [n for n, _, _ in ro.criteria(res)]
    assert ">=1 open per 10 min" in names
