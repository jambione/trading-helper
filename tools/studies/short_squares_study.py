#!/usr/bin/env python3
"""Short the desk's own entry signals. Pre-registered: docs/studies/short_squares_prereg.json.

A  every square event (signal_timing.events_for: the desk's %R code, live config) on admitted names,
   2026-09-04..10-02, 09:45-15:30 ET, one per name per 15 min.
B  every filled desk BUY on the paper account since 2026-09-23, flipped to a short.
Short at the open of the first bar after the signal bar closes / after the fill; cover at the close
of the bar 5/15/30 min on (or 15:55); buy-stops +1/2/3/5% filled at max(stop, bar open) + 0.10%.
Cost by price tier (short_fade_study). Primary set: shortable + easy_to_borrow today, not in SSR.

Run on the mini: .venv/bin/python tools/studies/short_squares_study.py
"""
from __future__ import annotations

import collections
import json
import math
import os
import pickle
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)
import bars  # noqa: E402
import signal_timing as st  # noqa: E402

HORIZONS = (5, 15, 30)
STOPS = (1.0, 2.0, 3.0, 5.0)
PRIMARY = (30, 3.0)
OUT = os.path.join(ROOT, "ai_reports", "short_squares")
SPLIT_A = [("tune", "2026-09-04", "2026-09-17"), ("validate", "2026-09-18", "2026-09-25"),
           ("holdout", "2026-09-28", "2026-10-02")]
SPLIT_B = [("B1", "2026-09-23", "2026-09-28"), ("B2", "2026-09-29", "2026-10-02")]


def cost(px: float) -> float:
    return 0.20 if px >= 10 else 0.40 if px >= 5 else 1.00


def at(day: str, hh: int, mm: int) -> float:
    y, m, d = map(int, day.split("-"))
    return datetime(y, m, d, hh, mm, tzinfo=bars.ET).timestamp()


def admit_days() -> list[str]:
    days = set()
    for line in open(os.path.join(ROOT, "ai_reports", "events.jsonl")):
        if '"admit_funnel"' in line:
            try:
                d = bars.day_of(float(json.loads(line)["ts"]))
            except Exception:  # noqa: BLE001
                continue
            if "2026-09-04" <= d <= "2026-10-02" and datetime.strptime(d, "%Y-%m-%d").weekday() < 5:
                days.add(d)
    return sorted(days)


def desk_fills() -> list[dict]:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest
    from config import load_config
    c = load_config() or {}
    tc = TradingClient(c.get("api_key"), c.get("secret_key"), paper=True)
    out, after = [], datetime(2026, 9, 23, tzinfo=timezone.utc)
    until = datetime.now(timezone.utc)
    while True:
        got = tc.get_orders(GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=500, after=after,
                                             until=until, direction="desc"))
        for o in got:
            if str(getattr(o.side, "value", o.side)).lower() == "buy" and o.filled_at and o.filled_avg_price:
                out.append({"sym": o.symbol, "t": o.filled_at.timestamp(), "fill": float(o.filled_avg_price),
                            "day": bars.day_of(o.filled_at.timestamp())})
        if len(got) < 500:
            break
        until = min(o.submitted_at for o in got)
    return sorted(out, key=lambda r: r["t"])


def shortable(syms: set[str]) -> dict[str, tuple[bool, bool]]:
    p = os.path.join(OUT, "shortable.json")
    have = json.load(open(p)) if os.path.exists(p) else {}
    from alpaca.trading.client import TradingClient
    from config import load_config
    c = load_config() or {}
    tc = TradingClient(c.get("api_key"), c.get("secret_key"), paper=True)
    for s in sorted(syms - set(have)):
        try:
            a = tc.get_asset(s)
            have[s] = [bool(a.shortable), bool(a.easy_to_borrow)]
        except Exception:  # noqa: BLE001
            have[s] = [False, False]
        time.sleep(0.25)
    json.dump(have, open(p, "w"))
    return {k: tuple(v) for k, v in have.items()}


def prev_closes(want: dict[str, set]) -> dict[tuple, float]:
    p = os.path.join(OUT, "prev_close.pkl")
    have = pickle.load(open(p, "rb")) if os.path.exists(p) else {}
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    cl = bars.client()
    for day, syms in sorted(want.items()):
        need = sorted(s for s in syms if (s, day) not in have)
        d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=bars.ET)
        for i in range(0, len(need), 200):
            chunk = need[i:i + 200]
            try:
                got = cl.get_stock_bars(StockBarsRequest(
                    symbol_or_symbols=chunk, timeframe=TimeFrame.Day, start=(d - timedelta(days=8)).astimezone(timezone.utc),
                    end=(d - timedelta(hours=1)).astimezone(timezone.utc), feed=DataFeed.SIP,
                    adjustment=Adjustment.RAW)).data
            except Exception as e:  # noqa: BLE001
                print(f"  daily fail {day}: {str(e)[:80]}", file=sys.stderr)
                continue
            for s in chunk:
                bs = [b for b in (got or {}).get(s, []) if bars.day_of(b.timestamp.timestamp()) < day]
                have[(s, day)] = float(bs[-1].close) if bs else None
            time.sleep(0.5)
    pickle.dump(have, open(p, "wb"))
    return have


def score(B, i: int, day: str, extra: dict) -> dict | None:
    """Short at the open of bar i."""
    t, o, h, _l, c = B[0], B[1], B[2], B[3], B[4]
    if i >= len(t):
        return None
    px = o[i]
    if not px or px < 1.0 or t[i] > at(day, 15, 30):
        return None
    close_i = max((k for k, ts in enumerate(t) if ts <= at(day, 15, 55)), default=-1)
    if close_i <= i:
        return None
    row = {**extra, "day": day, "px": px, "cost": cost(px)}
    for H in HORIZONS:
        j = min(i + H - 1, close_i)
        row[f"r{H}"] = (px - c[j]) / px * 100 - row["cost"]
        row[f"sq{H}"] = (max(h[i:j + 1]) / px - 1) * 100
        for S in STOPS:
            stop, res = px * (1 + S / 100), None
            for k in range(i, j + 1):
                if h[k] >= stop:
                    res = (px - max(stop, o[k]) * 1.001) / px * 100 - row["cost"]
                    break
            row[f"r{H}_s{S:g}"] = res if res is not None else row[f"r{H}"]
    return row


def summ(rows: list[dict], key: str) -> str:
    x = [r[key] for r in rows if r.get(key) is not None]
    if len(x) < 3:
        return f"n={len(x)}"
    m, sd = statistics.mean(x), statistics.stdev(x)
    xs = sorted(x)
    q = lambda p: xs[max(0, min(len(xs) - 1, int(p * (len(xs) - 1))))]  # noqa: E731
    return (f"n={len(x):4d} mean {m:+6.2f}% med {statistics.median(x):+6.2f}% win {sum(v > 0 for v in x) / len(x):4.0%} "
            f"p5 {q(.05):+6.2f} p1 {q(.01):+6.2f} t {m / (sd / math.sqrt(len(x))):+5.2f}")


def report(title: str, rows: list[dict], splits) -> None:
    print(f"\n######## {title}: {len(rows)} trades")
    for name, lo, hi in splits:
        part = [r for r in rows if lo <= r["day"] <= hi]
        print(f"\n  [{name}] {lo}..{hi}")
        for H in HORIZONS:
            print(f"    {H:2d}m no stop  {summ(part, f'r{H}')}")
            for S in STOPS:
                tag = "  <- PRIMARY" if (H, S) == PRIMARY else ""
                print(f"    {H:2d}m stop+{S:g}% {summ(part, f'r{H}_s{S:g}')}{tag}")
    sq = sorted(r["sq30"] for r in rows if r.get("sq30") is not None)
    if sq:
        q = lambda p: sq[int(p * (len(sq) - 1))]  # noqa: E731
        print(f"  squeeze within 30 m (max high vs entry): p50 {q(.5):+.2f}% p90 {q(.9):+.2f}% p95 {q(.95):+.2f}% "
              f"p99 {q(.99):+.2f}% max {sq[-1]:+.2f}%; >= +3% {sum(v >= 3 for v in sq) / len(sq):.0%}")


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    from config import load_config
    cfg = load_config()
    days = admit_days()
    st.DAYS = days
    want = st.admitted()
    print(f"days {days[0]}..{days[-1]} ({len(days)}), admitted name-days {sum(len(v) for v in want.values())}", file=sys.stderr)
    fills = desk_fills()
    print(f"desk buy fills since 9/23: {len(fills)}", file=sys.stderr)
    for f in fills:
        want.setdefault(f["day"], set()).add(f["sym"])
    cache = st.fetch_ext(want)
    pc = prev_closes(want)
    sh = shortable({s for v in want.values() for s in v})

    def flags(sym, day, px):
        s_ok, etb = sh.get(sym, (False, False))
        prev = pc.get((sym, day))
        ssr = bool(prev and px <= prev * 0.90)
        return {"sym": sym, "shortable": s_ok, "etb": etb, "ssr": ssr,
                "primary": s_ok and etb and not ssr}

    A, Bf = [], []
    for day, syms in sorted(want.items()):
        for s in syms:
            df = cache.get((s, day))
            if df is None or len(df) < 150:
                continue
            Bars = st.to_B(df)
            ev = st.events_for(df, cfg)["events"].get("square", [])
            for i in ev:
                if bars.et_minutes(Bars[0][i]) < 9 * 60 + 45:
                    continue
                r = score(Bars, i + 1, day, flags(s, day, Bars[1][i + 1] if i + 1 < len(Bars[1]) else 0))
                if r:
                    A.append(r)
    for f in fills:
        df = cache.get((f["sym"], f["day"]))
        if df is None:
            continue
        Bars = st.to_B(df)
        i = next((k for k, ts in enumerate(Bars[0]) if ts > f["t"]), None)
        if i is None:
            continue
        r = score(Bars, i, f["day"], {**flags(f["sym"], f["day"], Bars[1][i]), "fill": f["fill"]})
        if r:
            Bf.append(r)
    json.dump({"A": A, "B": Bf}, open(os.path.join(OUT, "rows.json"), "w"))
    for label, rows, splits in (("A squares, PRIMARY (shortable+ETB, no SSR)", [r for r in A if r["primary"]], SPLIT_A),
                                ("A squares, all", A, SPLIT_A),
                                ("B desk fills flipped, PRIMARY", [r for r in Bf if r["primary"]], SPLIT_B),
                                ("B desk fills flipped, all", Bf, SPLIT_B)):
        report(label, rows, splits)
    pa = [r for r in A if r["primary"]]
    tv = [r[f"r{PRIMARY[0]}_s{PRIMARY[1]:g}"] for r in pa if r["day"] <= "2026-09-25"]
    print(f"\nexcluded from A primary: not shortable {sum(not r['shortable'] for r in A)}, not ETB "
          f"{sum(r['shortable'] and not r['etb'] for r in A)}, SSR {sum(r['ssr'] for r in A)}")
    if len(tv) > 2:
        m, sd = statistics.mean(tv), statistics.stdev(tv)
        print(f"PRIMARY cell tune+validate pooled: n {len(tv)} mean {m:+.3f}% t {m / (sd / math.sqrt(len(tv))):+.2f}")


if __name__ == "__main__":
    main()
