"""Prefer-band CHG% scoreboard (wave-capture §1)."""
from __future__ import annotations

import json
from pathlib import Path

import tools.prefer_band_scoreboard as pb


def _row(*, day: str, pct, mfe, pl, r, hold=120.0, slip=0.01):
    # entry_time noon ET that day
    from datetime import datetime
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
    ts = datetime.fromisoformat(f"{day}T12:00:00").replace(tzinfo=ET).timestamp()
    return {
        "symbol": "X",
        "entry_time": ts,
        "exit_time": ts + hold,
        "close_reason": "local_trail",
        "mfe_r": mfe,
        "realized_pl_usd": pl,
        "realized_r_multiple": r,
        "hold_sec": hold,
        "entry_slippage_r": slip,
        "features": {"pct_change": pct},
    }


def test_band_classify():
    assert pb._band(18.0, 8, 40, 50) == "prefer"
    assert pb._band(5.0, 8, 40, 50) == "below_prefer"
    assert pb._band(45.0, 8, 40, 50) == "mid"
    assert pb._band(60.0, 8, 40, 50) == "over_soft"
    assert pb._band(None, 8, 40, 50) == "unknown"


def test_score_day_prefer_vs_outside(tmp_path: Path):
    day = "2026-09-17"
    rows = [
        _row(day=day, pct=18.0, mfe=0.25, pl=2.0, r=0.3),   # prefer pays
        _row(day=day, pct=22.0, mfe=0.20, pl=1.0, r=0.2),   # prefer pays
        _row(day=day, pct=60.0, mfe=0.02, pl=-1.0, r=-0.2), # over_soft
        _row(day=day, pct=3.0, mfe=0.0, pl=-0.5, r=-0.1),   # below
    ]
    path = tmp_path / "outcomes.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    out = pb.score_day(
        day, outcomes_path=path,
        prefer_min=8.0, prefer_max=40.0, soft_max=50.0,
    )
    assert out["prefer"]["n"] == 2
    assert out["outside"]["n"] == 2
    assert out["over_soft"]["n"] == 1
    assert out["prefer"]["pct_mfe_ge_015"] == 1.0
    assert out["outside"]["pct_mfe_ge_015"] == 0.0
    assert out["pass_hint"] == "prefer_pays"


def test_write_day(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(pb, "OUT_DIR", tmp_path)
    day = "2026-09-17"
    payload = pb.score_day(
        day,
        outcomes_path=tmp_path / "empty.jsonl",
        prefer_min=8.0, prefer_max=40.0, soft_max=50.0,
    )
    (tmp_path / "empty.jsonl").write_text("", encoding="utf-8")
    payload = {
        "day": day,
        "bounds": {"prefer_min": 8, "prefer_max": 40, "soft_max": 50,
                   "mfe_pay_r": 0.15},
        "n_total": 0,
        "prefer": pb._bucket_stats([]),
        "outside": pb._bucket_stats([]),
        "over_soft": pb._bucket_stats([]),
        "pass_hint": "insufficient_n",
    }
    jp, mp = pb.write_day(payload)
    assert jp.exists() and mp.exists()
    assert "Prefer CHG" in mp.read_text(encoding="utf-8")
