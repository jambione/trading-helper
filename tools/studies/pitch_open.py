#!/usr/bin/env python3
"""Pitch open. Spec: docs/studies/pitch_open_prereg.json (schema pitch_open_v2).

One grade: both 1-minute %R pitches up, and the fast pitch at least the slow pitch
within 5 tenths. Lines are copied here (Williams %R then EMA). The live arm stays
out of this file. PRIMARY lines are IEX. Prices are SIP.

  (default)  count step. Trades, days, MDE, skips, cell counts. No means.
  --canary   add 10 bp to a copy of the primary gross. Print the change only.
  --score    the primary ladder. Refuses without the ready id, the floors, and canary_ok.
  --lines sip  information run. It has no pass of its own.
"""
from __future__ import annotations

import argparse
import hashlib
import json
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
DEC_LO, DEC_HI, RTH_END, CTRL_SPAN = 585, 920, 960, 60
REFRACT, HOLD = 15, 10
COST, LIFT_BAR, T_PASS, T_INFO = 0.0010, 0.0005, 2.0, 2.73
COSTS_INFO = (0.0003, 0.0005)
FLOOR_TRADES, FLOOR_DAYS, SPLIT_JUMP = 300, 20, 0.30
PRE, OB, CONF, EPS = -35.0, -20.0, 15.0, 0.05
SKIP_KEYS = (
    "no_line_feed", "no_previous_session", "split_jump", "no_warmup",
    "bad_line_series", "missing_entry", "missing_exit", "missing_control",
)
POP_TAGS = {
    "primary": ("pitch-d", "pitch-d2", "pitch-x", "pitch-x2"),
    "confirmed": ("pitch-k-d", "pitch-k-d2", "pitch-k-x", "pitch-k-x2"),
    "complement": ("pitch-c-d", "pitch-c-d2", "pitch-c-x", "pitch-c-x2"),
    "empty": ("pitch-e-d", "pitch-e-d2", "pitch-e-x", "pitch-e-x2"),
}


def ema(x: np.ndarray, span: int) -> np.ndarray:
    """pandas ewm(span, adjust=False), the desk smoother. Copied, not imported."""
    import pandas as pd
    return pd.Series(x).ewm(span=max(1, int(span)), adjust=False).mean().to_numpy()


def pct_r(h: np.ndarray, lo: np.ndarray, c: np.ndarray, n: int) -> np.ndarray:
    """-100 * (HH - C) / (HH - LL). A zero range is NaN."""
    import pandas as pd
    hh = pd.Series(h).rolling(n).max()
    ll = pd.Series(lo).rolling(n).min()
    return (-100 * (hh - pd.Series(c)) / (hh - ll).replace(0, np.nan)).to_numpy()


def series_ok(rows) -> bool:
    """True when minutes are non-empty and strictly increasing. Duplicates are rejected."""
    if rows is None or len(rows) == 0:
        return False
    prev = None
    for r in rows:
        m = int(r[0])
        if prev is not None and m <= prev:
            return False
        prev = m
    return True


def grid(rows_prev, rows_today):
    """In-session gaps of 2..15 minutes filled flat at the previous close. Overnight is not filled."""
    keys, H, L, C = [], [], [], []
    for off, rows in ((0, rows_prev), (10000, rows_today)):
        prev_m = None
        for r in rows:
            m = int(r[0])
            if prev_m is not None and 1 < m - prev_m <= GAP_FILL:
                for g in range(prev_m + 1, m):
                    keys.append(off + g)
                    H.append(C[-1])
                    L.append(C[-1])
                    C.append(C[-1])
            keys.append(off + m)
            H.append(float(r[2]))
            L.append(float(r[3]))
            C.append(float(r[4]))
            prev_m = m
    return keys, np.array(H), np.array(L), np.array(C)


def lines(rows_prev, rows_today):
    """{real today minute: fast}, {real today minute: slow}. Filled minutes are not stored."""
    keys, H, L, C = grid(rows_prev, rows_today)
    f = ema(pct_r(H, L, C, FAST_LEN), FAST_SPAN)
    s = ema(pct_r(H, L, C, SLOW_LEN), SLOW_SPAN)
    real = {int(r[0]) for r in rows_today}
    fast, slow = {}, {}
    for k, a, b in zip(keys, f, s):
        if k < 10000 or np.isnan(a) or np.isnan(b):
            continue
        m = k - 10000
        if m in real:
            fast[m] = float(a)
            slow[m] = float(b)
    return fast, slow


def tenths(pitch: float) -> int:
    return int(round(pitch * 10))


def pair(fast: dict, slow: dict, t: int):
    """(f, s) in tenths, or None when the previous real minute is absent."""
    if t not in fast or (t - 1) not in fast or t not in slow or (t - 1) not in slow:
        return None
    fp = round(fast[t] - fast[t - 1], 1)
    sp = round(slow[t] - slow[t - 1], 1)
    return tenths(fp), tenths(sp)


def state_fs(fs) -> bool:
    """The one grade: both up, and fast pitch at least the slow pitch within 5 tenths."""
    if fs is None:
        return False
    f, s = fs
    return f > 0 and s > 0 and f + 5 >= s


def complement_fs(fs) -> bool:
    if fs is None:
        return False
    f, s = fs
    return f > 0 and s > 0 and s > f + 5


def is_same(f: int, s: int) -> bool:
    return f > 0 and s > 0 and abs(f - s) <= 5


def is_steep(f: int, s: int) -> bool:
    return f > 0 and s > 0 and f > s + 5


def empty_square(fast: dict, slow: dict, t: int) -> bool:
    """Live empty-square shape on completed minutes. Not the grade."""
    if t not in fast or (t - 1) not in fast or t not in slow or (t - 1) not in slow:
        return False
    ft, st = fast[t], slow[t]
    if ft < PRE or st < PRE or abs(ft - st) > CONF:
        return False
    if ft >= OB and st >= OB:
        return False
    return ft > fast[t - 1] + EPS


def fires(pred, lo: int = DEC_LO, hi: int = DEC_HI, refract: int = REFRACT) -> list:
    """Onsets inside the window. The clock is set at the signal, before any fill.

    A minute before lo does not set the clock. A candidate inside the refractory
    is not a fire and does not move the clock.
    """
    last = None
    out = []
    for t in range(lo, hi + 1):
        if not (pred(t) and not pred(t - 1)):
            continue
        if last is not None and t - last < refract:
            continue
        last = t
        out.append(t)
    return out


def seeded(*parts) -> random.Random:
    raw = "|".join(map(str, parts)).encode()
    return random.Random(int(hashlib.sha256(raw).hexdigest()[:16], 16))


def gross_at(sip: dict, entry: int, hold: int):
    """Open of entry to close of entry+hold-1. Both prices must be > 0."""
    exit_m = entry + hold - 1
    if exit_m >= RTH_END:
        return None
    a, b = sip.get(entry), sip.get(exit_m)
    if a is None or b is None or a[0] <= 0 or b[1] <= 0:
        return None
    return b[1] / a[0] - 1.0


def miss_reason(sip: dict, entry: int, hold: int):
    exit_m = entry + hold - 1
    a = sip.get(entry)
    if a is None or a[0] <= 0:
        return "missing_entry"
    if exit_m >= RTH_END:
        return "missing_exit"
    b = sip.get(exit_m)
    if b is None or b[1] <= 0:
        return "missing_exit"
    return None


def d_candidates(sip: dict, entry: int, hold: int) -> list:
    off = hold - 1
    out = []
    for c in range(entry + 1, entry + CTRL_SPAN + 1):
        if c + off >= RTH_END:
            continue
        a, b = sip.get(c), sip.get(c + off)
        if a is not None and b is not None and a[0] > 0 and b[1] > 0:
            out.append(c)
    return out


def x_candidates(sips: dict, pool: list, sym: str, entry: int, hold: int) -> list:
    exit_m = entry + hold - 1
    if exit_m >= RTH_END:
        return []
    out = []
    for n in pool:
        if n == sym:
            continue
        a, b = sips[n].get(entry), sips[n].get(exit_m)
        if a is not None and b is not None and a[0] > 0 and b[1] > 0:
            out.append(n)
    return out


def draw_d(sip: dict, sym: str, day: str, entry: int, hold: int, tag: str):
    cand = d_candidates(sip, entry, hold)
    if not cand:
        return None
    c = seeded(sym, day, entry, tag).choice(cand)
    return gross_at(sip, c, hold)


def draw_x(sips: dict, pool: list, cache: dict, sym: str, day: str, entry: int, hold: int, tag: str):
    key = (entry, hold)
    names = cache.get(key)
    if names is None:
        names = x_candidates(sips, pool, "", entry, hold)
        # x_candidates with sym "" keeps every name, pool order, which is sorted.
        cache[key] = names
    cand = [n for n in names if n != sym]
    if not cand:
        return None
    n = seeded(sym, day, entry, tag).choice(cand)
    return gross_at(sips[n], entry, hold)


def half_of(day: str) -> str:
    return "A" if day <= HALF_A_END else "B"


def day_task(args) -> dict:
    day, prev_day, line_feed = args
    import indicator_confirm_hist as H
    sip_raw = H.load_raw("sip", day)
    lf_today = H.load_raw(line_feed, day)
    lf_prev = H.load_raw(line_feed, prev_day)
    sip_prev = H.load_raw("sip", prev_day)
    sips = {s: {int(r[0]): (float(r[1]), float(r[4])) for r in rows} for s, rows in sip_raw.items()}
    pool = sorted(sips)
    half = half_of(day)
    st, rows = Counter(), []
    cache: dict = {}
    for sym in pool:
        if sym not in lf_today:
            st[f"{half}|no_line_feed"] += 1
            continue
        if sym not in lf_prev:
            st[f"{half}|no_previous_session"] += 1
            continue
        if not series_ok(lf_today[sym]) or not series_ok(lf_prev[sym]):
            st[f"{half}|bad_line_series"] += 1
            continue
        if sym in sip_prev and len(sip_prev[sym]) and len(sip_raw[sym]):
            pc, op = float(sip_prev[sym][-1][4]), float(sip_raw[sym][0][1])
            if pc > 0 and abs(op / pc - 1) > SPLIT_JUMP:
                st[f"{half}|split_jump"] += 1
                continue
        fast, slow = lines(lf_prev[sym], lf_today[sym])
        if not any(DEC_LO <= m <= DEC_HI for m in fast):
            st[f"{half}|no_warmup"] += 1
            continue
        sip = sips[sym]

        def pred_state(t, _fast=fast, _slow=slow):
            return state_fs(pair(_fast, _slow, t))

        def pred_confirmed(t, _fast=fast, _slow=slow):
            return state_fs(pair(_fast, _slow, t)) and state_fs(pair(_fast, _slow, t - 1))

        def pred_comp(t, _fast=fast, _slow=slow):
            return complement_fs(pair(_fast, _slow, t))

        def pred_empty(t, _fast=fast, _slow=slow):
            return empty_square(_fast, _slow, t)

        pops = (
            ("primary", pred_state),
            ("confirmed", pred_confirmed),
            ("complement", pred_comp),
            ("empty", pred_empty),
        )
        for pop, pred in pops:
            tags = POP_TAGS[pop]
            for t in fires(pred):
                e = t + 1
                why = miss_reason(sip, e, HOLD)
                if why:
                    st[f"{half}|{why}"] += 1
                    continue
                g = gross_at(sip, e, HOLD)
                d = draw_d(sip, sym, day, e, HOLD, tags[0])
                d2 = draw_d(sip, sym, day, e, HOLD, tags[1])
                x = draw_x(sips, pool, cache, sym, day, e, HOLD, tags[2])
                x2 = draw_x(sips, pool, cache, sym, day, e, HOLD, tags[3])
                if g is None or d is None or d2 is None or x is None or x2 is None:
                    st[f"{half}|missing_control"] += 1
                    continue
                fs = pair(fast, slow, t)
                if fs is None:
                    f = s = None
                    pitch_class = "unknown"
                else:
                    f, s = fs
                    pitch_class = "ok" if state_fs(fs) else "not"
                row = {
                    "day": day, "sym": sym, "pop": pop, "dec_min": t, "entry_min": e,
                    "f": f, "s": s, "gross": g, "d": d, "d2": d2, "x": x, "x2": x2,
                    "fast_under": bool(t in fast and t in slow and fast[t] < slow[t]),
                    "pitch_class": pitch_class,
                }
                if pop == "primary":
                    row["px"] = sip[e][0]
                    row["empty_shape"] = empty_square(fast, slow, t)
                    for hold, suffix in ((3, "h3"), (5, "h5")):
                        row[f"gross_{suffix}"] = gross_at(sip, e, hold)
                        row[f"d_{suffix}"] = draw_d(sip, sym, day, e, hold, f"pitch-d-{suffix}")
                        row[f"d2_{suffix}"] = draw_d(sip, sym, day, e, hold, f"pitch-d2-{suffix}")
                        row[f"x_{suffix}"] = draw_x(sips, pool, cache, sym, day, e, hold, f"pitch-x-{suffix}")
                        row[f"x2_{suffix}"] = draw_x(sips, pool, cache, sym, day, e, hold, f"pitch-x2-{suffix}")
                rows.append(row)
    return {"rows": rows, "stats": dict(st)}


def per_day(rows, key) -> dict:
    per = defaultdict(list)
    for r in rows:
        v = key(r)
        if v is not None:
            per[r["day"]].append(v)
    return {d: statistics.mean(v) for d, v in per.items()}


def dstat(rows, key) -> dict:
    return E.stats(list(per_day(rows, key).values()))


def lift_d(r):
    return r["gross"] - r["d"] if r.get("d") is not None else None


def lift_x(r):
    return r["gross"] - r["x"] if r.get("x") is not None else None


def drop_days(rows, key, k=3):
    m = per_day(rows, key)
    if len(m) <= k:
        return None
    drop = set(sorted(m, key=lambda d: (-m[d], d))[:k])
    return dstat([r for r in rows if r["day"] not in drop], key)["mean"]


def drop_names(rows, key, k=5):
    tot = defaultdict(float)
    for r in rows:
        v = key(r)
        if v is not None:
            tot[r["sym"]] += v
    if len(tot) <= k:
        return None
    drop = set(sorted(tot, key=lambda s: (-tot[s], s))[:k])
    return dstat([r for r in rows if r["sym"] not in drop], key)["mean"]


def mde(rows, a, b):
    vals = list(per_day(
        rows, lambda r: (r[a] - r[b]) if r.get(a) is not None and r.get(b) is not None else None
    ).values())
    if len(vals) < 2:
        return None
    sd = statistics.stdev(vals)
    return (2.0 + 0.84) * sd / (len(vals) ** 0.5)


def prereg_path() -> str:
    return os.path.join(ROOT, "docs", "studies", "pitch_open_prereg.json")


def prereg_sha() -> str:
    return hashlib.sha256(open(prereg_path(), "rb").read()).hexdigest()


def module_shas() -> dict:
    d = os.path.dirname(os.path.abspath(__file__))
    return {m: hashlib.sha256(open(os.path.join(d, f"{m}.py"), "rb").read()).hexdigest()
            for m in ("pitch_open", "eod_reversal", "indicator_confirm_hist")}


def out_dir_for(lines: str) -> str:
    name = "pitch_open" if lines == "iex" else "pitch_open_sip_lines"
    return os.path.join(ROOT, "ai_reports", name)


def write_json(path: str, obj, pretty: bool = False) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        if pretty:
            json.dump(obj, f, indent=1, default=str)
        else:
            json.dump(obj, f, separators=(",", ":"), default=str)
    os.replace(tmp, path)


def _primary(rows):
    return [r for r in rows if r.get("pop") == "primary"]


def _check_partition(rows) -> None:
    """Outcome-blind. A broken predicate stops the count before any mean exists."""
    for r in rows:
        f, s, pop = r.get("f"), r.get("s"), r.get("pop")
        if pop == "primary":
            if not (f is not None and s is not None and f > 0 and s > 0 and f + 5 >= s):
                raise SystemExit("predicate partition broken")
            if is_same(f, s) == is_steep(f, s):
                raise SystemExit("predicate partition broken")
        elif pop == "complement":
            if not (f is not None and s is not None and s > f + 5 and f > 0 and s > 0):
                raise SystemExit("predicate partition broken")
        elif pop == "empty" and r.get("pitch_class") == "ok":
            if not state_fs((f, s)):
                raise SystemExit("predicate partition broken")


def count_body(rows, st: Counter) -> dict:
    _check_partition(rows)
    prim = _primary(rows)
    cnt = {
        "stats_skips": {h: {k: int(st.get(f"{h}|{k}", 0)) for k in SKIP_KEYS} for h in ("A", "B")},
        "module_sha256": module_shas(),
        "prereg_sha256": prereg_sha(),
    }
    for h in ("A", "B"):
        hr = [r for r in prim if half_of(r["day"]) == h]
        md, mx = mde(hr, "d", "d2"), mde(hr, "x", "x2")
        days_n = len({r["day"] for r in hr})
        same = sum(1 for r in hr if is_same(r["f"], r["s"]))
        steep = sum(1 for r in hr if is_steep(r["f"], r["s"]))
        if same + steep != len(hr):
            raise SystemExit("predicate partition broken")
        empty = [r for r in rows if r["pop"] == "empty" and half_of(r["day"]) == h]
        cnt[h] = {
            "trades": len(hr),
            "days": days_n,
            "mde_d": md,
            "mde_x": mx,
            "hold_bars": HOLD,
            "floors_met": bool(
                len(hr) >= FLOOR_TRADES and days_n >= FLOOR_DAYS
                and md is not None and md <= LIFT_BAR and mx is not None and mx <= LIFT_BAR
            ),
            "cells": {
                "primary": len(hr),
                "same": same,
                "fast_steeper": steep,
                "fast_under": sum(1 for r in hr if r.get("fast_under")),
                "confirmed": sum(1 for r in rows if r["pop"] == "confirmed" and half_of(r["day"]) == h),
                "complement": sum(1 for r in rows if r["pop"] == "complement" and half_of(r["day"]) == h),
                "empty_square": len(empty),
                "empty_square_pitch_ok": sum(1 for r in empty if r.get("pitch_class") == "ok"),
                "empty_square_pitch_not": sum(1 for r in empty if r.get("pitch_class") == "not"),
                "empty_square_pitch_unknown": sum(1 for r in empty if r.get("pitch_class") == "unknown"),
            },
        }
    return cnt


def load_or_build(lines: str, workers: int, prereg_commit: str):
    import indicator_confirm_hist as H
    dest = out_dir_for(lines)
    cache = os.path.join(dest, "rows.json")
    sha, psha = module_shas(), prereg_sha()
    if os.path.exists(cache):
        with open(cache) as f:
            c = json.load(f)
        if c.get("module_sha256") == sha and c.get("prereg_sha256") == psha:
            return c["rows"], Counter(c.get("stats") or {}), dest
    days = H.warehouse_days()
    run = [(d, days[i - 1], lines) for i, d in enumerate(days) if FIRST <= d <= LAST and i >= 1]
    rows, st = [], Counter()
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for i, res in enumerate(ex.map(day_task, run, chunksize=2), 1):
            rows.extend(res["rows"])
            st.update(res["stats"])
            print(f"session {i}/{len(run)}", file=sys.stderr, flush=True)
    write_json(cache, {"rows": rows, "stats": dict(st), "module_sha256": sha, "prereg_sha256": psha,
                       "prereg_commit": prereg_commit, "lines": lines})
    return rows, st, dest


def canary_deltas(rows) -> dict:
    """Change in the day-equal net and lifts after +10 bp on a copy. No levels."""
    out = {}
    for h in ("A", "B"):
        hr = [r for r in _primary(rows) if half_of(r["day"]) == h]
        alt = [dict(r, gross=r["gross"] + 0.0010) for r in hr]

        def net(r):
            return r["gross"] - COST

        for name, key in (("net", net), ("lift_d", lift_d), ("lift_x", lift_x)):
            a, b = list(per_day(hr, key).values()), list(per_day(alt, key).values())
            if not a or not b:
                out[f"{h}_{name}"] = None
            else:
                out[f"{h}_{name}"] = statistics.mean(b) - statistics.mean(a)
    return out


def cell(rows, gkey="gross", dkey="d", xkey="x") -> dict:
    use = [r for r in rows if r.get(gkey) is not None and r.get(dkey) is not None and r.get(xkey) is not None]

    def ld(r, gk=gkey, dk=dkey):
        return r[gk] - r[dk]

    def lx(r, gk=gkey, xk=xkey):
        return r[gk] - r[xk]

    def net(r, gk=gkey):
        return r[gk] - COST

    return {
        "n": len(use),
        "lift_d": dstat(use, ld)["mean"],
        "lift_x": dstat(use, lx)["mean"],
        "net": dstat(use, net)["mean"],
        "label": "not-a-grade",
    }


def info_for(hr, rows, h) -> dict:
    same = [r for r in hr if is_same(r["f"], r["s"])]
    steep = [r for r in hr if is_steep(r["f"], r["s"])]
    under = [r for r in hr if r.get("fast_under")]
    not_under = [r for r in hr if not r.get("fast_under")]
    empty_shape = [r for r in hr if r.get("empty_shape")]
    confirmed = [r for r in rows if r["pop"] == "confirmed" and half_of(r["day"]) == h]
    comp = [r for r in rows if r["pop"] == "complement" and half_of(r["day"]) == h]
    empty = [r for r in rows if r["pop"] == "empty" and half_of(r["day"]) == h]
    info = {
        "same_only": cell(same),
        "fast_steeper_only": cell(steep),
        "fast_under": cell(under),
        "fast_not_under": cell(not_under),
        "confirmed": cell(confirmed),
        "complement": cell(comp),
        "empty_square_pitch_ok": cell([r for r in empty if r.get("pitch_class") == "ok"]),
        "empty_square_pitch_not": cell([r for r in empty if r.get("pitch_class") == "not"]),
        "empty_square_pitch_unknown": cell([r for r in empty if r.get("pitch_class") == "unknown"]),
        "primary_and_empty_square": cell(empty_shape),
        "hold_3": cell(hr, "gross_h3", "d_h3", "x_h3"),
        "hold_5": cell(hr, "gross_h5", "d_h5", "x_h5"),
        "open_at_least_20": cell([r for r in hr if (r.get("px") or 0) >= 20]),
        "by_hour": {str(hh): cell([r for r in hr if r["entry_min"] // 60 == hh]) for hh in range(9, 16)},
        "other_costs": {
            "n": len(hr),
            "gross": dstat(hr, lambda r: r["gross"])["mean"],
            "net_3bp": dstat(hr, lambda r: r["gross"] - COSTS_INFO[0])["mean"],
            "net_5bp": dstat(hr, lambda r: r["gross"] - COSTS_INFO[1])["mean"],
            "label": "not-a-grade",
        },
    }
    return info


def ladder(hr) -> dict:
    ld, lx = dstat(hr, lift_d), dstat(hr, lift_x)
    net = dstat(hr, lambda r: r["gross"] - COST)
    dd, dx = drop_days(hr, lift_d), drop_days(hr, lift_x)
    nd, nx = drop_names(hr, lift_d), drop_names(hr, lift_x)
    hard = (ld["t"] or 0) <= -2 or (lx["t"] or 0) <= -2
    passed = (
        ld["mean"] is not None and ld["mean"] >= LIFT_BAR and (ld["t"] or 0) >= T_PASS
        and lx["mean"] is not None and lx["mean"] >= LIFT_BAR and (lx["t"] or 0) >= T_PASS
        and net["mean"] is not None and net["mean"] > 0
        and dd is not None and dd >= 0 and dx is not None and dx >= 0
        and nd is not None and nd >= 0 and nx is not None and nx >= 0
    )
    if hard:
        v = "FAIL"
    elif passed:
        v = "PASS"
    else:
        v = "NOT_PASSED"
    return {
        "verdict": v,
        "n": len(hr),
        "days": len({r["day"] for r in hr}),
        "lift_d": ld,
        "lift_x": lx,
        "net": net,
        "drop3_d": dd, "drop3_x": dx, "drop5_d": nd, "drop5_x": nx,
        "bonferroni_2_73_met": min(ld["t"] or 0, lx["t"] or 0) >= T_INFO,
    }


def overall(halves: dict) -> str:
    hv = [halves[h]["verdict"] for h in ("A", "B")]
    ubs = [(halves[h]["lift_d"]["ub"], halves[h]["lift_x"]["ub"], halves[h]["net"]["ub"]) for h in ("A", "B")]
    if "FAIL" in hv:
        return "FAIL"
    if all(v == "PASS" for v in hv):
        return "PASS"
    if any(all(u[i] is not None and u[i] < LIFT_BAR for u in ubs) for i in (0, 1)):
        return "FAIL"
    if all(u[2] is not None and u[2] < 0 for u in ubs):
        return "FAIL"
    return "UNDERPOWERED"


def run_canary(lines: str) -> None:
    dest = out_dir_for(lines)
    cache = os.path.join(dest, "rows.json")
    if not os.path.exists(cache):
        raise SystemExit("canary needs rows.json from this count")
    with open(cache) as f:
        c = json.load(f)
    if c.get("module_sha256") != module_shas() or c.get("prereg_sha256") != prereg_sha():
        raise SystemExit("rows.json is from different code or a different prereg")
    deltas = canary_deltas(c["rows"])
    ok = all(v is not None and abs(v - 0.0010) <= 1e-9 for v in deltas.values())
    obj = {"deltas": deltas, "canary_ok": ok, "module_sha256": module_shas(), "prereg_sha256": prereg_sha()}
    write_json(os.path.join(dest, "canary.json"), obj, pretty=True)
    print(json.dumps({"deltas": deltas, "canary_ok": ok}))


def run_score(lines: str, skeptic_ready: str) -> None:
    dest = out_dir_for(lines)
    if not skeptic_ready:
        raise SystemExit("score needs --skeptic-ready")
    cnt_path = os.path.join(dest, "count.json")
    if not os.path.exists(cnt_path):
        raise SystemExit("score needs count.json")
    with open(cnt_path) as f:
        cnt = json.load(f)
    if cnt.get("module_sha256") != module_shas() or cnt.get("prereg_sha256") != prereg_sha():
        raise SystemExit("count.json from different code or a different prereg")
    if not cnt.get("prereg_commit"):
        raise SystemExit("count.json without --prereg-commit")
    if not all(cnt[h]["floors_met"] for h in ("A", "B")):
        print("UNDERPOWERED")
        return
    canary_path = os.path.join(dest, "canary.json")
    if not os.path.exists(canary_path):
        raise SystemExit("score needs canary.json")
    with open(canary_path) as f:
        canary = json.load(f)
    if (not canary.get("canary_ok") or canary.get("module_sha256") != module_shas()
            or canary.get("prereg_sha256") != prereg_sha()):
        raise SystemExit("canary_ok is missing on this code and prereg")
    with open(os.path.join(dest, "rows.json")) as f:
        rows = json.load(f)["rows"]
    _check_partition(rows)
    halves = {}
    for h in ("A", "B"):
        hr = [r for r in _primary(rows) if half_of(r["day"]) == h]
        halves[h] = ladder(hr)
    verdict = overall(halves)
    res = {
        "verdict": verdict,
        "halves": halves,
        "skeptic_ready": skeptic_ready,
        "lines": lines,
        "prereg_sha256": prereg_sha(),
        "module_sha256": module_shas(),
    }
    if verdict in ("PASS", "FAIL"):
        res["info"] = {h: info_for(
            [r for r in _primary(rows) if half_of(r["day"]) == h], rows, h
        ) for h in ("A", "B")}
    write_json(os.path.join(dest, "result.json"), res, pretty=True)
    print(json.dumps(res, indent=1, default=str))


def run_count(lines: str, workers: int, prereg_commit: str) -> None:
    if not prereg_commit:
        raise SystemExit("count needs --prereg-commit")
    rows, st, dest = load_or_build(lines, workers, prereg_commit)
    body = count_body(rows, st)
    body["prereg_commit"] = prereg_commit
    body["lines"] = lines
    write_json(os.path.join(dest, "count.json"), body, pretty=True)
    shown = {k: body[k] for k in ("A", "B", "stats_skips", "lines") if k in body}
    print(json.dumps(shown, indent=1, default=str))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--score", action="store_true")
    ap.add_argument("--canary", action="store_true")
    ap.add_argument("--skeptic-ready", default="")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--prereg-commit", default="")
    ap.add_argument("--lines", default="iex", choices=("iex", "sip"))
    a = ap.parse_args()
    if a.canary:
        run_canary(a.lines)
        return
    if a.score:
        run_score(a.lines, a.skeptic_ready)
        return
    run_count(a.lines, a.workers, a.prereg_commit)


if __name__ == "__main__":
    main()
