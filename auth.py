"""
auth.py — JWT authentication for the Signal Scanner dashboard.

Credentials are read (in priority order) from:
  1. secrets.json  keys: dashboard_user, dashboard_pass, jwt_secret
  2. Environment variables: DASHBOARD_USER, DASHBOARD_PASS, JWT_SECRET
  3. Hardcoded defaults (local dev only — change before exposing publicly)

Add to secrets.json to set credentials:
  {
    "dashboard_user": "yourname",
    "dashboard_pass": "yourpassword",
    "jwt_secret":     "a-long-random-string"
  }
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

_SECRETS_FILE = Path(__file__).parent / "secrets.json"
TOKEN_TTL     = 86400  # 24 hours


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


def _dashboard_user() -> str:
    return _secret("dashboard_user", "DASHBOARD_USER", "admin")


def _dashboard_pass() -> str:
    return _secret("dashboard_pass", "DASHBOARD_PASS", "changeme")


# ── JWT ───────────────────────────────────────────────────────────────────────

def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64d(s: str) -> bytes:
    pad = 4 - len(s) % 4
    return base64.urlsafe_b64decode(s + "=" * (pad % 4))


def _sign(msg: str) -> str:
    return _b64(hmac.new(_jwt_secret().encode(), msg.encode(), hashlib.sha256).digest())


def create_token() -> str:
    hdr = _b64(b'{"alg":"HS256","typ":"JWT"}')
    pay = _b64(json.dumps({"exp": int(time.time()) + TOKEN_TTL}).encode())
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


def check_credentials(username: str, password: str) -> bool:
    return (
        hmac.compare_digest(username.encode(), _dashboard_user().encode()) and
        hmac.compare_digest(password.encode(), _dashboard_pass().encode())
    )
