#!/usr/bin/env python3
"""
dashboard.py — Signal Scanner
Ties together transcription, real-time prices, and signals.
  http://localhost:8888
"""

import asyncio
import logging
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse as _SJSONResponse

from auth import check_credentials, create_token, verify_token, is_auth_required

from config import load_config, save_config, SAFE_CONFIG_KEYS
from signals import compute_signals
import alpaca_api as _api
from scanner_models import ScannerBroadcast
from scanner_engine import ScannerEngine
from scanner_data import ScannerDataClient

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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


# ── Shared state ──────────────────────────────────────────────────────────────

class _State:
    def __init__(self):
        self.lock              = threading.Lock()
        self.cfg               = load_config()
        self.data_client       = None
        self.tickers: dict     = {}   # ticker → signal/price dict
        self.transcriber       = None # subprocess.Popen or None
        self.transcript_lines  = []   # in-memory only, never written to disk
        self.scan_ts           = ""
        self.scan_running      = False
        self.bars_cache        = {}   # ticker -> pd.DataFrame
        self.last_bar_fetch    = 0    # timestamp
        self.dirty_tickers     = set() # tickers needing signal recalculation
        self.api_call_count    = 0
        self.api_reset_ts      = time.time()
        self.executor          = ThreadPoolExecutor(max_workers=10)
        self.last_calc_price   = {}   # ticker -> last price used for signal calc
        self.scanner_state     = None  # last ScannerBroadcast as dict
        self.scanner_engine    = None  # ScannerEngine instance

    def record_api_call(self, count: int = 1):
        with self.lock:
            now = time.time()
            if now - self.api_reset_ts > 60:
                self.api_call_count = 0
                self.api_reset_ts = now
            self.api_call_count += count
            if self.api_call_count > 150:
                log.warning(f"[SAFETY] High API volume: {self.api_call_count} calls/min")

    @property
    def transcriber_running(self) -> bool:
        return self.transcriber is not None and self.transcriber.poll() is None

STATE = _State()


# ── Ticker log ────────────────────────────────────────────────────────────────

_ticker_cache: dict = {"mtime": -1.0, "tickers": []}


def load_tickers() -> list:
    """Read the watchlist JSON file, caching by mtime to avoid constant disk I/O."""
    if not TICKER_LOG.exists():
        _ticker_cache["mtime"]   = -1.0
        _ticker_cache["tickers"] = []
        return []
    try:
        mtime = TICKER_LOG.stat().st_mtime
        if mtime == _ticker_cache["mtime"]:
            return list(_ticker_cache["tickers"])
        import json as _json
        data    = _json.loads(TICKER_LOG.read_text(encoding="utf-8"))
        tickers = [t.strip().upper() for t in data if isinstance(t, str) and t.strip()]
        
        # Stricter validation for tickers already in the list
        tickers = [t for t in tickers if 2 <= len(t) <= 5 and t.isalpha()]
        
        _ticker_cache["mtime"]   = mtime
        _ticker_cache["tickers"] = tickers
        return tickers
    except Exception:
        return []


def clear_ticker_log():
    try:
        import json as _json
        TICKER_LOG.write_text(_json.dumps([]), encoding="utf-8")
        _ticker_cache["mtime"]   = -1.0
        _ticker_cache["tickers"] = []
    except Exception:
        pass
    with STATE.lock:
        STATE.tickers.clear()


def add_ticker_to_log(ticker: str) -> tuple[bool, bool]:
    """Add a single ticker. Returns (ok, is_new) — is_new is False when already present."""
    ticker = ticker.upper()
    try:
        import json as _json
        tickers = load_tickers()
        if ticker in tickers:
            return True, False
        tickers = sorted(set(tickers + [ticker]))
        TICKER_LOG.parent.mkdir(parents=True, exist_ok=True)
        TICKER_LOG.write_text(_json.dumps(tickers, indent=2), encoding="utf-8")
        _ticker_cache["mtime"] = -1.0   # invalidate cache
        return True, True
    except Exception as e:
        log.error(f"[TICKER] Add {ticker} failed: {e}")
        return False, False


def remove_ticker_from_log(ticker: str) -> bool:
    """Remove a single ticker from the watchlist and internal state."""
    ticker = ticker.upper()
    try:
        import json as _json
        tickers = load_tickers()
        if ticker not in tickers:
            return True
        tickers = [t for t in tickers if t != ticker]
        TICKER_LOG.write_text(_json.dumps(tickers, indent=2), encoding="utf-8")
        _ticker_cache["mtime"] = -1.0
        with STATE.lock:
            if ticker in STATE.tickers:
                del STATE.tickers[ticker]
            if ticker in STATE.bars_cache:
                del STATE.bars_cache[ticker]
            if ticker in STATE.last_calc_price:
                del STATE.last_calc_price[ticker]
        return True
    except Exception as e:
        log.error(f"[TICKER] Remove {ticker} failed: {e}")
        return False


# ── Transcription subprocess ──────────────────────────────────────────────────

_MAX_TRANSCRIPT_LINES = 200


def _stdout_reader(proc: subprocess.Popen):
    """Read subprocess stdout line-by-line into STATE.transcript_lines (in memory only)."""
    try:
        for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip()
            if not line:
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


# ── Signal computation ────────────────────────────────────────────────────────

def _signal_summary(row, cfg: dict, day_open: float = None, day_vol: int = None, rvol: float = None) -> dict:
    rte_fast  = float(row.get("rte_fast",  -100))
    rte_slow  = float(row.get("rte_slow",  -100))
    streak    = int(row.get("rte_boxes_streak", 0))
    cm_rsi    = float(row.get("cm_rsi",     50))
    obv_up    = bool(row.get("obv_trending_up", False))
    signal    = str(row.get("signal", "HOLD"))
    threshold = int(cfg.get("rte_threshold", 20))
    min_boxes = int(cfg.get("rte_min_boxes",  2))

    if signal == "BUY":
        status = "BUY"
    elif streak >= min_boxes:
        status = "ON_DECK"
    elif rte_fast < -60:
        status = "WARMING"
    else:
        status = "COLD"

    # Proximity: 0 = far from oversold, 1 = deep in oversold zone
    proximity = round(min(1.0, max(0.0, (-rte_fast - threshold) / max(1, 100 - threshold))), 3)

    return {
        "rte_fast":  round(rte_fast, 1),
        "rte_slow":  round(rte_slow, 1),
        "streak":    streak,
        "cm_rsi":    round(cm_rsi, 1),
        "obv_up":    obv_up,
        "status":    status,
        "proximity": proximity,
        "day_open":  round(day_open, 4) if day_open is not None else None,
        "day_vol":   day_vol,
        "rvol":      rvol,
    }


def _compute_ticker_signal(t: str, df: pd.DataFrame, lp: float, cfg: dict) -> tuple[str, any]:
    """Helper for parallel signal calculation."""
    if df is None or len(df) < 50:
        return t, None
    try:
        # Extract day_open, day_vol, rvol from real bars BEFORE injecting synthetic row
        day_open = None
        day_vol  = None
        rvol     = None
        try:
            if getattr(df.index, 'tz', None) is not None:
                idx_dates = df.index.tz_convert(ET).date
            else:
                idx_dates = df.index.date
            last_date = idx_dates[-1]
            mask      = idx_dates == last_date
            today_df  = df[mask]
            if not today_df.empty:
                day_open = float(today_df.iloc[0]["open"])
                day_vol  = int(today_df["volume"].sum())
        except Exception:
            pass
        if day_open is None:
            day_open = float(df.iloc[-1]["open"])
        try:
            avg_vol  = float(df["volume"].rolling(20).mean().iloc[-1])
            last_vol = float(df["volume"].iloc[-1])
            rvol = round(last_vol / avg_vol, 2) if avg_vol > 0 else None
        except Exception:
            pass

        # Inject latest price as a synthetic last row
        if lp:
            now_et = datetime.now(ET)
            new_row_data = [[lp, lp, lp, lp, 0]]
            new_row_df = pd.DataFrame(new_row_data, columns=["open", "high", "low", "close", "volume"], index=[now_et])
            df = pd.concat([df, new_row_df])

        sig_df = compute_signals(df, cfg)
        return t, _signal_summary(sig_df.iloc[-1], cfg, day_open=day_open, day_vol=day_vol, rvol=rvol)
    except Exception as ex:
        log.debug(f"[SCAN] {t}: {ex}")
        return t, False


def run_scan(force_fetch_bars: bool = False, ticker_subset: list = None):
    if not _scan_lock.acquire(blocking=False):
        return  # another scan in progress; drop this request

    # Only scan tickers that are in the watchlist
    watchlist_tickers = load_tickers()
    watchlist_set = set(watchlist_tickers)
    
    if ticker_subset:
        tickers = [t for t in ticker_subset if t in watchlist_set]
    else:
        tickers = list(watchlist_tickers)

    cfg     = STATE.cfg
    client  = STATE.data_client

    with STATE.lock:
        STATE.scan_running = True
    try:
        if not tickers or client is None:
            if client is None and tickers:
                log.warning("[SCAN] No Alpaca client")
            return
        ts_now = time.time()
        # Only fetch new bars if forced or if cache is older than 5 minutes
        with STATE.lock:
            needs_fetch = force_fetch_bars or (ts_now - STATE.last_bar_fetch > 300)
            bars = STATE.bars_cache

        if needs_fetch:
            all_tickers = load_tickers()
            log.info(f"[SCAN] Fetching fresh bars for {len(all_tickers)} tickers…")
            bars = _api.fetch_bars_batch(client, all_tickers, cfg)
            STATE.record_api_call(1)
            with STATE.lock:
                STATE.bars_cache = bars
                STATE.last_bar_fetch = ts_now
        
        ts   = datetime.now(ET).strftime("%H:%M:%S")

        # Get latest prices from Finnhub to inject into signal calculation
        latest_prices = {}
        if FINNHUB_STATE.connected:
            with FINNHUB_STATE.lock:
                latest_prices = {t: d["price"] for t in tickers if (d := FINNHUB_STATE.prices.get(t)) and d.get("price")}

        # Filter out tickers whose price hasn't changed enough since last calculation
        to_calc = []
        with STATE.lock:
            for t in tickers:
                lp = latest_prices.get(t)
                last_lp = STATE.last_calc_price.get(t)
                # If we have a price and it's basically the same as last time, skip
                if lp and last_lp and abs(lp - last_lp) < 1e-6:
                    continue
                to_calc.append(t)
                if lp:
                    STATE.last_calc_price[t] = lp

        if not to_calc:
            return

        # Compute signals in parallel WITHOUT holding STATE.lock
        futures = [
            STATE.executor.submit(_compute_ticker_signal, t, bars.get(t), latest_prices.get(t), cfg)
            for t in to_calc
        ]
        
        computed = {}
        for future in futures:
            t, result = future.result()
            computed[t] = result
        
        with STATE.lock:
            for t in tickers:
                entry  = STATE.tickers.get(t, {})
                result = computed.get(t)
                if result is None:
                    entry.update({"status": "NO_DATA", "proximity": 0,
                                  "streak": 0, "rte_fast": -100, "rte_slow": -100,
                                  "cm_rsi": 50, "obv_up": False})
                elif result is False:
                    entry["status"] = "ERROR"
                else:
                    entry.update(result)
                entry["last_scan"] = ts
                STATE.tickers[t]   = entry
            STATE.scan_ts = ts

        log.info(f"[SCAN] Done at {ts}")
    finally:
        with STATE.lock:
            STATE.scan_running = False
        _scan_lock.release()


def _scan_loop():
    time.sleep(2)   # let startup settle
    while True:
        try:
            run_scan()
        except Exception as e:
            log.error(f"[SCAN] {e}")
        # Sleep in 2-second increments so config interval changes take effect quickly
        start = time.monotonic()
        while True:
            time.sleep(2)
            if time.monotonic() - start >= STATE.cfg.get("scan_interval_sec", 60):
                break


# ── Price polling ─────────────────────────────────────────────────────────────

_scan_lock = threading.Lock()

# Alpaca fallback runs in its own thread so it never blocks the price loop.
_alpaca_price_cache: dict = {}          # last results from Alpaca fallback
_alpaca_cache_lock = threading.Lock()
_alpaca_fallback_running = False


def _alpaca_fallback_worker(client, tickers: list, cfg: dict):
    """Fetch Alpaca latest-trade prices in a background thread and cache the result."""
    global _alpaca_fallback_running
    try:
        prices = _api.get_latest_trade_prices(client, tickers, cfg)
        STATE.record_api_call(1)
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
                price = float(q.get("c", 0)) if q.get("ok") else 0
                if price > 0:
                    FINNHUB_STATE.update_price(ticker, price)
                time.sleep(0.1) # small throttle
            except Exception:
                pass
    finally:
        _finnhub_rest_running = False


def _price_loop():
    global _alpaca_fallback_running, _finnhub_rest_running
    last_signal_update  = 0
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
                        if t not in STATE.tickers:
                            STATE.tickers[t] = {}
                        
                        entry = STATE.tickers[t]
                        old_p = entry.get("price")
                        
                        # Mark as dirty if price changed (used for reactive signal recompute)
                        if old_p is not None and abs(p - old_p) / old_p > 1e-7:
                            STATE.dirty_tickers.add(t)
                        elif old_p is None:
                            STATE.dirty_tickers.add(t)
                            
                        entry["price"]    = round(p, 4)
                        entry["price_ts"] = ts

                # Reactive signal recompute — at most every 250ms
                if not STATE.scan_running and (now - last_signal_update > 0.25):
                    with STATE.lock:
                        to_scan = list(STATE.dirty_tickers)
                        STATE.dirty_tickers.clear()
                    if to_scan:
                        last_signal_update = now
                        threading.Thread(target=run_scan,
                                         kwargs={"force_fetch_bars": False, "ticker_subset": to_scan},
                                         daemon=True, name="scan-reactive").start()

        except Exception as e:
            log.debug(f"[PRICE] {e}")
        time.sleep(0.1)  # 10Hz — Finnhub ticks are picked up within 100ms


# ── Mention detection ─────────────────────────────────────────────────────────

def _build_mention_rank(tx_lines: list, ticker_set: set, window_s: float = 30.0) -> dict:
    """Return {ticker: rank} for up to 3 watchlist tickers spoken in the last
    window_s seconds.  Rank 0 = most recently mentioned.

    Only speech lines ([HH:MM:SS] ...) count — [LOG] lines mark freshly-added
    tickers (not already on the list) and are excluded entirely."""
    now = datetime.now()

    # [LOG] TICKER lines are emitted only when a ticker is newly added.
    recently_added: set[str] = set()
    for line in tx_lines:
        if line.startswith('[LOG] '):
            sym = line[6:].strip()
            if re.fullmatch(r'[A-Z]{2,5}', sym) and sym in ticker_set:
                recently_added.add(sym)

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
            if sym in ticker_set and sym not in recently_added and sym not in seen:
                seen.append(sym)
                if len(seen) == 3:
                    return {s: i for i, s in enumerate(seen)}

    return {s: i for i, s in enumerate(seen)}


# ── State snapshot ────────────────────────────────────────────────────────────

def _snapshot() -> dict:
    tickers  = load_tickers()
    # Read transcript lines BEFORE acquiring STATE.lock — read_transcript_lines also
    # acquires STATE.lock, and threading.Lock is non-reentrant; nested acquisition deadlocks.
    tx_lines = read_transcript_lines(30)
    mention_rank = _build_mention_rank(tx_lines, set(tickers))

    with STATE.lock:
        rows = []
        for t in tickers:
            d = dict(STATE.tickers.get(t, {}))
            d["ticker"] = t
            d["mentioned"] = t in mention_rank
            price    = d.get("price")
            day_open = d.get("day_open")
            d["pct_change"] = round((price - day_open) / day_open * 100, 2) if (price and day_open and day_open > 0) else None
            rows.append(d)
        rows.sort(key=lambda r: (
            mention_rank.get(r["ticker"], len(mention_rank)),
            -r.get("proximity", 0) if r["ticker"] not in mention_rank else 0,
            (r.get("price") or float("inf")) if r["ticker"] not in mention_rank else 0,
        ))
        return {
            "transcriber": {
                "running": STATE.transcriber_running,
                "lines":   tx_lines,
                "count":   len(tickers),
            },
            "tickers":      rows,
            "scan_running": STATE.scan_running,
            "scan_ts":      STATE.scan_ts,
            "config":       {k: STATE.cfg.get(k) for k in SAFE_CONFIG_KEYS},
            "scanner":      STATE.scanner_state,
        }


# ── Scanner engine ────────────────────────────────────────────────────────────

async def _on_scanner_broadcast(broadcast: ScannerBroadcast):
    with STATE.lock:
        STATE.scanner_state = broadcast.model_dump()


# ── FastAPI ───────────────────────────────────────────────────────────────────

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")


# ── Auth middleware ───────────────────────────────────────────────────────────

_PUBLIC_PATHS   = {"/", "/login", "/auth/login", "/api/meta"}
_PUBLIC_PREFIX  = ("/static/",)


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

        auth  = request.headers.get("authorization", "")
        token = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
        if not token:
            token = request.query_params.get("token", "")

        if not verify_token(token):
            return _SJSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)

        return await call_next(request)


# Middleware is applied outermost-last: CORS wraps AuthMiddleware wraps app
app.add_middleware(_AuthMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["Authorization", "Content-Type"],
)


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

    threading.Thread(target=_scan_loop,  daemon=True, name="scan").start()
    threading.Thread(target=_price_loop, daemon=True, name="price").start()

    fh_key_for_scanner = STATE.cfg.get("finnhub_key", "")
    scanner_data = ScannerDataClient(finnhub_key=fh_key_for_scanner)
    engine = ScannerEngine(scanner_data, poll_interval=60)
    with STATE.lock:
        STATE.scanner_engine = engine
    asyncio.create_task(engine.run(_on_scanner_broadcast))
    log.info("[STARTUP] Scanner engine started")


@app.get("/")
async def root():
    return FileResponse("dashboard.html")


@app.get("/login")
async def login_page():
    return FileResponse("login.html")


@app.post("/auth/login")
async def auth_login(request: Request):
    try:
        body     = await request.json()
        username = str(body.get("username", "")).strip()
        password = str(body.get("password", ""))
        if not check_credentials(username, password):
            return JSONResponse({"ok": False, "error": "Invalid credentials"}, status_code=401)
        return JSONResponse({"ok": True, "token": create_token()})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.get("/api/state")
async def api_state():
    loop = asyncio.get_running_loop()
    snap = await loop.run_in_executor(None, _snapshot)
    return JSONResponse(snap)


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
        if not ticker or not ticker.isalpha() or len(ticker) > 5:
            return JSONResponse({"ok": False, "error": "Invalid ticker symbol"}, status_code=400)
        loop = asyncio.get_running_loop()
        ok, is_new = await loop.run_in_executor(None, lambda: add_ticker_to_log(ticker))
        return JSONResponse({"ok": ok})
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
            import pyaudiowpatch as pyaudio
            p = pyaudio.PyAudio()
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


@app.post("/api/scan")
async def api_scan():
    threading.Thread(target=run_scan, daemon=True, name="scan-manual").start()
    return JSONResponse({"ok": True})


@app.get("/api/scanner/health")
async def api_scanner_health():
    with STATE.lock:
        engine = STATE.scanner_engine
    return JSONResponse({"running": engine.is_running if engine else False})


@app.post("/api/scanner/start")
async def api_scanner_start():
    with STATE.lock:
        engine = STATE.scanner_engine
    if engine and engine.is_running:
        return JSONResponse({"ok": True, "running": True})
    fh_key = STATE.cfg.get("finnhub_key", "")
    scanner_data = ScannerDataClient(finnhub_key=fh_key)
    new_engine = ScannerEngine(scanner_data, poll_interval=60)
    with STATE.lock:
        STATE.scanner_engine = new_engine
    asyncio.create_task(new_engine.run(_on_scanner_broadcast))
    return JSONResponse({"ok": True, "running": True})


@app.post("/api/scanner/stop")
async def api_scanner_stop():
    with STATE.lock:
        engine = STATE.scanner_engine
    if engine:
        engine.stop()
    return JSONResponse({"ok": True, "running": False})


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
async def api_meta():
    return JSONResponse({"auth_required": is_auth_required()})


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
            await asyncio.sleep(0.1)   # 10Hz poll; only pushes on change
    except (WebSocketDisconnect, Exception):
        pass


if __name__ == "__main__":
    import webbrowser
    url = f"http://localhost:{PORT}"
    print(f"\n  Signal Scanner  —  {url}\n  Ctrl+C to stop\n")
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run("dashboard:app", host="0.0.0.0", port=PORT, log_level="warning")
