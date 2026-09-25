#!/usr/bin/env python3
"""nightly_report.py — the after-close loop, one command, one file.

Writes ai_reports/nightly/<day>.md with, in order:
  1. scorecard.py by day (last 10 sessions) and by config fingerprint
  2. exec_report.py — every order vs the SIP bid/ask at submit
  3. studies/counterfactual_today.py — the day replayed, with and without gates
  4. premarket_grade.py — were the premarket picks worth seating
  5. arm refusals from today's gates (engine_stale, spread, gap)
  6. volume-pace observe gate: today's fills split at pace 1.64
  7. watchdog WEDGED restarts
  8. cross funnel — where every in-band -50 cross on a seed name died
Plus, at the top, the replay fidelity verdict (does the replay still match
live?). Every replay A/B leans on it: 2026-09-25 the replay skipped a live
code path and opened 56 where live opened 13.

Every step runs as a subprocess with a timeout; a failed step is written into
the report as a failure and the rest still run. Needs SIP data that is 15+
minutes old, so it runs from 16:30 ET (tools/watchdog.py nightly slot).

USAGE (on the mini)
    .venv/bin/python tools/nightly_report.py                # today
    .venv/bin/python tools/nightly_report.py --day 2026-09-24
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import bars  # noqa: E402

GATE_REASONS = ("engine_stale", "spread_wide", "spread_unknown", "gapped_down", "gap_unknown")


def run(args: list[str], timeout: float) -> tuple[str, str]:
    """(status, output) of a tools script run with the repo's interpreter."""
    t0 = time.time()
    try:
        p = subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True,
                           text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        out = (p.stdout or "").rstrip()
        status = f"ok ({time.time() - t0:.0f}s)" if p.returncode == 0 else \
            f"FAILED rc={p.returncode}: {(p.stderr or '').strip()[-400:]}"
        return status, out
    except subprocess.TimeoutExpired:
        return f"TIMED OUT after {timeout:.0f}s", ""


def gate_refusals(day: str) -> str:
    c: collections.Counter = collections.Counter()
    syms: dict = collections.defaultdict(set)
    t0 = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=bars.ET).timestamp()
    for line in open(os.path.join(ROOT, "ai_reports", "events.jsonl")):
        if not any(r in line for r in GATE_REASONS):
            continue
        try:
            e = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if float(e.get("ts") or 0) < t0 or bars.day_of(e["ts"]) != day:
            continue
        for k in ("reason", "why", "block"):
            v = e.get(k)
            if v in GATE_REASONS:
                c[v] += 1
                if e.get("symbol"):
                    syms[v].add(e["symbol"])
                break
    if not c:
        return "no gate refusals logged"
    return "\n".join(f"{k:<16}{n:>6}   {', '.join(sorted(syms[k]))[:200]}"
                     for k, n in c.most_common())


def rvol_pace_split(day: str, threshold: float = 1.64) -> str:
    """Today's fills split by the observe-mode volume pace stamped at entry."""
    groups: dict = {"pace >= %.2f" % threshold: [], "pace < %.2f" % threshold: [],
                    "pace unknown": []}
    for line in open(os.path.join(ROOT, "ai_reports", "outcomes.jsonl")):
        try:
            r = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        et = r.get("entry_time")
        if not isinstance(et, (int, float)) or bars.day_of(et) != day:
            continue
        pace = (r.get("features") or {}).get("rvol_pace_sip")
        px, q, pl = r.get("entry_price"), r.get("total_qty"), r.get("realized_pl_usd")
        if not px or not q or not isinstance(pl, (int, float)):
            continue
        key = ("pace unknown" if pace is None else
               ("pace >= %.2f" % threshold if pace >= threshold else "pace < %.2f" % threshold))
        groups[key].append((pl / (float(px) * float(q)) * 100, pl))
    lines = [f"{'group':<16}{'fills':>6}{'win':>6}{'net/trade':>11}{'P/L':>10}"]
    for k, v in groups.items():
        if not v:
            lines.append(f"{k:<16}{0:>6}")
            continue
        lines.append(f"{k:<16}{len(v):>6}{sum(a > 0 for a, _ in v) / len(v):>6.0%}"
                     f"{sum(a for a, _ in v) / len(v):>+10.3f}%{sum(b for _, b in v):>+10.2f}")
    return "\n".join(lines) + ("\n(observe mode: pace is stamped at the arm pass; "
                               "nothing was refused on it)")


def wedges(day: str) -> str:
    path = os.path.join(ROOT, "logs", "watchdog.log")
    if not os.path.exists(path):
        return "no watchdog log"
    hits = [l.rstrip() for l in open(path, errors="replace") if "WEDGED" in l]
    # watchdog lines carry only HH:MM:SS, so this is "recent", not strictly "today"
    return "\n".join(hits[-10:]) or "no WEDGED restarts in logs/watchdog.log"


def fidelity_verdict(day: str, wait_sec: float = 1800.0) -> str:
    """OK / DRIFT / MISSING from ~/session_snapshots/<day>/fidelity.json."""
    snap = os.path.join(os.path.expanduser("~"), "session_snapshots", day)
    path = os.path.join(snap, "fidelity.json")
    t0 = time.time()
    while not os.path.exists(path) and time.time() - t0 < wait_sec:
        time.sleep(30)                 # the replay starts 16:05 and waits for SIP
    if not os.path.exists(path):
        return (f"**MISSING** — no fidelity.json. The nightly replay failed or is still "
                f"running; see {os.path.join(snap, 'fidelity.log')}. Do not trust replay "
                f"A/Bs for this day until it lands.")
    try:
        f = json.load(open(path))
    except ValueError as e:
        return f"**MISSING** — unreadable fidelity.json: {e}"
    rec, prec = f.get("recall"), f.get("precision")
    ok = rec is not None and prec is not None and rec >= 0.5 and prec >= 0.5
    head = "**OK**" if ok else ("**DRIFT** — the replay does not reproduce live; find out why "
                                "before trusting any replay A/B (a skipped live code path, a "
                                "different data clock, a new stateful gate)")
    return (f"{head}\n\nwindow {f.get('window')} on {str(f.get('sha') or '')[:8]}: live buys "
            f"{f.get('live_buys')}, replay opens {f.get('replay_opens')}, matched within 90 s "
            f"{f.get('matched')} (recall {rec}, precision {prec}), book overlap "
            f"{f.get('book_overlap')}; live-only {', '.join(f.get('live_only') or []) or '—'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default=datetime.now(bars.ET).strftime("%Y-%m-%d"))
    args = ap.parse_args()
    day = args.day
    since = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=14)).strftime("%Y-%m-%d")
    steps = [
        ("Scorecard by day (last 2 weeks)", ["tools/scorecard.py", "--since", since], 1200),
        ("Scorecard by config fingerprint (today)",
         ["tools/scorecard.py", "--since", day, "--by", "config_fp"], 1200),
        ("Execution cost vs the SIP bid/ask", ["tools/exec_report.py", "--day", day], 900),
        ("Counterfactual: the day replayed",
         ["tools/studies/counterfactual_today.py", day], 1800),
        ("Premarket scan grade", ["tools/premarket_grade.py", "--day", day], 900),
        ("Cross funnel: where the -50 crosses died", ["tools/cross_funnel.py", "--day", day], 1800),
    ]
    out_dir = os.path.join(ROOT, "ai_reports", "nightly")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{day}.md")
    parts = [f"# Nightly report — {day}\n",
             f"Generated {datetime.now(bars.ET):%Y-%m-%d %H:%M} ET by tools/nightly_report.py\n"]
    for title, argv, timeout in steps:
        status, out = run(argv, timeout)
        parts.append(f"## {title}\n\n`{' '.join(argv)}` — {status}\n\n```\n{out}\n```\n")
        print(f"[nightly] {title}: {status}", flush=True)
    parts.append(f"## Arm refusals from the gates\n\n```\n{gate_refusals(day)}\n```\n")
    parts.append(f"## Volume-pace observe gate (would it have helped?)\n\n"
                 f"```\n{rvol_pace_split(day)}\n```\n")
    parts.append(f"## Watchdog: hung-engine restarts\n\n```\n{wedges(day)}\n```\n")
    parts.insert(2, f"## Replay fidelity\n\n{fidelity_verdict(day)}\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))
    print(f"[nightly] wrote {path}", flush=True)


if __name__ == "__main__":
    main()
