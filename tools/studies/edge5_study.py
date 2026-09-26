#!/usr/bin/env python3
"""edge5_study.py — Round-5 addendum T5 (news catalyst), T6 (breadth regime), T7 (seed-source audit).
Pre-registered in docs/studies/edge5_prereg.json; data from edge5_fetch.py (+ round-4 book trades).

USAGE  edge5_study.py T5 | T6 | T7
"""
from __future__ import annotations

import json
import os
import pickle
import sys
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np

ROOT = os.environ.get("REPO") or os.getcwd()
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))
import edge_common as ec  # noqa: E402

ET = ZoneInfo("America/New_York")
E5 = os.path.join(ROOT, "ai_reports", "edge5")
HS = (1, 5, 10)


def load_daily():
    D = pickle.load(open(os.path.join(E5, "daily.pkl"), "rb"))
    cal = [r[0] for r in D["SPY"]]
    px = {s: {r[0]: (r[1], r[2], r[3]) for r in rows} for s, rows in D.items()}
    return cal, px


def st_line(label, vals, days, ndays=None):
    if len(vals) < 5:
        return f"  {label:34} n {len(vals)}"
    s = ec.stats(np.array(vals), np.array(days), boot=500, n_days_total=ndays)
    return (f"  {label:34} n {s['n']:5d} days {s['days']:4d}  mean {s['net']:+7.1f}  med {s['median']:+7.1f}  "
            f"win {s['win']:4.0%}  t {s['t']:+5.2f}  CI [{s['lo']:+6.1f},{s['hi']:+6.1f}]"), s


def t5():
    import edge2_fetch as e2
    cal, px = load_daily()
    ci = {d: i for i, d in enumerate(cal)}
    split = json.load(open(os.path.join(ROOT, "ai_reports", "edge2_split.json")))
    part = {d: p for p in ("tune", "validate", "holdout") for d in split[p]}
    news = pickle.load(open(os.path.join(E5, "news.pkl"), "rb"))["items"]
    U = sorted(e2.U2)
    # news counts per (sym, trading day) using window (16:00 d-1, 15:30 d]
    cut = {}
    for i, d in enumerate(cal):
        dt = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=ET)
        cut[d] = dt.replace(hour=15, minute=30).timestamp()
    ends = np.array([cut[d] for d in cal])
    cnt = defaultdict(int)
    for _id, (ts, syms, _src) in news.items():
        j = int(np.searchsorted(ends, ts))            # first day whose 15:30 cut >= ts
        if j >= len(cal):
            continue
        d = cal[j]
        if j > 0:
            prev_close = datetime.strptime(cal[j - 1], "%Y-%m-%d").replace(hour=16, tzinfo=ET).timestamp()
            if not (prev_close < ts <= ends[j]):
                # published between 15:30 and 16:00 of day j-1 -> belongs to no window (not known before the close decision)
                if ts <= prev_close:
                    continue
        for s in syms:
            if s in px:
                cnt[(s, d)] += 1
    sm = ec.SpreadModel(set(U))
    bench = {}
    for d in part:
        i = ci[d]
        for h in HS:
            if i + h >= len(cal):
                continue
            rs = [px[s][cal[i + h]][1] / px[s][d][1] - 1 for s in U if d in px[s] and cal[i + h] in px[s]]
            bench[(d, h)] = float(np.mean(rs)) if rs else None
    rows = defaultdict(list)   # (event, side, h, part) -> [(day_idx, net_excess_bp)]
    counts = defaultdict(int)
    di = {d: k for k, d in enumerate(sorted(part))}
    for s in U:
        P = px[s]
        for d in part:
            i = ci[d]
            if i < 21 or d not in P or cal[i - 1] not in P:
                continue
            hist = [cal[k] for k in range(i - 21, i)]
            if not all(x in P for x in hist):
                continue
            c = [P[x][1] for x in hist] + [P[d][1]]
            r = np.diff(c) / np.array(c[:-1])
            rd, sig = r[-1], np.std(r[:-1], ddof=1)
            vol20 = np.mean([P[x][2] for x in hist[1:]])
            big = abs(rd) >= 2 * sig and P[d][2] >= 1.5 * vol20
            n_today = cnt.get((s, d), 0)
            mean_cnt = np.mean([cnt.get((s, x), 0) for x in hist[1:]])
            ev = []
            if big and n_today > 0:
                ev.append("E1 news+move+vol")
            if big and n_today == 0:
                ev.append("E0 move+vol, NO news")
            if n_today >= max(3, 3 * mean_cnt):
                ev.append("E2 news-count spike")
            if not ev:
                continue
            side = "long" if rd > 0 else "short"
            dr = 1 if rd > 0 else -1
            for h in HS:
                if (d, h) not in bench or bench[(d, h)] is None or cal[i + h] not in P:
                    continue
                fr = P[cal[i + h]][1] / P[d][1] - 1
                x = dr * (fr - bench[(d, h)]) * 1e4 - sm.get(s, d, 385)
                for e in ev:
                    rows[(e, side, h, part[d])].append((di[d], x))
                    counts[(e, part[d])] += 1 if h == 1 else 0
    print("T5 news catalyst — L universe (U2), follow the day's direction, enter at close d, net excess vs U2 equal-weight (bp)")
    print(f"  news items fetched: {len(news)}; symbol-day windows with news: {len(cnt)}")
    promos = []
    for e in ("E1 news+move+vol", "E0 move+vol, NO news", "E2 news-count spike"):
        for side in ("long", "short"):
            for h in HS:
                out = []
                ok = True
                ss = {}
                for p in ("tune", "validate"):
                    v = rows.get((e, side, h, p), [])
                    L = st_line(f"{e} {side} {h}d {p}", [x[1] for x in v], [x[0] for x in v])
                    if isinstance(L, tuple):
                        out.append(L[0]); ss[p] = L[1]
                    else:
                        out.append(L); ok = False
                v = rows.get((e, side, h, "tune"), []) + rows.get((e, side, h, "validate"), [])
                L = st_line(f"{e} {side} {h}d tune+validate", [x[1] for x in v], [x[0] for x in v])
                if isinstance(L, tuple):
                    out.append(L[0])
                    if ok and side == "long" and e != "E0 move+vol, NO news" and ss["tune"]["net"] > 0 and ss["validate"]["net"] > 0 and L[1]["t"] >= 2:
                        promos.append((e, side, h))
                else:
                    out.append(L)
                print("\n".join(out))
    print(f"Promoted: {promos}")
    for e, side, h in promos:
        v = rows.get((e, side, h, "holdout"), [])
        L = st_line(f"HOLDOUT {e} {side} {h}d", [x[1] for x in v], [x[0] for x in v])
        print(L[0] if isinstance(L, tuple) else L)


def breadth(cal, px, U):
    out = {}
    closes = {s: np.array([px[s].get(d, (np.nan, np.nan, np.nan))[1] for d in cal]) for s in U}
    spy = closes["SPY"]
    rets = np.diff(spy) / spy[:-1]
    rv = np.full(len(cal), np.nan)
    for i in range(21, len(cal)):
        rv[i] = np.std(rets[i - 20:i], ddof=1) * np.sqrt(252)
    for i, d in enumerate(cal):
        if i < 50:
            continue
        a50 = [closes[s][i] > np.nanmean(closes[s][i - 49:i + 1]) for s in U if np.isfinite(closes[s][i - 49:i + 1]).sum() >= 45]
        a20 = [closes[s][i] > np.nanmean(closes[s][i - 19:i + 1]) for s in U if np.isfinite(closes[s][i - 19:i + 1]).sum() >= 18]
        adv = sum(1 for s in U if closes[s][i] > closes[s][i - 1]); dec = sum(1 for s in U if closes[s][i] < closes[s][i - 1])
        med = np.nanmedian(rv[max(0, i - 250):i]) if i > 60 else np.nan
        out[d] = {"pct50": float(np.mean(a50)), "pct20": float(np.mean(a20)), "ad": adv / max(1, dec), "rv20": float(rv[i]), "rv_med250": float(med)}
    return out


def t6():
    import edge2_fetch as e2
    cal, px = load_daily()
    U = sorted(e2.U2)
    B = breadth(cal, px, U)
    split = json.load(open(os.path.join(ROOT, "ai_reports", "edge2_split.json")))
    part = {d: p for p in ("tune", "validate", "holdout") for d in split[p]}
    ci = {d: i for i, d in enumerate(cal)}
    on = {}
    for d in part:
        prev = cal[ci[d] - 1]
        b = B.get(prev)
        on[d] = bool(b and b["pct50"] >= 0.5 and b["rv20"] <= b["rv_med250"])
    print("T6 regime rule (prior close): % U2 above 50-day SMA >= 50% AND SPY 20d realized vol <= its trailing 250-day median")
    for p in ("tune", "validate", "holdout"):
        dd = [d for d in part if part[d] == p]
        print(f"  {p}: filter ON {sum(on[d] for d in dd)}/{len(dd)} days")
    T = json.load(open(os.path.join(ROOT, "ai_reports", "edge4", "book_P_L_trades.json")))
    for spec, tr in T.items():
        print(f"\n  book {spec} (L, max 5 concurrent, $1,000/position):")
        for p in ("tune", "validate", "holdout"):
            dd = sorted(d for d in part if part[d] == p)
            dix = {d: k for k, d in enumerate(dd)}
            for lab, sel in (("all days", lambda d: True), ("filter ON", lambda d: on[d]), ("filter OFF", lambda d: not on[d])):
                v = [(dix[d], n) for (d, g, n) in tr if part.get(d) == p and sel(d)]
                nd = sum(1 for d in dd if sel(d))
                L = st_line(f"{p:8} {lab}", [x[1] for x in v], [x[0] for x in v])
                usd = sum(x[1] for x in v) * 1000 / 1e4 / max(1, nd)
                print((L[0] if isinstance(L, tuple) else L) + f"  $/day {usd:+7.2f} over {nd} days")
    json.dump({d: {**B[cal[ci[d] - 1]], "on": on[d]} for d in part if cal[ci[d] - 1] in B}, open(os.path.join(E5, "regime.json"), "w"))


def t7():
    import edge2_fetch as e2
    cal, px = load_daily()
    ci = {d: i for i, d in enumerate(cal)}
    U = sorted(e2.U2)
    noms = json.load(open(os.path.join(E5, "noms.json")))
    grp = {"agy": "research", "xai": "research", "anthropic": "research"}
    bench = {}
    for d in cal:
        i = ci[d]
        for h in HS:
            if i + h < len(cal):
                rs = [px[s][cal[i + h]][1] / px[s][d][1] - 1 for s in U if d in px[s] and cal[i + h] in px[s]]
                bench[(d, h)] = float(np.mean(rs)) if rs else None
    rows = defaultdict(list)
    nd = sorted({r[0] for r in noms})
    dix = {d: k for k, d in enumerate(nd)}
    skipped = 0
    for (d, src, sym, ts, price) in noms:
        src = grp.get(src, src)
        t = datetime.fromtimestamp(ts, ET)
        if d not in ci or sym not in px or d not in px[sym] or t.hour * 60 + t.minute >= 15 * 60 + 55:
            skipped += 1; continue
        c0 = px[sym][d][1]
        for h in HS:
            i = ci[d]
            if i + h >= len(cal) or cal[i + h] not in px[sym] or bench.get((d, h)) is None:
                continue
            fr = px[sym][cal[i + h]][1] / c0 - 1
            x = (fr - bench[(d, h)]) * 1e4
            for lab in (src, "ALL sources"):
                rows[(lab, h, "all")].append((dix[d], x))
                if 20 <= c0 <= 100:
                    rows[(lab, h, "$20-100")].append((dix[d], x))
    print(f"T7 seed-source audit: {len(noms)} (source, symbol, day) nominations {nd[0]}..{nd[-1]}, {skipped} skipped (no bars / after 15:55)")
    print("  entry = close of the nomination day; excess = return - U2 equal-weight same horizon; bp, gross (no cost); t day-clustered")
    for band in ("all", "$20-100"):
        print(f"\n  price band: {band}")
        for src in ("momentum", "trending", "movers", "research", "bb_live", "discord", "ALL sources"):
            for h in HS:
                v = rows.get((src, h, band), [])
                L = st_line(f"{src:12} {h:2d}d", [x[1] for x in v], [x[0] for x in v])
                print(L[0] if isinstance(L, tuple) else L)


if __name__ == "__main__":
    {"T5": t5, "T6": t6, "T7": t7}[sys.argv[1]]()
