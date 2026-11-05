"""Sideways-shape vs never-green (operator 11/05). Rules fixed in docs/studies/sideways_shape_prereg.json BEFORE scoring.

Six price/tape shape measures from the 15 closed IEX minutes before each desk entry (plus the entry price as the live
close). For each, the FLAT quartile (cut = pooled 25th/75th percentile of the measure, label-free) is compared with the
other three quarters on: never-green rate (peak_price < entry +5 bp) and net bp/trade (exit vs entry - 3 bp).
Run on the mini (needs ~/replay_cache/bars_v2_<day>.pkl and ai_reports/tapes/<day>/outcomes.jsonl)."""
from __future__ import annotations

import glob
import json
import math
import os
import pickle
import statistics as st
import sys

CACHE = os.path.expanduser("~/replay_cache")
FIRST, LAST = "2026-09-24", "2026-11-03"
HALF_SPLIT = "2026-10-14"          # A: 9/24-10/13, B: 10/14-11/03
N_BARS = 15
MEASURES = {                       # name -> (direction that means SIDEWAYS: "low" or "high")
    "er15": "low", "rng15": "low", "r2_15": "low", "vwapx15": "high", "bbw20": "low", "tape5": "low",
}
Z_BAR = 2.40                       # one-sided p < 0.05/6
COST_BP = 3.0


def _sub(rows, t):
    """Closed bars strictly before the minute containing t."""
    cut = t - (t % 60)
    out = [r for r in rows if r[0] < cut]
    return out


def measures(rows, entry_px, t):
    b = _sub(rows, t)
    if len(b) < 80:
        return None
    last = b[-N_BARS:]
    if (b[-1][0] < t - 240) or (last[-1][0] - last[0][0]) > 60 * (N_BARS + 3):
        return None
    cl = [r[4] for r in last] + [entry_px]
    path = sum(abs(cl[i] - cl[i - 1]) for i in range(1, len(cl)))
    out = {"er15": abs(cl[-1] - cl[0]) / path if path > 0 else 0.0}
    out["rng15"] = (max(r[2] for r in last) - min(r[3] for r in last)) / entry_px * 1e4
    n = len(cl); xs = list(range(n)); mx = sum(xs) / n; my = sum(cl) / n
    sxx = sum((x - mx) ** 2 for x in xs); sxy = sum((x - mx) * (y - my) for x, y in zip(xs, cl))
    syy = sum((y - my) ** 2 for y in cl)
    out["r2_15"] = (sxy * sxy / (sxx * syy)) if syy > 0 else 0.0
    cv = pv = 0.0; side = []
    for r in last:
        tp = (r[2] + r[3] + r[4]) / 3.0; v = float(r[5] or 0)
        cv += tp * v; pv += v
        if pv > 0:
            side.append(1 if r[4] >= cv / pv else -1)
    out["vwapx15"] = sum(1 for i in range(1, len(side)) if side[i] != side[i - 1])
    c20 = [r[4] for r in b[-20:]]
    m20 = sum(c20) / len(c20)
    out["bbw20"] = (4 * st.pstdev(c20) / m20) * 1e4 if m20 > 0 else 0.0
    v5 = sum(float(r[5] or 0) for r in b[-5:])
    base = [sum(float(r[5] or 0) for r in b[i - 5:i]) for i in range(len(b) - 60, len(b) - 4, 5) if i >= 5]
    med = st.median(base) if base else 0
    out["tape5"] = v5 / med if med > 0 else None
    return out


def load():
    rows = []
    for f in sorted(glob.glob("ai_reports/tapes/2026-*/outcomes.jsonl")):
        d = f.split("/")[-2]
        if len(d) != 10 or not (FIRST <= d <= LAST):
            continue
        pk = f"{CACHE}/bars_v2_{d}.pkl"
        if not os.path.exists(pk):
            continue
        bars = pickle.load(open(pk, "rb")); seen = set()
        for line in open(f):
            try:
                o = json.loads(line)
            except ValueError:
                continue
            e, x, t = o.get("entry_price"), o.get("exit_price"), o.get("entry_time")
            if not (isinstance(e, (int, float)) and x and t) or o.get("entry_path") != "watch" or e < 20:
                continue
            k = (o["symbol"], round(t))
            if k in seen:
                continue
            seen.add(k)
            iex = (bars.get(o["symbol"]) or {}).get("iex") or []
            m = measures(iex, float(e), float(t))
            if not m:
                continue
            pkp = o.get("peak_price") or e
            rows.append({"d": d, "sym": o["symbol"], "ng": (pkp - e) / e * 1e4 < 5,
                         "bp": (x - e) / e * 1e4 - COST_BP, **m})
    return rows


def zdiff(k1, n1, k2, n2):
    if min(n1, n2) < 10:
        return float("nan")
    p = (k1 + k2) / (n1 + n2); se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    return (k1 / n1 - k2 / n2) / se if se else float("nan")


def score(rows):
    res = {}
    for name, side in MEASURES.items():
        vals = sorted(r[name] for r in rows if r.get(name) is not None)
        if len(vals) < 200:
            res[name] = {"verdict": "NO_DATA", "n": len(vals)}; continue
        cut = vals[int(len(vals) * (0.25 if side == "low" else 0.75))]
        def flat(r):
            v = r.get(name)
            return v is not None and (v <= cut if side == "low" else v >= cut)
        out = {"cut": round(cut, 4), "n": len(vals)}
        for lab, sel in (("ALL", lambda r: True), ("A", lambda r: r["d"] < HALF_SPLIT), ("B", lambda r: r["d"] >= HALF_SPLIT)):
            sub = [r for r in rows if r.get(name) is not None and sel(r)]
            fl = [r for r in sub if flat(r)]; ot = [r for r in sub if not flat(r)]
            if not fl or not ot:
                out[lab] = None; continue
            out[lab] = {"n_flat": len(fl), "ng_flat": sum(r["ng"] for r in fl) / len(fl), "ng_rest": sum(r["ng"] for r in ot) / len(ot),
                        "z": zdiff(sum(r["ng"] for r in fl), len(fl), sum(r["ng"] for r in ot), len(ot)),
                        "net_flat": sum(r["bp"] for r in fl) / len(fl), "net_rest": sum(r["bp"] for r in ot) / len(ot),
                        "net_all": sum(r["bp"] for r in sub) / len(sub)}
        a, b, al = out.get("A"), out.get("B"), out.get("ALL")
        ok_ng = bool(al and a and b and al["z"] >= Z_BAR and a["ng_flat"] > a["ng_rest"] and b["ng_flat"] > b["ng_rest"])
        ok_pay = bool(al and a and b and all(x["net_rest"] > x["net_all"] for x in (al, a, b)))
        out["verdict"] = "PASS_NG_AND_PAYOFF" if ok_ng and ok_pay else "PASS_NG_ONLY" if ok_ng else "FAIL"
        res[name] = out
    return res


def main():
    rows = load()
    print(f"entries scored {len(rows)} over {len({r['d'] for r in rows})} days; never-green {sum(r['ng'] for r in rows)/max(1,len(rows)):.0%}")
    res = score(rows)
    for name, o in res.items():
        print(f"\n{name} ({MEASURES[name]} = sideways)  verdict {o['verdict']}")
        for lab in ("ALL", "A", "B"):
            c = o.get(lab)
            if c:
                print(f"  {lab:3s} flat n{c['n_flat']:4d} NG {c['ng_flat']:.0%} vs {c['ng_rest']:.0%} z {c['z']:+.2f} | net flat {c['net_flat']:+.1f} rest {c['net_rest']:+.1f} all {c['net_all']:+.1f} bp")
    json.dump(res, open(os.path.expanduser("~/sideways_shape_result.json"), "w"), indent=1, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
