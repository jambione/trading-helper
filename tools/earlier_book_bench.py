#!/usr/bin/env python3
"""Earlier-book coverage benchmark — baseline vs after soft-seed/warming.

Scores whether names get **seated before** mistimed/hot RSI, not whether
arms/exits are looser. Does **not** write ``config/bot_config.json``.

Metrics (per ET day + pooled):
  mistimed_first_hot_rate   — mistimed-family symbols with no prior same-day
                              heating_too_low (arrived already hot). Lower better.
  first_mistimed_rsi_med    — median RSI at first mistimed-family refuse
  share_rsi_ge_52 / ge_55   — at first mistimed refuse
  htl_seated_ge_2m_rate     — heating_too_low with on-book ≥2m (warming OK)
  late_seat_htl_rate        — heating_too_low seated <2m. Lower better.
  median_sec_kept_to_*      — from admit_funnel kept_symbols (post A1); else
                              watch_drop elapsed reconstruction (documented)
  n_kept / kept_ge_4        — occupancy proxy from admit_funnel

Usage::

    .venv/bin/python tools/earlier_book_bench.py --from 2026-09-08 --to 2026-09-11 --label baseline
    .venv/bin/python tools/earlier_book_bench.py --from 2026-09-08 --to 2026-09-11 --label baseline --counterfactual-soft-seed
    .venv/bin/python tools/earlier_book_bench.py --compare benchmarks/earlier_book/baseline.json benchmarks/earlier_book/after_v1.json
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ET = ZoneInfo("America/New_York")
OUT_DIR = ROOT / "benchmarks" / "earlier_book"
EVENTS = ROOT / "ai_reports" / "events.jsonl"
LEDGER_DIR = ROOT / "ai_reports" / "decision_ledger"
MOVERS = ROOT / "movers_stocks.json"
TRENDING = ROOT / "trending_stocks.json"

MISTIMED_FAMILY = frozenset({
    "mistimed_heat", "late_heat", "already_extended",
    "cheap_ob_band", "extended_cheap",
})
HTL = "heating_too_low"


def _et_day(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), timezone.utc).astimezone(ET).strftime(
        "%Y-%m-%d")


def _why0(r: dict) -> str:
    w = str(r.get("arm_why") or "").strip().lower()
    if w:
        return w.split()[0].split(":")[0]
    return str(r.get("arm_bucket") or "").strip().lower().split()[0] if r.get("arm_bucket") else ""


def _f(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v


def _days(day_from: str, day_to: str) -> list[str]:
    d0 = datetime.strptime(day_from, "%Y-%m-%d").date()
    d1 = datetime.strptime(day_to, "%Y-%m-%d").date()
    out = []
    d = d0
    while d <= d1:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def load_drop_intervals(day_from: str, day_to: str) -> dict[str, list[tuple]]:
    """symbol -> [(start, end, reason, elapsed), ...] from watch_drop elapsed_sec."""
    intervals: dict[str, list] = defaultdict(list)
    if not EVENTS.exists():
        return intervals
    with EVENTS.open(encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("kind") != "watch_drop":
                continue
            ts = _f(r.get("ts"))
            sym = str(r.get("symbol") or "").upper().strip()
            if ts is None or not sym:
                continue
            day = _et_day(ts)
            if day < day_from or day > day_to:
                continue
            elapsed = _f(r.get("elapsed_sec")) or 60.0
            if elapsed <= 0:
                elapsed = 60.0
            intervals[sym].append(
                (ts - elapsed, ts, str(r.get("reason") or ""), elapsed))
    return intervals


def seated_at(
    intervals: dict,
    sym: str,
    ts: float,
) -> tuple[bool, float | None]:
    """Return (on_book, seated_sec) at *ts* using drop-interval reconstruction."""
    best = None
    for a, b, _reason, _el in intervals.get(sym) or []:
        if a <= ts <= b + 1.0:
            seated = max(0.0, ts - a)
            if best is None or seated > best:
                best = seated
    if best is None:
        return False, None
    return True, best


def load_admit_funnel_events(day_from: str, day_to: str) -> list[dict]:
    rows = []
    if not EVENTS.exists():
        return rows
    with EVENTS.open(encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("kind") != "admit_funnel":
                continue
            ts = _f(r.get("ts"))
            if ts is None:
                continue
            day = _et_day(ts)
            if day < day_from or day > day_to:
                continue
            rows.append(r)
    return rows


def first_kept_ts_by_symbol(funnel_events: list[dict]) -> dict[tuple[str, str], float]:
    """(sym, day) -> earliest funnel event ts that listed the symbol in kept_symbols."""
    out: dict[tuple[str, str], float] = {}
    for r in funnel_events:
        ts = float(r["ts"])
        day = _et_day(ts)
        kept = r.get("kept_symbols") or []
        if isinstance(kept, str):
            kept = [x.strip() for x in kept.split(",") if x.strip()]
        for raw in kept:
            sym = str(raw or "").upper().strip()
            if not sym:
                continue
            key = (sym, day)
            if key not in out or ts < out[key]:
                out[key] = ts
    return out


def load_ledger_day(day: str) -> list[dict]:
    path = LEDGER_DIR / f"{day}.jsonl"
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("arm_ok") is True:
                continue
            rows.append(r)
    return rows


def _rsi_at(r: dict) -> float | None:
    for k in ("cm_rsi", "rsi", "arm_rsi", "pass_rsi"):
        v = _f(r.get(k))
        if v is not None:
            return v
    ind = r.get("indicator") if isinstance(r.get("indicator"), dict) else {}
    return _f(ind.get("cm_rsi") or ind.get("rsi"))


def score_day(
    day: str,
    intervals: dict,
    funnel_events: list[dict],
    kept_first: dict[tuple[str, str], float],
) -> dict[str, Any]:
    ledger = load_ledger_day(day)
    # First mistimed-family and first HTL per symbol
    first_mistimed: dict[str, dict] = {}
    first_htl: dict[str, dict] = {}
    htl_rows: list[dict] = []
    for r in ledger:
        ts = _f(r.get("ts"))
        sym = str(r.get("symbol") or "").upper().strip()
        if ts is None or not sym:
            continue
        if _et_day(ts) != day:
            continue
        tok = _why0(r)
        if tok == HTL:
            htl_rows.append(r)
            if sym not in first_htl or ts < float(first_htl[sym]["ts"]):
                first_htl[sym] = r
        if tok in MISTIMED_FAMILY:
            if sym not in first_mistimed or ts < float(first_mistimed[sym]["ts"]):
                first_mistimed[sym] = r

    mistimed_syms = sorted(first_mistimed)
    arrived_hot = 0
    rsi_vals = []
    ge52 = ge55 = 0
    for sym in mistimed_syms:
        m = first_mistimed[sym]
        mts = float(m["ts"])
        h = first_htl.get(sym)
        prior_htl = h is not None and float(h["ts"]) < mts - 1.0
        if not prior_htl:
            arrived_hot += 1
        rsi = _rsi_at(m)
        if rsi is not None:
            rsi_vals.append(rsi)
            if rsi >= 52:
                ge52 += 1
            if rsi >= 55:
                ge55 += 1

    htl_ge2 = htl_late = htl_unknown = 0
    for r in htl_rows:
        sym = str(r.get("symbol") or "").upper()
        ts = float(r["ts"])
        on, seated = seated_at(intervals, sym, ts)
        if not on or seated is None:
            htl_unknown += 1
            continue
        if seated >= 120.0:
            htl_ge2 += 1
        else:
            htl_late += 1
    htl_known = htl_ge2 + htl_late

    # Timing from kept_symbols when present
    dt_warm = []
    dt_mist = []
    for sym, m in first_mistimed.items():
        key = (sym, day)
        kt = kept_first.get(key)
        if kt is None:
            continue
        dt_mist.append(float(m["ts"]) - float(kt))
    for sym, h in first_htl.items():
        key = (sym, day)
        kt = kept_first.get(key)
        if kt is None:
            continue
        dt_warm.append(float(h["ts"]) - float(kt))

    day_funnels = [r for r in funnel_events if _et_day(float(r["ts"])) == day]
    n_kept_vals = []
    ge4 = 0
    for r in day_funnels:
        nk = r.get("n_kept")
        if nk is None:
            nk = r.get("kept_n")
        nk = int(nk) if nk is not None else len(r.get("kept_symbols") or [])
        n_kept_vals.append(nk)
        if nk >= 4:
            ge4 += 1

    n_mist = len(mistimed_syms) or 0
    return {
        "day": day,
        "n_mistimed_symbols": n_mist,
        "mistimed_first_hot_rate": (arrived_hot / n_mist) if n_mist else None,
        "arrived_hot_n": arrived_hot,
        "first_mistimed_rsi_med": (
            statistics.median(rsi_vals) if rsi_vals else None),
        "share_rsi_ge_52": (ge52 / len(rsi_vals)) if rsi_vals else None,
        "share_rsi_ge_55": (ge55 / len(rsi_vals)) if rsi_vals else None,
        "n_htl": len(htl_rows),
        "htl_seated_ge_2m_rate": (htl_ge2 / htl_known) if htl_known else None,
        "late_seat_htl_rate": (htl_late / htl_known) if htl_known else None,
        "htl_seating_unknown": htl_unknown,
        "median_sec_kept_to_first_warm_refuse": (
            statistics.median(dt_warm) if dt_warm else None),
        "median_sec_kept_to_first_mistimed": (
            statistics.median(dt_mist) if dt_mist else None),
        "n_kept_mean": statistics.fmean(n_kept_vals) if n_kept_vals else None,
        "n_kept_med": statistics.median(n_kept_vals) if n_kept_vals else None,
        "kept_ge4_share": (ge4 / len(n_kept_vals)) if n_kept_vals else None,
        "n_funnel_snaps": len(day_funnels),
        "kept_symbols_instrumented": any(
            r.get("kept_symbols") for r in day_funnels),
    }


def counterfactual_soft_seed(
    day_from: str,
    day_to: str,
    intervals: dict,
) -> dict[str, Any]:
    """Among arrived-hot mistimed symbols, was the name discoverable earlier?

    Uses movers/trending file snapshots if present (current file only — limited)
    plus ledger/shadow presence. Reports % with any same-day refuse/seed
    evidence ≥10m / ≥30m before first mistimed.
    """
    # Build arrived-hot set across days
    arrived: list[tuple[str, str, float]] = []  # sym, day, mistimed_ts
    for day in _days(day_from, day_to):
        first_m: dict[str, float] = {}
        first_h: dict[str, float] = {}
        for r in load_ledger_day(day):
            ts = _f(r.get("ts"))
            sym = str(r.get("symbol") or "").upper().strip()
            if ts is None or not sym:
                continue
            tok = _why0(r)
            if tok == HTL:
                first_h[sym] = min(first_h.get(sym, ts), ts)
            if tok in MISTIMED_FAMILY:
                first_m[sym] = min(first_m.get(sym, ts), ts)
        for sym, mts in first_m.items():
            hts = first_h.get(sym)
            if hts is None or hts >= mts - 1.0:
                arrived.append((sym, day, mts))

    # Evidence: any ledger row or watch_drop for sym earlier same day
    early10 = early30 = 0
    for sym, day, mts in arrived:
        found10 = found30 = False
        # watch intervals starting earlier
        for a, b, _r, _e in intervals.get(sym) or []:
            if _et_day(a) != day:
                continue
            if a <= mts - 600:
                found10 = True
            if a <= mts - 1800:
                found30 = True
        for r in load_ledger_day(day):
            if str(r.get("symbol") or "").upper() != sym:
                continue
            ts = _f(r.get("ts"))
            if ts is None:
                continue
            if ts <= mts - 600:
                found10 = True
            if ts <= mts - 1800:
                found30 = True
        if found10:
            early10 += 1
        if found30:
            early30 += 1

    n = len(arrived) or 0
    return {
        "arrived_hot_n": n,
        "discoverable_ge_10m_rate": (early10 / n) if n else None,
        "discoverable_ge_30m_rate": (early30 / n) if n else None,
        "note": (
            "Evidence = earlier same-day ledger refuse or watch_drop interval. "
            "Historical movers/trending file snapshots are not retained; "
            "this understates discoverability if panels saw the name without "
            "a ledger/watch footprint."
        ),
    }


def pool_days(day_rows: list[dict]) -> dict[str, Any]:
    def _avg(key):
        vals = [r[key] for r in day_rows if r.get(key) is not None]
        return statistics.fmean(vals) if vals else None

    return {
        "n_days": len(day_rows),
        "mistimed_first_hot_rate": _avg("mistimed_first_hot_rate"),
        "first_mistimed_rsi_med": _avg("first_mistimed_rsi_med"),
        "share_rsi_ge_52": _avg("share_rsi_ge_52"),
        "share_rsi_ge_55": _avg("share_rsi_ge_55"),
        "htl_seated_ge_2m_rate": _avg("htl_seated_ge_2m_rate"),
        "late_seat_htl_rate": _avg("late_seat_htl_rate"),
        "median_sec_kept_to_first_warm_refuse": _avg(
            "median_sec_kept_to_first_warm_refuse"),
        "median_sec_kept_to_first_mistimed": _avg(
            "median_sec_kept_to_first_mistimed"),
        "n_kept_mean": _avg("n_kept_mean"),
        "kept_ge4_share": _avg("kept_ge4_share"),
    }


def write_outputs(label: str, payload: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{label}.json"
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    with (OUT_DIR / "results.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"label": label, **payload.get("pooled", {}),
                             "ts": datetime.now(tz=ET).isoformat()},
                            default=str) + "\n")
    lines = [
        f"# Earlier book — `{label}`",
        "",
        f"Window: `{payload.get('day_from')}` → `{payload.get('day_to')}`",
        "",
        "## Pooled",
    ]
    pooled = payload.get("pooled") or {}
    for k, v in pooled.items():
        if isinstance(v, float):
            lines.append(f"- **{k}**: {v:.4f}")
        else:
            lines.append(f"- **{k}**: {v}")
    if payload.get("counterfactual"):
        lines.append("")
        lines.append("## Counterfactual soft-seed headroom")
        for k, v in (payload["counterfactual"] or {}).items():
            lines.append(f"- **{k}**: {v}")
    lines.append("")
    lines.append("Lower is better: mistimed_first_hot_rate, share_rsi_ge_52, "
                 "late_seat_htl_rate.")
    (OUT_DIR / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {path}")


def compare(a_path: Path, b_path: Path) -> int:
    a = json.loads(a_path.read_text(encoding="utf-8"))
    b = json.loads(b_path.read_text(encoding="utf-8"))
    pa, pb = a.get("pooled") or {}, b.get("pooled") or {}
    keys = [
        "mistimed_first_hot_rate", "share_rsi_ge_52", "share_rsi_ge_55",
        "late_seat_htl_rate", "htl_seated_ge_2m_rate",
        "n_kept_mean", "kept_ge4_share",
    ]
    print(f"compare  {a_path.name}  →  {b_path.name}")
    print(f"{'metric':<36} {'before':>10} {'after':>10} {'delta':>10}")
    for k in keys:
        va, vb = pa.get(k), pb.get(k)
        if va is None and vb is None:
            continue
        da = (vb - va) if (va is not None and vb is not None) else None
        print(f"{k:<36} {_fmt(va):>10} {_fmt(vb):>10} {_fmt(da):>10}")
    print("\nSuccess direction: mistimed_first_hot_rate↓  share_rsi_ge_52↓  "
          "late_seat_htl_rate↓  kept_ge4_share↑")
    print("Do not claim win from one P&L day.")
    return 0


def _fmt(v: Any) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):+.4f}"
    except (TypeError, ValueError):
        return str(v)


def main() -> int:
    ap = argparse.ArgumentParser(description="Earlier-book coverage benchmark")
    ap.add_argument("--from", dest="day_from", default="2026-09-08")
    ap.add_argument("--to", dest="day_to", default="2026-09-11")
    ap.add_argument("--label", default="baseline")
    ap.add_argument("--counterfactual-soft-seed", action="store_true")
    ap.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"))
    args = ap.parse_args()

    if args.compare:
        return compare(Path(args.compare[0]), Path(args.compare[1]))

    day_from, day_to = args.day_from, args.day_to
    print(f"earlier_book_bench  {day_from}..{day_to}  label={args.label}")
    intervals = load_drop_intervals(day_from, day_to)
    funnel = load_admit_funnel_events(day_from, day_to)
    kept_first = first_kept_ts_by_symbol(funnel)
    print(f"watch_drop symbols={len(intervals)}  admit_funnel events={len(funnel)}  "
          f"kept_index={len(kept_first)}")

    day_rows = []
    for day in _days(day_from, day_to):
        row = score_day(day, intervals, funnel, kept_first)
        day_rows.append(row)
        hot = row.get("mistimed_first_hot_rate")
        print(
            f"  {day}  mistimed={row['n_mistimed_symbols']}  "
            f"first_hot={_fmt(hot)}  "
            f"late_htl={_fmt(row.get('late_seat_htl_rate'))}  "
            f"kept_ge4={_fmt(row.get('kept_ge4_share'))}"
        )

    pooled = pool_days(day_rows)
    payload = {
        "label": args.label,
        "day_from": day_from,
        "day_to": day_to,
        "days": day_rows,
        "pooled": pooled,
        "limitations": (
            "Seating from watch_drop elapsed_sec reconstruction when "
            "admit_funnel kept_symbols absent. Timing medians require A1 "
            "kept_symbols on events."
        ),
    }
    if args.counterfactual_soft_seed:
        payload["counterfactual"] = counterfactual_soft_seed(
            day_from, day_to, intervals)
        cf = payload["counterfactual"]
        print(f"counterfactual arrived_hot={cf.get('arrived_hot_n')}  "
              f"ge10m={_fmt(cf.get('discoverable_ge_10m_rate'))}  "
              f"ge30m={_fmt(cf.get('discoverable_ge_30m_rate'))}")

    print("\n=== POOLED ===")
    for k, v in pooled.items():
        print(f"  {k}: {_fmt(v) if isinstance(v, float) else v}")
    write_outputs(args.label, payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
