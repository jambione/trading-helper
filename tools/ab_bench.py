#!/usr/bin/env python3
"""
ab_bench.py — A/B benchmark series for the 3-indicator trading engine.

The excellence loop for the strategy we ACTUALLY trade (backtest_v2.py loops
the older momentum strategy). Fetches each ticker's history ONCE, then runs
every variant against the same cleaned bars, so a 10-variant series costs one
fetch instead of ten.

Honesty rules (inherited from backtest_3ind.py):
  • signal on close[i] fills at open[i+1] + adverse slippage
  • split-corrupt microcap bars are adjusted; RTH-only filter
  • never-sold end-of-data positions are EXCLUDED
  • pooled across the whole alert-style ticker list — single names fire too
    rarely to mean anything

Robustness check (anti-curve-fit):
  • The pool is split into two halves (alternating tickers). A variant's edge
    must hold on BOTH halves — a config that only wins on one half of the
    names is fitting noise, not signal.

Output: console tables + benchmarks/ab_bench_<date>.csv + updates BENCHMARKS.md.

USAGE
    venv/bin/python tools/ab_bench.py BYAH VSME EDHL ... --months 2
    venv/bin/python tools/ab_bench.py --default-pool --months 2
    venv/bin/python tools/ab_bench.py --default-pool --feed sip
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

_HERE = Path(__file__).parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_HERE))

# Load signal_engine.env so credentials match the live engine (backtest.py's
# fallback is config/secrets.json, which can drift stale).
import os                                                     # noqa: E402
for _line in (_ROOT / "signal_engine.env").read_text(encoding="utf-8").splitlines():
    _line = _line.strip()
    if _line and not _line.startswith("#") and "=" in _line:
        _k, _, _v = _line.partition("=")
        os.environ.setdefault(_k.strip(), _v.split("#")[0].strip())

import strategy_three_indicator as strat                      # noqa: E402
from backtest import _load_alpaca_credentials, fetch_history  # noqa: E402
from backtest_3ind import (                                   # noqa: E402
    simulate, completed_trades, sanitize_splits, filter_regular_hours,
)

# Tickers the Discord feed actually surfaced recently (watchlist + volume feed
# + signal log). The benchmark should run on what the engine will really see.
DEFAULT_POOL = ["BYAH", "VSME", "EDHL", "PIII", "SLE", "WOK",
                "ORGN", "AUUD", "QUCY", "TOPS", "TWAV", "IONZ"]

SLIPPAGE_BPS = 25.0   # default; override with --slippage (5 ≈ liquid large-caps)


# ── Variant definitions ───────────────────────────────────────────────────────
# name, strategy-param overrides, stop %, target %
VARIANTS: list[dict] = [
    # A — production config as currently set in signal_engine.env
    dict(name="A:prod SL1/TP2 any",      params={},                    stop=1.0, target=2.0),
    # Exit-style A/Bs
    dict(name="B1:exit=all",             params={"exit_mode": "all"},  stop=1.0, target=2.0),
    dict(name="B2:no TP (reversal+SL)",  params={},                    stop=1.0, target=0.0),
    dict(name="B3:SL1/TP3",              params={},                    stop=1.0, target=3.0),
    dict(name="B4:SL0.5/TP1.5 tight",    params={},                    stop=0.5, target=1.5),
    dict(name="B5:SL1.5/TP2",            params={},                    stop=1.5, target=2.0),
    dict(name="B6:SL2/TP2 symmetric",    params={},                    stop=2.0, target=2.0),
    # Entry-strictness A/Bs
    dict(name="B7:sep1.5 strict entry",  params={"macd_sep_mult": 1.5}, stop=1.0, target=2.0),
    dict(name="B8:sep0.5 loose entry",   params={"macd_sep_mult": 0.5}, stop=1.0, target=2.0),
    dict(name="B9:cw4 fresh cross",      params={"confirm_window": 4},  stop=1.0, target=2.0),
    dict(name="B10:cm35 deeper dip",     params={"cm_rsi_buy_max": 35}, stop=1.0, target=2.0),
    # exit=all family — round 1 showed "any" exits are hair-triggered (the
    # backward window makes one of the 3 reversal conditions ~always true),
    # so sweep around the "all" exit which 7x'd the win rate.
    dict(name="C1:all SL1/TP1.5",        params={"exit_mode": "all"},  stop=1.0, target=1.5),
    dict(name="C2:all SL0.75/TP1.5",     params={"exit_mode": "all"},  stop=0.75, target=1.5),
    dict(name="C3:all SL1/TP3",          params={"exit_mode": "all"},  stop=1.0, target=3.0),
    dict(name="C4:all+sep1.5",           params={"exit_mode": "all", "macd_sep_mult": 1.5},
         stop=1.0, target=2.0),
    dict(name="C5:all+cw4",              params={"exit_mode": "all", "confirm_window": 4},
         stop=1.0, target=2.0),
    dict(name="C6:all no TP",            params={"exit_mode": "all"},  stop=1.0, target=0.0),

    # D — the %R timeframe question. A:prod now runs the two-timeframe gate
    # (long scale for the setup, short scale for the trigger), so D1 is the
    # LEGACY arm: the single fast-line test both %R lines used to share. Read
    # D1 against A to price the change; the rest vary the long scale itself.
    dict(name="D1:legacy fast-only %R",
         params={"rte_require_slow": False},                  stop=1.0, target=2.0),
    dict(name="D2:slow 5min",
         params={"rte_slow_timeframe": "5min"},               stop=1.0, target=2.0),
    dict(name="D3:slow 30min",
         params={"rte_slow_timeframe": "30min"},              stop=1.0, target=2.0),
    dict(name="D4:slow window 120",
         params={"rte_slow_window": 120},                     stop=1.0, target=2.0),
    dict(name="D5:slow band -70",
         params={"rte_slow_threshold": 30},                   stop=1.0, target=2.0),
]


# ── Metrics (superset of calc_stats, scored for small-consistent-wins) ───────

def metrics(trades: list[dict]) -> dict:
    if not trades:
        return {"trades": 0, "win_rate": 0.0, "avg_pnl": 0.0, "total_pnl": 0.0,
                "pf": 0.0, "sharpe": 0.0, "max_dd": 0.0, "max_cl": 0,
                "avg_win": 0.0, "avg_loss": 0.0, "expectancy_dollar": 0.0,
                "score": 0.0}
    pnls   = [t["pnl_pct"] for t in trades]
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    n      = len(pnls)
    gw, gl = sum(wins), abs(sum(losses))
    pf     = min(gw / gl, 50.0) if gl > 0 else 50.0
    arr    = np.array(pnls)
    sharpe = float(arr.mean() / arr.std()) if arr.std() > 1e-9 else 0.0

    equity = peak = 10_000.0
    max_dd = 0.0
    for p in pnls:
        equity *= 1 + p / 100
        peak    = max(peak, equity)
        max_dd  = max(max_dd, (peak - equity) / peak * 100)

    max_cl = cl = 0
    for p in pnls:
        cl = cl + 1 if p <= 0 else 0
        max_cl = max(max_cl, cl)

    win_rate = len(wins) / n * 100
    # Composite score: same shape as backtest_v2 — consistency first
    if n < 30:
        score = 0.0
    else:
        score = (min(win_rate, 80)
                 + min(pf * 15, 30)
                 + min(max(sharpe * 10, 0), 20)
                 - min(max_cl * 3, 20)
                 - min(max_dd, 20))
    return {
        "trades": n,
        "win_rate": round(win_rate, 1),
        "avg_pnl": round(arr.mean(), 3),
        "total_pnl": round(sum(pnls), 2),
        "pf": round(pf, 2),
        "sharpe": round(sharpe, 3),
        "max_dd": round(max_dd, 1),
        "max_cl": max_cl,
        "avg_win": round(np.mean(wins), 3) if wins else 0.0,
        "avg_loss": round(np.mean(losses), 3) if losses else 0.0,
        "expectancy_dollar": round(arr.mean() / 100 * 500, 2),   # $ at $500/trade
        "score": round(max(score, 0.0), 1),
    }


def run_variant(data: dict, v: dict, tickers: list[str], slippage: float = SLIPPAGE_BPS) -> list[dict]:
    """Simulate one variant over the given tickers; returns pooled real trades."""
    p = strat.params(**v["params"])
    pooled: list[dict] = []
    for sym in tickers:
        df = data.get(sym)
        if df is None:
            continue
        trades = simulate(df, p, slippage, v["stop"], v["target"])
        trades, _ = completed_trades(trades)
        for t in trades:
            t["ticker"] = sym
        pooled.extend(trades)
    return pooled


def reason_breakdown(trades: list[dict]) -> str:
    by: dict = {}
    for t in trades:
        by.setdefault(t["reason"], [0, 0])
        by[t["reason"]][0] += 1 if t["win"] else 0
        by[t["reason"]][1] += 1
    return "  ".join(f"{r}:{w}/{n}" for r, (w, n) in sorted(by.items()))


def main():
    ap = argparse.ArgumentParser(description="A/B benchmark series — 3-indicator engine")
    ap.add_argument("tickers", nargs="*", help="Ticker pool (or --default-pool)")
    ap.add_argument("--default-pool", action="store_true")
    ap.add_argument("--months", type=int, default=2)
    ap.add_argument("--slippage", type=float, default=SLIPPAGE_BPS,
                    help="per-side bps (25 microcaps, ~5 liquid)")
    ap.add_argument("--feed", choices=("iex", "sip"), default="iex",
                    help="Historical tape. sip = free delayed SIP (15m lag).")
    args = ap.parse_args()

    tickers = [t.upper() for t in args.tickers] or (DEFAULT_POOL if args.default_pool else [])
    if not tickers:
        ap.error("give tickers or --default-pool")

    api, sec = _load_alpaca_credentials()
    if not (api and sec):
        sys.exit("❌ Alpaca credentials missing")

    # ── Fetch once per ticker ─────────────────────────────────────────────────
    data: dict = {}
    for sym in tickers:
        df = fetch_history(sym, api, sec, months=args.months, feed=args.feed)
        if df is None or len(df) < 200:
            print(f"  ⚠️  {sym}: not enough data — dropped from pool")
            continue
        df = filter_regular_hours(df)
        df, n_spl = sanitize_splits(df)
        if n_spl:
            print(f"  {sym}: split-adjusted {n_spl} corrupt bar(s)")
        if len(df) < 200:
            print(f"  ⚠️  {sym}: too few RTH bars — dropped")
            continue
        data[sym] = df
    pool = list(data.keys())
    print(f"\n  Pool: {', '.join(pool)}  ({len(pool)} tickers, "
          f"{sum(len(d) for d in data.values()):,} bars)\n")

    half_a = pool[0::2]
    half_b = pool[1::2]

    # ── Run all variants ──────────────────────────────────────────────────────
    rows: list[dict] = []
    for v in VARIANTS:
        full   = run_variant(data, v, pool, args.slippage)
        m      = metrics(full)
        m_a    = metrics(run_variant(data, v, half_a, args.slippage))
        m_b    = metrics(run_variant(data, v, half_b, args.slippage))
        # Robust = profitable (avg>0) on BOTH ticker halves
        robust = (m_a["trades"] >= 5 and m_b["trades"] >= 5
                  and m_a["avg_pnl"] > 0 and m_b["avg_pnl"] > 0)
        rows.append({"variant": v["name"], **m,
                     "halfA_avg": m_a["avg_pnl"], "halfB_avg": m_b["avg_pnl"],
                     "robust": robust, "exits": reason_breakdown(full)})

    # ── Report ────────────────────────────────────────────────────────────────
    W = 132
    print("=" * W)
    print(f"  A/B BENCHMARK — 3-indicator strategy, pooled over {len(pool)} alert-pool tickers, "
          f"{args.months}mo RTH, {args.feed.upper()}, slip {args.slippage:.0f}bps")
    print("=" * W)
    hdr = (f"  {'variant':<26}{'n':>5} {'win%':>6} {'avg%':>7} {'tot%':>8} "
           f"{'PF':>6} {'Sharpe':>7} {'DD%':>6} {'CL':>3} {'$/trade':>8} "
           f"{'A½':>7} {'B½':>7}  robust")
    print(hdr)
    print("-" * W)
    for r in rows:
        print(f"  {r['variant']:<26}{r['trades']:>5} {r['win_rate']:>6.1f} "
              f"{r['avg_pnl']:>7.3f} {r['total_pnl']:>8.2f} {r['pf']:>6.2f} "
              f"{r['sharpe']:>7.3f} {r['max_dd']:>6.1f} {r['max_cl']:>3} "
              f"{r['expectancy_dollar']:>8.2f} {r['halfA_avg']:>7.3f} "
              f"{r['halfB_avg']:>7.3f}  {'✓' if r['robust'] else '✗'}")
    print("=" * W)
    for r in rows:
        print(f"  {r['variant']:<26} exits: {r['exits']}")

    # ── Persist benchmarks ────────────────────────────────────────────────────
    out_dir = _ROOT / "benchmarks"
    out_dir.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = out_dir / f"ab_bench_{stamp}_{'-'.join(pool[:3])}_slip{int(args.slippage)}.csv"
    cols = ["variant", "trades", "win_rate", "avg_pnl", "total_pnl", "pf",
            "sharpe", "max_dd", "max_cl", "avg_win", "avg_loss",
            "expectancy_dollar", "score", "halfA_avg", "halfB_avg",
            "robust", "exits"]
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"\n  📄  {out}")
    print(f"  Pool: {' '.join(pool)}   months={args.months}  slippage={args.slippage}bps")


if __name__ == "__main__":
    main()
