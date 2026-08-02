"""
traffic_log.py — Who is hitting the dashboard (and from where).

Records meaningful HTTP hits with IP, Cloudflare country (when present),
async city/region geo, user-agent, path, method, status, and optional username.

Designed to stay quiet on high-frequency polls (/api/state) while still
capturing page loads, auth, mutations, and occasional "presence" heartbeats
so an open dashboard tab shows up in the log.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
TRAFFIC_LOG_FILE = Path(__file__).parent / "traffic_log.json"
MAX_ENTRIES = 2000

# Paths we never record (noise / static).
_SKIP_EXACT = frozenset({
    "/favicon.ico",
    "/robots.txt",
    "/cdn-cgi/rum",
})
_SKIP_PREFIXES = (
    "/static/",
    "/cdn-cgi/",
)

# High-frequency endpoints: at most one presence event per IP in this window.
_PRESENCE_PATHS = frozenset({
    "/api/state",
    "/static/ticker_feed.json",
    "/api/ticker-feed",
})
_PRESENCE_DEBOUNCE_S = 30 * 60  # 30 minutes

# Other GETs: collapse repeats from the same IP+path.
_GET_DEBOUNCE_S = 15 * 60  # 15 minutes

# Always record these GETs (page / auth shell).
_ALWAYS_GET = frozenset({
    "/",
    "/login",
    "/register",
})

_lock = threading.Lock()
# key -> monotonic last-record time
_debounce: dict[str, float] = {}


def client_ip_from_request(request) -> str:
    """Prefer Cloudflare / proxy real IP headers, then peer address."""
    try:
        headers = request.headers
        ip = (
            headers.get("CF-Connecting-IP")
            or headers.get("cf-connecting-ip")
            or (headers.get("X-Forwarded-For") or headers.get("x-forwarded-for") or "").split(",")[0].strip()
            or (request.client.host if request.client else "")
        )
        return (ip or "unknown").strip()
    except Exception:
        return "unknown"


def client_meta_from_request(request) -> dict:
    """IP, UA, and free Cloudflare edge fields (no external call)."""
    headers = request.headers
    return {
        "ip": client_ip_from_request(request),
        "user_agent": headers.get("user-agent") or headers.get("User-Agent") or "",
        "cf_country": (headers.get("CF-IPCountry") or headers.get("cf-ipcountry") or "").upper(),
        "cf_ray": headers.get("CF-Ray") or headers.get("cf-ray") or "",
    }


def _load() -> list:
    if not TRAFFIC_LOG_FILE.exists():
        return []
    try:
        data = json.loads(TRAFFIC_LOG_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        log.exception("[traffic] failed to load %s", TRAFFIC_LOG_FILE)
        return []


def _save(entries: list) -> None:
    """Atomic write so a crash mid-write does not corrupt the log."""
    TRAFFIC_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = TRAFFIC_LOG_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    tmp.replace(TRAFFIC_LOG_FILE)


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


def _location_seed(cf_country: str) -> dict | None:
    """Immediate location from CF header; city filled in async later."""
    if not cf_country or cf_country in ("XX", "T1"):
        return None
    # T1 = Tor, XX = unknown
    return {
        "city": "",
        "region": "",
        "country": cf_country,
        "country_code": cf_country,
        "source": "cf",
    }


def _should_record(method: str, path: str, ip: str) -> tuple[bool, str]:
    """
    Return (record?, event_kind).

    event_kind: page | auth | action | presence | api
    """
    method = (method or "GET").upper()
    path = path or "/"

    if path in _SKIP_EXACT or any(path.startswith(p) for p in _SKIP_PREFIXES):
        return False, ""

    if method == "OPTIONS":
        return False, ""

    now = time.monotonic()

    # Mutations always count (light debounce collapses middleware + explicit doubles).
    if method in ("POST", "PUT", "PATCH", "DELETE"):
        key = f"mut:{ip}:{method}:{path}"
        last = _debounce.get(key, 0.0)
        if now - last < 2:
            return False, ""
        _debounce[key] = now
        if path.startswith("/auth/"):
            return True, "auth"
        return True, "action"

    if method != "GET" and method != "HEAD":
        # WebSocket upgrade is GET; other verbs rare — record.
        return True, "api"

    # Presence heartbeat for poll endpoints.
    if path in _PRESENCE_PATHS:
        key = f"presence:{ip}"
        last = _debounce.get(key, 0.0)
        if now - last < _PRESENCE_DEBOUNCE_S:
            return False, ""
        _debounce[key] = now
        return True, "presence"

    if path in _ALWAYS_GET or path.startswith("/auth/"):
        key = f"page:{ip}:{path}"
        last = _debounce.get(key, 0.0)
        # Still collapse double page loads within 60s (refresh storms).
        if now - last < 60:
            return False, ""
        _debounce[key] = now
        return True, "page" if path in _ALWAYS_GET else "auth"

    # Other GETs (admin APIs, meta, etc.) — light debounce.
    key = f"get:{ip}:{path}"
    last = _debounce.get(key, 0.0)
    if now - last < _GET_DEBOUNCE_S:
        return False, ""
    _debounce[key] = now
    return True, "api"


def record_hit(
    *,
    path: str,
    method: str = "GET",
    status: int | None = None,
    ip: str = "unknown",
    user_agent: str = "",
    cf_country: str = "",
    cf_ray: str = "",
    username: str = "",
    event: str | None = None,
) -> bool:
    """
    Append one traffic event if it passes filters. Returns True when recorded.
    Safe to call from any thread; geo is filled in the background.
    """
    path = path or "/"
    method = (method or "GET").upper()
    ip = (ip or "unknown").strip()

    ok, kind = _should_record(method, path, ip)
    if not ok:
        return False
    if event:
        kind = event

    timestamp = datetime.now(ET).isoformat(timespec="seconds")
    entry = {
        "timestamp": timestamp,
        "event": kind,
        "method": method,
        "path": path,
        "status": status,
        "ip": ip,
        "username": (username or "").strip().lower() or None,
        "user_agent": user_agent or "",
        "cf_country": (cf_country or "").upper() or None,
        "cf_ray": cf_ray or None,
        "location": _location_seed(cf_country),
    }

    with _lock:
        entries = _load()
        entries.append(entry)
        if len(entries) > MAX_ENTRIES:
            entries = entries[-MAX_ENTRIES:]
        try:
            _save(entries)
        except Exception:
            log.exception("[traffic] failed to save log")
            return False

    def _fill_geo():
        geo = _geo_lookup(ip)
        if not geo:
            return
        # Prefer city/region from ip-api; keep country from CF if ip-api empty.
        with _lock:
            rows = _load()
            for row in reversed(rows):
                if (
                    row.get("timestamp") == timestamp
                    and row.get("ip") == ip
                    and row.get("path") == path
                ):
                    merged = dict(row.get("location") or {})
                    merged.update({k: v for k, v in geo.items() if v})
                    merged["source"] = "ip-api"
                    row["location"] = merged
                    break
            try:
                _save(rows)
            except Exception:
                log.exception("[traffic] failed to save geo update")

    threading.Thread(target=_fill_geo, daemon=True, name="traffic-geo").start()
    return True


def get_log(limit: int = 200) -> list:
    """Newest first."""
    with _lock:
        rows = list(reversed(_load()))
    return rows[: max(1, int(limit))]


def summarize(hours: float = 24.0) -> dict:
    """
    Aggregate unique visitors and locations for the last `hours`.
    Unique key: IP (primary). Also breaks down by country/city and event type.
    """
    cutoff = datetime.now(ET) - timedelta(hours=hours)
    with _lock:
        rows = _load()

    recent = []
    for row in rows:
        ts = row.get("timestamp") or ""
        try:
            # Handle both offset-aware ET isoformat strings
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ET)
            if dt >= cutoff:
                recent.append(row)
        except Exception:
            continue

    by_ip: dict[str, dict] = {}
    for row in recent:
        ip = row.get("ip") or "unknown"
        slot = by_ip.setdefault(ip, {
            "ip": ip,
            "hits": 0,
            "events": Counter(),
            "paths": Counter(),
            "usernames": set(),
            "cf_countries": Counter(),
            "location": None,
            "user_agents": set(),
            "first_seen": row.get("timestamp"),
            "last_seen": row.get("timestamp"),
        })
        slot["hits"] += 1
        slot["events"][row.get("event") or "unknown"] += 1
        slot["paths"][row.get("path") or "/"] += 1
        if row.get("username"):
            slot["usernames"].add(row["username"])
        if row.get("cf_country"):
            slot["cf_countries"][row["cf_country"]] += 1
        if row.get("location"):
            slot["location"] = row["location"]  # prefer latest geo
        if row.get("user_agent"):
            ua = row["user_agent"]
            if len(slot["user_agents"]) < 5:
                slot["user_agents"].add(ua[:160])
        slot["last_seen"] = row.get("timestamp")
        if (row.get("timestamp") or "") < (slot["first_seen"] or ""):
            slot["first_seen"] = row.get("timestamp")

    visitors = []
    country_counter: Counter = Counter()
    city_counter: Counter = Counter()

    for ip, slot in by_ip.items():
        loc = slot["location"] or {}
        cf_cc = slot["cf_countries"].most_common(1)[0][0] if slot["cf_countries"] else ""
        country = loc.get("country") or cf_cc or ""
        country_code = loc.get("country_code") or cf_cc or ""
        city = loc.get("city") or ""
        region = loc.get("region") or ""
        if country:
            country_counter[country] += 1
        place = ", ".join(p for p in (city, region, country) if p)
        if place:
            city_counter[place] += 1

        visitors.append({
            "ip": ip,
            "hits": slot["hits"],
            "events": dict(slot["events"]),
            "top_paths": slot["paths"].most_common(8),
            "usernames": sorted(slot["usernames"]),
            "location": {
                "city": city,
                "region": region,
                "country": country,
                "country_code": country_code,
            },
            "user_agents": sorted(slot["user_agents"]),
            "first_seen": slot["first_seen"],
            "last_seen": slot["last_seen"],
        })

    visitors.sort(key=lambda v: v["last_seen"] or "", reverse=True)

    event_totals: Counter = Counter()
    for row in recent:
        event_totals[row.get("event") or "unknown"] += 1

    return {
        "hours": hours,
        "window_start": cutoff.isoformat(timespec="seconds"),
        "total_events": len(recent),
        "unique_visitors": len(visitors),
        "by_country": country_counter.most_common(),
        "by_city": city_counter.most_common(20),
        "by_event": event_totals.most_common(),
        "visitors": visitors,
    }
