#!/usr/bin/env python3
"""End-of-day reversal (Baltussen, Da & Soebhag 2025), long-only. Spec: docs/studies/eod_reversal_prereg.json (as
amended; common C1-C4).

Each warehouse day: ROD3 = 14:59 close / prior session's 15:59 close - 1 for every liquid name with a tradable 15:30
bar (open >= $10); LOSERS = the bottom decile. Buy the 15:30 open, sell at the last close at or before 15:59.
LIFT = loser mean minus the all-eligible mean over the same window; per day; day-equal; day-clustered t.

  (default) count step: eligible names, losers and days per half; MDE from a seeded random-decile placebo (C1)
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

ROOT = os.environ.get("EEH_ROOT") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT_DIR = os.path.join(ROOT, "ai_reports", "eod_reversal")
FIRST, LAST, HALF_A_END = "2026-05-11", "2026-10-15", "2026-07-29"
M_SIG, M_SIG29, M_IN, M_OUT, M_OUT58, M_OPEN = 899, 929, 930, 959, 958, 570
MIN_PX, INFO_PX = 10.0, 20.0
COST, COSTS = 0.0005, (0.0003, 0.0005, 0.0010, 0.0020)
LIFT_BAR, T_PASS, T_NET, T_BATCH, T_PLACEBO = 0.0005, 2.0, 1.0, 2.73, 2.0
FLOOR_TRADES, FLOOR_DAYS, MIN_ELIGIBLE = 300, 20, 20
DECILE, QUINTILE = 0.10, 0.20


# ── pure helpers ─────────────────────────────────────────────────────────────

def bar_at(r, m: int):
    """The row whose minute == m, else None."""
    import numpy as np
    k = int(np.searchsorted(r[:, 0], m))
    return r[k] if k < len(r) and int(r[k, 0]) == m else None


def last_close_at_or_before(r, m: int) -> float | None:
    import numpy as np
    k = int(np.searchsorted(r[:, 0], m, side="right")) - 1
    return float(r[k, 4]) if k >= 0 else None


def name_row(sym: str, today, prev) -> tuple[dict | None, str | None]:
    """One name-day's signal and outcome fields, or (None, skip reason). Eligibility uses no bar after 15:30 (C2)."""
    if prev is None:
        return None, "no_prior_session"
    pc = bar_at(prev, M_OUT)
    if pc is None:
        return None, "no_prior_1559"
    sig = bar_at(today, M_SIG)
    if sig is None:
        return None, "no_1459"
    ent = bar_at(today, M_IN)
    if ent is None:
        return None, "no_1530"
    px = float(ent[1])
    if px < MIN_PX:
        return None, "below_10"
    prior_close = float(pc[4])
    opn = bar_at(today, M_OPEN)
    out = {"s": sym, "rod3": float(sig[4]) / prior_close - 1, "px": px,
           "rod29": (last_close_at_or_before(today, M_SIG29) or float(sig[4])) / prior_close - 1,
           "ovn": float(opn[1]) / prior_close - 1 if opn is not None else None,
           "lh": last_close_at_or_before(today, M_OUT) / px - 1,
           "lh58": last_close_at_or_before(today, M_OUT58) / px - 1}
    return out, None


def bottom(names: list[dict], frac: float, key: str = "rod3") -> list[dict]:
    """The lowest `frac` of names by key (ties by symbol); at least one name."""
    k = max(1, int(len(names) * frac))
    return sorted(names, key=lambda r: (r[key], r["s"]))[:k]


def top(names: list[dict], frac: float, key: str = "rod3") -> list[dict]:
    k = max(1, int(len(names) * frac))
    return sorted(names, key=lambda r: (-r[key], r["s"]))[:k]


def placebo(names: list[dict], k: int, day: str) -> list[dict]:
    rng = random.Random(int(hashlib.sha256(f"eod-placebo|{day}".encode()).hexdigest()[:16], 16))
    return rng.sample(sorted(names, key=lambda r: r["s"]), k)


def mean_lh(rs: list[dict], key: str = "lh") -> float | None:
    xs = [r[key] for r in rs if r.get(key) is not None]
    return statistics.mean(xs) if xs else None


# ── per day ──────────────────────────────────────────────────────────────────

def day_task(args) -> dict:
    day, prev_day = args
    import indicator_confirm_hist as H
    today = H.load_raw("sip", day)
    prev = H.load_raw("sip", prev_day) if prev_day else {}
    st, names = Counter(), []
    for sym, r in today.items():
        row, why = name_row(sym, r, prev.get(sym))
        if row is None:
            st[f"skip_{why}"] += 1
        else:
            names.append(row)
    spy = next((n for n in names if n["s"] == "SPY"), None)
    if spy is None and "SPY" in today and "SPY" in prev:
        spy, _ = name_row("SPY", today["SPY"], prev["SPY"])
    if len(names) < MIN_ELIGIBLE:
        st["day_skipped_few_names"] += 1
        return {"day": day, "names": [], "stats": dict(st), "spy_lh": spy["lh"] if spy else None}
    return {"day": day, "names": names, "stats": dict(st), "spy_lh": spy["lh"] if spy else None}


def day_summary(d: dict) -> dict | None:
    names = d["names"]
    if not names:
        return None
    los, win = bottom(names, DECILE), top(names, DECILE)
    allm = mean_lh(names)
    pl = placebo(names, len(los), d["day"])
    return {"day": d["day"], "n": len(names), "k": len(los), "all": allm, "los": mean_lh(los),
            "lift": mean_lh(los) - allm, "placebo_lift": mean_lh(pl) - allm, "spy_lh": d["spy_lh"],
            "los20": mean_lh([r for r in los if r["px"] >= INFO_PX]),
            "all20": mean_lh([r for r in names if r["px"] >= INFO_PX]),
            "q_lift": mean_lh(bottom(names, QUINTILE)) - allm,
            "win_lift": mean_lh(win) - allm,
            "lift29": mean_lh(bottom(names, DECILE, "rod29")) - allm,
            "lift58": mean_lh(los, "lh58") - mean_lh(names, "lh58"),
            "lift_ovn_rank": (mean_lh(bottom([r for r in names if r["ovn"] is not None], DECILE, "ovn"))
                              - allm) if any(r["ovn"] is not None for r in names) else None}


# ── statistics ───────────────────────────────────────────────────────────────

def t_ppf(p: float, df: int) -> float:
    import indicator_confirm_study as S
    return S.t_ppf(p, df)


def stats(xs: list[float]) -> dict:
    xs = [x for x in xs if x is not None]
    n = len(xs)
    if n < 2:
        return {"days": n, "mean": xs[0] if xs else None, "t": None, "ub": None}
    mu, se = statistics.mean(xs), statistics.stdev(xs) / math.sqrt(n)
    return {"days": n, "mean": mu, "t": mu / se if se else None,
            "ub": mu + t_ppf(0.975, n - 1) * se if se else None}


def drop3(days: list[dict]) -> float | None:
    keep = sorted(days, key=lambda d: d["lift"], reverse=True)[3:]
    return stats([d["lift"] for d in keep])["mean"]


def beta(days: list[dict]) -> dict:
    pts = [(d["spy_lh"], d["lift"]) for d in days if d["spy_lh"] is not None]
    if len(pts) < 3:
        return {"n": len(pts), "slope": None, "intercept": None}
    mx, my = statistics.mean(p[0] for p in pts), statistics.mean(p[1] for p in pts)
    sxx = sum((p[0] - mx) ** 2 for p in pts)
    slope = sum((p[0] - mx) * (p[1] - my) for p in pts) / sxx if sxx else None
    return {"n": len(pts), "slope": slope, "intercept": my - slope * mx if slope is not None else None}


def half_verdict(days: list[dict]) -> tuple[str, dict]:
    s = stats([d["lift"] for d in days])
    net = stats([d["los"] - COST for d in days])
    trades = sum(d["k"] for d in days)
    s.update(trades=trades, net_mean=net["mean"], net_t=net["t"], drop3=drop3(days))
    if s["t"] is not None and s["t"] <= -2:
        return "FAIL", s
    if trades < FLOOR_TRADES or s["days"] < FLOOR_DAYS or s["t"] is None:
        return "UNDERPOWERED", s
    ok = (s["mean"] >= LIFT_BAR and s["t"] >= T_PASS and net["mean"] is not None and net["mean"] > 0
          and (net["t"] or 0) >= T_NET and s["drop3"] is not None and s["drop3"] >= 0)
    return ("PASS" if ok else "NOT_PASSED"), s


def final(hv: list[str], ss: list[dict]) -> str:
    if "FAIL" in hv:
        return "FAIL"
    if all(h == "PASS" for h in hv):
        return "PASS (batch-robust)" if all((s["t"] or 0) >= T_BATCH for s in ss) else \
            "PASS (single-test), not batch-robust"
    if all(s["days"] >= 2 and s["ub"] is not None and s["ub"] < LIFT_BAR for s in ss):
        return "FAIL"
    return "UNDERPOWERED"


# ── io ───────────────────────────────────────────────────────────────────────

def module_shas() -> dict:
    return {"eod_reversal": hashlib.sha256(open(os.path.abspath(__file__), "rb").read()).hexdigest()}


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
    days = H.warehouse_days()
    run = [(d, days[i - 1] if i >= 1 else None) for i, d in enumerate(days) if FIRST <= d <= LAST]
    cache = os.path.join(OUT_DIR, "days.json")
    if os.path.exists(cache) and json.load(open(cache)).get("module_sha256") == module_shas():
        c = json.load(open(cache))
        summ, st = c["days"], Counter(c["stats"])
    else:
        summ, st = [], Counter()
        with ProcessPoolExecutor(max_workers=a.workers) as ex:
            for d in ex.map(day_task, run, chunksize=2):
                st.update(d["stats"])
                s = day_summary(d)
                if s:
                    summ.append(s)
        write("days.json", {"days": summ, "stats": dict(st), "module_sha256": module_shas()})
    halves = {"A": [d for d in summ if d["day"] <= HALF_A_END], "B": [d for d in summ if d["day"] > HALF_A_END]}
    if not a.score:
        cnt = {"stats": dict(st), "module_sha256": module_shas(), "prereg_commit": a.prereg_commit}
        for h, ds in halves.items():
            pl = [d["placebo_lift"] for d in ds]
            sd = statistics.stdev(pl) if len(pl) >= 2 else None
            mde = (T_PLACEBO + 0.84) * sd / math.sqrt(len(pl)) if sd else None
            trades = sum(d["k"] for d in ds)
            cnt[h] = {"days": len(ds), "losers": trades, "eligible_median": statistics.median(d["n"] for d in ds)
                      if ds else None, "spy_days": sum(1 for d in ds if d["spy_lh"] is not None), "mde": mde,
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
        if not cnt[h]["floors_met"]:
            v, s = "UNDERPOWERED", stats([d["lift"] for d in ds])
        else:
            v, s = half_verdict(ds)
        hv.append(v)
        ss.append(s)
        res["halves"][h] = {"verdict": v, **s,
                            "loser_net_by_cost": {f"{round(c * 1e4)}bp": stats([d["los"] - c for d in ds])["mean"]
                                                  for c in COSTS},
                            "batch_t_met": (s.get("t") or 0) >= T_BATCH}
        res["info"][h] = {k: stats([d[k] for d in ds])["mean"] for k in
                          ("q_lift", "win_lift", "lift29", "lift58", "lift_ovn_rank", "placebo_lift")}
        res["info"][h]["price_ge_20_lift"] = stats([d["los20"] - d["all20"] for d in ds
                                                    if d["los20"] is not None and d["all20"] is not None])["mean"]
        res["info"][h]["beta_vs_spy_lh"] = beta(ds)
        for lab, sub in (("spy_up_day", [d for d in ds if (d["spy_lh"] or 0) > 0]),
                         ("spy_down_day", [d for d in ds if (d["spy_lh"] or 0) <= 0])):
            res["info"][h][lab] = stats([d["lift"] for d in sub])
    res["verdict"] = final(hv, ss)
    res["skeptic_ready"], res["module_sha256"] = a.skeptic_ready, module_shas()
    res["declared"] = "SPY up/down split uses SPY's 15:30-15:59 move (information only, not knowable at entry)."
    write("result.json", res)
    print(json.dumps(res, indent=1, default=str))


if __name__ == "__main__":
    main()
