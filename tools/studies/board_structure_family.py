#!/usr/bin/env python3
"""board_structure_family (docs/studies/board_structure_family_prereg.json, amended after skeptic round 1). Read-only.

H1 peer lead-lag and H2 stop-run reclaim on the SIP warehouse (ai_reports/warehouse/sip/DAY.npz). H3 (board breadth) is a separate mode, built later.
Trade for H1/H2: buy at the open of the bar after the trigger, sell at the close 10 bars after the entry bar; 6 bp round trip (10 bp stress).

    .venv/bin/python tools/studies/board_structure_family.py h1h2      # mini
"""
from __future__ import annotations

import hashlib
import math
import random
import sys
from collections import deque
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))
from relative_strength_scan import load_day, prev_weekday, dollar_volume  # noqa: E402

ET = ZoneInfo("America/New_York")
WH = ROOT / "ai_reports" / "warehouse" / "sip"
TOP_N, PRICE_LO, PRICE_HI = 400, 10.0, 500.0
COST, COST_STRESS, HOLD = 6.0, 10.0, 10
ALPHA = 0.05 / 3
H1_FIRST, H2_FIRST, LAST = 15, 20, 360           # bar indexes: 09:45, 09:50, 15:30 (0 = 09:30)
REFRACT = 30


def seeded(*parts):
    return random.Random(int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest(), 16) % (2 ** 32))


def universe(day, prev, excluded):
    dv = dollar_volume(prev)
    out = []
    for i in np.argsort(-dv):
        s = prev["syms"][i]
        if s == "SPY" or s in excluded:
            continue
        j = day["idx"].get(s)
        if j is None:
            continue
        o0 = day["o"][j, 0]
        if np.isnan(o0) or not (PRICE_LO <= o0 <= PRICE_HI):
            continue
        out.append(s)
        if len(out) >= TOP_N:
            break
    return out


# ---------- H1 pieces ----------
def residual_stats(hist, names):
    """beta vs SPY and residual-correlation peers from the previous sessions (09:30-09:34 excluded). -> (beta{}, peers{})"""
    cols = []
    spy_cols = []
    present = {s: 0 for s in names}
    for d in hist:
        si = d["idx"].get("SPY")
        if si is None:
            continue
        spy = d["c"][si]
        rs = spy[6:] / spy[5:-1] - 1                     # returns from 09:35 on
        mat = np.full((len(names), len(rs)), np.nan, dtype=np.float64)
        for k, s in enumerate(names):
            i = d["idx"].get(s)
            if i is None:
                continue
            c = d["c"][i]
            r = c[6:] / c[5:-1] - 1
            if np.sum(~np.isnan(r)) > 100:
                present[s] += 1
            mat[k] = r
        cols.append(mat)
        spy_cols.append(rs)
    if not cols:
        return {}, {}
    R = np.concatenate(cols, axis=1)
    S = np.concatenate(spy_cols)
    beta = {}
    E = np.full_like(R, np.nan)
    for k, s in enumerate(names):
        m = ~np.isnan(R[k]) & ~np.isnan(S)
        if m.sum() < 500 or present[s] < 15:
            continue
        b = float(np.dot(R[k, m], S[m]) / np.dot(S[m], S[m])) if np.dot(S[m], S[m]) > 0 else 1.0
        beta[s] = b
        E[k, m] = R[k, m] - b * S[m]
    ok = [k for k, s in enumerate(names) if s in beta]
    peers = {}
    if len(ok) < 10:
        return beta, peers
    X = E[ok]
    M = ~np.isnan(X)
    Xz = np.where(M, X, 0.0)
    cnt = M.astype(np.float64) @ M.T.astype(np.float64)
    sx = Xz @ M.T.astype(np.float64)
    sxx = (Xz * Xz) @ M.T.astype(np.float64)
    sxy = Xz @ Xz.T
    with np.errstate(invalid="ignore", divide="ignore"):
        mx, my = sx / cnt, sx.T / cnt
        vx, vy = sxx / cnt - mx ** 2, sxx.T / cnt - my ** 2
        corr = (sxy / cnt - mx * my) / np.sqrt(vx * vy)
    corr[cnt < 300] = np.nan
    np.fill_diagonal(corr, np.nan)
    for a, ka in enumerate(ok):
        row = corr[a]
        order = [b for b in np.argsort(-np.nan_to_num(row, nan=-9)) if not np.isnan(row[b]) and row[b] >= 0.35][:5]
        if len(order) >= 2:
            peers[names[ka]] = [names[ok[b]] for b in order]
    return beta, peers


def slow_wr(prev_c, prev_h, prev_l, c, h, l, n=112, span=3):  # noqa: E741
    """Slow %R(112) with EMA span 3 on prev-session + today RTH bars (RTH-only warm-up). Returns today's 390 values."""
    C = np.concatenate([prev_c, c]); H = np.concatenate([prev_h, h]); L = np.concatenate([prev_l, l])
    raw = np.full(len(C), np.nan)
    for i in range(n - 1, len(C)):
        hh, ll, cc = np.nanmax(H[i - n + 1:i + 1]), np.nanmin(L[i - n + 1:i + 1]), C[i]
        if not np.isnan(cc) and hh > ll:
            raw[i] = -100.0 * (hh - cc) / (hh - ll)
    a = 2.0 / (span + 1)
    out = np.full(len(C), np.nan)
    prevv = None
    for i, v in enumerate(raw):
        if np.isnan(v):
            out[i] = prevv if prevv is not None else np.nan
            continue
        prevv = v if prevv is None else a * v + (1 - a) * prevv
        out[i] = prevv
    return out[len(prev_c):]


def trade_ret(day, i, t, cost):
    """Buy the open of bar t+1, sell the close of bar t+1+HOLD (walk forward up to 5 bars if missing; truncated at 15:59)."""
    o = day["o"][i]
    c = day["c"][i]
    if t + 1 >= 390 or np.isnan(o[t + 1]):
        return None
    for k in range(min(389, t + 1 + HOLD), min(390, t + 1 + HOLD + 6)):
        if not np.isnan(c[k]):
            return (c[k] / o[t + 1] - 1) * 1e4 - cost
    return None


def refract(ts):
    out, last = [], -999
    for t in ts:
        if t - last >= REFRACT:
            out.append(t)
            last = t
    return out


def control_minutes(rng, trig_ts, allowed_mask, t, k=5):
    """Same-name control minutes STRICTLY AFTER the trade (t+HOLD+2 .. t+HOLD+62), clear of every trigger's hold window.
    11/07 (P2 control standard): the earlier +-30 window drew minutes from t-30..t-16, on the path that made the stock fire."""
    cand = [m for m in range(max(H1_FIRST, t + HOLD + 2), min(LAST, t + HOLD + 62) + 1)
            if allowed_mask[m] and all(m < x - 15 or m > x + HOLD + 1 for x in trig_ts)]
    if not cand:
        return []
    return [rng.choice(cand) for _ in range(k)]


def score_h1(day, prev, hist, names, d):
    beta, peers = residual_stats(hist, names)
    spy = day["c"][day["idx"]["SPY"]]
    r2spy = np.full(390, np.nan)
    r2spy[2:] = spy[2:] / spy[:-2] - 1
    res2, surge = {}, {}
    for s in names:
        if s not in beta:
            continue
        i = day["idx"][s]
        c, v = day["c"][i], day["v"][i]
        r2 = np.full(390, np.nan)
        r2[2:] = c[2:] / c[:-2] - 1
        res2[s] = r2 - beta[s] * r2spy
        vr = np.full(390, np.nan)
        for t in range(15, 390):
            w = v[t - 15:t]
            mv = np.nanmean(w) if np.any(~np.isnan(w)) else np.nan
            vr[t] = v[t] / mv if mv and mv > 0 else np.nan
        surge[s] = (res2[s] >= 0.0040) & (vr >= 2.0)
    trades = []
    for s, ps in peers.items():
        if s not in res2:
            continue
        i = day["idx"][s]
        pi = prev["idx"].get(s)
        if pi is None:
            continue
        wr = slow_wr(prev["c"][pi], prev["h"][pi], prev["l"][pi], day["c"][i], day["h"][i], day["l"][i])
        lows = day["l"][i]
        cnt = np.zeros(390)
        for p in ps:
            if p in surge:
                cnt += np.nan_to_num(surge[p].astype(float))
        trig = []
        for t in range(H1_FIRST, LAST + 1):
            r = res2[s][t]
            if cnt[t] >= 2 and not np.isnan(r) and 0 <= r <= 0.0015 and not np.isnan(wr[t]) and wr[t] < -20:
                prior = lows[t - 15:t]
                if np.any(~np.isnan(prior)) and day["c"][i][t] > np.nanmin(prior):
                    trig.append(t)
        trig = refract(trig)
        allowed = np.ones(390, dtype=bool)
        for t in trig:
            net = trade_ret(day, i, t, COST)
            if net is None:
                continue
            rng = seeded(s, d, "H1-a", t)
            ca = [trade_ret(day, i, m, COST) for m in control_minutes(rng, trig, allowed, t)]
            ca = [x for x in ca if x is not None]
            others = [x for x in names if x != s and x not in ps and x in res2]
            rb = seeded(s, d, t, "H1-b")
            cb = None
            for _ in range(10):
                o = rb.choice(others)
                cb = trade_ret(day, day["idx"][o], t, COST)
                if cb is not None:
                    break
            trades.append({"d": d, "sym": s, "net": net, "net10": net + COST - COST_STRESS,
                           "ca": sum(ca) / len(ca) if ca else None, "cb": cb})
    return trades


def score_h2(day, prev, names, d):
    trades = []
    for s in names:
        i = day["idx"][s]
        pi = prev["idx"].get(s)
        if pi is None:
            continue
        pc = prev["c"][pi]
        okp = np.where(~np.isnan(pc))[0]
        if not len(okp):
            continue
        prev_close = pc[okp[-1]]
        o, h, l, c, v = (day[k][i] for k in ("o", "h", "l", "c", "v"))  # noqa: E741
        pv = np.nan_to_num(c * v)
        cv = np.cumsum(np.nan_to_num(v))
        vwap = np.where(cv > 0, np.cumsum(pv) / np.where(cv > 0, cv, 1), np.nan)
        pre = (c > prev_close) & (c > vwap)
        trig = []
        for t in range(H2_FIRST, LAST + 1):
            if not pre[t] or np.isnan(l[t - 1]) or np.isnan(c[t]):
                continue
            w = l[t - 16:t - 1]
            wv = v[t - 16:t - 1]
            if np.sum(~np.isnan(w)) < 10:
                continue
            low15 = np.nanmin(w)
            under = (low15 - l[t - 1]) / low15 * 1e4
            mv = np.nanmean(wv)
            if 5 <= under <= 20 and mv > 0 and v[t - 1] >= 2 * mv and c[t] > low15:
                trig.append(t)
        trig = refract(trig)
        for t in trig:
            net = trade_ret(day, i, t, COST)
            if net is None:
                continue
            rng = seeded(s, d, "H2-a", t)
            ca = [trade_ret(day, i, m, COST) for m in control_minutes(rng, trig, pre, t)]
            ca = [x for x in ca if x is not None]
            rb = seeded(s, d, t, "H2-b")
            cb = None
            for _ in range(20):
                o2 = rb.choice(names)
                j = day["idx"][o2]
                p2 = prev["idx"].get(o2)
                if o2 == s or p2 is None:
                    continue
                pc2 = prev["c"][p2]
                ok2 = np.where(~np.isnan(pc2))[0]
                c2, v2 = day["c"][j], day["v"][j]
                cv2 = np.cumsum(np.nan_to_num(v2))
                if not len(ok2) or cv2[t] <= 0 or np.isnan(c2[t]):
                    continue
                vw2 = np.cumsum(np.nan_to_num(c2 * v2))[t] / cv2[t]
                if c2[t] > pc2[ok2[-1]] and c2[t] > vw2:
                    cb = trade_ret(day, j, t, COST)
                    if cb is not None:
                        break
            trades.append({"d": d, "sym": s, "net": net, "net10": net + COST - COST_STRESS,
                           "ca": sum(ca) / len(ca) if ca else None, "cb": cb})
    return trades


# ---------- statistics ----------
def tstat(v):
    n = len(v)
    if n < 3:
        return float("nan")
    m = sum(v) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in v) / (n - 1))
    return m / (sd / math.sqrt(n)) if sd else float("nan")


def evaluate(trades):
    from scipy import stats
    if not trades:
        return {"status": "NO_TRIGGERS"}
    days = sorted({t["d"] for t in trades})
    mid = days[len(days) // 2]
    dm = lambda ts, key: {d: sum(x[key] for x in ts if x["d"] == d) / sum(1 for x in ts if x["d"] == d) for d in sorted({x["d"] for x in ts})}  # noqa: E731
    net_d = dm(trades, "net")
    v = list(net_d.values())
    crit = float(stats.t.ppf(1 - ALPHA, max(1, len(v) - 1)))
    t_net = tstat(v)
    h = lambda dd: (sum(x for d_, x in dd.items() if d_ < mid) / max(1, sum(1 for d_ in dd if d_ < mid)), sum(x for d_, x in dd.items() if d_ >= mid) / max(1, sum(1 for d_ in dd if d_ >= mid)))  # noqa: E731
    lifts = {}
    for key in ("ca", "cb"):
        ts = [{**x, "lift": x["net"] - x[key]} for x in trades if x.get(key) is not None]
        ld = dm(ts, "lift") if ts else {}
        lifts[key] = {"mean": sum(ld.values()) / max(1, len(ld)), "t": tstat(list(ld.values())), "h": h(ld) if ld else (float("nan"), float("nan"))}
    best = set(sorted(net_d, key=lambda d_: -net_d[d_])[:5])
    names = {}
    for x in trades:
        names[x["sym"]] = names.get(x["sym"], 0) + x["net"]
    top = set(sorted(names, key=lambda s: -names[s])[:5])
    drop_d = [x["net"] for x in trades if x["d"] not in best]
    drop_n = [x["net"] for x in trades if x["sym"] not in top]
    n_h = (sum(1 for x in trades if x["d"] < mid), sum(1 for x in trades if x["d"] >= mid))
    hn = h(net_d)
    c = {"1": sum(v) / len(v) > 0 and t_net >= crit, "2": hn[0] > 0 and hn[1] > 0,
         "3": all(lifts[k]["h"][0] > 0 and lifts[k]["h"][1] > 0 and lifts[k]["t"] >= 1.65 for k in lifts),
         "4": (sum(drop_d) / max(1, len(drop_d)) > 0) and (sum(drop_n) / max(1, len(drop_n)) > 0),
         "5": sum(x["net10"] for x in trades) / len(trades) > 0}
    under = len(trades) < 300 or min(n_h) < 100
    status = "UNDERPOWERED" if under else ("PASS" if all(c.values()) else "FAIL")
    return {"status": status, "n": len(trades), "n_h": n_h, "days": len(v), "mean": sum(v) / len(v), "t": t_net, "crit": crit, "halves": hn,
            "lift_a": lifts["ca"], "lift_b": lifts["cb"], "drop_days": sum(drop_d) / max(1, len(drop_d)), "drop_names": sum(drop_n) / max(1, len(drop_n)),
            "net10": sum(x["net10"] for x in trades) / len(trades), "c": c}


def run_h1h2():
    from ticker_filters import is_not_stock
    from day_hold import asset_names
    names_map = asset_names() or {}
    if sum(1 for k in names_map if k != "__cached__") < 1000:
        raise SystemExit("asset names unavailable: refusing to run without the ETF/ETN exclusion")
    excluded = set()
    files = sorted(WH.glob("20??-??-??.npz"))
    hist = deque(maxlen=20)
    prev, prev_date = None, None
    t1, t2, dropped = [], [], 0
    for f in files:
        d = date.fromisoformat(f.stem)
        day = load_day(f)
        if "SPY" not in day["idx"] or prev is None or prev_date != prev_weekday(d):
            dropped += 1
            prev, prev_date = day, d
            hist.append(day)
            continue
        for s in prev["syms"]:
            if s not in excluded and is_not_stock(s, names_map.get(s, "") or ""):
                excluded.add(s)
        u = universe(day, prev, excluded)
        if len(hist) >= 15:
            t1 += score_h1(day, prev, list(hist), u, f.stem)
        t2 += score_h2(day, prev, u, f.stem)
        prev, prev_date = day, d
        hist.append(day)
        print(f.stem, "H1", len(t1), "H2", len(t2), flush=True)
    return t1, t2, dropped, len(files)


def report_h1h2():
    t1, t2, dropped, nf = run_h1h2()
    out = [f"# Board-structure family: H1 peer lead-lag, H2 stop-run reclaim (prereg docs/studies/board_structure_family_prereg.json)",
           f"written {datetime.now(ET):%Y-%m-%d %H:%M} ET; {nf} warehouse sessions, {dropped} dropped (no SPY / not consecutive). Family bar: one-sided alpha 0.0167.", ""]
    for lab, tr in (("H1 peer lead-lag", t1), ("H2 stop-run reclaim", t2)):
        ev = evaluate(tr)
        out.append(f"## {lab}")
        if ev.get("status") == "NO_TRIGGERS":
            out += ["no triggers", ""]
            continue
        ca_v = [x["ca"] for x in tr if x.get("ca") is not None]
        cb_v = [x["cb"] for x in tr if x.get("cb") is not None]
        out += [f"- CONTROL SANITY: same-name later-only control mean {sum(ca_v) / max(1, len(ca_v)):+.2f} bp (n {len(ca_v)}), same-minute other-stock control mean {sum(cb_v) / max(1, len(cb_v)):+.2f} bp (n {len(cb_v)}); unconditional random minute on this warehouse ~ -6.2 bp (bar_signal_family 11/07)",
                f"- triggers {ev['n']} over {ev['days']} days, by half {ev['n_h']}",
                f"- mean per-day net {ev['mean']:+.1f} bp, t {ev['t']:+.2f} (family critical {ev['crit']:.2f}); halves {ev['halves'][0]:+.1f} / {ev['halves'][1]:+.1f}",
                f"- lift vs same-name later-only random minutes {ev['lift_a']['mean']:+.1f} (t {ev['lift_a']['t']:+.2f}, halves {ev['lift_a']['h'][0]:+.1f} / {ev['lift_a']['h'][1]:+.1f})",
                f"- lift vs same-minute random stock {ev['lift_b']['mean']:+.1f} (t {ev['lift_b']['t']:+.2f}, halves {ev['lift_b']['h'][0]:+.1f} / {ev['lift_b']['h'][1]:+.1f})",
                f"- drop best 5 days {ev['drop_days']:+.1f}; drop top 5 names {ev['drop_names']:+.1f}; at 10 bp cost {ev['net10']:+.1f}; conditions {ev['c']}",
                f"**VERDICT: {ev['status']}**", ""]
    text = "\n".join(out)
    (ROOT / "docs" / "BOARD_STRUCTURE_H1H2.md").write_text(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    a = sys.argv[1:] or ["h1h2"]
    raise SystemExit(report_h1h2() if a[0] == "h1h2" else 2)
