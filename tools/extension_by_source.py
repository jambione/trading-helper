#!/usr/bin/env python3
"""Extension rate and R by source — the dead-follow-through ship rule.

Read-only. Does not write config or call the broker.

The RTH buy allow-list (``ai_watch_arm_sources``) stays ``*`` until this
tool prints PASS on the live ledger. Allowed sources are momentum and
movers. Everything else is the blocked set.

PASS (all of these):
  - blocked n >= 25
  - blocked sum R <= 0
  - blocked extension rate <= 1/10 of the momentum+movers extension rate
  - at most one blocked trade extended, and its R is not above the
    median R of an allowed extension (dropping the set does not remove
    more than one extended trade's R)

THIN: blocked n < 25. Do not flip the knob.
FAIL: n is large enough and a pass condition missed.

Usage (on the mini, where the ledger lives):
    .venv/bin/python tools/extension_by_source.py
    .venv/bin/python tools/extension_by_source.py --from 2026-09-18 --to 2026-09-22
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai_paths import resolve_report_dir  # noqa: E402

ET = ZoneInfo("America/New_York")
ALLOWED = frozenset({"momentum", "movers"})
BLOCKED_MIN_N = 25
EXT_MFE_R = 0.15


def _f(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _day(row: dict) -> str:
    ts = row.get("entry_time")
    if ts is None:
        ts = row.get("ts")
    try:
        return datetime.fromtimestamp(float(ts), ET).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return ""


def _feat(row: dict, key: str) -> float | None:
    features = row.get("features") if isinstance(row.get("features"), dict) else {}
    if key in features:
        return _f(features.get(key))
    return _f(row.get(key))


def _source(row: dict) -> str:
    return str(row.get("source") or "unknown").strip().lower() or "unknown"


def is_extended(row: dict) -> bool:
    """Stamped class wins. MFE is only the fallback for an unstamped row."""
    cls = str(row.get("extension_class") or "")
    if cls:
        return cls == "extended"
    mfe = _f(row.get("mfe_r"))
    return mfe is not None and mfe + 1e-12 >= EXT_MFE_R


def _median(xs: list[float]) -> float | None:
    return statistics.median(xs) if xs else None


def _load(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and _day(row):
                rows.append(row)
    return rows


def _bucket(rows: list[dict]) -> dict[str, Any]:
    by: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by[_source(row)].append(row)
    sources = []
    for src in sorted(by, key=lambda s: -len(by[s])):
        group = by[src]
        rs = [_f(r.get("realized_r_multiple")) or 0.0 for r in group]
        ext = [r for r in group if is_extended(r)]
        chg = [v for v in (_feat(r, "pct_change") for r in group) if v is not None]
        rvol = [v for v in (_feat(r, "rvol") for r in group) if v is not None]
        sources.append({
            "source": src,
            "n": len(group),
            "sum_r": round(sum(rs), 4),
            "median_r": None if not rs else round(_median(rs) or 0.0, 4),
            "wins": sum(1 for r in rs if r > 0),
            "extended_n": len(ext),
            "dead_n": len(group) - len(ext),
            "extension_rate": round(len(ext) / len(group), 4) if group else 0.0,
            "median_pct_change": None if not chg else round(_median(chg) or 0.0, 3),
            "median_rvol": None if not rvol else round(_median(rvol) or 0.0, 3),
            "allowed": src in ALLOWED,
        })
    return {"n": len(rows), "sources": sources}


def _set_stats(rows: list[dict]) -> dict[str, Any]:
    rs = [_f(r.get("realized_r_multiple")) or 0.0 for r in rows]
    ext = [r for r in rows if is_extended(r)]
    ext_r = [_f(r.get("realized_r_multiple")) or 0.0 for r in ext]
    return {
        "n": len(rows),
        "sum_r": round(sum(rs), 4),
        "extended_n": len(ext),
        "extension_rate": round(len(ext) / len(rows), 4) if rows else 0.0,
        "extended_sum_r": round(sum(ext_r), 4),
        "extended": [
            {
                "symbol": r.get("symbol"),
                "day": _day(r),
                "source": _source(r),
                "r": round(_f(r.get("realized_r_multiple")) or 0.0, 4),
                "mfe_r": _f(r.get("mfe_r")),
            }
            for r in ext
        ],
    }


def ship_verdict(rows: list[dict]) -> dict[str, Any]:
    """Pre-registered rule. See the module docstring."""
    allowed = [r for r in rows if _source(r) in ALLOWED]
    blocked = [r for r in rows if _source(r) not in ALLOWED]
    a = _set_stats(allowed)
    b = _set_stats(blocked)
    allowed_ext_rs = [
        _f(r.get("realized_r_multiple")) or 0.0
        for r in allowed if is_extended(r)
    ]
    median_allowed_ext = _median(allowed_ext_rs)
    blocked_ext = [r for r in blocked if is_extended(r)]
    blocked_ext_rs = [_f(r.get("realized_r_multiple")) or 0.0 for r in blocked_ext]
    blocked_ext_sum = sum(blocked_ext_rs)
    rate_cap = 0.1 * float(a["extension_rate"])
    reasons: list[str] = []
    if b["n"] < BLOCKED_MIN_N:
        reasons.append(f"blocked n {b['n']} < {BLOCKED_MIN_N}")
    if b["sum_r"] > 0:
        reasons.append(f"blocked sum R {b['sum_r']:+.3f} > 0")
    if b["extension_rate"] > rate_cap + 1e-12:
        reasons.append(
            f"blocked extension rate {b['extension_rate']:.3f} "
            f"> 1/10 of allowed {a['extension_rate']:.3f}"
        )
    if not allowed_ext_rs:
        reasons.append("allowed set has no extension")
    drop_limit = median_allowed_ext if median_allowed_ext is not None else 0.0
    if len(blocked_ext) > 1 or blocked_ext_sum > max(drop_limit, 0.0) + 1e-9:
        reasons.append(
            f"blocked extensions n={len(blocked_ext)} "
            f"sum R {blocked_ext_sum:+.3f} "
            f"(median allowed extension {drop_limit:+.3f})"
        )
    thin = b["n"] < BLOCKED_MIN_N
    if not reasons:
        verdict = "PASS"
    elif thin:
        verdict = "THIN"
    else:
        verdict = "FAIL"
    return {
        "verdict": verdict,
        "reasons": reasons,
        "allowed": a,
        "blocked": b,
        "rate_cap": round(rate_cap, 4),
        "median_allowed_extension_r": (
            None if median_allowed_ext is None else round(median_allowed_ext, 4)
        ),
    }


def score(
    rows: list[dict],
    *,
    day_from: str | None = None,
    day_to: str | None = None,
    require_class: bool = True,
) -> dict[str, Any]:
    """Default pool is rows that carry ``extension_class``.

    That stamp started 2026-09-21. Older rows are a different entry
    regime; inferring "extended" from MFE on them mixes the rule.
    ``require_class=False`` keeps those rows for a historical look.
    """
    chosen = []
    for row in rows:
        day = _day(row)
        if day_from and day < day_from:
            continue
        if day_to and day > day_to:
            continue
        if require_class and not row.get("extension_class"):
            continue
        if not row.get("extension_class") and _f(row.get("mfe_r")) is None:
            continue
        chosen.append(row)
    days = sorted({_day(r) for r in chosen})
    by_day = []
    for day in days:
        day_rows = [r for r in chosen if _day(r) == day]
        by_day.append({"day": day, **_bucket(day_rows), "ship": ship_verdict(day_rows)})
    return {
        "from": days[0] if days else day_from,
        "to": days[-1] if days else day_to,
        "days": days,
        "n": len(chosen),
        "pooled": _bucket(chosen),
        "ship": ship_verdict(chosen),
        "by_day": by_day,
    }


def _fmt(value: Any, spec: str = "") -> str:
    if value is None:
        return ""
    if spec:
        return format(value, spec)
    return str(value)


def render(payload: dict[str, Any]) -> str:
    ship = payload["ship"]
    lines = [
        f"# Extension by source — {payload.get('from')} → {payload.get('to')}",
        "",
        "Pool is rows that carry `extension_class` (square journal, from 2026-09-21). "
        "Pass `--include-unstamped` to fold in the older book.",
        "",
        f"**Ship rule: {ship['verdict']}**",
        "",
    ]
    if ship["reasons"]:
        lines.append("Reasons:")
        for reason in ship["reasons"]:
            lines.append(f"- {reason}")
        lines.append("")
    lines.append(
        f"Allowed (momentum, movers): n={ship['allowed']['n']} "
        f"sum R {ship['allowed']['sum_r']:+.3f} "
        f"extended {ship['allowed']['extended_n']} "
        f"rate {ship['allowed']['extension_rate']:.3f}"
    )
    lines.append(
        f"Blocked: n={ship['blocked']['n']} "
        f"sum R {ship['blocked']['sum_r']:+.3f} "
        f"extended {ship['blocked']['extended_n']} "
        f"rate {ship['blocked']['extension_rate']:.3f} "
        f"(cap {ship['rate_cap']:.3f})"
    )
    lines.append("")
    lines.append("## Pooled by source")
    lines.append("")
    lines.append("| Source | Set | n | Sum R | Median R | Wins | Extended | Dead | Rate | Med %chg | Med rvol |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for row in payload["pooled"]["sources"]:
        which = "allow" if row["allowed"] else "block"
        lines.append(
            f"| `{row['source']}` | {which} | {row['n']} | {row['sum_r']:+.3f} | "
            f"{_fmt(row['median_r'], '+.3f')} | {row['wins']} | {row['extended_n']} | "
            f"{row['dead_n']} | {row['extension_rate']:.3f} | "
            f"{_fmt(row['median_pct_change'], '+.2f')} | {_fmt(row['median_rvol'], '.2f')} |"
        )
    lines.append("")
    lines.append("## Extended trades")
    lines.append("")
    ext = ship["allowed"]["extended"] + ship["blocked"]["extended"]
    if not ext:
        lines.append("None.")
    else:
        lines.append("| Day | Symbol | Source | R | MFE |")
        lines.append("|---|---|---|---:|---:|")
        for row in sorted(ext, key=lambda r: (r["day"], r["source"], str(r["symbol"]))):
            lines.append(
                f"| {row['day']} | {row['symbol']} | `{row['source']}` | "
                f"{row['r']:+.3f} | {_fmt(row['mfe_r'], '+.3f')} |"
            )
    lines.append("")
    lines.append("## By day")
    lines.append("")
    for day in payload["by_day"]:
        lines.append(f"### {day['day']} — {day['ship']['verdict']} (n={day['n']})")
        lines.append("")
        lines.append("| Source | n | Sum R | Extended | Rate |")
        lines.append("|---|---:|---:|---:|---:|")
        for row in day["sources"]:
            lines.append(
                f"| `{row['source']}` | {row['n']} | {row['sum_r']:+.3f} | "
                f"{row['extended_n']} | {row['extension_rate']:.3f} |"
            )
        lines.append("")
    lines.append(
        "Knob stays `ai_watch_arm_sources=*`. Flip to `momentum,movers` "
        "only on PASS, in its own off-hours deploy."
    )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--from", dest="day_from", default=None)
    ap.add_argument("--to", dest="day_to", default=None)
    ap.add_argument("--report-dir", type=Path, default=None)
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument(
        "--include-unstamped",
        action="store_true",
        help="Include older rows with no extension_class (different regime)",
    )
    args = ap.parse_args(argv)

    rdir = args.report_dir or resolve_report_dir()
    rows = _load(rdir / "outcomes.jsonl")
    payload = score(
        rows,
        day_from=args.day_from,
        day_to=args.day_to,
        require_class=not args.include_unstamped,
    )
    text = render(payload)
    print(text)
    if not args.no_write and payload["days"]:
        audit = rdir / "audit"
        audit.mkdir(parents=True, exist_ok=True)
        stem = f"extension_by_source_{payload['from']}_{payload['to']}"
        (audit / f"{stem}.md").write_text(text, encoding="utf-8")
        (audit / f"{stem}.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8")
        print(f"Wrote {audit / stem}.md")
    return 0 if payload["ship"]["verdict"] != "FAIL" else 2


if __name__ == "__main__":
    raise SystemExit(main())
