#!/usr/bin/env python3
"""movers_cap - would lifting the $100 price cap (CAP) or the 90% day-range admission cap (RANGE) add buy signals at no
worse quality? Pass bars: docs/studies/movers_cap_prereg.json (+ amended_1). Read-only. Run on the mini.

    .venv/bin/python tools/studies/movers_cap.py signals   # -> ai_reports/movers_cap/signals.json
    .venv/bin/python tools/studies/movers_cap.py quotes    # real SIP spread (shared cache)
    .venv/bin/python tools/studies/movers_cap.py score     # -> result.json
"""
from __future__ import annotations

import gzip
import hashlib
import json
import math
import sys
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))

ET = ZoneInfo("America/New_York")
OUT = ROOT / "ai_reports" / "movers_cap"
T_FIRST, T_LAST, REFRACT, HOLD = 15, 360, 30, 10
COST, GATE, SAMPLE = 6.0, 6.0, 800
SPAN = (T_LAST - T_FIRST + 1) / 10


def ten_min_gross(o, c, t):
    """Buy open t+1, sell close t+HOLD (bp, before cost). Pure."""
    if t + HOLD >= len(c):
        return None
    e, x = o[t + 1], c[t + HOLD]
    if not (np.isfinite(e) and np.isfinite(x) and e > 0):
        return None
    return (x / e - 1) * 1e4


def paced_rvol(v, t, avg10):
    """Cumulative volume to t over (prior-10-day mean full-day volume x max(elapsed/390, 0.05)). Pure."""
    if not avg10 or avg10 <= 0:
        return None
    return float(np.nansum(v[:t + 1])) / (avg10 * max((t + 1) / 390, 0.05))


def range_pos(h, l, c, t):
    """0-100 position of close t in today's RTH high/low through t (admission_filter.range_pos). Pure."""
    hh, ll = h[:t + 1], l[:t + 1]
    hh, ll = hh[np.isfinite(hh)], ll[np.isfinite(ll)]
    if len(hh) < 5 or not np.isfinite(c[t]):
        return None
    hi, lo = float(hh.max()), float(ll.min())
    if hi <= lo:
        return None
    return (float(c[t]) - lo) / (hi - lo) * 100


def refractory(ts, gap=REFRACT):
    out, last = [], -10 ** 9
    for t in sorted(ts):
        if t - last >= gap:
            out.append(t)
            last = t
    return out


def kept_symbols(day):
    import news_source as ns
    for d in ns.LEDGER_DIRS:
        for p in (d / f"{day}.jsonl", d / f"{day}.jsonl.gz"):
            if p.exists():
                op = gzip.open if p.suffix == ".gz" else open
                kept = set()
                with op(p, "rt") as fh:
                    for line in fh:
                        try:
                            r = json.loads(line)
                        except ValueError:
                            continue
                        if r.get("kept") is True and r.get("symbol"):
                            kept.add(r["symbol"])
                return kept
    return None


def signals():
    from relative_strength_scan import load_day
    from board_structure_family import universe
    from evening_family import WH_SIP, ALL_ETF, minute_grid, presquare_bars, first_true
    import news_source as ns
    hist = defaultdict(lambda: deque(maxlen=10))
    out = {"cap": [], "base": [], "range": []}
    days, prev = [], None
    etf = set(ALL_ETF)
    for fp in sorted(WH_SIP.glob("20??-??-??.npz")):
        day = load_day(fp)
        kept = kept_symbols(fp.stem)
        if prev is not None and kept is not None and day["idx"].get("SPY") is not None:
            days.append(fp.stem)
            top = set(universe(day, prev, etf))
            for s, i in day["idx"].items():
                j = prev["idx"].get(s)
                if j is None or s in etf:
                    continue
                pc = ns.last_close(prev["c"][j])
                if not pc > 0:
                    continue
                o, h, l, c, v = (day[k][i] for k in "ohlcv")
                in_top = s in top
                cap_name = s not in kept and len(hist[s]) >= 5
                if not (in_top or cap_name):
                    continue
                if not np.any(np.isfinite(c[:T_LAST + 1]) & (c[:T_LAST + 1] >= 20)):
                    continue
                cc, hh, ll, off = minute_grid(prev["c"][j], prev["h"][j], prev["l"][j], c, h, l)
                trig = refractory(t for t in first_true(presquare_bars(cc, hh, ll, off, 21)) if T_FIRST <= t <= T_LAST)
                avg10 = float(np.mean(hist[s])) if hist[s] else None
                for t in trig:
                    px = c[t]
                    if not np.isfinite(px) or (px / pc - 1) * 100 < 1.5:
                        continue
                    g = ten_min_gross(o, c, t)
                    if g is None:
                        continue
                    rec = {"day": fp.stem, "sym": s, "t": int(t), "gross": round(float(g), 2), "px": round(float(px), 2)}
                    if in_top and 20 <= px <= 400:
                        out["base"].append(rec)
                        rp = range_pos(h, l, c, t)
                        if rp is not None:
                            out["range"].append({**rec, "rpos": round(rp, 1)})
                    if cap_name and 100 < px <= 400:
                        rv = paced_rvol(v, t, avg10)
                        dv = float(np.nansum(c[:t + 1] * v[:t + 1]))
                        if rv is not None and rv >= 1.0 and dv >= 2e6:
                            out["cap"].append({**rec, "rvol": round(rv, 2)})
            print(fp.stem, {k: len(v) for k, v in out.items()}, flush=True)
        for s, ii in day["idx"].items():
            dv = float(np.nansum(day["v"][ii]))
            if dv > 0:
                hist[s].append(dv)
        prev = day
    out["days"] = days
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "signals.json").write_text(json.dumps(out))
    print({k: len(v) for k, v in out.items() if k != "days"}, "days", len(days))


def _h(*parts):
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest(), 16)


def pick(rows, tag, n=SAMPLE):
    return sorted(rows, key=lambda r: _h(tag, r["day"], r["sym"], r["t"]))[:n]


def plan_sets(sig):
    """{'cap': all, 'base': 800, 'high': 800, 'low': 800} -> lists of signal dicts. Pure."""
    hi = [r for r in sig["range"] if r["rpos"] > 90]
    lo = [r for r in sig["range"] if r["rpos"] <= 90]
    return {"cap": sig["cap"], "base": pick(sig["base"], "movers-cap-base"),
            "high": pick(hi, "range"), "low": pick(lo, "range")}


def quotes():
    import news_source as ns
    sets = plan_sets(json.loads((OUT / "signals.json").read_text()))
    plan = sorted({("mc", r["day"], r["sym"], r["t"], 0.0) for v in sets.values() for r in v}, key=lambda p: (p[1], p[2], p[3]))
    print({k: len(v) for k, v in sets.items()}, "unique", len(plan), flush=True)
    ns.fetch_quotes(plan)


def gated(rows, cache):
    import news_source as ns
    out, wide, missing = [], 0, 0
    for r in rows:
        sp = cache.get(ns.qkey(r["day"], r["sym"], r["t"]))
        if sp is None:
            missing += 1
        elif sp > GATE:
            wide += 1
        else:
            out.append({**r, "spread": float(sp), "net": r["gross"] - COST, "net_own": r["gross"] - float(sp)})
    return out, wide, missing


def summ(rows, key="net"):
    if not rows:
        return {"n": 0}
    x = np.array([r[key] for r in rows])
    return {"n": len(rows), "mean": round(float(x.mean()), 2), "win": round(100 * float((x > 0).mean()), 1),
            "sd": round(float(x.std(ddof=1)), 2) if len(x) > 1 else None}


def welch_t(a, b):
    if a.get("n", 0) < 2 or b.get("n", 0) < 2:
        return None
    se = math.sqrt(a["sd"] ** 2 / a["n"] + b["sd"] ** 2 / b["n"])
    return round((a["mean"] - b["mean"]) / se, 2) if se > 0 else None


def judge(test, ref, min_n=200, rate=None):
    """Quality bars shared by CAP and RANGE (prereg): mean >= ref - 1, Welch t > -2, win >= ref - 3, n >= 200. Pure."""
    if test.get("n", 0) < min_n:
        return "UNDERPOWERED", [f"n {test.get('n', 0)} (need {min_n})"]
    f = []
    if test["mean"] < ref["mean"] - 1.0:
        f.append(f"mean {test['mean']} vs {ref['mean']} - 1")
    t = welch_t(test, ref)
    if t is not None and t <= -2.0:
        f.append(f"welch t {t}")
    if test["win"] < ref["win"] - 3.0:
        f.append(f"win {test['win']} vs {ref['win']} - 3")
    if rate is not None and rate < 0.10:
        f.append(f"opens {rate:.2f} of base (need 0.10)")
    return ("PASS" if not f else "FAIL"), f


def score():
    import news_source as ns
    sig = json.loads((OUT / "signals.json").read_text())
    cache = json.load(open(ns.QUOTES))
    sets = plan_sets(sig)
    g = {k: gated(v, cache) for k, v in sets.items()}
    s = {k: summ(v[0]) for k, v in g.items()}
    nd = max(1, len(sig["days"]))
    base_share = len(g["base"][0]) / max(1, len(sets["base"]))
    base_rate = len(sig["base"]) / nd / SPAN * base_share
    cap_rate = len(g["cap"][0]) / nd / SPAN
    res = {"days": nd, "counts": {k: len(v) for k, v in sig.items() if k != "days"},
           "gated": s, "wide_or_missing": {k: {"wide": v[1], "no_quote": v[2]} for k, v in g.items()},
           "opens_per_10m": {"base_gated": round(base_rate, 3), "cap_gated": round(cap_rate, 3)},
           "welch_t": {"cap_vs_base": welch_t(s["cap"], s["base"]), "high_vs_low": welch_t(s["high"], s["low"])}}
    res["verdict_CAP"] = judge(s["cap"], s["base"], rate=cap_rate / base_rate if base_rate > 0 else 0.0)
    res["verdict_RANGE"] = judge(s["high"], s["low"])
    cap_g = g["cap"][0]
    res["info"] = {"cap_100_200": summ([r for r in cap_g if r["px"] <= 200]), "cap_200_400": summ([r for r in cap_g if r["px"] > 200]),
                   "cap_own_spread": summ(cap_g, "net_own"), "base_own_spread": summ(g["base"][0], "net_own"),
                   "high_own_spread": summ(g["high"][0], "net_own"), "low_own_spread": summ(g["low"][0], "net_own"),
                   "median_spread": {k: (round(float(np.median([r["spread"] for r in v[0]])), 2) if v[0] else None) for k, v in g.items()}}
    res["generated"] = datetime.now(ET).isoformat(timespec="minutes")
    (OUT / "result.json").write_text(json.dumps(res, indent=1))
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    {"signals": signals, "quotes": quotes, "score": score}[sys.argv[1]]()
