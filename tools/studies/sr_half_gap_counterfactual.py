#!/usr/bin/env python3
"""What-if: rising-%R squares, sell halfway to resistance or at 15:55.

Pre-registered: docs/studies/sr_half_gap_counterfactual_prereg.json
COUNTERFACTUAL on the already-mined 2026-09-04..10-02 tape. No pass bar.
No bot_config. No fetch.

  TH_ROOT=~/repo/trading-helper .venv/bin/python tools/studies/sr_half_gap_counterfactual.py
  .venv/bin/python tools/studies/sr_half_gap_counterfactual.py --self-test
"""
from __future__ import annotations

import collections
import math
import os
import pickle
import random
import statistics
import sys
from datetime import datetime

import numpy as np

ROOT = os.environ.get("TH_ROOT") or os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))
import order_blocks as OB  # noqa: E402
from order_block_gate import series  # noqa: E402
from sr_exit_resist import ET, IND, charted_at, is_resist  # noqa: E402

COST = 0.0020
WIDE = 0.010
MODEST = 0.003
SEED = 51
REDRAWS = 30


def half_px(entry, resist):
    return entry + 0.5 * (resist - entry)


def walk(rows, i_entry, entry, target, day_end):
    """Gross return to a through-print at target, else the 15:55 close.
    A bar whose open time is >= day_end is not traded."""
    # The entry bar's high is not known at the open, so the target can fill
    # only on a later bar. The time exit may still use the entry bar's close.
    if i_entry >= len(rows) or rows[i_entry][0] >= day_end:
        return None, "no_bar"
    last = i_entry
    for j in range(i_entry + 1, len(rows)):
        t, _o, h, _l, _c = rows[j]
        if t >= day_end:
            break
        last = j
        if h > target:
            return target / entry - 1, "target"
    return rows[last][4] / entry - 1, "close"


def tstat(xs, days):
    if len(xs) < 2:
        return float("nan")
    m = statistics.fmean(xs)
    by = collections.defaultdict(float)
    for v, d in zip(xs, days):
        by[d] += v - m
    g = len(by)
    if g < 2:
        return float("nan")
    se = math.sqrt(sum(s * s for s in by.values()) * g / (g - 1)) / len(xs)
    return m / se if se > 0 else float("nan")


def resist_of(rows, states, closes, j_dec):
    """(i_entry, entry, resist_bottom) or None. Levels known at the decision close."""
    if j_dec < 0 or j_dec + 1 >= len(rows):
        return None
    i_entry = j_dec + 1
    entry = rows[i_entry][1]
    if not entry or entry <= 0:
        return None
    t_dec = closes[j_dec]
    ch, _ = charted_at(states, closes, t_dec)
    if ch is None:
        return None
    above = [b.btm for b in ch if is_resist(b) and b.btm > entry]
    if not above:
        return None
    return i_entry, entry, min(above)


def et_hour(ts):
    d = datetime.fromtimestamp(ts, ET)
    return d.hour


def minute_of(ts):
    d = datetime.fromtimestamp(ts, ET)
    return d.hour * 60 + d.minute


def self_test():
    # Halfway price.
    assert math.isclose(half_px(100.0, 110.0), 105.0)
    # Touch fills at the half target. 15:55 is far away.
    rows = [
        (0, 100, 100, 100, 100),
        (60, 100, 104, 99, 103),
        (120, 103, 106, 102, 105),
        (180, 105, 105, 104, 104),
    ]
    g, how = walk(rows, 1, 100.0, 105.0, 10_000)
    assert how == "target" and math.isclose(g, 0.05), (g, how)
    # No touch: last in-session close.
    g, how = walk(rows, 1, 100.0, 110.0, 10_000)
    assert how == "close" and math.isclose(g, 0.04), (g, how)
    # day_end cuts the path before the touch.
    g, how = walk(rows, 1, 100.0, 105.0, 120)
    assert how == "close" and math.isclose(g, 0.03), (g, how)
    # A bar that has not opened yet is not an exit.
    g, how = walk(rows, 1, 100.0, 110.0, 60)
    assert g is None and how == "no_bar"
    print("self-test ok")


def band_of(room):
    if room >= WIDE:
        return "wide"
    if room >= MODEST:
        return "modest"
    return "wall"


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        self_test()
        return
    import json
    path = os.path.join(IND if os.path.isabs(IND) else os.path.join(ROOT, "ai_reports", "indicator_levels"), "rows.json")
    # sr_exit_resist.IND is rooted at that file's repo, which is wrong if we set TH_ROOT
    # and import the module from elsewhere. Always read via ROOT.
    ind = os.path.join(ROOT, "ai_reports", "indicator_levels")
    R = json.load(open(os.path.join(ind, "rows.json")))
    days = list(R["days"])
    mid = len(days) // 2
    half = {d: ("A" if k < mid else "B") for k, d in enumerate(days)}
    cache = pickle.load(open(os.path.join(ind, "ext.pkl"), "rb"))
    cal = sorted({d for (_, d) in cache})
    prev_of = {d: cal[cal.index(d) - 1] if cal.index(d) else None for d in cal}

    arms = [r for r in R["rows"] if r.get("pop") == "E" and r.get("t") and r.get("px")]
    by = collections.defaultdict(list)
    for r in arms:
        by[(r["sym"], r["day"])].append(r)

    prepared = {}
    for n, key in enumerate(by):
        sym, day = key
        rows = series(cache, sym, day, prev_of.get(day))
        if len(rows) < 50:
            continue
        states = list(OB.order_blocks(rows, bar_sec=60))
        closes = [rows[i][0] + 60 for i, _ in states]
        d0 = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
        day_end = d0.replace(hour=15, minute=55).timestamp()
        prepared[key] = (rows, states, closes, day_end)
        if n and n % 100 == 0:
            print(f"prepared {n}/{len(by)}", flush=True)

    def quote(rows, states, closes, day_end, j_dec):
        got = resist_of(rows, states, closes, j_dec)
        if got is None:
            return None
        i_entry, entry, resist = got
        if rows[i_entry][0] >= day_end:
            return None
        room = resist / entry - 1
        if room <= 0:
            return None
        hg, hhow = walk(rows, i_entry, entry, half_px(entry, resist), day_end)
        fg, _ = walk(rows, i_entry, entry, resist, day_end)
        if hg is None or fg is None:
            return None
        return {
            "room": room, "half_g": hg, "full_g": fg, "how": hhow,
            "hour": et_hour(closes[j_dec]), "j": j_dec,
        }

    trades = []
    drops = collections.Counter()
    cands = {}  # (sym, day, hour) -> [j_dec]
    for r in arms:
        key = (r["sym"], r["day"])
        if key not in prepared:
            drops["no_bars"] += 1
            continue
        rows, states, closes, day_end = prepared[key]
        j_dec = int(np.searchsorted(closes, float(r["t"]), side="right")) - 1
        q = quote(rows, states, closes, day_end, j_dec)
        if q is None:
            drops["no_gap"] += 1
            continue
        b = band_of(q["room"])
        if b == "wall":
            drops["wall"] += 1
            continue
        trades.append({
            "sym": r["sym"], "day": r["day"], "half": half[r["day"]], "band": b,
            "room": q["room"], "half_g": q["half_g"], "full_g": q["full_g"],
            "how": q["how"], "hour": q["hour"], "j": q["j"],
        })
        cands.setdefault((r["sym"], r["day"], q["hour"]), [])

    # Candidate minutes: every in-range bar close in that name-day, built once per name-day.
    bar_cands = {}
    for (sym, day), (rows, states, closes, day_end) in prepared.items():
        js = []
        for j, t_close in enumerate(closes):
            m = minute_of(t_close)
            if 9 * 60 + 40 <= m <= 15 * 60 + 30 and j + 1 < len(rows):
                js.append(j)
        bar_cands[(sym, day)] = js

    rng = random.Random(SEED)

    def random_peer(tr):
        rows, states, closes, day_end = prepared[(tr["sym"], tr["day"])]
        pool = [j for j in bar_cands[(tr["sym"], tr["day"])]
                if et_hour(closes[j]) == tr["hour"] and j != tr["j"]]
        if not pool:
            return None
        for _ in range(REDRAWS):
            j = pool[rng.randrange(len(pool))]
            q = quote(rows, states, closes, day_end, j)
            if q is None:
                continue
            if q["room"] < MODEST:
                continue
            return q
        return None

    # Attach peers in a stable order so seed 51 is reproducible.
    trades.sort(key=lambda tr: (tr["day"], tr["sym"], tr["j"]))
    for tr in trades:
        peer = random_peer(tr)
        if peer is None:
            tr["peer_g"] = None
        else:
            tr["peer_g"] = peer["half_g"]

    def line(label, xs, days):
        if len(xs) < 2:
            return f"{label}: n {len(xs)}"
        mu = statistics.fmean(xs) * 1e4
        tv = tstat([x * 1e4 for x in xs], days)
        return f"{label}: n {len(xs)}  {mu:+.1f} bp  t {tv:+.2f}"

    print("COUNTERFACTUAL — not a pass. Tape 2026-09-04..10-02, already mined.")
    print(f"days {days[0]}..{days[-1]}  A {days[0]}..{days[mid - 1]}  B {days[mid]}..{days[-1]}")
    print(f"squares kept {len(trades)}  drops {dict(drops)}")
    print(f"wide n {sum(t['band']=='wide' for t in trades)}  "
          f"modest n {sum(t['band']=='modest' for t in trades)}")
    print("exit: halfway to resistance, else 15:55. cost 20 bp. no pass bar.")
    print()
    for hh in ("A", "B"):
        sub = [t for t in trades if t["half"] == hh]
        print(f"== half {hh} ==")
        for band in ("wide", "modest"):
            tr = [t for t in sub if t["band"] == band]
            paired = [t for t in tr if t["peer_g"] is not None]
            lifts = [t["half_g"] - t["peer_g"] for t in paired]
            print(line(f"  {band} half-gap gross", [t["half_g"] for t in tr], [t["day"] for t in tr]))
            print(line(f"  {band} half-gap net", [t["half_g"] - COST for t in tr], [t["day"] for t in tr]))
            if tr:
                hit = sum(t["how"] == "target" for t in tr) / len(tr)
                med = statistics.median(t["room"] for t in tr) * 100
                print(f"    hit halfway {hit:.0%}  median room {med:.2f}%  days {len({t['day'] for t in tr})}")
            print(line(f"  {band} minus random", lifts, [t["day"] for t in paired]))
            if paired:
                print(line(f"    random gross", [t["peer_g"] for t in paired], [t["day"] for t in paired]))
        wide = [t for t in sub if t["band"] == "wide"]
        modest = [t for t in sub if t["band"] == "modest"]
        if wide and modest:
            dw = statistics.fmean(t["half_g"] for t in wide) - statistics.fmean(t["half_g"] for t in modest)
            print(f"  wide minus modest gross {dw * 1e4:+.1f} bp  (unpaired means)")
        if wide:
            gap = [t["full_g"] - t["half_g"] for t in wide]
            print(line("  INFO wide full-gap minus half-gap", gap, [t["day"] for t in wide]))
        print()
    print("END counterfactual. Do not ship. Do not change bot_config.")


if __name__ == "__main__":
    main()
