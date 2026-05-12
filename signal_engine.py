#!/usr/bin/env python3
"""
signal_engine.py — Automated RSI + MACD Signal Engine

Polls the local dashboard for highlighted (mentioned) tickers, fetches
historical bars from Alpaca for indicator warmup, then tracks MACD
histogram momentum to fire buy/sell signals.

HOW IT WORKS
────────────
1. Every second, polls http://localhost:8888/api/state for the live
   ticker list and their current prices.
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

    Reads credentials from the same secrets.json / bot_config.json that
    the dashboard uses (in the same directory).  You can also override
    with environment variables:
        ALPACA_API_KEY   ALPACA_SECRET_KEY

CONFIG TWEAKS  (edit the DEFAULTS section below)
─────────────
    DASHBOARD_URL      — where your dashboard is running
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

# ── Configuration — edit these to tune the engine ─────────────────────────────

DASHBOARD_URL  = "http://localhost:8888"  # address of the running dashboard

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

def _load_credentials() -> tuple[str, str]:
    """
    Return (api_key, secret_key) for Alpaca.

    Priority order:
      1. Environment variables  ALPACA_API_KEY / ALPACA_SECRET_KEY
      2. secrets.json in the same folder (written by the dashboard's
         Settings → Save Settings workflow)

    If neither source has credentials, we print a warning and continue.
    Bar fetching will simply be skipped until credentials appear.
    """
    # Check env vars first — useful for CI or Docker setups
    api_key    = os.getenv("ALPACA_API_KEY", "")
    secret_key = os.getenv("ALPACA_SECRET_KEY", "")

    # Fall back to the shared secrets file the dashboard also uses
    secrets_path = _HERE / "secrets.json"
    if secrets_path.exists():
        try:
            s = json.loads(secrets_path.read_text())
            # Only overwrite if env var was empty
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
        "limit":     count,       # how many bars to return (most recent first)
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
        # Ensure numeric types — Alpaca sometimes returns strings
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
        (potential pullback).  We use it here as a safety filter — we
        won't buy if RSI is already above RSI_BUY_MAX.

    MACD histogram:
        MACD line = EMA(12) − EMA(26) of the close price.
        Signal line = EMA(9) of the MACD line.
        Histogram = MACD line − signal line.

        When the histogram is positive and rising, the fast EMA is pulling
        further above the slow EMA — momentum is accelerating upward.
        When it starts falling again, momentum is fading — that's our cue
        to sell.

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
    rsi_val    = float(rsi_series.iloc[-1])    # most recent bar's RSI
    hist_val   = float(df["macd_hist"].iloc[-1])  # most recent bar's histogram
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
        "rsi":       round(rsi,   2),    # RSI at time of buy (should be < RSI_BUY_MAX)
        "macd_hist": round(hist, 6),     # histogram value that triggered the buy
        "time":      ts,
    }
    _append_log(entry)
    print(f"  🟢 BUY  {ticker:6s}  ${price:.2f}  RSI={rsi:.1f}  hist={hist:+.4f}  [{ts}]")


def log_sell(ticker: str, price: float, buy_price: float,
             rsi: float, hist: float, buy_time: str):
    """Record a SELL signal — includes P&L % vs the buy price."""
    ts  = _now_iso()
    # Simple percentage return: (exit − entry) / entry × 100
    pnl = round((price - buy_price) / buy_price * 100, 2)
    entry = {
        "action":    "SELL",
        "ticker":    ticker,
        "price":     round(price,     4),
        "buy_price": round(buy_price, 4),
        "pnl_pct":   pnl,                # positive = profit, negative = loss
        "rsi":       round(rsi,  2),
        "macd_hist": round(hist, 6),     # histogram value that triggered the sell
        "time":      ts,
        "buy_time":  buy_time,           # when we entered the position
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
        self.prev_hist:    Optional[float] = None  # histogram value from last evaluation
        self.hist_growing: bool            = False # True while histogram is positive & rising

        # Latest market data (updated each poll cycle)
        self.last_rsi:   Optional[float] = None
        self.last_price: Optional[float] = None
        self.last_bar_fetch: float       = 0.0    # epoch time of last Alpaca bar pull

    def is_expired(self) -> bool:
        """
        Returns True if we've been watching this ticker for longer than
        TICKER_EXPIRY without taking a position.  Positions are never expired
        — we hold until the SELL signal fires.
        """
        return (not self.in_position) and (time.time() - self.added_ts > TICKER_EXPIRY)

    def update_momentum(self, hist: float, price: float, rsi: float):
        """
        Core momentum logic — called after each bar refresh.

        The MACD histogram represents the distance between the fast EMA line
        and the signal line.  We watch how that distance is changing:

          Histogram positive AND rising:
            The fast line is above the signal line and still pulling away.
            This is an accelerating uptrend — safe zone to enter a position.
            → set hist_growing = True
            → fire BUY if not already in a position and RSI is acceptable

          Histogram was growing, now falling:
            The fast line is still above the signal line BUT it's heading
            back toward it.  Momentum is fading.  This is the exit signal.
            → fire SELL if we're in a position
            → reset hist_growing = False

        We always store the current histogram value as prev_hist so the
        next call can detect which direction things are moving.
        """
        self.last_price = price
        self.last_rsi   = rsi

        prev = self.prev_hist  # histogram from the previous evaluation cycle

        if prev is not None:
            # ── Detect growing histogram ───────────────────────────────────
            # Both conditions must be true:
            #   hist > 0     → fast line is above signal line (bullish)
            #   hist > prev  → gap is widening (momentum accelerating)
            if hist > 0 and hist > prev:
                self.hist_growing = True

            # ── Detect reversal (shrinking after growing) ──────────────────
            # hist < prev means the histogram bar is shorter than last time —
            # the fast line is heading back toward the signal line.
            # We only care about this reversal if the area WAS growing;
            # a histogram that's been negative and declining is just a
            # downtrend, not a reversal of an upswing we rode.
            elif self.hist_growing and hist < prev:
                if self.in_position:
                    # ── SELL ──────────────────────────────────────────────
                    log_sell(
                        ticker    = self.ticker,
                        price     = price,
                        buy_price = self.buy_price,
                        rsi       = rsi,
                        hist      = hist,
                        buy_time  = self.buy_time,
                    )
                    # Clear position tracking
                    self.in_position = False
                    self.buy_price   = None
                    self.buy_time    = None

                # Reset the growing flag — we'll need a fresh growing phase
                # before we can consider buying again
                self.hist_growing = False

        # ── BUY check ─────────────────────────────────────────────────────
        # All four conditions must be met simultaneously:
        #   hist_growing  → we're in an accelerating upswing
        #   not in_position → we don't already own the stock
        #   rsi < RSI_BUY_MAX → stock isn't already overbought
        #   price > 0     → sanity check — we have a valid price
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

        # Save current histogram so next call can compare direction
        self.prev_hist = hist


# ── Main engine ───────────────────────────────────────────────────────────────

class SignalEngine:
    """
    Orchestrates the polling loop:
      - Fetches dashboard state every POLL_INTERVAL seconds
      - Builds and maintains the active ticker list from highlighted rows
      - Refreshes bars + indicators every BAR_REFRESH seconds per ticker
      - Retires expired tickers
    """

    def __init__(self):
        self.api_key, self.secret_key = _load_credentials()

        # sym → TickerState for every ticker we're currently watching
        self.active: dict[str, TickerState] = {}

        # Tracks which mentioned tickers we've already added so we don't
        # create duplicate TickerState objects on every poll cycle
        self._known_mentioned: set[str] = set()

    # ── Dashboard polling ─────────────────────────────────────────────────────

    def _poll_dashboard(self) -> Optional[dict]:
        """
        GET /api/state from the local dashboard server.

        The dashboard exposes a full JSON snapshot every time this endpoint
        is hit — ticker list with prices, mention counts, and highlighted
        status.  We use it for two things:
          1. Real-time price updates (so we don't need a separate data feed)
          2. Detecting which tickers just got highlighted on the screen
        Returns the parsed JSON dict, or None if the dashboard is unreachable.
        """
        try:
            resp = requests.get(f"{DASHBOARD_URL}/api/state", timeout=3)
            resp.raise_for_status()
            return resp.json()
        except Exception:
            # Dashboard might be starting up or temporarily unreachable — just
            # skip this cycle and try again next second
            return None

    def _ingest_state(self, state: dict):
        """
        Process one snapshot from the dashboard.

        For each ticker row in the snapshot:
          • If it's in our active list, update its last-known price so that
            when the next signal fires we use the freshest price available.
          • If it's marked 'mentioned' (highlighted on the dashboard) and
            we haven't seen it before, add it to the active list.
        """
        tickers = state.get("tickers", [])

        for row in tickers:
            sym       = row.get("ticker", "")
            price     = row.get("price")      # may be None if market is closed
            mentioned = row.get("mentioned", False)  # True = highlighted row

            if not sym:
                continue   # skip malformed rows

            # Keep price current for already-tracked tickers
            if sym in self.active and price is not None:
                self.active[sym].last_price = float(price)

            # A ticker becomes "mentioned" the first time it's called out on air
            # (the transcription engine sets this flag).  Add it once.
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

        We rate-limit bar fetches to BAR_REFRESH seconds (default 60) per
        ticker to avoid hammering the Alpaca API — prices don't change that
        dramatically minute-to-minute.  The real-time price from the dashboard
        poll is what we actually use for entry/exit pricing; bars are just for
        the indicator math.
        """
        now = time.time()

        # Skip if we fetched less than BAR_REFRESH seconds ago
        if now - ts.last_bar_fetch < BAR_REFRESH:
            return

        df = fetch_bars(ts.ticker, self.api_key, self.secret_key)

        # Need at least MACD_SLOW + MACD_SIG + a few extra bars for the
        # indicators to be stable (26 + 9 + 5 = 40 minimum)
        if df is None or len(df) < MACD_SLOW + MACD_SIG + 5:
            ts.last_bar_fetch = now   # mark as attempted so we back off
            return

        try:
            rsi_val, hist_val = compute_indicators(df)
        except Exception as e:
            print(f"[SIG] {ts.ticker}: indicator error — {e}")
            ts.last_bar_fetch = now
            return

        # Prefer the live price from the dashboard poll; fall back to the
        # closing price of the most recent bar if we don't have one yet
        price = ts.last_price if ts.last_price is not None else float(df["close"].iloc[-1])

        ts.last_bar_fetch = now
        ts.update_momentum(hist=hist_val, price=price, rsi=rsi_val)

    # ── Expiry cleanup ────────────────────────────────────────────────────────

    def _expire_tickers(self):
        """
        Remove tickers that have been in the active list longer than
        TICKER_EXPIRY without triggering a BUY.  Tickers with open
        positions are never expired — we hold until the SELL signal fires.
        """
        expired = [sym for sym, ts in self.active.items() if ts.is_expired()]
        for sym in expired:
            print(f"  ⏰ Expired {sym}  (no position taken within {TICKER_EXPIRY // 60} min)")
            del self.active[sym]
            # Remove from known_mentioned so if it gets highlighted again
            # later in the session it will be re-added fresh
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
        print(f"  Log file  : {LOG_FILE}")
        print(f"  Expiry    : {TICKER_EXPIRY // 60} min  |  Bar refresh: {BAR_REFRESH}s")
        print(f"  Alpaca key: {'✓ loaded' if self.api_key else '✗ missing'}")
        print("=" * 60)
        print("  Waiting for highlighted tickers on dashboard…\n")

        while True:
            try:
                # Step 1 + 2: get dashboard snapshot, update active list
                state = self._poll_dashboard()
                if state:
                    self._ingest_state(state)

                # Step 3: evaluate signals for all active tickers
                # list() snapshot so we can safely mutate self.active inside the loop
                for ts in list(self.active.values()):
                    self._refresh_ticker(ts)

                # Step 4: retire stale tickers
                self._expire_tickers()

                time.sleep(POLL_INTERVAL)

            except KeyboardInterrupt:
                print("\n  Stopped by user.")
                break
            except Exception as e:
                # Catch-all so a transient network error doesn't kill the engine
                print(f"[ENGINE] Unexpected error: {e}")
                time.sleep(POLL_INTERVAL)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    SignalEngine().run()
