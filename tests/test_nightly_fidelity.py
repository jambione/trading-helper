"""The nightly report's replay-fidelity verdict."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import nightly_report as nr  # noqa: E402


def _write(tmp_path, monkeypatch, **f):
    monkeypatch.setenv("HOME", str(tmp_path))
    d = tmp_path / "session_snapshots" / "2026-09-25"
    d.mkdir(parents=True)
    if f:
        (d / "fidelity.json").write_text(json.dumps(f))


def test_drift_when_the_replay_invents_opens(tmp_path, monkeypatch):
    """2026-09-25: 56 replay opens vs 13 live, precision 0.14."""
    _write(tmp_path, monkeypatch, recall=0.615, precision=0.143, live_buys=13, replay_opens=56)
    assert nr.fidelity_verdict("2026-09-25", wait_sec=0).startswith("**DRIFT**")


def test_ok_when_both_sides_match(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, recall=0.8, precision=0.7)
    assert nr.fidelity_verdict("2026-09-25", wait_sec=0).startswith("**OK**")


def test_missing_says_so(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch)
    assert nr.fidelity_verdict("2026-09-25", wait_sec=0).startswith("**MISSING**")
