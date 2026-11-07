#!/usr/bin/env python3
"""bar_signal_family (docs/studies/bar_signal_family_prereg.json, skeptic rounds 1-2 applied). Read-only, SIP warehouse.

V1 volume-clock %R, K absorption bar, W opening washout, D dwell/knock. Trade: buy the open of bar t+1, sell the close of bar t+10
(hold 10 bars including the entry bar), 6 bp round trip (10 bp binding case). Bar index 0 = 09:30.

    .venv/bin/python tools/studies/bar_signal_family.py counts   # trigger counts only, no returns (dry run)
    .venv/bin/python tools/studies/bar_signal_family.py run      # full read -> docs/BAR_SIGNAL_FAMILY.md
"""
from __future__ import annotations

import math
import sys
from collections import deque
from datetime import date
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))
from relative_strength_scan import load_day, prev_weekday  # noqa: E402
from board_structure_family import seeded, universe, tstat  # noqa: E402

WH = ROOT / "ai_reports" / "warehouse" / "sip"
COST, COST10, HOLD = 6.0, 10.0, 10
FIRST, LAST = 15, 360                  # 09:45 .. 15:30
ALPHA = 0.05 / 4
REFRACT = 30
NDRAW = 5


# ---------------- pure helpers ----------------
def wr_smoothed(h, l, c, n=21, span=7):  # noqa: E741
    """Williams %R(n) then ewm(span, adjust=False) over valid values (signals.py). NaN until n bars exist."""
    h, l, c = (np.asarray(x, dtype=float) for x in (h, l, c))  # noqa: E741
    out = np.full(len(c), np.nan)
    a = 2.0 / (span + 1.0)
    prev = None
    for i in range(n - 1, len(c)):
        hh, ll = np.nanmax(h[i - n + 1:i + 1]), np.nanmin(l[i - n + 1:i + 1])
        if not np.isfinite(hh) or not np.isfinite(ll) or hh <= ll or not np.isfinite(c[i]):
            continue
        raw = -100.0 * (hh - c[i]) / (hh - ll)
        prev = raw if prev is None else a * raw + (1 - a) * prev
        out[i] = prev
    return out


def volume_bars(v, h, l, c, size):  # noqa: E741
    """-> list of (crossing_minute, H, L, C). At most ONE bar per minute; overshoot carries (counter jumps to floor(cum/size)+1)."""
    bars, cum, nxt, start = [], 0.0, size, 0
    for m in range(len(c)):
        vm = v[m]
        cum += 0.0 if not np.isfinite(vm) else float(vm)
        if cum >= nxt and size > 0:
            seg_h, seg_l = np.asarray(h[start:m + 1], float), np.asarray(l[start:m + 1], float)
            if np.isfinite(seg_h).any() and np.isfinite(seg_l).any() and np.isfinite(c[m]):
                bars.append((m, float(np.nanmax(seg_h)), float(np.nanmin(seg_l)), float(c[m])))
            nxt = (math.floor(cum / size) + 1) * size
            start = m + 1
    return bars


def vclock_wr_by_minute(bars, n_min=390, n=21, span=7):
    """Per minute: list of smoothed volume-clock %R values for bars closed at or before that minute (carried forward)."""
    if len(bars) < n:
        return [[] for _ in range(n_min)]
    w = wr_smoothed([b[1] for b in bars], [b[2] for b in bars], [b[3] for b in bars], n, span)
    out, k = [], 0
    for m in range(n_min):
        while k < len(bars) and bars[k][0] <= m:
            k += 1
        out.append([x for x in w[:k] if np.isfinite(x)])
    return out


def v1_cond(vals, need=5):
    """Leaving oversold: latest >= -50 and min of the last `need` units <= -80."""
    return bool(vals) and vals[-1] >= -50 and min(vals[-need:]) <= -80


def v1_flags(day, i, size):
    """-> (vol_cond[390], time_cond[390], vol_oversold_notleft[390]) transition-free condition arrays."""
    o, h, l, c, v = (day[k][i] for k in ("o", "h", "l", "c", "v"))  # noqa: E741
    vb = volume_bars(v, h, l, c, size)
    per_min = vclock_wr_by_minute(vb)
    vol = np.array([v1_cond(p) for p in per_min])
    vol_over = np.array([bool(p) and min(p[-5:]) <= -80 and p[-1] < -50 for p in per_min])
    tw = wr_smoothed(h, l, c)
    tim = np.zeros(390, dtype=bool)
    for m in range(21, 390):
        win = tw[m - 4:m + 1]
        tim[m] = bool(np.isfinite(tw[m]) and tw[m] >= -50 and np.isfinite(win).any() and np.nanmin(win) <= -80)
    return vol, tim, vol_over


def transitions(cond, first=FIRST, last=LAST):
    return [t for t in range(max(first, 1), last + 1) if cond[t] and not cond[t - 1]]


def refract(ts):
    kept, supp, last = [], [], -999
    for t in ts:
        if t - last >= REFRACT:
            kept.append(t)
            last = t
        else:
            supp.append(t)
    return kept, supp


def eligible_mask(kept, supp, lo=FIRST, hi=LAST):
    m = np.ones(390, dtype=bool)
    m[:lo] = False
    m[hi + 1:] = False
    # Symmetric: a control minute's 10-bar forward return must not contain the move that fired a trigger, so exclude
    # 15 bars BEFORE as well as the hold window after (prereg: "more than 15 minutes from any trigger"). 11/07 sanity
    # check found the one-sided version inflated same-name controls to +11..+28 bp against -7.5 bp for random minutes.
    for x in list(kept) + list(supp):
        m[max(0, x - 15):x + HOLD + 2] = False
    return m


def draw(rng, mask, t, k=NDRAW, extra=None):
    cand = [m for m in range(max(FIRST, t - 30), min(LAST, t + 30) + 1) if mask[m] and (extra is None or extra[m])]
    return [rng.choice(cand) for _ in range(k)] if cand else []


def k_masks(day, i):
    """-> (full trigger cond, drop&low15 cond without the volume/range part). Measured at bar close t, no look-ahead."""
    o, h, l, c, v = (day[k][i] for k in ("o", "h", "l", "c", "v"))  # noqa: E741
    full, partial = np.zeros(390, dtype=bool), np.zeros(390, dtype=bool)
    rng = h - l
    for t in range(20, 390):
        wv, wr = v[t - 20:t], rng[t - 20:t]
        if np.isnan(wv).any() or np.isnan(wr).any() or np.isnan(v[t]) or np.isnan(c[t]) or np.isnan(c[t - 1]) or np.isnan(c[t - 16]):
            continue
        mv, mr = float(np.median(wv)), float(np.median(wr))
        if mv <= 0 or mr <= 0:
            continue
        drop = c[t - 1] / c[t - 16] - 1 <= -0.003
        low15 = l[t] <= np.nanmin(l[t - 14:t + 1]) + 1e-12
        if drop and low15:
            partial[t] = True
            if v[t] >= 3 * mv and rng[t] <= 0.5 * mr and c[t] >= (h[t] + l[t]) / 2:
                full[t] = True
    return full, partial & ~full


def w_info(day, i):
    """-> (washout bool, first-cross bar in 15..60 or None, below_open_at_0944 bool)."""
    o, l, c = day["o"][i], day["l"][i], day["c"][i]
    if np.isnan(o[0]) or np.isnan(c[14]):
        return False, None, False
    below = bool(c[14] < o[0])
    wash = bool(np.nanmin(l[0:15]) <= o[0] * 0.99) and below
    cross = None
    for t in range(15, 61):
        if np.isfinite(c[t]) and c[t] > o[0]:
            cross = t
            break
    return wash, cross, below


def d_events(day, i):
    """-> list of (t, knocks) for fresh-breakout bars (close>level, previous two closes <= level), t >= 30 and <= LAST."""
    h, l, c = day["h"][i], day["l"][i], day["c"][i]
    out = []
    for t in range(30, LAST + 1):
        win = h[t - 30:t - 2]
        if np.isnan(win).any() or np.isnan(c[t]) or np.isnan(c[t - 1]) or np.isnan(c[t - 2]):
            continue
        level = float(np.max(win))
        if not (c[t] > level and c[t - 1] <= level and c[t - 2] <= level):
            continue
        k1 = int(np.argmax(win)) + (t - 30)
        knocks, prev = 1, k1
        for j in range(k1 + 1, t - 2):
            if h[j] >= level * (1 - 0.0005) and j - prev >= 2 and np.nanmin(l[prev + 1:j]) <= level * (1 - 0.001):
                knocks += 1
                prev = j
        out.append((t, knocks))
    return out


def ret(day, i, t, hold=HOLD, cost=COST):
    o, c = day["o"][i], day["c"][i]
    if t + 1 >= 390 or np.isnan(o[t + 1]):
        return None
    for k in range(min(389, t + 1 + hold), min(390, t + 1 + hold + 6)):
        if not np.isnan(c[k]):
            return (c[k] / o[t + 1] - 1) * 1e4 - cost
    return None


def trade(day, i, sym, d, t, **ctrl):
    r = ret(day, i, t)
    if r is None:
        return None
    row = {"d": d, "sym": sym, "t": t, "net": r, "net10": r + COST - COST10,
           "h3": ret(day, i, t, 3), "h30": ret(day, i, t, 30)}
    row.update(ctrl)
    return row


def mean_ret(day, i, ts):
    r = [x for x in (ret(day, i, m) for m in ts) if x is not None]
    return sum(r) / len(r) if r else None


# ---------------- evaluation ----------------
def evaluate(trades, lift_keys, mandatory_b=None):
    from scipy import stats
    if not trades:
        return {"status": "UNDERPOWERED", "n": 0}
    days = sorted({t["d"] for t in trades})
    mid = days[len(days) // 2]
    dm = lambda ts, key: {d: sum(x[key] for x in ts if x["d"] == d) / sum(1 for x in ts if x["d"] == d) for d in sorted({x["d"] for x in ts})}  # noqa: E731
    net_d = dm(trades, "net")
    crit = float(stats.t.ppf(1 - ALPHA, max(1, len(net_d) - 1)))
    h = lambda dd: (sum(x for d_, x in dd.items() if d_ < mid) / max(1, sum(1 for d_ in dd if d_ < mid)),  # noqa: E731
                    sum(x for d_, x in dd.items() if d_ >= mid) / max(1, sum(1 for d_ in dd if d_ >= mid)))
    lifts, matched, nt = {}, {}, []
    for k in lift_keys:
        ts = [{**x, "lift": x["net"] - x[k]} for x in trades if x.get(k) is not None]
        matched[k] = len(ts)
        ld = dm(ts, "lift") if ts else {}
        lifts[k] = {"n": len(ts), "mean": sum(ld.values()) / max(1, len(ld)), "t": tstat(list(ld.values())), "h": h(ld) if ld else (float("nan"), float("nan"))}
        if k == mandatory_b and len(ts) < 100:
            nt.append(k)
    n_h = (sum(1 for x in trades if x["d"] < mid), sum(1 for x in trades if x["d"] >= mid))
    dh = (sum(1 for d_ in net_d if d_ < mid), sum(1 for d_ in net_d if d_ >= mid))
    best = set(sorted(net_d, key=lambda d_: -net_d[d_])[:5])
    names = {}
    for x in trades:
        names[x["sym"]] = names.get(x["sym"], 0) + x["net"]
    top = set(sorted(names, key=lambda s: -names[s])[:5])
    rest = [x["net"] for x in trades if x["d"] not in best and x["sym"] not in top]
    hn = h(net_d)
    t_net = tstat(list(net_d.values()))
    c = {"1": sum(net_d.values()) / len(net_d) > 0 and t_net >= crit, "2": hn[0] > 0 and hn[1] > 0,
         "3": all(lifts[k]["h"][0] > 0 and lifts[k]["h"][1] > 0 and lifts[k]["t"] >= crit for k in lift_keys),
         "4": bool(rest) and sum(rest) / len(rest) > 0,
         "5": sum(x["net10"] for x in trades) / len(trades) > 0}
    under = len(trades) < 300 or min(n_h) < 100 or len(net_d) < 60 or min(dh) < 25 or bool(nt)
    status = "UNDERPOWERED" if under else ("PASS" if all(c.values()) else "FAIL")
    info = {k: sum(x[k] for x in trades if x.get(k) is not None) / max(1, sum(1 for x in trades if x.get(k) is not None)) for k in ("h3", "h30")}
    return {"status": status, "not_tested": nt, "n": len(trades), "n_h": n_h, "days": len(net_d), "day_h": dh, "mean": sum(net_d.values()) / len(net_d),
            "t": t_net, "crit": crit, "halves": hn, "lifts": lifts, "drop_both": sum(rest) / max(1, len(rest)),
            "net10": sum(x["net10"] for x in trades) / len(trades), "c": c, "info": info}


# ---------------- scoring per day ----------------
def score_day(day, prev, hist, names, d, counts_only=False):
    out = {"V1": [], "K": [], "W": [], "D": []}
    if len(hist) < 20:
        return out
    dv = {}
    for s in names:
        sums = []
        for hd in hist:
            j = hd["idx"].get(s)
            if j is None:
                sums = None
                break
            sums.append(float(np.nansum(hd["v"][j])))
        if sums:
            dv[s] = float(np.median(sums))
    idx = {s: day["idx"][s] for s in names if s in dv and s in day["idx"]}
    # per-name flags
    k_full, k_part, w_inf, d_ev, v1 = {}, {}, {}, {}, {}
    for s, i in idx.items():
        k_full[s], k_part[s] = k_masks(day, i)
        w_inf[s] = w_info(day, i)
        d_ev[s] = d_events(day, i)
        size = dv[s] / 100.0
        if size > 0:
            v1[s] = v1_flags(day, i, size)
    # --- V1
    for s, (vol, tim, vover) in v1.items():
        i = idx[s]
        kept, supp = refract(transitions(vol))
        if not kept:
            continue
        tk, _ = refract(transitions(tim))
        mask = eligible_mask(kept, supp)
        for t in kept:
            if counts_only:
                out["V1"].append({"d": d, "sym": s, "t": t})
                continue
            rng = seeded(s, d, "V1a", t)
            ca = mean_ret(day, i, draw(rng, mask, t))
            cc = mean_ret(day, i, draw(seeded(s, d, "V1c", t), mask, t, extra=vover))
            near = [x for x in tk if abs(x - t) <= 30]
            cb = mean_ret(day, i, near) if near else None
            r = trade(day, i, s, d, t, ca=ca, cb=cb, cc=cc)
            if r:
                out["V1"].append(r)
    # --- K
    drop_mat = {}
    for s, i in idx.items():
        c = day["c"][i]
        m = np.zeros(390, dtype=bool)
        for t in range(16, 390):
            if np.isfinite(c[t - 1]) and np.isfinite(c[t - 16]) and c[t - 1] / c[t - 16] - 1 <= -0.003:
                m[t] = True
        drop_mat[s] = m
    for s, i in idx.items():
        kept, supp = refract(transitions(k_full[s]))
        mask = eligible_mask(kept, supp)
        for t in kept:
            if counts_only:
                out["K"].append({"d": d, "sym": s, "t": t})
                continue
            ca = mean_ret(day, i, draw(seeded(s, d, "Ka", t), mask, t))
            cc = mean_ret(day, i, draw(seeded(s, d, "Kc", t), mask, t, extra=k_part[s]))
            pool = [x for x in idx if x != s and drop_mat[x][t]]
            cb = None
            if pool:
                rr = seeded(s, d, "Kb", t)
                picks = [rr.choice(pool) for _ in range(NDRAW)]
                vals = [ret(day, idx[x], t) for x in picks]
                vals = [x for x in vals if x is not None]
                cb = sum(vals) / len(vals) if vals else None
            r = trade(day, i, s, d, t, ca=ca, cb=cb, cc=cc)
            if r:
                out["K"].append(r)
    # --- W
    cross_pool = {}
    for s, (wash, cross, below) in w_inf.items():
        deep = bool(np.nanmin(day["l"][idx[s]][0:15]) <= day["o"][idx[s]][0] * 0.99)
        if below and (not deep) and cross is not None:
            cross_pool.setdefault(cross, []).append(s)
    for s, (wash, cross, below) in w_inf.items():
        if not wash or cross is None:
            continue
        i, t = idx[s], cross
        if counts_only:
            out["W"].append({"d": d, "sym": s, "t": t})
            continue
        mask = np.zeros(390, dtype=bool)
        mask[15:61] = True
        mask[max(0, t - 15):t + HOLD + 2] = False
        ca = mean_ret(day, i, draw(seeded(s, d, "Wa", t), mask, t))
        pool = [x for x in cross_pool.get(t, []) if x != s]
        cb = None
        if pool:
            rr = seeded(s, d, "Wb", t)
            vals = [ret(day, idx[x], t) for x in (rr.choice(pool) for _ in range(NDRAW))]
            vals = [x for x in vals if x is not None]
            cb = sum(vals) / len(vals) if vals else None
        r = trade(day, i, s, d, t, ca=ca, cb=cb)
        if r:
            out["W"].append(r)
    # --- D
    for s, i in idx.items():
        evs = d_ev[s]
        trig = [t for t, k in evs if k >= 3]
        kept, supp = refract(trig)
        brk1 = [t for t, k in evs if k <= 1]
        bk, _ = refract(brk1)
        mask = eligible_mask(kept, supp)
        for t in kept:
            if counts_only:
                out["D"].append({"d": d, "sym": s, "t": t})
                continue
            ca = mean_ret(day, i, draw(seeded(s, d, "Da", t), mask, t))
            near = [x for x in bk if abs(x - t) <= 30 and mask[x]]
            cb = None
            if near:
                rr = seeded(s, d, "Db", t)
                cb = mean_ret(day, i, [rr.choice(near) for _ in range(NDRAW)])
            r = trade(day, i, s, d, t, ca=ca, cb=cb)
            if r:
                out["D"].append(r)
    return out


def run(counts_only=False):
    from ticker_filters import is_not_stock
    from day_hold import asset_names
    names_map = asset_names() or {}
    if sum(1 for k in names_map if k != "__cached__") < 1000:
        raise SystemExit("asset names unavailable: refusing to run without the ETF/ETN exclusion")
    excluded, files = set(), sorted(WH.glob("20??-??-??.npz"))
    hist, prev, prev_date = deque(maxlen=20), None, None
    allt = {"V1": [], "K": [], "W": [], "D": []}
    for f in files:
        d = date.fromisoformat(f.stem)
        day = load_day(f)
        if "SPY" not in day["idx"] or prev is None or prev_date != prev_weekday(d):
            prev, prev_date = day, d
            hist.append(day)
            continue
        for s in prev["syms"]:
            if s not in excluded and is_not_stock(s, names_map.get(s, "") or ""):
                excluded.add(s)
        u = universe(day, prev, excluded)
        res = score_day(day, prev, list(hist), u, f.stem, counts_only=counts_only)
        for k in allt:
            allt[k] += res[k]
        prev, prev_date = day, d
        hist.append(day)
        print(f.stem, {k: len(v) for k, v in allt.items()}, flush=True)
    return allt, len(files)


def report(allt, nfiles):
    spec = {"V1": (["ca", "cc", "cb"], "cb"), "K": (["ca", "cb", "cc"], "cb"), "W": (["ca", "cb"], "cb"), "D": (["ca", "cb"], "cb")}
    lines = ["# bar_signal_family (prereg bar_signal_family_prereg.json)", "", f"{nfiles} warehouse files. Primary t at 6 bp; 10 bp binding for net. Information cells (hold 3/30 bars) are descriptive only.", ""]
    for k, (keys, mand) in spec.items():
        r = evaluate(allt[k], keys, mand)
        lines.append(f"## {k}: {r['status']}")
        if r.get("n"):
            lines.append(f"- n {r['n']} (halves {r['n_h']}), days {r['days']} (halves {r['day_h']}), mean per-day net {r['mean']:+.2f} bp, t {r['t']:.2f} vs crit {r['crit']:.2f}, halves {r['halves'][0]:+.2f}/{r['halves'][1]:+.2f}")
            for lk, lv in r["lifts"].items():
                lines.append(f"- lift vs {lk}: n {lv['n']}, mean {lv['mean']:+.2f}, t {lv['t']:.2f}, halves {lv['h'][0]:+.2f}/{lv['h'][1]:+.2f}")
            lines.append(f"- drop best 5 days + top 5 names: {r['drop_both']:+.2f}; net at 10 bp: {r['net10']:+.2f}; conditions {r['c']}; NOT_TESTED {r['not_tested']}")
            lines.append(f"- info: hold 3 bars {r['info']['h3']:+.2f}, hold 30 bars {r['info']['h30']:+.2f} (gross of cost differences; descriptive)")
        lines.append("")
    out = "\n".join(lines)
    (ROOT / "docs" / "BAR_SIGNAL_FAMILY.md").write_text(out)
    print(out)


def main(argv=None):
    a = (argv if argv is not None else sys.argv[1:]) or ["counts"]
    if a[0] == "counts":
        allt, n = run(counts_only=True)
        for k, v in allt.items():
            days = sorted({x["d"] for x in v})
            mid = days[len(days) // 2] if days else ""
            print(k, "triggers", len(v), "days", len(days), "halves", sum(1 for x in v if x["d"] < mid), sum(1 for x in v if x["d"] >= mid))
        return 0
    allt, n = run()
    report(allt, n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
