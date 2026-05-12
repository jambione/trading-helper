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

# ── Import our own signal library ──────────────────────────────────────────────
_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))
from signals import rsi as calc_rsi, compute_macd

# ── Defaults (edit here or override in bot_config.json) ───────────────────────
DASHBOARD_URL  = "http://localhost:8888"
POLL_INTERVAL  = 1          # seconds between dashboard state polls
BAR_REFRESH    = 60         # seconds between Alpaca bar re-fetches per ticker
TICKER_EXPIRY  = 600        # 10 minutes — drop ticker if no position taken
BAR_COUNT      = 100        # bars to fetch for warmup (needs >= 35 for MACD)
BAR_TIMEFRAME  = "1Min"
RSI_PERIOD     = 14
MACD_FAST      = 12
MACD_SLOW      = 26
MACD_SIG       = 9
RSI_BUY_MAX    = 70         # don't buy if RSI is overbought

LOG_FILE       = _HERE / "signal_log.json"

ALPACA_BASE_URL  = "https://data.alpaca.markets"
ALPACA_TRADE_URL = "https://paper-api.alpaca.markets"   # paper by default


# ── Load credentials from the shared config system ────────────────────────────

def _load_credentials() -> tuple[str, str]:
    """Read Alpaca API key + secret from secrets.json or env vars."""
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
            "       Set ALPACA_API_KEY / ALPACA_SECRET_KEY env vars,\n"
            "       or save them via the dashboard Settings → API Keys tab.\n"
            "       Bar fetching will be skipped until credentials are set."
        )
    return api_key, secret_key


# ── Alpaca bars ────────────────────────────────────────────────────────────────

def fetch_bars(symbol: str, api_key: str, secret_key: str,
               count: int = BAR_COUNT, timeframe: str = BAR_TIMEFRAME) -> Optional[pd.DataFrame]:
    """
    Fetch recent 1-Min bars from Alpaca data API.
    Returns a DataFrame with columns: open, high, low, close, volume
    or None on error.
    """
    if not api_key or not secret_key:
        return None

    url = f"{ALPACA_BASE_URL}/v2/stocks/{symbol}/bars"
    params = {
        "timeframe": timeframe,
        "limit":     count,
        "feed":      "iex",       # IEX feed is free with Alpaca
        "sort":      "asc",
    }
    headers = {
        "APCA-API-KEY-ID":     api_key,
        "APCA-API-SECRET-KEY": secret_key,
    }
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        bars = resp.json().get("bars", [])
        if not bars:
            return None

        df = pd.DataFrame(bars)
        df = df.rename(columns={"o": "open", "h": "high", "l": "low",
                                 "c": "close", "v": "volume", "t": "time"})
        df["close"]  = df["close"].astype(float)
        df["open"]   = df["open"].astype(float)
        df["high"]   = df["high"].astype(float)
        df["low"]    = df["low"].astype(float)
        df["volume"] = df["volume"].astype(float)
        return df.reset_index(drop=True)

    except Exception as e:
        print(f"[BARS] {symbol}: fetch failed — {e}")
        return None


# ── Signal computation ─────────────────────────────────────────────────────────

def compute_indicators(df: pd.DataFrame) -> tuple[float, float]:
    """
    Run RSI and MACD on the bar DataFrame.
    Returns (rsi_value, macd_hist_value) for the most recent bar.
    """
    cfg = {"macd_fast": MACD_FAST, "macd_slow": MACD_SLOW, "macd_signal": MACD_SIG}
    df  = compute_macd(df, cfg)

    rsi_series  = calc_rsi(df["close"], RSI_PERIOD)
    rsi_val     = float(rsi_series.iloc[-1])
    hist_val    = float(df["macd_hist"].iloc[-1])
    return rsi_val, hist_val


# ── Logging ────────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_log() -> list:
    if LOG_FILE.exists():
        try:
            return json.loads(LOG_FILE.read_text())
        except Exception:
            pass
    return []


def _append_log(entry: dict):
    entries = _load_log()
    entries.append(entry)
    LOG_FILE.write_text(json.dumps(entries, indent=2))


def log_buy(ticker: str, price: float, rsi: float, hist: float):
    ts = _now_iso()
    entry = {
        "action": "BUY",
        "ticker": ticker,
        "price":  round(price, 4),
        "rsi":    round(rsi,   2),
        "macd_hist": round(hist, 6),
        "time":   ts,
    }
    _append_log(entry)
    print(f"  🟢 BUY  {ticker:6s}  ${price:.2f}  RSI={rsi:.1f}  hist={hist:+.4f}  [{ts}]")


def log_sell(ticker: str, price: float, buy_price: float,
             rsi: float, hist: float, buy_time: str):
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


# ── Per-ticker state ───────────────────────────────────────────────────────────

class TickerState:
    """All mutable state tracked for one active ticker."""

    def __init__(self, ticker: str):
        self.ticker         = ticker
        self.added_ts       = time.time()     # when it entered the active list
        self.in_position    = False
        self.buy_price: Optional[float] = None
        self.buy_time:  Optional[str]   = None
        self.prev_hist: Optional[float] = None
        self.hist_growing   = False           # histogram was positive and rising
        self.last_rsi: Optional[float]  = None
        self.last_price: Optional[float] = None
        self.last_bar_fetch = 0.0             # epoch of last bar pull

    def is_expired(self) -> bool:
        """Ticker is expired if it's been > TICKER_EXPIRY seconds with no position."""
        return (not self.in_position) and (time.time() - self.added_ts > TICKER_EXPIRY)

    def update_momentum(self, hist: float, price: float, rsi: float):
        """
        Update MACD histogram momentum state and fire buy/sell signals.

        MACD histogram momentum logic:
          • hist > 0 AND hist > prev_hist → histogram area is GROWING
            → safe zone to buy (fast line moving away from signal line, above it)
          • hist_growing AND hist < prev_hist → histogram area is SHRINKING
            → fast line heading back toward signal line → time to sell
        """
        self.last_price = price
        self.last_rsi   = rsi

        prev = self.prev_hist

        if prev is not None:
            # Detect growing area
            if hist > 0 and hist > prev:
                self.hist_growing = True

            # Detect reversal — histogram was growing, now shrinking
            elif self.hist_growing and hist < prev:
                if self.in_position:
                    # ── SELL signal ────────────────────────────────────────
                    log_sell(
                        ticker    = self.ticker,
                        price     = price,
                        buy_price = self.buy_price,
                        rsi       = rsi,
                        hist      = hist,
                        buy_time  = self.buy_time,
                    )
                    self.in_position  = False
                    self.buy_price    = None
                    self.buy_time     = None
                self.hist_growing = False

        # ── BUY signal ─────────────────────────────────────────────────────────
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


# ── Main engine loop ───────────────────────────────────────────────────────────

class SignalEngine:

    def __init__(self):
        self.api_key, self.secret_key = _load_credentials()
        self.active: dict[str, TickerState] = {}   # sym → TickerState
        self._known_mentioned: set[str] = set()    # already-added mentioned tickers

    # ── Dashboard polling ──────────────────────────────────────────────────────

    def _poll_dashboard(self) -> Optional[dict]:
        """GET /api/state from the local dashboard.  Returns parsed JSON or None."""
        try:
            resp = requests.get(
                f"{DASHBOARD_URL}/api/state",
                timeout=3,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return None

    def _ingest_state(self, state: dict):
        """
        Process dashboard state:
          • Add newly mentioned tickers to the active list.
          • Update last-known price for already-active tickers.
        """
        tickers = state.get("tickers", [])
        for row in tickers:
            sym       = row.get("ticker", "")
            price     = row.get("price")
            mentioned = row.get("mentioned", False)

            if not sym:
                continue

            # Update price on already-active tickers
            if sym in self.active and price is not None:
                self.active[sym].last_price = float(price)

            # Add newly highlighted tickers
            if mentioned and sym not in self._known_mentioned:
                self._known_mentioned.add(sym)
                if sym not in self.active:
                    self.active[sym] = TickerState(sym)
                    age_mins = TICKER_EXPIRY // 60
                    print(
                        f"  ➕ Added  {sym}  to active list "
                        f"(expires in {age_mins} min if no position)"
                    )

    # ── Bar fetch + signal evaluation ─────────────────────────────────────────

    def _refresh_ticker(self, ts: TickerState):
        """Fetch fresh bars for one ticker, compute indicators, update momentum."""
        now = time.time()
        if now - ts.last_bar_fetch < BAR_REFRESH:
            return   # not due yet

        df = fetch_bars(ts.ticker, self.api_key, self.secret_key)
        if df is None or len(df) < MACD_SLOW + MACD_SIG + 5:
            # Not enough data to be meaningful
            ts.last_bar_fetch = now
            return

        try:
            rsi_val, hist_val = compute_indicators(df)
        except Exception as e:
            print(f"[SIG] {ts.ticker}: indicator error — {e}")
            ts.last_bar_fetch = now
            return

        price = ts.last_price
        if price is None:
            # Fall back to last bar close
            price = float(df["close"].iloc[-1])

        ts.last_bar_fetch = now
        ts.update_momentum(hist=hist_val, price=price, rsi=rsi_val)

    # ── Expiry cleanup ─────────────────────────────────────────────────────────

    def _expire_tickers(self):
        expired = [sym for sym, ts in self.active.items() if ts.is_expired()]
        for sym in expired:
            print(f"  ⏰ Expired {sym}  (no position taken within {TICKER_EXPIRY // 60} min)")
            del self.active[sym]
            self._known_mentioned.discard(sym)

    # ── Main loop ──────────────────────────────────────────────────────────────

    def run(self):
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
                state = self._poll_dashboard()
                if state:
                    self._ingest_state(state)

                # Evaluate signals for all active tickers
                for ts in list(self.active.values()):
                    self._refresh_ticker(ts)

                # Drop expired tickers
                self._expire_tickers()

                time.sleep(POLL_INTERVAL)

            except KeyboardInterrupt:
                print("\n  Stopped by user.")
                break
            except Exception as e:
                print(f"[ENGINE] Unexpected error: {e}")
                time.sleep(POLL_INTERVAL)


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    SignalEngine().run()
