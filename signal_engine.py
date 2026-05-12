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
   is added to the active watchlist (capped at MAX_ACTIVE_TICKERS).
3. Every second, logs each active ticker's proximity to the buy signal.
4. Every 60 s, fetches 100 × 1-Min bars from Alpaca for each active
   ticker and recomputes RSI(14) + MACD(12, 26, 9).
   Bar fetches are STAGGERED — one ticker every BAR_STAGGER seconds so
   they never all fire at once.
5. Expiry:
     • 3 min  (EXPIRY_COLD)   — ticker never showed a positive histogram
     • 10 min (EXPIRY_WARM)   — ticker showed positive hist at least once
       (we give it more time since it came close to a signal)
6. MACD histogram momentum rules (per ticker):
     • Area growing  → hist positive AND rising  → safe to BUY
     • Area shrinking → hist was growing, now falling → time to SELL
7. Logs every BUY and SELL to signal_log.json (and prints to console).

EFFICIENCY NOTES
────────────────
  • One dashboard HTTP request per second total (not per ticker).
  • Alpaca bar fetches are rate-limited to BAR_REFRESH seconds per ticker
    AND staggered so they don't all fire in the same second.
  • Active list is capped at MAX_ACTIVE_TICKERS (default 20).
  • Every-second proximity checks are pure in-memory — no I/O.

RUN
───
    python signal_engine.py
    (reads signal_engine.env automatically)

CONFIG (signal_engine.env or env vars)
───────────────────────────────────────
    DASHBOARD_URL / DASHBOARD_USER / DASHBOARD_PASS
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

# ── Load signal_engine.env ────────────────────────────────────────────────────
def _load_env_file(path: Path):
    """
    Parse KEY=VALUE lines from an env file and inject into os.environ.
    Shell environment always wins — we only set keys that aren't already set.
    """
    if not path.exists():
        return
    loaded = []
    with open(path) as f:
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

POLL_INTERVAL  = int(os.getenv("POLL_INTERVAL",  "1"))   # seconds between dashboard polls

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

BAR_COUNT      = int(os.getenv("BAR_COUNT",  "100"))
BAR_TIMEFRAME  = os.getenv("BAR_TIMEFRAME",  "1Min")

RSI_PERIOD     = int(os.getenv("RSI_PERIOD",   "14"))
MACD_FAST      = int(os.getenv("MACD_FAST",    "12"))
MACD_SLOW      = int(os.getenv("MACD_SLOW",    "26"))
MACD_SIG       = int(os.getenv("MACD_SIG",     "9"))
RSI_BUY_MAX    = int(os.getenv("RSI_BUY_MAX",  "70"))

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
    Download recent OHLCV bars from Alpaca.
    Returns a DataFrame(open, high, low, close, volume) or None on error.
    """
    if not api_key or not secret_key:
        return None
    url = f"{ALPACA_BASE_URL}/v2/stocks/{symbol}/bars"
    params  = {"timeframe": timeframe, "limit": count, "feed": "iex", "sort": "asc"}
    headers = {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret_key}
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        bars = resp.json().get("bars", [])
        if not bars:
            return None
        df = pd.DataFrame(bars).rename(columns={
            "o": "open", "h": "high", "l": "low",
            "c": "close", "v": "volume", "t": "time",
        })
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = df[col].astype(float)
        return df.reset_index(drop=True)
    except Exception as e:
        print(f"[BARS] {symbol}: fetch failed — {e}")
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
    _append_log({"action": "BUY", "ticker": ticker,
                 "price": round(price, 4), "rsi": round(rsi, 2),
                 "macd_hist": round(hist, 6), "time": ts})
    print(f"\n  {'='*56}")
    print(f"  🟢 BUY  {ticker}  ${price:.2f}  RSI={rsi:.1f}  hist={hist:+.4f}  [{ts}]")
    print(f"  {'='*56}\n")

def log_sell(ticker: str, price: float, buy_price: float,
             rsi: float, hist: float, buy_time: str):
    ts  = _now_iso()
    pnl = round((price - buy_price) / buy_price * 100, 2)
    _append_log({"action": "SELL", "ticker": ticker,
                 "price": round(price, 4), "buy_price": round(buy_price, 4),
                 "pnl_pct": pnl, "rsi": round(rsi, 2),
                 "macd_hist": round(hist, 6), "time": ts, "buy_time": buy_time})
    sign = "+" if pnl >= 0 else ""
    print(f"\n  {'='*56}")
    print(f"  🔴 SELL {ticker}  ${price:.2f}  P&L={sign}{pnl}%  hist={hist:+.4f}  [{ts}]")
    print(f"  {'='*56}\n")


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

        # fetch_offset_s staggers the first bar fetch so all new tickers
        # don't hammer Alpaca at the same moment
        self.last_bar_fetch: float = time.time() - BAR_REFRESH + fetch_offset_s
        # e.g. offset=0  → fetches immediately
        #      offset=10 → fetches in 10 s
        #      offset=20 → fetches in 20 s

        self.check_count: int = 0   # increments every second — used in log output

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
            status = "📈 IN POSITION — watching for SELL"
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
        self._token: Optional[str] = None
        self.active: dict[str, TickerState] = {}       # sym → TickerState
        self._known_mentioned: set[str] = set()        # ever-seen mentioned syms
        self._stagger_index: int = 0                   # increments per added ticker

    def _auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    def _ensure_logged_in(self):
        if not self._token and DASHBOARD_USER and DASHBOARD_PASS:
            self._token = _dashboard_login(DASHBOARD_USER, DASHBOARD_PASS)

    # ── Dashboard polling ─────────────────────────────────────────────────────

    def _poll_dashboard(self) -> Optional[dict]:
        """Single GET /api/state — retries once after a 401 re-login."""
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
        - Update prices for already-active tickers (cheap, always runs).
        - Add newly mentioned tickers (only when list has room).
        """
        for row in state.get("tickers", []):
            sym   = row.get("ticker", "")
            price = row.get("price")
            if not sym:
                continue

            # Always keep prices fresh for tracked tickers
            if sym in self.active and price is not None:
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

    # ── Bar refresh ───────────────────────────────────────────────────────────

    def _refresh_bars(self, ts: TickerState):
        """
        Fetch fresh bars for one ticker if it's due.
        Rate-limited by last_bar_fetch + BAR_REFRESH.
        """
        now = time.time()
        if now - ts.last_bar_fetch < BAR_REFRESH:
            return   # not due yet

        print(f"  [{ticker_tag(ts.ticker)}] 🔄 fetching bars from Alpaca…")
        df = fetch_bars(ts.ticker, self.api_key, self.secret_key)

        if df is None or len(df) < MACD_SLOW + MACD_SIG + 5:
            print(f"  [{ticker_tag(ts.ticker)}] ⚠️  not enough bars "
                  f"({len(df) if df is not None else 0} received, "
                  f"need {MACD_SLOW + MACD_SIG + 5})")
            ts.last_bar_fetch = now
            return

        try:
            rsi_val, hist_val = compute_indicators(df)
        except Exception as e:
            print(f"  [{ticker_tag(ts.ticker)}] ❌ indicator error: {e}")
            ts.last_bar_fetch = now
            return

        price = ts.last_price if ts.last_price is not None else float(df["close"].iloc[-1])
        ts.last_bar_fetch = now
        ts.bars_fetched   = True

        print(
            f"  [{ticker_tag(ts.ticker)}] 📊 bars loaded  "
            f"RSI={rsi_val:.1f}  hist={hist_val:+.4f}  "
            f"price={price:.2f}  bars={len(df)}"
        )

        ts.update_momentum(hist=hist_val, price=price, rsi=rsi_val)

    # ── Every-second proximity check ──────────────────────────────────────────

    def _check_proximity(self, ts: TickerState):
        """
        Log one line per ticker per second showing how close it is to a signal.
        Pure in-memory — no network calls.
        """
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
        print(f"  Alpaca key    : {'✓ loaded' if self.api_key else '✗ missing'}")
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

                # 1. One dashboard request per cycle — updates prices + detects new tickers
                state = self._poll_dashboard()
                if state:
                    self._ingest_state(state)

                # 2. For each active ticker:
                #      a) refresh bars if due (rate-limited, staggered)
                #      b) log proximity to signal (every second, in-memory)
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
