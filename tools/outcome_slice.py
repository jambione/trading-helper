#!/usr/bin/env python3
"""
outcome_slice.py — slice realized outcomes by decision-time feature.

Answers "did EXT-tagged entries actually do better?" and the like, off the
feature vector ai_entry_watch._entry_features attaches at entry and
ai_positions._record_outcome carries into claude_reports/outcomes.jsonl. No
join: each row already holds both the features and the result.

WHAT THIS IS NOT
    Not an A/B test. Arms are not randomized — the desk chose every trade it
    took, so a slice compares populations that differ in more than the feature
    being sliced (a name that fired EXT was also moving, liquid and trending).
    Live A/B at this desk's volume is arithmetically hopeless besides: with
    per-trade noise around 1R, separating a 0.1R edge needs on the order of
    780 trades per arm. Real A/B belongs in the replay harness
    (tools/ab_bench.py), which randomizes nothing but at least holds the
    universe fixed and re-runs variants over the same bars.

    Treat every number here as a hypothesis to take to the replay harness.
    The --power column exists to make that unavoidable.

HONESTY RULES (inherited from ab_bench.py)
    • Rows with no realized_r_multiple are excluded, never zero-filled — a
      trade whose exit could not be priced is missing, not flat.
    • Rows predating the feature vector are reported separately, not silently
      pooled into whichever bucket a missing key defaults to.
    • Every group carries its own n and an explicit underpowered flag. A mean
      over 4 trades is an anecdote and is labelled one.
    • First-half / second-half means are shown for any group large enough to
      split. The indicator gate that looked real in benchmarks/ab_bench_*
      (t=+2.16) was entirely in the first half of its sample and gone in the
      second; a single pooled mean hid that.

USAGE
    venv/bin/python tools/outcome_slice.py
    venv/bin/python tools/outcome_slice.py --by look_reason --by source
    venv/bin/python tools/outcome_slice.py --min-n 10 --json
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from ai_paths import find_report_file, resolve_report_dir  # noqa: E402

# Resolve the way the desk writes (ai_reports/ preferred, claude_reports/
# legacy) — a hardcoded path silently freezes after a migration.
OUTCOMES = (find_report_file("outcomes.jsonl")
            or resolve_report_dir() / "outcomes.jsonl")

# Per-trade R is roughly unit-variance on this desk, so the sample needed to
# resolve an effect is ~7.85/delta^2 (two-sided, alpha .05, power .80).
_Z_SUM_SQ = (1.959964 + 0.841621) ** 2


def required_n(delta_r: float, sd: float = 1.0) -> int:
    """Trades per arm needed to resolve *delta_r*, one sample against zero."""
    if delta_r <= 0:
        return 0
    return int(math.ceil(_Z_SUM_SQ * (sd ** 2) / (delta_r ** 2)))


def load_outcomes(path: Path = OUTCOMES) -> tuple[list[dict], dict[str, int]]:
    """Rows usable for slicing, plus a census of what was dropped and why."""
    rows: list[dict] = []
    skipped = {"unparsed": 0, "no_realized_r": 0, "no_features": 0}
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return rows, skipped
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            skipped["unparsed"] += 1
            continue
        if not isinstance(r, dict):
            skipped["unparsed"] += 1
            continue
        # An unpriceable exit is missing data, not a flat trade.
        if r.get("realized_r_multiple") is None:
            skipped["no_realized_r"] += 1
            continue
        if not isinstance(r.get("features"), dict):
            # Predates the feature vector (or a path that never set one).
            skipped["no_features"] += 1
            continue
        rows.append(r)
    return rows, skipped


def _bucket(feat: dict, key: str) -> Any:
    """Group label for *key*. Membership keys read the criteria list."""
    if key.startswith("crit:"):
        want = key.split(":", 1)[1]
        return want in (feat.get("criteria") or [])
    if key == "entry_hour_et":
        h = feat.get(key)
        if h is None:
            return None
        # Open drive / midday / into-the-flatten: a 15:45 entry facing the
        # 15:50 liquidate is a different trade from the same signal at 09:35.
        if h < 10.0:
            return "open<10"
        if h < 14.0:
            return "mid10-14"
        return "late>=14"
    if key == "rvol":
        v = feat.get(key)
        if v is None:
            return None
        return "rvol<2" if v < 2.0 else "rvol2-4" if v < 4.0 else "rvol>=4"
    return feat.get(key)


def summarize(group: list[dict], min_n: int) -> dict:
    rs = [float(r["realized_r_multiple"]) for r in group]
    n = len(rs)
    mean = statistics.fmean(rs)
    out: dict[str, Any] = {
        "n": n,
        "mean_r": round(mean, 4),
        "median_r": round(statistics.median(rs), 4),
        "win_rate": round(100.0 * sum(1 for x in rs if x > 0) / n, 1),
        "total_r": round(sum(rs), 3),
        "underpowered": n < min_n,
    }
    if n >= 2:
        sd = statistics.stdev(rs)
        out["sd_r"] = round(sd, 3)
        se = sd / math.sqrt(n)
        out["t"] = round(mean / se, 2) if se > 0 else None
        # What this sample could actually have resolved, and what the observed
        # effect would need. Reported even when t looks good — a t of 2 on
        # n=6 is noise wearing a suit.
        out["n_needed_for_observed"] = required_n(abs(mean), sd) if mean else None
    if n >= 4:
        # Chronological halves — an edge present only in the first half is
        # the signature that killed the indicator gate in ab_bench.
        ordered = [float(r["realized_r_multiple"])
                   for r in sorted(group, key=lambda x: x.get("entry_time") or x.get("ts") or 0)]
        half = len(ordered) // 2
        a, b = ordered[:half], ordered[half:]
        out["half_a_mean"] = round(statistics.fmean(a), 4)
        out["half_b_mean"] = round(statistics.fmean(b), 4)
        # Same sign in both halves, and neither half flat.
        out["holds_both_halves"] = bool(
            out["half_a_mean"] * out["half_b_mean"] > 0)
    return out


def slice_by(rows: list[dict], key: str, min_n: int) -> dict[str, dict]:
    groups: dict[Any, list[dict]] = {}
    for r in rows:
        label = _bucket(r.get("features") or {}, key)
        if label is None:
            label = "(unrecorded)"
        groups.setdefault(str(label), []).append(r)
    return {k: summarize(v, min_n) for k, v in sorted(groups.items())}


DEFAULT_KEYS = [
    "source", "look_reason", "cm_rsi_rising", "cm_ok", "pctr_ok", "macd_ok",
    "entry_hour_et", "rvol", "crit:ext", "crit:rvol", "crit:big_move",
    "crit:flag", "crit:score",
]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--by", action="append", dest="keys",
                    help="feature to slice by (repeatable). Default: a standard set.")
    ap.add_argument("--min-n", type=int, default=30,
                    help="below this a group is flagged underpowered (default 30)")
    ap.add_argument("--path", default=str(OUTCOMES))
    ap.add_argument("--json", action="store_true", help="emit JSON, not tables")
    args = ap.parse_args()

    rows, skipped = load_outcomes(Path(args.path))
    keys = args.keys or DEFAULT_KEYS

    if args.json:
        print(json.dumps({
            "usable": len(rows),
            "skipped": skipped,
            "slices": {k: slice_by(rows, k, args.min_n) for k in keys},
        }, indent=2, default=str))
        return

    print(f"\noutcomes: {args.path}")
    print(f"usable rows: {len(rows)}   skipped: "
          f"{skipped['no_features']} pre-feature-vector, "
          f"{skipped['no_realized_r']} unpriced exit, "
          f"{skipped['unparsed']} unparsed")

    if not rows:
        print("\nNothing to slice yet. A row becomes usable once a position "
              "opened with a feature vector has CLOSED with a priced exit.")
        print(f"To resolve a 0.10R edge you would need ~{required_n(0.10)} "
              f"trades per arm; 0.25R needs ~{required_n(0.25)}.")
        return

    for key in keys:
        table = slice_by(rows, key, args.min_n)
        print(f"\n── by {key} " + "─" * max(0, 58 - len(key)))
        print(f"{'group':<14}{'n':>5}{'mean R':>9}{'win%':>7}{'t':>7}"
              f"{'halfA':>8}{'halfB':>8}  note")
        for label, s in table.items():
            t = s.get("t")
            ha = s.get("half_a_mean")
            hb = s.get("half_b_mean")
            note = []
            if s["underpowered"]:
                note.append(f"UNDERPOWERED (n<{args.min_n})")
            if s.get("holds_both_halves") is False:
                note.append("SIGN FLIPS ACROSS HALVES")
            nn = s.get("n_needed_for_observed")
            if nn and nn > s["n"]:
                note.append(f"needs n~{nn}")
            print(f"{label:<14}{s['n']:>5}{s['mean_r']:>9.3f}{s['win_rate']:>7.1f}"
                  f"{(f'{t:.2f}' if t is not None else '—'):>7}"
                  f"{(f'{ha:.3f}' if ha is not None else '—'):>8}"
                  f"{(f'{hb:.3f}' if hb is not None else '—'):>8}"
                  f"  {'; '.join(note)}")

    print("\nNo number here is evidence on its own — arms are not randomized "
          "and the desk chose every trade.\nTake anything promising to "
          "tools/ab_bench.py and see if it survives out of sample.")


if __name__ == "__main__":
    main()
