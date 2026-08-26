#!/usr/bin/env python3
"""Does the Finnhub stream carry enough premarket tape to build indicators on?

Finnhub is ruled out as a *quote* source (/stock/bidask is 403, /quote
returns yesterday's close premarket). But its websocket carries trades, and
stream_bars builds 1m bars from them, so it is the candidate for the OTHER
half of the premarket problem: EXH and %R having live input before 09:30.

cm_rsi_stream is present on 100% of today's rows and pctr_stream on 24%,
which is a gap worth understanding before either is trusted. This prints
bar coverage, empty minutes, and how far the stream indicator sits from the
IEX-bar one it shadows.

Read-only.
"""
from __future__ import annotations

import sys
from collections import defaultdict

sys.path.insert(0, ".")
sys.path.insert(0, "tools")

import bars  # noqa: E402
import shadow_report as sr  # noqa: E402

rows = sr.load()
day = sorted({bars.day_of(r["ts"]) for r in rows if r.get("ts")})[-1]
t = [r for r in rows if r.get("ts") and bars.day_of(r["ts"]) == day]
print(f"session {day} — {len(t)} rows "
      f"({bars.et_minutes(t[0]['ts'])//60:02d}:xx to "
      f"{bars.et_minutes(t[-1]['ts'])//60:02d}:xx ET)\n")


def dist(label, xs, unit=""):
    if not xs:
        print(f"  {label:<26} none")
        return
    xs = sorted(xs)
    n = len(xs)
    q = lambda p: xs[int(p * (n - 1))]  # noqa: E731
    print(f"  {label:<26} n={n:<5} p10 {q(.1):>6.1f}  med {q(.5):>6.1f}  "
          f"p90 {q(.9):>6.1f}{unit}")


print("STREAM BAR COVERAGE")
dist("stream_bar_count", [float(r["stream_bar_count"]) for r in t
                          if r.get("stream_bar_count") is not None])
dist("pctr_stream_bars", [float(r["pctr_stream_bars"]) for r in t
                          if r.get("pctr_stream_bars") is not None])
dist("cm_rsi_stream_bars", [float(r["cm_rsi_stream_bars"]) for r in t
                            if r.get("cm_rsi_stream_bars") is not None])
dist("stream_empty_min", [float(r["stream_empty_min"]) for r in t
                          if r.get("stream_empty_min") is not None])
dist("pctr_stream_span_sec", [float(r["pctr_stream_span_sec"]) for r in t
                              if r.get("pctr_stream_span_sec") is not None], "s")

print("\nWHY pctr_stream IS MISSING (rows where pctr_stream is None)")
miss = [r for r in t if r.get("pctr_stream") is None]
have = [r for r in t if r.get("pctr_stream") is not None]
print(f"  missing {len(miss)} / {len(t)}")
for label, grp in (("missing", miss), ("present", have)):
    bc = [float(r["stream_bar_count"]) for r in grp
          if r.get("stream_bar_count") is not None]
    if bc:
        bc.sort()
        print(f"  {label:<8} stream_bar_count  med {bc[len(bc)//2]:>5.0f}  "
              f"min {bc[0]:>4.0f}  max {bc[-1]:>5.0f}")
srcs = defaultdict(int)
for r in miss:
    srcs[str(r.get("pctr_stream_src"))] += 1
print("  pctr_stream_src on missing rows:", dict(srcs))

print("\nAGREEMENT — stream indicator vs the IEX-bar one it shadows")
for a, b in (("pctr", "pctr_stream"), ("cm_rsi", "cm_rsi_stream")):
    pair = [(float(r[a]), float(r[b])) for r in t
            if r.get(a) is not None and r.get(b) is not None]
    if not pair:
        print(f"  {a} vs {b}: no paired rows")
        continue
    d = sorted(abs(x - y) for x, y in pair)
    n = len(d)
    print(f"  {a:<8} vs {b:<16} n={n:<5} "
          f"|diff| med {d[n//2]:>6.2f}  p90 {d[int(.9*(n-1))]:>6.2f}  "
          f"max {d[-1]:>6.2f}")

print("\nPER-SYMBOL premarket stream coverage (n>=50 rows)")
per = defaultdict(lambda: {"n": 0, "rsi": 0, "pctr": 0, "empty": []})
for r in t:
    s = r.get("symbol") or r.get("ticker") or "?"
    d = per[s]
    d["n"] += 1
    d["rsi"] += r.get("cm_rsi_stream") is not None
    d["pctr"] += r.get("pctr_stream") is not None
    if r.get("stream_empty_min") is not None:
        d["empty"].append(float(r["stream_empty_min"]))
print(f"  {'symbol':<8}{'rows':>6}{'rsi_str':>9}{'pctr_str':>10}"
      f"{'empty_min':>11}")
for s, d in sorted(per.items(), key=lambda kv: -kv[1]["n"]):
    if d["n"] < 50:
        continue
    e = sorted(d["empty"])
    em = f"{e[len(e)//2]:.0f}" if e else "-"
    print(f"  {s:<8}{d['n']:>6}{100*d['rsi']/d['n']:>8.0f}%"
          f"{100*d['pctr']/d['n']:>9.0f}%{em:>11}")
