"""Stage-2 entry universe: first tick that is setup AND timed."""
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

ss = pytest.importorskip("setup_screen")


def test_load_setup_entry_takes_the_first_timed_tick(tmp_path, monkeypatch):
    day_ts = 1_787_000_000.0  # aligned enough for DS._day_of
    rows = [
        {"ts": day_ts, "symbol": "CDTG", "setup_ok": True,
         "setup_entry_ok": False},
        {"ts": day_ts + 120, "symbol": "CDTG", "setup_ok": True,
         "setup_entry_ok": True},
        {"ts": day_ts + 240, "symbol": "CDTG", "setup_ok": True,
         "setup_entry_ok": True},
        {"ts": day_ts + 60, "symbol": "NOPE", "setup_ok": False,
         "setup_entry_ok": True},
    ]
    log = tmp_path / "shadow.jsonl"
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    def fake_iter(name, days):
        assert name == "shadow.jsonl"
        for line in log.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            yield float(r["ts"]), r

    monkeypatch.setattr(ss.US, "_iter_log", fake_iter)
    monkeypatch.setattr(ss.US, "_clamp_to_rth", lambda ts, day: float(ts))
    got = ss.load_setup_entry(days=20, max_shares_m=10.0)
    days = list(got.values())
    assert len(days) == 1
    members = days[0]
    assert "CDTG" in members
    assert "NOPE" not in members
    assert members["CDTG"] == pytest.approx(day_ts + 120)


def test_unknown_timing_does_not_qualify(tmp_path, monkeypatch):
    day_ts = 1_787_000_000.0
    rows = [{"ts": day_ts, "symbol": "PCLA", "setup_ok": True,
             "pctr_rising": True, "pctr_slow_rising": None, "cm_rsi": 10.0,
             "cm_rsi_rising": True}]
    log = tmp_path / "shadow.jsonl"
    log.write_text(json.dumps(rows[0]) + "\n", encoding="utf-8")

    def fake_iter(name, days):
        r = json.loads(log.read_text(encoding="utf-8"))
        yield float(r["ts"]), r

    monkeypatch.setattr(ss.US, "_iter_log", fake_iter)
    got = ss.load_setup_entry(days=20, max_shares_m=10.0)
    assert got == {}
