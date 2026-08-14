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
from traffic_log import (
    record_hit as record_traffic_hit,
    get_log as get_traffic_log,
    summarize as summarize_traffic,
    client_meta_from_request,
    client_ip_from_request,
)

from config import load_config, save_config, SAFE_CONFIG_KEYS
from session_clock import session_window, next_shot
import engine_env
import version

from email_service import send_suggestion_email, send_login_email, smtp_status
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
RS_FILE            = Path("rs_ratings.json")
CLAUDE_SUGGESTIONS_FILE = Path("claude_suggestions.json")
GROK_SUGGESTIONS_FILE   = Path("grok_suggestions.json")
AI_POSITIONS_FILE       = Path("ai_positions_state.json")
CLAUDE_POSITIONS_FILE   = Path("claude_positions_state.json")  # legacy alias
TRENDING_FILE      = Path("trending_stocks.json")
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
        # {reason: count} reported by the OCR source each heartbeat — what it
        # threw away, so a feed that has stopped surfacing names is visible
        # here instead of only inferable from the absence of alerts.
        self.discord_drops: dict = {}
        # "Bullish Bob LIVE" call-outs read off the same OCR stream, oldest
        # first. Display-only: they drive the header's "Suggests:" chip and its
        # history list, and are deliberately kept out of the mention/burst path
        # so a caller's chatter can never move the trader.
        self.bb_live: deque = deque(maxlen=_MAX_BB_LIVE)
        # Signals currently being price-tracked, keyed (ticker, kind) so a
        # call-out and a scanner spike on the same name stay separable —
        # telling them apart is the whole point of measuring them.
        self.signal_watch: dict = {}
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
# "Bullish Bob LIVE" call-outs → the header's "Suggests:" chip. History is kept
# for the session so the operator can look back; only the newest call within
# _BB_LIVE_FRESH_SEC is promoted to the live chip, so a symbol from two hours
# ago can never keep sitting under the product badge as if it were current.
_MAX_BB_LIVE       = 40
_BB_LIVE_FRESH_SEC = 30 * 60
# Signal shadow — the counterfactual price track behind every Discord-side
# signal (call-out, price spike, mention burst). Sampled off prices the price
# loop has already fetched, so it costs no API quota; forward returns are
# derived downstream by tools/signal_report.py rather than tracked here, the
# same division of labour ai_positions.log_shadow_sample uses.
_SIGNAL_WINDOW_SEC = 30 * 60   # how long a signal stays sampled
_SIGNAL_SAMPLE_SEC = 30.0      # cadence per symbol (price loop runs at 10Hz)

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


# ── Relative strength ─────────────────────────────────────────────────────────
# Written by rs_screener.py once a day after the close. Unlike the swing and
# momentum surfaces this is a DAILY statistic computed off completed sessions —
# it does not move intraday, and the header's as_of says which tape it is from.
#
# The whole payload is served, not just the rows: an RS rating is not
# interpretable without the population it was ranked against, and the price
# series is not comparable to anything without knowing its adjustment and feed.

_rs_cache: dict = {"mtime": -1.0, "payload": {}}

def load_rs() -> dict:
    """Read rs_ratings.json, cache by mtime. Returns the full payload (header
    plus `rows`), or {} when the screener has never run."""
    try:
        if not RS_FILE.exists():
            return {}
        mtime = RS_FILE.stat().st_mtime
        if mtime == _rs_cache["mtime"]:
            return _rs_cache["payload"]
        import json as _json
        payload = _json.loads(RS_FILE.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            payload = {}
        if not isinstance(payload.get("rows"), list):
            payload["rows"] = []
        _rs_cache.update(mtime=mtime, payload=payload)
        return payload
    except Exception as e:
        log.warning(f"[RS] Failed to load rs_ratings.json: {e}")
        return {}


# ── Claude trader + trending screener ─────────────────────────────────────────
# Written by ai_trader.py and trending_screener.py (Anthropic research +
# risk-sized entries + Stocktwits poll). Grok ideas land in grok_suggestions.json.
# The monitor renders from /api/state only.
#
# Served whole rather than rows-only, for the same reason as RS: the header
# says whether the desk is trading, in which mode, when the next research run
# is, and what went wrong — none of which the rows can convey on their own. A
# stale row list with no error field reads as "nothing is happening" when the
# truth may be "the CLI is missing."

def _load_json_payload(path: Path, cache: dict, label: str) -> dict:
    """Read a screener payload, cached by mtime. {} when it has never run."""
    try:
        if not path.exists():
            return {}
        mtime = path.stat().st_mtime
        if mtime == cache["mtime"]:
            return cache["payload"]
        import json as _json
        payload = _json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            payload = {}
        if not isinstance(payload.get("rows"), list):
            payload["rows"] = []
        cache.update(mtime=mtime, payload=payload)
        return payload
    except Exception as e:
        log.warning(f"[{label}] Failed to load {path.name}: {e}")
        return {}


_claude_suggestions_cache: dict = {"mtime": -1.0, "payload": {}}

def load_claude_suggestions() -> dict:
    return _load_json_payload(
        CLAUDE_SUGGESTIONS_FILE, _claude_suggestions_cache, "CLAUDE")


_grok_suggestions_cache: dict = {"mtime": -1.0, "payload": {}}

def load_grok_suggestions() -> dict:
    """xAI / Grok research publish file (optional until publisher is on)."""
    return _load_json_payload(
        GROK_SUGGESTIONS_FILE, _grok_suggestions_cache, "GROK")


def build_ai_suggestions(
    claude_payload: dict | None = None,
    grok_payload: dict | None = None,
) -> dict:
    """Merge Anthropic + xAI rows for the desk (agreement first, A/X/AX marks)."""
    from ai_suggest import merge_suggestion_rows

    claude_payload = claude_payload if claude_payload is not None else load_claude_suggestions()
    grok_payload = grok_payload if grok_payload is not None else load_grok_suggestions()
    c_rows = list((claude_payload or {}).get("rows") or [])
    g_rows = list((grok_payload or {}).get("rows") or [])
    merged = merge_suggestion_rows(c_rows, g_rows)
    c_ok = float((claude_payload or {}).get("last_ok") or 0) or 0.0
    g_ok = float((grok_payload or {}).get("last_ok") or 0) or 0.0
    last_ok = max(c_ok, g_ok)

    def _hard_error(payload: dict | None, rows: list) -> str:
        """Schedule 'next research…' is status when empty; not a failure with rows."""
        err = str((payload or {}).get("error") or "").strip()
        if not err:
            return ""
        soft = err.lower().startswith("next research") or err == "no research times configured"
        if soft and rows:
            return ""
        return err

    c_err = _hard_error(claude_payload, c_rows)
    g_err = _hard_error(grok_payload, g_rows)
    errs = []
    if c_err and not c_rows:
        errs.append(f"A:{c_err}")
    if g_err and not g_rows:
        errs.append(f"X:{g_err}")
    n_ax = sum(1 for r in merged if r.get("agreement"))
    # Desk rule: never attach a hard error banner when the merged table has
    # rows. One source can fail parse while the other (or prior publish) still
    # supplies ideas — the stamp/next_run_label carries schedule status.
    if merged:
        merged_error = ""
    elif errs:
        merged_error = " · ".join(errs)
    else:
        merged_error = c_err or g_err or ""
    # Prefer Anthropic last_usage (entry path), else Grok, else file latest.
    last_usage = (claude_payload or {}).get("last_usage") or {}
    if not last_usage:
        last_usage = (grok_payload or {}).get("last_usage") or {}
    token_day: dict = {}
    try:
        from ai_suggest import latest_token_usage, summarize_token_metrics
        token_day = summarize_token_metrics(day="today")
        if not last_usage:
            last_usage = latest_token_usage() or {}
    except Exception:
        token_day = {}

    # Book owner for display: prefer Grok when it is trading, else Claude.
    g_trading = bool((grok_payload or {}).get("trading"))
    c_trading = bool((claude_payload or {}).get("trading"))
    if g_trading:
        book = grok_payload or {}
    elif c_trading:
        book = claude_payload or {}
    else:
        book = {}

    return {
        "updated": time.time(),
        "last_ok": last_ok,
        "error": merged_error,
        "quotes_error": (claude_payload or {}).get("quotes_error")
                        or (grok_payload or {}).get("quotes_error")
                        or "",
        "last_quote_ok": max(
            float((claude_payload or {}).get("last_quote_ok") or 0),
            float((grok_payload or {}).get("last_quote_ok") or 0),
        ) or 0.0,
        "model": (book.get("model")
                  or (claude_payload or {}).get("model")
                  or (grok_payload or {}).get("model")
                  or ""),
        "backend": "merged",
        "source": "merged",
        "trading": bool(book.get("trading")),
        "trading_mode": book.get("trading_mode") or "off",
        "max_price": (claude_payload or {}).get("max_price")
                     if (claude_payload or {}).get("max_price") is not None
                     else (grok_payload or {}).get("max_price"),
        "next_run_label": (claude_payload or {}).get("next_run_label")
                          or (grok_payload or {}).get("next_run_label")
                          or "",
        "last_report_path": (claude_payload or {}).get("last_report_path")
                            or (grok_payload or {}).get("last_report_path")
                            or "",
        "last_trades": book.get("last_trades") or [],
        "last_usage": last_usage if isinstance(last_usage, dict) else {},
        "token_day": token_day,
        "n_anthropic": len(c_rows),
        "n_xai": len(g_rows),
        "n_agreement": n_ax,
        "rows": merged,
    }


_ai_positions_cache: dict = {"mtime": -1.0, "payload": {}}
_ai_positions_legacy_cache: dict = {"mtime": -1.0, "payload": {}}
# Last valid dashboard wire — ignore clobber from managed-book shape.
_ai_positions_last_good: dict = {}


def _is_ai_positions_wire(payload: dict | None) -> bool:
    """True if payload looks like ai_trader book wire, not managed {SYM: …}."""
    if not isinstance(payload, dict) or not payload:
        return False
    # Full wire always has a timestamp or nested positions map.
    if payload.get("updated") is not None:
        return True
    if "entry_book" in payload or "book_owner" in payload or "watch_meta" in payload:
        return True
    if isinstance(payload.get("positions"), dict):
        return True
    return False


def load_ai_positions() -> dict:
    """Shared trading book — prefer ai_positions_state.json, else legacy path.

    Reject bare managed-position maps (symbol keys only) so a bad write cannot
    push empty/stale into the WebSocket and flash AI Watch live↔stale.
    """
    global _ai_positions_last_good
    payload = _load_json_payload(AI_POSITIONS_FILE, _ai_positions_cache, "AI")
    if not _is_ai_positions_wire(payload):
        payload = _load_json_payload(
            CLAUDE_POSITIONS_FILE, _ai_positions_legacy_cache, "AI")
    if _is_ai_positions_wire(payload):
        _ai_positions_last_good = payload
        return payload
    if _is_ai_positions_wire(_ai_positions_last_good):
        return _ai_positions_last_good
    return payload if isinstance(payload, dict) else {}


# Back-compat name used by older clients / docs
load_claude_positions = load_ai_positions


def _ai_book_symbols(payload: dict | None = None) -> list[str]:
    """Symbols currently on the AI Watch book (entry_book / entry_watch / positions).

    Used to Finnhub-subscribe and paint live prices without waiting for the
    ~20s REST watch poller — same trade stream as Momentum Stocks.
    """
    if payload is None:
        payload = load_ai_positions()
    if not isinstance(payload, dict):
        return []
    out: list[str] = []
    seen: set[str] = set()

    def _add(raw) -> None:
        t = str(raw or "").strip().upper()
        if not t or not t.isalpha() or not (2 <= len(t) <= 5) or t in seen:
            return
        seen.add(t)
        out.append(t)

    for key in ("entry_book", "entry_watch"):
        rows = payload.get(key)
        if not isinstance(rows, list):
            continue
        for r in rows:
            if isinstance(r, dict):
                _add(r.get("symbol") or r.get("ticker"))
    pos = payload.get("positions")
    if isinstance(pos, dict):
        for sym in pos:
            _add(sym)
    return out


def _live_quote_for(sym: str, now: float | None = None) -> tuple[float | None, float | None]:
    """(price, age_sec) from Finnhub/Alpaca desk feeds, or (None, None).

    Prefers the freshest trade among STATE.tickers (already merged by the price
    loop), Finnhub stream, and the Alpaca REST cache — same sources Momentum
    Stocks uses.
    """
    t = str(sym or "").strip().upper()
    if not t:
        return None, None
    now = float(now if now is not None else time.time())
    candidates: list[tuple[float, float]] = []  # (obs_ts, price)

    with STATE.lock:
        ent = STATE.tickers.get(t) or {}
    try:
        px = float(ent.get("price") or 0)
    except (TypeError, ValueError):
        px = 0.0
    if px > 0:
        age = ent.get("price_age_sec")
        try:
            age_f = float(age) if age is not None else None
        except (TypeError, ValueError):
            age_f = None
        if age_f is not None and age_f >= 0:
            candidates.append((now - age_f, px))
        else:
            # Unknown age still usable as a last resort (below fresher prints).
            candidates.append((0.0, px))

    with FINNHUB_STATE.lock:
        fh = FINNHUB_STATE.prices.get(t) or {}
    try:
        fpx = float(fh.get("price") or 0)
    except (TypeError, ValueError):
        fpx = 0.0
    if fpx > 0:
        ts = float(fh.get("ts_unix") or fh.get("trade_ts") or 0)
        candidates.append((ts if ts > 0 else 0.0, fpx))

    with _alpaca_cache_lock:
        ap = _alpaca_price_cache.get(t)
    if ap:
        try:
            # cache: (price, write_ts, trade_ts)
            apx = float(ap[0] or 0)
            ats = float(ap[2] or ap[1] or 0)
        except (TypeError, ValueError, IndexError):
            apx, ats = 0.0, 0.0
        if apx > 0:
            candidates.append((ats if ats > 0 else 0.0, apx))

    if not candidates:
        return None, None
    candidates.sort(key=lambda c: c[0], reverse=True)
    obs, price = candidates[0]
    age_sec = round(max(0.0, now - obs), 1) if obs > 0 else None
    return round(price, 4), age_sec


def overlay_ai_book_live_prices(
    payload: dict | None,
    *,
    now: float | None = None,
    max_age_sec: float = 120.0,
) -> dict:
    """Stamp entry_book / entry_watch rows with live desk prices for the WS.

    Mutates a shallow copy of *payload* so the on-disk ai_positions file is
    untouched (poller last_ask stays the authority for arming). Rows with a
    print older than *max_age_sec* keep the poller's last_ask.
    """
    if not isinstance(payload, dict) or not payload:
        return payload if isinstance(payload, dict) else {}
    now = float(now if now is not None else time.time())
    out = dict(payload)
    for key in ("entry_book", "entry_watch"):
        rows = out.get(key)
        if not isinstance(rows, list) or not rows:
            continue
        new_rows = []
        for r in rows:
            if not isinstance(r, dict):
                new_rows.append(r)
                continue
            row = dict(r)
            sym = str(row.get("symbol") or row.get("ticker") or "").upper()
            px, age = _live_quote_for(sym, now)
            if px is not None and (age is None or age <= max_age_sec):
                row["price"] = px
                # Always overwrite last_ask for display so AI Watch tracks the
                # same Finnhub/Alpaca stream as Momentum Stocks (arming still
                # uses the poller's on-disk last_ask via entry_watch_state).
                row["last_ask"] = px
                row["price_src"] = "stream"
                if age is not None:
                    row["price_age_sec"] = age
                # Live above/below from stream vs zone so BLOCKER is not stuck
                # on a 20s poller verdict while PRICE ticks. Armable overshoots
                # (within max_r below the floor) count as in-zone — same rule
                # as ai_entry_watch.ask_triggers_zone / should_arm_buy.
                try:
                    lo = float(row.get("entry_low") or 0)
                    hi = float(row.get("entry_high") or 0)
                except (TypeError, ValueError):
                    lo = hi = 0.0
                try:
                    stop = float(row.get("stop_price") or 0) or None
                except (TypeError, ValueError):
                    stop = None
                # RStop only on an open long. Watch rows keep the plan stop
                # in stop_price; do not preview last − give above the zone.
                is_open = bool(
                    row.get("is_position")
                    or str(row.get("phase") or "") == "open")
                if not is_open:
                    row["local_stop"] = None
                else:
                    try:
                        from ai_positions import never_lower_rstop
                        locked = never_lower_rstop(
                            row.get("local_stop"),
                            row.get("local_stop_price"),
                            row.get("entry_stop_price"),
                        )
                        if locked is not None:
                            row["local_stop"] = locked
                    except Exception:
                        pass
                if lo > 0 and hi > 0:
                    try:
                        from ai_entry_watch import (
                            ask_triggers_zone,
                            arm_below_max_r,
                            DEFAULT_ARM_BELOW_MAX_R,
                        )
                        try:
                            from config import load_config
                            _max_r = arm_below_max_r(load_config())
                        except Exception:
                            _max_r = DEFAULT_ARM_BELOW_MAX_R
                        triggers = ask_triggers_zone(
                            px, lo, hi,
                            stop=stop,
                            max_below_r=_max_r,
                            arm_below=True,
                        )
                    except Exception:
                        triggers = min(lo, hi) <= px <= max(lo, hi)
                    if triggers:
                        row["blocker"] = "in zone"
                        row["block_code"] = "in_zone"
                        row["in_zone"] = True
                        row["ready"] = True
                    elif px > max(lo, hi):
                        row["blocker"] = "above zone"
                        row["block_code"] = "above_zone"
                        row["in_zone"] = False
                        row["ready"] = False
                    else:
                        row["blocker"] = "below zone"
                        row["block_code"] = "below_zone"
                        row["in_zone"] = False
                        row["ready"] = False
            new_rows.append(row)
        out[key] = new_rows
    return out


_trending_cache: dict = {"mtime": -1.0, "payload": {}}

def load_trending() -> dict:
    return _load_json_payload(TRENDING_FILE, _trending_cache, "TRENDING")


def filter_trending_by_max_price(
    payload: dict | None,
    max_price: float | None,
) -> dict:
    """Drop names at/above max_price when last price is known (same rule as monitor)."""
    payload = dict(payload or {})
    rows = list(payload.get("rows") or [])
    if max_price is None:
        payload["max_price"] = None
        payload["rows"] = rows
        return payload
    try:
        cap = float(max_price)
    except (TypeError, ValueError):
        payload["max_price"] = None
        payload["rows"] = rows
        return payload
    kept: list = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        px = r.get("price")
        try:
            if px is not None and float(px) >= cap:
                continue
        except (TypeError, ValueError):
            pass
        kept.append(r)
    payload["rows"] = kept
    payload["max_price"] = cap
    return payload


def _trending_max_price_from_cfg(cfg: dict | None = None) -> float | None:
    """Prefer stocktwits_max_price; fall back to trending_max_price; default $35."""
    cfg = cfg if cfg is not None else (STATE.cfg if hasattr(STATE, "cfg") else {})
    raw = cfg.get("stocktwits_max_price")
    if raw is None:
        raw = cfg.get("trending_max_price", 35.0)
    if raw is None or raw == "" or raw is False:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 35.0


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
        entry = STATE.tickers.get(ticker, {})
        price = entry.get("price")
        _archive_burst(ticker, price, len(STATE.mention_ts[ticker]), entry, now)
        # Locked variant: this function is documented as lock-held.
        note_signal_locked(ticker, "mention_burst", now, entry, now)
        threading.Thread(
            target=_send_push_notifications,
            args=(ticker, price),
            daemon=True,
        ).start()


# Redirectable so the suite does not append synthetic bursts to the real
# benchmark record. These files are analysis input — a run that injects rows
# with null prices and identical timestamps quietly corrupts the dataset any
# later threshold work would be calibrated against. Same reasoning as
# TRADE_GUARD_STATE_FILE; see tests/conftest.py.
_BENCH_DIR      = Path(os.getenv("BENCHMARK_DIR")
                       or (Path(__file__).parent / "benchmarks"))
_BURST_LOG      = _BENCH_DIR / "mention_bursts.jsonl"
_PRICE_SPIKE_LOG = _BENCH_DIR / "price_spikes.jsonl"


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


def _archive_burst(ticker: str, price, window_count: int,
                   entry: dict | None = None, now: float | None = None):
    """
    Append every mention burst to an append-only JSONL archive.

    This is the missing dataset for the excellence loop: backtests showed the
    3-indicator entry has no standalone edge on alert-pool microcaps — the
    catalyst (the burst) is the candidate edge. Replaying catalyst-gated
    entries needs burst timestamps, which the in-memory state doesn't keep.

    A burst usually fires before the price poller has a quote for the name —
    Discord surfaces a microcap the instant it is mentioned, and for OTC symbols
    Alpaca and Finnhub return nothing at all — which left 60% of the archive
    carrying price=null and therefore useless for the forward-return work it
    exists to support. The scanner's own price seed is the fallback: it is a
    snapshot from when the alert fired rather than a live quote, so it is
    recorded with its source and age instead of being merged into `price` and
    silently passed off as one.
    """
    entry = entry or {}
    now   = now or time.time()
    src   = "quote" if price is not None else None
    age   = None
    if price is None:
        seed = entry.get("scanner_price")
        if seed is not None:
            price = seed
            src   = "scanner"
            seed_ts = entry.get("scanner_price_ts")
            if seed_ts:
                age = round(max(0.0, now - float(seed_ts)), 1)
    try:
        _BURST_LOG.parent.mkdir(exist_ok=True)
        with open(_BURST_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ticker": ticker,
                "time":   datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "unix":   round(now, 1),
                "price":  price,
                "price_src": src,          # "quote" | "scanner" | None
                "price_age_sec": age,      # how stale the scanner seed was
                "window_count": window_count,
            }) + "\n")
    except Exception as e:
        log.warning(f"[BURST] archive failed: {e}")


def _archive_bb_live(rec: dict, entry: dict | None = None, now: float | None = None):
    """Append one "Bullish Bob LIVE" call-out to ai_reports/bb_live.jsonl.

    The header chip is in-memory only — a deque that empties on every dashboard
    restart, of which there are several on any day the code changes. That made
    the desk's newest and most prominent signal the only one it kept no record
    of, so "was the call worth acting on?" had no data behind it at all.

    Each row carries the price at call time and where that price came from, so
    it can serve as the t0 anchor for forward-return work. `at` is when the call
    was made (Discord's stamp where OCR caught it), `unix` when we read it.

    A dashboard restart can re-archive a call still on screen; dedupe on
    (ticker, said, text) when analysing.
    """
    entry = entry or {}
    now   = now or time.time()
    price = entry.get("price")
    src   = "quote" if price is not None else None
    age   = None
    if price is None:
        seed = entry.get("scanner_price")
        if seed is not None:
            price, src = seed, "scanner"
            seed_ts = entry.get("scanner_price_ts")
            if seed_ts:
                age = round(max(0.0, now - float(seed_ts)), 1)
    try:
        import ai_paths
        path = ai_paths.report_file("bb_live.jsonl", create_parent=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                **rec,
                "time":  datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "price": price,
                "price_src": src,
                "price_age_sec": age,
            }) + "\n")
    except Exception as e:
        log.warning(f"[BB-LIVE] archive failed: {e}")


def _entry_price(entry: dict, now: float) -> tuple[float | None, str | None, float | None]:
    """(price, source, seed_age) from a watchlist entry, quote preferred.

    The scanner seed is a snapshot from when the alert fired, not a quote, so it
    is reported as its own source with its age rather than merged into `price`.
    """
    price = entry.get("price")
    if price is not None:
        return price, "quote", None
    seed = entry.get("scanner_price")
    if seed is None:
        return None, None, None
    seed_ts = entry.get("scanner_price_ts")
    age = round(max(0.0, now - float(seed_ts)), 1) if seed_ts else None
    return seed, "scanner", age


def note_signal_locked(ticker: str, kind: str, at: float,
                       entry: dict | None = None, now: float | None = None) -> None:
    """note_signal for callers that already hold STATE.lock (_track_mention)."""
    now = now or time.time()
    price, src, age = _entry_price(entry or {}, now)
    STATE.signal_watch[(ticker, kind)] = {
        "ticker": ticker, "kind": kind, "at": at,
        "entry_price": price, "entry_price_src": src,
        "entry_price_age_sec": age,
        "last_sample": 0.0,
    }


def note_signal(ticker: str, kind: str, at: float, entry: dict | None = None,
                now: float | None = None) -> None:
    """Start price-tracking a Discord-side signal.

    `kind` is "bb_live" | "price_spike" | "mention_burst". `at` is when the
    signal happened (Discord's own stamp for a call-out, capture time
    otherwise) — the anchor forward returns are measured from.

    Re-signalling the same (ticker, kind) restarts the window: a second call on
    a name is a fresh opinion, and the row it anchors should be measured from
    when it was made rather than from the first one.
    """
    with STATE.lock:
        note_signal_locked(ticker, kind, at, entry, now)


def _sample_signal_shadow(now: float | None = None) -> int:
    """Append one price sample per tracked signal that is due. Returns the count.

    Called from the price loop off prices it has already merged — this must
    never cost an API call, for the same reason log_shadow_sample must not: the
    desk is already over Alpaca's rate limit and the names that can actually
    trade are the ones being starved.

    A symbol with no price yet is still sampled, with price null. Call-outs are
    deliberately kept out of the watchlist, so some never get a quote at all —
    recording the blank makes that coverage gap measurable instead of leaving
    the file quietly short of the signals it was supposed to cover.
    """
    if not STATE.signal_watch:      # fast path — the price loop calls this at 10Hz
        return 0
    now = now or time.time()
    due: list[dict] = []
    with STATE.lock:
        for key, rec in list(STATE.signal_watch.items()):
            if now - rec["at"] > _SIGNAL_WINDOW_SEC:
                del STATE.signal_watch[key]
                continue
            if now - rec["last_sample"] < _SIGNAL_SAMPLE_SEC:
                continue
            rec["last_sample"] = now
            price, src, age = _entry_price(STATE.tickers.get(rec["ticker"], {}), now)
            due.append({
                "ts":     round(now, 1),
                "time":   datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "ticker": rec["ticker"],
                "signal": rec["kind"],
                "signal_at": round(rec["at"], 1),
                "elapsed_sec": round(now - rec["at"], 1),
                "entry_price": rec["entry_price"],
                "entry_price_src": rec["entry_price_src"],
                "price": price,
                "price_src": src,
                "price_age_sec": age,
            })
    if not due:
        return 0
    try:
        import ai_paths
        path = ai_paths.report_file("signal_shadow.jsonl", create_parent=True)
        with open(path, "a", encoding="utf-8") as f:
            for row in due:
                f.write(json.dumps(row) + "\n")
    except Exception as e:
        log.warning("[SIGNAL] shadow sample failed: %s", e)
    return len(due)


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

# Hard cap on the momentum watchlist. Age alone did not bound it: the feeds
# re-add faster than the 15-minute purge retires, so the panel drifted to 26+.
# Every entry also costs a quote on a desk already past Alpaca's rate limit,
# and the same list seeds AI Watch's momentum candidates
# (ai_entry_watch._momentum_flagged_from_dashboard) — so this bounds the book's
# intake too, not just the display.
#
# Newest-first is deliberate. Ranking on a "quality" signal invented today
# would be a rule fit to one afternoon; recency matches the existing freshness
# model and is honest about knowing nothing yet. Once shadow.jsonl has enough
# forward-return history per symbol (tools/shadow_report.py) this can retire
# on measured performance instead.
#
# RVOL floor is the one measured quality gate we trust today: desk volume is
# already published on STATE.tickers by _vol_loop (same time-adjusted figure as
# the morning funnel). Candidates must show ≥ this multiple of session average
# once rvol is known; below it they are refused at add and purged on refresh.
# Unknown rvol is allowed provisionally (Discord firehose often lands a name
# before the first 60s volume sample). Held positions and src=book rows skip
# the floor — they need tape for exit/entry logic, not a heat filter.
# 0 disables the gate.
_cfg_at_import = load_config()
try:
    TICKER_MAX_COUNT = max(1, int(_cfg_at_import.get("momentum_max_tickers", 8) or 8))
except (TypeError, ValueError):
    TICKER_MAX_COUNT = 8

try:
    TICKER_MIN_RVOL = float(_cfg_at_import.get("momentum_min_rvol", 2.0) or 0.0)
except (TypeError, ValueError):
    TICKER_MIN_RVOL = 2.0
if TICKER_MIN_RVOL < 0:
    TICKER_MIN_RVOL = 0.0

# Absolute ceiling on the list, candidates and desk-covered names together.
# This is a real external limit, not a taste knob: Finnhub's free tier allows
# ~50 concurrent WebSocket subscriptions across the whole desk, and
# finnhub_stream.request_subscribe does not enforce it. Past the ceiling a
# symbol silently receives no trades — no forming bars, no indicator state — so
# overflow is invisible rather than loud. Kept below 50 to leave the engine its
# own headroom.
try:
    _SUB_BUDGET = max(
        TICKER_MAX_COUNT,
        int(_cfg_at_import.get("realtime_symbol_budget", 40) or 40),
    )
except (TypeError, ValueError):
    _SUB_BUDGET = 40

_ticker_cache: dict = {"mtime": -1.0, "tickers": [], "entries": []}
_ticker_err_ts: float = 0.0   # last load_tickers failure log (rate limit)
_ticker_lock  = threading.RLock()   # guards _ticker_cache + TICKER_LOG reads/writes


# Symbols the desk is committed to. This list is what the engine computes
# indicators FOR, so being retired from it means a name goes dark — no CM RSI,
# no %R, no sell_signal. Fine for a candidate nobody acted on; not fine for a
# position, which is exactly when the desk needs to know whether to hold or
# get out. The purge and the cap are both keyed on recency, so a name bought at
# 14:20 was evicted by 14:35 while still held, and the sell_signal defence in
# ai_positions could never fire for it.
#
# Operator rule: if we are in a position, that symbol's data takes precedence.
_HELD_TTL_SEC = 5.0
_held_cache: tuple[float, frozenset] = (0.0, frozenset())

# Watch-book statuses that mean "the book is done with this name". Everything
# else in ai_entry_watch's vocabulary — watching, armed, submitted, filled — is
# a row the book still polls, and therefore a row that needs live data.
# Expressed as the DONE set rather than the live set so a status added later
# defaults to keeping its data on, which is the safe direction: the failure
# mode of guessing wrong here is a silent fallback to REST quotes.
_WATCH_DONE_STATUSES = frozenset({"invalidated", "expired"})


def _committed_symbols() -> frozenset:
    """Symbols the desk needs live data for — never evict these.

    Two populations, one rule. A name with an open position or a live order
    obviously has to stay. So does a name the AI Watch book is *watching*: the
    book decides "is price in the entry zone" from the real-time tape this list
    feeds (ai_entry_watch.stream_quote reads it off /api/state), and when the
    tape has nothing it falls back to a REST ask+bid per symbol per 20s poll.
    Evicting a watched name therefore does not save quote cost, it MOVES it to
    a more expensive path — the opposite of what the cap is for.

    That inversion was live on 2026-08-10: the book pushed 15 names into a
    10-slot list, so the list evicted the oldest, the book re-pushed it, and the
    cycle repeated — 305 evictions in ~40 minutes, hitting every book symbol
    ~18 times each. No name kept coverage long enough for the tape prefilter to
    fire, so all of them fell through to REST and drew 882 "too many requests"
    errors in one session.

    Read off the desk's own state files rather than the broker: this is
    consulted from load_tickers(), which runs at the price loop's poll rate,
    and a network round trip there would be a rate-limit problem of its own.
    Cached briefly for the same reason. Fails open (empty) — a missing file
    must not take the watchlist down.
    """
    global _held_cache
    now = time.time()
    ts, val = _held_cache
    if now - ts < _HELD_TTL_SEC:
        return val
    out: set[str] = set()
    try:
        import ai_paths
        report_dir = ai_paths.resolve_report_dir()
        for name in ("positions_state.json", "entry_watch_state.json"):
            path = report_dir / name
            if not path.exists():
                continue
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                continue
            for sym, rec in raw.items():
                t = str(sym or "").strip().upper()
                if not (2 <= len(t) <= 5 and t.isalpha()):
                    continue
                # Every live row of the watch book counts, not just the
                # committed ones. "watching" used to be treated as ordinary
                # churn, on the reasoning that a candidate nobody acted on is
                # cheap to drop — true for display, false for quotes, because
                # dropping it is what forces the REST fallback described above.
                # Rows the book has finished with (closed//expired/cancelled)
                # are not on it any more and get no protection.
                if name == "entry_watch_state.json":
                    status = str((rec or {}).get("status") or "").lower()
                    if status in _WATCH_DONE_STATUSES:
                        continue
                out.add(t)
    except Exception:
        pass
    val = frozenset(out)
    _held_cache = (now, val)
    return val


# Parallel rvol index for the watchlist floor. Written by _vol_loop alongside
# STATE.tickers; read by load_tickers under _ticker_lock. A separate lock avoids
# the deadlock STATE.lock ↔ _ticker_lock ( _snapshot holds STATE then calls
# _ticker_src → _ticker_lock; load_tickers used to take STATE for rvol while
# holding _ticker_lock — /api/state hung and Discord heartbeats timed out).
_rvol_cache: dict[str, float] = {}
_rvol_cache_lock = threading.Lock()


def _rvol_cache_update(vol_data: dict, stale: list[str] | None = None) -> None:
    """Publish known rvol into the lock-safe cache; drop *stale* symbols."""
    with _rvol_cache_lock:
        for sym, vd in (vol_data or {}).items():
            t = str(sym or "").upper()
            if not t:
                continue
            v = (vd or {}).get("rvol")
            if v is None:
                _rvol_cache.pop(t, None)
                continue
            try:
                _rvol_cache[t] = float(v)
            except (TypeError, ValueError):
                _rvol_cache.pop(t, None)
        for sym in stale or []:
            _rvol_cache.pop(str(sym or "").upper(), None)


def _known_rvol(ticker: str) -> float | None:
    """Live time-adjusted rvol from the vol-loop cache, or None if unknown."""
    t = str(ticker or "").strip().upper()
    if not t:
        return None
    with _rvol_cache_lock:
        return _rvol_cache.get(t)


def _rvol_floor_exempt(ticker: str, src: str, held: frozenset | set) -> bool:
    """Held / book rows skip the RVOL floor (they need tape, not a heat filter)."""
    if ticker in held:
        return True
    return str(src or "").strip().lower() == "book"


def _passes_rvol_floor(ticker: str, src: str = "",
                       held: frozenset | set | None = None) -> bool:
    """True if this name may stay / enter as a momentum candidate.

    Floor disabled (TICKER_MIN_RVOL <= 0), exempt rows, or unknown rvol → pass.
    Known rvol below the floor → fail. Safe under ``_ticker_lock`` (uses the
    rvol cache, never STATE.lock).
    """
    if TICKER_MIN_RVOL <= 0:
        return True
    if held is None:
        held = _committed_symbols()
    if _rvol_floor_exempt(ticker, src, held):
        return True
    rv = _known_rvol(ticker)
    if rv is None:
        return True
    return rv >= TICKER_MIN_RVOL


def load_tickers() -> list:
    """Read watchlist, purge by age + RVOL floor, cap, return ticker strings.

    Writes the file back whenever entries are purged or the format is migrated,
    so the on-disk file is always up-to-date. Caches by mtime for I/O, but
    always re-applies time- and volume-dependent filters on the cached rows so
    age/RVOL can retire names even when nothing else has rewritten the file.
    """
    with _ticker_lock:
        if not TICKER_LOG.exists():
            _ticker_cache.update(mtime=-1.0, tickers=[], entries=[])
            return []
        try:
            import json as _json
            mtime = TICKER_LOG.stat().st_mtime
            if mtime == _ticker_cache["mtime"] and _ticker_cache["entries"] is not None:
                raw = list(_ticker_cache["entries"])
                from_cache = True
            else:
                raw = _json.loads(TICKER_LOG.read_text(encoding="utf-8"))
                from_cache = False
            if not isinstance(raw, list):
                return []

            now_ts  = time.time()
            now_iso = datetime.now(ET).isoformat(timespec="seconds")
            cutoff  = now_ts - TICKER_MAX_AGE
            held    = _committed_symbols()

            kept    = []
            changed = False   # True if any entry was purged or migrated from old format

            for item in raw:
                src_tag = ""
                if isinstance(item, str):
                    t, added = item.strip().upper(), now_iso
                    changed = True          # migrate plain string → object
                elif isinstance(item, dict):
                    t     = str(item.get("ticker", "")).strip().upper()
                    added = item.get("added", now_iso)
                    src_tag = str(item.get("src") or "")
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

                # A held name is exempt from BOTH the age purge and the cap
                # below. Recency is a fine proxy for "still interesting" while
                # a symbol is only a candidate; once the desk owns it, the
                # question changes from "is this worth watching" to "when do I
                # get out", and that question cannot be answered without live
                # indicators. Evicting a position is how a name goes dark.
                if not (added_ts >= cutoff or t in held):
                    changed = True
                    log.debug(f"[TICKER] Purged stale {t}")
                    continue

                if not _passes_rvol_floor(t, src_tag, held):
                    changed = True
                    rv = _known_rvol(t)
                    log.info(
                        "[TICKER] Purged low-rvol %s (rvol=%s < %.2f)",
                        t, rv, TICKER_MIN_RVOL)
                    continue

                row = {"ticker": t, "added": added, "_ts": added_ts,
                       "_held": t in held}
                if src_tag:
                    row["src"] = src_tag
                kept.append(row)

            # Cap after purging, newest first. Sorted on the parsed timestamp
            # rather than the ISO string so a mixed-offset file cannot order
            # entries by text and silently evict the wrong ones. Exempt names
            # sort ahead of everything and are never in the dropped tail — the
            # cap exists to bound quote cost, and a position has to be quoted
            # regardless.
            #
            # The cap counts CANDIDATE slots, not total rows. It used to be
            # max(cap, n_exempt), which silently reduced the candidate budget
            # as the desk got busier: 11 watched names against a cap of 10 left
            # room for zero momentum candidates, so the panel the book feeds
            # from would go empty exactly when the desk was most active. Exempt
            # names are additive — they are already paid for by not being
            # REST-quoted — and the total is bounded below.
            kept.sort(key=lambda e: (e["_held"], e["_ts"]), reverse=True)
            n_held = sum(1 for e in kept if e["_held"])
            limit = min(n_held + TICKER_MAX_COUNT, _SUB_BUDGET)
            limit = max(limit, n_held)   # never drop an exempt name
            if len(kept) > limit:
                dropped = kept[limit:]
                kept = kept[:limit]
                changed = True
                if dropped:
                    log.info(
                        "[TICKER] Over cap (%d candidate slots + %d desk-covered,"
                        " budget %d) — retired %d oldest: %s",
                        TICKER_MAX_COUNT, n_held, _SUB_BUDGET, len(dropped),
                        ", ".join(e["ticker"] for e in dropped))
            # Re-admit a held name that is not on the list at all. Exempting
            # them from eviction is only half the rule: a position opened after
            # its symbol had already aged out would otherwise stay dark for as
            # long as it was held, which is the case that matters most.
            present = {e["ticker"] for e in kept}
            for t in sorted(held - present):
                kept.append({"ticker": t, "added": now_iso, "_ts": now_ts,
                             "_held": True, "src": "book"})
                changed = True
                log.info("[TICKER] Re-admitted held position %s", t)

            for e in kept:
                e.pop("_held", None)
                e.pop("_ts", None)

            if changed:
                _atomic_write_json(TICKER_LOG, kept)
                mtime = TICKER_LOG.stat().st_mtime
            elif from_cache:
                mtime = _ticker_cache["mtime"]

            tickers = [e["ticker"] for e in kept]
            _ticker_cache.update(mtime=mtime, tickers=tickers, entries=kept)
            return tickers
        except Exception:
            # Empty here means "the whole desk has no symbols": no momentum
            # panel, no quotes, no indicators. A bare `return []` made a code
            # bug in this function indistinguishable from a genuinely empty
            # watchlist, and a NameError went unnoticed until the panel was
            # observed empty by eye. Loud, and rate-limited so a persistent
            # failure cannot flood the log at the 10Hz poll rate.
            global _ticker_err_ts
            now_mono = time.monotonic()
            if now_mono - _ticker_err_ts > 30.0:
                _ticker_err_ts = now_mono
                log.exception("[TICKER] load_tickers failed — watchlist reads as EMPTY")
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


def add_ticker_to_log(ticker: str, src: str = "") -> tuple[bool, bool]:
    """Add a single ticker. Returns (ok, is_new) — is_new is False when already present.

    ``src`` records WHY the symbol is here. "book" means the AI Watch book
    pushed it purely so it carries live tape; those rows are real subscriptions
    but not momentum candidates, and the Momentum panel filters them out. Any
    other value (or none) is an ordinary candidate from Discord/scanner/manual.

    Momentum candidates are refused when live rvol is known and below
    TICKER_MIN_RVOL. Unknown rvol is allowed (provisional); the next volume
    sample will purge if still cold. Book rows skip the floor.
    """
    ticker = ticker.upper()
    src_tag = str(src)[:16] if src else ""
    # RVOL gate before any ticker lock (and load_tickers snapshots rvol itself).
    if not _passes_rvol_floor(ticker, src_tag):
        log.info(
            "[TICKER] Refused %s — rvol=%s < %.2f",
            ticker, _known_rvol(ticker), TICKER_MIN_RVOL)
        return True, False
    with _ticker_lock:
        try:
            load_tickers()   # refresh cache + purge stale
            if ticker in _ticker_cache["tickers"]:
                return True, False
            now_iso = datetime.now(ET).isoformat(timespec="seconds")
            entry = {"ticker": ticker, "added": now_iso}
            if src_tag:
                entry["src"] = src_tag
            entries = list(_ticker_cache["entries"]) + [entry]
            _atomic_write_json(TICKER_LOG, entries)
            _ticker_cache["mtime"] = -1.0   # force re-read on next load
            return True, True
        except Exception as e:
            log.error(f"[TICKER] Add {ticker} failed: {e}")
            return False, False


def _ticker_src(ticker: str) -> str:
    """The recorded ``src`` for a watchlist entry ('' when plain candidate).

    The stamp is authoritative and there is deliberately NO fallback to "is it
    on the book". The book SEEDS from the momentum panel, so nearly every
    momentum name gets adopted onto the book within a poll or two; inferring
    "book" from book membership therefore hides the very candidates the panel
    exists to show, and the panel empties itself as the book fills. Only rows
    the book itself introduced carry the tag — set by push_candidates_to_engine
    and by the re-admit path in load_tickers — and an unstamped entry is a
    candidate that some other source put there, whatever the book later does
    with it.
    """
    t = str(ticker or "").upper()
    with _ticker_lock:
        for e in _ticker_cache.get("entries") or []:
            if str(e.get("ticker") or "").upper() == t:
                return str(e.get("src") or "")
    return ""


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

def ingest_discord_alerts(alerts: list[dict], sentiment=None, drops=None,
                          bb_live=None) -> int:
    """Record captured Discord alerts: add each ticker to the watchlist, count a
    mention (for burst detection), and append to the live feed. Returns the
    number of alerts accepted. Always stamps discord_last_ts (so an empty list is
    a valid heartbeat). Each alert: {"ticker": str, "line": str, "burst": bool}.

    sentiment is a list of SentimentEvent dicts (ticker/score/source/raw/ts);
    near-zero scores are dropped, the rest stored per-ticker for the rolling mean.

    bb_live is a list of BBLiveCall dicts (ticker/text/ts) — "Bullish Bob LIVE"
    call-outs for the header's "Suggests:" chip. Recorded for display only: they
    do not touch the watchlist, mentions, or sentiment.

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
            # Seed the scanner's own price. Kept in its own field, never merged
            # into "price": it is a snapshot from when the alert fired, not a
            # quote, and the two disagree (a live $MB card read $5.33 against a
            # 3.75 print). For an OTC symbol it is the only number we will ever
            # have — Alpaca and Finnhub both return empty for those — so a row
            # that would otherwise be permanently blank shows something, aged.
            try:
                seed = float(a.get("price") or 0)
            except (TypeError, ValueError):
                seed = 0.0
            if seed > 0:
                entry = STATE.tickers.setdefault(ticker, {})
                entry["scanner_price"]    = round(seed, 4)
                entry["scanner_price_ts"] = time.time()
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
            note_signal(ticker, "price_spike", spike_rec["unix"],
                        {"price": spike_rec.get("price")})
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

    for c in (bb_live or []):
        if not isinstance(c, dict):
            continue
        ticker = str(c.get("ticker", "")).strip().upper()
        # Re-validate the shape here: the header chip is the one place a bad
        # symbol would be most visible, and the producer is a separate process.
        if not ticker.isalpha() or not (2 <= len(ticker) <= 5):
            continue
        try:
            ts = float(c.get("ts") or 0) or time.time()
        except (TypeError, ValueError):
            ts = time.time()
        # Discord's own stamp for the message, when OCR caught it. Preferred
        # over capture time for both display and freshness: on a fresh start the
        # source sees an hour of call-outs at once and would otherwise stamp
        # them all with the same minute and call them all current.
        said      = str(c.get("said", "")).strip()[:10]
        said_unix = _bb_said_unix(said, ts)
        text      = str(c.get("text", "")).strip()[:120]
        rec = {
            "ts":     datetime.fromtimestamp(ts, ET).strftime("%H:%M:%S"),
            "unix":   ts,
            "said":   said,
            "at":     said_unix or ts,
            "ticker": ticker,
            "text":   text,
        }
        with STATE.lock:
            # Same symbol called again replaces the earlier entry rather than
            # stacking duplicates — the history is a list of calls, not of OCR
            # frames, and the newest one is what the chip should show.
            existing = [r for r in STATE.bb_live if r["ticker"] == ticker]
            # The OCR source re-posts everything visible when it restarts, so an
            # identical call already in memory is a replay, not a new call — the
            # archive must not count it twice.
            replay = any(r.get("said") == said and r.get("text") == text
                         for r in existing)
            for r in existing:
                STATE.bb_live.remove(r)
            STATE.bb_live.append(rec)
            entry = STATE.tickers.get(ticker, {})
        if not replay:
            _archive_bb_live(rec, entry, ts)
            # Measure from when the call was MADE, not when OCR read it.
            note_signal(ticker, "bb_live", said_unix or ts, entry, ts)

    with STATE.lock:
        STATE.discord_last_ts = time.time()
        if isinstance(drops, dict):
            STATE.discord_drops = {
                str(k): int(v) for k, v in drops.items()
                if isinstance(v, (int, float))
            }
    return accepted


_BB_SAID_RE = re.compile(r"^(\d{1,2}):(\d{2})\s*([AP])M$", re.IGNORECASE)


def _bb_said_unix(said: str, seen_ts: float) -> float | None:
    """Discord's "8:21 AM" resolved against the ET day we read it, or None.

    The stamp carries no date, so it is anchored to seen_ts's ET date. A result
    that lands in the future means the message is from an earlier day (or OCR
    misread the digits) — safer to fall back to capture time than to invent a
    call-out that hasn't happened yet.
    """
    m = _BB_SAID_RE.match(said.strip())
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if not (1 <= hh <= 12 and mm <= 59):
        return None
    hh = hh % 12 + (12 if m.group(3).upper() == "P" else 0)
    seen = datetime.fromtimestamp(seen_ts, ET)
    at   = seen.replace(hour=hh, minute=mm, second=0, microsecond=0).timestamp()
    return None if at > seen_ts + 120 else at


def _bb_live_payload(calls: list[dict], now_ts: float) -> tuple[dict | None, list[dict]]:
    """(current, history) from STATE.bb_live's oldest-first records.

    current: the newest call, but only while it is still fresh — an old symbol
    must not sit under the product badge looking like a live idea. Freshness is
    measured from when the call was *said* where Discord told us, not from when
    OCR happened to read it off the screen.
    history:  every call this session, newest first, regardless of age.

    Takes the records as an argument rather than reading STATE, so the snapshot
    builder (which already holds STATE.lock) can reuse it without deadlocking.
    """
    def _at(r: dict) -> float:
        return float(r.get("at") or r.get("unix") or 0)

    # Order by when it was said, not by when we read it — priming reads a whole
    # screen at once, so arrival order alone would jumble an hour of calls.
    # sorted() is stable, so same-minute calls keep newest-arrival-first.
    history = sorted(reversed(calls), key=_at, reverse=True)
    current = history[0] if history else None
    if current and now_ts - _at(current) > _BB_LIVE_FRESH_SEC:
        current = None
    return current, history


def bb_live_snapshot(now_ts: float | None = None) -> dict:
    """Trader Bro's call-out suggestions for the header: {current, history}."""
    now_ts = now_ts if now_ts is not None else time.time()
    with STATE.lock:
        calls = list(STATE.bb_live)
    current, history = _bb_live_payload(calls, now_ts)
    return {"current": current, "history": history}


def discord_status() -> dict:
    """Snapshot of the OCR source for the UI: alive flag + recent alert feed."""
    with STATE.lock:
        last  = STATE.discord_last_ts
        feed  = list(STATE.discord_alerts)
        drops = dict(STATE.discord_drops)
    return {
        "running":   bool(last) and (time.time() - last) <= _DISCORD_STALE_SEC,
        "last_ts":   last,
        "alerts":    feed,
        "drops":     drops,
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
# ticker -> (price, observed_unix_ts). The timestamp is the TRADE's own time,
# not when we fetched it, so downstream can tell a live print from a stale one.
_alpaca_price_cache: dict = {}
_alpaca_cache_lock = threading.Lock()
_alpaca_fallback_running = False

# A price older than this is not trusted as current: the Alpaca fallback is
# polled for the symbol even if Finnhub has something, and the fresher of the
# two wins the merge.
_PRICE_STALE_SEC = 20.0


def stream_covered(fh_prices: dict, now: float,
                   stale_sec: float = _PRICE_STALE_SEC) -> set:
    """Symbols whose streamed price is young enough to still be current.

    Only these are excluded from the Alpaca fallback poll. Previously *any*
    Finnhub price counted, so a symbol that printed once and went quiet kept
    the 5s fallback switched off for the rest of the session — and pre-market,
    where the stream is often idle, that was the normal case.
    """
    covered = set()
    for t, d in (fh_prices or {}).items():
        try:
            if not d.get("price"):
                continue
            obs = float(d.get("ts_unix") or 0)
        except (TypeError, ValueError):
            continue
        # No timestamp means unknown age, which is not the same as current.
        if obs > 0 and (now - obs) <= stale_sec:
            covered.add(t)
    return covered


def freshest_prices(*sources: dict) -> dict:
    """Merge {ticker: (price, observed_ts[, trade_ts])} maps, newest observation
    winning. Returns {ticker: (price, observed_ts, trade_ts|None)}.

    Preferring a source over an observation time is what let a 90s-old stream
    tick beat a 5s-old Alpaca print. Ties keep the earlier source so repeated
    merges of unchanged input cannot flap between polls.

    The merge runs on OBSERVED time — the most recently learned price is the
    one to show — while trade_ts rides along so the age we publish can be the
    print's age rather than the fetch's. A source that cannot supply one passes
    None, which means "unknown", not "now".
    """
    merged: dict = {}
    for src in sources:
        for t, val in (src or {}).items():
            try:
                p, obs = float(val[0]), float(val[1])
            except (TypeError, ValueError, IndexError):
                continue
            try:
                trade_ts = float(val[2]) if len(val) > 2 and val[2] else None
            except (TypeError, ValueError):
                trade_ts = None
            cur = merged.get(t)
            if cur is None or obs > cur[1]:
                merged[t] = (p, obs, trade_ts)
    return merged


def _alpaca_fallback_worker(client, tickers: list, cfg: dict):
    """Fetch Alpaca latest-trade prices in a background thread and cache the result."""
    global _alpaca_fallback_running
    try:
        quotes = _api.get_latest_trade_quotes(client, tickers, cfg)
        now = time.time()
        with _alpaca_cache_lock:
            for t, (p, ts) in quotes.items():
                # Observed-time falls back to fetch time so the merge still
                # ranks this as a fresh observation. The trade's own time is
                # kept separately and stays None when Alpaca did not supply
                # one — the published age must not claim a print happened at
                # the moment we happened to ask.
                trade_ts = ts if ts and 0 < ts <= now + 5 else None
                _alpaca_price_cache[t] = (p, trade_ts or now, trade_ts)
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
    _prev_book: set     = set()
    while True:
        try:
            tickers = load_tickers()
            current = set(tickers)
            # AI Watch book symbols need the same trade stream even when they
            # are not on the momentum watchlist (cap / age purge). Subscribe
            # them and price them into STATE.tickers so the 4Hz snapshot can
            # overlay live prints onto entry_book.
            try:
                book_syms = set(_ai_book_symbols())
            except Exception:
                book_syms = set()
            quote_universe = current | book_syms

            # Subscribe new tickers to Finnhub as they appear; periodic scan picks them up.
            new = (current | book_syms) - (_prev_tickers | _prev_book)
            if new and FINNHUB_STATE.connected:
                _fh_subscribe(list(new))
            _prev_tickers = current
            _prev_book = book_syms

            client = STATE.data_client
            ts     = datetime.now(ET).strftime("%H:%M:%S")
            now    = time.time()

            if quote_universe and client:
                # Primary: Finnhub real-time stream prices (zero extra HTTP cost).
                # Only prices young enough to still be current count as
                # "covered" — a symbol that printed once and went quiet must not
                # keep the Alpaca fallback switched off for the rest of the
                # session. Pre-market the stream is often idle, which is exactly
                # when this mattered most.
                finnhub_prices: dict = {}
                if FINNHUB_STATE.connected:
                    with FINNHUB_STATE.lock:
                        fh_snap = {t: FINNHUB_STATE.prices.get(t)
                                   for t in quote_universe
                                   if FINNHUB_STATE.prices.get(t)}
                    for t in stream_covered(fh_snap, now):
                        finnhub_prices[t] = float(fh_snap[t]["price"])

                # Finnhub REST quote poll — supplements WebSocket during extended hours
                # when no trades have streamed yet. 30s cadence, free-tier safe.
                fh_key = STATE.cfg.get("finnhub_key", "")
                if fh_key and quote_universe and not _finnhub_rest_running and (now - last_fh_rest_poll > _FINNHUB_REST_INTERVAL):
                    last_fh_rest_poll     = now
                    _finnhub_rest_running = True
                    threading.Thread(
                        target=_finnhub_rest_poll_worker,
                        args=(fh_key, list(quote_universe)),
                        daemon=True, name="fh-rest-poll",
                    ).start()

                # Fallback: Alpaca REST for tickers not covered by Finnhub.
                # Runs in a background thread — never blocks this loop.
                # Polls every 5s when Finnhub has gaps; every 2s when Finnhub is down.
                alpaca_tickers = [t for t in quote_universe if t not in finnhub_prices]
                poll_interval  = 2.0 if not FINNHUB_STATE.connected else 5.0
                if alpaca_tickers and not _alpaca_fallback_running and (now - last_alpaca_poll > poll_interval):
                    last_alpaca_poll       = now
                    _alpaca_fallback_running = True
                    threading.Thread(
                        target=_alpaca_fallback_worker,
                        args=(client, alpaca_tickers, STATE.cfg),
                        daemon=True, name="alpaca-fallback",
                    ).start()

                # Merge on FRESHNESS, not on source. Preferring Finnhub
                # unconditionally meant a stream tick from 90s ago beat an
                # Alpaca print from 5s ago — and pre-market, where the stream
                # idles, that was the normal case.
                with _alpaca_cache_lock:
                    cached_alpaca = dict(_alpaca_price_cache)
                with FINNHUB_STATE.lock:
                    fh_all = {t: (float(d["price"]),
                                  float(d.get("ts_unix") or 0),
                                  d.get("trade_ts"))
                              for t, d in FINNHUB_STATE.prices.items()
                              if d.get("price")}

                merged = freshest_prices(cached_alpaca, fh_all)

                with STATE.lock:
                    for t, (p, obs, trade_ts) in merged.items():
                        if t not in quote_universe:
                            continue
                        entry = STATE.tickers.setdefault(t, {})
                        entry["price"]    = round(p, 4)
                        # price_ts is a WRITE time and always reads fresh; it is
                        # kept only because the web UI shows it. price_age_sec is
                        # the real answer to "how old is this number" — seconds
                        # since the print ITSELF, so it is computed from the
                        # trade's own timestamp and is None when the winning
                        # source could not supply one. A Finnhub REST quote
                        # cannot: stamping it with fetch time made a 30s
                        # re-fetch of an unchanged price read as 3 seconds old.
                        entry["price_ts"] = ts
                        entry["price_age_sec"] = (
                            round(max(0.0, now - trade_ts), 1)
                            if trade_ts and trade_ts > 0 else None)

            # Counterfactual track for Discord-side signals, off the prices
            # just merged above — no extra API call. Self-throttling per
            # symbol, so calling it at 10Hz is fine.
            _sample_signal_shadow(now)

            _fail_streak = 0
        except Exception as e:
            _fail_streak += 1
            if _fail_streak in (1, 5, 25) or _fail_streak % 50 == 0:
                log.warning("[PRICE] loop error (%d consecutive): %s", _fail_streak, e)
            else:
                log.debug("[PRICE] %s", e)
        time.sleep(0.1)  # 10Hz — Finnhub ticks are picked up within 100ms


# ── Day-volume polling ────────────────────────────────────────────────────────

_VOL_AVG_CACHE: dict = {}          # sym -> avg daily volume, or None
_VOL_AVG_DATE: str = ""            # ET date the averages were computed for


def _vol_avg_volumes(mf, client, tickers: list, cfg: dict, now_et) -> dict:
    """Average session volume per symbol, from completed sessions only.

    Summed from MINUTE bars, not daily bars, because the numerator this divides
    into is a minute-bar sum. An IEX daily bar carries odd-lot and off-exchange
    prints that appear in no minute bar — measured at ~30% of the bar for a
    13K-share/day name and ~0% for AAPL — so a daily-bar average understated
    rvol by that much on exactly the thin names the watchlist is full of. See
    `morning_funnel.avg_session_volume`.

    Cached for the ET day; changes once every 24h. Symbols with no usable
    history cache a None. The watchlist is almost entirely different names each
    day and turns over intraday, so a fresh listing with fewer than five
    completed sessions is routine here, not an edge case — without the negative
    entry it would be re-requested every 60s and never resolve.
    """
    global _VOL_AVG_DATE
    # Cache key and session cutoff both come off `now_et`, so they cannot drift
    # apart and average a session the cache thinks is still open.
    today_et = now_et.date()
    stamp = today_et.isoformat()
    if _VOL_AVG_DATE != stamp:
        _VOL_AVG_CACHE.clear()
        _VOL_AVG_DATE = stamp

    wanted = [t for t in tickers if t not in _VOL_AVG_CACHE]
    if not wanted:
        return _VOL_AVG_CACHE

    # Same knob the funnel uses, so funnel.rvol and row.rvol agree — T2.2
    # prefers the funnel value and falls back to this one.
    avg_days = int(mf.knobs_from_cfg(cfg)["avg_days"])
    hist = mf.fetch_minutes_history(client, wanted, cfg, now_et)
    if hist is None:
        # Same trap as tools/morning_funnel.avg_session_volumes: a fetch that
        # could not be made returns None, and this cache is keyed by the ET
        # day, so caching a negative here turns one 429 into a session with no
        # rvol at all. An empty-but-successful {} still falls through and
        # caches negatives, which is what a fresh listing needs.
        return _VOL_AVG_CACHE
    for sym in wanted:
        try:
            _VOL_AVG_CACHE[sym] = mf.avg_session_volume(
                hist.get(sym), today_et, avg_days)
        except Exception:                                  # noqa: BLE001
            _VOL_AVG_CACHE[sym] = None
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

            avg_by_sym = _vol_avg_volumes(mf, client, tickers, cfg, now_et)
            minutes = mf.fetch_minutes_today(client, tickers, cfg, now_et) or {}

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
            # Lock-safe index for the watchlist RVOL floor (see _rvol_cache).
            _rvol_cache_update(vol_data, stale)
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
    rs       = load_rs()      # file read — daily, served whole (header + rows)
    claude_suggestions = load_claude_suggestions()
    grok_suggestions   = load_grok_suggestions()
    ai_suggestions     = build_ai_suggestions(claude_suggestions, grok_suggestions)
    claude_positions   = load_ai_positions()
    # Live Finnhub/Alpaca prints on AI Watch rows (up to 4Hz WS), independent of
    # the ~20s REST poller that owns arming last_ask on disk.
    try:
        book_syms = _ai_book_symbols(claude_positions)
        if book_syms:
            _fh_subscribe(book_syms)
        claude_positions = overlay_ai_book_live_prices(claude_positions)
    except Exception:
        pass
    trending           = filter_trending_by_max_price(
        load_trending(), _trending_max_price_from_cfg(STATE.cfg))
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
            # Why this symbol holds a data subscription. The Momentum panel
            # hides src="book" rows that have no momentum activity of their own
            # — they are here for the AI Watch book's tape, not as candidates.
            src_tag = _ticker_src(t)
            if src_tag:
                d["src"] = src_tag
            price    = d.get("price")
            day_open = d.get("day_open")
            # Scanner snapshot age, so the UI can grey it out by staleness and
            # never mistake it for a live print.
            snap_ts = d.pop("scanner_price_ts", None)
            if d.get("scanner_price") is not None and snap_ts:
                d["scanner_price_age_sec"] = round(max(0.0, now_ts - snap_ts), 1)
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

        # RS candidates get the same live-confluence overlay: a name that has
        # been beating the market for months AND is being talked about right now
        # is the pairing the screener exists to surface. The rating itself is
        # left alone — it is a completed-session number and must not be restated
        # by anything intraday.
        rs_payload = dict(rs) if rs else {}
        if rs_payload.get("rows"):
            rs_rows = []
            for c in rs_payload["rows"]:
                row = dict(c)
                conf = _confluence_sources(c.get("ticker"))
                if len(conf) >= 2:
                    row["confluence"] = {"sources": conf, "count": len(conf)}
                rs_rows.append(row)
            rs_payload["rows"] = rs_rows

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
        bb_current, bb_history = _bb_live_payload(list(STATE.bb_live), now_ts)

        return {
            "discord": {
                "running": bool(STATE.discord_last_ts)
                           and (now_ts - STATE.discord_last_ts) <= _DISCORD_STALE_SEC,
                "last_ts": STATE.discord_last_ts,
                "alerts":  list(STATE.discord_alerts),
                "count":   len(tickers),
            },
            "price_spikes": list(STATE.price_spikes),
            # Trader Bro's "Suggests:" chip + its history (header, display-only).
            "bb_live": {
                "current": bb_current,
                "history": bb_history,
            },
            "tradingview": {
                "last_ts": tv_last,
                "alerts":  tv_feed,
            },
            "tickers": rows,
            "funnel":  _funnel_snapshot(f_rows, f_ts, now_ts, now_et, STATE.cfg),
            "market_sentiment": market_sent,
            "swing":   swing_rows,
            "rs":      rs_payload,
            # Per-source publishes (audit / metrics) + merged desk list.
            "claude_suggestions": claude_suggestions,
            "grok_suggestions":   grok_suggestions,
            # Preferred for the AI panel: agreement-first merge (A / X / AX).
            "ai_suggestions":     ai_suggestions,
            # Token spend for the ET day (same object as ai_suggestions.token_day).
            "ai_token_metrics":   (ai_suggestions or {}).get("token_day") or {},
            "ai_positions":       claude_positions,
            "claude_positions":   claude_positions,  # legacy alias
            "trending":           trending,
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
                "product_name":    version.PRODUCT_NAME,
                "product_version": version.PRODUCT_VERSION,
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
            # Still track named sessions when a token / ?user=jmb is present
            token, username = _request_identity(request)
            if username:
                _touch_session(username)
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


class _TrafficMiddleware(BaseHTTPMiddleware):
    """Record meaningful hits (page loads, actions, presence) with IP + geo."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        try:
            path = request.url.path
            method = request.method
            meta = client_meta_from_request(request)
            _token, username = _request_identity(request)
            status = getattr(response, "status_code", None)
            # Avoid blocking the response path on disk I/O / debounce locks.
            loop = asyncio.get_running_loop()
            loop.run_in_executor(
                None,
                lambda p=path, m=method, st=status, mt=meta, u=username: record_traffic_hit(
                    path=p,
                    method=m,
                    status=st,
                    ip=mt["ip"],
                    user_agent=mt["user_agent"],
                    cf_country=mt.get("cf_country") or "",
                    cf_ray=mt.get("cf_ray") or "",
                    username=u or "",
                ),
            )
        except Exception:
            log.exception("[traffic] middleware record failed")
        return response


# Middleware is applied outermost-last: CORS wraps Traffic wraps Auth wraps app
app.add_middleware(_AuthMiddleware)
app.add_middleware(_TrafficMiddleware)
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

        meta = client_meta_from_request(request)
        ip = meta["ip"]
        ua = meta["user_agent"]
        cf_country = meta.get("cf_country") or ""

        ok = check_credentials(username, password)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: record_login(username, ip, ua, success=ok, cf_country=cf_country),
        )
        await loop.run_in_executor(None, lambda: send_login_email(username, ok, ip=ip, ua=ua))
        # Explicit traffic row (middleware also records POSTs; this tags username).
        await loop.run_in_executor(
            None,
            lambda: record_traffic_hit(
                path="/auth/login",
                method="POST",
                status=200 if ok else 401,
                ip=ip,
                user_agent=ua,
                cf_country=cf_country,
                username=username if ok else "",
                event="auth",
            ),
        )

        if not ok:
            return JSONResponse({"ok": False, "error": "Invalid credentials"}, status_code=401)
        _touch_session(username.lower())
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


@app.get("/api/rs")
async def api_rs():
    """Relative-strength ratings from rs_ratings.json (written by rs_screener.py).

    Returns the whole payload, header included, plus live run progress. The
    header is not optional context: `population` is what makes a percentile
    interpretable, and `as_of` / `adjustment` / `feed` say which tape produced
    it. A bare list of rows would be a number without its units.
    """
    payload = dict(load_rs())
    try:
        import rs_screener
        payload["progress"] = rs_screener.progress()
    except Exception:                                      # noqa: BLE001
        pass
    return JSONResponse(payload)


@app.get("/api/claude")
async def api_claude():
    """Anthropic research ideas from claude_suggestions.json (ai_trader.py)."""
    return JSONResponse(load_claude_suggestions())


@app.get("/api/grok")
async def api_grok():
    """Grok / xAI research ideas from grok_suggestions.json (when published)."""
    return JSONResponse(load_grok_suggestions())


@app.get("/api/ai/suggestions")
async def api_ai_suggestions():
    """Merged AI list: Anthropic + xAI, agreement first (A / X / AX marks)."""
    return JSONResponse(build_ai_suggestions())


@app.get("/api/ai/token-metrics")
async def api_ai_token_metrics(day: str = "today"):
    """Token / cost rollup from ai_reports/ token_metrics.jsonl.

    Query ``day=today`` (default ET calendar day), ``day=YYYY-MM-DD``, or
    ``day=all``.
    """
    try:
        from ai_suggest import summarize_token_metrics
        return JSONResponse(summarize_token_metrics(day=day or "today"))
    except Exception as e:  # noqa: BLE001
        return JSONResponse(
            {"count": 0, "error": str(e)[:200], "day": day or "today"},
            status_code=500,
        )


@app.get("/api/ai/positions")
async def api_ai_positions():
    """AI desk positions, resting orders and realized performance."""
    return JSONResponse(load_ai_positions())


@app.get("/api/claude/positions")
async def api_claude_positions():
    """Legacy alias for /api/ai/positions."""
    return JSONResponse(load_ai_positions())


@app.get("/api/trending")
async def api_trending():
    """Stocktwits trending from trending_stocks.json (trending_screener.py)."""
    return JSONResponse(filter_trending_by_max_price(
        load_trending(), _trending_max_price_from_cfg(STATE.cfg)))


# One screen at a time, same guard as the swing refresh. An RS run is far
# heavier — a cold cache is a few hundred Alpaca requests.
_rs_refresh_lock = threading.Lock()

@app.post("/api/rs/refresh")
async def api_rs_refresh():
    """Trigger an on-demand RS screen. Runs in a worker thread; poll /api/rs for
    progress, since a cold-cache run takes minutes rather than seconds."""
    if not _rs_refresh_lock.acquire(blocking=False):
        return JSONResponse({"ok": False, "status": "already-running"}, status_code=409)

    def _run():
        try:
            import rs_screener
            rs_screener.run_screen(load_config())
            _rs_cache["mtime"] = -1.0             # force reload on next serve
        except Exception as e:
            # RunRefused lands here too: a refused run is a successful outcome
            # for the previous file, which stays exactly as it was.
            log.warning(f"[RS] refresh failed: {e}")
        finally:
            _rs_refresh_lock.release()

    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, _run)
    return JSONResponse({"ok": True, "status": "started"})


@app.post("/api/rs/check")
async def api_rs_check(request: Request):
    """What did these symbols rate, including ones the filters dropped?

    Reads the ratings table rs_screener writes for the WHOLE ranked population,
    not just the served rows. That is what makes the percentile auditable: if a
    name is missing from the list you can still see the rating it earned and the
    population it earned it against.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    seen: dict[str, None] = {}
    for tok in re.split(r"[,\s]+", str(body.get("symbols", "")).upper()):
        if tok:
            seen.setdefault(tok, None)
    symbols = list(seen)[:25]
    if not symbols:
        return JSONResponse({"results": []})

    def _run():
        import rs_cache
        import rs_screener
        cfg = load_config()
        served = {r["ticker"]: r for r in load_rs().get("rows", [])}
        try:
            cache = rs_cache.BarCache(rs_screener._cache_path(cfg),
                                      adjustment=str(cfg.get("rs_bar_adjustment", "split")))
        except Exception as e:                             # noqa: BLE001
            return [{"ticker": s, "error": str(e)} for s in symbols]
        try:
            out = []
            for symbol in symbols:
                history = cache.rating_history(symbol, limit=10)
                out.append({
                    "ticker": symbol,
                    "served": symbol in served,
                    "row": served.get(symbol),
                    "history": history,
                    "rated": bool(history),
                })
            return out
        finally:
            cache.close()

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
    plus an implicit heartbeat. Body: {"alerts": [{"ticker","line"}, ...],
    "bb_live": [{"ticker","text","ts"}, ...]}. Each alert feeds the mention
    system (watchlist add + burst) and the live feed; bb_live carries the
    "Bullish Bob LIVE" call-outs, which are display-only. An empty body is a
    valid heartbeat that keeps the source 'alive'."""
    try:
        body   = await request.json()
        alerts = body.get("alerts", [])
        if not isinstance(alerts, list):
            return JSONResponse({"ok": False, "error": "alerts must be a list"}, status_code=400)
        sentiment = body.get("sentiment", [])
        if not isinstance(sentiment, list):
            sentiment = []
        drops = body.get("drops", {})
        if not isinstance(drops, dict):
            drops = {}
        bb_live = body.get("bb_live", [])
        if not isinstance(bb_live, list):
            bb_live = []
    except Exception:
        alerts    = []
        sentiment = []
        drops     = {}
        bb_live   = []
    loop = asyncio.get_running_loop()
    accepted = await loop.run_in_executor(
        None, lambda: ingest_discord_alerts(alerts, sentiment, drops, bb_live))
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
        # Callers that are subscribing for data rather than nominating a
        # candidate say so; the AI Watch book sends src="book".
        src = str(body.get("src") or "")[:16]
        loop  = asyncio.get_running_loop()
        added = []
        for item in raw[:100]:          # safety cap
            t = str(item).strip().upper()
            if not t or not t.isalpha() or not (2 <= len(t) <= 5):
                continue
            ok, is_new = await loop.run_in_executor(
                None, lambda t=t: add_ticker_to_log(t, src=src))
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
async def api_login_log(request: Request):
    _token, username = _request_identity(request)
    if is_auth_required() and not is_admin_user(username):
        return JSONResponse({"ok": False, "error": "Admin access required"}, status_code=403)
    loop    = asyncio.get_running_loop()
    entries = await loop.run_in_executor(None, get_login_log)
    return JSONResponse({"ok": True, "entries": entries})


@app.get("/api/traffic-log")
async def api_traffic_log(request: Request):
    """Recent meaningful hits (page / auth / action / presence). Admin when auth on."""
    _token, username = _request_identity(request)
    if is_auth_required() and not is_admin_user(username):
        return JSONResponse({"ok": False, "error": "Admin access required"}, status_code=403)
    limit = 200
    try:
        limit = min(1000, max(1, int(request.query_params.get("limit", "200"))))
    except ValueError:
        pass
    hours = 24.0
    try:
        hours = max(0.25, float(request.query_params.get("hours", "24")))
    except ValueError:
        pass
    loop = asyncio.get_running_loop()
    summary = await loop.run_in_executor(None, lambda: summarize_traffic(hours))
    entries = await loop.run_in_executor(None, lambda: get_traffic_log(limit))
    return JSONResponse({
        "ok": True,
        "summary": summary,
        "entries": entries,
    })


@app.post("/api/suggestions")
async def api_add_suggestion(request: Request):
    try:
        body    = await request.json()
        message = str(body.get("message", "")).strip()
        if not message:
            return JSONResponse({"ok": False, "error": "Empty message"}, status_code=400)
        if len(message) > 4000:
            message = message[:4000]
        ip = (
            request.headers.get("CF-Connecting-IP")
            or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or (request.client.host if request.client else "unknown")
        )
        ua   = request.headers.get("user-agent", "")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: save_suggestion(message, ip, ua))
        log.info(f"[SUGGESTION] from {ip}: {message[:60]}")
        # Wait for email result so the UI can show whether it left the box.
        # Keep it on a worker thread so SMTP never blocks the event loop.
        email_sent = await loop.run_in_executor(
            None, lambda: send_suggestion_email(message, ip, ua))
        status = smtp_status()
        return JSONResponse({
            "ok": True,
            "email_sent": bool(email_sent),
            "email_to": status.get("notify_to"),
            "email_configured": bool(status.get("configured")),
        })
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
    local_url = f"http://localhost:{PORT}"
    print(f"\n  Signal Scanner  —  {local_url}\n  Ctrl+C to stop\n")
    uvicorn.run("dashboard:app", host="0.0.0.0", port=PORT, log_level="warning")
