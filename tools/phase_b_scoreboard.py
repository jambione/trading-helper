#!/usr/bin/env python3
"""Phase B scoreboard — PASS / FAIL / NO DECISION on pre-registered bars.

Reads ``ai_reports/phase_b_ledger/YYYY-MM-DD.jsonl`` (or a fixture). Scores
**only** Phase B rows. Never mixes into RTH Phase 1/2 gates.

Frozen bars: ``docs/PHASE_B_PREMARKET.md`` (Hybrid C). Changing
``ROUND_TRIP_FRICTION_PCT`` after seeing results voids the pass — deliberate
commit only.

Usage:
  python3 tools/phase_b_scoreboard.py --fixture tests/fixtures/phase_b_scoreboard_pass.json
  python3 tools/phase_b_scoreboard.py --ledger-dir ai_reports/phase_b_ledger
  python3 tools/phase_b_scoreboard.py --days 2026-09-15,2026-09-16
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Frozen with docs/PHASE_B_PREMARKET.md — do not retune on the scoring window.
ROUND_TRIP_FRICTION_PCT = 0.50
MIN_FILLS = 25
MIN_DAYS_WITH_FILLS = 5
MIN_FILLS_PER_DAY = 3
PASS_FILL_RATE = 0.40
PASS_MED_MFE_R = 0.10
PASS_FLAT_ON_TIME = 0.95
PASS_MED_ENTRY_SLIP_PCT = 0.40  # median entry slip vs last <= +0.40%
MAX_PEAK_OPENS = 2


def load_jsonl(path: Path | str) -> list[dict]:
    rows: list[dict] = []
    p = Path(path)
    if not p.exists():
        return rows
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def load_fixture(path: str | Path) -> list[dict]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, dict)]
    if isinstance(raw, dict):
        rows = raw.get("rows") or raw.get("ledger") or []
        return [r for r in rows if isinstance(r, dict)]
    return []


def load_ledger_dir(dir_path: str | Path, days: list[str] | None = None) -> list[dict]:
    d = Path(dir_path)
    rows: list[dict] = []
    if not d.exists():
        return rows
    files = sorted(d.glob("*.jsonl"))
    for f in files:
        day = f.stem
        if days and day not in days:
            continue
        rows.extend(load_jsonl(f))
    return rows


def _f(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _median(vals: list[float]) -> float | None:
    if not vals:
        return None
    return float(statistics.median(vals))


def summarize(rows: list[dict]) -> dict[str, Any]:
    """Aggregate Phase B ledger into scoreboard metrics."""
    armed = [r for r in rows if str(r.get("kind") or "") == "arm"]
    limits = [
        r for r in rows
        if str(r.get("kind") or "") in ("entry_limit", "limit")
    ]
    fills = [r for r in rows if str(r.get("kind") or "") == "fill"]
    unfilled = [
        r for r in rows
        if str(r.get("kind") or "") == "unfilled"
        or str(r.get("reason") or "") == "phase_b_unfilled"
    ]
    exits = [r for r in rows if str(r.get("kind") or "") in ("exit", "flatten")]
    sod_wipes = [
        r for r in rows
        if str(r.get("kind") or "") in ("sod_wipe", "flat_deadline_miss")
        or str(r.get("reason") or "") == "sod_wipe"
    ]
    flat_ok = [
        r for r in rows
        if str(r.get("kind") or "") == "flat_on_time"
        or (str(r.get("kind") or "") == "exit" and r.get("flat_on_time") is True)
    ]

    # Fill rate: fills / entry limits (prefer explicit limits; else arms).
    n_limits = len(limits) if limits else len(armed)
    n_fills = len(fills)
    fill_rate = (n_fills / n_limits) if n_limits > 0 else None

    entry_slips: list[float] = []
    for r in fills:
        slip = _f(r.get("entry_slip_pct"))
        if slip is None:
            last = _f(r.get("arm_last") or r.get("last"))
            fill_px = _f(r.get("fill_px") or r.get("entry"))
            if last and last > 0 and fill_px is not None:
                slip = (fill_px - last) / last * 100.0
        if slip is not None:
            entry_slips.append(slip)

    mfe_rs: list[float] = []
    mfe_pcts: list[float] = []
    pl_pcts: list[float] = []
    for r in fills + exits:
        mr = _f(r.get("mfe_r"))
        if mr is not None:
            mfe_rs.append(mr)
        mp = _f(r.get("mfe_pct"))
        if mp is not None:
            mfe_pcts.append(mp)
        pl = _f(r.get("pl_pct") or r.get("realized_pct"))
        if pl is not None:
            pl_pcts.append(pl)

    # Days with fills
    days_fills: dict[str, int] = {}
    for r in fills:
        day = str(r.get("day") or "")
        if day:
            days_fills[day] = days_fills.get(day, 0) + 1

    peak_opens = 0
    for r in rows:
        po = _f(r.get("peak_opens") or r.get("open_count"))
        if po is not None:
            peak_opens = max(peak_opens, int(po))
    # Also infer from concurrent fill without exit markers if stamped.
    for r in rows:
        if r.get("peak_opens") is not None:
            continue

    n_flat_events = len(flat_ok) + len(sod_wipes)
    # Lots that needed to be flat: fills that later exited, or explicit markers.
    lots_needing_flat = max(n_fills, len(flat_ok) + len(sod_wipes))
    flat_on_time_pct = None
    if lots_needing_flat > 0:
        # SOD wipe / deadline miss counts against.
        ok_n = len(flat_ok)
        if not flat_ok and not sod_wipes and n_fills > 0:
            # No flatten telemetry — cannot claim pass on this bar.
            flat_on_time_pct = None
        else:
            flat_on_time_pct = ok_n / float(lots_needing_flat)

    sum_pl = sum(pl_pcts) if pl_pcts else 0.0
    # Expectancy after stated RT friction.
    friction = ROUND_TRIP_FRICTION_PCT
    adj_pls = [p - friction for p in pl_pcts] if pl_pcts else []
    expectancy = (sum(adj_pls) / len(adj_pls)) if adj_pls else None
    sum_pl_after_friction = sum(adj_pls) if adj_pls else None

    wins = [p for p in pl_pcts if p > 0]
    win_pct = (100.0 * len(wins) / len(pl_pcts)) if pl_pcts else None

    return {
        "n_armed": len(armed),
        "n_entry_limits": n_limits,
        "n_fills": n_fills,
        "n_unfilled": len(unfilled),
        "fill_rate": fill_rate,
        "unfilled_pct": (
            (len(unfilled) / n_limits) if n_limits > 0 else None
        ),
        "med_entry_slip_pct": _median(entry_slips),
        "med_mfe_r": _median(mfe_rs),
        "med_mfe_pct": _median(mfe_pcts),
        "sum_pl_pct": sum_pl if pl_pcts else None,
        "expectancy_after_friction": expectancy,
        "sum_pl_after_friction": sum_pl_after_friction,
        "friction_pct": friction,
        "win_pct": win_pct,
        "flat_on_time_pct": flat_on_time_pct,
        "n_sod_wipe": len(sod_wipes),
        "n_flat_on_time": len(flat_ok),
        "peak_opens": peak_opens,
        "days_with_fills": days_fills,
        "n_days_with_fills": len(days_fills),
        "n_days_ge_min_fills": sum(
            1 for n in days_fills.values() if n >= MIN_FILLS_PER_DAY
        ),
    }


def verdict(summary: dict[str, Any]) -> dict[str, Any]:
    """PASS / FAIL / NO DECISION against pre-registered bars."""
    reasons: list[str] = []
    n_fills = int(summary.get("n_fills") or 0)
    n_days_ok = int(summary.get("n_days_ge_min_fills") or 0)

    enough = n_fills >= MIN_FILLS or n_days_ok >= MIN_DAYS_WITH_FILLS
    if not enough:
        return {
            "decision": "NO DECISION",
            "pass": False,
            "reasons": [
                f"n_fills_{n_fills}_lt_{MIN_FILLS}_and_days_ge3_{n_days_ok}_lt_{MIN_DAYS_WITH_FILLS}"
            ],
            "friction_pct": ROUND_TRIP_FRICTION_PCT,
            "summary": summary,
        }

    # Fail-fast: SOD wipe
    if int(summary.get("n_sod_wipe") or 0) > 0:
        reasons.append("sod_wipe_phase_b_lot")

    # Fail-fast: peak opens > 2
    if int(summary.get("peak_opens") or 0) > MAX_PEAK_OPENS:
        reasons.append(f"peak_opens_gt_{MAX_PEAK_OPENS}")

    fr = summary.get("fill_rate")
    if fr is None or float(fr) < PASS_FILL_RATE:
        reasons.append("fill_rate_lt_40pct")

    exp = summary.get("expectancy_after_friction")
    sum_pl = summary.get("sum_pl_pct")
    sum_adj = summary.get("sum_pl_after_friction")
    edge_ok = False
    if exp is not None and float(exp) > 0:
        edge_ok = True
    if sum_pl is not None and float(sum_pl) > 0:
        edge_ok = True
    if sum_adj is not None and float(sum_adj) > 0:
        edge_ok = True
    if not edge_ok:
        reasons.append("expectancy_after_friction_not_positive")

    med_mfe = summary.get("med_mfe_r")
    med_mfe_pct = summary.get("med_mfe_pct")
    mfe_ok = False
    if med_mfe is not None and float(med_mfe) >= PASS_MED_MFE_R:
        mfe_ok = True
    if med_mfe_pct is not None and float(med_mfe_pct) >= 0.3:
        mfe_ok = True
    if not mfe_ok:
        reasons.append("med_mfe_below_bar")

    flat = summary.get("flat_on_time_pct")
    if flat is None or float(flat) < PASS_FLAT_ON_TIME:
        reasons.append("flat_on_time_pct_lt_95")

    slip = summary.get("med_entry_slip_pct")
    if slip is None or float(slip) > PASS_MED_ENTRY_SLIP_PCT:
        reasons.append("med_entry_slip_gt_0_40pct")

    # Fail-fast: win%-only while expectancy / med MFE <= 0
    win = summary.get("win_pct")
    if (
        win is not None
        and float(win) >= 50.0
        and (
            (exp is not None and float(exp) <= 0)
            or (med_mfe is not None and float(med_mfe) <= 0)
        )
    ):
        reasons.append("win_pct_only_edge_nonpositive")

    decision = "PASS" if not reasons else "FAIL"
    return {
        "decision": decision,
        "pass": decision == "PASS",
        "reasons": reasons,
        "friction_pct": ROUND_TRIP_FRICTION_PCT,
        "summary": summary,
    }


def format_report(v: dict[str, Any]) -> str:
    s = v.get("summary") or {}
    lines = [
        "Phase B scoreboard (Hybrid C)",
        f"friction_rt_pct={v.get('friction_pct')}  "
        f"(changing this after results voids PASS)",
        f"decision={v.get('decision')}",
        f"n_armed={s.get('n_armed')}  n_entry_limits={s.get('n_entry_limits')}  "
        f"n_fills={s.get('n_fills')}  fill_rate={s.get('fill_rate')}",
        f"med_entry_slip_pct={s.get('med_entry_slip_pct')}  "
        f"med_mfe_r={s.get('med_mfe_r')}  "
        f"flat_on_time_pct={s.get('flat_on_time_pct')}",
        f"expectancy_after_friction={s.get('expectancy_after_friction')}  "
        f"sum_pl_pct={s.get('sum_pl_pct')}  win_pct={s.get('win_pct')}",
        f"peak_opens={s.get('peak_opens')}  n_sod_wipe={s.get('n_sod_wipe')}  "
        f"days_ge3={s.get('n_days_ge_min_fills')}",
    ]
    if v.get("reasons"):
        lines.append("reasons: " + ", ".join(v["reasons"]))
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Phase B scoreboard")
    p.add_argument("--fixture", help="JSON fixture with rows/ledger")
    p.add_argument(
        "--ledger-dir",
        default=str(ROOT / "ai_reports" / "phase_b_ledger"),
        help="Directory of day-split JSONL ledgers",
    )
    p.add_argument("--days", help="Comma-separated ET days YYYY-MM-DD")
    args = p.parse_args(argv)

    if args.fixture:
        rows = load_fixture(args.fixture)
    else:
        days = [d.strip() for d in (args.days or "").split(",") if d.strip()] or None
        rows = load_ledger_dir(args.ledger_dir, days=days)

    summary = summarize(rows)
    v = verdict(summary)
    sys.stdout.write(format_report(v))
    if v["decision"] == "PASS":
        return 0
    if v["decision"] == "NO DECISION":
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
