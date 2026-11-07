#!/usr/bin/env python3
"""regime_beta_family (docs/studies/regime_beta_family_prereg.json, amended after skeptic round 1). Read-only; SIP warehouse only.

R5: does the morning follow-through of beta-adjusted +20 bp volume surges (09:45-10:24) predict whether afternoon surge-continuation trades work?
B2: does a high-beta stock that holds flat while SPY drops >= 15 bp over 3 bars outperform controls over the next 10 bars?
Alpha 0.05/5 = 0.01 one-sided for each (five hypotheses run this weekend).

    .venv/bin/python tools/studies/regime_beta_family.py [report]      # mini
"""
from __future__ import annotations

import math
import sys
from collections import deque
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))
from relative_strength_scan import load_day, prev_weekday  # noqa: E402
from board_structure_family import universe, residual_stats, seeded, refract  # noqa: E402

ET = ZoneInfo("America/New_York")
WH = ROOT / "ai_reports" / "warehouse" / "sip"
ALPHA = 0.01
COST, COST_STRESS = 6.0, 10.0


def spy_ret(day, a, b, use_open_a=False):
    s = day["c"][day["idx"]["SPY"]]
    o = day["o"][day["idx"]["SPY"]]
    base = o[a] if use_open_a else s[a]
    if np.isnan(base) or np.isnan(s[b]):
        return None
    return float(s[b] / base - 1)


def hold_trade(day, i, t, hold, beta, cost=COST):
    """Buy open of bar t+1, sell close of bar t+1+hold (walk up to 5 bars if missing). -> (net, hedged_net) in bp or None."""
    o, c = day["o"][i], day["c"][i]
    if t + 1 >= 390 or np.isnan(o[t + 1]):
        return None
    for k in range(min(389, t + 1 + hold), min(390, t + 1 + hold + 6)):
        if not np.isnan(c[k]):
            g = (c[k] / o[t + 1] - 1) * 1e4
            sp = spy_ret(day, t + 1, k, use_open_a=True)
            return g - cost, (g - beta * (sp or 0.0) * 1e4 - cost)
    return None


def stock_stats(day, names, beta):
    """Per-name arrays: bar return (1) beta-adjusted, volume ratio, 3-bar returns. Returns dict sym -> dict(arrays)."""
    spy = day["c"][day["idx"]["SPY"]]
    out = {}
    for s in names:
        if s not in beta:
            continue
        i = day["idx"][s]
        c, v = day["c"][i], day["v"][i]
        r1 = np.full(390, np.nan); r1[1:] = c[1:] / c[:-1] - 1
        sp1 = np.full(390, np.nan); sp1[1:] = spy[1:] / spy[:-1] - 1
        res1 = r1 - beta[s] * sp1
        vr = np.full(390, np.nan)
        cnt = np.zeros(390)
        for t in range(15, 390):
            w = v[t - 15:t]
            n = int(np.sum(~np.isnan(w)))
            cnt[t] = n
            m = np.nanmean(w) if n else np.nan
            vr[t] = v[t] / m if (m and m > 0) else np.nan
        r3 = np.full(390, np.nan); r3[3:] = c[3:] / c[:-3] - 1
        sp3 = np.full(390, np.nan); sp3[3:] = spy[3:] / spy[:-3] - 1
        out[s] = {"i": i, "res1": res1, "vr": vr, "nvol": cnt, "r3": r3, "res3": r3 - beta[s] * sp3, "c": c}
    return out


def surge_events(st, s, lo, hi):
    a = st[s]
    ts = [t for t in range(lo, hi + 1) if not np.isnan(a["res1"][t]) and a["res1"][t] >= 0.0020 and not np.isnan(a["vr"][t]) and a["vr"][t] >= 2.0 and a["nvol"][t] >= 10
          and not np.isnan(a["c"][t - 1])]
    return refract(ts)


def ft5(day, st, s, t, b):
    c = day["c"][st[s]["i"]]
    if t + 5 >= 390 or np.isnan(c[t + 5]) or np.isnan(c[t]):
        return None
    sp = spy_ret(day, t, t + 5)
    return ((c[t + 5] / c[t] - 1) - b * (sp or 0.0)) * 1e4


def score_day_r5(day, prev, hist, names, d):
    beta, _ = residual_stats(hist, names)
    st = stock_stats(day, names, beta)
    morning = []
    msyms = set()
    for s in st:
        for t in surge_events(st, s, 15, 54):
            v = ft5(day, st, s, t, beta[s])
            if v is not None:
                morning.append(v)
                msyms.add(s)
    if len(morning) < 30 or len(msyms) < 15:
        return None
    nets, hedged, ctl, nets10 = [], [], [], []
    for s in st:
        ev = surge_events(st, s, 60, 360)
        for t in ev:
            tr = hold_trade(day, st[s]["i"], t, 5, beta[s])
            if tr is None:
                continue
            nets.append(tr[0]); hedged.append(tr[1]); nets10.append(tr[0] + COST - COST_STRESS)
            rng = seeded(s, d, t, "R5-a")
            cand = [m for m in range(max(60, t - 30), min(360, t + 30) + 1) if all(abs(m - x) > 15 for x in ev)]
            cs = []
            for _ in range(5):
                if not cand:
                    break
                ct = hold_trade(day, st[s]["i"], rng.choice(cand), 5, beta[s])
                if ct is not None:
                    cs.append(ct[0])
            if cs:
                ctl.append(tr[0] - sum(cs) / len(cs))
    if len(nets) < 5:
        return None
    m = lambda x: sum(x) / len(x) if x else float("nan")  # noqa: E731
    return {"d": d, "M": m(morning), "n_morning": len(morning), "net": m(nets), "hedged": m(hedged), "net10": m(nets10), "lift": m(ctl), "n_aft": len(nets)}


def hac_t(y, x, lag=5):
    """t of the slope of y on a 0/1 dummy x with Newey-West SE."""
    y, x = np.asarray(y, float), np.asarray(x, float)
    n = len(y)
    X = np.column_stack([np.ones(n), x])
    b = np.linalg.solve(X.T @ X, X.T @ y)
    u = y - X @ b
    xu = X * u[:, None]
    S = xu.T @ xu
    for L in range(1, lag + 1):
        w = 1 - L / (lag + 1)
        G = xu[L:].T @ xu[:-L]
        S += w * (G + G.T)
    A = np.linalg.inv(X.T @ X)
    V = A @ S @ A
    return float(b[1] / math.sqrt(V[1, 1])) if V[1, 1] > 0 else float("nan")


def evaluate_r5(days):
    from scipy import stats
    days = sorted(days, key=lambda r: r["d"])
    labelled = []
    for k, r in enumerate(days):
        prior = [x["M"] for x in days[:k]]
        if len(prior) < 10:
            continue
        labelled.append({**r, "high": r["M"] > float(np.median(prior))})
    if len(labelled) < 20:
        return {"status": "UNDERPOWERED", "n": len(labelled)}
    hi = [r for r in labelled if r["high"]]
    lo = [r for r in labelled if not r["high"]]
    mid = labelled[len(labelled) // 2]["d"]
    H = lambda r: r["d"] >= mid  # noqa: E731
    cnt = {"hi_all": (len(hi), len(lo)), "hi_h": (sum(1 for r in hi if not H(r)), sum(1 for r in hi if H(r))), "lo_h": (sum(1 for r in lo if not H(r)), sum(1 for r in lo if H(r)))}
    under = len(hi) < 30 or len(lo) < 30 or min(cnt["hi_h"] + cnt["lo_h"]) < 12
    a, b = np.array([r["net"] for r in hi]), np.array([r["net"] for r in lo])
    tw = stats.ttest_ind(a, b, equal_var=False)
    df_w = float(tw.df) if hasattr(tw, "df") else len(a) + len(b) - 2
    crit_w = float(stats.t.ppf(1 - ALPHA, df_w))
    y = np.array([r["net"] for r in labelled]); x = np.array([1.0 if r["high"] else 0.0 for r in labelled])
    t_hac = hac_t(y, x)
    crit_h = float(stats.t.ppf(1 - ALPHA, len(y) - 2))
    mean = lambda rs, key: sum(r[key] for r in rs) / len(rs) if rs else float("nan")  # noqa: E731
    halves = lambda key, sub: tuple(mean([r for r in sub if H(r) == h], key) for h in (False, True))  # noqa: E731
    diff_h = tuple(halves("net", hi)[k] - halves("net", lo)[k] for k in (0, 1))
    lift_t = float(stats.ttest_1samp([r["lift"] for r in hi if not math.isnan(r["lift"])], 0).statistic) if len(hi) > 3 else float("nan")
    c = {"1": mean(hi, "net") - mean(lo, "net") > 0 and tw.statistic >= crit_w and t_hac >= crit_h, "2": all(v > 0 for v in diff_h),
         "3": all(v > 0 for v in halves("net", hi)), "5": all(v > 0 for v in halves("net10", hi)), "6": all(v > 0 for v in halves("hedged", hi)),
         "7": all(v > 0 for v in halves("lift", hi)) and lift_t >= 1.65}
    status = "UNDERPOWERED" if under else ("PASS" if all(c.values()) else "FAIL")
    return {"status": status, "n": len(labelled), "hi": len(hi), "lo": len(lo), "cnt": cnt, "diff": mean(hi, "net") - mean(lo, "net"), "t_welch": float(tw.statistic),
            "crit_w": crit_w, "t_hac": t_hac, "crit_h": crit_h, "diff_h": diff_h, "hi_net_h": halves("net", hi), "hi_hedged_h": halves("hedged", hi), "hi_lift_h": halves("lift", hi),
            "lift_t": lift_t, "hi_net10_h": halves("net10", hi), "persist": float(np.corrcoef([r["M"] for r in labelled], [r["net"] for r in labelled])[0, 1]), "c": c}


# ---------- B2 ----------
def score_day_b2(day, prev, hist, names, d):
    beta, _ = residual_stats(hist, names)
    st = stock_stats(day, names, beta)
    spy = day["c"][day["idx"]["SPY"]]
    sp3 = np.full(390, np.nan); sp3[3:] = spy[3:] / spy[:-3] - 1
    dips = [t for t in range(15, 361) if not np.isnan(sp3[t]) and sp3[t] <= -0.0015]
    episodes = 0
    last = -99
    for t in dips:
        if t - last > 10:
            episodes += 1
        last = t
    trades = []
    left_out = 0
    for s, a in st.items():
        if beta[s] < 1.2:
            continue
        ts = [t for t in dips if not np.isnan(a["r3"][t]) and a["r3"][t] >= -0.0002 and a["res3"][t] >= 0.0015]
        trig = refract(ts)
        for t in trig:
            tr = hold_trade(day, a["i"], t, 10, beta[s])
            if tr is None:
                continue
            sp_next = spy_ret(day, t + 1, min(389, t + 11), use_open_a=True)
            rng = seeded(s, d, "B2-a", t)
            cand = [m for m in range(max(15, t - 30), min(360, t + 30) + 1) if all(abs(m - x) > 15 for x in trig)]
            ca = []
            for _ in range(5):
                if not cand:
                    break
                ct = hold_trade(day, a["i"], rng.choice(cand), 10, beta[s])
                if ct is not None:
                    ca.append(ct[0])
            rb = seeded(s, d, t, "B2-b")
            cb = None
            pool = [o for o in st if o != s and abs(beta[o] - beta[s]) <= 0.2 and beta[o] >= 1.2 and not np.isnan(st[o]["res3"][t]) and abs(st[o]["res3"][t]) <= 0.0005]
            for _ in range(20):
                if not pool:
                    break
                o = rb.choice(pool)
                ctb = hold_trade(day, st[o]["i"], t, 10, beta[o])
                if ctb is not None:
                    cb = ctb[0]
                    break
            if cb is None:
                left_out += 1
            trades.append({"d": d, "sym": s, "net": tr[0], "hedged": tr[1], "net10": tr[0] + COST - COST_STRESS, "ca": sum(ca) / len(ca) if ca else None, "cb": cb,
                           "spy_up": (sp_next or 0) > 0})
    return trades, episodes, left_out


def tstat(v):
    n = len(v)
    if n < 3:
        return float("nan")
    m = sum(v) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in v) / (n - 1))
    return m / (sd / math.sqrt(n)) if sd else float("nan")


def evaluate_b2(trades, left_out):
    from scipy import stats
    if not trades:
        return {"status": "NO_TRIGGERS"}
    days = sorted({t["d"] for t in trades})
    mid = days[len(days) // 2]
    dm = lambda ts, key: {d: sum(x[key] for x in ts if x["d"] == d) / sum(1 for x in ts if x["d"] == d) for d in sorted({x["d"] for x in ts})}  # noqa: E731
    net_d = dm(trades, "net")
    v = list(net_d.values())
    crit = float(stats.t.ppf(1 - ALPHA, max(1, len(v) - 1)))
    h = lambda dd: (sum(x for d_, x in dd.items() if d_ < mid) / max(1, sum(1 for d_ in dd if d_ < mid)), sum(x for d_, x in dd.items() if d_ >= mid) / max(1, sum(1 for d_ in dd if d_ >= mid)))  # noqa: E731
    lifts = {}
    for key in ("ca", "cb"):
        ts = [{**x, "lift": x["net"] - x[key]} for x in trades if x.get(key) is not None]
        ld = dm(ts, "lift") if ts else {}
        lifts[key] = {"mean": sum(ld.values()) / max(1, len(ld)), "t": tstat(list(ld.values())), "h": h(ld) if ld else (float("nan"), float("nan"))}
    hed = dm(trades, "hedged")
    best = set(sorted(net_d, key=lambda d_: -net_d[d_])[:5])
    names = {}
    for x in trades:
        names[x["sym"]] = names.get(x["sym"], 0) + x["net"]
    top = set(sorted(names, key=lambda s: -names[s])[:5])
    drop_d = [x["net"] for x in trades if x["d"] not in best]
    drop_n = [x["net"] for x in trades if x["sym"] not in top]
    n_h = (sum(1 for x in trades if x["d"] < mid), sum(1 for x in trades if x["d"] >= mid))
    d_h = (len({x["d"] for x in trades if x["d"] < mid}), len({x["d"] for x in trades if x["d"] >= mid}))
    n10 = (sum(x["net10"] for x in trades if x["d"] < mid) / max(1, n_h[0]), sum(x["net10"] for x in trades if x["d"] >= mid) / max(1, n_h[1]))
    hn = h(net_d)
    c = {"1": sum(v) / len(v) > 0 and tstat(v) >= crit, "2": hn[0] > 0 and hn[1] > 0,
         "3": all(lifts[k]["h"][0] > 0 and lifts[k]["h"][1] > 0 and lifts[k]["t"] >= 1.65 for k in lifts),
         "4": (sum(drop_d) / max(1, len(drop_d)) > 0) and (sum(drop_n) / max(1, len(drop_n)) > 0), "5": n10[0] > 0 and n10[1] > 0,
         "7": all(x > 0 for x in h(hed)) and tstat(list(hed.values())) >= 1.65}
    void = left_out / max(1, len(trades)) > 0.20
    under = len(trades) < 300 or min(n_h) < 100 or len(days) < 40 or min(d_h) < 15
    status = "VOID" if void else ("UNDERPOWERED" if under else ("PASS" if all(c.values()) else "FAIL"))
    up = [x["net"] for x in trades if x["spy_up"]]; dn = [x["net"] for x in trades if not x["spy_up"]]
    return {"status": status, "n": len(trades), "n_h": n_h, "days": len(days), "mean": sum(v) / len(v), "t": tstat(v), "crit": crit, "halves": hn, "lift_a": lifts["ca"], "lift_b": lifts["cb"],
            "hedged": sum(hed.values()) / len(hed), "hedged_t": tstat(list(hed.values())), "hedged_h": h(hed), "drop_days": sum(drop_d) / max(1, len(drop_d)),
            "drop_names": sum(drop_n) / max(1, len(drop_n)), "net10_h": n10, "left_out": left_out, "spy_up": (sum(up) / max(1, len(up)), len(up)), "spy_dn": (sum(dn) / max(1, len(dn)), len(dn)), "c": c}


def run():
    from ticker_filters import is_not_stock
    from day_hold import asset_names
    nm = asset_names() or {}
    if sum(1 for k in nm if k != "__cached__") < 1000:
        raise SystemExit("asset names unavailable: refusing to run without the ETF/ETN exclusion")
    excluded = set()
    files = sorted(WH.glob("20??-??-??.npz"))
    hist = deque(maxlen=20)
    prev, prev_date = None, None
    r5, b2, eps, left = [], [], 0, 0
    dropped = 0
    for f in files:
        d = date.fromisoformat(f.stem)
        day = load_day(f)
        if "SPY" not in day["idx"] or prev is None or prev_date != prev_weekday(d):
            dropped += 1
            prev, prev_date = day, d
            hist.append(day)
            continue
        for s in prev["syms"]:
            if s not in excluded and is_not_stock(s, nm.get(s, "") or ""):
                excluded.add(s)
        u = universe(day, prev, excluded)
        if len(hist) >= 15:
            r = score_day_r5(day, prev, list(hist), u, f.stem)
            if r:
                r5.append(r)
            tr, e, lo = score_day_b2(day, prev, list(hist), u, f.stem)
            b2 += tr; eps += e; left += lo
        prev, prev_date = day, d
        hist.append(day)
        print(f.stem, "R5 days", len(r5), "B2 trades", len(b2), flush=True)
    return r5, b2, eps, left, dropped, len(files)


def report():
    r5, b2, eps, left, dropped, nf = run()
    e5 = evaluate_r5(r5)
    e2 = evaluate_b2(b2, left)
    f = lambda x: "nan" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:+.1f}"  # noqa: E731
    out = [f"# Regime + beta decoupling (prereg docs/studies/regime_beta_family_prereg.json), written {datetime.now(ET):%Y-%m-%d %H:%M} ET",
           f"{nf} warehouse sessions, {dropped} dropped. Alpha 0.01 one-sided (five hypotheses this weekend).", "", "## R5 regime gate"]
    if "diff" in e5:
        out += [f"- scored days {e5['n']}: HIGH {e5['hi']}, other {e5['lo']}; counts by half {e5['cnt']}",
                f"- afternoon continuation net: HIGH minus other {e5['diff']:+.1f} bp; Welch t {e5['t_welch']:+.2f} (crit {e5['crit_w']:.2f}); HAC t {e5['t_hac']:+.2f} (crit {e5['crit_h']:.2f}); by half {f(e5['diff_h'][0])} / {f(e5['diff_h'][1])}",
                f"- HIGH-day net by half {f(e5['hi_net_h'][0])} / {f(e5['hi_net_h'][1])}; hedged {f(e5['hi_hedged_h'][0])} / {f(e5['hi_hedged_h'][1])}; lift vs same-hour minutes {f(e5['hi_lift_h'][0])} / {f(e5['hi_lift_h'][1])} (t {e5['lift_t']:+.2f}); 10 bp {f(e5['hi_net10_h'][0])} / {f(e5['hi_net10_h'][1])}",
                f"- persistence: correlation of the morning score with the afternoon net {e5['persist']:+.2f}; conditions {e5['c']}"]
    out += [f"**R5 VERDICT: {e5['status']}**", "", "## B2 beta decoupling"]
    if "mean" in e2:
        out += [f"- triggers {e2['n']} over {e2['days']} days (by half {e2['n_h']}); distinct SPY-dip episodes {eps}; left out of control (b) {e2['left_out']}",
                f"- mean per-day net {e2['mean']:+.1f} bp, t {e2['t']:+.2f} (crit {e2['crit']:.2f}); halves {f(e2['halves'][0])} / {f(e2['halves'][1])}",
                f"- lift vs same-name same-hour {e2['lift_a']['mean']:+.1f} (t {e2['lift_a']['t']:+.2f}); vs matched non-decoupler {e2['lift_b']['mean']:+.1f} (t {e2['lift_b']['t']:+.2f})",
                f"- beta-hedged net {e2['hedged']:+.1f} (t {e2['hedged_t']:+.2f}, halves {f(e2['hedged_h'][0])} / {f(e2['hedged_h'][1])}); drop best 5 days {e2['drop_days']:+.1f}; top 5 names {e2['drop_names']:+.1f}; 10 bp by half {f(e2['net10_h'][0])} / {f(e2['net10_h'][1])}",
                f"- SPY next 10 bars up: net {e2['spy_up'][0]:+.1f} (n {e2['spy_up'][1]}); not up: {e2['spy_dn'][0]:+.1f} (n {e2['spy_dn'][1]}); conditions {e2['c']}"]
    out += [f"**B2 VERDICT: {e2['status']}**", ""]
    text = "\n".join(out)
    (ROOT / "docs" / "REGIME_BETA.md").write_text(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(report())
