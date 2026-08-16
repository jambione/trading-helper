"""
login_log.py — Login event audit trail.

Records every login attempt (success or failure) with timestamp, IP, and
user-agent. Prefer Cloudflare CF-IPCountry immediately; city/region is filled
asynchronously via ip-api.com when available.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
LOGIN_LOG_FILE = Path(__file__).parent / "login_log.json"
MAX_ENTRIES = 500

_lock = threading.Lock()


def _load() -> list:
    if not LOGIN_LOG_FILE.exists():
        return []
    try:
        data = json.loads(LOGIN_LOG_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        log.exception("[login_log] failed to load %s", LOGIN_LOG_FILE)
        return []


def _save(entries: list) -> None:
    LOGIN_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = LOGIN_LOG_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    tmp.replace(LOGIN_LOG_FILE)


def _geo_lookup(ip: str) -> dict:
    if ip in ("127.0.0.1", "::1", "localhost", "unknown", ""):
        return {"city": "localhost", "region": "", "country": "local", "country_code": "LO"}
    try:
        url = f"http://ip-api.com/json/{ip}?fields=city,regionName,country,countryCode,status"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
        if data.get("status") == "success":
            return {
                "city": data.get("city") or "",
                "region": data.get("regionName") or "",
                "country": data.get("country") or "",
                "country_code": data.get("countryCode") or "",
            }
    except Exception:
        pass
    return {}


def record_login(
    username: str,
    ip: str,
    user_agent: str = "",
    success: bool = True,
    cf_country: str = "",
):
    """Append a login event and resolve geo in a background thread."""
    timestamp = datetime.now(ET).isoformat(timespec="seconds")
    cc = (cf_country or "").upper()
    location = None
    if cc and cc not in ("XX", "T1"):
        location = {
            "city": "",
            "region": "",
            "country": cc,
            "country_code": cc,
            "source": "cf",
        }
    elif ip in ("127.0.0.1", "::1", "localhost"):
        location = {"city": "localhost", "region": "", "country": "local", "country_code": "LO"}

    entry = {
        "username": (username or "").strip(),
        "timestamp": timestamp,
        "success": bool(success),
        "ip": (ip or "unknown").strip(),
        "user_agent": user_agent or "",
        "cf_country": cc or None,
        "location": location,
    }

    with _lock:
        entries = _load()
        entries.append(entry)
        if len(entries) > MAX_ENTRIES:
            entries = entries[-MAX_ENTRIES:]
        try:
            _save(entries)
        except Exception:
            log.exception("[login_log] failed to save")
            return

    def _fill_geo():
        geo = _geo_lookup(ip)
        if not geo:
            return
        with _lock:
            rows = _load()
            for row in reversed(rows):
                if row.get("timestamp") == timestamp and row.get("ip") == ip:
                    merged = dict(row.get("location") or {})
                    merged.update({k: v for k, v in geo.items() if v})
                    merged["source"] = "ip-api"
                    row["location"] = merged
                    break
            try:
                _save(rows)
            except Exception:
                log.exception("[login_log] failed to save geo update")

    threading.Thread(target=_fill_geo, daemon=True, name="login-geo").start()


def get_log() -> list:
    """Return all entries, newest first."""
    with _lock:
        return list(reversed(_load()))


def get_log_for_user(username: str, limit: int = 25) -> list:
    """Recent login attempts for one username, newest first."""
    want = (username or "").strip().lower()
    if not want:
        return []
    rows = [
        row for row in get_log()
        if (row.get("username") or "").strip().lower() == want
    ]
    return rows[: max(1, int(limit))]
