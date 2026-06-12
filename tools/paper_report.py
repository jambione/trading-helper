#!/usr/bin/env python3
"""
paper_report.py — Forward-test performance report from signal_log.json

This is the measurement half of paper forward-testing: run the engine in
TRADER_MODE=paper, let the real mention-driven flow place trades, then run
this to evaluate what actually happened. No engine changes, no network — it
just reads the signal_log.json the engine already writes.

Each SELL entry in the log is a complete round-trip (it carries buy_price,
pnl_pct, reason, buy_time), so closed trades come straight from the SELLs.
Each is matched back to its originating BUY (via ticker + buy_time) to recover
entry context — most importantly whether it was a "hot" priority entry where
the mention-velocity bypass skipped the RSI filter.

USAGE
    python paper_report.py
    python paper_report.py --days 14            # last 14 days only
    python paper_report.py --ticker NVDA        # one symbol
    python paper_report.py --size 2000          # $ P&L at $2k/trade
    python paper_report.py --json               # machine-readable output

CAVEAT
    P&L here is computed from the engine's signal-time prices, not the actual
    Alpaca paper fills. It is therefore optimistic — real fills (especially on
    low-float small caps) differ. Reconciling against Alpaca fill data is the
    natural next step; this report bounds the best case.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")

LOG_FILE = Path(__file__).resolve().parent.parent / "signal_log.json"  # repo root


# ── Loading & pairing ─────────────────────────────────────────────────────────

def _parse_ts(s: str) -> datetime | None:
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def load_trades(path: Path, days: int | None, ticker: str | None) -> tuple[list[dict], list[dict]]:
    """
    Return (closed_trades, open_positions).

    closed_trades: one dict per SELL, enriched with entry context from the
                   matching BUY (hot/priority, rvol, mention_velocity).
    open_positions: BUYs with no matching SELL.
    """
    if not path.exists():
        return [], []
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[paper_report] could not read {path.name}: {e}")
        return [], []

    cutoff = None
    if days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    tkr = ticker.upper() if ticker else None

    # Index BUYs by (ticker, time) so a SELL.buy_time can recover its entry.
    buys_by_key: dict[tuple[str, str], dict] = {}
    buy_consumed: set[tuple[str, str]] = set()
    for e in entries:
        if e.get("action") == "BUY":
            buys_by_key[(e.get("ticker"), e.get("time"))] = e

    closed: list[dict] = []
    for e in entries:
        if e.get("action") != "SELL":
            continue
        if tkr and e.get("ticker") != tkr:
            continue
        sell_ts = _parse_ts(e.get("time", ""))
        if cutoff and sell_ts and sell_ts < cutoff:
            continue

        key = (e.get("ticker"), e.get("buy_time"))
        buy = buys_by_key.get(key)
        if buy is not None:
            buy_consumed.add(key)

        hold = e.get("hold_minutes")
        if hold is None:
            bts, sts = _parse_ts(e.get("buy_time", "")), sell_ts
            if bts and sts:
                hold = round((sts - bts).total_seconds() / 60, 2)

        closed.append({
            "ticker":     e.get("ticker"),
            "pnl_pct":    float(e.get("pnl_pct", 0.0)),
            "reason":     e.get("reason", "unknown"),
            "buy_price":  e.get("buy_price"),
            "sell_price": e.get("price"),
            "buy_time":   e.get("buy_time"),
            "sell_time":  e.get("time"),
            "hold_min":   hold,
            "hot":        bool(buy.get("priority")) if buy else False,
            "mention_velocity": (buy or {}).get("mention_velocity"),
            "rvol":       (buy or {}).get("rvol"),
            "entry_known": buy is not None,
        })

    # Open positions: BUYs never consumed by a SELL.
    open_pos: list[dict] = []
    for (t, ts), buy in buys_by_key.items():
        if (t, ts) in buy_consumed:
            continue
        if tkr and t != tkr:
            continue
        bts = _parse_ts(ts)
        if cutoff and bts and bts < cutoff:
            continue
        open_pos.append(buy)

    closed.sort(key=lambda c: c.get("sell_time") or "")
    return closed, open_pos


# ── Stats ─────────────────────────────────────────────────────────────────────

def stats(trades: list[dict], size: float) -> dict:
    if not trades:
        return {"trades": 0}
    pnls = [t["pnl_pct"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    holds = [t["hold_min"] for t in trades if t["hold_min"] is not None]

    max_consec = consec = 0
    for p in pnls:
        consec = consec + 1 if p <= 0 else 0
        max_consec = max(max_consec, consec)

    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else float("inf")

    return {
        "trades":        len(trades),
        "wins":          len(wins),
        "win_rate":      round(len(wins) / len(trades) * 100, 1),
        "avg_pnl_pct":   round(statistics.mean(pnls), 3),
        "median_pnl_pct": round(statistics.median(pnls), 3),
        "total_pnl_pct": round(sum(pnls), 3),
        "total_dollar":  round(sum(p / 100 * size for p in pnls), 2),
        "expectancy_pct": round(statistics.mean(pnls), 3),
        "profit_factor": (round(profit_factor, 2) if profit_factor != float("inf") else None),
        "best":          round(max(pnls), 3),
        "worst":         round(min(pnls), 3),
        "avg_hold_min":  round(statistics.mean(holds), 1) if holds else None,
        "max_consec_losses": max_consec,
    }


def _group_stats(trades: list[dict], key_fn, size: float) -> dict[str, dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        groups[key_fn(t)].append(t)
    return {k: stats(v, size) for k, v in groups.items()}


def _et_day(t: dict) -> str:
    """ET calendar date of the trade's sell time (trading-day grouping)."""
    ts = _parse_ts(t.get("sell_time") or "")
    return ts.astimezone(_ET).strftime("%Y-%m-%d") if ts else "unknown"


# ── Rendering ─────────────────────────────────────────────────────────────────

def _fmt_pct(v) -> str:
    if v is None:
        return "  —  "
    return f"{'+' if v >= 0 else ''}{v:.2f}%"


def _line(label: str, s: dict, size: float):
    if not s.get("trades"):
        return f"  {label:<22} (no trades)"
    pf = "∞" if s["profit_factor"] is None else f"{s['profit_factor']:.2f}"
    return (f"  {label:<22} {s['trades']:>4} trades  "
            f"win {s['win_rate']:>5.1f}%  "
            f"avg {_fmt_pct(s['avg_pnl_pct']):>8}  "
            f"total {_fmt_pct(s['total_pnl_pct']):>9}  "
            f"${s['total_dollar']:>10,.0f}  PF {pf}")


def render(closed: list[dict], open_pos: list[dict], size: float,
           show_daily: bool = False):
    print("=" * 78)
    print("  PAPER FORWARD-TEST REPORT")
    print("=" * 78)

    if not closed:
        print("  No closed trades in range. (Run the engine in paper mode to collect data.)")
        if open_pos:
            print(f"  {len(open_pos)} open position(s).")
        print("=" * 78)
        return

    span_lo = closed[0].get("sell_time", "?")
    span_hi = closed[-1].get("sell_time", "?")
    print(f"  Window: {span_lo}  →  {span_hi}   |   $ figures assume ${size:,.0f}/trade")
    print("-" * 78)

    overall = stats(closed, size)
    pf = "∞" if overall["profit_factor"] is None else f"{overall['profit_factor']:.2f}"
    print(f"  Closed trades   : {overall['trades']}")
    print(f"  Win rate        : {overall['win_rate']}%  ({overall['wins']}/{overall['trades']})")
    print(f"  Avg / trade     : {_fmt_pct(overall['avg_pnl_pct'])}   "
          f"(median {_fmt_pct(overall['median_pnl_pct'])})")
    print(f"  Total return    : {_fmt_pct(overall['total_pnl_pct'])}   "
          f"(${overall['total_dollar']:,.2f})")
    print(f"  Profit factor   : {pf}   "
          f"(best {_fmt_pct(overall['best'])}, worst {_fmt_pct(overall['worst'])})")
    print(f"  Avg hold        : {overall['avg_hold_min']} min" if overall['avg_hold_min'] is not None
          else "  Avg hold        : —")
    print(f"  Max consec loss : {overall['max_consec_losses']}")

    # ── Hot vs cold — the core question for a mention-driven system ───────────
    print("-" * 78)
    print("  HOT (mention bypass) vs COLD entries")
    by_hot = _group_stats(closed, lambda t: "hot" if t["hot"] else "cold", size)
    print(_line("🔥 hot (RSI bypassed)", by_hot.get("hot", {"trades": 0}), size))
    print(_line("❄  cold (normal)", by_hot.get("cold", {"trades": 0}), size))
    unknown = sum(1 for t in closed if not t["entry_known"])
    if unknown:
        print(f"  (note: {unknown} trade(s) had no matching BUY in the log — "
              f"counted as cold; entry context unknown)")

    # ── By exit reason ────────────────────────────────────────────────────────
    print("-" * 78)
    print("  BY EXIT REASON")
    for reason, s in sorted(_group_stats(closed, lambda t: t["reason"], size).items(),
                            key=lambda kv: kv[1]["total_pnl_pct"], reverse=True):
        print(_line(reason, s, size))

    # ── By ticker ─────────────────────────────────────────────────────────────
    print("-" * 78)
    print("  BY TICKER (ranked by total return)")
    by_tkr = _group_stats(closed, lambda t: t["ticker"], size)
    for tkr, s in sorted(by_tkr.items(), key=lambda kv: kv[1]["total_pnl_pct"], reverse=True):
        print(_line(tkr, s, size))

    # ── By day — the compounding decision view ───────────────────────────────
    # "Is it consistent enough to raise TRADE_AMOUNT?" lives here: green days
    # vs red days, day-by-day. Enable with --daily.
    if show_daily:
        print("-" * 78)
        print("  BY DAY (ET trading date)")
        by_day = _group_stats(closed, _et_day, size)
        green = red = 0
        for day in sorted(by_day):
            s = by_day[day]
            if s["total_pnl_pct"] > 0:
                green += 1
            elif s["trades"]:
                red += 1
            print(_line(day, s, size))
        total_days = green + red
        if total_days:
            print(f"  {'─' * 74}")
            print(f"  Green days: {green}/{total_days} ({green / total_days * 100:.0f}%)   "
                  f"avg $/day: ${overall['total_dollar'] / total_days:,.2f}")

    # ── Open positions ────────────────────────────────────────────────────────
    if open_pos:
        print("-" * 78)
        print(f"  OPEN POSITIONS ({len(open_pos)})")
        for b in open_pos:
            tag = "  🔥" if b.get("priority") else ""
            print(f"    {b.get('ticker'):<6} entry ${b.get('price')}  "
                  f"{b.get('time')}{tag}")

    print("=" * 78)


def build_json(closed: list[dict], open_pos: list[dict], size: float) -> dict:
    return {
        "overall": stats(closed, size),
        "by_entry": _group_stats(closed, lambda t: "hot" if t["hot"] else "cold", size),
        "by_reason": _group_stats(closed, lambda t: t["reason"], size),
        "by_ticker": _group_stats(closed, lambda t: t["ticker"], size),
        "by_day": _group_stats(closed, _et_day, size),
        "open_positions": [
            {"ticker": b.get("ticker"), "entry": b.get("price"),
             "time": b.get("time"), "hot": bool(b.get("priority"))}
            for b in open_pos
        ],
    }


def main():
    ap = argparse.ArgumentParser(description="Performance report from signal_log.json")
    ap.add_argument("--days", type=int, default=None, help="Only trades from the last N days")
    ap.add_argument("--ticker", type=str, default=None, help="Filter to one symbol")
    ap.add_argument("--size", type=float, default=10000, help="$ per trade for dollar P&L (default 10000)")
    ap.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    ap.add_argument("--daily", action="store_true",
                    help="Per-day P&L breakdown (the raise-TRADE_AMOUNT view)")
    ap.add_argument("--file", type=str, default=str(LOG_FILE), help="Path to signal_log.json")
    args = ap.parse_args()

    closed, open_pos = load_trades(Path(args.file), args.days, args.ticker)

    if args.json:
        print(json.dumps(build_json(closed, open_pos, args.size), indent=2))
    else:
        render(closed, open_pos, args.size, show_daily=args.daily)


if __name__ == "__main__":
    main()
