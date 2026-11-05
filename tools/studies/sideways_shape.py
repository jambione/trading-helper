"""Sideways-shape vs never-green (operator 11/05). Rules fixed in docs/studies/sideways_shape_prereg.json (amended_1)
BEFORE scoring. Five shape measures from the 15 closed IEX minutes before each desk entry (last CLOSED bar close is the
final close - the broker fill is not used). Label: no IEX 1-min high in the 300 s after entry reaches entry +5 bp.
Run on the mini (needs ~/replay_cache/bars_v2_<day>.pkl and ai_reports/tapes/<day>/outcomes.jsonl)."""
from __future__ import annotations

import glob
import json
import math
import os
import pickle
import random
import statistics as st
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CACHE = os.path.expanduser("~/replay_cache")
FIRST, LAST = "2026-09-24", "2026-10-30"
HALF_SPLIT = "2026-10-14"          # A: 9/24-10/13, B: 10/14-10/30
N_BARS = 15
MEASURES = {"er15": "low", "rng15": "low", "r2_15": "low", "vwap15_crossings": "high", "bbw20": "low"}
Z_BAR = 2.33                       # one-sided p < 0.05/5
MIN_FLAT_N = 80
SHARE_OK = (0.20, 0.30)
COST_BP = 3.0
BOOT = 2000
DROPS: dict = {}


def _drop(why):
    DROPS[why] = DROPS.get(why, 0) + 1


def measures(rows, t):
    cut = t - (t % 60)
    b = [r for r in rows if r[0] < cut]
    if len(b) < 80:
        _drop("short_history"); return None
    last = b[-N_BARS:]
    if b[-1][0] < cut - 180 or (last[-1][0] - last[0][0]) > 60 * (N_BARS + 3):
        _drop("gappy_15"); return None
    c20 = b[-20:]
    if (c20[-1][0] - c20[0][0]) > 60 * 24:
        _drop("gappy_20"); return None
    cl = [r[4] for r in last]
    px = cl[-1]
    path = sum(abs(cl[i] - cl[i - 1]) for i in range(1, len(cl)))
    out = {"er15": abs(cl[-1] - cl[0]) / path if path > 0 else 0.0,
           "rng15": (max(r[2] for r in last) - min(r[3] for r in last)) / px * 1e4}
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
    out["vwap15_crossings"] = sum(1 for i in range(1, len(side)) if side[i] != side[i - 1])
    cc = [r[4] for r in c20]; m20 = sum(cc) / len(cc)
    out["bbw20"] = (4 * st.pstdev(cc) / m20) * 1e4 if m20 > 0 else 0.0
    return out


def ng_label(rows, e, t):
    """True if no IEX 1-min bar starting in (t, t+300] has a high >= e*1.0005; None if the bars are not there."""
    post = [r for r in rows if t < r[0] <= t + 300]
    if len(post) < 3:
        return None
    return max(r[2] for r in post) < e * 1.0005


def load():
    rows = []
    for f in sorted(glob.glob(os.path.join(ROOT, "ai_reports/tapes/2026-*/outcomes.jsonl"))):
        d = f.split("/")[-2]
        if len(d) != 10 or not (FIRST <= d <= LAST):
            continue
        pk = f"{CACHE}/bars_v2_{d}.pkl"
        if not os.path.exists(pk):
            _drop("no_cache_day"); continue
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
            if not iex:
                _drop("no_bars"); continue
            m = measures(iex, float(t))
            if not m:
                continue
            lab = ng_label(iex, float(e), float(t))
            if lab is None:
                _drop("no_post_bars"); continue
            pkp = o.get("peak_price") or e
            rows.append({"d": d, "sym": o["symbol"], "ng": lab, "ng_peak": (pkp - e) / e * 1e4 < 5,
                         "bp": (x - e) / e * 1e4 - COST_BP, **m})
    return rows


def _z(k1, n1, k2, n2):
    if min(n1, n2) < 10:
        return float("nan")
    p = (k1 + k2) / (n1 + n2); se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    return (k1 / n1 - k2 / n2) / se if se else float("nan")


def _flat_fn(name, side, cut):
    return (lambda r: r[name] <= cut) if side == "low" else (lambda r: r[name] >= cut)


def _boot(rows, flat, rng):
    """Day-clustered bootstrap: lower 5th pct of (kept - all) net and of (flat - rest) never-green rate; plus the
    same payoff under a random same-count skip per day (permutation control, one draw per bootstrap)."""
    days = {}
    for r in rows:
        days.setdefault(r["d"], []).append(r)
    names = list(days)
    pay, ngd, ctl = [], [], []
    for _ in range(BOOT):
        s = [r for d in (rng.choice(names) for _ in names) for r in days[d]]
        fl = [r for r in s if flat(r)]; ot = [r for r in s if not flat(r)]
        if not fl or not ot:
            continue
        pay.append(sum(r["bp"] for r in ot) / len(ot) - sum(r["bp"] for r in s) / len(s))
        ngd.append(sum(r["ng"] for r in fl) / len(fl) - sum(r["ng"] for r in ot) / len(ot))
        sk = set()
        for d in {r["d"] for r in s}:
            dr = [r for r in s if r["d"] == d]
            sk.update(id(r) for r in rng.sample(dr, min(len(dr), sum(1 for r in dr if flat(r)))))
        kp = [r for r in s if id(r) not in sk]
        if kp:
            ctl.append(sum(r["bp"] for r in kp) / len(kp) - sum(r["bp"] for r in s) / len(s))
    q = lambda v, p: sorted(v)[int(len(v) * p)] if v else float("nan")
    return {"payoff_lo5": q(pay, 0.05), "payoff_med": q(pay, 0.5), "ng_diff_lo5": q(ngd, 0.05), "control_med": q(ctl, 0.5)}


def score(rows):
    res = {}
    a_rows = [r for r in rows if r["d"] < HALF_SPLIT]
    rng_cuts = None
    for name, side in MEASURES.items():
        va = sorted(r[name] for r in a_rows)
        if len(va) < 150:
            res[name] = {"verdict": "NO_DATA", "n_A": len(va)}; continue
        cut = va[int(len(va) * (0.25 if side == "low" else 0.75))]      # fit on half A only
        flat = _flat_fn(name, side, cut)
        out = {"cut_from_A": round(cut, 4)}
        halves = {"ALL": rows, "A": a_rows, "B": [r for r in rows if r["d"] >= HALF_SPLIT]}
        bad_share = under = False
        for lab, sub in halves.items():
            fl = [r for r in sub if flat(r)]; ot = [r for r in sub if not flat(r)]
            if not fl or not ot:
                out[lab] = None; bad_share = True; continue
            share = len(fl) / len(sub)
            if not (SHARE_OK[0] <= share <= SHARE_OK[1]):
                bad_share = True
            if lab != "ALL" and len(fl) < MIN_FLAT_N:
                under = True
            # volatility-matched NG: same comparison inside rng15 terciles (pooled over A)
            out[lab] = {"n": len(sub), "n_flat": len(fl), "share": share,
                        "ng_flat": sum(r["ng"] for r in fl) / len(fl), "ng_rest": sum(r["ng"] for r in ot) / len(ot),
                        "z": _z(sum(r["ng"] for r in fl), len(fl), sum(r["ng"] for r in ot), len(ot)),
                        "net_flat": sum(r["bp"] for r in fl) / len(fl), "net_rest": sum(r["bp"] for r in ot) / len(ot),
                        "net_all": sum(r["bp"] for r in sub) / len(sub),
                        "ng_peak_flat": sum(r["ng_peak"] for r in fl) / len(fl), "ng_peak_rest": sum(r["ng_peak"] for r in ot) / len(ot)}
        if name != "rng15":
            rv = sorted(r["rng15"] for r in rows); t1, t2 = rv[len(rv) // 3], rv[2 * len(rv) // 3]
            diffs = []
            for lo, hi in ((-1, t1), (t1, t2), (t2, 1e18)):
                g = [r for r in rows if lo < r["rng15"] <= hi]
                fl = [r for r in g if flat(r)]; ot = [r for r in g if not flat(r)]
                if len(fl) >= 20 and len(ot) >= 20:
                    diffs.append(sum(r["ng"] for r in fl) / len(fl) - sum(r["ng"] for r in ot) / len(ot))
            out["ng_diff_within_rng15_terciles"] = [round(x, 3) for x in diffs]
        out["boot"] = _boot(rows, flat, random.Random(20261105))
        al, a, b = out.get("ALL"), out.get("A"), out.get("B")
        if bad_share or not (al and a and b):
            out["verdict"] = "ABORT_SHARE"
        elif under:
            out["verdict"] = "UNDERPOWERED"
        else:
            ng_ok = al["z"] >= Z_BAR and a["ng_flat"] > a["ng_rest"] and b["ng_flat"] > b["ng_rest"] and out["boot"]["ng_diff_lo5"] > 0
            vm = out.get("ng_diff_within_rng15_terciles")
            if vm is not None and not (len(vm) >= 2 and sum(x > 0 for x in vm) >= 2):
                ng_ok = False
            pay_ok = out["boot"]["payoff_lo5"] > 0 and out["boot"]["payoff_med"] > out["boot"]["control_med"]
            out["verdict"] = "PASS_NG_AND_PAYOFF" if ng_ok and pay_ok else "PASS_NG_ONLY" if ng_ok else "FAIL"
        res[name] = out
    return res


def main():
    rows = load()
    days = sorted({r["d"] for r in rows})
    print("drops", DROPS)
    print(f"entries scored {len(rows)} over {len(days)} days; never-green(bar) {sum(r['ng'] for r in rows)/max(1,len(rows)):.0%}, peak-label {sum(r['ng_peak'] for r in rows)/max(1,len(rows)):.0%}")
    if len(rows) < 300:
        print("ABORT: fewer than 300 entries"); return 2
    res = score(rows)
    for name, o in res.items():
        print(f"\n{name} ({MEASURES[name]} = sideways) verdict {o['verdict']}  cut(A) {o.get('cut_from_A')}")
        for lab in ("ALL", "A", "B"):
            c = o.get(lab)
            if c:
                print(f"  {lab:3s} flat n{c['n_flat']:4d} ({c['share']:.0%}) NG {c['ng_flat']:.0%} vs {c['ng_rest']:.0%} z {c['z']:+.2f} | peak-label {c['ng_peak_flat']:.0%} vs {c['ng_peak_rest']:.0%} | net flat {c['net_flat']:+.1f} rest {c['net_rest']:+.1f} all {c['net_all']:+.1f}")
        if "ng_diff_within_rng15_terciles" in o:
            print("  NG diff within rng15 terciles", o["ng_diff_within_rng15_terciles"])
        if "boot" in o:
            print("  day-bootstrap", {k: round(v, 2) for k, v in o["boot"].items()})
    json.dump(res, open(os.path.expanduser("~/sideways_shape_result.json"), "w"), indent=1, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
