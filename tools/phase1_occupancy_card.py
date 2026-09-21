#!/usr/bin/env python3
"""Phase 1 Occupancy & Square Attribution Card.

Full-day score for Phase 1 paper desk:
  1. Minute Occupancy: % RTH minutes with open >= 1 / >= 2 / >= 3
  2. Watch Board Mix: square vs pre_square vs far seat residency
  3. Square Extension: late_into_square vs dead_follow_through vs extended
  4. Exit Race: left_overbought vs local_trail vs min_hold deferrals

Usage:
    .venv/bin/python tools/phase1_occupancy_card.py --day 2026-09-21
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai_paths import resolve_report_dir  # noqa: E402
from tools.open_minutes_scoreboard import score_day  # noqa: E402

ET = ZoneInfo("America/New_York")


def _iter_jsonl(path: Path):
    if not path.exists():
        return
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield row


def compute_card(day: str, report_dir: Path | None = None) -> dict[str, Any]:
    rdir = report_dir or Path(resolve_report_dir())

    # 1. Minute Occupancy from position_shadow
    shadow_path = rdir / "position_shadow.jsonl"
    occ_card = score_day(day, shadow_path=shadow_path, outcomes_path=rdir / "outcomes.jsonl")

    # 2. Outcomes for day
    outcomes_path = rdir / "outcomes.jsonl"
    t0_dt = datetime.fromisoformat(day).replace(hour=0, minute=0, second=0, tzinfo=ET)
    t1_dt = datetime.fromisoformat(day).replace(hour=23, minute=59, second=59, tzinfo=ET)
    t0 = t0_dt.timestamp()
    t1 = t1_dt.timestamp()

    day_outcomes = []
    for row in _iter_jsonl(outcomes_path):
        ts = row.get("ts") or row.get("exit_time")
        if ts is not None and t0 <= float(ts) <= t1:
            day_outcomes.append(row)

    # Extension breakdown
    n_outcomes = len(day_outcomes)
    mfe_vals = []
    r_vals = []
    ext_classes = Counter()
    close_reasons = Counter()
    min_hold_blocks_sum = 0
    mfe_ge_015 = 0

    for out in day_outcomes:
        cr = str(out.get("close_reason") or "unknown")
        close_reasons[cr] += 1
        r = out.get("realized_r_multiple")
        if r is not None:
            try:
                r_vals.append(float(r))
            except (TypeError, ValueError):
                pass
        mfe = out.get("mfe_r")
        if mfe is not None:
            try:
                mf = float(mfe)
                mfe_vals.append(mf)
                if mf >= 0.15:
                    mfe_ge_015 += 1
            except (TypeError, ValueError):
                pass
        
        # Extension classification
        eclass = out.get("extension_class")
        if not eclass:
            time_in_sq = out.get("time_in_square_before_entry_sec")
            if time_in_sq is not None and float(time_in_sq) >= 60.0:
                eclass = "late_into_square"
            elif mfe is not None and float(mfe) < 0.15:
                eclass = "dead_follow_through"
            elif mfe is not None and float(mfe) >= 0.15:
                eclass = "extended"
            else:
                eclass = "unclassified"
        ext_classes[eclass] += 1

        mhb = out.get("min_hold_blocks")
        if mhb:
            try:
                min_hold_blocks_sum += int(mhb)
            except (TypeError, ValueError):
                pass

    # 3. Events for Day (Admit / Steal / Defer)
    events_path = rdir / "events.jsonl"
    event_counts = Counter()
    seat_mix = Counter()

    for ev in _iter_jsonl(events_path):
        ts = ev.get("ts")
        if ts is not None and t0 <= float(ts) <= t1:
            k = ev.get("kind") or ev.get("event")
            if k:
                event_counts[str(k)] += 1
            if k == "watch_drop":
                sc = ev.get("exh_seat_class")
                if sc:
                    seat_mix[f"drop_{sc}"] += 1
            elif k in ("local_trail_deferred_dual_ob", "left_overbought_deferred"):
                event_counts[f"{k}:{ev.get('reason')}"] += 1

    return {
        "day": day,
        "occupancy": {
            "open_ge1_pct": occ_card["open_ge1_pct"],
            "open_ge2_pct": occ_card["open_ge2_pct"],
            "open_ge3_pct": occ_card["open_ge3_pct"],
            "zero_pct": occ_card["zero_pct"],
            "peak_open_n": occ_card["peak_open_n"],
            "peak_open_hm": occ_card["peak_open_hm"],
        },
        "trades": {
            "count": n_outcomes,
            "win_rate": round(sum(1 for r in r_vals if r > 0) / len(r_vals), 3) if r_vals else 0.0,
            "sum_r": round(sum(r_vals), 2),
            "avg_r": round(sum(r_vals) / len(r_vals), 3) if r_vals else 0.0,
            "mfe_ge_015_count": mfe_ge_015,
            "mfe_ge_015_pct": round(mfe_ge_015 / n_outcomes * 100, 1) if n_outcomes else 0.0,
            "extension_classes": dict(ext_classes),
            "close_reasons": dict(close_reasons),
            "min_hold_blocks_sum": min_hold_blocks_sum,
        },
        "events": {
            "counts": dict(event_counts),
            "seat_mix": dict(seat_mix),
        },
    }


def render_markdown(card: dict[str, Any]) -> str:
    day = card["day"]
    occ = card["occupancy"]
    tr = card["trades"]
    ev = card["events"]

    lines = [
        f"# Phase 1 Full-Day Occupancy & Attribution Card — `{day}`",
        "",
        "## 1. Minute Occupancy (RTH 09:30–16:00)",
        f"- **≥1 Position:** {occ['open_ge1_pct']:.1f}%",
        f"- **≥2 Positions:** {occ['open_ge2_pct']:.1f}%",
        f"- **≥3 Positions:** {occ['open_ge3_pct']:.1f}%",
        f"- **Zero Positions:** {occ['zero_pct']:.1f}%",
        f"- **Peak:** {occ['peak_open_n']} open @ {occ['peak_open_hm']}",
        "",
        "## 2. Trades & Extension Attribution",
        f"- **Closed Trades:** {tr['count']}",
        f"- **Sum R:** {tr['sum_r']:+.2f}R (Avg: {tr['avg_r']:+.3f}R/trade, Win Rate: {tr['win_rate']*100:.1f}%)",
        f"- **Extension (MFE ≥ 0.15R):** {tr['mfe_ge_015_count']}/{tr['count']} ({tr['mfe_ge_015_pct']}%)",
        "",
        "| Extension Class | Count | % of Trades | Action |",
        "|---|---:|---:|---|",
    ]

    total_tr = tr["count"] or 1
    for cls, cnt in sorted(tr["extension_classes"].items(), key=lambda x: -x[1]):
        pct = cnt / total_tr * 100
        action = (
            "Killed by Age-of-Square Gate" if cls == "late_into_square"
            else ("Filter by Volume Thrust" if cls == "dead_follow_through"
            else "Thesis payoff")
        )
        lines.append(f"| `{cls}` | {cnt} | {pct:.1f}% | {action} |")

    lines.extend([
        "",
        "## 3. Exit Breakdown",
        "| Close Reason | Count | Note |",
        "|---|---:|---|",
    ])
    for cr, cnt in sorted(tr["close_reasons"].items(), key=lambda x: -x[1]):
        note = "Primary thesis exit" if cr == "left_overbought" else ("Trail backup" if cr == "local_trail" else "Disaster/protective")
        lines.append(f"| `{cr}` | {cnt} | {note} |")

    lines.extend([
        "",
        f"- **Min-Hold Total Blocks / Deferrals:** {tr['min_hold_blocks_sum']}",
    ])

    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--day", default=datetime.now(ET).strftime("%Y-%m-%d"), help="ET day YYYY-MM-DD")
    ap.add_argument("--report-dir", type=Path, default=None)
    args = ap.parse_args(argv)

    card = compute_card(args.day, report_dir=args.report_dir)
    print(render_markdown(card))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
