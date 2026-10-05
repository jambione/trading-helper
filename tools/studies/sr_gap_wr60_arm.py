#!/usr/bin/env python3
"""−60 %R open-arm in the S/R gap (near support, room to resistance).
Pre-registration: docs/studies/sr_gap_wr60_arm_prereg.json (committed BEFORE this script is run).

Sample: admitted name-days of the 2026-10-02 indicator-levels study ($10+; control rows of
ai_reports/indicator_levels/rows.json) with its 1-min premarket+RTH SIP bars (ext.pkl).
Blocks: tools/order_blocks.py, point-in-time, charted last 3 per side, swing 10, wicks, 1-min.

SIGNAL at the close of 1-min bar i (09:40-15:30 ET), one per name per 15 min, all known at that close:
  fast %R (21, EMA 7) <= -60
  near a charted support block S (unbroken bullish OB or bearish breaker): inside [S.btm, S.top]
      OR within 0.30% above S.top
  not inside a charted resistance block
  room to nearest charted resistance above (unbroken bearish OB or bullish breaker) >= ROOM_MIN %
TRADE: buy at the open of bar i+1; hold 15 min (15:55 cap); gross = exit_close/entry - 1;
       net = gross - 0.20% ($10+ tier). Measured RT spread = mean(gross - net) of signal (= 20 bp).
CONTROL: per signal, one random minute (seed 31) in the same name-day AND same ET clock hour that
       ALSO has fast %R <= -60 (fallback: same hour only, counted); same 15-min hold.
HALVES: chronological first/second half of sample days.
PASS: both halves: (signal mean gross - control mean gross) > measured RT spread of that half,
      and >= 50 signal trades per half. Also report net. Any PASS = PENDING SKEPTIC REVIEW.
INFO: room >= 0.60%; near-support 0.10% and inside-only.
Run on the mini: .venv/bin/python tools/studies/sr_gap_wr60_arm.py
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

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))
import order_blocks as OB  # noqa: E402
from order_block_gate import series  # noqa: E402

ET = ZoneInfo("America/New_York")
IND = os.path.join(ROOT, "ai_reports", "indicator_levels")
OUT = os.path.join(ROOT, "ai_reports", "sr_gap_wr60_arm")
COST = 0.0020
ROOM_PRIMARY = 0.40
NEAR_PRIMARY = 0.30  # % above support top
HOLD = 15 * 60
WR_ARM = -60.0
SEED = 31


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


def is_support(b):
    return (b.kind == "bull" and not b.breaker) or (b.kind == "bear" and b.breaker)


def is_resist(b):
    return (b.kind == "bear" and not b.breaker) or (b.kind == "bull" and b.breaker)


def near_support(ch, px, near_pct):
    """Return (ok, dist_pct_above_top_or_0_if_inside, chosen block) for the nearest qualifying support."""
    best = None
    best_dist = None
    for b in ch:
        if not is_support(b):
            continue
        if b.btm <= px <= b.top:
            dist = 0.0
        elif px > b.top:
            dist = (px / b.top - 1) * 100
            if dist > near_pct:
                continue
        else:
            continue  # below the zone: not "closer to support from above / inside"
        if best is None or dist < best_dist:
            best, best_dist = b, dist
    if best is None:
        return False, None, None
    return True, best_dist, best


def outcome(rows, i_entry, day_end):
    """Gross return from open of i_entry to the close ~15 min later (15:55 cap)."""
    entry = rows[i_entry][1]
    t0 = rows[i_entry][0]
    j_exit = None
    for j in range(i_entry, len(rows)):
        t = rows[j][0]
        if t >= day_end:
            j_exit = j - 1 if j > i_entry else None
            break
        if t - t0 >= HOLD:
            j_exit = j
            break
    if j_exit is None or j_exit < i_entry:
        return None
    return rows[j_exit][4] / entry - 1


def tstat(xs, dd):
    if len(xs) < 2:
        return float("nan"), float("nan")
    m = statistics.fmean(xs)
    by = collections.defaultdict(float)
    for v, d in zip(xs, dd):
        by[d] += v - m
    G, n = len(by), len(xs)
    if G < 2:
        return m, float("nan")
    se = math.sqrt(sum(s * s for s in by.values()) * G / (G - 1)) / n
    return m, (m / se if se and se == se and se > 0 else float("nan"))


def main():
    os.makedirs(OUT, exist_ok=True)
    R = json.load(open(os.path.join(IND, "rows.json")))
    days = list(R["days"])
    mid = len(days) // 2
    half = {d: ("A" if k < mid else "B") for k, d in enumerate(days)}
    namedays = sorted({(r["sym"], r["day"]) for r in R["rows"] if r["pop"] == "C"})
    cache = pickle.load(open(os.path.join(IND, "ext.pkl"), "rb"))
    cal = sorted({d for (_, d) in cache})
    prev_of = {d: cal[cal.index(d) - 1] if cal.index(d) else None for d in cal}
    prepared = []
    drops0 = collections.Counter()
    for sym, day in namedays:
        rows = series(cache, sym, day, prev_of.get(day))
        if len(rows) < 100:
            drops0["short_series"] += 1
            continue
        h = [r[2] for r in rows]
        l = [r[3] for r in rows]
        c = [r[4] for r in rows]
        wr = wr_fast(h, l, c)
        states = list(OB.order_blocks(rows, bar_sec=60))
        day_idx = [i for i, r in enumerate(rows)
                   if datetime.fromtimestamp(r[0], ET).strftime("%Y-%m-%d") == day]
        if not day_idx:
            continue
        d0 = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
        day_end = d0.replace(hour=15, minute=55).timestamp()
        # decision window: close known in 09:40-15:30; need next bar for entry
        window = [i for i in day_idx
                  if 9 * 60 + 40 <= et_min(rows[i][0] + 60) <= 15 * 60 + 30 and i + 1 < len(rows)]
        prepared.append((sym, day, rows, c, wr, states, window, day_end))

    def variant(room_min, near_pct, inside_only=False):
        rng = random.Random(SEED)
        trades, ctrls, drops = [], [], collections.Counter(drops0)
        last_arm = {}  # (sym, day) -> last decision minute (refractory 15)
        for sym, day, rows, c, wr, states, window, day_end in prepared:
            for i in window:
                tq = rows[i][0] + 60
                mdec = et_min(tq)
                key_nd = (sym, day)
                if key_nd in last_arm and mdec - last_arm[key_nd] < 15:
                    continue
                if not (wr[i] <= WR_ARM):
                    continue
                ch = OB.charted([b for b in states[i][1] if b.known_ts <= tq])
                resist = [b for b in ch if is_resist(b)]
                if any(b.btm <= c[i] <= b.top for b in resist):
                    drops["inside_resistance"] += 1
                    continue
                ok, dist, S = near_support(ch, c[i], 0.0 if inside_only else near_pct)
                if not ok:
                    # still a -60 arm; not a signal (no near support). Refractory still applies so
                    # control pool density matches "arms", but we do not count it as a drop of a signal.
                    continue
                if inside_only and not (S.btm <= c[i] <= S.top):
                    continue
                above = [b.btm for b in resist if b.btm > c[i]]
                if not above:
                    drops["no_resistance_above"] += 1
                    continue
                room = (min(above) / c[i] - 1) * 100
                if room < room_min:
                    drops["room_below_min"] += 1
                    continue
                g = outcome(rows, i + 1, day_end)
                if g is None:
                    drops["no_outcome"] += 1
                    continue
                last_arm[key_nd] = mdec
                trades.append({
                    "sym": sym, "day": day, "t": tq, "room": room, "dist_sup": dist,
                    "wr": wr[i], "gross": g, "net": g - COST,
                })
                hr = mdec // 60
                # like-for-like: same hour, %R <= -60, not this bar
                same_hr = [k for k in window if k != i and et_min(rows[k][0] + 60) // 60 == hr]
                like = [k for k in same_hr if wr[k] <= WR_ARM]
                if not like:
                    drops["control_fallback_same_hour_only"] += 1
                pool = like or same_hr
                if not pool:
                    drops["control_none"] += 1
                    continue
                k = rng.choice(pool)
                cg = outcome(rows, k + 1, day_end)
                if cg is not None:
                    ctrls.append({
                        "sym": sym, "day": day, "gross": cg, "net": cg - COST,
                        "pair": len(trades) - 1, "like": bool(like),
                    })
        return trades, ctrls, drops

    trades, ctrls, drops = variant(ROOM_PRIMARY, NEAR_PRIMARY, False)
    info = {
        "room>=0.60% near0.30%": variant(0.60, NEAR_PRIMARY, False),
        "room>=0.40% near0.10%": variant(ROOM_PRIMARY, 0.10, False),
        "room>=0.40% inside-only": variant(ROOM_PRIMARY, 0.0, True),
    }
    json.dump(
        {"trades": trades, "controls": ctrls, "drops": dict(drops),
         "days": days, "half_A": days[:mid], "half_B": days[mid:],
         "prereg": "docs/studies/sr_gap_wr60_arm_prereg.json"},
        open(os.path.join(OUT, "rows.json"), "w"),
    )

    L = [
        "# −60 %R open-arm in the S/R gap (prereg docs/studies/sr_gap_wr60_arm_prereg.json)",
        "",
        f"name-days {len(namedays)}; sample days {len(days)} ({days[0]}..{days[-1]}); "
        f"chrono half A {days[:mid][0]}..{days[:mid][-1]} ({len(days[:mid])}); "
        f"half B {days[mid:][0]}..{days[mid:][-1]} ({len(days[mid:])})",
        f"signals {len(trades)}; controls {len(ctrls)}; drops {dict(drops)}",
        "",
    ]
    res = {}
    for hh in ("A", "B", "all"):
        tr = [t for t in trades if hh == "all" or half[t["day"]] == hh]
        cx = [c for c in ctrls if hh == "all" or half[c["day"]] == hh]
        if len(tr) < 5:
            L.append(f"- half {hh}: {len(tr)} signals (too few)")
            res[hh] = None
            continue
        gm = statistics.fmean([x["gross"] * 1e4 for x in tr])
        nm = statistics.fmean([x["net"] * 1e4 for x in tr])
        rt = statistics.fmean([(x["gross"] - x["net"]) * 1e4 for x in tr])  # measured RT bp
        g_t, _ = tstat([x["gross"] * 1e4 for x in tr], [x["day"] for x in tr])
        n_tstat = tstat([x["net"] * 1e4 for x in tr], [x["day"] for x in tr])
        if cx:
            # paired where possible; also unpaired means
            paired = [(trades[c["pair"]]["gross"] - c["gross"]) * 1e4 for c in cx]
            pdays = [c["day"] for c in cx]
            diff_m, diff_t = tstat(paired, pdays)
            cg = statistics.fmean([c["gross"] * 1e4 for c in cx])
            lift_unpaired = gm - cg
        else:
            diff_m = diff_t = cg = lift_unpaired = float("nan")
        win = sum(x["net"] > 0 for x in tr) / len(tr)
        L.append(
            f"- half {hh}: signals {len(tr)} on {len({x['day'] for x in tr})} days | "
            f"gross {gm:+.1f} bp | net {nm:+.1f} bp (day-clustered t {n_tstat[1]:+.2f}) | "
            f"measured RT spread {rt:.1f} bp | win {win:.0%} | "
            f"median room {statistics.median(x['room'] for x in tr):.2f}% | "
            f"median dist-to-sup {statistics.median(x['dist_sup'] for x in tr):.3f}% | "
            f"control gross {cg:+.1f} bp (n {len(cx)}) | "
            f"signal−control gross (paired) {diff_m:+.1f} bp (t {diff_t:+.2f}) | "
            f"unpaired lift {lift_unpaired:+.1f} bp | "
            f"pass check: lift {diff_m:+.1f} > RT {rt:.1f}? {diff_m > rt if diff_m == diff_m else False}"
        )
        res[hh] = (len(tr), diff_m, rt, nm)

    if any(res[h] is None or res[h][0] < 50 for h in ("A", "B")):
        v = "UNDERPOWERED (fewer than 50 signals in a half): no verdict"
    elif all(res[h][1] == res[h][1] and res[h][1] > res[h][2] for h in ("A", "B")):
        v = "PASS — PENDING SKEPTIC REVIEW (not a live gate; needs ≥ 10-session IEX confirmation)"
    else:
        v = "FAIL"

    for lab, (tr2, ct2, dr2) in info.items():
        for hh in ("A", "B"):
            tr = [t for t in tr2 if half[t["day"]] == hh]
            cx = [c for c in ct2 if half[c["day"]] == hh]
            if len(tr) < 5:
                L.append(f"- INFO {lab} half {hh}: {len(tr)} signals (too few)")
                continue
            gm = statistics.fmean([x["gross"] * 1e4 for x in tr])
            nm = statistics.fmean([x["net"] * 1e4 for x in tr])
            rt = statistics.fmean([(x["gross"] - x["net"]) * 1e4 for x in tr])
            if cx:
                paired = [(tr2[c["pair"]]["gross"] - c["gross"]) * 1e4 for c in cx]
                diff_m, diff_t = tstat(paired, [c["day"] for c in cx])
            else:
                diff_m = diff_t = float("nan")
            L.append(
                f"- INFO {lab} half {hh}: n {len(tr)} | gross {gm:+.1f} | net {nm:+.1f} | "
                f"RT {rt:.1f} | signal−control gross {diff_m:+.1f} (t {diff_t:+.2f}) | "
                f"lift>RT? {diff_m > rt if diff_m == diff_m else False}"
            )

    L.append("")
    L.append(
        f"**PRIMARY: {v}** (bar, both chronological halves: signal−control gross > measured RT spread, "
        f"n >= 50; also report net after costs)"
    )
    if res.get("A") and res.get("B"):
        L.append(
            f"Half A: n {res['A'][0]}, lift {res['A'][1]:+.1f} bp vs RT {res['A'][2]:.1f} bp, "
            f"net {res['A'][3]:+.1f} bp"
        )
        L.append(
            f"Half B: n {res['B'][0]}, lift {res['B'][1]:+.1f} bp vs RT {res['B'][2]:.1f} bp, "
            f"net {res['B'][3]:+.1f} bp"
        )
    open(os.path.join(OUT, "report.md"), "w").write("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
