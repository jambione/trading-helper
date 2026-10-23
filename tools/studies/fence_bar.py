#!/usr/bin/env python3
"""Fence-bar opening retest on SPY + QQQ. Spec: docs/studies/fence_bar_prereg.json (as amended; skeptic READY r2).

Fence = high/low of the 09:30-09:34 five-minute bar. LONG: a 5-minute close above TOP arms; a later close at or below
TOP cancels (a new close above re-arms); the RETEST is the first armed bar that opens above TOP, trades down to TOP and
closes above it, ending by 10:59. Filter: SMA20 of 5-minute closes (continuous across kept sessions, restarted
2026-04-27) at the retest >= one bar earlier. Entry: the next 1-minute open. Stop MID (50% of the fence), target 2R,
else the 15:59 close. SHORT mirrored (information). Reading of 'first retest': the first retest bar decides that side
for the day; if it fails the SMA filter there is no trade on that side (declared).

  (default) count step: long trades, days per half, MDE from control - control2 (outcome-blind)
  --score   verdict; refuses without --skeptic-ready and a count step from this code
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import sys
from collections import Counter, deque

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import etf_bars as B  # noqa: E402

OUT_DIR = os.path.join(B.ROOT, "ai_reports", "fence_bar")
SYMS = ("SPY", "QQQ")
LAST_RETEST_END = 659          # 10:55-10:59 bar
COST, COSTS = 0.0002, (0.0001, 0.0002, 0.0005, 0.0010)
R_MULT, INFO_R = 2.0, 1.5


def find_trade(bars5: list, sma_at: list, side: int) -> dict | None:
    """bars5[k] = (o, h, l, c, end) or None; sma_at[k] = SMA20 through bar k (None without 20 bars)."""
    if bars5[0] is None:
        return None
    top, bot = bars5[0][1], bars5[0][2]
    rail = top if side > 0 else bot
    armed = False
    for k in range(1, len(bars5)):
        b = bars5[k]
        if b is None:
            continue
        o, h, lo, c, end = b
        if end > LAST_RETEST_END:
            return None
        outside = c > rail if side > 0 else c < rail
        if not armed:
            armed = outside
            continue
        if not outside:
            armed = False
            continue
        retest = (o > rail and lo <= rail) if side > 0 else (o < rail and h >= rail)
        if retest:
            ok = (sma_at[k] is not None and sma_at[k - 1] is not None
                  and (sma_at[k] >= sma_at[k - 1] if side > 0 else sma_at[k] <= sma_at[k - 1]))
            return {"k": k, "end": end, "top": top, "bot": bot, "mid": (top + bot) / 2, "sma_ok": ok,
                    "sma": sma_at[k], "close": c, "rt_low": lo, "rt_high": h}
    return None


def trade(r, sig: dict, side: int, sym: str, day: str, r_mult: float = R_MULT, stop: float | None = None) -> dict:
    e = B.first_after(r, sig["end"])
    if e is None:
        return {"skip": "no_entry_bar"}
    o = float(r[e, 1])
    stop = sig["mid"] if stop is None else stop
    risk = (o - stop) * side
    if risk <= 0:
        return {"skip": "open_beyond_stop"}
    tgt = o + side * r_mult * risk
    res = B.simulate(r, e, side, stop, tgt)
    sp, tp = risk / o, r_mult * risk / o
    c1 = B.control(r, sym, day, sig["end"], sig["end"] + 60, side, sp, tp, "fence-ctrl")
    c2 = B.control(r, sym, day, sig["end"], sig["end"] + 60, side, sp, tp, "fence-ctrl2")
    return {**res, "entry_min": int(r[e, 0]), "stop_pct": sp, "ctrl": c1["gross"] if c1 else None,
            "ctrl2": c2["gross"] if c2 else None}


def run_symbol(sym: str) -> tuple[list[dict], Counter]:
    days = B.load(sym)
    rows, st = [], Counter()
    closes: deque = deque(maxlen=20)
    restarted = False
    for day, r_all in days.items():
        if day >= B.B_RESTART and not restarted:          # SMA restarts after the held-out gap (amended (1))
            closes.clear()
            restarted = True
        if not B.is_full_session(r_all):
            st["early_close_dropped"] += 1
            continue
        r = B.rth(r_all)
        bars5 = B.clock_bars(r, 5, 4)
        sma_at = []
        for b in bars5:
            if b is not None:
                closes.append(b[3])
            sma_at.append(statistics.mean(closes) if len(closes) == 20 else None)
        for side, lab in ((1, "long"), (-1, "short")):
            sig = find_trade(bars5, sma_at, side)
            if sig is None:
                continue
            st[f"{lab}_retest"] += 1
            info = {"day": day, "sym": sym, "side": lab, "half": B.half_of(day),
                    "far": abs(sig["close"] - sig["sma"]) / sig["sma"] if sig["sma"] else None}
            if not sig["sma_ok"]:
                st[f"{lab}_sma_filtered"] += 1
                t0 = trade(r, sig, side, sym, day)
                if "skip" not in t0:
                    rows.append({**info, **t0, "variant": "nofilter_only"})
                continue
            t = trade(r, sig, side, sym, day)
            if "skip" in t:
                st[f"{lab}_skip_{t['skip']}"] += 1
                continue
            rows.append({**info, **t, "variant": "primary"})
            for var, kw in (("r15", {"r_mult": INFO_R}),
                            ("stop_retest", {"stop": sig["rt_low"] - B.TICK if side > 0 else sig["rt_high"] + B.TICK})):
                tv = trade(r, sig, side, sym, day, **kw)
                if "skip" not in tv:
                    rows.append({**info, **tv, "variant": var})
        # information: plain ORB, first 5-minute close outside, same MID / 2R bracket
        for side, lab in ((1, "long"), (-1, "short")):
            if bars5[0] is None:
                break
            top, bot = bars5[0][1], bars5[0][2]
            for k in range(1, len(bars5)):
                b = bars5[k]
                if b is None or b[4] > LAST_RETEST_END:
                    continue
                if (b[3] > top) if side > 0 else (b[3] < bot):
                    sig = {"end": b[4], "mid": (top + bot) / 2}
                    t = trade(r, sig, side, sym, day)
                    if "skip" not in t:
                        rows.append({"day": day, "sym": sym, "side": lab, "half": B.half_of(day), **t,
                                     "variant": "plain_orb"})
                    break
    return rows, st


def module_shas() -> dict:
    d = os.path.dirname(os.path.abspath(__file__))
    return {m: hashlib.sha256(open(os.path.join(d, f"{m}.py"), "rb").read()).hexdigest()
            for m in ("fence_bar", "etf_bars")}


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
    ap.add_argument("--prereg-commit", default="")
    a = ap.parse_args()
    log = json.load(open(os.path.join(B.CACHE, "fetch_log.json")))
    if log.get("failed"):
        raise SystemExit(f"fetch has failures: {log['failed']}")
    cache = os.path.join(OUT_DIR, "rows.json")
    if os.path.exists(cache) and json.load(open(cache)).get("module_sha256") == module_shas():
        c = json.load(open(cache))
        rows, st = c["rows"], Counter(c["stats"])
    else:
        rows, st = [], Counter()
        for sym in SYMS:
            rs, s = run_symbol(sym)
            rows += rs
            st.update({f"{sym}_{k}": v for k, v in s.items()})
        write("rows.json", {"rows": rows, "stats": dict(st), "module_sha256": module_shas()})
    prim = [x for x in rows if x["variant"] == "primary" and x["side"] == "long"]
    if not a.score:
        cnt = {"stats": dict(st), "module_sha256": module_shas(), "prereg_commit": a.prereg_commit}
        for h in ("A", "B"):
            hr = [x for x in prim if x["half"] == h]
            days = len({x["day"] for x in hr})
            m = B.mde(hr)
            cnt[h] = {"long_trades": len(hr), "long_days": days, "with_control": sum(1 for x in hr if x["ctrl"]
                                                                                     is not None),
                      "mde": m, "floors_met": days >= 300 and m is not None and m <= 0.0005}
        print(json.dumps(cnt, indent=1, default=str))
        print(write("count.json", cnt))
        return
    if not a.skeptic_ready:
        raise SystemExit("--score needs --skeptic-ready")
    cnt = json.load(open(os.path.join(OUT_DIR, "count.json")))
    if cnt.get("module_sha256") != module_shas() or not cnt.get("prereg_commit"):
        raise SystemExit("count.json from different code or without --prereg-commit")
    res = {"primary_long": B.primary_verdict(prim, COST), "info": {}}
    for lab, sel in (("short", lambda x: x["variant"] == "primary" and x["side"] == "short"),
                     ("both_sides", lambda x: x["variant"] == "primary"),
                     ("no_sma_filter_long", lambda x: x["variant"] in ("primary", "nofilter_only")
                      and x["side"] == "long"),
                     ("stop_at_retest_long", lambda x: x["variant"] == "stop_retest" and x["side"] == "long"),
                     ("r15_long", lambda x: x["variant"] == "r15" and x["side"] == "long"),
                     ("plain_orb_long", lambda x: x["variant"] == "plain_orb" and x["side"] == "long")):
        sub = [x for x in rows if sel(x)]
        res["info"][lab] = {h: {"net": B.day_stats([x for x in sub if x["half"] == h], lambda x: x["gross"] - COST)
                                ["mean"],
                                "lift": B.day_stats([x for x in sub if x["half"] == h],
                                                    lambda x: x["gross"] - x["ctrl"] if x["ctrl"] is not None
                                                    else None)["mean"], "n": sum(1 for x in sub if x["half"] == h)}
                            for h in ("A", "B")}
    far = sorted(x["far"] for x in prim if x["far"] is not None)
    med = far[len(far) // 2] if far else None
    for h in ("A", "B"):
        hr = [x for x in prim if x["half"] == h]
        res["info"][f"win_rate_{h}"] = sum(1 for x in hr if x["gross"] > 0) / max(1, len(hr))
        res["info"][f"exit_mix_{h}"] = dict(Counter(x["why"] for x in hr))
        res["info"][f"near_anchor_net_{h}"] = B.day_stats([x for x in hr if med and x["far"] <= med],
                                                          lambda x: x["gross"] - COST)["mean"]
        res["info"][f"far_anchor_net_{h}"] = B.day_stats([x for x in hr if med and x["far"] > med],
                                                         lambda x: x["gross"] - COST)["mean"]
        res["info"][f"net_by_cost_{h}"] = {f"{round(c * 1e4)}bp": B.day_stats(hr, lambda x, c=c: x["gross"] - c)
                                           ["mean"] for c in COSTS}
    res["verdict"] = res["primary_long"]["verdict"]
    res["declared"] = ["warehouse-stock information read not built (declared dropped before scoring)",
                       "the first retest bar decides a side for the day; a failed SMA filter means no trade"]
    res["skeptic_ready"], res["module_sha256"] = a.skeptic_ready, module_shas()
    write("result.json", res)
    print(json.dumps(res, indent=1, default=str))


if __name__ == "__main__":
    main()
