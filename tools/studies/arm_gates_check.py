#!/usr/bin/env python3
"""arm_gates_check - do the live %R trend gate (A) and the 5-minute slow %R veto (B) avoid bad buys or only cost opens?
Prereg: docs/studies/arm_gates_check_prereg.json.

    .venv/bin/python tools/studies/arm_gates_check.py features    # CPU: gate flags per sampled signal
    .venv/bin/python tools/studies/quotes_parallel.py arm_gates_check all 4     # after 16:05 only
    .venv/bin/python tools/studies/arm_gates_check.py score
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

OUT = ROOT / "ai_reports" / "arm_gates_check"
DECIDE = ("2026-04-28", "2026-09-23")
CONFIRM = ("2026-09-24", "2026-11-18")
CAP, GATE_BP = 5000, 15.0


def sample(rows):
    by = defaultdict(list)
    for r in rows:
        by[r["day"] <= DECIDE[1]].append(r)
    out = []
    for v in by.values():
        out += sorted(v, key=lambda r: hashlib.sha256(f"gates|{r['day']}|{r['sym']}|{r['t']}".encode()).hexdigest())[:CAP]
    return out


def five_min(cc, hh, ll):
    """1-minute grid -> 5-minute bars aligned to the grid's end (last bar = the 5 minutes ending at the grid end). Pure."""
    n = len(cc) // 5 * 5
    s = len(cc) - n
    c5 = cc[s:].reshape(-1, 5)[:, -1]
    h5 = np.nanmax(hh[s:].reshape(-1, 5), axis=1)
    l5 = np.nanmin(ll[s:].reshape(-1, 5), axis=1)
    return c5, h5, l5


def gate_flags(cc, hh, ll, off, t):
    """(A_blocks, B_blocks) at today's bar t on the prepended grid. Pure."""
    from bar_signal_family import wr_smoothed
    k = off + t
    fast = wr_smoothed(hh[:k + 1], ll[:k + 1], cc[:k + 1], n=21, span=7)
    slow = wr_smoothed(hh[:k + 1], ll[:k + 1], cc[:k + 1], n=112, span=3)
    a = not (np.isfinite(fast[k]) and np.isfinite(fast[k - 10]) and fast[k] - fast[k - 10] >= 15)
    b1 = np.isfinite(slow[k]) and np.isfinite(slow[k - 5]) and slow[k] - slow[k - 5] < 0
    # completed 5-minute bars up to minute t (a bar is complete when its last minute <= t): grid through k, aligned
    # so 5-minute bars end on minutes where (t + 1) % 5 == 0
    end = off + (t + 1) // 5 * 5 - 1
    c5, h5, l5 = five_min(cc[:end + 1], hh[:end + 1], ll[:end + 1])
    s5 = wr_smoothed(h5, l5, c5, n=28, span=3)
    b2 = len(s5) >= 2 and np.isfinite(s5[-1]) and np.isfinite(s5[-2]) and s5[-1] - s5[-2] < 0
    return bool(a), bool(b1 or b2)


def pace(v, prev_vol, t):
    """Cumulative volume 09:30..t over the prior day's volume at the same elapsed share. None if unknown. Pure."""
    if not (prev_vol and prev_vol > 0):
        return None
    cv = float(np.nansum(v[:t + 1]))
    return round(cv / (prev_vol * (t + 1) / 390.0), 3)


CAP_C = 3000


def below_band_pool(day, prev, d):
    """Pre-square triggers on top-400 $20-$400 names with day change in [-5%, +1.5%) at t (amended_1, gate C). Pure-ish."""
    from board_structure_family import universe
    from evening_family import ALL_ETF, minute_grid, presquare_bars, first_true
    from holistic_entry import bracket, COST
    import news_source as ns
    out = []
    for s in universe(day, prev, set(ALL_ETF)):
        i, j = day["idx"][s], prev["idx"].get(s)
        if j is None:
            continue
        pc = ns.last_close(prev["c"][j])
        if not pc > 0:
            continue
        o, h, l, c, v = (day[k][i] for k in "ohlcv")
        cc, hh, ll, off = minute_grid(prev["c"][j], prev["h"][j], prev["l"][j], c, h, l)

        def ok(t):
            return np.isfinite(c[t]) and 20 <= c[t] <= 400 and -5.0 <= (c[t] / pc - 1) * 100 < 1.5
        for t in first_true([t for t in presquare_bars(cc, hh, ll, off, 21) if 15 <= t <= 360 and ok(t)], 30):
            b = bracket(o, h, l, c, t)
            if b is None:
                continue
            ga, gb = gate_flags(cc, hh, ll, off, t)
            out.append({"day": d, "sym": s, "t": int(t), "gross": round(float(b) + COST, 2), "pool": "below",
                        "A_blocks": ga, "B_blocks": gb, "C_blocks": True,
                        "pace": pace(v, float(np.nansum(prev["v"][j])), t)})
    return out


def features():
    import signal_cache as sc
    from relative_strength_scan import load_day
    from evening_family import WH_SIP, minute_grid
    rows = sample(sc.load(DECIDE[0], CONFIRM[1]))
    by_day = defaultdict(list)
    for r in rows:
        by_day[r["day"]].append(r)
    files = {p.stem: p for p in WH_SIP.glob("20??-??-??.npz")}
    days = sorted(files)
    out, below = [], []
    for d in sorted(by_day):
        i = days.index(d)
        if i == 0:
            continue
        day, prev = load_day(files[d]), load_day(files[days[i - 1]])
        for r in by_day[d]:
            a, b = day["idx"].get(r["sym"]), prev["idx"].get(r["sym"])
            if a is None or b is None or r.get("bracket_gross") is None:
                continue
            cc, hh, ll, off = minute_grid(prev["c"][b], prev["h"][b], prev["l"][b], day["c"][a], day["h"][a], day["l"][a])
            ga, gb = gate_flags(cc, hh, ll, off, r["t"])
            pc_ = pace(day["v"][a], float(np.nansum(prev["v"][b])), r["t"])
            out.append({"day": d, "sym": r["sym"], "t": r["t"], "gross": r["bracket_gross"], "g10": r.get("gross10"),
                        "A_blocks": ga, "B_blocks": gb, "C_blocks": False, "pool": "cache", "pace": pc_,
                        "D_blocks": pc_ is not None and pc_ < 1.0})
        below += below_band_pool(day, prev, d)
        print(d, len(out), len(below), flush=True)
    byp = defaultdict(list)
    for r in below:
        byp[r["day"] <= DECIDE[1]].append(r)
    for v in byp.values():
        out += sorted(v, key=lambda r: hashlib.sha256(f"gates_c|{r['day']}|{r['sym']}|{r['t']}".encode()).hexdigest())[:CAP_C]
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "features.json").write_text(json.dumps(out))


def plan(which="all"):
    return sorted({("gates", r["day"], r["sym"], r["t"], 0.0) for r in json.loads((OUT / "features.json").read_text())})


def cluster_se(vals, days):
    n = len(vals)
    if n < 3:
        return None
    x = np.array(vals, float)
    m = x.mean()
    s = defaultdict(float)
    for xi, d in zip(x, days):
        s[d] += xi - m
    g = len(s)
    return float(math.sqrt(sum(v * v for v in s.values()) * g / (g - 1)) / n) if g >= 3 else None


def lift(rows, key):
    """Mean net of passed minus blocked, day-clustered t. Pure."""
    p = [r for r in rows if not r[key]]
    b = [r for r in rows if r[key]]
    if len(p) < 3 or len(b) < 3:
        return {"n_pass": len(p), "n_block": len(b), "lift": None, "t": None}
    mp, mb = np.mean([r["net"] for r in p]), np.mean([r["net"] for r in b])
    sp, sb = cluster_se([r["net"] for r in p], [r["day"] for r in p]), cluster_se([r["net"] for r in b], [r["day"] for r in b])
    se = math.sqrt(sp * sp + sb * sb) if sp and sb else None
    return {"n_pass": len(p), "n_block": len(b), "pass_net": round(float(mp), 2), "block_net": round(float(mb), 2),
            "lift": round(float(mp - mb), 2), "t": round(float((mp - mb) / se), 2) if se else None,
            "block_rate": round(len(b) / (len(p) + len(b)), 3)}


def decide(dec, conf):
    if dec.get("n_pass", 0) < 200 or dec.get("n_block", 0) < 200:
        return "UNDERPOWERED"
    if (dec.get("lift") or -99) >= 2.0 and (dec.get("t") or -99) >= 2.0 and (conf.get("lift") or -1) > 0:
        return "KEEP"
    if (dec.get("lift") if dec.get("lift") is not None else 99) <= 0.5 and (conf.get("lift") if conf.get("lift") is not None else 99) <= 0.5:
        return "REMOVE"
    return "UNCLEAR"


def score():
    import news_source as ns
    q = json.load(open(ns.QUOTES)) if ns.QUOTES.exists() else {}
    rows = []
    noq = gated = 0
    for r in json.loads((OUT / "features.json").read_text()):
        sp = q.get(ns.qkey(r["day"], r["sym"], r["t"]))
        if sp is None:
            noq += 1
            continue
        if sp > GATE_BP:
            gated += 1
            continue
        rows.append({**r, "net": r["gross"] - sp, "either": r["A_blocks"] or r["B_blocks"],
                     "D_blocks": bool(r.get("D_blocks")), "C_blocks": bool(r.get("C_blocks"))})
    res = {}
    for per, (a, b) in (("decide", DECIDE), ("confirm", CONFIRM)):
        sel_all = [r for r in rows if a <= r["day"] <= b]
        sel = [r for r in sel_all if r.get("pool", "cache") == "cache"]
        res[per] = {k: lift(sel, k) for k in ("A_blocks", "B_blocks", "either", "D_blocks")}
        res[per]["C_blocks"] = lift(sel_all, "C_blocks")
        res[per]["D_pace_known"] = sum(r.get("pace") is not None for r in sel)
        nd = len({r["day"] for r in sel}) or 1
        res[per]["opens_per_10m_all"] = round(len(sel) / nd / 34.5, 3)
    verdict = {g: decide(res["decide"][g], res["confirm"][g]) for g in ("A_blocks", "B_blocks", "C_blocks", "D_blocks")}
    out = {"VERDICT": verdict, "results": res, "noquote": noq, "gated": gated}
    (OUT / "score.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))
    return out


if __name__ == "__main__":
    {"features": features, "score": score}[sys.argv[1]]()
