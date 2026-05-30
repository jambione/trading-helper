#!/usr/bin/env python3
"""
backtest_3ind.py — Backtest the 3-indicator strategy (CM RSI-2 + %R Exhaustion + MACD).

Replays strategy_three_indicator.py over real Alpaca 1-minute history with
HONEST fills:
  • A signal computed on the close of bar i fills at bar i+1's OPEN (no
    same-bar lookahead — the flaw in the older backtest.py).
  • Slippage (bps) is applied adverse to every fill.
  • An optional hard stop-loss / take-profit acts intrabar as a safety net.

Because it imports the strategy's pure functions, this tests the SAME logic
that will later drive the live engine — not a parallel copy.

USAGE
    python backtest_3ind.py NVDA
    python backtest_3ind.py NVDA TSLA --months 2 --slippage 25
    python backtest_3ind.py NVDA --exit-mode all --stop 2 --target 4
    python backtest_3ind.py NVDA --sweep            # grid over key thresholds

Credentials: ALPACA_API_KEY / ALPACA_SECRET_KEY from signal_engine.env (same
file the engine and backtest.py use).
"""

from __future__ import annotations

import argparse
import sys
from itertools import product
from pathlib import Path

import pandas as pd

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))

import strategy_three_indicator as strat
from backtest import _load_alpaca_credentials, calc_stats, fetch_history


# ── Core simulation ───────────────────────────────────────────────────────────

def simulate(df: pd.DataFrame, p: dict,
             slippage_bps: float = 15.0,
             stop_loss_pct: float = 0.0,
             take_profit_pct: float = 0.0) -> list[dict]:
    """
    Walk bars applying the strategy. Returns a list of completed-trade dicts
    compatible with backtest.calc_stats (pnl_pct, hold_bars, win, …).

    Fills: signal on close[i] → trade at open[i+1]. Slippage adverse both sides.
    Stop/TP (if > 0) checked intrabar against bar i's high/low while in position.
    """
    df = strat.compute_indicators(df, p)
    a = strat.to_arrays(df)
    n = len(df)

    open_ = a["open"]
    high  = a["high"]
    low   = a["low"]
    has_time = "time" in df.columns
    time_col = df["time"].to_numpy() if has_time else None
    slip = slippage_bps / 10_000.0

    trades: list[dict] = []
    in_pos = False
    entry_px = entry_bar = 0.0
    entry_time = ""

    def _close(sell_bar: int, sell_px: float, reason: str):
        nonlocal in_pos
        pnl = (sell_px - entry_px) / entry_px * 100.0
        trades.append({
            "buy_bar": int(entry_bar), "sell_bar": int(sell_bar),
            "buy_price": round(entry_px, 4), "sell_price": round(sell_px, 4),
            "pnl_pct": round(pnl, 4), "hold_bars": int(sell_bar - entry_bar),
            "buy_time": entry_time,
            "sell_time": str(time_col[sell_bar]) if has_time else str(sell_bar),
            "reason": reason, "win": pnl > 0,
        })
        in_pos = False

    # Last bar can't be an entry bar (no i+1 open to fill on).
    for i in range(n - 1):
        if in_pos:
            # 1) Protective stop / target — intrabar on THIS bar.
            if stop_loss_pct > 0:
                stop_px = entry_px * (1 - stop_loss_pct / 100.0)
                if low[i] <= stop_px:
                    _close(i, stop_px * (1 - slip), "stop_loss")
                    continue
            if take_profit_pct > 0:
                tgt_px = entry_px * (1 + take_profit_pct / 100.0)
                if high[i] >= tgt_px:
                    _close(i, tgt_px * (1 - slip), "take_profit")
                    continue
            # 2) Strategy reversal exit — fill next open.
            if strat.sell_signal(a, i, p):
                _close(i + 1, open_[i + 1] * (1 - slip), "reversal")
                continue
        else:
            if strat.buy_signal(a, i, p):
                entry_px = open_[i + 1] * (1 + slip)
                entry_bar = i + 1
                entry_time = str(time_col[i + 1]) if has_time else str(i + 1)
                in_pos = True

    # Close any open position at the final bar's close.
    if in_pos:
        _close(n - 1, a["close"][n - 1] * (1 - slip), "end_of_data")

    return trades


# ── Rendering ─────────────────────────────────────────────────────────────────

def _print_stats(label: str, s: dict):
    if not s.get("trades"):
        print(f"  {label}: no trades triggered")
        return
    print(f"  {label}")
    print(f"    trades {s['trades']}  |  win {s['win_rate']}%  |  "
          f"avg {s['avg_pnl_pct']:+.3f}%  |  total {s['total_pnl_pct']:+.2f}%  "
          f"(${s['total_dollar_pnl']:,.0f} @ $10k)")
    print(f"    best {s['best_trade']:+.2f}%  worst {s['worst_trade']:+.2f}%  "
          f"avg hold {s['avg_hold_bars']} bars  max consec losses {s['max_consec_losses']}")


def _reason_breakdown(trades: list[dict]):
    if not trades:
        return
    wins_by: dict = {}
    for t in trades:
        wins_by.setdefault(t["reason"], [0, 0])
        wins_by[t["reason"]][0] += 1 if t["win"] else 0
        wins_by[t["reason"]][1] += 1
    print("    exits:", "  ".join(
        f"{r}={w}/{tot}W" for r, (w, tot) in wins_by.items()))


# ── Parameter sweep ───────────────────────────────────────────────────────────

_SWEEP = {
    "cm_rsi_buy_max": [35, 40, 45],
    "macd_sep_mult":  [0.5, 1.0, 1.5],
    "confirm_window": [4, 8, 12],
    "exit_mode":      ["any", "all"],
}


def run_sweep(df: pd.DataFrame, slippage_bps: float, stop: float, target: float):
    keys = list(_SWEEP)
    rows = []
    combos = list(product(*(_SWEEP[k] for k in keys)))
    print(f"  Sweeping {len(combos)} combos …")
    for vals in combos:
        p = strat.params(**dict(zip(keys, vals)))
        s = calc_stats(simulate(df, p, slippage_bps, stop, target))
        rows.append((dict(zip(keys, vals)), s))
    rows.sort(key=lambda r: r[1].get("total_dollar_pnl", 0), reverse=True)

    print("\n  Top combos (ranked by total P&L — treat as a robustness scan, NOT a")
    print("  recommendation; the best of many combos is partly curve-fit):")
    print(f"  {'cm<':>4} {'sep':>4} {'cw':>3} {'exit':>5} {'trades':>7} {'win%':>6} {'total%':>8} {'$@10k':>9}")
    for cfg, s in rows[:12]:
        if not s.get("trades"):
            continue
        print(f"  {cfg['cm_rsi_buy_max']:>4} {cfg['macd_sep_mult']:>4} "
              f"{cfg['confirm_window']:>3} {cfg['exit_mode']:>5} "
              f"{s['trades']:>7} {s['win_rate']:>6} {s['total_pnl_pct']:>+8.2f} "
              f"{s['total_dollar_pnl']:>+9,.0f}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Backtest the 3-indicator strategy.")
    ap.add_argument("tickers", nargs="+", help="Symbols, e.g. NVDA TSLA")
    ap.add_argument("--months", type=int, default=3, help="History to fetch (default 3)")
    ap.add_argument("--slippage", type=float, default=15.0, help="Per-side slippage bps (default 15)")
    ap.add_argument("--stop", type=float, default=0.0, help="Protective stop-loss %% (0=off)")
    ap.add_argument("--target", type=float, default=0.0, help="Take-profit %% (0=off)")
    ap.add_argument("--exit-mode", choices=["any", "all"], default="any",
                    help="Exit on first reversal signal (any) or require all three (all)")
    ap.add_argument("--sweep", action="store_true", help="Grid-search key thresholds")
    args = ap.parse_args()

    api_key, secret_key = _load_alpaca_credentials()
    if not api_key or not secret_key:
        print("❌ Alpaca credentials not found — set ALPACA_API_KEY / ALPACA_SECRET_KEY "
              "in signal_engine.env")
        sys.exit(1)

    for ticker in (t.upper() for t in args.tickers):
        print(f"\n{'─' * 64}\n  {ticker}\n{'─' * 64}")
        df = fetch_history(ticker, api_key, secret_key, months=args.months)
        if df is None or len(df) < 200:
            print(f"  ⚠️  not enough data for {ticker} — skipping")
            continue

        if args.sweep:
            run_sweep(df, args.slippage, args.stop, args.target)
            continue

        p = strat.params(exit_mode=args.exit_mode)
        trades = simulate(df, p, args.slippage, args.stop, args.target)
        _print_stats(f"{ticker}  (exit={args.exit_mode}, slip={args.slippage}bps"
                     f"{f', stop {args.stop}%' if args.stop else ''}"
                     f"{f', target {args.target}%' if args.target else ''})",
                     calc_stats(trades))
        _reason_breakdown(trades)


if __name__ == "__main__":
    main()
