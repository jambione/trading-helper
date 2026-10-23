#!/usr/bin/env python3
"""NR7 + inside day breakout (Crabel). Spec: docs/studies/nr7_inside_prereg.json (amended, amended_2; common C1-C4).

Day t-1 is NR7 (smallest RTH high-low range of t-7 .. t-1, the name present in all 7) AND inside t-2 (high below,
low above). Day t: skip (counted) if the 09:30 open is above the t-1 high (gap-through). Trigger = the first 1-minute
bar from 09:35 to 15:00 whose open <= level and high >= level (level = t-1 high + $0.01); fill at the level. Stop: a
1-minute close below the t-1 low exits at the next bar's open; else the last close <= 15:59. Control: a seeded random
minute in (trigger, min(trigger + 60, 15:00)] on the same name-day, same percent stop, same exits.

  (default) count step (outcome-blind; MDE from control - control2); --score after a result-skeptic READY
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import statistics
import sys
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eod_reversal as E  # noqa: E402

ROOT = E.ROOT
OUT_DIR = os.path.join(ROOT, "ai_reports", "nr7_inside")
FIRST, LAST, HALF_A_END = "2026-05-11", "2026-10-15", "2026-07-29"
M_OPEN, M_FROM, M_TO, M_LAST = 570, 575, 900, 959
TICK, MIN_PX, COST, COSTS = 0.01, 10.0, 0.0005, (0.0003, 0.0005, 0.0010, 0.0020)
LIFT_BAR, T_PASS, T_BATCH = 0.0005, 2.0, 2.84
FLOOR_TRADES, FLOOR_DAYS = 300, 20


def day_ranges(day: str) -> dict:
    import indicator_confirm_hist as H
    return {s: (float(r[:, 2].max()), float(r[:, 3].min())) for s, r in H.load_raw("sip", day).items()}


def setups(prior: list) -> dict:
    """prior: 7 (high, low) tuples t-7 .. t-1 -> flags for day t."""
    rng = [h - lo for h, lo in prior]
    nr7 = rng[-1] < min(rng[:-1])                        # strictly narrowest (ties are not NR7)
    nr4 = rng[-1] < min(rng[-4:-1])
    inside = prior[-1][0] < prior[-2][0] and prior[-1][1] > prior[-2][1]
    return {"nr7_inside": nr7 and inside, "nr7": nr7, "nr4_inside": nr4 and inside}


def walk(r, e: int, entry: float, stop: float, side: int = 1) -> dict:
    """From row e (the entry row, filled at `entry`): a close beyond the stop exits at the next open; else 15:59."""
    for k in range(e, len(r)):
        if r[k, 0] > M_LAST:
            break
        c = r[k, 4]
        if (c < stop) if side > 0 else (c > stop):
            if k + 1 < len(r) and r[k + 1, 0] <= M_LAST:
                return {"gross": side * (r[k + 1, 1] / entry - 1), "why": "stop"}
            return {"gross": side * (c / entry - 1), "why": "stop_last"}
    last = E.last_close_at_or_before(r, M_LAST)
    return {"gross": side * (last / entry - 1), "why": "time"}


def trigger(r, level: float, side: int = 1) -> int | None:
    for k in range(len(r)):
        m = r[k, 0]
        if m < M_FROM:
            continue
        if m > M_TO:
            return None
        o, h, lo = r[k, 1], r[k, 2], r[k, 3]
        if side > 0 and o <= level and h >= level:
            return k
        if side < 0 and o >= level and lo <= level:
            return k
    return None


def control(r, sym, day, k_trig, stop_pct, tag, side=1):
    t0 = r[k_trig, 0]
    cand = [k for k in range(len(r)) if t0 < r[k, 0] <= min(t0 + 60, M_TO)]
    if not cand:
        return None
    rng = random.Random(int(hashlib.sha256(f"{sym}|{day}|{tag}".encode()).hexdigest()[:16], 16))
    e = rng.choice(cand)
    o = float(r[e, 1])
    return walk(r, e, o, o * (1 - side * stop_pct), side)["gross"]


def day_task(args) -> dict:
    day, cands = args
    import indicator_confirm_hist as H
    raw = H.load_raw("sip", day)
    rows, st = [], Counter()
    for sym, prior, flags in cands:
        r = raw.get(sym)
        if r is None:
            st["no_bars_today"] += 1
            continue
        hi1, lo1 = prior[-1]
        for var, on in flags.items():
            if not on:
                continue
            for side, lab in ((1, "long"), (-1, "short")):
                if side < 0 and var != "nr7_inside":
                    continue
                level = hi1 + TICK if side > 0 else lo1 - TICK
                stop = lo1 if side > 0 else hi1
                op = E.bar_at(r, M_OPEN)
                if op is not None and ((op[1] > hi1) if side > 0 else (op[1] < lo1)):
                    st[f"{var}_{lab}_gap_through"] += 1
                    continue
                k = trigger(r, level, side)
                if k is None:
                    st[f"{var}_{lab}_no_trigger"] += 1
                    continue
                if level < MIN_PX:
                    st[f"{var}_{lab}_below_10"] += 1
                    continue
                res = walk(r, k, level, stop, side)
                sp = abs(level - stop) / level
                c1 = control(r, sym, day, k, sp, f"nr7-ctrl|{var}|{lab}", side)
                c2 = control(r, sym, day, k, sp, f"nr7-ctrl2|{var}|{lab}", side)
                rows.append({"day": day, "sym": sym, "var": var, "side": lab, **res, "ctrl": c1, "ctrl2": c2,
                             "trig_min": int(r[k, 0])})
    return {"rows": rows, "stats": dict(st)}


def module_shas() -> dict:
    d = os.path.dirname(os.path.abspath(__file__))
    return {m: hashlib.sha256(open(os.path.join(d, f"{m}.py"), "rb").read()).hexdigest()
            for m in ("nr7_inside", "eod_reversal")}


def write(name: str, obj) -> str:
    os.makedirs(OUT_DIR, exist_ok=True)
    p = os.path.join(OUT_DIR, name)
    json.dump(obj, open(p + ".tmp", "w"), indent=1, default=str)
    os.replace(p + ".tmp", p)
    return p


def dstats(rows, key):
    per = defaultdict(list)
    for x in rows:
        v = key(x)
        if v is not None:
            per[x["day"]].append(v)
    return E.stats([statistics.mean(v) for v in per.values()]), per


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--score", action="store_true")
    ap.add_argument("--skeptic-ready", default="")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--prereg-commit", default="")
    a = ap.parse_args()
    import indicator_confirm_hist as H
    days = [d for d in H.warehouse_days() if d <= LAST]
    cache = os.path.join(OUT_DIR, "rows.json")
    if os.path.exists(cache) and json.load(open(cache)).get("module_sha256") == module_shas():
        c = json.load(open(cache))
        rows, st = c["rows"], Counter(c["stats"])
    else:
        with ProcessPoolExecutor(max_workers=a.workers) as ex:
            ranges = list(ex.map(day_ranges, days, chunksize=2))
        tasks = []
        for i, day in enumerate(days):
            if day < FIRST or i < 7:
                continue
            cands = []
            for sym in ranges[i]:
                prior = [ranges[j].get(sym) for j in range(i - 7, i)]
                if any(p is None for p in prior):
                    continue                              # not present in all 7 prior sessions
                fl = setups(prior)
                if any(fl.values()):
                    cands.append((sym, prior, fl))
            tasks.append((day, cands))
        rows, st = [], Counter()
        with ProcessPoolExecutor(max_workers=a.workers) as ex:
            for res in ex.map(day_task, tasks, chunksize=2):
                rows += res["rows"]
                st.update(res["stats"])
        write("rows.json", {"rows": rows, "stats": dict(st), "module_sha256": module_shas()})
    prim = [x for x in rows if x["var"] == "nr7_inside" and x["side"] == "long"]
    half = lambda x: "A" if x["day"] <= HALF_A_END else "B"  # noqa: E731
    if not a.score:
        cnt = {"stats": dict(st), "module_sha256": module_shas(), "prereg_commit": a.prereg_commit}
        for h in ("A", "B"):
            hr = [x for x in prim if half(x) == h]
            s, _ = dstats(hr, lambda x: x["ctrl"] - x["ctrl2"] if x["ctrl"] is not None and x["ctrl2"] is not None
                          else None)
            per = defaultdict(list)
            for x in hr:
                if x["ctrl"] is not None and x["ctrl2"] is not None:
                    per[x["day"]].append(x["ctrl"] - x["ctrl2"])
            xs = [statistics.mean(v) for v in per.values()]
            mde = (2.0 + 0.84) * statistics.stdev(xs) / math.sqrt(len(xs)) if len(xs) >= 2 else None
            days_n = len({x["day"] for x in hr})
            cnt[h] = {"trades": len(hr), "days": days_n, "with_control": sum(1 for x in hr if x["ctrl"] is not None),
                      "mde": mde, "floors_met": len(hr) >= FLOOR_TRADES and days_n >= FLOOR_DAYS and mde is not None
                      and mde <= LIFT_BAR}
        print(json.dumps(cnt, indent=1, default=str))
        print(write("count.json", cnt))
        return
    if not a.skeptic_ready:
        raise SystemExit("--score needs --skeptic-ready")
    cnt = json.load(open(os.path.join(OUT_DIR, "count.json")))
    if cnt.get("module_sha256") != module_shas() or not cnt.get("prereg_commit"):
        raise SystemExit("count.json from different code or without --prereg-commit")
    if not all(cnt[h]["floors_met"] for h in ("A", "B")):
        raise SystemExit("UNDERPOWERED at the count step: no outcome is read")
    res, hv, ubs, ts = {"halves": {}, "info": {}}, [], [], []
    for h in ("A", "B"):
        hr = [x for x in prim if half(x) == h]
        lift, _ = dstats(hr, lambda x: x["gross"] - x["ctrl"] if x["ctrl"] is not None else None)
        net, _ = dstats(hr, lambda x: x["gross"] - COST)
        if (lift["t"] is not None and lift["t"] <= -2) or (net["t"] is not None and net["t"] <= -2):
            v = "FAIL"
        elif lift["mean"] >= LIFT_BAR and (lift["t"] or 0) >= T_PASS and net["mean"] > 0:
            v = "PASS"
        else:
            v = "NOT_PASSED"
        hv.append(v)
        ubs.append(lift["ub"])
        ts.append(lift["t"] or 0)
        res["halves"][h] = {"verdict": v, "lift": lift, "net": net, "exit_mix": dict(Counter(x["why"] for x in hr)),
                            "net_by_cost": {f"{round(c * 1e4)}bp": dstats(hr, lambda x, c=c: x["gross"] - c)[0]["mean"]
                                            for c in COSTS}}
        for var, side in (("nr7", "long"), ("nr4_inside", "long"), ("nr7_inside", "short")):
            sub = [x for x in rows if x["var"] == var and x["side"] == side and half(x) == h]
            res["info"][f"{var}_{side}_{h}"] = {
                "n": len(sub), "net": dstats(sub, lambda x: x["gross"] - COST)[0]["mean"],
                "lift": dstats(sub, lambda x: x["gross"] - x["ctrl"] if x["ctrl"] is not None else None)[0]["mean"]}
    if "FAIL" in hv:
        verdict = "FAIL"
    elif all(x == "PASS" for x in hv):
        verdict = "PASS (batch-robust)" if min(ts) >= T_BATCH else "PASS (single-test), not batch-robust"
    elif all(u is not None and u < LIFT_BAR for u in ubs):
        verdict = "FAIL"
    else:
        verdict = "UNDERPOWERED"
    res["verdict"] = verdict
    res["declared"] = ["the trigger needs open <= level (a bar opening above the level after a close below never "
                       "triggers; fast breakouts under-counted, amended_2)", "ties for the narrowest range are not NR7",
                       "the 'nr7' (no inside rule) information set includes the primary set"]
    res["skeptic_ready"], res["module_sha256"] = a.skeptic_ready, module_shas()
    write("result.json", res)
    print(json.dumps(res, indent=1, default=str))


if __name__ == "__main__":
    main()
