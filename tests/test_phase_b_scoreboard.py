"""Phase B scoreboard PASS / FAIL / NO DECISION fixtures."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "tools"))

import phase_b_scoreboard as sb  # noqa: E402

PASS_FIX = _ROOT / "tests" / "fixtures" / "phase_b_scoreboard_pass.json"
FAIL_FIX = _ROOT / "tests" / "fixtures" / "phase_b_scoreboard_fail.json"


def test_friction_constant_is_documented():
    assert sb.ROUND_TRIP_FRICTION_PCT == 0.50
    src = (_ROOT / "tools" / "phase_b_scoreboard.py").read_text(encoding="utf-8")
    assert "0.50" in src
    assert "voids" in src.lower() or "void" in src.lower()


def test_pass_fixture_passes():
    rows = sb.load_fixture(PASS_FIX)
    summary = sb.summarize(rows)
    v = sb.verdict(summary)
    assert v["decision"] == "PASS", v
    assert v["pass"] is True
    assert v["friction_pct"] == 0.50
    assert summary["n_fills"] >= 20


def test_fail_fixture_fails():
    rows = sb.load_fixture(FAIL_FIX)
    summary = sb.summarize(rows)
    v = sb.verdict(summary)
    assert v["decision"] == "FAIL", v
    assert v["pass"] is False
    assert "sod_wipe_phase_b_lot" in v["reasons"] or any(
        "fill_rate" in r or "slip" in r or "mfe" in r or "peak" in r
        for r in v["reasons"]
    )


def test_tiny_sample_is_no_decision():
    rows = [
        {"kind": "arm", "day": "2026-09-16", "symbol": "AAA"},
        {"kind": "entry_limit", "day": "2026-09-16", "symbol": "AAA"},
        {"kind": "fill", "day": "2026-09-16", "symbol": "AAA",
         "pl_pct": 1.0, "mfe_r": 0.5, "entry_slip_pct": 0.1},
        {"kind": "flat_on_time", "day": "2026-09-16", "symbol": "AAA"},
    ]
    v = sb.verdict(sb.summarize(rows))
    assert v["decision"] == "NO DECISION"


def test_cli_smoke_pass(capsys):
    code = sb.main(["--fixture", str(PASS_FIX)])
    out = capsys.readouterr().out
    assert "Phase B scoreboard" in out
    assert "PASS" in out
    assert code == 0


def test_cli_smoke_fail(capsys):
    code = sb.main(["--fixture", str(FAIL_FIX)])
    out = capsys.readouterr().out
    assert "FAIL" in out
    assert code == 1
