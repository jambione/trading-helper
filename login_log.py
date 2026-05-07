"""
login_log.py — Login event audit trail.

Records every login attempt (success or failure) with timestamp, IP, and
user-agent. Geo-location is resolved asynchronously via ip-api.com (free,
no key required) and written back once available.
"""

import json
import logging
import threading
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

ET            = ZoneInfo("America/New_York")
LOGIN_LOG_FILE = Path(__file__).parent / "login_log.json"
MAX_ENTRIES   = 500

_lock = threading.Lock()


def _load() -> list:
    if not LOGIN_LOG_FILE.exists():
        return []
    try:
        return json.loads(LOGIN_LOG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save(entries: list):
    LOGIN_LOG_FILE.write_text(json.dumps(entries, indent=2), encoding="utf-8")


def _geo_lookup(ip: str) -> dict:
    if ip in ("127.0.0.1", "::1", "localhost", "unknown"):
        return {"city": "localhost", "region": "", "country": "local"}
    try:
        url = f"http://ip-api.com/json/{ip}?fields=city,regionName,country,countryCode,status"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
        if data.get("status") == "success":
            return {
                "city":         data.get("city", ""),
                "region":       data.get("regionName", ""),
                "country":      data.get("country", ""),
                "country_code": data.get("countryCode", ""),
            }
    except Exception:
        pass
    return {}


def record_login(username: str, ip: str, user_agent: str = "", success: bool = True):
    """Append a login event and resolve geo in a background thread."""
    timestamp = datetime.now(ET).isoformat(timespec="seconds")
    entry = {
        "username":   username,
        "timestamp":  timestamp,
        "success":    success,
        "ip":         ip,
        "user_agent": user_agent,
        "location":   None,
    }

    with _lock:
        entries = _load()
        entries.append(entry)
        if len(entries) > MAX_ENTRIES:
            entries = entries[-MAX_ENTRIES:]
        _save(entries)

    def _fill_geo():
        geo = _geo_lookup(ip)
        if not geo:
            return
        with _lock:
            rows = _load()
            for row in reversed(rows):
                if row.get("timestamp") == timestamp and row.get("ip") == ip:
                    row["location"] = geo
                    break
            _save(rows)

    threading.Thread(target=_fill_geo, daemon=True, name="geo-lookup").start()


def get_log() -> list:
    """Return all entries, newest first."""
    with _lock:
        return list(reversed(_load()))
