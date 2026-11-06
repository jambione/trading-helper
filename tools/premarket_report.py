#!/usr/bin/env python3
"""Premarket report (operator 11/06: premarket is a first-class part of the project). Read-only.

Reads the premarket ledger ai_reports/phase_b_ledger/YYYY-MM-DD.jsonl (config key prefix stays ai_phase_b_*; reports say
"premarket"). Per morning: the funnel (admits -> arm attempts -> arms -> entry limits -> fills -> exits), refusals by
reason, dry run or live, plus the frozen Hybrid C PASS / FAIL / NO DECISION verdict from tools/phase_b_scoreboard.py
(bars frozen in docs/PHASE_B_PREMARKET.md; this tool never moves them).

    .venv/bin/python tools/premarket_report.py                # last 10 mornings -> docs/PREMARKET.md (+ stdout)
    .venv/bin/python tools/premarket_report.py --ping         # today's morning in a few lines (09:00 cron), desktop banner on macOS
    .venv/bin/python tools/premarket_report.py --brief        # the short section daily_scoreboard embeds
"""
from __future__ import annotations

import argparse
import collections
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools import phase_b_scoreboard as pbs  # noqa: E402

ET = ZoneInfo("America/New_York")
LEDGER = ROOT / "ai_reports" / "phase_b_ledger"
OUT = ROOT / "docs" / "PREMARKET.md"
FEED_REASONS = {"phase_b_missing_print", "phase_b_stale_print", "pctr_not_live_alpaca", "missing_pctr", "stale_quote"}


def _k(r: dict) -> str:
    return str(r.get("kind") or "")


def funnel(rows: list[dict]) -> dict:
    """One morning's counts. Pure: rows are ledger dicts."""
    arm_ref = collections.Counter(str(r.get("reason") or "?") for r in rows if _k(r) == "arm_refuse")
    adm_ref = collections.Counter(str(r.get("reason") or "?") for r in rows if _k(r) == "admit_refuse")
    admits = [r for r in rows if _k(r) == "admit"]
    arms = [r for r in rows if _k(r) == "arm"]
    limits = [r for r in rows if _k(r) in ("entry_limit", "limit")]
    fills = [r for r in rows if _k(r) == "fill"]
    exits = [r for r in rows if _k(r) in ("exit", "flatten")]
    fp = next((r.get("fingerprint") or {} for r in rows if _k(r) == "config_fingerprint"), {})
    dry = fp.get("ai_phase_b_dry_run")
    if dry is None and limits:
        dry = all(bool(r.get("dry_run")) for r in limits)
    n_ref = sum(arm_ref.values())
    feed = sum(v for k, v in arm_ref.items() if k in FEED_REASONS)
    pl = [x for x in (pbs._f(r.get("pl_pct") or r.get("realized_pct")) for r in exits) if x is not None]
    return {
        "admits": len(admits), "names": len({r.get("symbol") for r in admits}),
        "arm_refusals": n_ref, "feed_share": (feed / n_ref) if n_ref else None,
        "arm_refuse_top": arm_ref.most_common(4), "admit_refuse_top": adm_ref.most_common(3),
        "arms": len(arms), "arm_symbols": sorted({str(r.get("symbol")) for r in arms}),
        "limits": len(limits), "fills": len(fills), "exits": len(exits),
        "net_pl_pct": round(sum(pl), 3) if pl else None,
        "dry_run": dry, "logged": bool(rows),
    }


def load_day(day: str) -> list[dict]:
    return pbs.load_jsonl(LEDGER / f"{day}.jsonl")


def days_available(n: int) -> list[str]:
    return sorted(p.stem for p in LEDGER.glob("20??-??-??.jsonl"))[-n:]


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{100 * x:.1f}%"


def _top(pairs: list[tuple[str, int]], total: int) -> str:
    if not pairs:
        return "-"
    return ", ".join(f"{k.replace('phase_b_', '')} {v} ({100 * v / max(1, total):.0f}%)" for k, v in pairs)


def mode_text(f: dict) -> str:
    return "DRY RUN" if f["dry_run"] else ("LIVE (paper)" if f["dry_run"] is False else "?")


def verdict_line(days: list[str]) -> str:
    rows: list[dict] = []
    for d in days:
        rows += load_day(d)
    v = pbs.verdict(pbs.summarize(rows))
    s = v.get("summary") or {}
    why = "; ".join(v.get("reasons") or []) or "-"
    return (f"Hybrid C verdict (frozen bars, docs/PHASE_B_PREMARKET.md): **{v.get('decision')}** - fills {s.get('n_fills')} "
            f"of {pbs.MIN_FILLS} needed, mornings with 3+ fills {s.get('n_days_ge_min_fills')} of {pbs.MIN_DAYS_WITH_FILLS}. ({why})")


def table(days: list[str]) -> list[str]:
    out = ["| morning | mode | names admitted | arm refusals | feed-caused | top refusals | arms | limits | fills | exits | net P/L % |",
           "|---|---|---|---|---|---|---|---|---|---|---|"]
    for d in reversed(days):
        f = funnel(load_day(d))
        if not f["logged"]:
            out.append(f"| {d} | not logged | - | - | - | - | - | - | - | - | - |")
            continue
        out.append(f"| {d} | {mode_text(f)} | {f['names']} | {f['arm_refusals']} | {_pct(f['feed_share'])} | "
                   f"{_top(f['arm_refuse_top'][:2], f['arm_refusals'])} | {f['arms']} | {f['limits']} | {f['fills']} | {f['exits']} | "
                   f"{'-' if f['net_pl_pct'] is None else f['net_pl_pct']} |")
    return out


def brief(n: int = 5) -> list[str]:
    days = days_available(n)
    if not days:
        return ["## Premarket", "", "No premarket ledger yet."]
    return ["## Premarket (04:00-09:28 ET lane; full report docs/PREMARKET.md)", "", verdict_line(days_available(30)), ""] + table(days)


def full(n: int = 10) -> str:
    days = days_available(n)
    lines = [f"# Premarket report (written {datetime.now(ET):%Y-%m-%d %H:%M} ET)", "",
             "Lane: 04:00 start, last entry 09:20, flatten 09:25, flat by 09:28. Limit orders only (extended_hours). "
             "Feed-caused = arm refusals for a missing/stale print or no live %R; on IEX this is ~100%, SIP should cut it.", "",
             verdict_line(days_available(30)), ""] + table(days)
    for d in reversed(days[-3:]):
        f = funnel(load_day(d))
        if f["logged"]:
            lines += ["", f"### {d}", f"- arm refusals: {_top(f['arm_refuse_top'], f['arm_refusals'])}",
                      f"- admit refusals: {_top(f['admit_refuse_top'], sum(v for _, v in f['admit_refuse_top']))}",
                      f"- armed: {', '.join(f['arm_symbols']) or '-'}"]
    return "\n".join(lines) + "\n"


def ping_text(day: str | None = None) -> str:
    day = day or datetime.now(ET).strftime("%Y-%m-%d")
    f = funnel(load_day(day))
    if not f["logged"]:
        return f"Premarket {day}: NOT LOGGED (no ledger file) - check the desk."
    return (f"Premarket {day} [{mode_text(f)}]: {f['names']} names, {f['arms']} arms, {f['limits']} limits, {f['fills']} fills, "
            f"{f['exits']} exits; refusals {f['arm_refusals']} ({_pct(f['feed_share'])} feed). Top: "
            f"{_top(f['arm_refuse_top'][:2], f['arm_refusals'])}")


def banner(text: str) -> None:
    try:
        safe = text.replace("\\", " ").replace('"', "'")[:230]
        subprocess.run(["osascript", "-e", f'display notification "{safe}" with title "Premarket"'], timeout=10, check=False)
    except Exception:  # noqa: BLE001 - a banner is a convenience, never a failure
        pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ping", action="store_true")
    ap.add_argument("--brief", action="store_true")
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--no-banner", action="store_true")
    a = ap.parse_args(argv)
    if a.ping:
        t = ping_text()
        print(t)
        (ROOT / "ai_reports").mkdir(exist_ok=True)
        with open(ROOT / "ai_reports" / "premarket_morning.log", "a") as fh:
            fh.write(f"{datetime.now(ET):%Y-%m-%d %H:%M} {t}\n")
        if not a.no_banner:
            banner(t)
        return 0
    if a.brief:
        print("\n".join(brief()))
        return 0
    text = full(a.days)
    OUT.write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
