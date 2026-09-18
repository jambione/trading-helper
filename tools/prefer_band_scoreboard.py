#!/usr/bin/env python3
"""Prefer-band CHG% admissions scoreboard (wave-capture §1).

Splits closed outcomes into prefer-band day CHG (~+8…+40%) vs outside
(below prefer or above soft max). Uses ``features.pct_change`` at entry
(admit stamp when present). Does **not** change knobs — measure first.

Writes::

    benchmarks/prefer_band/<day>.json
    benchmarks/prefer_band/<day>_summary.md
    benchmarks/prefer_band/range_<from>_<to>.json
    benchmarks/prefer_band/range_<from>_<to>_summary.md

Usage (mini, venv)::

    .venv/bin/python tools/prefer_band_scoreboard.py --day 2026-09-18
    .venv/bin/python tools/prefer_band_scoreboard.py --from 2026-09-17 --to 2026-09-18
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai_paths import resolve_report_dir  # noqa: E402

ET = ZoneInfo("America/New_York")
OUT_DIR = ROOT / "benchmarks" / "prefer_band"

DEFAULT_PREFER_MIN = 8.0
DEFAULT_PREFER_MAX = 40.0
DEFAULT_SOFT_MAX = 50.0
MFE_PAY_R = 0.15


def _cfg_bounds() -> tuple[float, float, float]:
    try:
        from config import load_config
        c = load_config()
        lo = float(c.get("ai_watch_admit_chg_prefer_min", DEFAULT_PREFER_MIN)
                   or DEFAULT_PREFER_MIN)
        hi = float(c.get("ai_watch_admit_chg_prefer_max", DEFAULT_PREFER_MAX)
                   or DEFAULT_PREFER_MAX)
        soft = float(c.get("ai_watch_admit_chg_soft_max", DEFAULT_SOFT_MAX)
                     or DEFAULT_SOFT_MAX)
        if hi < lo:
            lo, hi = hi, lo
        if soft < hi:
            soft = hi
        return lo, hi, soft
    except Exception:
        return DEFAULT_PREFER_MIN, DEFAULT_PREFER_MAX, DEFAULT_SOFT_MAX


def _f(v: Any) -> float | None:
    try:
        if v is None:
            return None
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return None
        return x
    except (TypeError, ValueError):
        return None


def _entry_day(row: dict) -> str | None:
    et = row.get("entry_time")
    if et is None:
        return None
    try:
        if isinstance(et, (int, float)):
            return datetime.fromtimestamp(float(et), ET).date().isoformat()
        s = str(et).strip()
        if len(s) >= 10 and s[4] == "-" and s[7] == "-":
            return s[:10]
        # ISO with Z
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(
            ET).date().isoformat()
    except Exception:
        return None


def _pct_at_entry(row: dict) -> float | None:
    feat = row.get("features") if isinstance(row.get("features"), dict) else {}
    for src in (row, feat):
        if not isinstance(src, dict):
            continue
        for k in ("admit_pct_change", "pct_change", "day_pct", "chg"):
            v = _f(src.get(k))
            if v is not None:
                return v
    return None


def _band(pct: float | None, lo: float, hi: float, soft: float) -> str:
    if pct is None:
        return "unknown"
    if lo <= float(pct) <= hi:
        return "prefer"
    if float(pct) > soft:
        return "over_soft"
    if float(pct) < lo:
        return "below_prefer"
    return "mid"  # between prefer_max and soft_max


def _iter_outcomes(path: Path):
    if not path.exists():
        return
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _bucket_stats(rows: list[dict]) -> dict[str, Any]:
    n = len(rows)
    if n == 0:
        return {
            "n": 0, "n_closes": 0, "win_rate": None, "sum_usd": 0.0,
            "sum_r": 0.0, "pct_mfe_ge_015": None, "never_green_pct": None,
            "med_hold_sec": None, "med_mfe_r": None, "med_entry_slip_r": None,
        }
    pls = [_f(r.get("realized_pl_usd")) for r in rows]
    rs = [_f(r.get("realized_r_multiple")) for r in rows]
    mfes = [_f(r.get("mfe_r")) for r in rows]
    holds = [_f(r.get("hold_sec")) for r in rows]
    slips = [_f(r.get("entry_slippage_r")) for r in rows]
    pls_ok = [x for x in pls if x is not None]
    rs_ok = [x for x in rs if x is not None]
    mfes_ok = [x for x in mfes if x is not None]
    holds_ok = [x for x in holds if x is not None]
    slips_ok = [x for x in slips if x is not None]
    wins = sum(1 for x in pls_ok if x > 1e-9)
    mfe_pay = sum(1 for x in mfes_ok if x + 1e-12 >= MFE_PAY_R)
    never_g = sum(1 for x in mfes_ok if x <= 1e-12)
    return {
        "n": n,
        "n_closes": n,
        "win_rate": round(wins / len(pls_ok), 4) if pls_ok else None,
        "sum_usd": round(sum(pls_ok), 4) if pls_ok else 0.0,
        "sum_r": round(sum(rs_ok), 4) if rs_ok else 0.0,
        "pct_mfe_ge_015": round(mfe_pay / len(mfes_ok), 4) if mfes_ok else None,
        "never_green_pct": round(never_g / len(mfes_ok), 4) if mfes_ok else None,
        "med_hold_sec": (
            round(statistics.median(holds_ok), 1) if holds_ok else None),
        "med_mfe_r": round(statistics.median(mfes_ok), 4) if mfes_ok else None,
        "med_entry_slip_r": (
            round(statistics.median(slips_ok), 4) if slips_ok else None),
    }


def score_day(
    day: str,
    *,
    outcomes_path: Path | None = None,
    prefer_min: float | None = None,
    prefer_max: float | None = None,
    soft_max: float | None = None,
) -> dict[str, Any]:
    lo, hi, soft = _cfg_bounds()
    if prefer_min is not None:
        lo = float(prefer_min)
    if prefer_max is not None:
        hi = float(prefer_max)
    if soft_max is not None:
        soft = float(soft_max)
    path = outcomes_path or (resolve_report_dir() / "outcomes.jsonl")
    by_band: dict[str, list[dict]] = {
        "prefer": [], "below_prefer": [], "mid": [], "over_soft": [], "unknown": [],
    }
    for row in _iter_outcomes(path):
        if not isinstance(row, dict):
            continue
        if _entry_day(row) != day:
            continue
        # Need a close to score P&L / MFE
        if row.get("exit_time") is None and row.get("close_reason") is None:
            continue
        pct = _pct_at_entry(row)
        band = _band(pct, lo, hi, soft)
        by_band.setdefault(band, []).append(row)

    prefer = _bucket_stats(by_band["prefer"])
    # Outside = everything scored that is not prefer (excl unknown optional)
    outside_rows = (
        by_band["below_prefer"] + by_band["mid"] + by_band["over_soft"]
    )
    outside = _bucket_stats(outside_rows)
    over = _bucket_stats(by_band["over_soft"])
    unknown = _bucket_stats(by_band["unknown"])

    n_total = sum(len(v) for v in by_band.values())
    payload = {
        "day": day,
        "bounds": {
            "prefer_min": lo, "prefer_max": hi, "soft_max": soft,
            "mfe_pay_r": MFE_PAY_R,
        },
        "n_total": n_total,
        "prefer": prefer,
        "outside": outside,
        "over_soft": over,
        "below_prefer": _bucket_stats(by_band["below_prefer"]),
        "mid": _bucket_stats(by_band["mid"]),
        "unknown": unknown,
        "pass_hint": _pass_hint(prefer, outside, n_total),
    }
    return payload


def _pass_hint(prefer: dict, outside: dict, n_total: int) -> str:
    if n_total < 4:
        return "insufficient_n"
    p_mfe = prefer.get("pct_mfe_ge_015")
    o_mfe = outside.get("pct_mfe_ge_015")
    p_usd = prefer.get("sum_usd") or 0.0
    o_usd = outside.get("sum_usd") or 0.0
    if p_mfe is None:
        return "no_prefer_mfe"
    if o_mfe is None:
        o_mfe = 0.0
    if p_mfe + 1e-9 >= o_mfe and p_usd >= o_usd:
        return "prefer_pays"
    if p_mfe + 0.05 < o_mfe or p_usd + 1e-9 < o_usd:
        return "prefer_misses_runners"
    return "mixed"


def _md_table(payload: dict) -> str:
    b = payload["bounds"]
    lines = [
        f"# Prefer-band scoreboard — {payload['day']}",
        "",
        f"Bounds: prefer **{b['prefer_min']:g}…{b['prefer_max']:g}%**, "
        f"soft max **{b['soft_max']:g}%**, MFE pay ≥ **{b['mfe_pay_r']}R**.",
        "",
        f"n_total={payload['n_total']} · pass_hint=`{payload['pass_hint']}`",
        "",
        "| Metric | Prefer CHG | Outside | Over soft |",
        "|--------|------------|---------|-----------|",
    ]
    keys = [
        ("n opens / closes", "n"),
        ("win rate", "win_rate"),
        ("sum $", "sum_usd"),
        ("sum R", "sum_r"),
        ("% MFE ≥ 0.15R", "pct_mfe_ge_015"),
        ("never_green %", "never_green_pct"),
        ("med hold sec", "med_hold_sec"),
        ("med MFE R", "med_mfe_r"),
        ("med entry slip R", "med_entry_slip_r"),
    ]
    for label, k in keys:
        def fmt(bucket: dict) -> str:
            v = bucket.get(k)
            if v is None:
                return "—"
            if k in ("win_rate", "pct_mfe_ge_015", "never_green_pct"):
                return f"{100.0 * float(v):.1f}%"
            if isinstance(v, float):
                return f"{v:.3g}"
            return str(v)
        lines.append(
            f"| {label} | {fmt(payload['prefer'])} | "
            f"{fmt(payload['outside'])} | {fmt(payload['over_soft'])} |"
        )
    lines.append("")
    lines.append(
        "Pass signal: prefer-band pays (MFE≥0.15R rate + $) vs outside; "
        "blow-offs / over_soft don’t; pair with open-minutes for occupancy."
    )
    lines.append("")
    return "\n".join(lines)


def write_day(payload: dict) -> tuple[Path, Path]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    day = payload["day"]
    jp = OUT_DIR / f"{day}.json"
    mp = OUT_DIR / f"{day}_summary.md"
    jp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    mp.write_text(_md_table(payload), encoding="utf-8")
    return jp, mp


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--day", help="ET calendar day YYYY-MM-DD")
    ap.add_argument("--from", dest="from_day", help="Range start (inclusive)")
    ap.add_argument("--to", dest="to_day", help="Range end (inclusive)")
    ap.add_argument("--outcomes", type=Path, default=None)
    ap.add_argument("--prefer-min", type=float, default=None)
    ap.add_argument("--prefer-max", type=float, default=None)
    ap.add_argument("--soft-max", type=float, default=None)
    args = ap.parse_args(argv)

    days: list[str] = []
    if args.day:
        days = [args.day]
    elif args.from_day and args.to_day:
        from datetime import date, timedelta
        a = date.fromisoformat(args.from_day)
        b = date.fromisoformat(args.to_day)
        if b < a:
            a, b = b, a
        cur = a
        while cur <= b:
            days.append(cur.isoformat())
            cur += timedelta(days=1)
    else:
        ap.error("pass --day or --from/--to")

    payloads = []
    for d in days:
        p = score_day(
            d,
            outcomes_path=args.outcomes,
            prefer_min=args.prefer_min,
            prefer_max=args.prefer_max,
            soft_max=args.soft_max,
        )
        jp, mp = write_day(p)
        payloads.append(p)
        print(f"wrote {jp.relative_to(ROOT)} · {mp.relative_to(ROOT)} · "
              f"n={p['n_total']} hint={p['pass_hint']}")

    if len(payloads) > 1:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        lo, hi = days[0], days[-1]
        # Aggregate prefer vs outside across days
        def _sum_bucket(key: str) -> dict[str, Any]:
            rows_n = sum(int(p[key]["n"] or 0) for p in payloads)
            # Re-score by concatenating is better; approximate from per-day
            # weighted averages for rates.
            sum_usd = sum(float(p[key]["sum_usd"] or 0) for p in payloads)
            sum_r = sum(float(p[key]["sum_r"] or 0) for p in payloads)
            # Weighted MFE rate
            mfe_num = 0.0
            mfe_den = 0.0
            ng_num = 0.0
            wr_num = 0.0
            wr_den = 0.0
            for p in payloads:
                b = p[key]
                n = int(b["n"] or 0)
                if n <= 0:
                    continue
                if b.get("pct_mfe_ge_015") is not None:
                    mfe_num += float(b["pct_mfe_ge_015"]) * n
                    mfe_den += n
                if b.get("never_green_pct") is not None:
                    ng_num += float(b["never_green_pct"]) * n
                if b.get("win_rate") is not None:
                    wr_num += float(b["win_rate"]) * n
                    wr_den += n
            return {
                "n": rows_n,
                "sum_usd": round(sum_usd, 4),
                "sum_r": round(sum_r, 4),
                "pct_mfe_ge_015": (
                    round(mfe_num / mfe_den, 4) if mfe_den else None),
                "never_green_pct": (
                    round(ng_num / mfe_den, 4) if mfe_den else None),
                "win_rate": round(wr_num / wr_den, 4) if wr_den else None,
            }
        agg = {
            "from": lo,
            "to": hi,
            "days": [p["day"] for p in payloads],
            "prefer": _sum_bucket("prefer"),
            "outside": _sum_bucket("outside"),
            "over_soft": _sum_bucket("over_soft"),
            "n_total": sum(int(p["n_total"] or 0) for p in payloads),
            "pass_hints": [p["pass_hint"] for p in payloads],
        }
        jp = OUT_DIR / f"range_{lo}_{hi}.json"
        mp = OUT_DIR / f"range_{lo}_{hi}_summary.md"
        jp.write_text(json.dumps(agg, indent=2), encoding="utf-8")
        md = [
            f"# Prefer-band range {lo} → {hi}",
            "",
            f"n_total={agg['n_total']}",
            "",
            "| Bucket | n | win% | sum $ | sum R | % MFE≥0.15R | never_green% |",
            "|--------|---|------|-------|-------|-------------|--------------|",
        ]
        for name in ("prefer", "outside", "over_soft"):
            b = agg[name]
            def pct(x):
                return "—" if x is None else f"{100*float(x):.1f}%"
            md.append(
                f"| {name} | {b['n']} | {pct(b.get('win_rate'))} | "
                f"{b['sum_usd']:.2f} | {b['sum_r']:.2f} | "
                f"{pct(b.get('pct_mfe_ge_015'))} | "
                f"{pct(b.get('never_green_pct'))} |"
            )
        md.append("")
        md.append(f"per-day hints: {', '.join(agg['pass_hints'])}")
        md.append("")
        mp.write_text("\n".join(md), encoding="utf-8")
        print(f"wrote {jp.relative_to(ROOT)} · {mp.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
