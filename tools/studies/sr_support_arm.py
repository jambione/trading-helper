#!/usr/bin/env python3
"""Arm EARLIER on support instead of squares, plus exit families without desk hold-window limits.
Pre-registrations (committed BEFORE this script was run):
  docs/studies/sr_support_arm_prereg.json        (3255be0)  support-touch arm vs square arm, 15-min primary
  docs/studies/sr_support_arm_exits_prereg.json  (afc50c0)  exit families a/b/c/d, (b) primary; earlier ideas info

Seated names = name-days with >= 1 E square arm (indicator_levels rows.json, $10+, 9/11-10/02); 10/05 not cached.
Blocks: tools/order_blocks.py, point-in-time, charted last 3 per side, swing 10, wicks, 1-min SIP bars.
SUPPORT ARM at close i (09:40-15:30): a charted support block S known at that close with low[i] <= S.top and
  close[i] >= S.btm; only the first such bar per block per name-day (tracked from 09:40); not inside resistance;
  room to nearest charted resistance bottom above close[i] >= 0.40% (none above -> no arm); 1 per name per 15 min.
SQUARE ARM: E rows. Entry for everything: open of the bar after the decision close. Cost 0.20% round trip.
EXITS: f15 (parent primary: close of the first bar starting >= 15 min after entry, 15:55 cap); a 15:55;
  b resistance touch (PRIMARY of the addendum); c support stop (btm x 0.999) + resistance target; d60/d120/d240.
Run on the mini: .venv/bin/python tools/studies/sr_support_arm.py
"""
from __future__ import annotations

import collections
import json
import math
import os
import pickle
import random
import statistics
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))
import order_blocks as OB  # noqa: E402
from order_block_gate import series, cl_diff  # noqa: E402

ET = ZoneInfo("America/New_York")
IND = os.path.join(ROOT, "ai_reports", "indicator_levels")
OUT = os.path.join(ROOT, "ai_reports", "sr_support_arm")
COST = 0.0020
FAM = ("f15", "a", "b", "c", "d60", "d120", "d240")
INF = 10 ** 9


def et_min(ts):
    d = datetime.fromtimestamp(ts, ET)
    return d.hour * 60 + d.minute


def wr_fast(h, l, c, n=21, ema=7):
    raw = []
    for i in range(len(c)):
        a = max(0, i - n + 1)
        hh, ll = max(h[a:i + 1]), min(l[a:i + 1])
        raw.append(-100.0 * (hh - c[i]) / (hh - ll) if hh > ll else -50.0)
    out, k = [], 2 / (ema + 1)
    for v in raw:
        out.append(v if not out else out[-1] + k * (v - out[-1]))
    return out


def is_sup(b):
    return (b.kind == "bull" and not b.breaker) or (b.kind == "bear" and b.breaker)


def is_res(b):
    return (b.kind == "bear" and not b.breaker) or (b.kind == "bull" and b.breaker)


def bkey(b):
    return (b.kind, round(b.top, 4), round(b.btm, 4), b.origin_ts)


class ND:
    """One name-day: bars, %R, point-in-time charted blocks per bar."""

    def __init__(self, sym, day, rows):
        self.sym, self.day, self.rows = sym, day, rows
        self.h = [r[2] for r in rows]
        self.l = [r[3] for r in rows]
        self.c = [r[4] for r in rows]
        self.wr = wr_fast(self.h, self.l, self.c)
        self.states = [b for _, b in OB.order_blocks(rows, bar_sec=60)]
        self.closes = [r[0] + 60 for r in rows]
        d0 = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
        self.day_end = d0.replace(hour=15, minute=55).timestamp()
        self.dayidx = [i for i, r in enumerate(rows) if datetime.fromtimestamp(r[0], ET).strftime("%Y-%m-%d") == day]
        self._ch = {}

    def ch(self, i):
        if i not in self._ch:
            tq = self.closes[i]
            self._ch[i] = OB.charted([b for b in self.states[i] if b.known_ts <= tq])
        return self._ch[i]

    def window(self, lo, hi):
        return [i for i in self.dayidx if lo <= et_min(self.closes[i]) <= hi and i + 1 < len(self.rows)]

    def idx_of_close(self, t):
        j = int(np.searchsorted(self.closes, t, side="right")) - 1
        return j

    def exits(self, i_dec, stop_btm=None):
        """All exit families for an entry at the open of bar i_dec+1. Returns dict fam -> gross, or None."""
        rows, ie = self.rows, i_dec + 1
        if ie >= len(rows) or rows[ie][0] >= self.day_end:
            return None
        entry, t0 = rows[ie][1], rows[ie][0]
        ch = self.ch(i_dec)
        above = [b.btm for b in ch if is_res(b) and b.btm > entry]
        tgt = min(above) if above else None
        if stop_btm is None:
            sb = [b for b in ch if is_sup(b) and b.btm < entry]
            stop_btm = max(sb, key=lambda b: b.top).btm if sb else None
        stop = stop_btm * (1 - 0.001) if stop_btm is not None else None
        out = {}
        last = None                                  # index of the last bar before 15:55 start
        for j in range(ie, len(rows)):
            if rows[j][0] >= self.day_end:
                break
            last = j
        if last is None:
            return None
        eod = rows[last][4] / entry - 1
        out["a"] = eod
        for fam, H in (("f15", 15), ("d60", 60), ("d120", 120), ("d240", 240)):
            g = eod
            for j in range(ie, last + 1):
                if rows[j][0] - t0 >= H * 60:
                    g = rows[j][4] / entry - 1
                    break
            out[fam] = g
        gb = gc = None
        cdone = False
        for j in range(ie, last + 1):
            o, hi, lo = rows[j][1], rows[j][2], rows[j][3]
            if gb is None and tgt is not None and hi >= tgt:
                gb = max(tgt, o) / entry - 1
            if not cdone:
                if stop is not None and lo <= stop:
                    gc, cdone = min(stop, o) / entry - 1, True
                elif tgt is not None and hi >= tgt:
                    gc, cdone = max(tgt, o) / entry - 1, True
            if gb is not None and cdone:
                break
        out["b"] = eod if gb is None else gb
        out["c"] = eod if not cdone else gc
        return out


def tstat(xs, dd):
    if len(xs) < 3:
        return float("nan"), float("nan")
    m = statistics.fmean(xs)
    by = collections.defaultdict(float)
    for v, d in zip(xs, dd):
        by[d] += v - m
    G, n = len(by), len(xs)
    if G < 2:
        return m, float("nan")
    se = math.sqrt(sum(s * s for s in by.values()) * G / (G - 1)) / n
    return m, (m / se if se > 0 else float("nan"))


def diff_t(ys, gs, dd):
    """support (g=1) minus square (g=0) mean, day-clustered CR1 t; (nan, nan) if degenerate."""
    if sum(gs) < 3 or sum(1 - g for g in gs) < 3 or len(set(dd)) < 3:
        return float("nan"), float("nan")
    b, se = cl_diff(ys, gs, dd)
    return b, (b / se if se and se > 0 else float("nan"))


def support_arms(nd, room_min=0.40, need_wr=False):
    used, last, arms, drops = set(), -INF, [], collections.Counter()
    for i in nd.window(9 * 60 + 40, 15 * 60 + 30):
        ch = nd.ch(i)
        touch = [b for b in ch if is_sup(b) and nd.l[i] <= b.top and nd.c[i] >= b.btm]
        new = [b for b in touch if bkey(b) not in used]
        for b in touch:
            used.add(bkey(b))
        if not new:
            continue
        S = max(new, key=lambda b: b.top)
        res = [b for b in ch if is_res(b)]
        if any(b.btm <= nd.c[i] <= b.top for b in res):
            drops["inside_resistance"] += 1
            continue
        ab = [b.btm for b in res if b.btm > nd.c[i]]
        if not ab:
            drops["no_resistance_above"] += 1
            continue
        room = (min(ab) / nd.c[i] - 1) * 100
        if room < room_min:
            drops["room_below_min"] += 1
            continue
        if need_wr and not nd.wr[i] <= -50:
            drops["wr_not_low"] += 1
            continue
        m = et_min(nd.closes[i])
        if m - last < 15:
            drops["refractory_15min"] += 1
            continue
        g = nd.exits(i, stop_btm=S.btm)
        if g is None:
            drops["no_outcome"] += 1
            continue
        last = m
        arms.append({"i": i, "t": nd.closes[i], "room": room, "g": g})
    return arms, drops


def random_ctrl(nd, i, rng, pool_fn=None):
    hr = et_min(nd.closes[i]) // 60
    win = nd.window(9 * 60 + 40, 15 * 60 + 30)
    same = [k for k in win if k != i and et_min(nd.closes[k]) // 60 == hr]
    pool = [k for k in same if pool_fn(k)] if pool_fn else same
    fb = False
    if not pool:
        pool, fb = same, True
    if not pool:
        return None, fb
    k = rng.choice(pool)
    return nd.exits(k), fb


def main():
    os.makedirs(OUT, exist_ok=True)
    R = json.load(open(os.path.join(IND, "rows.json")))
    cache = pickle.load(open(os.path.join(IND, "ext.pkl"), "rb"))
    cal = sorted({d for (_, d) in cache})
    prev_of = {d: cal[cal.index(d) - 1] if cal.index(d) else None for d in cal}
    E = [r for r in R["rows"] if r["pop"] == "E"]
    e_nd = collections.defaultdict(list)
    for r in E:
        e_nd[(r["sym"], r["day"])].append(r)
    sdays = sorted({d for (_, d) in e_nd})
    halfS = {d: ("A" if k < len(sdays) // 2 else "B") for k, d in enumerate(sdays)}
    c_nd = sorted({(r["sym"], r["day"]) for r in R["rows"] if r["pop"] == "C"})
    alld = list(R["days"])
    halfC = {d: ("A" if k < len(alld) // 2 else "B") for k, d in enumerate(alld)}

    need = sorted(set(e_nd) | set(c_nd))
    NDs, short = {}, 0
    for sym, day in need:
        rows = series(cache, sym, day, prev_of.get(day))
        if len(rows) < 100:
            short += 1
            continue
        NDs[(sym, day)] = ND(sym, day, rows)

    # ---------------- support arm vs square arm (seated name-days) ----------------
    rng37, rng41 = random.Random(37), random.Random(41)
    sup, sq, sup_ctrl, sq_ctrl = [], [], [], []
    drops = collections.Counter()
    first_gap, since_sup = [], []
    info_variants = {"room>=0.25%": (0.25, False), "room>=0.60%": (0.60, False), "room>=0.40% + %R<=-50": (0.40, True)}
    info_rows = {k: [] for k in info_variants}
    for key in sorted(e_nd):
        nd = NDs.get(key)
        if nd is None:
            drops["short_series"] += len(e_nd[key])
            continue
        arms, dr = support_arms(nd)
        drops.update(dr)
        for a in arms:
            rec = {"sym": key[0], "day": key[1], "t": a["t"], "room": a["room"], **{f: a["g"][f] for f in FAM}}
            sup.append(rec)
            cg, fb = random_ctrl(nd, a["i"], rng37)
            if fb:
                drops["ctrl_fallback"] += 1
            if cg is not None:
                sup_ctrl.append({"day": key[1], "pair": len(sup) - 1, **{f: cg[f] for f in FAM}})
        sqt = []
        for r in e_nd[key]:
            i = nd.idx_of_close(float(r["t"]))
            if i < 0 or abs(nd.closes[i] - float(r["t"])) > 1:
                drops["square_bar_mismatch"] += 1
                continue
            g = nd.exits(i)
            if g is None:
                drops["square_no_outcome"] += 1
                continue
            sq.append({"sym": key[0], "day": key[1], "t": float(r["t"]), **{f: g[f] for f in FAM}})
            sqt.append(float(r["t"]))
            cg, _ = random_ctrl(nd, i, rng41)
            if cg is not None:
                sq_ctrl.append({"day": key[1], "pair": len(sq) - 1, **{f: cg[f] for f in FAM}})
        st = [a["t"] for a in arms]
        if st and sqt:
            first_gap.append((min(sqt) - min(st)) / 60)
        for t in sqt:
            prior = [s for s in st if s < t]
            since_sup.append((t - max(prior)) / 60 if prior else None)
        for lab, (rm, w) in info_variants.items():
            arms2, _ = support_arms(nd, rm, w)
            info_rows[lab] += [{"day": key[1], "sym": key[0], **{f: a["g"][f] for f in FAM}} for a in arms2]

    matched = {(r["sym"], r["day"]) for r in sup} & {(r["sym"], r["day"]) for r in sq}
    L = ["# Support-touch arm vs square arm, with exit families (preregs 3255be0 + afc50c0)", "",
         f"seated name-days {len(e_nd)} on {len(sdays)} days ({sdays[0]}..{sdays[-1]}); 10/05 not cached (excluded); "
         f"chrono A {sdays[0]}..{sdays[len(sdays)//2-1]}, B {sdays[len(sdays)//2]}..{sdays[-1]}",
         f"support arms {len(sup)} on {len({(r['sym'], r['day']) for r in sup})} name-days; square arms {len(sq)}; "
         f"matched name-days {len(matched)}; drops {dict(drops)}; short series {short}", ""]

    def bp(x):
        return x * 1e4

    verdicts, table = {}, []
    for fam in FAM:
        for hh in ("A", "B", "all"):
            inh = lambda d: hh == "all" or halfS[d] == hh  # noqa: E731
            S = [r for r in sup if inh(r["day"]) and (r["sym"], r["day"]) in matched]
            Q = [r for r in sq if inh(r["day"]) and (r["sym"], r["day"]) in matched]
            SA = [r for r in sup if inh(r["day"])]
            SC = [c for c in sup_ctrl if inh(c["day"])]
            QC = [c for c in sq_ctrl if inh(c["day"])]
            if len(S) < 5 or len(Q) < 5:
                table.append(f"| {fam} | {hh} | {len(S)} | — | — | {len(Q)} | — | — | — | — |")
                verdicts[(fam, hh)] = None
                continue
            sg = statistics.fmean(bp(r[fam]) for r in S)
            qg = statistics.fmean(bp(r[fam]) for r in Q)
            rt = bp(COST)
            _, snt = tstat([bp(r[fam] - COST) for r in S], [r["day"] for r in S])
            d, dt = diff_t([bp(r[fam]) for r in S + Q], [1] * len(S) + [0] * len(Q), [r["day"] for r in S + Q])
            rc, rct = tstat([bp(sup[c["pair"]][fam] - c[fam]) for c in SC], [c["day"] for c in SC])
            qcm = statistics.fmean(bp(c[fam]) for c in QC) if QC else float("nan")
            sam = statistics.fmean(bp(r[fam]) for r in SA)
            table.append(f"| {fam} | {hh} | {len(S)} | {sg:+.1f} | {sg - rt:+.1f} (t {snt:+.2f}) | {len(Q)} | {qg:+.1f} | "
                         f"{qg - rt:+.1f} | **{d:+.1f}** (t {dt:+.2f}) | {rc:+.1f} (t {rct:+.2f}, n {len(SC)}) | "
                         f"all-support {sam:+.1f} (n {len(SA)}); square's random ctrl {qcm:+.1f} |")
            verdicts[(fam, hh)] = (len(S), d, dt, rt, sg - rt, qg - rt)
    L += ["Gross/net in bp per trade. Matched name-days only for support vs square. RT = measured round trip (20 bp).", "",
          "| exit | half | support n | support gross | support net (t) | square n | square gross | square net | "
          "support − square gross (t) | support − same-hour random (t) | other |",
          "|---|---|---|---|---|---|---|---|---|---|---|"] + table + [""]

    def verdict(fam):
        v = [verdicts.get((fam, h)) for h in ("A", "B")]
        if any(x is None or x[0] < 50 for x in v):
            return "UNDERPOWERED (fewer than 50 support arms in a half): no verdict"
        if all(x[1] > x[3] for x in v):
            return "PASS — PENDING SKEPTIC REVIEW (not a live arm; SIP != IEX; config untouched)"
        return "FAIL"
    v15, vb = verdict("f15"), verdict("b")

    # info: room / %R variants, exits f15 and b (unmatched vs all squares in the same half)
    L.append("## Info: support-arm variants (all support arms in the variant vs all square arms; exits f15 and b)")
    for lab, rows in info_rows.items():
        for fam in ("f15", "b"):
            parts = []
            for hh in ("A", "B"):
                S = [r for r in rows if halfS[r["day"]] == hh]
                Q = [r for r in sq if halfS[r["day"]] == hh]
                if len(S) < 5:
                    parts.append(f"{hh}: n {len(S)} (too few)")
                    continue
                d, dt = diff_t([bp(r[fam]) for r in S + Q], [1] * len(S) + [0] * len(Q), [r["day"] for r in S + Q])
                parts.append(f"{hh}: n {len(S)} gross {statistics.fmean(bp(r[fam]) for r in S):+.1f} "
                             f"net {statistics.fmean(bp(r[fam] - COST) for r in S):+.1f} | vs square {d:+.1f} (t {dt:+.2f})")
            L.append(f"- {lab}, exit {fam}: " + " || ".join(parts))
    L.append("")

    # minutes earlier
    fg = sorted(first_gap)
    ss = [x for x in since_sup if x is not None]
    L.append("## Minutes earlier (support arm vs square arm)")
    if fg:
        L.append(f"- matched name-days {len(fg)}: first square − first support = median {statistics.median(fg):+.0f} min, "
                 f"mean {statistics.fmean(fg):+.0f} min; support first on {sum(x > 0 for x in fg) / len(fg):.0%}, "
                 f"square first on {sum(x < 0 for x in fg) / len(fg):.0%}, same minute {sum(x == 0 for x in fg) / len(fg):.0%}")
        L.append(f"- per square arm: {len(ss)}/{len(since_sup)} ({len(ss) / max(1, len(since_sup)):.0%}) had an earlier support arm the same "
                 f"name-day; minutes since it: median {statistics.median(ss):.0f}, IQR {np.percentile(ss, 25):.0f}–{np.percentile(ss, 75):.0f}"
                 if ss else "- no square arm had an earlier support arm")
    L.append("")

    # ---------------- earlier ideas, info only, exits a and b ----------------
    L.append("## Info: earlier S/R ideas rerun with exits (a) 15:55 and (b) resistance touch (own samples, chronological 10/10 halves)")
    gap, gap_c, rng_, rng_c = [], [], [], []
    r31, r29 = random.Random(31), random.Random(29)
    for key in c_nd:
        nd = NDs.get(key)
        if nd is None:
            continue
        # gap arm (sr_gap_wr60_arm primary)
        last = -INF
        for i in nd.window(9 * 60 + 40, 15 * 60 + 30):
            m = et_min(nd.closes[i])
            if m - last < 15 or not nd.wr[i] <= -60:
                continue
            ch = nd.ch(i)
            res = [b for b in ch if is_res(b)]
            if any(b.btm <= nd.c[i] <= b.top for b in res):
                continue
            near = [b for b in ch if is_sup(b) and (b.btm <= nd.c[i] <= b.top or
                                                    (nd.c[i] > b.top and (nd.c[i] / b.top - 1) * 100 <= 0.30))]
            if not near:
                continue
            ab = [b.btm for b in res if b.btm > nd.c[i]]
            if not ab or (min(ab) / nd.c[i] - 1) * 100 < 0.40:
                continue
            g = nd.exits(i)
            if g is None:
                continue
            last = m
            gap.append({"day": key[1], "a": g["a"], "b": g["b"]})
            cg, _ = random_ctrl(nd, i, r31, lambda k, nd=nd: nd.wr[k] <= -60)
            if cg is not None:
                gap_c.append({"day": key[1], "pair": len(gap) - 1, "a": cg["a"], "b": cg["b"]})
        # range trade S->R (sr_range_trade primary)
        used = set()
        for i in nd.window(9 * 60 + 40, 15 * 60):
            ch = nd.ch(i)
            cand = [b for b in ch if is_sup(b) and nd.c[i - 1] <= b.top < nd.c[i]]
            if not cand:
                continue
            S = max(cand, key=lambda b: b.top)
            if bkey(S) in used:
                continue
            used.add(bkey(S))
            res = [b for b in ch if is_res(b)]
            if any(b.btm <= nd.c[i] <= b.top for b in res):
                continue
            ab = [b.btm for b in res if b.btm > nd.c[i]]
            if not ab or (min(ab) / nd.c[i] - 1) * 100 < 0.40:
                continue
            if not (nd.wr[i] < -20 and nd.wr[i] > nd.wr[i - 1]):
                continue
            g = nd.exits(i)
            if g is None:
                continue
            rng_.append({"day": key[1], "a": g["a"], "b": g["b"]})
            cg, _ = random_ctrl(nd, i, r29, lambda k, nd=nd: nd.c[k] > nd.c[k - 1] and nd.wr[k] < -20 and nd.wr[k] > nd.wr[k - 1])
            if cg is not None:
                rng_c.append({"day": key[1], "pair": len(rng_) - 1, "a": cg["a"], "b": cg["b"]})
    L += ["| idea | exit | half | n | gross | net (t) | control gross | signal − control (t) |", "|---|---|---|---|---|---|---|---|"]
    for lab, X, C in (("gap arm (−60 %R near support)", gap, gap_c), ("range trade S→R", rng_, rng_c)):
        for fam in ("a", "b"):
            for hh in ("A", "B"):
                xs = [r for r in X if halfC[r["day"]] == hh]
                cs = [c for c in C if halfC[c["day"]] == hh]
                if len(xs) < 5:
                    L.append(f"| {lab} | {fam} | {hh} | {len(xs)} | — | — | — | — |")
                    continue
                _, nt = tstat([bp(r[fam] - COST) for r in xs], [r["day"] for r in xs])
                lm, lt = tstat([bp(X[c["pair"]][fam] - c[fam]) for c in cs], [c["day"] for c in cs])
                L.append(f"| {lab} | {fam} | {hh} | {len(xs)} | {statistics.fmean(bp(r[fam]) for r in xs):+.1f} | "
                         f"{statistics.fmean(bp(r[fam] - COST) for r in xs):+.1f} (t {nt:+.2f}) | "
                         f"{statistics.fmean(bp(c[fam]) for c in cs):+.1f} | {lm:+.1f} (t {lt:+.2f}) |")
    L.append("")

    for tag, fam, v in (("PARENT PRIMARY (3255be0, 15-min hold)", "f15", v15), ("ADDENDUM PRIMARY (afc50c0, exit b)", "b", vb)):
        L.append(f"**{tag}: {v}** (bar: support − square gross on matched name-days > measured RT 20 bp in BOTH "
                 f"chronological halves, >= 50 support arms per half)")
        for h in ("A", "B"):
            x = verdicts.get((fam, h))
            if x:
                L.append(f"- half {h}: n support {x[0]}, support − square {x[1]:+.1f} bp (t {x[2]:+.2f}) vs RT {x[3]:.1f}; "
                         f"net support {x[4]:+.1f} / square {x[5]:+.1f} bp")
    json.dump({"support": sup, "square": sq, "support_ctrl": sup_ctrl, "square_ctrl": sq_ctrl,
               "first_gap_min": first_gap, "drops": dict(drops), "sample_days": sdays},
              open(os.path.join(OUT, "rows.json"), "w"))
    open(os.path.join(OUT, "report.md"), "w").write("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
