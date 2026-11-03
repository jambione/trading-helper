#!/usr/bin/env python3
"""Score the two shadow loggers (operator 2026-11-03 "build loggers").

READ ONLY. Never places, cancels or changes an order. Run on the mini AFTER
the close (it uses the historical SIP data key, ALPACA_DATA_API_KEY when set,
so it does not spend the desk's trading budget).

1. exits  - would a sell limit at the SIP midpoint have filled within a collar?
   Source: ai_reports/fills/<day>.jsonl (every desk SELL submit + its fill,
   logged since 2026-09-16), so no live logger is needed and past sessions
   score too. For each exit, at the submit instant t0:
     * NBBO = last SIP quote at or before t0; limit = mid rounded to the cent.
     * STRICT fill: a SIP print strictly ABOVE the limit inside the collar
       (a print AT the limit may be someone else's queue). LENIENT (>=) is
       reported beside it, never as the headline.
     * Unfilled at the collar: sells at the SIP bid at t0 + collar (a market
       order then). The real market fill is what it is compared with.
     delta_bp = (simulated price - actual fill) / actual fill. Positive = the
     midpoint route would have been better.

2. pullback - forward moves after each pullback-shadow trigger
   (ai_reports/pullback_shadow/<day>.jsonl, ai_watch_pullback_shadow).
   Entry = OPEN of the first 1-minute SIP bar after the trigger (never the
   shadow quote: shadow prices inflate backtests). Forward close-to-entry at
   5/15/30 min, 15-min MFE/MAE, against 5 same-name same-day random minutes
   (seeded) as the control. Gross, plus net at a 3 bp round trip.

Usage (mini, repo root):
    .venv/bin/python tools/studies/shadow_loggers_score.py exits --day 2026-11-03
    .venv/bin/python tools/studies/shadow_loggers_score.py exits --since 2026-10-26
    .venv/bin/python tools/studies/shadow_loggers_score.py pullback --day 2026-11-04
Writes ai_reports/shadow_scores/<kind>_<first>_<last>.json.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import statistics
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
for p in (str(ROOT), str(ROOT / "tools")):
    if p not in sys.path:
        sys.path.insert(0, p)

ET = ZoneInfo("America/New_York")
REPORTS = ROOT / "ai_reports"
RT_COST_BP = 3.0
HORIZONS = (5, 15, 30)


# ---------------------------------------------------------------- pure parts
def cent(x: float) -> float:
    return math.floor(float(x) * 100.0 + 0.5) / 100.0


def nbbo_at(quotes: list[tuple[float, float, float]], t: float) -> tuple[float, float] | None:
    """Last sane (bid, ask) at or before t. quotes = [(ts, bid, ask)] sorted."""
    best = None
    for ts, b, a in quotes:
        if ts > t:
            break
        if b > 0 and a > 0 and a >= b:
            best = (b, a)
    return best


def simulate_mid_exit(t0: float, quotes: list, trades: list, actual_fill: float,
                      collar_sec: float = 10.0) -> dict | None:
    """One exit. quotes [(ts,bid,ask)], trades [(ts,price)], both sorted.
    None when there is no NBBO at t0 or at the collar."""
    q0 = nbbo_at(quotes, t0)
    if q0 is None or not (actual_fill > 0):
        return None
    bid0, ask0 = q0
    limit = cent((bid0 + ask0) / 2.0)
    window = [p for ts, p in trades if t0 < ts <= t0 + collar_sec]
    strict = any(p > limit + 1e-9 for p in window)
    lenient = any(p >= limit - 1e-9 for p in window)
    q1 = nbbo_at(quotes, t0 + collar_sec)
    if q1 is None:
        return None
    timeout_px = q1[0]  # market sell at the collar = the bid then
    sim = limit if strict else timeout_px
    lsim = limit if lenient else timeout_px
    return {
        "bid0": bid0, "ask0": ask0, "limit": limit,
        "spread_bp": round((ask0 - bid0) / ((ask0 + bid0) / 2.0) * 1e4, 2),
        "filled_strict": strict, "filled_lenient": lenient,
        "sim_px": sim,
        "delta_bp": round((sim - actual_fill) / actual_fill * 1e4, 2),
        "delta_lenient_bp": round((lsim - actual_fill) / actual_fill * 1e4, 2),
        "actual_vs_bid0_bp": round((actual_fill - bid0) / bid0 * 1e4, 2),
    }


def pair_sells(rows: list[dict]) -> list[dict]:
    """SELL submits joined to their fill by order_id (fills ledger rows)."""
    subs = {r.get("order_id"): r for r in rows
            if r.get("event") == "submit" and str(r.get("action", "")).upper() == "SELL" and r.get("order_id")}
    out = []
    for r in rows:
        if r.get("event") != "fill" or str(r.get("side", "")).lower() != "sell":
            continue
        s = subs.get(r.get("order_id"))
        if not s:
            continue
        try:
            px = float(r.get("filled_avg_price") or 0)
            qty = float(r.get("filled_qty") or 0)
        except (TypeError, ValueError):
            continue
        if px <= 0 or qty <= 0:
            continue
        out.append({"symbol": str(s.get("symbol") or r.get("symbol")).upper(), "t0": float(s["ts"]),
                    "qty": qty, "fill": px, "note": s.get("note"), "order_id": r.get("order_id")})
    out.sort(key=lambda x: x["t0"])
    return out


def forward_from_bars(bars: list[tuple], t_sig: float) -> dict | None:
    """bars [(ts_minute_start, o, h, l, c)] sorted. Entry = open of the first
    bar starting after t_sig."""
    idx = next((i for i, b in enumerate(bars) if b[0] > t_sig), None)
    if idx is None:
        return None
    e = float(bars[idx][1])
    if e <= 0:
        return None
    t_e = bars[idx][0]
    res = {"entry_ts": t_e, "entry": e}
    for h in HORIZONS:
        tgt = t_e + h * 60
        past = [b for b in bars[idx:] if b[0] + 60 <= tgt + 1e-6]
        if not past or past[-1][0] + 60 < tgt - 61:
            res[f"fwd{h}"] = None
            continue
        res[f"fwd{h}"] = (float(past[-1][4]) - e) / e * 1e4
    w = [b for b in bars[idx:] if b[0] < t_e + 15 * 60]
    res["mfe15"] = (max(float(b[2]) for b in w) - e) / e * 1e4 if w else None
    res["mae15"] = (min(float(b[3]) for b in w) - e) / e * 1e4 if w else None
    return res


def control_times(sym: str, day: str, bars: list[tuple], k: int = 5) -> list[float]:
    """k seeded random minutes 10:00-15:25 ET that have bars."""
    seed = int(hashlib.sha256(f"{sym}|{day}".encode()).hexdigest()[:12], 16)
    rng = random.Random(seed)
    pool = [b[0] for b in bars
            if "10:00" <= datetime.fromtimestamp(b[0], ET).strftime("%H:%M") <= "15:25"]
    if not pool:
        return []
    return [rng.choice(pool) - 1.0 for _ in range(k)]


def _mean(v):
    v = [x for x in v if x is not None]
    return (sum(v) / len(v)) if v else None


def _t(v):
    v = [x for x in v if x is not None]
    if len(v) < 3:
        return None
    sd = statistics.stdev(v)
    return (statistics.mean(v) / (sd / math.sqrt(len(v)))) if sd > 0 else None


# ---------------------------------------------------------------- data access
def _client():
    import fill_truth_report as ft
    ft._load_env()
    from alpaca.data.historical import StockHistoricalDataClient
    k = os.getenv("ALPACA_DATA_API_KEY") or os.getenv("ALPACA_API_KEY")
    s = os.getenv("ALPACA_DATA_SECRET_KEY") or os.getenv("ALPACA_SECRET_KEY")
    return StockHistoricalDataClient(k, s)


def _ts(x) -> float:
    return x.timestamp() if hasattr(x, "timestamp") else float(x)


def fetch_quotes_trades(cl, sym: str, t0: float, collar: float):
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockQuotesRequest, StockTradesRequest
    start = datetime.fromtimestamp(t0 - 30, tz=ET)
    end = datetime.fromtimestamp(t0 + collar + 2, tz=ET)
    q = cl.get_stock_quotes(StockQuotesRequest(symbol_or_symbols=sym, start=start, end=end, feed=DataFeed.SIP))
    tr = cl.get_stock_trades(StockTradesRequest(symbol_or_symbols=sym, start=start, end=end, feed=DataFeed.SIP))
    qd = (getattr(q, "data", {}) or {}).get(sym, []) or []
    td = (getattr(tr, "data", {}) or {}).get(sym, []) or []
    quotes = sorted((_ts(x.timestamp), float(x.bid_price or 0), float(x.ask_price or 0)) for x in qd)
    trades = sorted((_ts(x.timestamp), float(x.price)) for x in td if x.price)
    return quotes, trades


def fetch_day_bars(cl, sym: str, day: str):
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
    r = cl.get_stock_bars(StockBarsRequest(symbol_or_symbols=sym, timeframe=TimeFrame.Minute,
                                           start=d.replace(hour=9, minute=30), end=d.replace(hour=16),
                                           feed=DataFeed.SIP))
    rows = (getattr(r, "data", {}) or {}).get(sym, []) or []
    return sorted((_ts(b.timestamp), float(b.open), float(b.high), float(b.low), float(b.close)) for b in rows)


def _read_jsonl(p: Path) -> list[dict]:
    out = []
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _days(args) -> list[str]:
    if args.day:
        return [args.day]
    d0 = datetime.strptime(args.since, "%Y-%m-%d")
    d1 = datetime.strptime(args.until, "%Y-%m-%d") if args.until else datetime.now(ET).replace(tzinfo=None)
    out = []
    while d0 <= d1:
        if d0.weekday() < 5:
            out.append(d0.strftime("%Y-%m-%d"))
        d0 += timedelta(days=1)
    return out


# ---------------------------------------------------------------- commands
def cmd_exits(args) -> dict:
    cl = _client()
    rows = []
    for day in _days(args):
        sells = pair_sells(_read_jsonl(REPORTS / "fills" / f"{day}.jsonl"))
        for s in sells:
            hm = datetime.fromtimestamp(s["t0"], ET).strftime("%H:%M")
            if not ("09:30" <= hm < "16:00"):
                continue
            try:
                quotes, trades = fetch_quotes_trades(cl, s["symbol"], s["t0"], args.collar)
            except Exception as e:  # noqa: BLE001
                rows.append({**s, "day": day, "error": str(e)[:120]})
                continue
            time.sleep(args.sleep)
            sim = simulate_mid_exit(s["t0"], quotes, trades, s["fill"], args.collar)
            rows.append({**s, "day": day, **(sim or {"no_nbbo": True})})
    scored = [r for r in rows if "delta_bp" in r]
    summ = {
        "kind": "exits", "collar_sec": args.collar, "n_exits": len(rows), "n_scored": len(scored),
        "n_error": sum(1 for r in rows if "error" in r), "n_no_nbbo": sum(1 for r in rows if r.get("no_nbbo")),
        "fill_rate_strict": _mean([1.0 if r["filled_strict"] else 0.0 for r in scored]),
        "fill_rate_lenient": _mean([1.0 if r["filled_lenient"] else 0.0 for r in scored]),
        "delta_bp_mean": _mean([r["delta_bp"] for r in scored]),
        "delta_bp_median": statistics.median([r["delta_bp"] for r in scored]) if scored else None,
        "delta_bp_t": _t([r["delta_bp"] for r in scored]),
        "delta_lenient_bp_mean": _mean([r["delta_lenient_bp"] for r in scored]),
        "unfilled_delta_bp_mean": _mean([r["delta_bp"] for r in scored if not r["filled_strict"]]),
        "actual_vs_bid0_bp_mean": _mean([r["actual_vs_bid0_bp"] for r in scored]),
        "spread_bp_median": statistics.median([r["spread_bp"] for r in scored]) if scored else None,
        "by_day": {},
    }
    for day in sorted({r["day"] for r in scored}):
        v = [r for r in scored if r["day"] == day]
        summ["by_day"][day] = {"n": len(v), "fill_strict": _mean([1.0 if r["filled_strict"] else 0.0 for r in v]),
                               "delta_bp": _mean([r["delta_bp"] for r in v])}
    return {"summary": summ, "rows": rows}


def cmd_pullback(args) -> dict:
    cl = _client()
    rows = []
    for day in _days(args):
        sig = _read_jsonl(REPORTS / "pullback_shadow" / f"{day}.jsonl")
        by_sym: dict[str, list[dict]] = {}
        for r in sig:
            by_sym.setdefault(str(r.get("symbol")).upper(), []).append(r)
        for sym, rs in by_sym.items():
            try:
                bars = fetch_day_bars(cl, sym, day)
            except Exception as e:  # noqa: BLE001
                rows.extend({**r, "error": str(e)[:120]} for r in rs)
                continue
            time.sleep(args.sleep)
            ctl = [forward_from_bars(bars, t) for t in control_times(sym, day, bars)]
            ctl = [c for c in ctl if c]
            for r in rs:
                f = forward_from_bars(bars, float(r["ts"]))
                if not f:
                    rows.append({**r, "no_bars": True})
                    continue
                out = {**r, **f}
                for h in HORIZONS:
                    cm = _mean([c.get(f"fwd{h}") for c in ctl])
                    out[f"ctl{h}"] = cm
                    out[f"lift{h}"] = (f[f"fwd{h}"] - cm) if (f[f"fwd{h}"] is not None and cm is not None) else None
                rows.append(out)
    scored = [r for r in rows if "entry" in r]

    def block(v):
        b = {"n": len(v)}
        for h in HORIZONS:
            b[f"fwd{h}_bp"] = _mean([r.get(f"fwd{h}") for r in v])
            b[f"net{h}_bp"] = (b[f"fwd{h}_bp"] - RT_COST_BP) if b[f"fwd{h}_bp"] is not None else None
            b[f"lift{h}_bp"] = _mean([r.get(f"lift{h}") for r in v])
            b[f"lift{h}_t"] = _t([r.get(f"lift{h}") for r in v])
        b["mfe15_bp"] = _mean([r.get("mfe15") for r in v])
        b["mae15_bp"] = _mean([r.get("mae15") for r in v])
        b["struct_stop_bp_median"] = statistics.median([r["struct_stop_bp"] for r in v if r.get("struct_stop_bp") is not None]) if v else None
        b["struct_stop_hit15"] = _mean([1.0 if (r.get("mae15") is not None and r.get("struct_stop_bp") is not None
                                               and -r["mae15"] >= r["struct_stop_bp"]) else 0.0 for r in v])
        return b

    summ = {"kind": "pullback", "rt_cost_bp": RT_COST_BP, "n_signals": len(rows), "n_scored": len(scored),
            "all": block(scored),
            "main_armed": block([r for r in scored if r.get("main_arm_ok")]),
            "main_not_armed": block([r for r in scored if not r.get("main_arm_ok")]),
            "by_day": {d: block([r for r in scored if r.get("day") == d]) for d in sorted({r.get("day") for r in scored})}}
    return {"summary": summ, "rows": rows}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("kind", choices=["exits", "pullback"])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--day")
    g.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("--collar", type=float, default=10.0)
    ap.add_argument("--sleep", type=float, default=0.35, help="seconds between data calls")
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args(argv)
    res = cmd_exits(args) if args.kind == "exits" else cmd_pullback(args)
    print(json.dumps(res["summary"], indent=2, default=lambda x: round(x, 3) if isinstance(x, float) else str(x)))
    if not args.no_write:
        days = _days(args)
        out = REPORTS / "shadow_scores"
        out.mkdir(parents=True, exist_ok=True)
        p = out / f"{args.kind}_{days[0]}_{days[-1]}.json"
        p.write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
        print(f"wrote {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
