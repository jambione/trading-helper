#!/usr/bin/env python3
"""confirm_health.py — daily confirm-streak health (IREN-class detector).

Does not change arm behavior. Aggregates existing shadow.jsonl arm_ok rows
and events.jsonl arm_recheck streak fields into:

    confirm_ready_rate = (# symbols with max_streak >= need)
                       / (# symbols with max_streak >= 1)

and flags streak1_stuck symbols: many arm_ok (or confirm ticks) but max
streak never reached need — the 2026-09-03 IREN failure mode, where the
2s book rebuild dropped arm_streak so every YES logged streak=1.

    venv/bin/python tools/confirm_health.py
    venv/bin/python tools/confirm_health.py --day 2026-09-03
    venv/bin/python tools/confirm_health.py --day 2026-09-03 --split-at 2026-09-03T12:36:39-04:00
    venv/bin/python tools/confirm_health.py --json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "tools"))

from ai_paths import find_report_file, resolve_report_dir  # noqa: E402

ET = ZoneInfo("America/New_York")

# "Many" arm_ok without ever clearing confirm. IREN logged 21 YESes at streak=1.
STUCK_MIN_ARM_OK = 5
# Rate collapse vs prior day (absolute drop).
RATE_DROP_WARN = 0.40
# Absolute floor when the sample is big enough to mean something.
RATE_FLOOR_WARN = 0.35
RATE_FLOOR_MIN_N = 5
# Stuck-count spike vs prior, and an absolute tripwire.
STUCK_SPIKE_MIN = 3
STUCK_SPIKE_DELTA = 2
STUCK_ABS_WARN = 5
MIN_N_FOR_WARN = 3


def _report(name: str) -> Path:
    return find_report_file(name) or (resolve_report_dir() / name)


def _et_date(ts: Any) -> date | None:
    try:
        return datetime.fromtimestamp(float(ts), tz=ET).date()
    except Exception:
        return None


def _sym(row: dict) -> str:
    return str(row.get("symbol") or "").upper().strip()


def _truthy_arm_ok(row: dict) -> bool:
    v = row.get("arm_ok")
    return v is True or v == 1 or v == "true"


def _int_or_none(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _need_from_cfg() -> int:
    try:
        from config import load_config
        return max(1, int(load_config().get("ai_watch_arm_confirm_ticks") or 1))
    except Exception:
        return 1


def accumulate(
    shadow_rows: Iterable[dict],
    event_rows: Iterable[dict],
) -> dict[str, dict[str, Any]]:
    """Per-symbol max_streak / arm_ok counts. Pure; no I/O, no day filter."""
    stats: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "n_arm_ok": 0,
        "max_streak": 0,
        "n_confirm": 0,
        "n_pass": 0,
        "need_seen": [],
    })
    for r in shadow_rows:
        if not isinstance(r, dict):
            continue
        if not _truthy_arm_ok(r):
            continue
        s = _sym(r)
        if not s:
            continue
        stats[s]["n_arm_ok"] += 1
    for r in event_rows:
        if not isinstance(r, dict):
            continue
        if str(r.get("kind") or "") != "arm_recheck":
            continue
        s = _sym(r)
        if not s:
            continue
        stage = str(r.get("stage") or "")
        streak = _int_or_none(r.get("streak"))
        need = _int_or_none(r.get("need"))
        if need is not None:
            stats[s]["need_seen"].append(need)
        if streak is not None:
            if streak > stats[s]["max_streak"]:
                stats[s]["max_streak"] = streak
            if stage == "pass" or (need is not None and streak >= need):
                stats[s]["n_pass"] += 1
            else:
                # confirm stage, or a streak-bearing row that has not cleared.
                stats[s]["n_confirm"] += 1
        elif stage == "confirm":
            stats[s]["n_confirm"] += 1
        elif stage == "pass":
            stats[s]["n_pass"] += 1
    return {k: dict(v) for k, v in stats.items()}


def _stuck(row: dict, need: int, stuck_min: int) -> bool:
    max_s = int(row.get("max_streak") or 0)
    n_ok = int(row.get("n_arm_ok") or 0)
    n_conf = int(row.get("n_confirm") or 0)
    if max_s <= 0 or max_s >= need:
        return False
    return (n_ok >= stuck_min) or (n_conf >= stuck_min)


def _warn_reason(
    *,
    rate: float | None,
    n_streak: int,
    n_stuck: int,
    stuck_names: list[str],
    prior: dict | None,
) -> str | None:
    if n_streak < MIN_N_FOR_WARN and n_stuck < STUCK_SPIKE_MIN:
        return None
    reasons: list[str] = []
    prior = prior or {}
    prior_rate = prior.get("confirm_ready_rate")
    try:
        prior_rate_f = float(prior_rate) if prior_rate is not None else None
    except (TypeError, ValueError):
        prior_rate_f = None
    prior_stuck = prior.get("n_stuck")
    try:
        prior_stuck_n = int(prior_stuck) if prior_stuck is not None else 0
    except (TypeError, ValueError):
        prior_stuck_n = 0

    if (
        rate is not None
        and prior_rate_f is not None
        and n_streak >= MIN_N_FOR_WARN
        and (prior_rate_f - rate) >= RATE_DROP_WARN - 1e-12
    ):
        reasons.append(
            f"rate collapsed {prior_rate_f:.0%} → {rate:.0%} (n={n_streak})"
        )
    if (
        rate is not None
        and n_streak >= RATE_FLOOR_MIN_N
        and rate < RATE_FLOOR_WARN
        and (prior_rate_f is None or prior_rate_f >= 0.50)
    ):
        reasons.append(f"rate {rate:.0%} below floor {RATE_FLOOR_WARN:.0%} (n={n_streak})")
    if n_stuck >= STUCK_ABS_WARN:
        names = ",".join(stuck_names[:8])
        reasons.append(f"streak1_stuck={n_stuck} [{names}]")
    elif n_stuck >= STUCK_SPIKE_MIN and n_stuck >= prior_stuck_n + STUCK_SPIKE_DELTA:
        names = ",".join(stuck_names[:8])
        reasons.append(f"streak1_stuck spiked {prior_stuck_n} → {n_stuck} [{names}]")
    return "; ".join(reasons) if reasons else None


def summarize(
    per_sym: dict[str, dict[str, Any]],
    *,
    need: int = 2,
    stuck_min: int = STUCK_MIN_ARM_OK,
    prior: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Pure roll-up. *need* is ai_watch_arm_confirm_ticks (usually 2)."""
    need = max(1, int(need or 1))
    symbols: list[dict[str, Any]] = []
    for sym, row in per_sym.items():
        max_s = int(row.get("max_streak") or 0)
        n_ok = int(row.get("n_arm_ok") or 0)
        if max_s < 1 and n_ok < 1:
            continue
        symbols.append({
            "symbol": sym,
            "max_streak": max_s,
            "n_arm_ok": n_ok,
            "n_confirm": int(row.get("n_confirm") or 0),
            "n_pass": int(row.get("n_pass") or 0),
        })
    symbols.sort(key=lambda r: (-r["max_streak"], -r["n_arm_ok"], r["symbol"]))

    n_streak = sum(1 for r in symbols if r["max_streak"] >= 1)
    n_ready = sum(1 for r in symbols if r["max_streak"] >= need)
    rate = (n_ready / n_streak) if n_streak else None
    stuck = [r for r in symbols if _stuck(
        {"max_streak": r["max_streak"], "n_arm_ok": r["n_arm_ok"],
         "n_confirm": r["n_confirm"]},
        need, stuck_min,
    )]
    stuck_names = [r["symbol"] for r in stuck]
    warn_reason = _warn_reason(
        rate=rate, n_streak=n_streak, n_stuck=len(stuck),
        stuck_names=stuck_names, prior=prior,
    )
    return {
        "need": need,
        "stuck_min_arm_ok": stuck_min,
        "n_arm_ok_syms": sum(1 for r in symbols if r["n_arm_ok"] >= 1),
        "n_streak": n_streak,
        "n_ready": n_ready,
        "confirm_ready_rate": rate,
        "n_stuck": len(stuck),
        "streak1_stuck": stuck,
        "symbols": symbols,
        "warn": bool(warn_reason),
        "warn_reason": warn_reason,
    }


def resolve_need(per_sym: dict[str, dict[str, Any]], fallback: int | None = None) -> int:
    """Prefer the need stamped on arm_recheck events; else live config."""
    counts: Counter[int] = Counter()
    for row in per_sym.values():
        for n in row.get("need_seen") or []:
            try:
                counts[int(n)] += 1
            except (TypeError, ValueError):
                continue
    if counts:
        return max(1, counts.most_common(1)[0][0])
    if fallback is not None:
        return max(1, int(fallback))
    return _need_from_cfg()


def iter_jsonl_day(
    path: Path,
    day: date,
    *,
    ts_min: float | None = None,
    ts_max: float | None = None,
) -> Iterable[dict]:
    """Stream dict rows for an ET calendar day. Missing file → empty."""
    try:
        fh = path.open(encoding="utf-8")
    except OSError:
        return
    try:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if not isinstance(r, dict):
                continue
            ts = r.get("ts")
            if ts_min is not None or ts_max is not None:
                try:
                    tsf = float(ts)
                except (TypeError, ValueError):
                    continue
                if ts_min is not None and tsf < ts_min:
                    continue
                if ts_max is not None and tsf >= ts_max:
                    continue
            d = _et_date(ts)
            if d != day:
                continue
            yield r
    finally:
        try:
            fh.close()
        except Exception:
            pass


def collect_day(
    day: date,
    *,
    shadow_path: Path | None = None,
    events_path: Path | None = None,
    ts_min: float | None = None,
    ts_max: float | None = None,
) -> dict[str, dict[str, Any]]:
    shadow = shadow_path or _report("shadow.jsonl")
    events = events_path or _report("events.jsonl")
    return accumulate(
        iter_jsonl_day(shadow, day, ts_min=ts_min, ts_max=ts_max),
        iter_jsonl_day(events, day, ts_min=ts_min, ts_max=ts_max),
    )


def prior_from_ledger(path: Path, day: date) -> dict[str, Any] | None:
    """Most recent ledger line before *day* that carries confirm fields."""
    if not path.exists():
        return None
    best: dict[str, Any] | None = None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if not isinstance(r, dict):
            continue
        d = str(r.get("day") or "")
        if not d or d >= day.isoformat():
            continue
        if r.get("confirm_ready_rate") is None and r.get("confirm_n_streak") is None:
            continue
        if best is None or d > str(best.get("day") or ""):
            best = {
                "day": d,
                "confirm_ready_rate": r.get("confirm_ready_rate"),
                "n_streak": r.get("confirm_n_streak"),
                "n_stuck": r.get("confirm_stuck_n"),
            }
    return best


def build_for_day(
    day: date,
    *,
    shadow_path: Path | None = None,
    events_path: Path | None = None,
    ledger_path: Path | None = None,
    need: int | None = None,
    stuck_min: int = STUCK_MIN_ARM_OK,
    ts_min: float | None = None,
    ts_max: float | None = None,
) -> dict[str, Any]:
    per_sym = collect_day(
        day, shadow_path=shadow_path, events_path=events_path,
        ts_min=ts_min, ts_max=ts_max)
    resolved_need = resolve_need(per_sym, fallback=need)
    prior = prior_from_ledger(
        ledger_path or (resolve_report_dir() / "daily_ledger.jsonl"), day)
    out = summarize(per_sym, need=resolved_need, stuck_min=stuck_min, prior=prior)
    out["day"] = day.isoformat()
    if prior:
        out["prior_day"] = prior.get("day")
        out["prior_rate"] = prior.get("confirm_ready_rate")
        out["prior_stuck"] = prior.get("n_stuck")
    return out


def one_liner(summary: dict[str, Any]) -> str:
    n_ready = summary.get("n_ready")
    n_streak = summary.get("n_streak")
    rate = summary.get("confirm_ready_rate")
    need = summary.get("need")
    n_stuck = summary.get("n_stuck") or 0
    pct = f"{100.0 * rate:.0f}%" if isinstance(rate, (int, float)) else "n/a"
    stuck = summary.get("streak1_stuck") or []
    names = ",".join(r.get("symbol") or "?" for r in stuck[:6])
    extra = f" stuck={n_stuck}[{names}]" if n_stuck else " stuck=0"
    return f"{n_ready}/{n_streak} ready ({pct}) need={need}{extra}"


def ledger_fields(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "confirm_ready_rate": summary.get("confirm_ready_rate"),
        "confirm_n_ready": summary.get("n_ready"),
        "confirm_n_streak": summary.get("n_streak"),
        "confirm_stuck_n": summary.get("n_stuck"),
        "confirm_need": summary.get("need"),
        "confirm_warn": bool(summary.get("warn")),
    }


def emit_summary_events(summary: dict[str, Any], *, warn_only: bool = False) -> None:
    """Append confirm_health / confirm_health_warn. Never raises. No knob changes."""
    path = resolve_report_dir() / "events.jsonl"
    stuck = summary.get("streak1_stuck") or []
    fields = {
        "day": summary.get("day"),
        "need": summary.get("need"),
        "n_ready": summary.get("n_ready"),
        "n_streak": summary.get("n_streak"),
        "confirm_ready_rate": summary.get("confirm_ready_rate"),
        "n_stuck": summary.get("n_stuck"),
        "stuck": [r.get("symbol") for r in stuck[:16]],
        "warn_reason": summary.get("warn_reason"),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = time.time()
        if not warn_only:
            row = {"ts": ts, "kind": "confirm_health", **{
                k: v for k, v in fields.items() if v is not None}}
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, default=str) + "\n")
        if summary.get("warn"):
            row = {"ts": ts, "kind": "confirm_health_warn", **{
                k: v for k, v in fields.items() if v is not None}}
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, default=str) + "\n")
    except Exception:
        pass


def _parse_split(raw: str | None) -> float | None:
    if not raw:
        return None
    raw = raw.strip()
    try:
        return float(raw)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as e:
        raise SystemExit(f"bad --split-at {raw!r}: {e}") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ET)
    return dt.timestamp()


def _print_human(summary: dict[str, Any], label: str = "") -> None:
    head = f"CONFIRM STREAK HEALTH {label}".strip()
    print(head)
    print(f"  {one_liner(summary)}")
    if summary.get("warn"):
        print(f"  WARN: {summary.get('warn_reason')}")
    stuck = summary.get("streak1_stuck") or []
    if stuck:
        print("  stuck symbols:")
        for r in stuck:
            print(
                f"    {r['symbol']:<8} arm_ok={r['n_arm_ok']:<4} "
                f"confirm={r['n_confirm']:<4} pass={r['n_pass']:<3} "
                f"max_streak={r['max_streak']}"
            )
    elif (summary.get("n_streak") or 0) == 0:
        print("  (no arm_ok / streak rows)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--day", help="YYYY-MM-DD (default: today ET)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--split-at",
                    help="ISO ts or unix; print pre/post windows (no write)")
    ap.add_argument("--need", type=int, default=None,
                    help="override ai_watch_arm_confirm_ticks")
    ap.add_argument("--stuck-min", type=int, default=STUCK_MIN_ARM_OK)
    ap.add_argument("--emit", action="store_true",
                    help="append confirm_health / confirm_health_warn events")
    args = ap.parse_args()

    if args.day:
        day = date.fromisoformat(args.day)
    else:
        day = datetime.now(tz=ET).date()

    split_ts = _parse_split(args.split_at)
    common = dict(need=args.need, stuck_min=args.stuck_min)

    if split_ts is not None:
        pre = build_for_day(day, ts_max=split_ts, **common)
        post = build_for_day(day, ts_min=split_ts, **common)
        whole = build_for_day(day, **common)
        payload = {
            "day": day.isoformat(),
            "split_at": args.split_at,
            "split_ts": split_ts,
            "pre": pre,
            "post": post,
            "day_total": whole,
        }
        if args.json:
            print(json.dumps(payload, indent=2, default=str))
        else:
            split_et = datetime.fromtimestamp(split_ts, tz=ET).isoformat()
            print(f"day={day} split_at={split_et}")
            _print_human(pre, "pre")
            _print_human(post, "post")
            _print_human(whole, "day")
        return 0

    summary = build_for_day(day, **common)
    if args.emit:
        emit_summary_events(summary)
    if args.json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        _print_human(summary, day.isoformat())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
