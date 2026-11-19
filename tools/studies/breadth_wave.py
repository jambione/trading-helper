#!/usr/bin/env python3
"""breadth_wave - a board-wide buying wave picks the moment, the name's %R trigger times the entry, only names under the
desk's 6 bp spread gate trade. Pass bar: docs/studies/breadth_wave_prereg.json. Read-only. Run on the mini.

    .venv/bin/python tools/studies/breadth_wave.py events   # -> ai_reports/breadth_wave/events.jsonl
    .venv/bin/python tools/studies/breadth_wave.py quotes   # real SIP spread at each entry (shared cache)
    .venv/bin/python tools/studies/breadth_wave.py score    # -> result.json
"""
from __future__ import annotations

import hashlib
import json
import math
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))

ET = ZoneInfo("America/New_York")
OUT = ROOT / "ai_reports" / "breadth_wave"
SPLIT = "2026-09-24"
T_FIRST, T_LAST, REFRACT = 15, 330, 16
MIN_BOARD, WAVE_SHARE, WAVE_LIVE, MAX_SPREAD, CAP = 10, 0.35, 2, 6.0, 4000
MODES = ("strict", "loose")
EXITS = ("desk", "atr")
T_CRIT = 2.5
BUCKETS = (("09:45-10:30", 15, 60), ("10:30-12:00", 60, 150), ("12:00-14:00", 150, 270), ("14:00-15:00", 270, 331))


def new_high5(h):
    """(nsym, 390) highs -> bool matrix: high[t] > max(high[t-5..t-1]) (False where unknown). Pure."""
    out = np.zeros(h.shape, dtype=bool)
    for t in range(5, h.shape[1]):
        prev = np.nanmax(np.where(np.isfinite(h[:, t - 5:t]), h[:, t - 5:t], -np.inf), axis=1)
        cur = h[:, t]
        out[:, t] = np.isfinite(cur) & np.isfinite(prev) & (cur > prev)
    return out


def wave_minutes(band, nh):
    """band, nh: (nsym, 390) bool. Wave at t if board >= MIN_BOARD and >= WAVE_SHARE of board made a new 5-min high at
    t or t-1. Returns (wave bool[390], active bool[390]) where active = a wave at t, t-1 or t-2. Pure."""
    n = band.shape[1]
    wave = np.zeros(n, dtype=bool)
    for t in range(1, n):
        b = band[:, t]
        if b.sum() < MIN_BOARD:
            continue
        hit = (nh[:, t] | nh[:, t - 1]) & b
        wave[t] = hit.sum() / b.sum() >= WAVE_SHARE
    active = wave.copy()
    for k in range(1, WAVE_LIVE + 1):
        active[k:] |= wave[:-k]
    return wave, active


def events():
    from relative_strength_scan import load_day
    from board_structure_family import universe
    from evening_family import WH_SIP, ALL_ETF, minute_grid, presquare_bars, first_true
    from bar_signal_family import wr_smoothed
    from holistic_entry import bracket, COST
    from catalyst_primary import vol_exit, loose_ok
    import news_source as ns
    rows, prev, waves = [], None, {}
    for fp in sorted(WH_SIP.glob("20??-??-??.npz")):
        day = load_day(fp)
        if prev is not None and day["idx"].get("SPY") is not None:
            names = []
            for s in universe(day, prev, set(ALL_ETF)):
                j = prev["idx"].get(s)
                pc = ns.last_close(prev["c"][j]) if j is not None else None
                if pc and pc > 0:
                    names.append((s, day["idx"][s], j, pc))
            if names:
                ii = [x[1] for x in names]
                C, H = day["c"][ii], day["h"][ii]
                pcs = np.array([x[3] for x in names])[:, None]
                band = np.isfinite(C) & (C >= 20) & (C <= 400) & ((C / pcs - 1) * 100 >= 1.5)
                wave, active = wave_minutes(band, new_high5(H))
                waves[fp.stem] = int(wave[T_FIRST:T_LAST + 1].sum())
                for k, (s, i, j, pc) in enumerate(names):
                    o, h, l, c = (day[x][i] for x in "ohlc")
                    cc, hh, ll, off = minute_grid(prev["c"][j], prev["h"][j], prev["l"][j], c, h, l)
                    strict = set(first_true(presquare_bars(cc, hh, ll, off, 21)))
                    fast = wr_smoothed(hh, ll, cc, n=21, span=7)
                    last = {(m, w): -999 for m in MODES for w in (True, False)}
                    for t in range(T_FIRST, T_LAST + 1):
                        if not band[k, t]:
                            continue
                        for m in MODES:
                            fired = (t in strict) if m == "strict" else loose_ok(fast, t + off)
                            if not fired:
                                continue
                            w = bool(active[t])
                            if t - last[(m, w)] < REFRACT:
                                continue
                            b = bracket(o, h, l, c, t)
                            a = vol_exit(o, h, l, c, t)
                            if b is None or a is None:
                                continue
                            last[(m, w)] = t
                            rows.append({"day": fp.stem, "sym": s, "t": t, "mode": m, "wave": w,
                                         "desk": round(float(b) + COST, 2), "atr": round(float(a), 2)})
            print(fp.stem, waves.get(fp.stem), len(rows), flush=True)
        prev = day
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / "events.jsonl", "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    (OUT / "waves_per_day.json").write_text(json.dumps(waves))
    print("events", len(rows))


def _h(*parts):
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest(), 16)


def sample(rows):
    """<= CAP per (mode, wave, period) by the lowest sha256('breadth'|day|sym|t). Pure."""
    by = defaultdict(list)
    for r in rows:
        by[(r["mode"], r["wave"], r["day"] < SPLIT)].append(r)
    out = []
    for v in by.values():
        out += sorted(v, key=lambda r: _h("breadth", r["day"], r["sym"], r["t"]))[:CAP]
    return out


def load_events():
    return [json.loads(x) for x in open(OUT / "events.jsonl")]


def quotes():
    import news_source as ns
    rows = sample(load_events())
    plan = sorted({("bw", r["day"], r["sym"], r["t"], 0.0) for r in rows if r["wave"]}, key=lambda p: (p[1] >= SPLIT, p[1], p[2], p[3]))
    ctl = sorted({("bw", r["day"], r["sym"], r["t"], 0.0) for r in rows if not r["wave"]}, key=lambda p: (p[1] >= SPLIT, p[1], p[2], p[3]))
    print("wave entries", len(plan), "control entries", len(ctl), flush=True)
    ns.fetch_quotes(plan + ctl)


def stats(rows, key):
    if not rows:
        return {"n": 0}
    per = defaultdict(list)
    for r in rows:
        per[r["day"]].append(r[key])
    m = [np.mean(v) for v in per.values()]
    sd = float(np.std(m, ddof=1)) if len(m) > 2 else 0.0
    days = sorted(per)
    h1 = set(days[: len(days) // 2])
    a = [r[key] for r in rows if r["day"] in h1]
    b = [r[key] for r in rows if r["day"] not in h1]
    return {"n": len(rows), "days": len(days), "mean_net": round(float(np.mean([r[key] for r in rows])), 2),
            "win_pct": round(100 * float(np.mean([r[key] > 0 for r in rows])), 1),
            "t_day": round(float(np.mean(m) / (sd / math.sqrt(len(m)))), 2) if sd > 0 else None,
            "half1": round(float(np.mean(a)), 2) if a else None, "half2": round(float(np.mean(b)), 2) if b else None}


def verdict(st):
    if st.get("n", 0) < 300:
        return "UNDERPOWERED", [f"n {st.get('n', 0)} (need 300)"]
    f = []
    if st["mean_net"] < 2.0:
        f.append(f"edge {st['mean_net']} bp (need +2.0)")
    if (st.get("t_day") or 0) < T_CRIT:
        f.append(f"t {st.get('t_day')} (need {T_CRIT})")
    if not ((st.get("half1") or -1) > 0 and (st.get("half2") or -1) > 0):
        f.append(f"halves {st.get('half1')} / {st.get('half2')}")
    return ("PASS" if not f else "FAIL"), f


def traded(rows, cache):
    """Attach the real spread; keep entries the 6 bp gate would let through; net_<exit> = gross - spread. Pure."""
    import news_source as ns
    out, gated, missing = [], 0, 0
    for r in rows:
        sp = cache.get(ns.qkey(r["day"], r["sym"], r["t"]))
        if sp is None:
            missing += 1
            continue
        if sp > MAX_SPREAD:
            gated += 1
            continue
        out.append({**r, "spread": float(sp), "net_desk": r["desk"] - sp, "net_atr": r["atr"] - sp})
    return out, gated, missing


def score():
    import news_source as ns
    cache = json.load(open(ns.QUOTES))
    allev = load_events()
    rows, gated, missing = traded(sample(allev), cache)
    res = {"verdicts": {}, "decide": {}, "info": {}, "gated_over_6bp": gated, "no_quote": missing}
    for m in MODES:
        for x in EXITS:
            for period, sel in (("pre", lambda r: r["day"] < SPLIT), ("post", lambda r: r["day"] >= SPLIT)):
                w = [r for r in rows if r["mode"] == m and r["wave"] and sel(r)]
                nw = [r for r in rows if r["mode"] == m and not r["wave"] and sel(r)]
                sw, snw = stats(w, f"net_{x}"), stats(nw, f"net_{x}")
                if period == "pre":
                    res["decide"][f"{m}|{x}"] = sw
                    res["verdicts"][f"{m}|{x}"] = verdict(sw)
                    for name, a, b in BUCKETS:
                        res["info"][f"{m}|{x}|pre|{name}"] = stats([r for r in w if a <= r["t"] < b], f"net_{x}")
                else:
                    res["info"][f"{m}|{x}|post"] = sw
                res["info"][f"{m}|{x}|{period}|NON_WAVE"] = snw
                if sw.get("n") and snw.get("n"):
                    res["info"][f"{m}|{x}|{period}|lift_vs_non_wave"] = round(sw["mean_net"] - snw["mean_net"], 2)
        for period, sel in (("pre", lambda r: r["day"] < SPLIT),):
            ev = [r for r in allev if r["mode"] == m and r["wave"] and sel(r)]
            nd = len({r["day"] for r in allev if sel(r)})
            res["info"][f"{m}|opens_per_10m_before_gate|{period}"] = round(len(ev) / max(1, nd) / ((T_LAST - T_FIRST + 1) / 10), 3)
            tr = [r for r in rows if r["mode"] == m and r["wave"] and sel(r)]
            sampled = [r for r in sample(allev) if r["mode"] == m and r["wave"] and sel(r)]
            share = len(tr) / max(1, len(sampled))
            res["info"][f"{m}|opens_per_10m_after_gate_est|{period}"] = round(res["info"][f"{m}|opens_per_10m_before_gate|{period}"] * share, 3)
            res["info"][f"{m}|median_spread_traded|{period}"] = round(float(np.median([r["spread"] for r in tr])), 2) if tr else None
    res["generated"] = datetime.now(ET).isoformat(timespec="minutes")
    (OUT / "result.json").write_text(json.dumps(res, indent=1))
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    {"events": events, "quotes": quotes, "score": score}[sys.argv[1]]()
