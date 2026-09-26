#!/usr/bin/env python3
"""book_names_study.py — which names did each book hold when they crossed -50, and did they run?

Name-choice half of docs/ENTRY_OPTIMIZATION_2026-09-25.md, on the session
recordings (no replay engine, no fills): for every fast %R (21, EWM 7) up
cross through -50 on the desk's IEX 1m bars (premarket-warmed, as live) of
every name the replay bar cache holds, $20-$100, it records whether the name
was seated in (a) the LIVE book (recorded entry_watch_state) and (b) the book
server's SHADOW would-seat list at that minute, plus (c) room below the
running SIP session high (causal) and the SIP close-to-close result 15/30
minutes later net of the price-tier spread. Counted per cross and per name.

USAGE (on the mini, after hours; reads the replay bar cache and recordings)
  .venv/bin/python tools/studies/book_names_study.py --day 2026-09-25
  .venv/bin/python tools/studies/book_names_study.py --day 2026-09-24 --start 13:10
"""
from __future__ import annotations

import argparse
import bisect
import gzip
import json
import os
import pickle
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "tools"))
import mid_rise_runway_study as mr  # noqa: E402
import replay_session as rs  # noqa: E402

ET = ZoneInfo("America/New_York")
_REPO = Path(os.path.dirname(os.path.dirname(HERE)))
ROOT = _REPO if (_REPO / "ai_reports").exists() else Path.cwd()  # run from the repo root


def cost(px):
    return 0.10 if px >= 20 else 0.20 if px >= 10 else 0.40 if px >= 5 else 1.00


def minute(ts):
    return int(ts // 60) * 60


def live_book(day):
    """minute -> set of seated symbols (last recorded state in that minute)."""
    p = Path.home() / "session_snapshots" / day / "state_snapshots.jsonl.gz"
    out = {}
    for line in gzip.open(p, "rt"):
        if '"ai_reports/entry_watch_state.json"' not in line[:300]:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("file") == "ai_reports/entry_watch_state.json":
            out[minute(r["ts"])] = set(rs.watch_rows(r.get("data")))
    return out


def shadow_book(day):
    p = ROOT / "ai_reports" / "book_server_shadow" / f"{day}.jsonl"
    out = {}
    for line in open(p):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("kind") == "shadow_book":
            out[minute(r["ts"])] = set(r.get("would_symbols") or [])
    return out


def at_or_before(d, keys, m):
    i = bisect.bisect_right(keys, m) - 1
    return d[keys[i]] if i >= 0 else set()


def st(rows):
    if not rows:
        return "n=0"
    by = defaultdict(list)
    for r in rows:
        by[r["symbol"]].append(r)
    n = len(rows)
    m30 = statistics.mean(r["net30"] for r in rows if r["net30"] is not None)
    sd30 = statistics.mean(statistics.mean(x["net30"] for x in v) for v in by.values())
    sd15 = statistics.mean(statistics.mean(x["net15"] for x in v) for v in by.values())
    run = sum(r["runner"] for r in rows) / n
    return (f"n={n:4d} names={len(by):3d} runner {run:4.0%} net30 {m30:+.3f}% | per name net15 "
            f"{sd15:+.3f}% net30 {sd30:+.3f}%")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", required=True)
    ap.add_argument("--start", default="09:40")
    ap.add_argument("--end", default="15:20")
    ap.add_argument("--room", type=float, default=2.6)
    a = ap.parse_args()
    bars = pickle.load(open(Path.home() / "replay_cache" / f"bars_v2_{a.day}.pkl", "rb"))
    lb, sb = live_book(a.day), shadow_book(a.day)
    lk, sk = sorted(lb), sorted(sb)
    t0, t1 = rs.at(a.day, a.start), rs.at(a.day, a.end)
    open_t = rs.at(a.day, "09:30")
    rows = []
    for sym, b in bars.items():
        iex = [r for r in b.get("iex") or [] if r[0] >= open_t - 6 * 3600]
        sip = b.get("s") or []
        if len(iex) < 30 or len(sip) < 30:
            continue
        t, o, h, l, c, v = (list(x) for x in zip(*iex))
        fast = mr.percent_r_series(h, l, c, mr.FAST_LEN, mr.FAST_SPAN)
        st_ = [x[0] for x in sip]
        for i in range(1, len(c)):
            if fast[i - 1] is None or fast[i] is None or not (fast[i - 1] <= -50 < fast[i]):
                continue
            tc = t[i] + 60                  # bar completes
            if not (t0 <= tc <= t1):
                continue
            j = bisect.bisect_right(st_, t[i]) - 1
            if j < 0:
                continue
            px = sip[j][4]
            if not (20 <= px <= 100):
                continue
            hod = max(x[2] for x in sip[:j + 1] if x[0] >= open_t)
            k = cost(px)

            def fwd(mins):
                jj = bisect.bisect_right(st_, t[i] + mins * 60) - 1
                return (sip[jj][4] / px - 1) * 100 - k if jj > j else None
            path = [x for x in sip[j + 1:] if x[0] <= t[i] + 30 * 60]
            runner = 0
            for x in path:
                if x[3] / px - 1 <= -0.01:
                    break
                if x[2] / px - 1 >= 0.006:
                    runner = 1
                    break
            m = minute(tc)
            rows.append({"symbol": sym, "ts": tc, "price": px, "room": (px / hod - 1) * 100,
                         "live": sym in at_or_before(lb, lk, m), "shadow": sym in at_or_before(sb, sk, m),
                         "net15": fwd(15), "net30": fwd(30), "runner": runner})
    rows = [r for r in rows if r["net15"] is not None and r["net30"] is not None]
    room = lambda r: r["room"] <= -a.room
    print(f"{a.day} {a.start}-{a.end}: {len(rows)} in-band -50 crosses (IEX %R, SIP outcome) on "
          f"{len({r['symbol'] for r in rows})} names in the replay bar cache")
    for label, f in (("all cached names", lambda r: True),
                     ("(a) live book seated", lambda r: r["live"]),
                     ("(b) shadow book would seat", lambda r: r["shadow"]),
                     ("(c) shadow + room", lambda r: r["shadow"] and room(r)),
                     ("    live + room", lambda r: r["live"] and room(r)),
                     ("    shadow only (not live)", lambda r: r["shadow"] and not r["live"]),
                     ("    live only (not shadow)", lambda r: r["live"] and not r["shadow"]),
                     ("    any name, room", room)):
        print(f"  {label:28s} {st([r for r in rows if f(r)])}")
    for label, f in (("live", lambda r: r["live"]), ("shadow", lambda r: r["shadow"])):
        by = defaultdict(list)
        for r in rows:
            if f(r):
                by[r["symbol"]].append(r["net30"])
        best = sorted(by.items(), key=lambda kv: -statistics.mean(kv[1]))
        print(f"  {label} names by net30: " + " ".join(f"{s}{len(v)}:{statistics.mean(v):+.2f}" for s, v in best))


if __name__ == "__main__":
    main()
