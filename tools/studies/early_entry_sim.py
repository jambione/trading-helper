#!/usr/bin/env python3
"""Early-entry what-if: the desk's real trades, bought N minutes before the real fill.

Read-only. Places no orders and changes no config.

For every closed trade in ai_reports/outcomes.jsonl:

  actual   entry_price -> exit_price (the real fills, already net of the spread paid)
  early_A  buy at the open of the SIP 1-min bar that starts N minutes before the
           real fill minute, plus half the quoted spread at entry; sell at the
           real exit fill. "Same sell, earlier buy."
  early_B  same earlier buy; sell at the open of the bar N minutes before the
           real exit minute, minus half the spread. "Everything N minutes earlier."

Same share count as the real trade, so dollar P&L is comparable.

HINDSIGHT WARNING: the desk bought at minute M because the tape had already
moved by M. Buying at M-N assumes we knew at M-N that the signal was coming.
The `on_board` cut (dwell_sec >= N*60: the name was already admitted and
watched N minutes before the fill) is the subset where an earlier buy was at
least physically possible.

Run on the mini (the MacBook keys are dead):
  .venv/bin/python tools/studies/early_entry_sim.py --since 2026-09-14 --shift 5
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import math
import os
import pickle
import statistics
import sys
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if not os.path.isdir(os.path.join(ROOT, "ai_reports")):  # copied to /tmp and run from the repo
    ROOT = os.getcwd()
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)
import bars  # noqa: E402

ET = bars.ET


def load_trades(path: str, since: str) -> list[dict]:
    out = []
    for line in open(path):
        try:
            r = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        et, xt = r.get("entry_time"), r.get("exit_time")
        ep, xp = r.get("entry_price"), r.get("exit_price")
        q = r.get("total_qty")
        if not (et and xt and ep and xp and q) or float(q) <= 0 or float(xt) <= float(et):
            continue
        day = bars.day_of(float(et))
        if day < since:
            continue
        f = r.get("features") or {}
        bid, ask = f.get("bid"), f.get("ask")
        half = None
        if bid and ask and float(ask) >= float(bid) > 0:
            half = (float(ask) - float(bid)) / 2 / ((float(ask) + float(bid)) / 2)
        out.append({
            "sym": r["symbol"], "day": day, "et": float(et), "xt": float(xt),
            "ep": float(ep), "xp": float(xp), "qty": float(q),
            "pl": r.get("realized_pl_usd"), "source": r.get("source") or "?",
            "reason": r.get("close_reason") or "?", "half": half,
            "dwell": f.get("dwell_sec"),
        })
    return out


def fetch(trades: list[dict], cache_path: str) -> dict:
    try:
        cache = pickle.load(open(cache_path, "rb"))
    except Exception:  # noqa: BLE001
        cache = {}
    want = collections.defaultdict(set)
    for t in trades:
        if (t["sym"], t["day"]) not in cache:
            want[t["day"]].add(t["sym"])
    if not want:
        return cache
    import pandas as pd
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    cl = bars.client()
    if cl is None:
        raise SystemExit("no Alpaca data client (config/secrets.json); run on the mini")
    for day, syms in sorted(want.items()):
        d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
        syms = sorted(syms)
        for i in range(0, len(syms), 100):
            chunk = syms[i:i + 100]
            df = None
            for attempt in range(4):
                try:
                    df = cl.get_stock_bars(StockBarsRequest(
                        symbol_or_symbols=chunk, timeframe=TimeFrame(1, TimeFrameUnit.Minute),
                        start=d.replace(hour=4).astimezone(timezone.utc),
                        end=min(d.replace(hour=20).astimezone(timezone.utc),
                                datetime.fromtimestamp(time.time() - 16 * 60, timezone.utc)),
                        limit=2_000_000, extended_hours=True, feed=DataFeed.SIP)).df
                    break
                except Exception as e:  # noqa: BLE001
                    print(f"  fetch fail {day} try {attempt}: {str(e)[:80]}", file=sys.stderr)
                    time.sleep(5 * (attempt + 1))
            if df is None:
                print(f"  bars {day}: fetch failed for {len(chunk)} names (not cached)", file=sys.stderr)
                continue
            got = {}
            if not df.empty:
                if not isinstance(df.index, pd.MultiIndex):
                    df = pd.concat({chunk[0]: df}, names=["symbol"])
                for s in df.index.get_level_values("symbol").unique():
                    sub = df.xs(s, level="symbol").sort_index()
                    got[str(s)] = {int(ts.timestamp()): (float(o), float(c))
                                   for ts, o, c in zip(sub.index, sub["open"], sub["close"])}
            for s in chunk:
                cache[(s, day)] = got.get(s)
            print(f"  bars {day}: {len(got)}/{len(chunk)}", file=sys.stderr)
            time.sleep(0.5)
    pickle.dump(cache, open(cache_path, "wb"))
    return cache


def px_at(series: dict, ts: int, max_back: int = 3) -> float | None:
    """Open of the bar starting at ts; if that minute has no print, the close of the
    latest bar within max_back minutes before it (the last trade before ts)."""
    if ts in series:
        return series[ts][0]
    for k in range(1, max_back + 1):
        b = series.get(ts - 60 * k)
        if b is not None:
            return b[1]
    return None


def simulate(trades: list[dict], cache: dict, shift: int, default_half: float) -> tuple[list[dict], dict]:
    rows, drops = [], collections.Counter()
    for t in trades:
        series = cache.get((t["sym"], t["day"]))
        if not series:
            drops["no_bars"] += 1
            continue
        half = t["half"] if t["half"] is not None else default_half
        m_in = int(t["et"] // 60 * 60)
        m_out = int(t["xt"] // 60 * 60)
        raw_in = px_at(series, m_in - 60 * shift)
        if raw_in is None:
            drops["no_early_bar"] += 1
            continue
        raw_out = px_at(series, m_out - 60 * shift)
        early_in = raw_in * (1 + half)
        a_ret = t["xp"] / early_in - 1
        b_ret = (raw_out * (1 - half)) / early_in - 1 if raw_out is not None and m_out - 60 * shift > m_in - 60 * shift else None
        act = t["xp"] / t["ep"] - 1
        rows.append({
            **{k: t[k] for k in ("sym", "day", "source", "reason", "qty", "ep", "xp")},
            "entry_et": datetime.fromtimestamp(t["et"], ET).strftime("%H:%M:%S"),
            "hold_min": round((t["xt"] - t["et"]) / 60, 1),
            "early_px": round(early_in, 4),
            "pre_run": t["ep"] / raw_in - 1,
            "act": act, "a": a_ret, "b": b_ret,
            "act_usd": (t["xp"] - t["ep"]) * t["qty"],
            "a_usd": (t["xp"] - early_in) * t["qty"],
            "b_usd": ((raw_out * (1 - half)) - early_in) * t["qty"] if b_ret is not None else None,
            "on_board": bool(t["dwell"] is not None and float(t["dwell"]) >= 60 * shift),
            "half_est": t["half"] is None,
        })
    return rows, drops


def day_t(rows: list[dict], key: str) -> tuple[float | None, float | None, int]:
    by = collections.defaultdict(list)
    for r in rows:
        if r.get(key) is not None:
            by[r["day"]].append(r[key])
    means = [statistics.mean(v) for v in by.values()]
    if len(means) < 3:
        return (statistics.mean(means) if means else None), None, len(means)
    mu, sd = statistics.mean(means), statistics.stdev(means)
    return mu, (mu / (sd / math.sqrt(len(means))) if sd > 0 else None), len(means)


def block(title: str, rows: list[dict]) -> list[str]:
    if not rows:
        return [f"### {title}", "(no trades)", ""]
    n = len(rows)
    bp = lambda x: "—" if x is None else f"{x * 1e4:+.0f} bp"  # noqa: E731
    out = [f"### {title}  (n={n}, days={len({r['day'] for r in rows})})", "",
           "| | total $ | mean/trade | median | win rate |", "|---|---:|---:|---:|---:|"]
    for lab, k, ku in (("actual", "act", "act_usd"), ("early A: same sell", "a", "a_usd"),
                       ("early B: sell also early", "b", "b_usd")):
        v = [r[k] for r in rows if r[k] is not None]
        u = [r[ku] for r in rows if r[ku] is not None]
        out.append(f"| {lab} | {sum(u):+.2f} | {bp(statistics.mean(v))} | {bp(statistics.median(v))} | "
                   f"{100 * sum(x > 0 for x in v) / len(v):.0f}% |")
    pre = [r["pre_run"] for r in rows]
    for r in rows:
        r["lift_a"] = r["a"] - r["act"]
        r["lift_b"] = (r["b"] - r["act"]) if r["b"] is not None else None
    la, ta, nd = day_t(rows, "lift_a")
    lb, tb, _ = day_t(rows, "lift_b")
    fmt_t = lambda t: "—" if t is None else f"{t:+.2f}"  # noqa: E731
    out += ["",
            f"- price move in the {SHIFT} min before our fill: median {bp(statistics.median(pre))}, "
            f"mean {bp(statistics.mean(pre))}, up in {100 * sum(x > 0 for x in pre) / n:.0f}% of trades",
            f"- early A minus actual, day-equal: {bp(la)} (t {fmt_t(ta)}, {nd} days)",
            f"- early B minus actual, day-equal: {bp(lb)} (t {fmt_t(tb)})", ""]
    return out


SHIFT = 5


def main() -> None:
    global SHIFT
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-09-14")
    ap.add_argument("--shift", type=int, default=5)
    ap.add_argument("--outcomes", default=os.path.join(ROOT, "ai_reports", "outcomes.jsonl"))
    ap.add_argument("--cache", default="/tmp/early_entry_bars.pkl")
    ap.add_argument("--csv", default="/tmp/early_entry_trades.csv")
    a = ap.parse_args()
    SHIFT = a.shift
    trades = load_trades(a.outcomes, a.since)
    halves = [t["half"] for t in trades if t["half"] is not None]
    default_half = statistics.median(halves) if halves else 0.001
    cache = fetch(trades, a.cache)
    rows, drops = simulate(trades, cache, a.shift, default_half)
    lines = [f"# Early-entry sim: buy {a.shift} min before the real fill",
             f"trades loaded {len(trades)} since {a.since}; scored {len(rows)}; drops {dict(drops)}; "
             f"half-spread median {default_half * 1e4:.1f} bp (used where the quote was missing: "
             f"{sum(r['half_est'] for r in rows)} trades)", ""]
    lines += block("All trades", rows)
    lines += block(f"On the board >= {a.shift} min before the fill (earlier buy was possible)",
                   [r for r in rows if r["on_board"]])
    for src in sorted({r["source"] for r in rows}):
        lines += block(f"source = {src}", [r for r in rows if r["source"] == src])
    lines.append("### By day (early A = same sell)")
    lines += ["", "| day | n | actual $ | early A $ | early B $ |", "|---|---:|---:|---:|---:|"]
    for d in sorted({r["day"] for r in rows}):
        rr = [r for r in rows if r["day"] == d]
        lines.append(f"| {d} | {len(rr)} | {sum(r['act_usd'] for r in rr):+.2f} | "
                     f"{sum(r['a_usd'] for r in rr):+.2f} | {sum(r['b_usd'] or 0 for r in rr):+.2f} |")
    print("\n".join(lines))
    with open(a.csv, "w", newline="") as f:
        cols = [k for k in rows[0] if k not in ("lift_a", "lift_b")] if rows else []
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    main()
