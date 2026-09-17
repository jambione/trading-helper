#!/usr/bin/env python3
"""Stream-ready occupancy card — Phase A metric for the week direction.

Direction: fill the book with names that can earn decision-fresh stream tape,
and keep seats for those that do.

Primary fields:
  seated_n, seated_young_n, tape_only_share, heat_share, arm_ok_n,
  thin_rvol_share, n_kept

Observe-only. Writes ai_reports/screens/occupancy_{day}.json (+ summary.md).

Usage (mini, venv)::

    .venv/bin/python tools/occupancy_card.py --day 2026-09-16
    .venv/bin/python tools/occupancy_card.py --day 2026-09-16 --since-hhmm 10:34
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai_paths import resolve_report_dir  # noqa: E402

ET = ZoneInfo("America/New_York")
HEAT_WHYS = frozenset({
    "rsi_extended",
    "exh_falling",
    "exh_not_rising",
    "already_extended",
    "mistimed_heat",
    "heating_too_low",
    "last_heating",
    "extended_cheap",
})
SCREEN_DIR = Path(resolve_report_dir()) / "screens"


def _day_start_ts(day: str) -> float:
    return datetime.fromisoformat(day).replace(
        hour=0, minute=0, second=0, microsecond=0, tzinfo=ET,
    ).timestamp()


def _parse_since(day: str, since_hhmm: str | None) -> float:
    base = _day_start_ts(day)
    if not since_hhmm:
        # Default: RTH open
        return datetime.fromisoformat(day).replace(
            hour=9, minute=30, second=0, microsecond=0, tzinfo=ET,
        ).timestamp()
    hh, mm = since_hhmm.split(":")
    return datetime.fromisoformat(day).replace(
        hour=int(hh), minute=int(mm), second=0, microsecond=0, tzinfo=ET,
    ).timestamp()


def _decision_max_age() -> float:
    try:
        from config import load_config
        return float(load_config().get("ai_watch_decision_max_age_sec") or 15.0)
    except Exception:
        return 15.0


def _load_watch_rows() -> dict[str, dict]:
    report = Path(resolve_report_dir())
    for p in (report / "entry_watch_state.json", ROOT / "entry_watch_state.json"):
        if not p.exists():
            continue
        try:
            w = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(w, dict):
            continue
        # Flat symbol->row (live shape) or nested under watch/records.
        nested = w.get("watch") or w.get("records")
        if isinstance(nested, dict):
            return {
                str(k).upper(): v for k, v in nested.items()
                if isinstance(v, dict)
            }
        out = {}
        for k, v in w.items():
            if isinstance(v, dict) and (
                v.get("symbol") or str(k).isupper() or v.get("source")
            ):
                out[str(k).upper()] = v
        if out:
            return out
    return {}


def _age_of(row: dict) -> float | None:
    for k in ("last_ask_age_sec", "price_age_sec", "rt_price_age_sec"):
        try:
            if row.get(k) is not None:
                return float(row[k])
        except (TypeError, ValueError):
            pass
    return None


def seated_snapshot(decision_max: float) -> dict[str, Any]:
    rows = _load_watch_rows()
    young = []
    blocks: Counter[str] = Counter()
    for sym, r in rows.items():
        code = str(r.get("block_code") or r.get("block_reason") or "none")
        blocks[code] += 1
        age = _age_of(r)
        if age is not None and age <= decision_max:
            young.append({
                "symbol": sym,
                "age": age,
                "block": code,
                "source": r.get("source"),
                "seat": r.get("seat_role"),
            })
    return {
        "seated_n": len(rows),
        "seated_young_n": len(young),
        "seated_young": young,
        "blocks": dict(blocks.most_common()),
    }


def arm_poll_shares(day: str, since_ts: float) -> dict[str, Any]:
    path = Path(resolve_report_dir()) / "decision_ledger" / f"{day}.jsonl"
    why: Counter[str] = Counter()
    arm_ok = 0
    n = 0
    if path.exists():
        for line in path.open(encoding="utf-8", errors="replace"):
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            try:
                ts = float(r.get("ts"))
            except (TypeError, ValueError):
                continue
            if ts < since_ts:
                continue
            n += 1
            w = str(r.get("arm_why") or r.get("why") or "")
            why[w or "?"] += 1
            if r.get("arm_ok") is True or w in ("armed", "ok", "buy"):
                arm_ok += 1
    total = n or 1
    tape_only = why.get("tape_only", 0)
    heat = sum(why.get(h, 0) for h in HEAT_WHYS)
    return {
        "arm_polls": n,
        "arm_ok_n": arm_ok,
        "tape_only_n": tape_only,
        "tape_only_share": tape_only / total if n else None,
        "heat_n": heat,
        "heat_share": heat / total if n else None,
        "arm_why_top": why.most_common(12),
    }


def funnel_shares(since_ts: float) -> dict[str, Any]:
    path = Path(resolve_report_dir()) / "events.jsonl"
    seed: Counter[str] = Counter()
    kept: list[int] = []
    n_funnels = 0
    if not path.exists():
        return {
            "funnels": 0,
            "thin_rvol_share": None,
            "n_kept_avg": None,
            "n_kept_last": None,
            "seed_top": [],
        }
    # Tail read — funnels are frequent.
    data = path.read_bytes()[-6_000_000:].decode("utf-8", errors="replace")
    for line in data.splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("kind") != "admit_funnel":
            continue
        try:
            ts = float(r.get("ts"))
        except (TypeError, ValueError):
            continue
        if ts < since_ts:
            continue
        n_funnels += 1
        kept.append(int(r.get("n_kept") or r.get("kept_n") or 0))
        drops = r.get("seed_drops") or r.get("seed_drop_counts") or {}
        if not isinstance(drops, dict):
            continue
        for a, b in drops.items():
            if isinstance(b, dict):
                for reason, n in b.items():
                    try:
                        seed[str(reason)] += int(n)
                    except (TypeError, ValueError):
                        pass
            else:
                # Compact "src:reason" keys
                key = str(a)
                reason = key.split(":", 1)[-1] if ":" in key else key
                try:
                    seed[reason] += int(b)
                except (TypeError, ValueError):
                    pass
    seed_total = sum(seed.values()) or 1
    thin = seed.get("thin_rvol", 0)
    return {
        "funnels": n_funnels,
        "thin_rvol_n": thin,
        "thin_rvol_share": thin / seed_total if seed else None,
        "n_kept_avg": (sum(kept) / len(kept)) if kept else None,
        "n_kept_last": kept[-1] if kept else None,
        "seed_top": seed.most_common(10),
    }


def realtime_tape() -> dict[str, Any]:
    for p in (
        Path(resolve_report_dir()) / "signal_state.json",
        ROOT / "signal_state.json",
    ):
        if p.exists():
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                return d.get("realtime_tape") or {}
            except Exception:
                return {}
    return {}


def build_card(day: str, since_ts: float) -> dict[str, Any]:
    decision_max = _decision_max_age()
    seated = seated_snapshot(decision_max)
    arms = arm_poll_shares(day, since_ts)
    funnel = funnel_shares(since_ts)
    return {
        "day": day,
        "direction": "stream_ready_occupancy",
        "decision_max_age_sec": decision_max,
        "since_ts": since_ts,
        "asof_et": datetime.now(tz=ET).isoformat(),
        "realtime_tape": realtime_tape(),
        **seated,
        **arms,
        **funnel,
        "phase_b_lever": "stream_proven_thin_rvol_waive",
        "note": (
            "Primary: seated_young_n up + tape_only_share down. "
            "Do not globally cut min_rvol without stream predicate."
        ),
    }


def summary_md(card: dict[str, Any]) -> str:
    lines = [
        f"# Stream-ready occupancy — {card.get('day')}",
        "",
        f"asof: {card.get('asof_et')}",
        f"decision_max_age_sec: {card.get('decision_max_age_sec')}",
        f"phase_b_lever: {card.get('phase_b_lever')}",
        "",
        "## Primary",
        f"- seated_n: **{card.get('seated_n')}**",
        f"- seated_young_n: **{card.get('seated_young_n')}**",
        f"- tape_only_share: **{card.get('tape_only_share')}** "
        f"({card.get('tape_only_n')}/{card.get('arm_polls')} polls)",
        f"- heat_share: **{card.get('heat_share')}**",
        f"- arm_ok_n: **{card.get('arm_ok_n')}**",
        f"- thin_rvol_share: **{card.get('thin_rvol_share')}**",
        f"- n_kept avg/last: {card.get('n_kept_avg')} / {card.get('n_kept_last')}",
        "",
        "## Tape",
        f"- realtime_tape: `{card.get('realtime_tape')}`",
        "",
        "## Blocks on book",
        f"- `{card.get('blocks')}`",
        "",
        "## Arm why top",
        f"- `{card.get('arm_why_top')}`",
        "",
        "## Seed drop top",
        f"- `{card.get('seed_top')}`",
        "",
        card.get("note") or "",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--day", default="", help="ET day YYYY-MM-DD (default today)")
    ap.add_argument("--since-hhmm", default="",
                    help="ET HH:MM lower bound (default 09:30 RTH)")
    ap.add_argument("--since-reboot", action="store_true",
                    help="shorthand: --since-hhmm 10:34 on 2026-09-16 reboot day")
    args = ap.parse_args()
    day = args.day or datetime.now(tz=ET).strftime("%Y-%m-%d")
    since_hhmm = args.since_hhmm
    if args.since_reboot and not since_hhmm:
        since_hhmm = "10:34"
    since_ts = _parse_since(day, since_hhmm or None)
    card = build_card(day, since_ts)
    SCREEN_DIR.mkdir(parents=True, exist_ok=True)
    out_json = SCREEN_DIR / f"occupancy_{day}.json"
    out_md = SCREEN_DIR / f"occupancy_{day}_summary.md"
    out_json.write_text(json.dumps(card, indent=2, default=str) + "\n",
                        encoding="utf-8")
    out_md.write_text(summary_md(card), encoding="utf-8")
    print(f"wrote {out_json}")
    print(f"wrote {out_md}")
    print(
        f"seated={card.get('seated_n')} young={card.get('seated_young_n')} "
        f"tape_only_share={card.get('tape_only_share')} "
        f"thin_rvol_share={card.get('thin_rvol_share')} "
        f"n_kept_last={card.get('n_kept_last')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
