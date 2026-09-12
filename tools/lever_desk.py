#!/usr/bin/env python3
"""Lever Desk — score one active product lever after close.

Read-only on the trading path. Never writes ``config/bot_config.json``,
never arms, never fetches bars. Uses outcome-row fields only for capture.

    .venv/bin/python tools/lever_desk.py
    .venv/bin/python tools/lever_desk.py --days 10 --json
    .venv/bin/python tools/lever_desk.py --classify "widen the give"
    .venv/bin/python tools/lever_desk.py --record KEEP --note "..."
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import capital_auditor as ca  # noqa: E402
import eod  # noqa: E402
from ai_paths import resolve_report_dir  # noqa: E402

REGISTRY_PATH = ROOT / "config" / "lever_desk.json"
VALID_VERDICTS = frozenset({"KEEP", "KILL", "MEASURE"})
SCORE_KINDS = frozenset({"hold_capture", "session_r"})


def _desk_dir(report_dir: Path | None = None) -> Path:
    base = Path(report_dir) if report_dir is not None else resolve_report_dir()
    out = base / "lever_desk"
    out.mkdir(parents=True, exist_ok=True)
    return out


def load_registry(path: Path | None = None) -> dict[str, Any]:
    p = Path(path) if path is not None else REGISTRY_PATH
    if not p.exists():
        return {"version": 1, "levers": []}
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {"version": 1, "levers": []}
    levers = data.get("levers")
    if not isinstance(levers, list):
        data["levers"] = []
    return data


def load_state(report_dir: Path | None = None) -> dict[str, Any]:
    path = _desk_dir(report_dir) / "state.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: dict[str, Any], report_dir: Path | None = None) -> None:
    path = _desk_dir(report_dir) / "state.json"
    payload = dict(state)
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def load_status(report_dir: Path | None = None) -> dict[str, str]:
    path = _desk_dir(report_dir) / "status.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def save_status(status: dict[str, str], report_dir: Path | None = None) -> None:
    path = _desk_dir(report_dir) / "status.json"
    path.write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")


def active_lever(registry: dict[str, Any], state: dict[str, Any]) -> dict[str, Any] | None:
    levers = [L for L in (registry.get("levers") or []) if isinstance(L, dict) and L.get("id")]
    if not levers:
        return None
    aid = (state or {}).get("active_id")
    if aid:
        for L in levers:
            if L.get("id") == aid:
                return L
    live = [L for L in levers if str(L.get("status") or "") == "live"]
    if live:
        return live[0]
    return levers[0]


def hold_capture(trades: list[dict], *, min_mfe_r: float = 0.25) -> dict[str, Any]:
    """Hold-period capture from outcome rows only — no bar fetch."""
    reasons: Counter[str] = Counter()
    caps: list[float] = []
    mfes_qual: list[float] = []
    all_mfes: list[float] = []
    realized: list[float] = []
    n_local = 0
    give_ok = 0
    give_n = 0
    n_qualifying = 0
    n_mfe_missing = 0

    for d in trades:
        reason = str(d.get("close_reason") or "unknown")
        reasons[reason] += 1
        r = d.get("realized_r_multiple")
        mfe = d.get("mfe_r")
        try:
            rf = float(r) if r is not None else None
        except (TypeError, ValueError):
            rf = None
        try:
            mf = float(mfe) if mfe is not None else None
        except (TypeError, ValueError):
            mf = None

        if rf is not None:
            realized.append(rf)
        if mf is None:
            n_mfe_missing += 1
        else:
            all_mfes.append(mf)
            if mf >= float(min_mfe_r):
                n_qualifying += 1
                if mf > 0.01 and rf is not None:
                    caps.append(rf / mf)
                    mfes_qual.append(mf)

        if reason == "local_trail":
            n_local += 1
            give = d.get("give_r_at_exit")
            try:
                gf = float(give) if give is not None else None
            except (TypeError, ValueError):
                gf = None
            if gf is not None and mf is not None:
                give_n += 1
                if gf < mf:
                    give_ok += 1

    return {
        "n": len(trades),
        "n_qualifying": n_qualifying,
        "n_mfe_missing": n_mfe_missing,
        "min_mfe_r": float(min_mfe_r),
        "max_mfe_r": (max(all_mfes) if all_mfes else None),
        "median_capture": (statistics.median(caps) if caps else None),
        "median_mfe_r": (statistics.median(mfes_qual) if mfes_qual else None),
        "median_realized_r": (statistics.median(realized) if realized else None),
        "n_local_trail": n_local,
        "give_lt_mfe_frac": (give_ok / give_n) if give_n else None,
        "close_reasons": dict(reasons.most_common()),
    }


def _window_days(lever: dict[str, Any], all_days: list[str]) -> list[str]:
    shipped = str(lever.get("shipped_at") or "")
    if not shipped:
        return list(all_days)
    return [d for d in all_days if d >= shipped]


def _trades_for_days(by_day: dict[str, list], days: list[str]) -> list[dict]:
    out: list[dict] = []
    for d in days:
        out.extend(by_day.get(d) or [])
    return out


def _hold_capture_verdict(
    current: dict[str, Any],
    score_cfg: dict[str, Any],
    *,
    gap: str | None,
) -> tuple[str, str]:
    if gap:
        return "MEASURE", f"gap — {gap}"
    min_n = int(score_cfg.get("min_n") or 5)
    min_mfe = float(current.get("min_mfe_r") or score_cfg.get("min_mfe_r") or 0.25)
    n = int(current.get("n") or 0)
    nq = int(current.get("n_qualifying") or 0)
    if nq < min_n:
        max_mfe = current.get("max_mfe_r")
        max_s = f"{float(max_mfe):.2f}R" if max_mfe is not None else "—"
        return (
            "MEASURE",
            f"{nq}/{n} fills with MFE≥{min_mfe:g}R (need {min_n}; max MFE {max_s})",
        )
    med = current.get("median_capture")
    if med is None:
        return "MEASURE", "no capture sample"
    kill = (score_cfg.get("kill") or {})
    pas = (score_cfg.get("pass") or {})
    kill_lt = kill.get("median_capture_lt")
    pass_gte = pas.get("median_capture_gte")
    try:
        if kill_lt is not None and float(med) < float(kill_lt):
            return "KILL", f"median_capture {med:.3f} < kill {float(kill_lt):.3f}"
    except (TypeError, ValueError):
        pass
    try:
        if pass_gte is not None and float(med) >= float(pass_gte):
            return "KEEP", f"median_capture {med:.3f} >= pass {float(pass_gte):.3f}"
    except (TypeError, ValueError):
        pass
    return "MEASURE", "between pass and kill thresholds"


def _session_r_stats(by_day: dict[str, list], days: list[str], trig: dict | None = None) -> dict[str, Any]:
    trig = trig or {}
    live_rs: list[float] = []
    n = 0
    for d in days:
        trades = by_day.get(d) or []
        n += len(trades)
        scored = eod.score_session(trades, trig)
        live_rs.append(float(scored.get("live_r") or 0.0))
    return {
        "n": n,
        "n_sessions": len(days),
        "median_live_r": (statistics.median(live_rs) if live_rs else None),
        "sum_live_r": (sum(live_rs) if live_rs else None),
        "session_live_r": {d: live_rs[i] for i, d in enumerate(days)} if days else {},
    }


def _session_r_verdict(
    current: dict[str, Any],
    score_cfg: dict[str, Any],
    *,
    gap: str | None,
) -> tuple[str, str]:
    if gap:
        return "MEASURE", f"gap — {gap}"
    min_n = int(score_cfg.get("min_n") or 5)
    n = int(current.get("n") or 0)
    n_sessions = int(current.get("n_sessions") or 0)
    if n < min_n:
        return (
            "MEASURE",
            f"{n} fills across {n_sessions} sessions (need {min_n} fills)",
        )
    med = current.get("median_live_r")
    if med is None:
        return "MEASURE", "no session_r sample"
    kill = (score_cfg.get("kill") or {})
    pas = (score_cfg.get("pass") or {})
    kill_lt = kill.get("median_live_r_lt")
    pass_gte = pas.get("median_live_r_gte")
    if kill_lt is None and pass_gte is None:
        return "MEASURE", "session_r thresholds not set"
    try:
        if kill_lt is not None and float(med) < float(kill_lt):
            return "KILL", f"median_live_r {med:.3f} < kill {float(kill_lt):.3f}"
    except (TypeError, ValueError):
        pass
    try:
        if pass_gte is not None and float(med) >= float(pass_gte):
            return "KEEP", f"median_live_r {med:.3f} >= pass {float(pass_gte):.3f}"
    except (TypeError, ValueError):
        pass
    return "MEASURE", "between pass and kill thresholds"


def score_lever(
    lever: dict[str, Any] | None,
    by_day: dict[str, list],
    *,
    days_back: int = 10,
    gap: str | None = None,
    trig: dict | None = None,
) -> dict[str, Any]:
    if not lever:
        return {
            "kind": None,
            "window_days": [],
            "baseline_days": [],
            "current": None,
            "baseline": None,
            "verdict": "MEASURE",
            "reason": "no active lever",
        }
    score_cfg = dict(lever.get("score") or {})
    kind = str(score_cfg.get("kind") or "")
    all_days = sorted(by_day.keys())[-days_back:]
    window = _window_days(lever, all_days)
    baseline_days = [d for d in (lever.get("baseline_sessions") or []) if d in by_day]

    if kind not in SCORE_KINDS:
        return {
            "kind": kind or None,
            "window_days": window,
            "baseline_days": baseline_days,
            "current": None,
            "baseline": None,
            "verdict": "MEASURE",
            "reason": f"unknown score.kind {kind!r}",
        }

    if kind == "hold_capture":
        min_mfe = float(score_cfg.get("min_mfe_r") or 0.25)
        current = hold_capture(_trades_for_days(by_day, window), min_mfe_r=min_mfe)
        baseline = (
            hold_capture(_trades_for_days(by_day, baseline_days), min_mfe_r=min_mfe)
            if baseline_days else None
        )
        verdict, reason = _hold_capture_verdict(current, score_cfg, gap=gap)
    else:
        current = _session_r_stats(by_day, window, trig)
        baseline = _session_r_stats(by_day, baseline_days, trig) if baseline_days else None
        verdict, reason = _session_r_verdict(current, score_cfg, gap=gap)

    return {
        "kind": kind,
        "window_days": window,
        "baseline_days": baseline_days,
        "current": current,
        "baseline": baseline,
        "verdict": verdict,
        "reason": reason,
    }


def _load_cfg(cfg: dict | None) -> dict:
    if cfg is not None:
        return cfg
    try:
        from config import load_config
        return load_config()
    except Exception:
        return {}


def _exit_trig(report_dir: Path | None, repo: Path | None, days: list[str]) -> dict:
    """local_trail print stamps for live-equivalent unpaid cost (same as auditor)."""
    trig: dict = {}
    old_events = eod.EVENTS
    try:
        for root in ca.report_roots(report_dir, repo):
            ev = root / "events.jsonl"
            if not ev.exists():
                continue
            eod.EVENTS = str(ev)
            for k, v in eod.exit_slip_by_trade(set(days) if days else set()).items():
                trig.setdefault(k, []).extend(v)
    finally:
        eod.EVENTS = old_events
    return trig


def snapshot(
    *,
    days_back: int = 10,
    report_dir: Path | None = None,
    repo: Path | None = None,
    cfg: dict | None = None,
) -> dict[str, Any]:
    report_dir = report_dir or resolve_report_dir()
    repo = repo or ROOT
    cfg = _load_cfg(cfg)

    audit = ca.audit(days_back=days_back, report_dir=report_dir, cfg=cfg, repo=repo)
    corpus = audit.get("corpus") or {}
    totals = corpus.get("totals") or {}
    outcomes_n = int(totals.get("outcomes_lines") or 0)
    events_n = int(totals.get("events_lines") or 0)
    # Auditor gap is for laptop clones with almost no outcomes. Never surface
    # "missing ledger" when the book clearly has closed trades.
    gap = corpus.get("gap") if outcomes_n < 30 else None
    days, by_day = ca.load_sessions(days_back, report_dir, repo)
    trig = _exit_trig(report_dir, repo, days)

    registry = load_registry()
    state = load_state(report_dir)
    status_overlay = load_status(report_dir)
    lever = active_lever(registry, state)
    scored = score_lever(lever, by_day, days_back=days_back, gap=gap, trig=trig)

    levers_summary = []
    live_ids = []
    for L in registry.get("levers") or []:
        if not isinstance(L, dict) or not L.get("id"):
            continue
        lid = str(L["id"])
        st = status_overlay.get(lid) or str(L.get("status") or "queued")
        if st == "live":
            live_ids.append(lid)
        levers_summary.append({
            "id": lid,
            "title": L.get("title") or lid,
            "status": st,
        })

    lever_out = None
    if lever:
        lid = str(lever["id"])
        lever_out = {
            "id": lid,
            "title": lever.get("title") or lid,
            "hypothesis": lever.get("hypothesis") or "",
            "status": status_overlay.get(lid) or str(lever.get("status") or "queued"),
            "shipped_at": lever.get("shipped_at") or "",
            "knobs": lever.get("knobs") or {},
            "score": scored,
            "next_on_keep": lever.get("next_on_keep") or "",
            "next_on_kill": lever.get("next_on_kill") or "",
        }

    warnings = []
    if len(live_ids) > 1:
        warnings.append(f"multiple_live: {live_ids}")

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "gap": gap,
        "corpus": {
            "outcomes_lines": outcomes_n,
            "events_lines": events_n,
            "roots": corpus.get("roots") or [],
        },
        "go_live": audit.get("go_live"),
        "posture": audit.get("posture"),
        "latest": audit.get("latest"),
        "active_id": (lever or {}).get("id") if lever else None,
        "lever": lever_out,
        "levers": levers_summary,
        "doctrine": {
            "keep": [{"item": k.get("item"), "anchor": k.get("anchor")} for k in (audit.get("keep") or [])],
            "kill": [{"item": k.get("item"), "anchor": k.get("anchor")} for k in (audit.get("kill") or [])],
        },
        "warnings": warnings,
    }


def classify(text: str) -> dict[str, Any]:
    return ca.classify_proposal(text)


def record_verdict(
    verdict: str,
    *,
    note: str = "",
    operator: str = "",
    report_dir: Path | None = None,
    score: dict | None = None,
) -> dict[str, Any]:
    v = str(verdict or "").upper().strip()
    if v not in VALID_VERDICTS:
        raise ValueError(f"verdict must be one of {sorted(VALID_VERDICTS)}")

    report_dir = report_dir or resolve_report_dir()
    registry = load_registry()
    state = load_state(report_dir)
    lever = active_lever(registry, state)
    if not lever:
        raise ValueError("no active lever")

    lid = str(lever["id"])
    day = eod.bars.day_of(time.time())
    row = {
        "ts": time.time(),
        "day": day,
        "lever_id": lid,
        "verdict": v,
        "note": note or "",
        "score": score,
        "operator": operator or "",
    }
    path = _desk_dir(report_dir) / "verdicts.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, default=str) + "\n")

    status = load_status(report_dir)
    status[lid] = v.lower()
    save_status(status, report_dir)

    new_state = dict(state)
    new_state["last_verdict"] = v
    new_state["last_verdict_lever"] = lid
    if v in ("KEEP", "KILL"):
        next_id = None
        for L in registry.get("levers") or []:
            if not isinstance(L, dict):
                continue
            oid = str(L.get("id") or "")
            if not oid or oid == lid:
                continue
            overlay = status.get(oid) or str(L.get("status") or "")
            if overlay == "queued":
                next_id = oid
                break
        new_state["active_id"] = next_id
    else:
        new_state["active_id"] = lid
    save_state(new_state, report_dir)

    return {"recorded": row, "active_id": new_state.get("active_id")}


def activate(lever_id: str, *, report_dir: Path | None = None) -> dict[str, Any]:
    registry = load_registry()
    ids = {str(L.get("id")) for L in (registry.get("levers") or []) if isinstance(L, dict)}
    if lever_id not in ids:
        raise KeyError(lever_id)
    state = load_state(report_dir)
    state["active_id"] = lever_id
    save_state(state, report_dir)
    return {"active_id": lever_id}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--classify", default="", help="classify one proposal string")
    ap.add_argument("--record", default="", help="KEEP|KILL|MEASURE")
    ap.add_argument("--note", default="")
    args = ap.parse_args()

    if args.classify:
        print(json.dumps(classify(args.classify), indent=2))
        return 0

    if args.record:
        snap = snapshot(days_back=args.days)
        score = (snap.get("lever") or {}).get("score")
        out = record_verdict(args.record, note=args.note, score=score)
        print(json.dumps(out, indent=2, default=str))
        return 0

    payload = snapshot(days_back=args.days)
    if args.json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        lever = payload.get("lever") or {}
        score = lever.get("score") or {}
        print(f"Lever Desk  active={payload.get('active_id')}")
        if payload.get("gap"):
            print(f"GAP: {payload['gap']}")
        print(f"verdict={score.get('verdict')}  reason={score.get('reason')}")
        cur = score.get("current") or {}
        if score.get("kind") == "hold_capture":
            mc = cur.get("median_capture")
            mc_s = f"{mc:.3f}" if mc is not None else "—"
            print(
                f"hold_capture  n={cur.get('n')}  n_qual={cur.get('n_qualifying')}  "
                f"median_capture={mc_s}"
            )
        latest = payload.get("latest")
        if latest:
            print(
                f"latest {latest.get('session')}  n={latest.get('n')}  "
                f"live_r={latest.get('live_r')}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
