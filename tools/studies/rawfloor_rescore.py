#!/usr/bin/env python3
"""Raw-price re-score (2026-10-10, information update; no prereg is changed).

(1) SIP_BREAKOUT_2026-10-03: drop name-days whose RAW 09:30 open < $10, re-score the pre-registered table/verdict.
(1b) SR_BREAKOUT_HISTORY_2026-10-06: count scored events from name-days with a raw open < $10.
(2) needle C2: raw $20-100 band, every moment equal weight, day-clustered; control = same day-hour mean of
    other name-days' non-cell banded moments (as the prereg). Also the prereg's biased order for reference.

Raw prices: ai_reports/sr_breakout_hist/min_<day>.pkl (RAW SIP, same universe, 5/1-9/10); 9/11 fetched here once.
Run on the mini (after hours; fetches 9/11 raw bars once): nice -n 15 .venv/bin/python tools/studies/rawfloor_rescore.py ai_reports/rawfloor_rescore_2026-10-10
"""
import bisect
import json
import math
import os
import pickle
import statistics
import sys
from collections import defaultdict
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))
sys.path.insert(0, ROOT)
import bars  # noqa: E402
import needle_cells_oos as N  # noqa: E402

OUTD = sys.argv[1]
os.makedirs(OUTD, exist_ok=True)
ALLSYM = os.path.join(ROOT, "ai_reports", "allsym")
SRH = os.path.join(ROOT, "ai_reports", "sr_breakout_hist")
ET = bars.ET
L = []


def P(s=""):
    print(s, flush=True)
    L.append(s)


def raw_rows(day, syms):
    p = os.path.join(SRH, f"min_{day}.pkl")
    if os.path.exists(p):
        return pickle.load(open(p, "rb")), "sr_cache"
    p2 = os.path.join(OUTD, f"raw_{day}.pkl")
    if os.path.exists(p2):
        return pickle.load(open(p2, "rb")), "fetched"
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
    r = bars.client().get_stock_bars(StockBarsRequest(
        symbol_or_symbols=sorted(syms), timeframe=TimeFrame(1, TimeFrameUnit.Minute),
        start=d.replace(hour=9, minute=30), end=d.replace(hour=16), feed=DataFeed.SIP, adjustment=Adjustment.RAW))
    out = {s: [(b.timestamp.timestamp(), float(b.open), float(b.high), float(b.low), float(b.close)) for b in rows]
           for s, rows in (r.data or {}).items()}
    pickle.dump(out, open(p2, "wb"))
    return out, "fetched"


# ---------------------------------------------------------------- raw open + adj->raw factor per name-day
days = N.cached_days()
info = {}          # (sym, day) -> (raw_open, factor)
src = defaultdict(int)
for day in days:
    adj = pickle.load(open(os.path.join(ALLSYM, f"sipbrk_min_{day}.pkl"), "rb"))
    raw, s = raw_rows(day, list(adj))
    src[s] += 1
    t930 = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET, hour=9, minute=30).timestamp()
    for sym, rows in adj.items():
        rr = sorted(raw.get(sym) or [])
        rth = [r for r in rr if r[0] >= t930]
        if not rth:
            info[(sym, day)] = (None, None)
            continue
        bym = {round(r[0]): r[4] for r in rth}
        rat = [bym[round(a[0])] / a[3] for a in rows if round(a[0]) in bym and a[3] > 0]
        info[(sym, day)] = (rth[0][1], statistics.median(rat) if rat else None)
nd_all = len(info)
no_raw = [k for k, v in info.items() if v[0] is None]
below = {k for k, v in info.items() if v[0] is not None and v[0] < 10}
facs = [v[1] for v in info.values() if v[1] is not None]
P(f"# Raw-price re-score, {days[0]}..{days[-1]} ({len(days)} days), raw source days {dict(src)}")
P(f"name-days {nd_all}; no raw RTH bars {len(no_raw)}; raw 09:30 open < $10: {len(below)}")
P(f"adj->raw factor: share within 1% of 1.0 = {sum(abs(f - 1) < 0.01 for f in facs) / len(facs):.3f}; "
  f"factor < 0.5 (reverse split later) {sum(f < 0.5 for f in facs)}; factor > 2 (forward split later) {sum(f > 2 for f in facs)}")
P("below-$10 name-days (sym day raw_open factor): " + ", ".join(
    f"{s} {d} {info[(s, d)][0]:.2f} x{info[(s, d)][1]:.3g}" if info[(s, d)][1] else f"{s} {d} {info[(s, d)][0]:.2f}"
    for s, d in sorted(below, key=lambda k: k[1])))
P()

# ---------------------------------------------------------------- (1) SIP breakout
ev = pickle.load(open(os.path.join(ALLSYM, "sipbrk_events.pkl"), "rb"))
half = days[len(days) // 2]
CAP = 5.0


def dct(xs, k):
    by = defaultdict(list)
    for x in xs:
        if x.get(k) is not None:
            by[x["day"]].append(x[k])
    allv = [v for vs in by.values() for v in vs]
    dm = [statistics.mean(v) for v in by.values()]
    t = (statistics.mean(dm) / (statistics.stdev(dm) / math.sqrt(len(dm)))
         if len(dm) > 2 and statistics.stdev(dm) > 0 else float("nan"))
    return (statistics.mean(allv) if allv else float("nan")), t, len(allv)


def sip_table(evs, title):
    P(f"## {title}")
    P(f"  {'arm':<34}{'half':<8}{'n':>6}{'med spr':>8}{'gross15':>9}{'net15 (t)':>16}{'net30':>8}")
    arms = (("ALL signals", lambda e: e["kind"] == "sig"),
            ("CAPPED signals (spr <= 5 bp)", lambda e: e["kind"] == "sig" and e["spr"] <= CAP),
            ("CAPPED + quiet-tape filter", lambda e: e["kind"] == "sig" and e["spr"] <= CAP and not e["quiet"]),
            ("CAPPED random control", lambda e: e["kind"] == "ctl" and e["spr"] <= CAP))
    ok, cap, ctl = True, {}, {}
    for lab, f in arms:
        for hn, hf in (("first", lambda e: e["day"] < half), ("second", lambda e: e["day"] >= half)):
            xs = [e for e in evs if f(e) and hf(e)]
            if not xs:
                continue
            g = dct(xs, "g15")
            m, t, nn = dct(xs, "n15")
            P(f"  {lab:<34}{hn:<8}{nn:>6}{statistics.median(e['spr'] for e in xs):>8.1f}{g[0]:>+9.1f}"
              f"{m:>+9.1f} ({t:+.2f}){dct(xs, 'n30')[0]:>+8.1f}")
            if lab.startswith("CAPPED signals"):
                cap[hn] = m
                ok &= m >= 5 and t >= 2 and nn >= 100
            if lab.startswith("CAPPED random"):
                ctl[hn] = m
    for h in ("first", "second"):
        ok &= h in cap and h in ctl and cap[h] > ctl[h]
    P(f"  VERDICT: {'PASS' if ok else 'FAIL'}")
    P()


sip_table(ev, "SIP breakout, as published (all events)")
bad = [e for e in ev if (e["sym"], e["day"]) in below]
nobad = [e for e in ev if (e["sym"], e["day"]) not in below and (e["sym"], e["day"]) not in set(no_raw)]
for kind in ("sig", "ctl"):
    xs = [e for e in bad if e["kind"] == kind]
    P(f"SIP {kind} events from raw<$10 name-days: {len(xs)} of {sum(e['kind'] == kind for e in ev)} "
      f"(capped {sum(e['spr'] <= CAP for e in xs)}, name-days {len({(e['sym'], e['day']) for e in xs})})")
P(f"SIP events from name-days with no raw bars (also excluded): {sum((e['sym'], e['day']) in set(no_raw) for e in ev)}")
P()
sip_table(nobad, "SIP breakout, raw open >= $10 only")

# ---------------------------------------------------------------- (1b) SR breakout history
sr = json.load(open(os.path.join(SRH, "events.json")))
srb = [e for e in sr["events"] if (e["sym"], e["day"]) in below]
P(f"## SR breakout history: events {len(sr['events'])}, from raw<$10 name-days {len(srb)} "
  f"(the script already dropped {sr['drops'].get('raw_open_below_10_or_no_rth')} raw<$10/no-RTH name-days)")
srnd = {(e['sym'], e['day']) for e in sr['events']}
P(f"SR scored name-days {len(srnd)}; of those below $10 by this check: {len(srnd & below)}")
P()

# ---------------------------------------------------------------- (2) needle C2
days_, moments, drops, ff, spy = N.build_moments()
for x in moments:
    x["raw_ok"] = info.get(x["nd"], (None, None))[1] is not None
P(f"## Needle C2: moments {len(moments)}")


def clustered(vals_by_day):
    allv = [v for vs in vals_by_day.values() for v in vs]
    n, G = len(allv), len(vals_by_day)
    if n < 2 or G < 3:
        return float("nan"), float("nan"), n, G
    m = statistics.fmean(allv)
    ss = sum(sum(v - m for v in vs) ** 2 for vs in vals_by_day.values())
    se = math.sqrt(ss * G / (G - 1)) / n
    return m, (m / se if se > 0 else float("nan")), n, G


def equal_weight(ms, cell="c2", cost=N.COST_BP, drop_nd=(), days_subset=None, first_only=False):
    """moment minus its day-hour control (other name-days' non-cell moments, moment-weighted as the prereg); every
    cell moment weight 1; t clustered by day."""
    cs, cn = defaultdict(lambda: defaultdict(float)), defaultdict(lambda: defaultdict(int))
    for x in ms:
        if not x[cell]:
            k = (x["day"], x["hour"])
            cs[k][x["nd"]] += x["gross"] - cost
            cn[k][x["nd"]] += 1
    tot = {k: (sum(v.values()), sum(cn[k].values())) for k, v in cs.items()}
    diff, net, seen, ndd = defaultdict(list), defaultdict(list), set(), defaultdict(list)
    for x in sorted(ms, key=lambda x: x["te"]):
        if not x[cell] or x["nd"] in drop_nd or (days_subset is not None and x["day"] not in days_subset):
            continue
        if first_only:
            if x["nd"] in seen:
                continue
            seen.add(x["nd"])
        k = (x["day"], x["hour"])
        s, n = tot.get(k, (0.0, 0))
        s -= cs[k].get(x["nd"], 0.0)
        n -= cn[k].get(x["nd"], 0)
        nt = x["gross"] - cost
        net[x["day"]].append(nt)
        if n > 0:
            diff[x["day"]].append(nt - s / n)
            ndd[x["nd"]].append(nt - s / n)
    return diff, net, ndd


def report(ms, label):
    halves = {"A": set(days_[0::2]), "B": set(days_[1::2])}
    P(f"### {label}")
    for h in ("A", "B"):
        d, nt, ndd = equal_weight(ms, days_subset=halves[h])
        md, td, n, G = clustered(d)
        mn, tn, _, _ = clustered(nt)
        P(f"- half {h}: moments {n}, name-days {len(ndd)}, days {G} | net30 {mn:+.1f} (t {tn:+.2f}) | "
          f"minus control {md:+.1f} (t {td:+.2f})")
    d, nt, ndd = equal_weight(ms)
    md, td, n, G = clustered(d)
    mn, tn, _, _ = clustered(nt)
    top3 = sorted(ndd, key=lambda k: statistics.fmean(ndd[k]), reverse=True)[:3]
    d3, _, _ = equal_weight(ms, drop_nd=set(top3))
    md3, td3, _, _ = clustered(d3)
    P(f"- pooled: moments {n}, name-days {len(ndd)}, days {G} | net30 {mn:+.1f} (t {tn:+.2f}) | minus control "
      f"{md:+.1f} (t {td:+.2f}); without top 3 name-days {', '.join(f'{s} {dd}' for s, dd in top3)}: {md3:+.1f} (t {td3:+.2f})")
    d1, n1, _ = equal_weight(ms, first_only=True)
    m1, t1, nn1, G1 = clustered(d1)
    P(f"- first C2 moment per name-day: n {nn1}, days {G1} | minus control {m1:+.1f} (t {t1:+.2f}) | "
      f"net30 {clustered(n1)[0]:+.1f}")
    d4, _, _ = equal_weight(ms, cost=N.COST_INFO_BP)
    _, nt4, _ = equal_weight(ms, cost=N.COST_INFO_BP)
    P(f"- at 4 bp: net30 {clustered(nt4)[0]:+.1f}, minus control {clustered(d4)[0]:+.1f}")
    ok, lines = N.verdict(ms, halves)
    P(f"- prereg (name-day-first) order on this band, for reference: {'PASS' if ok else 'FAIL'}")
    for s in lines:
        P(f"    - {s}")
    P()


# build_moments drops the close; recover it from the cache: the decision bar is the last bar before entry te
cache = {}
for day in days_:
    for sym, rows in pickle.load(open(os.path.join(ALLSYM, f"sipbrk_min_{day}.pkl"), "rb")).items():
        cache[(sym, day)] = ([r[0] for r in rows], [r[3] for r in rows])
for x in moments:
    ts, cl = cache[x["nd"]]
    k = bisect.bisect_left(ts, x["te"]) - 1
    x["px_adj"] = cl[k] if k >= 0 else None
    f = info.get(x["nd"], (None, None))[1]
    x["band_raw"] = x["px_adj"] is not None and f is not None and N.BAND[0] <= x["px_adj"] * f <= N.BAND[1]
chk = sum(1 for x in moments if x["px_adj"] is not None and (N.BAND[0] <= x["px_adj"] <= N.BAND[1]) != x["band"])
P(f"band reconstruction check: adjusted-band mismatches {chk} of {len(moments)}; px missing "
  f"{sum(x['px_adj'] is None for x in moments)}; no raw factor {sum(info.get(x['nd'], (0, None))[1] is None for x in moments)}")
adj_band = [x for x in moments if x["band"]]
raw_band = [x for x in moments if x["band_raw"]]
c2a = {x["nd"] for x in adj_band if x["c2"]}
c2r = {x["nd"] for x in raw_band if x["c2"]}
P(f"C2 name-days: adjusted band {len(c2a)}, raw band {len(c2r)}, leave {len(c2a - c2r)} ({', '.join(f'{s} {d}' for s, d in sorted(c2a - c2r))}), "
  f"enter {len(c2r - c2a)} ({', '.join(f'{s} {d}' for s, d in sorted(c2r - c2a))})")
P()
report(adj_band, "C2, ADJUSTED $20-100 band (as published, reproduces -24.9 in the prereg order)")
report(raw_band, "C2, RAW $20-100 band")
report([x for x in moments if x["raw_ok"] and info[x["nd"]][0] is not None and info[x["nd"]][0] >= 10],
       "C2, raw open >= $10, no band (information)")
open(os.path.join(OUTD, "report.md"), "w").write("\n".join(L) + "\n")
