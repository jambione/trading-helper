#!/usr/bin/env python3
"""Where in the %R cycle should we buy? Square (what we do) vs the turn up.

POPULATION  names the book admitted (admit_funnel kept_symbols), 09-16..09-22,
            price >= $10 at the event. SIP 1m bars with extended hours from
            04:00 so the 112-bar slow %R is valid at the open, as it is live.

%R          signals.compute_percent_r_exhaustion with the desk's config
            (fast 21/EMA7, slow 112/EMA3, threshold 20). Same code as live.

EVENTS      scored from the close of the signal bar (the moment it is known),
            RTH 09:40-15:30, at most one event of a kind per name per 15 min:
  square     first bar both lines >= -20 and tight (what the desk arms on)
  os_tri     first bar after a dual-oversold run of >= 2 bars ends (TV ▲)
  fast_os    fast %R crosses up through -80
  mid_rise   fast crosses up through -50 with the slow line rising
CONTROL     every 5th RTH minute of the same name-days.

METRICS     up1 (+1% before -1%), up05 (+0.5% before -0.5%), r30, up30
            (best move in 30m). Split by date halves for stability.
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
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)
import bars  # noqa: E402
import runway_study as rs  # noqa: E402

DAYS = ["2026-09-16", "2026-09-17", "2026-09-18", "2026-09-21", "2026-09-22"]
EXT_CACHE = os.path.join(ROOT, "ai_reports", "ext_bars_cache.pkl")
COOL = 15 * 60


def admitted() -> dict:
    out = collections.defaultdict(set)
    t0 = time.mktime(time.strptime(DAYS[0], "%Y-%m-%d"))
    days = set(DAYS)
    for line in open(os.path.join(ROOT, "ai_reports", "events.jsonl")):
        if '"admit_funnel"' not in line:
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        ts = float(e.get("ts") or 0)
        if ts < t0 or bars.day_of(ts) not in days:
            continue
        for s in e.get("kept_symbols") or []:
            if rs.SYM_RE.match(str(s)):
                out[bars.day_of(ts)].add(str(s))
    return out


def fetch_ext(want: dict) -> dict:
    try:
        cache = pickle.load(open(EXT_CACHE, "rb"))
    except Exception:
        cache = {}
    cl = bars.client()
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    from alpaca.data.enums import DataFeed
    import pandas as pd
    for day, syms in sorted(want.items()):
        need = sorted(s for s in syms if (s, day) not in cache)
        d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=bars.ET)
        for i in range(0, len(need), 100):
            chunk = need[i:i + 100]
            try:
                df = cl.get_stock_bars(StockBarsRequest(
                    symbol_or_symbols=chunk, timeframe=TimeFrame(1, TimeFrameUnit.Minute),
                    start=d.replace(hour=4, minute=0).astimezone(timezone.utc),
                    end=d.replace(hour=16, minute=0).astimezone(timezone.utc),
                    limit=2_000_000, extended_hours=True, feed=DataFeed.SIP)).df
            except Exception as e:
                print(f"  ext fail {day}: {str(e)[:80]}", file=sys.stderr)
                continue
            got = {}
            if df is not None and not df.empty:
                if not isinstance(df.index, pd.MultiIndex):
                    df = pd.concat({chunk[0]: df}, names=["symbol"])
                for s in df.index.get_level_values("symbol").unique():
                    got[str(s)] = df.xs(s, level="symbol").sort_index()[
                        ["open", "high", "low", "close", "volume"]]
            for s in chunk:
                cache[(s, day)] = got.get(s)
            print(f"  ext {day}: {len(got)}/{len(chunk)}", file=sys.stderr)
            time.sleep(1.0)
    pickle.dump(cache, open(EXT_CACHE, "wb"))
    return cache


def events_for(df, cfg) -> dict:
    import signals
    x = signals.compute_percent_r_exhaustion(df, cfg)
    f, s = x["s_percentR"], x["l_percentR"]
    thr = float(cfg.get("rte_threshold", 20))
    ob_tight = x["rte_tight"].fillna(False).to_numpy()
    os_ = x["oversold"].fillna(False).to_numpy()
    osn = x["os_consecutive"].fillna(0).to_numpy()
    fv, sv = f.to_numpy(), s.to_numpy()
    ts = [t.timestamp() for t in x.index]
    out = collections.defaultdict(list)
    last = collections.defaultdict(lambda: -1e18)

    def add(kind, i):
        if ts[i] - last[kind] >= COOL:
            out[kind].append(i)
            last[kind] = ts[i]
    for i in range(1, len(ts)):
        m = bars.et_minutes(ts[i])
        if m < 9 * 60 + 40 or m > 15 * 60 + 30:
            continue
        if any(math.isnan(v) for v in (fv[i], sv[i], fv[i - 1], sv[i - 1])):
            continue
        if ob_tight[i] and not ob_tight[i - 1]:
            add("square", i)
        if os_[i - 1] and not os_[i] and osn[i - 1] >= 2:
            add("os_tri", i)
        if fv[i - 1] <= -100 + thr < fv[i]:
            add("fast_os", i)
        if fv[i - 1] <= -50 < fv[i] and sv[i] > sv[i - 1]:
            add("mid_rise", i)
    return {"events": out, "ts": ts}


def to_B(df):
    """(t, o, h, l, c, v) lists — runway_study.score_path's bar format."""
    return ([t.timestamp() for t in df.index], list(df["open"]), list(df["high"]),
            list(df["low"]), list(df["close"]), list(df["volume"]))


def main():
    from config import load_config
    cfg = load_config()
    want = admitted()
    print("admitted name-days:", sum(len(v) for v in want.values()), file=sys.stderr)
    cache = fetch_ext(want)
    res = collections.defaultdict(list)   # kind -> [(day, metrics)]
    for day, syms in want.items():
        for s in syms:
            df = cache.get((s, day))
            if df is None or len(df) < 150:
                continue
            ev = events_for(df, cfg)
            B = to_B(df)
            for kind, idxs in ev["events"].items():
                for i in idxs:
                    if B[4][i] < 10:
                        continue
                    sc = rs.score_path(B, i, B[4][i])
                    if sc:
                        res[kind].append((day, sc))
            for i in range(0, len(B[0]), 5):
                m = bars.et_minutes(B[0][i])
                if 9 * 60 + 40 <= m <= 15 * 60 + 30 and B[4][i] >= 10:
                    sc = rs.score_path(B, i, B[4][i])
                    if sc:
                        res["~control"].append((day, sc))
    h1 = set(DAYS[:3])

    def stat(rows, key):
        v = [r[key] for _, r in rows if r.get(key) is not None]
        return (rs.mean(v), len(v)) if v else (None, 0)

    print(f"{'event':<10}{'n':>6}{'up1':>8}{'up05':>8}{'r30':>9}{'±':>7}{'up30':>8}"
          f"{'up1 h1':>8}{'up1 h2':>8}{'r30 h1':>9}{'r30 h2':>9}")
    for kind in ("~control", "square", "os_tri", "fast_os", "mid_rise"):
        rows = res.get(kind, [])
        if not rows:
            continue
        r30 = [r["ret_30"] for _, r in rows if r.get("ret_30") is not None]
        se = statistics.stdev(r30) / math.sqrt(len(r30)) if len(r30) > 2 else float("nan")
        a = [(d, r) for d, r in rows if d in h1]
        b = [(d, r) for d, r in rows if d not in h1]
        f = lambda x, p=True: "—" if x is None else (f"{x:.1%}" if p else f"{x:+.3f}")
        print(f"{kind:<10}{len(rows):>6}{f(stat(rows,'tp_1.0')[0]):>8}{f(stat(rows,'tp_0.5')[0]):>8}"
              f"{f(stat(rows,'ret_30')[0], False):>9}{se:>7.3f}{f(stat(rows,'up_30')[0], False):>8}"
              f"{f(stat(a,'tp_1.0')[0]):>8}{f(stat(b,'tp_1.0')[0]):>8}"
              f"{f(stat(a,'ret_30')[0], False):>9}{f(stat(b,'ret_30')[0], False):>9}")


if __name__ == "__main__":
    main()
