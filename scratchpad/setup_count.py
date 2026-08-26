"""How often does the operator's actual setup occur in our logged tape?

Every gate this lab tested was MARGINAL -- one condition at a time against
the whole watchlist. The operator's thesis is a CONJUNCTION of five, and a
conjunction that rare would be invisible to every test run so far: pooled
against 314 names it would be a rounding error, and the marginal tests
would read exactly as they did.

Counts name-days meeting each condition alone and all together, so we know
whether this is testable at all before building anything.
"""
import json, collections, sys, time
sys.path.insert(0, ".")

NEWS = {}
try:
    NEWS = json.load(open("ai_reports/news_cache.json", encoding="utf-8"))
except Exception:
    pass

def day_of(ts):
    return time.strftime("%Y-%m-%d", time.gmtime(ts))

best = {}   # (day, sym) -> best row seen
for line in open("ai_reports/shadow.jsonl", encoding="utf-8", errors="replace"):
    line = line.strip()
    if not line:
        continue
    try:
        r = json.loads(line)
    except ValueError:
        continue
    ts = r.get("ts")
    sym = r.get("symbol")
    if not ts or not sym:
        continue
    k = (day_of(float(ts)), str(sym).upper())
    px = r.get("price")
    pct = r.get("pct_change")
    rv = r.get("rvol")
    cur = best.get(k)
    # keep the row with the highest pct_change for this name-day
    if cur is None or (pct is not None and (cur.get("pct") is None or pct > cur["pct"])):
        best[k] = {"px": px, "pct": pct, "rv": rv, "ts": float(ts)}

def has_news(sym, ts):
    items = NEWS.get(sym) or []
    return any(ts - 24*3600 <= n["ts"] < ts for n in items)

C = collections.Counter()
both = []
for (day, sym), v in best.items():
    px, pct, rv, ts = v["px"], v["pct"], v["rv"], v["ts"]
    c_pct = pct is not None and pct >= 10.0
    c_rv  = rv is not None and 5.0 <= rv <= 100.0
    c_px  = px is not None and 2.0 <= px <= 20.0
    c_news = has_news(sym, ts)
    C["total"] += 1
    C["up10"] += c_pct
    C["rvol5"] += c_rv
    C["px2_20"] += c_px
    C["news24h"] += c_news
    if c_pct and c_rv:
        C["up10+rvol5"] += 1
    if c_pct and c_rv and c_px:
        C["up10+rvol5+px"] += 1
    if c_pct and c_rv and c_px and c_news:
        C["ALL FOUR"] += 1
        both.append((day, sym, pct, rv, px))

print(f"name-days in shadow: {C['total']}  (news cache: {len(NEWS)} symbols)\n")
for k in ("up10", "rvol5", "px2_20", "news24h",
          "up10+rvol5", "up10+rvol5+px", "ALL FOUR"):
    n = C[k]
    print(f"  {k:<18} {n:>5}  ({100*n/max(C['total'],1):>5.1f}%)")

days = collections.Counter(d for d, s, *_ in both)
print(f"\nALL FOUR spans {len(days)} sessions:")
for d in sorted(days):
    print(f"  {d}  {days[d]}")
print("\nexamples:")
for row in sorted(both, key=lambda r: -r[2])[:12]:
    print(f"  {row[0]}  {row[1]:<6} pct={row[2]:>7.1f}  rvol={row[3]:>6.2f}  px=${row[4]:.2f}")
