"""
auth.py — JWT authentication for the Signal Scanner dashboard.

Supports multiple user accounts stored in users.json.
Passwords are hashed securely using PBKDF2-SHA256.

users.json format:
  {
    "jmb":   { "hash": "salt:hexhash", "admin": true },
    "alice": { "hash": "salt:hexhash", "admin": false }
  }

Quick setup:
  1. Start the server — auth is enabled by default.
  2. Go to /register to create your first account.
  3. Log in at /login.

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
import time
from pathlib import Path

log = logging.getLogger(__name__)

_SECRETS_FILE = Path(__file__).parent / "config" / "secrets.json"
_USERS_FILE   = Path(__file__).parent / "users.json"
TOKEN_TTL     = 86400  # 24 hours


# ── Secrets / config helpers ──────────────────────────────────────────────────

def _load_secrets() -> dict:
    if _SECRETS_FILE.exists():
        try:
            return json.loads(_SECRETS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


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

def _load_users() -> dict:
    """
    Return dict of {username: {"hash": str, "admin": bool}}.
    Handles legacy format {username: "salt:hash"} transparently.
    """
    if _USERS_FILE.exists():
        try:
            raw = json.loads(_USERS_FILE.read_text(encoding="utf-8"))
            # Normalise legacy plain-string format → new object format
            normalised = {}
            for uname, val in raw.items():
                if isinstance(val, str):
                    normalised[uname] = {"hash": val, "admin": False}
                else:
                    normalised[uname] = val
            return normalised
        except Exception:
            pass
    return {}


def _save_users(users: dict):
    _USERS_FILE.write_text(json.dumps(users, indent=2), encoding="utf-8")


def user_exists(username: str) -> bool:
    return username.lower() in _load_users()


def create_user(username: str, password: str, admin: bool = False) -> bool:
    """
    Create a new user account. Returns True on success, False if username taken.
    Usernames are stored lowercased.
    """
    username = username.strip().lower()
    if not username or not password:
        return False
    users = _load_users()
    if username in users:
        return False
    users[username] = {"hash": _hash_password(password), "admin": admin}
    _save_users(users)
    log.info("[AUTH] New account created: %s (admin=%s)", username, admin)
    return True


def set_admin(username: str, admin: bool) -> bool:
    """Grant or revoke admin on an existing user. Returns True if user was found."""
    username = username.lower()
    users = _load_users()
    if username not in users:
        return False
    users[username]["admin"] = admin
    _save_users(users)
    log.info("[AUTH] Admin set to %s for user: %s", admin, username)
    return True


def is_admin_user(username: str) -> bool:
    """Return True if the given username has admin privileges."""
    if not username:
        return False
    users = _load_users()
    entry = users.get(username.lower())
    if entry is None:
        return False
    return bool(entry.get("admin", False))


# ── Credential check — multi-user first, legacy fallback ─────────────────────

def check_credentials(username: str, password: str) -> bool:
    username = username.strip().lower()
    users = _load_users()

    if users:
        stored_entry = users.get(username)
        if stored_entry is None:
            return False
        stored_hash = stored_entry.get("hash", "") if isinstance(stored_entry, dict) else stored_entry
        return _verify_password(password, stored_hash)

    # Legacy single-user fallback (secrets.json / env vars)
    legacy_user = _secret("dashboard_user", "DASHBOARD_USER", "admin")
    legacy_pass = _secret("dashboard_pass", "DASHBOARD_PASS", "changeme")
    return (
        hmac.compare_digest(username.encode(), legacy_user.lower().encode()) and
        hmac.compare_digest(password.encode(), legacy_pass.encode())
    )


# ── JWT ───────────────────────────────────────────────────────────────────────

def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64d(s: str) -> bytes:
    pad = 4 - len(s) % 4
    return base64.urlsafe_b64decode(s + "=" * (pad % 4))


def _sign(msg: str) -> str:
    return _b64(hmac.new(_jwt_secret().encode(), msg.encode(), hashlib.sha256).digest())


def create_token(username: str = "") -> str:
    hdr = _b64(b'{"alg":"HS256","typ":"JWT"}')
    pay = _b64(json.dumps({
        "exp":   int(time.time()) + TOKEN_TTL,
        "sub":   username,
        "admin": is_admin_user(username),
    }).encode())
    return f"{hdr}.{pay}.{_sign(f'{hdr}.{pay}')}"


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
    return False


# ── Bootstrap: ensure jmb is always an admin ─────────────────────────────────

def _ensure_jmb_admin():
    """If a 'jmb' account exists, make sure it has admin=True."""
    users = _load_users()
    if "jmb" in users and not users["jmb"].get("admin"):
        users["jmb"]["admin"] = True
        _save_users(users)
        log.info("[AUTH] Granted admin to existing user: jmb")

_ensure_jmb_admin()
