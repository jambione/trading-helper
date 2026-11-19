#!/usr/bin/env python3
"""opening_grid - which opening setup (signal x window x name filter) is best after real spreads, and does the best one
beat what the same search over random entries finds by luck? (operator 11/19). Prereg (with amended_1):
docs/studies/opening_grid_prereg.json.

    .venv/bin/python tools/studies/opening_grid.py build    # CPU: per-day cache of signals, filters, bracket gross
    .venv/bin/python tools/studies/opening_grid.py run      # quotes (rounds 1 and 2, then confirm info) + null + verdict

Fetches use the data key shared with the live dashboard: run 'run' only after 16:05 ET.
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

OUT = ROOT / "ai_reports" / "opening_grid"
CACHE = OUT / "cache"
OWNQ = OUT / "quotes.json"
SEARCH = ("2026-04-28", "2026-09-23")
CONFIRM = ("2026-09-24", "2026-11-18")
T_SIG = 70                      # signal/filter bars stored: 09:30 .. 10:39 (windows + 5-min neighbours)
T_GROSS = 375                   # bracket gross stored for every entry bar 0..374 (same-name later control)
UP, DOWN, HORIZON = 20.0, 50.0, 15
GATE_BP, CAP, MIN_N = 15.0, 400, 300
T_PASS, EDGE_T, MIN_EDGE = 3.3, 2.5, 2.0
N_NULL = 200
BITS = {"SQUARE": 1, "PRESQ": 2, "ORB": 4, "HEAT20": 8}
WINDOWS = {"W1": (1, 14), "W2": (15, 29), "W3": (30, 59)}
FILTERS = {"F1": (4.0, False, None), "F2": (4.0, False, 1.5), "F3": (4.0, True, None), "F4": (4.0, True, 1.5)}


# ---------------------------------------------------------------- pure building blocks

def bracket_all(o, h, l, c, n=T_GROSS):
    """Gross bp of the desk bracket (+20 before -50 within 15 min, entry open t+1, stop first on a two-touch minute,
    else the last finite close in t+1..t+15) for every t in 0..n-1 at once. Equals holistic_entry.bracket + COST. Pure."""
    g = np.full(n, np.nan)
    t = np.arange(n)
    e = o[t + 1]
    okay = np.isfinite(e) & (e > 0)
    up, dn = e * (1 + UP / 1e4), e * (1 - DOWN / 1e4)
    done = ~okay
    last = np.full(n, np.nan)
    with np.errstate(invalid="ignore"):
        for k in range(1, HORIZON + 1):
            m = t + k
            stop = ~done & np.isfinite(l[m]) & (l[m] <= dn)
            g[stop] = -DOWN
            done |= stop
            tgt = ~done & np.isfinite(h[m]) & (h[m] >= up)
            g[tgt] = UP
            done |= tgt
            cm = c[m]
            last = np.where(np.isfinite(cm), cm, last)
    rest = ~done & np.isfinite(last)
    g[rest] = (last[rest] / e[rest] - 1) * 1e4
    return g


def heat20_turns(cc, hh, ll, off):
    """low_heat_rising.heat_turns without its T_LO/T_HI clip and with heat < 20 (amended_1). Pure."""
    from bar_signal_family import wr_smoothed
    heat = 100.0 + wr_smoothed(hh, ll, cc, n=21, span=7)
    prev = np.concatenate(([np.nan], heat[:-1]))
    with np.errstate(invalid="ignore"):
        up = np.isfinite(heat) & np.isfinite(prev) & (heat - prev > 0.05)
        i = np.arange(off + 2, len(cc))
        hit = i[(heat[i] < 20.0) & up[i] & up[i - 1] & ~up[i - 2]] - off
    return [int(t) for t in hit]


def orb_cross(h, c):
    """The first bar t >= 5 where close[t] > max(high 09:30-09:34) and close[t-1] <= it, else None. Pure."""
    hi = h[:5]
    if not np.isfinite(hi).any():
        return None
    orh = np.nanmax(hi)
    for t in range(5, len(c)):
        if np.isfinite(c[t]) and np.isfinite(c[t - 1]) and c[t] > orh >= c[t - 1]:
            return t
    return None


def pace_series(v, pv, n=T_SIG):
    """Cumulative RTH volume to bar t / the prior session's cumulative RTH volume to the same minute. Pure."""
    a = np.nancumsum(np.nan_to_num(v[:n], nan=0.0))
    b = np.nancumsum(np.nan_to_num(pv[:n], nan=0.0))
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(b > 0, a / b, np.nan)


def make_cell(sig, lo, hi, chg_edge, above, pace_min, name=None):
    return {"sig": sig, "lo": lo, "hi": hi, "edge": chg_edge, "above": above, "pace": pace_min,
            "id": name or f"{sig}|{lo}-{hi}|chg{'>=' if above else '<'}{chg_edge}|pace{pace_min or 'any'}"}


def base_cells():
    return [make_cell(s, *WINDOWS[w], *FILTERS[f], name=f"{s}|{w}|{f}") for s in BITS for w in WINDOWS for f in FILTERS]


def neighbours(cell):
    """Round-2 neighbours (prereg): window edges +/-5 min, chg edge 3 / 6, pace cut 1.0 / 2.0 (pace cells only). Pure."""
    out, c = [], cell
    for lo, hi in ((c["lo"] - 5, c["hi"]), (c["lo"] + 5, c["hi"]), (c["lo"], c["hi"] - 5), (c["lo"], c["hi"] + 5)):
        lo = max(lo, 1)
        if lo <= hi and hi < T_SIG:
            out.append(make_cell(c["sig"], lo, hi, c["edge"], c["above"], c["pace"]))
    for e in (3.0, 6.0):
        out.append(make_cell(c["sig"], c["lo"], c["hi"], e, c["above"], c["pace"]))
    if c["pace"] is not None:
        for p in (1.0, 2.0):
            out.append(make_cell(c["sig"], c["lo"], c["hi"], c["edge"], c["above"], p))
    return [x for x in out if x["id"] != c["id"]]


def filt_mask(d, cell):
    """(names, T_SIG) bool: price $20-$400, day change in the cell's band (>= +1.5%), pace cut. Pure."""
    chg, px, pace = d["chg"], d["px"], d["pace"]
    with np.errstate(invalid="ignore"):
        m = np.isfinite(chg) & (px >= 20) & (px <= 400) & (chg >= 1.5)
        m &= (chg >= cell["edge"]) if cell["above"] else (chg < cell["edge"])
        if cell["pace"] is not None:
            m &= np.isfinite(pace) & (pace >= cell["pace"])
    return m


def candidates(days, cell):
    """[(day, sym, t, gross)] - the first bar in the window with the signal and the filter, per name-day. Pure."""
    out, b = [], BITS[cell["sig"]]
    for day, d in days.items():
        m = filt_mask(d, cell) & ((d["sig"] & b) > 0)
        w = np.zeros(T_SIG, bool)
        w[cell["lo"]:cell["hi"] + 1] = True
        m &= w
        for i in np.where(m.any(axis=1))[0]:
            t = int(np.argmax(m[i]))
            g = d["gross"][i, t]
            if np.isfinite(g):
                out.append((day, d["syms"][i], t, float(g)))
    return out


def hkey(*p):
    return hashlib.sha256("|".join(str(x) for x in p).encode()).hexdigest()


def draw(cands, spread_of, cap=CAP):
    """Walk candidates by the lowest sha256('ogrid'|day|sym|t); keep spread <= 15 bp until cap traded.
    spread_of(day, sym, t) -> bp, None (no quote), or 'missing' (not fetched). -> (traded rows, missing keys). Pure."""
    traded, missing, n_ok = [], [], 0
    for day, sym, t, g in sorted(cands, key=lambda r: hkey("ogrid", r[0], r[1], r[2])):
        if n_ok >= cap:
            break
        sp = spread_of(day, sym, t)
        if sp == "missing":
            missing.append((day, sym, t))
            n_ok += 1                    # optimistic until fetched; the next round re-walks
            continue
        if sp is None or sp > GATE_BP:
            continue
        traded.append({"day": day, "sym": sym, "t": t, "gross": g, "spread": sp, "net": g - sp})
        n_ok += 1
    return traded, missing


def day_t(rows, key="net"):
    per = defaultdict(list)
    for r in rows:
        per[r["day"]].append(r[key])
    m = [np.mean(v) for v in per.values()]
    if len(m) < 3 or np.std(m, ddof=1) == 0:
        return None
    return float(np.mean(m) / (np.std(m, ddof=1) / math.sqrt(len(m))))


def mde(rows, key="net", t=T_PASS):
    per = defaultdict(list)
    for r in rows:
        per[r["day"]].append(r[key])
    m = [np.mean(v) for v in per.values()]
    return float(t * np.std(m, ddof=1) / math.sqrt(len(m))) if len(m) >= 3 else None


def winsor_mean(x, top=0.01):
    x = np.asarray(x, float)
    if not len(x):
        return None
    return float(np.minimum(x, np.quantile(x, 1 - top)).mean())


def summ(rows, n_days):
    if not rows:
        return {"n": 0}
    x = np.array([r["net"] for r in rows])
    days = sorted({r["day"] for r in rows})
    h = days[len(days) // 2]
    a = [r["net"] for r in rows if r["day"] < h]
    b = [r["net"] for r in rows if r["day"] >= h]
    t = day_t(rows)
    return {"n": len(rows), "days": len(days), "mean_net": round(float(x.mean()), 2),
            "mean_gross": round(float(np.mean([r["gross"] for r in rows])), 2),
            "win_pct": round(100 * float((x > 0).mean()), 1), "t_net": round(t, 2) if t is not None else None,
            "mde_bp": round(mde(rows), 1) if mde(rows) is not None else None,
            "half1": round(float(np.mean(a)), 2) if a else None, "half2": round(float(np.mean(b)), 2) if b else None,
            "w_half1": round(winsor_mean(a), 2) if a else None, "w_half2": round(winsor_mean(b), 2) if b else None,
            "trades_per_day": round(len(rows) / n_days, 2),
            "usd_per_day_approx": round(float(x.mean()) / 1e4 * 3200 * len(rows) / n_days, 2),
            "median_spread": round(float(np.median([r["spread"] for r in rows])), 2)}


def choose(stats):
    """Highest t_net among cells with n >= MIN_N; ties by $/day (amended_1). stats: {id: summ}. Pure."""
    ok = [(s.get("t_net") or -99, s.get("usd_per_day_approx") or -99, k) for k, s in stats.items() if s.get("n", 0) >= MIN_N]
    return max(ok)[2] if ok else None


# ---------------------------------------------------------------- controls (gross, no quotes)

def controls(days, cell, rows):
    """(a) same minute, up to 5 other names passing the cell filter; (b) same name, a later minute; (c) unconditional
    random-minute mean in the window. -> dict. Pure given days."""
    lifts, later = [], []
    masks = {}
    for r in rows:
        d = days[r["day"]]
        if r["day"] not in masks:
            masks[r["day"]] = filt_mask(d, cell)
        m = masks[r["day"]]
        others = [i for i in np.where(m[:, r["t"]])[0] if d["syms"][i] != r["sym"] and np.isfinite(d["gross"][i, r["t"]])]
        others = sorted(others, key=lambda i: hkey("ogc", r["day"], d["syms"][i], r["t"]))[:5]
        if others:
            lifts.append({"day": r["day"], "lift": r["gross"] - float(np.mean(d["gross"][others, r["t"]]))})
        i = d["syms"].index(r["sym"])
        span = T_GROSS - 1 - r["t"]
        if span > 0:
            t2 = r["t"] + 1 + int(hkey("ogl", r["day"], r["sym"], r["t"]), 16) % span
            g2 = d["gross"][i, t2]
            if np.isfinite(g2):
                later.append(r["gross"] - float(g2))
    unc = []
    for day, d in days.items():
        m = filt_mask(d, cell)
        g = d["gross"][:, :T_SIG]
        sel = m.copy()
        sel[:, :cell["lo"]] = False
        sel[:, cell["hi"] + 1:] = False
        v = g[sel & np.isfinite(g)]
        if len(v):
            unc.append(float(v.mean()))
    edge_t = day_t(lifts, "lift")
    return {"same_minute_lift": round(float(np.mean([x["lift"] for x in lifts])), 2) if lifts else None,
            "edge_t": round(edge_t, 2) if edge_t is not None else None, "n_same_minute": len(lifts),
            "same_name_later_lift": round(float(np.mean(later)), 2) if later else None,
            "unconditional_gross": round(float(np.mean(unc)), 2) if unc else None}


# ---------------------------------------------------------------- luck null (gross, the whole loop)

def random_cell(days, cell, n_eff, rng):
    """Random minute in the window (t >= 2, i.e. after 09:31) per name-day, kept if the filter passes there;
    subsampled to n_eff. -> gross rows. Pure given rng."""
    rows = []
    lo = max(cell["lo"], 2)
    if lo > cell["hi"]:
        return rows
    for day, d in days.items():
        m = filt_mask(d, cell)
        nn = len(d["syms"])
        tt = rng.integers(lo, cell["hi"] + 1, size=nn)
        g = d["gross"][np.arange(nn), tt]
        keep = m[np.arange(nn), tt] & np.isfinite(g)
        rows += [{"day": day, "gross": float(x)} for x in g[keep]]
    if len(rows) > n_eff:
        idx = rng.choice(len(rows), size=n_eff, replace=False)
        rows = [rows[i] for i in idx]
    return rows


def null_winner(days, cells, n_eff_of, rng):
    """Run the full two-round loop on random entries: 48 cells, top 3 by gross t (n >= MIN_N), their neighbours,
    the winner by t. -> the winner's mean gross (or None). Pure given rng."""
    st, by_id = {}, {}
    for c in cells:
        r = random_cell(days, c, n_eff_of(c), rng)
        t = day_t(r, "gross")
        st[c["id"]] = (t if t is not None else -99, len(r), float(np.mean([x["gross"] for x in r])) if r else None)
        by_id[c["id"]] = c
    top = sorted((v[0], k) for k, v in st.items() if v[1] >= MIN_N)[-3:]
    for _, k in top:
        for nb in neighbours(by_id[k]):
            if nb["id"] in st:
                continue
            r = random_cell(days, nb, n_eff_of(nb), rng)
            t = day_t(r, "gross")
            st[nb["id"]] = (t if t is not None else -99, len(r), float(np.mean([x["gross"] for x in r])) if r else None)
    ok = [(v[0], v[2]) for v in st.values() if v[1] >= MIN_N]
    return max(ok)[1] if ok else None


# ---------------------------------------------------------------- build (CPU)

def build():
    from relative_strength_scan import load_day
    from board_structure_family import universe
    from evening_family import ALL_ETF, WH_SIP, minute_grid
    from opening_square import square_bars, presq_all
    import news_source as ns
    CACHE.mkdir(parents=True, exist_ok=True)
    files = sorted(p for p in WH_SIP.glob("20??-??-??.npz") if p.stem <= CONFIRM[1])
    prev = None
    for fp in files:
        if fp.stem < SEARCH[0]:
            prev = fp
            continue
        dst = CACHE / f"{fp.stem}.npz"
        if dst.exists() or prev is None:
            prev = fp
            continue
        day, pday = load_day(fp), load_day(prev)
        if day["idx"].get("SPY") is None:
            prev = fp
            continue
        syms, G, CHG, PX, PACE, SIG = [], [], [], [], [], []
        for s in universe(day, pday, set(ALL_ETF)):
            i, j = day["idx"][s], pday["idx"].get(s)
            if j is None:
                continue
            pc = ns.last_close(pday["c"][j])
            if not pc > 0:
                continue
            o, h, l, c = (day[k][i] for k in "ohlc")
            cc, hh, ll, off = minute_grid(pday["c"][j], pday["h"][j], pday["l"][j], c, h, l)
            sig = np.zeros(T_SIG, np.uint8)
            for t in square_bars(cc, hh, ll, off):
                if t < T_SIG:
                    sig[t] |= BITS["SQUARE"]
            for t in presq_all(cc, hh, ll, off):
                if t < T_SIG:
                    sig[t] |= BITS["PRESQ"]
            for t in heat20_turns(cc, hh, ll, off):
                if t < T_SIG:
                    sig[t] |= BITS["HEAT20"]
            t_orb = orb_cross(h, c)
            if t_orb is not None and t_orb < T_SIG:
                sig[t_orb] |= BITS["ORB"]
            syms.append(s)
            G.append(bracket_all(o, h, l, c))
            with np.errstate(invalid="ignore"):
                CHG.append((c[:T_SIG] / pc - 1) * 100)
            PX.append(c[:T_SIG])
            PACE.append(pace_series(day["v"][i], pday["v"][j]))
            SIG.append(sig)
        k = day["idx"]["SPY"]
        so, sc = day["o"][k], day["c"][k]
        t = np.arange(T_GROSS)
        with np.errstate(invalid="ignore", divide="ignore"):
            spy = (sc[t + HORIZON] / so[t + 1] - 1) * 1e4
        np.savez_compressed(dst, syms=np.array(syms), gross=np.array(G, np.float32), chg=np.array(CHG, np.float32),
                            px=np.array(PX, np.float32), pace=np.array(PACE, np.float32), sig=np.array(SIG, np.uint8),
                            spy=spy.astype(np.float32))
        print(fp.stem, len(syms), flush=True)
        prev = fp


def load_cache(lo, hi):
    out = {}
    for p in sorted(CACHE.glob("*.npz")):
        if lo <= p.stem <= hi:
            z = np.load(p)
            out[p.stem] = {"syms": [str(s) for s in z["syms"]], "gross": z["gross"].astype(float),
                           "chg": z["chg"].astype(float), "px": z["px"].astype(float),
                           "pace": z["pace"].astype(float), "sig": z["sig"], "spy": z["spy"].astype(float)}
    return out


# ---------------------------------------------------------------- run (quotes + verdict)

def market_hours_now():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    hm = datetime.now(ZoneInfo("America/New_York")).strftime("%H%M")
    return "0900" <= hm < "1605"


def fetch_parallel(keys, n=4):
    """Never fetches 09:00-16:05 ET: the data key is shared with the live dashboard (exit; rerun 'run' after 16:05)."""
    from multiprocessing import Process
    import news_source as ns
    if market_hours_now():
        print("market hours - stopping before fetch; rerun after 16:05 (quotes so far are kept)", flush=True)
        sys.exit(0)
    main = json.load(open(ns.QUOTES)) if ns.QUOTES.exists() else {}
    plan = [("ogrid", d, s, t, 0.0) for d, s, t in keys]
    parts = [OUT / f"quotes_part{i}.json" for i in range(n)]
    procs = [Process(target=ns.fetch_quotes, args=(plan[i::n],), kwargs={"path": parts[i], "skip": set(main)})
             for i in range(n)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
    own = json.loads(OWNQ.read_text()) if OWNQ.exists() else {}
    for p in parts:
        if p.exists():
            own.update(json.loads(p.read_text()))
    OWNQ.write_text(json.dumps(own))


def spread_lookup():
    import news_source as ns
    main = json.load(open(ns.QUOTES)) if ns.QUOTES.exists() else {}
    own = json.loads(OWNQ.read_text()) if OWNQ.exists() else {}

    def f(day, sym, t):
        k = ns.qkey(day, sym, t)
        if k in main:
            return main[k]
        if k in own:
            return own[k]
        return "missing"
    return f


def score_cells(days, cells, n_days, rounds=8):
    """Draw + fetch until no cell needs a missing quote. -> {id: (summ, traded rows, n_candidates)}."""
    from early_stop import gross_screen
    cands = {c["id"]: candidates(days, c) for c in cells}
    screened = {}
    for c in cells:      # gross-first (amended_2): no quotes for a cell whose gross cannot reach the net bar
        v, st = gross_screen([{"day": d, "gross": g} for d, _, _, g in cands[c["id"]]], pass_mean=MIN_EDGE)
        if v == "SKIP_QUOTES":
            screened[c["id"]] = {"n": 0, "FAIL_gross": st}
    for rnd in range(rounds):
        f = spread_lookup()
        res, need = {}, set()
        for cid, st in screened.items():
            res[cid] = (st, [], len(cands[cid]))
        for c in cells:
            if c["id"] in screened:
                continue
            tr, miss = draw(cands[c["id"]], f)
            need.update(miss)
            res[c["id"]] = (summ(tr, n_days), tr, len(cands[c["id"]]))
        print("round", rnd, "cells", len(cells), "missing quotes", len(need), flush=True)
        if not need:
            return res
        fetch_parallel(sorted(need))
    print("WARNING: quotes still missing after", rounds, "rounds", flush=True)
    return res


def run():
    days = load_cache(*SEARCH)
    n_days = len(days)
    cells = base_cells()
    by_id = {c["id"]: c for c in cells}
    res = score_cells(days, cells, n_days)
    top = sorted(((s.get("t_net") or -99), k) for k, (s, _, _) in res.items() if s.get("n", 0) >= MIN_N)[-3:]
    r2 = []
    for _, k in top:
        for nb in neighbours(by_id[k]):
            if nb["id"] not in by_id:
                by_id[nb["id"]] = nb
                r2.append(nb)
    if r2:
        res.update(score_cells(days, r2, n_days))
    stats = {k: v[0] for k, v in res.items()}
    win = choose(stats)
    (OUT / "choice.json").write_text(json.dumps({"chosen": win, "written": __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
                                                 "cells": len(stats)}, indent=1))
    # luck null: same loop on random entries, n per cell matched to the real cell (else the pooled gate rate)
    quoted = sum(v[2] for v in res.values())
    traded = sum(v[0].get("n", 0) for v in res.values())
    rate = traded / quoted if quoted else 0.0
    cand_n = {}

    def n_eff_of(c):
        if c["id"] in res:
            return res[c["id"]][0].get("n", 0)
        if c["id"] not in cand_n:
            cand_n[c["id"]] = len(candidates(days, c))
        return min(CAP, int(round(cand_n[c["id"]] * rate)))
    rng = np.random.default_rng(20261119)
    null = [w for w in (null_winner(days, base_cells(), n_eff_of, rng) for _ in range(N_NULL)) if w is not None]
    p95 = float(np.percentile(null, 95)) if null else None
    out = {"cells": stats, "chosen": win, "null_winner_gross_p95": round(p95, 2) if p95 is not None else None,
           "null_runs": len(null)}
    if win:
        s, rows, _ = res[win]
        ctl = controls(days, by_id[win], rows)
        nbs = [stats.get(nb["id"], {}) for nb in neighbours(by_id[win])]
        nb_pos = [x.get("mean_net", -1) > 0 for x in nbs if x.get("n")]
        spy = np.array([days[r["day"]]["spy"][r["t"]] for r in rows])
        g = np.array([r["gross"] for r in rows])
        okb = np.isfinite(spy)
        beta = float(np.cov(g[okb], spy[okb])[0, 1] / np.var(spy[okb], ddof=1)) if okb.sum() > 10 else None
        checks = {
            "n >= 300": s.get("n", 0) >= MIN_N,
            "t_net >= 3.3": (s.get("t_net") or -99) >= T_PASS,
            "mean net >= +2.0": (s.get("mean_net") or -99) >= MIN_EDGE,
            "both halves > 0": (s.get("half1") or -1) > 0 and (s.get("half2") or -1) > 0,
            "winsorized halves > 0": (s.get("w_half1") or -1) > 0 and (s.get("w_half2") or -1) > 0,
            "beats luck (gross > null p95)": p95 is not None and s.get("mean_gross", -99) > p95,
            "EDGE same-minute t >= 2.5": (ctl.get("edge_t") or -99) >= EDGE_T,
            "half of neighbours > 0": bool(nb_pos) and sum(nb_pos) >= len(nb_pos) / 2}
        out["winner"] = {"stats": s, "controls": ctl, "beta_spy": round(beta, 3) if beta is not None else None,
                         "neighbours_positive": f"{sum(nb_pos)}/{len(nb_pos)}", "checks": checks}
        out["VERDICT"] = "SEARCH PASS - forward 20 sessions next" if all(checks.values()) else "FAIL"
        # confirm period as INFORMATION only (amended_1), chosen cell only, opened after the choice is written
        cdays = load_cache(*CONFIRM)
        if cdays:
            cres = score_cells(cdays, [by_id[win]], len(cdays))
            out["confirm_info"] = cres[win][0]
    else:
        out["VERDICT"] = "FAIL - no cell reached 300 traded entries"
    (OUT / "score.json").write_text(json.dumps(out, indent=1))
    lines = ["# Opening grid (information table; verdict in score.json)", "",
             f"chosen: {win}   verdict: {out['VERDICT']}   null winner gross p95: {out['null_winner_gross_p95']}", "",
             "| cell | n | net bp | gross bp | t | MDE | win % | trades/day | $/day~ | halves |", "|---|---|---|---|---|---|---|---|---|---|"]
    for k, s in sorted(stats.items(), key=lambda kv: -(kv[1].get("t_net") or -99)):
        if s.get("n"):
            lines.append(f"| {k} | {s['n']} | {s['mean_net']} | {s['mean_gross']} | {s['t_net']} | {s['mde_bp']} | "
                         f"{s['win_pct']} | {s['trades_per_day']} | {s['usd_per_day_approx']} | {s['half1']} / {s['half2']} |")
    (OUT / "report.md").write_text("\n".join(lines) + "\n")
    print(json.dumps({k: v for k, v in out.items() if k != "cells"}, indent=1))
    return out


if __name__ == "__main__":
    {"build": build, "run": run}[sys.argv[1]]()
