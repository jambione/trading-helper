#!/usr/bin/env python3
"""
backtest.py — Signal Engine Backtester

Replays the RSI + MACD momentum strategy against 3 months of real
historical 1-minute bars, then sweeps through combinations of key
parameters to find which settings perform best.

WHAT IT TESTS
─────────────
  RSI overbought limit  : 60, 65, 70, 75
  MACD fast/slow/signal : (8,21,9)  (10,21,9)  (12,26,9)
  Stop-loss %           : 0.5%, 1.0%, 2.0%
  Take-profit %         : 2.0%, 4.0%, 6.0%

  That's 144 combinations per ticker.

HOW TO RUN
──────────
  python backtest.py AAPL
  python backtest.py AAPL TSLA NVDA
  python backtest.py AAPL --months 1
  python backtest.py AAPL --top 10

  Results are printed to the console and saved to backtest_results.csv
  in the same folder.

HOW IT WORKS
────────────
  1. Downloads historical 1-min OHLCV bars from Alpaca (handles
     pagination automatically — 3 months is ~24,000+ bars per ticker).
  2. Pre-computes RSI and MACD histogram for the full bar series in one
     pass (fast — not recalculated per bar).
  3. Walks through every bar running the exact same state machine as
     signal_engine.py:
       • hist > 0 AND hist > prev → histogram growing
       • hist_growing AND rsi < RSI_MAX → BUY at next bar's open
       • hist was growing, now shrinking → SELL (momentum reversal)
       • OR stop-loss / take-profit level hit → SELL immediately
  4. Collects every completed trade with entry/exit price + reason.
  5. Calculates stats per parameter combo and ranks by total return.

CREDENTIALS
───────────
  Reads from signal_engine.env (same file used by signal_engine.py).
  ALPACA_API_KEY and ALPACA_SECRET_KEY must be set.
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

import pandas as pd
import requests

# ── Load our signal library ───────────────────────────────────────────────────
_HERE = Path(__file__).parent
_ROOT = _HERE.parent  # repo root — this tool now lives under tools/
sys.path.insert(0, str(_ROOT))  # signals / strategy_three_indicator live at root
from signals import rsi as calc_rsi, compute_macd


# ── Load credentials from signal_engine.env ──────────────────────────────────

def _load_env_file(path: Path):
    """Read KEY=VALUE lines from an env file into os.environ (shell wins)."""
    if not path.exists():
        return
    with open(path, encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key and key not in os.environ:
                os.environ[key] = value.strip()

_load_env_file(_ROOT / "signal_engine.env")


def _load_alpaca_credentials() -> tuple[str, str]:
    api_key    = os.getenv("ALPACA_API_KEY", "")
    secret_key = os.getenv("ALPACA_SECRET_KEY", "")
    if not api_key or not secret_key:
        secrets_path = _ROOT / "config" / "secrets.json"
        if secrets_path.exists():
            try:
                s = json.loads(secrets_path.read_text())
                api_key    = api_key    or s.get("api_key", "")
                secret_key = secret_key or s.get("secret_key", "")
            except Exception:
                pass
    return api_key, secret_key


# ── Parameter grid ────────────────────────────────────────────────────────────
# Edit these lists to change what gets tested.
# More values = more combinations = longer run time.

RSI_MAX_VALUES    = [60, 65, 70, 75]          # RSI overbought cutoff
MACD_CONFIGS      = [                         # (fast, slow, signal)
    (8,  21, 9),
    (10, 21, 9),
    (12, 26, 9),
]
STOP_LOSS_VALUES  = [0.005, 0.01, 0.02]       # 0.5%, 1.0%, 2.0%
TAKE_PROFIT_VALUES= [0.02,  0.04, 0.06]       # 2.0%, 4.0%, 6.0%

RSI_PERIOD = 14     # RSI lookback period (not swept — standard value)


# ── Alpaca bar fetching with pagination ───────────────────────────────────────

ALPACA_BASE_URL = "https://data.alpaca.markets"

def fetch_history(symbol: str, api_key: str, secret_key: str,
                  months: int = 3, adjustment: str = "split") -> Optional[pd.DataFrame]:
    """
    Download N months of 1-minute OHLCV bars from Alpaca.

    Alpaca returns a maximum of 10,000 bars per request, so we follow
    the 'next_page_token' cursor until all bars are collected.

    adjustment : Alpaca corporate-action adjustment — "raw" (none), "split",
                 "dividend", or "all". Defaults to "split": sub-$1 microcaps
                 reverse-split constantly, and raw bars show a 1:50 split as a
                 single 50x jump that poisons the indicators. "split" rescales
                 history on Alpaca's side so the series is continuous.

    Returns a DataFrame with columns: time, open, high, low, close, volume
    sorted oldest → newest.  Returns None on failure.
    """
    if not api_key or not secret_key:
        print("  ❌  Alpaca credentials missing — set ALPACA_API_KEY / ALPACA_SECRET_KEY "
              "in signal_engine.env")
        return None

    headers  = {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret_key}
    url      = f"{ALPACA_BASE_URL}/v2/stocks/{symbol}/bars"
    start_dt = (datetime.now(timezone.utc) - timedelta(days=months * 31)
                ).strftime("%Y-%m-%dT%H:%M:%SZ")

    all_bars   = []
    page_token = None
    page_num   = 0

    print(f"  Fetching {months}-month bar history for {symbol}…", end="", flush=True)

    while True:
        params: dict = {
            "timeframe":  "1Min",
            "start":      start_dt,
            "limit":      10000,
            "feed":       "iex",
            "sort":       "asc",
            "adjustment": adjustment,
        }
        if page_token:
            params["page_token"] = page_token

        try:
            resp = requests.get(url, params=params, headers=headers, timeout=30)
            resp.raise_for_status()
            data       = resp.json()
            bars       = data.get("bars", [])
            page_token = data.get("next_page_token")
            all_bars.extend(bars)
            page_num  += 1
            print(".", end="", flush=True)

            if not page_token:
                break          # no more pages

            time.sleep(0.25)   # be polite to Alpaca's rate limiter

        except requests.exceptions.HTTPError as e:
            if resp.status_code == 422:
                # IEX may not have data — retry with SIP feed
                params["feed"] = "sip"
                try:
                    resp = requests.get(url, params=params, headers=headers, timeout=30)
                    resp.raise_for_status()
                    data       = resp.json()
                    bars       = data.get("bars", [])
                    page_token = data.get("next_page_token")
                    all_bars.extend(bars)
                    page_num  += 1
                    print("s", end="", flush=True)
                    if not page_token:
                        break
                    time.sleep(0.25)
                except Exception as e2:
                    print(f"\n  ❌  SIP feed also failed: {e2}")
                    break
            else:
                print(f"\n  ❌  HTTP {resp.status_code}: {e}")
                break
        except Exception as e:
            print(f"\n  ❌  Fetch error: {e}")
            break

    print(f" {len(all_bars):,} bars")

    if not all_bars:
        return None

    df = pd.DataFrame(all_bars).rename(columns={
        "o": "open", "h": "high", "l": "low",
        "c": "close", "v": "volume", "t": "time",
    })
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = df[col].astype(float)
    df = df.reset_index(drop=True)
    return df


# ── Core simulation ───────────────────────────────────────────────────────────

def _precompute_indicators(df: pd.DataFrame, macd_fast: int,
                           macd_slow: int, macd_sig: int) -> pd.DataFrame:
    """
    Compute RSI and MACD histogram for every bar in the DataFrame.
    Done ONCE per (ticker, MACD config) — not repeated per bar.
    """
    cfg    = {"macd_fast": macd_fast, "macd_slow": macd_slow, "macd_signal": macd_sig}
    result = compute_macd(df.copy(), cfg)
    result["rsi"] = calc_rsi(df["close"], RSI_PERIOD)
    return result


def simulate(df_ind: pd.DataFrame, params: dict) -> list[dict]:
    """
    Walk through pre-computed indicators bar by bar, running the exact same
    state machine used by signal_engine.py.

    Rules:
      BUY  — hist > 0 AND hist > prev_hist AND rsi < rsi_max AND no open position
      SELL — hist was growing, now shrinking (momentum reversal)
           OR stop-loss hit  (price fell  stop_loss% from entry)
           OR take-profit hit (price rose take_profit% from entry)

    Returns a list of trade dicts, one per completed trade.
    """
    rsi_max     = params["rsi_max"]
    stop_loss   = params["stop_loss"]    # fraction, e.g. 0.01
    take_profit = params["take_profit"]  # fraction, e.g. 0.04

    warmup = df_ind["macd_hist"].first_valid_index()
    if warmup is None:
        return []
    warmup = int(warmup) + 1

    trades      = []
    prev_hist   = None
    hist_growing= False
    in_position = False
    buy_price   = None
    buy_bar_idx = None
    buy_time    = None

    hist_col  = df_ind["macd_hist"].values
    rsi_col   = df_ind["rsi"].values
    close_col = df_ind["close"].values
    time_col  = df_ind["time"].values if "time" in df_ind.columns else list(range(len(df_ind)))
    n         = len(df_ind)

    for i in range(warmup, n):
        hist  = float(hist_col[i])
        rsi   = float(rsi_col[i])
        price = float(close_col[i])

        if pd.isna(hist) or pd.isna(rsi):
            prev_hist = hist if not pd.isna(hist) else prev_hist
            continue

        # ── Stop-loss / take-profit check ─────────────────────────────────
        if in_position and buy_price is not None:
            pnl = (price - buy_price) / buy_price
            if pnl <= -stop_loss:
                trades.append(_make_trade(
                    buy_bar=buy_bar_idx, sell_bar=i,
                    buy_price=buy_price, sell_price=price,
                    buy_time=buy_time, sell_time=str(time_col[i]),
                    reason="stop_loss",
                ))
                in_position  = False
                buy_price    = None
                buy_bar_idx  = None
                hist_growing = False
                prev_hist    = hist
                continue
            elif pnl >= take_profit:
                trades.append(_make_trade(
                    buy_bar=buy_bar_idx, sell_bar=i,
                    buy_price=buy_price, sell_price=price,
                    buy_time=buy_time, sell_time=str(time_col[i]),
                    reason="take_profit",
                ))
                in_position  = False
                buy_price    = None
                buy_bar_idx  = None
                hist_growing = False
                prev_hist    = hist
                continue

        # ── MACD state machine ────────────────────────────────────────────
        if prev_hist is not None:
            if hist > 0 and hist > prev_hist:
                # Histogram positive AND growing → BUY zone
                hist_growing = True

            elif hist_growing and hist < prev_hist:
                # Was growing, now shrinking → momentum reversal → SELL
                if in_position and buy_price is not None:
                    trades.append(_make_trade(
                        buy_bar=buy_bar_idx, sell_bar=i,
                        buy_price=buy_price, sell_price=price,
                        buy_time=buy_time, sell_time=str(time_col[i]),
                        reason="macd_reversal",
                    ))
                    in_position = False
                    buy_price   = None
                    buy_bar_idx = None
                hist_growing = False

        # ── BUY trigger ───────────────────────────────────────────────────
        if hist_growing and not in_position and rsi < rsi_max and price > 0:
            in_position = True
            buy_price   = price
            buy_bar_idx = i
            buy_time    = str(time_col[i])

        prev_hist = hist

    # Close any position still open at the end of the data
    if in_position and buy_price is not None:
        price = float(close_col[-1])
        trades.append(_make_trade(
            buy_bar=buy_bar_idx, sell_bar=n - 1,
            buy_price=buy_price, sell_price=price,
            buy_time=buy_time, sell_time=str(time_col[-1]),
            reason="end_of_data",
        ))

    return trades


def _make_trade(buy_bar: int, sell_bar: int,
                buy_price: float, sell_price: float,
                buy_time: str, sell_time: str,
                reason: str) -> dict:
    pnl_pct = (sell_price - buy_price) / buy_price * 100
    return {
        "buy_bar":   buy_bar,
        "sell_bar":  sell_bar,
        "buy_price": round(buy_price,  4),
        "sell_price": round(sell_price, 4),
        "pnl_pct":   round(pnl_pct,    4),
        "hold_bars": sell_bar - buy_bar,
        "buy_time":  buy_time,
        "sell_time": sell_time,
        "reason":    reason,
        "win":       pnl_pct > 0,
    }


# ── Statistics ────────────────────────────────────────────────────────────────

def calc_stats(trades: list[dict]) -> dict:
    """
    Summarise a list of completed trades into a stats dict.
    Assumes $10,000 per trade (fixed position size) for dollar P&L.
    """
    if not trades:
        return {
            "trades": 0, "wins": 0, "win_rate": 0.0,
            "avg_pnl_pct": 0.0, "total_pnl_pct": 0.0,
            "total_dollar_pnl": 0.0, "avg_hold_bars": 0.0,
            "best_trade": 0.0, "worst_trade": 0.0,
            "max_consec_losses": 0,
        }

    pnls         = [t["pnl_pct"] for t in trades]
    wins         = sum(1 for p in pnls if p > 0)
    hold_bars    = [t["hold_bars"] for t in trades]

    # Max consecutive losses
    max_consec = consec = 0
    for p in pnls:
        if p <= 0:
            consec += 1
            max_consec = max(max_consec, consec)
        else:
            consec = 0

    position_size = 10_000   # dollars per trade (assumed fixed)

    return {
        "trades":            len(trades),
        "wins":              wins,
        "win_rate":          round(wins / len(trades) * 100, 1),
        "avg_pnl_pct":       round(sum(pnls) / len(pnls), 3),
        "total_pnl_pct":     round(sum(pnls), 3),
        "total_dollar_pnl":  round(sum(p / 100 * position_size for p in pnls), 2),
        "avg_hold_bars":     round(sum(hold_bars) / len(hold_bars), 1),
        "best_trade":        round(max(pnls), 3),
        "worst_trade":       round(min(pnls), 3),
        "max_consec_losses": max_consec,
    }


# ── Parameter sweep ───────────────────────────────────────────────────────────

def run_sweep(ticker: str, df: pd.DataFrame) -> list[dict]:
    """
    Run every parameter combination against the full bar history for one ticker.
    Returns a list of result dicts (one per combo) sorted by total dollar P&L.
    """
    results = []

    # Group by MACD config so we only precompute indicators once per config
    total_combos = (len(RSI_MAX_VALUES) * len(MACD_CONFIGS)
                    * len(STOP_LOSS_VALUES) * len(TAKE_PROFIT_VALUES))
    print(f"  Running {total_combos} parameter combinations for {ticker}…")

    done = 0
    for (fast, slow, sig) in MACD_CONFIGS:
        # Pre-compute indicators once for this MACD config
        df_ind = _precompute_indicators(df, fast, slow, sig)

        for rsi_max, sl, tp in product(RSI_MAX_VALUES, STOP_LOSS_VALUES, TAKE_PROFIT_VALUES):
            params = {
                "rsi_max":     rsi_max,
                "macd_fast":   fast,
                "macd_slow":   slow,
                "macd_sig":    sig,
                "stop_loss":   sl,
                "take_profit": tp,
            }
            trades = simulate(df_ind, params)
            stats  = calc_stats(trades)

            results.append({
                "ticker":          ticker,
                "rsi_max":         rsi_max,
                "macd":            f"{fast}/{slow}/{sig}",
                "stop_loss_pct":   round(sl  * 100, 1),
                "take_profit_pct": round(tp  * 100, 1),
                **stats,
                "_trades_detail":  trades,   # kept for CSV export, removed from table
            })
            done += 1

        # Progress indicator every 36 combos (one MACD config done)
        pct = done / total_combos * 100
        print(f"    {done}/{total_combos} ({pct:.0f}%) done…")

    results.sort(key=lambda r: r["total_dollar_pnl"], reverse=True)
    return results


# ── Console output ────────────────────────────────────────────────────────────

def print_summary(results: list[dict], ticker: str, top_n: int = 15):
    """Print a ranked summary table of the top N parameter combos."""
    print()
    print("=" * 90)
    print(f"  BACKTEST RESULTS — {ticker}  (top {top_n} of {len(results)} combos, "
          f"ranked by total dollar P&L assuming $10k/trade)")
    print("=" * 90)
    header = (
        f"{'Rank':>4}  {'RSI':>5}  {'MACD':<10}  {'SL%':>5}  {'TP%':>5}  "
        f"{'Trades':>6}  {'Win%':>6}  {'Avg P&L':>8}  {'Total $':>9}  "
        f"{'Best':>7}  {'Worst':>7}  {'MaxCL':>5}"
    )
    print(header)
    print("-" * 90)

    for rank, r in enumerate(results[:top_n], 1):
        sign  = "+" if r["total_dollar_pnl"] >= 0 else ""
        a_sign = "+" if r["avg_pnl_pct"]     >= 0 else ""
        print(
            f"  {rank:>3}.  "
            f"{r['rsi_max']:>5}  "
            f"{r['macd']:<10}  "
            f"{r['stop_loss_pct']:>4}%  "
            f"{r['take_profit_pct']:>4}%  "
            f"{r['trades']:>6}  "
            f"{r['win_rate']:>5}%  "
            f"{a_sign}{r['avg_pnl_pct']:>6.3f}%  "
            f"{sign}${r['total_dollar_pnl']:>8,.2f}  "
            f"{r['best_trade']:>+6.2f}%  "
            f"{r['worst_trade']:>+7.2f}%  "
            f"{r['max_consec_losses']:>5}"
        )

    print("=" * 90)

    # Highlight the best combo
    best = results[0]
    print(f"\n  🏆  Best combo for {ticker}:")
    print(f"      RSI max={best['rsi_max']}  MACD={best['macd']}  "
          f"stop={best['stop_loss_pct']}%  target={best['take_profit_pct']}%")
    print(f"      {best['trades']} trades  |  {best['win_rate']}% win rate  |  "
          f"avg {'+' if best['avg_pnl_pct']>=0 else ''}{best['avg_pnl_pct']}% per trade  |  "
          f"total {'+' if best['total_dollar_pnl']>=0 else ''}${best['total_dollar_pnl']:,.2f}")
    print()


# ── CSV export ────────────────────────────────────────────────────────────────

def save_csv(all_results: list[dict], out_path: Path):
    """
    Save two CSV files:
      backtest_results.csv      — one row per parameter combo, ranked
      backtest_trades.csv       — full trade-by-trade log
    """
    # ── Summary CSV ──────────────────────────────────────────────────────────
    summary_path = out_path.parent / "backtest_results.csv"
    summary_cols = [
        "ticker", "rsi_max", "macd", "stop_loss_pct", "take_profit_pct",
        "trades", "wins", "win_rate", "avg_pnl_pct", "total_pnl_pct",
        "total_dollar_pnl", "avg_hold_bars", "best_trade", "worst_trade",
        "max_consec_losses",
    ]
    with open(summary_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=summary_cols)
        writer.writeheader()
        for r in all_results:
            writer.writerow({k: r[k] for k in summary_cols})
    print(f"  📄  Summary saved → {summary_path}")

    # ── Trade detail CSV ─────────────────────────────────────────────────────
    trades_path = out_path.parent / "backtest_trades.csv"
    trade_cols  = [
        "ticker", "rsi_max", "macd", "stop_loss_pct", "take_profit_pct",
        "buy_time", "sell_time", "buy_price", "sell_price",
        "pnl_pct", "hold_bars", "reason", "win",
    ]
    with open(trades_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=trade_cols)
        writer.writeheader()
        for r in all_results:
            for t in r.get("_trades_detail", []):
                writer.writerow({
                    "ticker":           r["ticker"],
                    "rsi_max":          r["rsi_max"],
                    "macd":             r["macd"],
                    "stop_loss_pct":    r["stop_loss_pct"],
                    "take_profit_pct":  r["take_profit_pct"],
                    **{k: t[k] for k in trade_cols if k in t},
                })
    print(f"  📄  Trade log saved  → {trades_path}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Backtest the RSI+MACD signal strategy against historical data."
    )
    parser.add_argument(
        "tickers",
        nargs="+",
        help='Ticker symbols to test — e.g.  AAPL TSLA NVDA',
    )
    parser.add_argument(
        "--months",
        type=int,
        default=3,
        help="Months of history to fetch (default: 3)",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=15,
        help="How many top combos to show per ticker (default: 15)",
    )
    args = parser.parse_args()

    api_key, secret_key = _load_alpaca_credentials()
    if not api_key or not secret_key:
        print("❌  Cannot run backtest — Alpaca credentials not found.")
        print("    Add ALPACA_API_KEY and ALPACA_SECRET_KEY to signal_engine.env")
        sys.exit(1)

    all_results: list[dict] = []

    for ticker in [t.upper() for t in args.tickers]:
        print(f"\n{'─'*60}")
        print(f"  {ticker}")
        print(f"{'─'*60}")

        df = fetch_history(ticker, api_key, secret_key, months=args.months)
        if df is None or len(df) < 50:
            print(f"  ⚠️   Not enough data for {ticker} — skipping")
            continue

        print(f"  {len(df):,} bars loaded  "
              f"({df['time'].iloc[0][:10]} → {df['time'].iloc[-1][:10]})")

        results = run_sweep(ticker, df)
        all_results.extend(results)
        print_summary(results, ticker, top_n=args.top)

    if not all_results:
        print("No results to save.")
        return

    save_csv(all_results, _ROOT / "backtest_results.csv")

    # If multiple tickers were tested, show a cross-ticker best
    tickers_tested = list(dict.fromkeys(r["ticker"] for r in all_results))
    if len(tickers_tested) > 1:
        print("\n" + "=" * 60)
        print("  OVERALL BEST COMBO ACROSS ALL TICKERS")
        print("=" * 60)
        # Find the combo key that performs best on average across tickers
        from collections import defaultdict
        combo_totals: dict[str, list[float]] = defaultdict(list)
        for r in all_results:
            key = f"RSI{r['rsi_max']} MACD{r['macd']} SL{r['stop_loss_pct']} TP{r['take_profit_pct']}"
            combo_totals[key].append(r["total_dollar_pnl"])
        best_key = max(combo_totals, key=lambda k: sum(combo_totals[k]))
        best_avg = sum(combo_totals[best_key]) / len(combo_totals[best_key])
        print(f"  {best_key}")
        print(f"  Average P&L across {len(tickers_tested)} tickers: "
              f"{'+' if best_avg >= 0 else ''}${best_avg:,.2f}")
        print()


if __name__ == "__main__":
    main()
