#!/usr/bin/env python3
"""
backtest_exits.py — Which EXIT discipline best captures a microcap pump?

The 3-indicator backtest proved that *indicator entries* have no edge on
low-float microcaps (≈10% win rate). But on these names the entry isn't the
hard part — the catalyst (a chat mention / halt / news) tells you when to get
in. The hard part is the EXIT: pumps spike and dump in minutes, so the rule
that gets you out determines the P&L.

We can't backtest the catalyst itself (audio mentions aren't in price history),
so we use a PROXY entry: a relative-volume spike on a green bar — the footprint
a pump leaves in the bars. Then we hold that entry fixed and compare exit
disciplines (hard stop / time stop / trailing stop / take-profit), pooled
across the whole watchlist for a real sample size.

This answers a reusable question — "what exit rule wins on these names?" — even
though the live entry trigger (the alert) only gets confirmed in paper trading.

HONEST FILLS
  • Entry: spike detected on close[i] → fill at open[i+1] (no same-bar peek).
  • Exits: checked intrabar; trailing peak uses only bars STRICTLY before the
    current one (no lookahead). Slippage applied adverse to every fill.
  • Stop is checked before take-profit within a bar (pessimistic when both
    could trigger).

USAGE
    python backtest_exits.py NAMM HUBC PLCE VCIG ...          # pooled compare
    python backtest_exits.py <tickers> --months 6 --rvol 3
    python backtest_exits.py <tickers> --regular-hours-only
    python backtest_exits.py <tickers> --per-ticker          # also show each name

Credentials: ALPACA_API_KEY / ALPACA_SECRET_KEY from signal_engine.env.
Bars are split-adjusted at the source (fetch_history default).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))

from backtest import _load_alpaca_credentials, calc_stats, fetch_history
from backtest_3ind import filter_regular_hours


# ── Entry proxy: relative-volume spike on a green bar ──────────────────────────

def mark_spikes(df: pd.DataFrame, rvol_mult: float, vol_window: int) -> np.ndarray:
    """
    Edge-triggered entry signal: relative volume crosses up through rvol_mult on
    an up bar. Edge-triggering (was below, now above) keeps one pump from firing
    on every elevated-volume bar. Returns a bool array aligned to df rows.
    """
    vol   = df["volume"].to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)
    open_ = df["open"].to_numpy(dtype=float)

    avg = pd.Series(vol).rolling(vol_window, min_periods=vol_window).mean().to_numpy()
    rvol = np.divide(vol, avg, out=np.zeros_like(vol), where=avg > 0)

    green = close > open_
    above = rvol >= rvol_mult
    prev_above = np.concatenate(([False], above[:-1]))
    return above & ~prev_above & green


# ── One exit discipline ────────────────────────────────────────────────────────

def simulate_exits(df: pd.DataFrame, entries: np.ndarray, cfg: dict,
                   slippage_bps: float = 15.0, cooldown: int = 30) -> list[dict]:
    """
    Walk bars. On a spike, enter next-open. While in position, apply the exit
    cfg intrabar. cfg keys (0/None = disabled):
        stop      hard stop-loss %% below entry
        trail     trailing stop %% below the peak high since entry
        tp        take-profit %% above entry
        max_bars  time stop — force exit after this many bars held
    cooldown: bars to wait after an exit before a new entry (avoid re-firing on
    the same spike's aftershocks).
    """
    open_ = df["open"].to_numpy(dtype=float)
    high  = df["high"].to_numpy(dtype=float)
    low   = df["low"].to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)
    has_time = "time" in df.columns
    tcol = df["time"].to_numpy() if has_time else None
    n = len(df)
    slip = slippage_bps / 10_000.0

    stop     = cfg.get("stop", 0) or 0
    trail    = cfg.get("trail", 0) or 0
    tp       = cfg.get("tp", 0) or 0
    max_bars = cfg.get("max_bars", 0) or 10**9

    trades: list[dict] = []
    in_pos = False
    entry_px = entry_bar = 0.0
    peak = 0.0
    cool_until = -1

    def _close(j: int, px: float, reason: str):
        nonlocal in_pos
        pnl = (px - entry_px) / entry_px * 100.0
        trades.append({
            "buy_bar": int(entry_bar), "sell_bar": int(j),
            "buy_price": round(entry_px, 4), "sell_price": round(px, 4),
            "pnl_pct": round(pnl, 4), "hold_bars": int(j - entry_bar),
            "buy_time": str(tcol[int(entry_bar)]) if has_time else str(entry_bar),
            "sell_time": str(tcol[j]) if has_time else str(j),
            "reason": reason, "win": pnl > 0,
        })
        in_pos = False

    for i in range(n - 1):
        if in_pos:
            held = i - entry_bar
            # trailing level uses peak from bars STRICTLY before i (no lookahead)
            if stop and low[i] <= entry_px * (1 - stop / 100.0):
                _close(i, entry_px * (1 - stop / 100.0) * (1 - slip), "stop")
            elif trail and peak > 0 and low[i] <= peak * (1 - trail / 100.0):
                _close(i, peak * (1 - trail / 100.0) * (1 - slip), "trail")
            elif tp and high[i] >= entry_px * (1 + tp / 100.0):
                _close(i, entry_px * (1 + tp / 100.0) * (1 - slip), "take_profit")
            elif held >= max_bars:
                _close(i, close[i] * (1 - slip), "time")
            else:
                peak = max(peak, high[i])   # update AFTER checks
                continue
            cool_until = i + cooldown
        elif entries[i] and i > cool_until:
            entry_px = open_[i + 1] * (1 + slip)
            entry_bar = i + 1
            peak = entry_px
            in_pos = True

    if in_pos:
        _close(n - 1, close[n - 1] * (1 - slip), "end_of_data")

    return [t for t in trades if t["reason"] != "end_of_data"]


# ── Exit disciplines to compare ────────────────────────────────────────────────
# Deliberately a short, hand-picked set (not a giant grid) — a small honest
# comparison resists curve-fitting better than "best of 200 combos".

PRESETS: list[tuple[str, dict]] = [
    ("time 15m only",        dict(max_bars=15)),
    ("time 30m only",        dict(max_bars=30)),
    ("stop8 + time30",       dict(stop=8, max_bars=30)),
    ("stop5 + time15",       dict(stop=5, max_bars=15)),
    ("trail15",              dict(trail=15)),
    ("trail15 + stop8",      dict(trail=15, stop=8)),
    ("trail10 + stop5",      dict(trail=10, stop=5)),
    ("trail10+stop5+time30", dict(trail=10, stop=5, max_bars=30)),
    ("stop5 tp20 time30",    dict(stop=5, tp=20, max_bars=30)),
    ("trail8+stop5+time15",  dict(trail=8, stop=5, max_bars=15)),
]


def _row(label: str, s: dict) -> str:
    if not s.get("trades"):
        return f"  {label:<22} {'—  no trades':>40}"
    return (f"  {label:<22} {s['trades']:>5} {s['win_rate']:>6}% "
            f"{s['avg_pnl_pct']:>+8.3f} {s['total_pnl_pct']:>+9.2f} "
            f"{s['avg_hold_bars']:>7} {s['max_consec_losses']:>6}")


def main():
    ap = argparse.ArgumentParser(description="Compare EXIT disciplines on a spike-entry proxy.")
    ap.add_argument("tickers", nargs="+", help="Symbols, e.g. NAMM HUBC PLCE")
    ap.add_argument("--months", type=int, default=6, help="History to fetch (default 6)")
    ap.add_argument("--rvol", type=float, default=3.0, help="Relative-volume spike multiple (default 3)")
    ap.add_argument("--vol-window", type=int, default=30, help="Bars for the avg-volume baseline (default 30)")
    ap.add_argument("--slippage", type=float, default=15.0, help="Per-side slippage bps (default 15)")
    ap.add_argument("--cooldown", type=int, default=30, help="Bars to wait after an exit before re-entry (default 30)")
    ap.add_argument("--regular-hours-only", action="store_true",
                    help="Drop pre/post-market bars (RTH 09:30–16:00 ET)")
    ap.add_argument("--per-ticker", action="store_true", help="Also print each ticker's spike count")
    args = ap.parse_args()

    api_key, secret_key = _load_alpaca_credentials()
    if not api_key or not secret_key:
        print("❌ Alpaca credentials not found — set ALPACA_API_KEY / ALPACA_SECRET_KEY in signal_engine.env")
        sys.exit(1)

    # entries are fixed; only the exit cfg varies → pool trades per preset
    pooled: dict[str, list[dict]] = {name: [] for name, _ in PRESETS}
    total_spikes = 0

    for ticker in (t.upper() for t in args.tickers):
        df = fetch_history(ticker, api_key, secret_key, months=args.months)
        if df is not None and args.regular_hours_only:
            df = filter_regular_hours(df)
        if df is None or len(df) < max(200, args.vol_window + 5):
            print(f"  ⚠️  not enough data for {ticker} — skipping")
            continue
        entries = mark_spikes(df, args.rvol, args.vol_window)
        n_spk = int(entries.sum())
        total_spikes += n_spk
        if args.per_ticker:
            print(f"  {ticker:<6} {len(df):>7,} bars  {n_spk:>4} spikes")
        for name, cfg in PRESETS:
            pooled[name].extend(simulate_exits(df, entries, cfg, args.slippage, args.cooldown))

    print(f"\n{'═' * 78}")
    print(f"  EXIT-DISCIPLINE COMPARISON  —  {len(args.tickers)} tickers, "
          f"rvol≥{args.rvol}, {total_spikes} spike entries, {args.months}mo")
    print(f"{'═' * 78}")
    print(f"  {'exit rule':<22} {'trades':>5} {'win':>7} {'avg%':>8} "
          f"{'total%':>9} {'hold':>7} {'maxDD':>6}")
    print(f"  {'-' * 74}")

    rows = [(name, calc_stats(pooled[name])) for name, _ in PRESETS]
    rows.sort(key=lambda r: r[1].get("avg_pnl_pct", -1e9), reverse=True)
    for name, s in rows:
        print(_row(name, s))

    print("\n  Ranked by avg% (expectancy per trade). 'hold' = avg bars (minutes).")
    print("  'maxDD' = max consecutive losses. Entry is identical across rows —")
    print("  only the exit differs, so differences are pure exit-discipline effect.")
    print("\n  ⚠️  This is the EXIT proxy. The live entry (a chat-mention burst) is")
    print("      not in price history; confirm it in forward paper trading.")


if __name__ == "__main__":
    main()
