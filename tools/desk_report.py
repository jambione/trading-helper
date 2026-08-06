#!/usr/bin/env python3
"""
desk_report.py — end-of-day roll-up across every record the desk keeps.

The other tools each answer one question. This is the sitting-down-at-the-end-
of-the-day view, and it is the only place the ADMITTED and REJECTED arms are
put side by side — which is the only honest way to ask whether a gate earned
its place.

SECTIONS
  1. FUNNEL          candidates -> admitted -> zoned -> touched -> armed ->
                     filled. On a day with no trades this single line explains
                     more than everything else combined: it says WHERE the
                     desk stopped.

  2. GATE SCORECARD  for each rejection reason, what the rejected names did
                     next, against what admitted names did over the same
                     horizon. A gate whose rejects outperform is costing money
                     no matter how principled it looks. Reads rejects.jsonl,
                     which nothing else does.

  3. COMPLETENESS    how often each feature was actually PRESENT at decision
                     time. Without this "the gate rejected it" and "the gate
                     was blind" are indistinguishable, and a gate scored on
                     absent inputs produces a confident, fake verdict. On
                     2026-08-06 rvol was None all morning and look_reason was
                     never populated at all.

  4. EXECUTION       order rejects by error, unprotected/naked buys, and fill
                     rate. The bracket rejection that opened a naked 83%-of-
                     equity position on 08-06 had already fired on 08-04 and
                     nothing aggregated it.

HONESTY
  Same rules as the sibling tools. Missing is not zero, small samples are
  labelled underpowered rather than quietly averaged, and none of this is
  randomized — the desk chose what it watched. Findings are hypotheses for
  tools/ab_bench.py, not verdicts.

USAGE
    venv/bin/python tools/desk_report.py
    venv/bin/python tools/desk_report.py --horizon 30 --json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "tools"))

import shadow_report as sr  # noqa: E402

REPORTS = _ROOT / "claude_reports"
SHADOW = REPORTS / "shadow.jsonl"
REJECTS = REPORTS / "rejects.jsonl"
OUTCOMES = REPORTS / "outcomes.jsonl"
EVENTS = REPORTS / "events.jsonl"
TRADELOG = _ROOT / "alpaca_trade_log.json"

MIN_N = 30  # below this, a group is an anecdote


def _jsonl(path: Path, day: date | None) -> list[dict]:
    out = []
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if not isinstance(r, dict):
            continue
        if day is not None:
            ts = r.get("ts") or 0
            try:
                if datetime.fromtimestamp(ts).date() != day:
                    continue
            except Exception:
                continue
        out.append(r)
    return out


# ── 2. reject arm ────────────────────────────────────────────────────────

def reject_episodes(rows: list[dict], horizon_sec: float) -> list[dict]:
    """One episode per symbol: first rejection, then what price did after.

    Keyed on symbol alone (not symbol+reason): a name rejected for one reason
    and later another is one continuous story of being kept out, and splitting
    it would double-count the same forward move.
    """
    by: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r.get("price") is not None:
            by[r["symbol"]].append(r)
    eps = []
    for sym, series in by.items():
        series.sort(key=lambda r: r.get("ts") or 0)
        fwd = sr.forward_return(series, 0, horizon_sec)
        prices = [float(r["price"]) for r in series]
        eps.append({
            "symbol": sym,
            "samples": len(series),
            "first_reason": series[0].get("reason"),
            "reasons": sorted({str(r.get("reason") or "") for r in series}),
            "source": series[0].get("source"),
            "fwd_return_pct": fwd,
            "max_excursion_pct": (round((max(prices) - prices[0]) / prices[0] * 100, 3)
                                  if prices[0] else None),
        })
    return eps


def _mean(vals: list[float]) -> float | None:
    return round(statistics.fmean(vals), 3) if vals else None


# ── 4. execution ─────────────────────────────────────────────────────────

def execution_stats(day: date | None) -> dict[str, Any]:
    try:
        rows = json.loads(TRADELOG.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(rows, list):
        return {}
    if day is not None:
        keep = []
        for e in rows:
            t = str(e.get("time") or "")
            try:
                if datetime.fromisoformat(t.replace("Z", "+00:00")).date() != day:
                    continue
            except Exception:
                continue
            keep.append(e)
        rows = keep

    actions = Counter(e.get("action") for e in rows)
    errors: Counter = Counter()
    for e in rows:
        err = e.get("error")
        if not err:
            continue
        msg = str(err)
        try:
            msg = json.loads(err).get("message", msg)
        except Exception:
            pass
        errors[str(msg)[:70]] += 1

    # A buy that carries a policy_fallback note is one that lost its bracket.
    # This is the exact shape that opened a naked 83%-of-equity position.
    naked = [e for e in rows
             if e.get("action") == "BUY"
             and "fallback" in str(e.get("note") or "").lower()]
    return {
        "rows": len(rows),
        "actions": dict(actions),
        "errors": dict(errors.most_common(8)),
        "naked_buys": [
            {"ticker": e.get("ticker"), "qty": e.get("qty"),
             "price": e.get("price"), "note": e.get("note"), "time": e.get("time")}
            for e in naked
        ],
    }


# ── 3. completeness ──────────────────────────────────────────────────────

_FEATURES = ["rvol", "look_reason", "score", "pct_change",
             "cm_ok", "pctr_ok", "cm_rsi_rising", "proximity_pct"]


def completeness(rows: list[dict]) -> dict[str, dict]:
    out = {}
    n = len(rows) or 1
    for f in _FEATURES:
        present = sum(1 for r in rows if r.get(f) is not None)
        out[f] = {"present": present, "pct": round(100.0 * present / n, 1)}
    return out


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--horizon", type=float, default=30.0,
                    help="forward-return horizon, minutes (default 30)")
    ap.add_argument("--all-days", action="store_true",
                    help="ignore the date filter (default: today only)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    day = None if args.all_days else date.today()
    hz = args.horizon * 60.0

    shadow_rows = _jsonl(SHADOW, day)
    reject_rows = _jsonl(REJECTS, day)
    events = _jsonl(EVENTS, day)
    outcomes = _jsonl(OUTCOMES, day)

    shadow_eps = [sr.episode_summary(s, hz)
                  for s in sr.by_episode(shadow_rows).values() if s]
    rej_eps = reject_episodes(reject_rows, hz)

    kinds = Counter(e.get("kind") for e in events)
    admitted = {e["symbol"] for e in shadow_eps}
    rejected = {e["symbol"] for e in rej_eps}
    # Counted in EPISODES, not symbols, from "admitted" down. A name dropped
    # and re-admitted gets a fresh zone and a fresh chance to arm, so symbol
    # counts made the funnel widen as it descended — 13 admitted but 20 zoned,
    # which reads as a bug in the desk rather than a mixed unit in the report.
    funnel = {
        "candidates_seen": len(admitted | rejected),
        "symbols_admitted": len(admitted),
        "symbols_rejected": len(rejected),
        "admitted": len(shadow_eps),
        "zoned": sum(1 for e in shadow_eps if e["zone"][0] is not None),
        "zone_touched": sum(1 for e in shadow_eps if e["zone_touched"]),
        "armed": sum(1 for e in shadow_eps if e["armed"]),
        "filled": int(kinds.get("entry_ok", 0)),
        "closed_with_outcome": len(outcomes),
    }

    adm_fwd = [e["fwd_return_pct"] for e in shadow_eps
               if e["fwd_return_pct"] is not None]
    by_reason: dict[str, list[float]] = defaultdict(list)
    for e in rej_eps:
        if e["fwd_return_pct"] is not None:
            by_reason[str(e["first_reason"])].append(e["fwd_return_pct"])

    report = {
        "day": str(day) if day else "all",
        "horizon_min": args.horizon,
        "funnel": funnel,
        "admitted_fwd_mean_pct": _mean(adm_fwd),
        "admitted_fwd_n": len(adm_fwd),
        "reject_arm": {r: {"n": len(v), "fwd_mean_pct": _mean(v)}
                       for r, v in sorted(by_reason.items())},
        "completeness_admitted": completeness(shadow_rows),
        "completeness_rejected": completeness(reject_rows),
        "execution": execution_stats(day),
        "event_kinds": dict(kinds),
    }

    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return

    print(f"\n{'=' * 66}\n  DESK REPORT — {report['day']}   "
          f"(forward horizon {args.horizon:.0f}m)\n{'=' * 66}")

    print("\n1. FUNNEL   (symbols at the top, admission episodes below)")
    f = funnel
    print(f"   candidates seen      {f['candidates_seen']:>5}  symbols")
    print(f"     admitted           {f['symbols_admitted']:>5}  symbols")
    print(f"     rejected           {f['symbols_rejected']:>5}  symbols")
    print(f"   admission episodes   {f['admitted']:>5}")
    print(f"   zone drawn           {f['zoned']:>5}")
    print(f"   price reached zone   {f['zone_touched']:>5}")
    print(f"   armed                {f['armed']:>5}")
    print(f"   filled               {f['filled']:>5}")
    print(f"   closed w/ outcome    {f['closed_with_outcome']:>5}")
    # Name the stage that actually stopped the desk.
    stages = [("nothing admitted", f["admitted"]),
              ("no zone drawn", f["zoned"]),
              ("price never reached the zone", f["zone_touched"]),
              ("never armed (indicator gate)", f["armed"]),
              ("armed but never filled", f["filled"])]
    for label, v in stages:
        if v == 0:
            print(f"   -> stopped at: {label}")
            break

    print("\n2. GATE SCORECARD  (what the desk turned away, and what it did next)")
    am = report["admitted_fwd_mean_pct"]
    print(f"   admitted        n={len(adm_fwd):<4} fwd "
          f"{(f'{am:+.3f}%' if am is not None else '—')}")
    if not by_reason:
        print("   rejected        no reject episodes with a usable forward window yet")
    for reason, v in sorted(by_reason.items()):
        m = _mean(v)
        flag = ""
        if m is not None and am is not None and m > am:
            flag = "  <- REJECTS BEAT ADMITS (gate may be costing)"
        print(f"   {reason:<15} n={len(v):<4} fwd {m:+.3f}%{flag}"
              + ("   [UNDERPOWERED]" if len(v) < MIN_N else ""))

    print("\n3. FEATURE COMPLETENESS  (present at decision time)")
    print(f"   {'feature':<16}{'admitted':>12}{'rejected':>12}")
    ca, cr = report["completeness_admitted"], report["completeness_rejected"]
    for feat in _FEATURES:
        a = ca.get(feat, {}).get("pct", 0.0)
        r = cr.get(feat, {}).get("pct", 0.0)
        warn = "  <- never observed" if a == 0.0 else ""
        print(f"   {feat:<16}{a:>11.0f}%{r:>11.0f}%{warn}")

    ex = report["execution"]
    print("\n4. EXECUTION")
    if not ex:
        print("   no trade-log rows for this day")
    else:
        print(f"   orders logged        {ex['rows']}")
        print(f"   actions              {ex['actions']}")
        if ex["errors"]:
            print("   errors:")
            for msg, n in ex["errors"].items():
                print(f"     {n:>3}x  {msg}")
        if ex["naked_buys"]:
            print("   *** UNPROTECTED BUYS (bracket lost, position opened anyway):")
            for b in ex["naked_buys"]:
                print(f"     {b['ticker']} {b['qty']}sh @ {b['price']} "
                      f"[{b['note']}] {b['time']}")
        else:
            print("   unprotected buys     0")

    print("\nNone of this is randomized — the desk chose what it watched.")
    print("Anything promising is a hypothesis for tools/ab_bench.py, not a verdict.\n")


if __name__ == "__main__":
    main()
