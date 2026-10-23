#!/usr/bin/env python3
"""Intraday periodicity (Heston, Korajczyk & Sadka 2010). Spec: docs/studies/periodicity_prereg.json (as amended;
common C1-C4).

13 half-hour slots. History return of slot s = last close <= slot end / last close <= previous slot end - 1 (slot 1:
09:59 close / 09:30 open). SIG(name, s, day) = mean of the previous 20 warehouse sessions' slot-s history returns (>= 10
present). Each day and slot 2..13: eligible = SIG present, the slot's first bar present, its open >= $10; LONG = the top
decile (>= 20 eligible). Trade = slot's first bar open -> last close <= slot end. LIFT = long mean minus all-eligible
mean; per day the mean over slots; day-equal; day-clustered t.

  (default) count step: slot-trades and days per half; MDE from a seeded random-decile placebo (C1)
  --score   verdict; refuses without --skeptic-ready, --prereg-commit in count.json and a count step from this code
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
import eod_reversal as E  # noqa: E402  (shared stats / verdict helpers)

ROOT = E.ROOT
OUT_DIR = os.path.join(ROOT, "ai_reports", "periodicity")
FIRST, LAST, HALF_A_END = "2026-05-11", "2026-10-15", "2026-07-29"
LOOKBACK, MIN_HIST = 20, 10
INFO_LOOKBACKS = {"lb5": (5, 3), "lb40": (40, 20)}      # information; lb40 incomplete early in half A
MAX_LB = 40
SLOT_START = [570 + 30 * k for k in range(13)]          # 09:30 .. 15:30
SLOT_END = [s + 29 for s in SLOT_START]                 # 09:59 .. 15:59
TRADED = range(1, 13)                                   # slots 2..13 (index 1..12); slot 1 is information
MIN_PX, COST, COSTS = 10.0, 0.0005, (0.0003, 0.0005, 0.0010, 0.0020)
LIFT_BAR, T_PASS, T_BATCH, T_PLACEBO = 0.0005, 2.0, 2.84, 2.0     # batch: 11 studies (fence_bar amended (4))
FLOOR_TRADES, FLOOR_DAYS, MIN_ELIGIBLE, DECILE = 300, 20, 20, 0.10


def slot_fields(r) -> dict:
    """hist[k]: history return of slot k (None if missing); ent[k]: open of the slot's first bar (exact minute);
    ret[k]: ent -> last close <= slot end (C2)."""
    hist, ent, ret = [], [], []
    for k in range(13):
        end_c = E.last_close_at_or_before(r, SLOT_END[k])
        if k == 0:
            b0 = E.bar_at(r, SLOT_START[0])
            base = float(b0[1]) if b0 is not None else None
        else:
            base = E.last_close_at_or_before(r, SLOT_END[k - 1])
        lo_m = SLOT_START[k] - 1
        own = bool(((r[:, 0] > lo_m) & (r[:, 0] <= SLOT_END[k])).any())   # a slot with no bars of its own: None
        hist.append(end_c / base - 1 if own and end_c is not None and base else None)
        b = E.bar_at(r, SLOT_START[k])
        if b is None:
            ent.append(None)
            ret.append(None)
        else:
            o = float(b[1])
            ent.append(o)
            ret.append(E.last_close_at_or_before(r, SLOT_END[k]) / o - 1)
    return {"hist": hist, "ent": ent, "ret": ret}


def day_task(day: str) -> dict:
    import indicator_confirm_hist as H
    return {"day": day, "f": {s: slot_fields(r) for s, r in H.load_raw("sip", day).items()}}


def signals(history: dict, sym: str, k: int, cur: int, lb: int = LOOKBACK, need: int = MIN_HIST) -> float | None:
    """Mean of the name's slot-k history returns over the previous `lb` SESSIONS (session index window, not the
    name's last appearances)."""
    xs = [v for (i, v) in history.get(sym, {}).get(k, []) if cur - lb <= i < cur and v is not None]
    return statistics.mean(xs) if len(xs) >= need else None


def placebo(names: list[str], n: int, day: str, k: int) -> list[str]:
    rng = random.Random(int(hashlib.sha256(f"periodicity-placebo|{day}|{k}".encode()).hexdigest()[:16], 16))
    return rng.sample(sorted(names), n)


def _lift(elig: list, top_first: bool = True) -> float | None:
    if len(elig) < MIN_ELIGIBLE:
        return None
    n = max(1, int(len(elig) * DECILE))
    pick = sorted(elig, key=lambda e: ((-e[0]) if top_first else e[0], e[1]))[:n]
    return statistics.mean(e[2] for e in pick) - statistics.mean(e[2] for e in elig)


def day_summary(day: str, f: dict, history: dict, st: Counter, cur: int) -> dict | None:
    lifts, plifts, tops, k_tot, slot1 = [], [], [], 0, None
    info = defaultdict(list)
    for k in range(13):
        elig = []
        for s, x in f.items():
            if x["ent"][k] is None:
                st["skip_no_entry_bar"] += 1
                continue
            if x["ent"][k] < MIN_PX:
                st["skip_below_10"] += 1
                continue
            sig = signals(history, s, k, cur)
            if sig is None:
                st["no_history"] += 1
                continue
            elig.append((sig, s, x["ret"][k]))
            if k >= 1:
                for lab, (lb, need) in INFO_LOOKBACKS.items():
                    v = signals(history, s, k, cur, lb, need)
                    if v is not None:
                        info[(lab, k)].append((v, s, x["ret"][k]))
        if len(elig) < MIN_ELIGIBLE:
            st["slot_skipped_few_names"] += 1
            continue
        n = max(1, int(len(elig) * DECILE))
        top = sorted(elig, key=lambda e: (-e[0], e[1]))[:n]
        allm = statistics.mean(e[2] for e in elig)
        lift = statistics.mean(e[2] for e in top) - allm
        rets = {e[1]: e[2] for e in elig}
        pl = statistics.mean(rets[s] for s in placebo(list(rets), n, day, k)) - allm
        if k == 0:
            slot1 = lift
            continue
        lifts.append(lift)
        plifts.append(pl)
        tops.append(statistics.mean(e[2] for e in top))
        k_tot += n
        info["bottom"].append(_lift(elig, top_first=False))
        info["px20"].append(_lift([e for e in elig if f[e[1]]["ent"][k] >= 20.0]))
    if not lifts:
        return None
    def m(xs):
        xs = [x for x in xs if x is not None]
        return statistics.mean(xs) if xs else None
    out = {"day": day, "lift": statistics.mean(lifts), "placebo_lift": statistics.mean(plifts),
           "los": statistics.mean(tops), "k": k_tot, "slots": len(lifts), "slot1_lift": slot1,
           "slot13_lift": lifts[-1] if len(lifts) == 12 else None,
           "bottom_lift": m(info["bottom"]), "px20_lift": m(info["px20"])}
    for lab in INFO_LOOKBACKS:
        out[f"{lab}_lift"] = m([_lift(info[(lab, k)]) for k in range(1, 13)])
    return out


def half_verdict(ds: list[dict]) -> tuple[str, dict]:
    """The registered rule exactly: LIFT >= +5 bp with t >= 2 AND top-decile mean net (after 5 bp) > 0.
    FAIL (t <= -2) is checked first; net t and drop-3 are information."""
    s = E.stats([d["lift"] for d in ds])
    net = E.stats([d["los"] - COST for d in ds])
    trades = sum(d["k"] for d in ds)
    s.update(trades=trades, net_mean=net["mean"], net_t=net["t"], drop3_info=E.drop3(ds))
    if s["t"] is not None and s["t"] <= -2:
        return "FAIL", s
    if trades < FLOOR_TRADES or s["days"] < FLOOR_DAYS or s["t"] is None:
        return "UNDERPOWERED", s
    ok = s["mean"] >= LIFT_BAR and s["t"] >= T_PASS and net["mean"] is not None and net["mean"] > 0
    return ("PASS" if ok else "NOT_PASSED"), s


def final(hv: list[str], ss: list[dict]) -> str:
    if "FAIL" in hv:
        return "FAIL"
    if all(h == "PASS" for h in hv):
        return "PASS (batch-robust)" if all((x["t"] or 0) >= T_BATCH for x in ss) else \
            "PASS (single-test), not batch-robust"
    if all(x["days"] >= 2 and x["ub"] is not None and x["ub"] < LIFT_BAR for x in ss):
        return "FAIL"
    return "UNDERPOWERED"


def module_shas() -> dict:
    return {m: hashlib.sha256(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), f"{m}.py"), "rb")
                              .read()).hexdigest() for m in ("periodicity", "eod_reversal")}


def write(name: str, obj) -> str:
    os.makedirs(OUT_DIR, exist_ok=True)
    p = os.path.join(OUT_DIR, name)
    json.dump(obj, open(p + ".tmp", "w"), indent=1, default=str)
    os.replace(p + ".tmp", p)
    return p


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--score", action="store_true")
    ap.add_argument("--skeptic-ready", default="")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--prereg-commit", default="")
    a = ap.parse_args()
    import indicator_confirm_hist as H
    days = [d for d in H.warehouse_days() if d <= LAST]
    cache = os.path.join(OUT_DIR, "days.json")
    if os.path.exists(cache) and json.load(open(cache)).get("module_sha256") == module_shas():
        c = json.load(open(cache))
        summ, st = c["days"], Counter(c["stats"])
    else:
        summ, st = [], Counter()
        history: dict = defaultdict(lambda: defaultdict(list))   # sym -> slot -> [(session index, value)]
        with ProcessPoolExecutor(max_workers=a.workers) as ex:
            for cur, d in enumerate(ex.map(day_task, days, chunksize=2)):   # map keeps day order
                if d["day"] >= FIRST:
                    s = day_summary(d["day"], d["f"], history, st, cur)    # scored BEFORE its own returns enter
                    if s:
                        summ.append(s)
                for sym, x in d["f"].items():
                    for k in range(13):
                        lst = history[sym][k]
                        lst.append((cur, x["hist"][k]))
                        while lst and lst[0][0] < cur - MAX_LB:
                            lst.pop(0)
        write("days.json", {"days": summ, "stats": dict(st), "module_sha256": module_shas()})
    halves = {"A": [d for d in summ if d["day"] <= HALF_A_END], "B": [d for d in summ if d["day"] > HALF_A_END]}
    if not a.score:
        cnt = {"stats": dict(st), "module_sha256": module_shas(), "prereg_commit": a.prereg_commit}
        for h, ds in halves.items():
            pl = [d["placebo_lift"] for d in ds]
            sd = statistics.stdev(pl) if len(pl) >= 2 else None
            mde = (T_PLACEBO + 0.84) * sd / math.sqrt(len(pl)) if sd else None
            trades = sum(d["k"] for d in ds)
            cnt[h] = {"days": len(ds), "slot_trades": trades, "mde": mde,
                      "floors_met": trades >= FLOOR_TRADES and len(ds) >= FLOOR_DAYS and mde is not None
                      and mde <= LIFT_BAR}
        print(json.dumps(cnt, indent=1, default=str))
        print(write("count.json", cnt))
        return
    if not a.skeptic_ready:
        raise SystemExit("--score needs --skeptic-ready")
    cnt = json.load(open(os.path.join(OUT_DIR, "count.json")))
    if cnt.get("module_sha256") != module_shas():
        raise SystemExit("count.json from different code")
    if not cnt.get("prereg_commit"):
        raise SystemExit("count.json has no prereg commit (--prereg-commit)")
    res = {"halves": {}, "info": {}}
    hv, ss = [], []
    for h, ds in halves.items():
        v, s = half_verdict(ds)
        if v != "FAIL" and not cnt[h]["floors_met"]:
            v = "UNDERPOWERED"
        hv.append(v)
        ss.append(s)
        res["halves"][h] = {"verdict": v, **s, "batch_t_met": (s.get("t") or 0) >= T_BATCH,
                            "long_net_by_cost": {f"{round(c * 1e4)}bp": E.stats([d["los"] - c for d in ds])["mean"]
                                                 for c in COSTS}}
        res["info"][h] = {k: E.stats([d[k] for d in ds])["mean"] for k in
                          ("slot1_lift", "slot13_lift", "placebo_lift", "bottom_lift", "px20_lift", "lb5_lift",
                           "lb40_lift")}
    res["verdict"] = final(hv, ss)
    res["declared"] = ["The placebo MDE measures random-subset noise only; the top decile is a systematic tilt, so read "
                       "power from the lift's upper bound.", "lb40 is incomplete before ~2026-06-22 (10 warm-up sessions)."]
    res["skeptic_ready"], res["module_sha256"] = a.skeptic_ready, module_shas()
    write("result.json", res)
    print(json.dumps(res, indent=1, default=str))


if __name__ == "__main__":
    main()
