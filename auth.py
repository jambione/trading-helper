from __future__ import annotations
"""
auth.py — JWT authentication for the Signal Scanner dashboard.

Supports multiple user accounts stored in users.json.
Passwords are hashed securely using PBKDF2-SHA256.

users.json format (legacy {username: "salt:hash"} still loads):
  {
    "jmb": {
      "hash": "salt:hexhash",
      "admin": true,
      "status": "active",
      "display_name": "Jonathan",
      "email": "you@example.com",
      "created_at": "...",
      "broker":  { "provider": "alpaca", "key": "", "secret": "", "enabled": false },
      "billing": { "plan": "none", "status": "none" }
    }
  }

``broker`` and ``billing`` are reserved for later (personal Alpaca keys,
paid plans). They are stored now so we do not have to migrate again.

The owner admin is always ``jmb`` (display name JMB, email jambione@icloud.com).
That account cannot be disabled or demoted. JMB reviews access requests and
can fix any other user's name, email, status, or password.

Quick setup:
  1. Start the server — auth is enabled by default.
  2. Sign in as jmb. Later signups request access; JMB approves them.
  3. Log in at /login.

Sessions last 30 days and slide forward on each dashboard visit
(HttpOnly ``tb_session`` cookie + JWT). Sign out is the only way to
end them early. Override lifetime with secrets.json ``token_ttl_seconds``.

To disable auth entirely (local dev), add to secrets.json:
  { "require_auth": false }
Or set environment variable REQUIRE_AUTH=false.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

_SECRETS_FILE = Path(__file__).parent / "config" / "secrets.json"
_USERS_FILE   = Path(__file__).parent / "users.json"
# Sliding session: login (and each dashboard visit) lasts this long unless
# secrets.json ``token_ttl_seconds`` or TOKEN_TTL_SECONDS overrides it.
TOKEN_TTL     = 30 * 86400
COOKIE_NAME   = "tb_session"
_MIN_TTL_SEC  = 3600


# ── Secrets / config helpers ──────────────────────────────────────────────────

# (path, mtime_ns, size, parsed). is_auth_required() runs on every request and
# every _sign() goes through _jwt_secret(), so an uncached read meant ~4 reads
# and 4 json.loads of secrets.json per authenticated request. Keyed on
# path+mtime+size so an edited file is still picked up without a restart, and
# so tests that point _SECRETS_FILE at a tmp file never see another file's
# cache. Callers only ever .get() the result, so sharing the dict is safe.
_SECRETS_CACHE: tuple[str, int, int, dict] = ("", 0, 0, {})


def _load_secrets() -> dict:
    global _SECRETS_CACHE
    path = str(_SECRETS_FILE)
    try:
        st = _SECRETS_FILE.stat()
    except OSError:
        _SECRETS_CACHE = ("", 0, 0, {})
        return {}
    c_path, c_mtime, c_size, cached = _SECRETS_CACHE
    if (cached and c_path == path
            and c_mtime == st.st_mtime_ns and c_size == st.st_size):
        return cached
    try:
        data = json.loads(_SECRETS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    _SECRETS_CACHE = (path, st.st_mtime_ns, st.st_size, data)
    return data


def _secret(key: str, env_var: str, default: str) -> str:
    return _load_secrets().get(key) or os.getenv(env_var) or default


def _jwt_secret() -> str:
    val = _secret("jwt_secret", "JWT_SECRET", "")
    if not val:
        log.warning("[AUTH] jwt_secret not set — using insecure default. Add it to secrets.json!")
        return "dev-insecure-secret-please-change"
    return val


# ── Password hashing (PBKDF2-SHA256, standard library only) ──────────────────

def _hash_password(password: str, salt: str | None = None) -> str:
    """Return 'salt:hash' string. Generates a new salt if none provided."""
    if salt is None:
        salt = base64.urlsafe_b64encode(os.urandom(16)).decode()
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode(), 260_000)
    return f"{salt}:{dk.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    """Constant-time check of password against the stored 'salt:hash' string."""
    try:
        salt, _ = stored.split(":", 1)
        return hmac.compare_digest(stored, _hash_password(password, salt))
    except Exception:
        return False


# ── Multi-user store (users.json) ─────────────────────────────────────────────

_ET = ZoneInfo("America/New_York")
_USERS_LOCK = threading.Lock()
_STATUSES = ("pending", "active", "disabled", "rejected")
_USER_RE = re.compile(r"^[a-z][a-z0-9_]{2,23}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_RESERVED_NAMES = frozenset({
    "register", "login", "logout", "admin", "root", "system",
    "api", "support", "forgot", "reset", "null", "undefined",
})
RESET_TTL = 3600
MIN_PASSWORD = 8
OWNER_USERNAME = "jmb"
OWNER_DISPLAY_NAME = "JMB"
OWNER_EMAIL = "jambione@icloud.com"


def is_owner(username: str) -> bool:
    return (username or "").strip().lower() == OWNER_USERNAME


def _now_iso() -> str:
    return datetime.now(_ET).isoformat(timespec="seconds")


def _load_users() -> dict:
    """
    Return dict of {username: record}.
    Handles legacy format {username: "salt:hash"} transparently.
    """
    if _USERS_FILE.exists():
        try:
            raw = json.loads(_USERS_FILE.read_text(encoding="utf-8"))
            normalised = {}
            for uname, val in raw.items():
                if isinstance(val, str):
                    normalised[uname] = {"hash": val, "admin": False}
                elif isinstance(val, dict):
                    normalised[uname] = val
            return normalised
        except Exception:
            pass
    return {}


def _save_users(users: dict):
    _USERS_FILE.write_text(json.dumps(users, indent=2), encoding="utf-8")


def _as_record(username: str, entry) -> dict:
    """Fill defaults without dropping extra keys already on the record."""
    if isinstance(entry, str):
        entry = {"hash": entry}
    rec = dict(entry or {})
    rec.setdefault("hash", "")
    rec.setdefault("admin", False)
    rec.setdefault("status", "active")
    rec.setdefault("display_name", username)
    rec.setdefault("email", "")
    rec.setdefault("created_at", "")
    rec.setdefault("approved_at", "")
    rec.setdefault("approved_by", "")
    rec.setdefault("password_changed_at", "")
    rec.setdefault("reset_token", "")
    rec.setdefault("reset_expires", 0)
    broker = rec.get("broker")
    if not isinstance(broker, dict):
        broker = {}
    broker.setdefault("provider", "alpaca")
    broker.setdefault("key", "")
    broker.setdefault("secret", "")
    broker.setdefault("enabled", False)
    rec["broker"] = broker
    billing = rec.get("billing")
    if not isinstance(billing, dict):
        billing = {}
    billing.setdefault("plan", "none")
    billing.setdefault("status", "none")
    rec["billing"] = billing
    rec["admin"] = bool(rec.get("admin"))
    rec["email"] = str(rec.get("email") or "").strip().lower()
    rec["display_name"] = str(rec.get("display_name") or username).strip()
    if rec.get("status") not in _STATUSES:
        rec["status"] = "active"
    return rec


def _mutate_user(username: str, fn) -> bool:
    username = username.strip().lower()
    with _USERS_LOCK:
        users = _load_users()
        if username not in users:
            return False
        rec = _as_record(username, users[username])
        fn(rec)
        users[username] = rec
        _save_users(users)
        return True


def public_profile(username: str, entry=None, *, admin_view: bool = False) -> dict | None:
    """Safe dict for the API — never includes hashes, reset tokens, or keys."""
    username = (username or "").strip().lower()
    if not username:
        return None
    if entry is None:
        entry = _load_users().get(username)
    if entry is None:
        return None
    rec = _as_record(username, entry)
    broker = rec["broker"]
    out = {
        "username": username,
        "display_name": rec["display_name"],
        "email": rec["email"],
        "admin": rec["admin"],
        "status": rec["status"],
        "created_at": rec["created_at"],
        "billing": {
            "plan": rec["billing"].get("plan", "none"),
            "status": rec["billing"].get("status", "none"),
        },
        "broker": {
            "provider": broker.get("provider", "alpaca"),
            "enabled": bool(broker.get("enabled")),
            "configured": bool(broker.get("key") or broker.get("secret")),
        },
    }
    if admin_view:
        out["approved_at"] = rec.get("approved_at") or ""
        out["approved_by"] = rec.get("approved_by") or ""
        out["password_changed_at"] = rec.get("password_changed_at") or ""
        out["owner"] = is_owner(username)
    return out


def list_public_users(*, admin_view: bool = False) -> list[dict]:
    rows = []
    for uname, entry in _load_users().items():
        profile = public_profile(uname, entry, admin_view=admin_view)
        if profile:
            rows.append(profile)
    rows.sort(key=lambda r: (r["status"] != "pending", r["username"]))
    return rows


def user_exists(username: str) -> bool:
    return username.strip().lower() in _load_users()


def email_taken(email: str, *, except_username: str = "") -> bool:
    email = (email or "").strip().lower()
    if not email:
        return False
    skip = except_username.strip().lower()
    for uname, entry in _load_users().items():
        if uname == skip:
            continue
        rec = _as_record(uname, entry)
        if rec["email"] and rec["email"] == email:
            return True
    return False


def validate_username(username: str) -> str:
    username = (username or "").strip().lower()
    if not username:
        return "Username is required"
    if username in _RESERVED_NAMES:
        return "That username is reserved"
    if not _USER_RE.match(username):
        return "Username must be 3–24 characters, start with a letter, and use only letters, numbers, or _"
    return ""


def validate_email(email: str) -> str:
    email = (email or "").strip()
    if not email:
        return "Email is required"
    if not _EMAIL_RE.match(email) or len(email) > 120:
        return "Enter a valid email address"
    return ""


def validate_password(password: str, username: str = "") -> str:
    if not password:
        return "Password is required"
    if len(password) < MIN_PASSWORD:
        return f"Password must be at least {MIN_PASSWORD} characters"
    if username and password.lower() == username.lower():
        return "Password cannot be the same as your username"
    if password.lower() in {"password", "password1", "changeme", "12345678"}:
        return "Choose a stronger password"
    return ""


def create_user(
    username: str,
    password: str,
    admin: bool = False,
    *,
    email: str = "",
    display_name: str = "",
    status: str = "active",
) -> bool:
    """
    Create an account immediately (tests / first-admin bootstrap).
    Public signups should use request_account() so they land as pending.
    """
    username = username.strip().lower()
    if not username or not password:
        return False
    if status not in _STATUSES:
        return False
    with _USERS_LOCK:
        users = _load_users()
        if username in users:
            return False
        rec = _as_record(username, {
            "hash": _hash_password(password),
            "admin": admin,
            "status": status,
            "email": email,
            "display_name": display_name or username,
            "created_at": _now_iso(),
        })
        if rec["status"] == "active":
            rec["approved_at"] = rec["created_at"]
        users[username] = rec
        _save_users(users)
    log.info("[AUTH] New account created: %s (admin=%s status=%s)", username, admin, status)
    return True


def request_account(
    username: str,
    password: str,
    email: str,
    display_name: str,
) -> tuple[dict | None, str]:
    """
    Public signup. First account on an empty store becomes admin+active.
    Everyone after that is pending until an admin approves.
    Returns (public_profile, error).
    """
    username = username.strip().lower()
    email = email.strip().lower()
    display_name = (display_name or "").strip()
    err = validate_username(username)
    if err:
        return None, err
    err = validate_email(email)
    if err:
        return None, err
    err = validate_password(password, username)
    if err:
        return None, err
    if not display_name or len(display_name) > 60:
        return None, "Enter your name"
    if email == OWNER_EMAIL and username != OWNER_USERNAME:
        return None, "Email is already in use"
    with _USERS_LOCK:
        users = _load_users()
        if username in users:
            return None, "Username is taken"
        for uname, entry in users.items():
            rec = _as_record(uname, entry)
            if rec["email"] and rec["email"] == email:
                return None, "Email is already in use"
        first = len(users) == 0
        status = "active" if first else "pending"
        rec = _as_record(username, {
            "hash": _hash_password(password),
            "admin": first,
            "status": status,
            "email": email,
            "display_name": display_name,
            "created_at": _now_iso(),
            "approved_at": _now_iso() if first else "",
            "approved_by": username if first else "",
            "billing": {"plan": "owner" if first else "none", "status": "none"},
        })
        users[username] = rec
        _save_users(users)
    log.info("[AUTH] Access requested: %s (status=%s first=%s)", username, rec["status"], first)
    return public_profile(username, rec), ""


def set_admin(username: str, admin: bool) -> bool:
    """Grant or revoke admin on an existing user. Returns True if user was found."""
    username = username.lower()
    if is_owner(username) and not admin:
        return False
    ok = _mutate_user(username, lambda rec: rec.__setitem__("admin", bool(admin)))
    if ok:
        log.info("[AUTH] Admin set to %s for user: %s", admin, username)
    return ok


def is_admin_user(username: str) -> bool:
    """Return True if the given username has admin privileges."""
    if not username:
        return False
    if is_owner(username) and username.lower() in _load_users():
        return True
    entry = _load_users().get(username.lower())
    if entry is None:
        return False
    return bool(_as_record(username, entry).get("admin", False))


def user_is_active(username: str) -> bool:
    if not username:
        return False
    entry = _load_users().get(username.lower())
    if entry is None:
        return False
    return _as_record(username, entry)["status"] == "active"


def _admin_count(users: dict | None = None) -> int:
    users = _load_users() if users is None else users
    n = 0
    for uname, entry in users.items():
        rec = _as_record(uname, entry)
        if rec["admin"] and rec["status"] == "active":
            n += 1
    return n


def set_user_status(username: str, status: str, *, by: str = "") -> tuple[bool, str]:
    """Approve / reject / disable / re-enable. Returns (ok, error)."""
    username = username.strip().lower()
    if status not in _STATUSES:
        return False, "Unknown status"
    by = (by or "").strip().lower()
    with _USERS_LOCK:
        users = _load_users()
        if username not in users:
            return False, "User not found"
        rec = _as_record(username, users[username])
        if is_owner(username) and status != "active":
            return False, "The JMB admin account cannot be disabled"
        if by and username == by and status in ("disabled", "rejected"):
            return False, "You cannot disable your own account"
        if rec["admin"] and rec["status"] == "active" and status != "active":
            if _admin_count(users) <= 1:
                return False, "Cannot disable the last admin"
        rec["status"] = status
        if status == "active":
            rec["approved_at"] = _now_iso()
            rec["approved_by"] = by
        users[username] = rec
        _save_users(users)
    log.info("[AUTH] status=%s for %s by %s", status, username, by or "-")
    return True, ""


def change_password(username: str, current: str, new: str) -> tuple[bool, str]:
    username = username.strip().lower()
    err = validate_password(new, username)
    if err:
        return False, err
    users = _load_users()
    entry = users.get(username)
    if entry is None:
        return False, "User not found"
    rec = _as_record(username, entry)
    if not _verify_password(current, rec["hash"]):
        return False, "Current password is incorrect"
    if _verify_password(new, rec["hash"]):
        return False, "New password must be different"

    def _apply(r):
        r["hash"] = _hash_password(new)
        r["password_changed_at"] = _now_iso()
        r["reset_token"] = ""
        r["reset_expires"] = 0

    _mutate_user(username, _apply)
    log.info("[AUTH] Password changed: %s", username)
    return True, ""


def update_profile(username: str, *, display_name: str | None = None,
                   email: str | None = None) -> tuple[dict | None, str]:
    username = username.strip().lower()
    if username not in _load_users():
        return None, "User not found"
    if is_owner(username):
        if email is not None and email.strip().lower() != OWNER_EMAIL:
            return None, "The admin email is fixed"
        if display_name is not None and display_name.strip() and display_name.strip() != OWNER_DISPLAY_NAME:
            return None, "The admin name is fixed"

    def _apply(rec):
        if display_name is not None:
            name = display_name.strip()
            if not name or len(name) > 60:
                raise ValueError("Enter your name")
            rec["display_name"] = name
        if email is not None:
            err = validate_email(email)
            if err:
                raise ValueError(err)
            cleaned = email.strip().lower()
            if email_taken(cleaned, except_username=username):
                raise ValueError("Email is already in use")
            rec["email"] = cleaned

    try:
        if not _mutate_user(username, _apply):
            return None, "User not found"
    except ValueError as e:
        return None, str(e)
    return public_profile(username), ""


def admin_update_user(
    username: str,
    *,
    display_name: str | None = None,
    email: str | None = None,
    status: str | None = None,
    by: str = "",
) -> tuple[dict | None, str]:
    """JMB (or another admin) fixes a user's name, email, or status."""
    username = username.strip().lower()
    users = _load_users()
    if username not in users:
        return None, "User not found"
    if is_owner(username):
        if status is not None and status != "active":
            return None, "The JMB admin account cannot be disabled"
        if email is not None and email.strip().lower() != OWNER_EMAIL:
            return None, "The admin email is fixed"
        if display_name is not None and display_name.strip() != OWNER_DISPLAY_NAME:
            return None, "The admin name is fixed"
    if status is not None:
        ok, err = set_user_status(username, status, by=by)
        if not ok:
            return None, err
    if display_name is not None or email is not None:
        profile, err = update_profile(
            username,
            display_name=display_name,
            email=email,
        )
        if err:
            return None, err
        return profile, ""
    return public_profile(username, admin_view=True), ""


def admin_set_password(username: str, new: str) -> tuple[bool, str]:
    """Set a user's password without knowing the current one (admin repair)."""
    username = username.strip().lower()
    err = validate_password(new, username)
    if err:
        return False, err
    if username not in _load_users():
        return False, "User not found"

    def _apply(r):
        r["hash"] = _hash_password(new)
        r["password_changed_at"] = _now_iso()
        r["reset_token"] = ""
        r["reset_expires"] = 0

    if not _mutate_user(username, _apply):
        return False, "User not found"
    log.info("[AUTH] Password set by admin: %s", username)
    return True, ""


def create_reset_token(email: str) -> tuple[str, str]:
    """
    Issue a one-hour reset token for the account with this email.
    Returns (raw_token, username). Both empty if the email is unknown.
    """
    email = (email or "").strip().lower()
    if validate_email(email):
        return "", ""
    found = ""
    for uname, entry in _load_users().items():
        rec = _as_record(uname, entry)
        if rec["email"] and rec["email"] == email:
            found = uname
            break
    if not found:
        return "", ""
    raw = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
    digest = hashlib.sha256(raw.encode()).hexdigest()
    expires = int(time.time()) + RESET_TTL

    def _apply(rec):
        rec["reset_token"] = digest
        rec["reset_expires"] = expires

    _mutate_user(found, _apply)
    return raw, found


def reset_password(token: str, new: str) -> tuple[bool, str]:
    raw = (token or "").strip()
    if not raw:
        return False, "Reset link is missing or expired"
    digest = hashlib.sha256(raw.encode()).hexdigest()
    now = time.time()
    found = ""
    for uname, entry in _load_users().items():
        rec = _as_record(uname, entry)
        if rec.get("reset_token") and hmac.compare_digest(str(rec["reset_token"]), digest):
            if float(rec.get("reset_expires") or 0) < now:
                return False, "Reset link is missing or expired"
            found = uname
            break
    if not found:
        return False, "Reset link is missing or expired"
    err = validate_password(new, found)
    if err:
        return False, err

    def _apply(r):
        r["hash"] = _hash_password(new)
        r["password_changed_at"] = _now_iso()
        r["reset_token"] = ""
        r["reset_expires"] = 0

    _mutate_user(found, _apply)
    log.info("[AUTH] Password reset: %s", found)
    return True, ""


def resolve_login_id(login_id: str) -> str:
    """Username for a login id (username or email). Empty if unknown."""
    login_id = (login_id or "").strip().lower()
    if not login_id:
        return ""
    users = _load_users()
    if login_id in users:
        return login_id
    if "@" in login_id:
        for uname, entry in users.items():
            rec = _as_record(uname, entry)
            if rec["email"] and rec["email"] == login_id:
                return uname
    return ""


# ── Credential check — multi-user first, legacy fallback ─────────────────────

def authenticate(login_id: str, password: str) -> tuple[str, str]:
    """
    Verify username/email + password.
    Returns (username, error). Username is set only on success.
    """
    login_id = (login_id or "").strip()
    username = resolve_login_id(login_id)
    users = _load_users()

    if users:
        if not username:
            return "", "Invalid credentials"
        rec = _as_record(username, users[username])
        if not _verify_password(password, rec["hash"]):
            return "", "Invalid credentials"
        if rec["status"] == "pending":
            return "", "Your access request is still pending"
        if rec["status"] != "active":
            return "", "This account is not active"
        return username, ""

    legacy_user = _secret("dashboard_user", "DASHBOARD_USER", "admin")
    legacy_pass = _secret("dashboard_pass", "DASHBOARD_PASS", "changeme")
    guess = login_id.strip().lower()
    if (
        hmac.compare_digest(guess.encode(), legacy_user.lower().encode()) and
        hmac.compare_digest(password.encode(), legacy_pass.encode())
    ):
        return legacy_user.lower(), ""
    return "", "Invalid credentials"


def check_credentials(username: str, password: str) -> bool:
    user, err = authenticate(username, password)
    return bool(user) and not err


# ── JWT ───────────────────────────────────────────────────────────────────────

def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64d(s: str) -> bytes:
    pad = 4 - len(s) % 4
    return base64.urlsafe_b64decode(s + "=" * (pad % 4))


def _sign(msg: str) -> str:
    return _b64(hmac.new(_jwt_secret().encode(), msg.encode(), hashlib.sha256).digest())


def token_ttl_seconds() -> int:
    """How long a new session lasts. Default 30 days; override in secrets.json."""
    val = _load_secrets().get("token_ttl_seconds")
    if val is None:
        val = os.getenv("TOKEN_TTL_SECONDS", "")
    try:
        n = int(val)
        if n >= _MIN_TTL_SEC:
            return n
    except (TypeError, ValueError):
        pass
    return TOKEN_TTL


def create_token(username: str = "", ttl: int | None = None) -> str:
    now = int(time.time())
    life = token_ttl_seconds() if ttl is None else max(int(ttl), 1)
    hdr = _b64(b'{"alg":"HS256","typ":"JWT"}')
    pay = _b64(json.dumps({
        "exp":   now + life,
        "iat":   now,
        "jti":   base64.urlsafe_b64encode(os.urandom(8)).decode().rstrip("="),
        "sub":   username,
        "admin": is_admin_user(username),
    }).encode())
    return f"{hdr}.{pay}.{_sign(f'{hdr}.{pay}')}"


def first_valid_token(*candidates: str) -> str:
    """First non-empty candidate that verifies, else the first non-empty one."""
    fallback = ""
    for raw in candidates:
        tok = (raw or "").strip()
        if not tok:
            continue
        if not fallback:
            fallback = tok
        if verify_token(tok):
            return tok
    return fallback


def request_is_https(request) -> bool:
    """True when the browser reached us over HTTPS (incl. Cloudflare)."""
    headers = getattr(request, "headers", {}) or {}
    forwarded = str(headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip().lower()
    if forwarded in ("https", "on"):
        return True
    if forwarded == "http":
        return False
    visitor = str(headers.get("cf-visitor") or "").lower()
    if "https" in visitor:
        return True
    url = getattr(request, "url", None)
    return bool(url is not None and getattr(url, "scheme", "") == "https")


def set_session_cookie(response, token: str, *, secure: bool = False) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=token_ttl_seconds(),
        path="/",
        httponly=True,
        samesite="lax",
        secure=secure,
    )


def clear_session_cookie(response, *, secure: bool = False) -> None:
    response.delete_cookie(
        COOKIE_NAME,
        path="/",
        samesite="lax",
        secure=secure,
    )


def verify_token(token: str) -> bool:
    if not token:
        return False
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return False
        h, p, s = parts
        if not hmac.compare_digest(s, _sign(f"{h}.{p}")):
            return False
        data = json.loads(_b64d(p))
        return data.get("exp", 0) > time.time()
    except Exception:
        return False


def get_token_username(token: str) -> str:
    """Decode a verified token and return the username ('sub' claim). Returns '' on failure."""
    if not token:
        return ""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return ""
        h, p, s = parts
        if not hmac.compare_digest(s, _sign(f"{h}.{p}")):
            return ""
        data = json.loads(_b64d(p))
        if data.get("exp", 0) <= time.time():
            return ""
        return str(data.get("sub", ""))
    except Exception:
        return ""


# ── Auth toggle ───────────────────────────────────────────────────────────────

def is_auth_required() -> bool:
    """
    Returns True when dashboard auth is required.

    Config (first match wins):
      1. secrets.json key ``require_auth``
      2. env REQUIRE_AUTH
      3. default False (open dashboard; traffic_log still records visitors)

    Set require_auth true in config/secrets.json to force login for remote use.
    """
    val = _load_secrets().get("require_auth")
    if val is None:
        val = os.getenv("REQUIRE_AUTH", "false")
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() not in ("false", "0", "no", "")


# ── Bootstrap: JMB is the owner admin ────────────────────────────────────────

def ensure_owner_admin():
    """Keep jmb as the owner: admin, active, name JMB, email jambione@icloud.com."""
    with _USERS_LOCK:
        users = _load_users()
        if OWNER_USERNAME not in users:
            return
        rec = _as_record(OWNER_USERNAME, users[OWNER_USERNAME])
        changed = False
        if not rec.get("admin"):
            rec["admin"] = True
            changed = True
        if rec.get("status") != "active":
            rec["status"] = "active"
            changed = True
        if rec.get("display_name") != OWNER_DISPLAY_NAME:
            rec["display_name"] = OWNER_DISPLAY_NAME
            changed = True
        if rec.get("email") != OWNER_EMAIL:
            rec["email"] = OWNER_EMAIL
            changed = True
        if rec["billing"].get("plan") != "owner":
            rec["billing"]["plan"] = "owner"
            changed = True
        if changed:
            users[OWNER_USERNAME] = rec
            _save_users(users)
            log.info("[AUTH] Owner admin pinned: %s <%s>", OWNER_DISPLAY_NAME, OWNER_EMAIL)

ensure_owner_admin()
