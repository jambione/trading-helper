#!/usr/bin/env python3
"""Open-minutes scoreboard — % RTH minutes with open≥1 / ≥2 / ≥3.

KPI for Phase 1 seating: zeros = failure. Built from ``position_shadow.jsonl``
(preferred) with optional ``outcomes.jsonl`` fill for thin-shadow days.

Excludes unconfirmed limit ghosts: ``hold_sec ≤ TTL`` and no last price
(``price`` null). TTL defaults to ``ai_entry_limit_ttl_sec`` (30s).

Writes::

    benchmarks/open_minutes/<day>.json
    benchmarks/open_minutes/<day>_summary.md
    benchmarks/open_minutes/range_<from>_<to>.json   (multi-day)
    benchmarks/open_minutes/range_<from>_<to>_summary.md

Usage (mini, venv)::

    .venv/bin/python tools/open_minutes_scoreboard.py --day 2026-09-17
    .venv/bin/python tools/open_minutes_scoreboard.py --from 2026-09-15 --to 2026-09-17
    .venv/bin/python tools/open_minutes_scoreboard.py --from 2026-09-15 --to 2026-09-17 --compare
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai_paths import resolve_report_dir  # noqa: E402

ET = ZoneInfo("America/New_York")
OUT_DIR = ROOT / "benchmarks" / "open_minutes"
RTH_START = (9, 30)
RTH_END = (16, 0)  # exclusive → 390 minutes
DEFAULT_TTL_SEC = 30.0


def _day_ts(day: str, hour: int, minute: int) -> float:
    return datetime.fromisoformat(day).replace(
        hour=hour, minute=minute, second=0, microsecond=0, tzinfo=ET,
    ).timestamp()


def _rth_bounds(day: str) -> tuple[float, float]:
    return (
        _day_ts(day, RTH_START[0], RTH_START[1]),
        _day_ts(day, RTH_END[0], RTH_END[1]),
    )


def _rth_minute_keys(day: str) -> list[int]:
    """Epoch-minute ints covering RTH [09:30, 16:00)."""
    t0, t1 = _rth_bounds(day)
    out: list[int] = []
    t = t0
    while t < t1 - 1e-9:
        out.append(int(t) // 60)
        t += 60.0
    return out


def _entry_limit_ttl_sec() -> float:
    try:
        from config import load_config
        return float(load_config().get("ai_entry_limit_ttl_sec") or DEFAULT_TTL_SEC)
    except Exception:
        return DEFAULT_TTL_SEC


def _is_ghost(row: dict[str, Any], ttl_sec: float) -> bool:
    """Unconfirmed limit ghost: short hold and no last price."""
    px = row.get("price")
    if px is not None:
        try:
            if float(px) > 0:
                return False
        except (TypeError, ValueError):
            pass
    # No usable last price — ghost only when still inside entry limit TTL.
    try:
        hs = float(row.get("hold_sec")) if row.get("hold_sec") is not None else None
    except (TypeError, ValueError):
        hs = None
    if hs is None:
        return False
    return hs <= float(ttl_sec) + 1e-9


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


def _shadow_path() -> Path:
    return Path(resolve_report_dir()) / "position_shadow.jsonl"


def _outcomes_path() -> Path:
    return Path(resolve_report_dir()) / "outcomes.jsonl"


def load_shadow_opens_by_minute(
    day: str,
    *,
    shadow_path: Path | None = None,
    ttl_sec: float | None = None,
) -> tuple[dict[int, set[str]], dict[str, Any]]:
    """minute_key → set(symbols) from real (non-ghost) shadow ticks in RTH."""
    ttl = float(ttl_sec if ttl_sec is not None else _entry_limit_ttl_sec())
    t0, t1 = _rth_bounds(day)
    path = shadow_path or _shadow_path()
    by_min: dict[int, set[str]] = defaultdict(set)
    meta = {
        "rows_rth": 0,
        "rows_real": 0,
        "rows_ghost": 0,
        "ttl_sec": ttl,
        "source": "position_shadow",
        "path": str(path),
    }
    for row in _iter_jsonl(path) or []:
        try:
            ts = float(row.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        if ts < t0 or ts >= t1:
            continue
        meta["rows_rth"] += 1
        if _is_ghost(row, ttl):
            meta["rows_ghost"] += 1
            continue
        sym = str(row.get("symbol") or "").upper().strip()
        if not sym:
            continue
        meta["rows_real"] += 1
        by_min[int(ts) // 60].add(sym)
    return by_min, meta


def load_outcomes_opens_by_minute(
    day: str,
    *,
    outcomes_path: Path | None = None,
) -> tuple[dict[int, set[str]], dict[str, Any]]:
    """Fallback: fill each RTH minute a confirmed outcome was open."""
    t0, t1 = _rth_bounds(day)
    path = outcomes_path or _outcomes_path()
    by_min: dict[int, set[str]] = defaultdict(set)
    meta = {
        "outcomes_day": 0,
        "source": "outcomes",
        "path": str(path),
    }
    for row in _iter_jsonl(path) or []:
        try:
            et = float(row.get("entry_time") or 0)
            xt = float(row.get("exit_time") or row.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        if not et or not xt or xt <= et:
            continue
        # Overlap with this day's RTH?
        if xt < t0 or et >= t1:
            continue
        meta["outcomes_day"] += 1
        sym = str(row.get("symbol") or "").upper().strip()
        if not sym:
            continue
        start = max(et, t0)
        end = min(xt, t1)
        # Mark every RTH minute the position was open.
        m0 = int(start) // 60
        m1 = int(end - 1e-9) // 60
        for m in range(m0, m1 + 1):
            # Only count minutes that are in RTH for this day.
            mt = m * 60
            if mt < t0 or mt >= t1:
                continue
            by_min[m].add(sym)
    return by_min, meta


def score_day(
    day: str,
    *,
    shadow_path: Path | None = None,
    outcomes_path: Path | None = None,
    ttl_sec: float | None = None,
    prefer_outcomes_if_thin: bool = True,
    thin_real_rows: int = 50,
) -> dict[str, Any]:
    mins = _rth_minute_keys(day)
    n_mins = len(mins)
    by_min, meta = load_shadow_opens_by_minute(
        day, shadow_path=shadow_path, ttl_sec=ttl_sec)
    used = "position_shadow"
    if prefer_outcomes_if_thin and meta["rows_real"] < thin_real_rows:
        ob, om = load_outcomes_opens_by_minute(day, outcomes_path=outcomes_path)
        if sum(len(v) for v in ob.values()) > sum(len(v) for v in by_min.values()):
            by_min = ob
            meta["outcomes_fallback"] = om
            used = "outcomes"

    def _pct(n: int) -> float:
        return round(100.0 * n / n_mins, 2) if n_mins else 0.0

    ge1 = sum(1 for m in mins if len(by_min.get(m, ())) >= 1)
    ge2 = sum(1 for m in mins if len(by_min.get(m, ())) >= 2)
    ge3 = sum(1 for m in mins if len(by_min.get(m, ())) >= 3)
    zero = sum(1 for m in mins if len(by_min.get(m, ())) == 0)

    # Peak / first zero stretch after open for posture.
    peak_n = 0
    peak_hm = None
    first_zero_after_peak = None
    saw_peak = False
    for m in mins:
        n = len(by_min.get(m, ()))
        hm = datetime.fromtimestamp(m * 60, tz=ET).strftime("%H:%M")
        if n > peak_n:
            peak_n = n
            peak_hm = hm
            saw_peak = True
            first_zero_after_peak = None
        elif saw_peak and peak_n > 0 and n == 0 and first_zero_after_peak is None:
            first_zero_after_peak = hm

    hist: dict[str, int] = {}
    for m in mins:
        k = str(len(by_min.get(m, ())))
        hist[k] = hist.get(k, 0) + 1

    return {
        "day": day,
        "rth": f"{RTH_START[0]:02d}:{RTH_START[1]:02d}-{RTH_END[0]:02d}:{RTH_END[1]:02d}",
        "rth_minutes": n_mins,
        "source_used": used,
        "shadow_meta": meta,
        "open_ge1": ge1,
        "open_ge1_pct": _pct(ge1),
        "open_ge2": ge2,
        "open_ge2_pct": _pct(ge2),
        "open_ge3": ge3,
        "open_ge3_pct": _pct(ge3),
        "zero_minutes": zero,
        "zero_pct": _pct(zero),
        "peak_open_n": peak_n,
        "peak_open_hm": peak_hm,
        "first_zero_after_peak_hm": first_zero_after_peak,
        "count_hist": dict(sorted(hist.items(), key=lambda kv: int(kv[0]))),
        "pass_ge1": ge1 == n_mins,  # zero minutes = failure
        "pass_note": (
            "FAIL: zero open minutes present"
            if zero > 0
            else "PASS: every RTH minute had open≥1"
        ),
    }


def render_day_md(card: dict[str, Any]) -> str:
    lines = [
        f"# Open-minutes scoreboard — `{card['day']}`",
        "",
        f"RTH `{card['rth']}` ET · **{card['rth_minutes']}** minutes · "
        f"source `{card['source_used']}`",
        "",
        "## KPI",
        "",
        f"| Metric | Count | % of RTH |",
        f"|---|---:|---:|",
        f"| open ≥ 1 | {card['open_ge1']} | **{card['open_ge1_pct']:.1f}%** |",
        f"| open ≥ 2 | {card['open_ge2']} | **{card['open_ge2_pct']:.1f}%** |",
        f"| open ≥ 3 | {card['open_ge3']} | **{card['open_ge3_pct']:.1f}%** |",
        f"| open = 0 | {card['zero_minutes']} | **{card['zero_pct']:.1f}%** |",
        "",
        f"**{card['pass_note']}**",
        "",
        "## Peak / collapse",
        "",
        f"- peak open n: **{card['peak_open_n']}** at `{card['peak_open_hm']}`",
        f"- first zero after peak: `{card.get('first_zero_after_peak_hm')}`",
        "",
        "## Shadow hygiene",
        "",
    ]
    sm = card.get("shadow_meta") or {}
    lines.append(
        f"- rows_rth={sm.get('rows_rth')} real={sm.get('rows_real')} "
        f"ghost={sm.get('rows_ghost')} ttl_sec={sm.get('ttl_sec')}"
    )
    lines.extend(["", "## Count hist (opens per minute)", "", "```",
                  json.dumps(card.get("count_hist") or {}, indent=2), "```", ""])
    return "\n".join(lines)


def render_range_md(cards: list[dict[str, Any]], *, compare: bool = False) -> str:
    if not cards:
        return "# Open-minutes scoreboard — empty\n"
    days = [c["day"] for c in cards]
    lines = [
        f"# Open-minutes scoreboard — `{days[0]}` → `{days[-1]}`",
        "",
        "| Day | ≥1 % | ≥2 % | ≥3 % | zero % | peak | note |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for c in cards:
        lines.append(
            f"| {c['day']} | {c['open_ge1_pct']:.1f} | {c['open_ge2_pct']:.1f} | "
            f"{c['open_ge3_pct']:.1f} | {c['zero_pct']:.1f} | "
            f"{c['peak_open_n']}@{c['peak_open_hm']} | {c['pass_note'].split(':')[0]} |"
        )
    lines.append("")
    if compare and len(cards) >= 2:
        # Prefer Wed/Thu labels when present.
        by = {c["day"]: c for c in cards}
        wed = by.get("2026-09-16")
        thu = by.get("2026-09-17")
        if wed and thu:
            lines.extend([
                "## Thu vs Wed (2026-09-17 vs 2026-09-16)",
                "",
                f"| KPI | Wed 09-16 | Thu 09-17 | Δ pp |",
                f"|---|---:|---:|---:|",
            ])
            for key, label in (
                ("open_ge1_pct", "open≥1 %"),
                ("open_ge2_pct", "open≥2 %"),
                ("open_ge3_pct", "open≥3 %"),
                ("zero_pct", "zero %"),
            ):
                a, b = float(wed[key]), float(thu[key])
                lines.append(
                    f"| {label} | {a:.1f} | {b:.1f} | {b - a:+.1f} |"
                )
            lines.append("")
            # Posture line
            if thu["zero_pct"] > wed["zero_pct"] + 1.0:
                posture = "Thu worse on zeros vs Wed — Phase 1 seating still failing stretches."
            elif thu["open_ge1_pct"] >= wed["open_ge1_pct"] - 1.0 and thu["zero_pct"] <= 5.0:
                posture = "Thu holds seating vs Wed — checkpoint posture improving."
            else:
                posture = "Mixed — read ≥2/≥3 and peak timing before declaring checkpoint."
            lines.extend([f"**Posture:** {posture}", ""])
    # Week means
    if len(cards) > 1:
        def _mean(k: str) -> float:
            return round(sum(float(c[k]) for c in cards) / len(cards), 2)
        lines.extend([
            "## Week means",
            "",
            f"- open≥1 mean **{_mean('open_ge1_pct'):.1f}%**",
            f"- open≥2 mean **{_mean('open_ge2_pct'):.1f}%**",
            f"- open≥3 mean **{_mean('open_ge3_pct'):.1f}%**",
            f"- zero mean **{_mean('zero_pct'):.1f}%**",
            "",
        ])
    return "\n".join(lines)


def _daterange(start: str, end: str) -> list[str]:
    a = date.fromisoformat(start)
    b = date.fromisoformat(end)
    if b < a:
        a, b = b, a
    out = []
    cur = a
    while cur <= b:
        out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def write_outputs(
    cards: list[dict[str, Any]],
    out_dir: Path,
    *,
    compare: bool = False,
) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for c in cards:
        jp = out_dir / f"{c['day']}.json"
        mp = out_dir / f"{c['day']}_summary.md"
        jp.write_text(json.dumps(c, indent=2) + "\n", encoding="utf-8")
        mp.write_text(render_day_md(c), encoding="utf-8")
        written.extend([jp, mp])
    if len(cards) > 1:
        a, b = cards[0]["day"], cards[-1]["day"]
        jp = out_dir / f"range_{a}_{b}.json"
        mp = out_dir / f"range_{a}_{b}_summary.md"
        payload = {"from": a, "to": b, "days": cards}
        jp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        mp.write_text(render_range_md(cards, compare=compare), encoding="utf-8")
        written.extend([jp, mp])
    return written


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--day", help="Single ET day YYYY-MM-DD")
    ap.add_argument("--from", dest="date_from", help="Range start ET day")
    ap.add_argument("--to", dest="date_to", help="Range end ET day")
    ap.add_argument("--shadow", type=Path, default=None)
    ap.add_argument("--outcomes", type=Path, default=None)
    ap.add_argument("--ttl-sec", type=float, default=None)
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    ap.add_argument(
        "--compare", action="store_true",
        help="Add Wed/Thu comparison block when those days are in range",
    )
    ap.add_argument("--print-only", action="store_true",
                    help="Do not write benchmark files")
    args = ap.parse_args(argv)

    if args.day:
        days = [args.day]
    elif args.date_from and args.date_to:
        days = _daterange(args.date_from, args.date_to)
    else:
        ap.error("pass --day or both --from and --to")
        return 2

    cards = [
        score_day(
            d,
            shadow_path=args.shadow,
            outcomes_path=args.outcomes,
            ttl_sec=args.ttl_sec,
        )
        for d in days
    ]

    # Console summary first (mini tonight).
    print(render_range_md(cards, compare=args.compare or len(cards) > 1))
    for c in cards:
        print(
            f"  {c['day']}: ≥1={c['open_ge1_pct']:.1f}%  "
            f"≥2={c['open_ge2_pct']:.1f}%  ≥3={c['open_ge3_pct']:.1f}%  "
            f"zero={c['zero_pct']:.1f}%  "
            f"peak={c['peak_open_n']}@{c['peak_open_hm']}  "
            f"ghosts={c['shadow_meta'].get('rows_ghost')}  "
            f"src={c['source_used']}"
        )

    if not args.print_only:
        written = write_outputs(
            cards, args.out_dir, compare=args.compare or len(cards) > 1)
        print("wrote:")
        for p in written:
            print(f"  {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
