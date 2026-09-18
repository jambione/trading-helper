"""The ?user=jmb identity bypass, and authorization on the config write path.

Regression cover for the hole found 2026-09-18: ``_request_identity`` granted
the owner's identity from a bare ``?user=jmb`` query parameter, and
``_AuthMiddleware`` admitted any request whose username was ``jmb``. The
dashboard is served to the public internet through a Cloudflare tunnel with no
Access policy in front, and ``POST /api/config`` carried no authorization of
its own — so that parameter was unauthenticated write access to the Alpaca
credentials and every risk limit on an account that places orders.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auth  # noqa: E402
from config import PROTECTED_CONFIG_KEYS, SAFE_CONFIG_KEYS  # noqa: E402


@pytest.fixture
def auth_client(tmp_path, monkeypatch):
    """Dashboard TestClient with auth on and a throwaway user file."""
    import dashboard as d
    from fastapi.testclient import TestClient

    users = tmp_path / "users.json"
    monkeypatch.setattr(auth, "_USERS_FILE", users)
    assert auth.create_user("alice", "s3cret1")
    monkeypatch.setattr(d, "is_auth_required", lambda: True)
    monkeypatch.setattr(d, "record_login", lambda *a, **k: None)
    monkeypatch.setattr(d, "record_traffic_hit", lambda *a, **k: None)
    return TestClient(d.app)


# ── The bypass itself ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", [
    "/api/config",
    "/api/state",
    "/",
])
def test_user_jmb_query_param_does_not_authenticate(auth_client, path):
    """The whole point. ?user=jmb must be worth exactly nothing."""
    r = auth_client.get(f"{path}?user=jmb", follow_redirects=False)
    assert r.status_code in (401, 302), (
        f"{path}?user=jmb returned {r.status_code} — the bypass is open"
    )


def test_user_jmb_cannot_write_config(auth_client):
    r = auth_client.post("/api/config?user=jmb", json={"bar_count": 999})
    assert r.status_code == 401


def test_case_and_whitespace_variants_are_not_a_way_back_in(auth_client):
    for variant in ("JMB", " jmb ", "Jmb", "jmb%20"):
        r = auth_client.get(f"/api/config?user={variant}")
        assert r.status_code == 401, f"?user={variant!r} authenticated"


def test_request_identity_ignores_the_query_param():
    """Unit-level: identity comes from a token, or it does not come."""
    import dashboard as d

    class _Req:
        query_params = {"user": "jmb"}
        cookies: dict = {}
        headers: dict = {}

    token, username = d._request_identity(_Req())
    assert token == ""
    assert username == ""


def test_a_real_token_still_authenticates(auth_client):
    login = auth_client.post("/auth/login",
                             json={"username": "alice", "password": "s3cret1"})
    assert login.status_code == 200
    assert auth_client.get("/api/config").status_code == 200


# ── Config write authorization ────────────────────────────────────────────────

def _login_admin(auth_client, tmp_path, monkeypatch):
    """Log in as an admin user the dashboard will accept."""
    assert auth.create_user("jmb", "s3cret1")
    users = json.loads(auth._USERS_FILE.read_text())
    entry = users["jmb"]
    if isinstance(entry, dict):
        entry["admin"] = True
        entry["status"] = "active"
    auth._USERS_FILE.write_text(json.dumps(users))
    r = auth_client.post("/auth/login",
                         json={"username": "jmb", "password": "s3cret1"})
    assert r.status_code == 200
    return r


def test_non_admin_cannot_write_config(auth_client):
    auth_client.post("/auth/login",
                     json={"username": "alice", "password": "s3cret1"})
    r = auth_client.post("/api/config", json={"bar_count": 999})
    assert r.status_code == 403


def test_admin_cannot_write_protected_keys(auth_client, tmp_path, monkeypatch):
    _login_admin(auth_client, tmp_path, monkeypatch)
    r = auth_client.post("/api/config", json={"ai_risk_pct": 99.0})
    assert r.status_code == 403
    body = r.json()
    assert body["ok"] is False
    assert body["refused"] == ["ai_risk_pct"]


def test_protected_write_is_refused_whole_not_partially(auth_client, tmp_path,
                                                        monkeypatch):
    """A mixed body must not apply its safe half and drop the rest silently."""
    import dashboard as d

    _login_admin(auth_client, tmp_path, monkeypatch)
    saved = {}
    monkeypatch.setattr(d, "save_config", lambda cfg: saved.update(cfg))

    r = auth_client.post("/api/config",
                         json={"bar_count": 777, "ai_max_positions": 99})
    assert r.status_code == 403
    assert saved == {}, "a refused request must write nothing at all"


# ── The protected set ─────────────────────────────────────────────────────────

def test_every_protected_key_is_actually_refused(auth_client, tmp_path,
                                                 monkeypatch):
    _login_admin(auth_client, tmp_path, monkeypatch)
    for key in sorted(PROTECTED_CONFIG_KEYS):
        r = auth_client.post("/api/config", json={key: 1})
        assert r.status_code == 403, f"{key} was accepted over HTTP"


def test_protected_keys_still_load_through_config():
    """Protection is HTTP-only — the desk must still read and write these."""
    from config import load_config
    cfg = load_config()
    for key in ("ai_risk_pct", "ai_daily_loss_limit_r", "ai_max_positions"):
        assert key in cfg, f"{key} vanished from the resolved config"


def test_money_and_audit_keys_are_covered():
    """Guards the list itself — these are why PROTECTED_CONFIG_KEYS exists."""
    must_protect = {
        "ai_risk_pct", "ai_trade_amount", "ai_max_positions",
        "ai_max_open_risk_pct", "ai_daily_loss_limit_r",
        "ai_trading_enabled", "ai_trader_enabled", "ai_trading_host",
        "ai_broker_stop_enabled", "require_protective_exit",
        "ai_fill_ledger_enabled",
    }
    missing = must_protect - set(PROTECTED_CONFIG_KEYS)
    assert not missing, f"unprotected money/audit keys: {sorted(missing)}"


def test_protected_keys_are_a_subset_of_safe_keys():
    """A protected key outside SAFE_CONFIG_KEYS would be dead weight."""
    stray = set(PROTECTED_CONFIG_KEYS) - set(SAFE_CONFIG_KEYS)
    assert not stray, f"protected but not in SAFE_CONFIG_KEYS: {sorted(stray)}"
