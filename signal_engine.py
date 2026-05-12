#!/usr/bin/env python3
"""
signal_engine.py — Automated RSI + MACD Signal Engine

Polls the dashboard for highlighted (mentioned) tickers, fetches
historical bars from Alpaca for indicator warmup, then tracks MACD
histogram momentum to fire buy/sell signals.

HOW IT WORKS
────────────
1. Every 5 s (DASHBOARD_POLL_INTERVAL), polls the dashboard's /api/state
   to detect newly highlighted tickers.  Price data from the dashboard
   is used only as a fallback — real-time prices come from Finnhub.

2. Any ticker with `mentioned = true` (highlighted row on dashboard)
   is added to the active watchlist (capped at MAX_ACTIVE_TICKERS) AND
   immediately subscribed to the Finnhub WebSocket for live prices.

3. Finnhub WebSocket provides real-time trade prices during market hours
   (free tier, up to 50 symbols).  get_latest_price() is called each
   second to keep last_price current — no extra network round-trips.

4. Every second, logs each active ticker's proximity to the buy signal.
   RSI and MACD histogram are recomputed each second by injecting the
   live Finnhub price as the forming bar's close into the cached bar
   DataFrame — so indicators reflect real-time price movement rather
   than being frozen until the next closed bar arrives.

5. Once per clock minute (aligned to the minute boundary + stagger),
   fetches up to 200 × 1-Min bars from Alpaca per active ticker.
   The freshly closed bar locks in confirmed RSI/MACD values and runs
   the BUY/SELL signal check.  Bar fetches are staggered so tickers
   don't all hit Alpaca at the same second.

6. Expiry:
     • 3 min  (EXPIRY_COLD)   — ticker never showed a positive histogram
     • 10 min (EXPIRY_WARM)   — ticker showed positive hist at least once
       (we give it more time since it came close to a signal)

7. MACD histogram momentum rules (per ticker):
     • Area growing  → hist positive AND rising  → safe to BUY
     • Area shrinking → hist was growing, now falling → time to SELL

8. Logs every BUY and SELL to signal_log.json (and prints to console).

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
  • Active list is capped at MAX_ACTIVE_TICKERS (default 20, also
    Finnhub free-tier sub limit is 50 — well within range).

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
_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))
from signals import rsi as calc_rsi, compute_macd

# ── Import Alpaca trader module (optional — activated by TRADER_MODE) ─────────
import alpaca_trader

# ── Import Finnhub WebSocket stream ───────────────────────────────────────────
from finnhub_stream import (
    start_finnhub_stream,
    request_subscribe,
    get_latest_price,
    FINNHUB_STATE,
)

# ── Load signal_engine.env ────────────────────────────────────────────────────
def _load_env_file(path: Path):
    """
    Parse KEY=VALUE lines from an env file and inject into os.environ.
    Shell environment always wins — we only set keys that aren't already set.
    """
    if not path.exists():
        return
    loaded = []
    with open(path, encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if key and key not in os.environ:
                os.environ[key] = value
                loaded.append(key)
    if loaded:
        print(f"[ENV] Loaded {len(loaded)} setting(s) from signal_engine.env")

_load_env_file(_HERE / "signal_engine.env")

# ── Configuration ─────────────────────────────────────────────────────────────

DASHBOARD_URL  = os.getenv("DASHBOARD_URL",  "https://trading.jbrasfield.com")
DASHBOARD_USER = os.getenv("DASHBOARD_USER", "")
DASHBOARD_PASS = os.getenv("DASHBOARD_PASS", "")

POLL_INTERVAL          = int(os.getenv("POLL_INTERVAL",          "1"))   # main loop cadence (seconds)
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

MAX_ACTIVE_TICKERS = int(os.getenv("MAX_ACTIVE_TICKERS", "20"))  # hard cap on active list

BAR_COUNT          = int(os.getenv("BAR_COUNT",         "200"))  # max bars to fetch
BAR_LOOKBACK_DAYS  = int(os.getenv("BAR_LOOKBACK_DAYS",  "5"))   # calendar days back to start from
BAR_TIMEFRAME  = os.getenv("BAR_TIMEFRAME",  "1Min")

RSI_PERIOD     = int(os.getenv("RSI_PERIOD",   "14"))
MACD_FAST      = int(os.getenv("MACD_FAST",    "12"))
MACD_SLOW      = int(os.getenv("MACD_SLOW",    "26"))
MACD_SIG       = int(os.getenv("MACD_SIG",     "9"))
RSI_BUY_MAX    = int(os.getenv("RSI_BUY_MAX",  "70"))

STOP_LOSS      = float(os.getenv("STOP_LOSS",   "0.0"))   # % e.g. 1.0
TAKE_PROFIT    = float(os.getenv("TAKE_PROFIT", "0.0"))   # % e.g. 2.0

# ── Trader mode ───────────────────────────────────────────────────────────────
# Controls whether the signal engine places orders or just logs signals.
#   off   — log signals only, no orders (default, safe)
#   paper — paper trade via Alpaca ($100k fake money, same API keys)
#   live  — real money via Alpaca (test on paper first!)
TRADER_MODE   = os.getenv("TRADER_MODE",   "off").lower().strip()
TRADE_AMOUNT  = float(os.getenv("TRADE_AMOUNT", "500"))   # $ per BUY signal

LOG_FILE       = _HERE / "signal_log.json"

ALPACA_BASE_URL = "https://data.alpaca.markets"


# ── Credential loading ────────────────────────────────────────────────────────

def _load_alpaca_credentials() -> tuple[str, str]:
    api_key    = os.getenv("ALPACA_API_KEY", "")
    secret_key = os.getenv("ALPACA_SECRET_KEY", "")
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
            "       Set ALPACA_API_KEY / ALPACA_SECRET_KEY in signal_engine.env\n"
            "       or via the dashboard Settings → API Keys tab.\n"
            "       Bar fetching will be skipped until credentials are set."
        )
    return api_key, secret_key


def _load_finnhub_key() -> str:
    """
    Load the Finnhub API key.
    Priority: FINNHUB_API_KEY env var → secrets.json 'finnhub_key' field.
    Returns empty string if not found (Finnhub stream will be skipped).
    """
    key = os.getenv("FINNHUB_API_KEY", "")
    if not key:
        secrets_path = _HERE / "secrets.json"
        if secrets_path.exists():
            try:
                s   = json.loads(secrets_path.read_text())
                key = s.get("finnhub_key", "")
            except Exception as e:
                print(f"[CFG] Could not read secrets.json for Finnhub key: {e}")
    if not key:
        print(
            "[CFG] WARNING: FINNHUB_API_KEY not set.\n"
            "       Real-time prices will fall back to dashboard poll values.\n"
            "       Set FINNHUB_API_KEY in signal_engine.env to enable the stream."
        )
    return key


# ── Dashboard authentication ──────────────────────────────────────────────────

def _dashboard_login(user: str, password: str) -> Optional[str]:
    """POST /auth/login and return the Bearer token, or None on failure."""
    if not user or not password:
        return None
    try:
        resp = requests.post(
            f"{DASHBOARD_URL}/auth/login",
            json={"username": user, "password": password},
            timeout=10,
        )
        data = resp.json()
        if resp.status_code == 200 and data.get("ok"):
            print(f"[AUTH] Logged in as '{user}' ✓")
            return data.get("token", "")
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
    Download recent OHLCV bars from Alpaca going back BAR_LOOKBACK_DAYS
    calendar days.  Using a start date instead of relying on limit alone
    ensures we always get bars from multiple sessions — critical for tickers
    that are thinly traded or when the market has only been open a few minutes.

    Strategy:
      1. Try the IEX feed first (free tier).
      2. If IEX returns fewer bars than we need for warmup, retry with the
         SIP feed (broader coverage, also available on free paper accounts).
    """
    if not api_key or not secret_key:
        return None

    headers = {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret_key}
    url     = f"{ALPACA_BASE_URL}/v2/stocks/{symbol}/bars"

    # Go back BAR_LOOKBACK_DAYS calendar days — covers weekends + holidays
    from datetime import datetime, timedelta, timezone as _tz
    start_dt = (datetime.now(_tz.utc) - timedelta(days=BAR_LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")

    min_needed = MACD_SLOW + MACD_SIG + 5   # 40 bars minimum for stable MACD

    for feed in ("iex", "sip"):
        params = {
            "timeframe": timeframe,
            "start":     start_dt,   # fetch from N days ago, not just today
            "limit":     count,
            "feed":      feed,
            "sort":      "asc",
        }
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=10)
            resp.raise_for_status()
            bars = resp.json().get("bars", [])

            if not bars:
                print(f"  [BARS] {symbol}: no bars on {feed} feed (start={start_dt[:10]})")
                continue

            df = pd.DataFrame(bars).rename(columns={
                "o": "open", "h": "high", "l": "low",
                "c": "close", "v": "volume", "t": "time",
            })
            for col in ("open", "high", "low", "close", "volume"):
                df[col] = df[col].astype(float)
            df = df.reset_index(drop=True)

            if len(df) >= min_needed:
                print(f"  [BARS] {symbol}: {len(df)} bars via {feed} ✓")
                return df

            # Not enough yet — try the next feed
            print(f"  [BARS] {symbol}: only {len(df)} bars on {feed} "
                  f"(need {min_needed}) — trying next feed…")

        except Exception as e:
            print(f"  [BARS] {symbol}: {feed} fetch failed — {e}")

    print(f"  [BARS] {symbol}: ❌ could not get {min_needed} bars on any feed "
          f"(lookback={BAR_LOOKBACK_DAYS}d) — skipping this cycle")
    return None


# ── Indicator computation ─────────────────────────────────────────────────────

def compute_indicators(df: pd.DataFrame) -> tuple[float, float]:
    """Run RSI and MACD on a bar DataFrame. Returns (rsi, macd_hist) for last bar."""
    cfg = {"macd_fast": MACD_FAST, "macd_slow": MACD_SLOW, "macd_signal": MACD_SIG}
    df  = compute_macd(df, cfg)
    rsi_val  = float(calc_rsi(df["close"], RSI_PERIOD).iloc[-1])
    hist_val = float(df["macd_hist"].iloc[-1])
    return rsi_val, hist_val


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

def _append_log(entry: dict):
    entries = _load_log()
    entries.append(entry)
    LOG_FILE.write_text(json.dumps(entries, indent=2), encoding="utf-8")

def log_buy(ticker: str, price: float, rsi: float, hist: float):
    ts = _now_iso()
    _append_log({"action": "BUY", "ticker": ticker,
                 "price": round(price, 4), "rsi": round(rsi, 2),
                 "macd_hist": round(hist, 6), "time": ts})
    print(f"\n  {'='*56}")
    print(f"  🟢 BUY  {ticker}  ${price:.2f}  RSI={rsi:.1f}  hist={hist:+.4f}  [{ts}]")
    print(f"  {'='*56}\n")
    # Forward to Alpaca trader if enabled (off / paper / live)
    alpaca_trader.buy(ticker=ticker, price=price, rsi=rsi, hist=hist)


def log_sell(ticker: str, price: float, buy_price: float,
             rsi: float, hist: float, buy_time: str, reason: str = "reversal"):
    ts  = _now_iso()
    pnl = round((price - buy_price) / buy_price * 100, 2)
    _append_log({"action": "SELL", "ticker": ticker,
                 "price": round(price, 4), "buy_price": round(buy_price, 4),
                 "pnl_pct": pnl, "rsi": round(rsi, 2), "reason": reason,
                 "macd_hist": round(hist, 6), "time": ts, "buy_time": buy_time})
    sign = "+" if pnl >= 0 else ""
    print(f"\n  {'='*56}")
    print(f"  🔴 SELL {ticker}  ${price:.2f}  P&L={sign}{pnl}%  [{reason}]  hist={hist:+.4f}  [{ts}]")
    print(f"  {'='*56}\n")
    # Forward to Alpaca trader if enabled
    alpaca_trader.sell(ticker=ticker, price=price, rsi=rsi, hist=hist,
                       buy_price=buy_price)


# ── Per-ticker state ──────────────────────────────────────────────────────────

class TickerState:
    """
    All state tracked for one active ticker.

    Expiry rules:
      • Never saw a positive histogram (EXPIRY_COLD = 3 min) — probably not moving
      • Saw positive histogram at least once (EXPIRY_WARM = 10 min) — came close, give it time
      • In a position — never expires, held until SELL fires
    """

    def __init__(self, ticker: str, fetch_offset_s: float = 0):
        self.ticker      = ticker
        self.fetch_offset_s = fetch_offset_s
        self.added_ts    = time.time()
        self.in_position = False

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

    def expiry_seconds(self) -> int:
        """How long this ticker is allowed to live without a position."""
        return EXPIRY_WARM if self.ever_positive_hist else EXPIRY_COLD

    def age_s(self) -> float:
        """Seconds since this ticker was added."""
        return time.time() - self.added_ts

    def time_left_s(self) -> float:
        """Seconds remaining before expiry (if no position)."""
        return max(0.0, self.expiry_seconds() - self.age_s())

    def is_expired(self) -> bool:
        if self.in_position:
            return False   # never expire an open position
        return self.age_s() > self.expiry_seconds()

    def proximity_summary(self) -> str:
        """
        One-line human-readable summary of how close this ticker is to a BUY signal.
        Printed every second so you can watch conditions converge in real time.
        """
        age   = self.age_s()
        left  = self.time_left_s()
        check = self.check_count

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

        rsi_str  = f"RSI={rsi:.1f}"   if rsi  is not None else "RSI=?"
        hist_str = f"hist={hist:+.4f}" if hist is not None else "hist=?"
        price_str = f"${price:.2f}" if price is not None else "$?"

        # Condition indicators
        rsi_tag  = "✓" if rsi_ok   else f"✗(need<{RSI_BUY_MAX})"
        hist_tag = "✓pos" if hist_pos else "✗neg"
        grow_tag = "✓growing🔥" if growing else ("→flat" if hist_pos else "↓")

        # Overall status
        if self.in_position:
            pnl = (price - self.buy_price) / self.buy_price * 100
            status = f"📈 IN POSITION — P&L: {pnl:+.2f}%"
        elif growing and rsi_ok:
            status = "🔥 BUY ZONE — signal imminent"
        elif growing and not rsi_ok:
            status = "⚠️  growing but RSI overbought — holding off"
        elif hist_pos and not growing:
            status = "👀 hist positive — watching for growth"
        elif self.ever_positive_hist:
            status = "↩️  retreated — was positive, now negative"
        else:
            status = "😴 no signal yet"

        return (
            f"  [{ticker_tag(self.ticker)}] {price_str}  "
            f"{rsi_str} {rsi_tag}  "
            f"{hist_str} {hist_tag} {grow_tag}  "
            f"age={age:.0f}s ttl={left:.0f}s  "
            f"#{check}  {status}"
        )

    def update_momentum(self, hist: float, price: float, rsi: float):
        """
        Apply one indicator reading — check for BUY/SELL signals and update state.
        Called by _refresh_ticker() after each bar fetch.
        """
        self.last_price = price
        self.last_rsi   = rsi
        self.last_hist  = hist

        if hist > 0:
            self.ever_positive_hist = True

        # ── Stop-loss / take-profit check ─────────────────────────────────────
        if self.in_position and self.buy_price is not None:
            pnl_pct = (price - self.buy_price) / self.buy_price * 100
            
            reason = None
            if STOP_LOSS > 0 and pnl_pct <= -STOP_LOSS:
                reason = "STOP LOSS"
            elif TAKE_PROFIT > 0 and pnl_pct >= TAKE_PROFIT:
                reason = "TAKE PROFIT"
            
            if reason:
                log_sell(
                    ticker    = self.ticker,
                    price     = price,
                    buy_price = self.buy_price,
                    rsi       = rsi,
                    hist      = hist,
                    buy_time  = self.buy_time,
                    reason    = reason
                )
                self.in_position  = False
                self.buy_price    = None
                self.buy_time     = None
                self.hist_growing = False
                self.prev_hist    = hist
                return

        prev = self.prev_hist

        if prev is not None:
            # Histogram growing: positive and larger than last read
            if hist > 0 and hist > prev:
                if not self.hist_growing:
                    print(f"  [{ticker_tag(self.ticker)}] 📈 histogram started GROWING"
                          f"  hist={hist:+.4f} (was {prev:+.4f})"
                          f"  RSI={rsi:.1f}  price={price:.2f}")
                self.hist_growing = True

            # Reversal: was growing, now shrinking
            elif self.hist_growing and hist < prev:
                print(f"  [{ticker_tag(self.ticker)}] 📉 histogram REVERSING"
                      f"  hist={hist:+.4f} (was {prev:+.4f})"
                      f"  RSI={rsi:.1f}  price={price:.2f}")
                if self.in_position:
                    log_sell(
                        ticker    = self.ticker,
                        price     = price,
                        buy_price = self.buy_price,
                        rsi       = rsi,
                        hist      = hist,
                        buy_time  = self.buy_time,
                        reason    = "reversal"
                    )
                    self.in_position = False
                    self.buy_price   = None
                    self.buy_time    = None
                self.hist_growing = False

        # BUY check
        if self.hist_growing and not self.in_position and rsi < RSI_BUY_MAX and price > 0:
            log_buy(ticker=self.ticker, price=price, rsi=rsi, hist=hist)
            self.in_position = True
            self.buy_price   = price
            self.buy_time    = _now_iso()

        self.prev_hist = hist


def ticker_tag(sym: str) -> str:
    """Fixed-width ticker label for aligned log output."""
    return sym.ljust(6)


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

        self._token: Optional[str] = None
        self.active: dict[str, TickerState] = {}       # sym → TickerState
        self._known_mentioned: set[str] = set()        # ever-seen mentioned syms
        self._stagger_index: int = 0                   # increments per added ticker
        self._last_poll_time: float = 0.0              # last time we hit /api/state

        # Initialise Alpaca trader (off / paper / live — set by TRADER_MODE)
        alpaca_trader.init(
            mode         = TRADER_MODE,
            api_key      = self.api_key,
            secret_key   = self.secret_key,
            trade_amount = TRADE_AMOUNT,
        )

        # Start the Finnhub WebSocket in a background thread.
        # It will subscribe to tickers as they are added to the active list.
        if self.finnhub_key:
            start_finnhub_stream(self.finnhub_key, tickers=[])
            print("[FH] Finnhub WebSocket stream started — subscribing tickers as they appear")

    def _auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    def _ensure_logged_in(self):
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
        - Add newly mentioned tickers and immediately subscribe them to Finnhub.
        """
        for row in state.get("tickers", []):
            sym   = row.get("ticker", "")
            price = row.get("price")
            if not sym:
                continue

            # Use dashboard price as a fallback if Finnhub hasn't seen a trade yet
            if sym in self.active and price is not None:
                fh_price = get_latest_price(sym)
                if fh_price is None:
                    # No Finnhub reading yet — use dashboard value as seed
                    self.active[sym].last_price = float(price)

            # Add newly mentioned tickers
            if row.get("mentioned") and sym not in self._known_mentioned:
                self._known_mentioned.add(sym)
                if sym in self.active:
                    continue  # already tracking

                if len(self.active) >= MAX_ACTIVE_TICKERS:
                    print(f"  [ENGINE] ⚠️  active list full ({MAX_ACTIVE_TICKERS}) "
                          f"— skipping {sym}")
                    continue

                # Stagger bar fetches: ticker #0 fetches now, #1 in BAR_STAGGER s,
                # #2 in 2×BAR_STAGGER s, etc.  Prevents a burst of Alpaca calls
                # when several tickers are mentioned close together.
                offset = self._stagger_index * BAR_STAGGER
                self._stagger_index = (self._stagger_index + 1) % MAX_ACTIVE_TICKERS

                self.active[sym] = TickerState(sym, fetch_offset_s=offset)
                expiry_tag = "3min" if not self.active[sym].ever_positive_hist else "10min"
                print(
                    f"\n  ➕ ADDED {sym}  "
                    f"(bar fetch in ~{offset}s | expiry {expiry_tag} if no signal)\n"
                )

                # Subscribe to Finnhub WebSocket for real-time trade prices
                if self.finnhub_key:
                    request_subscribe([sym])
                    fh_status = "✓ subscribed to Finnhub" if FINNHUB_STATE.connected else "⏳ queued (stream connecting)"
                    print(f"  [FH]  {sym} — {fh_status}")

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
        current_minute   = int(now // 60)               # ever-increasing minute counter
        secs_past_minute = dt.second + dt.microsecond / 1_000_000  # 0–59.999

        if ts.last_bar_minute == -1:
            # ── First fetch: respect the stagger timer ────────────────────
            if now - ts.last_bar_fetch < BAR_REFRESH:
                return
        else:
            # ── Subsequent fetches: wait for next minute boundary ─────────
            # Fire 5 s into the new minute + this ticker's stagger offset.
            # E.g. stagger=0 → fires at :05, stagger=5 → :10, stagger=10 → :15
            fire_at = 5 + ts.fetch_offset_s
            if current_minute <= ts.last_bar_minute:
                return   # same minute we already fetched — nothing new yet
            if secs_past_minute < fire_at:
                return   # new minute opened but our stagger slot hasn't arrived

        print(f"  [{ticker_tag(ts.ticker)}] 🔄 fetching bars  "
              f"(minute={current_minute} secs={secs_past_minute:.1f})")
        df = fetch_bars(ts.ticker, self.api_key, self.secret_key)

        # Always mark the minute so we don't retry this same minute on failure
        ts.last_bar_fetch  = now
        ts.last_bar_minute = current_minute

        if df is None or len(df) < MACD_SLOW + MACD_SIG + 5:
            print(f"  [{ticker_tag(ts.ticker)}] ⚠️  not enough bars "
                  f"({len(df) if df is not None else 0} received, "
                  f"need {MACD_SLOW + MACD_SIG + 5})")
            return

        try:
            rsi_val, hist_val = compute_indicators(df)
        except Exception as e:
            print(f"  [{ticker_tag(ts.ticker)}] ❌ indicator error: {e}")
            return

        # Prefer Finnhub live price; fall back to last dashboard poll value,
        # then finally the last bar's close price.
        fh_price = get_latest_price(ts.ticker)
        price    = fh_price if fh_price is not None else (
                   ts.last_price if ts.last_price is not None else
                   float(df["close"].iloc[-1]))
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
        ts.update_momentum(hist=hist_val, price=price, rsi=rsi_val)

    # ── Every-second proximity check ──────────────────────────────────────────

    def _check_proximity(self, ts: TickerState):
        """
        Every second:
          1. Pull the latest Finnhub price (pure in-memory dict read — no I/O).
          2. Check stop-loss / take-profit against the live price immediately.
          3. If we have cached bars, inject the live price as the last bar's
             close and recompute RSI + MACD — giving per-second indicator
             updates rather than waiting for the next closed bar.
          4. Log the proximity summary.

        NOTE: BUY/SELL momentum signals are NOT fired here — only on confirmed
        closed bars in _refresh_bars.  This prevents the forming bar's
        fluctuating histogram from triggering false signals mid-minute.
        """
        # ── 1. Refresh price from Finnhub ─────────────────────────────────────
        fh_price = get_latest_price(ts.ticker)
        if fh_price is not None:
            ts.last_price = fh_price

            # ── 2. Real-time stop-loss / take-profit check ─────────────────────
            # Check SL/TP on every tick so we don't have to wait up to a minute
            # for the next bar fetch to exit a losing or winning position.
            if ts.in_position and ts.buy_price is not None:
                pnl_pct = (fh_price - ts.buy_price) / ts.buy_price * 100

                reason = None
                if STOP_LOSS > 0 and pnl_pct <= -STOP_LOSS:
                    reason = "STOP LOSS (RT)"
                elif TAKE_PROFIT > 0 and pnl_pct >= TAKE_PROFIT:
                    reason = "TAKE PROFIT (RT)"

                if reason:
                    log_sell(
                        ticker    = ts.ticker,
                        price     = fh_price,
                        buy_price = ts.buy_price,
                        rsi       = ts.last_rsi or 0,
                        hist      = ts.last_hist or 0,
                        buy_time  = ts.buy_time,
                        reason    = reason,
                    )
                    ts.in_position  = False
                    ts.buy_price    = None
                    ts.buy_time     = None
                    ts.hist_growing = False

        # ── 3. Recompute indicators using live price as forming-bar close ──────
        # Inject the current price into the last bar of the cached DataFrame
        # and recompute RSI + MACD so the proximity log shows live values.
        if ts.cached_df is not None and ts.last_price is not None:
            try:
                df_live = ts.cached_df.copy()
                df_live.at[df_live.index[-1], "close"] = ts.last_price
                rsi_live, hist_live = compute_indicators(df_live)
                ts.last_rsi  = rsi_live
                ts.last_hist = hist_live
            except Exception:
                pass   # keep last known values if recompute fails

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
            reason = (
                "never showed positive histogram (cold)"
                if not ts.ever_positive_hist
                else "histogram retreated before signal fired (warm)"
            )
            print(
                f"\n  ⏰ EXPIRED {sym}  "
                f"({ts.age_s():.0f}s watched | {reason})\n"
            )
            del self.active[sym]
            # Allow re-adding if the ticker gets mentioned again later
            self._known_mentioned.discard(sym)

    # ── Main loop ─────────────────────────────────────────────────────────────

    def run(self):
        print("=" * 60)
        print("  Signal Engine — RSI + MACD Momentum")
        print(f"  Dashboard     : {DASHBOARD_URL}")
        print(f"  Auth user     : {DASHBOARD_USER or '(none)'}")
        print(f"  Finnhub key   : {'✓ loaded — WebSocket active' if self.finnhub_key else '✗ missing — using dashboard prices'}")
        print(f"  Alpaca key    : {'✓ loaded' if self.api_key else '✗ missing'}")
        trader_label = {"off": "off — log only", "paper": "PAPER trading", "live": "⚠️  LIVE trading"}.get(TRADER_MODE, TRADER_MODE)
        print(f"  Trader mode   : {trader_label}  (${TRADE_AMOUNT:.0f}/trade)")
        print(f"  Loop cadence  : every {POLL_INTERVAL}s")
        print(f"  Dashboard poll: every {DASHBOARD_POLL_INTERVAL}s (new ticker detection only)")
        print(f"  Bar refresh   : every {BAR_REFRESH}s (staggered {BAR_STAGGER}s apart)")
        print(f"  Expiry cold   : {EXPIRY_COLD}s (no positive hist seen)")
        print(f"  Expiry warm   : {EXPIRY_WARM}s (positive hist seen at least once)")
        print(f"  Max tickers   : {MAX_ACTIVE_TICKERS}")
        print(f"  Log file      : {LOG_FILE}")
        print("=" * 60)

        self._ensure_logged_in()
        print("  Watching dashboard for highlighted tickers…\n")

        while True:
            try:
                cycle_start = time.time()

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

                # 4. Sleep for the remainder of POLL_INTERVAL so we don't
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
