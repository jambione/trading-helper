#!/usr/bin/env python3
"""Relative-strength scan (docs/studies/relative_strength_scan_prereg.json, amended after skeptic round 1). Read-only; uses the SIP warehouse
ai_reports/warehouse/sip/DAY.npz (arrays syms, sym_idx, minute = minutes since midnight ET, o h l c v). No new data, no subscription.

Every day: universe = top 400 by prior-session dollar volume ($10-$500, no ETFs/levered products, bars at 09:30 and 09:59, no 30% gap, no 15% move
in the first 30 minutes). RS = stock return minus SPY return from the 09:30 bar close to the 09:59 close. LEADERS = top decile. Buy at the 10:00 bar open,
sell at the 11:30 bar open, 6 bp round trip. Three replication blocks (72/24/24 sessions). The strong bar is in the prereg; nothing is tunable.

    .venv/bin/python tools/studies/relative_strength_scan.py [report]       # mini
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import sys
from collections import deque
from datetime import date, timedelta
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))
WH = ROOT / "ai_reports" / "warehouse" / "sip"
OUT = ROOT / "ai_reports" / "relative_strength"
M0, M_SIG, M_ENTRY, M_EXIT = 570, 599, 600, 690          # 09:30, 09:59, 10:00, 11:30 (minutes since midnight ET)
TOP_N, PRICE_LO, PRICE_HI, GAP_MAX, MOVE_MAX = 400, 10.0, 500.0, 0.30, 0.15
COST_BP, COST_STRESS_BP = 6.0, 10.0
BLOCKS = (72, 24, 24)


def prev_weekday(d: date) -> date:
    d = d - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def load_day(path):
    """-> dict(syms=[..], idx={sym: row}, o,h,l,c,v matrices (nsym, 390) NaN where no bar)."""
    z = np.load(path, allow_pickle=True)
    syms = [str(s) for s in z["syms"]]
    n = len(syms)
    shape = (n, 390)
    mats = {k: np.full(shape, np.nan, dtype=np.float32) for k in ("o", "h", "l", "c", "v")}
    si = z["sym_idx"].astype(np.int64)
    mi = z["minute"].astype(np.int64) - M0
    ok = (mi >= 0) & (mi < 390)
    for k in mats:
        mats[k][si[ok], mi[ok]] = z[k][ok]
    return {"syms": syms, "idx": {s: i for i, s in enumerate(syms)}, **mats}


def dollar_volume(day):
    return np.nansum(day["c"] * day["v"], axis=1)


def first_at_or_after(row_o, row_c, m_from, m_limit, last_ok=True):
    """Open of the first bar with minute in [m_from, m_limit]; (px, flag). flag 'last' when it fell back to the last available close."""
    a, b = m_from - M0, m_limit - M0
    seg = row_o[a:b + 1]
    ok = np.where(~np.isnan(seg))[0]
    if len(ok):
        return float(seg[ok[0]]), ""
    if last_ok:
        cl = row_c[a:]
        okc = np.where(~np.isnan(cl))[0]
        if len(okc):
            return float(cl[okc[-1]]), "last"
    return None, "missing"


def betas(hist, syms, spy_hist):
    """Beta of each name vs SPY from the previous sessions' 1-minute returns. hist = list of day dicts (<= 20); NaN-safe."""
    num = {s: 0.0 for s in syms}
    den = 0.0
    cnt = {s: 0 for s in syms}
    for d in hist:
        spy = d["c"][d["idx"]["SPY"]]
        rs = spy[1:] / spy[:-1] - 1
        ms = ~np.isnan(rs)
        den += float(np.nansum(rs[ms] ** 2))
        for s in syms:
            i = d["idx"].get(s)
            if i is None:
                continue
            c = d["c"][i]
            r = c[1:] / c[:-1] - 1
            m = ms & ~np.isnan(r)
            if m.sum() > 50:
                num[s] += float(np.sum(r[m] * rs[m]))
                cnt[s] += 1
    return {s: (num[s] / den if den > 0 and cnt[s] >= 5 else 1.0) for s in syms}


def score_day(day, prev, hist, excluded, day_str, entry_off=0, exit_off=0, cost_bp=COST_BP):
    """Pure given arrays. Returns None (dropped) or dict(net_leaders, net_univ, rnd, lift, beta_lift, n_leaders, names=[(sym, net)], gap_mean, exit_flags, spy_ret)."""
    spy_i = day["idx"].get("SPY")
    if spy_i is None or prev is None or "SPY" not in prev["idx"]:
        return None
    spy_c = day["c"][spy_i]
    if np.isnan(spy_c[0]) or np.isnan(spy_c[M_SIG - M0]):
        return None
    spy_ret = float(spy_c[M_SIG - M0] / spy_c[0] - 1)
    dv = dollar_volume(prev)
    order = np.argsort(-dv)
    cand = []
    for i in order:
        s = prev["syms"][i]
        if s in excluded or s == "SPY":
            continue
        j = day["idx"].get(s)
        if j is None:
            continue
        o0, c0, c59 = day["o"][j, 0], day["c"][j, 0], day["c"][j, M_SIG - M0]
        if np.isnan(o0) or np.isnan(c0) or np.isnan(c59) or not (PRICE_LO <= o0 <= PRICE_HI):
            continue
        pc = prev["c"][prev["idx"][s]]
        okp = np.where(~np.isnan(pc))[0]
        if not len(okp):
            continue
        prev_close = float(pc[okp[-1]])
        if abs(o0 / prev_close - 1) > GAP_MAX or abs(c59 / c0 - 1) > MOVE_MAX:
            continue
        cand.append((s, j, float(c59 / c0 - 1), float(o0 / prev_close - 1)))
        if len(cand) >= TOP_N:
            break
    if len(cand) < 100:
        return None
    nets, flags = {}, 0
    for s, j, ret, gap in cand:
        pe, fe = first_at_or_after(day["o"][j], day["c"][j], M_ENTRY + entry_off, M_ENTRY + entry_off + 5, last_ok=False)
        if pe is None:
            continue
        px, fx = first_at_or_after(day["o"][j], day["c"][j], M_EXIT + exit_off, M_EXIT + exit_off + 5)
        if px is None:
            continue
        flags += 1 if fx else 0
        nets[s] = (px / pe - 1) * 1e4 - cost_bp
    rows = [(s, ret - spy_ret, gap) for s, j, ret, gap in cand if s in nets]
    if len(rows) < 100:
        return None
    k = max(30, math.ceil(0.10 * len(rows)))
    rows.sort(key=lambda r: -r[1])
    leaders = [r[0] for r in rows[:k]]
    rnd = random.Random(int(hashlib.sha256((day_str + "|RS-rand").encode()).hexdigest(), 16) % (2 ** 32))
    rand = rnd.sample([r[0] for r in rows], k)
    mean = lambda ss: sum(nets[s] for s in ss) / len(ss)  # noqa: E731
    bt = betas(hist, [r[0] for r in rows], None)
    # beta-adjusted lift: (leaders' gross - beta x SPY's move over the SAME holding window) minus the same for the whole universe
    sp_o = day["o"][spy_i]
    a_, b_ = M_ENTRY + entry_off - M0, M_EXIT + exit_off - M0
    spy_hold = float(sp_o[b_] / sp_o[a_] - 1) * 1e4 if not (np.isnan(sp_o[a_]) or np.isnan(sp_o[b_])) else 0.0
    univ = mean([r[0] for r in rows])
    ln = mean(leaders)
    lead_adj = (ln + cost_bp) - (sum(bt[x] for x in leaders) / len(leaders)) * spy_hold
    univ_adj = (univ + cost_bp) - (sum(bt[r[0]] for r in rows) / len(rows)) * spy_hold
    beta_lift = lead_adj - univ_adj
    return {"net_leaders": ln, "net_univ": univ, "rnd": mean(rand), "lift": ln - univ, "beta_lift": beta_lift, "n_leaders": len(leaders),
            "names": [(s, nets[s]) for s in leaders], "gap_mean": sum(r[2] for r in rows[:k]) / k * 1e4, "exit_flags": flags, "n_univ": len(rows),
            "spy_ret_bp": spy_ret * 1e4, "laggards": mean([r[0] for r in rows[-k:]]),
            "quintile": mean([r[0] for r in rows[:max(30, math.ceil(0.2 * len(rows)))]])}


def tstat(v):
    n = len(v)
    if n < 3:
        return float("nan")
    m = sum(v) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in v) / (n - 1))
    return m / (sd / math.sqrt(n)) if sd else float("nan")


def block_bootstrap_t(v, block=5, reps=2000, seed=1106):
    n = len(v)
    if n < 2 * block:
        return float("nan")
    rng = random.Random(seed)
    starts = list(range(0, n - block + 1))
    means = []
    for _ in range(reps):
        s = []
        while len(s) < n:
            i = rng.choice(starts)
            s += v[i:i + block]
        means.append(sum(s[:n]) / n)
    m = sum(means) / reps
    sd = math.sqrt(sum((x - m) ** 2 for x in means) / (reps - 1))
    return (sum(v) / n) / sd if sd else float("nan")


def split_blocks(days):
    a, b, c = BLOCKS
    return {"DISCOVERY": days[:a], "CONFIRM": days[a:a + b], "HELD-OUT": days[a + b:a + b + c]}


def verdict(res, stress=None):
    """res: {day: score_day dict}. Applies the prereg's pass conditions. Pure."""
    days = sorted(res)
    blocks = split_blocks(days)
    blk = {}
    for name, ds in blocks.items():
        net = [res[d]["net_leaders"] for d in ds]
        lift = [res[d]["lift"] for d in ds]
        blk[name] = {"n": len(ds), "net": sum(net) / max(1, len(net)), "t": tstat(net), "lift": sum(lift) / max(1, len(lift)),
                     "mde": 3.08 * (math.sqrt(sum((x - sum(net) / max(1, len(net))) ** 2 for x in net) / max(1, len(net) - 1)) / math.sqrt(max(1, len(net)))) if len(net) > 2 else float("nan")}
    allnet = [res[d]["net_leaders"] for d in days]
    pooled_t, boot_t = tstat(allnet), block_bootstrap_t(allnet)
    lift_t = tstat([res[d]["lift"] for d in days])
    adj_t = tstat([res[d]["beta_lift"] for d in days])
    rnd_t = tstat([res[d]["net_leaders"] - res[d]["rnd"] for d in days])
    disc = blocks["DISCOVERY"]
    h = len(disc) // 2
    disc_halves = (sum(res[d]["net_leaders"] for d in disc[:h]) / max(1, h), sum(res[d]["net_leaders"] for d in disc[h:]) / max(1, len(disc) - h))
    drop_days = sorted(allnet)[:-5] if len(allnet) > 5 else allnet
    names = {}
    for d in days:
        for s, x in res[d]["names"]:
            names[s] = names.get(s, 0.0) + x
    top5 = set(sorted(names, key=lambda s: -names[s])[:5])
    kept = [x for d in days for s, x in res[d]["names"] if s not in top5]
    avg_leaders = sum(res[d]["n_leaders"] for d in days) / max(1, len(days))
    c1 = all(b["net"] > 0 for b in blk.values()) and sum(1 for b in blk.values() if b["t"] >= 1.0) >= 2
    c2 = sum(allnet) / len(allnet) > 0 and pooled_t >= 2.0 and boot_t >= 2.0
    c3 = all(b["lift"] > 0 for b in blk.values()) and lift_t >= 2.0 and adj_t >= 2.0 and rnd_t >= 1.65
    c4 = disc_halves[0] > 0 and disc_halves[1] > 0
    c5 = (sum(drop_days) / len(drop_days) > 0) and (sum(kept) / max(1, len(kept)) > 0)
    c6 = avg_leaders >= 30
    c7 = stress is not None and stress > 0
    return {"blocks": blk, "pooled_t": pooled_t, "boot_t": boot_t, "lift_t": lift_t, "adj_t": adj_t, "rnd_t": rnd_t, "disc_halves": disc_halves,
            "c": (c1, c2, c3, c4, c5, c6, c7), "pass": all((c1, c2, c3, c4, c5, c6, c7)), "avg_leaders": avg_leaders, "mean": sum(allnet) / len(allnet)}


def run():
    files = sorted(WH.glob("20??-??-??.npz"))
    excluded = set()
    try:
        from ticker_filters import is_not_stock
        from day_hold import asset_names
        names = asset_names()
        top = set()
    except Exception:  # noqa: BLE001
        is_not_stock, names, top = None, {}, set()
    dayrecs = {}
    hist = deque(maxlen=20)
    prev, prev_date = None, None
    res, res_stress, dropped = {}, {}, {"not_consecutive": 0, "bad_spy": 0, "thin": 0}
    for f in files:
        ds = f.stem
        d = date.fromisoformat(ds)
        day = load_day(f)
        spy_i = day["idx"].get("SPY")
        if spy_i is None or int(np.sum(~np.isnan(day["c"][spy_i]))) != 390:
            dropped["bad_spy"] += 1
            prev, prev_date = day, d
            hist.append(day)
            continue
        if prev is None or prev_date != prev_weekday(d):
            dropped["not_consecutive"] += 1
            prev, prev_date = day, d
            hist.append(day)
            continue
        if is_not_stock is not None:
            for s in prev["syms"][:6000]:
                if s not in top:
                    top.add(s)
                    try:
                        if is_not_stock(s, names.get(s, "") or ""):
                            excluded.add(s)
                    except Exception:  # noqa: BLE001
                        pass
        r = score_day(day, prev, list(hist), excluded, ds)
        r2 = score_day(day, prev, list(hist), excluded, ds, entry_off=1, exit_off=1, cost_bp=COST_STRESS_BP)
        if r is None or r2 is None:
            dropped["thin"] += 1
        else:
            res[ds], res_stress[ds] = r, r2
        prev, prev_date = day, d
        hist.append(day)
    return res, res_stress, dropped, len(files), len(excluded)


def report():
    res, stress, dropped, nfiles, nex = run()
    OUT.mkdir(parents=True, exist_ok=True)
    stress_mean = sum(x["net_leaders"] for x in stress.values()) / max(1, len(stress))
    v = verdict(res, stress_mean)
    lines = [f"# Relative-strength scan (prereg docs/studies/relative_strength_scan_prereg.json)", "",
             f"{nfiles} session files; scored days {len(res)}; dropped {dropped}; excluded ETF/levered symbols {nex}. Blocks 72/24/24 by date.", ""]
    for n, b in v["blocks"].items():
        lines.append(f"- {n}: {b['n']} days, mean net {b['net']:+.1f} bp (t {b['t']:+.2f}), lift vs universe {b['lift']:+.1f} bp, MDE {b['mde']:.1f} bp")
    lines += ["", f"Pooled mean net {v['mean']:+.1f} bp per leader-trade, t {v['pooled_t']:+.2f}, block-bootstrap t {v['boot_t']:+.2f}; lift t {v['lift_t']:+.2f}; beta-adjusted lift t {v['adj_t']:+.2f}; leaders minus random decile t {v['rnd_t']:+.2f}",
              f"Discovery halves net {v['disc_halves'][0]:+.1f} / {v['disc_halves'][1]:+.1f}; average leaders per day {v['avg_leaders']:.0f}; entry-lag 10:01 and 10 bp cost pooled net {stress_mean:+.1f} bp",
              f"Information: top quintile {sum(x['quintile'] for x in res.values()) / max(1, len(res)):+.1f} bp, laggards (bottom decile) {sum(x['laggards'] for x in res.values()) / max(1, len(res)):+.1f} bp, "
              f"leaders' mean gap {sum(x['gap_mean'] for x in res.values()) / max(1, len(res)):+.0f} bp", "",
              f"Conditions (1..7): {v['c']}", f"**VERDICT: {'PASS (skeptic review, then the forward re-run)' if v['pass'] else 'FAIL / not shown'}**", ""]
    out = "\n".join(lines)
    (ROOT / "docs" / "RELATIVE_STRENGTH_SCAN.md").write_text(out + "\n")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(report())
