#!/usr/bin/env python3
"""The operator's %R trend trade as one rule. Spec: docs/studies/wr_trend_trade_prereg.json - implemented from its
`amended` block ((1a) first qualifying minute, exit scan from t+1; (1b) re-entry after a new oversold event at or after
the exit fill), which supersedes the original rule/data text. The "missing t+1 bar" is the SIP bar (prices are SIP).

Lines (desk definitions, signals._minute_grid_pr): FAST = %R(21) EMA(7), SLOW = %R(112) EMA(3) on a minute grid
(gaps <= 15 min filled flat at the previous close; the overnight gap is not), warmed up on the previous warehouse
session. PRIMARY lines on warehouse IEX bars; all prices SIP.

Entry at t+1 open for the first t in 09:45 .. 15:00 with: an oversold event (both lines <= -80) in t-20 .. t (and at
or after the last exit fill), fast(t) > -80, fast(t) - fast(t-5) >= 5 and slow(t) - slow(t-5) > 0. Exit at u+1 open for
the first u >= t+1 with fast(u) < fast(u-3), else the last close <= 15:59. Controls: d = same name, seeded minute in
(entry, entry + 60], same hold; x = seeded other name, same minutes.

  (default) count step (outcome-blind: MDE from control - control2, hold distribution)
  --score   verdict; refuses without --skeptic-ready, --prereg-commit in count.json, the floors, or from other code
  --lines sip  information run with SIP-computed lines
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import statistics
import sys
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eod_reversal as E  # noqa: E402

ROOT = E.ROOT
FIRST, LAST, HALF_A_END = "2026-05-11", "2026-10-15", "2026-07-29"
FAST_LEN, FAST_SPAN, SLOW_LEN, SLOW_SPAN, GAP_FILL = 21, 7, 112, 3, 15
OS, OB, CONF = -80.0, -20.0, 15.0
OS_WINDOW, RISE_LAG, FAST_RISE, TURN_LAG = 20, 5, 5.0, 3
DEC_LO, DEC_HI, M_LAST, CTRL_SPAN = 585, 900, 959, 60
COST, COSTS, LIFT_BAR, T_PASS, T_INFO = 0.0005, (0.0003, 0.0005, 0.0010, 0.0020), 0.0005, 2.0, 2.73
FLOOR_TRADES, FLOOR_DAYS, SPLIT_JUMP = 300, 20, 0.30


# ── lines ────────────────────────────────────────────────────────────────────

def ema(x: np.ndarray, span: int) -> np.ndarray:
    """The desk's smoothing, literally: pandas ewm(span, adjust=False).mean() (signals._minute_grid_pr)."""
    import pandas as pd
    return pd.Series(x).ewm(span=max(1, int(span)), adjust=False).mean().to_numpy()


def pct_r(h: np.ndarray, lo: np.ndarray, c: np.ndarray, n: int) -> np.ndarray:
    """signals.williams_pr: -100 x (HH - C) / (HH - LL), NaN where the range is zero."""
    import pandas as pd
    hh = pd.Series(h).rolling(n).max()
    ll = pd.Series(lo).rolling(n).min()
    return (-100 * (hh - pd.Series(c)) / (hh - ll).replace(0, np.nan)).to_numpy()


def grid(rows_prev: np.ndarray, rows_today: np.ndarray) -> tuple[list, np.ndarray, np.ndarray, np.ndarray]:
    """Absolute minute keys (prev session = minute, today = 10000 + minute) with in-session gaps <= 15 filled flat."""
    keys, H, L, C = [], [], [], []
    for off, rows in ((0, rows_prev), (10000, rows_today)):
        prev_m = None
        for r in rows:
            m = int(r[0])
            if prev_m is not None and 1 < m - prev_m <= GAP_FILL:
                for g in range(prev_m + 1, m):
                    keys.append(off + g)
                    H.append(C[-1]); L.append(C[-1]); C.append(C[-1])
            keys.append(off + m)
            H.append(float(r[2])); L.append(float(r[3])); C.append(float(r[4]))
            prev_m = m
    return keys, np.array(H), np.array(L), np.array(C)


def lines(rows_prev: np.ndarray, rows_today: np.ndarray) -> tuple[dict, dict]:
    """{today minute: fast}, {today minute: slow} on the grid."""
    keys, H, L, C = grid(rows_prev, rows_today)
    f = ema(pct_r(H, L, C, FAST_LEN), FAST_SPAN)
    s = ema(pct_r(H, L, C, SLOW_LEN), SLOW_SPAN)
    fast, slow = {}, {}
    for k, a, b in zip(keys, f, s):
        if k >= 10000 and not np.isnan(a) and not np.isnan(b):
            fast[k - 10000], slow[k - 10000] = float(a), float(b)
    return fast, slow


# ── the rule ─────────────────────────────────────────────────────────────────

def rising(fast: dict, slow: dict, t: int, fast_only: bool = False) -> bool:
    if t - RISE_LAG not in fast or t not in fast:
        return False
    ok = fast[t] - fast[t - RISE_LAG] >= FAST_RISE
    return ok if fast_only else ok and t - RISE_LAG in slow and slow[t] - slow[t - RISE_LAG] > 0


def is_oversold(fast: dict, slow: dict, m: int) -> bool:
    return m in fast and fast[m] <= OS and slow[m] <= OS


def is_square(fast: dict, slow: dict, m: int) -> bool:
    return m in fast and fast[m] >= OB and slow[m] >= OB and abs(fast[m] - slow[m]) <= CONF


def exit_minute(fast: dict, slow: dict, start: int, mode: str = "any") -> int | None:
    """First u >= start meeting the exit rule ('any' downturn | 'top' downturn from the top zone or back to the
    bottom | 'min5' any downturn from start + 4 on)."""
    lo = start + 4 if mode == "min5" else start
    for u in range(lo, M_LAST + 1):
        if u not in fast or u - TURN_LAG not in fast:
            continue
        down = fast[u] < fast[u - TURN_LAG]
        if mode == "top":
            if (down and fast[u] >= OB) or fast[u] <= OS:
                return u
        elif down:
            return u
    return None


def entries(fast: dict, slow: dict, variant: str = "primary") -> list[int]:
    """Signal minutes t in order, honouring one-open-trade and the re-entry rule (new oversold event after the
    previous exit fill); the exit used for that bookkeeping is the primary 'any' exit."""
    out, free_from, last_os = [], 0, None
    for t in range(DEC_LO - OS_WINDOW, DEC_HI + 1):
        if is_oversold(fast, slow, t) and t >= free_from:
            last_os = t
        if t < DEC_LO or t < free_from:
            continue
        if variant == "square":
            ok = is_square(fast, slow, t) and t - RISE_LAG in fast and fast[t] >= fast[t - RISE_LAG]
        else:
            ok = (last_os is not None and t - OS_WINDOW <= last_os <= t and t in fast and fast[t] > OS
                  and rising(fast, slow, t, fast_only=(variant == "fast_only")))
        if not ok:
            continue
        out.append(t)
        u = exit_minute(fast, slow, t + 1)
        free_from = (u + 1) if u is not None else 10 ** 6
        last_os = None
    return out


# ── prices ───────────────────────────────────────────────────────────────────

def px_open(sip: dict, m: int):
    v = sip.get(m)
    return v[0] if v else None


def px_close_at_or_before(sip: dict, m: int, lo: int):
    for k in range(min(m, M_LAST), lo - 1, -1):
        if k in sip:
            return sip[k][1]
    return None


def exit_px(sip: dict, x_min: int, e: int):
    """The trade's exit convention, shared by trades and controls: the open of x_min when it has a bar (the u+1
    fill), else the last close at or before it (C2); at 15:59 or later, the last close <= 15:59."""
    if x_min > M_LAST:
        return px_close_at_or_before(sip, M_LAST, e)
    return px_open(sip, x_min) or px_close_at_or_before(sip, x_min, e)


def trade(sip: dict, t: int, u: int | None) -> dict | None:
    e = t + 1
    o = px_open(sip, e)
    if o is None:
        return None
    if u is None or u + 1 > M_LAST:
        x_min, x = M_LAST, px_close_at_or_before(sip, M_LAST, e)
    else:
        x_min = u + 1
        x = exit_px(sip, x_min, e)
    return {"entry_min": e, "exit_min": x_min, "gross": x / o - 1, "hold": x_min - e}


def ret_over(sip: dict, e: int, x_min: int) -> float | None:
    """Open of e to the trade's exit convention at x_min (controls exit exactly as trades do)."""
    o = px_open(sip, e)
    if o is None:
        return None
    x = exit_px(sip, x_min, e)
    return x / o - 1 if x else None


def seeded(*parts) -> random.Random:
    return random.Random(int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:16], 16))


def d_control(sip: dict, sym: str, day: str, e: int, hold: int, tag: str) -> float | None:
    cand = [c for c in range(e + 1, min(e + CTRL_SPAN, M_LAST) + 1)]
    rng = seeded(sym, day, e, tag)
    for _ in range(10):
        if not cand:
            return None
        c = rng.choice(cand)
        if c in sip:
            return ret_over(sip, c, c + hold)
    return None


def x_control(sips: dict, pool: list, sym: str, day: str, e: int, x_min: int, tag: str) -> float | None:
    rng = seeded(sym, day, e, tag)
    for _ in range(10):
        n = pool[rng.randrange(len(pool))]
        if n != sym and e in sips[n]:
            return ret_over(sips[n], e, x_min)
    return None


# ── per day ──────────────────────────────────────────────────────────────────

def day_task(args) -> dict:
    day, prev_day, line_feed = args
    import indicator_confirm_hist as H
    sip_raw = H.load_raw("sip", day)
    lf_today = H.load_raw(line_feed, day)
    lf_prev = H.load_raw(line_feed, prev_day)
    sip_prev = H.load_raw("sip", prev_day)
    sips = {s: {int(r[0]): (float(r[1]), float(r[4])) for r in rows} for s, rows in sip_raw.items()}
    pool = sorted(sips)
    st, rows = Counter(), []
    for sym in pool:
        if sym not in lf_today or sym not in lf_prev:
            st["no_line_bars_or_prev"] += 1
            continue
        if sym in sip_prev and len(sip_prev[sym]) and len(sip_raw[sym]):
            pc, op = float(sip_prev[sym][-1][4]), float(sip_raw[sym][0][1])
            if pc > 0 and abs(op / pc - 1) > SPLIT_JUMP:
                st["split_jump_skip"] += 1
                continue
        fast, slow = lines(lf_prev[sym], lf_today[sym])
        if not any(DEC_LO <= m <= DEC_HI for m in fast):
            st["no_warmup_lines"] += 1                     # < 112 grid bars before the window (thin IEX names)
            continue
        st["names_with_lines"] += 1
        sip = sips[sym]
        for variant in ("primary", "fast_only", "square"):
            for t in entries(fast, slow, variant):
                u = exit_minute(fast, slow, t + 1)
                tr = trade(sip, t, u)
                if tr is None:
                    st[f"{variant}_skip_no_entry_bar"] += 1
                    continue
                e, hold = tr["entry_min"], tr["hold"]
                row = {"day": day, "sym": sym, "variant": variant, "dec_min": t, **tr,
                       "d": d_control(sip, sym, day, e, hold, "wrtrend-d"),
                       "d2": d_control(sip, sym, day, e, hold, "wrtrend-d2"),
                       "x": x_control(sips, pool, sym, day, e, tr["exit_min"], "wrtrend-x"),
                       "x2": x_control(sips, pool, sym, day, e, tr["exit_min"], "wrtrend-x2"),
                       "os_tri": None}
                if variant == "primary":
                    for mode in ("top", "min5"):
                        tv = trade(sip, t, exit_minute(fast, slow, t + 1, mode))
                        row[f"gross_{mode}"] = tv["gross"] if tv else None
                    row["gross_t15"] = ret_over(sip, e, e + 14)
                    rng = seeded(sym, day, e, "wrtrend-d")
                    cand = [c for c in range(e + 1, min(e + CTRL_SPAN, M_LAST) + 1) if c in sip]
                    if cand:
                        c = rng.choice(cand)
                        uc = exit_minute(fast, slow, c)
                        tc = trade(sip, c - 1, uc)
                        row["d_rule_exit"] = tc["gross"] if tc else None
                    lo_os = [m for m in range(t - OS_WINDOW, t + 1) if is_oversold(fast, slow, m)]
                    row["os_tri"] = any(abs(fast[m] - slow[m]) <= CONF for m in lo_os)
                    row["px"] = px_open(sip, e)
                rows.append(row)
    return {"rows": rows, "stats": dict(st)}


# ── statistics ───────────────────────────────────────────────────────────────

def per_day(rows: list[dict], key) -> dict:
    per = defaultdict(list)
    for r in rows:
        v = key(r)
        if v is not None:
            per[r["day"]].append(v)
    return {d: statistics.mean(v) for d, v in per.items()}


def dstat(rows: list[dict], key) -> dict:
    return E.stats(list(per_day(rows, key).values()))


def lift_d(r):
    return r["gross"] - r["d"] if r.get("d") is not None else None


def lift_x(r):
    return r["gross"] - r["x"] if r.get("x") is not None else None


def drop_days(rows, key, k=3):
    m = per_day(rows, key)
    top = set(sorted(m, key=lambda d: m[d], reverse=True)[:k])
    return dstat([r for r in rows if r["day"] not in top], key)["mean"]


def drop_names(rows, key, k=5):
    tot = Counter()
    for r in rows:
        v = key(r)
        if v is not None:
            tot[r["sym"]] += v
    top = {s for s, _ in tot.most_common(k)}
    return dstat([r for r in rows if r["sym"] not in top], key)["mean"]


def mde(rows, a, b) -> float | None:
    s = list(per_day(rows, lambda r: (r[a] - r[b]) if r.get(a) is not None and r.get(b) is not None else None)
             .values())
    return (2.0 + 0.84) * statistics.stdev(s) / math.sqrt(len(s)) if len(s) >= 2 else None


def module_shas() -> dict:
    d = os.path.dirname(os.path.abspath(__file__))
    return {m: hashlib.sha256(open(os.path.join(d, f"{m}.py"), "rb").read()).hexdigest()
            for m in ("wr_trend_trade", "eod_reversal")}


def write(out_dir, name, obj):
    os.makedirs(out_dir, exist_ok=True)
    p = os.path.join(out_dir, name)
    json.dump(obj, open(p + ".tmp", "w"), indent=1, default=str)
    os.replace(p + ".tmp", p)
    return p


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--score", action="store_true")
    ap.add_argument("--skeptic-ready", default="")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--prereg-commit", default="")
    ap.add_argument("--lines", default="iex", choices=("iex", "sip"))
    a = ap.parse_args()
    out_dir = os.path.join(ROOT, "ai_reports", "wr_trend_trade" + ("" if a.lines == "iex" else "_sip_lines"))
    import indicator_confirm_hist as H
    days = H.warehouse_days()
    run = [(d, days[i - 1], a.lines) for i, d in enumerate(days) if FIRST <= d <= LAST and i >= 1]
    cache = os.path.join(out_dir, "rows.json")
    if os.path.exists(cache) and json.load(open(cache)).get("module_sha256") == module_shas():
        c = json.load(open(cache))
        rows, st = c["rows"], Counter(c["stats"])
    else:
        rows, st = [], Counter()
        with ProcessPoolExecutor(max_workers=a.workers) as ex:
            for res in ex.map(day_task, run, chunksize=2):
                rows += res["rows"]
                st.update(res["stats"])
        write(out_dir, "rows.json", {"rows": rows, "stats": dict(st), "module_sha256": module_shas()})
    half = lambda r: "A" if r["day"] <= HALF_A_END else "B"  # noqa: E731
    prim = [r for r in rows if r["variant"] == "primary"]
    if not a.score:
        cnt = {"stats": dict(st), "module_sha256": module_shas(), "prereg_commit": a.prereg_commit, "lines": a.lines}
        for h in ("A", "B"):
            hr = [r for r in prim if half(r) == h]
            holds = sorted(r["hold"] for r in hr)
            md, mx = mde(hr, "d", "d2"), mde(hr, "x", "x2")
            days_n = len({r["day"] for r in hr})
            cnt[h] = {"trades": len(hr), "days": days_n, "mde_d": md, "mde_x": mx,
                      "hold_median": holds[len(holds) // 2] if holds else None,
                      "hold_share_le3": sum(1 for x in holds if x <= 3) / len(holds) if holds else None,
                      "floors_met": len(hr) >= FLOOR_TRADES and days_n >= FLOOR_DAYS and md is not None
                      and md <= LIFT_BAR and mx is not None and mx <= LIFT_BAR}
        print(json.dumps(cnt, indent=1, default=str))
        print(write(out_dir, "count.json", cnt))
        return
    if not a.skeptic_ready:
        raise SystemExit("--score needs --skeptic-ready")
    cnt = json.load(open(os.path.join(out_dir, "count.json")))
    if cnt.get("module_sha256") != module_shas() or not cnt.get("prereg_commit"):
        raise SystemExit("count.json from different code or without --prereg-commit")
    if not all(cnt[h]["floors_met"] for h in ("A", "B")):
        raise SystemExit("UNDERPOWERED at the count step: no outcome is read")
    res, hv, ubs = {"halves": {}, "info": {}}, [], []
    for h in ("A", "B"):
        hr = [r for r in prim if half(r) == h]
        ld, lx, net = dstat(hr, lift_d), dstat(hr, lift_x), dstat(hr, lambda r: r["gross"] - COST)
        if (ld["t"] or 0) <= -2 or (lx["t"] or 0) <= -2:
            v = "FAIL"
        elif (ld["mean"] >= LIFT_BAR and (ld["t"] or 0) >= T_PASS and lx["mean"] >= LIFT_BAR
              and (lx["t"] or 0) >= T_PASS and net["mean"] > 0
              and min(drop_days(hr, lift_d), drop_days(hr, lift_x)) >= 0
              and min(drop_names(hr, lift_d), drop_names(hr, lift_x)) >= 0):
            v = "PASS"
        else:
            v = "NOT_PASSED"
        hv.append(v)
        ubs.append((ld["ub"], lx["ub"], net["ub"]))
        res["halves"][h] = {"verdict": v, "lift_d": ld, "lift_x": lx, "net": net,
                            "bonferroni_2_73_met": min(ld["t"] or 0, lx["t"] or 0) >= T_INFO,
                            "net_by_cost": {f"{round(c * 1e4)}bp": dstat(hr, lambda r, c=c: r["gross"] - c)["mean"]
                                            for c in COSTS},
                            "win_rate": sum(1 for r in hr if r["gross"] > COST) / max(1, len(hr)),
                            "hold_median": sorted(r["hold"] for r in hr)[len(hr) // 2] if hr else None}
        info = {}
        for lab, key in (("exit_top_zone", "gross_top"), ("min_hold_5", "gross_min5"), ("time_stop_15", "gross_t15")):
            info[lab] = {"net": dstat(hr, lambda r, k=key: r[k] - COST if r.get(k) is not None else None)["mean"],
                         "lift_d": dstat(hr, lambda r, k=key: (r[k] - r["d"]) if r.get(k) is not None
                                         and r.get("d") is not None else None)["mean"]}
        info["d_rule_exit_lift"] = dstat(hr, lambda r: (r["gross"] - r["d_rule_exit"])
                                         if r.get("d_rule_exit") is not None else None)["mean"]
        for var in ("fast_only", "square"):
            vr = [r for r in rows if r["variant"] == var and half(r) == h]
            info[var] = {"n": len(vr), "net": dstat(vr, lambda r: r["gross"] - COST)["mean"],
                         "lift_d": dstat(vr, lift_d)["mean"], "lift_x": dstat(vr, lift_x)["mean"]}
        info["os_tri_lift_d"] = dstat([r for r in hr if r.get("os_tri")], lift_d)["mean"]
        info["px20_lift_d"] = dstat([r for r in hr if (r.get("px") or 0) >= 20], lift_d)["mean"]
        info["by_hour_net"] = {f"{hh}": dstat([r for r in hr if r["entry_min"] // 60 == hh],
                                             lambda r: r["gross"] - COST)["mean"] for hh in range(9, 16)}
        res["info"][h] = info
    if "FAIL" in hv:
        verdict = "FAIL"
    elif all(x == "PASS" for x in hv):
        verdict = "PASS"
    elif any(all(u[i] is not None and u[i] < LIFT_BAR for u in ubs) for i in (0, 1)) or \
            all(u[2] is not None and u[2] < 0 for u in ubs):
        verdict = "FAIL"
    else:
        verdict = "UNDERPOWERED"
    res["verdict"] = verdict
    res["skeptic_ready"], res["module_sha256"], res["lines"] = a.skeptic_ready, module_shas(), a.lines
    write(out_dir, "result.json", res)
    print(json.dumps(res, indent=1, default=str))


if __name__ == "__main__":
    main()
