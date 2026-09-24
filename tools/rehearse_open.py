#!/usr/bin/env python3
"""rehearse_open.py — replay a session's book, minute by minute, from the desk's own ledgers.

The question it answers: at each minute, how many names were on the book, how
many of them could actually have been bought (only the -50 cross missing), and
what kept every other name out or blocked? A morning with a "full" book and
no fills (2026-09-24) is invisible in P/L; it is obvious here.

OBSERVED LAYER (this file): reconstructs what the live desk did, from
  ai_reports/admit_ledger/<day>.jsonl     every seed + inclusion decision
  ai_reports/decision_ledger/<day>.jsonl  every arm evaluation (arm_why, tape age)
  ai_reports/events.jsonl                 admit_funnel (book membership), watch_drop,
                                          arm_recheck / entry_ok / stale_data / skips
No market data is fetched and nothing is written. It must reproduce the live
morning before any fix is judged against it (step B of the after-close plan).

Buckets for a seated name's last arm verdict in a minute:
  armable      only the signal is missing (wait_mid_rise, mid_rise_lost,
               mid_rise_stale) or the arm passed — the seat is doing its job
  gate         an intended gate refused it (spread_wide, gapped_down, ...)
  data         a data/plumbing refusal (stale_quote, engine_stale, spread_unknown,
               gap_unknown, no_rsi_data, ...) — the fixable kind
  other        anything else (logged as its reason)

USAGE (on the mini, read-only)
    .venv/bin/python tools/rehearse_open.py --day 2026-09-24 --start 09:30 --end 11:30
    .venv/bin/python tools/rehearse_open.py --day 2026-09-24 --step 5 --json out.json
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ET = ZoneInfo("America/New_York")

SIGNAL_WAIT = {"wait_mid_rise", "mid_rise_lost", "mid_rise_stale"}
GATE = {"spread_wide", "gapped_down", "above_max_price", "below_min_price",
        "reentry_cooldown", "rvol_pace_low", "look_wash", "hard_no"}
DATA = {"stale_quote", "engine_stale", "spread_unknown", "gap_unknown",
        "no_rsi_data", "rvol_pace_unknown", "no_structure", "tape_only",
        "stale_tape", "confirm_stale"}
ARM_EVENTS = {"arm_recheck", "entry_ok", "stale_data", "submit_released",
              "watch_skip", "entry_skip"}


def bucket(why: str | None, ok: bool = False) -> str:
    if ok:
        return "armable"
    w = str(why or "")
    if w in SIGNAL_WAIT:
        return "armable"
    if w in GATE:
        return "gate"
    if w in DATA:
        return "data"
    return "other"


def _mins(ts: float) -> int:
    d = datetime.fromtimestamp(ts, ET)
    return d.hour * 60 + d.minute


def _hhmm(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def _day_bounds(day: str) -> tuple[float, float]:
    d0 = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
    return d0.timestamp(), d0.timestamp() + 86400


def _jsonl(path: str, t0: float, t1: float, kinds: set | None = None,
           prefilter: tuple = ()):
    if not os.path.exists(path):
        return
    with open(path, errors="replace") as f:
        for line in f:
            if prefilter and not any(p in line for p in prefilter):
                continue
            try:
                e = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            ts = e.get("ts")
            if not isinstance(ts, (int, float)) or not (t0 <= ts < t1):
                continue
            if kinds is not None and (e.get("kind") or e.get("stage")) not in kinds:
                continue
            yield e


def replay(day: str, start: str, end: str, step: int, reports: str) -> dict:
    t0, t1 = _day_bounds(day)
    m_start = int(start[:2]) * 60 + int(start[3:])
    m_end = int(end[:2]) * 60 + int(end[3:])

    def slot(ts):
        m = _mins(ts)
        if m < m_start or m >= m_end:
            return None
        return m_start + ((m - m_start) // step) * step

    slots = list(range(m_start, m_end, step))
    book: dict[int, set] = {}
    cands: dict[int, list] = collections.defaultdict(list)
    incl: dict[int, collections.Counter] = collections.defaultdict(collections.Counter)
    seed: dict[int, collections.Counter] = collections.defaultdict(collections.Counter)
    last_arm: dict[int, dict] = collections.defaultdict(dict)   # slot -> sym -> (why, ok)
    tape_age: dict[int, list] = collections.defaultdict(list)
    drops: dict[int, collections.Counter] = collections.defaultdict(collections.Counter)
    trade_ev: list[tuple[float, str, str, str]] = []

    ev_path = os.path.join(reports, "events.jsonl")
    for e in _jsonl(ev_path, t0, t1, prefilter=('"admit_funnel"', '"watch_drop"',
                                               '"arm_recheck"', '"entry_ok"',
                                               '"stale_data"', '"watch_skip"',
                                               '"entry_skip"', '"submit_released"')):
        s = slot(e["ts"])
        k = e.get("kind")
        if k == "admit_funnel":
            if s is not None:
                book[s] = set(e.get("kept_symbols") or [])   # last pass in the slot wins
                cands[s].append(int(e.get("n_candidates") or 0))
        elif k == "watch_drop":
            if s is not None:
                drops[s][str(e.get("reason"))] += 1
        elif k in ARM_EVENTS:
            if k == "watch_skip" and e.get("reason") in ("above_max_price", "reentry_cooldown"):
                continue
            if s is not None or k in ("entry_ok", "stale_data"):
                trade_ev.append((e["ts"], k, str(e.get("symbol")),
                                 str(e.get("reason") or e.get("why") or e.get("stage") or "")))

    for e in _jsonl(os.path.join(reports, "admit_ledger", f"{day}.jsonl"), t0, t1):
        s = slot(e["ts"])
        if s is None or e.get("kept"):
            continue
        r = str(e.get("reason") or "?")
        if e.get("stage") == "inclusion":
            incl[s][r] += 1
        elif e.get("stage") == "seed":
            seed[s][f"{e.get('source')}:{r}"] += 1

    for e in _jsonl(os.path.join(reports, "decision_ledger", f"{day}.jsonl"), t0, t1,
                    kinds={"arm"}):
        s = slot(e["ts"])
        if s is None:
            continue
        last_arm[s][str(e.get("symbol"))] = (e.get("arm_why"), bool(e.get("arm_ok")))
        a = e.get("tape_age_sec")
        if isinstance(a, (int, float)):
            tape_age[s].append(float(a))

    rows = []
    prev_book: set = set()
    for s in slots:
        b = book.get(s, prev_book)
        prev_book = b
        verdicts = {sym: last_arm[s].get(sym) for sym in b}
        bk = collections.Counter()
        reasons = collections.Counter()
        for sym, v in verdicts.items():
            if v is None:
                bk["unjudged"] += 1
                continue
            why, ok = v
            bk[bucket(why, ok)] += 1
            if not ok and why not in SIGNAL_WAIT:
                reasons[str(why)] += 1
        ages = sorted(tape_age[s])
        rows.append({
            "t": _hhmm(s),
            "book": len(b),
            "armable": bk["armable"],
            "gate": bk["gate"],
            "data": bk["data"],
            "other": bk["other"],
            "unjudged": bk["unjudged"],
            "cand": (round(sum(cands[s]) / len(cands[s])) if cands[s] else None),
            "tape_age_p50": (round(ages[len(ages) // 2], 1) if ages else None),
            "top_blocks": reasons.most_common(3),
            "top_incl": incl[s].most_common(3),
            "top_seed": seed[s].most_common(3),
            "drops": drops[s].most_common(2),
            "names": sorted(b),
        })

    trades = [{"t": datetime.fromtimestamp(ts, ET).strftime("%H:%M:%S"), "kind": k,
               "symbol": sym, "detail": d}
              for ts, k, sym, d in sorted(trade_ev)
              if m_start <= _mins(ts) < m_end or k in ("entry_ok", "stale_data")]
    return {"day": day, "start": start, "end": end, "step": step,
            "rows": rows, "events": trades}


def criteria(res: dict) -> list[tuple[str, bool, str]]:
    """The pass/fail line the fixes are judged on (proposed 2026-09-24)."""
    rows = res["rows"]
    by = {r["t"]: r for r in rows}
    at = next((r for r in rows if r["t"] >= "09:40"), None)
    after = [r for r in rows if r["t"] >= "09:40"]
    arms = sum(1 for e in res["events"] if e["kind"] == "arm_recheck")
    entries = sum(1 for e in res["events"] if e["kind"] == "entry_ok")
    first_entry = next((e["t"] for e in res["events"] if e["kind"] == "entry_ok"), None)
    out = []
    if at:
        out.append(("book >= 10 by 09:40", at["book"] >= 10, f"{at['book']} at {at['t']}"))
        out.append((">= 6 armable at 09:40", at["armable"] >= 6, f"{at['armable']} at {at['t']}"))
    if after:
        med = sorted(r["armable"] for r in after)[len(after) // 2]
        out.append(("median armable after 09:40 >= 6", med >= 6, f"median {med}"))
        dat = sum(r["data"] for r in after) / max(1, sum(r["book"] for r in after))
        out.append(("data-blocked share of seats < 10%", dat < 0.10, f"{dat:.0%}"))
    out.append(("every arm reached an order", arms == entries,
                f"{arms} arm(s), {entries} entr(y/ies)"))
    out.append(("first buy by 09:45", bool(first_entry) and first_entry <= "09:45:59",
                f"first entry {first_entry or 'none'}"))
    # Concurrency headline (optional — present when res carries concurrency).
    c = res.get("concurrency") or {}
    pct1, pct2 = c.get("pct_ge1"), c.get("pct_ge2")
    if pct1 is not None:
        out.append((">=1 open for >=80% of session", pct1 >= 0.80,
                    f"{pct1:.0%} (avg {c.get('avg_open')} max {c.get('max_open')})"))
    if pct2 is not None:
        out.append((">=2 open for >=50% of session", pct2 >= 0.50, f"{pct2:.0%}"))
    # Opens cadence: >=1 entry_ok per 10 minutes across the scored window.
    if after and res.get("events") is not None:
        window_min = max(1, (int(res["end"][:2]) * 60 + int(res["end"][3:])
                             - max(int(res["start"][:2]) * 60 + int(res["start"][3:]), 9 * 60 + 40)))
        n_opens = sum(1 for e in res["events"] if e.get("kind") == "entry_ok")
        per_10 = n_opens / max(1.0, window_min / 10.0)
        out.append((">=1 open per 10 min", per_10 >= 1.0,
                    f"{n_opens} opens / {window_min}m ({per_10:.2f}/10m)"))
    _ = by
    return out


def render(res: dict) -> str:
    L = [f"REHEARSAL {res['day']} {res['start']}-{res['end']} (observed, step {res['step']}m)", ""]
    L.append(f"{'time':>5} {'book':>4} {'arm':>4} {'gate':>4} {'data':>4} {'oth':>4} "
             f"{'unj':>4} {'cand':>4} {'tape50':>6}  top blocks | top admission refusals")
    for r in res["rows"]:
        blocks = ", ".join(f"{k}:{n}" for k, n in r["top_blocks"])
        incl = ", ".join(f"{k}:{n}" for k, n in r["top_incl"])
        L.append(f"{r['t']:>5} {r['book']:>4} {r['armable']:>4} {r['gate']:>4} {r['data']:>4} "
                 f"{r['other']:>4} {r['unjudged']:>4} {str(r['cand'] or '-'):>4} "
                 f"{str(r['tape_age_p50'] if r['tape_age_p50'] is not None else '-'):>6}  "
                 f"{blocks or '-'} | {incl or '-'}")
    L.append("")
    L.append("Trade path events:")
    for e in res["events"]:
        L.append(f"  {e['t']}  {e['kind']:<16} {e['symbol']:<6} {e['detail']}")
    if not res["events"]:
        L.append("  none")
    L.append("")
    L.append("Pass criteria:")
    for name, ok, detail in criteria(res):
        L.append(f"  [{'PASS' if ok else 'FAIL'}] {name:<36} {detail}")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default=datetime.now(ET).strftime("%Y-%m-%d"))
    ap.add_argument("--start", default="09:30")
    ap.add_argument("--end", default="16:00")
    ap.add_argument("--step", type=int, default=5, help="minutes per row")
    ap.add_argument("--reports", default=os.path.join(ROOT, "ai_reports"))
    ap.add_argument("--json", help="also write the result here")
    args = ap.parse_args()
    res = replay(args.day, args.start, args.end, args.step, args.reports)
    print(render(res))
    if args.json:
        with open(args.json, "w") as f:
            json.dump(res, f, indent=1)


if __name__ == "__main__":
    main()
