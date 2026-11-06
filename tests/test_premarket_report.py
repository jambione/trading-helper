"""Premarket report funnel: counts, feed-caused share, dry/live mode, and the frozen Hybrid C verdict passthrough."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import premarket_report as pr  # noqa: E402

ROWS = [
    {"kind": "config_fingerprint", "fingerprint": {"ai_phase_b_dry_run": True}},
    {"kind": "admit", "symbol": "ARQ"}, {"kind": "admit", "symbol": "ARQ"}, {"kind": "admit", "symbol": "NKE"},
    {"kind": "arm_refuse", "symbol": "ARQ", "reason": "phase_b_missing_print"},
    {"kind": "arm_refuse", "symbol": "ARQ", "reason": "phase_b_stale_print"},
    {"kind": "arm_refuse", "symbol": "NKE", "reason": "exh_falling"},
    {"kind": "admit_refuse", "symbol": "BMNR", "reason": "phase_b_seats_full"},
    {"kind": "arm", "symbol": "NKE"}, {"kind": "entry_limit", "symbol": "NKE", "dry_run": True},
]


def test_funnel_counts_and_feed_share():
    f = pr.funnel(ROWS)
    assert (f["names"], f["admits"], f["arm_refusals"], f["arms"], f["limits"], f["fills"]) == (2, 3, 3, 1, 1, 0)
    assert abs(f["feed_share"] - 2 / 3) < 1e-9 and f["dry_run"] is True and pr.mode_text(f) == "DRY RUN"


def test_live_mode_and_pl_from_exits():
    rows = [{"kind": "config_fingerprint", "fingerprint": {"ai_phase_b_dry_run": False}},
            {"kind": "exit", "pl_pct": 0.4}, {"kind": "exit", "realized_pct": -0.1}]
    f = pr.funnel(rows)
    assert pr.mode_text(f) == "LIVE (paper)" and f["net_pl_pct"] == 0.3


def test_empty_morning_is_not_logged_and_ping_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "LEDGER", tmp_path)
    assert pr.funnel([])["logged"] is False
    assert "NOT LOGGED" in pr.ping_text("2026-11-09")


def test_report_reads_the_ledger_and_the_frozen_verdict(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "LEDGER", tmp_path)
    (tmp_path / "2026-11-05.jsonl").write_text("\n".join(json.dumps(r) for r in ROWS))
    text = pr.full(5)
    assert "NO DECISION" in text and "| 2026-11-05 | DRY RUN | 2 | 3 | 66.7% |" in text
    assert "Premarket 2026-11-05 [DRY RUN]: 2 names, 1 arms" in pr.ping_text("2026-11-05")
