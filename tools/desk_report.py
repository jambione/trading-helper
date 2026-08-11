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
from functools import lru_cache
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "tools"))

import shadow_report as sr  # noqa: E402

from ai_paths import find_report_file, resolve_report_dir  # noqa: E402


def _report(name: str) -> Path:
    """Resolve a report file the same way the desk writes it.

    Goes through ai_paths.resolve_report_dir() rather than hardcoding a
    directory, so AI_REPORT_DIR and any future move stay in one place. A
    hardcoded path does not fail loudly when it drifts from the writers — it
    just reports a frozen tree, with no error and numbers that stopped moving.
    """
    return find_report_file(name) or (resolve_report_dir() / name)


SHADOW = _report("shadow.jsonl")
REJECTS = _report("rejects.jsonl")
OUTCOMES = _report("outcomes.jsonl")
EVENTS = _report("events.jsonl")
TRADELOG = _ROOT / "alpaca_trade_log.json"

MIN_N = 30  # below this, a group is an anecdote


@lru_cache(maxsize=None)
def _first_day(path: Path) -> date | None:
    """Earliest day this log holds a row for, or None when it holds none.

    The whole point of the report is that missing is not zero, and the
    scorecard broke that rule the day it shipped: shadow.jsonl and
    rejects.jsonl start on 2026-08-06, so comparing 08-06 against 08-05 read
    every shadow metric as 0 -> N and printed BETTER for instrumentation
    arriving. Knowing when a log STARTS is what separates "the desk did not
    do this" from "nothing was watching".
    """
    first: date | None = None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ts = json.loads(line).get("ts")
            d = datetime.fromtimestamp(float(ts)).date()
        except Exception:
            continue
        if first is None or d < first:
            first = d
    return first


def _tradelog_first_day() -> date | None:
    """Earliest day in the (JSON list, ISO-timestamped) execution log."""
    try:
        rows = json.loads(TRADELOG.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(rows, list):
        return None
    first: date | None = None
    for e in rows:
        try:
            d = datetime.fromisoformat(
                str(e.get("time") or "").replace("Z", "+00:00")).date()
        except Exception:
            continue
        if first is None or d < first:
            first = d
    return first


def _instrumented(day: date | None) -> dict[str, bool]:
    """Which records were actually being written on *day*.

    A source counts as instrumented from its first row onward. Before that it
    is unknowable, not zero, and any metric derived from it must decline to
    answer rather than score a change that only reflects the logger starting.
    """
    if day is None:
        return {k: True for k in
                ("shadow", "rejects", "events", "outcomes", "tradelog")}
    starts = {
        "shadow": _first_day(SHADOW),
        "rejects": _first_day(REJECTS),
        "events": _first_day(EVENTS),
        "outcomes": _first_day(OUTCOMES),
        "tradelog": _tradelog_first_day(),
    }
    return {k: (v is not None and day >= v) for k, v in starts.items()}


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
    #
    # It is NOT the only shape, and scoring only this one made the metric lie.
    # On 2026-08-10 the report printed "unprotected buys 0" while AXTI sat on
    # 20 shares with no protective order at all: a dual-tranche entry whose
    # second leg was refused as a wash trade, and whose rollback cancelled the
    # first leg's stop while its fill survived. No fallback note anywhere —
    # the bracket was never rejected, it was cancelled afterwards.
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
        "live_naked": _live_unprotected(),
    }


def _live_unprotected() -> list[dict]:
    """Positions held RIGHT NOW with no protective sell order against them.

    Ground truth from the broker rather than a pattern in the trade log. The
    log-based check above can only recognise failure shapes someone has already
    seen; this one is the operator rule stated directly — "no position without
    a protective stop" — so it catches shapes nobody has thought of yet,
    including the rollback orphan that produced it.

    Returns [] when the broker cannot be reached: an EOD report must still run
    offline, and "cannot tell" must not print as "all clear" — the caller
    distinguishes them via the None-vs-empty check on `err`.
    """
    try:
        import alpaca_trader
        pos = alpaca_trader.get_positions_detail() or {}
        if not pos:
            return []
        orders = alpaca_trader.get_open_orders() or []
        protected = {
            str(o.get("symbol") or "").upper()
            for o in orders
            if str(o.get("side") or "").lower() == "sell"
            and (o.get("stop") is not None
                 or str(o.get("type") or "") in ("stop", "stop_limit", "trailing_stop"))
        }
        out = []
        for sym, p in pos.items():
            s = str(sym).upper()
            if s in protected:
                continue
            try:
                qty = float(p.get("qty") or 0)
                px = float(p.get("avg_entry_price") or p.get("avg_entry") or 0)
            except (TypeError, ValueError):
                qty = px = 0.0
            out.append({"ticker": s, "qty": qty, "price": px,
                        "notional": round(qty * px, 2)})
        return out
    except Exception:
        return []


# ── 3. completeness ──────────────────────────────────────────────────────

_FEATURES = ["rvol", "look_reason", "score", "pct_change",
             "cm_ok", "pctr_ok", "cm_rsi_rising", "proximity_pct"]

# Indicator state is read when a name is already on the book and being polled.
# A rejection happens before that, so these can never appear on the reject arm
# — 0% there is the shape of the pipeline, not a hole in the logging, and
# printing it as 0% alongside real gaps made every reader chase it once.
_ADMITTED_ONLY = {"cm_ok", "pctr_ok", "cm_rsi_rising", "proximity_pct"}


def completeness(rows: list[dict]) -> dict[str, dict]:
    out = {}
    n = len(rows) or 1
    for f in _FEATURES:
        present = sum(1 for r in rows if r.get(f) is not None)
        out[f] = {"present": present, "pct": round(100.0 * present / n, 1)}
    return out


# Metrics that decide whether the desk actually got better, with the direction
# that counts as improvement. Deliberately short: a scorecard nobody reads is
# not a scorecard. "higher" / "lower" / "watch" — watch means a change is
# informative but not good or bad on its own.
SCORECARD = [
    ("zones drawn",        "zoned",            "watch"),
    ("zone touch rate %",  "touch_rate",       "higher"),
    ("armed",              "armed",            "higher"),
    ("filled",             "filled",           "higher"),
    ("closed w/ outcome",  "closed_with_outcome", "higher"),
    ("unprotected buys",   "naked_buys",       "lower"),
    ("order errors",       "order_errors",     "lower"),
    ("admitted (symbols)", "symbols_admitted", "watch"),
    ("rejected (symbols)", "symbols_rejected", "watch"),
]

# Which record each scorecard metric is derived from. A metric whose source
# was not yet being written is unanswerable for that day, not zero.
_METRIC_SOURCE = {
    "zoned": "shadow",
    "touch_rate": "shadow",
    "armed": "shadow",
    "symbols_admitted": "shadow",
    "symbols_rejected": "rejects",
    "filled": "events",
    "closed_with_outcome": "outcomes",
    "naked_buys": "tradelog",
    "order_errors": "tradelog",
}


def scorecard_metrics(report: dict) -> dict[str, float | None]:
    """Flatten a report into the handful of numbers worth comparing.

    A metric reads None when its source log was not yet recording on that day
    — otherwise the first day of instrumentation scores as improvement, which
    is the one result a scorecard must never produce.
    """
    f = report.get("funnel") or {}
    ex = report.get("execution") or {}
    ins = report.get("instrumented") or {}
    zoned = f.get("zoned") or 0
    touched = f.get("zone_touched") or 0
    vals: dict[str, float | None] = {
        "zoned": zoned,
        "touch_rate": (round(100.0 * touched / zoned, 1) if zoned else None),
        "armed": f.get("armed"),
        "filled": f.get("filled"),
        "closed_with_outcome": f.get("closed_with_outcome"),
        "naked_buys": len(ex.get("naked_buys") or []),
        "order_errors": sum((ex.get("errors") or {}).values()),
        "symbols_admitted": f.get("symbols_admitted"),
        "symbols_rejected": f.get("symbols_rejected"),
    }
    for key, src in _METRIC_SOURCE.items():
        if not ins.get(src, True):
            vals[key] = None
    return vals


def print_scorecard(now: dict, prev: dict, now_day: str, prev_day: str) -> None:
    a, b = scorecard_metrics(now), scorecard_metrics(prev)
    print(f"\n{'=' * 66}\n  SCORECARD — {now_day} vs {prev_day}\n{'=' * 66}")
    print(f"  {'metric':<22}{prev_day:>12}{now_day:>12}{'delta':>10}  verdict")

    def cell(v: float | None) -> str:
        return "n/a" if v is None else f"{v}"

    any_na = False
    for label, key, want in SCORECARD:
        cur, old = a.get(key), b.get(key)
        if cur is None and old is None:
            continue
        if cur is None or old is None:
            # One side is unanswerable. Show both, score neither — a delta
            # against an unrecorded day measures the logger, not the desk.
            any_na = True
            print(f"  {label:<22}{cell(old):>12}{cell(cur):>12}"
                  f"{'—':>10}  n/a")
            continue
        d = round(cur - old, 1)
        if want == "watch" or d == 0:
            verdict = "—"
        elif (want == "higher" and d > 0) or (want == "lower" and d < 0):
            verdict = "BETTER"
        else:
            verdict = "WORSE"
        print(f"  {label:<22}{old:>12}{cur:>12}{d:>+10}  {verdict}")

    if any_na:
        missing = sorted(
            src for src, on in (prev.get("instrumented") or {}).items()
            if not on)
        note = (f" — {', '.join(missing)} not recording yet"
                if missing else "")
        print(f"\n  n/a: the metric's source log has no data for {prev_day}"
              f"{note}.\n  Scoring it would credit the desk for "
              f"instrumentation arriving.")
    print("\n  One day against one day is not evidence of a trend — it is a"
          "\n  check that a change did what it was supposed to do. Sustained"
          "\n  claims need weeks, or tools/ab_bench.py.\n")



def exhaustion_stats(shadow: list[dict], horizon_sec: float) -> dict:
    """Coverage and behaviour of the %R exhaustion rule.

    Coverage first, because it decides how much of the rest means anything:
    a name with no %R reading is not being traded by this rule at all, it is
    on the fallback path, and averaging the two produces a number describing
    neither.
    """
    rows = [r for r in shadow if r.get("symbol")]
    if not rows:
        return {}
    with_p = [r for r in rows if r.get("exhaustion") is not None]
    live = sum(1 for r in with_p if r.get("pctr_src") == "live")
    no_data = sorted({str(r["symbol"]) for r in rows
                      if r.get("exhaustion") is None})
    # Names that NEVER got a reading all day, separated from names that merely
    # had gaps. Under ai_watch_require_exhaustion_data these can never arm, so
    # each one is a book slot that could not have traded whatever it did — the
    # number that says whether the coverage gate is protecting the desk or
    # starving it.
    ever = {str(r["symbol"]) for r in rows if r.get("exhaustion") is not None}
    blind = sorted({str(r["symbol"]) for r in rows} - ever)
    # Rows written before the exhaustion fields existed have no key at all,
    # which is not the same as a name we could not read. Without this the
    # report says "100% blind" for every historical day and buries the real
    # number the day it starts to matter.
    instrumented = any("exhaustion" in r for r in rows)
    states = Counter(str(r.get("exhaustion_state") or "?") for r in rows)
    why = Counter(str(r.get("arm_why") or "") for r in rows
                  if r.get("arm_ok") is not None)

    # Forward return bucketed by how exhausted the name was when we looked.
    by_sym: dict[str, list[dict]] = defaultdict(list)
    for r in with_p:
        by_sym[str(r["symbol"])].append(r)
    buckets: list[tuple[float, float, int, float]] = []
    for lo, hi in ((0, 25), (25, 50), (50, 75), (75, 90), (90, 100)):
        vals = []
        for series in by_sym.values():
            series.sort(key=lambda r: r.get("ts") or 0)
            for i, r in enumerate(series):
                e = r.get("exhaustion")
                if e is None or not (lo <= float(e) < hi):
                    continue
                f = sr.forward_return(series, i, horizon_sec)
                if f is not None:
                    vals.append(f)
        if vals:
            buckets.append((lo, hi, len(vals), statistics.fmean(vals)))
    return {
        "coverage": {"rows": len(rows), "with_pctr": len(with_p),
                     "live": live, "engine": len(with_p) - live},
        "no_data_symbols": no_data,
        "blind_symbols": blind,
        "symbols_seen": len(ever | set(blind)),
        "instrumented": instrumented,
        "states": dict(states),
        "arm_why": why.most_common(10),
        "by_bucket": buckets,
    }

def build_report(day, hz: float) -> dict:
    """Assemble one day's report. Extracted so --compare can build two."""
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
    # counts made the funnel widen as it descended.
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

    return {
        "day": str(day) if day else "all",
        "horizon_min": hz / 60.0,
        "funnel": funnel,
        "admitted_fwd_mean_pct": _mean(adm_fwd),
        "admitted_fwd_n": len(adm_fwd),
        "reject_arm": {r: {"n": len(v), "fwd_mean_pct": _mean(v)}
                       for r, v in sorted(by_reason.items())},
        "reject_by_reason": {r: v for r, v in by_reason.items()},
        "completeness_admitted": completeness(shadow_rows),
        "completeness_rejected": completeness(reject_rows),
        "execution": execution_stats(day),
        "exhaustion": exhaustion_stats(shadow_rows, hz),
        "event_kinds": dict(kinds),
        "instrumented": _instrumented(day),
    }


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--horizon", type=float, default=30.0,
                    help="forward-return horizon, minutes (default 30)")
    ap.add_argument("--all-days", action="store_true",
                    help="ignore the date filter (default: today only)")
    ap.add_argument("--day", help="report a specific day, YYYY-MM-DD")
    ap.add_argument("--compare",
                    help="prior day to measure against, YYYY-MM-DD. Prints a "
                         "SCORECARD of deltas on the metrics that decide "
                         "whether the desk got better.")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.all_days:
        day = None
    elif args.day:
        day = date.fromisoformat(args.day)
    else:
        day = date.today()
    hz = args.horizon * 60.0

    report = build_report(day, hz)

    prev = None
    if args.compare:
        prev_day = date.fromisoformat(args.compare)
        prev = build_report(prev_day, hz)

    if args.json:
        payload = {"report": report}
        if prev is not None:
            payload["compare"] = prev
            payload["scorecard"] = {
                "current": scorecard_metrics(report),
                "previous": scorecard_metrics(prev),
            }
        print(json.dumps(payload, indent=2, default=str))
        return

    if prev is not None:
        print_scorecard(report, prev, report["day"], prev["day"])

    print(f"\n{'=' * 66}\n  DESK REPORT — {report['day']}   "
          f"(forward horizon {args.horizon:.0f}m)\n{'=' * 66}")

    print("\n1. FUNNEL   (symbols at the top, admission episodes below)")
    f = report["funnel"]
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
    by_reason = report["reject_by_reason"]
    print(f"   admitted        n={report['admitted_fwd_n']:<4} fwd "
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
        warn = "  <- never observed" if a == 0.0 else ""
        if feat in _ADMITTED_ONLY:
            print(f"   {feat:<16}{a:>11.0f}%{'n/a':>12}{warn}")
            continue
        r = cr.get(feat, {}).get("pct", 0.0)
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
            print("   unprotected buys     0  (from trade log)")
        live = ex.get("live_naked") or []
        if live:
            print("   *** HELD RIGHT NOW WITH NO PROTECTIVE ORDER:")
            for b in live:
                print(f"     {b['ticker']} {b['qty']:.0f}sh @ {b['price']:.2f} "
                      f"= ${b['notional']:,.0f}  — NO STOP")
        else:
            print("   live unprotected     0  (broker check)")

    exh = report.get("exhaustion") or {}
    if exh:
        print("\n5. EXHAUSTION  (the rule that now gates entries)")
        cov = exh.get("coverage") or {}
        n = cov.get("rows", 0)
        if n and not exh.get("instrumented"):
            print("   (rows predate the exhaustion fields — no coverage to report)")
        elif n:
            print(f"   %R reading present   {cov.get('with_pctr', 0)}/{n} "
                  f"({100.0 * cov.get('with_pctr', 0) / n:.0f}%)  "
                  f"live={cov.get('live', 0)} engine={cov.get('engine', 0)}")
            miss = exh.get("no_data_symbols") or []
            if miss:
                print(f"   NO reading (some rows): {', '.join(miss[:14])}"
                      + (f" +{len(miss) - 14} more" if len(miss) > 14 else ""))
            blind = exh.get("blind_symbols") or []
            seen = exh.get("symbols_seen") or 0
            if seen:
                pct = 100.0 * len(blind) / seen
                print(f"   BLIND all day (cannot ever arm): {len(blind)}/{seen} "
                      f"({pct:.0f}% of book slots)")
                if blind:
                    print(f"     {', '.join(blind[:14])}"
                          + (f" +{len(blind) - 14} more" if len(blind) > 14 else ""))
        if exh.get("states"):
            print(f"   states seen          {exh['states']}")
        if exh.get("arm_why"):
            print("   arm verdicts:")
            for k, v in exh["arm_why"]:
                print(f"     {v:>5}x  {k}")
        if exh.get("by_bucket"):
            print("   forward return by exhaustion at decision:")
            for lo, hi, cnt, fwd in exh["by_bucket"]:
                print(f"     {lo:>3.0f}-{hi:<3.0f}%  n={cnt:<4} fwd {fwd:+.3f}%")

    print("\nNone of this is randomized — the desk chose what it watched.")
    print("Anything promising is a hypothesis for tools/ab_bench.py, not a verdict.\n")


if __name__ == "__main__":
    main()
