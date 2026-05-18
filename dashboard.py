#!/usr/bin/env python3
"""
dashboard.py — Signal Scanner
Ties together transcription, real-time prices, and signals.
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
from datetime import datetime
from pathlib import Path
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
    except Exception:
        pass


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
from email_service import send_suggestion_email, send_login_email
import alpaca_api as _api

import sys as _sys
_sys.path.insert(0, str(Path(__file__).parent / "transcription"))
from workflows import workflow_add_wb, workflow_add_brave_tv, workflow_add_wb_and_tv
from finnhub_stream import (
    FINNHUB_STATE,
    start_finnhub_stream,
    request_subscribe as _fh_subscribe,
    fetch_realtime_quote as _fh_rest_quote,
)

ET                 = ZoneInfo("America/New_York")
PORT               = 8888
_TICKER_RE         = re.compile(r'\b([A-Z]{2,5})\b')
_SPEECH_LINE_RE    = re.compile(r'^\[(\d{2}:\d{2}:\d{2})\] ')
TICKER_LOG         = Path("transcription/wb_watchlist.json")
TRANSCRIBER_SCRIPT = Path("transcription/transcribe_action.py")
NEWS_FILE          = Path("news.json")
SUGGESTIONS_FILE   = Path("suggestions.json")
TICKER_FEED_FILE   = Path("static/ticker_feed.json")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


# ── Shared state ──────────────────────────────────────────────────────────────

class _State:
    def __init__(self):
        self.lock             = threading.Lock()
        self.cfg              = load_config()
        self.data_client      = None
        self.tickers: dict    = {}   # ticker → {price, price_ts, day_open, pct_change}
        self.transcriber      = None # subprocess.Popen or None
        self.transcript_lines = []   # in-memory only, never written to disk
        self.transcriber_metrics = {
            "queue_drops": 0,
            "ollama_timeouts": 0,
            "ollama_failures": 0,
            "ollama_retry_ok": 0,
            "queue_size": 0,
            "queue_capacity": 16,
            "workers": 0,
            "ollama_ready": False,
        }
        # Mention tracking — resets on server restart (fresh each trading day)
        self.mention_ts:    dict = {}  # ticker → [float, ...]  recent timestamps
        self.mention_daily: dict = {}  # ticker → int  daily total count
        from datetime import date as _date
        self.mention_reset_date: str  = str(_date.today())
        self.mention_market_opened: bool = False

    @property
    def transcriber_running(self) -> bool:
        return self.transcriber is not None and self.transcriber.poll() is None

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


def _mention_window_count(ticker: str) -> int:
    """Current mention count within the rolling window. Thread-safe read."""
    now    = time.time()
    window = float(STATE.cfg.get("mention_alert_window", 10))
    ts     = STATE.mention_ts.get(ticker, [])
    return sum(1 for t in ts if now - t <= window)


# ── Ticker log ────────────────────────────────────────────────────────────────
# File format: [{"ticker": "AAPL", "added": "2026-05-07T09:30:00-04:00"}, ...]
# Backward-compat: plain strings are migrated to objects on first read.

TICKER_MAX_AGE = 15 * 60  # seconds — entries older than this are auto-purged

_ticker_cache: dict = {"mtime": -1.0, "tickers": [], "entries": []}


def load_tickers() -> list:
    """Read watchlist, purge entries ≥ 15 min old, return list of ticker strings.

    Writes the file back whenever entries are purged or the format is migrated,
    so the on-disk file is always up-to-date. Caches by mtime to keep I/O cheap
    at the 10 Hz poll rate used by the price loop.
    """
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
            TICKER_LOG.parent.mkdir(parents=True, exist_ok=True)
            TICKER_LOG.write_text(_json.dumps(kept, indent=2), encoding="utf-8")
            mtime = TICKER_LOG.stat().st_mtime

        tickers = [e["ticker"] for e in kept]
        _ticker_cache.update(mtime=mtime, tickers=tickers, entries=kept)
        return tickers
    except Exception:
        return []


def clear_ticker_log():
    try:
        import json as _json
        TICKER_LOG.write_text(_json.dumps([]), encoding="utf-8")
        _ticker_cache.update(mtime=-1.0, tickers=[], entries=[])
    except Exception:
        pass
    with STATE.lock:
        STATE.tickers.clear()
        STATE.mention_ts.clear()
        STATE.mention_daily.clear()


def add_ticker_to_log(ticker: str) -> tuple[bool, bool]:
    """Add a single ticker. Returns (ok, is_new) — is_new is False when already present."""
    ticker = ticker.upper()
    try:
        import json as _json
        load_tickers()   # refresh cache + purge stale
        if ticker in _ticker_cache["tickers"]:
            return True, False
        now_iso = datetime.now(ET).isoformat(timespec="seconds")
        entries = list(_ticker_cache["entries"]) + [{"ticker": ticker, "added": now_iso}]
        TICKER_LOG.parent.mkdir(parents=True, exist_ok=True)
        TICKER_LOG.write_text(_json.dumps(entries, indent=2), encoding="utf-8")
        _ticker_cache["mtime"] = -1.0   # force re-read on next load
        return True, True
    except Exception as e:
        log.error(f"[TICKER] Add {ticker} failed: {e}")
        return False, False


def remove_ticker_from_log(ticker: str) -> bool:
    """Remove a single ticker from the watchlist and internal state."""
    ticker = ticker.upper()
    try:
        import json as _json
        load_tickers()   # refresh cache + purge stale
        if ticker not in _ticker_cache["tickers"]:
            return True
        entries = [e for e in _ticker_cache["entries"] if e["ticker"] != ticker]
        TICKER_LOG.write_text(_json.dumps(entries, indent=2), encoding="utf-8")
        _ticker_cache["mtime"] = -1.0
        with STATE.lock:
            STATE.tickers.pop(ticker, None)
            STATE.mention_ts.pop(ticker, None)
            STATE.mention_daily.pop(ticker, None)
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
    import json as _json
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
        TICKER_LOG.write_text(_json.dumps(entries, indent=2), encoding="utf-8")
        _ticker_cache.update(mtime=-1.0, entries=entries,
                             tickers=[e["ticker"] for e in entries])


# ── Transcription subprocess ──────────────────────────────────────────────────

_MAX_TRANSCRIPT_LINES = 200


_METRICS_RE = re.compile(r'^\[METRICS\]\s+(\{.*\})\s*$')


def _stdout_reader(proc: subprocess.Popen):
    try:
        for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip()
            if not line:
                continue
            m = _METRICS_RE.match(line)
            if m:
                try:
                    metrics = json.loads(m.group(1))
                    with STATE.lock:
                        STATE.transcriber_metrics = metrics
                except Exception:
                    pass
                continue
            with STATE.lock:
                STATE.transcript_lines.append(line)
                if len(STATE.transcript_lines) > _MAX_TRANSCRIPT_LINES:
                    STATE.transcript_lines = STATE.transcript_lines[-_MAX_TRANSCRIPT_LINES:]
    except Exception:
        pass


def read_transcript_lines(n: int = 60) -> list:
    with STATE.lock:
        return list(STATE.transcript_lines[-n:])


def clear_transcript():
    with STATE.lock:
        STATE.transcript_lines.clear()


def start_transcriber() -> dict:
    if STATE.transcriber_running:
        return {"ok": False, "msg": "already running"}
    clear_transcript()
    args = [sys.executable, "-u", str(TRANSCRIBER_SCRIPT)]
    device = STATE.cfg.get("device_index")
    if device is not None:
        args += ["--device", str(int(device))]
    try:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        proc = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # merge stderr so errors appear in transcript panel
            cwd=Path(__file__).parent,
            env=env,
        )
        with STATE.lock:
            STATE.transcriber = proc
        threading.Thread(target=_stdout_reader, args=(proc,), daemon=True, name="tx-reader").start()
        log.info(f"[TX] Started pid={proc.pid}")
        return {"ok": True}
    except Exception as e:
        log.error(f"[TX] Start failed: {e}")
        return {"ok": False, "msg": str(e)}


def stop_transcriber():
    with STATE.lock:
        proc, STATE.transcriber = STATE.transcriber, None
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    log.info("[TX] Stopped")


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

        for ticker in to_poll:
            try:
                q = _fh_rest_quote(api_key, ticker)
                if not q.get("ok"):
                    time.sleep(0.1)
                    continue
                price = float(q.get("c", 0))
                if price > 0:
                    FINNHUB_STATE.update_price(ticker, price)
                    day_open = float(q.get("o", 0))
                    if day_open > 0:
                        with STATE.lock:
                            entry = STATE.tickers.setdefault(ticker, {})
                            entry["day_open"] = round(day_open, 4)
                time.sleep(0.1)
            except Exception:
                pass
    finally:
        _finnhub_rest_running = False


def _price_loop():
    global _alpaca_fallback_running, _finnhub_rest_running
    last_alpaca_poll    = 0
    last_fh_rest_poll   = 0
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

        except Exception as e:
            log.debug(f"[PRICE] {e}")
        time.sleep(0.1)  # 10Hz — Finnhub ticks are picked up within 100ms


# ── Day-volume polling ────────────────────────────────────────────────────────

def _vol_loop():
    """Fetch day volume + relative volume for watchlist tickers via yfinance.
    Runs every 60 seconds in a background thread and writes into STATE.tickers
    so the values flow through to the /api/state response automatically.
    """
    while True:
        try:
            tickers = load_tickers()
            if tickers:
                import yfinance as yf
                tks = yf.Tickers(" ".join(tickers))
                vol_data: dict = {}
                for sym in tickers:
                    try:
                        fi = tks.tickers[sym].fast_info
                        day_vol = int(getattr(fi, "last_volume", 0) or 0)
                        avg_vol = int(getattr(fi, "three_month_average_volume", 0) or 0)
                        if day_vol > 0:
                            vol_data[sym] = {
                                "day_vol": day_vol,
                                "rvol": round(day_vol / avg_vol, 2) if avg_vol > 0 else None,
                            }
                    except Exception:
                        pass
                with STATE.lock:
                    for sym, vd in vol_data.items():
                        STATE.tickers.setdefault(sym, {}).update(vd)
                log.debug(f"[VOL] updated volume for {len(vol_data)}/{len(tickers)} tickers")
        except Exception as e:
            log.debug(f"[VOL] {e}")
        time.sleep(60)


# ── Mention detection ─────────────────────────────────────────────────────────

def _build_mention_rank(tx_lines: list, ticker_set: set, window_s: float = 30.0) -> dict:
    """Return {ticker: rank} for up to 3 watchlist tickers spoken in the last
    window_s seconds.  Rank 0 = most recently mentioned."""
    now = datetime.now()
    seen: list[str] = []
    for line in reversed(tx_lines):
        m = _SPEECH_LINE_RE.match(line)
        if not m:
            continue
        ts = m.group(1)
        try:
            line_time = now.replace(
                hour=int(ts[0:2]), minute=int(ts[3:5]), second=int(ts[6:8]), microsecond=0
            )
            if (now - line_time).total_seconds() > window_s:
                break  # lines are chronological; everything older can be skipped
        except ValueError:
            continue
        for sym in _TICKER_RE.findall(line):
            if sym in ticker_set and sym not in seen:
                seen.append(sym)
                if len(seen) == 3:
                    return {s: i for i, s in enumerate(seen)}

    return {s: i for i, s in enumerate(seen)}


# ── State snapshot ────────────────────────────────────────────────────────────

def _snapshot() -> dict:
    tickers  = load_tickers()
    news     = load_news()
    # Read transcript lines BEFORE acquiring STATE.lock — read_transcript_lines also
    # acquires STATE.lock, and threading.Lock is non-reentrant; nested acquisition deadlocks.
    tx_lines = read_transcript_lines(30)
    mention_rank = _build_mention_rank(tx_lines, set(tickers))
    if mention_rank:
        refresh_ticker_timestamps(list(mention_rank.keys()))

    with STATE.lock:
        now_ts    = time.time()
        threshold = int(STATE.cfg.get("mention_alert_threshold", 5))
        m_window  = float(STATE.cfg.get("mention_alert_window", 10))
        rows = []
        for t in tickers:
            d = dict(STATE.tickers.get(t, {}))
            d["ticker"] = t
            d["mentioned"] = t in mention_rank
            price    = d.get("price")
            day_open = d.get("day_open")
            d["pct_change"] = round((price - day_open) / day_open * 100, 2) if (price and day_open and day_open > 0) else None
            # Mention counts — prune stale timestamps in-place
            ts = STATE.mention_ts.get(t, [])
            ts = [tm for tm in ts if now_ts - tm <= m_window]
            STATE.mention_ts[t] = ts
            d["mention_count"]  = STATE.mention_daily.get(t, 0)
            d["mention_window"] = len(ts)
            d["mention_burst"]  = len(ts) >= threshold
            rows.append(d)
        def _row_sort_key(r):
            in_mention = r["ticker"] in mention_rank
            rank  = mention_rank.get(r["ticker"], 999)
            price = r["price"] if r.get("price") is not None else float("inf")
            return (0 if in_mention else 1, rank, price)
        rows.sort(key=_row_sort_key)
        return {
            "transcriber": {
                "running": STATE.transcriber_running,
                "lines":   tx_lines,
                "count":   len(tickers),
                "metrics": dict(STATE.transcriber_metrics),
            },
            "tickers": rows,
            "news":    news,
            "config":  {k: STATE.cfg.get(k) for k in SAFE_CONFIG_KEYS},
        }




# ── FastAPI ───────────────────────────────────────────────────────────────────

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")


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
                    log.info("[MENTIONS] Daily reset — new calendar day")

                elif 930 <= hhmm <= 935 and not STATE.mention_market_opened:
                    STATE.mention_market_opened = True
                    STATE.mention_daily.clear()
                    STATE.mention_ts.clear()
                    log.info("[MENTIONS] Daily reset — market open window")

                elif 1605 <= hhmm <= 1610 and not close_reset_fired:
                    close_reset_fired = True
                    STATE.mention_daily.clear()
                    STATE.mention_ts.clear()
                    log.info("[MENTIONS] Daily reset — market close window")

                elif hhmm > 1610 and close_reset_fired:
                    close_reset_fired = False

        except Exception as e:
            log.warning(f"[MENTIONS] reset worker error: {e}")

        time.sleep(30)

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
    snap = await loop.run_in_executor(None, _snapshot)
    return JSONResponse(snap)


@app.get("/api/news")
async def api_news():
    return JSONResponse({"items": load_news()})


@app.post("/api/transcriber/start")
async def api_tx_start():
    loop   = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, start_transcriber)
    return JSONResponse({**result, "running": STATE.transcriber_running})


@app.post("/api/transcriber/stop")
async def api_tx_stop():
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, stop_transcriber)
    return JSONResponse({"ok": True, "running": False})


@app.post("/api/ticker-log/clear")
async def api_clear():
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, clear_ticker_log)
    return JSONResponse({"ok": True})


@app.post("/api/transcript/clear")
async def api_transcript_clear():
    await asyncio.get_running_loop().run_in_executor(None, clear_transcript)
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
            if not t or not t.isalpha() or not (1 <= len(t) <= 5):
                continue
            ok, is_new = await loop.run_in_executor(None, lambda t=t: add_ticker_to_log(t))
            if ok and is_new:
                added.append(t)
        return JSONResponse({"ok": True, "added": len(added), "tickers": added})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.get("/api/audio-devices")
async def api_audio_devices():
    def _list():
        try:
            if sys.platform == "win32":
                import pyaudiowpatch as _pa_mod
            else:
                import pyaudio as _pa_mod
            p = _pa_mod.PyAudio()
            devices = []
            for i in range(p.get_device_count()):
                dev = p.get_device_info_by_index(i)
                if dev["maxInputChannels"] > 0:
                    devices.append({
                        "index":    i,
                        "name":     dev["name"],
                        "loopback": "loopback" in dev["name"].lower(),
                    })
            p.terminate()
            return {"ok": True, "devices": devices}
        except Exception as e:
            return {"ok": False, "error": str(e), "devices": []}
    result = await asyncio.get_running_loop().run_in_executor(None, _list)
    return JSONResponse(result)


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
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


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
        TICKER_FEED_FILE.write_text(
            json.dumps(sanitised, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return JSONResponse({"ok": True, "count": len(sanitised)})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ── Windows Agent proxy (forward requests from remote dashboard to local agent) ──

@app.post("/api/agent/add-wb")
async def api_agent_add_wb(request: Request):
    """Proxy to Windows agent for adding ticker to Webull watchlist."""
    try:
        body = await request.json()
        ticker = str(body.get("ticker", "")).strip().upper()
        if not ticker:
            return JSONResponse({"ok": False, "error": "missing ticker"}, status_code=400)
        
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post("http://localhost:8889/add-wb", json={"ticker": ticker})
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
                return line.split('"')[1]
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
Runs on your Windows machine and automates adding tickers to Webull Desktop
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
    url = f"http://localhost:{PORT}"
    print(f"\n  Signal Scanner  —  {url}\n  Ctrl+C to stop\n")
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run("dashboard:app", host="0.0.0.0", port=PORT, log_level="warning")
