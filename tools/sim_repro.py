#!/usr/bin/env python3
"""
sim_repro.py — reproduce the 2026-08-11 edge-mode numbers from a frozen fixture.

WHY THIS EXISTS
  ai_reports/ is gitignored and its .jsonl files are append-only live logs that
  already span several sessions. Every number in the edge-mode write-up came
  from those logs on the Mac mini, which means none of it was reproducible and
  nothing would have caught the logs rotating out from under the analysis.

  It also guards a mistake that actually happened: reading outcomes.jsonl
  WITHOUT a day filter mixes sessions and invents a cohort of "blind" entries
  belonging to other days. The pinned values below are day-filtered.

HOW IT WORKS
  Decompresses tests/fixtures/sim_<day>/*.jsonl.gz into a temp directory,
  points AI_REPORT_DIR at it (the escape hatch documented in ai_paths.py), runs
  each sim with --json, and compares the headline numbers against expected.json.

  Section 3 of sim_edge_mode_ab (trending seed A/B) is NOT pinned: its input,
  trending_stocks.json, is gitignored generated data. Runs use --no-trending.

USAGE
    .venv/bin/python tools/sim_repro.py            # verify against pinned
    .venv/bin/python tools/sim_repro.py --update   # re-pin after intended change
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
DAY = "2026-08-11"
FIXTURE = _ROOT / "tests" / "fixtures" / f"sim_{DAY}"
EXPECTED = FIXTURE / "expected.json"

# Tolerance for float compare. The sims are deterministic given identical
# input, so this only absorbs float formatting, not real drift.
TOL = 1e-6


def unpack(dest: Path) -> None:
    """Decompress the fixture's .jsonl.gz into *dest*."""
    n = 0
    for gz in sorted(FIXTURE.glob("*.jsonl.gz")):
        out = dest / gz.name[:-3]
        with gzip.open(gz, "rb") as fi, open(out, "wb") as fo:
            shutil.copyfileobj(fi, fo)
        n += 1
    if not n:
        raise SystemExit(f"no fixture data in {FIXTURE}")


def run(script: str, args: list[str], report_dir: Path) -> dict[str, Any]:
    env = dict(os.environ)
    env["AI_REPORT_DIR"] = str(report_dir)
    cmd = [sys.executable, str(_ROOT / "tools" / script), "--day", DAY,
           "--json", *args]
    p = subprocess.run(cmd, capture_output=True, text=True, env=env,
                       cwd=_ROOT, check=False)
    if p.returncode != 0:
        raise SystemExit(f"{script} failed:\n{p.stderr}")
    return json.loads(p.stdout)


def headline(ab: dict, sweep: dict, hyb: dict) -> dict[str, Any]:
    """The numbers that appear in the write-up. Anything here is load-bearing."""
    arm, ex = ab["arm"], ab["exit"]
    b = hyb["book"]["books"]
    day = hyb["daybook"]
    core = sweep["core"]
    return {
        # arm gate
        "in_zone_n": arm["in_zone_n"],
        "arm_both": arm["both"],
        "arm_cont_only": arm["continuation_only"],
        "arm_scalp_only": arm["scalp_only"],
        "fwd_scalp_mean_pct": arm["fwd_scalp_mean_pct"],
        "fwd_cont_mean_pct": arm["fwd_cont_mean_pct"],
        "fwd_cont_only_mean_pct": arm["fwd_cont_only_mean_pct"],
        # hybrid arm gate must equal scalp exactly
        "hybrid_arms": hyb["arm"]["hybrid_arms"],
        "hybrid_scalp_mismatch": hyb["arm"]["hybrid_vs_scalp_mismatch"],
        # exit A/B
        "exit_n_scored": ex["n_scored"],
        "exit_sum_live_r": ex["sum_live_r"],
        "exit_sum_cont_r": ex["sum_cont_r"],
        "exit_sum_delta_r": ex["sum_delta_r"],
        # books over the left_overbought trades
        "book_live_r": b["live"]["sum_r"],
        "book_live_usd": b["live"]["sum_usd"],
        "book_cont_r": b["cont"]["sum_r"],
        "book_cont_usd": b["cont"]["sum_usd"],
        "book_hybrid_r": b["hybrid_strict"]["sum_r"],
        "book_hybrid_usd": b["hybrid_strict"]["sum_usd"],
        "book_hybrid_n": b["hybrid_strict"]["n"],
        "book_hybrid_loose_r": b["hybrid_loose"]["sum_r"],
        # whole session (day-filtered — see module docstring)
        "day_n_outcomes": day["n_outcomes"],
        "day_live_usd": day["live_total_usd"],
        "day_hybrid_usd": day["hybrid_total_usd"],
        "day_swing_usd": day["swing_usd"],
        "day_states": {k: v["n"] for k, v in sorted(day["by_state"].items())},
        # heat_min sweep — the overbought core it is measured against
        "core_n": core["n"],
        "core_mean": core["mean"],
        "core_median": core["median"],
        "heating_n": sweep["heating_n"],
    }


def compare(got: dict, want: dict) -> list[str]:
    bad = []
    for k in sorted(set(got) | set(want)):
        g, w = got.get(k, "<missing>"), want.get(k, "<unpinned>")
        if isinstance(g, float) and isinstance(w, float):
            if abs(g - w) <= TOL:
                continue
        elif g == w:
            continue
        bad.append(f"{k}: got {g!r}, pinned {w!r}")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser(description="reproduce pinned sim numbers")
    ap.add_argument("--update", action="store_true",
                    help="re-pin expected.json from this run")
    args = ap.parse_args()

    with tempfile.TemporaryDirectory(prefix="sim_repro_") as td:
        d = Path(td)
        unpack(d)
        ab = run("sim_edge_mode_ab.py", ["--no-trending"], d)
        sweep = run("sim_heat_min_sweep.py", [], d)
        hyb = run("sim_hybrid.py", [], d)

    got = headline(ab, sweep, hyb)

    if args.update:
        EXPECTED.write_text(json.dumps(got, indent=2, sort_keys=True) + "\n")
        print(f"pinned {len(got)} values -> {EXPECTED.relative_to(_ROOT)}")
        return 0

    if not EXPECTED.exists():
        print(f"no pinned file at {EXPECTED}; run with --update", file=sys.stderr)
        return 2

    want = json.loads(EXPECTED.read_text())
    bad = compare(got, want)

    print(f"SIM REPRO — {DAY}  (fixture: {FIXTURE.relative_to(_ROOT)})")
    print(f"  checked {len(got)} pinned values")
    if bad:
        print(f"  DRIFT in {len(bad)}:")
        for b in bad:
            print(f"    {b}")
        return 1
    print("  OK — all values reproduce")
    print()
    print(f"  in-zone polls        {got['in_zone_n']}")
    print(f"  hybrid arms          {got['hybrid_arms']}"
          f"  (mismatch vs scalp: {got['hybrid_scalp_mismatch']})")
    print(f"  LOB trades           {got['exit_n_scored']}"
          f"  ({got['book_hybrid_n']} pass the overbought gate)")
    print(f"  book  live / hybrid  ${got['book_live_usd']:+.2f}"
          f" / ${got['book_hybrid_usd']:+.2f}")
    print(f"  day   live / hybrid  ${got['day_live_usd']:+.2f}"
          f" / ${got['day_hybrid_usd']:+.2f}"
          f"   swing ${got['day_swing_usd']:+.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
