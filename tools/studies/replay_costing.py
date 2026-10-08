#!/usr/bin/env python3
"""replay_costing.py — charge replay_session.py trades the real SIP spread, so config variants compare net.

replay_session.py fills at the recorded price with no spread, so a variant that trades wider names looks no
worse than one that trades tight names. For each closed replay trade this looks up the SIP NBBO at entry
(exec_report.nbbo_at, cached) and charges the full quoted spread once per round trip (half in, half out),
as the strategy-edge studies do.

USAGE (mini):  .venv/bin/python tools/studies/replay_costing.py /tmp/rp/*.json
  files are named DAY-VARIANT.json (as written by --out); one table row per variant, pooled over days.
"""
from __future__ import annotations

import json
import math
import os
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

ROOT = os.environ.get("REPO") or os.getcwd()
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)
import bars  # noqa: E402
import exec_report as er  # noqa: E402

CACHE = os.path.join(ROOT, "ai_reports", "replay_costing_cache.json")


def load_cache() -> dict:
    return json.load(open(CACHE)) if os.path.exists(CACHE) else {}


def save_cache(cache: dict) -> None:
    json.dump(cache, open(CACHE, "w"))


def entry_spread_bp(cache: dict, symbol: str, entry_ts: float, client_box: dict | None = None) -> float | None:
    """Full SIP quoted spread (bp of mid) at the entry, from the cache or exec_report.nbbo_at; None = no NBBO.
    Fetches (and caches) on a miss; client_box holds the data client across calls."""
    key = f"{symbol}|{float(entry_ts):.0f}"
    if key not in cache:
        box = client_box if client_box is not None else {}
        box["cl"] = box.get("cl") or bars.client()
        q = er.nbbo_at(box["cl"], symbol, datetime.fromtimestamp(float(entry_ts), timezone.utc))
        cache[key] = list(q) if q else None
        time.sleep(0.3)   # the live engine shares these data keys
    q = cache[key]
    if not q or q[1] <= q[0] or q[0] <= 0:
        return None
    return (q[1] - q[0]) / ((q[0] + q[1]) / 2) * 1e4


def main():
    cache = load_cache()
    box: dict = {}
    by_var = defaultdict(list)
    for path in sys.argv[1:]:
        name = os.path.basename(path)[:-5]
        day, var = name[:10], name[11:]
        d = json.load(open(path))
        for t in d.get("closed") or []:
            if t.get("ret") is None or not t.get("entry_px"):
                continue
            spr = entry_spread_bp(cache, t["symbol"], t["entry_ts"], box)
            if spr is None:
                continue
            by_var[var].append({"day": day, "gross": t["ret"] * 1e4, "spr": spr,
                                "net": t["ret"] * 1e4 - spr, "px": float(t["entry_px"])})
        save_cache(cache)
    days = sorted({r["day"] for v in by_var.values() for r in v})
    print(f"REPLAY COSTING {days[0]}..{days[-1]} ({len(days)} days): bp per trade, full SIP spread charged once\n")
    print(f"  {'variant':<8}{'trades':>7}{'/day':>6}{'gross':>8}{'spread':>8}{'net':>8}{'t(days)':>9}{'net $/day @1k':>15}")
    for var in sorted(by_var):
        xs = by_var[var]
        dm = [statistics.mean([r["net"] for r in xs if r["day"] == d]) for d in days
              if any(r["day"] == d for r in xs)]
        t = (statistics.mean(dm) / (statistics.stdev(dm) / math.sqrt(len(dm)))
             if len(dm) > 2 and statistics.stdev(dm) > 0 else float("nan"))
        print(f"  {var:<8}{len(xs):>7}{len(xs) / len(days):>6.0f}{statistics.mean(r['gross'] for r in xs):>+8.1f}"
              f"{statistics.mean(r['spr'] for r in xs):>8.1f}{statistics.mean(r['net'] for r in xs):>+8.1f}{t:>+9.2f}"
              f"{sum(r['net'] for r in xs) / 1e4 * 1000 / len(days):>+15.2f}")


if __name__ == "__main__":
    main()
