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
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from config import load_config, save_config, SAFE_CONFIG_KEYS
from signals import compute_signals
import alpaca_api as _api

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

    @property
    def transcriber_running(self) -> bool:
        return self.transcriber is not None and self.transcriber.poll() is None

STATE = _State()


# ── Ticker log ────────────────────────────────────────────────────────────────

def load_tickers() -> list:
    if not TICKER_LOG.exists():
        return []
    try:
        import json as _json
        data = _json.loads(TICKER_LOG.read_text(encoding="utf-8"))
        return [t.strip().upper() for t in data if isinstance(t, str) and t.strip()]
    except Exception:
        return []


def clear_ticker_log():
    try:
        import json as _json
        TICKER_LOG.write_text(_json.dumps([]), encoding="utf-8")
    except Exception:
        pass
    with STATE.lock:
        STATE.tickers.clear()


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
            stderr=subprocess.DEVNULL,
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
    elif rte_fast > -60:
        status = "WARMING"
    else:
        status = "COLD"

    # 0 = far from zone edge, 1.0 = at or inside zone
    proximity = round(min(1.0, max(0.0, (rte_fast + 100) / max(1, 100 - threshold))), 3)

    return {
        "rte_fast":  round(rte_fast, 1),
        "rte_slow":  round(rte_slow, 1),
        "streak":    streak,
        "cm_rsi":    round(cm_rsi, 1),
        "obv_up":    obv_up,
        "status":    status,
        "proximity": proximity,
    }


def run_scan():
    tickers = load_tickers()
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
        log.info(f"[SCAN] {len(tickers)} tickers…")
        bars = _api.fetch_bars_batch(client, tickers, cfg)
        ts   = datetime.now(ET).strftime("%H:%M:%S")

        with STATE.lock:
            for t in tickers:
                df    = bars.get(t)
                entry = STATE.tickers.get(t, {})
                if df is None or len(df) < 50:
                    entry.update({"status": "NO_DATA", "proximity": 0,
                                  "streak": 0, "rte_fast": -100, "rte_slow": -100,
                                  "cm_rsi": 50, "obv_up": False})
                else:
                    try:
                        sig_df = compute_signals(df, cfg)
                        entry.update(_signal_summary(sig_df.iloc[-1], cfg))
                    except Exception as ex:
                        log.debug(f"[SCAN] {t}: {ex}")
                        entry["status"] = "ERROR"
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
        time.sleep(STATE.cfg.get("scan_interval_sec", 60))


# ── Price polling ─────────────────────────────────────────────────────────────

def _price_loop():
    while True:
        try:
            tickers = load_tickers()
            client  = STATE.data_client
            if tickers and client:
                prices = _api.get_latest_trade_prices(client, tickers, STATE.cfg)
                ts     = datetime.now(ET).strftime("%H:%M:%S")
                with STATE.lock:
                    for t, p in prices.items():
                        if t not in STATE.tickers:
                            STATE.tickers[t] = {}
                        STATE.tickers[t]["price"]    = round(p, 4)
                        STATE.tickers[t]["price_ts"] = ts
        except Exception as e:
            log.debug(f"[PRICE] {e}")
        time.sleep(3)


# ── State snapshot ────────────────────────────────────────────────────────────

_STATUS_ORDER = {"BUY": 0, "ON_DECK": 1, "WARMING": 2, "COLD": 3, "NO_DATA": 4, "ERROR": 5}

def _snapshot() -> dict:
    tickers = load_tickers()
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
                "lines":   read_transcript_lines(30),
                "count":   len(tickers),
            },
            "tickers":      rows,
            "scan_running": STATE.scan_running,
            "scan_ts":      STATE.scan_ts,
            "config":       {k: STATE.cfg.get(k) for k in SAFE_CONFIG_KEYS},
        }


# ── FastAPI ───────────────────────────────────────────────────────────────────

app = FastAPI()


@app.on_event("startup")
async def _startup():
    try:
        STATE.data_client = _api.connect_data_client(STATE.cfg)
        log.info("[STARTUP] Alpaca connected")
    except Exception as e:
        log.warning(f"[STARTUP] Alpaca unavailable: {e}")
    threading.Thread(target=_scan_loop,  daemon=True, name="scan").start()
    threading.Thread(target=_price_loop, daemon=True, name="price").start()


@app.get("/")
async def root():
    return FileResponse("dashboard.html")


@app.get("/api/state")
async def api_state():
    snap = await asyncio.get_event_loop().run_in_executor(None, _snapshot)
    return JSONResponse(snap)


@app.post("/api/transcriber/start")
async def api_tx_start():
    result = await asyncio.get_event_loop().run_in_executor(None, start_transcriber)
    return JSONResponse({**result, "running": STATE.transcriber_running})


@app.post("/api/transcriber/stop")
async def api_tx_stop():
    await asyncio.get_event_loop().run_in_executor(None, stop_transcriber)
    return JSONResponse({"ok": True, "running": False})


@app.post("/api/ticker-log/clear")
async def api_clear():
    await asyncio.get_event_loop().run_in_executor(None, clear_ticker_log)
    return JSONResponse({"ok": True})


@app.post("/api/transcript/clear")
async def api_transcript_clear():
    await asyncio.get_event_loop().run_in_executor(None, clear_transcript)
    return JSONResponse({"ok": True})


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
                        "index": i,
                        "name":  dev["name"],
                        "loopback": "loopback" in dev["name"].lower(),
                    })
            p.terminate()
            return {"ok": True, "devices": devices}
        except Exception as e:
            return {"ok": False, "error": str(e), "devices": []}
    result = await asyncio.get_event_loop().run_in_executor(None, _list)
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
        body = await request.json()
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
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            snap = await asyncio.get_event_loop().run_in_executor(None, _snapshot)
            await ws.send_json(snap)
            await asyncio.sleep(1)
    except (WebSocketDisconnect, Exception):
        pass


if __name__ == "__main__":
    import webbrowser
    url = f"http://localhost:{PORT}"
    print(f"\n  Signal Scanner  —  {url}\n  Ctrl+C to stop\n")
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run("dashboard:app", host="0.0.0.0", port=PORT, log_level="warning")
