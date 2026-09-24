#!/usr/bin/env python3
"""rehearse_whatif.py — score a fix on a replayed session: which names would it add?

Companion to rehearse_open.py (the observed book). A fix is judged by what
it would have let onto the book and whether those names then passed the
gates and armed, on the SIP bars of that day. No live state is written.

WHAT-IFS
  volume_clock   movers rvol paced against the minutes the delayed SIP daily
                 bar covers (movers_screener.sip_data_mins_open), not the
                 live clock. Re-scores every movers seed refused thin_rvol
                 in admit_ledger: rvol_fixed = rvol * f(m) / f(m - delay).

For each name the fix newly admits: first eligible minute, whether the live
desk ever seated it anyway, price band at that minute, the day's gap vs the
prior close, the name-day SIP spread (10:30 median, the study proxy), and
every one-arm event (fast %R crosses -50 up with the slow line rising, on
complete 1m SIP bars) from eligibility to the window end, with the first
arm's +1%-before--1% outcome over the next 60 minutes.

USAGE (on the mini; needs SIP data at least 15 minutes old)
    .venv/bin/python tools/rehearse_whatif.py --day 2026-09-24 --start 09:30 --end 12:00
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))
import bars  # noqa: E402
import rehearse_open as ro  # noqa: E402


def volume_clock_candidates(day: str, start: str, end: str, reports: str,
                            floor: float = 1.0, delay: float = 15.0) -> dict:
    """{symbol: (first_ts_fixed_passes, rvol_logged, rvol_fixed)} for movers thin_rvol rows."""
    import morning_funnel as mf
    t0, t1 = ro._day_bounds(day)
    m0 = int(start[:2]) * 60 + int(start[3:])
    m1 = int(end[:2]) * 60 + int(end[3:])
    out: dict = {}
    for e in ro._jsonl(os.path.join(reports, "admit_ledger", f"{day}.jsonl"), t0, t1,
                       prefilter=('"thin_rvol"',)):
        if e.get("stage") != "seed" or e.get("source") != "movers" or e.get("reason") != "thin_rvol":
            continue
        rv = e.get("rvol")
        if not isinstance(rv, (int, float)) or rv <= 0:
            continue
        m = ro._mins(e["ts"])
        if not (m0 <= m < m1):
            continue
        live = m - 570 + (datetime.fromtimestamp(e["ts"], ro.ET).second / 60.0)
        if live <= 0:
            continue
        fixed = rv * mf.expected_fraction(live) / mf.expected_fraction(max(1.0, live - delay))
        sym = str(e.get("symbol"))
        if fixed >= floor and sym not in out:
            out[sym] = (e["ts"], float(rv), float(fixed))
    return out


def observed_seated(day: str, reports: str) -> set:
    t0, t1 = ro._day_bounds(day)
    seen: set = set()
    for e in ro._jsonl(os.path.join(reports, "events.jsonl"), t0, t1,
                       prefilter=('"admit_funnel"',)):
        if e.get("kind") == "admit_funnel":
            seen.update(e.get("kept_symbols") or [])
    return seen


def _minute_bars(cl, syms: list[str], day: str) -> dict:
    """{sym: DataFrame} of 1m SIP bars 04:00 -> min(16:00, now-20m). No cache writes."""
    import pandas as pd
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=bars.ET)
    end = min(d.replace(hour=16).astimezone(timezone.utc),
              datetime.now(timezone.utc) - timedelta(minutes=20))
    out: dict = {}
    for i in range(0, len(syms), 50):
        chunk = syms[i:i + 50]
        try:
            df = cl.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=chunk, timeframe=TimeFrame(1, TimeFrameUnit.Minute),
                start=d.replace(hour=4).astimezone(timezone.utc), end=end,
                limit=2_000_000, extended_hours=True, feed=DataFeed.SIP)).df
        except Exception as e:  # noqa: BLE001
            print(f"  bars fail: {str(e)[:100]}", file=sys.stderr)
            continue
        if df is None or df.empty:
            continue
        if not isinstance(df.index, pd.MultiIndex):
            df = pd.concat({chunk[0]: df}, names=["symbol"])
        for s in df.index.get_level_values("symbol").unique():
            out[str(s)] = df.xs(s, level="symbol").sort_index()[
                ["open", "high", "low", "close", "volume"]]
        time.sleep(0.5)
    return out


def _prior_close(cl, syms: list[str], day: str) -> dict:
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    d = datetime.strptime(day, "%Y-%m-%d")
    out: dict = {}
    try:
        resp = cl.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=syms, timeframe=TimeFrame.Day,
            start=(d - timedelta(days=10)).replace(tzinfo=timezone.utc),
            end=datetime.now(timezone.utc) - timedelta(minutes=20),
            feed=DataFeed.SIP, limit=10000))
        for s, seq in (getattr(resp, "data", {}) or {}).items():
            prev = [b for b in seq if bars.day_of(b.timestamp.timestamp()) < day]
            if prev:
                out[s] = float(prev[-1].close)
    except Exception as e:  # noqa: BLE001
        print(f"  daily fail: {str(e)[:100]}", file=sys.stderr)
    return out


def score(day: str, start: str, end: str, reports: str, cands: dict,
          lo: float = 20.0, hi: float = 100.0, max_spread: float = 0.20,
          gap_block: float = 1.0) -> list[dict]:
    from config import load_config
    import entry_screen as es
    import name_rank_study as nr
    cfg = load_config()
    cl = bars.client()
    syms = sorted(cands)
    mb = _minute_bars(cl, syms, day)
    pc = _prior_close(cl, syms, day)
    seated = observed_seated(day, reports)
    m1 = int(end[:2]) * 60 + int(end[3:])
    rows = []
    for s in syms:
        ts0, rv, rvf = cands[s]
        r = {"symbol": s, "eligible": datetime.fromtimestamp(ts0, ro.ET).strftime("%H:%M"),
             "rvol_logged": round(rv, 2), "rvol_fixed": round(rvf, 2),
             "seated_live": s in seated}
        df = mb.get(s)
        if df is None or len(df) < 40:
            r["note"] = "no bars"
            rows.append(r)
            continue
        ind = es.indicators(df, cfg, None)
        t, c, h, l = ind["ts"], ind["c"], ind["h"], ind["l"]
        first = ind["first"]
        import numpy as np
        k0 = int(np.searchsorted(t, ts0))
        px = float(c[min(k0, len(c) - 1)])
        r["price"] = round(px, 2)
        r["band_ok"] = lo <= px <= hi
        day_open = float(df["open"].to_numpy()[first]) if first < len(t) else None
        prev = pc.get(s)
        r["gap"] = round((day_open / prev - 1) * 100, 2) if day_open and prev else None
        r["gap_ok"] = r["gap"] is not None and r["gap"] >= -gap_block
        sp = nr.name_day_spread(cl, s, day)
        time.sleep(0.2)
        r["spread"] = round(sp, 3) if sp is not None else None
        r["spread_ok"] = sp is not None and sp <= max_spread
        arms = []
        for i in range(max(k0, first + 12), len(t) - 1):
            if ind["mins"][i] >= m1:
                break
            if "mid_rise" in es.fire(ind, i):
                arms.append(i)
        r["arms"] = len(arms)
        if arms:
            i = arms[0]
            r["first_arm"] = datetime.fromtimestamp(t[i], ro.ET).strftime("%H:%M")
            e0 = c[i]
            out = "open"
            for j in range(i + 1, min(len(t), i + 61)):
                if l[j] <= e0 * 0.99:
                    out = "-1% first"
                    break
                if h[j] >= e0 * 1.01:
                    out = "+1% first"
                    break
            r["first_arm_60m"] = out
        r["passes_gates"] = bool(r["band_ok"] and r["gap_ok"] and r["spread_ok"])
        rows.append(r)
    return rows


def project(obs: dict, rows: list[dict]) -> list[dict]:
    """Per-slot projection for volume_clock + armable-only book.

    armable_proj = observed armable seats + names the volume fix newly admits
    that pass price/gap/spread and are eligible by that slot and were not
    already seated. Gate-blocked seats are evicted (armable-only book), so
    they count toward neither side: the book shown is the armable book plus
    the data-blocked seats the freshness fix has not cleared yet.
    """
    add_at = sorted(r["eligible"] for r in rows
                    if r.get("passes_gates") and not r["seated_live"])
    out = []
    for r in obs["rows"]:
        extra = sum(1 for t in add_at if t <= r["t"])
        out.append({"t": r["t"], "book_obs": r["book"], "armable_obs": r["armable"],
                    "gate_evicted": r["gate"], "added": extra,
                    "armable_proj": r["armable"] + extra,
                    "book_proj": r["book"] - r["gate"] + extra,
                    "data_blocked": r["data"]})
    return out


def render_projection(proj: list[dict]) -> str:
    L = ["", "PROJECTION volume_clock + armable-only book (data-blocked seats unchanged)", "",
         f"{'time':>5} {'book':>5} {'->':>2} {'proj':>4} {'armable':>7} {'->':>2} {'proj':>4} "
         f"{'evicted':>7} {'added':>5} {'data':>4}"]
    for p in proj:
        L.append(f"{p['t']:>5} {p['book_obs']:>5} {'':>2} {p['book_proj']:>4} {p['armable_obs']:>7} "
                 f"{'':>2} {p['armable_proj']:>4} {p['gate_evicted']:>7} {p['added']:>5} "
                 f"{p['data_blocked']:>4}")
    after = [p for p in proj if p["t"] >= "09:40"]
    if after:
        at = after[0]
        med = sorted(p["armable_proj"] for p in after)[len(after) // 2]
        dat = sum(p["data_blocked"] for p in after) / max(1, sum(p["book_proj"] for p in after))
        L += ["", "Pass criteria (projected):",
              f"  [{'PASS' if at['book_proj'] >= 10 else 'FAIL'}] book >= 10 by 09:40         "
              f"{at['book_proj']} at {at['t']}",
              f"  [{'PASS' if at['armable_proj'] >= 6 else 'FAIL'}] >= 6 armable at 09:40       "
              f"{at['armable_proj']} at {at['t']}",
              f"  [{'PASS' if med >= 6 else 'FAIL'}] median armable after 09:40  median {med}",
              f"  [{'PASS' if dat < 0.10 else 'FAIL'}] data-blocked share < 10%     {dat:.0%}  "
              f"(the freshness fix's job)"]
    return "\n".join(L)


def render(rows: list[dict], label: str) -> str:
    L = [f"WHAT-IF {label}: names the fix newly admits", ""]
    L.append(f"{'sym':<6} {'elig':>5} {'rv_log':>6} {'rv_fix':>6} {'live?':>5} {'px':>7} "
             f"{'gap%':>6} {'sprd%':>6} {'gates':>5} {'arms':>4} {'1st arm':>7} {'60m':>9}")
    for r in sorted(rows, key=lambda x: x["eligible"]):
        L.append(f"{r['symbol']:<6} {r['eligible']:>5} {r['rvol_logged']:>6} {r['rvol_fixed']:>6} "
                 f"{('yes' if r['seated_live'] else 'no'):>5} {str(r.get('price', '-')):>7} "
                 f"{str(r.get('gap', '-')):>6} {str(r.get('spread', '-')):>6} "
                 f"{('ok' if r.get('passes_gates') else 'no'):>5} {str(r.get('arms', '-')):>4} "
                 f"{r.get('first_arm', '-'):>7} {r.get('first_arm_60m', '-'):>9}"
                 + (f"  {r['note']}" if r.get("note") else ""))
    new = [r for r in rows if not r["seated_live"]]
    ok = [r for r in new if r.get("passes_gates")]
    armed = [r for r in ok if r.get("arms")]
    wins = sum(1 for r in armed if r.get("first_arm_60m") == "+1% first")
    loss = sum(1 for r in armed if r.get("first_arm_60m") == "-1% first")
    L += ["",
          f"newly admitted by the fix:        {len(rows)} ({len(new)} never seated live)",
          f"  of those, pass price/gap/spread: {len(ok)}",
          f"  of those, armed in the window:   {len(armed)} "
          f"({sum(r['arms'] for r in armed)} arm events)",
          f"  first arm +1% before -1% (60m):  {wins} win / {loss} loss / "
          f"{len(armed) - wins - loss} neither",
          "  (coin-flip base rate for admitted names is ~51%; this measures supply, not edge)"]
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default=datetime.now(ro.ET).strftime("%Y-%m-%d"))
    ap.add_argument("--start", default="09:30")
    ap.add_argument("--end", default="12:00")
    ap.add_argument("--what", default="volume_clock", choices=["volume_clock"])
    ap.add_argument("--floor", type=float, default=1.0)
    ap.add_argument("--delay", type=float, default=15.0)
    ap.add_argument("--reports", default=os.path.join(ROOT, "ai_reports"))
    ap.add_argument("--json")
    args = ap.parse_args()
    cands = volume_clock_candidates(args.day, args.start, args.end, args.reports,
                                    floor=args.floor, delay=args.delay)
    print(f"movers thin_rvol refusals the fix would pass: {len(cands)} names", file=sys.stderr)
    rows = score(args.day, args.start, args.end, args.reports, cands)
    print(render(rows, f"{args.what} {args.day} {args.start}-{args.end}"))
    obs = ro.replay(args.day, args.start, args.end, 5, args.reports)
    print(render_projection(project(obs, rows)))
    if args.json:
        with open(args.json, "w") as f:
            json.dump(rows, f, indent=1)


if __name__ == "__main__":
    main()
