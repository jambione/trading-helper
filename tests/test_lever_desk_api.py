"""
test_lever_desk_api.py — /api/lever-desk* endpoints.

Reads are open when auth is off (localhost). When auth is on, admin only.
Never writes config/bot_config.json.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auth  # noqa: E402
import dashboard as d  # noqa: E402

ET = ZoneInfo("America/New_York")
DAY = "2026-09-11"


def _ts(hour=15, day=DAY):
    dt = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
    return dt.replace(hour=hour).timestamp()


def _outcome(*, r=0.25, mfe=0.50, day=DAY):
    return {
        "ts": _ts(15, day=day),
        "symbol": "TEST",
        "entry_price": 10.0,
        "stop_price": 9.5,
        "entry_time": _ts(10, day=day),
        "exit_price": 10.0,
        "exit_time": _ts(15, day=day),
        "realized_r_multiple": r,
        "realized_pl_usd": r * 5.0,
        "mfe_r": mfe,
        "close_reason": "local_trail",
        "give_r_at_exit": 0.1,
        "features": {},
    }


def _registry():
    return {
        "version": 1,
        "levers": [{
            "id": "green_catchup_trail",
            "title": "Green catch-up",
            "hypothesis": "test",
            "status": "live",
            "shipped_at": "2026-09-10",
            "baseline_sessions": [],
            "knobs": {"ai_local_trail_time_decay_enabled": True},
            "score": {
                "kind": "hold_capture",
                "min_mfe_r": 0.25,
                "min_n": 5,
                "pass": {"median_capture_gte": 0.40},
                "kill": {"median_capture_lt": 0.15},
            },
            "next_on_kill": "revert manually",
        }],
    }


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "is_auth_required", lambda: False)
    d._ld_cache_clear()

    reg = tmp_path / "lever_desk.json"
    reg.write_text(json.dumps(_registry()), encoding="utf-8")

    # Point report dir + registry at tmp before lazy import binds paths.
    monkeypatch.setenv("AI_REPORT_DIR", str(tmp_path))
    (tmp_path / "outcomes.jsonl").write_text(
        "\n".join(json.dumps(_outcome(r=0.25, mfe=0.50)) for _ in range(5)) + "\n",
        encoding="utf-8",
    )

    # Force (re)load of lever_desk with patched REGISTRY_PATH.
    d._lever_desk_mod = None
    mod = d._get_lever_desk()
    monkeypatch.setattr(mod, "REGISTRY_PATH", reg)

    c = TestClient(d.app)
    c.tmp_path = tmp_path
    c.reg = reg
    return c


def test_get_shape(client):
    res = client.get("/api/lever-desk?days=10&refresh=1")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["active_id"] == "green_catchup_trail"
    assert body["lever"]["score"]["verdict"] in ("KEEP", "KILL", "MEASURE")
    assert "go_live" in body
    assert "posture" in body


def test_post_classify(client):
    res = client.post("/api/lever-desk/classify",
                      json={"text": "widen the give so we stop getting stomped"})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["verdict"] == "KILL"
    assert any(f["id"] == "widen_give" for f in body["families"])


def test_post_verdict_does_not_change_bot_config(client):
    bot = Path(__file__).resolve().parents[1] / "config" / "bot_config.json"
    before = bot.read_bytes() if bot.exists() else None

    res = client.post("/api/lever-desk/verdict",
                      json={"verdict": "MEASURE", "note": "need another day"})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["recorded"]["verdict"] == "MEASURE"
    assert (client.tmp_path / "lever_desk" / "verdicts.jsonl").exists()

    if before is not None:
        assert bot.read_bytes() == before


def test_activate_unknown_404(client):
    res = client.post("/api/lever-desk/activate", json={"id": "nope"})
    assert res.status_code == 404


def test_upsert_lever_from_dashboard(client):
    bot = Path(__file__).resolve().parents[1] / "config" / "bot_config.json"
    before = bot.read_bytes() if bot.exists() else None
    res = client.post("/api/lever-desk/lever", json={
        "id": "occupancy_dead_reentry",
        "title": "Dead reentry off",
        "hypothesis": "Seat liquid names instead of all-day bench after scratch",
        "shipped_at": "2026-09-12",
        "knobs": {"ai_dead_reentry_block": False},
        "score_kind": "hold_capture",
        "min_n": 5,
        "make_active": True,
    })
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["active_id"] == "occupancy_dead_reentry"
    assert (client.tmp_path / "lever_desk" / "registry.json").exists()
    if before is not None:
        assert bot.read_bytes() == before

    got = client.get("/api/lever-desk?refresh=1")
    assert got.status_code == 200
    assert got.json()["active_id"] == "occupancy_dead_reentry"
    assert got.json()["registry_source"] == "runtime"


def test_auth_on_non_admin_403(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "is_auth_required", lambda: True)
    d._ld_cache_clear()

    users = tmp_path / "users.json"
    monkeypatch.setattr(auth, "_USERS_FILE", users)
    assert auth.create_user("alice", "s3cret1", admin=False)

    reg = tmp_path / "lever_desk.json"
    reg.write_text(json.dumps(_registry()), encoding="utf-8")
    monkeypatch.setenv("AI_REPORT_DIR", str(tmp_path))
    d._lever_desk_mod = None
    mod = d._get_lever_desk()
    monkeypatch.setattr(mod, "REGISTRY_PATH", reg)

    monkeypatch.setattr(d, "send_login_email", lambda *a, **k: True)
    monkeypatch.setattr(d, "record_login", lambda *a, **k: None)
    monkeypatch.setattr(d, "record_traffic_hit", lambda *a, **k: None)

    c = TestClient(d.app)
    c.post("/auth/login", json={"username": "alice", "password": "s3cret1"})
    res = c.get("/api/lever-desk")
    assert res.status_code == 403
    assert res.json()["error"] == "Admin access required"
