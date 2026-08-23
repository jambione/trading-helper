"""Point-in-time catalyst features for the live desk.

The desk has never known why a name was moving. `tools/catalyst_screen.py`
answers that question *retroactively* — it pulls Alpaca news months later
and asks whether a headline existed. That can measure the field, but it can
never say what the desk knew at the instant it bought, and those are
different questions: one is about the world, the other about us.

This is the live half. A background refresher keeps a disk cache warm for
whatever is on the watchlist; the entry path only ever *reads* that cache,
so no network call sits in a loop that has to decide within two seconds. A
cold or stale cache reports absence, never a guess.

Absence is the trap here, so it is made visible: `cache_age_sec` rides
along with every reading. "No catalyst" and "nobody looked" produce the
same `n_24h = 0`, and without the age column a week of a dead refresher
would read as a week of uneventful names.

Point-in-time discipline is identical to the screen's: a headline informs a
sample only if it was published STRICTLY BEFORE that instant.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(os.path.dirname(os.path.abspath(__file__)))
CACHE_PATH = ROOT / "ai_reports" / "news_cache.json"

# Headline shape, not sentiment. These are the words that reliably mean a
# specific corporate action on this kind of name; anything cleverer would be
# a text model whose failures nobody could audit after a losing week.
BEARISH = re.compile(
    r"\b(offering|dilut\w*|pricing of|registered direct|shelf|"
    r"S-1|S-3|ATM|warrant|reverse split|going concern|delist\w*|"
    r"downgrade[sd]?|cuts? (?:price )?target|halt\w*)\b", re.I)
BULLISH = re.compile(
    r"\b(FDA|approval|approved|clearance|beats?|tops?|raises? guidance|"
    r"upgrade[sd]?|contract|award\w*|partnership|acquisition|buyout|"
    r"positive (?:results|data)|phase [23])\b", re.I)

_LOCK = threading.Lock()
_CACHE: dict[str, Any] = {"mtime": None, "data": {}}

EMPTY = {
    "has_news_24h": None,
    "n_news_24h": None,
    "mins_since": None,
    "bearish": None,
    "bullish": None,
}


def features_at(news: list[dict], instant: float) -> dict:
    """Catalyst features knowable AT *instant*.

    Strictly ``ts < instant``. A headline published one second later is not
    information the desk had, and letting it in is how a screen invents an
    edge that evaporates live.
    """
    prior = [n for n in news if n.get("ts") is not None and n["ts"] < instant]
    day_start = instant - 24 * 3600
    today = [n for n in prior if n["ts"] >= day_start]
    last = prior[-1] if prior else None
    text = " ".join(str(n.get("headline") or "") for n in today)
    return {
        "has_news_24h": bool(today),
        "n_news_24h": len(today),
        "mins_since": ((instant - last["ts"]) / 60.0) if last else None,
        "bearish": bool(BEARISH.search(text)),
        "bullish": bool(BULLISH.search(text)),
    }


def load_cache() -> dict[str, list[dict]]:
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


def cache_age_sec() -> float | None:
    """Seconds since the cache was last written. None when there is none.

    The difference between "this name has no catalyst" and "the refresher
    has been dead since Tuesday". Both otherwise look like zero headlines.
    """
    try:
        return max(0.0, time.time() - CACHE_PATH.stat().st_mtime)
    except OSError:
        return None


def features_for(symbol: str, instant: float | None = None) -> dict:
    """Point-in-time catalyst features for one symbol. Never raises.

    Returns all-None (not all-False) when the cache holds nothing for this
    name: we did not look, which is not the same as finding nothing.
    """
    try:
        sym = str(symbol or "").upper().strip()
        if not sym:
            return dict(EMPTY)
        news = load_cache().get(sym)
        if not news:
            return dict(EMPTY)
        return features_at(news, float(instant if instant is not None
                                       else time.time()))
    except Exception:  # noqa: BLE001 — a logging path must never break a poll
        return dict(EMPTY)


def refresh(symbols: list[str], lookback_days: int = 3,
            limit: int = 50) -> int:
    """Fetch recent headlines for *symbols* and merge them into the cache.

    Called from the watchdog, never from the entry loop. Returns the number
    of symbols updated; 0 on any failure, because a dead refresher must show
    up as a stale `cache_age_sec` rather than as an exception in the trader.
    """
    syms = sorted({str(s).upper().strip() for s in (symbols or []) if s})
    if not syms:
        return 0
    try:
        from alpaca.data.historical.news import NewsClient
        from alpaca.data.requests import NewsRequest
        from config import load_config
        cfg = load_config()
        client = NewsClient(
            api_key=cfg.get("api_key") or cfg.get("alpaca_api_key"),
            secret_key=cfg.get("secret_key") or cfg.get("alpaca_secret_key"))
    except Exception:  # noqa: BLE001
        return 0
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=lookback_days)
    cache = dict(load_cache())
    updated = 0
    for sym in syms:
        try:
            res = client.get_news(NewsRequest(
                symbols=sym, start=start, end=end, limit=limit))
            items = res.data.get("news", []) if hasattr(res, "data") else []
        except Exception:  # noqa: BLE001
            continue
        rows = sorted(
            ({"ts": n.created_at.timestamp(),
              "headline": str(n.headline or "")} for n in items),
            key=lambda r: r["ts"])
        # Merge rather than replace: the window is 3 days and the cache is
        # the desk's whole history of what it could have known.
        merged = {(r["ts"], r["headline"]): r
                  for r in (cache.get(sym) or []) + rows}
        cache[sym] = sorted(merged.values(), key=lambda r: r["ts"])
        updated += 1
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache), encoding="utf-8")
        os.replace(tmp, CACHE_PATH)
    except OSError:
        return 0
    return updated
