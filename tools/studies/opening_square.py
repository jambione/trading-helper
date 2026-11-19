#!/usr/bin/env python3
"""opening_square - is a full %R square a good buy in the first 30 minutes? (operator 11/19). Prereg:
docs/studies/opening_square_prereg.json. Steps:

    .venv/bin/python tools/studies/opening_square.py events     # CPU: triggers + gross outcomes -> events.json
    .venv/bin/python tools/studies/opening_square.py quotes     # real SIP spreads (shared news_source cache)
    .venv/bin/python tools/studies/opening_square.py score      # verdict + information
"""
from __future__ import annotations

import hashlib
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))

OUT = ROOT / "ai_reports" / "opening_square"
DECIDE = ("2026-04-28", "2026-09-23")
CONFIRM = ("2026-09-24", "2026-11-18")
W_LO, W_HI = 0, 28
LATE_LO, LATE_HI = 30, 330
GATE_BP = 15.0


def square_bars(cc, hh, ll, off):
    """Today's bar indexes where both %R lines enter >= -20 this bar (not the previous) with fast rising. Pure."""
    from bar_signal_family import wr_smoothed
    fast = wr_smoothed(hh, ll, cc, n=21, span=7)
    slow = wr_smoothed(hh, ll, cc, n=112, span=3)
    fp = np.concatenate(([np.nan], fast[:-1]))
    with np.errstate(invalid="ignore"):
        good = np.isfinite(fast) & np.isfinite(slow)
        sq = good & (fast >= -20) & (slow >= -20)
        rising = np.isfinite(fp) & (fast - fp > 0.05)
    i = np.arange(off + 1, len(cc))
    hit = i[sq[i] & ~sq[i - 1] & rising[i]] - off
    return [int(t) for t in hit]


def presq_all(cc, hh, ll, off):
    """presquare_bars without its 15..360 clip (same rule)."""
    import evening_family as ef
    lo, hi = ef.FIRST, ef.LAST
    try:
        ef.FIRST, ef.LAST = 0, 389
        return ef.presquare_bars(cc, hh, ll, off, 21)
    finally:
        ef.FIRST, ef.LAST = lo, hi


def g10(o, c, t):
    if t + 10 >= 390 or not (np.isfinite(o[t + 1]) and np.isfinite(c[t + 10]) and o[t + 1] > 0):
        return None
    return round(float((c[t + 10] / o[t + 1] - 1) * 1e4), 2)


def rand_t(day, sym):
    return W_LO + int(hashlib.sha256(f"osq|{day}|{sym}".encode()).hexdigest(), 16) % (W_HI - W_LO + 1)


def events():
    from relative_strength_scan import load_day
    from board_structure_family import universe
    from evening_family import ALL_ETF, WH_SIP, minute_grid
    from holistic_entry import bracket, COST
    import news_source as ns
    files = sorted(p for p in WH_SIP.glob("20??-??-??.npz") if DECIDE[0] <= p.stem <= CONFIRM[1] or p.stem < DECIDE[0])
    rows, prev = [], None
    for fp in files:
        day = load_day(fp)
        if prev is None or fp.stem < DECIDE[0]:
            prev = day
            continue
        if day["idx"].get("SPY") is None:
            prev = day
            continue
        for s in universe(day, prev, set(ALL_ETF)):
            i, j = day["idx"][s], prev["idx"].get(s)
            if j is None:
                continue
            pc = ns.last_close(prev["c"][j])
            if not pc > 0:
                continue
            o, h, l, c = (day[k][i] for k in "ohlc")
            cc, hh, ll, off = minute_grid(prev["c"][j], prev["h"][j], prev["l"][j], c, h, l)

            def ok(t):
                px = c[t]
                return np.isfinite(px) and 20 <= px <= 400 and (px / pc - 1) * 100 >= 1.5

            def add(arm, t):
                b = bracket(o, h, l, c, t)
                if b is None:
                    return
                rows.append({"arm": arm, "day": fp.stem, "sym": s, "t": int(t),
                             "gross": round(float(b) + COST, 2), "g10": g10(o, c, t)})
            sq = [t for t in square_bars(cc, hh, ll, off) if ok(t)]
            early = [t for t in sq if W_LO <= t <= W_HI]
            if early:
                add("square", early[0])
            late = [t for t in sq if LATE_LO <= t <= LATE_HI]
            if late:
                add("square_late", late[0])
            pre = [t for t in presq_all(cc, hh, ll, off) if W_LO <= t <= W_HI and ok(t)]
            if pre:
                add("presquare", pre[0])
            if early or pre:
                rt = rand_t(fp.stem, s)
                if ok(rt):
                    add("random", rt)
        print(fp.stem, len(rows), flush=True)
        prev = day
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "events.json").write_text(json.dumps(rows))
    return rows


INFO_CAP = {"presquare": 1500, "random": 1500, "square_late": 1000}


def keep_rows(rows):
    """Amendment 1: every 'square' row; info arms capped per (arm, period) by the lowest sha256('osq'|day|sym|t). Pure."""
    by = defaultdict(list)
    for r in rows:
        by[(r["arm"], r["day"] <= DECIDE[1])].append(r)
    out = []
    for (arm, _), v in by.items():
        cap = INFO_CAP.get(arm)
        if cap is not None and len(v) > cap:
            v = sorted(v, key=lambda r: hashlib.sha256(f"osq|{r['day']}|{r['sym']}|{r['t']}".encode()).hexdigest())[:cap]
        out += v
    return out


def plan(which="all"):
    rows = keep_rows(json.loads((OUT / "events.json").read_text()))
    if which != "all":
        rows = [r for r in rows if (r["arm"] == "square") == (which == "square")]
    return sorted({("osq", r["day"], r["sym"], r["t"], 0.0) for r in rows})


def quotes():
    import news_source as ns
    ns.fetch_quotes(plan())


def day_t(rows):
    per = defaultdict(list)
    for r in rows:
        per[r["day"]].append(r["net"])
    m = [np.mean(v) for v in per.values()]
    if len(m) < 3 or np.std(m, ddof=1) == 0:
        return None
    return round(float(np.mean(m) / (np.std(m, ddof=1) / math.sqrt(len(m)))), 2)


def summ(rows):
    if not rows:
        return {"n": 0}
    x = np.array([r["net"] for r in rows])
    days = sorted({r["day"] for r in rows})
    h = days[len(days) // 2] if days else ""
    a = [r["net"] for r in rows if r["day"] < h]
    b = [r["net"] for r in rows if r["day"] >= h]
    return {"n": len(rows), "days": len(days), "mean_net": round(float(x.mean()), 2),
            "win_pct": round(100 * float((x > 0).mean()), 1), "t_day": day_t(rows),
            "half1": round(float(np.mean(a)), 2) if a else None, "half2": round(float(np.mean(b)), 2) if b else None}


def verdict(dec, conf):
    why = [f"edge {dec.get('mean_net')} bp (need +2.0)", f"t {dec.get('t_day')} (need 2.5)",
           f"halves {dec.get('half1')} / {dec.get('half2')}", f"n {dec.get('n')} (need 300)",
           f"confirm {conf.get('mean_net')} (need > 0)"]
    ok = (dec.get("n", 0) >= 300 and (dec.get("mean_net") or -99) >= 2.0 and (dec.get("t_day") or -99) >= 2.5
          and (dec.get("half1") or -1) > 0 and (dec.get("half2") or -1) > 0 and (conf.get("mean_net") or -1) > 0)
    return ("PASS" if ok else "FAIL"), why


def score():
    import news_source as ns
    cache = json.load(open(ns.QUOTES)) if ns.QUOTES.exists() else {}
    rows = keep_rows(json.loads((OUT / "events.json").read_text()))
    stats = defaultdict(lambda: {"noquote": 0, "gated": 0, "spreads": []})
    traded = defaultdict(list)
    for r in rows:
        per = "decide" if r["day"] <= DECIDE[1] else "confirm"
        k = (r["arm"], per)
        sp = cache.get(ns.qkey(r["day"], r["sym"], r["t"]), "missing")
        if sp == "missing" or sp is None:
            stats[k]["noquote"] += 1
            continue
        stats[k]["spreads"].append(sp)
        if sp > GATE_BP:
            stats[k]["gated"] += 1
            continue
        traded[k].append({**r, "net": r["gross"] - sp})
    out = {}
    for k in sorted(set(stats) | set(traded)):
        s = summ(traded[k])
        s["noquote"], s["gated"] = stats[k]["noquote"], stats[k]["gated"]
        s["median_spread"] = round(float(np.median(stats[k]["spreads"])), 2) if stats[k]["spreads"] else None
        g = [r["g10"] for r in traded[k] if r["g10"] is not None]
        s["g10_gross"] = round(float(np.mean(g)), 2) if g else None
        out["|".join(k)] = s
    v = verdict(out.get("square|decide", {}), out.get("square|confirm", {}))
    out["VERDICT"] = v
    if out.get("square|decide", {}).get("n"):
        out["opens_per_10m_decide"] = round(out["square|decide"]["n"] / out["square|decide"]["days"] / 3.0, 2)
    (OUT / "score.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))
    return out


if __name__ == "__main__":
    {"events": events, "quotes": quotes, "score": score}[sys.argv[1]]()
