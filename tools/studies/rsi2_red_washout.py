#!/usr/bin/env python3
"""rsi2_red_washout - RSI-2 red early off a fresh low (A), and confirmed by the %R oversold-exit triangle (B1).
Spec: docs/studies/rsi2_red_washout_prereg.json. Reuses rsi2_colour_timing's cache, trigger, ratchet and peers.

    .venv/bin/python tools/studies/rsi2_red_washout.py examples   # code check on 11/16-18 (signals only)
    .venv/bin/python tools/studies/rsi2_red_washout.py screen     # step 1, SEARCH only (CONFIRM counts only)
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
from early_entry_honest import os_leave_flag  # noqa: E402

PREREG = "docs/studies/rsi2_red_washout_prereg.json"
WARM = 112
HOLDS = (1, 3, 5)
EX = ("m1", "m3", "m5", "ratchet")
EXCLUDED = ("2026-11-16", "2026-11-17", "2026-11-18")


# ---------------------------------------------------------------- pure pieces

def low_bar(l, t, look=15):
    """Bar index of the lowest LOW among completed bars t-look .. t-1 (NaN skipped), or None. Pure."""
    lo = max(0, t - look)
    seg = l[lo:t]
    if not np.isfinite(seg).any():
        return None
    return lo + int(np.nanargmin(seg))


def early(l, t):
    """Rule A's 'within 2 bars of the 15-minute low': the low is at t-1 or t-2. Pure."""
    b = low_bar(l, t)
    return b is not None and t - b in (1, 2)


def wr_lines(ph, pl, pc, h, l, c):
    """Fast %R(21, EWM 7) and slow %R(112, EWM 3) on prior-session warm-up (last 112 finite RTH bars) + today's 390.
    -> (fast, slow, off) with today's bar t at index off + t. Pure."""
    from bar_signal_family import wr_smoothed
    m = np.isfinite(pc) & np.isfinite(ph) & np.isfinite(pl)
    wh, wl, wc = ph[m][-WARM:], pl[m][-WARM:], pc[m][-WARM:]
    hh, ll, cc = np.concatenate([wh, h]), np.concatenate([wl, l]), np.concatenate([wc, c])
    return wr_smoothed(hh, ll, cc, n=21, span=7), wr_smoothed(hh, ll, cc, n=112, span=3), len(wc)


def signals(d, k, fast, slow, off):
    """A and B1 signals on one name-day. -> list of (rule, t_signal, entry_bar, entry, dist, at_open). Pure."""
    o, h, l, c = d["o"][k], d["h"][k], d["l"][k], d["c"][k]
    red, U, D, m4, m199 = d["red_pine"][k], d["U_pine"][k], d["D_pine"][k], d["m4_pine"][k], d["m199_pine"][k]
    pc = d["pc"][k]
    out = []
    for t in range(R.T_LO, R.T_HI + 1):
        # A: mid-bar red on bar t, early off the 15-minute low
        if not bool(red[t - 1]) and R.passes_filter_chg(c, pc, t - 1, -1e9) and early(l, t):
            P, below, _ = R.red_trigger_exact(R.px_or(c, t - 1), U[t - 1], D[t - 1], m4[t - 1], m199[t - 1])
            if np.isfinite(P) and below and P > c[t - 1]:
                e = R.stop_fill(P, R.px_or(o, t), R.px_or(h, t))
                if np.isfinite(e):
                    out.append(("A", t, t, e, P / c[t - 1] - 1, False))
        # B1: a red segment starting at s = t (closed red, previous not) that was A-early; triangle at u in s..s+2
        if np.isfinite(c[t]) and bool(red[t]) and not bool(red[t - 1]) and early(l, t):
            for u in range(t, min(t + 3, 389)):
                if os_leave_flag(fast, slow, off + u):
                    e = R.px_or(o, u + 1)
                    if np.isfinite(e) and R.passes_filter_chg(c, pc, u, -1e9) and u + 1 <= R.T_HI:
                        out.append(("B1", t, u + 1, e, None, True))
                    break
    return out


def outcomes(o, h, l, c, ent, e, at_open):
    rx, xb = R.ratchet_exit(o, h, l, c, ent, e, at_open)
    return [(R.px_or(c, ent + n - 1) / e - 1) * 1e4 for n in HOLDS] + [(rx / e - 1) * 1e4], xb


def peer_trade(rule, d, j, t, ent, dist):
    o, h, l, c = d["o"][j], d["h"][j], d["l"][j], d["c"][j]
    if rule == "A":
        cp = R.px_or(c, t - 1)
        if not np.isfinite(cp):
            return None
        P = np.ceil(round(cp * (1 + dist) * 100, 6)) / 100.0
        e = R.stop_fill(P, R.px_or(o, t), R.px_or(h, t))
        return outcomes(o, h, l, c, t, e, False)[0] if np.isfinite(e) else None
    e = R.px_or(o, ent)
    return outcomes(o, h, l, c, ent, e, True)[0] if np.isfinite(e) else None


def day_task(path, outcomes_on=True):
    from relative_strength_scan import load_day
    from evening_family import WH_SIP
    z = np.load(path)
    d = {k: z[k] for k in z.files}
    d["syms"] = [str(x) for x in z["syms"]]
    day = Path(path).stem
    d["day"] = day
    cal = sorted(p.stem for p in WH_SIP.glob("20??-??-??.npz"))
    if day not in cal or cal.index(day) == 0:
        return [], {"no_prior_session": 1}, day
    pd_ = load_day(WH_SIP / f"{cal[cal.index(day) - 1]}.npz")
    U_ = len(d["syms"])
    rows, cnt = [], defaultdict(int)
    for k, s in enumerate(d["syms"]):
        j = pd_["idx"].get(s)
        if j is None:
            cnt["no_prior_bars"] += 1
            continue
        fast, slow, off = wr_lines(pd_["h"][j], pd_["l"][j], pd_["c"][j], d["h"][k], d["l"][k], d["c"][k])
        if off < WARM:
            cnt["short_warmup"] += 1
        red = d["red_pine"][k]
        cnt["red_starts"] += int(sum(1 for t in range(R.T_LO, R.T_HI + 1) if red[t] and not red[t - 1] and np.isfinite(d["c"][k][t])))
        free = {"A": -1, "B1": -1}
        for rule, t, ent, e, dist, at_open in signals(d, k, fast, slow, off):
            cnt[f"{rule}_raw"] += 1
            if not outcomes_on:
                continue
            if ent < free[rule]:
                cnt[f"{rule}_refractory"] += 1
                continue
            o, h, l, c = d["o"][k], d["h"][k], d["l"][k], d["c"][k]
            vals, xb = outcomes(o, h, l, c, ent, e, at_open)
            free[rule] = max(xb + 1, ent + 15)
            r = {"rule": rule, "day": day, "sym": s, "t": t, "ent": ent, **dict(zip(EX, vals)), "exit_bar": xb}
            order = sorted((jj for jj in range(U_) if jj != k), key=lambda jj: R.hkey("wash-peer", day, d["syms"][jj], ent))
            fbar = t - 1 if rule == "A" else ent - 1
            peers = [jj for jj in order if R.passes_filter_chg(d["c"][jj], d["pc"][jj], fbar, -1e9)][:5]
            pv = [v for v in (peer_trade(rule, d, jj, t, ent, dist) for jj in peers) if v]
            r["n_peer"] = len(pv)
            for i, ex in enumerate(EX):
                xs = [v[i] for v in pv if np.isfinite(v[i])]
                r[f"peer_{ex}"] = float(np.mean(xs)) if xs else np.nan
                r[f"lift_{ex}"] = r[ex] - r[f"peer_{ex}"] if xs and np.isfinite(r[ex]) else np.nan
            rng = np.random.default_rng(R.hkey("wash-unc", day, s, ent, rule) % 2 ** 32)
            for ex in EX:
                r[f"unc_{ex}"] = np.nan
            for _ in range(20):
                jj, tt = int(rng.integers(0, U_)), int(rng.integers(R.T_LO, R.T_HI + 1))
                if R.passes_filter_chg(d["c"][jj], d["pc"][jj], tt - 1, -1e9):
                    v = peer_trade(rule, d, jj, tt, tt + (0 if rule == "A" else 1), dist)
                    if v:
                        for i, ex in enumerate(EX):
                            r[f"unc_{ex}"] = v[i]
                    break
            x0 = xb + 2 + R.hkey("wash-later", day, s, ent) % 61
            v = outcomes(d["o"][k], d["h"][k], d["l"][k], d["c"][k], x0, R.px_or(d["o"][k], x0), True)[0] \
                if x0 <= R.T_HI and np.isfinite(R.px_or(d["o"][k], x0)) else None
            for i, ex in enumerate(EX):
                r[f"lift_later_{ex}"] = r[ex] - v[i] if v and np.isfinite(v[i]) and np.isfinite(r[ex]) else np.nan
            rows.append(r)
    return rows, dict(cnt), day


def _counts_only(path):
    return day_task(path, outcomes_on=False)


# ---------------------------------------------------------------- commands

def examples():
    hm = lambda i: f"{9 + (30 + i) // 60:02d}:{(30 + i) % 60:02d}"
    from relative_strength_scan import load_day
    from evening_family import WH_SIP
    cal = sorted(p.stem for p in WH_SIP.glob("20??-??-??.npz"))
    for day, sym, note in (("2026-11-17", "BE", "should fire A/B1 ~10:55; NOT A at 10:23"),
                           ("2026-11-18", "WDC", "should fire ~11:58; NOT A at 11:24"),
                           ("2026-11-18", "SHW", "A at ~10:12")):
        z = np.load(R.CACHE / f"{day}.npz")
        d = {k: z[k] for k in z.files}
        d["syms"] = [str(x) for x in z["syms"]]
        d["day"] = day
        k = d["syms"].index(sym)
        pd_ = load_day(WH_SIP / f"{cal[cal.index(day) - 1]}.npz")
        j = pd_["idx"][sym]
        fast, slow, off = wr_lines(pd_["h"][j], pd_["l"][j], pd_["c"][j], d["h"][k], d["l"][k], d["c"][k])
        sig = signals(d, k, fast, slow, off)
        print(f"== {sym} {day} ({note}): " + ", ".join(f"{r} sig {hm(t)} entry {hm(e)} @ {px:.2f}" for r, t, e, px, _, _ in sig))


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
                ccount = ccnt
                ccount[k] += v
    counts = {"SEARCH_h1": dict(cnt["h1"]), "SEARCH_h2": dict(cnt["h2"]), "CONFIRM (sealed, counts only)": dict(ccnt)}
    print("COUNTS", json.dumps(counts), flush=True)
    out = {"prereg": PREREG, "counts": counts}
    any_pass = False
    for rule in ("A", "B1"):
        rr = [r for r in rows if r["rule"] == rule]
        rep = {"n": len(rr), "no_peer": sum(1 for r in rr if r["n_peer"] == 0)}
        for ex in EX:
            st = R._stat(rr, ex)
            st["gross_pooled"] = R.r2(R.mean(rr, ex))
            st["lift_pooled"] = R.r2(R.mean([r for r in rr if np.isfinite(r.get(f"lift_{ex}", np.nan))], f"lift_{ex}"))
            st["peer_mean"], st["unconditional_mean"] = R.r2(R.mean(rr, f"peer_{ex}")), R.r2(R.mean(rr, f"unc_{ex}"))
            pu = R._paired_day_t(rr, f"peer_{ex}", rr, f"unc_{ex}")
            st["peer_vs_unconditional_day"] = pu
            st["mismatch_block"] = bool(pu["diff"] is not None and pu["t"] is not None and abs(pu["diff"]) > 3 and abs(pu["t"]) > 2)
            lat = [r for r in rr if np.isfinite(r.get(f"lift_later_{ex}", np.nan))]
            st["same_name_later"] = {"n": len(lat), "lift": R.r2(R.mean(lat, f"lift_later_{ex}")), "t": R.r2(R.day_t(lat, f"lift_later_{ex}"))}
            if ex in ("m3", "m5", "ratchet"):
                gbar = 5.0 if ex == "ratchet" else 3.0
                g1 = R._gate_one(st)
                ok = g1 == "PASS" and (st["lift_pooled"] or -1) >= 3.0 and (st["gross_pooled"] or -1) >= gbar and not st["mismatch_block"]
                st["step1_gate"] = "PASS" if ok else ("UNDERPOWERED" if g1 == "UNDERPOWERED" else "FAIL")
                any_pass |= ok
            rep[ex] = st
        out[rule] = rep
    out["STEP2_GATE"] = "PASS - step 2 (live-exit replay, CONFIRM unsealed)" if any_pass else "STOP - neither rule passes the step-1 gate on SEARCH"
    R.OUT.mkdir(parents=True, exist_ok=True)
    (R.OUT / "washout_screen.json").write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    {"examples": examples, "screen": screen}.get(sys.argv[1] if len(sys.argv) > 1 else "", lambda: sys.exit(__doc__))()
