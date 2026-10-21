#!/usr/bin/env python3
"""Score three_minute_reread only if the reruns meet the result-skeptic's round-2 READY conditions (2026-10-21).

Waits until count.json, null.json and placebo.json were all written by the current modules and the held-out fetch
is complete, then checks:
  fetch     check_confirm_sample() returns 70 sessions, 2026-01-14 .. 2026-04-24
  count     110 scored sessions 2026-05-11 .. 2026-10-15, all 18 cells scored in both halves, RSI2T onsets within 1%
            of the 965bcacc run (457,699), S4 n >= 300 per half
  null      ok, 120 sessions, seed tag 'three-min-null', 72 t values, no missing/thin/over/investigate, max |t| within
            0.05 of the 965bcacc null (2.37)
  placebo   ok, 20 t values, none over or thin
Any failure prints the reason and does NOT score. Otherwise runs three_minute_reread.py --score with the READY note.

  EEH_ROOT=$PWD .venv/bin/python tools/studies/three_minute_autoscore.py [--wait-hours 6]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import three_minute_reread as T  # noqa: E402

PRIOR_RSI2T_ONSETS, PRIOR_NULL_MAX_T = 457_699, 2.37
READY_NOTE = ("result-skeptic round 2 2026-10-21: READY conditional on reruns by sha 7508902 (fetch 01-14..04-24, count, "
              "null max |t| ~2.37, placebo OK); conditions checked by three_minute_autoscore.py")


def _load(name: str) -> dict | None:
    p = os.path.join(T.OUT_DIR, name)
    return json.load(open(p)) if os.path.exists(p) else None


def ready_to_check() -> bool:
    ms = T.module_shas()
    objs = [_load(n) for n in ("count.json", "null.json", "placebo.json")]
    if not all(o and o.get("module_sha256") == ms for o in objs):
        return False
    try:
        T.check_confirm_sample()
    except SystemExit:
        return False
    return True


def conditions() -> list[str]:
    """Every failed condition, as text; empty means READY holds."""
    bad = []
    try:
        days = T.check_confirm_sample()
        if (days[0], days[-1], len(days)) != ("2026-01-14", "2026-04-24", 70):
            bad.append(f"fetch range {days[0]} .. {days[-1]} ({len(days)}), want 2026-01-14 .. 2026-04-24 (70)")
    except SystemExit as e:
        bad.append(f"fetch: {e}")
    c = _load("count.json") or {}
    s = c.get("scored_sessions") or []
    if len(s) != 110 or (s[:1], s[-1:]) != (["2026-05-11"], ["2026-10-15"]):
        bad.append(f"count sessions {len(s)} {s[:1]} .. {s[-1:]}")
    for group in ("standalone", "filters"):
        for cell, hs in (c.get(group) or {}).items():
            for h, v in hs.items():
                if not v.get("scored"):
                    bad.append(f"count: {cell} {h} not scored")
    on = (c.get("counts") or {}).get("RSI2T_onsets", 0)
    if abs(on - PRIOR_RSI2T_ONSETS) > 0.01 * PRIOR_RSI2T_ONSETS:
        bad.append(f"count: RSI2T onsets {on} vs {PRIOR_RSI2T_ONSETS}")
    for h in ("A", "B"):
        n = ((c.get("standalone") or {}).get("S4") or {}).get(h, {}).get("n", 0)
        if n < 300:
            bad.append(f"count: S4 {h} n {n} < 300")
    nl = _load("null.json") or {}
    if not (nl.get("ok") and nl.get("days") == 120 and nl.get("seed_tag") == "three-min-null" and nl.get("n_stats") == 72):
        bad.append(f"null: ok {nl.get('ok')} days {nl.get('days')} tag {nl.get('seed_tag')} n_stats {nl.get('n_stats')}")
    for k in ("missing", "thin", "over", "investigate"):
        if nl.get(k):
            bad.append(f"null {k}: {nl[k][:3]}")
    mt = nl.get("max_abs_t")
    if mt is None or abs(mt - PRIOR_NULL_MAX_T) > 0.05:
        bad.append(f"null max |t| {mt} vs {PRIOR_NULL_MAX_T}: something else changed")
    pl = _load("placebo.json") or {}
    if not (pl.get("ok") and pl.get("n_stats") == 20) or pl.get("over") or pl.get("thin"):
        bad.append(f"placebo: ok {pl.get('ok')} n_stats {pl.get('n_stats')} over {pl.get('over')} thin {pl.get('thin')}")
    return bad


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait-hours", type=float, default=6.0)
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    deadline = time.time() + a.wait_hours * 3600
    while not ready_to_check():
        if time.time() > deadline:
            print("autoscore: inputs never became complete; NOT scored")
            print("\n".join(conditions()))
            return
        time.sleep(60)
    bad = conditions()
    if bad:
        print("autoscore: READY conditions NOT met; NOT scored:")
        print("\n".join(f"  - {b}" for b in bad))
        return
    print("autoscore: all READY conditions met; scoring", flush=True)
    sys.exit(subprocess.call([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                           "three_minute_reread.py"), "--score", "--workers",
                              str(a.workers), "--skeptic-ready", READY_NOTE]))


if __name__ == "__main__":
    main()
