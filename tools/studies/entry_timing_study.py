#!/usr/bin/env python3
"""entry_timing_study.py — which names and which moment to open, on 1m bars since 9/1.

Validation half of docs/ENTRY_OPTIMIZATION_2026-09-25.md. Reuses the runway
bar cache + supply list (mid_rise_runway_study / day_change_fade_study) and
the name_selection_study cost model. Every event is a fast %R (21, EWM 7) up
cross on a supply name, $20-$100, 09:31-15:30, entered at the bar close
(or a later bar close for the confirmation variants). Labels are
close-to-close net15/net30 after a price-tier spread cost, and the runway
study's runner flag. Train <= 2026-09-17, test >= 2026-09-18; everything is
also averaged per symbol-day (a hot name crossing 6 times counts once), and
per-day sign vs baseline is reported.

NAME variants (filters on the -50 cross, kept vs dropped):
  room      >= 2.6% below the running session high (causal)
  pace>=1.64 / pace top tercile  SIP volume so far vs own 20-day normal
  bs_score top tercile, with and without the pace term (book_server.runway_score)
TIMING variants:
  level -60 / -40 instead of -50
  confirm1 / confirm2: still above -50 one / two bar closes later, enter then
  time-of-day buckets; first 10 minutes skipped
  same-name re-entry: one position per name (15-min hold proxy); block after
  a loser for 30 min / rest of day; max 2 per name per day

USAGE (on the mini; offline, reads caches only)
  .venv/bin/python tools/studies/entry_timing_study.py [--min-price 20 --max-price 100]
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import pickle
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
import mid_rise_runway_study as mr  # noqa: E402
import day_change_fade_study as dcf  # noqa: E402

ET = ZoneInfo("America/New_York")
TRAIN_END, TEST_START = "2026-09-17", "2026-09-18"
ROOM_CUT = -2.6


def cost(px):
    return 0.10 if px >= 20 else 0.20 if px >= 10 else 0.40 if px >= 5 else 1.00


def ret_at(B, j0, px, mins):
    t, c = B[0], B[4]
    j = bisect.bisect_right(t, t[j0] + mins * 60) - 1
    if j <= j0 or (t[j] - t[j0]) < mins * 60 * 0.5:
        return None
    return (c[j] / px - 1) * 100


def pace_term(p):
    if p is None:
        return 0.0
    p = min(max(0.0, p), 4.0)
    return 0.4 * p / 1.64 if p < 1.64 else 0.4 + 0.6 * (p - 1.64) / (4.0 - 1.64)


def build(cache, supply, stops, av, lo, hi):
    out = []
    for (sym, day), B in cache.items():
        if not isinstance(B, tuple) or len(B) != 6 or not ("2026-09-01" <= day <= "2026-09-24"):
            continue
        src, t0 = "?", None
        if supply and day in supply:
            if sym not in supply[day]:
                continue
            src, t0 = supply[day][sym]["src"], supply[day][sym]["t0"]
        t, o, h, l, c, v = B
        if len(c) < 30:
            continue
        fast = mr.percent_r_series(h, l, c, mr.FAST_LEN, mr.FAST_SPAN)
        stop = stops.get((sym, day), mr.DEFAULT_STOP_PCT)
        avg = av.get((sym, day))
        for level in (-60.0, -50.0, -40.0):
            for i in range(1, len(c)):
                a, b = fast[i - 1], fast[i]
                if a is None or b is None or not (a <= level < b):
                    continue
                dt = datetime.fromtimestamp(t[i], ET)
                m = dt.hour * 60 + dt.minute - 570
                if not (1 <= m <= 360):
                    continue
                if t0 is not None and t[i] < t0:
                    continue
                for conf in ((0, 1, 2) if level == -50.0 else (0,)):
                    j = i + conf
                    if j >= len(c) or any(fast[k] is None or fast[k] <= level for k in range(i, j + 1)):
                        continue
                    px = c[j]
                    if not (lo <= px <= hi):
                        continue
                    r15, r30 = ret_at(B, j, px, 15), ret_at(B, j, px, 30)
                    if r15 is None or r30 is None:
                        continue
                    lab = mr.label_path(B, j, px, stop)
                    hod = max(h[:j + 1])
                    fr = dcf.expected_fraction(m) if avg else 0
                    pace = sum(v[:j + 1]) / (avg * fr) if avg and fr > 0 else None
                    chg = (px / o[0] - 1) * 100 if o[0] else None
                    base = 1.5 * math.tanh((chg or 0) / 8) + (0.5 if m < 90 else 0) + (0.5 if src == "movers" else 0)
                    k = cost(px)
                    out.append({"symbol": sym, "day": day, "ts": t[j], "level": level, "conf": conf,
                                "mins": m, "price": px, "room": (px / hod - 1) * 100, "pace": pace,
                                "chg": chg, "bs_nopace": base, "bs_pace": base + (pace_term(pace) if m >= 16 else 0),
                                "runner": lab["runner"], "net15": r15 - k, "net30": r30 - k})
    return out


def st(rows):
    if not rows:
        return None
    n = len(rows)
    sd = defaultdict(list)
    for r in rows:
        sd[(r["symbol"], r["day"])].append(r)
    x30 = [r["net30"] for r in rows]
    se = statistics.pstdev(x30) / n ** 0.5 if n > 1 else 0
    return {"n": n, "sd": len(sd), "runner": sum(r["runner"] for r in rows) / n,
            "net15": statistics.mean(r["net15"] for r in rows), "net30": statistics.mean(x30),
            "t30": statistics.mean(x30) / se if se else 0,
            "sd_net30": statistics.mean(statistics.mean(y["net30"] for y in v) for v in sd.values()),
            "sd_net15": statistics.mean(statistics.mean(y["net15"] for y in v) for v in sd.values())}


def line(label, s):
    if not s:
        return f"  {label:34s} n=0"
    return (f"  {label:34s} n={s['n']:5d} sd={s['sd']:4d} runner {s['runner']:4.0%} net15 {s['net15']:+.3f} "
            f"net30 {s['net30']:+.3f} (t {s['t30']:+.1f}) | per sym-day net15 {s['sd_net15']:+.3f} net30 {s['sd_net30']:+.3f}")


def per_day(rows, keep, days):
    """Days where kept beats all (per sym-day net30)."""
    wins, tot = 0, 0
    for d in days:
        A = [r for r in rows if r["day"] == d]
        K = [r for r in A if keep(r)]
        a, k = st(A), st(K)
        if a and k and k["sd"] >= 2:
            tot += 1
            wins += k["sd_net30"] > a["sd_net30"]
    return f"{wins}/{tot}"


def sequence(rows, block_after_loss_min=None, max_per_day=None, hold=15):
    """Walk -50 crosses per name-day as trades (one open per name, 15-min hold)."""
    kept, dropped = [], []
    by = defaultdict(list)
    for r in rows:
        by[(r["symbol"], r["day"])].append(r)
    for _k, lst in by.items():
        lst.sort(key=lambda r: r["ts"])
        busy_until, blocked_until, n = -1, -1, 0
        for r in lst:
            if r["ts"] < busy_until:
                continue            # already holding this name: not an open either way
            if r["ts"] < blocked_until or (max_per_day and n >= max_per_day):
                dropped.append(r)
                continue
            kept.append(r)
            n += 1
            busy_until = r["ts"] + hold * 60
            if block_after_loss_min is not None and r["net15"] < 0:
                blocked_until = r["ts"] + hold * 60 + block_after_loss_min * 60
    return kept, dropped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-price", type=float, default=20.0)
    ap.add_argument("--max-price", type=float, default=100.0)
    ap.add_argument("--avgvol-cache", default="/tmp/bb_avgvol.json")
    ap.add_argument("--dump", default=None, help="write events JSON here")
    a = ap.parse_args()
    cache = pickle.load(open(dcf._first_path(dcf.CACHE_CANDIDATES), "rb"))
    sp = dcf._first_path(dcf.SUPPLY_CANDIDATES)
    supply = json.load(open(sp)) if sp else {}
    av = {}
    if os.path.exists(a.avgvol_cache):
        av = {tuple(k.split("|")): v for k, v in json.load(open(a.avgvol_cache)).items()}
    ev = build(cache, supply, mr.load_stop_map(), av, a.min_price, a.max_price)
    if a.dump:
        json.dump(ev, open(a.dump, "w"))
    base = [r for r in ev if r["level"] == -50 and r["conf"] == 0]
    days = sorted({r["day"] for r in base})
    halves = (("TRAIN <=9/17", [d for d in days if d <= TRAIN_END]),
              ("TEST >=9/18", [d for d in days if d >= TEST_START]))
    tr = [r for r in base if r["day"] <= TRAIN_END]
    cut = lambda key, q: sorted(r[key] for r in tr if r[key] is not None)[int(q * len([r for r in tr if r[key] is not None]))]
    pace_hi = cut("pace", 2 / 3)
    bs_p, bs_n = cut("bs_pace", 2 / 3), cut("bs_nopace", 2 / 3)
    chg_hi, chg_lo = cut("chg", 2 / 3), cut("chg", 1 / 3)
    print(f"events {len(ev)}; -50 baseline {len(base)} crosses on {len({(r['symbol'], r['day']) for r in base})} "
          f"sym-days, days {days[0]}..{days[-1]}; pace known {sum(r['pace'] is not None for r in base)}; "
          f"train cuts: chg top/bottom tercile {chg_hi:.1f}/{chg_lo:.1f}%, pace top tercile >= {pace_hi:.2f}, bs_pace >= {bs_p:.2f}, bs_nopace >= {bs_n:.2f}")
    filters = {
        "room <= -2.6%": lambda r: r["room"] <= ROOM_CUT,
        "pace >= 1.64": lambda r: r["pace"] is not None and r["pace"] >= 1.64,
        "pace top tercile": lambda r: r["pace"] is not None and r["pace"] >= pace_hi,
        "bs score (with pace) top 1/3": lambda r: r["bs_pace"] >= bs_p,
        "bs score (no pace) top 1/3": lambda r: r["bs_nopace"] >= bs_n,
        "bs(pace) top 1/3 + room": lambda r: r["bs_pace"] >= bs_p and r["room"] <= ROOM_CUT,
        "bs(no pace) top 1/3 + room": lambda r: r["bs_nopace"] >= bs_n and r["room"] <= ROOM_CUT,
        "green day + room": lambda r: (r["chg"] or 0) > 0 and r["room"] <= ROOM_CUT,
        "day change top 1/3": lambda r: r["chg"] is not None and r["chg"] >= chg_hi,
        "day change bottom 1/3": lambda r: r["chg"] is not None and r["chg"] <= chg_lo,
        "room + first 90 min": lambda r: r["room"] <= ROOM_CUT and r["mins"] < 90,
        "room OR first 90 min": lambda r: r["room"] <= ROOM_CUT or r["mins"] < 90,
        "skip first 10 min": lambda r: r["mins"] >= 10,
        "first 90 min only": lambda r: r["mins"] < 90,
    }
    for label, D in halves:
        H = [r for r in base if r["day"] in D]
        print(f"\n=== {label}: NAME / WINDOW filters on the -50 cross (kept | dropped; per-day kept>all) ===")
        print(line("all -50 crosses", st(H)))
        for name, f in filters.items():
            print(line(f"KEEP {name}", st([r for r in H if f(r)])) + f"  days {per_day(H, f, D)}")
            print(line(f"  drop {name}", st([r for r in H if not f(r)])))
        print(f"--- time of day ---")
        for a0, a1 in ((1, 10), (10, 30), (30, 90), (90, 210), (210, 360)):
            print(line(f"min {a0}-{a1}", st([r for r in H if a0 <= r["mins"] < a1])))
        print(f"--- cross level / confirmation (all names) ---")
        for lv, cf, nm in ((-60, 0, "level -60"), (-50, 0, "level -50 (baseline)"), (-40, 0, "level -40"),
                           (-50, 1, "confirm: above -50 1 bar later"), (-50, 2, "confirm: 2 bars later")):
            S = [r for r in ev if r["day"] in D and r["level"] == lv and r["conf"] == cf]
            print(line(nm, st(S)))
        print(f"--- same-name re-entry (one open per name, 15-min hold proxy) ---")
        k0, _ = sequence(H)
        print(line("baseline sequence", st(k0)))
        for nm, kw in (("block 30m after a loser", {"block_after_loss_min": 30}),
                       ("block rest of day after a loser", {"block_after_loss_min": 999}),
                       ("max 2 per name per day", {"max_per_day": 2})):
            k, d = sequence(H, **kw)
            print(line(f"KEEP {nm}", st(k)))
            print(line(f"  dropped by {nm}", st(d)))
    print("\nper day (per sym-day net30): all | room | -40 | confirm1 | skip10")
    for d in days:
        A = [r for r in base if r["day"] == d]
        R = [r for r in A if r["room"] <= ROOM_CUT]
        L4 = [r for r in ev if r["day"] == d and r["level"] == -40 and r["conf"] == 0]
        C1 = [r for r in ev if r["day"] == d and r["level"] == -50 and r["conf"] == 1]
        S10 = [r for r in A if r["mins"] >= 10]
        f = lambda s: f"{st(s)['sd_net30']:+.3f}({st(s)['sd']:3d})" if st(s) else "   n/a     "
        print(f"  {d} {f(A)} {f(R)} {f(L4)} {f(C1)} {f(S10)}")


if __name__ == "__main__":
    main()
