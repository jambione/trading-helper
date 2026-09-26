#!/usr/bin/env python3
"""edge4_report.py — print Round-4 tables from ai_reports/edge4/cells_{L,C,S1}.json and apply the
pre-registered promotion rule (docs/studies/edge4_prereg.json). Holdout rows are printed ONLY for
promoted cells (max 5 per universe, by pooled t)."""
from __future__ import annotations

import json
import os
import sys

ROOT = os.environ.get("REPO") or os.getcwd()
E4 = os.path.join(ROOT, "ai_reports", "edge4")
HO = ("15", "30", "60", "120", "eod")


def f(x, w=6, p=1, sign=True):
    if x is None:
        return " " * (w - 1) + "-"
    return f"{x:+{w}.{p}f}" if sign else f"{x:{w}.{p}f}"


def line(c):
    return (f"n {c['n']:5d} ({c['per_day']:.2f}/d) spr {c['spr']:4.1f} gross {f(c['gross'])} net {f(c['net'])} "
            f"med {f(c['median'])} win {c['win']:4.0%} t {f(c['t'],5,2)} CI [{f(c['lo'])},{f(c['hi'])}] "
            f"| rand {f(c['rand'])} excess {f(c['excess'])} (t {f(c['t_excess'],5,2)}) sameday {f(c['same_day'])}")


def report_p(U):
    p = os.path.join(E4, f"cells_P_{U}{os.environ.get('E4_SUFFIX', '')}.json")
    if not os.path.exists(p):
        print(f"\n## P {U}: no cells"); return
    cells = json.load(open(p))
    idx = {(c["d"], c["W"], c["hold"], c["part"]): c for c in cells}
    tv = "tune+validate" if U == "L" else "tune"
    print(f"\n## Test P (pullback limit after the arm), universe {U}: per ARM / per FILL / market on same arms / random-time limit")
    print(f"{'d':8} {'W':>2} {'hold':>4} | {'part':13} {'arms':>6} {'fill':>5} {'perARM':>6} {'t':>5} {'perFILL':>7} {'grossF':>6} "
          f"| {'mkt':>6} {'t':>5} {'mktG':>5} {'mktG|fill':>9} {'mktG|nofill':>11} | {'rFill':>5} {'rPerF':>6} {'rGrossF':>7} {'rPerAtt':>7} {'ex':>5} {'t_ex':>5} | promo")
    promos = []
    ds = sorted({c["d"] for c in cells}, key=lambda x: [c["d"] for c in cells].index(x))
    for d in ds:
        for W in (2, 5, 10):
            for h in ("15", "30", "60", "eod"):
                parts = ["tune", "validate", "tune+validate"] if U == "L" else ["tune"]
                cs = [idx.get((d, W, h, q)) for q in parts]
                if any(c is None for c in cs):
                    continue
                if U == "L":
                    ok = cs[0]["per_arm"] > 0 and cs[1]["per_arm"] > 0 and cs[2]["t_arm"] >= 2 and (cs[2]["excess_vs_rand"] or -1) > 0
                else:
                    ok = cs[0]["per_arm"] > 0 and cs[0]["t_arm"] >= 2 and (cs[0]["excess_vs_rand"] or -1) > 0
                if ok:
                    promos.append((cs[-1]["t_arm"], d, W, h))
                for q, c in zip(parts, cs):
                    print(f"{d:8} {W:2d} {h:>4} | {q:13} {c['arms']:6d} {c['fill']:5.0%} {f(c['per_arm'])} {f(c['t_arm'],5,2)} "
                          f"{f(c['per_fill'],7)} {f(c['gross_fill'])} | {f(c['mkt'])} {f(c['t_mkt'],5,2)} {f(c['mkt_gross'],5)} "
                          f"{f(c['mkt_gross_filled'],9)} {f(c['mkt_gross_unfilled'],11)} | {c['rand_fill'] or 0:5.0%} {f(c['rand_per_fill'])} "
                          f"{f(c['rand_gross_fill'],7)} {f(c['rand_per_attempt'],7)} {f(c['excess_vs_rand'],5)} {f(c['t_excess'],5,2)} | "
                          f"{'PROMOTE' if ok and q == parts[-1] else ''}")
    print(f"Promoted P cells: {len(promos)}")
    for t, d, W, h in sorted(promos, reverse=True)[:5]:
        c = idx.get((d, W, h, "holdout"))
        print(f"  HOLDOUT {d} W{W} {h}: " + (f"arms {c['arms']} fill {c['fill']:.0%} perARM {c['per_arm']:+.1f} (t {c['t_arm']:+.2f}) "
              f"perFILL {c['per_fill']:+.1f} mkt {c['mkt']:+.1f} randPerAtt {c['rand_per_attempt']:+.1f}" if c else "none"))


def main():
    if sys.argv[1:2] == ["P"]:
        for U in sys.argv[2:] or ("L", "C"):
            report_p(U)
        return
    for U in sys.argv[1:] or ("L", "C", "S1"):
        p = os.path.join(E4, f"cells_{U}{os.environ.get('E4_SUFFIX', '')}.json")
        if not os.path.exists(p):
            print(f"\n## {U}: no cells"); continue
        cells = json.load(open(p))
        idx = {(c["feed"], c["sig"], c["dir"], c["hold"], c["part"]): c for c in cells}
        sigs = sorted({(c["feed"], c["sig"]) for c in cells}, key=lambda x: (x[1], x[0]))
        hold_part = "tune+validate" if U != "C" else "tune"
        print(f"\n## Universe {U} — LONG cells (net bp/trade; t day-clustered)")
        if U != "C":
            print(f"{'signal':24} {'feed':4} {'hold':>4} | {'tune n':>6} {'net':>6} {'t':>5} | {'val n':>5} {'net':>6} {'t':>5} | "
                  f"{'T+V t':>5} {'excess':>6} {'t_ex':>5} {'rand':>6} {'sameday':>7} | promo")
        else:
            print(f"{'signal':24} {'feed':4} {'hold':>4} | {'tune n':>6} {'gross':>6} {'net':>6} {'t':>5} {'excess':>6} {'t_ex':>5} "
                  f"{'rand':>6} {'sameday':>7} | promo")
        promos = []
        tstats = []
        for feed, sig in sigs:
            for h in HO:
                tu = idx.get((feed, sig, 1, h, "tune"))
                if tu is None:
                    continue
                if U != "C":
                    va = idx.get((feed, sig, 1, h, "validate"))
                    tv = idx.get((feed, sig, 1, h, "tune+validate"))
                    if va is None or tv is None:
                        continue
                    ok = tu["net"] > 0 and va["net"] > 0 and tv["t"] >= 2 and (tv["excess"] or -1) > 0
                    tstats.append(tv["t"])
                    print(f"{sig:24} {feed:4} {h:>4} | {tu['n']:6d} {f(tu['net'])} {f(tu['t'],5,2)} | {va['n']:5d} {f(va['net'])} "
                          f"{f(va['t'],5,2)} | {f(tv['t'],5,2)} {f(tv['excess'])} {f(tv['t_excess'],5,2)} {f(tv['rand'])} "
                          f"{f(tv['same_day'],7)} | {'PROMOTE' if ok else ''}")
                    if ok:
                        promos.append((tv["t"], feed, sig, h))
                else:
                    ok = tu["net"] > 0 and tu["t"] >= 2 and (tu["excess"] or -1) > 0
                    tstats.append(tu["t"])
                    print(f"{sig:24} {feed:4} {h:>4} | {tu['n']:6d} {f(tu['gross'])} {f(tu['net'])} {f(tu['t'],5,2)} "
                          f"{f(tu['excess'])} {f(tu['t_excess'],5,2)} {f(tu['rand'])} {f(tu['same_day'],7)} | {'PROMOTE' if ok else ''}")
                    if ok:
                        promos.append((tu["t"], feed, sig, h))
        import math
        tt = [t for t in tstats if t == t]
        print(f"\nLong cells: {len(tt)}; t>=2: {sum(t >= 2 for t in tt)}, t<=-2: {sum(t <= -2 for t in tt)}, "
              f"net>0 pooled: {sum(t > 0 for t in tt)}  (under pure noise ~2.3% each tail)")
        print(f"Promoted (pre-registered rule): {len(promos)}")
        for t, feed, sig, h in sorted(promos, reverse=True)[:5]:
            print(f"  HOLDOUT {sig} [{feed}] hold {h}: ", end="")
            c = idx.get((feed, sig, 1, h, "holdout"))
            print(line(c) if c else "no holdout trades")
        print(f"\n### {U} short mirror (info; {hold_part} pooled): cells with t >= 2")
        for c in cells:
            if c["dir"] == -1 and c["part"] == hold_part and c["t"] == c["t"] and c["t"] >= 2:
                print(f"  {c['sig']:24} {c['feed']:4} {c['hold']:>4} {line(c)}")
        print(f"\n### {U} detail ({hold_part} pooled, long)")
        for feed, sig in sigs:
            for h in HO:
                c = idx.get((feed, sig, 1, h, hold_part))
                if c:
                    print(f"  {sig:24} {feed:4} {h:>4} {line(c)}")
        if U in ("L", "C"):
            print(f"\n### {U} SIP vs IEX detection (long, {hold_part} pooled): n / gross / net / t")
            for sig in sorted({s for _f, s in sigs}):
                row = []
                for h in HO:
                    a, b = idx.get(("sip", sig, 1, h, hold_part)), idx.get(("iex", sig, 1, h, hold_part))
                    row.append(f"{h}: " + (f"S {a['n']}/{a['gross']:+.1f}/{a['net']:+.1f}/{a['t']:+.1f}" if a else "S -") + "  " +
                               (f"I {b['n']}/{b['gross']:+.1f}/{b['net']:+.1f}/{b['t']:+.1f}" if b else "I -"))
                print(f"  {sig:22} " + " | ".join(row))


if __name__ == "__main__":
    main()
