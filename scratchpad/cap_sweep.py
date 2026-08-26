"""How many setups survive at each share-count cap?

The operator's rule is FLOAT under 10M. Finnhub publishes shares
OUTSTANDING, which is always >= float -- insiders and restricted stock sit
in the gap, and on microcaps that gap is routinely half the company. So a
10M *outstanding* cap is materially stricter than a 10M *float* cap and
will reject names that actually qualify.

Stated before looking: the cap is a proxy for an unobserved quantity, so
the sweep is a sensitivity check on a measurement limitation, not a search
for the threshold that gives the best answer. n at each cap is what
decides whether anything here is testable at all.
"""
import json, sys, collections, time
sys.path.insert(0, ".")
import float_feed, setup_rules

NEWS = {}
try:
    NEWS = json.load(open("ai_reports/news_cache.json", encoding="utf-8"))
except Exception:
    pass

rows = []
for line in open("ai_reports/shadow.jsonl", encoding="utf-8", errors="replace"):
    line = line.strip()
    if not line:
        continue
    try:
        r = json.loads(line)
    except ValueError:
        continue
    ts, sym = r.get("ts"), r.get("symbol")
    if not ts or not sym:
        continue
    rows.append((float(ts), str(sym).upper(), r.get("pct_change"),
                 r.get("rvol"), r.get("price")))

def n24(sym, ts):
    return sum(1 for n in (NEWS.get(sym) or [])
               if n.get("ts") is not None and ts - 86400 <= n["ts"] < ts)

print(f"{'cap':>6}{'name-days':>11}{'sessions':>10}   symbols")
print("-" * 60)
for cap in (10, 15, 20, 30, 50, 75, 1e9):
    seen = {}
    for ts, sym, pct, rv, px in rows:
        day = time.strftime("%Y-%m-%d", time.gmtime(ts))
        if (day, sym) in seen:
            continue
        legs = setup_rules.evaluate(
            pct_change=pct, rvol=rv, price=px,
            shares_out_m=float_feed.shares_out(sym), news_n_24h=n24(sym, ts))
        # re-test the float leg at this cap
        so = float_feed.shares_out(sym)
        ok = (legs["up"] and legs["rvol"] and legs["price"] and legs["news"]
              and so is not None and so < cap)
        if ok:
            seen[(day, sym)] = so
    days = len({d for d, s in seen})
    label = "none" if cap > 1e8 else f"{cap:g}M"
    names = ", ".join(sorted({s for d, s in seen}))[:70]
    print(f"{label:>6}{len(seen):>11}{days:>10}   {names}")
