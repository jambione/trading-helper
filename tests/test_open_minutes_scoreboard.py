"""Open-minutes scoreboard: RTH occupancy KPIs + ghost filter."""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "tools"))

import open_minutes_scoreboard as oms  # noqa: E402

ET = ZoneInfo("America/New_York")


def _ts(day: str, h: int, m: int, s: int = 0) -> float:
    return datetime.fromisoformat(day).replace(
        hour=h, minute=m, second=s, microsecond=0, tzinfo=ET,
    ).timestamp()


def _write_shadow(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def test_ghost_filter_excludes_short_hold_without_price():
    # Brief: hold_sec≤TTL without last price → ghost.
    assert oms._is_ghost({"hold_sec": 12.0, "price": None}, 30.0) is True
    assert oms._is_ghost({"hold_sec": 12.0, "price": 10.5}, 30.0) is False
    # Long hold without price is not a TTL ghost (may be dark tape).
    assert oms._is_ghost({"hold_sec": 90.0, "price": None}, 30.0) is False
    assert oms._is_ghost({"hold_sec": 90.0, "price": 10.5}, 30.0) is False


def test_score_day_counts_open_minutes(tmp_path):
    day = "2026-09-17"
    shadow = tmp_path / "position_shadow.jsonl"
    # Minute 09:30: AAA real; ghost BBB (no price, short hold)
    # Minute 09:31: AAA + CCC → open≥2
    # Minute 09:32: empty (no rows) → zero
    rows = [
        {"ts": _ts(day, 9, 30, 5), "symbol": "AAA", "price": 10.0, "hold_sec": 60},
        {"ts": _ts(day, 9, 30, 8), "symbol": "BBB", "price": None, "hold_sec": 5},
        {"ts": _ts(day, 9, 31, 2), "symbol": "AAA", "price": 10.1, "hold_sec": 120},
        {"ts": _ts(day, 9, 31, 4), "symbol": "CCC", "price": 20.0, "hold_sec": 40},
    ]
    _write_shadow(shadow, rows)
    card = oms.score_day(
        day, shadow_path=shadow, ttl_sec=30.0, prefer_outcomes_if_thin=False)
    assert card["rth_minutes"] == 390
    assert card["shadow_meta"]["rows_ghost"] == 1
    assert card["shadow_meta"]["rows_real"] == 3
    # Only two minutes have opens in this fixture; rest are zeros.
    assert card["open_ge1"] == 2
    assert card["open_ge2"] == 1
    assert card["open_ge3"] == 0
    assert card["zero_minutes"] == 388
    assert card["pass_ge1"] is False


def test_write_outputs(tmp_path):
    day = "2026-09-16"
    shadow = tmp_path / "shadow.jsonl"
    # Fill every RTH minute with one symbol → pass.
    rows = []
    t0 = _ts(day, 9, 30)
    for i in range(390):
        rows.append({
            "ts": t0 + i * 60 + 1,
            "symbol": "AAA",
            "price": 11.0,
            "hold_sec": 100,
        })
    _write_shadow(shadow, rows)
    card = oms.score_day(
        day, shadow_path=shadow, ttl_sec=30.0, prefer_outcomes_if_thin=False)
    assert card["open_ge1_pct"] == 100.0
    assert card["zero_minutes"] == 0
    assert card["pass_ge1"] is True
    out = tmp_path / "bench"
    written = oms.write_outputs([card], out)
    assert any(p.name.endswith("_summary.md") for p in written)
    assert (out / f"{day}.json").exists()
