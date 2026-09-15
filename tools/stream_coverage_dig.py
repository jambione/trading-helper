#!/usr/bin/env python3
"""Dig A2 no_stream_strike demote vs stream coverage (stream-before-keep).

Reads ``ai_reports/events.jsonl`` for an ET day and classifies demoted /
no_stream_trade names into coverage buckets. Writes::

    benchmarks/stream_coverage/<day>_summary.md
    benchmarks/stream_coverage/<day>.json

Usage::

    .venv/bin/python tools/stream_coverage_dig.py --day 2026-09-14
    .venv/bin/python tools/stream_coverage_dig.py --day 2026-09-14 --also 2026-09-11
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
OUT_DIR = ROOT / "benchmarks" / "stream_coverage"

# Default wait before no_stream_trade drop: subscribe grace + no_trade_after.
# Used only as a dig heuristic when config is unavailable.
DEFAULT_WAIT_SEC = 90.0 + 300.0


def _day_of(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), tz=ET).date().isoformat()


def _hm(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), tz=ET).strftime("%H:%M:%S")


def load_day_events(day: str, path: Path = EVENTS) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            # Cheap prefilter — day string often absent; still parse funnel/drop.
            if (
                "admit_funnel" not in line
                and "watch_drop" not in line
                and "no_stream" not in line
                and "ensure_watch" not in line
                and "stream" not in line
            ):
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


def classify_symbol(
    sym: str,
    *,
    first_kept: float | None,
    drops: list[dict],
    demote_ts: float | None,
    streamish_after: bool,
    wait_sec: float = DEFAULT_WAIT_SEC,
) -> str:
    """Bucket a demoted / struck name.

    Heuristics from event fields only (no live Finnhub history):
      - never_kept: never appeared in kept_symbols before first strike
      - subscribed_late: young stream / stream src appears after strikes started
      - subscribed_but_silent: full wait elapsed, age missing or old, no stream
      - thin_or_halted: waited full window; age present but always > decision-ish
      - other
    """
    if not drops:
        return "other"
    first_drop = drops[0]
    elapsed = first_drop.get("elapsed_sec")
    try:
        elapsed_f = float(elapsed) if elapsed is not None else None
    except (TypeError, ValueError):
        elapsed_f = None
    age = first_drop.get("age_sec")
    try:
        age_f = float(age) if age is not None else None
    except (TypeError, ValueError):
        age_f = None
    src = str(first_drop.get("src") or "").strip().lower()

    if first_kept is None:
        # Strike/demote without a prior keep is unexpected for A2 path;
        # treat as coverage miss before keep.
        return "never_subscribed"

    if streamish_after:
        return "subscribed_late"

    # Full post-admit wait then drop → had a seat; stream never young.
    if elapsed_f is not None and elapsed_f >= max(60.0, wait_sec * 0.85):
        if age_f is None and src in ("", "none", "stale_tape", "rest", "need_stream"):
            return "subscribed_but_silent"
        if age_f is not None and age_f > 30.0:
            return "thin_or_halted"
        return "subscribed_but_silent"

    if elapsed_f is not None and elapsed_f < 120.0:
        # Dropped too fast vs shipped wait — subscribe/admit stamp anomaly.
        return "other"

    return "other"


def dig_day(day: str, events: list[dict], wait_sec: float = DEFAULT_WAIT_SEC) -> dict:
    incl_by_minute: dict[str, Counter] = defaultdict(Counter)
    kept_by_minute: dict[str, list] = defaultdict(list)
    first_kept: dict[str, float] = {}
    last_kept: dict[str, float] = {}
    drops_by_sym: dict[str, list] = defaultdict(list)
    demote_ts: dict[str, float] = {}
    demote_meta: dict[str, dict] = {}
    stream_hints: dict[str, list] = defaultdict(list)  # ts of stream-ish sightings
    kinds = Counter()

    for r in events:
        kind = str(r.get("kind") or "")
        kinds[kind] += 1
        ts = float(r.get("ts") or 0)
        minute = datetime.fromtimestamp(ts, tz=ET).strftime("%H:%M")

        if kind == "admit_funnel":
            inc = r.get("inclusion") or {}
            if isinstance(inc, dict):
                for reason, n in inc.items():
                    if isinstance(n, (int, float)):
                        incl_by_minute[minute][str(reason)] += int(n)
            kept = [str(s).upper() for s in (r.get("kept_symbols") or []) if s]
            kept_by_minute[minute].append({
                "kept_n": r.get("kept_n"),
                "warming_n": r.get("warming_n"),
                "n_candidates": r.get("n_candidates"),
                "kept": kept,
            })
            for s in kept:
                if s not in first_kept:
                    first_kept[s] = ts
                last_kept[s] = ts

        elif kind == "watch_drop":
            sym = str(r.get("symbol") or "").upper()
            reason = str(r.get("reason") or "")
            if reason == "no_stream_trade" or r.get("no_stream_strikes"):
                drops_by_sym[sym].append(r)
            src = str(r.get("src") or "").lower()
            if src == "stream" or (
                r.get("age_sec") is not None
                and float(r.get("age_sec") or 1e9) <= 15.0
            ):
                stream_hints[sym].append(ts)

        elif kind == "no_stream_strike_demote":
            sym = str(r.get("symbol") or "").upper()
            if sym and sym not in demote_ts:
                demote_ts[sym] = ts
                demote_meta[sym] = {
                    "strikes": r.get("strikes"),
                    "limit": r.get("limit"),
                    "et_day": r.get("et_day"),
                }

        # Any event noting young stream on a symbol
        if str(r.get("last_ask_src") or r.get("price_src") or "").lower() == "stream":
            s = str(r.get("symbol") or "").upper()
            if s:
                stream_hints[s].append(ts)

    # Symbols of interest: demoted or had ≥1 no_stream_trade drop
    interest = sorted(set(demote_ts) | {s for s, ds in drops_by_sym.items() if ds})

    per_sym: list[dict] = []
    buckets = Counter()
    for sym in interest:
        drops = sorted(drops_by_sym.get(sym) or [], key=lambda r: float(r.get("ts") or 0))
        t_kept = first_kept.get(sym)
        t_demote = demote_ts.get(sym)
        t_first_drop = float(drops[0]["ts"]) if drops else None
        # stream hint after first drop ⇒ late coverage
        streamish_after = False
        if t_first_drop is not None:
            streamish_after = any(t > t_first_drop for t in stream_hints.get(sym) or [])
        bucket = classify_symbol(
            sym,
            first_kept=t_kept,
            drops=drops,
            demote_ts=t_demote,
            streamish_after=streamish_after,
            wait_sec=wait_sec,
        )
        buckets[bucket] += 1
        row = {
            "symbol": sym,
            "bucket": bucket,
            "first_kept": _hm(t_kept) if t_kept else None,
            "first_kept_ts": t_kept,
            "first_drop": _hm(t_first_drop) if t_first_drop else None,
            "first_drop_ts": t_first_drop,
            "demote": _hm(t_demote) if t_demote else None,
            "demote_ts": t_demote,
            "n_drops": len(drops),
            "sec_kept_to_first_drop": (
                round(t_first_drop - t_kept, 1)
                if t_kept and t_first_drop else None
            ),
            "sec_first_drop_to_demote": (
                round(t_demote - t_first_drop, 1)
                if t_demote and t_first_drop else None
            ),
            "first_drop_elapsed_sec": drops[0].get("elapsed_sec") if drops else None,
            "first_drop_age_sec": drops[0].get("age_sec") if drops else None,
            "first_drop_src": drops[0].get("src") if drops else None,
            "demoted": sym in demote_ts,
        }
        per_sym.append(row)

    # Minute reject mix (top reasons)
    minute_rows = []
    for minute in sorted(incl_by_minute):
        c = incl_by_minute[minute]
        total = sum(c.values())
        top = c.most_common(6)
        keeplist = kept_by_minute.get(minute) or []
        kept_ns = [k.get("kept_n") for k in keeplist if k.get("kept_n") is not None]
        minute_rows.append({
            "minute": minute,
            "inclusion_n": total,
            "top_reasons": top,
            "kept_n_mean": (
                round(statistics.mean(kept_ns), 2) if kept_ns else None
            ),
            "kept_n_max": max(kept_ns) if kept_ns else None,
            "funnel_snaps": len(keeplist),
        })

    # Recommend ship
    majority = buckets.most_common(1)[0][0] if buckets else "other"
    if majority in ("subscribed_but_silent", "thin_or_halted"):
        # Had seats, waited full window, never got young prints → grace on
        # strike alone won't restore midday; need re-admit clear when stream
        # appears (B3) + keep pre-warm (B2). Primary: B3 if demoted-then
        # anything streamish; else B1 only helps early false strikes.
        n_late = buckets.get("subscribed_late", 0)
        n_silent = buckets.get("subscribed_but_silent", 0) + buckets.get(
            "thin_or_halted", 0)
        if n_late >= max(1, len(interest) // 4):
            primary = "B3"
            rationale = (
                "Material subscribed_late share: demoted names later show "
                "stream — clear strikes on young print (narrow)."
            )
        elif n_silent >= len(interest) * 0.5:
            primary = "B1"
            rationale = (
                "Majority waited full admit+grace+no_trade window then "
                "dropped silent/thin (often src=stale_tape). B1 "
                "ai_watch_no_stream_strike_grace_sec after ensure_watch_stream "
                "is the coverage ship (no A2 off). Same-pack companion: soft-"
                "seed parity early ensure (B2). B1 alone will not revive names "
                "that already sat the full no_trade window silent."
            )
        else:
            primary = "B1"
            rationale = "Mixed; default to stream-before-strike grace (B1)."
    elif majority == "subscribed_late":
        primary = "B3"
        rationale = "Demoted then later printed — clear strikes on young stream."
    elif majority == "never_subscribed":
        primary = "B2"
        rationale = "Never kept / never subscribed before strike path — pre-warm."
    else:
        primary = "B1"
        rationale = "No clean majority; ship B1 grace as smallest coverage fix."

    # Open vs midday demote pressure
    demote_hours = Counter(
        datetime.fromtimestamp(ts, tz=ET).strftime("%H")
        for ts in demote_ts.values()
    )

    return {
        "day": day,
        "n_events": len(events),
        "kinds": dict(kinds.most_common()),
        "n_interest": len(interest),
        "n_demoted": len(demote_ts),
        "buckets": dict(buckets),
        "majority_bucket": majority,
        "primary_ship": primary,
        "rationale": rationale,
        "demote_by_hour": dict(sorted(demote_hours.items())),
        "wait_sec_assumed": wait_sec,
        "symbols": per_sym,
        "minutes": minute_rows,
        "unique_kept": len(first_kept),
    }


def write_artifacts(payload: dict) -> tuple[Path, Path]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    day = payload["day"]
    jp = OUT_DIR / f"{day}.json"
    mp = OUT_DIR / f"{day}_summary.md"
    jp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    lines = [
        f"# Stream coverage dig — `{day}`",
        "",
        f"Events scanned: **{payload['n_events']}**  ·  "
        f"unique kept: **{payload['unique_kept']}**  ·  "
        f"demoted: **{payload['n_demoted']}**  ·  "
        f"interest (drop|demote): **{payload['n_interest']}**",
        "",
        "## Buckets",
        "",
    ]
    for k, v in sorted(
        (payload.get("buckets") or {}).items(), key=lambda kv: -kv[1]
    ):
        lines.append(f"- **{k}**: {v}")
    lines += [
        "",
        f"**Majority:** `{payload.get('majority_bucket')}`",
        f"**Primary ship:** `{payload.get('primary_ship')}`",
        "",
        payload.get("rationale") or "",
        "",
        "## Demote by hour (ET)",
        "",
        "```",
        json.dumps(payload.get("demote_by_hour") or {}, indent=2),
        "```",
        "",
        "## Per-symbol (demoted first)",
        "",
        "| sym | bucket | kept→drop | drop→demote | elapsed | age | src | demoted |",
        "|---|---|---:|---:|---:|---:|---|---|",
    ]
    syms = sorted(
        payload.get("symbols") or [],
        key=lambda r: (not r.get("demoted"), r.get("first_drop_ts") or 0),
    )
    for r in syms:
        lines.append(
            f"| {r['symbol']} | {r['bucket']} | "
            f"{r.get('sec_kept_to_first_drop')} | "
            f"{r.get('sec_first_drop_to_demote')} | "
            f"{r.get('first_drop_elapsed_sec')} | "
            f"{r.get('first_drop_age_sec')} | "
            f"{r.get('first_drop_src')} | "
            f"{'Y' if r.get('demoted') else ''} |"
        )

    # Top minutes by no_stream_strike_demote inclusion
    lines += ["", "## Inclusion pressure (minutes with demote rejects)", ""]
    hot = []
    for m in payload.get("minutes") or []:
        reasons = dict(m.get("top_reasons") or [])
        dem = int(reasons.get("no_stream_strike_demote") or 0)
        if dem > 0:
            hot.append((dem, m))
    hot.sort(key=lambda t: -t[0])
    for dem, m in hot[:20]:
        lines.append(
            f"- `{m['minute']}` demote_rejects={dem} "
            f"kept_mean={m.get('kept_n_mean')} max={m.get('kept_n_max')} "
            f"top={m.get('top_reasons')[:3]}"
        )

    lines += [
        "",
        "## Ship rule",
        "",
        "Do **not** disable A2. Dig → one primary (B1/B2/B3). "
        "Soft-seed parity is eligibility + same `ensure_watch_stream`.",
        "",
    ]
    mp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return jp, mp


def main() -> int:
    ap = argparse.ArgumentParser(description="Stream coverage / A2 demote dig")
    ap.add_argument("--day", default="2026-09-14")
    ap.add_argument("--also", nargs="*", default=[])
    ap.add_argument("--events", type=Path, default=EVENTS)
    ap.add_argument("--wait-sec", type=float, default=DEFAULT_WAIT_SEC)
    args = ap.parse_args()

    days = [args.day] + list(args.also or [])
    for day in days:
        print(f"dig {day} …")
        events = load_day_events(day, args.events)
        payload = dig_day(day, events, wait_sec=float(args.wait_sec))
        jp, mp = write_artifacts(payload)
        print(
            f"  events={payload['n_events']} demoted={payload['n_demoted']} "
            f"buckets={payload['buckets']} → {payload['primary_ship']}"
        )
        print(f"  wrote {jp}")
        print(f"  wrote {mp}")
        print(f"  {payload['rationale']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
