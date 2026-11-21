#!/usr/bin/env python3
"""rsi2_red_inplay - RSI-2 red on stocks in play (top-decile 15-minute range at that minute), vs other in-play names.
Spec: docs/studies/rsi2_red_inplay_prereg.json.

    .venv/bin/python tools/studies/rsi2_red_inplay.py examples
    .venv/bin/python tools/studies/rsi2_red_inplay.py screen
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

PREREG = "docs/studies/rsi2_red_inplay_prereg.json"
EX = W.EX
EXCLUDED = ("2026-11-16", "2026-11-17", "2026-11-18")
LOOK, CLIMAX = 15, 1.5


def mean_range(h, l, c):
    """Per minute t: mean (high - low) / close over completed minutes t-15 .. t-1 (NaN skipped). Pure."""
    rg = (np.asarray(h, float) - np.asarray(l, float)) / np.asarray(c, float)
    out = np.full(len(rg), np.nan)
    for t in range(LOOK, len(rg)):
        seg = rg[t - LOOK:t]
        if np.isfinite(seg).sum() >= 8:
            out[t] = float(np.nanmean(seg))
    return out


def inplay_masks(MR, q):
    """MR: names x minutes. -> boolean names x minutes, True if the name's value is >= the q-quantile across names at
    that minute (names with a value). Pure."""
    out = np.zeros(MR.shape, bool)
    for t in range(MR.shape[1]):
        col = MR[:, t]
        ok = np.isfinite(col)
        if ok.sum() >= 20:
            out[:, t] = ok & (col >= np.quantile(col[ok], q))
    return out


def load(path):
    z = np.load(path)
    d = {k: z[k] for k in z.files}
    d["syms"] = [str(x) for x in z["syms"]]
    d["day"] = Path(path).stem
    return d


def day_signals(d):
    """-> (signals per name, MR, top-decile mask, top-quintile mask). Signal: (rule, t, ent, entry, dist, at_open). Pure."""
    U_ = len(d["syms"])
    MR = np.array([mean_range(d["h"][k], d["l"][k], d["c"][k]) for k in range(U_)])
    top10, top20 = inplay_masks(MR, 0.9), inplay_masks(MR, 0.8)
    sig = {}
    for k in range(U_):
        o, h, l, c = d["o"][k], d["h"][k], d["l"][k], d["c"][k]
        red, U, D, m4, m199 = d["red_pine"][k], d["U_pine"][k], d["D_pine"][k], d["m4_pine"][k], d["m199_pine"][k]
        out = []
        for t in range(R.T_LO, R.T_HI + 1):
            mr = MR[k, t]
            if not np.isfinite(mr):
                continue
            # S: mid-bar red; climax check on the completed bar t-1
            if not bool(red[t - 1]) and R.passes_filter_chg(c, d["pc"][k], t - 1, -1e9) and \
                    np.isfinite(h[t - 1]) and (h[t - 1] - l[t - 1]) / c[t - 1] <= CLIMAX * mr:
                P, below, _ = R.red_trigger_exact(R.px_or(c, t - 1), U[t - 1], D[t - 1], m4[t - 1], m199[t - 1])
                if np.isfinite(P) and below and P > c[t - 1]:
                    e = R.stop_fill(P, R.px_or(o, t), R.px_or(h, t))
                    if np.isfinite(e):
                        out.append(("S", t, t, e, P / c[t - 1] - 1, False))
            # C: closed red start; climax check on the red bar t itself (known at its close)
            if t < R.T_HI and np.isfinite(c[t]) and bool(red[t]) and not bool(red[t - 1]) and \
                    R.passes_filter_chg(c, d["pc"][k], t, -1e9) and (h[t] - l[t]) / c[t] <= CLIMAX * mr:
                e = R.px_or(o, t + 1)
                if np.isfinite(e):
                    out.append(("C", t, t + 1, e, None, True))
        sig[k] = out
    return sig, MR, top10, top20


def day_task(path, outcomes_on=True):
    d = load(path)
    day, U_ = d["day"], len(d["syms"])
    sig, MR, top10, top20 = day_signals(d)
    rows, cnt = [], defaultdict(int)
    for k, sigs in sig.items():
        o, h, l, c = d["o"][k], d["h"][k], d["l"][k], d["c"][k]
        free = defaultdict(lambda: -1)
        for rule, t, ent, e, dist, at_open in sigs:
            tsel = t if rule == "S" else t      # in-play read at the signal minute (from completed bars before it)
            ip10, ip20 = bool(top10[k, tsel]), bool(top20[k, tsel])
            if not ip20:
                continue
            cnt[f"{rule}_top20_raw"] += 1
            if ip10:
                cnt[f"{rule}_top10_raw"] += 1
            if not outcomes_on:
                continue
            key = (rule, ip10)
            if ent < free[rule]:
                cnt[f"{rule}_refractory"] += 1
                continue
            vals, xb = W.outcomes(o, h, l, c, ent, e, at_open)
            free[rule] = max(xb + 1, ent + 15)
            r = {"rule": rule, "top10": ip10, "day": day, "sym": d["syms"][k], "t": t, "ent": ent, **dict(zip(EX, vals)), "exit_bar": xb}
            kind = "open" if at_open else "stop"
            order = sorted((j for j in range(U_) if j != k), key=lambda j: R.hkey("inplay-peer", day, d["syms"][j], ent))
            fbar = t - 1 if kind == "stop" else t
            mask = top10 if ip10 else top20
            ip_peers = [j for j in order if mask[j, tsel] and R.passes_filter_chg(d["c"][j], d["pc"][j], fbar, -1e9)][:5]
            all_peers = [j for j in order if R.passes_filter_chg(d["c"][j], d["pc"][j], fbar, -1e9)][:5]
            for tag, js in (("ip", ip_peers), ("all", all_peers)):
                pv = [W.peer_trade(kind, d, j, t, ent, dist) for j in js]
                r[f"n_{tag}"] = sum(1 for v in pv if v)
                for i, ex in enumerate(EX):
                    m = W._mean_of(pv, i)
                    r[f"lift_{ex}" if tag == "ip" else f"liftall_{ex}"] = r[ex] - m if np.isfinite(m) and np.isfinite(r[ex]) else np.nan
            x0 = xb + 2 + R.hkey("inplay-later", day, r["sym"], ent) % 61
            v = W.outcomes(o, h, l, c, x0, R.px_or(o, x0), True)[0] if x0 <= R.T_HI and np.isfinite(R.px_or(o, x0)) else None
            for i, ex in enumerate(EX):
                r[f"lift_later_{ex}"] = r[ex] - v[i] if v and np.isfinite(v[i]) and np.isfinite(r[ex]) else np.nan
            rows.append(r)
    return rows, dict(cnt), day


def _counts_only(path):
    return day_task(path, outcomes_on=False)


def examples():
    hm = lambda i: f"{9 + (30 + i) // 60:02d}:{(30 + i) % 60:02d}"
    for day, sym, tm in (("2026-11-17", "BE", 85), ("2026-11-18", "WDC", 148)):
        d = load(R.CACHE / f"{day}.npz")
        sig, MR, top10, top20 = day_signals(d)
        k = d["syms"].index(sym)
        print(f"{sym} {day} {hm(tm)}: top10 {bool(top10[k, tm])} top20 {bool(top20[k, tm])} | signals near: " +
              ", ".join(f"{r} {hm(t)}" for r, t, *_ in sig[k] if abs(t - tm) <= 4))


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
    for rule in ("S", "C"):
        for band, sel in (("top10 (scored)", True), ("top20 (information)", None)):
            rr = [r for r in rows if r["rule"] == rule and (r["top10"] if sel else True)]
            rep = {"n": len(rr), "no_inplay_peer": sum(1 for r in rr if r["n_ip"] == 0)}
            for ex in EX:
                st = R._stat(rr, ex)
                st["gross_pooled"] = R.r2(R.mean(rr, ex))
                st["lift_pooled"] = R.r2(R.mean([r for r in rr if np.isfinite(r.get(f"lift_{ex}", np.nan))], f"lift_{ex}"))
                la = [r for r in rr if np.isfinite(r.get(f"liftall_{ex}", np.nan))]
                st["vs_all_names (information)"] = {"lift": R.r2(R.mean(la, f"liftall_{ex}")), "t": R.r2(R.day_t(la, f"liftall_{ex}"))}
                lat = [r for r in rr if np.isfinite(r.get(f"lift_later_{ex}", np.nan))]
                st["same_name_later"] = {"n": len(lat), "lift": R.r2(R.mean(lat, f"lift_later_{ex}")), "t": R.r2(R.day_t(lat, f"lift_later_{ex}"))}
                if sel and ex in ("m3", "m5", "ratchet"):
                    g1 = R._gate_one(st)
                    ok = (g1 == "PASS" and (st["lift_pooled"] or -1) >= 3.0 and (st["gross_pooled"] or -1) >= 5.0
                          and (st["same_name_later"]["lift"] if st["same_name_later"]["lift"] is not None else -1) >= 0)
                    st["step1_gate"] = "PASS" if ok else ("UNDERPOWERED" if g1 == "UNDERPOWERED" else "FAIL")
                    gate |= ok
                rep[ex] = st
            out[f"{rule} {band}"] = rep
    out["STEP2_GATE"] = "PASS - step 2" if gate else "STOP - RSI-2 red on in-play names does not pass; RSI-2 red CLOSED as an entry (prereg)"
    R.OUT.mkdir(parents=True, exist_ok=True)
    (R.OUT / "inplay_screen.json").write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    {"examples": examples, "screen": screen}.get(sys.argv[1] if len(sys.argv) > 1 else "", lambda: sys.exit(__doc__))()
