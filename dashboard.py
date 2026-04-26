#!/usr/bin/env python3
"""
dashboard.py — Signal Scanner
Ties together transcription, real-time prices, and signals.
  http://localhost:8888
"""

import asyncio
import logging
import os
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
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from config import load_config, save_config, SAFE_CONFIG_KEYS
from signals import compute_signals
import alpaca_api as _api

import sys as _sys
_sys.path.insert(0, str(Path(__file__).parent / "transcription"))
from workflows import workflow_add_wb, workflow_add_wb_bulk
from finnhub_stream import (
    FINNHUB_STATE,
    start_finnhub_stream,
    request_subscribe as _fh_subscribe,
)

ET                 = ZoneInfo("America/New_York")
PORT               = 8888
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

def _signal_summary(row, cfg: dict) -> dict:
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
    }


def _compute_ticker_signal(t: str, df: pd.DataFrame, lp: float, cfg: dict) -> tuple[str, any]:
    """Helper for parallel signal calculation."""
    if df is None or len(df) < 50:
        return t, None
    try:
        # Inject latest price as a synthetic last row
        if lp:
            now_et = datetime.now(ET)
            # Faster way to append a single row to a DataFrame
            new_row_data = [[lp, lp, lp, lp, 0]]
            new_row_df = pd.DataFrame(new_row_data, columns=["open", "high", "low", "close", "volume"], index=[now_et])
            df = pd.concat([df, new_row_df])

        sig_df = compute_signals(df, cfg)
        return t, _signal_summary(sig_df.iloc[-1], cfg)
    except Exception as ex:
        log.debug(f"[SCAN] {t}: {ex}")
        return t, False


def run_scan(force_fetch_bars: bool = False, ticker_subset: list = None):
    tickers = ticker_subset if ticker_subset else load_tickers()
    cfg     = STATE.cfg
    client  = STATE.data_client

    if not tickers:
        return
    if client is None:
        log.warning("[SCAN] No Alpaca client")
        return

    with STATE.lock:
        STATE.scan_running = True
    try:
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

_known_tickers: set = set()

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


def _price_loop():
    global _known_tickers, _alpaca_fallback_running
    last_signal_update = 0
    last_alpaca_poll   = 0
    while True:
        try:
            tickers = load_tickers()
            current = set(tickers)

            # Auto-scan and subscribe Finnhub when new tickers appear
            new = current - _known_tickers
            if new and not STATE.scan_running:
                log.info(f"[PRICE] New tickers {new} — triggering auto-scan")
                if FINNHUB_STATE.connected:
                    _fh_subscribe(list(new))
                threading.Thread(target=run_scan, kwargs={"force_fetch_bars": True},
                                 daemon=True, name="scan-auto").start()
            _known_tickers = current

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

                # Merge: Finnhub wins over cached Alpaca values
                with _alpaca_cache_lock:
                    cached_alpaca = dict(_alpaca_price_cache)
                all_prices = {**cached_alpaca, **finnhub_prices}

                with STATE.lock:
                    for t, p in all_prices.items():
                        if t not in tickers:
                            continue
                        entry = STATE.tickers.get(t, {})
                        old_p = entry.get("price")
                        if old_p is not None and abs(p - old_p) / old_p > 0.0001:
                            STATE.dirty_tickers.add(t)
                        if t not in STATE.tickers:
                            STATE.tickers[t] = {}
                            STATE.dirty_tickers.add(t)
                        STATE.tickers[t]["price"]    = round(p, 4)
                        STATE.tickers[t]["price_ts"] = ts

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


# ── State snapshot ────────────────────────────────────────────────────────────

_STATUS_ORDER = {"BUY": 0, "ON_DECK": 1, "WARMING": 2, "COLD": 3, "NO_DATA": 4, "ERROR": 5}


def _snapshot() -> dict:
    tickers  = load_tickers()
    # Read transcript lines BEFORE acquiring STATE.lock — read_transcript_lines also
    # acquires STATE.lock, and threading.Lock is non-reentrant; nested acquisition deadlocks.
    tx_lines = read_transcript_lines(30)
    with STATE.lock:
        rows = []
        for t in tickers:
            d = dict(STATE.tickers.get(t, {}))
            d["ticker"] = t
            rows.append(d)
        rows.sort(key=lambda r: (_STATUS_ORDER.get(r.get("status", "NO_DATA"), 9),
                                 -r.get("proximity", 0)))
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
        }


# ── FastAPI ───────────────────────────────────────────────────────────────────

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")


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


@app.get("/")
async def root():
    return FileResponse("dashboard.html")


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
        if ok and is_new:
            threading.Thread(target=workflow_add_wb, args=(ticker,),
                             daemon=True, name=f"wb-{ticker}").start()
        return JSONResponse({"ok": ok})
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
        if added:
            # Single thread processes the list sequentially — concurrent threads
            # would interleave keystrokes in the Webull search box.
            threading.Thread(target=workflow_add_wb_bulk, args=(added,),
                             daemon=True, name="wb-bulk").start()
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


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    last_snap_hash = None
    try:
        while True:
            snap = await asyncio.get_running_loop().run_in_executor(None, _snapshot)
            # Only transmit when something actually changed — avoids redundant serialisation
            snap_hash = hash(str(snap))
            if snap_hash != last_snap_hash:
                await ws.send_json(snap)
                last_snap_hash = snap_hash
            await asyncio.sleep(0.1)   # 10Hz poll; only pushes on change
    except (WebSocketDisconnect, Exception):
        pass


if __name__ == "__main__":
    import webbrowser
    url = f"http://localhost:{PORT}"
    print(f"\n  Signal Scanner  —  {url}\n  Ctrl+C to stop\n")
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run("dashboard:app", host="0.0.0.0", port=PORT, log_level="warning")
