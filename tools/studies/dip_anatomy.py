#!/usr/bin/env python3
"""dip_anatomy - when a dip in an up-on-the-day name ends with a first higher-low up-close, do some dips recover better
than the same minute on other names: by AGE (the operator's question), DEPTH in the stock's normal moves, SPEED, the
VOLUME shape inside the dip, or whether the MARKET dipped too? Operator 11/20. Prereg + amended_1:
docs/studies/dip_anatomy_prereg.json.

    .venv/bin/python tools/studies/dip_anatomy.py events     # CPU, warehouse only (no API): ai_reports/dip_anatomy/events_*.json
    .venv/bin/python tools/studies/dip_anatomy.py edge       # EDGE verdicts, age question, canary (gross, no quotes)
    .venv/bin/python tools/studies/dip_anatomy.py run        # + quotes for EDGE cells only (after 16:05 ET), PROFITABLE

Quotes are fetched only for cells that pass EDGE (PROFITABLE requires EDGE) and the gross-first screen.
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

OUT = ROOT / "ai_reports" / "dip_anatomy"
OWNQ = OUT / "quotes.json"
SEARCH = ("2026-04-28", "2026-09-23")
CONFIRM = ("2026-09-24", "2026-11-18")
SPLIT = "2026-07-11"
OPEN_WIN, MAX_AGE, DEPTH_MIN = 30, 120, 2.0
SIG_BARS, SIG_MIN, SIG_SKIP = 60, 40, 15          # sigma: last 60 same-session bars before s0, >= 40, excluding 09:30-09:44
T_LO, T_HI, HOLD, REFRACT = 15, 360, 15, 15
U_LO = SIG_SKIP + SIG_MIN                         # earliest minute an event can occur (sigma needs 40 bars after 09:44)
CHG_MIN, PX_LO, PX_HI = 1.5, 20.0, 400.0
EDGE_T, AGE_T, MDE_MAX, MIN_NET, MIN_N = 3.0, 2.0, 10.0, 2.0, 300
GATE_BP, CAP_PER_HALF, STRESS = 15.0, 1500, 1.5
FEATURES = ("AGE", "DEPTH", "SPEED", "VOLUME", "MARKET")
BLIND_AGES = (5, 10, 20)


def hkey(*p):
    return int(hashlib.sha256("|".join(str(x) for x in p).encode()).hexdigest(), 16)


# ---------------------------------------------------------------- dips and events (pure, one name-day)

def sigma_at(h, l, c, s0):
    """Median (high-low)/close over the last 60 same-session bars before s0, excluding bars 0..14; >= 40 finite."""
    lo = max(SIG_SKIP, s0 - SIG_BARS)
    if s0 - lo <= 0:
        return np.nan
    with np.errstate(invalid="ignore", divide="ignore"):
        r = (h[lo:s0] - l[lo:s0]) / c[lo:s0]
    r = r[np.isfinite(r)]
    return float(np.median(r)) if len(r) >= SIG_MIN and np.median(r) > 0 else np.nan


def detect(o, h, l, c, v, pc, counts=None):
    """Dips and their single turn event on one name-day, using only bars <= t at each t (amended_1 B1/B2).
    A dip OPENS at s0 when close[s0] is the highest finite close of the 30 bars ending s0; s0/Hs stay fixed until
    close >= Hs, the session ends, or 120 bars pass. The event is the first bar t after D first reaches 2.0 with
    close[t] > close[t-1], low[t] > low[t-1], t-1 at/after the lowest close since s0, and close[t] < Hs.
    -> (events: dicts with t, s0 and the features; opened: dip start bars). Filters/refractory: the caller. Pure."""
    counts = counts if counts is not None else defaultdict(int)
    n = len(c)
    ev, opened = [], []
    dip = None
    for t in range(n):
        if not np.isfinite(c[t]):
            continue
        if dip is not None and (c[t] >= dip["Hs"] or t - dip["s0"] > MAX_AGE):
            dip = None
        if dip is not None and t > dip["s0"]:
            if c[t] < dip["low_c"]:
                dip["low_c"], dip["low_bar"] = c[t], t
            D = (dip["Hs"] - c[t]) / (dip["Hs"] * dip["sig"]) if np.isfinite(dip["sig"]) else np.nan
            if not dip["done"] and dip["reach_bar"] is not None and t > dip["reach_bar"]:
                p = t - 1
                if (np.isfinite(c[p]) and np.isfinite(l[p]) and np.isfinite(l[t]) and c[t] > c[p] and l[t] > l[p]
                        and p >= dip["low_bar"]):
                    dip["done"] = True
                    ev.append(features(dip, t, h, l, c, v))
            if dip["reach_bar"] is None and np.isfinite(D) and D >= DEPTH_MIN:
                dip["reach_bar"] = t
        if dip is None:
            w = c[max(0, t - OPEN_WIN + 1): t + 1]
            if c[t] >= np.nanmax(w):
                sg = sigma_at(h, l, c, t)
                if not np.isfinite(sg):
                    counts["dip_no_sigma"] += 1
                else:
                    counts["dips_opened"] += 1
                dip = {"s0": t, "Hs": float(c[t]), "sig": sg, "low_c": float(c[t]), "low_bar": t,
                       "reach_bar": None, "done": False}
                opened.append(t)
    return ev, opened


def features(dip, t, h, l, c, v):
    s0, low_bar = dip["s0"], dip["low_bar"]
    depth = (dip["Hs"] - dip["low_c"]) / (dip["Hs"] * dip["sig"])
    dip_bars = np.arange(s0 + 1, t)
    vols = v[dip_bars] if len(dip_bars) else np.array([])
    last3 = v[max(s0 + 1, t - 3): t]
    vol = (float(np.nanmean(last3)) / float(np.nanmean(vols))
           if len(dip_bars) >= 4 and np.isfinite(vols).sum() >= 4 and np.nanmean(vols) > 0 else np.nan)
    return {"t": int(t), "s0": int(s0), "low_bar": int(low_bar), "AGE": float(t - s0), "DEPTH": float(depth),
            "SPEED": float(depth / max(low_bar - s0, 1)), "VOLUME": vol}


def entry_exit(o, c, t, hold=HOLD):
    """Entry: the first existing open in t+1..t+2 (else None). Exit: the last finite close in (entry .. t+hold].
    -> gross bp or NaN. Pure."""
    n = len(c)
    e = next((b for b in (t + 1, t + 2) if b < n and np.isfinite(o[b]) and o[b] > 0), None)
    if e is None or t + hold >= n:
        return np.nan
    xs = c[e: t + hold + 1]
    fin = np.where(np.isfinite(xs))[0]
    if not len(fin):
        return np.nan
    return float((xs[fin[-1]] / o[e] - 1) * 1e4)


def fwd_all(o, c, hold=HOLD):
    return np.array([entry_exit(o, c, t, hold) for t in range(len(c))])


def turn_mask(h, l, c):
    m = np.zeros(len(c), bool)
    with np.errstate(invalid="ignore"):
        m[1:] = (c[1:] > c[:-1]) & (l[1:] > l[:-1])
    return m


# ---------------------------------------------------------------- one day (pure given arrays)

def ctl_order(day, U, n=390):
    """Per-minute deterministic random order of the day's names (control draws). Pure."""
    return [np.random.default_rng(hkey("dipanat-ctl", day, t) % 2 ** 32).permutation(U) for t in range(n)]


def first_ok(order, mask, k, m=5):
    out = []
    for j in order:
        if j != k and mask[j]:
            out.append(int(j))
            if len(out) == m:
                break
    return out


def day_events(day, d, counts):
    """d: {syms, o,h,l,c,v (U x 390), pc (U), spy_c (390)}. -> event rows with gross, controls and features."""
    U = len(d["syms"])
    with np.errstate(invalid="ignore", divide="ignore"):
        chg = (d["c"] / d["pc"][:, None] - 1) * 100
    px_ok = (d["c"] >= PX_LO) & (d["c"] <= PX_HI)
    f15 = np.array([fwd_all(d["o"][k], d["c"][k]) for k in range(U)])
    f5 = np.array([fwd_all(d["o"][k], d["c"][k], 5) for k in range(U)])
    turns = np.array([turn_mask(d["h"][k], d["l"][k], d["c"][k]) for k in range(U)])
    try:
        from opening_grid import bracket_all
        br = np.array([bracket_all(d["o"][k], d["h"][k], d["l"][k], d["c"][k]) for k in range(U)])
    except Exception:
        br = None
    order = ctl_order(day, U, d["c"].shape[1])
    with np.errstate(invalid="ignore"):
        up_ok = chg >= CHG_MIN
    fin15 = np.isfinite(f15)
    rows, dips = [], {}
    for k in range(U):
        last = -10 ** 9
        evs, dips[k] = detect(d["o"][k], d["h"][k], d["l"][k], d["c"][k], d["v"][k], d["pc"][k], counts)
        for e in evs:
            t, s0 = e["t"], e["s0"]
            counts["events_raw"] += 1
            if not (T_LO <= t <= T_HI):
                counts["drop_window"] += 1
                continue
            if not (px_ok[k, t] and np.isfinite(chg[k, s0]) and chg[k, s0] >= CHG_MIN):
                counts["drop_filter"] += 1
                continue
            if t < last + REFRACT:
                counts["drop_refractory"] += 1
                continue
            g = f15[k, t]
            if not np.isfinite(g):
                counts["drop_no_entry_or_exit"] += 1
                continue
            last = t
            okm = px_ok[:, t] & up_ok[:, s0] & fin15[:, t]
            pick = first_ok(order[t], okm, k)
            tpick = first_ok(order[t], okm & turns[:, t], k)
            cand = [u for u in range(t + 16, min(t + 76, 389 - HOLD) + 1) if np.isfinite(f15[k, u])]
            later = float(f15[k, cand[hkey("dipanat-later", day, d["syms"][k], t) % len(cand)]]) if cand else np.nan
            unc = np.nan
            for z in range(30):
                hz = hkey("dipanat-unc", day, d["syms"][k], t, z)
                j, u = hz % U, U_LO + (hz >> 20) % (T_HI - U_LO + 1)
                if px_ok[j, u] and np.isfinite(chg[j, u]) and chg[j, u] >= CHG_MIN and np.isfinite(f15[j, u]):
                    unc = float(f15[j, u])
                    break
            spy = d["spy_c"]
            sp = (spy[t + HOLD] / spy[t] - 1) * 1e4 if t + HOLD < len(spy) and np.isfinite(spy[t + HOLD]) and spy[t] > 0 else np.nan
            mk = (spy[t] / spy[s0] - 1) * 1e4 if np.isfinite(spy[t]) and np.isfinite(spy[s0]) and spy[s0] > 0 else np.nan
            rows.append({"day": day, "sym": d["syms"][k], "t": t, "s0": s0, **{f: e[f] for f in ("AGE", "DEPTH", "SPEED", "VOLUME")},
                         "MARKET": float(mk), "gross": float(g), "gross5": float(f5[k, t]),
                         "bracket": float(br[k, t]) if br is not None and t < br.shape[1] else np.nan,
                         "ctl": float(np.mean(f15[pick, t])) if pick else np.nan, "ctl_n": len(pick),
                         "ctl_turn": float(np.mean(f15[tpick, t])) if tpick else np.nan,
                         "ctl5": float(np.nanmean(f5[pick, t])) if pick else np.nan,
                         "later": later, "unc": unc, "spy": float(sp)})
            counts["events"] += 1
            counts["volume_nan"] += int(not np.isfinite(e["VOLUME"]))
    rows += blind(day, d, up_ok, px_ok, f15, dips, order)
    return rows


def blind(day, d, up_ok, px_ok, f15, dips, order):
    """Information: entries at fixed dip ages (5/10/20 min after a dip opened) without waiting for the turn, while the
    dip is live (no close >= its start high yet). -> rows tagged blind=age. Pure."""
    out = []
    fin15 = np.isfinite(f15)
    for k, opened in dips.items():
        c = d["c"][k]
        for s0 in opened:
            if not up_ok[k, s0]:
                continue
            for a in BLIND_AGES:
                t = s0 + a
                if not (T_LO <= t <= T_HI) or not np.isfinite(c[t]) or not px_ok[k, t] or not fin15[k, t]:
                    continue
                if np.nanmax(c[s0 + 1: t + 1]) >= c[s0]:
                    continue
                pick = first_ok(order[t], px_ok[:, t] & up_ok[:, s0] & fin15[:, t], k)
                if pick:
                    out.append({"day": day, "sym": d["syms"][k], "t": t, "blind": a, "gross": float(f15[k, t]),
                                "ctl": float(np.mean(f15[pick, t]))})
    return out


# ---------------------------------------------------------------- statistics (pure)

def day_means(rows, key):
    per = defaultdict(list)
    for r in rows:
        x = r.get(key)
        if x is not None and np.isfinite(x):
            per[r["day"]].append(x)
    return {k: float(np.mean(v)) for k, v in per.items()}


def day_t(rows, key):
    m = list(day_means(rows, key).values())
    if len(m) < 3 or np.std(m, ddof=1) == 0:
        return None, None
    se = np.std(m, ddof=1) / math.sqrt(len(m))
    return float(np.mean(m) / se), float(se)


def mean(rows, key):
    v = [r[key] for r in rows if r.get(key) is not None and np.isfinite(r[key])]
    return float(np.mean(v)) if v else None


def winsor(x, top=0.01):
    x = np.asarray([y for y in x if np.isfinite(y)], float)
    return float(np.minimum(x, np.quantile(x, 1 - top)).mean()) if len(x) else None


def r2(x):
    return None if x is None else round(float(x), 2)


def halves(rows):
    return [r for r in rows if r["day"] < SPLIT], [r for r in rows if r["day"] >= SPLIT]


def add_lifts(rows):
    for r in rows:
        r["lift"] = r["gross"] - r["ctl"] if np.isfinite(r.get("ctl", np.nan)) else np.nan
        r["lift_later"] = r["gross"] - r["later"] if np.isfinite(r.get("later", np.nan)) else np.nan
        r["lift_unc"] = r["gross"] - r["unc"] if np.isfinite(r.get("unc", np.nan)) else np.nan
        r["lift_turn"] = r["gross"] - r["ctl_turn"] if np.isfinite(r.get("ctl_turn", np.nan)) else np.nan
        r["lift5"] = r["gross5"] - r["ctl5"] if np.isfinite(r.get("ctl5", np.nan)) and np.isfinite(r.get("gross5", np.nan)) else np.nan
        r["lift_spy"] = r["gross"] - r["spy"] if np.isfinite(r.get("spy", np.nan)) else np.nan
    return rows


def cut_points(rows):
    """Tertile cut points per feature on the final SEARCH event set (amended_1 m6; no outcome read). Pure."""
    out = {}
    for f in FEATURES:
        x = np.array([r[f] for r in rows if np.isfinite(r[f])])
        out[f] = [float(np.quantile(x, 1 / 3)), float(np.quantile(x, 2 / 3))] if len(x) >= 30 else None
    return out


def tertile(x, cuts):
    if cuts is None or not np.isfinite(x):
        return None
    return "lo" if x <= cuts[0] else ("mid" if x <= cuts[1] else "hi")


def cells(rows, cuts):
    out = {"ALL": rows}
    for f in FEATURES:
        for b in ("lo", "mid", "hi"):
            out[f"{f}_{b}"] = [r for r in rows if tertile(r[f], cuts[f]) == b]
    return out


def edge_cell(rows):
    a, b = halves(rows)
    la, lb = mean(a, "lift"), mean(b, "lift")
    t, se = day_t(rows, "lift")
    _, sea = day_t(a, "lift")
    _, seb = day_t(b, "lift")
    wa, wb = winsor([r["lift"] for r in a]), winsor([r["lift"] for r in b])
    ll, lu = mean(rows, "lift_later"), mean(rows, "lift_unc")
    mde_a = 3.84 * sea if sea else None
    mde_b = 3.84 * seb if seb else None
    mism = {}
    for key in ("ctl", "later"):       # TEST_STANDARD: each control's mean beside the unconditional mean, day-clustered t
        dd = [{"day": r["day"], "x": r[key] - r["unc"]} for r in rows
              if np.isfinite(r.get(key, np.nan)) and np.isfinite(r.get("unc", np.nan))]
        tt, _ = day_t(dd, "x")
        mism[key] = {"mean": r2(mean(rows, key)), "unc_mean": r2(mean(rows, "unc")), "diff": r2(mean(dd, "x")), "t_day": r2(tt)}
    dm = lambda rr: (float(np.mean(list(day_means(rr, "lift").values()))) if day_means(rr, "lift") else None)
    st = {"n": len(rows), "n_h1": len(a), "n_h2": len(b), "days": len({r["day"] for r in rows}),
          "gross": r2(mean(rows, "gross")), "lift_h1": r2(la), "lift_h2": r2(lb), "t": r2(t),
          "mde_h1": r2(mde_a), "mde_h2": r2(mde_b), "winsor_lift_h1": r2(wa), "winsor_lift_h2": r2(wb),
          "lift_same_name_later": r2(ll), "lift_unconditional": r2(lu), "lift_turn_matched_info": r2(mean(rows, "lift_turn")),
          "lift_5min_info": r2(mean(rows, "lift5")), "spy_hedged_gross_info": r2(mean(rows, "lift_spy")),
          "bracket_gross_info": r2(mean(rows, "bracket")),
          "control_mismatch": mism, "lift_daymean": r2(dm(rows)), "lift_daymean_h1": r2(dm(a)), "lift_daymean_h2": r2(dm(b)),
          "win_pct": r2(100 * float(np.mean([r["gross"] > 0 for r in rows]))) if rows else None}
    if (mde_a or 99) > MDE_MAX or (mde_b or 99) > MDE_MAX:
        v = "UNDERPOWERED -> UNPROVEN"
    else:
        ok = (la is not None and lb is not None and la > 0 and lb > 0 and t is not None and t >= EDGE_T
              and (ll or -1) > 0 and (lu or -1) > 0 and (wa or -1) > 0 and (wb or -1) > 0)
        v = "EDGE" if ok else "NO EDGE"
        bad = [k for k, m in mism.items() if (m["diff"] is not None and abs(m["diff"]) > 3) or (m["t_day"] is not None and abs(m["t_day"]) > 2)]
        if ok and bad:
            v = f"BLOCKED (control vs unconditional mismatch: {', '.join(bad)})"
    st["verdict"] = v
    return st


def paired_age_diff(rows, cuts, depth_bin=None):
    """Per-day paired difference of mean lift, oldest AGE tertile minus youngest, on days with both (m3). Pure."""
    sel = rows if depth_bin is None else [r for r in rows if tertile(r["DEPTH"], cuts["DEPTH"]) == depth_bin]
    old = day_means([r for r in sel if tertile(r["AGE"], cuts["AGE"]) == "hi"], "lift")
    young = day_means([r for r in sel if tertile(r["AGE"], cuts["AGE"]) == "lo"], "lift")
    both = sorted(set(old) & set(young))
    return [(d, old[d] - young[d]) for d in both]


def age_question(rows, cuts):
    diffs = paired_age_diff(rows, cuts)
    out = {}
    for name, sub in (("h1", [x for x in diffs if x[0] < SPLIT]), ("h2", [x for x in diffs if x[0] >= SPLIT]), ("pooled", diffs)):
        v = [x[1] for x in sub]
        if len(v) >= 3 and np.std(v, ddof=1) > 0:
            se = np.std(v, ddof=1) / math.sqrt(len(v))
            out[name] = {"days": len(v), "old_minus_young_bp": r2(np.mean(v)), "t": r2(np.mean(v) / se), "mde": r2(2.8 * se)}
        else:
            out[name] = {"days": len(v)}
    cond = []
    for b in ("lo", "mid", "hi"):
        v = [x[1] for x in paired_age_diff(rows, cuts, b)]
        if v:
            cond.append(float(np.mean(v)))
    out["within_depth_tertiles_avg_bp_info"] = r2(np.mean(cond)) if cond else None
    h1, h2, p = out["h1"], out["h2"], out["pooled"]
    if (h1.get("mde") or 99) > MDE_MAX or (h2.get("mde") or 99) > MDE_MAX:
        out["verdict"] = "UNPROVEN (MDE > 10 bp)"
    elif (h1.get("old_minus_young_bp") or -1) > 0 and (h2.get("old_minus_young_bp") or -1) > 0 and (p.get("t") or 0) >= AGE_T:
        out["verdict"] = "SUPPORTED (longer dips recover better)"
    else:
        out["verdict"] = "NOT SUPPORTED"
    out["survives_depth_conditioning"] = (out["within_depth_tertiles_avg_bp_info"] or -1) > 0
    return out


def spearman(rows):
    from scipy.stats import spearmanr
    M = np.array([[r[f] for f in FEATURES] for r in rows], float)
    ok = np.isfinite(M).all(axis=1)
    rho = spearmanr(M[ok]).correlation if ok.sum() > 10 else None
    return {f"{a}~{b}": r2(rho[i][j]) for i, a in enumerate(FEATURES) for j, b in enumerate(FEATURES) if i < j} if rho is not None else None


def canary(rows, cuts, plant=15.0):
    """amended_1 M3 on real rows (review fix): first make a null by shuffling outcomes WITHIN each day (seeded), then add
    +plant to SPEED-high events. Pass = SPEED_hi reads EDGE and no cell with NO SPEED-high events reads EDGE (the
    per-cell 'within 2 SE of plant x share' check was dropped: 15 cells at 2 SE fail by chance on a null; deviation
    recorded in the prereg). With plant = 0 it must FAIL. Pure."""
    by_day = defaultdict(list)
    for r in rows:
        by_day[r["day"]].append(r)
    pl = []
    for day, rr in by_day.items():
        rng = np.random.default_rng(hkey("dipanat-canary", day) % 2 ** 32)
        perm = rng.permutation(len(rr))
        for i, r in enumerate(rr):
            src = rr[perm[i]]
            x = dict(r)
            for k in ("gross", "ctl", "later", "unc", "ctl_turn", "gross5", "ctl5", "spy", "bracket"):
                x[k] = src.get(k, np.nan)
            if tertile(x["SPEED"], cuts["SPEED"]) == "hi":
                x["gross"] = x["gross"] + plant
            pl.append(x)
    add_lifts(pl)
    cs = cells(pl, cuts)
    if edge_cell(cs["SPEED_hi"])["verdict"] != "EDGE":
        return False
    for name, rr in cs.items():      # no leak: cells without SPEED-high events must not read EDGE
        if name == "SPEED_hi" or not rr:
            continue
        share = np.mean([tertile(r["SPEED"], cuts["SPEED"]) == "hi" for r in rr])
        if share == 0 and edge_cell(rr)["verdict"] == "EDGE":
            return False
    return True


# ---------------------------------------------------------------- I/O

def load_pair(fp, prev):
    from relative_strength_scan import load_day
    from board_structure_family import universe
    from evening_family import ALL_ETF
    import news_source as ns
    day, pday = load_day(fp), load_day(prev)
    if day["idx"].get("SPY") is None:
        return None
    syms, rows_i, pcs = [], [], []
    for s in universe(day, pday, set(ALL_ETF)):
        i, j = day["idx"][s], pday["idx"].get(s)
        if j is None:
            continue
        pc = ns.last_close(pday["c"][j])
        if pc > 0:
            syms.append(s)
            rows_i.append(i)
            pcs.append(pc)
    ix = np.array(rows_i, int)
    return {"syms": syms, **{k: day[k][ix].astype(float) for k in ("o", "h", "l", "c", "v")}, "pc": np.array(pcs),
            "spy_c": day["c"][day["idx"]["SPY"]].astype(float)}


def events():
    from evening_family import WH_SIP
    OUT.mkdir(parents=True, exist_ok=True)
    files = sorted(WH_SIP.glob("20??-??-??.npz"))
    for lo, hi, tag in ((*SEARCH, "search"), (*CONFIRM, "confirm")):
        dst = OUT / f"events_{tag}.json"
        done = json.loads(dst.read_text()) if dst.exists() else {"rows": [], "days": [], "counts": {}}
        counts = defaultdict(int, done["counts"])
        for i, fp in enumerate(files):
            if not (lo <= fp.stem <= hi) or i == 0 or fp.stem in done["days"]:
                continue
            d = load_pair(fp, files[i - 1])
            if d is None:
                counts["day_no_spy"] += 1
                continue
            done["rows"] += day_events(fp.stem, d, counts)
            done["days"].append(fp.stem)
            done["counts"] = dict(counts)
            dst.write_text(json.dumps(done, default=lambda x: None if isinstance(x, float) and not np.isfinite(x) else x))
            print(tag, fp.stem, counts["events"], flush=True)


def load_events(tag):
    d = json.loads((OUT / f"events_{tag}.json").read_text())
    rows = [{k: (np.nan if v is None else v) for k, v in r.items()} for r in d["rows"]]
    return [r for r in rows if "blind" not in r], [r for r in rows if "blind" in r], d["counts"]


def edge(write=True):
    ev, bl, counts = load_events("search")
    add_lifts(ev)
    cuts = cut_points(ev)
    cs = cells(ev, cuts)
    out = {"prereg": "docs/studies/dip_anatomy_prereg.json (amended_1)", "counts": counts, "cut_points": cuts,
           "events_per_cell": {k: [len(halves(v)[0]), len(halves(v)[1])] for k, v in cs.items()},
           "events_per_hour_by_tertile": {f"{f}_{b}": dict(sorted(__import__("collections").Counter(
               9 + (30 + r["t"]) // 60 for r in cs[f"{f}_{b}"]).items())) for f in FEATURES for b in ("lo", "mid", "hi")},
           "spearman": spearman(ev), "cells": {k: edge_cell(v) for k, v in cs.items()},
           "age_question": age_question(ev, cuts), "canary_pass": canary(ev, cuts)}
    bl_out = {}
    for a in BLIND_AGES:
        rr = [dict(r, lift=r["gross"] - r["ctl"]) for r in bl if r["blind"] == a]
        t, _ = day_t(rr, "lift")
        bl_out[f"age_{a}"] = {"n": len(rr), "lift": r2(mean(rr, "lift")), "t": r2(t)}
    out["blind_entry_info"] = bl_out
    try:
        cev, _, _ = load_events("confirm")
        add_lifts(cev)
        cc = cells(cev, cuts)
        out["confirm_info"] = {k: {"n": len(v), "lift": r2(mean(v, "lift")), "t": r2(day_t(v, "lift")[0])} for k, v in cc.items()}
    except FileNotFoundError:
        out["confirm_info"] = None
    if write:
        (OUT / "edge.json").write_text(json.dumps(out, indent=1, default=str))
    lines = ["# Dip anatomy - EDGE (gross, all events; prereg docs/studies/dip_anatomy_prereg.json amended_1)", "",
             "| cell | verdict | n h1/h2 | lift h1 | lift h2 | day-mean h1/h2 | t | MDE h1/h2 | later | unc | ctl / later / unc means | gross |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for k, s in out["cells"].items():
        mm = s["control_mismatch"]
        lines.append(f"| {k} | {s['verdict']} | {s['n_h1']}/{s['n_h2']} | {s['lift_h1']} | {s['lift_h2']} | "
                     f"{s['lift_daymean_h1']}/{s['lift_daymean_h2']} | {s['t']} | {s['mde_h1']}/{s['mde_h2']} | "
                     f"{s['lift_same_name_later']} | {s['lift_unconditional']} | {mm['ctl']['mean']} / {mm['later']['mean']} / {mm['ctl']['unc_mean']} | {s['gross']} |")
    lines += ["", f"Age question: {out['age_question']['verdict']} - {json.dumps(out['age_question'])}",
              f"Canary pass: {out['canary_pass']}", f"Blind entry (info): {json.dumps(bl_out)}"]
    if write:
        (OUT / "report.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return out, ev, cuts


def market_hours_now():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    hm = datetime.now(ZoneInfo("America/New_York")).strftime("%H%M")
    return "0900" <= hm < "1605"


def spread_lookup():
    import news_source as ns
    main = json.load(open(ns.QUOTES)) if ns.QUOTES.exists() else {}
    own = json.loads(OWNQ.read_text()) if OWNQ.exists() else {}

    def f(day, sym, t):
        k = ns.qkey(day, sym, t)
        return main[k] if k in main else (own[k] if k in own else "missing")
    return f


def fetch(keys, n=4):
    from multiprocessing import Process
    import news_source as ns
    if market_hours_now():
        print("market hours - stopping before fetch; rerun after 16:05", flush=True)
        sys.exit(0)
    main = json.load(open(ns.QUOTES)) if ns.QUOTES.exists() else {}
    plan = [("dipanat", d, s, t, 0.0) for d, s, t in keys]
    parts = [OUT / f"quotes_part{i}.json" for i in range(n)]
    procs = [Process(target=ns.fetch_quotes, args=(plan[i::n],), kwargs={"path": parts[i], "skip": set(main)}) for i in range(n)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
    own = json.loads(OWNQ.read_text()) if OWNQ.exists() else {}
    for p in parts:
        if p.exists():
            own.update(json.loads(p.read_text()))
    OWNQ.write_text(json.dumps(own))


def draw(rows, days=None):
    f = spread_lookup()
    traded, missing = [], []
    for half in halves([r for r in rows if days is None or r["day"] in days]):
        n_ok = 0
        for r in sorted(half, key=lambda r: hkey("dipanat", r["day"], r["sym"], r["t"])):
            if n_ok >= CAP_PER_HALF:
                break
            sp = f(r["day"], r["sym"], r["t"])
            if sp == "missing":
                missing.append((r["day"], r["sym"], r["t"]))
                n_ok += 1
                continue
            if sp is None or sp > GATE_BP:
                continue
            traded.append({**r, "spread": sp, "net": r["gross"] - sp, "stress": r["gross"] - STRESS * sp})
            n_ok += 1
    return traded, missing


def draw_fetched(rows, days=None, rounds=6):
    for _ in range(rounds):
        tr, miss = draw(rows, days)
        print("traded", len(tr), "missing", len(miss), flush=True)
        if not miss:
            return tr
        fetch(sorted(set(miss)))
    return draw(rows, days)[0]


def run():
    from early_stop import gross_screen, screen, screen_days
    out, ev, cuts = edge()
    cs = cells(ev, cuts)
    prof = {}
    for k, s in out["cells"].items():
        if s["verdict"] != "EDGE":
            prof[k] = {"verdict": "not quoted (PROFITABLE requires EDGE)"}
            continue
        rows = cs[k]
        v, gst = gross_screen([{"day": r["day"], "gross": r["gross"]} for r in rows], pass_mean=MIN_NET)
        if v == "SKIP_QUOTES":
            prof[k] = {"verdict": "FAIL-gross", "gross_screen": gst}
            continue
        first = draw_fetched(rows, set(screen_days([r["day"] for r in rows])))
        sv, sst = screen(first, key="net", pass_mean=MIN_NET)
        if sv == "STOP":
            prof[k] = {"verdict": "FAIL-early", "screen": sst}
            continue
        tr = draw_fetched(rows)
        a, b = halves(tr)
        na, nb, sa, sb = mean(a, "net"), mean(b, "net"), mean(a, "stress"), mean(b, "stress")
        wa, wb = winsor([r["net"] for r in a]), winsor([r["net"] for r in b])
        ok = len(a) >= MIN_N and len(b) >= MIN_N and all(x is not None and x >= MIN_NET for x in (na, nb)) and \
            all(x is not None and x > 0 for x in (sa, sb, wa, wb))
        prof[k] = {"verdict": "PROFITABLE" if ok else "NOT PROFITABLE", "n_h1": len(a), "n_h2": len(b), "net_h1": r2(na),
                   "net_h2": r2(nb), "stress_h1": r2(sa), "stress_h2": r2(sb), "winsor_h1": r2(wa), "winsor_h2": r2(wb)}
    out["PROFITABLE"] = prof
    (OUT / "result.json").write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps(prof, indent=1))


if __name__ == "__main__":
    {"events": events, "edge": edge, "run": run}[sys.argv[1]]()
