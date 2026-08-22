#!/usr/bin/env python3
"""Ratchet-as-harvest: separate shelf-stomp from a real run.

The operator thesis: %R/RSI only need to be directionally right; the 0.10R
shelf captures the move. Friday freeze left a shelf that sat on the print /
inside the spread, so instant kills are not a fair test of that idea.

This file, read-only:

  1. Split closed outcomes into STOMP (hold < 10s), FAST (10-30s), HARVEST
     (hold >= 30s). Report R / MFE / wins per bucket and per session.
  2. When spread_r is on the row: INSIDE-BOOK if spread_r > give_r (the whole
     cushion fits in one round trip). Those are the stomp-prone fills.
  3. Counterfactual: drop STOMP, and drop INSIDE-BOOK. Verdict uses the same
     session bar as desk_null (not one green afternoon).

Does not write bot_config. Does not arm.

    .venv/bin/python tools/harvest_screen.py
    .venv/bin/python tools/harvest_screen.py --days 10 --give-r 0.10
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from ai_paths import resolve_report_dir  # noqa: E402

ET = ZoneInfo("America/New_York")
STOMP_SEC = 10.0
HARVEST_SEC = 30.0
GIVE_R = 0.10
MIN_SESSIONS = 5


def _jsonl(path):
    p = path
    if not os.path.exists(p):
        return
    with open(p, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue


def feat(row: dict, key: str):
    f = row.get("features")
    if isinstance(f, dict) and f.get(key) is not None:
        return f.get(key)
    return row.get(key)


def day_of(row: dict) -> str | None:
    ts = row.get("ts") or row.get("exit_time")
    try:
        return datetime.fromtimestamp(float(ts), tz=ET).date().isoformat()
    except Exception:
        return None


def bucket(row: dict) -> str:
    try:
        h = float(row.get("hold_sec") or 0)
    except (TypeError, ValueError):
        h = 0.0
    if h < STOMP_SEC:
        return "stomp"
    if h < HARVEST_SEC:
        return "fast"
    return "harvest"


def inside_book(row: dict, give_r: float) -> str:
    v = feat(row, "spread_r")
    if v is None:
        return "unk"
    try:
        sp = float(v)
    except (TypeError, ValueError):
        return "unk"
    if sp > give_r + 1e-12:
        return "inside"
    return "outside"


def _rs(rows: list[dict]) -> list[float]:
    out = []
    for r in rows:
        v = r.get("realized_r_multiple")
        if v is None:
            continue
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            continue
    return out


def _mfes(rows: list[dict]) -> list[float]:
    out = []
    for r in rows:
        v = r.get("mfe_r")
        if v is None:
            continue
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            continue
    return out


def summarize(rows: list[dict]) -> dict:
    R = _rs(rows)
    M = _mfes(rows)
    wins = sum(1 for x in R if x > 0)
    holds = []
    for r in rows:
        try:
            holds.append(float(r.get("hold_sec") or 0))
        except (TypeError, ValueError):
            pass
    mfe_sp = []
    for r in rows:
        m, sp = r.get("mfe_r"), feat(r, "spread_r")
        if m is None or sp is None:
            continue
        try:
            mfe_sp.append(float(m) - float(sp))
        except (TypeError, ValueError):
            continue
    return {
        "n": len(rows),
        "n_scored": len(R),
        "win": (wins / len(R)) if R else None,
        "sum_r": sum(R) if R else None,
        "med_r": statistics.median(R) if R else None,
        "med_mfe": statistics.median(M) if M else None,
        "med_hold": statistics.median(holds) if holds else None,
        "n_mfe_spread": len(mfe_sp),
        "med_mfe_spread": statistics.median(mfe_sp) if mfe_sp else None,
        "pct_mfe_spread_pos": (
            sum(1 for x in mfe_sp if x > 0) / len(mfe_sp) if mfe_sp else None
        ),
    }


def session_sign(rows_by_day: dict[str, list]) -> dict:
    days = sorted(rows_by_day)
    pos = 0
    sums = []
    for d in days:
        R = _rs(rows_by_day[d])
        s = sum(R) if R else 0.0
        sums.append(s)
        if s > 0:
            pos += 1
    n = len(days)
    p = None
    if n:
        p = sum(
            __import__("math").comb(n, i) for i in range(pos, n + 1)
        ) / (2 ** n)
    return {"sessions": n, "positive": pos, "p": p, "sums": sums, "days": days}


def fmt_sum(s: dict) -> str:
    def pct(x):
        return "—" if x is None else f"{100 * x:.0f}%"
    def n(x, d=3):
        return "—" if x is None else f"{x:+.{d}f}"
    return (
        f"n={s['n']:3}  win={pct(s['win']):>4}  sumR={n(s['sum_r'])}  "
        f"medR={n(s['med_r'])}  medMFE={n(s['med_mfe'])}  "
        f"hold={s['med_hold'] if s['med_hold'] is not None else '—':}  "
        f"MFE-sp n={s['n_mfe_spread']} med={n(s['med_mfe_spread'])}"
    )


def load_days(days: int) -> dict[str, list]:
    path = os.path.join(str(resolve_report_dir()), "outcomes.jsonl")
    cutoff = (datetime.now(ET).date() - timedelta(days=max(days, 1) * 2)).isoformat()
    by = defaultdict(list)
    for r in _jsonl(path):
        if not isinstance(r, dict):
            continue
        d = day_of(r)
        if not d or d < cutoff:
            continue
        by[d].append(r)
    keep = sorted(by)[-days:]
    return {d: by[d] for d in keep}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=8)
    ap.add_argument("--give-r", type=float, default=GIVE_R)
    args = ap.parse_args()
    by = load_days(args.days)
    if not by:
        print("no outcome rows")
        return 1
    print("=" * 72)
    print("  HARVEST SCREEN — stomp vs ratchet capture")
    print(f"  days={', '.join(by)}  give_r={args.give_r}")
    print("  STOMP hold<10s  FAST 10-30s  HARVEST >=30s")
    print("  INSIDE-BOOK  spread_r > give_r (cushion does not clear one RT)")
    print("=" * 72)

    all_rows = []
    for d, rows in by.items():
        all_rows.extend(rows)

    print("\n  BY SESSION  (all / stomp / harvest)")
    for d, rows in by.items():
        a, s, h = (
            [r for r in rows],
            [r for r in rows if bucket(r) == "stomp"],
            [r for r in rows if bucket(r) == "harvest"],
        )
        print(f"  {d}")
        print(f"    ALL      {fmt_sum(summarize(a))}")
        print(f"    STOMP    {fmt_sum(summarize(s))}")
        print(f"    HARVEST  {fmt_sum(summarize(h))}")

    print("\n  POOLED")
    for name, pred in (
        ("ALL", lambda r: True),
        ("STOMP <10s", lambda r: bucket(r) == "stomp"),
        ("FAST 10-30s", lambda r: bucket(r) == "fast"),
        ("HARVEST >=30s", lambda r: bucket(r) == "harvest"),
        ("INSIDE-BOOK", lambda r: inside_book(r, args.give_r) == "inside"),
        ("OUTSIDE-BOOK", lambda r: inside_book(r, args.give_r) == "outside"),
        ("DROP STOMP", lambda r: bucket(r) != "stomp"),
        ("HARVEST + OUTSIDE", lambda r: bucket(r) == "harvest" and inside_book(r, args.give_r) != "inside"),
    ):
        xs = [r for r in all_rows if pred(r)]
        print(f"    {name:<20} {fmt_sum(summarize(xs))}")

    # Session sign on harvest-only and drop-stomp
    def by_day(pred):
        out = {}
        for d, rows in by.items():
            xs = [r for r in rows if pred(r)]
            if xs:
                out[d] = xs
        return out

    print("\n  SESSION SIGN (unit of independence)")
    for label, pred in (
        ("all", lambda r: True),
        ("drop stomp", lambda r: bucket(r) != "stomp"),
        ("harvest only", lambda r: bucket(r) == "harvest"),
        ("harvest + not inside-book", lambda r: (
            bucket(r) == "harvest" and inside_book(r, args.give_r) != "inside"
        )),
    ):
        st = session_sign(by_day(pred))
        p = f"{st['p']:.3f}" if st["p"] is not None else "—"
        flag = ""
        if st["sessions"] < MIN_SESSIONS:
            flag = " UNDERPOWERED (<5 sessions)"
        elif st["p"] is not None and st["p"] <= 0.05 and st["positive"] > st["sessions"] / 2:
            flag = " PASS-ish sign"
        else:
            flag = " FAIL sign"
        print(f"    {label:<28} {st['positive']}/{st['sessions']} green  p={p}{flag}")
        if st["days"]:
            bits = ", ".join(
                f"{d}:{s:+.2f}" for d, s in zip(st["days"], st["sums"])
            )
            print(f"      {bits}")

    print("\n  READ")
    print("  Stomp 0% wins = shelf on the print / inside the spread, not a")
    print("  directional miss. Harvest remainder still needs MFE-spread > 0")
    print("  and a session majority. Do not arm from one green Thursday.")
    print("  Config stays observe until that bar is met.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
