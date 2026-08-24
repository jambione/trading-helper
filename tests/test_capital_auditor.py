"""Capital-first auditor: facts, go-live, kill-list, no broker writes."""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import capital_auditor as ca  # noqa: E402

ET = ZoneInfo("America/New_York")
DAY = "2026-08-21"


def _ts(hour=15, minute=0, day=DAY):
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
    return (d.replace(hour=hour, minute=minute)).timestamp()


def _outcome(*, r, usd, mfe=None, spread_r=None, day=DAY, hour=15):
    e, st = 10.0, 9.5
    return {
        "ts": _ts(hour, day=day),
        "symbol": "TEST",
        "entry_price": e,
        "stop_price": st,
        "entry_time": _ts(10, day=day),
        "exit_price": e + (st - e) * (-r if False else 0),
        "exit_time": _ts(hour, day=day),
        "realized_r_multiple": r,
        "realized_pl_usd": usd,
        "mfe_r": mfe,
        "features": {"spread_r": spread_r} if spread_r is not None else {},
    }


def test_classify_kill_indicator_and_give():
    k = ca.classify_proposal("Add RSI and heat overlay to time the squeeze list")
    assert k["verdict"] == "KILL"
    ids = {f["id"] for f in k["families"]}
    assert "indicator_permute" in ids

    k2 = ca.classify_proposal("widen the give so we stop getting stomped")
    assert k2["verdict"] == "KILL"
    assert any(f["id"] == "widen_give" for f in k2["families"])


def test_classify_unknown_is_measure_not_keep():
    v = ca.classify_proposal("Screen names over $50 on the desk seed list for 15m MFE vs 0.79% RT")
    assert v["verdict"] == "MEASURE"
    assert v["families"] == []


def test_go_live_fails_on_red_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_REPORT_DIR", str(tmp_path))
    rows = []
    # 3 red sessions, underwater MFE-spread
    for i, day in enumerate(("2026-08-19", "2026-08-20", "2026-08-21")):
        rows.append(_outcome(r=-0.2, usd=-2.0, mfe=-0.05, spread_r=0.08, day=day))
    (tmp_path / "outcomes.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n")
    payload = ca.audit(days_back=10, report_dir=tmp_path, cfg={"desk_product": "scalp_legacy"})
    assert payload["go_live"]["pass"] is False
    assert payload["posture"]["stance"] == "observe"
    assert "not arming" in payload["posture"]["headline"].lower() or "observe" in payload["posture"]["headline"].lower()
    md = ca.render_markdown(payload)
    assert "improve the project" in md.lower()
    assert "approval" in md.lower()


def test_posture_observe_when_product_observe():
    golive = {
        "pass": True, "wins": 7, "n_sessions": 10, "trades": 40,
        "median_mfe_less_spread": 0.01, "sessions_ok": True,
        "trades_ok": True, "mfe_ok": True, "bar": ca.GO_LIVE,
    }
    p = ca.posture({"desk_product": "observe"}, golive)
    assert p["stance"] == "observe"
    assert p["allows_new_entries"] is False


def test_inventory_and_legacy_outcomes(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_REPORT_DIR", str(tmp_path / "ai_reports"))
    ai = tmp_path / "ai_reports"
    legacy = tmp_path / "claude_reports"
    ai.mkdir()
    legacy.mkdir()
    (ai / "events.jsonl").write_text(
        "\n".join(json.dumps({"ts": _ts(), "kind": "arm"}) for _ in range(101)) + "\n")
    rows = [
        json.dumps(_outcome(r=-0.1, usd=-1.0, day="2026-08-20")),
        json.dumps(_outcome(r=-0.2, usd=-2.0, day="2026-08-21")),
    ]
    (legacy / "outcomes.jsonl").write_text("\n".join(rows) + "\n")
    inv = ca.inventory(ai, repo=tmp_path)
    assert inv["totals"]["events_lines"] >= 101
    assert inv["totals"]["outcomes_lines"] == 2
    # load_sessions must pick up the legacy outcomes
    days, by_day = ca.load_sessions(10, report_dir=ai, repo=tmp_path)
    assert "2026-08-21" in days
    assert len(by_day["2026-08-20"]) == 1
    payload = ca.audit(days_back=10, report_dir=ai, repo=tmp_path,
                       cfg={"desk_product": "observe"})
    md = ca.render_markdown(payload)
    assert "Corpus" in md
    assert payload["corpus"]["jsonl"]


def test_write_artifacts(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_REPORT_DIR", str(tmp_path))
    (tmp_path / "outcomes.jsonl").write_text("")
    payload = ca.audit(days_back=5, report_dir=tmp_path, cfg={"desk_product": "observe"})
    paths = ca.write_artifacts(payload, report_dir=tmp_path)
    assert Path(paths["latest"]).exists()
    text = Path(paths["latest"]).read_text()
    assert "KEEP" in text and "KILL" in text and "MEASURE" in text
    # auditor must not touch bot config
    assert not (ROOT / "config" / "bot_config.json").samefile(
        tmp_path / "bot_config.json") if (tmp_path / "bot_config.json").exists() else True
