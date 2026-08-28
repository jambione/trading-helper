"""Share count for the desk — the supply side of a squeeze.

Every "strength" proxy this desk has ever logged is a *symptom*: percent
change, RVOL, trending score, extension. All four measured anti-predictive
in 2026-08, and the reason is that they describe the same event on wildly
different instruments. Five times average volume on a 500-million-share
company is liquidity. Five times average volume on a three-million-share
company is a supply constraint, and the price has to move to clear it.

Pooling those two is what made RVOL look useless. Share count is the term
that separates them, and it is the first *cause* in this codebase rather
than another reading of the effect.

**This is shares OUTSTANDING, not free float.** Finnhub's profile2 does not
publish float. Outstanding is always >= float, so a filter of
"outstanding < N" is conservative: it cannot admit a name whose float is
above N, but it will reject some genuinely low-float names whose insiders
hold most of the shares. Under-inclusive, never over-inclusive — which is
the safe direction for a filter that decides what to buy.

Cached hard. Share counts move on offerings and splits, not on ticks, so
the refresh window is days rather than minutes and the entry path only
ever reads the file.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(os.path.dirname(os.path.abspath(__file__)))
CACHE_PATH = ROOT / "ai_reports" / "float_cache.json"

# A share count is a fundamental. Re-reading it every poll would be a
# request per name per two seconds for a number that changes on an 8-K.
TTL_SEC = 7 * 24 * 3600

# Finnhub reports shareOutstanding in millions.
MILLIONS = 1_000_000.0

_LOCK = threading.Lock()
_CACHE: dict[str, Any] = {"mtime": None, "data": {}}


def _api_key() -> str | None:
    """The desk's Finnhub key, by the route config.py already establishes."""
    try:
        from config import load_config
        k = (load_config() or {}).get("finnhub_key")
        return str(k) if k else None
    except Exception:  # noqa: BLE001
        return None


def load_cache() -> dict[str, dict]:
    """The on-disk cache, re-read only when it changes. Never raises."""
    try:
        st = CACHE_PATH.stat()
    except OSError:
        return {}
    with _LOCK:
        if _CACHE["mtime"] == st.st_mtime:
            return _CACHE["data"]
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    with _LOCK:
        _CACHE["mtime"] = st.st_mtime
        _CACHE["data"] = data
    return data


def shares_out(symbol: str) -> float | None:
    """Shares outstanding in MILLIONS, or None when unknown. Never raises.

    None is not zero and must not be filtered as though it were: an unknown
    share count means the name was never looked up, and treating that as
    "small float" would admit exactly the names we know least about.
    """
    try:
        sym = str(symbol or "").upper().strip()
        if not sym:
            return None
        row = load_cache().get(sym)
        if not isinstance(row, dict):
            return None
        v = row.get("shares_out")
        return float(v) if v is not None else None
    except Exception:  # noqa: BLE001
        return None


def float_shares(symbol: str) -> float | None:
    """Shares outstanding in MILLIONS, or None when unknown. Never raises.

    None is not zero and must not be filtered as though it were: an unknown
    share count means the name was never looked up, and treating that as
    "small float" would admit exactly the names we know least about.
    """
    try:
        sym = str(symbol or "").upper().strip()
        if not sym:
            return None
        row = load_cache().get(sym)
        if not isinstance(row, dict):
            return None
        v = row.get("float_m")
        return float(v) if v is not None else None
    except Exception:  # noqa: BLE001
        return None


def is_low_float(symbol: str, max_millions: float = 10.0) -> bool | None:
    """True / False / None-when-unknown. Three states, deliberately.

    A boolean would collapse "we looked and it is big" into "we never
    looked", and the entry gate must be able to tell those apart.
    """
    v = shares_out(symbol)
    if v is None:
        return None
    return v < max_millions


# Finnhub's free tier allows 60 requests/minute. Over it every call 429s,
# and a caller that swallows those quietly reports "fetched 0" for five
# consecutive batches and looks like a cache that is already warm — which
# is exactly what the first backfill did.
RATE_LIMITED = object()


def _fetch_one(sym: str, key: str, timeout: float = 8.0):
    """Profile row, None on a normal failure, RATE_LIMITED on a 429."""
    url = ("https://finnhub.io/api/v1/stock/profile2"
           f"?symbol={sym}&token={key}")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as fh:
            d = json.loads(fh.read())
    except urllib.error.HTTPError as e:
        return RATE_LIMITED if e.code == 429 else None
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None
    if not isinstance(d, dict):
        return None
    so = d.get("shareOutstanding")
    try:
        so = float(so) if so is not None else None
    except (TypeError, ValueError):
        so = None
    # FLOAT, not shares outstanding. This module has been caching
    # shareOutstanding and calling the file float_cache.json, which are
    # different numbers and not by a little: AREN carries 47.6M outstanding
    # against a 13.12M float, KXIN 1.56M against 1.42M. A low-float screen
    # run on outstanding shares is not the screen the operator asked for.
    # Kept alongside rather than replacing it — universe_screen consumes
    # shares_out today and that is a separate, valid number.
    fl = d.get("floatingShare")
    try:
        fl = float(fl) if fl is not None else None
    except (TypeError, ValueError):
        fl = None
    # An empty profile is a real answer for a delisted or unknown ticker.
    # Cache it so we stop asking, but keep shares_out as None rather than 0.
    return {"shares_out": so, "float_m": fl,
            "name": str(d.get("name") or "")[:40],
            "ts": time.time()}


def refresh(symbols: list[str], limit: int = 10,
            ttl_sec: float = TTL_SEC, pace_sec: float = 0.0) -> int:
    """Fetch share counts for names we lack or whose reading has aged out.

    Returns the number fetched; 0 on any failure. Called from the watchdog,
    never the entry path.

    `limit` caps a single pass so one refresh cannot stall the supervisor
    behind sixty HTTP round trips — the default of 10 per pass is well
    inside Finnhub's 60/minute at any sane cadence. `pace_sec` is for
    bulk backfills, which do need to sleep between calls; leaving it at 0
    in the live path keeps the supervisor loop responsive.

    Stops immediately on a 429 rather than burning the rest of the batch
    against a closed window, and whatever was fetched before that is still
    written.
    """
    syms = [str(s).upper().strip() for s in (symbols or []) if s]
    if not syms:
        return 0
    key = _api_key()
    if not key:
        return 0
    cache = dict(load_cache())
    now = time.time()
    stale = []
    for s in dict.fromkeys(syms):          # de-dup, preserve caller's order
        row = cache.get(s)
        if not isinstance(row, dict) or (now - float(row.get("ts") or 0)) > ttl_sec:
            stale.append(s)
    if not stale:
        return 0
    fetched = 0
    for i, sym in enumerate(stale[:limit]):
        if pace_sec and i:
            time.sleep(pace_sec)
        row = _fetch_one(sym, key)
        if row is RATE_LIMITED:
            break
        if row is None:
            continue
        cache[sym] = row
        fetched += 1
    if not fetched:
        return 0
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache), encoding="utf-8")
        os.replace(tmp, CACHE_PATH)
    except OSError:
        return 0
    return fetched
