#!/usr/bin/env python3
"""square_confirmation - after an early starter on an ARMED (trending) name, does the desk's %R square within 15
minutes mark the starters whose run continues? Operator 11/20 (two-stage lane design). Prereg + amended_1:
docs/studies/square_confirmation_prereg.json.

    .venv/bin/python tools/studies/square_confirmation.py inputs   # after 16:05 ET: prior-session bars (shared key)
    .venv/bin/python tools/studies/square_confirmation.py power    # group counts + projected MDE (no outcomes)
    .venv/bin/python tools/studies/square_confirmation.py score    # only if the power gate passes

Same-day bars come from ai_reports/multisource_stack/inputs.pkl (fetched by multisource_stack.py inputs).
"""
from __future__ import annotations

import functools
import hashlib
import json
import math
import pickle
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))

OUT = ROOT / "ai_reports" / "square_confirmation"
MS_INPUTS = ROOT / "ai_reports" / "multisource_stack" / "inputs.pkl"
SS_CACHE = ROOT / "ai_reports" / "stack_search" / "days"
ET = ZoneInfo("America/New_York")
SPLIT = "2026-10-01"
N_CONF, START_WIN, HOLD, MIN_PX = 15, 30, 30, 2.0
T_CRIT, MDE_MAX = 2.0, 30.0


def hkey(*p):
    return int(hashlib.sha256("|".join(str(x) for x in p).encode()).hexdigest(), 16)


# ---------------------------------------------------------------- pure pieces

def to_arrays(bars: dict):
    """{minute: (open, close, high, low)} -> o, c, h, l arrays of 390 (NaN where no bar). Pure."""
    o, c, h, l = (np.full(390, np.nan) for _ in range(4))
    for k, v in bars.items():
        if 0 <= k < 390 and len(v) >= 4:
            o[k], c[k], h[k], l[k] = v[0], v[1], v[2], v[3]
    return o, c, h, l


def square_state(prev, today):
    """(entries, zone) for today: entries = bars where both %R lines enter >= -20 with fast rising
    (opening_square.square_bars); zone = both lines >= -20 on the bar. Warm-up = prior session's last 112 RTH bars
    (study convention, amended_1 #3). prev/today: (o, c, h, l) arrays. Pure."""
    from evening_family import minute_grid
    from bar_signal_family import wr_smoothed
    from opening_square import square_bars
    cc, hh, ll, off = minute_grid(prev[1], prev[2], prev[3], today[1], today[2], today[3])
    entries = square_bars(cc, hh, ll, off)
    fast = wr_smoothed(hh, ll, cc, n=21, span=7)
    slow = wr_smoothed(hh, ll, cc, n=112, span=3)
    with np.errstate(invalid="ignore"):
        zone = (np.isfinite(fast) & np.isfinite(slow) & (fast >= -20) & (slow >= -20))[off:]
    return entries, zone


def starter_bar(h, c, m):
    """First bar t in [m, m+30] whose close is above the high of the 5 bars before it (a 5-minute breakout). Pure."""
    for t in range(max(m, 5), min(m + START_WIN, 389) + 1):
        w = h[t - 5:t]
        if np.isfinite(c[t]) and np.isfinite(w).any() and c[t] > np.nanmax(w):
            return t
    return None


def classify(t_s, entries, zone, n=N_CONF):
    """'ALREADY_SQUARED' if the zone is on at t_s; 'CONFIRMED' (with t_c) if a square entry falls in
    [t_s+1, t_s+n]; else 'UNCONFIRMED'. Pure."""
    if zone[t_s]:
        return "ALREADY_SQUARED", None
    tc = next((e for e in entries if t_s + 1 <= e <= t_s + n), None)
    return ("CONFIRMED", tc) if tc is not None else ("UNCONFIRMED", None)


def hold_ret(o, c, entry_bar, exit_bar, strict=False):
    """Buy o[entry_bar], sell the last finite close in [entry_bar, exit_bar] (strict: the close AT exit_bar must
    exist); None if out of session. Pure."""
    if exit_bar > 389 or entry_bar > 389 or not np.isfinite(o[entry_bar]) or o[entry_bar] <= 0:
        return None
    if strict:
        return float((c[exit_bar] / o[entry_bar] - 1) * 1e4) if np.isfinite(c[exit_bar]) else None
    w = c[entry_bar: exit_bar + 1]
    f = np.where(np.isfinite(w))[0]
    return float((w[f[-1]] / o[entry_bar] - 1) * 1e4) if len(f) else None


def badj_ret(o, c, so, sc, beta, entry, exit_):
    """Beta-adjusted return; name and SPY both need a close AT the exit bar (review #9). Pure."""
    r = hold_ret(o, c, entry, exit_, strict=True)
    s_ = hold_ret(so, sc, entry, exit_, strict=True)
    return None if r is None or s_ is None else r - beta * s_


def beta_prev(prev_c, spy_prev_c):
    """OLS slope of the name's 1-minute returns on SPY's over the prior session only (amended_1 #5). Pure."""
    with np.errstate(invalid="ignore", divide="ignore"):
        x, y = np.diff(np.log(spy_prev_c)), np.diff(np.log(prev_c))
    ok = np.isfinite(x) & np.isfinite(y)
    if ok.sum() < 60 or np.var(x[ok]) == 0:
        return 1.0
    return float(np.cov(y[ok], x[ok])[0, 1] / np.var(x[ok], ddof=1))


def day_t(pairs):
    per = defaultdict(list)
    for d, x in pairs:
        if x is not None and np.isfinite(x):
            per[d].append(x)
    m = [float(np.mean(v)) for v in per.values()]
    if len(m) < 3 or np.std(m, ddof=1) == 0:
        return (float(np.mean(m)) if m else None), None, None, len(m)
    se = float(np.std(m, ddof=1) / math.sqrt(len(m)))
    return float(np.mean(m)), se, float(np.mean(m)) / se, len(m)


def separation(rows):
    """Day-level CONFIRMED minus UNCONFIRMED mean same-clock lift, on days with both groups. -> (day, diff) list."""
    by = defaultdict(lambda: {"C": [], "U": []})
    for r in rows:
        if r["lift"] is None:
            continue
        if r["group"] == "CONFIRMED":
            by[r["day"]]["C"].append(r["lift"])
        elif r["group"] == "UNCONFIRMED":
            by[r["day"]]["U"].append(r["lift"])
    return [(d, float(np.mean(v["C"]) - np.mean(v["U"]))) for d, v in by.items() if v["C"] and v["U"]]


# ---------------------------------------------------------------- inputs

def sessions():
    from evening_family import WH_SIP
    return sorted(p.stem for p in WH_SIP.glob("20??-??-??.npz"))


def inputs():
    hm = datetime.now(ET).strftime("%H%M")
    if "0900" <= hm < "1605":
        sys.exit("market hours - rerun after 16:05 ET (shared data key)")
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import DataFeed, Adjustment
    import time
    from multisource_stack import events
    k = json.load(open(ROOT / "secrets_data.json"))
    cl = StockHistoricalDataClient(k["alpaca_data_api_key"], k["alpaca_data_secret_key"])
    OUT.mkdir(parents=True, exist_ok=True)
    dst = OUT / "prior.pkl"
    res = pickle.load(open(dst, "rb")) if dst.exists() else {"prior": {}, "failed": []}
    res["failed"] = []
    sess = sessions()
    by_day = defaultdict(set)
    for ts, day, sym, et in events():
        by_day[day].add(sym)
    for day, syms in sorted(by_day.items()):
        if day in res["prior"] or day not in sess or sess.index(day) == 0:
            continue
        pday = sess[sess.index(day) - 1]
        s0 = datetime.fromisoformat(pday + "T09:30").replace(tzinfo=ET)
        got = None
        for attempt in range(6):
            try:
                got = cl.get_stock_bars(StockBarsRequest(symbol_or_symbols=sorted(syms | {"SPY"}), timeframe=TimeFrame.Minute,
                                                         start=s0, end=s0 + timedelta(minutes=390), feed=DataFeed.SIP,
                                                         adjustment=Adjustment.RAW))
                break
            except Exception as e:                  # noqa: BLE001
                time.sleep(60 if "429" in str(e) else 5 * (attempt + 1))
        if got is None:
            res["failed"].append(day)
            continue
        res["prior"][day] = {s: {(r.timestamp.astimezone(ET).hour - 9) * 60 + r.timestamp.astimezone(ET).minute - 30:
                                 (float(r.open), float(r.close), float(r.high), float(r.low)) for r in rows}
                             for s, rows in (got.data or {}).items()}
        pickle.dump(res, open(dst, "wb"))
        print("prior", day, len(res["prior"][day]), flush=True)
    print("PRIOR_DONE failed", len(res["failed"]), flush=True)


# ---------------------------------------------------------------- rows

def build(n=N_CONF):
    from multisource_stack import events, monitor_down_days, minute_of, wick_pressure, market_gaps, bucket
    ms = pickle.load(open(MS_INPUTS, "rb"))
    pr = pickle.load(open(OUT / "prior.pkl", "rb"))
    down = monitor_down_days()
    gaps = market_gaps(ms)
    rows, counts = [], defaultdict(int)
    st_time = defaultdict(dict)
    evs = events()
    for ts, day, sym, et in evs:
        st_time[day][sym] = minute_of(ts)
    pool = defaultdict(dict)        # day -> sym -> (o, c, beta): every event name with data, starter or not (review #2)
    for ts, day, sym, et in evs:
        tb, pb = ms["bars"].get(day, {}).get(sym), pr["prior"].get(day, {}).get(sym)
        spy_p = pr["prior"].get(day, {}).get("SPY")
        if tb and pb and spy_p and sym not in pool[day]:
            today = to_arrays(tb)
            pool[day][sym] = (today[0], today[1], beta_prev(to_arrays(pb)[1], to_arrays(spy_p)[1]))
    for ts, day, sym, et in evs:
        counts["events"] += 1
        if day in down:
            counts["monitor_down_day"] += 1
            continue
        tb = ms["bars"].get(day, {}).get(sym)
        pb = pr["prior"].get(day, {}).get(sym)
        spy_t, spy_p = ms["bars"].get(day, {}).get("SPY"), pr["prior"].get(day, {}).get("SPY")
        if not tb or not spy_t:
            counts["no_bars"] += 1
            continue
        if not pb or not spy_p:
            counts["no_prior_warmup"] += 1
            continue
        today, prev = to_arrays(tb), to_arrays(pb)
        o, c, h, l = today
        m = minute_of(ts)
        t_s = starter_bar(h, c, m)
        if t_s is None:
            counts["no_starter"] += 1
            continue
        if t_s + 45 > 389:
            counts["past_close"] += 1
            continue
        if not np.isfinite(o[t_s + 1]) or o[t_s + 1] < MIN_PX:
            counts["under_2_or_no_entry"] += 1
            continue
        entries, zone = square_state(prev, today)
        group, t_c = classify(t_s, entries, zone, n)
        sa = to_arrays(spy_t)
        so, sc = sa[0], sa[1]
        beta = pool[day][sym][2]
        fwd = badj_ret(o, c, so, sc, beta, t_s + 16, t_s + 45)
        if fwd is None:
            counts["past_close_or_gap"] += 1
            continue
        cm = t_s + 16
        peers = [q for q, v in pool[day].items() if q != sym and st_time[day].get(q, 999) <= cm]
        peers = sorted(peers, key=lambda q: hkey("sqc-ctl", day, q, sym, cm))
        vals = []
        for q in peers:
            qo, qc, qb = pool[day][q]
            v = badj_ret(qo, qc, so, sc, qb, cm, cm + 29)
            if v is not None:
                vals.append(v)
            if len(vals) == 5:
                break
        fb = False
        if len(vals) < 2:
            vals, fb = fallback_controls(day, cm, sym), True
            counts["fallback_control"] += 1
        if not vals:
            counts["no_control"] += 1
            continue
        ctl = float(np.mean(vals))
        rows.append({"day": day, "sym": sym, "m": m, "t_s": t_s, "t_c": t_c, "group": group, "fwd": fwd,
                     "ctl": ctl, "lift": fwd - ctl, "fallback": fb, "px": float(o[t_s + 1]),
                     "starter": badj_ret(o, c, so, sc, beta, t_s + 1, t_s + 30),
                     "add": badj_ret(o, c, so, sc, beta, t_c + 1, t_c + 30) if t_c is not None and t_c + 30 <= 389 else None,
                     "o": o, "c": c, "so": so, "sc": sc, "beta": beta,
                     "pre": hold_ret(o, c, max(m - 30, 0), m - 1) if m >= 1 else None,
                     "wp5": wick_pressure(tb, t_s + 16, 5), "mkt": bucket(gaps.get(day))})
        counts[group] += 1
    counts["prior_fetch_failed_days"] = len(pr.get("failed", []))
    return rows, dict(counts)


@functools.lru_cache(maxsize=4)
def _ss_day(day):
    p = SS_CACHE / f"{day}.pkl"
    if not p.exists():
        return None
    cch = pickle.load(open(p, "rb"))
    return np.asarray(cch["G"], float), cch["px"], cch["syms"]


def fallback_controls(day, cm, sym, k=5):
    """stack_search universe at the same minute: its cached beta-adjusted G[j, cm-1] is exactly 'buy the open of cm,
    sell the close of cm+29' on the same SIP RTH bars. DEVIATION (amended_2): those betas use up to 20 prior
    sessions, not the prior session only; the fallback share is printed."""
    d = _ss_day(day)
    if d is None:
        return []
    G, px, syms = d
    t = cm - 1
    ok = [j for j in range(len(syms)) if syms[j] != sym and px[j, t] and np.isfinite(G[j, t])]
    ok = sorted(ok, key=lambda j: hkey("sqc-fb", day, syms[j], cm))[:k]
    return [float(G[j, t]) for j in ok]


def controls_extra(rows):
    """For CONFIRMED rows (the add; review #4): same-name later (entry t_s+47..t_s+107) and unconditional random
    minute, both beta-adjusted with strict exits. -> later, unc, later_g, unc_g (lists of (day, x) / gross)."""
    later, unc, later_g, unc_g = [], [], [], []
    base = [r for r in rows if r["group"] in ("CONFIRMED", "UNCONFIRMED")]
    for r in rows:
        if r["group"] != "CONFIRMED":
            continue
        cand = [u for u in range(r["t_s"] + 47, min(r["t_s"] + 107, 360) + 1)
                if badj_ret(r["o"], r["c"], r["so"], r["sc"], r["beta"], u, u + 29) is not None]
        if cand:
            u = cand[hkey("sqc-later", r["day"], r["sym"]) % len(cand)]
            g = badj_ret(r["o"], r["c"], r["so"], r["sc"], r["beta"], u, u + 29)
            later.append((r["day"], r["fwd"] - g))
            later_g.append(g)
        q = base[hkey("sqc-unc", r["day"], r["sym"]) % len(base)]
        u = 5 + hkey("sqc-unc-m", r["day"], r["sym"]) % 325
        g = badj_ret(q["o"], q["c"], q["so"], q["sc"], q["beta"], u, u + 29)
        if g is not None:
            unc.append((r["day"], r["fwd"] - g))
            unc_g.append(g)
    return later, unc, later_g, unc_g


def halves_t(pairs):
    """(mean h1, t h1, mean h2, t h2, pooled mean, pooled t). Pure."""
    a = [x for x in pairs if x[0] < SPLIT]
    b = [x for x in pairs if x[0] >= SPLIT]
    ma, _, ta, _ = day_t(a)
    mb, _, tb, _ = day_t(b)
    mp, _, tp, _ = day_t(pairs)
    return ma, ta, mb, tb, mp, tp


def both_halves_t(pairs):
    ma, ta, mb, tb, _, _ = halves_t(pairs)
    return (ma or -1) > 0 and (mb or -1) > 0 and (ta or 0) >= T_CRIT and (tb or 0) >= T_CRIT


# ---------------------------------------------------------------- power, score

def power(rows):
    """Day-clustered projected MDE on PRE-event returns (no outcomes; review #6): for the separation (days with both
    groups) and for the CONFIRMED lift, per half."""
    out = {}
    pre_rows = [dict(r, lift=r["pre"]) for r in rows]
    for h, sel in (("h1", lambda d: d < SPLIT), ("h2", lambda d: d >= SPLIT)):
        rr = [r for r in pre_rows if sel(r["day"])]
        _, se_s, _, nd_s = day_t(separation(rr))
        _, se_c, _, nd_c = day_t([(r["day"], r["lift"]) for r in rr if r["group"] == "CONFIRMED"])
        out[h] = {"confirmed": sum(1 for r in rr if r["group"] == "CONFIRMED"),
                  "unconfirmed": sum(1 for r in rr if r["group"] == "UNCONFIRMED"),
                  "already_squared": sum(1 for r in rr if r["group"] == "ALREADY_SQUARED"),
                  "days_with_both": nd_s,
                  "mde_separation_bp": None if se_s is None else round(2.8 * se_s, 1),
                  "mde_confirmed_bp": None if se_c is None else round(2.8 * se_c, 1)}
    out["unproven_by_design"] = any((out[h][k] or 999) > MDE_MAX for h in ("h1", "h2")
                                    for k in ("mde_separation_bp", "mde_confirmed_bp"))
    return out


def run_power():
    rows, counts = build()
    pw = power(rows)
    (OUT / "power.json").write_text(json.dumps({"counts": counts, "power": pw}, indent=1))
    print(json.dumps({"counts": counts, "power": pw}, indent=1))


def score():
    pw_file = OUT / "power.json"
    if not pw_file.exists():
        sys.exit("run 'power' first (amended_1 #8)")
    pwj = json.loads(pw_file.read_text())
    if pwj["power"]["unproven_by_design"]:
        print("UNPROVEN by design (projected MDE > 30 bp) - outcomes not scored (amended_1 #8)")
        (OUT / "score.json").write_text(json.dumps({"VERDICT": "UNPROVEN by design", "power": pwj}, indent=1))
        return
    rows, counts = build()
    lost = counts.get("no_bars", 0) + counts.get("no_prior_warmup", 0) + counts.get("no_control", 0)
    lost_share = lost / max(counts.get("events", 1), 1)
    sep = separation(rows)
    sa, ta, sb, tb, sp, tsep = halves_t(sep)
    conf = [(r["day"], r["lift"]) for r in rows if r["group"] == "CONFIRMED"]
    ca, tca, cb, tcb, cp, tc = halves_t(conf)
    later, unc, later_g, unc_g = controls_extra(rows)
    la, _, lb, _, ml, _ = halves_t(later)
    ua, _, ub, _, mu, _ = halves_t(unc)
    ctl_g = [r["ctl"] for r in rows if r["group"] in ("CONFIRMED", "UNCONFIRMED")]
    gap_ok = {}
    for name, xs in (("same_minute", ctl_g), ("later", later_g)):
        if len(xs) > 2 and len(unc_g) > 2:
            se = math.sqrt(np.var(xs, ddof=1) / len(xs) + np.var(unc_g, ddof=1) / len(unc_g))
            gap_ok[name] = bool(abs(np.mean(xs) - np.mean(unc_g)) <= 2 * se)
        else:
            gap_ok[name] = False
    edge = (both_halves_t(sep) and both_halves_t(conf) and (la or -1) > 0 and (lb or -1) > 0
            and (ua or -1) > 0 and (ub or -1) > 0 and all(gap_ok.values()))
    elig = [r for r in rows if r["group"] in ("CONFIRMED", "UNCONFIRMED")]
    rate = (sum(1 for r in elig if r["group"] == "CONFIRMED") / len(elig)) if elig else 0
    hits = 0
    for seed in range(20):
        rng = np.random.default_rng(seed)
        rr = [dict(r, group=("CONFIRMED" if rng.random() < rate else "UNCONFIRMED")) for r in elig]
        hits += both_halves_t(separation(rr))
    planted = [dict(r, lift=r["lift"] + 30.0 if r["group"] == "CONFIRMED" else r["lift"]) for r in elig]
    planted_ok = both_halves_t(separation(planted))
    tpl = halves_t(separation(planted))[5]
    canary_ok = hits / 20 <= 0.10 and planted_ok
    info = {"starter_30m_by_group": {g: day_t([(r["day"], r["starter"]) for r in rows if r["group"] == g])[0]
                                     for g in ("CONFIRMED", "UNCONFIRMED", "ALREADY_SQUARED")},
            "add_at_tc_30m": day_t([(r["day"], r["add"]) for r in rows if r["add"] is not None])[0],
            "median_minutes_to_confirmation": float(np.median([r["t_c"] - r["t_s"] for r in rows if r["t_c"] is not None]))
            if any(r["t_c"] is not None for r in rows) else None,
            "confirmed_lift_px_ge_20": day_t([(r["day"], r["lift"]) for r in rows if r["group"] == "CONFIRMED" and r["px"] >= 20])[0],
            "market_split": {bk: day_t([(r["day"], r["lift"]) for r in rows if r["group"] == "CONFIRMED" and r["mkt"] == bk])[0]
                             for bk in ("down<=-1%", "flat", "up>=+1%")}}
    out = {"prereg": "docs/studies/square_confirmation_prereg.json (amended_1)", "counts": counts, "power": pwj["power"],
           "separation": {"h1": sa, "t_h1": ta, "h2": sb, "t_h2": tb, "pooled": sp, "t": tsep},
           "confirmed_lift": {"h1": ca, "t_h1": tca, "h2": cb, "t_h2": tcb, "pooled": cp, "t": tc},
           "controls": {"same_name_later": {"h1": la, "h2": lb}, "unconditional": {"h1": ua, "h2": ub}, "gaps_ok": gap_ok,
                        "same_minute_mean": float(np.mean(ctl_g)) if ctl_g else None,
                        "later_mean": float(np.mean(later_g)) if later_g else None,
                        "unconditional_mean": float(np.mean(unc_g)) if unc_g else None},
           "lost_share": round(lost_share, 4),
           "fallback_share": round(sum(1 for r in rows if r["fallback"]) / max(len(rows), 1), 3),
           "canary": {"random_label_pass_rate": hits / 20, "planted_t": tpl, "ok": canary_ok},
           "EDGE": bool(edge), "info": info}
    out["VERDICT"] = ("NO VERDICT (> 2% of events lost to missing data)" if lost_share > 0.02 else
                      "NO VERDICT (canary failed)" if not canary_ok else
                      ("EDGE - PROFITABLE check (quotes) next" if edge else "NO EDGE"))
    (OUT / "score.json").write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps({k: v for k, v in out.items() if k != "info"}, indent=1, default=str))
    print(json.dumps(info, indent=1, default=str))


if __name__ == "__main__":
    {"inputs": inputs, "power": run_power, "score": score}[sys.argv[1]]()
