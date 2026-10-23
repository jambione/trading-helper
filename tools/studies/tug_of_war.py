#!/usr/bin/env python3
"""Overnight vs intraday tug of war (Lou, Polk & Skouras 2019) as a name picker. Spec:
docs/studies/tug_of_war_prereg.json (amended, amended_2; common C1-C4).

For each warehouse name and day t: INTRA = sum of log(close/open) over the previous 20 warehouse sessions (>= 15
present), OVN = sum of log(open / prior close) over the same sessions; score = INTRA - OVN. Each scored day (2026-05-26
.. 10-15) LONG = the top decile by score among names with a 09:45 bar (open >= $10). Trade 09:45 open -> last close
<= 15:59. LIFT = long mean minus all-eligible mean; day-equal, day-clustered t; a two-way (day x name) clustered t of
the trade-level excess is required too.

  --precheck  Alpaca daily open/close vs warehouse 09:30 open / 15:59 close on 20 seeded name-days (no outcomes);
              picks the open/close source per amended (1)
  (default)   count step (outcome-blind; placebo MDE per C1)
  --score     verdict; refuses without --skeptic-ready, the precheck and a count step from this code
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
OUT_DIR = os.path.join(ROOT, "ai_reports", "tug_of_war")
FIRST, LAST, HALF_A_END = "2026-05-26", "2026-10-15", "2026-07-29"
LB, NEED, INFO_LB = 20, 15, 60
M_OPEN, M_ENTRY, M_CLOSE = 570, 585, 959
MIN_PX, COST, COSTS = 10.0, 0.0005, (0.0003, 0.0005, 0.0010, 0.0020)
LIFT_BAR, T_PASS, T_BATCH, T_PLACEBO = 0.0005, 2.0, 2.84, 2.0
FLOOR_TRADES, FLOOR_DAYS, MIN_ELIGIBLE, DECILE = 300, 20, 20, 0.10
GAP_LIMIT = 0.0010


def day_fields(day: str) -> dict:
    """sym -> (open 09:30, close <= 15:59, entry 09:45 open or None, trade return or None)."""
    import indicator_confirm_hist as H
    out = {}
    for s, r in H.load_raw("sip", day).items():
        o = E.bar_at(r, M_OPEN)
        c = E.last_close_at_or_before(r, M_CLOSE)
        ent = E.bar_at(r, M_ENTRY)
        eo = float(ent[1]) if ent is not None else None
        out[s] = (float(o[1]) if o is not None else None, c, eo, (c / eo - 1) if eo and c else None)
    return out


def score(hist: list, cur: int, lb: int = LB, need: int = NEED) -> tuple | None:
    """hist: [(session index, open, close, prior close)]; sessions cur-lb .. cur-1."""
    xs = [h for h in hist if cur - lb <= h[0] < cur and h[1] and h[2]]
    if len(xs) < need:
        return None
    intra = sum(math.log(h[2] / h[1]) for h in xs)
    ovn_x = [math.log(h[1] / h[3]) for h in xs if h[3]]
    if len(ovn_x) < need:
        return None
    ovn = sum(ovn_x)
    return intra - ovn, intra, ovn


def pick(elig: list, key: int, top_first: bool = True) -> list:
    n = max(1, int(len(elig) * DECILE))
    return sorted(elig, key=lambda e: ((-e[key]) if top_first else e[key], e[0]))[:n]


def two_way_t(trades: list[tuple[str, str, float]]) -> float | None:
    """Cameron-Gelbach-Miller two-way clustered t of the mean of x over (day, name, x)."""
    if len(trades) < 3:
        return None
    mu = statistics.mean(t[2] for t in trades)
    n = len(trades)

    def clus(idx):
        g = defaultdict(float)
        for t in trades:
            g[t[idx] if idx is not None else (t[0], t[1])] += t[2] - mu
        return sum(v * v for v in g.values())
    v_day, v_name = clus(0) / n ** 2, clus(1) / n ** 2
    var = v_day + v_name - clus(None) / n ** 2
    var = max(var, v_day, v_name)          # CGM can go negative; fall back to the larger one-way variance
    return mu / math.sqrt(var) if var > 0 else None


def placebo(names: list, k: int, day: str) -> list:
    rng = random.Random(int(hashlib.sha256(f"tug_of_war-placebo|{day}".encode()).hexdigest()[:16], 16))
    return rng.sample(sorted(names), k)


def module_shas() -> dict:
    d = os.path.dirname(os.path.abspath(__file__))
    return {m: hashlib.sha256(open(os.path.join(d, f"{m}.py"), "rb").read()).hexdigest()
            for m in ("tug_of_war", "eod_reversal")}


def write(name: str, obj) -> str:
    os.makedirs(OUT_DIR, exist_ok=True)
    p = os.path.join(OUT_DIR, name)
    json.dump(obj, open(p + ".tmp", "w"), indent=1, default=str)
    os.replace(p + ".tmp", p)
    return p


def precheck() -> dict:
    """Amended (1): compare Alpaca daily bars with the warehouse on 20 seeded name-days (prices only)."""
    import indicator_confirm_hist as H
    from datetime import datetime, timedelta
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    s = json.load(open(os.path.join(ROOT, "secrets_data.json")))
    cl = StockHistoricalDataClient(s["alpaca_data_api_key"], s["alpaca_data_secret_key"])
    days = [d for d in H.warehouse_days() if d <= LAST]
    rng = random.Random(int(hashlib.sha256(b"tug-precheck").hexdigest()[:16], 16))
    gaps, picks = [], []
    for day in rng.sample(days, 20):
        f = day_fields(day)
        sym = rng.choice(sorted(s_ for s_, v in f.items() if v[0] and v[1]))
        d0 = datetime.fromisoformat(day)
        b = cl.get_stock_bars(StockBarsRequest(symbol_or_symbols=sym, timeframe=TimeFrame.Day, start=d0,
                                               end=d0 + timedelta(days=1), feed=DataFeed.SIP,
                                               adjustment=Adjustment.SPLIT)).data.get(sym, [])
        if not b:
            continue
        wo, wc = f[sym][0], f[sym][1]
        gaps += [abs(b[0].open / wo - 1), abs(b[0].close / wc - 1)]
        picks.append((day, sym))
    med = statistics.median(gaps) if gaps else None
    res = {"pairs": picks, "median_abs_gap": med, "source": "alpaca_daily" if med is not None and med <= GAP_LIMIT
           else "warehouse", "note": "warehouse open/close used for INTRA/OVN in either case when the alpaca source "
                                     "is not fetched; see amended_2 (4)"}
    write("precheck.json", res)
    return res


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--precheck", action="store_true")
    ap.add_argument("--score", action="store_true")
    ap.add_argument("--skeptic-ready", default="")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--prereg-commit", default="")
    a = ap.parse_args()
    if a.precheck:
        print(json.dumps(precheck(), indent=1))
        return
    pc_path = os.path.join(OUT_DIR, "precheck.json")
    if not os.path.exists(pc_path):
        raise SystemExit("run --precheck first")
    import indicator_confirm_hist as H
    days = [d for d in H.warehouse_days() if d <= LAST]
    cache = os.path.join(OUT_DIR, "days.json")
    if os.path.exists(cache) and json.load(open(cache)).get("module_sha256") == module_shas():
        c = json.load(open(cache))
        summ, trades, st = c["days"], c["trades"], Counter(c["stats"])
    else:
        summ, trades, st = [], [], Counter()
        hist: dict = defaultdict(list)
        prev_close: dict = {}
        with ProcessPoolExecutor(max_workers=a.workers) as ex:
            for cur, (day, f) in enumerate(zip(days, ex.map(day_fields, days, chunksize=2))):
                if day >= FIRST:
                    elig = []
                    for s, (o, c, eo, ret) in f.items():
                        if eo is None:
                            st["skip_no_0945"] += 1
                            continue
                        if eo < MIN_PX:
                            st["skip_below_10"] += 1
                            continue
                        sc = score(hist[s], cur)
                        if sc is None:
                            st["no_score"] += 1
                            continue
                        sc60 = score(hist[s], cur, INFO_LB, 40)
                        elig.append((s, sc[0], sc[1], -sc[2], ret, sc60[0] if sc60 else None))
                    if len(elig) >= MIN_ELIGIBLE:
                        allm = statistics.mean(e[4] for e in elig)
                        top = pick(elig, 1)
                        pl = placebo([e[0] for e in elig], len(top), day)
                        rets = {e[0]: e[4] for e in elig}
                        e60 = [e for e in elig if e[5] is not None]
                        summ.append({"day": day, "lift": statistics.mean(e[4] for e in top) - allm,
                                     "los": statistics.mean(e[4] for e in top), "k": len(top),
                                     "placebo_lift": statistics.mean(rets[s] for s in pl) - allm,
                                     "intra_lift": statistics.mean(e[4] for e in pick(elig, 2)) - allm,
                                     "low_ovn_lift": statistics.mean(e[4] for e in pick(elig, 3)) - allm,
                                     "bottom_lift": statistics.mean(e[4] for e in pick(elig, 1, False)) - allm,
                                     "lb60_lift": (statistics.mean(e[4] for e in pick(e60, 5)) - statistics.mean(
                                         e[4] for e in e60)) if len(e60) >= MIN_ELIGIBLE else None})
                        trades += [(day, e[0], e[4] - allm) for e in top]
                    else:
                        st["day_skipped_few_names"] += 1
                for s, (o, c, eo, ret) in f.items():       # today's open/close enter history AFTER today is scored
                    hist[s].append((cur, o, c, prev_close.get(s)))
                    if len(hist[s]) > INFO_LB + 5:
                        hist[s].pop(0)
                    if c:
                        prev_close[s] = c
                    else:
                        prev_close.pop(s, None)
                for s in list(prev_close):
                    if s not in f:
                        prev_close.pop(s)                  # absent today: no prior close for tomorrow's overnight
        write("days.json", {"days": summ, "trades": trades, "stats": dict(st), "module_sha256": module_shas()})
    halves = {"A": [d for d in summ if d["day"] <= HALF_A_END], "B": [d for d in summ if d["day"] > HALF_A_END]}
    if not a.score:
        cnt = {"stats": dict(st), "module_sha256": module_shas(), "prereg_commit": a.prereg_commit,
               "precheck": json.load(open(pc_path))}
        for h, ds in halves.items():
            pl = [d["placebo_lift"] for d in ds]
            sd = statistics.stdev(pl) if len(pl) >= 2 else None
            mde = (T_PLACEBO + 0.84) * sd / math.sqrt(len(pl)) if sd else None
            n = sum(d["k"] for d in ds)
            cnt[h] = {"days": len(ds), "long_trades": n, "mde": mde,
                      "floors_met": n >= FLOOR_TRADES and len(ds) >= FLOOR_DAYS and mde is not None and mde <= LIFT_BAR}
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
    res = {"halves": {}, "info": {}}
    hv, ss = [], []
    for h, ds in halves.items():
        s = E.stats([d["lift"] for d in ds])
        net = E.stats([d["los"] - COST for d in ds])
        tw = two_way_t([t for t in trades if (t[0] <= HALF_A_END) == (h == "A")])
        if s["t"] is not None and s["t"] <= -2:
            v = "FAIL"
        elif (s["mean"] >= LIFT_BAR and (s["t"] or 0) >= T_PASS and net["mean"] > 0 and tw is not None
              and tw >= T_PASS):
            v = "PASS"
        else:
            v = "NOT_PASSED"
        hv.append(v)
        ss.append(s)
        res["halves"][h] = {"verdict": v, **s, "two_way_t": tw, "net_mean": net["mean"], "net_t": net["t"],
                            "batch_t_met": min(s["t"] or 0, tw or 0) >= T_BATCH,
                            "net_by_cost": {f"{round(c * 1e4)}bp": E.stats([d["los"] - c for d in ds])["mean"]
                                            for c in COSTS}}
        res["info"][h] = {k: E.stats([d[k] for d in ds])["mean"] for k in
                          ("intra_lift", "low_ovn_lift", "bottom_lift", "lb60_lift", "placebo_lift")}
    if "FAIL" in hv:
        verdict = "FAIL"
    elif all(x == "PASS" for x in hv):
        verdict = "PASS (batch-robust)" if all(res["halves"][h]["batch_t_met"] for h in ("A", "B")) else \
            "PASS (single-test), not batch-robust"
    elif all(x["ub"] is not None and x["ub"] < LIFT_BAR for x in ss):
        verdict = "FAIL"
    else:
        verdict = "UNDERPOWERED"
    res["verdict"] = verdict
    res["declared"] = ["INTRA/OVN built from warehouse 09:30 opens and 15:59 bar closes (not official auctions); "
                       "the precheck reports the gap to Alpaca daily bars", "lb60 needs 40 of 60 sessions: "
                       "incomplete early (information)", "half A is 2026-05-26 .. 07-29 (amended_2)"]
    res["skeptic_ready"], res["module_sha256"] = a.skeptic_ready, module_shas()
    write("result.json", res)
    print(json.dumps(res, indent=1, default=str))


if __name__ == "__main__":
    main()
