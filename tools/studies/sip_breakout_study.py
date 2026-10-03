#!/usr/bin/env python3
"""sip_breakout_study.py — pre-registered in docs/studies/sip_breakout_prereg.json (175bc9b, amended c0a8538).

Stage 1 of the paid-data decision. Universe per day: common stock, open >= $10, prior-day dollar volume >= $20M,
gap >= +2%, the 40 largest gaps (all-symbol daily panel). SIP 1-minute bars 2026-05-01..2026-09-12.
Signal: volume(t) > 3x mean of the 20 prior minutes AND close(t) > max high of the 15 prior minutes, 09:45-15:30,
one per name per 15 min. Entry at the next bar's open; exit at the close 15 (and 30) minutes later, cap 15:55.
Cost = full SIP spread at entry. CAPPED = spread <= 5 bp. Control = 1 random minute per signal, same name-day.
Quiet tape (secondary): drop signals whose last 5 closed minutes' volume < 0.25x the session's per-minute pace.

PASS: CAPPED net15 >= +5 bp, day-clustered t >= 2 in BOTH halves, n >= 100 per half, and above the capped control.

USAGE (mini): .venv/bin/python tools/studies/sip_breakout_study.py ai_reports/allsym/daily_2025-09-01_2026-10-02.pkl
"""
from __future__ import annotations

import json
import math
import os
import pickle
import random
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

OUT = os.path.join(ROOT, "ai_reports", "allsym")
LO, HI = "2026-05-01", "2026-09-12"
NAMES_PER_DAY, CAP_BP = 40, 5.0
QCACHE = os.path.join(OUT, "sipbrk_quotes.json")


def universe(daily):
    by = defaultdict(list)
    for sym, rows in daily.items():
        for (d0, o0, h0, l0, c0, v0), (d, o, h, l, c, v) in zip(rows, rows[1:]):
            if LO <= d <= HI and o >= 10 and c0 * v0 >= 20e6 and c0 > 0 and o / c0 - 1 >= 0.02:
                by[d].append((o / c0 - 1, sym))
    return {d: [s for _, s in sorted(v, reverse=True)[:NAMES_PER_DAY]] for d, v in by.items()}


def minute_bars(day, syms, cl):
    p = os.path.join(OUT, f"sipbrk_min_{day}.pkl")
    if os.path.exists(p):
        return pickle.load(open(p, "rb"))
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=bars.ET)
    out = {}
    for attempt in range(4):
        try:
            r = cl.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=syms, timeframe=TimeFrame(1, TimeFrameUnit.Minute),
                start=d.replace(hour=9, minute=30), end=d.replace(hour=16, minute=0),
                feed=DataFeed.SIP, adjustment=Adjustment.SPLIT))
            for s, rows in (r.data or {}).items():
                out[s] = [(b.timestamp.timestamp(), float(b.open), float(b.high), float(b.close), float(b.volume))
                          for b in rows]
            break
        except Exception as e:  # noqa: BLE001
            print(f"  {day} try {attempt}: {str(e)[:70]}", flush=True)
            time.sleep(5 * (attempt + 1))
    pickle.dump(out, open(p, "wb"))
    time.sleep(0.5)
    return out


def main():
    daily = pickle.load(open(sys.argv[1], "rb"))
    uni = universe(daily)
    days = sorted(uni)
    cl = bars.client()
    rng = random.Random(7)
    qc = json.load(open(QCACHE)) if os.path.exists(QCACHE) else {}
    ev = []
    print(f"days {len(days)} ({days[0]}..{days[-1]}), name-days {sum(len(v) for v in uni.values())}", flush=True)
    for n, day in enumerate(days):
        mb = minute_bars(day, uni[day], cl)
        dd = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=bars.ET)
        t945, t1530 = dd.replace(hour=9, minute=45).timestamp(), dd.replace(hour=15, minute=30).timestamp()
        t1555, t930 = dd.replace(hour=15, minute=55).timestamp(), dd.replace(hour=9, minute=30).timestamp()
        for sym, rows in mb.items():
            if len(rows) < 60:
                continue
            ts = [r[0] for r in rows]
            last_sig = -1e18

            def outcome(i_entry):
                """(entry_ts, entry_px, ret15, ret30) for entering at the open of bar i_entry."""
                te, pe = rows[i_entry][0], rows[i_entry][1]
                res = []
                for H in (15, 30):
                    tgt = min(te + H * 60, t1555)
                    k = i_entry
                    while k + 1 < len(rows) and rows[k + 1][0] <= tgt:
                        k += 1
                    res.append((rows[k][3] / pe - 1) * 1e4 if pe > 0 else None)
                return te, pe, res[0], res[1]

            def quiet(i):
                el = (ts[i] - t930) / 60.0 + 1
                pace = sum(r[4] for r in rows[:i + 1]) / max(1.0, el)
                last5 = sum(r[4] for r in rows[max(0, i - 4):i + 1]) / 5.0
                return pace > 0 and last5 < 0.25 * pace

            sig_idx = []
            for i in range(20, len(rows) - 1):
                if not (t945 <= ts[i] <= t1530) or ts[i] - last_sig < 15 * 60:
                    continue
                if ts[i] - ts[i - 20] > 25 * 60:            # need a real 20-minute history
                    continue
                vmean = statistics.mean(r[4] for r in rows[i - 20:i])
                hi15 = max(r[2] for r in rows[i - 15:i])
                if vmean > 0 and rows[i][4] > 3 * vmean and rows[i][3] > hi15:
                    sig_idx.append(i)
                    last_sig = ts[i]
            cand = [i for i in range(len(rows) - 1) if t945 <= ts[i] <= t1530]
            for i in sig_idx:
                for kind, ii in (("sig", i), ("ctl", rng.choice(cand) if cand else None)):
                    if ii is None:
                        continue
                    te, pe, r15, r30 = outcome(ii + 1)
                    key = f"{sym}|{te:.0f}"
                    if key not in qc:
                        q = er.nbbo_at(cl, sym, datetime.fromtimestamp(te, timezone.utc))
                        qc[key] = list(q) if q else None
                        time.sleep(0.3)   # the live engine shares these data keys
                    q = qc[key]
                    if not q or q[1] <= q[0] or q[0] <= 0 or r15 is None:
                        continue
                    spr = (q[1] - q[0]) / ((q[0] + q[1]) / 2) * 1e4
                    ev.append({"day": day, "sym": sym, "kind": kind, "spr": spr, "g15": r15, "g30": r30,
                               "n15": r15 - spr, "n30": (r30 - spr) if r30 is not None else None,
                               "quiet": quiet(ii)})
        if n % 5 == 0:
            json.dump(qc, open(QCACHE, "w"))
            print(f"  {day}: {sum(e['kind'] == 'sig' for e in ev)} signals so far", flush=True)
    json.dump(qc, open(QCACHE, "w"))
    pickle.dump(ev, open(os.path.join(OUT, "sipbrk_events.pkl"), "wb"))

    half = days[len(days) // 2]

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

    print(f"\nSIP BREAKOUT {days[0]}..{days[-1]}, halves split at {half}\n")
    print(f"  {'arm':<34}{'half':<8}{'n':>6}{'med spr':>8}{'gross15':>9}{'net15 (t)':>16}{'net30':>8}")
    verdict = True
    arms = (("ALL signals", lambda e: e["kind"] == "sig"),
            ("CAPPED signals (spr <= 5 bp)", lambda e: e["kind"] == "sig" and e["spr"] <= CAP_BP),
            ("CAPPED + quiet-tape filter", lambda e: e["kind"] == "sig" and e["spr"] <= CAP_BP and not e["quiet"]),
            ("CAPPED random control", lambda e: e["kind"] == "ctl" and e["spr"] <= CAP_BP))
    cap_net, ctl_net = {}, {}
    for lab, f in arms:
        for hname, hf in (("first", lambda e: e["day"] < half), ("second", lambda e: e["day"] >= half)):
            xs = [e for e in ev if f(e) and hf(e)]
            if not xs:
                continue
            g = dct(xs, "g15")
            m, t, nn = dct(xs, "n15")
            m30 = dct(xs, "n30")[0]
            print(f"  {lab:<34}{hname:<8}{nn:>6}{statistics.median(e['spr'] for e in xs):>8.1f}{g[0]:>+9.1f}"
                  f"{m:>+9.1f} ({t:+.2f}){m30:>+8.1f}")
            if lab.startswith("CAPPED signals"):
                cap_net[hname] = m
                verdict &= (m >= 5 and t >= 2 and nn >= 100)
            if lab.startswith("CAPPED random"):
                ctl_net[hname] = m
    for h in ("first", "second"):
        verdict &= h in cap_net and h in ctl_net and cap_net[h] > ctl_net[h]
    print(f"\nPRE-REGISTERED VERDICT (CAPPED net15 >= +5 bp, t >= 2 both halves, above control): "
          f"{'PASS' if verdict else 'FAIL'}")


if __name__ == "__main__":
    main()
