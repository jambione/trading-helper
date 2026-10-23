#!/usr/bin/env python3
"""Premarket-level power bar on SPY + QQQ. Spec: docs/studies/pm_power_bar_prereg.json (amended, amended_2; skeptic
READY r2). PMH / PML from 07:00-09:29 1-minute bars (>= 30 bars). LONG: the first 2-minute power bar ending by 10:29
whose low is within 0.15% of PML, closes above PML, with no RTH 1-minute close below PML before it. Entry next
1-minute open; stop = bar low - $0.01; target = PMH; else 15:59. SHORT mirrored (information).

  (default) count step (outcome-blind); --score after a result-skeptic READY
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import etf_bars as B  # noqa: E402

OUT_DIR = os.path.join(B.ROOT, "ai_reports", "pm_power_bar")
SYMS = ("SPY", "QQQ")
LAST_END, BAND, MIN_PM_BARS, MIN_PM_RANGE = 629, 0.0015, 30, 0.001
COST, COSTS, FLOOR_DAYS = 0.0002, (0.0001, 0.0002, 0.0005, 0.0010), 150
LOOSE_RANGE, LOOSE_BODY, MAX_COMBINE = 1.5, 0.5, 3          # amended_5 primary power bar


def levels(r_all, lo_min: int, wicks: bool = False):
    """PMH / PML from candle BODIES (amended_4: single-print wicks set ~1 in 4 levels); wicks=True is information."""
    pm = r_all[(r_all[:, 0] >= lo_min) & (r_all[:, 0] < B.RTH0)]
    if lo_min == 420 and len(pm) < MIN_PM_BARS:
        return None
    if not len(pm):
        return None
    if wicks:
        return float(pm[:, 2].max()), float(pm[:, 3].min())
    return float(np.maximum(pm[:, 1], pm[:, 4]).max()), float(np.minimum(pm[:, 1], pm[:, 4]).min())


def candidate(r, bars2, size, pmh, pml, side):
    """First qualifying power bar for a side, or None."""
    level = pml if side > 0 else pmh
    for k, b in enumerate(bars2):
        if b is None:
            continue
        end = b[4]
        if end > LAST_END:
            return None
        start = end - 1
        before = r[r[:, 0] < start]
        broken = (before[:, 4] < level).any() if side > 0 else (before[:, 4] > level).any()
        if broken:
            return None                           # level broken in RTH before the candidate: no trade this side
        near = abs((b[2] if side > 0 else b[1]) - level) <= BAND * level
        away = b[3] > level if side > 0 else b[3] < level
        if near and away and B.power_bar(b, size.ref(k), side):
            return {"k": k, "end": end, "low": b[2], "high": b[1]}
    return None


def combine(bars2: list, k: int, n: int):
    """n consecutive present 2-minute bars ending at k -> (open, high, low, close, end), else None."""
    if k - n + 1 < 0:
        return None
    w = bars2[k - n + 1:k + 1]
    if any(b is None for b in w):
        return None
    return (w[0][0], max(b[1] for b in w), min(b[2] for b in w), w[-1][3], w[-1][4])


def combos(bars2: list, n: int) -> list:
    return [combine(bars2, k, n) for k in range(len(bars2))]


def loose_candidate(r, bars2, refs, pmh, pml, side):
    """amended_5: the first end bar k (by 10:29) where a 1-3 bar window qualifies; the smallest n at that k."""
    level = pml if side > 0 else pmh
    for k, b in enumerate(bars2):
        if b is None:
            continue
        if b[4] > LAST_END:
            return None
        for n in range(1, MAX_COMBINE + 1):
            cb = combine(bars2, k, n)
            if cb is None:
                continue
            o, h, lo, c, end = cb
            start = end - 2 * n + 1
            before = r[r[:, 0] < start]
            if (before[:, 4] < level).any() if side > 0 else (before[:, 4] > level).any():
                continue                                       # level broken before this window starts
            ref = refs[n].ref(k)
            rng = h - lo
            if ref is None or rng <= 0 or rng < LOOSE_RANGE * ref or abs(c - o) < LOOSE_BODY * rng:
                continue
            if not ((c > o) if side > 0 else (c < o)):
                continue
            near = abs((lo if side > 0 else h) - level) <= BAND * level
            away = c > level if side > 0 else c < level
            if near and away:
                return {"k": k, "n": n, "end": end, "low": lo, "high": h}
    return None


def trade(r, sig, side, pmh, pml, sym, day, time_exit=None):
    e = B.first_after(r, sig["end"])
    if e is None:
        return {"skip": "no_entry_bar"}
    o = float(r[e, 1])
    stop = sig["low"] - B.TICK if side > 0 else sig["high"] + B.TICK
    tgt = pmh if side > 0 else pml
    if (o - stop) * side <= 0 or (tgt - o) * side <= 0:
        return {"skip": "open_beyond_stop_or_target"}
    rr = r if time_exit is None else r[r[:, 0] <= time_exit]       # a prefix: indices align with r
    if e >= len(rr):
        return {"skip": "no_room_before_time_exit"}
    res = B.simulate(rr, e, side, stop, tgt)
    sp, tp = abs(o - stop) / o, abs(tgt - o) / o
    c1 = B.control(r, sym, day, sig["end"], sig["end"] + 60, side, sp, tp, "pmpb-ctrl")
    c2 = B.control(r, sym, day, sig["end"], sig["end"] + 60, side, sp, tp, "pmpb-ctrl2")
    return {**res, "ctrl": c1["gross"] if c1 else None, "ctrl2": c2["gross"] if c2 else None}


def run_symbol(sym):
    rows, st = [], Counter()
    size = B.SizeRef()
    refs = {n: B.SizeRef() for n in range(1, MAX_COMBINE + 1)}
    restarted = False
    for day, r_all in B.load(sym).items():
        if not B.in_scope(day):
            st["out_of_scope_day"] += 1
            continue
        if day >= B.B_RESTART and not restarted:
            size.reset()
            for ref in refs.values():
                ref.reset()
            restarted = True
        if not B.is_full_session(r_all, day):
            st["early_close_dropped"] += 1
            continue
        r = B.rth(r_all)
        bars2 = B.clock_bars(r, 2, 2)
        for lab_lv, lo_min, wk in (("primary", 420, False), ("strict", 420, False), ("lv0400", 240, False),
                                   ("wicks", 420, True)):
            lv = levels(r_all, lo_min, wk)
            if lv is None:
                st[f"{lab_lv}_no_premarket"] += 1
                continue
            pmh, pml = lv
            if pmh - pml < MIN_PM_RANGE * pml:
                st[f"{lab_lv}_narrow_pm_range"] += 1
                continue
            if lab_lv == "strict":
                sigs = {sd: candidate(r, bars2, size, pmh, pml, sd) for sd in (1, -1)}
            else:
                sigs = {sd: loose_candidate(r, bars2, refs, pmh, pml, sd) for sd in (1, -1)}
            for side, lab in ((1, "long"), (-1, "short")):
                sig = sigs[side]
                if sig is None:
                    continue
                if side < 0 and sigs[1] is not None and sigs[1]["end"] <= sig["end"]:
                    st[f"{lab_lv}_short_not_first"] += 1      # information short set: first side only (amended (2))
                    continue
                t = trade(r, sig, side, pmh, pml, sym, day)
                if "skip" in t:
                    st[f"{lab_lv}_{lab}_skip_{t['skip']}"] += 1
                    continue
                base = {"day": day, "sym": sym, "side": lab, "half": B.half_of(day), "end": sig["end"],
                        "n_bars": sig.get("n", 1)}
                rows.append({**base, **t, "variant": lab_lv})
                if lab_lv == "primary":
                    t2 = trade(r, sig, side, pmh, pml, sym, day, time_exit=629)
                    if "skip" not in t2:
                        rows.append({**base, **t2, "variant": "exit_1030"})
        size.add(bars2)                       # today's ranges enter the reference only after today is scanned
        for n, ref in refs.items():
            ref.add(combos(bars2, n))
    return rows, st


def module_shas():
    d = os.path.dirname(os.path.abspath(__file__))
    return {m: hashlib.sha256(open(os.path.join(d, f"{m}.py"), "rb").read()).hexdigest()
            for m in ("pm_power_bar", "etf_bars")}


def write(name, obj):
    os.makedirs(OUT_DIR, exist_ok=True)
    p = os.path.join(OUT_DIR, name)
    json.dump(obj, open(p + ".tmp", "w"), indent=1, default=str)
    os.replace(p + ".tmp", p)
    return p


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--score", action="store_true")
    ap.add_argument("--skeptic-ready", default="")
    ap.add_argument("--prereg-commit", default="")
    a = ap.parse_args()
    if json.load(open(os.path.join(B.CACHE, "fetch_log.json"))).get("failed"):
        raise SystemExit("fetch has failures")
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
    if any(k.endswith("out_of_scope_day") and v for k, v in st.items()):
        raise SystemExit("held-out or post-window days present in the cache")
    if not a.score:
        cnt = {"stats": dict(st), "module_sha256": module_shas(), "prereg_commit": a.prereg_commit}
        for h in ("A", "B"):
            hr = [x for x in prim if x["half"] == h]
            m = B.mde(hr)
            days = len({x["day"] for x in hr})
            cnt[h] = {"long_trades": len(hr), "long_days": days, "mde": m,
                      "floors_met": days >= FLOOR_DAYS and m is not None and m <= 0.0005}
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
    res = {"primary_long": B.primary_verdict(prim, COST, floor_days=FLOOR_DAYS), "info": {}}
    for lab, sel in (("short", lambda x: x["variant"] == "primary" and x["side"] == "short"),
                     ("both_sides", lambda x: x["variant"] == "primary"),
                     ("levels_from_0400_long", lambda x: x["variant"] == "lv0400" and x["side"] == "long"),
                     ("wick_levels_long", lambda x: x["variant"] == "wicks" and x["side"] == "long"),
                     ("strict_single_bar_long", lambda x: x["variant"] == "strict" and x["side"] == "long"),
                     ("exit_1030_long", lambda x: x["variant"] == "exit_1030" and x["side"] == "long")):
        sub = [x for x in rows if sel(x)]
        res["info"][lab] = {h: {"net": B.day_stats([x for x in sub if x["half"] == h], lambda x: x["gross"] - COST)
                                ["mean"], "n": sum(1 for x in sub if x["half"] == h)} for h in ("A", "B")}
    for h in ("A", "B"):
        hr = [x for x in prim if x["half"] == h]
        res["info"][f"win_rate_{h}"] = sum(1 for x in hr if x["gross"] > 0) / max(1, len(hr))
        res["info"][f"target_before_stop_{h}"] = sum(1 for x in hr if x["why"] == "target") / max(1, len(hr))
        res["info"][f"exit_mix_{h}"] = dict(Counter(x["why"] for x in hr))
        res["info"][f"net_by_cost_{h}"] = {f"{round(c * 1e4)}bp": B.day_stats(hr, lambda x, c=c: x["gross"] - c)
                                           ["mean"] for c in COSTS}
    res["verdict"] = res["primary_long"]["verdict"]
    res["declared"] = ["combined 2-3-bar power bars and the R-multiple cell not built (information, dropped before "
                       "scoring)",
                       "the long set is every long trigger regardless of the short side"]
    res["skeptic_ready"], res["module_sha256"] = a.skeptic_ready, module_shas()
    write("result.json", res)
    print(json.dumps(res, indent=1, default=str))


if __name__ == "__main__":
    main()
