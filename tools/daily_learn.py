#!/usr/bin/env python3
"""daily_learn.py — close the learn-from-yesterday loop.

Writes one dated roll-up for a session day and appends a single line to the
hybrid forward-test ledger. Designed to run from the watchdog at EOD, or by
hand after the close:

    venv/bin/python tools/daily_learn.py
    venv/bin/python tools/daily_learn.py --day 2026-08-11
    venv/bin/python tools/daily_learn.py --instrumentation
    venv/bin/python tools/daily_learn.py --json

Artifacts (under AI_REPORT_DIR / ai_reports/):
  daily/YYYY-MM-DD.md      human EOD note (includes replay tuner brief)
  daily/YYYY-MM-DD.json    machine roll-up (includes confirm_health)
  daily_ledger.jsonl       one line per day (hybrid forward-test board)
  replay_ab/YYYY-MM-DD.*   counterfactual overlay ranking (tools/replay_ab.py)

Confirm-streak health (IREN-class): confirm_ready_rate and streak1_stuck are
aggregated from shadow arm_ok + arm_recheck streak fields. No arm-gate change.
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "tools"))

from ai_paths import resolve_report_dir  # noqa: E402
from learn_stamps import regime_stamp  # noqa: E402
import confirm_health as ch  # noqa: E402

ET = ZoneInfo("America/New_York")


def _report_dir() -> Path:
    return resolve_report_dir()


def _jsonl(path: Path, day: date | None) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return rows
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
        if day is not None:
            ts = r.get("ts") or r.get("exit_time")
            try:
                d = datetime.fromtimestamp(float(ts), tz=ET).date()
            except Exception:
                continue
            if d != day:
                continue
        rows.append(r)
    return rows


def _mean(xs: list[float]) -> float | None:
    return statistics.fmean(xs) if xs else None


def outcomes_summary(rows: list[dict]) -> dict[str, Any]:
    rs = [float(r["realized_r_multiple"])
          for r in rows if r.get("realized_r_multiple") is not None]
    pls = [float(r["realized_pl_usd"])
           for r in rows if r.get("realized_pl_usd") is not None]
    wins = [x for x in rs if x > 0]
    return {
        "n": len(rows),
        "n_scored": len(rs),
        "win_rate": (len(wins) / len(rs)) if rs else None,
        "avg_r": _mean(rs),
        "sum_r": sum(rs) if rs else None,
        "sum_pl_usd": sum(pls) if pls else None,
        "by_close_reason": dict(Counter(
            str(r.get("close_reason") or "?") for r in rows)),
        "by_entry_path": dict(Counter(
            str(r.get("entry_path") or "(none)") for r in rows)),
        "by_edge_mode": dict(Counter(
            str(r.get("edge_mode") or "(none)") for r in rows)),
        "by_entry_exhaustion_state": dict(Counter(
            str(r.get("entry_exhaustion_state") or "(none)") for r in rows)),
    }


def _desk_report(day: date) -> dict[str, Any] | None:
    try:
        import desk_report as dr
        return dr.build_report(day, 30.0 * 60.0)
    except Exception as e:
        return {"error": str(e)}


def _fill_truth(day: date) -> dict[str, Any]:
    """Best-effort Alpaca fill truth for the session day; offline on failure."""
    try:
        import fill_truth_report as ft
        from desk_core import load_desk_env
        load_desk_env()
        fills = ft.fetch_alpaca_fills(days=5, limit=300)
        ai_syms = ft._ai_symbols()
        eng_syms = ft._engine_symbols()
        fills = ft.tag_fills(fills, ai_syms, eng_syms)
        closed = ft._pair_round_trips(fills)
        day_closed = ft.closed_on_day(closed, day)
        return {
            "ok": True,
            "stats": ft._stats(day_closed),
            "by_source": {
                s: ft._stats([t for t in day_closed if t.get("source") == s])
                for s in ("ai", "engine", "manual")
            },
            "n_closed": len(day_closed),
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "offline": True}


def _replay_tune(day: date) -> dict[str, Any]:
    """Pack today's tape and score registered overlays. Never raises."""
    try:
        import desk_tape
        import replay_ab
        desk_tape.pack([day.isoformat()])
        payload = replay_ab.run_days([day.isoformat()], write=True)
        return replay_ab.brief_for_daily(payload)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def build_payload(day: date) -> dict[str, Any]:
    rd = _report_dir()
    outcomes = _jsonl(rd / "outcomes.jsonl", day)
    trades = _jsonl(rd / "trades.jsonl", day)
    stamp = regime_stamp()
    desk = _desk_report(day)
    fills = _fill_truth(day)
    osum = outcomes_summary(outcomes)
    funnel = (desk or {}).get("funnel") if isinstance(desk, dict) else None
    replay = _replay_tune(day)
    try:
        confirm = ch.build_for_day(
            day,
            shadow_path=rd / "shadow.jsonl",
            events_path=rd / "events.jsonl",
            ledger_path=rd / "daily_ledger.jsonl",
        )
    except Exception as e:  # noqa: BLE001
        confirm = {"ok": False, "error": str(e), "n_streak": 0, "n_ready": 0,
                   "n_stuck": 0, "confirm_ready_rate": None, "need": None,
                   "warn": False, "streak1_stuck": [], "symbols": []}
    best = (replay or {}).get("best_candidate") or (replay or {}).get("best_hypothesis") or {}
    ledger = {
        "day": day.isoformat(),
        "ts": datetime.now(timezone.utc).timestamp(),
        "edge_mode": stamp.get("edge_mode"),
        "exit_left_overbought": stamp.get("exit_left_overbought"),
        "git_version": stamp.get("git_version"),
        "config_fp": stamp.get("config_fp"),
        "paper": stamp.get("paper"),
        "n_outcomes": osum["n"],
        "n_scored": osum["n_scored"],
        "win_rate": osum["win_rate"],
        "avg_r": osum["avg_r"],
        "sum_r": osum["sum_r"],
        "sum_pl_usd": osum["sum_pl_usd"],
        "by_close_reason": osum["by_close_reason"],
        "filled": (funnel or {}).get("filled"),
        "armed": (funnel or {}).get("armed"),
        "admitted": (funnel or {}).get("admitted"),
        "fill_truth_ok": bool(fills.get("ok")),
        "fill_truth_trades": (fills.get("stats") or {}).get("trades"),
        "fill_truth_avg_pnl_pct": (fills.get("stats") or {}).get("avg_pnl_pct"),
        "replay_ok": bool((replay or {}).get("ok")),
        "replay_best": best.get("name"),
        "replay_best_delta_usd": best.get("delta_usd"),
        "replay_verdict": best.get("verdict"),
        **ch.ledger_fields(confirm),
    }
    return {
        "day": day.isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "regime": stamp,
        "outcomes": osum,
        "n_trades_log": len(trades),
        "desk": desk,
        "fill_truth": fills,
        "replay": replay,
        "confirm_health": confirm,
        "ledger": ledger,
    }


def _md(payload: dict[str, Any]) -> str:
    day = payload["day"]
    reg = payload.get("regime") or {}
    o = payload.get("outcomes") or {}
    funnel = ((payload.get("desk") or {}) if isinstance(payload.get("desk"), dict)
              else {}).get("funnel") or {}
    lines = [
        f"# Daily learn — {day}",
        "",
        f"Generated: {payload.get('generated_at')}",
        "",
        "## Regime (live config at report time)",
        f"- edge_mode: `{reg.get('edge_mode')}`",
        f"- desk_product: `{reg.get('desk_product')}`",
        f"- exit_left_overbought: `{reg.get('exit_left_overbought')}`",
        f"- git: `{reg.get('git_version')}` config_fp: `{reg.get('config_fp')}`",
        f"- paper: `{reg.get('paper')}` book_owner: `{reg.get('book_owner')}`",
        "",
        "## Outcomes",
        f"- n={o.get('n')} scored={o.get('n_scored')} "
        f"win_rate={o.get('win_rate')} avg_r={o.get('avg_r')} "
        f"sum_r={o.get('sum_r')} sum_pl_usd={o.get('sum_pl_usd')}",
        f"- close_reason: {o.get('by_close_reason')}",
        f"- entry_path: {o.get('by_entry_path')}",
        f"- edge_mode on rows: {o.get('by_edge_mode')}",
        f"- entry_exhaustion_state: {o.get('by_entry_exhaustion_state')}",
        "",
        "## Funnel",
        f"- admitted={funnel.get('admitted')} armed={funnel.get('armed')} "
        f"filled={funnel.get('filled')} closed={funnel.get('closed_with_outcome')}",
        "",
        "## Fill truth",
    ]
    ft = payload.get("fill_truth") or {}
    if ft.get("ok"):
        st = ft.get("stats") or {}
        lines.append(
            f"- ok trades={st.get('trades')} win={st.get('win_rate')}% "
            f"avg_pnl_pct={st.get('avg_pnl_pct')} total={st.get('total_pnl_pct')}"
        )
    else:
        lines.append(f"- unavailable: {ft.get('error') or 'offline'}")
    replay = payload.get("replay") or {}
    lines.extend(["", "## Replay tuner"])
    if replay.get("ok") and not replay.get("skipped"):
        bc, bh = replay.get("best_candidate"), replay.get("best_hypothesis")
        lines.append(f"- {replay.get('action')}")
        if bc:
            lines.append(f"- candidate: `{bc.get('name')}` Δ${bc.get('delta_usd')}")
        if bh:
            lines.append(f"- hypothesis: `{bh.get('name')}` Δ${bh.get('delta_usd')}")
        if not bc:
            lines.append("- no overlay cleared min_n + both halves — do not change config")
    elif replay.get("skipped"):
        lines.append(f"- skipped: {replay.get('skipped')}")
    else:
        lines.append(f"- unavailable: {replay.get('error') or 'not run'}")
    lines.extend(["", "## Confirm streak health"])
    chs = payload.get("confirm_health") or {}
    if chs.get("error"):
        lines.append(f"- unavailable: {chs.get('error')}")
    elif not chs or ((chs.get("n_streak") or 0) == 0 and (chs.get("n_arm_ok_syms") or 0) == 0):
        lines.append("- no arm_ok / streak rows")
    else:
        n_ready, n_streak = chs.get("n_ready"), chs.get("n_streak")
        rate = chs.get("confirm_ready_rate")
        pct = f"{100.0 * rate:.0f}%" if isinstance(rate, (int, float)) else "n/a"
        lines.append(
            f"- confirm_ready_rate: {n_ready}/{n_streak} = {pct} "
            f"(need={chs.get('need')})"
        )
        stuck = chs.get("streak1_stuck") or []
        if stuck:
            names = ", ".join(
                f"{r.get('symbol')}(arm_ok={r.get('n_arm_ok')}, "
                f"max={r.get('max_streak')})"
                for r in stuck[:12]
            )
            lines.append(f"- streak1_stuck: {len(stuck)}  {names}")
        else:
            lines.append("- streak1_stuck: 0")
        if chs.get("warn"):
            lines.append(f"- WARN confirm_health_warn: {chs.get('warn_reason')}")
        if chs.get("prior_day"):
            lines.append(
                f"- prior {chs.get('prior_day')}: "
                f"rate={chs.get('prior_rate')} stuck={chs.get('prior_stuck')}"
            )
    lines.extend([
        "",
        "## How to re-run",
        "```",
        f"venv/bin/python tools/daily_learn.py --day {day}",
        f"venv/bin/python tools/confirm_health.py --day {day}",
        f"venv/bin/python tools/desk_tape.py pack --day {day}",
        f"venv/bin/python tools/replay_ab.py --day {day}",
        f"venv/bin/python tools/replay_ab.py --search --days 10",
        f"venv/bin/python tools/desk_report.py --day {day}",
        f"venv/bin/python tools/outcome_slice.py  # filter by day in post",
        "```",
        "",
        "One day is a check, not a trend. Hybrid edge is a forward-test.",
        "",
    ])
    return "\n".join(lines)


def append_ledger(ledger: dict[str, Any], path: Path) -> None:
    """Replace same-day line if present, else append (idempotent re-runs)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    day = ledger.get("day")
    kept: list[str] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                kept.append(line)
                continue
            if r.get("day") == day:
                continue
            kept.append(json.dumps(r, default=str))
    kept.append(json.dumps(ledger, default=str))
    path.write_text("\n".join(kept) + "\n", encoding="utf-8")


def write_artifacts(payload: dict[str, Any]) -> dict[str, str]:
    rd = _report_dir()
    day = payload["day"]
    daily_dir = rd / "daily"
    daily_dir.mkdir(parents=True, exist_ok=True)
    md_path = daily_dir / f"{day}.md"
    json_path = daily_dir / f"{day}.json"
    ledger_path = rd / "daily_ledger.jsonl"
    md_path.write_text(_md(payload), encoding="utf-8")
    json_path.write_text(
        json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    append_ledger(payload["ledger"], ledger_path)
    try:
        ch.emit_summary_events(payload.get("confirm_health") or {})
    except Exception:
        pass
    return {
        "md": str(md_path),
        "json": str(json_path),
        "ledger": str(ledger_path),
    }


def run_instrumentation() -> int:
    """Delegate to instrumentation_check; preserve exit code for watchdog."""
    py = sys.executable
    script = _ROOT / "tools" / "instrumentation_check.py"
    return subprocess.call([py, str(script)], cwd=str(_ROOT))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--day", help="YYYY-MM-DD (default: today ET)")
    ap.add_argument("--instrumentation", action="store_true",
                    help="run tools/instrumentation_check.py and exit")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-write", action="store_true",
                    help="build payload only; do not write artifacts")
    args = ap.parse_args()

    if args.instrumentation:
        return run_instrumentation()

    if args.day:
        day = date.fromisoformat(args.day)
    else:
        day = datetime.now(tz=ET).date()

    payload = build_payload(day)
    paths = {} if args.no_write else write_artifacts(payload)

    if args.json:
        out = dict(payload)
        out["paths"] = paths
        print(json.dumps(out, indent=2, default=str))
    else:
        print(_md(payload))
        if paths:
            print("Wrote:")
            for k, v in paths.items():
                print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
