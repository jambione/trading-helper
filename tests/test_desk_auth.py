"""desk_auth — the shared dashboard login for every desk process.

These tests exist because of the 2026-08-18 login storm: 2,639 logins in a
day, 398 in the worst minute, from a client that re-logged-in on every 401
with no floor between attempts. The throttle and the single-flight lock are
the fix, so they are what is pinned here.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

import desk_auth


@pytest.fixture(autouse=True)
def _restore_dashboard_env():
    """load_creds() injects into os.environ — don't leak it into other tests."""
    import os
    keys = ("DASHBOARD_URL", "DASHBOARD_USER", "DASHBOARD_PASS")
    saved = {k: os.environ.get(k) for k in keys}
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


class _Resp:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _auth(tmp_path, **kwargs) -> desk_auth.DashboardAuth:
    a = desk_auth.DashboardAuth(tmp_path, default_url="http://dash", verbose=False,
                                **kwargs)
    a.set_creds("http://dash", "desk", "secret")
    return a


def _login_counter(monkeypatch, token: str = "t-1", ok: bool = True):
    """Patch urlopen; return the list of URLs it was asked for."""
    calls: list[str] = []

    def _fake(req, timeout=None):
        url = getattr(req, "full_url", None) or req.get_full_url()
        calls.append(url)
        if url.endswith("/auth/login"):
            if not ok:
                raise urllib.error.HTTPError(url, 401, "Unauthorized",
                                             hdrs=None, fp=None)
            return _Resp(json.dumps(
                {"ok": True, "token": token, "expires_in": 3600}).encode())
        return _Resp(b'{"ok":true}')

    monkeypatch.setattr(urllib.request, "urlopen", _fake)
    return calls


# ── credentials ──────────────────────────────────────────────────────────────

def test_creds_prefer_dotenv_over_engine_env(tmp_path, monkeypatch):
    """Operator creds live in .env; the engine file only fills gaps."""
    (tmp_path / ".env").write_text(
        "DASHBOARD_URL=http://from-dotenv\nDASHBOARD_USER=dotenv_user\n",
        encoding="utf-8")
    (tmp_path / "signal_engine.env").write_text(
        "DASHBOARD_URL=http://from-engine\nDASHBOARD_USER=engine_user\n"
        "DASHBOARD_PASS=engine_pass\n",
        encoding="utf-8")
    for key in ("DASHBOARD_URL", "DASHBOARD_USER", "DASHBOARD_PASS"):
        monkeypatch.delenv(key, raising=False)

    a = desk_auth.DashboardAuth(tmp_path, verbose=False)
    url, user, password = a.load_creds()
    assert (url, user) == ("http://from-dotenv", "dotenv_user")
    # .env has no PASS, so the engine file still supplies it.
    assert password == "engine_pass"


def test_shell_env_beats_both_files(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("DASHBOARD_USER=dotenv_user\n", encoding="utf-8")
    monkeypatch.setenv("DASHBOARD_USER", "shell_user")
    monkeypatch.setenv("DASHBOARD_PASS", "shell_pass")
    monkeypatch.delenv("DASHBOARD_URL", raising=False)

    a = desk_auth.DashboardAuth(tmp_path, verbose=False)
    _, user, password = a.load_creds()
    assert (user, password) == ("shell_user", "shell_pass")


def test_no_creds_means_no_login_attempt(tmp_path, monkeypatch):
    for key in ("DASHBOARD_URL", "DASHBOARD_USER", "DASHBOARD_PASS"):
        monkeypatch.delenv(key, raising=False)
    calls = _login_counter(monkeypatch)
    a = desk_auth.DashboardAuth(tmp_path, verbose=False)
    assert a.token() == ""
    assert calls == []


# ── token caching ────────────────────────────────────────────────────────────

def test_token_is_cached_across_calls(tmp_path, monkeypatch):
    calls = _login_counter(monkeypatch)
    a = _auth(tmp_path)
    assert a.token() == "t-1"
    for _ in range(50):
        assert a.token() == "t-1"
    assert len(calls) == 1


def test_expired_token_refreshes(tmp_path, monkeypatch):
    calls = _login_counter(monkeypatch)
    a = _auth(tmp_path, min_login_interval=0.0)
    assert a.token() == "t-1"
    a._expires_at = 0.0          # pretend the 30-day token lapsed
    assert a.token() == "t-1"
    assert len(calls) == 2


# ── the storm guards ─────────────────────────────────────────────────────────

def test_repeated_401s_do_not_storm_the_login_endpoint(tmp_path, monkeypatch):
    """The 2026-08-18 bug: force-login per 401, ~2s tick, 398 logins/minute."""
    calls = _login_counter(monkeypatch)
    a = _auth(tmp_path, min_login_interval=30.0)

    # 200 ticks of a permanently-401ing endpoint, all inside the floor.
    for _ in range(200):
        a.token(force=True)

    logins = [u for u in calls if u.endswith("/auth/login")]
    assert len(logins) == 1, f"{len(logins)} logins — the floor is not holding"


def test_floor_lifts_once_the_window_passes(tmp_path, monkeypatch):
    calls = _login_counter(monkeypatch)
    a = _auth(tmp_path, min_login_interval=30.0)
    a.token(force=True)
    assert len(calls) == 1
    a.token(force=True)
    assert len(calls) == 1, "second attempt should be inside the floor"

    a._last_attempt -= 31.0      # window has now elapsed
    a.token(force=True)
    assert len(calls) == 2


def test_first_login_of_a_process_is_never_throttled(tmp_path, monkeypatch):
    """A fresh process must authenticate immediately, floor or no floor."""
    calls = _login_counter(monkeypatch)
    a = _auth(tmp_path, min_login_interval=3600.0)
    assert a.token() == "t-1"
    assert len(calls) == 1


def test_failed_logins_back_off_exponentially(tmp_path, monkeypatch):
    _login_counter(monkeypatch, ok=False)
    a = _auth(tmp_path, min_login_interval=10.0, max_backoff=100.0)

    a.token(force=True)
    assert a._fail_streak == 1
    assert a._wait_needed() == pytest.approx(20.0, abs=1.0)   # 10 * 2**1

    a._last_attempt -= 21.0
    a.token(force=True)
    assert a._fail_streak == 2
    assert a._wait_needed() == pytest.approx(40.0, abs=1.0)   # 10 * 2**2


def test_backoff_is_capped(tmp_path, monkeypatch):
    """Without the cap, 20 failures would push the next attempt past a decade."""
    import time as _time
    _login_counter(monkeypatch, ok=False)
    a = _auth(tmp_path, min_login_interval=10.0, max_backoff=60.0)
    a._fail_streak = 20
    a._last_attempt = _time.monotonic()
    assert a._wait_needed() == pytest.approx(60.0, abs=1.0)


def test_a_success_clears_the_backoff(tmp_path, monkeypatch):
    state = {"ok": False}
    calls: list[str] = []

    def _fake(req, timeout=None):
        url = getattr(req, "full_url", None) or req.get_full_url()
        calls.append(url)
        if not state["ok"]:
            raise urllib.error.HTTPError(url, 401, "no", hdrs=None, fp=None)
        return _Resp(b'{"ok":true,"token":"t-ok","expires_in":3600}')

    monkeypatch.setattr(urllib.request, "urlopen", _fake)
    a = _auth(tmp_path, min_login_interval=10.0)
    a.token(force=True)
    assert a._fail_streak == 1

    state["ok"] = True
    a._last_attempt -= 100.0
    assert a.token(force=True) == "t-ok"
    assert a._fail_streak == 0
    assert a._wait_needed() == pytest.approx(10.0, abs=1.0)


def test_concurrent_first_requests_produce_one_login(tmp_path, monkeypatch):
    """N threads hitting an empty token used to mean N logins (no lock)."""
    calls: list[str] = []
    lock = threading.Lock()

    def _fake(req, timeout=None):
        url = getattr(req, "full_url", None) or req.get_full_url()
        with lock:
            calls.append(url)
        return _Resp(b'{"ok":true,"token":"t-1","expires_in":3600}')

    monkeypatch.setattr(urllib.request, "urlopen", _fake)
    a = _auth(tmp_path)

    tokens: list[str] = []
    threads = [threading.Thread(target=lambda: tokens.append(a.token()))
               for _ in range(25)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert tokens == ["t-1"] * 25
    assert len(calls) == 1, f"{len(calls)} concurrent logins — lost the lock"


# ── request wrapper ──────────────────────────────────────────────────────────

def test_urlopen_retries_once_after_a_401(tmp_path, monkeypatch):
    seen: list[tuple[str, str | None]] = []
    state = {"accept": "t-2"}

    def _fake(req, timeout=None):
        url = getattr(req, "full_url", None) or req.get_full_url()
        auth = req.get_header("Authorization")
        seen.append((url, auth))
        if url.endswith("/auth/login"):
            return _Resp(json.dumps(
                {"ok": True, "token": state["accept"], "expires_in": 3600}).encode())
        if auth != f"Bearer {state['accept']}":
            raise urllib.error.HTTPError(url, 401, "no", hdrs=None, fp=None)
        return _Resp(b'{"ok":true}')

    monkeypatch.setattr(urllib.request, "urlopen", _fake)
    a = _auth(tmp_path, min_login_interval=0.0)
    a._token = "stale"                     # a token the server no longer likes
    a._expires_at = float("inf")

    with a.urlopen("http://dash/api/state") as resp:
        assert json.loads(resp.read()) == {"ok": True}
    assert any(u.endswith("/auth/login") for u, _ in seen)


def test_throttled_401_reraises_instead_of_retrying_blind(tmp_path, monkeypatch):
    """When the floor blocks the refresh, the caller sees the 401."""
    calls = _login_counter(monkeypatch, ok=False)
    a = _auth(tmp_path, min_login_interval=30.0)
    a.token(force=True)                     # burns the one allowed attempt
    before = len(calls)

    def _always_401(req, timeout=None):
        url = getattr(req, "full_url", None) or req.get_full_url()
        raise urllib.error.HTTPError(url, 401, "no", hdrs=None, fp=None)

    monkeypatch.setattr(urllib.request, "urlopen", _always_401)
    with pytest.raises(urllib.error.HTTPError) as e:
        a.urlopen("http://dash/api/state")
    assert e.value.code == 401
    assert len(calls) == before, "a throttled refresh must not POST /auth/login"


def test_non_401_errors_are_not_retried(tmp_path, monkeypatch):
    calls = _login_counter(monkeypatch)
    a = _auth(tmp_path)
    a.token()
    n = len(calls)

    def _500(req, timeout=None):
        url = getattr(req, "full_url", None) or req.get_full_url()
        raise urllib.error.HTTPError(url, 500, "boom", hdrs=None, fp=None)

    monkeypatch.setattr(urllib.request, "urlopen", _500)
    with pytest.raises(urllib.error.HTTPError) as e:
        a.urlopen("http://dash/api/state")
    assert e.value.code == 500
    assert len(calls) == n


def test_headers_carry_the_user_agent_and_bearer(tmp_path, monkeypatch):
    _login_counter(monkeypatch)
    a = _auth(tmp_path, user_agent="trading-helper-desk/1.0")
    h = a.headers(json_body=True)
    assert h["User-Agent"] == "trading-helper-desk/1.0"
    assert h["Authorization"] == "Bearer t-1"
    assert h["Content-Type"] == "application/json"


def test_for_process_returns_one_instance_per_name(tmp_path):
    a = desk_auth.for_process("unit-test-proc", tmp_path, verbose=False)
    b = desk_auth.for_process("unit-test-proc", tmp_path, verbose=False)
    assert a is b
    desk_auth._INSTANCES.pop("unit-test-proc", None)
