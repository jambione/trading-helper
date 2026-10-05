#!/usr/bin/env python3
"""Breakout confirmation (not the first poke).
Pre-registration: docs/studies/sr_breakout_confirm_prereg.json (committed BEFORE this run).

Break: charted bearish OB becomes breaker. Control: buy next open (first poke).
Signal: within 15 min, close > broken top AND higher low vs break bar; buy next open.
Primary: paired events with both entries; lift = confirmed gross − poke gross.
Pass: both chrono halves lift > 20 bp RT, n >= 50. Report net.
Run: .venv/bin/python tools/studies/sr_breakout_confirm.py
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

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))
import order_blocks as OB  # noqa: E402
from order_block_gate import series  # noqa: E402

ET = ZoneInfo("America/New_York")
IND = os.path.join(ROOT, "ai_reports", "indicator_levels")
OUT = os.path.join(ROOT, "ai_reports", "sr_breakout_confirm")
COST = 0.0020
HOLD = 15 * 60
CONFIRM_WIN = 15 * 60


def et_min(ts):
    d = datetime.fromtimestamp(ts, ET)
    return d.hour * 60 + d.minute


def outcome(rows, i_entry, day_end):
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


def find_confirm(rows, i_brk, top, mode, vols):
    """Return j index of confirmation bar, or None. mode: hl, vol, either."""
    t_brk = rows[i_brk][0] + 60
    low_brk = rows[i_brk][3]
    for j in range(i_brk + 1, len(rows)):
        t_close = rows[j][0] + 60
        if t_close - t_brk > CONFIRM_WIN:
            return None
        if et_min(t_close) > 15 * 60:
            return None
        c, lo = rows[j][4], rows[j][3]
        if c <= top:
            continue
        hl_ok = lo > low_brk
        if mode == "hl":
            if hl_ok:
                return j
        elif mode == "vol":
            a = max(0, j - 20)
            base = vols[a:j]
            if base and vols[j] > statistics.fmean(base):
                return j
        else:  # either
            a = max(0, j - 20)
            base = vols[a:j]
            vol_ok = bool(base and vols[j] > statistics.fmean(base))
            if hl_ok or vol_ok:
                return j
    return None


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

    def collect(mode):
        pairs, poke_only, drops = [], [], collections.Counter()
        for sym, day in namedays:
            rows = series(cache, sym, day, prev_of.get(day))
            if len(rows) < 100:
                drops["short_series"] += 1
                continue
            # volume from cache df if present
            vols = []
            df = cache.get((sym, day))
            # rebuild volume aligned to rows of day+prev — approximate from day df only
            # ext rows don't carry volume in series(); pull from pickle bars
            vol_by_ts = {}
            for k in ((sym, prev_of.get(day)), (sym, day)):
                ddf = cache.get(k)
                if ddf is None or not len(ddf):
                    continue
                for t, r in ddf.iterrows():
                    vol_by_ts[t.timestamp()] = float(getattr(r, "volume", 0) or 0)
            vols = [vol_by_ts.get(r[0], 0.0) for r in rows]

            d0 = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
            day_end = d0.replace(hour=15, minute=55).timestamp()
            seen = set()
            prev_blocks = []
            for i, blocks in OB.order_blocks(rows, bar_sec=60):
                t_close = rows[i][0] + 60
                if datetime.fromtimestamp(rows[i][0], ET).strftime("%Y-%m-%d") != day:
                    prev_blocks = blocks
                    continue
                if not (9 * 60 + 40 <= et_min(t_close) <= 15 * 60):
                    prev_blocks = blocks
                    continue
                # detect new breakers among charted set
                ch = OB.charted([b for b in blocks if b.known_ts <= t_close])
                prev_ch = OB.charted([b for b in prev_blocks if b.known_ts <= t_close - 60]) if prev_blocks else []
                prev_keys = {(b.kind, round(b.top, 4), round(b.btm, 4), b.breaker) for b in prev_ch}
                for b in ch:
                    if not (b.kind == "bear" and b.breaker):
                        continue
                    key = (b.kind, round(b.top, 4), round(b.btm, 4), round(b.origin_ts, 0))
                    if key in seen:
                        continue
                    # newly broken on this bar?
                    was = any(
                        round(p.top, 4) == round(b.top, 4) and round(p.btm, 4) == round(b.btm, 4)
                        and p.kind == "bear" and not p.breaker
                        for p in prev_ch
                    )
                    just = b.break_ts is not None and abs(b.break_ts - t_close) < 1.0
                    if not (was or just):
                        # also accept if not in prev as unbroken
                        if (b.kind, round(b.top, 4), round(b.btm, 4), False) in prev_keys:
                            pass
                        elif not just:
                            continue
                    seen.add(key)
                    if i + 1 >= len(rows):
                        drops["no_poke_entry"] += 1
                        continue
                    pg = outcome(rows, i + 1, day_end)
                    if pg is None:
                        drops["no_poke_outcome"] += 1
                        continue
                    j = find_confirm(rows, i, b.top, mode, vols)
                    if j is None or j + 1 >= len(rows):
                        poke_only.append({
                            "sym": sym, "day": day, "gross": pg, "net": pg - COST, "mode": mode,
                        })
                        drops["no_confirm"] += 1
                        continue
                    cg = outcome(rows, j + 1, day_end)
                    if cg is None:
                        drops["no_confirm_outcome"] += 1
                        poke_only.append({
                            "sym": sym, "day": day, "gross": pg, "net": pg - COST, "mode": mode,
                        })
                        continue
                    pairs.append({
                        "sym": sym, "day": day,
                        "poke_gross": pg, "poke_net": pg - COST,
                        "conf_gross": cg, "conf_net": cg - COST,
                        "lift_gross": cg - pg, "delay_bars": j - i,
                        "mode": mode,
                    })
                prev_blocks = blocks
        return pairs, poke_only, drops

    pairs, poke_only, drops = collect("hl")
    info_vol, _, drops_v = collect("vol")
    info_either, _, drops_e = collect("either")

    json.dump({
        "pairs": pairs, "poke_only": poke_only, "drops": dict(drops),
        "days": days, "half_A": days[:mid], "half_B": days[mid:],
        "prereg": "docs/studies/sr_breakout_confirm_prereg.json",
    }, open(os.path.join(OUT, "rows.json"), "w"))

    L = [
        "# Breakout confirmation (prereg docs/studies/sr_breakout_confirm_prereg.json)",
        "",
        f"sample days {len(days)} ({days[0]}..{days[-1]}); chrono A {mid} days; B {len(days)-mid}",
        f"PRIMARY higher-low pairs {len(pairs)}; poke-only {len(poke_only)}; drops {dict(drops)}",
        "",
    ]
    res = {}

    def summarize(label, rows, store=False):
        for hh in ("A", "B", "all"):
            tr = [t for t in rows if hh == "all" or half[t["day"]] == hh]
            if len(tr) < 5:
                L.append(f"- {label} half {hh}: {len(tr)} (too few)")
                if store:
                    res[hh] = None
                continue
            cg = statistics.fmean([t["conf_gross"] * 1e4 for t in tr])
            cn = statistics.fmean([t["conf_net"] * 1e4 for t in tr])
            pg = statistics.fmean([t["poke_gross"] * 1e4 for t in tr])
            pn = statistics.fmean([t["poke_net"] * 1e4 for t in tr])
            rt = statistics.fmean([(t["conf_gross"] - t["conf_net"]) * 1e4 for t in tr])
            lifts = [t["lift_gross"] * 1e4 for t in tr]
            lm, lt = tstat(lifts, [t["day"] for t in tr])
            delay = statistics.fmean([t["delay_bars"] for t in tr])
            L.append(
                f"- {label} half {hh}: n {len(tr)} on {len({t['day'] for t in tr})} days | "
                f"confirmed gross {cg:+.1f} / net {cn:+.1f} | "
                f"poke gross {pg:+.1f} / net {pn:+.1f} | "
                f"lift {lm:+.1f} bp (t {lt:+.2f}) | RT {rt:.1f} | lift>RT? {lm > rt} | "
                f"mean delay {delay:.1f} bars"
            )
            if store:
                res[hh] = (len(tr), lm, rt, cn, pn)

    summarize("PRIMARY hl-paired", pairs, True)
    summarize("INFO vol-paired", info_vol)
    summarize("INFO either-paired", info_either)

    # poke-only info
    for hh in ("A", "B"):
        tr = [t for t in poke_only if half[t["day"]] == hh]
        if len(tr) < 5:
            L.append(f"- INFO poke-only (never confirm) half {hh}: {len(tr)} (too few)")
            continue
        L.append(
            f"- INFO poke-only half {hh}: n {len(tr)} | gross {statistics.fmean(t['gross']*1e4 for t in tr):+.1f} | "
            f"net {statistics.fmean(t['net']*1e4 for t in tr):+.1f}"
        )

    if any(res.get(h) is None or res[h][0] < 50 for h in ("A", "B")):
        v = "UNDERPOWERED (fewer than 50 paired events in a half): no verdict"
    elif all(res[h][1] > res[h][2] for h in ("A", "B")):
        v = "PASS — PENDING SKEPTIC REVIEW (not a live gate; needs IEX confirmation)"
    else:
        v = "FAIL"

    L.append("")
    L.append(
        f"**PRIMARY: {v}** (bar, both chronological halves: confirmed−poke gross > measured RT, "
        f"n >= 50; also report net)"
    )
    if res.get("A") and res.get("B"):
        for h in ("A", "B"):
            n, lm, rt, cn, pn = res[h]
            L.append(f"Half {h}: n {n}, lift {lm:+.1f} bp vs RT {rt:.1f} bp, conf net {cn:+.1f} / poke net {pn:+.1f} bp")

    open(os.path.join(OUT, "report.md"), "w").write("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
