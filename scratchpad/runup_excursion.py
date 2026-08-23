#!/usr/bin/env python3
"""If extended names lose, is it missing opportunity or unusable opportunity?

The run-up test came back with a trade-level rho of -0.139 (-2.59 sigma)
that did NOT reproduce at session level (6/9, p=0.51). Before deciding
whether that is a real effect measured badly or noise measured hopefully,
the mechanism has to be pinned, and MFE alone already hints at the answer:
the MOST extended bucket had the HIGHEST median MFE (0.057 R).

That is the opposite of the latency story. "Too late" means the move is
gone. A high MFE means the move is still there and the desk failed to
bank it — which is an exit-geometry problem wearing an entry costume.

So: MFE, MAE, and their ratio by run-up bucket. If MFE/MAE falls as
run-up rises, extended names are genuinely worse tape. If MFE/MAE holds
flat while MAE grows, they are the same tape at a bigger amplitude, and
the fix is shelf width, not admission speed.

Read-only. Mini + .venv.
"""
from __future__ import annotations

import json
import math
import os
import statistics
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import drift_screen as DS            # noqa: E402
import admission_latency as AL       # noqa: E402
from captured_vs_r import (          # noqa: E402
    anchor_metrics, binom_p, load_admits, load_fills, spearman)

DAYS = 20


def med(xs):
    return statistics.median(xs) if xs else float("nan")


def paired(rows: list[dict], key: str, outcome: str, label: str) -> None:
    byday = defaultdict(list)
    for r in rows:
        if r.get(key) is not None and r.get(outcome) is not None:
            byday[r["day"]].append(r)
    wins = n = 0
    deltas = []
    for d in sorted(byday):
        rs = sorted(byday[d], key=lambda r: r[key])
        if len(rs) < 6:
            continue
        h = len(rs) // 2
        lo = med([float(r[outcome]) for r in rs[:h]])
        hi = med([float(r[outcome]) for r in rs[-h:]])
        n += 1
        deltas.append(lo - hi)
        if lo > hi:
            wins += 1
    if not n:
        print(f"    {label}: no session with 6+ trades")
        return
    print(f"    {label}: fresh half wins {wins}/{n}  "
          f"(p={binom_p(wins, n):.3f})  median delta {med(deltas):+.4f}")


def main() -> int:
    fills = load_fills()
    admits = load_admits()
    syms = sorted({f["symbol"] for f in fills})
    from datetime import datetime, timedelta, timezone
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=DAYS + 10)
    minutes = DS.fetch_minutes(syms, start, end)
    if not minutes:
        print("no data client")
        return 0
    daily = AL.fetch_daily(syms, start.date().isoformat(), end.date().isoformat())

    rows = []
    for f in fills:
        b, d = minutes.get(f["symbol"]), daily.get(f["symbol"])
        if not b or not d:
            continue
        pc = AL.prior_close(d, f["day"])
        if not pc:
            continue
        a = admits.get((f["symbol"], f["day"]))
        f["before_admit"] = anchor_metrics(b, f["day"], a, pc)[0] if a else None
        if f["before_admit"] is None:
            continue
        if f.get("mfe_r") is None or f.get("mae_r") is None:
            continue
        f["mfe_r"] = float(f["mfe_r"])
        f["mae_r"] = abs(float(f["mae_r"]))
        rows.append(f)
    print(f"fills with run-up AND both excursions: {len(rows)}\n")

    print("=" * 78)
    print("EXCURSION BY PRE-ADMISSION RUN-UP")
    print("=" * 78)
    rows.sort(key=lambda r: r["before_admit"])
    q = len(rows) // 4
    print(f"  {'run-up bucket':<20}{'n':>5}{'medMFE':>9}{'medMAE':>9}"
          f"{'MFE/MAE':>9}{'medR':>9}{'hold s':>8}{'stomp%':>8}")
    for i in range(4):
        chunk = rows[i * q:(i + 1) * q] if i < 3 else rows[i * q:]
        mfe = [r["mfe_r"] for r in chunk]
        mae = [r["mae_r"] for r in chunk]
        hs = [float(r["hold_sec"]) for r in chunk if r.get("hold_sec") is not None]
        stomp = [1 for r in chunk
                 if r.get("hold_sec") is not None and float(r["hold_sec"]) < 10]
        ratio = (med(mfe) / med(mae)) if med(mae) else float("nan")
        lo, hi = chunk[0]["before_admit"], chunk[-1]["before_admit"]
        span = f"{lo:.1f}..{hi:.1f}%"
        print(f"  {span:<20}"
              f"{len(chunk):>5}{med(mfe):>9.3f}{med(mae):>9.3f}{ratio:>9.2f}"
              f"{med([r['r'] for r in chunk]):>9.3f}{med(hs):>8.0f}"
              f"{100.0 * len(stomp) / len(chunk):>8.0f}")

    print("\n" + "=" * 78)
    print("IS THE RUN-UP EFFECT REAL? session-paired on three outcomes")
    print("=" * 78)
    for outcome, lbl in (("r", "realized R"), ("mfe_r", "MFE available"),
                         ("mae_r", "MAE suffered")):
        print(f"  {lbl}:")
        paired(rows, "before_admit", outcome, "    all")
        for src in ("momentum", "trending"):
            rs = [r for r in rows if str(r["source"]) == src]
            if len(rs) >= 40:
                paired(rs, "before_admit", outcome, f"    {src:<10}")
        print()

    print("=" * 78)
    print("RANK CORRELATION of run-up with each excursion")
    print("=" * 78)
    for outcome in ("r", "mfe_r", "mae_r"):
        pairs = [(r["before_admit"], float(r[outcome])) for r in rows]
        rho, t, n = spearman([p[0] for p in pairs], [p[1] for p in pairs])
        print(f"  run-up vs {outcome:<8} rho={rho:+.3f}  sigma={t:+.2f}  n={n}")
    print("\n  A flat MFE/MAE with rising MAE says the tape is the same shape")
    print("  at a bigger amplitude — a shelf-width problem, not a clock problem.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
