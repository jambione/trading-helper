#!/usr/bin/env python3
"""
backtest_v2.py — Excellence Loop Backtester

Mission: small, consistent, profitable wins — high win rate, tight stops,
         positive expectancy on every trade.

Three-Phase Excellence Loop
───────────────────────────
  Phase 1 · Broad  — 768 combos on training bars (first 70%), ranked by score
  Phase 2 · Narrow — fine-tune top-15 Phase-1 winners (±1 step per param)
  Phase 3 · Validate — top-5 Phase-2 configs on held-out test bars (last 30%)

A/B Benchmark
─────────────
  Baseline (A): current signal_engine.env settings — always run first.
  Every Phase-3 winner (B) is compared against A on all key metrics.

Simulation Fidelity
───────────────────
  Faithfully replicates signal_engine.py:
    HIST_CONFIRM_BARS — N consecutive growing histogram bars required for BUY
    MIN_HOLD_BARS     — hold N bars before a MACD reversal sell fires
    MAX_HOLD_BARS     — force-exit after N bars (time stop)
    RSI_SELL_OB       — sell when RSI ≥ threshold on any bar (0 = disabled)
    Stop / Take       — checked against bar LOW (stop) and HIGH (take) each bar,
                        so intrabar extremes trigger exits (not just closes)
    RVOL filter       — skip BUY if relative volume < rvol_min (when enabled)

Composite Score  (tuned for small consistent wins)
───────────────────────────────────────────────────
  score = WinRate (40 pts max) + ProfitFactor (30 pts) + Sharpe (20 pts)
        − MaxConsecLoss penalty (3 pts each) − MaxDrawdown penalty (1 pt per %)
  Configs with fewer than 10 trades score 0 (statistically meaningless).

HOW TO RUN
──────────
  python backtest_v2.py GME
  python backtest_v2.py GME AAPL TSLA  --months 3
  python backtest_v2.py GME            --months 1 --top 10
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from itertools import product
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import requests


# ── Synthetic data generator (--demo mode) ────────────────────────────────────

def generate_synthetic_bars(n_days: int = 63, seed: int = 42,
                             price_start: float = 2.50) -> pd.DataFrame:
    """
    Adversarial synthetic 1-minute OHLCV bars for a sub-$5 momentum stock.

    Four regimes (chosen to stress-test the signal engine):
      CHOPPY       (60%) — mean-reverting noise, false MACD signals, whipsaws
      BULL_REAL    (15%) — genuine sustained uptrend, RVOL spike, 60% win probability
      BULL_TRAP    (12%) — looks like momentum (RVOL spike) but reverses mid-wave
      BEAR         (13%) — sustained downtrend — system should stay out

    Key adversarial features:
      • Bull traps fire RVOL 3-5x (same as real) so the RVOL filter gets tested
      • Choppy periods create frequent false MACD histogram positives
      • Overnight gaps ±1-4% disrupt indicator state
      • Spread noise makes stop-loss realistic (intrabar lows below close)
    """
    rng          = np.random.default_rng(seed)
    bars_per_day = 390
    n_bars       = n_days * bars_per_day
    avg_vol      = 80_000

    # ── Regime sequence ────────────────────────────────────────────────────────
    CHOPPY, BULL_REAL, BULL_TRAP, BEAR = 0, 1, 2, 3
    regime_arr = np.zeros(n_bars, dtype=int)
    i = 0
    while i < n_bars:
        r = rng.random()
        if r < 0.60:
            length = int(rng.integers(3, 12))
            regime_arr[i:min(i+length, n_bars)] = CHOPPY
        elif r < 0.75:
            length = int(rng.integers(10, 30))   # real bull: 10-29 bars
            regime_arr[i:min(i+length, n_bars)] = BULL_REAL
        elif r < 0.87:
            length = int(rng.integers(5, 18))    # trap: looks good then dies
            regime_arr[i:min(i+length, n_bars)] = BULL_TRAP
        else:
            length = int(rng.integers(8, 25))    # bear: sustained down
            regime_arr[i:min(i+length, n_bars)] = BEAR
        i += length

    # ── Price simulation ───────────────────────────────────────────────────────
    close_arr  = np.empty(n_bars)
    open_arr   = np.empty(n_bars)
    high_arr   = np.empty(n_bars)
    low_arr    = np.empty(n_bars)
    volume_arr = np.empty(n_bars, dtype=int)

    price = price_start
    for k in range(n_bars):
        if k % bars_per_day == 0:
            gap   = rng.normal(0, 0.020)          # ±2% overnight gap
            price = max(0.30, price * (1 + gap))

        reg = regime_arr[k]

        if reg == CHOPPY:
            # Noisy mean-reverting — lots of false MACD positives
            ret = rng.normal(0.0001, 0.0012)      # tiny positive bias
            vol = int(avg_vol * rng.lognormal(-0.2, 0.5))
            spread_mult = rng.uniform(1.0, 3.0)

        elif reg == BULL_REAL:
            # Genuine uptrend — sustained drift
            ret = rng.normal(0.0025, 0.0018)      # +0.25%/bar avg
            vol = int(avg_vol * rng.uniform(3.0, 7.0))
            spread_mult = rng.uniform(0.5, 1.5)

        elif reg == BULL_TRAP:
            # First half looks bullish, second half reverses
            half = int(rng.integers(5, 18)) // 2
            frac = (k % half) / max(half, 1)
            if frac < 0.5:
                ret = rng.normal(0.0020, 0.0020)  # initial pump
            else:
                ret = rng.normal(-0.0030, 0.0025) # reversal dump
            vol = int(avg_vol * rng.uniform(3.0, 5.0))  # high vol on both
            spread_mult = rng.uniform(1.5, 4.0)   # wide spreads

        else:  # BEAR
            ret = rng.normal(-0.0020, 0.0018)     # -0.20%/bar
            vol = int(avg_vol * rng.lognormal(0.3, 0.4))
            spread_mult = rng.uniform(0.8, 2.0)

        open_p  = price
        close_p = max(0.01, price * (1.0 + ret))
        move    = abs(close_p - open_p)
        spread  = (move + price * 0.0005) * spread_mult
        high_p  = max(open_p, close_p) + spread * rng.uniform(0.3, 1.0)
        low_p   = max(0.01, min(open_p, close_p) - spread * rng.uniform(0.3, 1.0))

        open_arr[k]   = round(open_p,  4)
        close_arr[k]  = round(close_p, 4)
        high_arr[k]   = round(high_p,  4)
        low_arr[k]    = round(low_p,   4)
        volume_arr[k] = max(100, vol)
        price         = close_p

    # ── Timestamps ────────────────────────────────────────────────────────────
    base = datetime(2026, 1, 2, 14, 30, 0, tzinfo=timezone.utc)
    times = [
        (base + timedelta(minutes=d * 1440 + m)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for d in range(n_days) for m in range(bars_per_day)
    ]

    return pd.DataFrame({
        "time": times, "open": open_arr, "high": high_arr,
        "low": low_arr, "close": close_arr, "volume": volume_arr,
    })

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))
from signals import rsi as calc_rsi, compute_macd


# ── Environment / credentials ─────────────────────────────────────────────────

import desk_core

_load_env = desk_core.load_desk_env
_load_env(_HERE / "signal_engine.env")


def _load_creds() -> tuple[str, str]:
    api = os.getenv("ALPACA_API_KEY", "")
    sec = os.getenv("ALPACA_SECRET_KEY", "")
    if not (api and sec):
        p = _HERE / "secrets.json"
        if p.exists():
            try:
                s = json.loads(p.read_text())
                api = api or s.get("api_key", "")
                sec = sec or s.get("secret_key", "")
            except Exception:
                pass
    return api, sec


# ── Baseline (A): read live signal_engine.env so A is always current ──────────

BASELINE: dict = {
    "hist_confirm":  int(os.getenv("HIST_CONFIRM_BARS",   "2")),
    "rsi_buy_max":   int(os.getenv("RSI_BUY_MAX",         "65")),
    "rsi_sell_ob":   int(os.getenv("RSI_SELL_OVERBOUGHT", "75")),
    "stop_loss":     float(os.getenv("STOP_LOSS",         "1.5")) / 100,
    "take_profit":   float(os.getenv("TAKE_PROFIT",       "3.0")) / 100,
    "min_hold_bars": max(1, int(os.getenv("MIN_HOLD_SECONDS", "60")) // 60),
    "max_hold_bars": int(os.getenv("MAX_HOLD_MINUTES",    "15")),
    "macd_fast":     int(os.getenv("MACD_FAST",           "12")),
    "macd_slow":     int(os.getenv("MACD_SLOW",           "26")),
    "macd_sig":      int(os.getenv("MACD_SIG",             "9")),
    "use_rvol":      False,
    "rvol_min":      float(os.getenv("RVOL_MIN",          "3.0")),
}


# ── Parameter grids ───────────────────────────────────────────────────────────
# Phase 1: 3×4×3×4×4×2×1×2×1 = 768 combos — fast on a single ticker

BROAD_GRID: dict = dict(
    hist_confirm  = [1, 2, 3],
    rsi_buy_max   = [55, 60, 65, 70],
    rsi_sell_ob   = [70, 75, 0],          # 0 = disabled
    stop_loss     = [0.005, 0.010, 0.015, 0.020],
    take_profit   = [0.015, 0.020, 0.025, 0.030],
    min_hold_bars = [1, 2],
    max_hold_bars = [15],
    use_rvol      = [False, True],
    macd_config   = [(12, 26, 9)],
)

# Narrow-sweep step sizes per parameter
_STEP: dict = {
    "hist_confirm":  1,
    "rsi_buy_max":   5,
    "rsi_sell_ob":   5,
    "stop_loss":     0.005,
    "take_profit":   0.005,
    "min_hold_bars": 1,
    "max_hold_bars": 5,
}

ALPACA_BASE = "https://data.alpaca.markets"


# ── Bar fetching ──────────────────────────────────────────────────────────────

def fetch_bars(symbol: str, api_key: str, secret_key: str,
               months: int = 3) -> Optional[pd.DataFrame]:
    headers    = {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret_key}
    url        = f"{ALPACA_BASE}/v2/stocks/{symbol}/bars"
    start      = (datetime.now(timezone.utc) - timedelta(days=months * 31)
                  ).strftime("%Y-%m-%dT%H:%M:%SZ")
    all_bars:  list = []
    page_token = None

    print(f"  Fetching {months}mo bars for {symbol}…", end="", flush=True)

    while True:
        params: dict = {"timeframe": "1Min", "start": start,
                        "limit": 10_000, "feed": "iex", "sort": "asc"}
        if page_token:
            params["page_token"] = page_token
        try:
            r = requests.get(url, params=params, headers=headers, timeout=30)
            if r.status_code == 422:
                params["feed"] = "sip"
                r = requests.get(url, params=params, headers=headers, timeout=30)
            r.raise_for_status()
            data       = r.json()
            all_bars.extend(data.get("bars", []))
            page_token = data.get("next_page_token")
            print(".", end="", flush=True)
            if not page_token:
                break
            time.sleep(0.2)
        except Exception as e:
            print(f"\n  ❌  {symbol}: {e}")
            break

    print(f"  {len(all_bars):,} bars")
    if not all_bars:
        return None

    df = pd.DataFrame(all_bars).rename(columns={
        "o": "open", "h": "high", "l": "low",
        "c": "close", "v": "volume", "t": "time"})
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col])
    return df.reset_index(drop=True)


def train_test_split(df: pd.DataFrame,
                     test_frac: float = 0.30) -> tuple[pd.DataFrame, pd.DataFrame]:
    split = int(len(df) * (1 - test_frac))
    return (df.iloc[:split].reset_index(drop=True),
            df.iloc[split:].reset_index(drop=True))


# ── Indicator computation ─────────────────────────────────────────────────────

def add_indicators(df: pd.DataFrame,
                   macd_fast: int, macd_slow: int, macd_sig: int) -> pd.DataFrame:
    cfg = {"macd_fast": macd_fast, "macd_slow": macd_slow, "macd_signal": macd_sig}
    df  = compute_macd(df.copy(), cfg)
    df["rsi"]  = calc_rsi(df["close"], 14)
    avg_vol    = df["volume"].rolling(20).mean().replace(0, np.nan)
    df["rvol"] = df["volume"] / avg_vol
    return df


# ── Core simulation ───────────────────────────────────────────────────────────

def simulate(df: pd.DataFrame, p: dict) -> list[dict]:
    """
    Faithful replication of signal_engine.py update_momentum() logic.

    Exit priority order (same as live engine):
      1. Stop-loss      — bar low ≤ buy_price × (1 − stop_loss)
      2. Take-profit    — bar high ≥ buy_price × (1 + take_profit)
      3. RSI overbought — bar RSI ≥ rsi_sell_ob  (0 = disabled)
      4. Time stop      — held ≥ max_hold_bars
      5. MACD reversal  — hist was growing, now shrinking (after min_hold_bars)
    """
    hist_confirm = p["hist_confirm"]
    rsi_buy_max  = p["rsi_buy_max"]
    rsi_sell_ob  = p.get("rsi_sell_ob", 0)
    stop_loss    = p["stop_loss"]
    take_profit  = p["take_profit"]
    min_hold     = p.get("min_hold_bars", 1)
    max_hold     = p.get("max_hold_bars", 15)
    use_rvol     = p.get("use_rvol", False)
    rvol_min     = p.get("rvol_min", 3.0)

    n         = len(df)
    warmup    = df["macd_hist"].first_valid_index()
    if warmup is None:
        return []
    warmup = max(int(warmup) + 1, 21)   # need 20 bars for RVOL rolling avg

    hist_arr  = df["macd_hist"].to_numpy(dtype=float)
    rsi_arr   = df["rsi"].to_numpy(dtype=float)
    close_arr = df["close"].to_numpy(dtype=float)
    high_arr  = df["high"].to_numpy(dtype=float)
    low_arr   = df["low"].to_numpy(dtype=float)
    rvol_arr  = df["rvol"].to_numpy(dtype=float)
    time_arr  = df["time"].to_numpy() if "time" in df.columns else np.arange(n)

    trades: list[dict] = []

    hist_grow_count = 0
    hist_growing    = False
    in_position     = False
    buy_price: Optional[float] = None
    buy_bar:   Optional[int]   = None
    prev_hist: Optional[float] = None

    for i in range(warmup, n):
        hist  = float(hist_arr[i])
        rsi   = float(rsi_arr[i])
        close = float(close_arr[i])
        high  = float(high_arr[i])
        low   = float(low_arr[i])
        rvol  = float(rvol_arr[i])

        if np.isnan(hist) or np.isnan(rsi) or close <= 0:
            if not np.isnan(hist):
                prev_hist = hist
            continue

        sold = False

        # ── Priority exits: stop/take check against intrabar high/low ────────
        if in_position and buy_price is not None:
            hold   = i - buy_bar
            reason = None
            exit_p = close

            if low <= buy_price * (1.0 - stop_loss):
                reason = "stop_loss"
                exit_p = buy_price * (1.0 - stop_loss)   # fill at stop level
            elif high >= buy_price * (1.0 + take_profit):
                reason = "take_profit"
                exit_p = buy_price * (1.0 + take_profit) # fill at target
            elif rsi_sell_ob > 0 and rsi >= rsi_sell_ob:
                reason = "rsi_overbought"
            elif max_hold > 0 and hold >= max_hold:
                reason = "time_stop"

            if reason:
                pnl = (exit_p - buy_price) / buy_price * 100
                trades.append({
                    "buy_bar": buy_bar, "sell_bar": i,
                    "buy_price": buy_price, "sell_price": round(exit_p, 4),
                    "buy_time":  str(time_arr[buy_bar]),
                    "sell_time": str(time_arr[i]),
                    "hold_bars": hold,
                    "pnl_pct":   round(pnl, 4),
                    "reason":    reason,
                    "win":       pnl > 0,
                })
                in_position     = False
                buy_price       = None
                buy_bar         = None
                hist_growing    = False
                hist_grow_count = 0
                sold            = True

        # ── MACD state machine — mirrors TickerState.update_momentum() ───────
        if not sold and prev_hist is not None:
            if hist > 0 and hist > prev_hist:
                hist_grow_count = min(hist_grow_count + 1, hist_confirm + 1)
                if hist_grow_count >= hist_confirm and not hist_growing:
                    hist_growing = True
            elif hist <= 0 or hist < prev_hist:
                if hist_growing:
                    if in_position and buy_price is not None:
                        hold = i - buy_bar
                        if hold >= min_hold:
                            pnl = (close - buy_price) / buy_price * 100
                            trades.append({
                                "buy_bar": buy_bar, "sell_bar": i,
                                "buy_price": buy_price, "sell_price": round(close, 4),
                                "buy_time":  str(time_arr[buy_bar]),
                                "sell_time": str(time_arr[i]),
                                "hold_bars": hold,
                                "pnl_pct":   round(pnl, 4),
                                "reason":    "macd_reversal",
                                "win":       pnl > 0,
                            })
                            in_position = False
                            buy_price   = None
                            buy_bar     = None
                            sold        = True
                    hist_growing    = False
                    hist_grow_count = 0
                else:
                    hist_grow_count = 0

        # ── BUY check ─────────────────────────────────────────────────────────
        rvol_ok = (not use_rvol) or (not np.isnan(rvol) and rvol >= rvol_min)
        if (hist_growing and not in_position
                and rsi < rsi_buy_max and close > 0 and rvol_ok):
            in_position = True
            buy_price   = close
            buy_bar     = i

        prev_hist = hist

    # Close any position still open at end of data
    if in_position and buy_price is not None:
        exit_p = float(close_arr[-1])
        pnl    = (exit_p - buy_price) / buy_price * 100
        trades.append({
            "buy_bar": buy_bar, "sell_bar": n - 1,
            "buy_price": buy_price, "sell_price": round(exit_p, 4),
            "buy_time":  str(time_arr[buy_bar]),
            "sell_time": str(time_arr[-1]),
            "hold_bars": n - 1 - buy_bar,
            "pnl_pct":   round(pnl, 4),
            "reason":    "end_of_data",
            "win":       pnl > 0,
        })

    return trades


# ── Metrics ───────────────────────────────────────────────────────────────────

def calc_metrics(trades: list[dict]) -> dict:
    """All metrics tuned for the 'small consistent wins' mission."""
    empty = {
        "trades": 0, "wins": 0, "win_rate": 0.0,
        "profit_factor": 0.0, "sharpe": 0.0,
        "avg_win": 0.0, "avg_loss": 0.0, "wr_ratio": 0.0,
        "max_dd": 0.0, "max_cl": 0, "total_pnl": 0.0,
        "avg_hold": 0.0, "score": 0.0,
    }
    if not trades:
        return empty

    pnls   = [t["pnl_pct"] for t in trades]
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    n      = len(pnls)

    win_rate = len(wins) / n * 100

    gross_win     = sum(wins)        if wins   else 0.0
    gross_loss    = abs(sum(losses)) if losses else None
    profit_factor = min(gross_win / gross_loss, 50.0) if gross_loss else 50.0

    avg_win  = sum(wins)   / len(wins)   if wins   else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    wr_ratio = avg_win / abs(avg_loss)   if avg_loss != 0 else 0.0

    arr    = np.array(pnls)
    std    = float(arr.std())
    sharpe = float(arr.mean() / std) if std > 1e-9 else 0.0

    # Max drawdown on $10k/trade equity curve
    equity = 10_000.0
    peak   = equity
    max_dd = 0.0
    for p in pnls:
        equity += equity * p / 100
        peak    = max(peak, equity)
        dd      = (peak - equity) / peak * 100
        max_dd  = max(max_dd, dd)

    max_cl = consec = 0
    for p in pnls:
        consec = consec + 1 if p <= 0 else 0
        max_cl = max(max_cl, consec)

    score = _score(win_rate, profit_factor, sharpe, max_cl, max_dd, n)

    return {
        "trades":        n,
        "wins":          len(wins),
        "win_rate":      round(win_rate, 1),
        "profit_factor": round(profit_factor, 2),
        "sharpe":        round(sharpe, 3),
        "avg_win":       round(avg_win, 3),
        "avg_loss":      round(avg_loss, 3),
        "wr_ratio":      round(wr_ratio, 2),
        "max_dd":        round(max_dd, 1),
        "max_cl":        max_cl,
        "total_pnl":     round(sum(pnls), 3),
        "avg_hold":      round(sum(t["hold_bars"] for t in trades) / n, 1),
        "score":         round(score, 2),
    }


def _score(win_rate: float, profit_factor: float, sharpe: float,
           max_cl: int, max_dd: float, n_trades: int) -> float:
    """
    Composite score optimised for small consistent wins.
    Rewards: high win rate, high profit factor, positive Sharpe.
    Penalises: long losing streaks, large drawdowns, insufficient sample.
    """
    if n_trades < 30:
        return 0.0   # fewer than 30 trades is not statistically meaningful

    wr_pts  = min(win_rate, 80)              # 0–80 pts  (80 = 80% win rate)
    pf_pts  = min(profit_factor * 15, 30)   # 0–30 pts  (2.0 PF → 30)
    sh_pts  = min(max(sharpe * 10, 0), 20)  # 0–20 pts  (2.0 Sharpe → 20)
    cl_pen  = min(max_cl * 3, 20)           # 0–20 pts penalty
    dd_pen  = min(max_dd, 20)               # 0–20 pts penalty
    return max(0.0, wr_pts + pf_pts + sh_pts - cl_pen - dd_pen)


# ── Sweep helpers ─────────────────────────────────────────────────────────────

def _expand_grid(grid: dict, max_combos: int = 0) -> list[dict]:
    """Expand a grid dict into a flat list of parameter dicts."""
    keys = list(grid.keys())
    vals = [grid[k] for k in keys]
    combos: list[dict] = []
    for combo in product(*vals):
        d = dict(zip(keys, combo))
        mf, ms, msig = d.pop("macd_config")
        d["macd_fast"] = mf
        d["macd_slow"] = ms
        d["macd_sig"]  = msig
        if "rvol_min" not in d:
            d["rvol_min"] = BASELINE["rvol_min"]
        combos.append(d)
    # Cap grid size to avoid multi-minute Phase 2 runs
    if max_combos and len(combos) > max_combos:
        rng = np.random.default_rng(0)
        idx = rng.choice(len(combos), size=max_combos, replace=False)
        combos = [combos[i] for i in sorted(idx)]
    return combos


_PHASE2_MAX = 600   # cap narrow sweep to keep runtime under 30s

def run_sweep(symbol: str, df: pd.DataFrame, grid: dict,
              label: str, max_combos: int = 0) -> list[dict]:
    combos = _expand_grid(grid, max_combos=max_combos)
    print(f"  [{label}] {len(combos)} combos…", end="", flush=True)

    # Pre-compute indicators once per unique MACD config
    cache: dict = {}
    for c in combos:
        key = (c["macd_fast"], c["macd_slow"], c["macd_sig"])
        if key not in cache:
            cache[key] = add_indicators(df, *key)

    results: list[dict] = []
    for p in combos:
        key     = (p["macd_fast"], p["macd_slow"], p["macd_sig"])
        trades  = simulate(cache[key], p)
        metrics = calc_metrics(trades)
        results.append({"symbol": symbol, **p, **metrics, "_trades": trades})

    results.sort(key=lambda r: r["score"], reverse=True)
    best = results[0]["score"] if results else 0
    print(f"  best score {best:.1f}")
    return results


# ── Phase 2: build narrow grid around top configs ─────────────────────────────

def build_narrow_grid(top_configs: list[dict]) -> dict:
    """
    For each numeric parameter, collect unique values from the top configs,
    then expand ±1 step (clamped to valid range).  Always test both MACD
    variants and both RVOL on/off.
    """
    def expand(key: str, vals: set) -> list:
        step = _STEP.get(key, 0)
        out  = set(vals)
        if step:
            for v in vals:
                if v == 0 and key == "rsi_sell_ob":
                    out.add(0)   # keep disabled option as-is
                    continue
                hi = v + step
                lo = v - step
                lo = max(1 if isinstance(v, int) else 0.001, lo)
                out.add(hi)
                out.add(lo)
        return sorted(out)

    agg: dict[str, set] = {k: set() for k in _STEP}
    macd_set: set = set()
    rvol_set: set = set()

    for c in top_configs:
        for k in _STEP:
            if k in c:
                agg[k].add(c[k])
        macd_set.add((c["macd_fast"], c["macd_slow"], c["macd_sig"]))
        rvol_set.add(c.get("use_rvol", False))

    grid = {k: expand(k, v) for k, v in agg.items()}
    grid["macd_config"] = list(macd_set | {(8, 21, 9), (12, 26, 9)})
    grid["use_rvol"]    = sorted(rvol_set | {True, False})
    return grid


# ── A/B comparison ────────────────────────────────────────────────────────────

def ab_compare(a_metrics: dict, b_metrics: dict) -> dict:
    """Return metric deltas and pass/fail for each metric."""
    out: dict = {}
    for m, higher_better in [
        ("win_rate", True), ("profit_factor", True),
        ("sharpe", True), ("wr_ratio", True), ("score", True),
        ("max_dd", False), ("max_cl", False),
    ]:
        a = a_metrics.get(m, 0)
        b = b_metrics.get(m, 0)
        delta = round(b - a, 3)
        out[m]          = delta
        out[f"{m}_pass"]= (delta >= 0) if higher_better else (delta <= 0)

    out["overall"] = all(
        out[f"{k}_pass"]
        for k in ("win_rate", "profit_factor", "sharpe", "max_cl", "max_dd")
    )
    return out


# ── Output / reporting ────────────────────────────────────────────────────────

def _fmt_row(r: dict, rank: int) -> str:
    rvol = "RVOL✓" if r.get("use_rvol") else "     "
    rsob = r.get("rsi_sell_ob", 0)
    rsob_str = f"ob={rsob:>2}" if rsob else "ob=--"
    return (
        f"  {rank:>3}. WR={r['win_rate']:>5.1f}%  "
        f"PF={r['profit_factor']:>4.2f}  "
        f"Sh={r['sharpe']:>5.3f}  "
        f"SL={r['stop_loss']*100:>4.1f}%  TP={r['take_profit']*100:>4.1f}%  "
        f"rsi<{r['rsi_buy_max']:>2} {rsob_str}  "
        f"hc={r['hist_confirm']} mh={r['min_hold_bars']} MH={r['max_hold_bars']}  "
        f"MACD={r['macd_fast']}/{r['macd_slow']}/{r['macd_sig']}  {rvol}  "
        f"DD={r['max_dd']:>4.1f}%  CL={r['max_cl']:>2}  "
        f"n={r['trades']:>4}  score={r['score']:>5.1f}"
    )


def print_phase(results: list[dict], label: str, top_n: int = 10):
    print()
    width = 145
    print("=" * width)
    print(f"  {label}  (top {min(top_n, len(results))} by composite score)")
    print(
        "  Rank  WinRate   PF    Sharpe   SL     TP    RSI-buy  RSI-ob  "
        "hc mh MH  MACD        RVOL  MaxDD  CL  Trades  Score"
    )
    print("-" * width)
    for rank, r in enumerate(results[:top_n], 1):
        print(_fmt_row(r, rank))
    print("=" * width)


def print_ab(a_label: str, a_m: dict, b_label: str, b_m: dict):
    delta = ab_compare(a_m, b_m)
    print()
    print("=" * 72)
    print("  A/B COMPARISON")
    print(f"  A = {a_label}")
    print(f"  B = {b_label}")
    print("=" * 72)
    rows = [
        ("win_rate",      "Win rate %",      "%",  True),
        ("profit_factor", "Profit factor",   "x",  True),
        ("sharpe",        "Sharpe ratio",    "",   True),
        ("wr_ratio",      "Avg win/loss",    "x",  True),
        ("max_dd",        "Max drawdown",    "%",  False),
        ("max_cl",        "Max consec loss", "",   False),
        ("score",         "Composite score", "",   True),
    ]
    for key, label, unit, higher_better in rows:
        a_v = a_m.get(key, 0)
        b_v = b_m.get(key, 0)
        d   = delta.get(key, 0)
        win = "B ✓" if delta.get(f"{key}_pass") else "A !"
        print(
            f"  {label:<22}  A={a_v:>7.3f}{unit}  "
            f"B={b_v:>7.3f}{unit}  Δ={d:>+.3f}  [{win}]"
        )
    verdict = "✅  B beats A on all key metrics" if delta["overall"] else "⚠️   B mixed vs A — review manually"
    print(f"\n  {verdict}")
    print("=" * 72)


def print_recommendation(best: dict, phase: str):
    print()
    print("=" * 72)
    print(f"  RECOMMENDED signal_engine.env SETTINGS  [{phase}]")
    print("=" * 72)
    lines = [
        f"HIST_CONFIRM_BARS={best['hist_confirm']}",
        f"RSI_BUY_MAX={best['rsi_buy_max']}",
        f"RSI_SELL_OVERBOUGHT={best.get('rsi_sell_ob', 0)}",
        f"STOP_LOSS={best['stop_loss'] * 100:.1f}",
        f"TAKE_PROFIT={best['take_profit'] * 100:.1f}",
        f"MIN_HOLD_SECONDS={best['min_hold_bars'] * 60}",
        f"MAX_HOLD_MINUTES={best['max_hold_bars']}",
        f"MACD_FAST={best['macd_fast']}",
        f"MACD_SLOW={best['macd_slow']}",
        f"MACD_SIG={best['macd_sig']}",
        f"BUY_FILTER_RVOL={'true' if best.get('use_rvol') else 'false'}",
        f"RVOL_MIN={best.get('rvol_min', 3.0):.1f}",
    ]
    for line in lines:
        print(f"  {line}")
    print()
    m = best
    print(
        f"  Stats:  WR={m['win_rate']:.1f}%  PF={m['profit_factor']:.2f}  "
        f"Sh={m['sharpe']:.3f}  AvgWin={m['avg_win']:.3f}%  "
        f"AvgLoss={m['avg_loss']:.3f}%  "
        f"MaxDD={m['max_dd']:.1f}%  MaxCL={m['max_cl']}  "
        f"Trades={m['trades']}  Score={m['score']:.1f}"
    )
    print("=" * 72)


# ── CSV export ────────────────────────────────────────────────────────────────

_COLS = [
    "symbol", "phase",
    "hist_confirm", "rsi_buy_max", "rsi_sell_ob", "stop_loss", "take_profit",
    "min_hold_bars", "max_hold_bars", "use_rvol", "rvol_min",
    "macd_fast", "macd_slow", "macd_sig",
    "trades", "wins", "win_rate", "profit_factor", "sharpe",
    "avg_win", "avg_loss", "wr_ratio", "max_dd", "max_cl",
    "total_pnl", "avg_hold", "score",
]


def save_csv(rows: list[dict], out_dir: Path):
    out = out_dir / "backtest_v2_results.csv"
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"\n  📄  Summary  → {out}")

    # Trade-by-trade log for validated configs
    val_rows = [r for r in rows if r.get("phase") == "validate"]
    if val_rows:
        tout = out_dir / "backtest_v2_trades.csv"
        tcols = ["symbol", "phase", "buy_time", "sell_time",
                 "buy_price", "sell_price", "pnl_pct", "hold_bars", "reason", "win"]
        with open(tout, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=tcols, extrasaction="ignore")
            w.writeheader()
            for row in val_rows[:5]:
                for t in row.get("_trades", []):
                    w.writerow({"symbol": row["symbol"], "phase": row["phase"], **t})
        print(f"  📄  Trades   → {tout}")


# ── Per-symbol excellence loop (shared by live and demo modes) ───────────────

def _run_symbol_loop(symbol: str, df,           # df=None triggers synthetic gen
                     api_key, secret_key, args,
                     demo: bool = False,
                     seed_offset: int = 0,
                     all_rows: list = None):
    if all_rows is None:
        all_rows = []

    if demo:
        n_days  = args.months * 21
        n_seeds = getattr(args, "seeds", 3)

        # Aggregate metrics across seeds for robustness
        phase1_agg: dict[str, list] = {}
        for seed in range(n_seeds):
            print(f"\n{'═'*72}")
            print(f"  EXCELLENCE LOOP — {symbol}  [SYNTHETIC seed={seed + seed_offset}]")
            print(f"{'═'*72}")
            synth_df             = generate_synthetic_bars(n_days=n_days,
                                                           seed=seed + seed_offset)
            train_df, test_df    = train_test_split(synth_df)
            print(f"  {len(synth_df):,} synthetic bars  "
                  f"train={len(train_df):,}  test={len(test_df):,}\n")
            _run_one_seed(symbol, train_df, test_df, args, all_rows,
                          seed=seed, phase1_agg=phase1_agg, demo=True)
    else:
        print(f"\n{'═'*72}")
        print(f"  EXCELLENCE LOOP — {symbol}")
        print(f"{'═'*72}")
        print(f"  {len(df):,} bars  "
              f"({df['time'].iloc[0][:10]} → {df['time'].iloc[-1][:10]})")
        train_df, test_df = train_test_split(df)
        print(f"  Train: {len(train_df):,} bars  |  "
              f"Held-out test: {len(test_df):,} bars\n")
        _run_one_seed(symbol, train_df, test_df, args, all_rows,
                      seed=0, phase1_agg=None, demo=False)


def _run_one_seed(symbol: str, train_df: pd.DataFrame, test_df: pd.DataFrame,
                  args, all_rows: list, seed: int,
                  phase1_agg: Optional[dict], demo: bool):
    seed_tag = f" [seed {seed}]" if demo else ""

    # ── Baseline A ───────────────────────────────────────────────────────────
    print(f"  ── Baseline (A): current signal_engine.env{seed_tag} ──")
    base_df      = add_indicators(train_df,
                                  BASELINE["macd_fast"],
                                  BASELINE["macd_slow"],
                                  BASELINE["macd_sig"])
    base_metrics = calc_metrics(simulate(base_df, BASELINE))
    print(
        f"  WR={base_metrics['win_rate']}%  "
        f"PF={base_metrics['profit_factor']}  "
        f"Sh={base_metrics['sharpe']}  "
        f"DD={base_metrics['max_dd']}%  "
        f"CL={base_metrics['max_cl']}  "
        f"n={base_metrics['trades']}  "
        f"Score={base_metrics['score']}"
    )
    all_rows.append({"symbol": symbol, "phase": f"baseline{seed_tag}",
                     **BASELINE, **base_metrics})

    # ── Phase 1: Broad sweep ─────────────────────────────────────────────────
    print(f"\n  ── Phase 1: Broad Sweep (768 combos){seed_tag} ──")
    p1 = run_sweep(symbol, train_df, BROAD_GRID, "Phase1")
    for r in p1[:25]:
        all_rows.append({**r, "phase": f"phase1{seed_tag}"})
    print_phase(p1, f"Phase 1 — {symbol}{seed_tag}", top_n=args.top)
    top15 = p1[:15]

    # ── Phase 2: Narrow sweep ────────────────────────────────────────────────
    print(f"\n  ── Phase 2: Narrow Sweep (fine-tuning top-15){seed_tag} ──")
    narrow_grid = build_narrow_grid(top15)
    p2          = run_sweep(symbol, train_df, narrow_grid, "Phase2",
                            max_combos=_PHASE2_MAX)
    for r in p2[:25]:
        all_rows.append({**r, "phase": f"phase2{seed_tag}"})
    print_phase(p2, f"Phase 2 — {symbol}{seed_tag}", top_n=args.top)
    top5 = p2[:5]

    # ── Phase 3: Out-of-sample validation ────────────────────────────────────
    print(f"\n  ── Phase 3: Validation on held-out test data{seed_tag} ──")
    val_results: list[dict] = []
    for p in top5:
        params = {k: p[k] for k in [
            "hist_confirm", "rsi_buy_max", "rsi_sell_ob",
            "stop_loss", "take_profit", "min_hold_bars", "max_hold_bars",
            "use_rvol", "rvol_min", "macd_fast", "macd_slow", "macd_sig",
        ]}
        df_val  = add_indicators(test_df,
                                 params["macd_fast"],
                                 params["macd_slow"],
                                 params["macd_sig"])
        trades  = simulate(df_val, params)
        metrics = calc_metrics(trades)
        row     = {"symbol": symbol, "phase": f"validate{seed_tag}",
                   **params, **metrics, "_trades": trades}
        val_results.append(row)
        all_rows.append(row)

    val_results.sort(key=lambda r: r["score"], reverse=True)
    print_phase(val_results, f"Phase 3 Validation — {symbol}{seed_tag}", top_n=5)

    # ── A/B Comparison ───────────────────────────────────────────────────────
    if val_results:
        best_val = val_results[0]

        base_test_df     = add_indicators(test_df,
                                          BASELINE["macd_fast"],
                                          BASELINE["macd_slow"],
                                          BASELINE["macd_sig"])
        base_test_metrics = calc_metrics(simulate(base_test_df, BASELINE))

        a_label = (
            f"Baseline (hc={BASELINE['hist_confirm']}  "
            f"rsi<{BASELINE['rsi_buy_max']}  "
            f"SL={BASELINE['stop_loss']*100:.1f}%  "
            f"TP={BASELINE['take_profit']*100:.1f}%)"
        )
        b_label = (
            f"Best B (hc={best_val['hist_confirm']}  "
            f"rsi<{best_val['rsi_buy_max']}  "
            f"SL={best_val['stop_loss']*100:.1f}%  "
            f"TP={best_val['take_profit']*100:.1f}%  "
            f"RVOL={'on' if best_val.get('use_rvol') else 'off'})"
        )
        print_ab(a_label, base_test_metrics, b_label, best_val)
        print_recommendation(best_val, f"Phase 3 validated{seed_tag}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Excellence Loop Backtester — mission: small consistent wins"
    )
    parser.add_argument("tickers", nargs="+", help="Ticker symbols to test")
    parser.add_argument("--months", type=int, default=3,
                        help="Months of bar history (default: 3)")
    parser.add_argument("--top", type=int, default=10,
                        help="Top N configs to show per phase (default: 10)")
    parser.add_argument("--demo", action="store_true",
                        help="Use synthetic data (no Alpaca connection needed)")
    parser.add_argument("--seeds", type=int, default=3,
                        help="Number of synthetic seeds to average over (--demo only, default: 3)")
    args = parser.parse_args()

    all_rows: list[dict] = []

    if args.demo:
        # ── Demo mode: synthetic data, no network needed ─────────────────────
        print("\n  [DEMO MODE] Running on synthetic sub-$5 momentum stock data")
        print(f"  Generating {args.months * 21} trading days × 390 bars, "
              f"{args.seeds} independent seeds\n")
        symbols = [t.upper() for t in args.tickers]
        for sym_idx, symbol in enumerate(symbols):
            _run_symbol_loop(symbol, None, None, None, args,
                             demo=True, seed_offset=sym_idx * 100,
                             all_rows=all_rows)
        if all_rows:
            save_csv(all_rows, _HERE)
        print("\n  ✅  Excellence loop complete.\n")
        return

    # ── Live mode: Alpaca data ────────────────────────────────────────────────
    api_key, secret_key = _load_creds()
    if not api_key or not secret_key:
        print("❌  Alpaca credentials not found — set in signal_engine.env")
        print("    Tip: run with --demo to benchmark on synthetic data instead")
        sys.exit(1)

    for symbol in [t.upper() for t in args.tickers]:
        print(f"\n{'═'*72}")
        print(f"  EXCELLENCE LOOP — {symbol}")
        print(f"{'═'*72}")

        df = fetch_bars(symbol, api_key, secret_key, months=args.months)
        if df is None or len(df) < 300:
            print(f"  ⚠️  Not enough data for {symbol} — skipping")
            continue
        _run_symbol_loop(symbol, df, api_key, secret_key, args,
                         demo=False, all_rows=all_rows)

    # ── Save CSVs ─────────────────────────────────────────────────────────────
    if all_rows:
        save_csv(all_rows, _HERE)

    print("\n  ✅  Excellence loop complete.\n")


if __name__ == "__main__":
    main()
