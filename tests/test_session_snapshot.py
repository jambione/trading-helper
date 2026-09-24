"""tools/session_snapshot — record format, trimming and the 16:05 close-out."""
import gzip
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import session_snapshot as ss  # noqa: E402


def test_trim_drops_unused_parts_and_duplicate_indicators():
    data = {"tickers": [{"ticker": "PFE", "price": 28.6, "price_age_sec": 1.2,
                         "signal_proximity": {"pctr": -52}}],
            "news": [1], "config": {"k": 1}, "grok_suggestions": {"rows": []}}
    out = ss._trim_api(data)
    assert out["tickers"] == [{"ticker": "PFE", "price": 28.6, "price_age_sec": 1.2}]
    assert "news" not in out and "config" not in out
    assert "grok_suggestions" in out  # research boards stay


def test_writer_appends_readable_members(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "OUT_BASE", tmp_path)
    w = ss.DayWriter("2026-09-25")
    for i in range(3):
        w.add({"ts": float(i), "file": "signal_state.json", "mtime": 0.0,
               "sha": str(i), "data": {"n": i}})
        w.flush(force=True)
    with gzip.open(w.path, "rt") as f:
        rows = [json.loads(line) for line in f]
    assert [r["data"]["n"] for r in rows] == [0, 1, 2]
    assert set(rows[0]) == {"ts", "file", "mtime", "sha", "data"}  # rehearse_snap format


def test_finish_day_links_ledgers_and_recorder(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "ai_reports" / "fills").mkdir(parents=True)
    (root / "ai_reports" / "fills" / "2026-09-25.jsonl").write_text('{"x":1}\n')
    (root / "ai_reports" / "fills" / "2026-09-24.jsonl").write_text('{"old":1}\n')
    rec = root / "ai_reports" / "sessions" / "2026-09-25"
    rec.mkdir(parents=True)
    (rec / "sources.jsonl.gz").write_bytes(gzip.compress(b'{"s":1}\n'))
    monkeypatch.setattr(ss, "ROOT", root)
    monkeypatch.setattr(ss, "OUT_BASE", tmp_path / "snaps")
    w = ss.DayWriter("2026-09-25")
    w.add({"ts": 1.0, "file": "api/state", "mtime": 1.0, "sha": "a", "data": {}})
    ss.finish_day(w)
    day = tmp_path / "snaps" / "2026-09-25"
    assert (day / "fills.jsonl").read_text() == '{"x":1}\n'
    assert os.stat(day / "fills.jsonl").st_ino == os.stat(
        root / "ai_reports" / "fills" / "2026-09-25.jsonl").st_ino  # hard link
    assert (day / "recorder_sources.jsonl.gz").exists()
    assert "done 1 records" in (day / "DONE").read_text()
    with gzip.open(day / "state_snapshots.jsonl.gz", "rt") as f:
        assert json.loads(f.readline())["file"] == "api/state"


def test_close_out_starts_the_fidelity_replay_only_when_asked(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(ss, "ROOT", tmp_path / "repo")
    monkeypatch.setattr(ss, "OUT_BASE", tmp_path / "snaps")
    monkeypatch.setattr(ss.subprocess, "Popen", lambda cmd, **kw: calls.append(cmd))
    ss.finish_day(ss.DayWriter("2026-09-25"))
    assert calls == []
    ss.finish_day(ss.DayWriter("2026-09-26"), fidelity=True)
    assert len(calls) == 1
    assert calls[0][-7:] == ["--day", "2026-09-26", "--start", "09:00",
                             "--end", "15:50", "--fidelity"]
