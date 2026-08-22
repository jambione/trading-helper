#!/usr/bin/env python3
"""Does the desk actually do what its config and its logs claim?

Every wrong conclusion this desk reached in 2026-08 came from plumbing, not
from analysis. A knob that reads 50 while the live gate ignores it. A screen
that sampled A-K and printed a verdict on "the universe". A simulator that
placed zero trades because it inherited a product flag. A secret scan that
had never run. None of those announced themselves; each produced a
plausible number instead.

So this checks the joins rather than the code:

  DEAD KNOBS        in bot_config.json, read by nothing
  UNDECLARED        read by code, absent from DEFAULT_CONFIG
  FINGERPRINT       changes what the desk buys/sells but is not in
                    learn_stamps._FINGERPRINT_KEYS, so two different
                    regimes would stamp the same config_fp
  LOG COVERAGE      fields the screens depend on, and how often they are
                    actually populated
  CODE FRESHNESS    running processes older than the code they claim to run

Read-only. Exit 1 if anything CRITICAL is found, so a watchdog can run it.

    python3 tools/setup_audit.py
    python3 tools/setup_audit.py --days 5
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

# Knobs whose value changes what is bought, sold, or sized. Anything matching
# these and missing from the fingerprint is a CRITICAL: the ledger would pool
# two regimes into one mean, which is how 2026-08-19 lost its own history.
REGIME_PATTERNS = (
    r"^ai_local_trail_", r"^ai_exit_", r"^ai_watch_min_", r"^ai_watch_open_seed",
    r"^ai_risk_", r"^ai_max_position", r"^ai_daily_loss", r"^desk_product$",
    r"^ai_h[34]_paper$", r"^ai_dead_trade_", r"^ai_max_spread_r$",
)
# Read by machinery, not strategy — absent from the fingerprint on purpose.
FINGERPRINT_EXEMPT = {
    "ai_local_trail_enabled", "ai_exit_left_overbought_deferred",
    "ai_watch_min_price", "ai_watch_min_stop_pct",
}

# Fields the screens join on. (file, field, floor%) — floor is the coverage
# below which the screen that uses it is reporting on a minority of rows.
LOG_FIELDS = [
    ("outcomes.jsonl", "realized_r_multiple", 95),
    ("outcomes.jsonl", "entry_time", 95),
    ("outcomes.jsonl", "stop_price", 90),
    ("outcomes.jsonl", "config_fp", 90),
    ("outcomes.jsonl", "hold_sec", 90),
    ("shadow.jsonl", "admit_ts", 80),
    ("shadow.jsonl", "arm_ok", 90),
    ("shadow.jsonl", "source", 80),
]

CRITICAL: list[str] = []
WARN: list[str] = []


def _repo_text() -> str:
    parts = []
    for p in ROOT.rglob("*.py"):
        s = str(p)
        if "/.claude/" in s or "/.venv" in s or "/worktrees/" in s:
            continue
        try:
            parts.append(p.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return "\n".join(parts)


def audit_knobs(live: dict) -> None:
    print("\n=== KNOBS ===")
    from config import DEFAULT_CONFIG
    body = _repo_text()
    dead = []
    for k in sorted(live):
        # A knob is live if any code mentions it by name at all.
        if not re.search(rf'["\']{re.escape(k)}["\']', body):
            dead.append(k)
    if dead:
        WARN.append(f"{len(dead)} config keys read by no code")
        print(f"  DEAD KNOBS ({len(dead)}) — set in bot_config.json, read by nothing:")
        for k in dead:
            print(f"    {k} = {live[k]!r}")
    else:
        print("  OK   every key in bot_config.json is referenced by code")

    undeclared = [k for k in sorted(live) if k not in DEFAULT_CONFIG]
    if undeclared:
        WARN.append(f"{len(undeclared)} live keys absent from DEFAULT_CONFIG")
        print(f"  UNDECLARED ({len(undeclared)}) — live but not in DEFAULT_CONFIG:")
        for k in undeclared:
            print(f"    {k} = {live[k]!r}")
    else:
        print("  OK   every live key is declared in DEFAULT_CONFIG")


def audit_fingerprint(live: dict) -> None:
    print("\n=== REGIME FINGERPRINT ===")
    import learn_stamps
    fp = set(learn_stamps._FINGERPRINT_KEYS)
    from config import DEFAULT_CONFIG
    universe = set(DEFAULT_CONFIG) | set(live)
    missing = []
    for k in sorted(universe):
        if k in fp or k in FINGERPRINT_EXEMPT:
            continue
        if any(re.search(p, k) for p in REGIME_PATTERNS):
            missing.append(k)
    if missing:
        CRITICAL.append(f"{len(missing)} regime knobs not fingerprinted")
        print(f"  CRITICAL ({len(missing)}) — changes what the desk buys/sells,")
        print("  but two different settings would stamp the SAME config_fp:")
        for k in missing:
            print(f"    {k}  (live: {live.get(k, '<default>')!r})")
    else:
        print(f"  OK   {len(fp)} keys fingerprinted; no regime knob is missing")


def audit_logs(days: int) -> None:
    print("\n=== LOG COVERAGE ===")
    from ai_paths import resolve_report_dir
    import bars
    d = Path(resolve_report_dir())
    recent = set()
    try:
        import datetime as _dt
        today = _dt.date.today()
        for i in range(days + 4):
            recent.add((today - _dt.timedelta(days=i)).isoformat())
    except Exception:
        pass
    for fname, field, floor in LOG_FIELDS:
        p = d / fname
        if not p.exists():
            WARN.append(f"{fname} missing")
            print(f"  MISSING  {fname}")
            continue
        seen = Counter()
        n = 0
        with p.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                ts = r.get("ts") or r.get("entry_time")
                if ts and recent:
                    try:
                        if bars.day_of(ts) not in recent:
                            continue
                    except Exception:
                        pass
                n += 1
                if r.get(field) is not None or (r.get("features") or {}).get(field) is not None:
                    seen[field] += 1
        if n == 0:
            print(f"  (no recent rows) {fname}:{field}")
            continue
        pct = 100 * seen[field] / n
        tag = "OK  " if pct >= floor else "LOW "
        if pct < floor:
            WARN.append(f"{fname}:{field} coverage {pct:.0f}% < {floor}%")
        print(f"  {tag} {fname:<18}{field:<22}{pct:>5.0f}%  (n={n}, floor {floor}%)")


def _proc_start_epoch(pid: str) -> float | None:
    """Epoch seconds a pid started, or None when it cannot be determined.

    ``ps -o lstart=`` is the portable field here — BSD/macOS ps has no
    ``etimes``. Returns None rather than a guess so the caller can raise.
    """
    import time
    try:
        raw = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                             capture_output=True, text=True).stdout.strip()
    except OSError:
        return None
    if not raw:
        return None
    for fmt in ("%a %b %d %H:%M:%S %Y", "%a %b  %d %H:%M:%S %Y"):
        try:
            return time.mktime(time.strptime(" ".join(raw.split()), fmt))
        except ValueError:
            continue
    return None


def audit_freshness() -> None:
    print("\n=== CODE FRESHNESS ===")
    procs = {
        "ai_trader.py": ["ai_positions.py", "ai_entry_watch.py", "ai_trader.py",
                         "config.py", "learn_stamps.py"],
        "dashboard.py": ["dashboard.py"],
        "tools/watchdog.py": ["tools/watchdog.py"],
    }
    for proc, files in procs.items():
        try:
            pid = subprocess.run(["pgrep", "-f", proc], capture_output=True,
                                 text=True).stdout.split()
        except OSError:
            pid = []
        if not pid:
            print(f"  DOWN {proc}")
            WARN.append(f"{proc} not running")
            continue
        proc_start = _proc_start_epoch(pid[0])
        if proc_start is None:
            # Unmeasurable is not OK. The first version of this check used
            # `ps -o etimes=`, which macOS does not have; the error text
            # failed to parse, the fallback was 0, and every process read as
            # fresh. A check that cannot measure must say so.
            CRITICAL.append(f"cannot read start time for {proc}")
            print(f"  CRITICAL {proc}: cannot determine process start time")
            continue
        stale = [f for f in files
                 if (ROOT / f).exists() and (ROOT / f).stat().st_mtime > proc_start]
        if stale:
            CRITICAL.append(f"{proc} predates {', '.join(stale)}")
            print(f"  CRITICAL {proc} started before its own code was written:")
            for f in stale:
                print(f"    {f} is newer — the running process does NOT have it")
        else:
            print(f"  OK   {proc} is newer than the code it runs")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--days", type=int, default=5)
    args = ap.parse_args()
    live = json.loads((ROOT / "config" / "bot_config.json").read_text("utf-8"))
    print(f"setup audit — {len(live)} live keys")
    audit_knobs(live)
    audit_fingerprint(live)
    audit_logs(args.days)
    audit_freshness()
    print("\n" + "=" * 60)
    if CRITICAL:
        print(f"  {len(CRITICAL)} CRITICAL")
        for c in CRITICAL:
            print(f"    - {c}")
    if WARN:
        print(f"  {len(WARN)} warning(s)")
        for w in WARN:
            print(f"    - {w}")
    if not CRITICAL and not WARN:
        print("  clean")
    return 1 if CRITICAL else 0


if __name__ == "__main__":
    raise SystemExit(main())
