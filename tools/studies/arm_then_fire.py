#!/usr/bin/env python3
"""arm_then_fire - a non-dilution headline ARMS a name; does a later price/volume trigger (FIRE) on it do better than
the same trigger on unarmed names at about the same time, and does the arming age matter? Operator 11/20.
Prereg + amended_1: docs/studies/arm_then_fire_prereg.json.

    .venv/bin/python tools/studies/arm_then_fire.py fires    # CPU: every trigger fire (uses stack_search day caches)
    .venv/bin/python tools/studies/arm_then_fire.py news     # after 16:05 ET: Alpaca news for the universe (shared key)
    .venv/bin/python tools/studies/arm_then_fire.py score    # arms, cells, controls, placebos, canaries, verdicts

Outcome = the stack_search cache's beta-adjusted 30-minute gross G[name, t] (buy open t+1, sell close t+30).
"""
from __future__ import annotations

import hashlib
import json
import math
import pickle
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))

OUT = ROOT / "ai_reports" / "arm_then_fire"
SS_CACHE = ROOT / "ai_reports" / "stack_search" / "days"
ET = ZoneInfo("America/New_York")
SEARCH = ("2026-04-28", "2026-08-31")
SPLIT = "2026-07-15"
CONFIRM = ("2026-09-01", "2026-11-19")
T_LO, T_HI, REFRACT, PEER_MIN = 15, 359, 30, 15
T_CRIT, MDE_K, MDE_MAX = 2.87, 3.71, 12.0
TRIGS = ("F1", "F2", "F3", "F4")
WINDOWS = {"W1": (0, 3600), "W2": (3600, 6 * 3600), "W3": (6 * 3600, 30 * 3600)}
UNARMED_H = 72 * 3600
BEARISH = re.compile(r"\b(offering|dilut\w*|pricing of|registered direct|shelf|S-1|S-3|warrant|reverse split|"
                     r"going concern|delist\w*|downgrade[sd]?|cuts? (?:price )?target)\b", re.I)
ATM = re.compile(r"\bATM\b")
REACTIVE = re.compile(r"(shares are trading|stock is moving|why .{0,60}shares|movers|trading higher|trading lower|"
                      r"stocks moving)", re.I)
EARNINGS = re.compile(r"\b(earnings|EPS|results|guidance|Q[1-4])\b", re.I)


def hkey(*p):
    return int(hashlib.sha256("|".join(str(x) for x in p).encode()).hexdigest(), 16)


def bar_open_ts(day: str, t: int) -> float:
    return datetime.fromisoformat(day + "T09:30").replace(tzinfo=ET).timestamp() + 60 * t


# ---------------------------------------------------------------- headline classes (pure)

def classify(h: dict) -> str:
    """'skip' (recap / reactive / updated late), 'bearish', or 'qualify'. Pure."""
    text = h.get("headline") or ""
    if (h.get("n_symbols") or 1) >= 5 or REACTIVE.search(text):
        return "skip"
    if h.get("updated") is not None and h["updated"] > h["t"] + 60:
        return "skip"
    if BEARISH.search(text) or ATM.search(text):
        return "bearish"
    return "qualify"


def arm_state(headlines: list[dict], fire_open: float):
    """-> (window or 'UNARMED' or None (excluded), earnings flag). headlines: [{t, headline, n_symbols, updated}],
    only those with t <= fire_open count (amended_1 #1). Pure."""
    past = [h for h in headlines if h["t"] <= fire_open and fire_open - h["t"] <= UNARMED_H]
    cls = [(h, classify(h)) for h in past]
    q = [h for h, c in cls if c == "qualify"]
    bear = any(c == "bearish" for _, c in cls)
    if not q:
        return ("UNARMED" if not bear else None), False
    last = max(q, key=lambda h: h["t"])
    age = fire_open - last["t"]
    earn = any(EARNINGS.search(h.get("headline") or "") for h in q if fire_open - h["t"] <= age + 1)
    for w, (lo, hi) in WINDOWS.items():
        if lo <= age < hi:
            if w == "W1" and age < 120:            # fire bar at least 2 minutes after the headline (#2)
                return None, earn
            return w, earn
    return None, earn                              # 30-72 h: excluded from both groups (#6)


# ---------------------------------------------------------------- fires (pure on layer masks)

def first_cross(mask: np.ndarray):
    idx = np.where(mask)[0]
    return int(idx[0]) if len(idx) else None


def fires_for_name(lay: dict, chg: np.ndarray, G_row: np.ndarray, px_row: np.ndarray) -> list[tuple[str, int]]:
    """F1 RVOL first crossing, F4 +3% first crossing (a crossing before 09:45 -> no fire that day, #5), F2 breakout and
    F3 turn with a 30-minute refractory; window 09:45-15:29; price band and finite outcome. Pure."""
    out = []
    ok = px_row & np.isfinite(G_row)
    for trig, mask in (("F1", lay["M1"]), ("F4", np.nan_to_num(chg) >= 0.03)):
        t = first_cross(mask)
        if t is not None and T_LO <= t <= T_HI and ok[t]:
            out.append((trig, t))
    for trig, key in (("F2", "T2"), ("F3", "T1")):
        nxt = -1
        for t in np.where(lay[key])[0]:
            if T_LO <= t <= T_HI and t >= nxt and ok[t]:
                out.append((trig, int(t)))
                nxt = t + REFRACT
    return out


def fires():
    """Per day: every fire with its outcome G, volatility tercile, opening-gap bucket; plus the day's G and px."""
    from stack_search import layers, day_arrays, load_days_pairs
    OUT.mkdir(parents=True, exist_ok=True)
    dst = OUT / "fires.pkl"
    res = pickle.load(open(dst, "rb")) if dst.exists() else {"days": {}}
    closes_hist = defaultdict(list)
    for fp, prev in load_days_pairs("2026-03-01", CONFIRM[1]):
        d, p = day_arrays(fp, prev)
        if d is None:
            continue
        day = fp.stem
        cache = SS_CACHE / f"{day}.pkl"
        if day >= SEARCH[0] and day not in res["days"] and cache.exists():
            c = pickle.load(open(cache, "rb"))
            assert c["syms"] == d["syms"]
            G, px = c["G"].astype(float), c["px"]
            vol = []
            for s in d["syms"]:
                h = closes_hist.get(s, [])[-21:]
                vol.append(float(np.std(np.diff(np.log(h)), ddof=1)) if len(h) >= 15 else np.nan)
            v = np.array(vol)
            q = np.nanquantile(v, [1 / 3, 2 / 3]) if np.isfinite(v).sum() >= 30 else [np.nan, np.nan]
            ev = []
            for k, s in enumerate(d["syms"]):
                lay, chg = layers(d["o"][k], d["h"][k], d["l"][k], d["c"][k], d["v"][k],
                                  p["c"][k], p["h"][k], p["l"][k], p["v"][k], d["spy_c"])
                pc = p["c"][k][np.isfinite(p["c"][k])]
                o0 = d["o"][k][np.isfinite(d["o"][k])]
                gap = (o0[0] / pc[-1] - 1) if len(pc) and len(o0) else np.nan
                gb = None if not np.isfinite(gap) else (0 if gap < -0.01 else (1 if gap <= 0.01 else 2))
                vt = None if not np.isfinite(v[k]) or not np.isfinite(q[0]) else int((v[k] > q[0]) + (v[k] > q[1]))
                for trig, t in fires_for_name(lay, chg, G[k], px[k]):
                    ev.append({"day": day, "sym": s, "k": k, "trig": trig, "t": t, "g": float(G[k, t]), "vt": vt, "gb": gb})
            res["days"][day] = {"fires": ev, "syms": d["syms"], "G": c["G"], "px": px}
            pickle.dump(res, open(dst, "wb"))
            print(day, len(ev), flush=True)
        for k, s in enumerate(d["syms"]):
            cc = d["c"][k][np.isfinite(d["c"][k])]
            if len(cc):
                closes_hist[s].append(float(cc[-1]))


def news():
    """Alpaca news for every universe name, 2026-04-23..2026-11-19 (no limit: alpaca-py pages internally)."""
    hm = datetime.now(ET).strftime("%H%M")
    if "0900" <= hm < "1605":
        sys.exit("market hours - rerun after 16:05 ET (shared data key)")
    from alpaca.data.historical import NewsClient
    from alpaca.data.requests import NewsRequest
    import time
    k = json.load(open(ROOT / "secrets_data.json"))
    nc = NewsClient(k["alpaca_data_api_key"], k["alpaca_data_secret_key"])
    res = pickle.load(open(OUT / "fires.pkl", "rb"))
    syms = sorted({s for d in res["days"].values() for s in d["syms"]})
    dst = OUT / "news.pkl"
    out = pickle.load(open(dst, "rb")) if dst.exists() else {"news": {}, "failed": []}
    for i in range(0, len(syms), 25):
        ch = [s for s in syms[i:i + 25] if s not in out["news"]]
        if not ch:
            continue
        got = None
        for attempt in range(6):
            try:
                got = nc.get_news(NewsRequest(symbols=",".join(ch), start=datetime(2026, 4, 23, tzinfo=timezone.utc),
                                              end=datetime(2026, 11, 20, tzinfo=timezone.utc)))
                break
            except Exception as e:                  # noqa: BLE001
                time.sleep(60 if "429" in str(e) else 5 * (attempt + 1))
        if got is None:
            out["failed"].append(ch)
            continue
        per = {s: [] for s in ch}
        for it in (got.data or {}).get("news", []):
            row = {"t": it.created_at.timestamp(), "updated": it.updated_at.timestamp() if it.updated_at else None,
                   "headline": it.headline, "n_symbols": len(it.symbols or [])}
            for s in it.symbols or []:
                if s in per:
                    per[s].append(row)
        out["news"].update(per)
        pickle.dump(out, open(dst, "wb"))
        print("news", i, len(syms), flush=True)
    print("NEWS_DONE failed batches", len(out["failed"]), flush=True)


# ---------------------------------------------------------------- scoring (pure on fires + news)

def assign(res, news, shift=None):
    """Arm state for every fire. shift: {sym: k sessions} moves that name's headlines (placebo). -> list of fire dicts
    with 'arm' and 'earn'. Pure."""
    days = sorted(res["days"])
    pos = {d: i for i, d in enumerate(days)}
    out = []
    for day in days:
        for f in res["days"][day]["fires"]:
            hs = news.get(f["sym"], [])
            if shift and shift.get(f["sym"]):
                k = shift[f["sym"]]
                j = pos[day] - k
                if not (0 <= j < len(days)):
                    continue
                delta = bar_open_ts(day, 0) - bar_open_ts(days[j], 0)
                hs = [dict(h, t=h["t"] + delta) for h in hs]
            arm, earn = arm_state(hs, bar_open_ts(day, f["t"]))
            out.append(dict(f, arm=arm, earn=earn))
    return out


def lifts(fs, cell_w, trig, ex_earn=False):
    """Armed (window cell_w) fires of `trig` minus the peer-count-weighted mean of UNARMED fires of the same trigger
    within +/-15 min on other names in the same volatility tercile and gap bucket. -> (rows, dropped, kept_gross,
    dropped_gross). Pure."""
    by_day = defaultdict(list)
    for f in fs:
        if f["trig"] == trig:
            by_day[f["day"]].append(f)
    rows, dropped, kg, dg = [], 0, [], []
    for day, ff in by_day.items():
        un = [f for f in ff if f["arm"] == "UNARMED"]
        for f in ff:
            if f["arm"] != cell_w or (ex_earn and f["earn"]):
                continue
            peers = [u["g"] for u in un if u["sym"] != f["sym"] and abs(u["t"] - f["t"]) <= PEER_MIN
                     and u["vt"] == f["vt"] and u["gb"] == f["gb"]]
            if not peers:
                dropped += 1
                dg.append(f["g"])
                continue
            kg.append(f["g"])
            rows.append({"day": day, "lift": f["g"] - float(np.mean(peers)), "w": len(peers) / (len(peers) + 1),
                         "sym": f["sym"], "t": f["t"], "g": f["g"], "peer_mean": float(np.mean(peers))})
    return rows, dropped, kg, dg


def day_stats(rows, key="lift", weighted=True):
    per = defaultdict(lambda: [0.0, 0.0])
    for r in rows:
        if r.get(key) is None or not np.isfinite(r[key]):
            continue
        w = r.get("w", 1.0) if weighted else 1.0
        per[r["day"]][0] += w * r[key]
        per[r["day"]][1] += w
    m = [a / b for a, b in per.values() if b > 0]
    if len(m) < 3:
        return (float(np.mean(m)) if m else None), None, None, len(m)
    se = float(np.std(m, ddof=1) / math.sqrt(len(m)))
    return float(np.mean(m)), se, (float(np.mean(m)) / se if se > 0 else None), len(m)


def winsor(x, top=0.01):
    x = np.asarray([v for v in x if v is not None and np.isfinite(v)], float)
    return float(np.minimum(x, np.quantile(x, 1 - top)).mean()) if len(x) else None


def cell_stats(fs, w, trig):
    rows, dropped, kg, dg = lifts(fs, w, trig)
    a = [r for r in rows if r["day"] < SPLIT]
    b = [r for r in rows if r["day"] >= SPLIT]
    ma, sea, _, _ = day_stats(a)
    mb, seb, _, _ = day_stats(b)
    mp, _, tp, nd = day_stats(rows)
    n = len(rows) + dropped
    st = {"n_events": n, "n_with_peer": len(rows), "dropped_share": round(dropped / n, 3) if n else None,
          "kept_gross": float(np.mean(kg)) if kg else None, "dropped_gross": float(np.mean(dg)) if dg else None,
          "lift_h1": ma, "lift_h2": mb, "lift": mp, "t": tp, "days": nd,
          "mde_h1": MDE_K * sea if sea else None, "mde_h2": MDE_K * seb if seb else None,
          "winsor_h1": winsor([r["lift"] for r in a]), "winsor_h2": winsor([r["lift"] for r in b]), "rows": rows}
    st["edge_core"] = bool((ma or -1) > 0 and (mb or -1) > 0 and (tp or 0) >= T_CRIT and (st["winsor_h1"] or -1) > 0
                           and (st["winsor_h2"] or -1) > 0)
    return st


def secondary(res, fs, rows, trig):
    """Same-name later (entry exit+2..exit+62), random same-minute names, same-name placebo (same trigger on an
    unarmed session within +/-20 sessions), unconditional random minute. -> dict of pooled lift means and the
    control-vs-unconditional gap check. Pure."""
    days = sorted(res["days"])
    pos = {d: i for i, d in enumerate(days)}
    un_by = defaultdict(list)
    for f in fs:
        if f["trig"] == trig and f["arm"] == "UNARMED":
            un_by[f["sym"]].append(f)
    later, rand, plac, unc, peer_g, later_g = [], [], [], [], [], []
    for r in rows:
        D = res["days"][r["day"]]
        G, px, syms = np.asarray(D["G"], float), D["px"], D["syms"]
        k = syms.index(r["sym"])
        cand = [u for u in range(r["t"] + 32, min(r["t"] + 92, T_HI) + 1) if np.isfinite(G[k, u])]
        if cand:
            g = G[k, cand[hkey("atf-later", r["day"], r["sym"], r["t"]) % len(cand)]]
            later.append({"day": r["day"], "x": r["g"] - g})
            later_g.append(g)
        ok = [j for j in range(len(syms)) if j != k and px[j, r["t"]] and np.isfinite(G[j, r["t"]])]
        if ok:
            pick = sorted(ok, key=lambda j: hkey("atf-rand", r["day"], syms[j], r["t"]))[:5]
            rand.append({"day": r["day"], "x": r["g"] - float(np.mean(G[pick, r["t"]]))})
        pl = [u for u in un_by.get(r["sym"], []) if u["day"] != r["day"] and abs(pos[u["day"]] - pos[r["day"]]) <= 20]
        if pl:
            u = pl[hkey("atf-plac", r["day"], r["sym"]) % len(pl)]
            plac.append({"day": r["day"], "x": r["g"] - u["g"]})
        okm = np.argwhere(px & np.isfinite(G))
        okm = okm[(okm[:, 1] >= T_LO) & (okm[:, 1] <= T_HI)]
        if len(okm):
            j, u = okm[hkey("atf-unc", r["day"], r["sym"], r["t"]) % len(okm)]
            unc.append(G[j, u])
        peer_g.append(r["peer_mean"])
    m = lambda rr: day_stats(rr, "x", weighted=False)[0]
    out = {"same_name_later": m(later), "random_same_minute": m(rand), "same_name_placebo": m(plac),
           "n_placebo": len(plac), "unconditional_mean": float(np.mean(unc)) if unc else None,
           "peer_control_mean": float(np.mean(peer_g)) if peer_g else None,
           "later_control_mean": float(np.mean(later_g)) if later_g else None}
    gaps = {}
    for name, xs in (("peer", peer_g), ("later", later_g)):
        if len(xs) > 2 and len(unc) > 2:
            se = math.sqrt(np.var(xs, ddof=1) / len(xs) + np.var(unc, ddof=1) / len(unc))
            gaps[name] = abs(np.mean(xs) - np.mean(unc)) <= 2 * se
        else:
            gaps[name] = False
    out["gaps_ok"] = gaps
    out["ok"] = all((out[x] or -1) > 0 for x in ("same_name_later", "random_same_minute", "same_name_placebo")) and all(gaps.values())
    return out


def score():
    res = pickle.load(open(OUT / "fires.pkl", "rb"))
    nw = pickle.load(open(OUT / "news.pkl", "rb"))
    news, failed = nw["news"], nw["failed"]
    res["days"] = {d: v for d, v in res["days"].items() if SEARCH[0] <= d <= SEARCH[1]}
    fs = assign(res, news)
    cov = defaultdict(int)
    for hs in news.values():
        for h in hs:
            cov[datetime.fromtimestamp(h["t"], ET).strftime("%Y-%m")] += 1
    namedays = {(f["day"], f["sym"]): f["arm"] for f in fs}
    out = {"prereg": "docs/studies/arm_then_fire_prereg.json (amended_1)", "news_per_month": dict(sorted(cov.items())),
           "unarmed_share_of_name_days": round(sum(1 for a in namedays.values() if a == "UNARMED") / max(len(namedays), 1), 3),
           "fetch_failed_batches": len(failed), "cells": {}}
    scored = []
    for w in WINDOWS:
        for trig in TRIGS:
            c = f"{w}x{trig}"
            st = cell_stats(fs, w, trig)
            sec = secondary(res, fs, st["rows"], trig)
            ex = cell_stats([f for f in fs if not (f["arm"] == w and f["earn"])], w, trig)
            hist = defaultdict(lambda: [0, 0])
            for f in fs:
                if f["trig"] == trig and f["arm"] in (w, "UNARMED"):
                    hist[9 + (30 + f["t"]) // 60][0 if f["arm"] == w else 1] += 1
            st.update({"secondary": sec, "ex_earnings": {k: ex[k] for k in ("lift_h1", "lift_h2", "t", "n_with_peer")},
                       "fire_hour_hist_armed_vs_unarmed": dict(sorted(hist.items()))})
            if (st["mde_h1"] or 99) > MDE_MAX or (st["mde_h2"] or 99) > MDE_MAX:
                st["verdict"] = "UNPROVEN (MDE)"
            elif (st["dropped_share"] or 1) > 0.30:
                st["verdict"] = "UNPROVEN (> 30% without a peer)"
            elif st["edge_core"] and sec["ok"]:
                st["verdict"] = "EDGE" if (ex["edge_core"]) else "earnings-day drift (not arming)"
                scored.append(c)
            else:
                st["verdict"] = "NO EDGE"
                scored.append(c)
            st.pop("rows")
            out["cells"][c] = st
    # canaries: shuffle news across names within day (20 seeds); arm-shift placebo (20 shifts); planted +15 bp
    syms = sorted(news)
    shuf_rate, shift_rate = defaultdict(float), defaultdict(float)
    for seed in range(20):
        rng = np.random.default_rng(seed)
        perm = dict(zip(syms, rng.permutation(syms)))
        fs_s = assign(res, {s: news[perm[s]] for s in syms})
        sh = {s: int(rng.choice([-5, -4, -3, -2, -1, 1, 2, 3, 4, 5])) for s in syms}
        fs_h = assign(res, news, shift=sh)
        for w in WINDOWS:
            for trig in TRIGS:
                shuf_rate[f"{w}x{trig}"] += cell_stats(fs_s, w, trig)["edge_core"] / 20
                shift_rate[f"{w}x{trig}"] += cell_stats(fs_h, w, trig)["edge_core"] / 20
    smallest = min(scored, key=lambda c: out["cells"][c]["n_with_peer"]) if scored else None
    planted = {}
    for c in {"W1xF2", smallest} - {None}:
        w, trig = c.split("x")
        fp = [dict(f, g=f["g"] + 15.0) if (f["arm"] == w and f["trig"] == trig) else f for f in fs]
        planted[c] = cell_stats(fp, w, trig)["edge_core"]
    out["canaries"] = {"shuffle_pass_rates": dict(shuf_rate), "arm_shift_pass_rates": dict(shift_rate), "planted": planted}
    canary_ok = all(v <= 0.10 for v in shuf_rate.values()) and all(v <= 0.10 for v in shift_rate.values()) and all(planted.values())
    nf = len(failed) * 25 / max(len(syms), 1)
    if nf > 0.02 or not canary_ok:
        out["VERDICT"] = "NO VERDICT (" + ("fetch failures > 2%" if nf > 0.02 else "canary failed") + ")"
    else:
        out["VERDICT"] = {c: v["verdict"] for c, v in out["cells"].items()}
    for trig in TRIGS:   # the operator's timing question (information)
        out[f"timing_{trig}"] = {w: out["cells"][f"{w}x{trig}"]["lift"] for w in WINDOWS}
    (OUT / "score.json").write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps({"VERDICT": out["VERDICT"], "canaries": out["canaries"],
                      "timing": {t: out[f"timing_{t}"] for t in TRIGS}}, indent=1, default=str))


if __name__ == "__main__":
    {"fires": fires, "news": news, "score": score}[sys.argv[1]]()
