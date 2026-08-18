"""One client-side dashboard auth path for every desk process.

Five processes used to carry their own copy of "log in, cache the token, retry
once on 401" — discord_source, signal_engine, ai_entry_watch, mac_agent and
windows_agent — and they had drifted apart. Two honoured ``expires_in``, two
never expired the token at all; one took a lock around the refresh, the rest
raced; each read credentials from a different file. Three of the ten commits
before 2026-08-18 were the same auth bug fixed in a different copy.

The reason this is one module now, and not five near-copies, is the login
storm on 2026-08-18: ``ai_entry_watch`` re-logged-in on *every* 401 with no
backoff, while ``dashboard_state()`` runs on a ~2s tick. When the desk was not
yet authorised for ``/api/state``, one misconfiguration became **2,639 logins
in a day, peaking at 398 in a single minute** — each one a 20ms PBKDF2 verify
on the dashboard plus a login-log row, which also burned the account's daily
email quota. The server-side limiter never saw it: it only counted *failed*
logins, and these all succeeded.

So the contract here is deliberately narrow:

* **One login in flight.** ``_lock`` serialises refreshes and every waiter
  re-checks the cache after acquiring it, so N threads hitting an empty token
  produce one POST, not N. Waiters block for the length of that POST rather
  than racing it — bounded by ``timeout``, and no worse for a 2s book tick
  than the slow ``/api/state`` fetch it already tolerates.
* **A floor between attempts.** ``min_login_interval`` (default 30s), backed
  off exponentially to ``max_backoff`` after consecutive failures. A 401 loop
  therefore costs at most ~2 logins/minute per process instead of ~400.
* **A throttled refresh still fails closed.** When the floor blocks a refresh
  ``token()`` returns ``""`` and :meth:`urlopen` re-raises the original 401
  rather than retrying blind — callers already treat that as a fetch failure.

The floor does not delay the honest cases. The first login of a process is
never throttled, and an expired 30-day token 401s long after the last attempt,
so it refreshes immediately. Only *repeated* failures inside the window wait.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

# Files that can carry DASHBOARD_URL / DASHBOARD_USER / DASHBOARD_PASS, in the
# order they are consulted. The shell always wins over both: desk_core's loader
# only injects keys that are not already set. ``.env`` is the operator's file
# (mac_agent reads it); ``signal_engine.env`` is the engine's copy and only
# fills gaps. Keeping the order identical in every process is the point — the
# clients used to disagree about which file was authoritative.
ENV_FILES = (".env", "signal_engine.env")

DEFAULT_USER_AGENT = "trading-helper-desk/1.0"

# Seconds between login attempts from one process. See the module docstring.
DEFAULT_MIN_LOGIN_INTERVAL = 30.0
DEFAULT_MAX_BACKOFF = 300.0

# Refresh this long before the token actually expires, so a live request never
# races the expiry. Tokens are 30 days, so this is nowhere near hot.
DEFAULT_REFRESH_BEFORE = 300.0


class DashboardAuth:
    """Bearer-token client for the dashboard, shared by every desk process.

    One instance per process (see :func:`for_process`). Thread-safe.
    """

    def __init__(
        self,
        root: Path | str,
        *,
        default_url: str = "http://localhost:8888",
        user_agent: str = DEFAULT_USER_AGENT,
        log_prefix: str = "[auth]",
        timeout: float = 10.0,
        min_login_interval: float = DEFAULT_MIN_LOGIN_INTERVAL,
        max_backoff: float = DEFAULT_MAX_BACKOFF,
        refresh_before: float = DEFAULT_REFRESH_BEFORE,
        verbose: bool = True,
    ) -> None:
        self.root = Path(root)
        self.default_url = default_url.rstrip("/")
        self.user_agent = user_agent
        self.log_prefix = log_prefix
        self.timeout = timeout
        self.min_login_interval = float(min_login_interval)
        self.max_backoff = float(max_backoff)
        self.refresh_before = float(refresh_before)
        self.verbose = verbose

        self._lock = threading.Lock()
        self._token = ""
        self._expires_at = 0.0
        # None (not 0.0) means "never attempted" — a real monotonic clock is
        # large on a machine with uptime, so 0.0 would read as "long ago" by
        # luck rather than by intent.
        self._last_attempt: float | None = None
        self._fail_streak = 0
        self._creds_loaded = False

        self.url = self.default_url
        self.user = ""
        self.password = ""

    # ── credentials ──────────────────────────────────────────────────────────

    def load_creds(self, *, force: bool = False) -> tuple[str, str, str]:
        """Resolve URL/user/password from the shell, then :data:`ENV_FILES`.

        Returns ``(url, user, password)`` and caches the result. Safe to call
        on every request — it only touches the filesystem once per process.
        """
        if self._creds_loaded and not force:
            return self.url, self.user, self.password
        self._creds_loaded = True
        try:
            from desk_core import load_env_file
            for name in ENV_FILES:
                load_env_file(self.root / name)
        except Exception:
            # A missing or broken env file must not stop a process that
            # already has credentials in its shell environment.
            pass
        self.url = (os.environ.get("DASHBOARD_URL") or self.default_url).rstrip("/")
        self.user = os.environ.get("DASHBOARD_USER", "")
        self.password = os.environ.get("DASHBOARD_PASS", "")
        return self.url, self.user, self.password

    def set_creds(self, url: str, user: str, password: str) -> None:
        """Use these credentials verbatim and stop consulting the env files.

        For callers that already resolved credentials their own way
        (signal_engine passes its module globals in).
        """
        self.url = (url or self.default_url).rstrip("/")
        self.user = user or ""
        self.password = password or ""
        self._creds_loaded = True

    # ── token ────────────────────────────────────────────────────────────────

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"{self.log_prefix} {msg}", flush=True)

    def _wait_needed(self) -> float:
        """Seconds still to wait before another login attempt is allowed."""
        if self._last_attempt is None:
            return 0.0
        if self._fail_streak <= 0:
            window = self.min_login_interval
        else:
            window = min(
                self.min_login_interval * (2 ** self._fail_streak),
                self.max_backoff,
            )
        elapsed = time.monotonic() - self._last_attempt
        return max(0.0, window - elapsed)

    def _token_is_fresh(self) -> bool:
        return bool(self._token) and time.monotonic() < (
            self._expires_at - self.refresh_before)

    def token(self, *, force: bool = False) -> str:
        """Cached Bearer token, logging in when needed. ``""`` on failure.

        ``force=True`` ignores the cached token (use it after a 401) but still
        respects the attempt floor — that is what stops a 401 loop from
        becoming a login storm.
        """
        if not force and self._token_is_fresh():
            return self._token
        with self._lock:
            # Re-check inside the lock: while we waited, another thread may
            # have refreshed. This is what collapses a thundering herd of
            # first-request threads into a single login.
            if self._token_is_fresh() and not force:
                return self._token
            url, user, password = self.load_creds()
            if not user or not password:
                return ""
            wait = self._wait_needed()
            if wait > 0:
                # Deliberately silent-ish: this fires on every request during
                # an outage, and a per-request log line is its own flood.
                return ""
            self._last_attempt = time.monotonic()
            tok, ttl, err = self._post_login(url, user, password)
            if tok:
                self._token = tok
                self._expires_at = time.monotonic() + max(ttl, 60.0)
                if self._fail_streak:
                    self._log(f"logged in as {user!r} after "
                              f"{self._fail_streak} failed attempt(s)")
                else:
                    self._log(f"logged in as {user!r} — "
                              f"token valid for {max(ttl, 60.0) / 3600:.0f}h")
                self._fail_streak = 0
                return self._token
            self._token = ""
            self._expires_at = 0.0
            self._fail_streak += 1
            self._log(f"login failed ({err}) — next attempt in "
                      f"{self._wait_needed():.0f}s")
            return ""

    def _post_login(self, url: str, user: str, password: str
                    ) -> tuple[str, float, str]:
        """POST /auth/login. Returns ``(token, ttl_seconds, error)``."""
        body = json.dumps({"username": user, "password": password}).encode()
        req = urllib.request.Request(
            f"{url}/auth/login",
            data=body,
            headers={"Content-Type": "application/json",
                     "User-Agent": self.user_agent},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode() or "{}")
        except Exception as e:  # noqa: BLE001 — any failure is one failure
            return "", 0.0, f"{type(e).__name__}: {e}"[:200]
        tok = str(data.get("token") or data.get("access_token") or "")
        if not tok:
            return "", 0.0, str(data.get("error") or "no token in response")
        try:
            ttl = float(data.get("expires_in") or 3600.0)
        except (TypeError, ValueError):
            ttl = 3600.0
        return tok, ttl, ""

    # ── requests ─────────────────────────────────────────────────────────────

    def headers(self, *, json_body: bool = False) -> dict[str, str]:
        out = {"User-Agent": self.user_agent}
        if json_body:
            out["Content-Type"] = "application/json"
        tok = self.token()
        if tok:
            out["Authorization"] = f"Bearer {tok}"
        return out

    def urlopen(self, url: str, *, data: bytes | None = None,
                method: str | None = None, timeout: float | None = None):
        """urllib request with auth and **one** throttled re-login on 401.

        When the floor blocks the refresh the original ``HTTPError`` is
        re-raised rather than retried, so an outage surfaces as a failed fetch
        instead of a login flood.
        """
        req_timeout = self.timeout if timeout is None else timeout

        def _open():
            req = urllib.request.Request(
                url,
                data=data,
                headers=self.headers(json_body=data is not None),
                method=method,
            )
            return urllib.request.urlopen(req, timeout=req_timeout)

        try:
            return _open()
        except urllib.error.HTTPError as e:
            if e.code != 401:
                raise
            if not self.token(force=True):
                raise
            return _open()

    # ── testing ──────────────────────────────────────────────────────────────

    def reset(self) -> None:
        """Drop the cached token, credentials and backoff state."""
        with self._lock:
            self._token = ""
            self._expires_at = 0.0
            self._last_attempt = None
            self._fail_streak = 0
            self._creds_loaded = False


_INSTANCES: dict[str, DashboardAuth] = {}
_INSTANCES_LOCK = threading.Lock()


def for_process(name: str, root: Path | str, **kwargs) -> DashboardAuth:
    """Return the process-wide :class:`DashboardAuth` for *name*.

    One instance per caller name means one token and one backoff clock per
    process, however many modules ask for it.
    """
    with _INSTANCES_LOCK:
        inst = _INSTANCES.get(name)
        if inst is None:
            inst = DashboardAuth(root, **kwargs)
            _INSTANCES[name] = inst
        return inst
