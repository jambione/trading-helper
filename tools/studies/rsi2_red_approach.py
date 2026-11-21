#!/usr/bin/env python3
"""rsi2_red_approach - closed RSI-2 red after a gradual climb (RSI-2[t-1] >= 80) vs after a jump (< 60).
Spec: docs/studies/rsi2_red_approach_prereg.json.

    .venv/bin/python tools/studies/rsi2_red_approach.py examples
    .venv/bin/python tools/studies/rsi2_red_approach.py screen
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))
import rsi2_colour_timing as R  # noqa: E402
import rsi2_red_washout as W  # noqa: E402

PREREG = "docs/studies/rsi2_red_approach_prereg.json"
EX = W.EX                                  # m1, m3, m5, ratchet
EXCLUDED = ("2026-11-16", "2026-11-17", "2026-11-18")


def group(rsi_prev):
    """G (>= 80), J (< 60), M (between) from RSI-2 on the bar before the red; None if unknown. Pure."""
    if not np.isfinite(rsi_prev):
        return None
    return "G" if rsi_prev >= 80 else ("J" if rsi_prev < 60 else "M")


def signals(d, k):
    """Closed red starts on one name-day with their group. -> [(group, t, entry_bar)]. Pure."""
    c, red, rsi = d["c"][k], d["red_pine"][k], d["rsi_pine"][k]
    out = []
    for t in range(R.T_LO, R.T_HI):
        if np.isfinite(c[t]) and bool(red[t]) and not bool(red[t - 1]) and R.passes_filter_chg(c, d["pc"][k], t, -1e9):
            g = group(rsi[t - 1])
            if g:
                out.append((g, t, t + 1))
    return out


def load(path):
    z = np.load(path)
    d = {k: z[k] for k in z.files}
    d["syms"] = [str(x) for x in z["syms"]]
    d["day"] = Path(path).stem
    return d


def day_task(path, outcomes_on=True):
    d = load(path)
    day, U_ = d["day"], len(d["syms"])
    rows, cnt = [], defaultdict(int)
    for k, s in enumerate(d["syms"]):
        o, h, l, c = d["o"][k], d["h"][k], d["l"][k], d["c"][k]
        free = defaultdict(lambda: -1)
        for g, t, ent in signals(d, k):
            cnt[f"{g}_raw"] += 1
            if not outcomes_on:
                continue
            e = R.px_or(o, ent)
            if not np.isfinite(e):
                cnt[f"{g}_nan_open"] += 1
                continue
            if ent < free[g]:
                cnt[f"{g}_refractory"] += 1
                continue
            vals, xb = W.outcomes(o, h, l, c, ent, e, True)
            free[g] = max(xb + 1, ent + 15)
            r = {"rule": g, "day": day, "sym": s, "t": t, "ent": ent, "rsi_prev": float(d["rsi_pine"][k][t - 1]),
                 **dict(zip(EX, vals)), "exit_bar": xb}
            for ex_, v_ in zip(EX, vals):
                if not np.isfinite(v_):
                    cnt[f"{g}_nan_{ex_}"] += 1
            order = sorted((j for j in range(U_) if j != k), key=lambda j: R.hkey("appr-peer", day, d["syms"][j], ent))
            peers = [j for j in order if R.passes_filter_chg(d["c"][j], d["pc"][j], t, -1e9)][:5]
            pv = [W.peer_trade("open", d, j, t, ent, None) for j in peers]
            r["n_peer"] = sum(1 for v in pv if v)
            for i, ex in enumerate(EX):
                m = W._mean_of(pv, i)
                r[f"peer_{ex}"] = m
                r[f"lift_{ex}"] = r[ex] - m if np.isfinite(m) and np.isfinite(r[ex]) else np.nan
            rng = np.random.default_rng(R.hkey("appr-unc", day, s, ent, g) % 2 ** 32)
            for ex in EX:
                r[f"unc_{ex}"] = np.nan
            for _ in range(20):
                j, tt = int(rng.integers(0, U_)), int(rng.integers(R.T_LO, R.T_HI))
                if R.passes_filter_chg(d["c"][j], d["pc"][j], tt, -1e9):
                    v = W.peer_trade("open", d, j, tt, tt + 1, None)
                    if v:
                        for i, ex in enumerate(EX):
                            r[f"unc_{ex}"] = v[i]
                    break
            x0 = xb + 2 + R.hkey("appr-later", day, s, ent) % 61
            v = W.outcomes(o, h, l, c, x0, R.px_or(o, x0), True)[0] if x0 <= R.T_HI and np.isfinite(R.px_or(o, x0)) else None
            for i, ex in enumerate(EX):
                r[f"lift_later_{ex}"] = r[ex] - v[i] if v and np.isfinite(v[i]) and np.isfinite(r[ex]) else np.nan
            rows.append(r)
    return rows, dict(cnt), day


def _counts_only(path):
    return day_task(path, outcomes_on=False)


def examples():
    hm = lambda i: f"{9 + (30 + i) // 60:02d}:{(30 + i) % 60:02d}"
    for day, sym, want in (("2026-11-18", "CBRS", {"12:16": "G", "11:58": "J", "10:14": "M"}),
                           ("2026-11-16", "QCOM", {"15:26": "G"})):
        d = load(R.CACHE / f"{day}.npz")
        k = d["syms"].index(sym)
        got = {hm(t): g for g, t, _ in signals(d, k)}
        for tm, g in want.items():
            print(f"{'OK  ' if got.get(tm) == g else 'MISS'} {sym} {day} {tm}: expected {g}, got {got.get(tm)}")


def g_minus_j(rows, ex):
    da = R.day_means([r for r in rows if r["rule"] == "G"], ex)
    db = R.day_means([r for r in rows if r["rule"] == "J"], ex)
    out = {}
    for h, keep in (("h1", lambda x: x < R.SPLIT), ("h2", lambda x: x >= R.SPLIT), ("all", lambda x: True)):
        dd = [da[k] - db[k] for k in da if k in db and keep(k)]
        if len(dd) >= 3 and np.std(dd, ddof=1) > 0:
            out[h] = {"diff": R.r2(float(np.mean(dd))), "t": R.r2(float(np.mean(dd) / (np.std(dd, ddof=1) / np.sqrt(len(dd))))), "days": len(dd)}
        else:
            out[h] = {"diff": R.r2(float(np.mean(dd))) if dd else None, "t": None, "days": len(dd)}
    return out


def screen():
    from multiprocessing import Pool
    sp = [str(p) for p in sorted(R.CACHE.glob("*.npz")) if R.SEARCH[0] <= p.stem <= R.SEARCH[1]]
    cp = [str(p) for p in sorted(R.CACHE.glob("*.npz")) if R.CONFIRM[0] <= p.stem <= R.CONFIRM[1] and p.stem not in EXCLUDED]
    rows, cnt, ccnt = [], {"h1": defaultdict(int), "h2": defaultdict(int)}, defaultdict(int)
    with Pool(int(os.environ.get("RED_WORKERS", "6"))) as pool:
        for rr, cc, day in pool.imap(day_task, sp):
            rows += rr
            for k, v in cc.items():
                cnt["h1" if day < R.SPLIT else "h2"][k] += v
        for _, cc, _ in pool.imap(_counts_only, cp):
            for k, v in cc.items():
                ccnt[k] += v
    counts = {"SEARCH_h1": dict(cnt["h1"]), "SEARCH_h2": dict(cnt["h2"]), "CONFIRM (sealed, counts only)": dict(ccnt)}
    print("COUNTS", json.dumps(counts), flush=True)
    out = {"prereg": PREREG, "counts": counts}
    gate = False
    for g in ("G", "J", "M"):
        rr = [r for r in rows if r["rule"] == g]
        ra, rb = R.halves(rr)
        rep = {"n": len(rr), "no_peer_h1": sum(1 for r in ra if r["n_peer"] == 0), "no_peer_h2": sum(1 for r in rb if r["n_peer"] == 0)}
        for ex in EX:
            st = R._stat(rr, ex)
            st["gross_pooled"] = R.r2(R.mean(rr, ex))
            st["lift_pooled"] = R.r2(R.mean([r for r in rr if np.isfinite(r.get(f"lift_{ex}", np.nan))], f"lift_{ex}"))
            st["peer_mean"], st["unconditional_mean"] = R.r2(R.mean(rr, f"peer_{ex}")), R.r2(R.mean(rr, f"unc_{ex}"))
            pu = R._paired_day_t(rr, f"peer_{ex}", rr, f"unc_{ex}")
            st["peer_vs_unconditional_day"] = pu
            st["mismatch_block"] = bool(pu["diff"] is not None and pu["t"] is not None and abs(pu["diff"]) > 3 and abs(pu["t"]) > 2)
            lat = [r for r in rr if np.isfinite(r.get(f"lift_later_{ex}", np.nan))]
            st["same_name_later"] = {"n": len(lat), "lift": R.r2(R.mean(lat, f"lift_later_{ex}")), "t": R.r2(R.day_t(lat, f"lift_later_{ex}")),
                                     "later_mean": R.r2(R.mean([dict(r, _lm=r[ex] - r[f"lift_later_{ex}"]) for r in lat], "_lm")) if lat else None}
            if g == "G":
                st["G_minus_J"] = g_minus_j(rows, ex)
                if ex in ("m3", "m5", "ratchet"):
                    gbar = 5.0 if ex == "ratchet" else 3.0
                    g1 = R._gate_one(st)
                    gj = st["G_minus_J"]
                    ok = (g1 == "PASS" and (st["lift_pooled"] or -1) >= 3.0 and (st["gross_pooled"] or -1) >= gbar
                          and not st["mismatch_block"] and (gj["h1"]["diff"] or -1) > 0 and (gj["h2"]["diff"] or -1) > 0
                          and gj["h1"]["days"] >= 20 and gj["h2"]["days"] >= 20
                          and (st["same_name_later"]["lift"] if st["same_name_later"]["lift"] is not None else -1) >= 0)
                    st["step1_gate"] = "PASS" if ok else ("UNDERPOWERED" if g1 == "UNDERPOWERED" else "FAIL")
                    gate |= ok
            rep[ex] = st
        if g == "G":                          # review fix 1: split by where RSI-2 was the bar before (information)
            rep["split (information)"] = {
                nm: {ex: {"n": len(x), "lift": R.r2(R.mean(x, f"lift_{ex}")), "t": R.r2(R.day_t(x, f"lift_{ex}"))} for ex in ("m3", "m5", "ratchet")}
                for nm, x in (("G80_90 (climbed into red)", [r for r in rr if r["rsi_prev"] < 90]),
                              (">=90 (RSI already high; an SMA condition flipped)", [r for r in rr if r["rsi_prev"] >= 90]))}
        out[g] = rep
    out["STEP2_GATE"] = "PASS - step 2" if gate else "STOP - gradual-approach red does not pass the step-1 gate on SEARCH"
    R.OUT.mkdir(parents=True, exist_ok=True)
    (R.OUT / "approach_screen.json").write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    {"examples": examples, "screen": screen}.get(sys.argv[1] if len(sys.argv) > 1 else "", lambda: sys.exit(__doc__))()
