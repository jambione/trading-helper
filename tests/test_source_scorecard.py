"""source_scorecard — overlap views + episode collapse (no live bars)."""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "tools"))

import source_scorecard as ss


def test_overlap_views():
    eps = [
        {"day": "2026-09-16", "symbol": "AAA", "proposer_norm": "momentum", "ts": 100.0},
        {"day": "2026-09-16", "symbol": "AAA", "proposer_norm": "research:xai", "ts": 110.0},
        {"day": "2026-09-16", "symbol": "BBB", "proposer_norm": "trending", "ts": 120.0},
    ]
    assert len(ss.apply_overlap(eps, "all")) == 3
    excl = ss.apply_overlap(eps, "exclusive")
    assert len(excl) == 1
    assert excl[0]["symbol"] == "BBB"
    first = ss.apply_overlap(eps, "first")
    assert len(first) == 2
    aaa = next(e for e in first if e["symbol"] == "AAA")
    assert aaa["proposer_norm"] == "momentum"


def test_episodes_from_ledger_first_ts():
    rows = [
        {"symbol": "aaa", "proposer": "momentum", "proposer_norm": "momentum",
         "ts": 200.0, "stage": "inclusion", "decision": "kept"},
        {"symbol": "AAA", "proposer": "momentum", "proposer_norm": "momentum",
         "ts": 100.0, "stage": "seed", "decision": "dropped", "reason": "thin_rvol",
         "heartbeat": False},
        {"symbol": "AAA", "proposer": "momentum", "proposer_norm": "momentum",
         "ts": 400.0, "stage": "seed", "decision": "dropped", "reason": "thin_rvol",
         "heartbeat": True},
    ]
    eps = ss.episodes_from_ledger(rows, "2026-09-16")
    assert len(eps) == 1
    assert eps[0]["ts"] == 100.0
    assert eps[0]["symbol"] == "AAA"
