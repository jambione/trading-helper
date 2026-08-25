#!/usr/bin/env python3
"""Did EXH/RSI flicker into a fill that then looked worse?

The live book stamps the decision print onto the fill. That print matches
the last shadow tick — freshness is fine. The open question is stability:
RSI-2 and live EXH thrash in the minutes around the click, so a calm fill
stamp can sit inside a window that already printed extended.

This screen does not arm anything. It asks, on real fills:

  - how often would a stability rule have blocked the entry?
  - what was realized R on blocked vs kept?

Rules (pre-registered; do not edit after looking):

  max_rsi_5m>=70     refuse if max cm_rsi in the prior 5m was >= 70
  rsi_hold_60s<50    require every cm_rsi tick in the prior 60s < 50
                     (needs >=3 ticks; unknown fails open = keep)
  rsi_hold_3ticks<50 last 3 pre-fill ticks all < 50
  max_exh_5m>=80     refuse if max exhaustion in the prior 5m was >= 80

Usage (mini, venv):

    .venv/bin/python tools/rsi_exh_stability_screen.py --days 1
    .venv/bin/python tools/rsi_exh_stability_screen.py --days 10
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from ai_paths import resolve_report_dir  # noqa: E402

SCREEN_DIR = Path(ROOT) / "ai_reports" / "screens"
ET = timezone(timedelta(hours=-4))

RULES = (
    "max_rsi_5m>=70",
    "rsi_hold_60s<50",
    "rsi_hold_3ticks<50",
    "max_exh_5m>=80",
)


def _f(v: Any) -> float | None:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _day_et(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), ET).strftime("%Y-%m-%d")


def load_fills(days: int) -> list[dict]:
    """Closed watch fills with an entry_time inside the window."""
    path = Path(resolve_report_dir()) / "outcomes.jsonl"
    if not path.exists():
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days + 1)).timestamp()
    out: list[dict] = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            et = _f(o.get("entry_time"))
            if et is None or et < cutoff:
                continue
            feat = o.get("features") if isinstance(o.get("features"), dict) else {}
            rsi = _f(feat.get("cm_rsi"))
            if rsi is None:
                continue
            out.append(o)
    return out


def load_shadow_for(symbols: set[str], days: int) -> dict[str, list[dict]]:
    path = Path(resolve_report_dir()) / "shadow.jsonl"
    if not path.exists() or not symbols:
        return {}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days + 1)).timestamp()
    by: dict[str, list[dict]] = defaultdict(list)
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.strip():
                continue
            # Cheap reject before json for huge logs.
            if '"cm_rsi"' not in line and '"exhaustion"' not in line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            sym = str(r.get("symbol") or "").upper()
            if sym not in symbols:
                continue
            ts = _f(r.get("ts"))
            if ts is None or ts < cutoff:
                continue
            by[sym].append(r)
    for sym in by:
        by[sym].sort(key=lambda r: float(r["ts"]))
    return by


def pre_window(rows: list[dict], entry_ts: float,
               lookback_sec: float = 300.0) -> list[dict]:
    lo = entry_ts - lookback_sec
    return [r for r in rows if lo <= float(r["ts"]) <= entry_ts]


def rule_max_rsi(pre: list[dict], threshold: float = 70.0) -> bool | None:
    """True = BLOCK. None = unknown (no RSI ticks)."""
    xs = [_f(r.get("cm_rsi")) for r in pre]
    xs = [x for x in xs if x is not None]
    if not xs:
        return None
    return max(xs) >= threshold


def rule_rsi_hold_sec(pre: list[dict], entry_ts: float,
                      hold_sec: float = 60.0, max_rsi: float = 50.0,
                      min_ticks: int = 3) -> bool | None:
    """True = BLOCK if any tick in the hold window is >= max_rsi."""
    xs = []
    for r in pre:
        ts = _f(r.get("ts"))
        v = _f(r.get("cm_rsi"))
        if ts is None or v is None:
            continue
        if entry_ts - hold_sec <= ts <= entry_ts:
            xs.append(v)
    if len(xs) < min_ticks:
        return None
    return any(v >= max_rsi for v in xs)


def rule_rsi_hold_ticks(pre: list[dict], n_ticks: int = 3,
                        max_rsi: float = 50.0) -> bool | None:
    xs = [_f(r.get("cm_rsi")) for r in pre]
    xs = [x for x in xs if x is not None]
    if len(xs) < n_ticks:
        return None
    last = xs[-n_ticks:]
    return any(v >= max_rsi for v in last)


def rule_max_exh(pre: list[dict], threshold: float = 80.0) -> bool | None:
    xs = [_f(r.get("exhaustion")) for r in pre]
    xs = [x for x in xs if x is not None]
    if not xs:
        return None
    return max(xs) >= threshold


def evaluate_fill(o: dict, shadow_rows: list[dict]) -> dict:
    et = float(o["entry_time"])
    feat = o.get("features") if isinstance(o.get("features"), dict) else {}
    fill_rsi = _f(feat.get("cm_rsi"))
    fill_exh = _f(o.get("entry_exhaustion"))
    pre = pre_window(shadow_rows, et, 300.0)
    rsis = [_f(r.get("cm_rsi")) for r in pre]
    rsis = [x for x in rsis if x is not None]
    exhs = [_f(r.get("exhaustion")) for r in pre]
    exhs = [x for x in exhs if x is not None]
    blocks = {
        "max_rsi_5m>=70": rule_max_rsi(pre, 70.0),
        "rsi_hold_60s<50": rule_rsi_hold_sec(pre, et, 60.0, 50.0, 3),
        "rsi_hold_3ticks<50": rule_rsi_hold_ticks(pre, 3, 50.0),
        "max_exh_5m>=80": rule_max_exh(pre, 80.0),
    }
    return {
        "symbol": str(o.get("symbol") or "").upper(),
        "day": _day_et(et),
        "entry_time": et,
        "fill_rsi": fill_rsi,
        "fill_exh": fill_exh,
        "max_pre_rsi": max(rsis) if rsis else None,
        "min_pre_rsi": min(rsis) if rsis else None,
        "rsi_range_5m": (max(rsis) - min(rsis)) if rsis else None,
        "max_pre_exh": max(exhs) if exhs else None,
        "n_pre": len(pre),
        "realized_r": _f(o.get("realized_r_multiple")),
        "blocks": blocks,
        "cm_rsi_src": o.get("cm_rsi_src") or feat.get("cm_rsi_src"),
        "bars_age_sec": _f(o.get("bars_age_sec") if o.get("bars_age_sec") is not None
                           else feat.get("bars_age_sec")),
    }


def _r_stats(rows: list[dict]) -> dict:
    rs = [r["realized_r"] for r in rows if r.get("realized_r") is not None]
    if not rs:
        return {"n": len(rows), "n_scored": 0, "sum_r": None, "avg_r": None,
                "win_rate": None}
    wins = sum(1 for r in rs if r > 0)
    return {
        "n": len(rows),
        "n_scored": len(rs),
        "sum_r": sum(rs),
        "avg_r": sum(rs) / len(rs),
        "win_rate": wins / len(rs),
    }


def summarize(rows: list[dict]) -> dict:
    out: dict[str, Any] = {"n_fills": len(rows), "rules": {}}
    for rule in RULES:
        blocked = [r for r in rows if r["blocks"].get(rule) is True]
        kept = [r for r in rows if r["blocks"].get(rule) is False]
        unknown = [r for r in rows if r["blocks"].get(rule) is None]
        out["rules"][rule] = {
            "blocked": _r_stats(blocked),
            "kept": _r_stats(kept),
            "unknown": _r_stats(unknown),
        }
    # Combined: block if max_rsi_5m OR (optional) — report intersection too
    both = [r for r in rows
            if r["blocks"].get("max_rsi_5m>=70") is True
            or r["blocks"].get("rsi_hold_60s<50") is True]
    neither = [r for r in rows
               if r["blocks"].get("max_rsi_5m>=70") is False
               and r["blocks"].get("rsi_hold_60s<50") is False]
    out["rules"]["max_rsi_OR_hold60"] = {
        "blocked": _r_stats(both),
        "kept": _r_stats(neither),
        "unknown": _r_stats([r for r in rows if r not in both and r not in neither]),
    }
    return out


def _fmt_stats(label: str, s: dict) -> str:
    if not s.get("n_scored"):
        return f"{label:<8} n={s['n']:<3}  (no scored R)"
    return (f"{label:<8} n={s['n']:<3} scored={s['n_scored']:<3} "
            f"sum_r={s['sum_r']:+7.3f}  avg_r={s['avg_r']:+7.3f}  "
            f"wr={100 * s['win_rate']:5.1f}%")


def print_report(rows: list[dict], summary: dict, days: int) -> None:
    print(f"rsi/exh stability screen  days={days}  fills_with_rsi={summary['n_fills']}")
    print("measure only — does not arm\n")
    if not rows:
        print("(no fills with cm_rsi in features)")
        return

    ages = [r["bars_age_sec"] for r in rows if r.get("bars_age_sec") is not None]
    src = defaultdict(int)
    for r in rows:
        src[str(r.get("cm_rsi_src"))] += 1
    if ages:
        ages.sort()
        print(f"bars_age_sec  n={len(ages)}  med={ages[len(ages) // 2]:.2f}  "
              f"max={ages[-1]:.2f}")
    print(f"cm_rsi_src    {dict(src)}")
    ranges = [r["rsi_range_5m"] for r in rows if r.get("rsi_range_5m") is not None]
    if ranges:
        ranges.sort()
        print(f"rsi_range_5m  med={ranges[len(ranges) // 2]:.0f}  "
              f"p90={ranges[int(0.9 * (len(ranges) - 1))]:.0f}")
    print()

    for rule, block in summary["rules"].items():
        print(f"## {rule}")
        print("  " + _fmt_stats("BLOCK", block["blocked"]))
        print("  " + _fmt_stats("KEEP", block["kept"]))
        if block["unknown"]["n"]:
            print("  " + _fmt_stats("UNKNOWN", block["unknown"]))
        b, k = block["blocked"], block["kept"]
        if b.get("n_scored") and k.get("n_scored"):
            # Positive delta_avg means blocking helped (kept avg > blocked avg)
            delta = (k["avg_r"] or 0) - (b["avg_r"] or 0)
            print(f"  keep_minus_block avg_r = {delta:+.3f}  "
                  f"(>0 means the rule removed worse fills)")
        print()

    # Show the thrashiest fills under max_rsi rule
    print("## worst pre-fill thrash (by rsi_range_5m)")
    print(f"{'day':10} {'sym':5} {'t':8} {'fillR':>6} {'maxR':>6} {'rng':>5} "
          f"{'fillE':>6} {'r':>7}  block_maxR")
    ranked = sorted(
        (r for r in rows if r.get("rsi_range_5m") is not None),
        key=lambda r: -float(r["rsi_range_5m"]),
    )[:15]
    for r in ranked:
        t = datetime.fromtimestamp(r["entry_time"], ET).strftime("%H:%M:%S")
        fr = r["fill_rsi"]
        mr = r["max_pre_rsi"]
        rng = r["rsi_range_5m"]
        fe = r["fill_exh"]
        rr = r["realized_r"]
        blk = r["blocks"].get("max_rsi_5m>=70")
        print(
            f"{r['day']:10} {r['symbol']:5} {t:8} "
            f"{fr if fr is not None else float('nan'):6.1f} "
            f"{mr if mr is not None else float('nan'):6.1f} "
            f"{rng if rng is not None else float('nan'):5.0f} "
            f"{fe if fe is not None else float('nan'):6.1f} "
            f"{rr if rr is not None else float('nan'):7.3f}  {blk}"
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=1,
                    help="Look back this many calendar days of fills")
    ap.add_argument("--write", action="store_true",
                    help="Write JSON under ai_reports/screens/")
    args = ap.parse_args(argv)

    fills = load_fills(args.days)
    symbols = {str(o.get("symbol") or "").upper() for o in fills}
    shadow = load_shadow_for(symbols, args.days)
    rows = [evaluate_fill(o, shadow.get(str(o.get("symbol") or "").upper(), []))
            for o in fills]
    summary = summarize(rows)
    print_report(rows, summary, args.days)

    if args.write:
        SCREEN_DIR.mkdir(parents=True, exist_ok=True)
        day = datetime.now(ET).strftime("%Y-%m-%d")
        path = SCREEN_DIR / f"rsi_exh_stability_{day}_d{args.days}.json"
        path.write_text(json.dumps({
            "generated": datetime.now(timezone.utc).isoformat(),
            "days": args.days,
            "summary": summary,
            "rows": rows,
        }, indent=2) + "\n", encoding="utf-8")
        print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
