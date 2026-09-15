#!/usr/bin/env python3
"""Dig stay-rate collapse: kept → stale → drop / refuse re-keep.

Reads ``ai_reports/events.jsonl`` for an ET day and classifies
``watch_drop`` / inclusion refuses into stay buckets. Writes::

    benchmarks/stale_stay/<day>_summary.md
    benchmarks/stale_stay/<day>.json

Usage::

    .venv/bin/python tools/stale_stay_dig.py --day 2026-09-15
    .venv/bin/python tools/stale_stay_dig.py --day 2026-09-15 --also 2026-09-14
    .venv/bin/python tools/stale_stay_dig.py --day 2026-09-15 --events /path/to/events.jsonl
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ET = ZoneInfo("America/New_York")
EVENTS = ROOT / "ai_reports" / "events.jsonl"
OUT_DIR = ROOT / "benchmarks" / "stale_stay"

# Full no_stream_trade wait ≈ subscribe grace + no_trade window.
DEFAULT_WAIT_SEC = 90.0 + 300.0


def _day_of(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), tz=ET).date().isoformat()


def _hm(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(float(ts), tz=ET).strftime("%H:%M:%S")


def _minute(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), tz=ET).strftime("%H:%M")


def load_day_events(day: str, path: Path = EVENTS) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    needles = (
        "admit_funnel", "watch_drop", "no_stream", "ensure_watch",
        "stale", "pin_", "stream",
    )
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            if not any(n in line for n in needles):
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            try:
                ts = float(r.get("ts") or 0)
            except (TypeError, ValueError):
                continue
            if not ts or _day_of(ts) != day:
                continue
            rows.append(r)
    rows.sort(key=lambda r: float(r.get("ts") or 0))
    return rows


def _f(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def classify_drop(
    *,
    first_kept: float | None,
    drop: dict,
    prior_grace: bool,
    prior_ensure_event: bool,
    wait_sec: float = DEFAULT_WAIT_SEC,
) -> str:
    """Bucket a single watch_drop for stay-rate dig.

    - never_streamed: never kept, or first seat died with ancient age and no grace
    - stream_then_silent: had a seat for the full wait; age blew out / silent
    - age_lie_fail_open: missing age on drop (historical fail-open footgun)
    - iex_only_no_finnhub: src hints REST/IEX without stream/grace
    - pin_thrash: pin seat dropped for stale / steal
    - other
    """
    reason = str(drop.get("reason") or "").strip().lower()
    src = str(drop.get("src") or drop.get("last_ask_src") or "").strip().lower()
    age = _f(drop.get("age_sec") if drop.get("age_sec") is not None
             else drop.get("last_ask_age_sec"))
    elapsed = _f(drop.get("elapsed_sec"))
    role = str(drop.get("seat_role") or "").strip().lower()
    ts = _f(drop.get("ts")) or 0.0

    if role == "pin" and reason in (
        "no_stream_trade", "stale_timeout", "stale_tape_cap",
        "preheat_steal", "unarmable_steal",
    ):
        return "pin_thrash"

    if age is None and reason in ("no_stream_trade", "stale_timeout", "stale_tape_cap"):
        return "age_lie_fail_open"

    if first_kept is None:
        return "never_streamed"

    kept_for = ts - float(first_kept) if first_kept else None
    if (
        not prior_grace
        and not prior_ensure_event
        and age is not None
        and age > 200
        and elapsed is not None
        and elapsed >= max(60.0, wait_sec * 0.85)
        and kept_for is not None
        and kept_for <= elapsed + 45
    ):
        # First seat cycle, ancient age, no grace stamp → never got young stream.
        return "never_streamed"

    if src in ("iex", "rest", "alpaca", "nbbo") and not prior_grace:
        if age is None or age > 30:
            return "iex_only_no_finnhub"

    if elapsed is not None and elapsed >= max(60.0, wait_sec * 0.75):
        return "stream_then_silent"

    if kept_for is not None and kept_for >= 120 and (
        age is None or age > 45 or src in ("stale_tape", "none", "")
    ):
        return "stream_then_silent"

    return "other"


def dig_day(
    day: str,
    events: list[dict],
    *,
    wait_sec: float = DEFAULT_WAIT_SEC,
    compare: dict | None = None,
) -> dict:
    kinds: Counter = Counter()
    inclusion_refuse: Counter = Counter()
    seed_drops: Counter = Counter()
    drop_reasons: Counter = Counter()
    drop_src: Counter = Counter()
    buckets: Counter = Counter()

    first_kept: dict[str, float] = {}
    last_kept: dict[str, float] = {}
    grace_ts: dict[str, list[float]] = defaultdict(list)
    ensure_ts: dict[str, list[float]] = defaultdict(list)
    pin_kinds: Counter = Counter()

    # minute -> last snap
    minute_kept: dict[str, dict] = {}
    drops: list[dict] = []
    drop_details: list[dict] = []

    for r in events:
        kind = str(r.get("kind") or "")
        kinds[kind] += 1
        ts = float(r.get("ts") or 0)
        minute = _minute(ts)

        if kind == "admit_funnel":
            nk = r.get("n_kept")
            if nk is None:
                nk = r.get("kept_n")
            if nk is None and isinstance(r.get("kept_symbols"), list):
                nk = len(r["kept_symbols"])
            try:
                nk_i = int(nk) if nk is not None else None
            except (TypeError, ValueError):
                nk_i = None
            minute_kept[minute] = {
                "n_kept": nk_i,
                "warming_n": r.get("warming_n"),
                "pin_n": r.get("pin_n"),
                "scout_n": r.get("scout_n"),
                "n_candidates": r.get("n_candidates"),
                "entry_watch_n": r.get("entry_watch_n") or r.get("watch_n"),
            }
            incl = r.get("inclusion")
            if isinstance(incl, dict):
                for reason, n in incl.items():
                    try:
                        inclusion_refuse[str(reason)] += int(n)
                    except (TypeError, ValueError):
                        inclusion_refuse[str(reason)] += 1
            sd = r.get("seed_drops")
            if isinstance(sd, dict):
                for k, v in sd.items():
                    try:
                        seed_drops[str(k)] += int(v)
                    except (TypeError, ValueError):
                        seed_drops[str(k)] += 1
            for sym in (r.get("kept_symbols") or []):
                s = str(sym).upper().strip()
                if not s:
                    continue
                if s not in first_kept:
                    first_kept[s] = ts
                last_kept[s] = ts

        elif kind == "watch_drop":
            drops.append(r)
            drop_reasons[str(r.get("reason") or "")] += 1
            drop_src[str(r.get("src") or "")] += 1

        elif kind == "no_stream_grace":
            s = str(r.get("symbol") or "").upper()
            if s:
                grace_ts[s].append(ts)

        elif "ensure" in kind and "stream" in kind:
            s = str(r.get("symbol") or "").upper()
            if s:
                ensure_ts[s].append(ts)

        elif kind.startswith("pin_"):
            pin_kinds[kind] += 1

    # Classify each drop
    for r in drops:
        sym = str(r.get("symbol") or "").upper()
        ts = float(r.get("ts") or 0)
        prior_grace = any(t <= ts for t in grace_ts.get(sym) or [])
        prior_ensure = any(t <= ts for t in ensure_ts.get(sym) or [])
        # grace logged in same poll as drop (sec_since≈0) still counts as prior
        bucket = classify_drop(
            first_kept=first_kept.get(sym),
            drop=r,
            prior_grace=prior_grace,
            prior_ensure_event=prior_ensure,
            wait_sec=wait_sec,
        )
        buckets[bucket] += 1
        age = _f(r.get("age_sec") if r.get("age_sec") is not None
                 else r.get("last_ask_age_sec"))
        elapsed = _f(r.get("elapsed_sec"))
        sec_grace = None
        if grace_ts.get(sym):
            prior = [t for t in grace_ts[sym] if t <= ts]
            if prior:
                sec_grace = round(ts - prior[-1], 3)
        sec_ensure = None
        if ensure_ts.get(sym):
            prior = [t for t in ensure_ts[sym] if t <= ts]
            if prior:
                sec_ensure = round(ts - prior[-1], 3)
        drop_details.append({
            "symbol": sym,
            "bucket": bucket,
            "hm": _hm(ts),
            "ts": ts,
            "reason": r.get("reason"),
            "src": r.get("src") or r.get("last_ask_src"),
            "bars_src": r.get("bars_src"),
            "age_sec": age,
            "last_ask_age_sec": _f(r.get("last_ask_age_sec")),
            "elapsed_sec": elapsed,
            "seat_role": r.get("seat_role"),
            "sec_since_no_stream_grace": sec_grace,
            "sec_since_ensure_event": sec_ensure,
            "kept_for_sec": (
                round(ts - first_kept[sym], 1) if sym in first_kept else None
            ),
            "no_stream_strike_grace": r.get("no_stream_strike_grace"),
        })

    # Stay-rate series (RTH 09:30–16:00)
    kept_vals: list[int] = []
    zero_n = 0
    rth_n = 0
    series: list[dict] = []
    for minute in sorted(minute_kept):
        snap = minute_kept[minute]
        nk = snap.get("n_kept")
        hh = int(minute.split(":")[0])
        mm = int(minute.split(":")[1])
        rth = (hh > 9 or (hh == 9 and mm >= 30)) and hh < 16
        if rth and nk is not None:
            rth_n += 1
            kept_vals.append(int(nk))
            if int(nk) == 0:
                zero_n += 1
        series.append({"minute": minute, **snap})

    peak = max(kept_vals) if kept_vals else 0
    peak_minute = None
    collapse_minute = None
    for row in series:
        m = row["minute"]
        nk = row.get("n_kept")
        if nk is None:
            continue
        if peak_minute is None or (nk == peak and peak_minute is None):
            if nk == peak:
                peak_minute = m
        if (
            peak_minute
            and peak >= 6
            and nk is not None
            and int(nk) <= 2
            and m >= peak_minute
            and collapse_minute is None
            and m >= "09:30"
        ):
            collapse_minute = m

    def _min_to_min(a: str | None, b: str | None) -> float | None:
        if not a or not b:
            return None
        ah, am = map(int, a.split(":"))
        bh, bm = map(int, b.split(":"))
        return float((bh * 60 + bm) - (ah * 60 + am))

    stay = {
        "avg_kept": round(statistics.mean(kept_vals), 3) if kept_vals else None,
        "max_kept": peak,
        "peak_minute": peak_minute,
        "collapse_to_le2_minute": collapse_minute,
        "minutes_peak_to_collapse": _min_to_min(peak_minute, collapse_minute),
        "zero_frac_rth": round(zero_n / rth_n, 4) if rth_n else None,
        "rth_snaps": rth_n,
    }

    majority = buckets.most_common(1)[0][0] if buckets else "other"
    n_silent = buckets.get("stream_then_silent", 0)
    n_pin = buckets.get("pin_thrash", 0)
    n_never = buckets.get("never_streamed", 0)
    n_age = buckets.get("age_lie_fail_open", 0)
    total_b = sum(buckets.values()) or 1

    if n_age >= total_b * 0.25:
        primary = "B3"
        rationale = (
            "Material age_lie_fail_open: missing quote age treated live — "
            "fail closed / force restream (no broad stale loosen)."
        )
    elif n_silent >= total_b * 0.5:
        primary = "B1"
        rationale = (
            "Majority stream_then_silent: kept seats wait full "
            "grace+no_trade window then drop on stale_tape; inclusion "
            "refuses re-keep via stale_tape_admit/no_tape. Ship "
            "re-stream-before-drop grace (ensure_watch_stream + hold)."
        )
        if n_pin >= max(3, total_b * 0.15):
            primary = "B1+B2"
            rationale += " Pin thrash also material — add pin stay through grace."
    elif n_pin >= total_b * 0.4:
        primary = "B2"
        rationale = "Pin thrash dominates — pin stay / no steal during stale blip."
    elif n_never >= total_b * 0.5:
        primary = "B1"
        rationale = (
            "Majority never_streamed on first seat — restream-before-drop "
            "still the smallest honest hold (ensure earlier + grace)."
        )
    else:
        primary = "B1"
        rationale = "No cleaner majority; default to restream-before-drop (B1)."

    compare_block = None
    if compare:
        compare_block = {
            "other_day": compare.get("day"),
            "other_stay": compare.get("stay"),
            "other_buckets": compare.get("buckets"),
            "delta_avg_kept": (
                None
                if stay["avg_kept"] is None
                or (compare.get("stay") or {}).get("avg_kept") is None
                else round(
                    float(stay["avg_kept"])
                    - float(compare["stay"]["avg_kept"]),
                    3,
                )
            ),
            "delta_zero_frac": (
                None
                if stay["zero_frac_rth"] is None
                or (compare.get("stay") or {}).get("zero_frac_rth") is None
                else round(
                    float(stay["zero_frac_rth"])
                    - float(compare["stay"]["zero_frac_rth"]),
                    4,
                )
            ),
        }

    return {
        "day": day,
        "n_events": len(events),
        "kinds": dict(kinds.most_common()),
        "stay": stay,
        "buckets": dict(buckets),
        "majority_bucket": majority,
        "primary_ship": primary,
        "rationale": rationale,
        "drop_reasons": dict(drop_reasons.most_common()),
        "drop_src": dict(drop_src.most_common()),
        "inclusion_refuse_top": inclusion_refuse.most_common(12),
        "seed_drops_top": seed_drops.most_common(10),
        "pin_events": dict(pin_kinds),
        "n_drops": len(drops),
        "unique_kept": len(first_kept),
        "grace_symbols": len(grace_ts),
        "ensure_event_symbols": len(ensure_ts),
        "wait_sec_assumed": wait_sec,
        "minute_series": series,
        "drops": drop_details,
        "compare": compare_block,
    }


def write_artifacts(payload: dict) -> tuple[Path, Path]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    day = payload["day"]
    jp = OUT_DIR / f"{day}.json"
    mp = OUT_DIR / f"{day}_summary.md"
    jp.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")

    stay = payload.get("stay") or {}
    buckets = payload.get("buckets") or {}
    lines = [
        f"# Stale-stay dig — `{day}`",
        "",
        f"Events scanned: **{payload.get('n_events')}** · "
        f"unique kept: **{payload.get('unique_kept')}** · "
        f"watch_drops: **{payload.get('n_drops')}**",
        "",
        "## Stay-rate (RTH)",
        "",
        f"- avg kept: **{stay.get('avg_kept')}**",
        f"- max kept: **{stay.get('max_kept')}** at `{stay.get('peak_minute')}`",
        f"- collapse to ≤2: `{stay.get('collapse_to_le2_minute')}` "
        f"({stay.get('minutes_peak_to_collapse')} min after peak)",
        f"- zero_frac (RTH snaps): **{stay.get('zero_frac_rth')}**",
        "",
        "## Drop buckets",
        "",
    ]
    for k, n in sorted(buckets.items(), key=lambda kv: -kv[1]):
        lines.append(f"- **{k}**: {n}")
    lines += [
        "",
        f"**Majority:** `{payload.get('majority_bucket')}`",
        f"**Primary ship:** `{payload.get('primary_ship')}`",
        "",
        f"{payload.get('rationale')}",
        "",
        "## Inclusion refuse (funnel top)",
        "",
        "```",
        json.dumps(payload.get("inclusion_refuse_top") or [], indent=2),
        "```",
        "",
        "## Drop reasons / src",
        "",
        "```",
        json.dumps({
            "reasons": payload.get("drop_reasons"),
            "src": payload.get("drop_src"),
            "pin_events": payload.get("pin_events"),
        }, indent=2),
        "```",
        "",
    ]
    cmp = payload.get("compare")
    if cmp:
        lines += [
            f"## vs `{cmp.get('other_day')}`",
            "",
            f"- Δ avg_kept: **{cmp.get('delta_avg_kept')}**",
            f"- Δ zero_frac: **{cmp.get('delta_zero_frac')}**",
            f"- other stay: `{json.dumps(cmp.get('other_stay'))}`",
            f"- other buckets: `{json.dumps(cmp.get('other_buckets'))}`",
            "",
        ]
    lines += [
        "## Sample drops",
        "",
        "| sym | bucket | time | age | elapsed | src | role | since_grace |",
        "|---|---|---|---:|---:|---|---|---:|",
    ]
    for d in (payload.get("drops") or [])[:40]:
        lines.append(
            f"| {d.get('symbol')} | {d.get('bucket')} | {d.get('hm')} | "
            f"{d.get('age_sec')} | {d.get('elapsed_sec')} | {d.get('src')} | "
            f"{d.get('seat_role') or ''} | {d.get('sec_since_no_stream_grace')} |"
        )
    lines.append("")
    mp.write_text("\n".join(lines), encoding="utf-8")
    return jp, mp


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--day", required=True, help="ET day YYYY-MM-DD")
    ap.add_argument("--also", default="", help="Optional compare day")
    ap.add_argument("--events", type=Path, default=EVENTS)
    ap.add_argument("--wait-sec", type=float, default=DEFAULT_WAIT_SEC)
    args = ap.parse_args(argv)

    compare_payload = None
    if args.also:
        also_events = load_day_events(args.also, args.events)
        compare_payload = dig_day(
            args.also, also_events, wait_sec=args.wait_sec)
        write_artifacts(compare_payload)
        print(
            f"wrote compare {args.also}: "
            f"buckets={compare_payload.get('buckets')} "
            f"ship={compare_payload.get('primary_ship')}"
        )

    events = load_day_events(args.day, args.events)
    payload = dig_day(
        args.day, events, wait_sec=args.wait_sec, compare=compare_payload)
    jp, mp = write_artifacts(payload)
    print(
        f"wrote {jp} / {mp}\n"
        f"majority={payload.get('majority_bucket')} "
        f"ship={payload.get('primary_ship')} "
        f"stay={payload.get('stay')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
