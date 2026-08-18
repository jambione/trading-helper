"""Session lifetime, cookie persistence, and login-page wiring."""

import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auth  # noqa: E402


def _payload(token: str) -> dict:
    return json.loads(auth._b64d(token.split(".")[1]))


def test_new_token_lasts_thirty_days():
    tok = auth.create_token("alice")
    assert auth.verify_token(tok)
    data = _payload(tok)
    assert data["sub"] == "alice"
    assert data["exp"] - data["iat"] == auth.TOKEN_TTL
    assert auth.TOKEN_TTL == 30 * 86400


def test_token_ttl_override_from_secrets(tmp_path, monkeypatch):
    secrets = tmp_path / "secrets.json"
    secrets.write_text(json.dumps({"token_ttl_seconds": 7200}), encoding="utf-8")
    monkeypatch.setattr(auth, "_SECRETS_FILE", secrets)
    assert auth.token_ttl_seconds() == 7200
    tok = auth.create_token("bob")
    data = _payload(tok)
    assert data["exp"] - data["iat"] == 7200


def test_short_ttl_override_is_ignored(tmp_path, monkeypatch):
    secrets = tmp_path / "secrets.json"
    secrets.write_text(json.dumps({"token_ttl_seconds": 30}), encoding="utf-8")
    monkeypatch.setattr(auth, "_SECRETS_FILE", secrets)
    assert auth.token_ttl_seconds() == auth.TOKEN_TTL


def test_expired_token_is_rejected():
    tok = auth.create_token("alice", ttl=1)
    time.sleep(1.1)
    assert auth.verify_token(tok) is False
    assert auth.get_token_username(tok) == ""


def test_first_valid_token_skips_expired():
    dead = auth.create_token("old", ttl=1)
    time.sleep(1.1)
    live = auth.create_token("new", ttl=60)
    assert auth.first_valid_token(dead, live) == live
    assert auth.first_valid_token("", dead) == dead
    assert auth.first_valid_token("", "") == ""


def test_https_detection_from_forwarded_proto():
    class _Req:
        def __init__(self, headers, scheme="http"):
            self.headers = headers
            self.url = type("U", (), {"scheme": scheme})()

    assert auth.request_is_https(_Req({"x-forwarded-proto": "https"})) is True
    assert auth.request_is_https(_Req({"x-forwarded-proto": "http"})) is False
    assert auth.request_is_https(_Req({"cf-visitor": '{"scheme":"https"}'})) is True
    assert auth.request_is_https(_Req({}, scheme="https")) is True


def test_session_cookie_round_trip():
    class _Resp:
        def __init__(self):
            self.calls = []

        def set_cookie(self, **kwargs):
            self.calls.append(("set", kwargs))

        def delete_cookie(self, key, **kwargs):
            self.calls.append(("del", {"key": key, **kwargs}))

    resp = _Resp()
    auth.set_session_cookie(resp, "tok-value", secure=True)
    kind, kwargs = resp.calls[0]
    assert kind == "set"
    assert kwargs["key"] == auth.COOKIE_NAME
    assert kwargs["value"] == "tok-value"
    assert kwargs["httponly"] is True
    assert kwargs["samesite"] == "lax"
    assert kwargs["secure"] is True
    assert kwargs["max_age"] == auth.token_ttl_seconds()

    auth.clear_session_cookie(resp, secure=True)
    assert resp.calls[1][0] == "del"
    assert resp.calls[1][1]["key"] == auth.COOKIE_NAME


@pytest.fixture
def auth_client(tmp_path, monkeypatch):
    """Dashboard TestClient with auth on and a throwaway user file."""
    import dashboard as d
    from fastapi.testclient import TestClient

    users = tmp_path / "users.json"
    monkeypatch.setattr(auth, "_USERS_FILE", users)
    assert auth.create_user("alice", "s3cret1")
    monkeypatch.setattr(d, "is_auth_required", lambda: True)
    monkeypatch.setattr(d, "send_login_email", lambda *a, **k: True)
    monkeypatch.setattr(d, "send_access_request_email", lambda *a, **k: True)
    monkeypatch.setattr(d, "send_access_received_email", lambda *a, **k: True)
    monkeypatch.setattr(d, "send_access_approved_email", lambda *a, **k: True)
    monkeypatch.setattr(d, "send_password_reset_email", lambda *a, **k: True)
    monkeypatch.setattr(d, "record_login", lambda *a, **k: None)
    monkeypatch.setattr(d, "record_traffic_hit", lambda *a, **k: None)
    return TestClient(d.app)


def test_login_sets_httponly_cookie(auth_client):
    r = auth_client.post("/auth/login", json={"username": "alice", "password": "s3cret1"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["token"]
    assert body["expires_in"] == auth.TOKEN_TTL
    cookie = r.cookies.get(auth.COOKIE_NAME)
    assert cookie
    assert auth.verify_token(cookie)


def test_cookie_alone_keeps_the_session(auth_client):
    denied = auth_client.get("/api/config")
    assert denied.status_code == 401

    auth_client.post("/auth/login", json={"username": "alice", "password": "s3cret1"})
    # TestClient keeps the cookie; no Authorization header on this call.
    r = auth_client.get("/api/config")
    assert r.status_code == 200


def test_expired_bearer_does_not_hide_cookie(auth_client):
    auth_client.post("/auth/login", json={"username": "alice", "password": "s3cret1"})
    dead = auth.create_token("alice", ttl=1)
    time.sleep(1.1)
    r = auth_client.get("/api/config", headers={"Authorization": f"Bearer {dead}"})
    assert r.status_code == 200


def test_logout_clears_cookie(auth_client):
    auth_client.post("/auth/login", json={"username": "alice", "password": "s3cret1"})
    assert auth_client.get("/api/config").status_code == 200
    out = auth_client.post("/auth/logout")
    assert out.status_code == 200
    assert auth_client.get("/api/config").status_code == 401


def test_discord_ingest_requires_auth(auth_client):
    denied = auth_client.post("/api/discord/ingest", json={"alerts": []})
    assert denied.status_code == 401

    auth_client.post("/auth/login", json={"username": "alice", "password": "s3cret1"})
    ok = auth_client.post("/api/discord/ingest", json={"alerts": []})
    assert ok.status_code == 200
    assert ok.json()["ok"] is True


def test_login_page_is_the_form(auth_client):
    r = auth_client.get("/login")
    assert r.status_code == 200
    assert b'id="login-user"' in r.content


def test_register_page_is_the_form(auth_client):
    r = auth_client.get("/register")
    assert r.status_code == 200
    assert b'id="reg-user"' in r.content
    assert b'id="reg-email"' in r.content


def test_forgot_and_reset_pages(auth_client):
    assert b'id="fg-email"' in auth_client.get("/forgot").content
    assert b'id="rs-pass"' in auth_client.get("/reset").content


def test_root_redirects_to_login_when_signed_out(auth_client):
    r = auth_client.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


def test_logged_in_root_serves_dashboard(auth_client):
    auth_client.post("/auth/login", json={"username": "alice", "password": "s3cret1"})
    r = auth_client.get("/", follow_redirects=False)
    assert r.status_code == 200
    assert b"Signal Scanner" in r.content or b"brasfield" in r.content.lower()


def test_login_page_redirects_when_already_signed_in(auth_client):
    auth_client.post("/auth/login", json={"username": "alice", "password": "s3cret1"})
    r = auth_client.get("/login", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/"


def test_meta_slides_a_fresh_token(auth_client):
    first = auth_client.post(
        "/auth/login", json={"username": "alice", "password": "s3cret1"}
    ).json()["token"]
    meta = auth_client.get("/api/meta")
    assert meta.status_code == 200
    body = meta.json()
    assert body["username"] == "alice"
    assert body["token"]
    assert body["token"] != first
    assert auth.verify_token(body["token"])
    assert auth.get_token_username(body["token"]) == "alice"


def test_request_account_is_pending_when_users_exist(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "_USERS_FILE", tmp_path / "users.json")
    assert auth.create_user("alice", "s3cret1")
    profile, err = auth.request_account("bob", "goodpass1", "bob@example.com", "Bob")
    assert err == ""
    assert profile["status"] == "pending"
    assert profile["email"] == "bob@example.com"
    user, login_err = auth.authenticate("bob", "goodpass1")
    assert user == ""
    assert "pending" in login_err


def test_first_request_becomes_admin(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "_USERS_FILE", tmp_path / "users.json")
    profile, err = auth.request_account("owner", "goodpass1", "owner@example.com", "Owner")
    assert err == ""
    assert profile["status"] == "active"
    assert profile["admin"] is True
    user, login_err = auth.authenticate("owner", "goodpass1")
    assert user == "owner"
    assert login_err == ""


def test_login_by_email(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "_USERS_FILE", tmp_path / "users.json")
    assert auth.create_user("alice", "s3cret1", email="alice@example.com")
    user, err = auth.authenticate("alice@example.com", "s3cret1")
    assert user == "alice"
    assert err == ""


def test_change_password_and_old_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "_USERS_FILE", tmp_path / "users.json")
    assert auth.create_user("alice", "s3cret1")
    ok, err = auth.change_password("alice", "s3cret1", "newpass99")
    assert ok and err == ""
    assert auth.authenticate("alice", "s3cret1")[0] == ""
    assert auth.authenticate("alice", "newpass99")[0] == "alice"


def test_password_reset_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "_USERS_FILE", tmp_path / "users.json")
    assert auth.create_user("alice", "s3cret1", email="alice@example.com")
    raw, uname = auth.create_reset_token("alice@example.com")
    assert uname == "alice" and raw
    ok, err = auth.reset_password(raw, "resetpass1")
    assert ok and err == ""
    assert auth.authenticate("alice", "resetpass1")[0] == "alice"
    assert auth.reset_password(raw, "another99")[0] is False


def test_public_profile_hides_secrets(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "_USERS_FILE", tmp_path / "users.json")
    assert auth.create_user("alice", "s3cret1", email="alice@example.com")
    profile = auth.public_profile("alice")
    blob = json.dumps(profile)
    assert "hash" not in blob
    assert "s3cret1" not in blob
    assert "reset_token" not in blob
    assert profile["broker"]["configured"] is False


def test_register_endpoint_pending(auth_client):
    r = auth_client.post("/auth/register", json={
        "username": "carol",
        "password": "goodpass1",
        "email": "carol@example.com",
        "display_name": "Carol",
    })
    assert r.status_code == 200
    assert r.json()["pending"] is True
    denied = auth_client.post("/auth/login", json={"username": "carol", "password": "goodpass1"})
    assert denied.status_code == 401
    assert "pending" in denied.json()["error"]


def test_admin_approves_then_user_can_login(auth_client):
    auth.create_user("boss", "goodpass1", admin=True)
    auth_client.post("/auth/register", json={
        "username": "dave",
        "password": "goodpass1",
        "email": "dave@example.com",
        "display_name": "Dave",
    })
    # Log in as alice — not admin
    auth_client.post("/auth/login", json={"username": "alice", "password": "s3cret1"})
    forbidden = auth_client.get("/api/admin/users")
    assert forbidden.status_code == 403

    # New client as boss
    from fastapi.testclient import TestClient
    import dashboard as d
    boss = TestClient(d.app)
    assert boss.post("/auth/login", json={"username": "boss", "password": "goodpass1"}).status_code == 200
    listed = boss.get("/api/admin/users")
    assert listed.status_code == 200
    names = [u["username"] for u in listed.json()["users"]]
    assert "dave" in names
    assert "hash" not in json.dumps(listed.json())
    ok = boss.post("/api/admin/users/dave/approve")
    assert ok.status_code == 200
    assert ok.json()["account"]["status"] == "active"

    dave = TestClient(d.app)
    inn = dave.post("/auth/login", json={"username": "dave", "password": "goodpass1"})
    assert inn.status_code == 200


def test_account_profile_and_password_endpoints(auth_client):
    auth_client.post("/auth/login", json={"username": "alice", "password": "s3cret1"})
    got = auth_client.get("/api/account")
    assert got.status_code == 200
    assert got.json()["account"]["username"] == "alice"

    saved = auth_client.post("/api/account", json={
        "display_name": "Alice A",
        "email": "alice@example.com",
    })
    assert saved.status_code == 200
    assert saved.json()["account"]["display_name"] == "Alice A"

    bad = auth_client.post("/api/account/password", json={
        "current": "wrong",
        "new": "newerpass1",
    })
    assert bad.status_code == 400

    good = auth_client.post("/api/account/password", json={
        "current": "s3cret1",
        "new": "newerpass1",
    })
    assert good.status_code == 200
    auth_client.post("/auth/logout")
    assert auth_client.post(
        "/auth/login", json={"username": "alice", "password": "newerpass1"}
    ).status_code == 200


def test_jmb_is_pinned_owner_admin(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "_USERS_FILE", tmp_path / "users.json")
    assert auth.create_user("jmb", "goodpass1")
    auth.ensure_owner_admin()
    profile = auth.public_profile("jmb", admin_view=True)
    assert profile["display_name"] == "JMB"
    assert profile["email"] == "jambione@icloud.com"
    assert profile["admin"] is True
    assert profile["status"] == "active"
    assert profile["owner"] is True
    assert auth.is_admin_user("jmb") is True


def test_cannot_disable_or_demote_jmb(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "_USERS_FILE", tmp_path / "users.json")
    assert auth.create_user("jmb", "goodpass1", admin=True)
    auth.ensure_owner_admin()
    ok, err = auth.set_user_status("jmb", "disabled", by="other")
    assert ok is False
    assert "JMB" in err
    assert auth.set_admin("jmb", False) is False
    assert auth.is_admin_user("jmb") is True


def test_admin_can_fix_user_profile_and_password(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "_USERS_FILE", tmp_path / "users.json")
    assert auth.create_user("kara", "oldpass11")
    profile, err = auth.admin_update_user(
        "kara", display_name="Kara B", email="kara@example.com", by="jmb")
    assert err == ""
    assert profile["display_name"] == "Kara B"
    assert profile["email"] == "kara@example.com"
    ok, perr = auth.admin_set_password("kara", "fixedpass1")
    assert ok and perr == ""
    assert auth.authenticate("kara", "fixedpass1")[0] == "kara"


def test_cannot_take_admin_email(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "_USERS_FILE", tmp_path / "users.json")
    assert auth.create_user("jmb", "goodpass1", admin=True)
    auth.ensure_owner_admin()
    profile, err = auth.request_account(
        "eve", "goodpass1", "jambione@icloud.com", "Eve")
    assert profile is None
    assert "already" in err.lower()


def test_cannot_disable_last_admin(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "_USERS_FILE", tmp_path / "users.json")
    assert auth.create_user("boss", "goodpass1", admin=True)
    ok, err = auth.set_user_status("boss", "disabled", by="other")
    assert ok is False
    assert "last admin" in err


def test_admin_user_detail_and_password_routes(auth_client):
    auth.create_user("jmb", "goodpass1", admin=True)
    auth.ensure_owner_admin()
    auth.create_user("kara", "oldpass11")
    from fastapi.testclient import TestClient
    import dashboard as d
    boss = TestClient(d.app)
    assert boss.post("/auth/login", json={"username": "jmb", "password": "goodpass1"}).status_code == 200
    detail = boss.get("/api/admin/users/kara")
    assert detail.status_code == 200
    assert detail.json()["account"]["username"] == "kara"
    assert "hash" not in json.dumps(detail.json())
    fixed = boss.post("/api/admin/users/kara", json={
        "display_name": "Kara",
        "email": "kara@example.com",
    })
    assert fixed.status_code == 200
    assert fixed.json()["account"]["email"] == "kara@example.com"
    pw = boss.post("/api/admin/users/kara/password", json={"password": "newpass88"})
    assert pw.status_code == 200
    denied = boss.post("/api/admin/users/jmb/disable")
    assert denied.status_code == 400
    kara = TestClient(d.app)
    assert kara.post("/auth/login", json={"username": "kara", "password": "newpass88"}).status_code == 200


def test_forgot_always_ok(auth_client):
    r = auth_client.post("/auth/forgot", json={"email": "missing@example.com"})
    assert r.status_code == 200
    assert r.json()["ok"] is True


# ── login rate limiting ──────────────────────────────────────────────────────

@pytest.fixture
def clean_auth_hits():
    """_AUTH_HITS is module state — don't let one test's storm reach another."""
    import dashboard as d
    d._AUTH_HITS.clear()
    yield d
    d._AUTH_HITS.clear()


def test_successful_login_storm_is_rate_limited(auth_client, clean_auth_hits):
    """The 2026-08-18 storm: 2,639 logins in a day, none of them throttled.

    Only *failed* logins were counted, so a desk client re-logging-in on every
    401 sailed past the limiter — each attempt costing a ~20ms PBKDF2 verify.
    """
    d = clean_auth_hits
    good = {"username": "alice", "password": "s3cret1"}
    codes = [auth_client.post("/auth/login", json=good).status_code
             for _ in range(d._LOGIN_BURST_LIMIT + 5)]

    assert codes[0] == 200
    assert 429 in codes, "a storm of valid logins was never throttled"
    assert codes.count(200) <= d._LOGIN_BURST_LIMIT
    assert codes[-1] == 429


def test_honest_login_volume_is_not_rate_limited(auth_client, clean_auth_hits):
    """Five desk clients plus a browser must never trip the burst ceiling."""
    good = {"username": "alice", "password": "s3cret1"}
    codes = [auth_client.post("/auth/login", json=good).status_code
             for _ in range(6)]
    assert codes == [200] * 6


def test_failed_logins_still_hit_the_tighter_ceiling(auth_client, clean_auth_hits):
    """Password guessing is capped at 10, well below the burst ceiling."""
    bad = {"username": "alice", "password": "wrong"}
    codes = [auth_client.post("/auth/login", json=bad).status_code
             for _ in range(12)]
    assert codes[0] == 401
    assert codes[-1] == 429
    assert codes.count(401) <= 10
