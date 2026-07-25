#!/usr/bin/env python3
"""
dashboard.py — Signal Scanner
Ties together the Discord OCR alert source, real-time prices, and signals.
  http://localhost:8888
"""

import asyncio
import io
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
import zipfile
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo


def _free_port(port: int):
    """Kill any process bound to the given port before we start."""
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True, text=True,
        )
        pids = [p.strip() for p in result.stdout.splitlines() if p.strip()]
        for pid in pids:
            try:
                os.kill(int(pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
        if pids:
            time.sleep(0.5)
    except Exception as e:
        print(f"[STARTUP] Warning: could not free port 8888: {e}", file=sys.stderr)


_free_port(8888)

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse as _SJSONResponse

from auth import (check_credentials, create_token, verify_token, is_auth_required,
                   create_user, user_exists, get_token_username, is_admin_user)
from login_log import record_login, get_log as get_login_log

from config import load_config, save_config, SAFE_CONFIG_KEYS
from session_clock import session_window, next_shot
import engine_env
import version

from email_service import send_suggestion_email, send_login_email
import alpaca_api as _api

sys.path.insert(0, str(Path(__file__).parent / "transcription"))
from workflows import workflow_add_wb, workflow_add_brave_tv, workflow_add_wb_and_tv, workflow_create_tv_alert
from finnhub_stream import (
    FINNHUB_STATE,
    start_finnhub_stream,
    request_subscribe as _fh_subscribe,
    fetch_realtime_quote as _fh_rest_quote,
)

ET                 = ZoneInfo("America/New_York")
PORT               = 8888
TICKER_LOG         = Path("transcription/wb_watchlist.json")
NEWS_FILE          = Path("news.json")
SWING_FILE         = Path("swing_candidates.json")
SUGGESTIONS_FILE   = Path("suggestions.json")
TICKER_FEED_FILE   = Path("static/ticker_feed.json")
PUSH_SUBS_FILE     = Path("config/push_subscriptions.json")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


# ── Shared state ──────────────────────────────────────────────────────────────

class _State:
    def __init__(self):
        self.lock             = threading.Lock()
        self.cfg              = load_config()
        self.data_client      = None
        self.tickers: dict    = {}   # ticker → {price, price_ts, day_open, pct_change}
        # Discord OCR source feed — the dashboard's only ticker producer. ingest()
        # appends captured alerts here and stamps discord_last_ts so the UI can
        # render a live feed + a 'source alive' status (in-memory only).
        self.discord_alerts: deque = deque(maxlen=_MAX_DISCORD_ALERTS)
        self.discord_last_ts: float = 0.0
        # TradingView webhook feed — a second independent signal source. Each
        # inbound webhook fires as a burst and is tracked separately so the UI
        # can show both sources and their agreement.
        self.tv_alerts: deque = deque(maxlen=_MAX_DISCORD_ALERTS)
        self.tv_last_ts: float = 0.0
        # Mention tracking — resets on server restart (fresh each trading day)
        self.mention_ts:    dict = {}  # ticker → [float, ...]  recent timestamps
        self.mention_daily: dict = {}  # ticker → int  daily total count
        # Find It First provenance — ticker → unix ts of the last "Find It First"
        # scanner card. Drives the momentum monitor's FIRST badge; expires per
        # _FIND_IT_FIRST_TTL and clears on the daily mention reset.
        self.find_it_first_ts: dict = {}
        from datetime import date as _date
        self.mention_reset_date: str  = str(_date.today())
        self.mention_market_opened: bool = False
        # Web Push — subscriptions loaded from disk at startup, notified set resets daily
        self.push_subscriptions: list = []
        self.push_notified: set       = set()
        # Sentiment — rolling direction signal from the Discord OCR source.
        # sentiment_events: ticker (or None for market) → [event dict, ...]
        self.sentiment_events: dict = {}
        self.sentiment_feed:   deque = deque(maxlen=_MAX_DISCORD_ALERTS)
        # Scanner Price Spike cards — dedicated feed for the UI strip + JSONL archive.
        self.price_spikes: deque = deque(maxlen=_MAX_PRICE_SPIKES)
        self.price_spike_base_ts: dict = {}  # base de-dupe key → first-seen unix time
        # Morning funnel — background rank of tradeable candidates for the single
        # monitor slot. Written by _funnel_loop() on a slow cadence (network calls),
        # read by _snapshot() at no cost so the 4Hz snapshot path never blocks.
        self.funnel_rows: list  = []   # ranked row dicts from morning_funnel.scan_once
        self.funnel_ts:   float = 0.0  # unix time of the last completed scan

# Discord OCR feed sizing + liveness window. The source pings every poll
# (~2.5s); if we haven't heard from it within this window it's considered down.
_MAX_DISCORD_ALERTS = 60
_MAX_PRICE_SPIKES   = 40
_SPIKE_TTL_SEC = 3 * 60   # price spikes live 3 min, then drop from UI + de-dupe
_DISCORD_STALE_SEC  = 15.0
# Sentiment events older than this drop out of the recency-weighted mean.
_SENTIMENT_WINDOW_SEC = 30 * 60
# How long a ticker keeps its "Find It First" provenance badge after the card.
_FIND_IT_FIRST_TTL = 30 * 60

STATE = _State()


# ── News ─────────────────────────────────────────────────────────────────────

_news_cache: dict = {"mtime": -1.0, "items": []}

def load_news() -> list:
    """Read news.json, cache by mtime. Returns list of news item dicts."""
    try:
        if not NEWS_FILE.exists():
            return []
        mtime = NEWS_FILE.stat().st_mtime
        if mtime == _news_cache["mtime"]:
            return _news_cache["items"]
        import json as _json
        items = _json.loads(NEWS_FILE.read_text(encoding="utf-8"))
        if not isinstance(items, list):
            items = []
        _news_cache.update(mtime=mtime, items=items)
        return items
    except Exception as e:
        log.warning(f"[NEWS] Failed to load news.json: {e}")
        return []

def save_news(items: list) -> None:
    """Write items list to news.json and invalidate the cache."""
    _atomic_write_json(NEWS_FILE, items)
    _news_cache["mtime"] = -1.0


# ── Swing candidates ─────────────────────────────────────────────────────────
# Written by swing_screener.py (FMP screen + our-own-signals confirmation).
# The dashboard reads them and, at serve time, overlays LIVE Discord confluence.

_swing_cache: dict = {"mtime": -1.0, "candidates": []}

def load_swing() -> list:
    """Read swing_candidates.json, cache by mtime. Returns the candidate list."""
    try:
        if not SWING_FILE.exists():
            return []
        mtime = SWING_FILE.stat().st_mtime
        if mtime == _swing_cache["mtime"]:
            return _swing_cache["candidates"]
        import json as _json
        data = _json.loads(SWING_FILE.read_text(encoding="utf-8"))
        candidates = data.get("candidates", []) if isinstance(data, dict) else []
        if not isinstance(candidates, list):
            candidates = []
        _swing_cache.update(mtime=mtime, candidates=candidates)
        return candidates
    except Exception as e:
        log.warning(f"[SWING] Failed to load swing_candidates.json: {e}")
        return []


# ── Suggestions ──────────────────────────────────────────────────────────────

def load_suggestions() -> list:
    try:
        if not SUGGESTIONS_FILE.exists():
            return []
        import json as _json
        data = _json.loads(SUGGESTIONS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as e:
        log.warning(f"[SUGGESTIONS] load failed: {e}")
        return []


def save_suggestion(message: str, ip: str, ua: str):
    import json as _json
    from datetime import datetime, timezone
    items = load_suggestions()
    items.append({
        "timestamp":  datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "message":    message,
        "ip":         ip,
        "user_agent": ua,
    })
    SUGGESTIONS_FILE.write_text(_json.dumps(items, indent=2), encoding="utf-8")


def delete_suggestion(timestamp: str) -> bool:
    """Remove the suggestion matching the given timestamp. Returns True if found."""
    import json as _json
    items = load_suggestions()
    before = len(items)
    items = [s for s in items if s.get("timestamp") != timestamp]
    if len(items) == before:
        return False  # nothing removed
    SUGGESTIONS_FILE.write_text(_json.dumps(items, indent=2), encoding="utf-8")
    return True


# ── Mention tracking ──────────────────────────────────────────────────────────

def _track_mention(ticker: str):
    """Record a mention for ticker. Must be called while holding STATE.lock."""
    now       = time.time()
    window    = float(STATE.cfg.get("mention_alert_window", 10))
    ts        = STATE.mention_ts.setdefault(ticker, [])
    ts.append(now)
    # Prune timestamps outside the rolling window
    STATE.mention_ts[ticker] = [t for t in ts if now - t <= window]
    STATE.mention_daily[ticker] = STATE.mention_daily.get(ticker, 0) + 1

    # Dispatch Web Push on burst threshold (rising edge only)
    threshold = int(STATE.cfg.get("mention_alert_threshold", 5))
    if len(STATE.mention_ts[ticker]) >= threshold and ticker not in STATE.push_notified:
        STATE.push_notified.add(ticker)
        price = STATE.tickers.get(ticker, {}).get("price")
        _archive_burst(ticker, price, len(STATE.mention_ts[ticker]))
        threading.Thread(
            target=_send_push_notifications,
            args=(ticker, price),
            daemon=True,
        ).start()


_BURST_LOG      = Path(__file__).parent / "benchmarks" / "mention_bursts.jsonl"
_PRICE_SPIKE_LOG = Path(__file__).parent / "benchmarks" / "price_spikes.jsonl"


def _is_price_spike_alert(a: dict) -> bool:
    """True for Discord OCR price-spike alerts (scanner cards or classic spike lines)."""
    if a.get("burst"):
        return False
    if a.get("price_spike"):
        return True
    alert_type = str(a.get("alert_type") or "").lower()
    return "spike" in alert_type


def _price_spike_base_key(ticker: str, a: dict) -> str:
    """Scanner-card identity — ticker + alert type + tier (OCR-jitter proof)."""
    alert_type = re.sub(
        r"[^A-Za-z0-9]", "", str(a.get("alert_type") or ""),
    ).lower()
    tier = str(a.get("scanner_tier") or "").upper()
    return f"sc|{ticker}|{alert_type}|{tier}".lower()


def _price_spike_line_key(ticker: str, a: dict) -> str:
    """Classic >>>>> spike lines — include normalised text so successive spikes differ."""
    line = re.sub(r"[^A-Za-z0-9]", "", str(a.get("line") or "")).lower()
    return f"ln|{ticker}|{line[:100]}"


def _is_scanner_price_spike(a: dict) -> bool:
    return bool(a.get("price_spike") or a.get("scanner_tier") or a.get("float_size") is not None)


def _prune_expired_price_spikes(now: float | None = None) -> None:
    """Remove price spikes and de-dupe keys older than _SPIKE_TTL_SEC. Hold STATE.lock."""
    now = now or time.time()
    cutoff = now - _SPIKE_TTL_SEC
    keep = [r for r in STATE.price_spikes if float(r.get("unix", 0)) > cutoff]
    STATE.price_spikes.clear()
    for rec in keep:
        STATE.price_spikes.append(rec)
    for key, ts in list(STATE.price_spike_base_ts.items()):
        if ts <= cutoff:
            del STATE.price_spike_base_ts[key]


def _price_spike_is_duplicate(ticker: str, a: dict) -> bool:
    """Return True when this spike was already ingested recently."""
    now = time.time()
    with STATE.lock:
        _prune_expired_price_spikes(now)
        if _is_scanner_price_spike(a):
            base = _price_spike_base_key(ticker, a)
            last = STATE.price_spike_base_ts.get(base, 0)
        else:
            last = STATE.price_spike_base_ts.get(_price_spike_line_key(ticker, a), 0)
    return bool(last and now - last < _SPIKE_TTL_SEC)


def _mark_price_spike_seen(ticker: str, a: dict) -> None:
    """Record that this spike was ingested. Must hold STATE.lock."""
    if _is_scanner_price_spike(a):
        STATE.price_spike_base_ts[_price_spike_base_key(ticker, a)] = time.time()
    else:
        STATE.price_spike_base_ts[_price_spike_line_key(ticker, a)] = time.time()


def _archive_price_spike(rec: dict) -> None:
    """Append every price-spike alert to an append-only JSONL archive."""
    try:
        _PRICE_SPIKE_LOG.parent.mkdir(exist_ok=True)
        with open(_PRICE_SPIKE_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception as e:
        log.warning("[SPIKE] archive failed: %s", e)


def _send_price_spike_push(ticker: str, price, float_size, tier: str) -> None:
    """Send Web Push for a scanner price-spike alert."""
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        return

    private_pem = STATE.cfg.get("push_vapid_private_key", "")
    contact     = STATE.cfg.get("push_contact_email") or "admin@localhost"
    if not private_pem:
        return

    parts = []
    if price is not None:
        parts.append(f"${float(price):.2f}")
    if float_size is not None:
        if float_size >= 1e6:
            parts.append(f"Float {float_size / 1e6:.2f}M")
        elif float_size >= 1e3:
            parts.append(f"Float {float_size / 1e3:.1f}K")
    if tier:
        parts.append(tier)
    body = " · ".join(parts) if parts else "Scanner price spike"

    payload = json.dumps({
        "title": f"⚡ {ticker} Price Spike",
        "body":  body,
        "tag":   f"spike-{ticker}-{int(time.time())}",
        "url":   "/",
    })

    with STATE.lock:
        subs = list(STATE.push_subscriptions)

    stale = []
    for sub in subs:
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=private_pem,
                vapid_claims={"sub": f"mailto:{contact}"},
            )
        except WebPushException as e:
            if e.response is not None and e.response.status_code in (404, 410):
                stale.append(sub)
        except Exception:
            pass

    if stale:
        with STATE.lock:
            STATE.push_subscriptions = [
                s for s in STATE.push_subscriptions if s not in stale
            ]


def _archive_burst(ticker: str, price, window_count: int):
    """
    Append every mention burst to an append-only JSONL archive.

    This is the missing dataset for the excellence loop: backtests showed the
    3-indicator entry has no standalone edge on alert-pool microcaps — the
    catalyst (the burst) is the candidate edge. Replaying catalyst-gated
    entries needs burst timestamps, which the in-memory state doesn't keep.
    """
    try:
        _BURST_LOG.parent.mkdir(exist_ok=True)
        with open(_BURST_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ticker": ticker,
                "time":   datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "unix":   round(time.time(), 1),
                "price":  price,
                "window_count": window_count,
            }) + "\n")
    except Exception as e:
        log.warning(f"[BURST] archive failed: {e}")


def _mention_window_count(ticker: str) -> int:
    """Current mention count within the rolling window. Thread-safe read."""
    now    = time.time()
    window = float(STATE.cfg.get("mention_alert_window", 10))
    ts     = STATE.mention_ts.get(ticker, [])
    return sum(1 for t in ts if now - t <= window)


# ── Sentiment ─────────────────────────────────────────────────────────────────

def _prune_sentiment(key):
    """Drop events older than _SENTIMENT_WINDOW_SEC. Must hold STATE.lock."""
    evs = STATE.sentiment_events.get(key)
    if not evs:
        return
    now = time.time()
    STATE.sentiment_events[key] = [e for e in evs if now - e["ts"] <= _SENTIMENT_WINDOW_SEC]


def _ticker_sentiment(key) -> dict:
    """Recency-weighted mean sentiment for a ticker (or None = market).
    Must hold STATE.lock."""
    _prune_sentiment(key)
    evs = STATE.sentiment_events.get(key) or []
    if not evs:
        return {"score": 0.0, "count": 0, "last_ts": None, "source": ""}
    now = time.time()
    num = den = 0.0
    for e in evs:
        age_min = (now - e["ts"]) / 60.0
        w       = max(0.3, 1.0 - age_min / 30.0 * 0.7)
        num    += e["score"] * w
        den    += w
    last = max(evs, key=lambda e: e["ts"])
    return {
        "score":   round(num / den, 4) if den else 0.0,
        "count":   len(evs),
        "last_ts": last["ts"],
        "source":  last.get("source", ""),
    }


# ── Confluence ────────────────────────────────────────────────────────────────
# A ticker corroborated by several INDEPENDENT producers — a Market Update
# scanner row, human chat, a bot alert, a squeeze — is a far stronger setup than
# any single source. We surface (and boost) that overlap. Mentions are NOT a
# source here: they're downstream of the others, so counting them would be
# circular.

_CONFLUENCE_SENT_SOURCES = ("chat", "scanner")


def _confluence_sources(ticker) -> list[str]:
    """Distinct independent signal sources currently active for `ticker`
    (subset of {"scanner","chat","alert","squeeze"}). Must hold STATE.lock."""
    if not ticker:
        return []
    srcs = set()
    _prune_sentiment(ticker)
    for e in STATE.sentiment_events.get(ticker) or []:
        s = e.get("source")
        if s in _CONFLUENCE_SENT_SOURCES:
            srcs.add(s)
    for a in STATE.discord_alerts:
        if a.get("ticker") == ticker:
            srcs.add("squeeze" if a.get("burst") else "alert")
    return sorted(srcs)


# ── Web Push ──────────────────────────────────────────────────────────────────

def _load_push_subscriptions() -> list:
    try:
        if not PUSH_SUBS_FILE.exists():
            return []
        return json.loads(PUSH_SUBS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning(f"[PUSH] Failed to load subscriptions: {e}")
        return []


def _save_push_subscriptions(subs: list) -> None:
    _atomic_write_json(PUSH_SUBS_FILE, subs)


def _generate_or_load_vapid_keys():
    """Auto-generate VAPID keys on first run and save to secrets.json."""
    if STATE.cfg.get("push_vapid_public_key") and STATE.cfg.get("push_vapid_private_key"):
        return
    try:
        from py_vapid import Vapid
        from cryptography.hazmat.primitives import serialization
        import base64

        v = Vapid()
        v.generate_keys()
        private_pem = v.private_pem().decode("utf-8")
        pub_bytes   = v.public_key.public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,  # 65-byte uncompressed point required by browsers
        )
        public_b64 = base64.urlsafe_b64encode(pub_bytes).rstrip(b"=").decode()

        STATE.cfg["push_vapid_public_key"]  = public_b64
        STATE.cfg["push_vapid_private_key"] = private_pem
        save_config(STATE.cfg)
        log.info("[PUSH] Generated new VAPID keys")
    except Exception as e:
        log.warning(f"[PUSH] Failed to generate VAPID keys: {e}")


def _send_push_notifications(ticker: str, price) -> None:
    """Send Web Push burst alert to all subscribers; prune stale endpoints."""
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        return

    private_pem = STATE.cfg.get("push_vapid_private_key", "")
    contact     = STATE.cfg.get("push_contact_email") or "admin@localhost"
    if not private_pem:
        return

    price_str = f"${price:.2f}  · " if price else ""
    payload = json.dumps({
        "title": f"🔥 {ticker} Burst",
        "body":  f"{price_str}Rapid mentions detected",
        "tag":   f"burst-{ticker}",
        "url":   "/",
    })

    # Snapshot under lock then release — HTTP calls must not hold STATE.lock
    with STATE.lock:
        subs = list(STATE.push_subscriptions)

    stale = []
    for sub in subs:
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=private_pem,
                vapid_claims={"sub": f"mailto:{contact}"},
            )
        except WebPushException as e:
            if e.response is not None and e.response.status_code in (404, 410):
                stale.append(sub)  # 404/410 = browser revoked or expired subscription
            else:
                log.warning(f"[PUSH] WebPushException for {ticker}: {e}")
        except Exception as e:
            log.warning(f"[PUSH] push failed for {ticker}: {e}")

    if stale:
        with STATE.lock:
            STATE.push_subscriptions = [s for s in STATE.push_subscriptions if s not in stale]
            pruned = list(STATE.push_subscriptions)  # capture inside lock before releasing
        _save_push_subscriptions(pruned)


# ── Ticker log ────────────────────────────────────────────────────────────────
# File format: [{"ticker": "AAPL", "added": "2026-05-07T09:30:00-04:00"}, ...]
# Backward-compat: plain strings are migrated to objects on first read.

def _atomic_write_json(path: Path, data) -> None:
    """Write JSON atomically via tmpfile + rename so a crash mid-write never corrupts the file."""
    import json as _json, tempfile as _tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = None
    try:
        fd, tmp_path = _tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                _json.dump(data, f, indent=2)
        except Exception:
            os.close(fd)
            raise
        Path(tmp_path).replace(path)
        tmp_path = None
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

TICKER_MAX_AGE = 15 * 60  # seconds — entries older than this are auto-purged

_ticker_cache: dict = {"mtime": -1.0, "tickers": [], "entries": []}
_ticker_lock  = threading.RLock()   # guards _ticker_cache + TICKER_LOG reads/writes


def load_tickers() -> list:
    """Read watchlist, purge entries ≥ 15 min old, return list of ticker strings.

    Writes the file back whenever entries are purged or the format is migrated,
    so the on-disk file is always up-to-date. Caches by mtime to keep I/O cheap
    at the 10 Hz poll rate used by the price loop.
    """
    with _ticker_lock:
        if not TICKER_LOG.exists():
            _ticker_cache.update(mtime=-1.0, tickers=[], entries=[])
            return []
        try:
            import json as _json
            mtime = TICKER_LOG.stat().st_mtime
            if mtime == _ticker_cache["mtime"]:
                return list(_ticker_cache["tickers"])

            raw = _json.loads(TICKER_LOG.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                return []

            now_ts  = time.time()
            now_iso = datetime.now(ET).isoformat(timespec="seconds")
            cutoff  = now_ts - TICKER_MAX_AGE

            kept    = []
            changed = False   # True if any entry was purged or migrated from old format

            for item in raw:
                if isinstance(item, str):
                    t, added = item.strip().upper(), now_iso
                    changed = True          # migrate plain string → object
                elif isinstance(item, dict):
                    t     = str(item.get("ticker", "")).strip().upper()
                    added = item.get("added", now_iso)
                else:
                    changed = True
                    continue

                if not (2 <= len(t) <= 5 and t.isalpha()):
                    changed = True
                    continue

                try:
                    added_ts = datetime.fromisoformat(added).timestamp()
                except Exception:
                    added_ts = now_ts       # unparseable → treat as fresh

                if added_ts >= cutoff:
                    kept.append({"ticker": t, "added": added})
                else:
                    changed = True
                    log.debug(f"[TICKER] Purged stale {t}")

            if changed:
                _atomic_write_json(TICKER_LOG, kept)
                mtime = TICKER_LOG.stat().st_mtime

            tickers = [e["ticker"] for e in kept]
            _ticker_cache.update(mtime=mtime, tickers=tickers, entries=kept)
            return tickers
        except Exception:
            return []


def clear_ticker_log():
    with _ticker_lock:
        try:
            _atomic_write_json(TICKER_LOG, [])
            _ticker_cache.update(mtime=-1.0, tickers=[], entries=[])
        except Exception:
            pass
    with STATE.lock:
        STATE.tickers.clear()
        STATE.mention_ts.clear()
        STATE.mention_daily.clear()
        STATE.find_it_first_ts.clear()


def add_ticker_to_log(ticker: str) -> tuple[bool, bool]:
    """Add a single ticker. Returns (ok, is_new) — is_new is False when already present."""
    ticker = ticker.upper()
    with _ticker_lock:
        try:
            load_tickers()   # refresh cache + purge stale
            if ticker in _ticker_cache["tickers"]:
                return True, False
            now_iso = datetime.now(ET).isoformat(timespec="seconds")
            entries = list(_ticker_cache["entries"]) + [{"ticker": ticker, "added": now_iso}]
            _atomic_write_json(TICKER_LOG, entries)
            _ticker_cache["mtime"] = -1.0   # force re-read on next load
            return True, True
        except Exception as e:
            log.error(f"[TICKER] Add {ticker} failed: {e}")
            return False, False


def remove_ticker_from_log(ticker: str) -> bool:
    """Remove a single ticker from the watchlist and internal state."""
    ticker = ticker.upper()
    with _ticker_lock:
        try:
            load_tickers()   # refresh cache + purge stale
            if ticker not in _ticker_cache["tickers"]:
                return True
            entries = [e for e in _ticker_cache["entries"] if e["ticker"] != ticker]
            _atomic_write_json(TICKER_LOG, entries)
            _ticker_cache["mtime"] = -1.0
            with STATE.lock:
                STATE.tickers.pop(ticker, None)
                STATE.mention_ts.pop(ticker, None)
                STATE.mention_daily.pop(ticker, None)
                STATE.find_it_first_ts.pop(ticker, None)
            return True
        except Exception as e:
            log.error(f"[TICKER] Remove {ticker} failed: {e}")
            return False


def refresh_ticker_timestamps(tickers: list[str]):
    """Reset 'added' to now for highlighted tickers, restarting their 15-min expiry clock.

    Only writes the file when at least one entry is > 30 s stale, so a ticker
    that stays highlighted for its full 30-second mention window causes at most
    one file write rather than one per 10 Hz tick.
    """
    if not tickers:
        return
    with _ticker_lock:
        load_tickers()   # ensure cache is fresh
        entries  = list(_ticker_cache["entries"])
        now_ts   = time.time()
        now_iso  = datetime.now(ET).isoformat(timespec="seconds")
        refresh  = set(tickers)
        changed  = False
        for e in entries:
            if e["ticker"] not in refresh:
                continue
            try:
                age = now_ts - datetime.fromisoformat(e["added"]).timestamp()
            except Exception:
                age = 9999
            if age > 30:
                e["added"] = now_iso
                changed = True
        if changed:
            _atomic_write_json(TICKER_LOG, entries)
            _ticker_cache.update(mtime=-1.0, entries=entries,
                                 tickers=[e["ticker"] for e in entries])


# ── Discord OCR source ingest ───────────────────────────────────────────────
# The Discord OCR producer (discord_source.py) POSTs here every poll: any newly
# captured alerts plus a heartbeat. Each alert drives the mention system exactly
# like the old transcriber did (watchlist add + _track_mention → burst), and is
# recorded in a rolling feed the dashboard renders.

def ingest_discord_alerts(alerts: list[dict], sentiment=None) -> int:
    """Record captured Discord alerts: add each ticker to the watchlist, count a
    mention (for burst detection), and append to the live feed. Returns the
    number of alerts accepted. Always stamps discord_last_ts (so an empty list is
    a valid heartbeat). Each alert: {"ticker": str, "line": str, "burst": bool}.

    sentiment is a list of SentimentEvent dicts (ticker/score/source/raw/ts);
    near-zero scores are dropped, the rest stored per-ticker for the rolling mean.

    A "burst" alert (e.g. a 'Squeeze Potential Alert') injects a full burst's
    worth of mentions at once (mention_alert_threshold) so the existing burst
    toast fires immediately — i.e. it simulates a mention spike."""
    sentiment = sentiment or []
    threshold = int(STATE.cfg.get("mention_alert_threshold", 5))
    accepted = 0
    for a in alerts:
        ticker = str(a.get("ticker", "")).strip().upper()
        line   = str(a.get("line", "")).strip()
        if not ticker or not ticker.isalpha() or len(ticker) > 5:
            continue
        is_spike = _is_price_spike_alert(a)
        if is_spike and _price_spike_is_duplicate(ticker, a):
            continue
        card_brand = str(a.get("card_brand") or "")
        add_ticker_to_log(ticker)
        hits = threshold if a.get("burst") else 1
        spike_rec = None
        with STATE.lock:
            if is_spike:
                _mark_price_spike_seen(ticker, a)
            if card_brand == "find_it_first":
                STATE.find_it_first_ts[ticker] = time.time()
            for _ in range(hits):
                _track_mention(ticker)
            alert_rec = {
                "ts":           datetime.now(ET).strftime("%H:%M:%S"),
                "ticker":       ticker,
                "line":         line[:160],
                "burst":        bool(a.get("burst")),
                "price_spike":  is_spike,
                "alert_type":   str(a.get("alert_type") or ""),
                "price":        a.get("price"),
                "volume":       a.get("volume"),
                "float_size":   a.get("float_size"),
                "scanner_tier": str(a.get("scanner_tier") or ""),
                "card_brand":   card_brand,
                "levels":       a.get("levels") or [],
            }
            STATE.discord_alerts.append(alert_rec)
            if is_spike:
                spike_price = a.get("price")
                if spike_price is None:
                    spike_price = STATE.tickers.get(ticker, {}).get("price")
                spike_rec = {
                    "ts":           alert_rec["ts"],
                    "unix":         round(time.time(), 1),
                    "ticker":       ticker,
                    "alert_type":   alert_rec["alert_type"],
                    "price":        spike_price,
                    "float_size":   a.get("float_size"),
                    "scanner_tier": alert_rec["scanner_tier"],
                    "line":         line[:160],
                }
                STATE.price_spikes.append(spike_rec)
        if spike_rec:
            _archive_price_spike(spike_rec)
            threading.Thread(
                target=_send_price_spike_push,
                args=(
                    ticker,
                    spike_rec.get("price"),
                    spike_rec.get("float_size"),
                    spike_rec.get("scanner_tier") or "",
                ),
                daemon=True,
            ).start()
        accepted += 1

    with STATE.lock:
        for ev in sentiment:
            try:
                score = float(ev.get("score", 0.0))
            except (TypeError, ValueError):
                continue
            if abs(score) < 0.05:
                continue
            tkr = ev.get("ticker")
            key = tkr.upper() if isinstance(tkr, str) and tkr else None
            rec = {
                "ticker": key,
                "score":  score,
                "source": str(ev.get("source") or ""),
                "raw":    str(ev.get("raw") or "")[:160],
                "ts":     float(ev.get("ts") or time.time()),
            }
            STATE.sentiment_events.setdefault(key, []).append(rec)
            STATE.sentiment_feed.append({
                "ts":     datetime.now(ET).strftime("%H:%M:%S"),
                "ticker": key,
                "score":  round(score, 3),
                "source": rec["source"],
                "raw":    rec["raw"][:80],
            })
            # Boost mentions (drives burst detection) when EITHER a single source
            # is high-confidence (|score| ≥ 0.7) OR the ticker is corroborated by
            # 2+ independent sources (e.g. a Market Update scanner row AND chat) —
            # confluence is a strong setup even when no single score clears 0.7.
            # Skip hv_alert (SPY direction fires every minute and would flood SPY).
            if key and rec["source"] != "hv_alert":
                strong    = abs(score) >= 0.7
                confluent = len(_confluence_sources(key)) >= 2
                if strong or confluent:
                    add_ticker_to_log(key)
                    _track_mention(key)

    with STATE.lock:
        STATE.discord_last_ts = time.time()
    return accepted


def discord_status() -> dict:
    """Snapshot of the OCR source for the UI: alive flag + recent alert feed."""
    with STATE.lock:
        last  = STATE.discord_last_ts
        feed  = list(STATE.discord_alerts)
    return {
        "running":   bool(last) and (time.time() - last) <= _DISCORD_STALE_SEC,
        "last_ts":   last,
        "alerts":    feed,
    }


# ── TradingView webhook ingest ────────────────────────────────────────────────
# TradingView POSTs here when a Pine Script alertcondition fires.
# Expected payload: {"ticker": "AAPL", "close": 150.23, "interval": "1"}
# TradingView prepends the exchange to the ticker (e.g. "NASDAQ:AAPL") —
# we strip it before validation.

def ingest_tv_alert(ticker: str, close: float, interval: str) -> bool:
    """Record one TradingView squeeze alert. Always fires as a full burst
    (the TV Pine Script only alerts when the squeeze has already confirmed).
    Returns True if the alert was accepted."""
    ticker = ticker.strip().upper()
    # Strip exchange prefix ("NASDAQ:AAPL" → "AAPL")
    if ":" in ticker:
        ticker = ticker.split(":")[-1]
    if not ticker or not ticker.isalpha() or len(ticker) > 5:
        return False
    threshold = int(STATE.cfg.get("mention_alert_threshold", 5))
    label = f"${close:.2f}" if close else ""
    line  = f"TV squeeze {label} ({interval}m)".strip()
    add_ticker_to_log(ticker)
    with STATE.lock:
        for _ in range(threshold):
            _track_mention(ticker)
        STATE.tv_alerts.append({
            "ts":     datetime.now(ET).strftime("%H:%M:%S"),
            "ticker": ticker,
            "close":  close,
            "line":   line,
        })
        STATE.tv_last_ts = time.time()
    return True


def tv_status() -> dict:
    """Snapshot of the TradingView webhook source for the UI."""
    with STATE.lock:
        last = STATE.tv_last_ts
        feed = list(STATE.tv_alerts)
    return {"last_ts": last, "alerts": feed}


# ── Price polling ─────────────────────────────────────────────────────────────

# Alpaca fallback runs in its own thread so it never blocks the price loop.
_alpaca_price_cache: dict = {}          # last results from Alpaca fallback
_alpaca_cache_lock = threading.Lock()
_alpaca_fallback_running = False


def _alpaca_fallback_worker(client, tickers: list, cfg: dict):
    """Fetch Alpaca latest-trade prices in a background thread and cache the result."""
    global _alpaca_fallback_running
    try:
        prices = _api.get_latest_trade_prices(client, tickers, cfg)
        with _alpaca_cache_lock:
            _alpaca_price_cache.update(prices)
    finally:
        _alpaca_fallback_running = False


# Finnhub REST quote poll — fills prices during extended hours when WebSocket is idle.
# Runs every 30s; only updates tickers not already covered by a live WebSocket price.
_FINNHUB_REST_INTERVAL = 30   # seconds
_finnhub_rest_running  = False


def _finnhub_rest_poll_worker(api_key: str, tickers: list):
    """Fetch Finnhub /quote for tickers with no live WebSocket price and cache result."""
    global _finnhub_rest_running
    try:
        with FINNHUB_STATE.lock:
            ws_has = {t for t in tickers if FINNHUB_STATE.prices.get(t, {}).get("price")}
        need = [t for t in tickers if t not in ws_has]
        # In pre-market/after-hours, WebSocket is often idle.
        # If tickers have no price, or price is > 60s old, poll them via REST.
        now = time.time()
        with FINNHUB_STATE.lock:
            stale = {t for t in tickers if (d := FINNHUB_STATE.prices.get(t)) and (now - d.get("ts_unix", 0) > 60)}
        
        to_poll = sorted(list(set(need) | stale))[:30] # cap per cycle to respect rate limits
        if to_poll:
            log.info(f"[PRICE] Extended hours REST poll for {len(to_poll)} tickers: {to_poll}")

        fail_streak = 0
        for ticker in to_poll:
            try:
                q = _fh_rest_quote(api_key, ticker)
                if not q.get("ok"):
                    fail_streak += 1
                    if fail_streak >= 3:
                        log.warning("[PRICE] Finnhub REST: %d consecutive failures — aborting cycle", fail_streak)
                        break
                    time.sleep(0.1 * fail_streak)
                    continue
                fail_streak = 0
                price = float(q.get("c", 0))
                if price > 0:
                    FINNHUB_STATE.update_price(ticker, price)
                    day_open = float(q.get("o", 0))
                    if day_open > 0:
                        with STATE.lock:
                            entry = STATE.tickers.setdefault(ticker, {})
                            entry["day_open"] = round(day_open, 4)
                time.sleep(0.1)
            except Exception as e:
                fail_streak += 1
                log.debug("[PRICE] Finnhub REST error for %s: %s", ticker, e)
                if fail_streak >= 3:
                    log.warning("[PRICE] Finnhub REST: %d consecutive errors — aborting cycle", fail_streak)
                    break
    finally:
        _finnhub_rest_running = False


def _price_loop():
    global _alpaca_fallback_running, _finnhub_rest_running
    last_alpaca_poll    = 0
    last_fh_rest_poll   = 0
    _fail_streak        = 0
    _prev_tickers: set  = set(load_tickers())  # seed from file; avoids first-run flood
    while True:
        try:
            tickers = load_tickers()
            current = set(tickers)

            # Subscribe new tickers to Finnhub as they appear; periodic scan picks them up.
            new = current - _prev_tickers
            if new and FINNHUB_STATE.connected:
                _fh_subscribe(list(new))
            _prev_tickers = current

            client = STATE.data_client
            ts     = datetime.now(ET).strftime("%H:%M:%S")
            now    = time.time()

            if tickers and client:
                # Primary: Finnhub real-time stream prices (zero extra HTTP cost)
                finnhub_prices: dict = {}
                if FINNHUB_STATE.connected:
                    with FINNHUB_STATE.lock:
                        for t in tickers:
                            d = FINNHUB_STATE.prices.get(t)
                            if d and d.get("price"):
                                finnhub_prices[t] = float(d["price"])

                # Finnhub REST quote poll — supplements WebSocket during extended hours
                # when no trades have streamed yet. 30s cadence, free-tier safe.
                fh_key = STATE.cfg.get("finnhub_key", "")
                if fh_key and tickers and not _finnhub_rest_running and (now - last_fh_rest_poll > _FINNHUB_REST_INTERVAL):
                    last_fh_rest_poll     = now
                    _finnhub_rest_running = True
                    threading.Thread(
                        target=_finnhub_rest_poll_worker,
                        args=(fh_key, list(tickers)),
                        daemon=True, name="fh-rest-poll",
                    ).start()

                # Fallback: Alpaca REST for tickers not covered by Finnhub.
                # Runs in a background thread — never blocks this loop.
                # Polls every 5s when Finnhub has gaps; every 2s when Finnhub is down.
                alpaca_tickers = [t for t in tickers if t not in finnhub_prices]
                poll_interval  = 2.0 if not FINNHUB_STATE.connected else 5.0
                if alpaca_tickers and not _alpaca_fallback_running and (now - last_alpaca_poll > poll_interval):
                    last_alpaca_poll       = now
                    _alpaca_fallback_running = True
                    threading.Thread(
                        target=_alpaca_fallback_worker,
                        args=(client, alpaca_tickers, STATE.cfg),
                        daemon=True, name="alpaca-fallback",
                    ).start()

                # Merge: Finnhub REST/WebSocket wins over cached Alpaca values
                with _alpaca_cache_lock:
                    cached_alpaca = dict(_alpaca_price_cache)
                with FINNHUB_STATE.lock:
                    fh_all = {t: float(d["price"]) for t, d in FINNHUB_STATE.prices.items() if d.get("price")}
                all_prices = {**cached_alpaca, **fh_all}

                with STATE.lock:
                    for t, p in all_prices.items():
                        if t not in tickers:
                            continue
                        entry = STATE.tickers.setdefault(t, {})
                        entry["price"]    = round(p, 4)
                        entry["price_ts"] = ts

            _fail_streak = 0
        except Exception as e:
            _fail_streak += 1
            if _fail_streak in (1, 5, 25) or _fail_streak % 50 == 0:
                log.warning("[PRICE] loop error (%d consecutive): %s", _fail_streak, e)
            else:
                log.debug("[PRICE] %s", e)
        time.sleep(0.1)  # 10Hz — Finnhub ticks are picked up within 100ms


# ── Day-volume polling ────────────────────────────────────────────────────────

_VOL_AVG_CACHE: dict = {}          # sym -> avg daily volume
_VOL_AVG_DATE: str = ""            # ET date the averages were computed for


def _vol_avg_volumes(mf, client, tickers: list, cfg: dict, today: str) -> dict:
    """Average daily volume per symbol, from completed sessions only.

    Cached for the ET day — this changes once every 24h, so re-fetching 45
    days of daily bars every minute would be pure waste.
    """
    global _VOL_AVG_DATE
    if _VOL_AVG_DATE == today and _VOL_AVG_CACHE:
        missing = [t for t in tickers if t not in _VOL_AVG_CACHE]
        if not missing:
            return _VOL_AVG_CACHE
        tickers = missing
    else:
        _VOL_AVG_CACHE.clear()
        _VOL_AVG_DATE = today

    daily = mf.fetch_daily(client, tickers, cfg)
    # Same knob the funnel uses, so funnel.rvol and row.rvol agree — T2.2
    # prefers the funnel value and falls back to this one.
    avg_days = int(mf.knobs_from_cfg(cfg)["avg_days"])
    for sym, df in daily.items():
        try:
            import pandas as pd
            dates = pd.Series(mf._et_index(df).date, index=df.index)
            completed = df[dates < pd.Timestamp(today).date()]
            if len(completed) >= 5:
                _VOL_AVG_CACHE[sym] = float(
                    completed["volume"].tail(avg_days).mean())
        except Exception:                                  # noqa: BLE001
            continue
    return _VOL_AVG_CACHE


def _vol_loop():
    """Day volume + time-adjusted relative volume for watchlist tickers.

    Source is Alpaca minute bars for today from 04:00 ET with
    extended_hours=True, summed — i.e. true volume-so-far including
    pre-market. This replaced yfinance `fast_info.last_volume`, which is the
    last *daily* bar: before the regular session opens that is yesterday's
    completed total, so both `day_vol` and `rvol` described the wrong day
    during the pre-market tranches. See tools.morning_funnel.rvol_pair.

    Publishes nothing at all for a symbol with no volume today (weekends,
    holidays, halted or untraded names). A blank volume cell is correct;
    a stale one from a previous session is not.

    Runs every 60s in a background thread and writes into STATE.tickers so
    the values flow through to /api/state automatically.
    """
    try:
        import tools.morning_funnel as mf   # lazy: pulls pandas into this thread
    except Exception as e:                                  # noqa: BLE001
        log.warning(f"[VOL] disabled — import failed: {e}")
        return

    while True:
        try:
            tickers = load_tickers()
            client = STATE.data_client
            if not tickers or client is None:
                time.sleep(5 if client is None else 60)
                continue

            cfg = STATE.cfg
            time_adj = bool(cfg.get("rvol_time_adjusted", True))
            now_et = datetime.now(ET)
            today = str(now_et.date())
            mins_open = (now_et.hour * 60 + now_et.minute) - mf.OPEN_MIN

            avg_by_sym = _vol_avg_volumes(mf, client, tickers, cfg, today)
            minutes = mf.fetch_minutes_today(client, tickers, cfg, now_et)

            vol_data: dict = {}
            stale: list = []
            for sym in tickers:
                df = minutes.get(sym)
                if df is None or df.empty:
                    stale.append(sym)
                    continue
                try:
                    vol_so_far = float(df["volume"].sum())
                    rvol, rvol_raw = mf.rvol_pair(
                        vol_so_far, avg_by_sym.get(sym), mins_open, time_adj)
                    if vol_so_far <= 0:
                        stale.append(sym)
                        continue
                    vol_data[sym] = {
                        "day_vol": int(vol_so_far),
                        "rvol": round(rvol, 2) if rvol is not None else None,
                        "rvol_raw": (round(rvol_raw, 2)
                                     if rvol_raw is not None else None),
                    }
                except Exception:                          # noqa: BLE001
                    stale.append(sym)

            with STATE.lock:
                for sym, vd in vol_data.items():
                    STATE.tickers.setdefault(sym, {}).update(vd)
                # Drop values we can no longer stand behind rather than
                # leaving yesterday's numbers on screen.
                for sym in stale:
                    row = STATE.tickers.get(sym)
                    if row:
                        for k in ("day_vol", "rvol", "rvol_raw"):
                            row.pop(k, None)
            log.debug("[VOL] %d/%d tickers  mins_open=%d  adj=%s",
                      len(vol_data), len(tickers), mins_open, time_adj)
        except Exception as e:                             # noqa: BLE001
            log.debug(f"[VOL] {e}")
        time.sleep(60)


# ── Mention detection ─────────────────────────────────────────────────────────

def _build_mention_rank(ticker_set: set, window_s: float = 30.0) -> dict:
    """Return {ticker: rank} for up to 3 watchlist tickers mentioned in the last
    window_s seconds (rank 0 = most recently mentioned). Driven by the mention
    store (STATE.mention_ts) that the Discord OCR source feeds — this is what
    floats a freshly-alerted ticker to the top of the watchlist."""
    now = time.time()
    recent: list[tuple[float, str]] = []   # (most-recent mention ts, ticker)
    with STATE.lock:
        for t, times in STATE.mention_ts.items():
            if t not in ticker_set or not times:
                continue
            last = max(times)
            if now - last <= window_s:
                recent.append((last, t))
    recent.sort(reverse=True)               # most recent first
    return {t: i for i, (_, t) in enumerate(recent[:3])}


# ── State snapshot ────────────────────────────────────────────────────────────

def _round_or_none(v, ndigits=2):
    """Coerce to a rounded float, or None if not numeric (also flattens numpy)."""
    try:
        return round(float(v), ndigits)
    except (TypeError, ValueError):
        return None


def _funnel_snapshot(f_rows: list, f_ts: float, now_ts: float,
                     now_et, cfg: dict) -> dict:
    """Build the funnel banner payload: current session window + the leading
    tradeable pick. `top` is the highest-scoring row with no hard rejects;
    `suggest` gates the one-click 'send to monitors' button on a score floor."""
    label, guide, color = session_window(now_et)
    shot = next_shot(now_et)
    top = next((r for r in f_rows if not r.get("rejects")), None)
    suggest_min = float(cfg.get("funnel_suggest_min", 60))
    refresh     = max(float(cfg.get("funnel_refresh_sec", 150.0)), _FUNNEL_MIN_REFRESH)
    top_out = None
    if top:
        score = round(float(top.get("score") or 0))
        top_out = {
            "sym":     top.get("sym"),
            "score":   score,
            "state":   top.get("state"),
            "chg_pct": _round_or_none(top.get("chg_pct"), 1),
            "rvol":    _round_or_none(top.get("rvol"), 1),
            "suggest": score >= suggest_min,
        }
    return {
        "ts":      f_ts or None,
        "waiting": not f_ts,
        "stale":   bool(f_ts) and (now_ts - f_ts) > max(refresh * 2, 120),
        "ranked":  sum(1 for r in f_rows if not r.get("rejects")),
        "session": {
            "label": label, "guide": guide, "color": color,
            "next_shot": ({"label": shot[0], "mins": shot[1]} if shot else None),
        },
        "top": top_out,
    }


def _snapshot() -> dict:
    tickers  = load_tickers()
    news     = load_news()
    swing    = load_swing()   # file read — confluence overlay applied under lock below
    # _build_mention_rank acquires STATE.lock internally, so call it BEFORE the
    # main lock below — threading.Lock is non-reentrant; nested acquisition deadlocks.
    mention_rank = _build_mention_rank(set(tickers))
    if mention_rank:
        refresh_ticker_timestamps(list(mention_rank.keys()))

    with STATE.lock:
        now_ts    = time.time()
        now_et    = datetime.now(ET)
        threshold = int(STATE.cfg.get("mention_alert_threshold", 5))
        m_window  = float(STATE.cfg.get("mention_alert_window", 10))
        f_rows    = list(STATE.funnel_rows)     # funnel overlay (see below)
        f_ts      = STATE.funnel_ts
        rows = []
        for t in tickers:
            d = dict(STATE.tickers.get(t, {}))
            d["ticker"] = t
            d["mentioned"] = t in mention_rank
            price    = d.get("price")
            day_open = d.get("day_open")
            d["pct_change"] = round((price - day_open) / day_open * 100, 2) if (price and day_open and day_open > 0) else None
            # Mention counts — count in-window entries without rebuilding the list;
            # _track_mention() prunes at write time so this path stays read-only.
            window_count        = sum(1 for tm in STATE.mention_ts.get(t, []) if now_ts - tm <= m_window)
            d["mention_count"]  = STATE.mention_daily.get(t, 0)
            d["mention_window"] = window_count
            d["mention_burst"]  = window_count >= threshold
            sent = _ticker_sentiment(t)
            if sent["count"] > 0:
                d["sentiment"] = sent
            conf = _confluence_sources(t)
            if len(conf) >= 2:
                d["confluence"] = {"sources": conf, "count": len(conf)}
            fif_ts = STATE.find_it_first_ts.get(t)
            if fif_ts and now_ts - fif_ts <= _FIND_IT_FIRST_TTL:
                d["find_it_first"] = True
            rows.append(d)

        # Morning-funnel overlay — attach the compact per-symbol score to any
        # watchlist row the funnel also ranked, so the badge reads inline.
        funnel_by_sym = {r.get("sym"): r for r in f_rows}
        for r in rows:
            fr = funnel_by_sym.get(r["ticker"])
            if fr:
                r["funnel"] = {
                    "score":   round(float(fr.get("score") or 0)),
                    "state":   fr.get("state"),
                    "rvol":    _round_or_none(fr.get("rvol"), 1),
                    "rejects": list(fr.get("rejects") or []),
                }

        market_sent = _ticker_sentiment(None)
        def _row_sort_key(r):
            in_mention = r["ticker"] in mention_rank
            rank  = mention_rank.get(r["ticker"], 999)
            price = r["price"] if r.get("price") is not None else float("inf")
            return (0 if in_mention else 1, rank, price)
        rows.sort(key=_row_sort_key)

        # Swing candidates — overlay LIVE Discord confluence onto the screener's
        # cached rows. A candidate that's also being talked about right now in the
        # Discord feed is a stronger setup; reuse the exact confluence system the
        # watchlist ⚡ badge uses so both stay consistent.
        swing_rows = []
        for c in swing:
            row = dict(c)
            conf = _confluence_sources(c.get("ticker"))
            if len(conf) >= 2:
                row["confluence"] = {"sources": conf, "count": len(conf)}
            swing_rows.append(row)

        # Merge signal-engine proximity + build badge. Done here (not just in the
        # /api/state HTTP handler) so the WebSocket stream — which the frontend
        # actually consumes — carries signal_proximity and the version too.
        sig = _load_signal_state() or {}
        sig_tickers = sig.get("tickers", {})
        if sig_tickers:
            for r in rows:
                sp = sig_tickers.get(r["ticker"])
                if sp:
                    r["signal_proximity"] = sp

        tv_last = STATE.tv_last_ts
        tv_feed = list(STATE.tv_alerts)

        _prune_expired_price_spikes(now_ts)

        return {
            "discord": {
                "running": bool(STATE.discord_last_ts)
                           and (now_ts - STATE.discord_last_ts) <= _DISCORD_STALE_SEC,
                "last_ts": STATE.discord_last_ts,
                "alerts":  list(STATE.discord_alerts),
                "count":   len(tickers),
            },
            "price_spikes": list(STATE.price_spikes),
            "tradingview": {
                "last_ts": tv_last,
                "alerts":  tv_feed,
            },
            "tickers": rows,
            "funnel":  _funnel_snapshot(f_rows, f_ts, now_ts, now_et, STATE.cfg),
            "market_sentiment": market_sent,
            "swing":   swing_rows,
            "news":    news,
            "config":  {k: STATE.cfg.get(k) for k in SAFE_CONFIG_KEYS},
            # Trading-engine status — trader mode + risk guard written by
            # signal_engine into signal_state.json; rendered by the Engine panel.
            "engine": {
                "strategy": sig.get("strategy"),
                "updated":  sig.get("updated"),
                "trader":   sig.get("trader"),
                "risk":     sig.get("risk"),
            },
            "version": {
                "dashboard":       version.get_version(),
                "engine":          sig.get("version"),
                "engine_strategy": sig.get("strategy"),
                "engine_started":  sig.get("started"),
                "engine_updated":  sig.get("updated"),
            },
        }




# ── FastAPI ───────────────────────────────────────────────────────────────────

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

# L2 monitor + broker endpoints for the Mobile Trader iPhone app
from trade_bridge.routes import router as _bridge_router, get_manager as _get_bridge  # noqa: E402
app.include_router(_bridge_router)


@app.on_event("startup")
async def _bridge_startup():
    # run an L2 engine for every momentum ticker so each symbol already
    # has a live stance when the phone opens it (load_tickers is defined
    # later in this module; resolved at call time).
    # Fail soft: missing Alpaca keys / SDK must not take down the dashboard.
    try:
        _get_bridge().start_auto_watch(lambda: load_tickers())
    except Exception as e:
        log.error("[BRIDGE] startup failed (dashboard continues): %s", e)


@app.on_event("shutdown")
async def _bridge_shutdown():
    try:
        await _get_bridge().shutdown()
    except Exception as e:
        log.warning("[BRIDGE] shutdown error: %s", e)


# ── Active session tracker ────────────────────────────────────────────────────

_ACTIVE_SESSIONS: dict = {}   # username → last_seen (epoch float)
_SESSION_LOCK = threading.Lock()
SESSION_TIMEOUT = 30 * 60     # 30 minutes = "currently active"


def _touch_session(username: str):
    """Record that this user just made an authenticated request."""
    if not username:
        return
    with _SESSION_LOCK:
        _ACTIVE_SESSIONS[username] = time.time()


def get_active_sessions() -> list[dict]:
    """Return list of {username, last_seen_ago_seconds} for recently active users."""
    now = time.time()
    with _SESSION_LOCK:
        return [
            {"username": u, "last_seen_seconds": int(now - ts)}
            for u, ts in sorted(_ACTIVE_SESSIONS.items(), key=lambda x: -x[1])
            if now - ts <= SESSION_TIMEOUT
        ]


# ── Auth middleware ───────────────────────────────────────────────────────────

_PUBLIC_PATHS   = {"/", "/login", "/register", "/auth/login", "/auth/register", "/api/meta", "/api/pnl", "/favicon.ico"}
_PUBLIC_PREFIX  = ("/static/", "/api/agent/")


def _request_identity(request: Request) -> tuple[str, str]:
    auth  = request.headers.get("authorization", "")
    token = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
    if not token:
        token = request.query_params.get("token", "")

    username = get_token_username(token) if token else ""
    if not username:
        query_user = request.query_params.get("user", "").strip().lower()
        if query_user == "jmb":
            username = "jmb"

    return token, username


class _AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Auth is opt-in — disabled by default for local use
        if not is_auth_required():
            return await call_next(request)

        path = request.url.path

        # Allow unauthenticated access to the login page, login endpoint, and static files
        if path in _PUBLIC_PATHS or any(path.startswith(p) for p in _PUBLIC_PREFIX):
            return await call_next(request)

        # CORS preflight passes through; CORSMiddleware handles the response
        if request.method == "OPTIONS":
            return await call_next(request)

        token, username = _request_identity(request)
        if not (verify_token(token) or username == "jmb"):
            return _SJSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)

        # Update last-seen for this user
        _touch_session(username)

        return await call_next(request)


# Middleware is applied outermost-last: CORS wraps AuthMiddleware wraps app
app.add_middleware(_AuthMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)



def _mention_reset_worker():
    import datetime as _dt
    ET = ZoneInfo("America/New_York")
    close_reset_fired = False
    while True:
        try:
            now   = _dt.datetime.now(ET)
            today = str(now.date())
            hhmm  = now.hour * 100 + now.minute

            with STATE.lock:
                if STATE.mention_reset_date != today:
                    STATE.mention_reset_date    = today
                    STATE.mention_market_opened = False
                    close_reset_fired = False
                    STATE.mention_daily.clear()
                    STATE.mention_ts.clear()
                    STATE.push_notified.clear()
                    STATE.sentiment_events.clear()
                    STATE.find_it_first_ts.clear()
                    log.info("[MENTIONS] Daily reset — new calendar day")

                elif 930 <= hhmm <= 935 and not STATE.mention_market_opened:
                    STATE.mention_market_opened = True
                    STATE.mention_daily.clear()
                    STATE.mention_ts.clear()
                    STATE.push_notified.clear()
                    STATE.sentiment_events.clear()
                    STATE.find_it_first_ts.clear()
                    log.info("[MENTIONS] Daily reset — market open window")

                elif 1605 <= hhmm <= 1610 and not close_reset_fired:
                    close_reset_fired = True
                    STATE.mention_daily.clear()
                    STATE.mention_ts.clear()
                    STATE.push_notified.clear()
                    STATE.sentiment_events.clear()
                    STATE.find_it_first_ts.clear()
                    log.info("[MENTIONS] Daily reset — market close window")

                elif hhmm > 1610 and close_reset_fired:
                    close_reset_fired = False

        except Exception as e:
            log.warning(f"[MENTIONS] reset worker error: {e}")

        time.sleep(30)

# ── Morning funnel — auto-rank candidates for the monitor slot ────────────────
# Runs the same scoring as tools/morning_funnel.py, but in-process: it reuses
# the already-connected Alpaca client and the live watchlist so the dashboard
# surfaces "point the monitors at SYM" without the separate CLI window. The scan
# makes network calls, so it lives on its own slow cadence and only deposits
# results on STATE; _snapshot() reads the cached rows for free.

_FUNNEL_MIN_REFRESH = 30.0   # floor on refresh cadence regardless of config

def _funnel_loop():
    try:
        import tools.morning_funnel as mf   # lazy: pulls pandas into this thread only
    except Exception as e:
        log.warning(f"[FUNNEL] disabled — import failed: {e}")
        return
    from datetime import datetime as _dt
    from session_clock import ET as _ET
    while True:
        refresh = _FUNNEL_MIN_REFRESH
        try:
            cfg    = STATE.cfg
            client = STATE.data_client
            refresh = max(float(cfg.get("funnel_refresh_sec", 150.0)), _FUNNEL_MIN_REFRESH)
            if client is None:          # Alpaca not connected yet — retry soon
                time.sleep(5)
                continue
            knobs = mf.knobs_from_cfg(cfg)
            # gather_candidates merges the live watchlist (wb_watchlist.json),
            # funnel_watchlist.txt, and swing_candidates.json — same sources the CLI uses.
            cands = mf.gather_candidates([], mf.ROOT, knobs["max_candidates"])
            rows  = mf.scan_once(client, cands, cfg, knobs, _dt.now(_ET)) if cands else []
            with STATE.lock:
                STATE.funnel_rows = rows
                STATE.funnel_ts   = time.time()
        except Exception as e:
            log.warning(f"[FUNNEL] scan error: {e}")
        time.sleep(refresh)


@app.on_event("startup")
async def _startup():
    loop = asyncio.get_running_loop()

    def _connect_alpaca():
        try:
            STATE.data_client = _api.connect_data_client(STATE.cfg)
            log.info("[STARTUP] Alpaca connected")
        except Exception as e:
            log.warning(f"[STARTUP] Alpaca unavailable: {e}")

    await loop.run_in_executor(None, _connect_alpaca)

    fh_key = STATE.cfg.get("finnhub_key", "")
    if fh_key:
        try:
            start_finnhub_stream(fh_key, load_tickers())
            log.info("[STARTUP] Finnhub stream started")
        except Exception as e:
            log.warning(f"[STARTUP] Finnhub unavailable: {e}")

    threading.Thread(target=_price_loop, daemon=True, name="price").start()
    threading.Thread(target=_vol_loop, daemon=True, name="vol").start()
    threading.Thread(target=_mention_reset_worker, daemon=True, name="mention-reset").start()
    threading.Thread(target=_funnel_loop, daemon=True, name="funnel").start()

    _generate_or_load_vapid_keys()
    STATE.push_subscriptions = _load_push_subscriptions()
    log.info(f"[PUSH] Loaded {len(STATE.push_subscriptions)} push subscription(s)")


@app.get("/")
async def root():
    return FileResponse("dashboard.html")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse("favicon.ico")


@app.get("/login")
async def login_page():
    return FileResponse("dashboard.html")


@app.get("/register")
async def register_page():
    return FileResponse("dashboard.html")


@app.post("/auth/login")
async def auth_login(request: Request):
    try:
        body     = await request.json()
        username = str(body.get("username", "")).strip()
        password = str(body.get("password", ""))

        # Prefer Cloudflare real-IP header; fall back to X-Forwarded-For, then direct peer
        ip = (
            request.headers.get("CF-Connecting-IP")
            or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or (request.client.host if request.client else "unknown")
        )
        ua = request.headers.get("user-agent", "")

        ok = check_credentials(username, password)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: record_login(username, ip, ua, success=ok))
        await loop.run_in_executor(None, lambda: send_login_email(username, ok, ip=ip, ua=ua))

        if not ok:
            return JSONResponse({"ok": False, "error": "Invalid credentials"}, status_code=401)
        return JSONResponse({"ok": True, "token": create_token(username)})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/auth/register")
async def auth_register(request: Request):
    """Create a new user account."""
    try:
        body     = await request.json()
        username = str(body.get("username", "")).strip()
        password = str(body.get("password", ""))

        if not username:
            return JSONResponse({"ok": False, "error": "Username is required"}, status_code=400)
        if len(username) < 3:
            return JSONResponse({"ok": False, "error": "Username must be at least 3 characters"}, status_code=400)
        if not password:
            return JSONResponse({"ok": False, "error": "Password is required"}, status_code=400)
        if len(password) < 6:
            return JSONResponse({"ok": False, "error": "Password must be at least 6 characters"}, status_code=400)

        if user_exists(username):
            return JSONResponse({"ok": False, "error": "Username already taken"}, status_code=409)

        loop = asyncio.get_running_loop()
        ok = await loop.run_in_executor(None, lambda: create_user(username, password))
        if not ok:
            return JSONResponse({"ok": False, "error": "Could not create account"}, status_code=500)

        log.info("[AUTH] Account created: %s", username)
        return JSONResponse({"ok": True, "message": "Account created successfully"})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.get("/api/state")
async def api_state():
    loop = asyncio.get_running_loop()
    # _snapshot() already merges signal_proximity + the version badge, so the
    # HTTP and WebSocket paths return identical data.
    snap = await loop.run_in_executor(None, _snapshot)
    return JSONResponse(snap)


@app.get("/api/signals")
async def api_signals():
    """
    Return the latest signal proximity state written by signal_engine.py.
    Each entry contains RSI, MACD histogram, proximity_pct (0–100), status,
    is_hot, mention_velocity, and data_source.

    The signal_engine.py process writes signal_state.json every 5 seconds;
    this endpoint reads and serves that file.  Returns an empty dict if the
    signal engine is not running.
    """
    data = _load_signal_state()
    return JSONResponse(data or {"updated": None, "tickers": {}})


_SIGNAL_STATE_FILE = Path(__file__).parent / "signal_state.json"

def _load_signal_state() -> Optional[dict]:
    """Read signal_state.json written by signal_engine.py, or return None."""
    if not _SIGNAL_STATE_FILE.exists():
        return None
    try:
        return json.loads(_SIGNAL_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


@app.get("/api/news")
async def api_news():
    return JSONResponse({"items": load_news()})


@app.get("/api/swing")
async def api_swing():
    """Swing-trade candidates from swing_candidates.json (written by the screener).
    Mostly for debugging — the frontend consumes the `swing` slice of /api/state."""
    return JSONResponse({"candidates": load_swing()})


# One screen at a time — the refresh button and the scheduled process both call
# run_screen; this guard stops overlapping runs from double-hitting the APIs.
_swing_refresh_lock = threading.Lock()

@app.post("/api/swing/refresh")
async def api_swing_refresh():
    """Trigger an on-demand swing screen (the dashboard refresh button). Runs the
    same run_screen the scheduled process uses, in a worker thread so the request
    returns promptly; the next /api/state poll picks up the fresh candidates."""
    if not _swing_refresh_lock.acquire(blocking=False):
        return JSONResponse({"ok": False, "status": "already-running"}, status_code=409)

    def _run():
        try:
            import swing_screener
            swing_screener.run_screen(load_config())
            _swing_cache["mtime"] = -1.0          # force reload on next serve
        except Exception as e:
            log.warning(f"[SWING] refresh failed: {e}")
        finally:
            _swing_refresh_lock.release()

    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, _run)
    return JSONResponse({"ok": True, "status": "started"})


@app.post("/api/swing/check")
async def api_swing_check(request: Request):
    """Evaluate arbitrary user-entered symbols against the swing filters and return
    a per-criterion reading for each — including symbols the screener would drop.
    Runs off the event loop since bars/fundamentals fetches block."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    raw = str(body.get("symbols", ""))
    # split on commas/whitespace, dedupe (order-preserving), cap to protect Finnhub quota
    seen: dict[str, None] = {}
    for tok in re.split(r"[,\s]+", raw.upper()):
        if tok:
            seen.setdefault(tok, None)
    symbols = list(seen)[:25]
    if not symbols:
        return JSONResponse({"results": []})

    def _run():
        import swing_screener
        cfg = load_config()
        return [swing_screener.evaluate(s, cfg) for s in symbols]

    loop = asyncio.get_running_loop()
    results = await loop.run_in_executor(None, _run)
    return JSONResponse({"results": results})


@app.post("/api/news")
async def api_save_news(request: Request):
    """Overwrite news.json with the posted items array (admin only)."""
    try:
        body  = await request.json()
        items = body.get("items", [])
        sanitised = []
        for item in items:
            headline = str(item.get("headline", "")).strip()
            if not headline:
                continue
            sanitised.append({
                "ticker":   str(item.get("ticker", "")).strip().upper(),
                "headline": headline,
                "date":     str(item.get("date",   "")).strip(),
                "body":     str(item.get("body",   "")).strip(),
            })
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: save_news(sanitised))
        return JSONResponse({"ok": True, "count": len(sanitised)})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/api/discord/ingest")
async def api_discord_ingest(request: Request):
    """The Discord OCR source POSTs here each poll: any newly-captured alerts
    plus an implicit heartbeat. Body: {"alerts": [{"ticker","line"}, ...]}.
    Each alert feeds the mention system (watchlist add + burst) and the live
    feed; an empty list is a valid heartbeat that keeps the source 'alive'."""
    try:
        body   = await request.json()
        alerts = body.get("alerts", [])
        if not isinstance(alerts, list):
            return JSONResponse({"ok": False, "error": "alerts must be a list"}, status_code=400)
        sentiment = body.get("sentiment", [])
        if not isinstance(sentiment, list):
            sentiment = []
    except Exception:
        alerts    = []
        sentiment = []
    loop = asyncio.get_running_loop()
    accepted = await loop.run_in_executor(None, lambda: ingest_discord_alerts(alerts, sentiment))
    return JSONResponse({"ok": True, "accepted": accepted})


@app.get("/api/discord/status")
async def api_discord_status():
    return JSONResponse(discord_status())


@app.post("/api/tradingview/webhook")
async def api_tv_webhook(request: Request):
    """Receive TradingView Pine Script alerts.

    Configure your TV alert with:
      Webhook URL:  https://trading.jbrasfield.com/api/tradingview/webhook?secret=<your_secret>
      Message:      {"ticker":"{{ticker}}","close":{{close}},"interval":"{{interval}}"}

    Set tv_webhook_secret in config/bot_config.json to validate the secret param.
    Leave it blank to accept all incoming webhooks (not recommended on public internet).
    """
    secret = str(STATE.cfg.get("tv_webhook_secret", "")).strip()
    if secret:
        token = (request.query_params.get("secret", "")
                 or request.headers.get("X-Webhook-Secret", ""))
        if token != secret:
            return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)

    ticker   = str(body.get("ticker", "")).strip()
    close    = float(body.get("close", 0) or 0)
    interval = str(body.get("interval", "")).strip()

    loop = asyncio.get_running_loop()
    ok   = await loop.run_in_executor(None, lambda: ingest_tv_alert(ticker, close, interval))
    if not ok:
        return JSONResponse({"ok": False, "error": "Invalid ticker"}, status_code=400)
    return JSONResponse({"ok": True, "ticker": ticker.split(":")[-1].upper()})


@app.get("/api/tradingview/status")
async def api_tv_status():
    return JSONResponse(tv_status())


# ── Trading-engine control (Engine panel) ─────────────────────────────────────

_ENGINE_RESTART_FLAG = Path(__file__).parent / "engine_restart.flag"


def _engine_control_allowed(request: Request) -> bool:
    """
    Mutating engine endpoints (settings writes, restart) are allowed for:
      • direct localhost requests (the local dashboard UI), or
      • requests carrying engine_control_secret (X-Engine-Secret header or
        ?secret=) set in config/bot_config.json.
    Requests through the Cloudflare tunnel LOOK local (the tunnel client runs
    on this machine), so proxy headers mark them as remote. This is stricter
    than the rest of the API on purpose: these endpoints move real settings
    on a system that trades money.
    """
    secret = str(STATE.cfg.get("engine_control_secret", "")).strip()
    if secret:
        token = (request.headers.get("X-Engine-Secret", "")
                 or request.query_params.get("secret", ""))
        if token == secret:
            return True
    via_proxy = bool(request.headers.get("CF-Connecting-IP")
                     or request.headers.get("X-Forwarded-For"))
    host = request.client.host if request.client else ""
    return (not via_proxy) and host in ("127.0.0.1", "::1", "localhost", "testclient")


@app.get("/api/engine/config")
async def api_engine_config_get():
    """Current whitelisted signal_engine.env values (no credentials)."""
    loop   = asyncio.get_running_loop()
    values = await loop.run_in_executor(None, engine_env.read_engine_env)
    return JSONResponse({"ok": True, "values": values})


@app.post("/api/engine/config")
async def api_engine_config_set(request: Request):
    """
    Update whitelisted signal_engine.env keys. Body: {"KEY": value, ...}.
    Values are validated (TRADER_MODE=live is always rejected). Changes take
    effect after an engine restart (/api/engine/restart).
    """
    if not _engine_control_allowed(request):
        return JSONResponse({"ok": False, "error": "Engine control requires localhost "
                             "or the engine control secret"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)
    if not isinstance(body, dict) or not body:
        return JSONResponse({"ok": False, "error": "Expected {KEY: value, ...}"}, status_code=400)

    loop = asyncio.get_running_loop()
    try:
        updated = await loop.run_in_executor(
            None, lambda: engine_env.update_engine_env(body))
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    log.info(f"[ENGINE] settings updated via dashboard: {', '.join(updated)}")
    return JSONResponse({"ok": True, "updated": updated, "restart_required": True})


@app.post("/api/engine/restart")
async def api_engine_restart(request: Request):
    """Ask the signal engine to re-exec itself (reloads signal_engine.env)."""
    if not _engine_control_allowed(request):
        return JSONResponse({"ok": False, "error": "Engine control requires localhost "
                             "or the engine control secret"}, status_code=403)
    _ENGINE_RESTART_FLAG.touch()
    log.info("[ENGINE] restart requested via dashboard")
    return JSONResponse({"ok": True, "note": "engine restarts within ~1s"})


_paper_report_mod = None


def _get_paper_report():
    """Lazy-import tools/paper_report.py (tools/ is not a package)."""
    global _paper_report_mod
    if _paper_report_mod is None:
        import importlib.util
        path = Path(__file__).parent / "tools" / "paper_report.py"
        spec = importlib.util.spec_from_file_location("paper_report", path)
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _paper_report_mod = mod
    return _paper_report_mod


@app.get("/api/engine/report")
async def api_engine_report(request: Request):
    """
    Paper/live performance report from signal_log.json — the same numbers as
    tools/paper_report.py --json, served for the Engine panel's report view.
    Query: days (default 14), size ($/trade for $ figures, default TRADE_AMOUNT).
    """
    try:
        days = int(request.query_params.get("days", "14"))
    except ValueError:
        days = 14
    try:
        size = float(request.query_params.get("size", "0")) or None
    except ValueError:
        size = None
    if size is None:
        env  = engine_env.read_engine_env()
        size = float(env.get("TRADE_AMOUNT") or 500)

    def _build():
        pr = _get_paper_report()
        closed, open_pos = pr.load_trades(pr.LOG_FILE, days, None)
        return pr.build_json(closed, open_pos, size)

    loop = asyncio.get_running_loop()
    try:
        report = await loop.run_in_executor(None, _build)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return JSONResponse({"ok": True, "days": days, "size": size, "report": report})


@app.post("/api/ticker-log/clear")
async def api_clear():
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, clear_ticker_log)
    return JSONResponse({"ok": True})


@app.post("/api/tickers/add")
async def api_add_ticker(request: Request):
    try:
        body   = await request.json()
        ticker = str(body.get("ticker", "")).strip().upper()
        count  = max(1, int(body.get("count", 1)))
        if not ticker or not ticker.isalpha() or len(ticker) > 5:
            return JSONResponse({"ok": False, "error": "Invalid ticker symbol"}, status_code=400)
        loop = asyncio.get_running_loop()
        ok, is_new = await loop.run_in_executor(None, lambda: add_ticker_to_log(ticker))
        # Record all mentions in this chunk
        with STATE.lock:
            for _ in range(count):
                _track_mention(ticker)
        return JSONResponse({"ok": ok, "is_new": is_new})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/api/tickers/mention")
async def api_mention_ticker(request: Request):
    """Record mention counts for a ticker already on the watchlist (no watchlist change)."""
    try:
        body   = await request.json()
        ticker = str(body.get("ticker", "")).strip().upper()
        count  = max(1, int(body.get("count", 1)))
        if not ticker:
            return JSONResponse({"ok": False, "error": "Missing ticker"}, status_code=400)
        with STATE.lock:
            for _ in range(count):
                _track_mention(ticker)
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/api/tickers/burst")
async def api_burst_ticker(request: Request):
    """Manually fire a burst alert for a ticker (injects threshold mentions at once)."""
    try:
        body   = await request.json()
        ticker = str(body.get("ticker", "")).strip().upper()
        if not ticker or not ticker.isalpha() or len(ticker) > 5:
            return JSONResponse({"ok": False, "error": "Invalid ticker symbol"}, status_code=400)
        threshold = int(STATE.cfg.get("mention_alert_threshold", 2))
        with STATE.lock:
            for _ in range(threshold):
                _track_mention(ticker)
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/api/tickers/remove")
async def api_remove_ticker(request: Request):
    try:
        body   = await request.json()
        ticker = str(body.get("ticker", "")).strip().upper()
        if not ticker:
            return JSONResponse({"ok": False, "error": "Missing ticker"}, status_code=400)
        loop = asyncio.get_running_loop()
        ok = await loop.run_in_executor(None, lambda: remove_ticker_from_log(ticker))
        return JSONResponse({"ok": ok})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/api/tickers/add-wb")
async def api_add_wb(request: Request):
    try:
        body   = await request.json()
        ticker = str(body.get("ticker", "")).strip().upper()
        if not ticker or not ticker.isalpha() or len(ticker) > 5:
            return JSONResponse({"ok": False, "error": "Invalid ticker symbol"}, status_code=400)
        threading.Thread(target=workflow_add_wb, args=(ticker,),
                         daemon=True, name=f"wb-{ticker}").start()
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/api/tickers/add-tv")
async def api_add_tv(request: Request):
    try:
        body   = await request.json()
        ticker = str(body.get("ticker", "")).strip().upper()
        if not ticker or not ticker.isalpha() or len(ticker) > 5:
            return JSONResponse({"ok": False, "error": "Invalid ticker symbol"}, status_code=400)
        cfg = STATE.cfg
        threading.Thread(target=workflow_add_brave_tv, args=(ticker, cfg),
                         daemon=True, name=f"tv-{ticker}").start()
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/api/tickers/add-wb-tv")
async def api_add_wb_tv(request: Request):
    try:
        body   = await request.json()
        ticker = str(body.get("ticker", "")).strip().upper()
        if not ticker or not ticker.isalpha() or len(ticker) > 5:
            return JSONResponse({"ok": False, "error": "Invalid ticker symbol"}, status_code=400)
        cfg = STATE.cfg
        threading.Thread(target=workflow_add_wb_and_tv, args=(ticker, cfg),
                         daemon=True, name=f"wbtv-{ticker}").start()
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/api/tickers/create-tv-alert")
async def api_create_tv_alert(request: Request):
    """Navigate TradingView to the given ticker and create a squeeze alert via Alt/Option+A."""
    try:
        body   = await request.json()
        ticker = str(body.get("ticker", "")).strip().upper()
        if not ticker or not ticker.isalpha() or len(ticker) > 5:
            return JSONResponse({"ok": False, "error": "Invalid ticker symbol"}, status_code=400)
        cfg = STATE.cfg
        threading.Thread(target=workflow_create_tv_alert, args=(ticker, cfg),
                         daemon=True, name=f"tvalert-{ticker}").start()
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/api/tickers/add-bulk")
async def api_add_bulk(request: Request):
    try:
        body = await request.json()
        raw  = body.get("tickers", [])
        if not isinstance(raw, list):
            return JSONResponse({"ok": False, "error": "tickers must be a list"}, status_code=400)
        loop  = asyncio.get_running_loop()
        added = []
        for item in raw[:100]:          # safety cap
            t = str(item).strip().upper()
            if not t or not t.isalpha() or not (2 <= len(t) <= 5):
                continue
            ok, is_new = await loop.run_in_executor(None, lambda t=t: add_ticker_to_log(t))
            if ok and is_new:
                added.append(t)
        return JSONResponse({"ok": True, "added": len(added), "tickers": added})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.get("/api/config")
async def api_config():
    with STATE.lock:
        return JSONResponse({"config": {k: STATE.cfg.get(k) for k in SAFE_CONFIG_KEYS}})


@app.post("/api/config")
async def api_config_save(request: Request):
    try:
        body       = await request.json()
        old_fh_key = STATE.cfg.get("finnhub_key", "")
        with STATE.lock:
            for k, v in body.items():
                if k in SAFE_CONFIG_KEYS:
                    STATE.cfg[k] = v
            save_config(dict(STATE.cfg))

        if any(k in body for k in ("api_key", "secret_key")):
            try:
                STATE.data_client = _api.connect_data_client(STATE.cfg)
            except Exception:
                pass

        new_fh_key = STATE.cfg.get("finnhub_key", "")
        if new_fh_key and new_fh_key != old_fh_key:
            try:
                start_finnhub_stream(new_fh_key, load_tickers())
                log.info("[CFG] Finnhub stream restarted with new key")
            except Exception as e:
                log.warning(f"[CFG] Finnhub restart: {e}")

        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.get("/api/push/vapid-public-key")
async def api_push_vapid_key():
    key = STATE.cfg.get("push_vapid_public_key", "")
    if not key:
        return JSONResponse({"error": "VAPID not configured"}, status_code=503)
    return JSONResponse({"key": key})


@app.post("/api/push/subscribe")
async def api_push_subscribe(request: Request):
    try:
        sub = await request.json()
        if not isinstance(sub, dict) or "endpoint" not in sub:
            return JSONResponse({"error": "invalid subscription"}, status_code=400)
        with STATE.lock:
            if not any(s.get("endpoint") == sub.get("endpoint") for s in STATE.push_subscriptions):
                STATE.push_subscriptions.append(sub)
                _save_push_subscriptions(STATE.push_subscriptions)
        return JSONResponse({"ok": True})
    except Exception as e:
        log.warning(f"[PUSH] subscribe error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.delete("/api/push/subscribe")
async def api_push_unsubscribe(request: Request):
    try:
        body     = await request.json()
        endpoint = body.get("endpoint", "")
        with STATE.lock:
            before = len(STATE.push_subscriptions)
            STATE.push_subscriptions = [
                s for s in STATE.push_subscriptions if s.get("endpoint") != endpoint
            ]
            if len(STATE.push_subscriptions) != before:
                _save_push_subscriptions(STATE.push_subscriptions)
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/meta")
async def api_meta(request: Request):
    _token, username = _request_identity(request)
    return JSONResponse({
        "auth_required": is_auth_required(),
        "is_admin":      is_admin_user(username),
        "username":      username,
    })


@app.get("/api/active-sessions")
async def api_active_sessions(request: Request):
    """Return currently active users. Admin-only."""
    _token, username = _request_identity(request)
    if is_auth_required() and not is_admin_user(username):
        return JSONResponse({"ok": False, "error": "Admin access required"}, status_code=403)
    sessions = get_active_sessions()
    return JSONResponse({"ok": True, "sessions": sessions, "count": len(sessions)})


@app.get("/api/login-log")
async def api_login_log():
    loop    = asyncio.get_running_loop()
    entries = await loop.run_in_executor(None, get_login_log)
    return JSONResponse({"entries": entries})


@app.post("/api/suggestions")
async def api_add_suggestion(request: Request):
    try:
        body    = await request.json()
        message = str(body.get("message", "")).strip()
        if not message:
            return JSONResponse({"ok": False, "error": "Empty message"}, status_code=400)
        ip = (
            request.headers.get("CF-Connecting-IP")
            or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or (request.client.host if request.client else "unknown")
        )
        ua   = request.headers.get("user-agent", "")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: save_suggestion(message, ip, ua))
        log.info(f"[SUGGESTION] from {ip}: {message[:60]}")
        # Fire email non-blocking — failures are logged but never break the response
        loop.run_in_executor(None, lambda: send_suggestion_email(message, ip, ua))
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.get("/api/suggestions")
async def api_get_suggestions():
    loop  = asyncio.get_running_loop()
    items = await loop.run_in_executor(None, load_suggestions)
    return JSONResponse({"suggestions": items})


@app.delete("/api/suggestions")
async def api_delete_suggestion(request: Request):
    try:
        body      = await request.json()
        timestamp = str(body.get("timestamp", "")).strip()
        if not timestamp:
            return JSONResponse({"ok": False, "error": "Missing timestamp"}, status_code=400)
        loop    = asyncio.get_running_loop()
        removed = await loop.run_in_executor(None, lambda: delete_suggestion(timestamp))
        if not removed:
            return JSONResponse({"ok": False, "error": "Not found"}, status_code=404)
        log.info(f"[SUGGESTION] deleted entry {timestamp}")
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


# ── Ticker feed (admin read/write) ───────────────────────────────────────────

@app.get("/api/ticker-feed")
async def api_get_ticker_feed():
    """Return current ticker feed items from ticker_feed.json."""
    try:
        items = json.loads(TICKER_FEED_FILE.read_text(encoding="utf-8")) if TICKER_FEED_FILE.exists() else []
        return JSONResponse({"items": items})
    except Exception as e:
        log.warning("[TICKER-FEED] corrupted ticker_feed.json, returning empty list: %s", e)
        return JSONResponse({"items": []})


@app.post("/api/ticker-feed")
async def api_save_ticker_feed(request: Request):
    """Overwrite ticker_feed.json with the posted items array."""
    try:
        body  = await request.json()
        items = body.get("items", [])
        # Validate: each item must have type and text strings
        allowed_types = {"info", "tip", "alert"}
        sanitised = []
        for item in items:
            t = str(item.get("type", "info")).lower()
            if t not in allowed_types:
                t = "info"
            sanitised.append({"type": t, "text": str(item.get("text", "")).strip()})
        sanitised = [i for i in sanitised if i["text"]]  # drop blank entries
        _atomic_write_json(TICKER_FEED_FILE, sanitised)
        return JSONResponse({"ok": True, "count": len(sanitised)})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ── Windows Agent proxy (forward requests from remote dashboard to local agent) ──

@app.post("/api/agent/add-wb")
async def api_agent_add_wb(request: Request):
    """Legacy path — broker retired; forwards to TradingView add."""
    try:
        body = await request.json()
        ticker = str(body.get("ticker", "")).strip().upper()
        if not ticker:
            return JSONResponse({"ok": False, "error": "missing ticker"}, status_code=400)
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post("http://localhost:8889/add-tv", json={"ticker": ticker})
            return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/agent/add-tv")
async def api_agent_add_tv(request: Request):
    """Proxy to Windows agent for adding ticker to TradingView watchlist."""
    try:
        body = await request.json()
        ticker = str(body.get("ticker", "")).strip().upper()
        if not ticker:
            return JSONResponse({"ok": False, "error": "missing ticker"}, status_code=400)
        
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post("http://localhost:8889/add-tv", json={"ticker": ticker})
            return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


def _agent_version() -> str:
    """Read the VERSION constant out of windows_agent.py — single source of truth."""
    try:
        src = (Path(__file__).parent / "windows_agent.py").read_text(encoding="utf-8")
        for line in src.splitlines():
            if line.startswith("VERSION"):
                m = re.search(r'VERSION\s*=\s*["\']([^"\']+)["\']', line)
                if m:
                    return m.group(1)
    except Exception:
        pass
    return "0.0.0"


@app.get("/api/download/wb-tv-agent")
async def download_wb_tv_agent(request: Request):
    """
    Package the WB+TV agent (windows_agent.py + launcher bat + requirements + README)
    into a versioned zip and return it as a download.  Admin only.
    """
    _token, username = _request_identity(request)
    if is_auth_required() and not is_admin_user(username):
        return JSONResponse({"ok": False, "error": "Admin access required"}, status_code=403)

    base    = Path(__file__).parent
    version = _agent_version()
    folder  = "wb-tv-agent"
    fname   = f"wb-tv-agent-v{version}.zip"

    README = f"""WB+TV Agent  v{version}
{'=' * (14 + len(version))}
Runs on your Windows machine and automates adding tickers to desktop broker
and TradingView when an alert fires on the Brasfield Momentum dashboard.

Setup
-----
1. Install Python 3.9+ (https://python.org) if not already installed.
2. Install dependencies:
       pip install -r requirements.txt
3. Copy .env.example to .env and fill in your values:
       DASHBOARD_URL   — your hosted dashboard URL
       DASHBOARD_USER  — matches dashboard_user in secrets.json (if auth enabled)
       DASHBOARD_PASS  — matches dashboard_pass in secrets.json (if auth enabled)
4. Double-click windows_agent.bat  (or run: python windows_agent.py)
   The agent polls the dashboard and auto-adds tickers on every alert.

Note: auth is disabled by default. Leave USER/PASS blank until you enable it.
"""

    REQUIREMENTS = "pyautogui\npygetwindow\npywin32\n"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        agent_path = base / "windows_agent.py"
        if agent_path.exists():
            zf.write(agent_path, f"{folder}/windows_agent.py")

        bat_path = base / "windows_agent.bat"
        if bat_path.exists():
            zf.write(bat_path, f"{folder}/windows_agent.bat")

        env_example = base / "wb_tv_agent_env_example.txt"
        if env_example.exists():
            zf.write(env_example, f"{folder}/.env.example")

        zf.writestr(f"{folder}/requirements.txt", REQUIREMENTS)
        zf.writestr(f"{folder}/README.txt", README)

    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket, token: str = ""):
    await ws.accept()
    if is_auth_required() and not verify_token(token):
        await ws.close(code=4001)
        return
    import json as _json
    last_snap_str = None
    try:
        while True:
            snap = await asyncio.get_running_loop().run_in_executor(None, _snapshot)
            snap_str = _json.dumps(snap, sort_keys=True, default=str)
            if snap_str != last_snap_str:
                await ws.send_text(snap_str)
                last_snap_str = snap_str
            await asyncio.sleep(0.25)  # 4Hz poll; only pushes on actual change
    except (WebSocketDisconnect, Exception):
        pass


if __name__ == "__main__":
    import webbrowser
    local_url = f"http://localhost:{PORT}"
    # Open the public (tunnelled) dashboard by default; override with DASHBOARD_OPEN_URL.
    open_url = os.environ.get(
        "DASHBOARD_OPEN_URL", "https://trading.jbrasfield.com/?user=jmb"
    )
    print(f"\n  Signal Scanner  —  {local_url}  (opening {open_url})\n  Ctrl+C to stop\n")
    threading.Timer(1.5, lambda: webbrowser.open(open_url)).start()
    uvicorn.run("dashboard:app", host="0.0.0.0", port=PORT, log_level="warning")
