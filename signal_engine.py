#!/usr/bin/env python3
"""
signal_engine.py — Automated RSI + MACD Signal Engine

Polls the dashboard for highlighted (mentioned) tickers, fetches
historical bars from Alpaca for indicator warmup, then tracks MACD
histogram momentum to fire buy/sell signals.

HOW IT WORKS
────────────
1. Every second, polls the dashboard's /api/state for the live ticker
   list and their current prices.
2. Any ticker with `mentioned = true` (highlighted row on dashboard)
   is added to the active watchlist with a timestamp.
3. Tickers expire after 10 minutes if no position has been taken.
4. Every 60 s, fetches 100 × 1-Min bars from Alpaca for each active
   ticker and recomputes RSI(14) + MACD(12, 26, 9).
5. MACD histogram momentum rules (per ticker):
     • Area growing  → histogram positive AND rising  → safe to BUY
     • Area shrinking → histogram was growing, now falling → time to SELL
6. Logs every BUY and SELL to signal_log.json (and prints to console).

RUN
───
    python signal_engine.py

    The easiest way to configure it is via environment variables:

        DASHBOARD_URL   https://trading.jbrasfield.com   (no trailing slash)
        DASHBOARD_USER  your dashboard login username
        DASHBOARD_PASS  your dashboard login password
        ALPACA_API_KEY  your Alpaca API key
        ALPACA_SECRET_KEY  your Alpaca secret key

    Alternatively, edit the CONFIG DEFAULTS section below.

    Alpaca credentials are also read from secrets.json in the same
    directory (the file the dashboard writes when you hit Save Settings).

CONFIG TWEAKS  (edit the DEFAULTS section below)
─────────────
    DASHBOARD_URL      — dashboard address (no trailing slash)
    DASHBOARD_USER     — dashboard login username
    DASHBOARD_PASS     — dashboard login password
    POLL_INTERVAL      — seconds between price/state polls (default 1)
    BAR_REFRESH        — seconds between Alpaca bar fetches (default 60)
    TICKER_EXPIRY      — seconds before an un-acted ticker is dropped (default 600)
    RSI_PERIOD         — RSI lookback (default 14)
    MACD_FAST/SLOW/SIG — MACD parameters (default 12/26/9)
    RSI_BUY_MAX        — max RSI to allow a buy (default 70)
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

# ── Import our own signal library ─────────────────────────────────────────────
# _HERE resolves to the folder this file lives in (the trading-helper directory).
# We add it to sys.path so Python can find signals.py in the same folder.
_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))
from signals import rsi as calc_rsi, compute_macd   # our custom indicator math

# ── Load signal_engine.env ────────────────────────────────────────────────────
# If signal_engine.env exists in the same folder, we load it before reading
# any configuration.  This lets you keep all settings in one place without
# having to set shell environment variables manually.
#
# Format: KEY=VALUE  (one per line, # for comments, blank lines ignored)
# Values from the env file are only applied if the variable isn't already
# set in the shell environment — shell env always wins.
def _load_env_file(path: Path):
    if not path.exists():
        return
    loaded = []
    with open(path) as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue   # skip blank lines and comments
            if "=" not in line:
                continue   # skip malformed lines
            key, _, value = line.partition("=")
            key   = key.strip()
            value = value.strip()
            # Shell environment takes priority — don't overwrite existing vars
            if key and key not in os.environ:
                os.environ[key] = value
                loaded.append(key)
    if loaded:
        print(f"[ENV] Loaded {len(loaded)} setting(s) from signal_engine.env")

_load_env_file(_HERE / "signal_engine.env")

# ── Configuration — edit these or set via environment variables ────────────────

# Dashboard address — set DASHBOARD_URL env var, or edit this line directly.
# No trailing slash.  Examples:
#   http://localhost:8888          (local)
#   https://trading.jbrasfield.com (remote / hosted)
DASHBOARD_URL  = os.getenv("DASHBOARD_URL",  "https://trading.jbrasfield.com")

# Dashboard login credentials — the same username/password you use in the browser.
# Set via env vars (recommended) or edit below.
DASHBOARD_USER = os.getenv("DASHBOARD_USER", "")
DASHBOARD_PASS = os.getenv("DASHBOARD_PASS", "")

POLL_INTERVAL  = 1     # how often (seconds) we ask the dashboard for new prices
                       # and check for newly highlighted tickers

BAR_REFRESH    = 60    # how often (seconds) we pull fresh price bars from Alpaca
                       # and recompute RSI / MACD for each active ticker

TICKER_EXPIRY  = 600   # 10 minutes — if we added a ticker but never took a
                       # position, we drop it to keep the list focused

BAR_COUNT      = 100   # number of 1-minute bars to fetch per ticker.
                       # MACD(12,26,9) needs at least 35 bars to be meaningful;
                       # 100 gives a comfortable warmup period.

BAR_TIMEFRAME  = "1Min"   # Alpaca bar timeframe string

RSI_PERIOD     = 14    # standard RSI lookback period (Wilder's original)
MACD_FAST      = 12    # MACD fast EMA period
MACD_SLOW      = 26    # MACD slow EMA period
MACD_SIG       = 9     # MACD signal line (EMA of the MACD line)

RSI_BUY_MAX    = 70    # refuse to buy if RSI is above this — stock is overbought

LOG_FILE       = _HERE / "signal_log.json"   # where buy/sell events are saved

# Alpaca REST endpoints
ALPACA_BASE_URL  = "https://data.alpaca.markets"      # market data (bars)
ALPACA_TRADE_URL = "https://paper-api.alpaca.markets"  # trading (paper by default)


# ── Credential loading ────────────────────────────────────────────────────────

def _load_alpaca_credentials() -> tuple[str, str]:
    """
    Return (api_key, secret_key) for Alpaca.

    Priority order:
      1. Environment variables  ALPACA_API_KEY / ALPACA_SECRET_KEY
      2. secrets.json in the same folder (written by the dashboard's
         Settings → Save Settings workflow)

    If neither source has credentials we print a warning and continue.
    Bar fetching will be skipped until credentials appear.
    """
    api_key    = os.getenv("ALPACA_API_KEY", "")
    secret_key = os.getenv("ALPACA_SECRET_KEY", "")

    # Fall back to the shared secrets file the dashboard also uses
    secrets_path = _HERE / "secrets.json"
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
            "       Set ALPACA_API_KEY / ALPACA_SECRET_KEY env vars,\n"
            "       or save them via the dashboard Settings → API Keys tab.\n"
            "       Bar fetching will be skipped until credentials are set."
        )
    return api_key, secret_key


# ── Dashboard authentication ──────────────────────────────────────────────────

def _dashboard_login(user: str, password: str) -> Optional[str]:
    """
    POST to /auth/login and return the Bearer token string, or None on failure.

    The dashboard uses JWT tokens.  We get one at startup and attach it to
    every subsequent request via the Authorization header.  If a request
    ever returns 401 (token expired), the engine will automatically re-login.
    """
    if not user or not password:
        # Auth might not be enabled on this dashboard instance — try without.
        return None

    try:
        resp = requests.post(
            f"{DASHBOARD_URL}/auth/login",
            json={"username": user, "password": password},
            timeout=10,
        )
        data = resp.json()
        if resp.status_code == 200 and data.get("ok"):
            token = data.get("token", "")
            print(f"[AUTH] Logged in as '{user}' ✓")
            return token
        else:
            print(f"[AUTH] Login failed: {data.get('error', resp.status_code)}")
            return None
    except Exception as e:
        print(f"[AUTH] Login error: {e}")
        return None


# ── Alpaca bar fetching ───────────────────────────────────────────────────────

def fetch_bars(symbol: str, api_key: str, secret_key: str,
               count: int = BAR_COUNT,
               timeframe: str = BAR_TIMEFRAME) -> Optional[pd.DataFrame]:
    """
    Download recent price bars (candlesticks) from Alpaca's market data API.

    A "bar" is one row of OHLCV data: Open, High, Low, Close, Volume for a
    given time bucket (here 1 minute).  We need these historical bars so that
    RSI and MACD have enough data to produce meaningful numbers — the first
    ~35 bars are the "warmup" period where the indicators stabilise.

    Returns a pandas DataFrame with columns:
        open, high, low, close, volume
    or None if the request failed or returned no data.
    """
    if not api_key or not secret_key:
        return None   # no point trying — we'd get a 403

    url = f"{ALPACA_BASE_URL}/v2/stocks/{symbol}/bars"
    params = {
        "timeframe": timeframe,   # e.g. "1Min"
        "limit":     count,       # how many bars to return
        "feed":      "iex",       # IEX feed is included in the free Alpaca tier
        "sort":      "asc",       # oldest-first so our DataFrames are in order
    }
    headers = {
        "APCA-API-KEY-ID":     api_key,
        "APCA-API-SECRET-KEY": secret_key,
    }
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        resp.raise_for_status()   # raises if HTTP status is 4xx / 5xx

        bars = resp.json().get("bars", [])
        if not bars:
            return None   # market may be closed or symbol not recognised

        # Alpaca returns single-letter column names; rename them to be readable
        df = pd.DataFrame(bars)
        df = df.rename(columns={
            "o": "open",
            "h": "high",
            "l": "low",
            "c": "close",
            "v": "volume",
            "t": "time",
        })
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = df[col].astype(float)

        return df.reset_index(drop=True)

    except Exception as e:
        print(f"[BARS] {symbol}: fetch failed — {e}")
        return None


# ── Indicator computation ─────────────────────────────────────────────────────

def compute_indicators(df: pd.DataFrame) -> tuple[float, float]:
    """
    Compute RSI and MACD histogram on a bar DataFrame and return the
    latest values.

    RSI (Relative Strength Index):
        Measures how overbought or oversold a stock is on a 0–100 scale.
        Below 30 = oversold (potential bounce), above 70 = overbought
        (potential pullback).  We use it as a safety filter — we won't
        buy if RSI is already above RSI_BUY_MAX.

    MACD histogram:
        MACD line = EMA(12) − EMA(26) of the close price.
        Signal line = EMA(9) of the MACD line.
        Histogram = MACD line − signal line.

        When the histogram is positive and rising, momentum is accelerating
        upward.  When it starts falling, momentum is fading — time to sell.

    Returns:
        (rsi_value, macd_hist_value)  — both are floats for the last bar.
    """
    cfg = {
        "macd_fast":   MACD_FAST,   # 12
        "macd_slow":   MACD_SLOW,   # 26
        "macd_signal": MACD_SIG,    # 9
    }
    df = compute_macd(df, cfg)   # adds macd_line, macd_signal_line, macd_hist columns

    rsi_series = calc_rsi(df["close"], RSI_PERIOD)
    rsi_val    = float(rsi_series.iloc[-1])
    hist_val   = float(df["macd_hist"].iloc[-1])
    return rsi_val, hist_val


# ── Trade logging ─────────────────────────────────────────────────────────────

def _now_iso() -> str:
    """Current UTC time as an ISO-8601 string, e.g. '2025-06-01T14:32:00Z'."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_log() -> list:
    """Read the existing signal log (list of buy/sell dicts) or return []."""
    if LOG_FILE.exists():
        try:
            return json.loads(LOG_FILE.read_text())
        except Exception:
            pass
    return []


def _append_log(entry: dict):
    """Append one entry to signal_log.json, creating the file if needed."""
    entries = _load_log()
    entries.append(entry)
    LOG_FILE.write_text(json.dumps(entries, indent=2))


def log_buy(ticker: str, price: float, rsi: float, hist: float):
    """Record a BUY signal — writes to signal_log.json and prints to console."""
    ts = _now_iso()
    entry = {
        "action":    "BUY",
        "ticker":    ticker,
        "price":     round(price, 4),
        "rsi":       round(rsi,   2),
        "macd_hist": round(hist, 6),
        "time":      ts,
    }
    _append_log(entry)
    print(f"  🟢 BUY  {ticker:6s}  ${price:.2f}  RSI={rsi:.1f}  hist={hist:+.4f}  [{ts}]")


def log_sell(ticker: str, price: float, buy_price: float,
             rsi: float, hist: float, buy_time: str):
    """Record a SELL signal — includes P&L % vs the buy price."""
    ts  = _now_iso()
    pnl = round((price - buy_price) / buy_price * 100, 2)
    entry = {
        "action":    "SELL",
        "ticker":    ticker,
        "price":     round(price,     4),
        "buy_price": round(buy_price, 4),
        "pnl_pct":   pnl,
        "rsi":       round(rsi,  2),
        "macd_hist": round(hist, 6),
        "time":      ts,
        "buy_time":  buy_time,
    }
    _append_log(entry)
    sign = "+" if pnl >= 0 else ""
    print(f"  🔴 SELL {ticker:6s}  ${price:.2f}  P&L={sign}{pnl}%  hist={hist:+.4f}  [{ts}]")


# ── Per-ticker state ──────────────────────────────────────────────────────────

class TickerState:
    """
    Tracks everything the engine needs to know about one active ticker.

    Lifecycle:
        created when the ticker first gets highlighted on the dashboard
        → bars fetched and indicators computed every BAR_REFRESH seconds
        → BUY fired when MACD histogram is positive and growing
        → SELL fired when histogram reverses (was growing, now shrinking)
        → expired and removed if TICKER_EXPIRY seconds pass with no position
    """

    def __init__(self, ticker: str):
        self.ticker      = ticker
        self.added_ts    = time.time()   # wall-clock timestamp when added
        self.in_position = False         # True after a BUY, False after a SELL

        # Position tracking — populated when we BUY
        self.buy_price: Optional[float] = None
        self.buy_time:  Optional[str]   = None

        # MACD momentum tracking
        self.prev_hist:    Optional[float] = None  # histogram from last evaluation
        self.hist_growing: bool            = False # True while histogram is positive & rising

        # Latest market data (updated each poll cycle)
        self.last_rsi:   Optional[float] = None
        self.last_price: Optional[float] = None
        self.last_bar_fetch: float       = 0.0    # epoch time of last Alpaca bar pull

    def is_expired(self) -> bool:
        """
        Returns True if we've been watching this ticker longer than
        TICKER_EXPIRY without taking a position.  Positions are never
        expired — we hold until the SELL signal fires.
        """
        return (not self.in_position) and (time.time() - self.added_ts > TICKER_EXPIRY)

    def update_momentum(self, hist: float, price: float, rsi: float):
        """
        Core momentum logic — called after each bar refresh.

          Histogram positive AND rising:
            Fast line above signal line and pulling away — accelerating uptrend.
            → set hist_growing = True
            → fire BUY if not already in a position and RSI is acceptable

          Histogram was growing, now falling:
            Fast line still above signal line but heading back toward it.
            Momentum fading — exit signal.
            → fire SELL if we're in a position
            → reset hist_growing = False
        """
        self.last_price = price
        self.last_rsi   = rsi

        prev = self.prev_hist

        if prev is not None:
            # Histogram growing: positive and larger than last bar
            if hist > 0 and hist > prev:
                self.hist_growing = True

            # Reversal: was growing, now shrinking
            elif self.hist_growing and hist < prev:
                if self.in_position:
                    log_sell(
                        ticker    = self.ticker,
                        price     = price,
                        buy_price = self.buy_price,
                        rsi       = rsi,
                        hist      = hist,
                        buy_time  = self.buy_time,
                    )
                    self.in_position = False
                    self.buy_price   = None
                    self.buy_time    = None
                self.hist_growing = False

        # BUY conditions: growing momentum + not already in + RSI not overbought
        if (
            self.hist_growing
            and not self.in_position
            and rsi < RSI_BUY_MAX
            and price > 0
        ):
            log_buy(
                ticker = self.ticker,
                price  = price,
                rsi    = rsi,
                hist   = hist,
            )
            self.in_position = True
            self.buy_price   = price
            self.buy_time    = _now_iso()

        self.prev_hist = hist


# ── Main engine ───────────────────────────────────────────────────────────────

class SignalEngine:
    """
    Orchestrates the polling loop:
      - Logs into the dashboard at startup (if credentials are configured)
      - Fetches dashboard state every POLL_INTERVAL seconds
      - Builds and maintains the active ticker list from highlighted rows
      - Refreshes bars + indicators every BAR_REFRESH seconds per ticker
      - Retires expired tickers
    """

    def __init__(self):
        self.api_key, self.secret_key = _load_alpaca_credentials()

        # Dashboard auth token — None means either auth is disabled or
        # we haven't logged in yet.  Set at startup, refreshed on 401.
        self._token: Optional[str] = None

        # sym → TickerState for every ticker we're currently watching
        self.active: dict[str, TickerState] = {}

        # Tracks which mentioned tickers we've already added so we don't
        # create duplicate TickerState objects on every poll cycle
        self._known_mentioned: set[str] = set()

    def _auth_headers(self) -> dict:
        """Return HTTP headers including the Bearer token if we have one."""
        if self._token:
            return {"Authorization": f"Bearer {self._token}"}
        return {}

    def _ensure_logged_in(self):
        """Log in to the dashboard if we have credentials and no token yet."""
        if not self._token and DASHBOARD_USER and DASHBOARD_PASS:
            self._token = _dashboard_login(DASHBOARD_USER, DASHBOARD_PASS)

    # ── Dashboard polling ─────────────────────────────────────────────────────

    def _poll_dashboard(self) -> Optional[dict]:
        """
        GET /api/state from the dashboard server.

        Attaches the Bearer token so the request is authorised.
        If the server returns 401 (token expired), automatically re-logs
        in and retries once.

        Returns the parsed JSON dict, or None if unreachable.
        """
        for attempt in range(2):   # up to 2 tries (second try after re-login)
            try:
                resp = requests.get(
                    f"{DASHBOARD_URL}/api/state",
                    headers=self._auth_headers(),
                    timeout=5,
                )

                if resp.status_code == 401:
                    # Token expired or not set — try to log in and retry
                    print("[AUTH] 401 received — re-authenticating…")
                    self._token = _dashboard_login(DASHBOARD_USER, DASHBOARD_PASS)
                    continue   # retry with new token

                resp.raise_for_status()
                return resp.json()

            except requests.exceptions.ConnectionError:
                if attempt == 0:
                    print(f"[POLL] Cannot reach {DASHBOARD_URL} — retrying…")
                return None
            except Exception as e:
                print(f"[POLL] Error: {e}")
                return None

        return None   # both attempts failed

    def _ingest_state(self, state: dict):
        """
        Process one snapshot from the dashboard.

        For each ticker row in the snapshot:
          • Update its last-known price if it's already in our active list.
          • If it's marked 'mentioned' (highlighted on the dashboard) and
            we haven't seen it before, add it to the active list.
        """
        tickers = state.get("tickers", [])

        for row in tickers:
            sym       = row.get("ticker", "")
            price     = row.get("price")
            mentioned = row.get("mentioned", False)

            if not sym:
                continue

            # Keep price current for already-tracked tickers
            if sym in self.active and price is not None:
                self.active[sym].last_price = float(price)

            # 'mentioned' = ticker was spoken in the last 30 s of transcript.
            # We add it once and keep watching even after the mention window passes.
            if mentioned and sym not in self._known_mentioned:
                self._known_mentioned.add(sym)
                if sym not in self.active:
                    self.active[sym] = TickerState(sym)
                    age_mins = TICKER_EXPIRY // 60
                    print(
                        f"  ➕ Added  {sym}  to active list "
                        f"(expires in {age_mins} min if no position)"
                    )

    # ── Bar refresh + signal evaluation ──────────────────────────────────────

    def _refresh_ticker(self, ts: TickerState):
        """
        If this ticker is due for a bar refresh, pull fresh data from Alpaca,
        recompute RSI + MACD, and pass the results to update_momentum().

        Rate-limited to BAR_REFRESH seconds per ticker to avoid hammering
        the Alpaca API.
        """
        now = time.time()
        if now - ts.last_bar_fetch < BAR_REFRESH:
            return   # not due yet

        df = fetch_bars(ts.ticker, self.api_key, self.secret_key)

        # Need at least MACD_SLOW + MACD_SIG + a few extra bars for stability
        if df is None or len(df) < MACD_SLOW + MACD_SIG + 5:
            ts.last_bar_fetch = now
            return

        try:
            rsi_val, hist_val = compute_indicators(df)
        except Exception as e:
            print(f"[SIG] {ts.ticker}: indicator error — {e}")
            ts.last_bar_fetch = now
            return

        # Prefer live price from dashboard; fall back to last bar's close
        price = ts.last_price if ts.last_price is not None else float(df["close"].iloc[-1])

        ts.last_bar_fetch = now
        ts.update_momentum(hist=hist_val, price=price, rsi=rsi_val)

    # ── Expiry cleanup ────────────────────────────────────────────────────────

    def _expire_tickers(self):
        """
        Remove tickers that have been in the active list longer than
        TICKER_EXPIRY without triggering a BUY.  Open positions are
        never expired — we hold until the SELL signal fires.
        """
        expired = [sym for sym, ts in self.active.items() if ts.is_expired()]
        for sym in expired:
            print(f"  ⏰ Expired {sym}  (no position taken within {TICKER_EXPIRY // 60} min)")
            del self.active[sym]
            self._known_mentioned.discard(sym)

    # ── Main loop ─────────────────────────────────────────────────────────────

    def run(self):
        """
        Start the engine.  Runs forever until Ctrl-C.

        Each iteration (every POLL_INTERVAL seconds):
          1. Poll the dashboard for the latest ticker state + prices
          2. Add any newly highlighted tickers to the active list
          3. For each active ticker, check if bars need refreshing and
             evaluate buy/sell signals
          4. Drop tickers that have expired without a position
        """
        print("=" * 60)
        print("  Signal Engine — RSI + MACD Momentum")
        print(f"  Dashboard : {DASHBOARD_URL}")
        print(f"  Auth user : {DASHBOARD_USER or '(none — auth may be disabled)'}")
        print(f"  Log file  : {LOG_FILE}")
        print(f"  Expiry    : {TICKER_EXPIRY // 60} min  |  Bar refresh: {BAR_REFRESH}s")
        print(f"  Alpaca key: {'✓ loaded' if self.api_key else '✗ missing'}")
        print("=" * 60)

        # Log in before the first poll
        self._ensure_logged_in()

        print("  Waiting for highlighted tickers on dashboard…\n")

        while True:
            try:
                # Step 1 + 2: get dashboard snapshot, update active list
                state = self._poll_dashboard()
                if state:
                    self._ingest_state(state)

                # Step 3: evaluate signals for all active tickers
                for ts in list(self.active.values()):
                    self._refresh_ticker(ts)

                # Step 4: retire stale tickers
                self._expire_tickers()

                time.sleep(POLL_INTERVAL)

            except KeyboardInterrupt:
                print("\n  Stopped by user.")
                break
            except Exception as e:
                print(f"[ENGINE] Unexpected error: {e}")
                time.sleep(POLL_INTERVAL)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    SignalEngine().run()
