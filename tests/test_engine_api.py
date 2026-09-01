"""
test_engine_api.py — /api/engine/* endpoints: guard semantics + wiring.

The engine control endpoints move settings on a system that trades money, so
the guard is stricter than the rest of the API: mutations require a direct
localhost request (no proxy headers) or the engine control secret. Reads are
open like the rest of the dashboard API.

Run:
    venv/bin/python -m pytest tests/test_engine_api.py -q
"""

import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dashboard as d   # noqa: E402
import engine_env       # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    """TestClient with the env file + restart flag redirected to tmp_path.

    Dashboard login is pinned OFF. These tests are about the ENGINE guard —
    localhost vs proxied vs control secret — which sits behind the dashboard's
    own auth middleware. That middleware is opt-in via ``require_auth`` in
    config/secrets.json, so on the mini (auth on, public tunnel) every request
    here returned 401 before reaching the endpoint and all twelve tests failed,
    while the same suite passed on a laptop with auth off. A test that reports
    the operator's secrets.json rather than the guard it names cannot be used
    to validate a deploy on the box that actually trades.
    """
    monkeypatch.setattr(d, "is_auth_required", lambda: False)
    env = tmp_path / "signal_engine.env"
    env.write_text("TRADER_MODE=paper\nTAKE_PROFIT=2.0\n", encoding="utf-8")
    monkeypatch.setattr(engine_env, "ENGINE_ENV_FILE", env)
    flag = tmp_path / "engine_restart.flag"
    monkeypatch.setattr(d, "_ENGINE_RESTART_FLAG", flag)
    c = TestClient(d.app)
    c.env_file = env
    c.flag = flag
    return c


# ── Reads (open) ──────────────────────────────────────────────────────────────

def test_get_config_returns_whitelist_only(client):
    res = client.get("/api/engine/config")
    assert res.status_code == 200
    values = res.json()["values"]
    assert values["TRADER_MODE"] == "paper"
    assert "ALPACA_SECRET_KEY" not in values


# ── Guard: localhost vs proxied vs secret ─────────────────────────────────────

def test_local_request_can_write(client):
    res = client.post("/api/engine/config", json={"TAKE_PROFIT": "1.5"})
    assert res.status_code == 200
    assert res.json()["updated"] == ["TAKE_PROFIT"]
    assert "TAKE_PROFIT=1.5" in client.env_file.read_text()


def test_proxied_request_without_secret_403(client):
    res = client.post("/api/engine/config",
                      json={"TAKE_PROFIT": "9"},
                      headers={"X-Forwarded-For": "203.0.113.7"})
    assert res.status_code == 403
    assert "TAKE_PROFIT=9" not in client.env_file.read_text()


def test_proxied_request_with_secret_allowed(client, monkeypatch):
    with d.STATE.lock:
        d.STATE.cfg["engine_control_secret"] = "s3cret"  # pragma: allowlist secret
    try:
        res = client.post("/api/engine/config",
                          json={"TAKE_PROFIT": "1.8"},
                          headers={"X-Forwarded-For": "203.0.113.7",
                                   "X-Engine-Secret": "s3cret"})  # pragma: allowlist secret
        assert res.status_code == 200
        res2 = client.post("/api/engine/config",
                           json={"TAKE_PROFIT": "1.9"},
                           headers={"CF-Connecting-IP": "203.0.113.7",
                                    "X-Engine-Secret": "wrong"})  # pragma: allowlist secret
        assert res2.status_code == 403
    finally:
        with d.STATE.lock:
            d.STATE.cfg.pop("engine_control_secret", None)


# ── Validation surfaces as 400 ────────────────────────────────────────────────

def test_live_mode_rejected_with_400(client):
    res = client.post("/api/engine/config", json={"TRADER_MODE": "live"})
    assert res.status_code == 400
    assert "live" in res.json()["error"]
    assert "TRADER_MODE=paper" in client.env_file.read_text()   # unchanged


def test_bad_value_rejected_with_400(client):
    res = client.post("/api/engine/config", json={"STOP_LOSS": "oops"})
    assert res.status_code == 400


# ── Restart flag ──────────────────────────────────────────────────────────────

def test_restart_touches_flag(client):
    assert not client.flag.exists()
    res = client.post("/api/engine/restart")
    assert res.status_code == 200
    assert client.flag.exists()


def test_restart_blocked_remote(client):
    res = client.post("/api/engine/restart",
                      headers={"CF-Connecting-IP": "203.0.113.7"})
    assert res.status_code == 403
    assert not client.flag.exists()


# ── Report endpoint ───────────────────────────────────────────────────────────

def test_report_endpoint_shape(client, tmp_path, monkeypatch):
    # Point the lazily imported paper_report module at a tiny synthetic log.
    log = tmp_path / "signal_log.json"
    log.write_text("""[
      {"action":"BUY","ticker":"AAA","price":2.0,"rsi":50,"macd_hist":0.1,
       "time":"2099-01-01T15:00:00Z"},
      {"action":"SELL","ticker":"AAA","price":2.04,"buy_price":2.0,"pnl_pct":2.0,
       "rsi":60,"reason":"tv_take_profit","macd_hist":0.1,
       "time":"2099-01-01T15:05:00Z","buy_time":"2099-01-01T15:00:00Z"}
    ]""")
    pr = d._get_paper_report()
    monkeypatch.setattr(pr, "LOG_FILE", log)

    res = client.get("/api/engine/report?days=0&size=500")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    report = body["report"]
    assert report["overall"]["trades"] == 1
    assert report["overall"]["total_dollar"] == 10.0   # +2% of $500
    assert "by_day" in report and "by_reason" in report
