#!/usr/bin/env python3
"""Gate 1 for the H4 swing — liquid universe, daily hold, vs SPY.

Not the squeeze watchlist and not a 1-minute admission screen. PASS is
permission to sweep exits for this horizon, not ``ai_h4_paper=true``.
See docs/PROFIT_REDESIGN.md.

Scoring reuses desk_null.verdict: n≥30, net median > 0 after 20 bps,
paired vs SPY ≥2σ, ≥5 sessions, sign-test p≤0.05, no session > 50% of n.

Read-only. Usage (mini, venv):

    .venv/bin/python tools/h4_screen.py
    .venv/bin/python tools/h4_screen.py --days 20 --hold-days 2
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import desk_h4  # noqa: E402
import desk_null as N  # noqa: E402

BENCH = "SPY"
SCREEN_DIR = Path(ROOT) / "ai_reports" / "screens"

# When rs_ratings.json is missing (screener is off on the mini) still score a
# liquid book so after-hours lab is not EMPTY. This is not an RS PASS.
LIQUID_FALLBACK = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "AVGO", "LLY",
    "JPM", "XOM", "UNH", "V", "WMT", "PG", "COST", "HD", "ABBV", "KO",
    "MRK", "JNJ",
]


def simulate_hold(
    ohlc: list[dict],
    *,
    hold_days: int,
    stop_pct: float,
    haircut_pct: float,
) -> list[dict]:
    """Non-overlapping swings on one name's daily bars.

    *ohlc* is oldest-first dicts with date, open, high, low, close.
    Entry at day[i] open; exit at stop (intraday, using low) or close of
    day[i + hold_days - 1]. The next entry starts the day after exit.
    """
    if hold_days < 1:
        hold_days = 1
    stop_frac = max(0.0, float(stop_pct) / 100.0)
    hair = float(haircut_pct)
    out: list[dict] = []
    i = 0
    n = len(ohlc)
    while i < n:
        if i + hold_days > n:
            break
        entry = float(ohlc[i].get("open") or 0)
        if entry <= 0:
            i += 1
            continue
        floor = entry * (1.0 - stop_frac)
        exit_i = i + hold_days - 1
        stopped = False
        exit_px = float(ohlc[exit_i].get("close") or 0)
        for j in range(i, exit_i + 1):
            low = float(ohlc[j].get("low") or 0)
            if low > 0 and low <= floor:
                exit_px = floor
                exit_i = j
                stopped = True
                break
            exit_px = float(ohlc[j].get("close") or exit_px)
        if exit_px <= 0:
            i += 1
            continue
        fwd = 100.0 * (exit_px - entry) / entry
        out.append({
            "entry_date": ohlc[i].get("date"),
            "exit_date": ohlc[exit_i].get("date"),
            "fwd": fwd,
            "net": fwd - hair,
            "stopped": stopped,
            "day": ohlc[i].get("date"),
        })
        i = exit_i + 1
    return out


def spy_return(spy: list[dict], entry_date: str, exit_date: str) -> float | None:
    by = {str(r.get("date")): r for r in spy}
    a, b = by.get(entry_date), by.get(exit_date)
    if not a or not b:
        return None
    o = float(a.get("open") or 0)
    c = float(b.get("close") or 0)
    if o <= 0 or c <= 0:
        return None
    return 100.0 * (c - o) / o


def attach_bench(swings: list[dict], spy: list[dict]) -> list[dict]:
    scored = []
    for s in swings:
        elig = spy_return(spy, s["entry_date"], s["exit_date"])
        row = dict(s)
        row["eligible"] = elig
        scored.append(row)
    return scored


def _to_null_scores(rows: list[dict]) -> list[dict]:
    """desk_null.verdict wants fwd/net/eligible and a session key."""
    out = []
    for r in rows:
        if r.get("fwd") is None or r.get("net") is None:
            continue
        out.append({
            "fwd": float(r["fwd"]),
            "net": float(r["net"]),
            "eligible": r.get("eligible"),
            "day": r.get("day") or r.get("entry_date"),
        })
    return out


def summarize(scores: list[dict]) -> dict:
    v = N.verdict(scores) if scores else "EMPTY"
    net = [s["net"] for s in scores]
    return {
        "verdict": v,
        "n": len(scores),
        "net_median": statistics.median(net) if net else None,
        "net_mean": statistics.fmean(net) if net else None,
        "diagnose": N.diagnose(scores) if scores else "nothing scored",
        "sessions": N.session_stats(scores, "eligible") if scores else {},
    }


def filter_rows(rows: list[dict], cfg: dict) -> list[dict]:
    return desk_h4.filter_universe(rows, cfg)


def _fetch_daily(symbols: list[str], start: str, end: str) -> dict[str, list[dict]]:
    """symbol -> oldest-first daily OHLC. Empty dict if no Alpaca client."""
    try:
        import alpaca_api as aa
        from config import load_config
        cfg = load_config()
        client = aa.connect_data_client(cfg)
        if client is None:
            return {}
        from datetime import date as _date
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        start_d = _date.fromisoformat(start)
        end_d = _date.fromisoformat(end)
        feed_kw = aa._get_feed_arg(cfg)
        out: dict[str, list[dict]] = {}
        chunk = 40
        for i in range(0, len(symbols), chunk):
            batch = symbols[i:i + chunk]
            req = StockBarsRequest(
                symbol_or_symbols=batch,
                timeframe=TimeFrame.Day,
                start=datetime(start_d.year, start_d.month, start_d.day,
                               tzinfo=timezone.utc),
                end=datetime(end_d.year, end_d.month, end_d.day,
                             tzinfo=timezone.utc),
                adjustment="split",
                **feed_kw,
            )
            barset = client.get_stock_bars(req)
            data = getattr(barset, "data", None) or {}
            _ingest_daily(data, out)
        return out
    except Exception:
        return {}


def _ingest_daily(data, out: dict) -> None:
    for sym, bars in (data or {}).items():
        rows = []
        for b in bars or []:
            ts = getattr(b, "timestamp", None)
            if ts is None:
                continue
            if getattr(ts, "tzinfo", None) is None:
                ts = ts.replace(tzinfo=timezone.utc)
            day = ts.astimezone(timezone.utc).strftime("%Y-%m-%d")
            rows.append({
                "date": day,
                "open": float(b.open),
                "high": float(b.high),
                "low": float(b.low),
                "close": float(b.close),
            })
        rows.sort(key=lambda r: r["date"])
        if rows:
            out[str(sym).upper()] = rows


def _cfg() -> dict:
    try:
        from config import load_config
        return load_config() or {}
    except Exception:
        return {}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=20,
                    help="calendar lookback for daily bars")
    ap.add_argument("--hold-days", type=int, default=0)
    ap.add_argument("--limit", type=int, default=40,
                    help="max names from the RS universe")
    ap.add_argument("--rs", default="",
                    help="path to rs_ratings.json (default repo file)")
    ap.add_argument("--no-fallback", action="store_true",
                    help="EMPTY if RS file missing, instead of liquid names")
    args = ap.parse_args()
    cfg = _cfg()
    hold = args.hold_days or int(cfg.get("h4_hold_days") or desk_h4.DEFAULT_HOLD_DAYS)
    stop_pct = float(cfg.get("h4_stop_pct") or desk_h4.DEFAULT_STOP_PCT)
    hair = float(cfg.get("h4_haircut_pct") or desk_h4.DEFAULT_HAIRCUT_PCT)

    rs_path = args.rs or str(Path(ROOT) / "rs_ratings.json")
    uni = filter_rows(desk_h4.load_rs_rows(rs_path), cfg)
    uni.sort(key=lambda r: float(r.get("rs_rating") or 0), reverse=True)
    uni = uni[: max(1, args.limit)]
    symbols = [str(r.get("ticker") or r.get("symbol") or "").upper()
               for r in uni]
    symbols = [s for s in symbols if s]
    universe_source = "rs_ratings"
    if not symbols:
        if args.no_fallback:
            print("  EMPTY — no names cleared price / dollar-vol / RS gates "
                  "(rs_ratings.json missing or stale).")
            return 0
        symbols = list(LIQUID_FALLBACK)
        universe_source = "liquid_fallback"
        print("  RS universe empty — scoring liquid fallback "
              f"({len(symbols)} names). Not an RS PASS.")
    if BENCH not in symbols:
        fetch_syms = symbols + [BENCH]
    else:
        fetch_syms = list(symbols)

    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=max(args.days, hold) + 10)
    print(f"H4 screen  hold={hold}d  stop={stop_pct:g}%  haircut={hair:g}%  "
          f"universe={len(symbols)} ({universe_source})  window={start}..{end}")

    bars = _fetch_daily(fetch_syms, start.isoformat(), end.isoformat())
    spy = bars.get(BENCH) or []
    if not bars:
        print("  no Alpaca data client — run on the mini with .venv "
              "(config/secrets.json api_key).")
        return 0
    if not spy:
        print(f"  no {BENCH} daily bars — cannot score vs the market dart.")
        return 0

    all_swings: list[dict] = []
    used = 0
    for sym in symbols:
        ohlc = bars.get(sym) or []
        if len(ohlc) < hold + 1:
            continue
        swings = simulate_hold(
            ohlc, hold_days=hold, stop_pct=stop_pct, haircut_pct=hair)
        for s in swings:
            s["symbol"] = sym
        all_swings.extend(attach_bench(swings, spy))
        used += 1

    scores = _to_null_scores(all_swings)
    summary = summarize(scores)
    print(f"  names with bars {used}/{len(symbols)}  swings={len(scores)}")
    print(f"  verdict {summary['verdict']}  n={summary['n']}  "
          f"net median {summary['net_median']}")
    print(f"  {summary['diagnose']}")
    print(N.format_sessions(summary["sessions"]) if summary["sessions"] else "")
    print("PASS is permission to sweep H4 exits, not a live config change.")

    SCREEN_DIR.mkdir(parents=True, exist_ok=True)
    day = datetime.now().strftime("%Y-%m-%d")
    payload = {
        "day": day,
        "hold_days": hold,
        "stop_pct": stop_pct,
        "haircut_pct": hair,
        "universe_source": universe_source,
        "n_universe": len(symbols),
        "n_with_bars": used,
        "summary": summary,
    }
    outp = SCREEN_DIR / f"h4_{day}.json"
    outp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"wrote {outp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
