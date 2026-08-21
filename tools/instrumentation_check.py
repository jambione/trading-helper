#!/usr/bin/env python3
"""
instrumentation_check.py — is TODAY actually being recorded?

Every other tool in here reports on a day that is already over. By then a
logging gap is not a bug, it is a lost day: on 2026-08-06 look_reason was
written on every row and logged as null on every row, and that was only
discovered the next morning, after the session it was supposed to measure.

This is the mid-session version of that question, and it is deliberately
cheap enough to run every hour: no API calls, no forward returns, no
episode reconstruction. It reads the four logs, and answers

    are rows arriving, and do they carry the fields decisions were made on?

VERDICTS
  RECORDING  rows arrived within the freshness window
  STALE      the log has today's rows, but none recently
  SILENT     no rows today at all

SILENT is only a failure once the desk is actually working — before
ai_watch_start_time there is nothing to record, and this says so rather than
crying wolf every pre-market morning.

Exit code is 1 when something that should be recording is not, so it can be
run from a watchdog rather than read by a human.

USAGE
    venv/bin/python tools/instrumentation_check.py
    venv/bin/python tools/instrumentation_check.py --day 2026-08-07 --stale 600
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from ai_paths import find_report_file, resolve_report_dir  # noqa: E402
from config import load_config  # noqa: E402


def _report(name: str) -> Path:
    return find_report_file(name) or (resolve_report_dir() / name)


LOGS = {
    "shadow": _report("shadow.jsonl"),
    "rejects": _report("rejects.jsonl"),
    "events": _report("events.jsonl"),
    "outcomes": _report("outcomes.jsonl"),
}

# Fields a decision was made on. If a gate reads it, the record must carry it
# — a gate scored on absent inputs produces a confident, fake verdict.
DECISION_FIELDS = {
    # pctr_slow and cm_rsi are the levels the live buy rule compares against
    # rte_threshold / rte_confluence_max / cm_rsi_buy_max. They belong here
    # rather than among the nice-to-haves: _tv_exh_rsi_allows_buy refuses with
    # "wait_rsi" when cm_rsi is None, so a session that never records one is a
    # session the desk could not have bought in, whatever the watch looked like.
    "shadow": ["score", "rvol", "pct_change", "look_reason", "arm_why",
               "pctr", "pctr_slow", "cm_rsi"],
    "rejects": ["reason", "price", "score", "rvol", "pct_change", "look_reason"],
}

# Never-present is a fault only for fields the product still needs in order
# to *log a decision*. look_reason / pctr_slow / cm_rsi are often sparse
# (22% look_reason on 2026-08-21) and tripped rc=1 all day while 13k shadow
# rows were writing. Coverage is still printed.
REQUIRED_FIELDS = {
    "shadow": ["score", "arm_why"],
    "rejects": ["reason"],
}

# Logs that only grow on an event that may legitimately never happen today.
# Silence here is information, not a fault.
EVENT_DRIVEN = {"outcomes"}


def _rows(path: Path, day: date) -> list[dict]:
    out: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
            if datetime.fromtimestamp(float(r.get("ts"))).date() != day:
                continue
        except Exception:
            continue
        if isinstance(r, dict):
            out.append(r)
    return out


def _desk_is_working(cfg: dict, now: datetime) -> bool:
    """True once the desk should have something to record.

    Reads ai_watch_start_time rather than assuming the open: the watch loop
    starts before the bell, and a check that only trusts 09:30 would call a
    working pre-market desk SILENT.
    """
    raw = str(cfg.get("ai_watch_start_time") or "09:00").strip()
    try:
        hh, mm = (int(x) for x in raw.split(":")[:2])
        start = dtime(hh, mm)
    except Exception:
        start = dtime(9, 0)
    return now.time() >= start and now.weekday() < 5


def check(day: date, stale_sec: float, now: datetime) -> tuple[list[str], bool]:
    cfg = load_config()
    working = _desk_is_working(cfg, now) if day == now.date() else True
    lines: list[str] = []
    failed = False

    lines.append(f"  INSTRUMENTATION — {day}  (checked {now:%H:%M:%S})")
    lines.append(f"  desk expected to be working: {'yes' if working else 'no'}"
                 f"  (ai_watch_start_time={cfg.get('ai_watch_start_time')})")
    lines.append("")
    lines.append(f"  {'log':<10}{'rows':>7}{'last row':>12}   verdict")

    counts: dict[str, list[dict]] = {}
    for name, path in LOGS.items():
        rows = _rows(path, day)
        counts[name] = rows
        if not rows:
            verdict = "SILENT"
            age_s = "—"
            if working and name not in EVENT_DRIVEN:
                verdict = "SILENT  <- expected rows by now"
                failed = True
        else:
            age = now.timestamp() - max(float(r.get("ts") or 0) for r in rows)
            age_s = f"{age / 60:.0f}m ago"
            if day != now.date():
                verdict = "RECORDING"
            elif age > stale_sec:
                verdict = "STALE"
                if working and name not in EVENT_DRIVEN:
                    verdict = "STALE   <- rows stopped arriving"
                    failed = True
            else:
                verdict = "RECORDING"
        lines.append(f"  {name:<10}{len(rows):>7}{age_s:>12}   {verdict}")

    lines.append("")
    lines.append("  DECISION FIELDS  (present on today's rows)")
    for name, fields in DECISION_FIELDS.items():
        rows = counts.get(name) or []
        if not rows:
            lines.append(f"  {name:<10} no rows yet")
            continue
        parts = []
        for f in fields:
            n = sum(1 for r in rows if r.get(f) is not None)
            pct = 100.0 * n / len(rows)
            parts.append(f"{f}={pct:.0f}%")
            required = REQUIRED_FIELDS.get(name, ())
            # A required field that is NEVER present is the failure mode this
            # exists for. Optional fields still print coverage.
            if n == 0 and f in required:
                failed = True
        lines.append(f"  {name:<10} " + "  ".join(parts))

    if failed:
        lines.append("")
        lines.append("  ^ something that should be recording is not. A day that"
                     " is not recorded\n    cannot be learned from — fix it"
                     " during the session, not after.")
    return lines, failed


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--day", help="YYYY-MM-DD (default today)")
    ap.add_argument("--stale", type=float, default=900.0,
                    help="seconds without a row before STALE (default 900)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    now = datetime.now()
    day = date.fromisoformat(args.day) if args.day else now.date()
    lines, failed = check(day, args.stale, now)

    if args.json:
        print(json.dumps({"day": str(day), "ok": not failed,
                          "report": lines}, indent=2))
    else:
        print(f"\n{'=' * 62}")
        print("\n".join(lines))
        print(f"{'=' * 62}\n")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
