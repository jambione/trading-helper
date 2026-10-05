#!/usr/bin/env python3
"""Exit-only at resistance: keep square opens as-is; TP at nearest charted resistance bottom.
Pre-registration: docs/studies/sr_exit_resist_prereg.json (committed BEFORE this script is run).

Sample: E square arms of ai_reports/indicator_levels/rows.json ($10+), bars from ext.pkl.
Blocks: tools/order_blocks.py, point-in-time, charted last 3 per side, swing 10, wicks, 1-min.

ENTRY: keep opens as-is — E arm at decision close; buy at next bar open.
SIGNAL exit: target = bottom of nearest charted resistance above entry (known at entry);
  fill when a later bar's HIGH > target (at target). Time stop 30 min / 15:55 close.
  PRIMARY: no support stop.
CONTROL: same opens, same time stop only (no S/R).
INFO: with support stop (close below nearest support btm -> next open; stop wins ties);
  F fills under the same rules.
HALVES: chronological. COST 0.20%. Measured RT = mean(gross-net) of signal.
PASS: both halves: (signal gross - control gross) > measured RT, n >= 50. Report net.
Any PASS = PENDING SKEPTIC REVIEW.
Run: .venv/bin/python tools/studies/sr_exit_resist.py
"""
from __future__ import annotations

import collections
import json
import math
import os
import pickle
import statistics
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))
import order_blocks as OB  # noqa: E402
from order_block_gate import series  # noqa: E402

ET = ZoneInfo("America/New_York")
IND = os.path.join(ROOT, "ai_reports", "indicator_levels")
OUT = os.path.join(ROOT, "ai_reports", "sr_exit_resist")
COST = 0.0020
HOLD = 30 * 60


def is_support(b):
    return (b.kind == "bull" and not b.breaker) or (b.kind == "bear" and b.breaker)


def is_resist(b):
    return (b.kind == "bear" and not b.breaker) or (b.kind == "bull" and b.breaker)


def charted_at(states, closes, tq):
    j = int(np.searchsorted(closes, tq, side="right")) - 1
    if j < 0:
        return None, j
    allb = [b for b in states[j][1] if b.known_ts <= tq]
    return OB.charted(allb), j


def run_exits(rows, i_entry, entry, target, stop_px, day_end, use_stop):
    """Returns (sig_gross, ctrl_gross, how) or (None, None, reason).
    ctrl always time-stops; sig uses target (+ optional stop)."""
    t0 = rows[i_entry][0]
    ctrl_j = None
    for j in range(i_entry, len(rows)):
        t, o, h, l, c = rows[j]
        if t >= day_end or t - t0 >= HOLD:
            ctrl_j = j - 1 if j > i_entry else None
            break
    if ctrl_j is None:
        return None, None, "no_ctrl_outcome"
    ctrl_g = rows[ctrl_j][4] / entry - 1

    for j in range(i_entry, len(rows)):
        t, o, h, l, c = rows[j]
        if t >= day_end or t - t0 >= HOLD:
            # time stop: same bar as control path
            return rows[ctrl_j][4] / entry - 1, ctrl_g, "time"
        hit_stop = use_stop and stop_px is not None and c < stop_px
        hit_tgt = h > target
        if hit_stop:
            exit_px = rows[j + 1][1] if j + 1 < len(rows) else c
            return exit_px / entry - 1, ctrl_g, "stop"
        if hit_tgt:
            return target / entry - 1, ctrl_g, "target"
    return None, None, "no_sig_outcome"


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
    cache = pickle.load(open(os.path.join(IND, "ext.pkl"), "rb"))
    cal = sorted({d for (_, d) in cache})
    prev_of = {d: cal[cal.index(d) - 1] if cal.index(d) else None for d in cal}

    arms = [r for r in R["rows"] if r["pop"] in ("E", "F") and r.get("t") and r.get("px")]
    by = collections.defaultdict(list)
    for r in arms:
        by[(r["sym"], r["day"])].append(r)

    prepared = {}  # (sym,day) -> (rows, states, closes, day_end)
    miss = collections.Counter()
    for (sym, day), rs_ in by.items():
        rows = series(cache, sym, day, prev_of.get(day))
        if len(rows) < 50:
            miss["short_series"] += len(rs_)
            continue
        states = list(OB.order_blocks(rows, bar_sec=60))
        closes = [rows[i][0] + 60 for i, _ in states]
        d0 = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
        day_end = d0.replace(hour=15, minute=55).timestamp()
        prepared[(sym, day)] = (rows, states, closes, day_end)

    def collect(pop, use_stop):
        trades, drops = [], collections.Counter(miss)
        for r in arms:
            if r["pop"] != pop:
                continue
            key = (r["sym"], r["day"])
            if key not in prepared:
                drops["no_bars"] += 1
                continue
            rows, states, closes, day_end = prepared[key]
            t_dec = float(r["t"])
            # entry = open of first bar whose open time >= decision (next bar after decision close)
            # decision is at close of bar; levels study uses next open. Find bar i with rows[i][0]+60 ~= t_dec
            j_dec = int(np.searchsorted(closes, t_dec, side="right")) - 1
            if j_dec < 0 or j_dec + 1 >= len(rows):
                drops["no_entry_bar"] += 1
                continue
            i_entry = j_dec + 1
            entry = rows[i_entry][1]
            t_entry = rows[i_entry][0]  # bar open time; blocks known by decision close (<= t_dec)
            ch, _ = charted_at(states, closes, t_dec)
            if ch is None:
                drops["no_blocks"] += 1
                continue
            resist_above = [b.btm for b in ch if is_resist(b) and b.btm > entry]
            if not resist_above:
                drops["no_resistance_above"] += 1
                continue
            target = min(resist_above)
            room = (target / entry - 1) * 100
            stop_px = None
            if use_stop:
                sup_below = [b.btm for b in ch if is_support(b) and b.top < entry]
                # nearest support below: highest top below entry; stop = its btm
                cands = [b for b in ch if is_support(b) and b.btm < entry]
                if cands:
                    S = max(cands, key=lambda b: b.top)
                    stop_px = S.btm
                else:
                    drops["no_support_below"] += 1
                    # still run without stop? prereg info says with stop — skip if none
                    continue
            sg, cg, how = run_exits(rows, i_entry, entry, target, stop_px, day_end, use_stop)
            if sg is None:
                drops[how] += 1
                continue
            trades.append({
                "sym": r["sym"], "day": r["day"], "pop": pop, "t": t_dec,
                "entry": entry, "target": target, "room": room, "how": how,
                "gross": sg, "net": sg - COST,
                "ctrl_gross": cg, "ctrl_net": cg - COST,
                "lift_gross": sg - cg,
            })
        return trades, drops

    primary, drops_p = collect("E", use_stop=False)
    info_stop, drops_s = collect("E", use_stop=True)
    info_f, drops_f = collect("F", use_stop=False)
    info_f_stop, drops_fs = collect("F", use_stop=True)

    json.dump({
        "primary": primary, "drops": dict(drops_p),
        "info_stop": info_stop, "info_f": info_f,
        "days": days, "half_A": days[:mid], "half_B": days[mid:],
        "prereg": "docs/studies/sr_exit_resist_prereg.json",
    }, open(os.path.join(OUT, "rows.json"), "w"))

    L = [
        "# Exit-only at resistance (prereg docs/studies/sr_exit_resist_prereg.json)",
        "",
        f"sample days {len(days)} ({days[0]}..{days[-1]}); chrono A {days[0]}..{days[mid-1]} ({mid}); "
        f"B {days[mid]}..{days[-1]} ({len(days)-mid})",
        f"PRIMARY E no-stop: paired {len(primary)}; drops {dict(drops_p)}",
        "",
    ]

    def summarize(label, trades, is_primary=False):
        nonlocal_res = {}
        for hh in ("A", "B", "all"):
            tr = [t for t in trades if hh == "all" or half[t["day"]] == hh]
            if len(tr) < 5:
                L.append(f"- {label} half {hh}: {len(tr)} (too few)")
                nonlocal_res[hh] = None
                continue
            sg = statistics.fmean([x["gross"] * 1e4 for x in tr])
            sn = statistics.fmean([x["net"] * 1e4 for x in tr])
            cg = statistics.fmean([x["ctrl_gross"] * 1e4 for x in tr])
            cn = statistics.fmean([x["ctrl_net"] * 1e4 for x in tr])
            rt = statistics.fmean([(x["gross"] - x["net"]) * 1e4 for x in tr])
            lifts = [x["lift_gross"] * 1e4 for x in tr]
            lm, lt = tstat(lifts, [x["day"] for x in tr])
            _, nt = tstat([x["net"] * 1e4 for x in tr], [x["day"] for x in tr])
            hit = sum(x["how"] == "target" for x in tr) / len(tr)
            stop_s = sum(x["how"] == "stop" for x in tr) / len(tr)
            win = sum(x["net"] > 0 for x in tr) / len(tr)
            L.append(
                f"- {label} half {hh}: n {len(tr)} on {len({x['day'] for x in tr})} days | "
                f"sig gross {sg:+.1f} / net {sn:+.1f} (t {nt:+.2f}) | "
                f"ctrl gross {cg:+.1f} / net {cn:+.1f} | "
                f"measured RT {rt:.1f} bp | "
                f"signal−control gross {lm:+.1f} bp (t {lt:+.2f}) | "
                f"lift>RT? {lm > rt if lm == lm else False} | "
                f"hit target {hit:.0%} stop {stop_s:.0%} win {win:.0%} | "
                f"median room {statistics.median(x['room'] for x in tr):.2f}%"
            )
            nonlocal_res[hh] = (len(tr), lm, rt, sn)
        return nonlocal_res

    res = summarize("PRIMARY E", primary, True)
    summarize("INFO E+stop", info_stop)
    summarize("INFO F", info_f)
    summarize("INFO F+stop", info_f_stop)

    if any(res[h] is None or res[h][0] < 50 for h in ("A", "B")):
        v = "UNDERPOWERED (fewer than 50 paired trades in a half): no verdict"
    elif all(res[h][1] == res[h][1] and res[h][1] > res[h][2] for h in ("A", "B")):
        v = "PASS — PENDING SKEPTIC REVIEW (not a live exit change; needs IEX confirmation)"
    else:
        v = "FAIL"

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
