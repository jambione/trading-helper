#!/usr/bin/env python3
"""hist_sim_read.py — the ONE read of docs/studies/hist_sim_prereg.json (scored with tools/studies/std_scorer.py).

Inputs (mini, written by scripts/hist_sim_batch.sh):
  DIR/DAY-VARIANT.json      synthetic-day replay output ({"closed": [{symbol, entry_ts, exit_ts, ret, reason}], "vacuity": {...}})
  DIR/DAY-dayinfo.json      {"spy_missing_share": x, "universe": n, "fetch_failed": k}  (exclusion criteria b, c)
  DIR/FIDELITY.json         calibration-day fidelity A/B verdict + measured power, written BEFORE the held-out batch
Order check: FIDELITY.json must be older than every held-out replay file, and its verdict must be PASS, else no verdict.
Costing: net = ret - full SIP spread at entry (replay_costing), charged once; no NBBO -> that day's pooled median across
variants (counted); a day with > 10% no-NBBO trades in ANY variant is excluded for ALL variants (criterion d).
USAGE (mini, repo root): .venv/bin/python tools/studies/hist_sim_read.py [--dir /tmp/hs]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import statistics
import sys

ROOT = os.getcwd()
sys.path[:0] = [os.path.join(ROOT, "tools", "studies"), os.path.join(ROOT, "tools"), ROOT]
import std_scorer as S  # noqa: E402

LO, HI, MID, LEAST_SEEN_HI = "2026-03-02", "2026-09-23", "2026-06-11", "2026-04-30"
VARS = ("base", "wr15", "wrrsi", "st", "nodecay", "wrrsi_st", "rand_wrrsi")
SECONDARY = (("wr15", "base"), ("wrrsi", "base"), ("st", "base"), ("nodecay", "base"), ("wrrsi", "rand_wrrsi"))
HALF_DAYS: set[str] = set()   # none inside 2026-03-02..09-23 (07-03 is a full holiday, not a half day)


def per_day_bp(trades, days):
    out = {}
    for d in days:
        xs = [t["net_bp"] for t in trades if t["day"] == d]
        if xs:
            out[d] = statistics.fmean(xs)
    return out


def dollars(trades, days):
    return {d: sum(t["net_bp"] for t in trades if t["day"] == d) / 1e4 * 1000 for d in days}


def paired_bp(a, b, days):
    pa, pb = per_day_bp(a, days), per_day_bp(b, days)
    return {d: pa[d] - pb[d] for d in days if d in pa and d in pb}


def cost_day(raw: dict, spread_of) -> tuple[dict, dict]:
    """raw: variant -> closed list. -> (variant -> trades with net_bp, info). spread_of(sym, ts) -> bp or None."""
    rows, spreads, miss = {}, [], {}
    for v, closed in raw.items():
        rows[v] = []
        for t in closed:
            if t.get("ret") is None:
                continue
            s = spread_of(t["symbol"], float(t["entry_ts"]))
            if s is not None:
                spreads.append(s)
            rows[v].append((t, s))
    med = statistics.median(spreads) if spreads else None
    out = {}
    for v, xs in rows.items():
        miss[v] = sum(1 for _, s in xs if s is None)
        out[v] = [{"symbol": t["symbol"], "entry_ts": float(t["entry_ts"]), "reason": t.get("reason"),
                   "net_bp": t["ret"] * 1e4 - (s if s is not None else med)} for t, s in xs if s is not None or med is not None]
    share = {v: (miss[v] / len(rows[v]) if rows[v] else 0.0) for v in rows}
    return out, {"no_nbbo": miss, "no_nbbo_share": share, "median": med}


def excluded(day: str, info: dict | None, cost_info: dict) -> str | None:
    if day in HALF_DAYS:
        return "a_half_day"
    if info is None:
        return "no_dayinfo"
    if float(info.get("spy_missing_share", 1.0)) > 0.05:
        return "b_spy_bars"
    u = max(1, int(info.get("universe") or 0))
    if int(info.get("fetch_failed") or 0) / u > 0.10:
        return "c_fetch_failed"
    if any(s > 0.10 for s in cost_info["no_nbbo_share"].values()):
        return "d_no_nbbo"
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="/tmp/hs")
    a = ap.parse_args()
    fid_p = os.path.join(a.dir, "FIDELITY.json")
    if not os.path.exists(fid_p):
        print("NO VERDICT: FIDELITY.json missing (the fidelity gate must be written before the held-out batch)")
        return
    fid = json.load(open(fid_p))
    files = sorted(glob.glob(os.path.join(a.dir, "20??-??-??-*.json")))
    held = [p for p in files if LO <= os.path.basename(p)[:10] <= HI and not p.endswith("-dayinfo.json")]
    if held and min(os.path.getmtime(p) for p in held) < os.path.getmtime(fid_p):
        print("NO VERDICT: a held-out replay is older than FIDELITY.json (order violated)")
        return
    print(f"fidelity: {fid.get('verdict')} | power: {fid.get('power')}")
    if fid.get("verdict") != "PASS":
        print("NOT SCORED under this prereg: the fidelity gate failed (numbers above). A new prereg is required.")
        return
    import replay_costing as RC
    cache, box = RC.load_cache(), {}
    spread_of = lambda s, ts: RC.entry_spread_bp(cache, s, ts, box)  # noqa: E731
    days = sorted({os.path.basename(p)[:10] for p in held})
    trades = {v: [] for v in VARS}
    excl, incomplete, vac, rand_invalid = {}, [], {v: [] for v in VARS}, []
    for d in days:
        raw = {}
        for v in VARS:
            p = os.path.join(a.dir, f"{d}-{v}.json")
            raw[v] = json.load(open(p)) if os.path.exists(p) else None
        if any(raw[v] is None for v in VARS):
            incomplete.append(d)
            continue
        info_p = os.path.join(a.dir, f"{d}-dayinfo.json")
        info = json.load(open(info_p)) if os.path.exists(info_p) else None
        costed, cinfo = cost_day({v: raw[v].get("closed") or [] for v in VARS}, spread_of)
        why = excluded(d, info, cinfo)
        if why:
            excl[d] = why
            continue
        key = lambda xs: [(t["symbol"], t["entry_ts"], t.get("exit_ts")) for t in xs]  # noqa: E731
        for v in VARS:
            if v != "base" and key(raw[v].get("closed") or []) == key(raw["base"].get("closed") or []):
                vac[v].append(d)
            for t in costed[v]:
                trades[v].append({**t, "day": d})
        nw, nr = len(costed["wrrsi"]), len(costed["rand_wrrsi"])
        if not nw or abs(nr - nw) > 0.15 * nw:
            rand_invalid.append(d)
    RC.save_cache(cache)
    scored = [d for d in days if d not in excl and d not in incomplete]
    print(f"held-out days found {len(days)}; scored {len(scored)}; excluded {excl}; incomplete {incomplete}")
    print("trades: " + ", ".join(f"{v} {len(trades[v])}" for v in VARS) + f" | vacuous days {{v: n}} {{{', '.join(f'{v}: {len(x)}' for v, x in vac.items() if x)}}}")

    P = "wrrsi_st"
    bp = per_day_bp(trades[P], scored)
    dl = dollars(trades[P], scored)
    pr = paired_bp(trades[P], trades["base"], scored)
    abs_bp, abs_dl, par = S.day_stat([bp[d] for d in sorted(bp)]), S.day_stat([dl[d] for d in scored]), S.day_stat([pr[d] for d in sorted(pr)])
    same_entry = S.paired_variants(trades["base"], trades[P], scored)

    def sub(lo, hi):
        ds = [d for d in scored if lo <= d <= hi]
        b = per_day_bp(trades[P], ds)
        p = paired_bp(trades[P], trades["base"], ds)
        return S.day_stat([b[x] for x in sorted(b)]), S.day_stat([p[x] for x in sorted(p)]), len(ds)

    h1, h2, ls = sub(LO, MID), sub("2026-06-12", HI), sub(LO, LEAST_SEEN_HI)
    print(f"\nPRIMARY {P}: abs net {abs_bp['mean']:+.2f} bp/trade (t {abs_bp['t']:+.2f}, {abs_bp['n']} days); $/day {abs_dl['mean']:+.2f} "
          f"(t {abs_dl['t']:+.2f}); vs base per-day {par['mean']:+.2f} bp (t {par['t']:+.2f}, {par['n']} days); same-entry "
          f"{same_entry['paired']['mean']:+.2f} ({same_entry['matched']} matched)")
    print(f"  halves abs {h1[0]['mean']:+.2f} / {h2[0]['mean']:+.2f}; least-seen 03-02..04-30 ({ls[2]} days) abs {ls[0]['mean']:+.2f} "
          f"paired {ls[1]['mean']:+.2f}")
    base_bp = S.day_stat(list(per_day_bp(trades["base"], scored).values()))
    print(f"  base abs net {base_bp['mean']:+.2f} bp/trade (t {base_bp['t']:+.2f})")
    conds = {
        "1_abs": abs_bp["mean"] > 0 and abs_bp["t"] >= 2 and abs_dl["mean"] > 0 and abs_dl["t"] >= 2,
        "2_vs_base": par["mean"] >= 3 and par["t"] >= 2,
        "3_halves": h1[0]["mean"] > 0 and h2[0]["mean"] > 0,
        "4_least_seen": ls[0]["mean"] > 0 and ls[1]["mean"] > 0,
        "5_size": len(scored) >= 60 and len(trades[P]) >= 600,
    }
    print(f"  conditions {conds}")
    print(f"  VERDICT: {'PASS (skeptic review next, then a live paper A/B)' if all(conds.values()) else ('UNDERPOWERED = FAIL' if not conds['5_size'] else 'FAIL')}")

    print("\nSECONDARY (better-than claims need t >= 2.6 and the same sign in both halves):")
    for v, ref in SECONDARY:
        ds = [d for d in scored if not (ref == "rand_wrrsi" and d in rand_invalid)]
        p = paired_bp(trades[v], trades[ref], ds)
        st_all = S.day_stat([p[x] for x in sorted(p)])
        hs = [S.day_stat([p[x] for x in sorted(p) if lo <= x <= hi])["mean"] for lo, hi in ((LO, MID), ("2026-06-12", HI))]
        ok = st_all["t"] >= 2.6 and all(h > 0 for h in hs) and st_all["mean"] > 0
        print(f"  {v:9s} - {ref:10s} {st_all['mean']:+6.2f} bp (t {st_all['t']:+.2f}, {st_all['n']} days) halves {hs[0]:+.2f}/{hs[1]:+.2f} "
              f"{'BETTER' if ok else '-'}")
    print(f"  rand_wrrsi invalid days (count off by > 15%): {len(rand_invalid)}")


if __name__ == "__main__":
    main()
