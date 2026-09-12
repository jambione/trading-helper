#!/usr/bin/env python3
"""Missed-overlap daily digest — concurrency gap + arrived-hot (read-only).

Answers: could we have had overlapping armable names, and did we seat / open them?
Research tool only — does **not** write ``config/bot_config.json`` or change arms.

Outputs under ``benchmarks/overlap_miss/``:
  - ``YYYY-MM-DD.json`` (or ``--label``)
  - append ``results.jsonl``
  - ``summary.md`` (latest run)

Usage::

    .venv/bin/python tools/overlap_miss_digest.py --day 2026-09-11
    .venv/bin/python tools/overlap_miss_digest.py --from 2026-09-08 --to 2026-09-11
    .venv/bin/python tools/overlap_miss_digest.py --day yesterday
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from ai_paths import resolve_report_dir  # noqa: E402

import earlier_book_bench as ebb  # noqa: E402

ET = ZoneInfo("America/New_York")
OUT_DIR = ROOT / "benchmarks" / "overlap_miss"
RTH_START_MIN = 9 * 60 + 30   # 09:30
RTH_END_MIN = 15 * 60 + 50    # 15:50

KILL_FOOTER = """KILL IF (1–2 weeks): gap_armable_minus_open ≈ 0 most days AND mistimed_first_hot_rate not actionable,
OR metrics too noisy / contradict dashboard. Then delete tools/overlap_miss_digest.py + benchmarks/overlap_miss/."""

# Approximation notes (printed in JSON/md)
METHOD = (
    "v1 log-native: armable = shadow arm_ok in that RTH minute; "
    "seated = admit_funnel.kept_symbols forward-fill when present, else "
    "watch_drop elapsed reconstruction; open = outcomes entry→exit spans "
    "(fallback: entry_ok without matching outcome close). "
    "heating_too_low = seated/warming not yet armable; mistimed family = hot/late."
)


def _report() -> Path:
    return resolve_report_dir()


def _f(x: Any) -> float | None:
    return ebb._f(x)


def _et_day(ts: float) -> str:
    return ebb._et_day(ts)


def _days(day_from: str, day_to: str) -> list[str]:
    return ebb._days(day_from, day_to)


def _parse_day_arg(raw: str) -> str:
    s = (raw or "").strip().lower()
    if s in ("yesterday", "yest"):
        return (datetime.now(tz=ET) - timedelta(days=1)).strftime("%Y-%m-%d")
    if s in ("today",):
        return datetime.now(tz=ET).strftime("%Y-%m-%d")
    datetime.strptime(raw, "%Y-%m-%d")  # validate
    return raw


def _et_minute(ts: float) -> int:
    dt = datetime.fromtimestamp(float(ts), timezone.utc).astimezone(ET)
    return dt.hour * 60 + dt.minute


def _rth_minutes() -> list[int]:
    return list(range(RTH_START_MIN, RTH_END_MIN + 1))


def load_shadow_arm_ok(day_from: str, day_to: str) -> dict[str, list[tuple[float, str]]]:
    """day -> [(ts, sym), ...] for shadow rows with arm_ok True."""
    path = _report() / "shadow.jsonl"
    out: dict[str, list[tuple[float, str]]] = defaultdict(list)
    if not path.exists():
        return out
    with path.open(encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("arm_ok") is not True:
                continue
            ts = _f(r.get("ts"))
            sym = str(r.get("symbol") or "").upper().strip()
            if ts is None or not sym:
                continue
            day = _et_day(ts)
            if day < day_from or day > day_to:
                continue
            out[day].append((ts, sym))
    return out


def load_outcomes_by_day(day_from: str, day_to: str) -> dict[str, list[dict]]:
    path = _report() / "outcomes.jsonl"
    out: dict[str, list[dict]] = defaultdict(list)
    if not path.exists():
        return out
    with path.open(encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except Exception:
                continue
            ts = _f(r.get("ts") or r.get("exit_time") or r.get("entry_time"))
            if ts is None:
                continue
            day = _et_day(ts)
            if day < day_from or day > day_to:
                continue
            out[day].append(r)
    return out


def load_entry_ok(day_from: str, day_to: str) -> dict[str, list[tuple[float, str]]]:
    path = _report() / "events.jsonl"
    out: dict[str, list[tuple[float, str]]] = defaultdict(list)
    if not path.exists():
        return out
    with path.open(encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("kind") != "entry_ok":
                continue
            ts = _f(r.get("ts"))
            sym = str(r.get("symbol") or "").upper().strip()
            if ts is None or not sym:
                continue
            day = _et_day(ts)
            if day < day_from or day > day_to:
                continue
            out[day].append((ts, sym))
    return out


def _open_spans(outcomes: list[dict]) -> list[tuple[float, float, str]]:
    """(entry_ts, exit_ts, sym) with best-effort fields."""
    spans = []
    for r in outcomes:
        sym = str(r.get("symbol") or "").upper().strip()
        et = _f(r.get("entry_time"))
        xt = _f(r.get("exit_time") or r.get("ts"))
        if not sym or et is None:
            continue
        if xt is None:
            xt = et + 60.0
        if xt < et:
            et, xt = xt, et
        spans.append((et, xt, sym))
    return spans


def _bucket_sets_from_events(
    events: list[tuple[float, str]],
    day: str,
) -> dict[int, set[str]]:
    buckets: dict[int, set[str]] = defaultdict(set)
    for ts, sym in events:
        if _et_day(ts) != day:
            continue
        m = _et_minute(ts)
        if RTH_START_MIN <= m <= RTH_END_MIN:
            buckets[m].add(sym)
    return buckets


def _seated_from_funnel(funnel_day: list[dict]) -> dict[int, set[str]] | None:
    """Forward-fill kept_symbols across RTH minutes. None if not instrumented."""
    snaps: list[tuple[int, set[str]]] = []
    for r in funnel_day:
        ts = _f(r.get("ts"))
        if ts is None:
            continue
        kept = r.get("kept_symbols")
        if not kept:
            continue
        if isinstance(kept, str):
            kept = [x.strip() for x in kept.split(",") if x.strip()]
        syms = {str(x).upper().strip() for x in kept if str(x).strip()}
        if not syms:
            continue
        m = _et_minute(ts)
        if RTH_START_MIN <= m <= RTH_END_MIN:
            snaps.append((m, syms))
    if not snaps:
        return None
    snaps.sort(key=lambda x: x[0])
    out: dict[int, set[str]] = {}
    idx = 0
    cur: set[str] = set()
    for m in _rth_minutes():
        while idx < len(snaps) and snaps[idx][0] <= m:
            cur = set(snaps[idx][1])
            idx += 1
        out[m] = set(cur)
    return out


def _seated_from_drops(intervals: dict, day: str) -> dict[int, set[str]]:
    """Expand watch_drop reconstruction into per-minute seated sets for *day*."""
    out: dict[int, set[str]] = defaultdict(set)
    for sym, ivals in intervals.items():
        for a, b, _reason, _el in ivals:
            if _et_day(a) != day and _et_day(b) != day:
                continue
            # iterate minutes covered
            ma = _et_minute(a) if _et_day(a) == day else RTH_START_MIN
            mb = _et_minute(b) if _et_day(b) == day else RTH_END_MIN
            if _et_day(a) < day:
                ma = RTH_START_MIN
            if _et_day(b) > day:
                mb = RTH_END_MIN
            for m in range(max(ma, RTH_START_MIN), min(mb, RTH_END_MIN) + 1):
                out[m].add(sym)
    return out


def _open_by_minute(spans: list[tuple[float, float, str]], day: str) -> dict[int, set[str]]:
    out: dict[int, set[str]] = defaultdict(set)
    for et, xt, sym in spans:
        # walk minutes in span clipped to this day RTH
        t = et
        # step by 60s from entry
        while t <= xt:
            if _et_day(t) == day:
                m = _et_minute(t)
                if RTH_START_MIN <= m <= RTH_END_MIN:
                    out[m].add(sym)
            t += 60.0
            if t - et > 8 * 3600:  # safety
                break
        # ensure exit minute counted if same day
        if _et_day(xt) == day:
            m = _et_minute(xt)
            if RTH_START_MIN <= m <= RTH_END_MIN:
                out[m].add(sym)
    return out


def _pct_minutes(buckets: dict[int, set[str]], threshold: int) -> float | None:
    mins = _rth_minutes()
    if not mins:
        return None
    hit = sum(1 for m in mins if len(buckets.get(m) or ()) >= threshold)
    return hit / len(mins)


def concurrency_for_day(
    day: str,
    arm_ok: list[tuple[float, str]],
    intervals: dict,
    funnel_events: list[dict],
    outcomes: list[dict],
    entry_oks: list[tuple[float, str]],
) -> dict[str, Any]:
    armable = _bucket_sets_from_events(arm_ok, day)

    funnel_day = [r for r in funnel_events if _et_day(float(r["ts"])) == day]
    seated_funnel = _seated_from_funnel(funnel_day)
    seated_src = "admit_funnel.kept_symbols"
    if seated_funnel is None:
        seated = _seated_from_drops(intervals, day)
        seated_src = "watch_drop_elapsed_reconstruction"
    else:
        seated = seated_funnel

    spans = _open_spans(outcomes)
    open_src = "outcomes_entry_exit"
    if not spans and entry_oks:
        # fallback: treat entry_ok as 5m open stub (documented)
        open_src = "entry_ok_5m_stub"
        spans = [(ts, ts + 300.0, sym) for ts, sym in entry_oks]
    opens = _open_by_minute(spans, day)

    peaks_a = max((len(s) for s in armable.values()), default=0)
    peaks_s = max((len(s) for s in seated.values()), default=0)
    peaks_o = max((len(s) for s in opens.values()), default=0)

    # Also count HTL-only minutes as context (warming seats)
    ledger = ebb.load_ledger_day(day)
    htl_by_min: dict[int, set[str]] = defaultdict(set)
    for r in ledger:
        if ebb._why0(r) != ebb.HTL:
            continue
        ts = _f(r.get("ts"))
        sym = str(r.get("symbol") or "").upper().strip()
        if ts is None or not sym or _et_day(ts) != day:
            continue
        m = _et_minute(ts)
        if RTH_START_MIN <= m <= RTH_END_MIN:
            htl_by_min[m].add(sym)

    return {
        "peak_armable_concurrent": peaks_a,
        "peak_seated": peaks_s,
        "peak_open": peaks_o,
        "gap_armable_minus_open": peaks_a - peaks_o,
        "pct_minutes_armable_ge_2": _pct_minutes(armable, 2),
        "pct_minutes_armable_ge_3": _pct_minutes(armable, 3),
        "pct_minutes_armable_ge_4": _pct_minutes(armable, 4),
        "pct_minutes_open_ge_2": _pct_minutes(opens, 2),
        "pct_minutes_open_ge_3": _pct_minutes(opens, 3),
        "peak_htl_warming": max((len(s) for s in htl_by_min.values()), default=0),
        "n_shadow_arm_ok": len(arm_ok),
        "n_outcomes": len(outcomes),
        "seated_source": seated_src,
        "open_source": open_src,
        "phase1_read": _phase1_read(peaks_a, peaks_o),
    }


def _phase1_read(peak_a: int, peak_o: int) -> str:
    gap = peak_a - peak_o
    if gap >= 2 and peak_a >= 3 and peak_o <= 1:
        return "seating/earlier-book problem (armable≥3 often available, open≤1)"
    if gap <= 1 and peak_a < 2:
        return "setup scarcity (armable rarely ≥2; gap small)"
    if gap >= 2:
        return "concurrency left on table (armable peak ahead of opens)"
    return "mixed / modest gap"


def arrived_hot_for_day(
    day: str,
    intervals: dict,
    funnel_events: list[dict],
    kept_first: dict,
) -> dict[str, Any]:
    row = ebb.score_day(day, intervals, funnel_events, kept_first)
    # Top arrived-hot symbols
    ledger = ebb.load_ledger_day(day)
    first_mistimed: dict[str, dict] = {}
    first_htl: dict[str, dict] = {}
    for r in ledger:
        ts = _f(r.get("ts"))
        sym = str(r.get("symbol") or "").upper().strip()
        if ts is None or not sym or _et_day(ts) != day:
            continue
        tok = ebb._why0(r)
        if tok == ebb.HTL:
            if sym not in first_htl or ts < float(first_htl[sym]["ts"]):
                first_htl[sym] = r
        if tok in ebb.MISTIMED_FAMILY:
            if sym not in first_mistimed or ts < float(first_mistimed[sym]["ts"]):
                first_mistimed[sym] = r

    top = []
    for sym, m in first_mistimed.items():
        mts = float(m["ts"])
        h = first_htl.get(sym)
        prior = h is not None and float(h["ts"]) < mts - 1.0
        if prior:
            continue
        dt = datetime.fromtimestamp(mts, timezone.utc).astimezone(ET)
        top.append({
            "symbol": sym,
            "first_refuse_why": ebb._why0(m),
            "first_rsi": ebb._rsi_at(m),
            "time_et": dt.strftime("%H:%M:%S"),
            "ts": mts,
        })
    top.sort(key=lambda x: x["ts"])
    row["arrived_hot_symbols"] = top[:25]
    return row


def dollar_deployment(outcomes: list[dict], peak_open: int) -> dict[str, Any]:
    pls = []
    notionals = []
    for r in outcomes:
        pl = _f(r.get("realized_pl_usd"))
        if pl is not None:
            pls.append(pl)
        qty = _f(r.get("total_qty"))
        px = _f(r.get("entry_price"))
        if qty is not None and px is not None and qty > 0 and px > 0:
            notionals.append(qty * px)
    out: dict[str, Any] = {
        "day_realized_pl_usd": (sum(pls) if pls else None),
        "n_fills_with_pl": len(pls),
        "max_concurrent_opens": peak_open,
        "approx_median_notional": (
            statistics.median(notionals) if notionals else None),
        "approx_peak_notional_if_flat_size": (
            (statistics.median(notionals) * peak_open)
            if notionals and peak_open else None),
    }
    if not pls:
        out["note"] = "No realized_pl_usd on outcomes for this day — $ section thin."
    elif peak_open <= 2:
        out["idle_equity_one_liner"] = (
            f"max concurrent opens={peak_open} with day P&L "
            f"${out['day_realized_pl_usd']:+.2f} — concurrency never showed up "
            "(idle equity story if book equity ≫ deployed notional)."
        )
    return out


def score_day_bundle(
    day: str,
    shadow: dict[str, list],
    intervals: dict,
    funnel: list[dict],
    kept_first: dict,
    outcomes_by_day: dict[str, list],
    entry_oks: dict[str, list],
) -> dict[str, Any]:
    conc = concurrency_for_day(
        day,
        shadow.get(day) or [],
        intervals,
        funnel,
        outcomes_by_day.get(day) or [],
        entry_oks.get(day) or [],
    )
    hot = arrived_hot_for_day(day, intervals, funnel, kept_first)
    dollars = dollar_deployment(outcomes_by_day.get(day) or [], conc["peak_open"])
    return {
        "day": day,
        "method": METHOD,
        "concurrency": conc,
        "arrived_hot": {
            "n_mistimed_symbols": hot.get("n_mistimed_symbols"),
            "mistimed_first_hot_rate": hot.get("mistimed_first_hot_rate"),
            "arrived_hot_n": hot.get("arrived_hot_n"),
            "first_mistimed_rsi_med": hot.get("first_mistimed_rsi_med"),
            "arrived_hot_symbols": hot.get("arrived_hot_symbols") or [],
            "kept_symbols_instrumented": hot.get("kept_symbols_instrumented"),
        },
        "dollars": dollars,
    }


def pool_days(rows: list[dict]) -> dict[str, Any]:
    def avg(path: tuple[str, ...]):
        vals = []
        for r in rows:
            cur: Any = r
            for k in path:
                cur = (cur or {}).get(k) if isinstance(cur, dict) else None
            if cur is not None:
                try:
                    vals.append(float(cur))
                except (TypeError, ValueError):
                    pass
        return statistics.fmean(vals) if vals else None

    return {
        "n_days": len(rows),
        "peak_armable_concurrent_avg": avg(("concurrency", "peak_armable_concurrent")),
        "peak_open_avg": avg(("concurrency", "peak_open")),
        "gap_armable_minus_open_avg": avg(("concurrency", "gap_armable_minus_open")),
        "pct_minutes_armable_ge_3_avg": avg(("concurrency", "pct_minutes_armable_ge_3")),
        "pct_minutes_open_ge_2_avg": avg(("concurrency", "pct_minutes_open_ge_2")),
        "mistimed_first_hot_rate_avg": avg(("arrived_hot", "mistimed_first_hot_rate")),
        "day_realized_pl_usd_sum": sum(
            float((r.get("dollars") or {}).get("day_realized_pl_usd") or 0)
            for r in rows
        ),
    }


def _fmt_pct(x: Any) -> str:
    if x is None:
        return "—"
    return f"{100.0 * float(x):.1f}%"


def _fmt_num(x: Any, nd: int = 2) -> str:
    if x is None:
        return "—"
    try:
        return f"{float(x):.{nd}f}"
    except (TypeError, ValueError):
        return str(x)


def render_summary(payload: dict) -> str:
    lines = [
        "# Overlap miss digest",
        "",
        f"Generated `{payload.get('generated_at')}`  ·  "
        f"range `{payload.get('day_from')}` → `{payload.get('day_to')}`"
        + (f"  ·  label `{payload['label']}`" if payload.get("label") else ""),
        "",
        f"**Method:** {METHOD}",
        "",
        "## Per day",
        "",
        "| day | peak_armable | peak_seated | peak_open | gap | "
        "armable≥3 min% | open≥2 min% | mistimed_first_hot | day $ | read |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for r in payload.get("days") or []:
        c = r.get("concurrency") or {}
        a = r.get("arrived_hot") or {}
        d = r.get("dollars") or {}
        pl = d.get("day_realized_pl_usd")
        pl_s = f"${pl:+.2f}" if pl is not None else "—"
        lines.append(
            f"| {r['day']} | {c.get('peak_armable_concurrent')} | "
            f"{c.get('peak_seated')} | {c.get('peak_open')} | "
            f"{c.get('gap_armable_minus_open')} | "
            f"{_fmt_pct(c.get('pct_minutes_armable_ge_3'))} | "
            f"{_fmt_pct(c.get('pct_minutes_open_ge_2'))} | "
            f"{_fmt_pct(a.get('mistimed_first_hot_rate'))} | {pl_s} | "
            f"{c.get('phase1_read', '')} |"
        )

    pooled = payload.get("pooled") or {}
    lines += [
        "",
        "## Pooled",
        "",
        f"- days: {pooled.get('n_days')}",
        f"- avg peak_armable: {_fmt_num(pooled.get('peak_armable_concurrent_avg'), 2)}",
        f"- avg peak_open: {_fmt_num(pooled.get('peak_open_avg'), 2)}",
        f"- avg gap_armable_minus_open: {_fmt_num(pooled.get('gap_armable_minus_open_avg'), 2)}",
        f"- avg % minutes armable≥3: {_fmt_pct(pooled.get('pct_minutes_armable_ge_3_avg'))}",
        f"- avg % minutes open≥2: {_fmt_pct(pooled.get('pct_minutes_open_ge_2_avg'))}",
        f"- avg mistimed_first_hot_rate: {_fmt_pct(pooled.get('mistimed_first_hot_rate_avg'))}",
        f"- sum day realized $: {_fmt_num(pooled.get('day_realized_pl_usd_sum'), 2)}",
        "",
        "## Arrived-hot samples (first day with any)",
        "",
    ]
    shown = False
    for r in payload.get("days") or []:
        syms = (r.get("arrived_hot") or {}).get("arrived_hot_symbols") or []
        if not syms:
            continue
        lines.append(f"### {r['day']}")
        lines.append("")
        lines.append("| symbol | why | RSI | time ET |")
        lines.append("|---|---|---:|---|")
        for s in syms[:15]:
            rsi = s.get("first_rsi")
            rsi_s = f"{rsi:.1f}" if rsi is not None else "—"
            lines.append(
                f"| {s.get('symbol')} | {s.get('first_refuse_why')} | "
                f"{rsi_s} | {s.get('time_et')} |"
            )
        lines.append("")
        shown = True
        break
    if not shown:
        lines.append("_No arrived-hot symbols in range._")
        lines.append("")

    lines += [
        "## Kill criteria",
        "",
        "```",
        KILL_FOOTER,
        "```",
        "",
    ]
    return "\n".join(lines)


def write_outputs(payload: dict, label: str | None) -> dict[str, str]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if label:
        stem = label
    elif payload.get("day_from") == payload.get("day_to"):
        stem = payload["day_from"]
    else:
        stem = f"{payload['day_from']}_to_{payload['day_to']}"
    json_path = OUT_DIR / f"{stem}.json"
    json_path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    results = OUT_DIR / "results.jsonl"
    with results.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "ts": payload.get("generated_at"),
            "stem": stem,
            "day_from": payload.get("day_from"),
            "day_to": payload.get("day_to"),
            "label": label,
            "pooled": payload.get("pooled"),
        }, default=str) + "\n")
    md_path = OUT_DIR / "summary.md"
    md_path.write_text(render_summary(payload), encoding="utf-8")
    # also keep dated copy
    dated_md = OUT_DIR / f"{stem}.md"
    dated_md.write_text(md_path.read_text(encoding="utf-8"), encoding="utf-8")
    return {
        "json": str(json_path),
        "results_jsonl": str(results),
        "summary_md": str(md_path),
        "dated_md": str(dated_md),
    }


def run(day_from: str, day_to: str, label: str | None = None) -> dict[str, Any]:
    print(f"overlap_miss_digest  {day_from} → {day_to}  report={_report()}")
    print(f"method: {METHOD}")
    intervals = ebb.load_drop_intervals(day_from, day_to)
    funnel = ebb.load_admit_funnel_events(day_from, day_to)
    kept_first = ebb.first_kept_ts_by_symbol(funnel)
    print("loading shadow arm_ok (may take a bit on large files)…")
    shadow = load_shadow_arm_ok(day_from, day_to)
    outcomes = load_outcomes_by_day(day_from, day_to)
    entry_oks = load_entry_ok(day_from, day_to)
    print(
        f"watch_drop_syms={len(intervals)}  funnel={len(funnel)}  "
        f"shadow_days={len(shadow)}  outcome_days={len(outcomes)}"
    )

    day_rows = []
    for day in _days(day_from, day_to):
        row = score_day_bundle(
            day, shadow, intervals, funnel, kept_first, outcomes, entry_oks)
        c = row["concurrency"]
        print(
            f"  {day}  armable={c['peak_armable_concurrent']}  "
            f"seated={c['peak_seated']}  open={c['peak_open']}  "
            f"gap={c['gap_armable_minus_open']}  "
            f"hot_rate={_fmt_pct(row['arrived_hot'].get('mistimed_first_hot_rate'))}  "
            f"[{c['phase1_read']}]"
        )
        day_rows.append(row)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "day_from": day_from,
        "day_to": day_to,
        "label": label,
        "report_dir": str(_report()),
        "method": METHOD,
        "kill_criteria": KILL_FOOTER,
        "days": day_rows,
        "pooled": pool_days(day_rows),
    }
    paths = write_outputs(payload, label)
    payload["wrote"] = paths
    print(f"wrote {paths['summary_md']}")
    return payload


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--day", default="", help="YYYY-MM-DD or yesterday")
    ap.add_argument("--from", dest="day_from", default="")
    ap.add_argument("--to", dest="day_to", default="")
    ap.add_argument("--label", default="", help="output stem override")
    args = ap.parse_args()

    if args.day:
        d = _parse_day_arg(args.day)
        day_from = day_to = d
    else:
        if not args.day_from or not args.day_to:
            ap.error("provide --day or both --from and --to")
        day_from = _parse_day_arg(args.day_from)
        day_to = _parse_day_arg(args.day_to)
        if day_to < day_from:
            ap.error("--to before --from")

    run(day_from, day_to, label=(args.label or None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
