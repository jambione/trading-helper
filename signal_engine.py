#!/usr/bin/env python3
"""
signal_engine.py — indicator engine. Measures and publishes; does not trade.

Watches the tickers the dashboard surfaces, fetches bars from Alpaca, and
computes indicators against them every second. Its entire output is
signal_state.json, which dashboard.py merges into /api/state — that is how the
web dashboard, the momentum desk, and the buy circle get their numbers.

It used to place orders too: entry logic sat inline with the measurement, and
exits (stop-loss, take-profit, ATR trail, time stop, RSI-overbought, reversal)
fired from the per-second price check. All of it is gone. Trading is manual —
the desk's B/S hotkeys and the dashboard, which route through alpaca_trader and
trade_bridge directly. Those modules are untouched; this engine simply no
longer calls them.

Keeping the split clean matters more than it looks: the reading and the
decision to act were interleaved, and the reading is what everything
downstream consumes.

HOW IT WORKS
────────────
1. Every DASHBOARD_POLL_INTERVAL seconds, poll /api/state for tickers worth
   watching — mentioned, mention_burst, find_it_first, or (TRACK_DESK_TICKERS)
   anything on the desk list.

2. Fetch up to BAR_COUNT 1-minute bars per ticker from Alpaca, staggered so
   they do not all hit at once, refreshed once per BAR_REFRESH seconds.

3. Every second, inject the live price as the forming bar's close and
   recompute, so published indicators move between bar closes instead of
   freezing for up to a minute.

4. STRATEGY_MODE selects which indicators are published:
     • three_indicator — CM RSI-2, %R Trend Exhaustion, MACD (what the buy
       circle reads)
     • momentum        — classic RSI + MACD histogram
     • alert           — mention velocity as proximity

5. Expiry:
     • EXPIRY_COLD — ticker never showed a positive histogram
     • EXPIRY_WARM — it did at least once, so it gets longer

PRICE DATA PRIORITY
───────────────────
  1. Finnhub WebSocket  (real-time, market hours, ≤50 symbols, free)
  2. Dashboard /api/state price field  (fallback — updated ~1s by server)

EFFICIENCY NOTES
────────────────
  • Dashboard HTTP request only every 5 s (not every second).
  • Finnhub price reads are pure in-memory dict lookups — no I/O.
  • Alpaca bar fetches are rate-limited to BAR_REFRESH seconds per ticker
    AND staggered so they don't all fire in the same second.
  • Active list is capped at MAX_ACTIVE_TICKERS (default 32, aligned with
    book push; Finnhub free-tier WS cap is ~50 — leave headroom).

RUN
───
    python signal_engine.py
    (reads signal_engine.env automatically)

CONFIG (signal_engine.env or env vars)
───────────────────────────────────────
    DASHBOARD_URL / DASHBOARD_USER / DASHBOARD_PASS
    FINNHUB_API_KEY
    ALPACA_API_KEY / ALPACA_SECRET_KEY
    See signal_engine.env for all tunable values.
"""

from __future__ import annotations

__version__ = "1.7"

import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

# ── Import our own signal library ─────────────────────────────────────────────
_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))
import desk_auth
import desk_core
from signals import (
    rsi as calc_rsi, compute_macd,
    compute_cm_rsi_lower, compute_obv_oscillator,
    calc_rvol, vwap_calc, atr as calc_atr,
)

# ── Import Alpaca trader module (optional — activated by TRADER_MODE) ─────────
import version

# ── Live prices: Alpaca IEX poll (default) and optional Finnhub WebSocket ─────
from finnhub_stream import (
    start_finnhub_stream,
    request_subscribe as fh_request_subscribe,
    request_unsubscribe as fh_request_unsubscribe,
    get_latest_price as fh_get_latest_price,
    FINNHUB_STATE,
)
import alpaca_price_poll as alpaca_px
import alpaca_api
from finnhub_stream import register_trade_callback as fh_register_trade_callback

# ── Import Massive API client (optional — activated by MASSIVE_API_KEY) ───────
import massive_client

# ── Import the 3-indicator strategy + realtime bar aggregator (optional) ──────
# Both are inert unless STRATEGY_MODE=three_indicator / REALTIME_BARS=1.
import strategy_three_indicator as three_ind
from realtime_bars import RealtimeBarAggregator

# ── Load signal_engine.env ────────────────────────────────────────────────────
# Keys loaded FROM THE FILE (not the shell) are tracked so a dashboard-requested
# restart can drop them from os.environ before re-exec — otherwise the inherited
# environment would shadow the freshly edited file ("env always wins").
_ENV_FILE_KEYS: list = []

_loaded_env_keys = desk_core.load_desk_env(_HERE / "signal_engine.env")
if _loaded_env_keys:
    _ENV_FILE_KEYS.extend(_loaded_env_keys)
    print(f"[ENV] Loaded {len(_loaded_env_keys)} setting(s) from signal_engine.env")

# Touched by the dashboard's "Restart Engine" button (POST /api/engine/restart).
# The main loop notices the flag, closes positions cleanly is NOT attempted —
# positions survive via the startup adoption/reconcile — and re-execs itself so
# the freshly edited signal_engine.env takes effect.
RESTART_FLAG = _HERE / "engine_restart.flag"
RESTART_FLAG.unlink(missing_ok=True)   # stale flag from a previous run

# ── Configuration ─────────────────────────────────────────────────────────────

DASHBOARD_URL  = os.getenv("DASHBOARD_URL",  "https://trading.jbrasfield.com")
DASHBOARD_USER = os.getenv("DASHBOARD_USER", "")
DASHBOARD_PASS = os.getenv("DASHBOARD_PASS", "")

def _env_int(name: str, default: str) -> int:
    """int() an env var that another component may have set as a float.

    POLL_INTERVAL is a shared name: the monitor writes it as float seconds
    (.env has 2.0), this engine wants an int. A bare int("2.0") raises, and
    at module scope that kills the engine at import — so coerce via float.
    """
    raw = os.getenv(name, default)
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return int(float(default))


POLL_INTERVAL          = _env_int("POLL_INTERVAL",               "1")    # main loop cadence (seconds)
DASHBOARD_POLL_INTERVAL = int(os.getenv("DASHBOARD_POLL_INTERVAL", "5"))  # how often to hit /api/state

BAR_REFRESH    = int(os.getenv("BAR_REFRESH",    "60"))  # seconds between Alpaca bar re-fetches
BAR_STAGGER    = int(os.getenv("BAR_STAGGER",    "5"))   # seconds between each ticker's first fetch
                                                          # prevents all tickers fetching at t=0

# Expiry: tickers with no bar data or a histogram that never went positive
# are dropped after EXPIRY_COLD seconds (3 min).  Once a ticker has shown
# a positive histogram at least once we extend it to EXPIRY_WARM (10 min)
# since it came close and deserves more watch time.
EXPIRY_COLD    = int(os.getenv("EXPIRY_COLD",    "180"))   # 3 min — never warmed up
EXPIRY_WARM    = int(os.getenv("EXPIRY_WARM",    "600"))   # 10 min — showed positive hist

MAX_ACTIVE_TICKERS = int(os.getenv("MAX_ACTIVE_TICKERS", "32"))  # hard cap; align with book push

# 200 was enough for every indicator computed off native bars, but the %R slow
# line is now a 15-minute resample (signals._resampled_percent_r) and 200
# one-minute bars resample to ~13 — short of the 21 it needs, so the long scale
# would silently never compute and the arm gate would refuse everything. 500
# gives ~33 coarse bars with headroom for the gaps IEX leaves in a thin name.
# The rate limit counts REQUESTS, not bytes: this is the same one call per
# symbol, with a bigger payload, and costs nothing against the 200/min budget
# that four processes already share.
BAR_COUNT          = int(os.getenv("BAR_COUNT",         "500"))  # max bars to fetch
BAR_LOOKBACK_DAYS  = int(os.getenv("BAR_LOOKBACK_DAYS",  "5"))   # calendar days back (steady-state)
# Longer lookback on first successful load — thin/illiquid names often have
# <40 1-min IEX bars in 5 calendar days, especially after hours / overnight.
BAR_LOOKBACK_THIN  = int(os.getenv("BAR_LOOKBACK_THIN", "20"))
# Retry interval after a failed first bar fetch (keep last_bar_minute=-1 so we
# don't wait a full minute on empty IEX responses).
BAR_FAIL_RETRY_S   = int(os.getenv("BAR_FAIL_RETRY_S",  "20"))
BAR_TIMEFRAME  = os.getenv("BAR_TIMEFRAME",  "1Min")
# When Finnhub realtime bars are fresh for a symbol, skip routine Alpaca
# bar refresh (still seed / recover via Alpaca). Occasional safety refresh
# keeps cached_df from rotting if the tape goes quiet later.
ALPACA_RT_SKIP_REFRESH = os.getenv("ALPACA_RT_SKIP_REFRESH", "1") in ("1", "true", "yes")
ALPACA_RT_SAFETY_REFRESH_S = float(os.getenv("ALPACA_RT_SAFETY_REFRESH_S", "300"))
ALPACA_BAR_MAX_RETRIES = int(os.getenv("ALPACA_BAR_MAX_RETRIES", "4"))

# When on, every symbol on the dashboard ticker list (momentum desk universe)
# is eligible for engine tracking — not only transcription "mentioned" /
# mention_burst rows. Find It First and scanner names get bars + CM RSI so the
# desk RSI focus column can light up. Capped by MAX_ACTIVE_TICKERS.
def _env_flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


TRACK_DESK_TICKERS = _env_flag("TRACK_DESK_TICKERS", "1")

RSI_PERIOD     = int(os.getenv("RSI_PERIOD",   "14"))
MACD_FAST      = int(os.getenv("MACD_FAST",    "12"))
MACD_SLOW      = int(os.getenv("MACD_SLOW",    "26"))
MACD_SIG       = int(os.getenv("MACD_SIG",     "9"))
RSI_BUY_MAX    = int(os.getenv("RSI_BUY_MAX",  "70"))

# ── Priority mention system ───────────────────────────────────────────────────
# When a ticker accumulates >= PRIORITY_MENTIONS new chat mentions within
# PRIORITY_WINDOW_SECONDS it is marked "hot" and the RSI filter is bypassed —
# MACD momentum alone is enough to trigger a BUY.  The crowd is piling in;
# waiting for RSI to cool down means missing the move.
# Set PRIORITY_MENTIONS=0 to disable the system entirely.
PRIORITY_MENTIONS       = int(os.getenv("PRIORITY_MENTIONS",       "5"))
PRIORITY_WINDOW_SECONDS = int(os.getenv("PRIORITY_WINDOW_SECONDS", "10"))
# How long (seconds) a ticker that went hot but cooled off stays on the list
# before being dropped.  Keeps the list clean when the crowd moves on.
EXPIRY_COOLED           = int(os.getenv("EXPIRY_COOLED",           "60"))

# Exit a position when RSI rises above this level (overbought exit).
# Applies to ALL positions — priority or normal — so we always respect the
# overbought condition on the way out.  Set to 0 to disable.
RSI_SELL_OVERBOUGHT     = int(os.getenv("RSI_SELL_OVERBOUGHT",     "0"))

STOP_LOSS      = float(os.getenv("STOP_LOSS",   "0.0"))   # % e.g. 1.0  (fixed, used when dynamic mode is off)
TAKE_PROFIT    = float(os.getenv("TAKE_PROFIT", "0.0"))   # % e.g. 2.0  (fixed, used when dynamic mode is off)

# ── Dynamic stop / take-profit ─────────────────────────────────────────────────
# DYNAMIC_EXIT=atr   — stop = buy_price - ATR_MULT × ATR
#                       take = buy_price + ATR_MULT × ATR
# DYNAMIC_EXIT=vwap  — stop = VWAP at buy time (sell if price falls below VWAP)
#                       take = buy_price + ATR_MULT × ATR above VWAP entry
# DYNAMIC_EXIT=off   — use fixed STOP_LOSS / TAKE_PROFIT percentages (default)

# Minimum seconds to hold a position before a MACD reversal sell can fire.
# Prevents buying and immediately selling on a one-tick histogram dip.
# Stop-loss and take-profit still fire immediately regardless of this setting.
MIN_HOLD_SECONDS     = int(os.getenv("MIN_HOLD_SECONDS",     "60"))  # normal tickers
HOT_MIN_HOLD_SECONDS = int(os.getenv("HOT_MIN_HOLD_SECONDS", "30"))  # hot tickers — moves are faster
# Require this many consecutive growing histogram bars before a BUY fires.
# 1 = original behaviour (single bar).  2 = two consecutive bars required (recommended).
HIST_CONFIRM_BARS    = int(os.getenv("HIST_CONFIRM_BARS",    "2"))
# Max total dollar exposure across ALL open positions (0 = no cap)
MAX_TOTAL_EXPOSURE   = float(os.getenv("MAX_TOTAL_EXPOSURE", "0"))

# ── Trader mode ───────────────────────────────────────────────────────────────
# Controls whether the signal engine places orders or just logs signals.
#   off   — log signals only, no orders (default, safe)
#   paper — paper trade via Alpaca ($100k fake money, same API keys)
#   live  — real money via Alpaca (test on paper first!)
TRADER_MODE      = os.getenv("TRADER_MODE",   "off").lower().strip()
TRADE_AMOUNT     = float(os.getenv("TRADE_AMOUNT", "500"))   # $ per BUY signal
MAX_PRICE        = float(os.getenv("MAX_PRICE", "0"))         # skip BUY if price > this (0 = no limit)
EXTENDED_HOURS   = os.getenv("EXTENDED_HOURS", "false").lower() in ("true", "1", "yes")  # premarket/afterhours orders

# ── Buy signal filters (all off by default) ───────────────────────────────────
BUY_FILTER_VWAP    = os.getenv("BUY_FILTER_VWAP",    "false").lower() in ("true","1","yes")
BUY_FILTER_OBV     = os.getenv("BUY_FILTER_OBV",     "false").lower() in ("true","1","yes")
BUY_FILTER_CM_RSI  = os.getenv("BUY_FILTER_CM_RSI",  "false").lower() in ("true","1","yes")
BUY_FILTER_RVOL    = os.getenv("BUY_FILTER_RVOL",    "false").lower() in ("true","1","yes")
RVOL_MIN           = float(os.getenv("RVOL_MIN", "3.0"))   # relative volume threshold
# Time stop: force-sell after this many minutes in position (0 = disabled)
MAX_HOLD_MINUTES   = int(os.getenv("MAX_HOLD_MINUTES", "0"))

LOG_FILE          = _HERE / "signal_log.json"
SIGNAL_STATE_FILE = _HERE / "signal_state.json"   # written every SIGNAL_STATE_INTERVAL s

ALPACA_BASE_URL = "https://data.alpaca.markets"

# ── Massive API ───────────────────────────────────────────────────────────────
# When MASSIVE_API_KEY is set, alert tickers (mention_burst / is_hot) will
# try Massive first for OHLCV bars before falling back to Alpaca.
# Free tier provides end-of-day bars only; paid tiers ($29+/mo) provide
# 15-minute delayed or real-time intraday bars.
MASSIVE_API_KEY         = os.getenv("MASSIVE_API_KEY", "")
# How often to write signal_state.json (seconds) — dashboard reads this file
SIGNAL_STATE_INTERVAL   = int(os.getenv("SIGNAL_STATE_INTERVAL", "5"))

# ── Strategy selection ────────────────────────────────────────────────────────
# "momentum"        → the original RSI + MACD-histogram engine (default; unchanged)
# "three_indicator" → the discretionary CM RSI-2 + %R Exhaustion + MACD-cross rule
#                     from strategy_three_indicator.py (validated in backtest_3ind.py).
# Routing always goes through alpaca_trader, so TRADER_MODE (off/paper/live) still
# governs whether real orders are placed — keep it on "paper" while validating.
STRATEGY_MODE  = os.getenv("STRATEGY_MODE", "momentum").lower()

# When 1, build live OHLCV bars from the Finnhub trade stream (realtime_bars.py)
# and evaluate the strategy on a forming candle that updates every tick — closing
# the gap with TradingView. Only consumed by the three_indicator path.
REALTIME_BARS  = os.getenv("REALTIME_BARS", "0") in ("1", "true", "yes")
# Seconds without a trade before the realtime bars are considered stale and the
# engine falls back to freshly-fetched Alpaca bars. 30s: a thin name can pause
# between prints without flapping; two silent minutes used to keep publishing
# a frozen Finnhub candle as "realtime" and the desk armed on it.
RT_BARS_MAX_STALE = float(os.getenv("REALTIME_BARS_MAX_STALE", "30"))

# Minimum seconds to hold a three_indicator position before a strategy reversal
# may close it — guards against per-second buy/sell flip-flop on the forming bar.
THREE_IND_MIN_HOLD = int(os.getenv("THREE_IND_MIN_HOLD", "30"))

# Require a mention burst (is_hot) before a 3-indicator BUY may fire.
# The 2026-06-12 A/B benchmarks (benchmarks/ab_bench_*.csv) showed the
# indicator entry has NO standalone edge on the alert-pool microcaps — every
# config lost money. The catalyst is the candidate edge; the indicators time
# it. Pinned compare tickers are exempt (they exist for TV validation).
THREE_IND_REQUIRE_HOT = os.getenv("THREE_IND_REQUIRE_HOT", "0") in ("1", "true", "yes")

# Strategy parameters. Every key in strategy_three_indicator.DEFAULT_PARAMS can
# be overridden from the environment as THREE_IND_<PARAM> (upper-cased), e.g.
#   THREE_IND_CM_RSI_BUY_MAX=35  THREE_IND_MACD_SEP_MULT=1.2
# so the live engine can be synced to the exact settings on the TradingView chart.
def _three_ind_env_params() -> dict:
    overrides = {"exit_mode": os.getenv("THREE_IND_EXIT_MODE", "any").lower()}
    for key, default in three_ind.DEFAULT_PARAMS.items():
        if key == "exit_mode":
            continue
        raw = os.getenv(f"THREE_IND_{key.upper()}")
        if raw is None:
            continue
        try:
            # bool BEFORE int: bool is an int subclass, so isinstance(True, int)
            # is True and a boolean knob went down the int path — int(float(
            # "false")) raises and the override was dropped with only a log
            # line. The obvious spelling of turning a flag off was the one
            # spelling that silently did nothing.
            if isinstance(default, bool):
                low = raw.strip().lower()
                if low in ("1", "true", "yes", "on"):
                    overrides[key] = True
                elif low in ("0", "false", "no", "off"):
                    overrides[key] = False
                else:
                    raise ValueError(raw)
            elif isinstance(default, int):
                overrides[key] = int(float(raw))
            elif isinstance(default, float):
                overrides[key] = float(raw)
            else:
                overrides[key] = raw
            print(f"[CFG] 3ind param override: {key} = {overrides[key]}")
        except ValueError:
            print(f"[CFG] Ignoring invalid THREE_IND_{key.upper()}={raw!r}")
    return overrides

THREE_IND_PARAMS = three_ind.params(**_three_ind_env_params())

# ── Pinned compare tickers ────────────────────────────────────────────────────
# Comma-separated symbols that are tracked from startup, never expire, and are
# evaluated on the forming bar every second — for side-by-side validation of
# the 3-indicator port against the live TradingView chart.
COMPARE_TICKERS = [s.strip().upper() for s in os.getenv("COMPARE_TICKERS", "").split(",")
                   if s.strip()]

# ── Trade guard (risk layer) ──────────────────────────────────────────────────
# DAILY_LOSS_LIMIT    — $ of realized loss in one ET day that halts new buys (0 = off)
# MAX_TRADES_PER_DAY  — cap on round-trips per ET day (0 = off)
# PDT_PROTECT         — warn | block | off : pattern-day-trader protection for
#                       margin accounts under $25k (3 day-trades per 5 business days)
# RECONCILE_INTERVAL_S — seconds between engine↔Alpaca position reconciliations
DAILY_LOSS_LIMIT     = float(os.getenv("DAILY_LOSS_LIMIT",    "0"))
MAX_TRADES_PER_DAY   = int(os.getenv("MAX_TRADES_PER_DAY",    "0"))
PDT_PROTECT          = os.getenv("PDT_PROTECT", "warn").lower().strip()
RECONCILE_INTERVAL_S = int(os.getenv("RECONCILE_INTERVAL_S",  "60"))

# Account-level risk guard — gates every BUY, records every closed trade.

# ── Alert strategy (STRATEGY_MODE=alert) ──────────────────────────────────────
# The thesis the backtests pointed to: on low-float microcaps, indicator ENTRIES
# have no edge — the catalyst does. So the buy signal IS the mention burst
# (a ticker going `is_hot`), not an oscillator. The money is then made on the
# EXIT, and backtest_exits.py showed the winning recipe across 1031 entries:
#   • trailing stop (let the runner run)  +  hard stop (cut losers fast)
#   • NO fixed time cap and NO take-profit cap — both tested strictly WORSE,
#     because the whole P&L lives in the rare big winner those rules amputate.
# These percentages are the levers; defaults are the backtest's top performer.
ALERT_HARD_STOP   = float(os.getenv("ALERT_HARD_STOP",  "8.0"))   # % below entry → hard stop
ALERT_TRAIL_STOP  = float(os.getenv("ALERT_TRAIL_STOP", "15.0"))  # % below peak  → trailing stop
# When on, attach a broker-held trailing stop after alert buys (RTH; GTC).
# Client-side trail still runs as backup.
BROKER_TRAIL = os.getenv("BROKER_TRAIL", "on").strip().lower() in (
    "1", "true", "on", "yes",
)
ALERT_MIN_HOLD    = int(os.getenv("ALERT_MIN_HOLD",     "30"))    # seconds to hold before a stop can fire
# Optional falling-knife veto: require the live price to be at/above the last
# closed bar (don't buy a ticker that's already dumping). Default off so the pure
# alert→buy thesis is what gets paper-tested first.
ALERT_REQUIRE_GREEN = os.getenv("ALERT_REQUIRE_GREEN", "0") in ("1", "true", "yes")


# ── Credential loading ────────────────────────────────────────────────────────

def _load_alpaca_credentials() -> tuple[str, str]:
    api_key    = os.getenv("ALPACA_API_KEY", "")
    secret_key = os.getenv("ALPACA_SECRET_KEY", "")
    secrets_path = _HERE / "config" / "secrets.json"
    if secrets_path.exists():
        try:
            s = json.loads(secrets_path.read_text())
            api_key    = api_key    or s.get("api_key", "")
            secret_key = secret_key or s.get("secret_key", "")
        except Exception as e:
            print(f"[CFG] Could not read secrets.json: {e}")
    if not api_key or not secret_key:
        print(
            "[CFG] WARNING: Alpaca credentials not found.\n"
            "       Set ALPACA_API_KEY / ALPACA_SECRET_KEY in signal_engine.env\n"
            "       or via the dashboard Settings → API Keys tab.\n"
            "       Bar fetching will be skipped until credentials are set."
        )
    return api_key, secret_key


# Live price source: alpaca (default free IEX poll) | finnhub | both | auto
PRICE_SOURCE = os.getenv("PRICE_SOURCE", "alpaca").strip().lower()


def get_latest_price(ticker: str):
    """Last trade price — Alpaca IEX first (default), then Finnhub if enabled."""
    if PRICE_SOURCE in ("alpaca", "both", "auto"):
        p = alpaca_px.get_latest_price(ticker)
        if p is not None:
            return p
    if PRICE_SOURCE in ("finnhub", "both", "auto"):
        return fh_get_latest_price(ticker)
    return None


def request_subscribe(tickers: list):
    if PRICE_SOURCE in ("alpaca", "both", "auto"):
        alpaca_px.request_subscribe(tickers)
    if PRICE_SOURCE in ("finnhub", "both", "auto"):
        fh_request_subscribe(tickers)


def request_unsubscribe(tickers: list):
    """Drop live-price subscriptions when a ticker leaves ``active``."""
    if PRICE_SOURCE in ("alpaca", "both", "auto"):
        alpaca_px.request_unsubscribe(tickers)
    if PRICE_SOURCE in ("finnhub", "both", "auto"):
        fh_request_unsubscribe(tickers)


# Below this, a timestamp cannot be milliseconds: 1e11 ms is 1973, and 1e11
# seconds is the year 5138. Anything smaller arrived as seconds.
_MS_FLOOR = 1e11


def _trade_ts_ms(ts) -> int:
    """Normalise a trade timestamp to milliseconds.

    The two feeds disagree and RealtimeBarAggregator.on_trade takes ms.
    Finnhub's socket carries the trade's own `t` in milliseconds; the Alpaca
    poller hands its callbacks `time.time()`, in seconds. Feeding seconds in
    is not a rounding error, it breaks the aggregator two ways: _last_ts lands
    ~1000x too small so age_seconds reports decades and the ticker can never
    be fresh, and _epoch_minute buckets the bar near 1970 so a ticker fed by
    both sources flips between two minute numbers and seals a garbage bar on
    every alternation.

    Observed 2026-08-20 on flipping PRICE_SOURCE to "both": realtime coverage
    went DOWN, 4/13 to 1/12, because the second feed was corrupting the very
    history it was added to fill.
    """
    try:
        v = float(ts)
    except (TypeError, ValueError):
        return 0
    if v <= 0:
        return 0
    return int(v * 1000.0) if v < _MS_FLOOR else int(v)


def register_trade_callback(cb):
    if PRICE_SOURCE in ("alpaca", "both", "auto"):
        alpaca_px.register_trade_callback(cb)
    if PRICE_SOURCE in ("finnhub", "both", "auto"):
        fh_register_trade_callback(cb)


def _load_finnhub_key() -> str:
    """
    Load the Finnhub API key.

    Priority: FINNHUB_API_KEY_ENGINE → FINNHUB_API_KEY → secrets.json
    'finnhub_key'. Returns empty string if not found (stream is skipped).

    FINNHUB_API_KEY_ENGINE exists because FINNHUB_API_KEY cannot hold an
    engine-specific value: desk_core.CREDENTIAL_ENV maps secrets.json's
    finnhub_key onto it, and apply_secrets_to_env deliberately lets
    secrets.json outrank signal_engine.env (shell > secrets > env file). So a
    second key placed in signal_engine.env as FINNHUB_API_KEY is loaded and
    then overwritten by the dashboard's key before this function ever runs —
    which is exactly the collision it was meant to resolve. This name is in no
    overlay table, so nothing rewrites it.
    """
    key = os.getenv("FINNHUB_API_KEY_ENGINE", "") or os.getenv("FINNHUB_API_KEY", "")
    if not key:
        secrets_path = _HERE / "config" / "secrets.json"
        if secrets_path.exists():
            try:
                s   = json.loads(secrets_path.read_text())
                key = s.get("finnhub_key", "")
            except Exception as e:
                print(f"[CFG] Could not read secrets.json for Finnhub key: {e}")
    if not key and PRICE_SOURCE in ("finnhub", "both"):
        print(
            "[CFG] WARNING: FINNHUB_API_KEY not set.\n"
            "       PRICE_SOURCE wants Finnhub — set FINNHUB_API_KEY or use PRICE_SOURCE=alpaca."
        )
    # One key, two processes, one connection allowed.
    #
    # The dashboard opens its own Finnhub socket from secrets.json
    # ('finnhub_key') at startup and the engine opens one from here. Finnhub's
    # free tier permits a single concurrent connection per key, so sharing one
    # means a race: on 2026-08-20 the dashboard won it and the engine's stream
    # never connected, its bar aggregator was never fed, and every CM RSI-2 /
    # %R reading in the book silently came off Alpaca REST fallback bars
    # instead of the live tape — for the whole session, with no error anywhere.
    #
    # Detectable before either socket is opened, so say it here. Give the
    # engine its own key in signal_engine.env (FINNHUB_API_KEY) to fix it.
    if key and PRICE_SOURCE in ("finnhub", "both"):
        try:
            secrets_path = _HERE / "config" / "secrets.json"
            if secrets_path.exists():
                dash_key = json.loads(secrets_path.read_text()).get("finnhub_key", "")
            else:
                dash_key = ""
        except Exception:
            dash_key = ""
        if dash_key and dash_key == key:
            print(
                "[CFG] WARNING: engine and dashboard share one Finnhub key.\n"
                "       Free tier allows ONE connection per key, so one of the\n"
                "       two streams will get no trades — and if it is this one,\n"
                "       realtime bars never form and every indicator quietly\n"
                "       falls back to Alpaca REST bars.\n"
                "       Fix: put a second key in signal_engine.env as "
                "FINNHUB_API_KEY_ENGINE\n"
                "       (NOT FINNHUB_API_KEY — secrets.json overwrites that one)."
            )
    return key


# ── Dashboard authentication ──────────────────────────────────────────────────

_dash_auth = desk_auth.for_process(
    "signal_engine",
    _HERE,
    default_url=DASHBOARD_URL,
    log_prefix="[AUTH]",
)


def _dashboard_login(user: str, password: str) -> Optional[str]:
    """Bearer token for *user*, or None on failure / while backing off.

    The token cache, the lock and the floor between attempts live in
    desk_auth now — this engine's copy had none of the three, so a persistent
    401 meant a fresh POST /auth/login on every poll.
    """
    if not user or not password:
        return None
    _dash_auth.set_creds(DASHBOARD_URL, user, password)
    return _dash_auth.token(force=True) or None


# ── Tracking triggers (dashboard row → engine active set) ─────────────────────

def row_triggers_tracking(row: dict, track_desk: bool | None = None) -> tuple[bool, str]:
    """
    Decide whether a dashboard ticker row should enter the engine active set.

    Returns (should_track, reason) where reason is a short tag for logs:
      mentioned | burst | find_it_first | desk | "" 

    Priority order matches how useful the catalyst signal is for trading;
    desk is the broadest (any symbol currently on the momentum list).
    """
    if not isinstance(row, dict) or not row.get("ticker"):
        return False, ""
    # Book-seeded names need indicators/tape even without momentum heat.
    if str(row.get("src") or "").strip().lower() == "book":
        return True, "book"
    if row.get("mentioned"):
        return True, "mentioned"
    if row.get("mention_burst"):
        return True, "burst"
    if row.get("find_it_first"):
        return True, "find_it_first"
    if track_desk is None:
        track_desk = TRACK_DESK_TICKERS
    if track_desk:
        return True, "desk"
    return False, ""


# ── Alpaca bar fetching ───────────────────────────────────────────────────────

def fetch_bars(symbol: str, api_key: str, secret_key: str,
               count: int = BAR_COUNT,
               timeframe: str = BAR_TIMEFRAME,
               lookback_days: int | None = None) -> Optional[pd.DataFrame]:
    """
    Download recent OHLCV bars from Alpaca going back `lookback_days`
    calendar days (default BAR_LOOKBACK_DAYS).  Using a start date instead of
    relying on limit alone ensures we always get bars from multiple sessions —
    critical for tickers that are thinly traded or when the market has only
    been open a few minutes.

    Strategy:
      Free-tier IEX feed only (no SIP — that requires a paid Alpaca data plan).
      HTTP 429s respect Retry-After (else exponential backoff + jitter).
      Process-wide throttle spaces multi-symbol warm/refresh so 32 names do
      not stampede the free IEX quota.
    """
    if not api_key or not secret_key:
        return None

    lookback = int(lookback_days if lookback_days is not None else BAR_LOOKBACK_DAYS)
    if lookback < 1:
        lookback = BAR_LOOKBACK_DAYS

    headers = {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret_key}
    url     = f"{ALPACA_BASE_URL}/v2/stocks/{symbol}/bars"

    # Go back `lookback` calendar days — covers weekends + holidays
    from datetime import datetime, timedelta, timezone as _tz
    start_dt = (datetime.now(_tz.utc) - timedelta(days=lookback)).strftime("%Y-%m-%dT%H:%M:%SZ")

    min_needed = MACD_SLOW + MACD_SIG + 5   # 40 bars minimum for stable MACD

    # iex is free. sip needs Algo Trader Plus and matches TradingView highs/lows.
    # Live engine stays on IEX unless explicitly configured — do not enable paid SIP here.
    feed_name = (os.getenv("ALPACA_BAR_FEED") or "iex").strip().lower()
    feeds = ("sip",) if feed_name == "sip" else ("iex",)
    max_retries = max(1, int(ALPACA_BAR_MAX_RETRIES))

    for feed in feeds:
        # sort=desc, NOT asc. `limit` truncates from whichever end the sort
        # starts at, so ascending returned the OLDEST `count` bars in the
        # lookback window — with the 5-day default that meant indicators were
        # computed on bars from three days ago and published as current. The
        # frame is flipped back to oldest-first below, which is what every
        # indicator downstream expects.
        params = {
            "timeframe": timeframe,
            "start":     start_dt,   # fetch from N days ago, not just today
            "limit":     count,
            "feed":      feed,
            "sort":      "desc",
        }
        for attempt in range(max_retries):
            try:
                alpaca_api.throttle_alpaca_request()
                resp = requests.get(url, params=params, headers=headers, timeout=10)
                code = getattr(resp, "status_code", None)
                if code == 429:
                    wait = alpaca_api.backoff_seconds(
                        attempt,
                        base_wait=1.0,
                        retry_after=alpaca_api.parse_retry_after(resp.headers),
                    )
                    alpaca_api.warn_429(
                        f"fetch_bars({symbol})",
                        wait,
                        f"attempt {attempt + 1}/{max_retries}",
                    )
                    if attempt >= max_retries - 1:
                        break
                    time.sleep(wait)
                    continue
                if isinstance(code, int) and code >= 500:
                    wait = alpaca_api.backoff_seconds(attempt, base_wait=1.0)
                    if attempt < max_retries - 1:
                        print(
                            f"  [BARS] {symbol}: HTTP {resp.status_code} on {feed}; "
                            f"backoff {wait:.1f}s (attempt {attempt + 1}/{max_retries})",
                            flush=True,
                        )
                        time.sleep(wait)
                        continue
                    resp.raise_for_status()
                resp.raise_for_status()
                bars = resp.json().get("bars", [])

                if not bars:
                    print(f"  [BARS] {symbol}: no bars on {feed} feed (start={start_dt[:10]})")
                    break

                df = pd.DataFrame(bars).rename(columns={
                    "o": "open", "h": "high", "l": "low",
                    "c": "close", "v": "volume", "t": "time",
                })
                for col in ("open", "high", "low", "close", "volume"):
                    df[col] = df[col].astype(float)
                # Back to oldest-first: the API gave us newest-first so that
                # `limit` kept the recent end, but every indicator reads forward
                # and takes .iloc[-1] as "now".
                df = df.iloc[::-1].reset_index(drop=True)

                if len(df) >= min_needed:
                    print(f"  [BARS] {symbol}: {len(df)} bars via {feed} ✓"
                          f" (lookback={lookback}d)")
                    return df

                # Not enough yet — try the next feed
                print(f"  [BARS] {symbol}: only {len(df)} bars on {feed} "
                      f"(need {min_needed}, lookback={lookback}d) — trying next feed…")
                break

            except requests.HTTPError as e:
                code = getattr(getattr(e, "response", None), "status_code", None)
                if code == 429 and attempt < max_retries - 1:
                    wait = alpaca_api.backoff_seconds(
                        attempt,
                        base_wait=1.0,
                        retry_after=alpaca_api.retry_after_from_exc(e),
                    )
                    alpaca_api.warn_429(
                        f"fetch_bars({symbol})",
                        wait,
                        f"attempt {attempt + 1}/{max_retries}",
                    )
                    time.sleep(wait)
                    continue
                print(f"  [BARS] {symbol}: {feed} fetch failed — {e}")
                break
            except Exception as e:
                print(f"  [BARS] {symbol}: {feed} fetch failed — {e}")
                break

    print(f"  [BARS] {symbol}: ❌ could not get {min_needed} bars on any feed "
          f"(lookback={lookback}d) — skipping this cycle")
    return None


# ── Indicator computation ─────────────────────────────────────────────────────

def compute_indicators(df: pd.DataFrame) -> tuple[float, float, float, float, float, bool, bool]:
    """Returns (rsi, macd_hist, atr_val, vwap_val, rvol, cm_rsi_ok, obv_ok)"""
    cfg = {"macd_fast": MACD_FAST, "macd_slow": MACD_SLOW, "macd_signal": MACD_SIG}
    df       = compute_macd(df, cfg)
    rsi_val  = float(calc_rsi(df["close"], RSI_PERIOD).iloc[-1])
    hist_val = float(df["macd_hist"].iloc[-1])

    # ATR — needs high/low/close; falls back gracefully if columns missing
    try:
        atr_val = float(calc_atr(df, period=14).iloc[-1])
    except Exception:
        atr_val = 0.0

    # VWAP — needs high/low/close/volume and a datetime index
    try:
        vwap_val = float(vwap_calc(df).iloc[-1])
    except Exception:
        vwap_val = 0.0

    # RVOL
    try:
        rvol = float(calc_rvol(df).iloc[-1])
    except Exception:
        rvol = 1.0

    # CM RSI approaching oversold
    try:
        df_cm = compute_cm_rsi_lower(df, {"cm_rsi_oversold": 25})
        cm_rsi_ok = bool(df_cm["cm_rsi_approaching"].iloc[-1])
    except Exception:
        cm_rsi_ok = True  # fail open — don't block buys if indicator errors

    # OBV oscillator trending up
    try:
        df_obv = compute_obv_oscillator(df, {"obv_length": 20})
        obv_ok = bool(df_obv["obv_trending_up"].iloc[-1])
    except Exception:
        obv_ok = True  # fail open

    return rsi_val, hist_val, atr_val, vwap_val, rvol, cm_rsi_ok, obv_ok


# ── Trade logging ─────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _load_log() -> list:
    if LOG_FILE.exists():
        try:
            return json.loads(LOG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []

# Keep only the most recent N log entries on disk. BUY/SELL volume is low, but
# _append_log rewrites the whole file each call, so an uncapped log grows without
# bound and the rewrite cost grows with it. Capping keeps recent history cheap.
LOG_MAX_ENTRIES = 5000

def _append_log(entry: dict):
    entries = _load_log()
    entries.append(entry)
    if len(entries) > LOG_MAX_ENTRIES:
        entries = entries[-LOG_MAX_ENTRIES:]
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=LOG_FILE.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(entries, f, indent=2)
        except Exception:
            try:
                os.close(fd)
            except Exception:
                pass
            raise
        Path(tmp_path).replace(LOG_FILE)
        tmp_path = None
    except Exception as e:
        print(f"  [engine] failed to write signal log: {e}", flush=True)
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

# ── Per-ticker state ──────────────────────────────────────────────────────────

class TickerState:
    """
    All state tracked for one active ticker.

    Expiry rules:
      • Never saw a positive histogram (EXPIRY_COLD = 3 min) — probably not moving
      • Saw positive histogram at least once (EXPIRY_WARM = 10 min) — came close, give it time
      • In a position — never expires, held until SELL fires
    """

    def __init__(self, ticker: str, fetch_offset_s: float = 0, pinned: bool = False):
        self.ticker      = ticker
        self.fetch_offset_s = fetch_offset_s
        self.added_ts    = time.time()
        self.in_position = False
        # Pinned compare tickers (COMPARE_TICKERS) never expire and are
        # evaluated on the forming bar every second for TV side-by-side checks.
        self.pinned      = pinned

        # Position data
        self.buy_price: Optional[float] = None
        self.buy_time:  Optional[str]   = None

        # MACD momentum tracking
        self.prev_hist:    Optional[float] = None
        self.hist_growing: bool            = False
        self.ever_positive_hist: bool      = False  # True once hist > 0 is ever seen

        # Latest indicator values — updated on each bar refresh
        self.last_rsi:   Optional[float] = None
        self.last_hist:  Optional[float] = None     # most recent histogram value
        self.last_price: Optional[float] = None
        self.bars_fetched: bool          = False    # True after first successful bar pull

        # First fetch: stagger so tickers added together don't all hit Alpaca at once.
        # fetch_offset_s=0 → fetches immediately, =5 → waits 5 s, =10 → waits 10 s
        self.last_bar_fetch: float  = time.time() - BAR_REFRESH + fetch_offset_s

        # After the first fetch we switch to minute-boundary alignment:
        # fetch ~5 s after each clock minute closes, offset by fetch_offset_s so
        # tickers stagger across the minute (ticker #0 at :05, #1 at :10, etc.)
        # last_bar_minute tracks which clock-minute we last fetched for.
        # -1 means "never fetched" — first fetch uses the stagger timer above.
        self.last_bar_minute: int   = -1

        self.check_count: int = 0   # increments every second — used in log output

        # Cached bar DataFrame (last 100 bars) — kept in memory between Alpaca fetches.
        # _check_proximity injects the live Finnhub price as the last bar's close
        # each second and recomputes RSI + MACD, so indicator values update in
        # real time instead of only once per minute.
        self.cached_df: Optional[pd.DataFrame] = None

        # ── Priority mention tracking ─────────────────────────────────────────
        # The dashboard is polled every DASHBOARD_POLL_INTERVAL seconds.
        # Each poll where this ticker is "mentioned" records an entry here.
        # When the rolling sum inside PRIORITY_WINDOW_SECONDS reaches
        # PRIORITY_MENTIONS the ticker is marked "hot" and the RSI filter is
        # bypassed — MACD growing alone is enough to fire a BUY.
        #
        # mention_history : list of (timestamp, count) — raw mention increments
        # mention_velocity: sum of counts in the current rolling window
        # is_hot          : True when velocity >= PRIORITY_MENTIONS
        # priority_buy    : True if the open position was entered as a hot buy
        # _last_raw_count : last numeric "mentions" value seen from dashboard
        #                   (used to compute deltas if the API returns a total)
        self.mention_history: list    = []   # [(float, int), ...]
        self.mention_velocity: int    = 0
        self.is_hot: bool             = False
        self.priority_buy: bool       = False
        self._last_raw_count: Optional[int] = None  # None = baseline not yet seeded
        self.went_hot: bool             = False  # True once this ticker was ever hot
        self.cooled_at: Optional[float] = None  # timestamp when it last lost hotness
        self.buy_time_ts: Optional[float] = None  # time.time() of last BUY (for min hold)

        # Fill reconciliation — set when a BUY order is submitted; a few seconds
        # later the engine looks the order up and rebases buy_price to the real
        # filled_avg_price so TP/SL bands measure from the actual entry.
        self.pending_order_id: Optional[str] = None
        self.fill_check_at: Optional[float]  = None
        self.needs_broker_trail: bool = False

        # Live indicator values — updated each bar fetch / proximity check
        self.last_atr:  Optional[float] = None
        self.last_vwap: Optional[float] = None
        self._last_computed_price: Optional[float] = None  # guards live-recompute throttle

        # Dynamic exit levels — set at BUY time, cleared on sell
        self.dyn_stop:  Optional[float] = None  # price to trigger stop-loss
        self.dyn_take:  Optional[float] = None  # price to trigger take-profit
        self.high_since_buy: Optional[float] = None  # highest price since entry (for trailing stop)

        # Consecutive growing histogram bar counter — BUY requires HIST_CONFIRM_BARS
        self.hist_grow_count: int = 0

        # Buy confirmation indicators — updated each bar fetch
        self.last_rvol:       Optional[float] = None
        self.last_cm_rsi_ok:  bool            = False  # CM RSI approaching oversold
        self.last_obv_ok:     bool            = False  # OBV oscillator trending up

        # Which API supplied the most recent bar data ("massive" or "alpaca")
        self._data_source: str = "alpaca"

        # Which pipe the bars the STRATEGY was evaluated on came out of, and
        # how stale the newest of them is. Distinct from _data_source, which
        # only ever names the REST vendor: with REALTIME_BARS on, the frame
        # handed to the indicators may instead be the Finnhub trade stream
        # aggregated locally, and "realtime" vs "alpaca" is the difference
        # between a reading drawn on the live tape and one drawn on the
        # fallback. Consumers that gate trades on an indicator need to be able
        # to tell those apart; see _strategy_df.
        self._bars_src: str = "alpaca"
        self._bars_age_sec: Optional[float] = None

        # Latest 3-indicator breakdown (set by _eval_three_indicator), surfaced
        # to the dashboard by proximity_state() when STRATEGY_MODE=three_indicator.
        self.three_ind_state: Optional[dict] = None

        # Still on the dashboard ticker list (momentum desk). Refreshed each
        # _ingest_state; while True, is_expired() stays False so RSI focus keeps
        # updating for symbols the desk is actively showing.
        self.on_desk: bool = False
        # Provenance from the dashboard ticker list. "book" means AI Watch
        # seeded this name for indicators/tape — prefer keeping it when the
        # active list is full over cold desk noise.
        self.src: str = ""
        # Failed bar-fetch attempts before first success (drives fast retry).
        self._bar_fetch_attempts: int = 0

    def expiry_seconds(self) -> int:
        """How long this ticker is allowed to live without a position."""
        return EXPIRY_WARM if self.ever_positive_hist else EXPIRY_COLD

    def age_s(self) -> float:
        """Seconds since this ticker was added."""
        return time.time() - self.added_ts

    def time_left_s(self) -> float:
        """Seconds remaining before expiry (if no position)."""
        if self.on_desk:
            return float("inf")  # held by desk presence
        if self.cooled_at is not None:
            return max(0.0, EXPIRY_COOLED - (time.time() - self.cooled_at))
        return max(0.0, self.expiry_seconds() - self.age_s())

    def is_expired(self) -> bool:
        if self.pinned:
            return False   # pinned compare tickers never expire
        if self.in_position:
            return False   # never expire an open position
        # Keep symbols the momentum desk is still showing so RSI / proximity
        # stay live; normal age/cool expiry applies once they leave the list.
        if self.on_desk:
            return False
        # Hot ticker that cooled off — drop it after EXPIRY_COOLED seconds
        if self.cooled_at is not None:
            return (time.time() - self.cooled_at) > EXPIRY_COOLED
        return self.age_s() > self.expiry_seconds()

    def decay_mentions(self):
        """
        Prune stale mention history and recalculate is_hot / cooled_at
        without recording a new mention.  Called every poll cycle so that
        tickers which stopped being mentioned (e.g. SPY) eventually cool off
        even if no new mention events arrive to trigger update_mention_velocity.
        """
        now    = time.time()
        cutoff = now - PRIORITY_WINDOW_SECONDS
        self.mention_history = [(t, c) for t, c in self.mention_history if t >= cutoff]
        self.mention_velocity = sum(c for _, c in self.mention_history)

        was_hot   = self.is_hot
        self.is_hot = (PRIORITY_MENTIONS > 0 and
                       self.mention_velocity >= PRIORITY_MENTIONS)

        if was_hot and not self.is_hot and not self.in_position and self.cooled_at is None:
            self.cooled_at = now
            print(
                f"\n  [{self.ticker:<6}] 🧊 COOLED OFF — "
                f"mentions expired from window "
                f"(velocity={self.mention_velocity} < threshold={PRIORITY_MENTIONS}) — "
                f"dropping in {EXPIRY_COOLED}s if no BUY\n"
            )

    def update_mention_velocity(self, count: int = 1):
        """
        Record `count` new mention(s) at the current time, prune the rolling
        window, and refresh mention_velocity + is_hot.

        Called every DASHBOARD_POLL_INTERVAL seconds by _ingest_state() for any
        ticker that is currently "mentioned" on the dashboard.

        If the dashboard returns a cumulative numeric count (e.g. row["mentions"]),
        pass the delta (new_count − last_count) so bursts inside one poll period
        are captured correctly.  If only a boolean is available, pass count=1.
        """
        now = time.time()
        self.mention_history.append((now, count))

        # Prune entries older than the rolling window
        cutoff = now - PRIORITY_WINDOW_SECONDS
        self.mention_history = [(t, c) for t, c in self.mention_history if t >= cutoff]

        self.mention_velocity = sum(c for _, c in self.mention_history)

        was_hot   = self.is_hot
        self.is_hot = (PRIORITY_MENTIONS > 0 and
                       self.mention_velocity >= PRIORITY_MENTIONS)

        # Print a one-time alert when the ticker first goes hot
        if self.is_hot and not was_hot:
            self.went_hot  = True
            self.cooled_at = None   # reset any previous cool-off timer
            print(
                f"\n  [{self.ticker:<6}] 🔥🔥 TICKER IS HOT — "
                f"{self.mention_velocity} mentions in {PRIORITY_WINDOW_SECONDS}s "
                f"(threshold={PRIORITY_MENTIONS}) — RSI filter BYPASSED on BUY\n"
            )

        # Detect cool-off: was hot, now below threshold — start the drop timer
        if was_hot and not self.is_hot and not self.in_position:
            self.cooled_at = time.time()
            print(
                f"\n  [{self.ticker:<6}] 🧊 COOLED OFF — "
                f"velocity dropped to {self.mention_velocity} "
                f"(below threshold={PRIORITY_MENTIONS}) — "
                f"dropping in {EXPIRY_COOLED}s if no BUY\n"
            )

    def proximity_summary(self) -> str:
        """
        One-line human-readable summary of how close this ticker is to a BUY signal.
        Printed every second so you can watch conditions converge in real time.
        """
        age   = self.age_s()
        left  = self.time_left_s()
        check = self.check_count

        # ── Alert mode: report the catalyst + position, not indicators ─────────
        if STRATEGY_MODE == "alert":
            price = self.last_price
            price_str = f"${price:.2f}" if price is not None else "$?"
            hot_str = f"🔥HOT(vel={self.mention_velocity})" if self.is_hot else \
                      f"vel={self.mention_velocity}/{PRIORITY_MENTIONS}"
            if self.in_position and self.buy_price and price:
                pnl  = (price - self.buy_price) / self.buy_price * 100
                peak = ((self.high_since_buy - self.buy_price) / self.buy_price * 100
                        if self.high_since_buy else 0.0)
                trail_at = (self.high_since_buy * (1 - ALERT_TRAIL_STOP / 100.0)
                            if self.high_since_buy else 0.0)
                status = (f"📈 IN POSITION P&L {pnl:+.2f}%  peak +{peak:.1f}%  "
                          f"trail@${trail_at:.2f} / hard@${self.buy_price*(1-ALERT_HARD_STOP/100):.2f}")
            elif self.is_hot:
                status = "🔥 BUY ZONE — catalyst live"
            else:
                status = "😴 watching for mention burst"
            return (f"  [{ticker_tag(self.ticker)}] {price_str}  {hot_str}  "
                    f"age={age:.0f}s ttl={left:.0f}s  #{check}  {status}")

        # ── Not yet loaded ─────────────────────────────────────────────────────
        if not self.bars_fetched:
            next_fetch = max(0, BAR_REFRESH - (time.time() - self.last_bar_fetch))
            return (
                f"  [{ticker_tag(self.ticker)}] ⏳ waiting for first bar fetch "
                f"(in ~{next_fetch:.0f}s)  age={age:.0f}s  ttl={left:.0f}s  #{check}"
            )

        rsi  = self.last_rsi
        hist = self.last_hist
        price = self.last_price

        # ── Build condition check marks ────────────────────────────────────────
        rsi_ok   = rsi  is not None and rsi  < RSI_BUY_MAX
        hist_pos = hist is not None and hist > 0
        growing  = self.hist_growing

        rsi_str  = f"RSI={rsi:.1f}"    if rsi   is not None else "RSI=?"
        hist_str = f"hist={hist:+.4f}" if hist  is not None else "hist=?"
        price_str = f"${price:.2f}"    if price is not None else "$?"

        # Condition indicators
        # If ticker is hot, RSI limit is bypassed — show it
        if self.is_hot and not rsi_ok:
            rsi_tag = f"🔥BYPASSED(hot vel={self.mention_velocity})"
        else:
            rsi_tag = "✓" if rsi_ok else f"✗(need<{RSI_BUY_MAX})"
        hist_tag = "✓pos" if hist_pos else "✗neg"
        grow_tag = "✓growing🔥" if growing else ("→flat" if hist_pos else "↓")

        # Hot indicator shown in tag when velocity >= threshold
        hot_str  = f" 🔥HOT(vel={self.mention_velocity})" if self.is_hot else ""

        # Overall status
        if self.in_position:
            pnl = (price - self.buy_price) / self.buy_price * 100
            prio_tag = " [PRIORITY]" if self.priority_buy else ""
            status = f"📈 IN POSITION — P&L: {pnl:+.2f}%{prio_tag}"
        elif growing and (rsi_ok or self.is_hot):
            status = f"🔥 BUY ZONE — signal imminent{hot_str}"
        elif growing and not rsi_ok and not self.is_hot:
            status = "⚠️  growing but RSI overbought — holding off"
        elif hist_pos and not growing:
            status = "👀 hist positive — watching for growth"
        elif self.ever_positive_hist:
            status = "↩️  retreated — was positive, now negative"
        else:
            status = f"😴 no signal yet{hot_str}"

        return (
            f"  [{ticker_tag(self.ticker)}] {price_str}  "
            f"{rsi_str} {rsi_tag}  "
            f"{hist_str} {hist_tag} {grow_tag}  "
            f"age={age:.0f}s ttl={left:.0f}s  "
            f"#{check}  {status}"
        )

    def proximity_pct(self) -> int:
        """
        Return 0–100 indicating how close this ticker is to a BUY signal.

        Three conditions, each worth 33 points:
          1. RSI below threshold  (or bypassed when ticker is_hot)
          2. MACD histogram > 0   (positive)
          3. MACD histogram growing

        A ticker that satisfies all three scores 100 and is in the BUY ZONE.
        """
        if not self.bars_fetched:
            return 0

        score = 0
        rsi   = self.last_rsi
        hist  = self.last_hist

        # Condition 1 — RSI filter (auto-pass when hot)
        if self.is_hot or (rsi is not None and rsi < RSI_BUY_MAX):
            score += 1

        # Condition 2 — MACD histogram positive
        if hist is not None and hist > 0:
            score += 1

        # Condition 3 — MACD histogram growing
        if self.hist_growing:
            score += 1

        return round(score / 3 * 100)

    def three_indicator_state(self) -> dict:
        """
        Dashboard state for the 3-indicator strategy. Reads the breakdown stashed
        by _eval_three_indicator (no recomputation) and adds position context.
        Shares the proximity_pct / status / pill keys the frontend already uses,
        with a "strategy" marker so the bar relabels its three conditions.
        """
        s = self.three_ind_state or {}
        pct = int(s.get("buy_pct", 0))

        if self.in_position and s.get("sell"):
            status = "exit_signal"
        elif self.in_position:
            status = "in_position"
        elif s.get("buy") or pct >= 100:
            status = "buy_zone"
        elif pct >= 67:
            status = "aligning"
        elif not self.bars_fetched:
            status = "watching"
        else:
            status = "watching"

        return {
            "strategy":       "three_indicator",
            "pinned":         self.pinned,
            "price":          round(self.last_price, 4) if self.last_price else None,
            "proximity_pct":  pct,
            "status":         status,
            "in_position":    self.in_position,
            "buy_price":      round(self.buy_price, 4) if self.buy_price else None,
            "is_hot":         self.is_hot,
            "mention_velocity": self.mention_velocity,
            "bars_fetched":   self.bars_fetched,
            "data_source":    getattr(self, "_data_source", "alpaca"),
            # Provenance of the bars every indicator below was computed on.
            # "realtime" = Finnhub trades aggregated locally; anything else is
            # the REST fallback. Published beside the readings rather than
            # inferred later, because the source flips per ticker mid-session.
            "bars_src":       getattr(self, "_bars_src", "alpaca"),
            "bars_age_sec":   getattr(self, "_bars_age_sec", None),
            # EXH provenance: live = Finnhub trades on realtime_bars.
            # Anything else is REST fallback and must not look like the tape.
            "pctr_src": (
                "live" if getattr(self, "_bars_src", "") == "realtime"
                else (getattr(self, "_bars_src", None) or "engine")
            ),
            # 3-indicator breakdown (drives the three condition pills)
            "cm_rsi":         s.get("cm_rsi"),
            "cm_ok":          bool(s.get("cm_ok")),
            "cm_rsi_rising":  bool(s.get("cm_rsi_rising")),
            "pctr":           s.get("pctr"),
            "pctr_ok":        bool(s.get("pctr_ok")),
            "pctr_rising":    bool(s.get("pctr_rising")),
            # Fast + slow %R deep-oversold band for desk FOCUS
            "pctr_slow":         s.get("pctr_slow"),
            "pctr_falling":      bool(s.get("pctr_falling")),
            "pctr_slow_falling": bool(s.get("pctr_slow_falling")),
            "pctr_slow_rising":  bool(s.get("pctr_slow_rising")),
            "pctr_deep_os":      bool(s.get("pctr_deep_os")),
            "pctr_ob":           bool(s.get("pctr_ob")),
            "pctr_tight":        bool(s.get("pctr_tight")),
            "pctr_gap":          s.get("pctr_gap"),
            "cm_rsi_low":        bool(s.get("cm_rsi_low")),
            "cm_rsi_green":      bool(s.get("cm_rsi_green")),
            "macd_fast":      s.get("macd_fast"),
            "macd_slow":      s.get("macd_slow"),
            "macd_gap":       s.get("macd_gap"),
            "macd_hist":      s.get("macd_hist") if s.get("macd_hist") is not None else s.get("macd_gap"),
            "macd_bull":      bool(s.get("macd_bull")),
            "macd_cross":     bool(s.get("macd_cross")),
            "macd_sep_ratio": s.get("macd_sep_ratio"),
            # Direction of the separation, not just its size: a wide
            # gap that is closing is momentum dying, and the arm gate
            # refuses it (ai_watch_macd_require_widening).
            "macd_gap_rising":  s.get("macd_gap_rising"),
            "macd_gap_falling": s.get("macd_gap_falling"),
            "macd_gap_prev":    s.get("macd_gap_prev"),
            "macd_ok":        bool(s.get("macd_ok")),
            "buy_signal":     bool(s.get("buy")),
            "sell_signal":    bool(s.get("sell")),
        }

    def alert_state(self) -> dict:
        """
        Dashboard state for the alert strategy. The "signal" here is the mention
        burst, so proximity is how close mention_velocity is to the hot threshold.
        Once in a position, surfaces live P&L and the peak gain the trailing stop
        is protecting. Reuses the generic proximity keys the frontend renders.
        """
        if PRIORITY_MENTIONS > 0:
            pct = int(min(100, self.mention_velocity / PRIORITY_MENTIONS * 100))
        else:
            pct = 100 if self.is_hot else 0

        pnl_pct = peak_gain = None
        if self.in_position and self.buy_price:
            if self.last_price:
                pnl_pct = round((self.last_price - self.buy_price) / self.buy_price * 100, 2)
            if self.high_since_buy:
                peak_gain = round((self.high_since_buy - self.buy_price) / self.buy_price * 100, 2)

        if self.in_position:
            status = "in_position"
        elif self.is_hot:
            status = "buy_zone"
        elif pct > 0:
            status = "aligning"
        else:
            status = "watching"

        return {
            "strategy":         "alert",
            "price":            round(self.last_price, 4) if self.last_price else None,
            "proximity_pct":    pct,
            "status":           status,
            "in_position":      self.in_position,
            "buy_price":        round(self.buy_price, 4) if self.buy_price else None,
            "pnl_pct":          pnl_pct,
            "peak_gain_pct":    peak_gain,
            "is_hot":           self.is_hot,
            "mention_velocity": self.mention_velocity,
            "hard_stop_pct":    ALERT_HARD_STOP,
            "trail_stop_pct":   ALERT_TRAIL_STOP,
            "bars_fetched":     self.bars_fetched,
            "data_source":      getattr(self, "_data_source", "alpaca"),
        }

    def proximity_state(self) -> dict:
        """
        Return a JSON-serialisable dict describing the current signal proximity.
        Written to signal_state.json every SIGNAL_STATE_INTERVAL seconds so the
        dashboard can render the visual progress bar without coupling to the engine.
        """
        if STRATEGY_MODE == "three_indicator":
            return self.three_indicator_state()
        if STRATEGY_MODE == "alert":
            return self.alert_state()

        rsi  = self.last_rsi
        hist = self.last_hist
        pct  = self.proximity_pct()

        if self.in_position:
            status = "in_position"
        elif self.hist_growing and (rsi is None or rsi < RSI_BUY_MAX or self.is_hot):
            status = "buy_zone"
        elif self.hist_growing:
            status = "growing_rsi_high"
        elif hist is not None and hist > 0:
            status = "hist_positive"
        elif self.ever_positive_hist:
            status = "retreated"
        else:
            status = "watching"

        return {
            "price":          round(self.last_price, 4) if self.last_price else None,
            "rsi":            round(rsi, 2)             if rsi is not None else None,
            "macd_hist":      round(hist, 6)            if hist is not None else None,
            "hist_positive":  hist is not None and hist > 0,
            "hist_growing":   self.hist_growing,
            "in_position":    self.in_position,
            "buy_price":      round(self.buy_price, 4)  if self.buy_price else None,
            "proximity_pct":  pct,
            "proximity_score": round(pct / 33.34),      # 0–3 integer
            "is_hot":         self.is_hot,
            "mention_velocity": self.mention_velocity,
            "status":         status,
            "bars_fetched":   self.bars_fetched,
            "data_source":    getattr(self, "_data_source", "alpaca"),
        }

    def update_momentum(self, hist: float, price: float, rsi: float,
                        open_positions: int = 0):
        """Record one indicator reading and track histogram momentum.

        This used to open the position and manage every exit — time stop, ATR
        trailing stop, stop-loss, take-profit, RSI-overbought, reversal — all
        interleaved with the measurement itself. The engine no longer trades,
        so what stays is the measurement: the histogram state the dashboard
        renders and the expiry rule reads.

        `open_positions` is retained in the signature because callers still
        pass it; the exposure cap was its only consumer.
        """
        self.last_price = price
        self.last_rsi   = rsi
        self.last_hist  = hist

        if hist > 0:
            self.ever_positive_hist = True

        prev = self.prev_hist
        if prev is not None:
            if hist > 0 and hist > prev:
                self.hist_grow_count = min(self.hist_grow_count + 1,
                                           HIST_CONFIRM_BARS)
                if self.hist_grow_count >= HIST_CONFIRM_BARS and not self.hist_growing:
                    print(f"  [{ticker_tag(self.ticker)}] 📈 histogram CONFIRMED GROWING"
                          f"  ({self.hist_grow_count} bars)  hist={hist:+.4f}"
                          f"  RSI={rsi:.1f}  price={price:.2f}")
                    self.hist_growing = True
            elif hist <= 0 or hist < prev:
                if self.hist_growing:
                    print(f"  [{ticker_tag(self.ticker)}] 📉 histogram REVERSING"
                          f"  hist={hist:+.4f} (was {prev:+.4f})"
                          f"  RSI={rsi:.1f}  price={price:.2f}")
                    self.hist_growing = False
                self.hist_grow_count = 0

        self.prev_hist = hist


def ticker_tag(sym: str) -> str:
    """Fixed-width ticker label for aligned log output."""
    return sym.ljust(6)


# ── Active-list priority ──────────────────────────────────────────────────────

def pick_cold_nonbook_eviction(active: dict) -> str | None:
    """Oldest cold, non-pinned, non-book, not-in-position symbol — or None.

    When the active list is full and a book/seeded name wants in, free a slot
    by dropping cold desk noise first. Never returns a book src, pinned, or
    in-position ticker.
    """
    candidates: list[tuple[float, str]] = []
    for sym, ts in active.items():
        if getattr(ts, "pinned", False) or getattr(ts, "in_position", False):
            continue
        if str(getattr(ts, "src", "") or "").strip().lower() == "book":
            continue
        if getattr(ts, "ever_positive_hist", False):
            continue  # warm — keep over cold noise
        candidates.append((float(getattr(ts, "added_ts", 0.0) or 0.0), sym))
    if not candidates:
        return None
    candidates.sort()  # oldest cold first
    return candidates[0][1]


# ── Main engine ───────────────────────────────────────────────────────────────

class SignalEngine:
    """
    Main loop.

    Efficiency design:
      • One dashboard HTTP call per second (never per ticker).
      • Alpaca bar fetches: max one per BAR_REFRESH seconds per ticker,
        staggered so they spread out over time.
      • Every-second proximity checks are pure in-memory comparisons.
      • Active list hard-capped at MAX_ACTIVE_TICKERS.
    """

    def __init__(self):
        self.api_key, self.secret_key = _load_alpaca_credentials()
        self.finnhub_key: str = _load_finnhub_key()

        self._started = _now_iso()                     # engine boot time (for the build badge)
        self._token: Optional[str] = None
        self.active: dict[str, TickerState] = {}       # sym → TickerState
        self._known_mentioned: set[str] = set()        # ever-seen mentioned syms
        self._stagger_index: int = 0                   # increments per added ticker
        self._last_poll_time: float = 0.0              # last time we hit /api/state
        self._last_state_write: float = 0.0            # last time we wrote signal_state.json

        # Initialise Alpaca trader (off / paper / live — set by TRADER_MODE)
        # BRACKET_EXITS=on|off|auto — auto attaches OTOCO when both SL and TP > 0
        # Live prices: Alpaca IEX poll (default) and/or Finnhub WebSocket
        if PRICE_SOURCE in ("alpaca", "both", "auto") and self.api_key and self.secret_key:
            ok = alpaca_px.start_alpaca_price_poll(
                self.api_key, self.secret_key, tickers=[], poll_sec=1.0)
            if ok:
                print("[ALPACA_PX] IEX latest-trade poller started — "
                      "subscribing tickers as they appear")
        if PRICE_SOURCE in ("finnhub", "both", "auto") and self.finnhub_key:
            start_finnhub_stream(self.finnhub_key, tickers=[])
            print("[FH] Finnhub WebSocket stream started — subscribing tickers as they appear")
        print(f"[PRICE] source={PRICE_SOURCE}")

        # Initialise Massive client for alert-ticker bar fetching
        self.massive = massive_client.MassiveClient(api_key=MASSIVE_API_KEY)
        if self.massive.is_configured():
            print("[MASSIVE] API key loaded — alert tickers will try Massive bars first")

        # Realtime bar aggregator — fed by live trade callbacks when enabled.
        self.rt_bars = RealtimeBarAggregator()
        # Tickers currently falling back off stale realtime bars. Transition
        # tracking only, so the log records the switch rather than every bar.
        self._rt_stale: set[str] = set()
        if STRATEGY_MODE == "three_indicator":
            print("[STRATEGY] three_indicator active  "
                  f"(realtime_bars={'on' if REALTIME_BARS else 'off'})")
            if REALTIME_BARS:
                register_trade_callback(
                    lambda sym, price, vol, ts: self.rt_bars.on_trade(
                        sym, price, vol, _trade_ts_ms(ts))
                )
                print("[STRATEGY] realtime bars: aggregating live OHLCV from price poll/stream")
        elif STRATEGY_MODE == "alert":
            print("[STRATEGY] alert active  (proximity = mention velocity)")

        # Pin compare tickers from startup — never expire and get realtime
        # forming-bar evaluation, for side-by-side validation against TradingView.
        for i, sym in enumerate(COMPARE_TICKERS):
            self.active[sym] = TickerState(sym, fetch_offset_s=i * BAR_STAGGER, pinned=True)
            self._known_mentioned.add(sym)
            request_subscribe([sym])
            print(f"  📌 PINNED {sym} — compare ticker (never expires, realtime eval)")

    def _auth_headers(self) -> dict:
        _dash_auth.set_creds(DASHBOARD_URL, DASHBOARD_USER, DASHBOARD_PASS)
        h = _dash_auth.headers()
        return {k: v for k, v in h.items() if k in ("Authorization", "X-Desk-Secret")}

    def _ensure_logged_in(self):
        _dash_auth.set_creds(DASHBOARD_URL, DASHBOARD_USER, DASHBOARD_PASS)
        if _dash_auth.desk_secret:
            return
        if not self._token and DASHBOARD_USER and DASHBOARD_PASS:
            self._token = _dashboard_login(DASHBOARD_USER, DASHBOARD_PASS)

    # ── Dashboard polling ─────────────────────────────────────────────────────

    def _poll_dashboard(self) -> Optional[dict]:
        """
        Single GET /api/state — retries once after a 401 re-login.
        Called at most every DASHBOARD_POLL_INTERVAL seconds (default 5 s).
        Its only job now is detecting newly mentioned tickers; real-time
        prices come from the Finnhub WebSocket instead.
        """
        for attempt in range(2):
            try:
                resp = requests.get(
                    f"{DASHBOARD_URL}/api/state",
                    headers=self._auth_headers(),
                    timeout=5,
                )
                if resp.status_code == 401:
                    print("[AUTH] 401 — re-authenticating…")
                    self._token = _dashboard_login(DASHBOARD_USER, DASHBOARD_PASS)
                    continue
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.ConnectionError:
                if attempt == 0:
                    print(f"[POLL] Cannot reach {DASHBOARD_URL}")
                return None
            except Exception as e:
                print(f"[POLL] Error: {e}")
                return None
        return None

    def _ingest_state(self, state: dict):
        """
        Process one dashboard snapshot.
        - Use dashboard price as a fallback ONLY if Finnhub has no reading yet.
        - Add newly highlighted / desk tickers and subscribe them for live prices.
        - Refresh on_desk so symbols still on the momentum list do not expire.
        """
        # Decay mention windows for ALL active tickers on every poll cycle.
        # This ensures tickers that stopped being mentioned (e.g. SPY) cool off
        # even if no new mention event ever arrives to trigger update_mention_velocity.
        # Clear on_desk; rows present below re-mark it so leavers can expire.
        for ts in list(self.active.values()):
            ts.decay_mentions()
            if not ts.pinned:
                ts.on_desk = False

        for row in state.get("tickers", []):
            sym   = row.get("ticker", "")
            price = row.get("price")
            if not sym:
                continue
            sym = str(sym).upper()

            # Keep last_price fresh: prefer live stream, but when the stream is
            # quiet/frozen (common after hours) and the dashboard snapshot moves,
            # adopt the dashboard price so RSI / proximity don't freeze.
            if sym in self.active and price is not None:
                ts_px = self.active[sym]
                stream = get_latest_price(sym)
                dash = float(price)
                if stream is None:
                    ts_px.last_price = dash
                else:
                    prev = ts_px.last_price
                    stream_stuck = (
                        prev is not None
                        and abs(stream - prev) < 1e-9
                        and abs(dash - prev) >= 0.005
                    )
                    ts_px.last_price = dash if stream_stuck else stream

            # Still on the dashboard ticker list → hold for RSI / proximity
            if sym in self.active:
                self.active[sym].on_desk = True
                src_tag = str(row.get("src") or "").strip().lower()
                if src_tag == "book":
                    self.active[sym].src = "book"

            # ── Mention velocity tracking ─────────────────────────────────────
            # Every time the dashboard says this ticker is mentioned, record it.
            # If the API returns a cumulative numeric count (row["mentions"]), we
            # use the delta since last poll to capture sub-poll bursts.
            # If only a boolean is available we count each poll as 1 mention.
            if sym in self.active and row.get("mentioned"):
                ts = self.active[sym]
                raw_count = row.get("mentions") if row.get("mentions") is not None else row.get("mention_count")
                if isinstance(raw_count, (int, float)) and raw_count > 0:
                    raw = int(raw_count)
                    if ts._last_raw_count is None:
                        ts._last_raw_count = raw  # seed baseline; don't fire on first sight
                    else:
                        delta = raw - ts._last_raw_count
                        ts._last_raw_count = raw
                        if delta > 0:
                            ts.update_mention_velocity(delta)
                else:
                    # Boolean "mentioned" only — each 5-second poll counts as 1
                    ts.update_mention_velocity(1)

            # Add tickers for: transcription mention, mention burst, Find It First,
            # or (when TRACK_DESK_TICKERS) any row on the dashboard ticker list.
            # Only mark _known_mentioned after a successful add so a full list
            # does not permanently block a later slot.
            is_triggered, reason = row_triggers_tracking(row)
            if not is_triggered:
                continue
            if sym in self.active:
                continue  # already tracking

            # Pinned compare tickers don't count against the cap.
            # When full, prefer keeping book/watch over cold desk noise —
            # evict a cold non-book slot before skipping a seeded/book name.
            if sum(1 for t in self.active.values() if not t.pinned) >= MAX_ACTIVE_TICKERS:
                is_book = (reason == "book"
                           or str(row.get("src") or "").strip().lower() == "book")
                victim = pick_cold_nonbook_eviction(self.active) if is_book else None
                if victim:
                    print(f"  [ENGINE] ♻️  active full ({MAX_ACTIVE_TICKERS}) "
                          f"— evicting cold {victim} for book {sym}")
                    del self.active[victim]
                    self._known_mentioned.discard(victim)
                    request_unsubscribe([victim])
                else:
                    print(f"  [ENGINE] ⚠️  active list full ({MAX_ACTIVE_TICKERS}) "
                          f"— skipping {sym} ({reason})")
                    continue

            # Stagger bar fetches: ticker #0 fetches now, #1 in BAR_STAGGER s,
            # #2 in 2×BAR_STAGGER s, etc.  Prevents a burst of Alpaca calls
            # when several tickers land on the desk close together.
            offset = self._stagger_index * BAR_STAGGER
            self._stagger_index = (self._stagger_index + 1) % MAX_ACTIVE_TICKERS

            ts = TickerState(sym, fetch_offset_s=offset)
            ts.on_desk = True
            if reason == "book" or str(row.get("src") or "").strip().lower() == "book":
                ts.src = "book"
            self.active[sym] = ts
            self._known_mentioned.add(sym)
            expiry_tag = "desk-held" if ts.on_desk else (
                "3min" if not ts.ever_positive_hist else "10min")
            print(
                f"\n  ➕ ADDED {sym}  [{reason}]  "
                f"(bar fetch in ~{offset}s | expiry {expiry_tag} if no signal)\n"
            )

            # Subscribe for real-time trade prices (Alpaca IEX and/or Finnhub)
            request_subscribe([sym])
            tags = []
            if PRICE_SOURCE in ("alpaca", "both", "auto"):
                tags.append("Alpaca IEX")
            if PRICE_SOURCE in ("finnhub", "both", "auto") and self.finnhub_key:
                tags.append("Finnhub" if FINNHUB_STATE.connected else "Finnhub…")
            print(f"  [PX]  {sym} — subscribed ({', '.join(tags) or PRICE_SOURCE})")

    # ── Bar refresh ───────────────────────────────────────────────────────────

    def _refresh_bars(self, ts: TickerState):
        """
        Fetch fresh bars for one ticker if it's due.

        First fetch  — uses the stagger timer (fetch_offset_s after being added)
                       so a newly mentioned ticker gets bar data quickly.

        Subsequent   — aligned to clock-minute boundaries.  A new 1-minute bar
                       closes at :00 of every minute; we wait an extra 5 s for
                       Alpaca to have it ready, then add fetch_offset_s so each
                       ticker fires at a slightly different second (spreads the
                       Alpaca load).  Result: indicators are never more than
                       ~65 s stale, and every second check now sees fresh data
                       right after each bar closes.
        """
        now = time.time()
        dt  = datetime.now(timezone.utc)
        current_minute   = int(now // 60)               # used for logging / last_bar_minute stamp
        secs_past_minute = dt.second + dt.microsecond / 1_000_000  # 0–59.999

        if not ts.bars_fetched:
            # ── Until first success: stagger, then fast retry on failure ──
            # Initial last_bar_fetch is set so the first attempt respects
            # fetch_offset_s via the BAR_REFRESH comparison. After a failed
            # attempt we only wait BAR_FAIL_RETRY_S so thin names recover
            # without waiting a full minute.
            min_wait = (BAR_FAIL_RETRY_S if ts._bar_fetch_attempts > 0
                        else BAR_REFRESH)
            if now - ts.last_bar_fetch < min_wait:
                return
        else:
            # ── Subsequent fetches: elapsed-time guard + stagger slot ─────
            # Using elapsed time (not minute counter) so NTP adjustments or
            # clock steps backward don't permanently block future fetches.
            fire_at = 5 + ts.fetch_offset_s
            if (now - ts.last_bar_fetch) < 55:
                return   # not yet ~1 minute since last fetch
            if secs_past_minute < fire_at:
                return   # stagger slot not reached yet in this minute

        # Prefer Finnhub realtime bars when live: skip routine Alpaca refresh
        # once seeded so 32 active symbols do not stampede free-tier IEX.
        # Still allow a slow safety refresh so cached_df can recover if the
        # tape later goes quiet. Does not touch arm gates.
        if (
            ALPACA_RT_SKIP_REFRESH
            and REALTIME_BARS
            and ts.bars_fetched
            and self.rt_bars.is_seeded(ts.ticker)
        ):
            age = self.rt_bars.age_seconds(ts.ticker)
            if age is not None and age <= RT_BARS_MAX_STALE:
                rt = self.rt_bars.get_bars(ts.ticker)
                min_needed_rt = MACD_SLOW + MACD_SIG + 5
                if rt is not None and len(rt) >= min_needed_rt:
                    since = now - ts.last_bar_fetch
                    if since < ALPACA_RT_SAFETY_REFRESH_S:
                        # Keep subsequent-fetch timing honest without hitting Alpaca.
                        ts.last_bar_fetch = now
                        ts.last_bar_minute = current_minute
                        return

        # Longer lookback until we have a successful load — thin / after-hours
        # names often lack 40×1m IEX bars inside the short steady-state window.
        lookback = (max(BAR_LOOKBACK_DAYS, BAR_LOOKBACK_THIN)
                    if not ts.bars_fetched else BAR_LOOKBACK_DAYS)

        print(f"  [{ticker_tag(ts.ticker)}] 🔄 fetching bars  "
              f"(minute={current_minute} secs={secs_past_minute:.1f} "
              f"lookback={lookback}d)")

        # ── Try Massive first for hot tickers OR first-load warmup ─────────
        # Massive helps after hours / thin names when IEX is sparse. Once we
        # already have bars, only prefer Massive for hot (burst) symbols.
        df = None
        min_needed = MACD_SLOW + MACD_SIG + 5
        use_massive = self.massive.is_configured() and (
            ts.is_hot or not ts.bars_fetched)
        if use_massive:
            df = self.massive.fetch_bars(
                symbol=ts.ticker,
                timeframe=BAR_TIMEFRAME,
                count=BAR_COUNT,
                lookback_days=lookback,
            )
            if df is not None and len(df) >= min_needed:
                ts._data_source = "massive"
                print(f"  [{ticker_tag(ts.ticker)}] ✨ using Massive bar data")
            else:
                if df is not None:
                    print(f"  [{ticker_tag(ts.ticker)}] ⚠️  Massive returned only "
                          f"{len(df)} bars (plan may be free-tier, need intraday) "
                          f"— falling back to Alpaca")
                df = None  # fall through to Alpaca

        # ── Fall back to Alpaca ────────────────────────────────────────────
        if df is None:
            df = fetch_bars(ts.ticker, self.api_key, self.secret_key,
                            lookback_days=lookback)
            if df is not None:
                ts._data_source = "alpaca"

        ts.last_bar_fetch = now
        ts._bar_fetch_attempts += 1

        if df is None or len(df) < min_needed:
            # Stay on the fast first-fetch path until bars_fetched flips True.
            if not ts.bars_fetched:
                ts.last_bar_minute = -1
            else:
                ts.last_bar_minute = current_minute
            print(f"  [{ticker_tag(ts.ticker)}] ⚠️  not enough bars "
                  f"({len(df) if df is not None else 0} received, "
                  f"need {min_needed}, lookback={lookback}d"
                  f"{f'; retry in {BAR_FAIL_RETRY_S}s' if not ts.bars_fetched else ''})")
            return

        try:
            rsi_val, hist_val, atr_val, vwap_val, rvol_val, cm_rsi_ok_val, obv_ok_val = compute_indicators(df)
        except Exception as e:
            # Keep first-fetch fast-retry if we never successfully published bars.
            if not ts.bars_fetched:
                ts.last_bar_minute = -1
            else:
                ts.last_bar_minute = current_minute
            print(f"  [{ticker_tag(ts.ticker)}] ❌ indicator error: {e}")
            return

        # Success — align subsequent refreshes to the clock-minute path
        ts.last_bar_minute = current_minute

        ts.last_atr      = atr_val  if atr_val  > 0 else ts.last_atr
        ts.last_vwap     = vwap_val if vwap_val > 0 else ts.last_vwap
        ts.last_rvol     = rvol_val
        ts.last_cm_rsi_ok = cm_rsi_ok_val
        ts.last_obv_ok   = obv_ok_val

        # Prefer Finnhub live price; fall back to last dashboard poll value,
        # then finally the last bar's close price.
        fh_price = get_latest_price(ts.ticker)
        if fh_price is not None:
            price = fh_price
        elif ts.last_price is not None:
            price = ts.last_price
        elif len(df) > 0 and "close" in df.columns:
            price = float(df["close"].iloc[-1])
        else:
            price = 0.0
        if fh_price is not None:
            ts.last_price = fh_price   # keep in sync

        ts.bars_fetched = True

        # Cache the last 100 bars — enough for stable MACD (3× the slow period).
        # _check_proximity will inject the live price into this cache every second.
        ts.cached_df = df.iloc[-100:].copy().reset_index(drop=True)

        print(
            f"  [{ticker_tag(ts.ticker)}] 📊 bars loaded  "
            f"RSI={rsi_val:.1f}  hist={hist_val:+.4f}  "
            f"price={price:.2f} {'[FH]' if fh_price is not None else '[dash]'}  "
            f"bars={len(df)}"
        )

        # Signal check on the freshly closed bar
        open_pos = sum(1 for t in self.active.values() if t.in_position)
        if STRATEGY_MODE == "three_indicator":
            if REALTIME_BARS and not self.rt_bars.is_seeded(ts.ticker):
                self.rt_bars.seed(ts.ticker, df)
            self._eval_three_indicator(ts, self._strategy_df(ts, df))
        else:
            ts.update_momentum(hist=hist_val, price=price, rsi=rsi_val,
                               open_positions=open_pos)

    # ── 3-indicator strategy evaluation (gated by STRATEGY_MODE) ──────────────

    def _strategy_df(self, ts: TickerState, fallback_df):
        """Realtime aggregated bars when enabled, sufficient, and *fresh*.

        Freshness is not optional. The aggregator keeps its sealed history and
        its forming bar when the trade stream drops, so get_bars() goes on
        returning a full-looking frame whose newest bar silently ages. Computing
        CM RSI-2 / %R / MACD on a frozen candle and publishing them as current
        readings is strictly worse than the ≤60s-old Alpaca bars already in
        hand: the desk cannot tell a stale indicator from a live one, and a
        buy-readiness verdict built on it looks exactly as confident.

        age_seconds() returns None for a ticker no trade has ever touched —
        seeded-but-unfed, or simply not trading. That is unusable, not fresh.
        """
        if REALTIME_BARS:
            age = self.rt_bars.age_seconds(ts.ticker)
            if age is not None and age <= RT_BARS_MAX_STALE:
                rt = self.rt_bars.get_bars(ts.ticker)
                if rt is not None and len(rt) >= MACD_SLOW + MACD_SIG + 5:
                    if ts.ticker in self._rt_stale:
                        self._rt_stale.discard(ts.ticker)
                        print(f"  [{ticker_tag(ts.ticker)}] ▶ realtime bars live again")
                    # Which pipe produced this frame, on the frame itself.
                    # Without it a Finnhub-bar reading and an Alpaca-bar
                    # reading are indistinguishable downstream, and the desk
                    # cannot say whether an indicator it is about to trade on
                    # came from the realtime tape or from the fallback. The
                    # source flips constantly — 20 recoveries and 27 fallbacks
                    # across 18 symbols on 2026-08-20 alone.
                    ts._bars_src = "realtime"
                    # Rounded: this goes on the wire and into the book's RSI
                    # tooltip, where "0.76976708984375s" is noise, not detail.
                    ts._bars_age_sec = round(float(age), 1)
                    return rt
            elif ts.ticker not in self._rt_stale:
                # Log the transition only — this runs on every bar close.
                self._rt_stale.add(ts.ticker)
                stale_for = "no trades yet" if age is None else f"{age:.0f}s stale"
                # Say whether the socket is even up. "No trades yet" reads like
                # a quiet symbol, but it is also exactly what a dead stream
                # looks like, and on 2026-08-20 that was the actual cause for
                # every ticker in the book for a full session: the dashboard
                # and the engine each opened a Finnhub connection on the same
                # free-tier key, which allows one, and the engine's got
                # nothing. The only visible tell was an ellipsis in a subscribe
                # tag. Never again — name it where someone is already looking.
                sock = "" if FINNHUB_STATE.connected else "  [SOCKET DOWN]"
                print(f"  [{ticker_tag(ts.ticker)}] ⏸ realtime bars {stale_for} — "
                      f"falling back to Alpaca bars{sock}")
        ts._bars_src = getattr(ts, "_data_source", "alpaca")
        ts._bars_age_sec = None
        return fallback_df

    def _eval_three_indicator(self, ts: TickerState, df):
        """
        Compute CM RSI-2 / %R Trend Exhaustion / MACD on `df` and publish the
        breakdown. Evaluation only — the engine does not trade.

        It used to place the order too, and the entry logic sat directly under
        this computation. That coupling is why the split matters: the reading
        is what the desk and the buy circle consume, and it has to keep running
        untouched now that nothing acts on it.
        """
        if df is None or len(df) < MACD_SLOW + MACD_SIG + 5:
            return
        try:
            ind = three_ind.compute_indicators(df, THREE_IND_PARAMS)
            a   = three_ind.to_arrays(ind)
        except Exception as e:
            print(f"  [{ticker_tag(ts.ticker)}] ❌ 3ind error: {e}")
            return

        i = len(a["close"]) - 1
        if i < 2:
            return

        # Stash the breakdown for the dashboard (reuses `a` — no extra indicator
        # computation). proximity_state() reads this every SIGNAL_STATE_INTERVAL.
        st = three_ind.evaluate_state(a, i, THREE_IND_PARAMS)
        ts.three_ind_state = st

        # A setup with 2 of 3 conditions aligned has shown promise — extend its
        # watch window to EXPIRY_WARM (the 3ind analogue of the momentum
        # engine's positive-histogram rule, which never runs in this mode).
        if st.get("buy_pct", 0) >= 67:
            ts.ever_positive_hist = True
            ts.high_since_buy = None
            ts.priority_buy   = False

    # ── Alert strategy evaluation (gated by STRATEGY_MODE=alert) ──────────────

    def _check_proximity(self, ts: TickerState):
        """
        Every second: refresh the live price, re-run the indicators against it,
        and log the proximity summary.

        This also used to be where money moved — a fill-price rebase, the alert
        strategy's entry, real-time stop-loss / take-profit, and an RSI
        overbought exit all fired from here on the live tick. None of that
        remains; the engine measures and publishes, and trading is manual.

        The forming-bar inject stays and matters: it is what makes CM RSI-2 and
        the %R pills move between bar closes instead of freezing for up to a
        minute.
        """
        fh_price = get_latest_price(ts.ticker)
        if fh_price is not None:
            ts.last_price = fh_price

        # Inject the live price as the forming bar's close and recompute, so
        # the dashboard shows current indicator values rather than the last
        # closed bar's.
        if ts.cached_df is not None and ts.last_price is not None:
            # Skip when price has not moved half a cent — the indicators would
            # be unchanged and the copy plus recompute is the expensive part.
            price_moved = (
                ts._last_computed_price is None or
                abs(ts.last_price - ts._last_computed_price) >= 0.005
            )
            if price_moved:
                try:
                    ts._last_computed_price = ts.last_price
                    df_live = ts.cached_df.copy()
                    df_live.at[df_live.index[-1], "close"] = ts.last_price
                    (rsi_live, hist_live, atr_live, vwap_live, rvol_live,
                     cm_rsi_live, obv_live) = compute_indicators(df_live)
                    ts.last_rsi       = rsi_live
                    ts.last_hist      = hist_live
                    if atr_live  > 0: ts.last_atr  = atr_live
                    if vwap_live > 0: ts.last_vwap = vwap_live
                    ts.last_rvol      = rvol_live
                    ts.last_cm_rsi_ok = cm_rsi_live
                    ts.last_obv_ok    = obv_live

                    if ts.bars_fetched and STRATEGY_MODE == "three_indicator":
                        self._eval_three_indicator(
                            ts, self._strategy_df(ts, df_live))
                    elif ts.bars_fetched:
                        # Was gated to hot tickers only, because running it on
                        # the forming bar could fire a false BUY. It cannot buy
                        # anything now, so every tracked name gets live values.
                        ts.update_momentum(hist=hist_live, price=ts.last_price,
                                           rsi=rsi_live)
                except Exception as e:
                    print(f"  [engine] live recompute error for {ts.ticker}: {e}",
                          flush=True)

        ts.check_count += 1
        print(ts.proximity_summary())

    # ── Expiry ────────────────────────────────────────────────────────────────

    def _expire_tickers(self):
        """
        Drop tickers that have been watched too long with no signal.
        Tickers with open positions are never dropped.
        """
        expired = [sym for sym, ts in self.active.items() if ts.is_expired()]
        for sym in expired:
            ts = self.active[sym]
            if ts.cooled_at is not None:
                reason = "went hot but crowd moved on — cooled off with no BUY"
            elif not ts.ever_positive_hist:
                reason = "never showed positive histogram (cold)"
            else:
                reason = "histogram retreated before signal fired (warm)"
            print(
                f"\n  ⏰ EXPIRED {sym}  "
                f"({ts.age_s():.0f}s watched | {reason})\n"
            )
            del self.active[sym]
            # Allow re-adding if the ticker gets mentioned again later
            self._known_mentioned.discard(sym)
            # Free the Finnhub/Alpaca subscription slot toward the ~50 cap.
            request_unsubscribe([sym])

    def _write_signal_state(self):
        """
        Write per-ticker proximity data to signal_state.json.

        Called every SIGNAL_STATE_INTERVAL seconds from the main loop.
        dashboard.py reads this file and merges it into /api/state, which is
        how the desk and the buy circle receive indicator values without
        talking to this process directly. It is the engine's whole output now.

        The trader/risk block it used to publish is gone with the trading:
        there are no engine positions to report, no exposure to cap, and no
        kill switch to trip. Manual trades go through the desk and the
        dashboard, which own their own state.

        `rt_price` / `rt_price_age_sec` are stamped HERE rather than taken
        from proximity_state(), and neither may be replaced by the `price`
        field beside them. Two reasons, both load-bearing:

        1. `price` is TickerState.last_price, and _ingest_state adopts the
           DASHBOARD's price into it whenever the Finnhub stream is quiet or
           frozen — which is most of premarket. Since the dashboard now reads
           this file back as a price source, using `price` here closes a loop:
           dashboard → last_price → this file → dashboard. The number would
           come home wearing the tape's clock.
        2. `bars_age_sec` is cached on the TickerState at bar-evaluation time
           and republished verbatim on every write, so it reports the age as
           of the last eval, not as of now. Recomputed per write below.

        Measured 2026-08-27 premarket, before this: eight symbols held a price
        frozen for over two minutes while the published age ticked 0.2s-1.2s.
        """
        try:
            _now_ms = time.time() * 1000.0
            tickers = {}
            for sym, ts in self.active.items():
                row = ts.proximity_state()
                # The realtime tape's own last print, with its own timestamp,
                # taken as one object. None when the ticker has never traded
                # on this socket — absent, not stale, and the consumer must
                # treat a missing pair as "no desk price", never as fresh.
                lt = self.rt_bars.last_trade(sym) if REALTIME_BARS else None
                if lt is not None:
                    _px, _ts_ms = lt
                    row["rt_price"] = round(float(_px), 4)
                    _age = max(0.0, (_now_ms - float(_ts_ms)) / 1000.0)
                    row["rt_price_age_sec"] = round(_age, 2)
                    # bars_age_sec is the SAME clock, but proximity_state()
                    # hands out the value cached on the TickerState at bar
                    # evaluation, republished unchanged on every write — so it
                    # reports the age as of the last eval, not as of now, and
                    # it only ever understates. It becomes macd_age_sec, which
                    # is what the MACD staleness guard reads.
                    #
                    # Measured 2026-08-27 mid-session, same file, same write:
                    # VNCE published bars_age_sec 0.5s against a trade 639s
                    # old; GRRR 1.1s against 41.8s. Harmless only because
                    # ai_watch_macd_max_age_sec is 0 today — the moment that
                    # ceiling is set it would be read off a number that
                    # understates by three orders of magnitude.
                    if str(row.get("bars_src") or "") == "realtime":
                        row["bars_age_sec"] = round(_age, 1)
                else:
                    row["rt_price"] = None
                    row["rt_price_age_sec"] = None
                tickers[sym] = row
            # Realtime tape coverage. Distinguishes the two failures that look
            # identical downstream — every name refused macd_not_realtime_alpaca:
            #
            #   a quiet name   subscribed, has traded before, just not lately
            #   a starved name subscribed and has NEVER received a trade
            #
            # The second is the Finnhub free-tier limit, which starved this
            # engine for a full session on 2026-08-20 with an ellipsis in a
            # subscribe tag as the only tell. INTC and HPQ sitting on the REST
            # fallback is what raised it again on 08-27: a mega-cap prints
            # thousands of times a minute, so "quiet" cannot explain it.
            rt = {}
            try:
                subs = set(FINNHUB_STATE.subscribed)
                fed = {s for s in subs
                       if self.rt_bars.last_trade_ms(s) is not None}
                fresh = set()
                for s in fed:
                    age = self.rt_bars.age_seconds(s)
                    if age is not None and age <= RT_BARS_MAX_STALE:
                        fresh.add(s)
                rt = {
                    "connected": bool(FINNHUB_STATE.connected),
                    "subscribed": len(subs),
                    "ever_fed": len(fed),
                    "fresh": len(fresh),
                    "max_stale_sec": RT_BARS_MAX_STALE,
                    # Subscribed but never once fed — the starvation signature.
                    "silent": sorted(subs - fed)[:25],
                }
            except Exception:  # noqa: BLE001
                rt = {}
            payload = {
                "updated": _now_iso(),
                "version": version.get_version(),   # which engine build is running
                "started": self._started,           # when this engine booted
                "strategy": STRATEGY_MODE,
                "realtime_tape": rt,
                "tickers": tickers,
            }
            SIGNAL_STATE_FILE.write_text(
                json.dumps(payload, indent=2), encoding="utf-8"
            )
        except Exception as e:
            print(f"[ENGINE] ⚠️  Could not write signal_state.json: {e}")

    # ── Main loop ─────────────────────────────────────────────────────────────

    def run(self):
        print("=" * 60)
        print("  Signal Engine — indicators only, no trading")
        print(f"  Dashboard     : {DASHBOARD_URL}")
        print(f"  Auth user     : {DASHBOARD_USER or '(none)'}")
        print(f"  Price source  : {PRICE_SOURCE}")
        print(f"  Finnhub key   : {'✓ loaded' if self.finnhub_key else '✗ missing (ok if PRICE_SOURCE=alpaca)'}")
        print(f"  Alpaca key    : {'✓ loaded' if self.api_key else '✗ missing'}")
        print(f"  Massive key   : {'✓ loaded — alert tickers use Massive bars first' if self.massive.is_configured() else '✗ not set — using Alpaca only (set MASSIVE_API_KEY)'}")
        print("  Trading       : none — this engine only measures and publishes")
        print(f"  Loop cadence  : every {POLL_INTERVAL}s")
        print(f"  Dashboard poll: every {DASHBOARD_POLL_INTERVAL}s (new ticker detection only)")
        print(f"  Bar refresh   : every {BAR_REFRESH}s (staggered {BAR_STAGGER}s apart)")
        print(f"  Bar lookback  : {BAR_LOOKBACK_DAYS}d steady / {BAR_LOOKBACK_THIN}d first-load "
              f"(fail retry {BAR_FAIL_RETRY_S}s)")
        print(f"  Desk tracking : {'on — all dashboard tickers' if TRACK_DESK_TICKERS else 'off — mentioned/burst/FIF only'}")
        print(f"  Expiry cold   : {EXPIRY_COLD}s (no positive hist seen; held while on desk)")
        print(f"  Expiry warm   : {EXPIRY_WARM}s (positive hist seen at least once)")
        print(f"  Max tickers   : {MAX_ACTIVE_TICKERS}")
        print(f"  RSI buy max   : {RSI_BUY_MAX}  (bypassed when ticker is hot)")
        if PRIORITY_MENTIONS > 0:
            print(f"  Priority tag  : {PRIORITY_MENTIONS}+ mentions in {PRIORITY_WINDOW_SECONDS}s → marks a ticker hot")
        else:
            print("  Priority tag  : disabled (PRIORITY_MENTIONS=0)")
        print(f"  Log file      : {LOG_FILE}")
        print("=" * 60)

        self._ensure_logged_in()
        print("  Watching dashboard for highlighted tickers…\n")

        while True:
            try:
                cycle_start = time.time()

                # 0. Dashboard-requested restart: re-exec with a clean env so
                #    the edited signal_engine.env is re-read. Same PID, so
                #    start_all's process handle and log pipes stay valid.
                if RESTART_FLAG.exists():
                    print("\n[ENGINE] 🔄 restart requested by dashboard — "
                          "re-executing to reload signal_engine.env\n", flush=True)
                    try:
                        RESTART_FLAG.unlink()
                    except Exception:
                        pass
                    self._write_signal_state()
                    for key in _ENV_FILE_KEYS:
                        os.environ.pop(key, None)
                    os.execv(sys.executable, [sys.executable, str(Path(__file__).resolve())])

                # 1. Poll the dashboard only every DASHBOARD_POLL_INTERVAL seconds.
                #    Its sole purpose here is detecting newly highlighted tickers.
                #    Real-time prices come from Finnhub WebSocket.
                now = time.time()
                if now - self._last_poll_time >= DASHBOARD_POLL_INTERVAL:
                    state = self._poll_dashboard()
                    if state:
                        self._ingest_state(state)
                    self._last_poll_time = time.time()

                # 2. For each active ticker:
                #      a) update last_price from Finnhub WebSocket (in-memory)
                #      b) refresh bars if due (rate-limited, staggered)
                #      c) log proximity to signal (every second, in-memory)
                for ts in list(self.active.values()):
                    self._refresh_bars(ts)
                    self._check_proximity(ts)

                # 3. Drop expired tickers
                self._expire_tickers()

                # 4. Write signal_state.json so the dashboard can render
                #    the visual proximity bars (throttled to every SIGNAL_STATE_INTERVAL s)
                if now - self._last_state_write >= SIGNAL_STATE_INTERVAL:
                    self._write_signal_state()
                    self._last_state_write = time.time()

                # 5. Sleep for the remainder of POLL_INTERVAL so we don't
                #    drift — if the above took 0.3 s we sleep 0.7 s.
                elapsed = time.time() - cycle_start
                sleep_for = max(0, POLL_INTERVAL - elapsed)
                if sleep_for < 0.05:
                    # Cycle took longer than POLL_INTERVAL — log a warning
                    # so you know the per-second cadence is slipping
                    print(f"  [ENGINE] ⚠️  cycle took {elapsed:.2f}s "
                          f"(>{POLL_INTERVAL}s) — consider raising POLL_INTERVAL")
                time.sleep(sleep_for)

            except KeyboardInterrupt:
                print("\n  Stopped by user.")
                break
            except Exception as e:
                print(f"[ENGINE] Unexpected error: {e}")
                time.sleep(POLL_INTERVAL)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    SignalEngine().run()
