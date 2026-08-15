#!/usr/bin/env python3
"""replay_ab.py — score declared overlays, and search a declared grid, on a packed tape.

This is the desk's profit tuner. It does not write ``bot_config.json``.

Two modes share the same tape:

  1. Named experiments (hybrid-exit, continuation, heat-floor, flatten-vs-hold)
     — the questions already on the board.
  2. ``--search`` — cartesian product of the knobs in
     ``tools/replay_experiments.json`` → ``search``. That is the overnight
     run. Add knobs there to make it longer; do not open an unbounded optimizer.

Pack the live logs first (``tools/desk_tape.py pack``) so every sim and every
cell of the search reads the same frozen rows. ``AI_REPORT_DIR`` pointed at a
tape directory is enough for the older one-off sims too.

Why not "find the global maximum": live A/B cannot resolve 0.1R at this
volume (``outcome_slice.required_n`` ≈ 780/arm). An open sweep on one day's
polls is how the indicator gate posted t=+2.16 and then died out of sample.
Search ranks a *declared* grid by counterfactual session $, then by the
held-out days when more than one day is on the tape. Underpowered winners
stay hypotheses.

HONESTY
  • Shadow is poll samples, not a tape. Prices are ask-like.
  • T1 / stop only count if shadow printed those prices.
  • Missing is not zero. A flatten with no later print cannot be re-priced.
  • One day is a hypothesis. ``candidate`` requires min_n *and* both halves.
  • Search ranks the declared grid. It is not the maximum possible profit.

USAGE
    venv/bin/python tools/desk_tape.py pack --days 10
    venv/bin/python tools/replay_ab.py --day 2026-08-11
    venv/bin/python tools/replay_ab.py --search --days 10
    venv/bin/python tools/replay_ab.py --search --tape ai_reports/tapes/2026-08-11
    venv/bin/python tools/replay_ab.py --list
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "tools"))

import desk_tape  # noqa: E402
import outcome_slice as osl  # noqa: E402
import sim_edge_mode_ab as sim  # noqa: E402
import sim_heat_min_sweep as heat  # noqa: E402
import sim_hybrid as hyb  # noqa: E402
from ai_paths import resolve_report_dir  # noqa: E402

DEFAULT_EXPERIMENTS = _ROOT / "tools" / "replay_experiments.json"
FLATTEN_REASONS = frozenset({
    "flattened", "unprotected_flatten", "session_end", "eod_liquidate", "clock",
})

# Verdicts, tightest first.
VERDICT_CANDIDATE = "candidate"
VERDICT_HYPOTHESIS = "hypothesis"
VERDICT_DO_NOT = "do_not_promote"
VERDICT_NO_SCORE = "no_score"


# ── registry / tape ──────────────────────────────────────────────────────────

def load_registry(path: Path | None = None) -> dict[str, Any]:
    p = path or DEFAULT_EXPERIMENTS
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("experiments"), list):
        raise ValueError(f"bad experiments file: {p}")
    data.setdefault("min_n", 30)
    data.setdefault("delta_r_for_power", 0.1)
    return data


def list_experiments(reg: dict[str, Any]) -> list[dict[str, Any]]:
    return [e for e in reg["experiments"] if isinstance(e, dict) and e.get("enabled", True)]


def _day_of(ts: Any) -> str:
    return sim.day_of(ts)


def bucket_jsonl(path: Path, ts_keys: tuple[str, ...]) -> dict[str, list[dict]]:
    buckets: dict[str, list[dict]] = defaultdict(list)
    if not path.exists():
        return buckets
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        for k in ts_keys:
            d = _day_of(row.get(k))
            if d:
                buckets[d].append(row)
                break
    return buckets


def _extend_unique(dst: list[dict], seen: set[tuple], rows: list[dict]) -> None:
    for r in rows:
        key = (r.get("ts"), r.get("symbol"), r.get("entry_time"), r.get("exit_time"),
               r.get("close_reason"), r.get("price"))
        if key in seen:
            continue
        seen.add(key)
        dst.append(r)


def load_tape(*days: str, report_dir: Path | None = None) -> dict[str, Any]:
    """Shadow / outcomes / rejects for the requested calendar days.

    Day membership matches ``sim.load_jsonl_day``: a row belongs to a day if
    *any* of its timestamp keys fall on that day. First-key bucketing would
    drop a trade whose exit printed after local midnight.
    """
    root = Path(report_dir) if report_dir is not None else None
    shadow_path = (root / "shadow.jsonl") if root else sim._report("shadow.jsonl")
    out_path = (root / "outcomes.jsonl") if root else sim._report("outcomes.jsonl")
    rej_path = (root / "rejects.jsonl") if root else sim._report("rejects.jsonl")
    shadow_b = bucket_jsonl(shadow_path, ("ts",))
    out_b = bucket_jsonl(out_path, ("exit_time", "ts", "entry_time"))
    wanted = list(days)
    shadow: list[dict] = []
    outcomes: list[dict] = []
    rejects: list[dict] = []
    seen_s: set[tuple] = set()
    seen_o: set[tuple] = set()
    seen_r: set[tuple] = set()
    for d in wanted:
        _extend_unique(shadow, seen_s, sim.load_jsonl_day(shadow_path, d, ("ts",)))
        _extend_unique(
            outcomes, seen_o,
            sim.load_jsonl_day(out_path, d, ("exit_time", "ts", "entry_time")),
        )
        _extend_unique(rejects, seen_r, sim.load_jsonl_day(rej_path, d, ("ts",)))
    return {
        "days": wanted,
        "shadow": shadow,
        "outcomes": outcomes,
        "rejects": rejects,
        "available_days": sorted(set(shadow_b) | set(out_b)),
        "paths": {
            "shadow": str(shadow_path),
            "outcomes": str(out_path),
            "rejects": str(rej_path),
        },
    }


def load_from(
    days: list[str] | None = None,
    *,
    tape_dir: Path | None = None,
    report_dir: Path | None = None,
) -> dict[str, Any]:
    """Packed tape directory, or a slice of the live / ``report_dir`` tree."""
    if tape_dir is not None:
        tape = desk_tape.load(tape_dir)
        if days:
            want = set(days)
            tape["days"] = [d for d in tape["days"] if d in want]
        return tape
    wanted = list(days or [])
    return load_tape(*wanted, report_dir=report_dir)


# ── scoring helpers ──────────────────────────────────────────────────────────

def _dollar_per_r(row: dict) -> float | None:
    r = row.get("realized_r_multiple")
    pl = row.get("realized_pl_usd")
    try:
        r_f, pl_f = float(r), float(pl)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if abs(r_f) <= 1e-9:
        return None
    return pl_f / r_f


def _sum_live_usd(outcomes: list[dict]) -> float:
    tot = 0.0
    for o in outcomes:
        pl = o.get("realized_pl_usd")
        if pl is None:
            continue
        try:
            tot += float(pl)
        except (TypeError, ValueError):
            continue
    return tot


def half_split_deltas(deltas: list[float]) -> dict[str, Any]:
    """Chronological halves of a delta series. Sign flip → not a candidate."""
    n = len(deltas)
    out: dict[str, Any] = {"n_deltas": n}
    if n < 4:
        out["holds_both_halves"] = None
        return out
    half = n // 2
    a, b = deltas[:half], deltas[half:]
    sa, sb = sum(a), sum(b)
    out["half_a"] = sa
    out["half_b"] = sb
    out["holds_both_halves"] = bool(sa * sb > 0)
    return out


def decide(exp: dict[str, Any], *, min_n: int) -> str:
    """Promote nothing underpowered or half-flipped. See module docstring."""
    if exp.get("delta_usd") is None or exp.get("metric") != "session_usd":
        return VERDICT_NO_SCORE
    n = int(exp.get("n_scored") or exp.get("n") or 0)
    delta = float(exp["delta_usd"])
    holds = exp.get("holds_both_halves")
    if n < min_n:
        return VERDICT_HYPOTHESIS if delta > 0 else VERDICT_DO_NOT
    if holds is False:
        return VERDICT_DO_NOT
    if delta > 0 and holds is True:
        return VERDICT_CANDIDATE
    if delta > 0:
        return VERDICT_HYPOTHESIS
    return VERDICT_DO_NOT


def _power_note(min_n: int, delta_r: float) -> dict[str, Any]:
    return {
        "min_n": min_n,
        "n_needed_for_delta_r": osl.required_n(delta_r),
        "delta_r_for_power": delta_r,
    }


# ── experiments ──────────────────────────────────────────────────────────────

def _exit_res(outcomes: list[dict], shadow: list[dict]) -> dict[str, Any]:
    return sim.section_exit(outcomes, shadow)


def run_hybrid(tape: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    shadow, outcomes = tape["shadow"], tape["outcomes"]
    arm = hyb.section_arm3(shadow)
    exit_res = _exit_res(outcomes, shadow)
    bk = hyb.section_book(outcomes, shadow, exit_res, lookback_s=300.0)
    daybook = hyb.section_daybook(outcomes, bk["rows"])
    scored = [d for d in daybook["detail"] if not d.get("skip")]
    deltas = [float(d["hybrid_usd"]) - float(d["live_usd"]) for d in scored]
    halves = half_split_deltas(deltas)
    return {
        "name": spec["name"],
        "kind": "hybrid",
        "description": spec.get("description") or "",
        "metric": "session_usd",
        "n": daybook["n_outcomes"],
        "n_scored": len(scored),
        "live_usd": daybook["live_total_usd"],
        "variant_usd": daybook["hybrid_total_usd"],
        "delta_usd": daybook["swing_usd"],
        "arm_hybrid": arm.get("hybrid_arms"),
        "arm_scalp": arm.get("scalp_arms"),
        "hybrid_vs_scalp_mismatch": arm.get("hybrid_vs_scalp_mismatch"),
        "in_zone_n": arm.get("in_zone_n"),
        "book": {k: dict(v) for k, v in (bk.get("books") or {}).items()},
        "exit_n_scored": exit_res.get("n_scored"),
        "exit_sum_delta_r": exit_res.get("sum_delta_r"),
        **halves,
    }


def run_continuation(tape: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """Every live fill, but left_overbought rows re-priced by the hold walk."""
    shadow, outcomes = tape["shadow"], tape["outcomes"]
    day = (tape["days"] or [""])[0]
    arm = sim.section_arm(shadow, day)
    exit_res = _exit_res(outcomes, shadow)
    lob = [r for r in exit_res["left_overbought_rows"]]
    lob_i = 0
    live_usd = 0.0
    var_usd = 0.0
    deltas: list[float] = []
    n_scored = 0
    for o in outcomes:
        pl = o.get("realized_pl_usd")
        is_lob = o.get("close_reason") == "left_overbought"
        row = lob[lob_i] if (is_lob and lob_i < len(lob)) else None
        if is_lob:
            lob_i += 1
        if pl is None:
            continue
        try:
            pl_f = float(pl)
        except (TypeError, ValueError):
            continue
        live_usd += pl_f
        n_scored += 1
        dpr = _dollar_per_r(o)
        if (row is not None and row.get("cont_exit") != "no_shadow"
                and dpr is not None):
            v = dpr * float(row["cont_r"])
        else:
            v = pl_f
        var_usd += v
        if is_lob:
            deltas.append(v - pl_f)
    halves = half_split_deltas(deltas)
    return {
        "name": spec["name"],
        "kind": "continuation",
        "description": spec.get("description") or "",
        "metric": "session_usd",
        "n": len(outcomes),
        "n_scored": n_scored,
        "live_usd": live_usd,
        "variant_usd": var_usd,
        "delta_usd": var_usd - live_usd,
        "in_zone_n": arm.get("in_zone_n"),
        "arm_both": arm.get("both"),
        "arm_cont_only": arm.get("continuation_only"),
        "fwd_cont_only_mean_pct": arm.get("fwd_cont_only_mean_pct"),
        "exit_n_scored": exit_res.get("n_scored"),
        "exit_sum_delta_r": exit_res.get("sum_delta_r"),
        **halves,
    }


def run_heat_floor(tape: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """Signal-quality sweep. Never a session-$ candidate."""
    shadow = tape["shadow"]
    grid = [float(x) for x in (spec.get("grid") or [50, 55, 60, 65, 70])]
    d = heat.collect(shadow)
    sweep = heat.section_sweep(d["core"], d["heating"], grid)
    core = heat.stats(d["core"])
    # Best blended *mean* that still keeps some heating arms — for the brief,
    # not for promotion. The 08-11 write-up already showed no floor rescues
    # the tail; this just re-measures it on whatever tape we have.
    best = None
    for row in sweep:
        b = row.get("blended") or {}
        if not b.get("n"):
            continue
        if best is None or (b.get("mean") or -1e9) > (best["blended"]["mean"] or -1e9):
            best = row
    return {
        "name": spec["name"],
        "kind": "heat_min",
        "description": spec.get("description") or "",
        "metric": "fwd_pct",
        "n": len(d["heating"]),
        "n_scored": len(d["heating"]),
        "in_zone_n": d["in_zone_n"],
        "core": core,
        "sweep": [
            {
                "heat_min": r["heat_min"],
                "cont_only_n": (r["cont_only"] or {}).get("n"),
                "cont_only_mean": (r["cont_only"] or {}).get("mean"),
                "blended_n": (r["blended"] or {}).get("n"),
                "blended_mean": (r["blended"] or {}).get("mean"),
                "dropped": r["dropped"],
            }
            for r in sweep
        ],
        "best_heat_min": None if best is None else best["heat_min"],
        "delta_usd": None,
        "holds_both_halves": None,
    }


def _walk_hold(
    o: dict,
    ticks: list[dict],
    *,
    dead_min: float = 20.0,
    dead_mfe_r: float = 0.25,
) -> dict[str, Any] | None:
    """Replay T1 / stop / dead_trade / last print from entry. Same walk as
    ``sim.section_exit``, kept here so flatten-vs-hold does not depend on
    the left_overbought filter."""
    try:
        entry = float(o.get("entry_price") or 0)
        exit_px = float(o.get("exit_price") or o.get("exit_price_approx") or 0)
    except (TypeError, ValueError):
        return None
    if not entry or not exit_px:
        return None
    stop = float(o.get("stop_price") or 0)
    target = float(o.get("target_1") or 0)
    t_entry = float(o.get("entry_time") or o.get("ts") or 0)
    risk = (entry - stop) if stop and stop < entry else entry * 0.05
    if risk <= 0:
        risk = entry * 0.05
    live_r = o.get("realized_r_multiple")
    if live_r is None:
        live_r = (exit_px - entry) / risk
    live_r = float(live_r)
    if not ticks:
        return None
    mfe = 0.0
    hold_s = 0.0
    cont_r = 0.0
    why = "session_end"
    for r in ticks:
        t = float(r["ts"])
        px = float(r["price"])
        hold_s = t - t_entry
        rr = (px - entry) / risk
        mfe = max(mfe, rr)
        if stop and px <= stop:
            cont_r, why = (stop - entry) / risk, "stop"
            break
        if target and px >= target:
            cont_r, why = (target - entry) / risk, "target_1"
            break
        if hold_s >= dead_min * 60 and mfe < dead_mfe_r and px <= entry * 1.001:
            cont_r, why = rr, "dead_trade"
            break
    else:
        px = float(ticks[-1]["price"])
        cont_r = (px - entry) / risk
        why = "session_end"
        hold_s = float(ticks[-1]["ts"]) - t_entry
    return {
        "live_r": live_r,
        "hold_r": cont_r,
        "hold_exit": why,
        "delta_r": cont_r - live_r,
        "mfe_r": mfe,
        "hold_min": hold_s / 60.0,
    }


def run_flatten_hold(tape: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    shadow, outcomes = tape["shadow"], tape["outcomes"]
    by_sym: dict[str, list[dict]] = defaultdict(list)
    for r in shadow:
        if r.get("price") is None:
            continue
        by_sym[str(r.get("symbol") or "")].append(r)
    for s in by_sym:
        by_sym[s].sort(key=lambda x: float(x["ts"]))

    live_usd = 0.0
    var_usd = 0.0
    deltas: list[float] = []
    n_flat = 0
    n_repriced = 0
    skipped = 0
    rows: list[dict[str, Any]] = []
    for o in outcomes:
        pl = o.get("realized_pl_usd")
        if pl is None:
            continue
        try:
            pl_f = float(pl)
        except (TypeError, ValueError):
            continue
        live_usd += pl_f
        reason = str(o.get("close_reason") or "")
        if reason not in FLATTEN_REASONS:
            var_usd += pl_f
            continue
        n_flat += 1
        sym = str(o.get("symbol") or "")
        t_entry = float(o.get("entry_time") or o.get("ts") or 0)
        ticks = [r for r in by_sym.get(sym, []) if float(r["ts"]) >= t_entry - 1]
        walked = _walk_hold(o, ticks)
        dpr = _dollar_per_r(o)
        if walked is None or dpr is None:
            var_usd += pl_f
            skipped += 1
            rows.append({"symbol": sym, "close_reason": reason, "repriced": False})
            continue
        v = dpr * float(walked["hold_r"])
        var_usd += v
        deltas.append(v - pl_f)
        n_repriced += 1
        rows.append({
            "symbol": sym,
            "close_reason": reason,
            "repriced": True,
            "live_r": walked["live_r"],
            "hold_r": walked["hold_r"],
            "hold_exit": walked["hold_exit"],
            "delta_usd": v - pl_f,
        })
    halves = half_split_deltas(deltas)
    return {
        "name": spec["name"],
        "kind": "flatten_hold",
        "description": spec.get("description") or "",
        "metric": "session_usd",
        "n": len(outcomes),
        "n_scored": n_repriced,
        "n_flatten": n_flat,
        "n_skipped": skipped,
        "live_usd": live_usd,
        "variant_usd": var_usd,
        "delta_usd": var_usd - live_usd,
        "rows": rows,
        **halves,
    }


_RUNNERS = {
    "hybrid": run_hybrid,
    "continuation": run_continuation,
    "heat_min": run_heat_floor,
    "flatten_hold": run_flatten_hold,
}


# ── overnight search ─────────────────────────────────────────────────────────

DEFAULT_SEARCH = {
    "ai_edge_mode": ["exhaustion_scalp", "continuation"],
    "ai_exit_left_overbought": [False, True],
    "ai_watch_exhaustion_heat_min_pct": [50, 55, 60, 65, 70],
    "flatten": ["clock", "hold"],
    "dead_min": [15, 20, 30],
}


def iter_grid(search: dict[str, list[Any]]) -> list[dict[str, Any]]:
    keys = [k for k, vs in search.items() if vs]
    if not keys:
        return [{}]
    values = [list(search[k]) for k in keys]
    return [dict(zip(keys, combo)) for combo in itertools.product(*values)]


def _outcome_day(o: dict) -> str:
    for k in ("exit_time", "ts", "entry_time"):
        d = _day_of(o.get(k))
        if d:
            return d
    return ""


def prepare_index(tape: dict[str, Any], dead_mins: list[float]) -> list[dict[str, Any]]:
    """One scored fill + precomputed hold-walks per dead_min. Built once."""
    by_sym: dict[str, list[dict]] = defaultdict(list)
    for r in tape.get("shadow") or []:
        if r.get("price") is None:
            continue
        by_sym[str(r.get("symbol") or "")].append(r)
    for s in by_sym:
        by_sym[s].sort(key=lambda x: float(x["ts"]))

    rows: list[dict[str, Any]] = []
    for o in tape.get("outcomes") or []:
        pl = o.get("realized_pl_usd")
        if pl is None:
            continue
        try:
            pl_f = float(pl)
        except (TypeError, ValueError):
            continue
        state = o.get("entry_exhaustion_state")
        exh = o.get("entry_exhaustion")
        adopted = state == "adopted" or (state == "overbought" and exh is None)
        reason = str(o.get("close_reason") or "")
        sym = str(o.get("symbol") or "")
        t_entry = float(o.get("entry_time") or o.get("ts") or 0)
        ticks = [r for r in by_sym.get(sym, []) if float(r["ts"]) >= t_entry - 1]
        walks = {float(dm): _walk_hold(o, ticks, dead_min=float(dm)) for dm in dead_mins}
        rows.append({
            "symbol": sym,
            "day": _outcome_day(o),
            "pl": pl_f,
            "dpr": _dollar_per_r(o),
            "state": state,
            "exh": exh,
            "adopted": adopted,
            "reason": reason,
            "is_lob": reason == "left_overbought",
            "is_flat": reason in FLATTEN_REASONS,
            "walks": walks,
        })
    return rows


def score_settings(
    index: list[dict[str, Any]],
    settings: dict[str, Any],
    *,
    min_n: int,
) -> dict[str, Any]:
    """Filled-only counterfactual: drop / reprice the trades that already happened."""
    mode = str(settings.get("ai_edge_mode") or "continuation")
    lob_on = bool(settings.get("ai_exit_left_overbought", mode == "exhaustion_scalp"))
    try:
        heat_min = float(settings.get("ai_watch_exhaustion_heat_min_pct") or 50.0)
    except (TypeError, ValueError):
        heat_min = 50.0
    flatten = str(settings.get("flatten") or "clock")
    try:
        dead_min = float(settings.get("dead_min") or 20.0)
    except (TypeError, ValueError):
        dead_min = 20.0

    live_usd = 0.0
    var_usd = 0.0
    deltas: list[float] = []
    n_taken = 0
    by_day: dict[str, list[float]] = defaultdict(list)

    for row in index:
        live_usd += row["pl"]
        take = True
        if not row["adopted"] and mode == "exhaustion_scalp" and row["state"] == "heating":
            take = False
        elif (
            not row["adopted"]
            and row["state"] == "heating"
            and row["exh"] is not None
            and float(row["exh"]) + 1e-9 < heat_min
        ):
            take = False

        if not take:
            deltas.append(-row["pl"])
            by_day[row["day"]].append(-row["pl"])
            continue

        n_taken += 1
        v = row["pl"]
        walk = row["walks"].get(dead_min)
        dpr = row["dpr"]
        if (not lob_on and row["is_lob"] and not row["adopted"]
                and walk is not None and dpr is not None):
            v = dpr * float(walk["hold_r"])
        elif (flatten == "hold" and row["is_flat"] and walk is not None and dpr is not None):
            v = dpr * float(walk["hold_r"])
        var_usd += v
        dlt = v - row["pl"]
        deltas.append(dlt)
        by_day[row["day"]].append(dlt)

    halves = half_split_deltas(deltas)
    day_names = sorted(d for d in by_day if d)
    if len(day_names) >= 4:
        cut = max(1, int(len(day_names) * 0.7))
        train_days, test_days = day_names[:cut], day_names[cut:]
    else:
        train_days, test_days = day_names, []
    train_delta = sum(sum(by_day[d]) for d in train_days)
    test_delta = sum(sum(by_day[d]) for d in test_days) if test_days else None
    result = {
        "settings": dict(settings),
        "metric": "session_usd",
        "n": len(index),
        "n_scored": n_taken,
        "live_usd": live_usd,
        "variant_usd": var_usd,
        "delta_usd": var_usd - live_usd,
        "train_days": train_days,
        "test_days": test_days,
        "train_delta_usd": train_delta,
        "test_delta_usd": test_delta,
        **halves,
    }
    result["verdict"] = decide(result, min_n=min_n)
    result["underpowered"] = n_taken < min_n
    return result


def run_search(
    tape: dict[str, Any],
    *,
    registry: dict[str, Any] | None = None,
    write: bool = True,
    progress_path: Path | None = None,
) -> dict[str, Any]:
    """Score every cell of the declared grid. Writes one jsonl line per cell."""
    reg = registry or load_registry()
    min_n = int(reg.get("min_n") or 30)
    delta_r = float(reg.get("delta_r_for_power") or 0.1)
    search = reg.get("search") or DEFAULT_SEARCH
    cells = iter_grid(search)
    dead_mins = sorted({
        float(c.get("dead_min") or 20.0) for c in cells
    } or {20.0})
    index = prepare_index(tape, dead_mins)
    if not index:
        return {
            "ok": True,
            "skipped": "no_outcomes",
            "days": tape.get("days") or [],
            "n_cells": 0,
            "cells": [],
            "action": "no scored outcomes on this tape — nothing to search",
            "power": _power_note(min_n, delta_r),
        }

    label = _label(tape.get("days") or [])
    if write and progress_path is None:
        out_dir = resolve_report_dir() / "replay_ab"
        out_dir.mkdir(parents=True, exist_ok=True)
        progress_path = out_dir / f"search_{label}.jsonl"
        if progress_path.exists():
            progress_path.unlink()

    scored: list[dict[str, Any]] = []
    fh = progress_path.open("a", encoding="utf-8") if progress_path else None
    try:
        for i, settings in enumerate(cells, start=1):
            cell = score_settings(index, settings, min_n=min_n)
            cell["i"] = i
            scored.append(cell)
            if fh is not None:
                fh.write(json.dumps(cell, default=str) + "\n")
                fh.flush()
    finally:
        if fh is not None:
            fh.close()

    def _sort_key(c: dict[str, Any]) -> tuple:
        oos = c.get("test_delta_usd")
        primary = float(oos) if oos is not None else float(c.get("delta_usd") or 0)
        order = {VERDICT_CANDIDATE: 0, VERDICT_HYPOTHESIS: 1, VERDICT_DO_NOT: 2}
        return (order.get(c.get("verdict"), 9), -primary)

    ranked = sorted(scored, key=_sort_key)
    best_c = next((c for c in ranked if c.get("verdict") == VERDICT_CANDIDATE), None)
    best_h = next((c for c in ranked if c.get("verdict") == VERDICT_HYPOTHESIS), None)
    if best_c is not None:
        action = (
            "candidate survived halves + min_n — still do not auto-write "
            f"config; review {best_c['settings']}"
        )
    elif best_h is not None:
        action = (
            f"no candidate. best hypothesis Δ${best_h['delta_usd']:+.2f} "
            f"{best_h['settings']} — do not change config"
        )
    else:
        action = "no cell beat live on this tape — do not change config"

    payload = {
        "ok": True,
        "skipped": None,
        "kind": "search",
        "days": tape.get("days") or [],
        "n_shadow": len(tape.get("shadow") or []),
        "n_outcomes": len(tape.get("outcomes") or []),
        "n_index": len(index),
        "n_cells": len(scored),
        "live_usd": _sum_live_usd(tape.get("outcomes") or []),
        "power": _power_note(min_n, delta_r),
        "search": search,
        "cells": ranked,
        "best_candidate": None if best_c is None else {
            "settings": best_c["settings"], "delta_usd": best_c["delta_usd"],
            "test_delta_usd": best_c.get("test_delta_usd"),
            "n_scored": best_c.get("n_scored"), "verdict": best_c["verdict"],
        },
        "best_hypothesis": None if best_h is None else {
            "settings": best_h["settings"], "delta_usd": best_h["delta_usd"],
            "test_delta_usd": best_h.get("test_delta_usd"),
            "n_scored": best_h.get("n_scored"), "verdict": best_h["verdict"],
        },
        "action": action,
    }
    if write:
        payload["paths"] = write_search_artifacts(payload, progress_path)
    return payload


def write_search_artifacts(
    payload: dict[str, Any],
    progress_path: Path | None,
) -> dict[str, str]:
    rd = resolve_report_dir() / "replay_ab"
    rd.mkdir(parents=True, exist_ok=True)
    label = _label(payload.get("days") or [])
    json_path = rd / f"search_{label}.json"
    md_path = rd / f"search_{label}.md"
    slim = dict(payload)
    # Full cell list stays in jsonl; keep the summary json readable.
    slim["cells"] = [
        {
            "settings": c.get("settings"),
            "delta_usd": c.get("delta_usd"),
            "test_delta_usd": c.get("test_delta_usd"),
            "train_delta_usd": c.get("train_delta_usd"),
            "n_scored": c.get("n_scored"),
            "verdict": c.get("verdict"),
            "holds_both_halves": c.get("holds_both_halves"),
        }
        for c in (payload.get("cells") or [])[:40]
    ]
    json_path.write_text(json.dumps(slim, indent=2, default=str) + "\n", encoding="utf-8")
    md_path.write_text(render_search_md(payload), encoding="utf-8")
    out = {"search_json": str(json_path), "search_md": str(md_path)}
    if progress_path is not None:
        out["search_jsonl"] = str(progress_path)
    return out


def render_search_md(payload: dict[str, Any]) -> str:
    days = payload.get("days") or []
    power = payload.get("power") or {}
    lines = [
        f"# Overnight search — {_label(days)}",
        "",
        "Declared grid on a frozen tape. Not an open optimizer. "
        "Does not write `bot_config.json`.",
        "",
        f"- days: {', '.join(days) or '(none)'}",
        f"- cells: {payload.get('n_cells')}  fills: {payload.get('n_index')}",
        f"- live session $: {payload.get('live_usd')}",
        f"- min_n={power.get('min_n')}  n needed for "
        f"{power.get('delta_r_for_power')}R: {power.get('n_needed_for_delta_r')}",
        "",
        "## Action",
        payload.get("action") or "do not change config",
        "",
        "## Top cells (held-out $ if present, else full-tape $)",
        "",
        "| Δ$ | test $ | n | verdict | settings |",
        "|---:|---:|---:|---|---|",
    ]
    for c in (payload.get("cells") or [])[:15]:
        d = c.get("delta_usd")
        t = c.get("test_delta_usd")
        ds = "—" if d is None else f"{d:+.2f}"
        ts = "—" if t is None else f"{t:+.2f}"
        lines.append(
            f"| {ds} | {ts} | {c.get('n_scored')} | {c.get('verdict')} | "
            f"`{c.get('settings')}` |"
        )
    lines += [
        "",
        "## Honesty",
        "- Search ranks the declared grid. It is not maximum possible profit.",
        "- Held-out days beat in-sample $ when more than one session is packed.",
        "- Underpowered beats are hypotheses. They do not change config.",
        "",
    ]
    return "\n".join(lines)


def run_experiment(tape: dict[str, Any], spec: dict[str, Any], *, min_n: int) -> dict[str, Any]:
    kind = str(spec.get("kind") or "")
    fn = _RUNNERS.get(kind)
    if fn is None:
        return {
            "name": spec.get("name") or kind,
            "kind": kind,
            "metric": None,
            "error": f"unknown kind {kind!r}",
            "delta_usd": None,
            "verdict": VERDICT_NO_SCORE,
        }
    result = fn(tape, spec)
    result["verdict"] = decide(result, min_n=min_n)
    result["underpowered"] = int(result.get("n_scored") or result.get("n") or 0) < min_n
    return result


def rank(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Session-$ experiments only, candidates first, then Δ$ descending."""
    money = [r for r in results if r.get("metric") == "session_usd" and r.get("delta_usd") is not None]
    order = {VERDICT_CANDIDATE: 0, VERDICT_HYPOTHESIS: 1, VERDICT_DO_NOT: 2, VERDICT_NO_SCORE: 3}
    return sorted(money, key=lambda r: (order.get(r.get("verdict"), 9), -float(r["delta_usd"])))


def pick(results: list[dict[str, Any]]) -> dict[str, Any]:
    ranked = rank(results)
    best_c = next((r for r in ranked if r.get("verdict") == VERDICT_CANDIDATE), None)
    best_h = next((r for r in ranked if r.get("verdict") == VERDICT_HYPOTHESIS), None)
    return {
        "best_candidate": None if best_c is None else {
            "name": best_c["name"], "delta_usd": best_c["delta_usd"],
            "n_scored": best_c.get("n_scored"), "verdict": best_c["verdict"],
        },
        "best_hypothesis": None if best_h is None else {
            "name": best_h["name"], "delta_usd": best_h["delta_usd"],
            "n_scored": best_h.get("n_scored"), "verdict": best_h["verdict"],
        },
        "action": (
            f"candidate {best_c['name']} survived halves + min_n — still do not "
            f"auto-write config; review {best_c['name']} then change by hand"
            if best_c is not None else
            f"no candidate. best hypothesis is {best_h['name']} "
            f"(Δ${best_h['delta_usd']:+.2f}) — do not change config"
            if best_h is not None else
            "no overlay beat live on this tape — do not change config"
        ),
    }


# ── run / artifacts ──────────────────────────────────────────────────────────

def run_days(
    days: list[str],
    *,
    registry: dict[str, Any] | None = None,
    only: list[str] | None = None,
    write: bool = True,
    tape_dir: Path | None = None,
    report_dir: Path | None = None,
    tape: dict[str, Any] | None = None,
) -> dict[str, Any]:
    reg = registry or load_registry()
    min_n = int(reg.get("min_n") or 30)
    delta_r = float(reg.get("delta_r_for_power") or 0.1)
    specs = list_experiments(reg)
    if only:
        want = set(only)
        specs = [s for s in specs if s.get("name") in want]

    if tape is None:
        tape = load_from(days, tape_dir=tape_dir, report_dir=report_dir)
    if not tape["shadow"] and not tape["outcomes"]:
        payload = {
            "ok": True,
            "skipped": "no_tape",
            "days": days,
            "available_days": tape["available_days"],
            "paths": tape["paths"],
            "experiments": [],
            "ranking": [],
            "best_candidate": None,
            "best_hypothesis": None,
            "action": "no shadow/outcomes for these days — nothing to tune",
            "power": _power_note(min_n, delta_r),
        }
        return payload

    results = [run_experiment(tape, spec, min_n=min_n) for spec in specs]
    chosen = pick(results)
    payload = {
        "ok": True,
        "skipped": None,
        "days": days,
        "n_shadow": len(tape["shadow"]),
        "n_outcomes": len(tape["outcomes"]),
        "live_usd": _sum_live_usd(tape["outcomes"]),
        "paths": tape["paths"],
        "power": _power_note(min_n, delta_r),
        "experiments": results,
        "ranking": [
            {
                "name": r["name"],
                "verdict": r.get("verdict"),
                "delta_usd": r.get("delta_usd"),
                "n_scored": r.get("n_scored"),
                "underpowered": r.get("underpowered"),
                "holds_both_halves": r.get("holds_both_halves"),
            }
            for r in rank(results)
        ],
        **chosen,
    }
    if write:
        payload["paths"] = {**tape["paths"], **write_artifacts(payload)}
    return payload


def _label(days: list[str]) -> str:
    if not days:
        return "none"
    if len(days) == 1:
        return days[0]
    return f"{days[0]}_{days[-1]}"


def render_md(payload: dict[str, Any]) -> str:
    days = payload.get("days") or []
    label = _label(days)
    power = payload.get("power") or {}
    lines = [
        f"# Replay tuner — {label}",
        "",
        "Declared overlays scored on this desk's own shadow/outcome tape. "
        "Not an open search. Does not write `bot_config.json`.",
        "",
        f"- days: {', '.join(days) or '(none)'}",
        f"- shadow rows: {payload.get('n_shadow')}  outcomes: {payload.get('n_outcomes')}",
        f"- live session $: {payload.get('live_usd')}",
        f"- min_n={power.get('min_n')}  "
        f"n needed for {power.get('delta_r_for_power')}R: "
        f"{power.get('n_needed_for_delta_r')}",
        "",
        "## Action",
        payload.get("action") or "do not change config",
        "",
    ]
    bc = payload.get("best_candidate")
    bh = payload.get("best_hypothesis")
    lines.append("## Picks")
    if bc:
        lines.append(f"- candidate: `{bc['name']}`  Δ${bc['delta_usd']:+.2f}  "
                     f"n={bc.get('n_scored')}")
    else:
        lines.append("- candidate: none")
    if bh:
        lines.append(f"- hypothesis: `{bh['name']}`  Δ${bh['delta_usd']:+.2f}  "
                     f"n={bh.get('n_scored')}")
    else:
        lines.append("- hypothesis: none")
    lines += ["", "## Ranking (session $)", ""]
    lines.append("| overlay | verdict | Δ$ | n | both halves |")
    lines.append("|---|---|---:|---:|---|")
    for r in payload.get("ranking") or []:
        halves = r.get("holds_both_halves")
        h = "—" if halves is None else ("yes" if halves else "NO")
        d = r.get("delta_usd")
        ds = "—" if d is None else f"{d:+.2f}"
        lines.append(
            f"| `{r['name']}` | {r.get('verdict')} | {ds} | "
            f"{r.get('n_scored')} | {h} |"
        )
    lines += ["", "## Experiments", ""]
    for exp in payload.get("experiments") or []:
        lines.append(f"### {exp.get('name')} ({exp.get('kind')})")
        if exp.get("description"):
            lines.append(exp["description"])
        if exp.get("metric") == "session_usd":
            lines.append(
                f"- live ${exp.get('live_usd')} → variant ${exp.get('variant_usd')}  "
                f"Δ${exp.get('delta_usd')}"
            )
            lines.append(
                f"- n_scored={exp.get('n_scored')}  verdict=`{exp.get('verdict')}`  "
                f"underpowered={exp.get('underpowered')}"
            )
        elif exp.get("kind") == "heat_min":
            core = exp.get("core") or {}
            lines.append(
                f"- overbought core n={core.get('n')} mean={core.get('mean')}%  "
                f"(signal only, not P&L)"
            )
            lines.append(f"- best heat_min on this tape: {exp.get('best_heat_min')}")
            lines.append("- not a session-$ candidate; do not promote from this sweep")
        if exp.get("error"):
            lines.append(f"- error: {exp['error']}")
        lines.append("")
    lines += [
        "## Honesty",
        "- Shadow is polls, not a tape; exits use ask-like prices.",
        "- An edge that flips sign across chronological halves is not a candidate.",
        "- Underpowered beats are hypotheses. They do not change config.",
        "",
    ]
    return "\n".join(lines)


def write_artifacts(payload: dict[str, Any]) -> dict[str, str]:
    rd = resolve_report_dir() / "replay_ab"
    rd.mkdir(parents=True, exist_ok=True)
    label = _label(payload.get("days") or [])
    json_path = rd / f"{label}.json"
    md_path = rd / f"{label}.md"
    json_path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    md_path.write_text(render_md(payload), encoding="utf-8")
    return {"replay_json": str(json_path), "replay_md": str(md_path)}


def brief_for_daily(payload: dict[str, Any]) -> dict[str, Any]:
    """Small dict daily_learn can drop into the ledger / EOD note."""
    if not payload.get("ok"):
        return {"ok": False, "error": payload.get("error") or "replay failed"}
    if payload.get("skipped"):
        return {"ok": True, "skipped": payload["skipped"],
                "action": payload.get("action")}
    bc, bh = payload.get("best_candidate"), payload.get("best_hypothesis")
    return {
        "ok": True,
        "days": payload.get("days"),
        "live_usd": payload.get("live_usd"),
        "best_candidate": bc,
        "best_hypothesis": bh,
        "action": payload.get("action"),
        "ranking": payload.get("ranking") or [],
    }


# ── CLI ──────────────────────────────────────────────────────────────────────

def _latest_day(report_dir: Path | None = None) -> str:
    have = desk_tape.available_days(report_dir)
    if have:
        return have[-1]
    return datetime.now().strftime("%Y-%m-%d")


def _resolve_days(
    *,
    day: str | None,
    n_days: int | None,
    report_dir: Path | None,
    tape_dir: Path | None,
) -> list[str]:
    if day:
        return [day]
    if tape_dir is not None:
        man = desk_tape.load(tape_dir)
        days = list(man.get("days") or [])
        if n_days:
            return days[-n_days:]
        return days
    have = desk_tape.available_days(report_dir)
    n = n_days if n_days is not None else 1
    if not have:
        return [_latest_day(report_dir)]
    return have[-n:] if n > 0 else have


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--day", help="single ET day YYYY-MM-DD")
    ap.add_argument("--days", type=int, default=None,
                    help="last N days that have tape (default: 1, the latest)")
    ap.add_argument("--tape", default=None,
                    help="packed tape directory from tools/desk_tape.py")
    ap.add_argument("--pack", action="store_true",
                    help="freeze the requested days into ai_reports/tapes/ first")
    ap.add_argument("--search", action="store_true",
                    help="score the declared settings grid (overnight run)")
    ap.add_argument("--experiments", default=str(DEFAULT_EXPERIMENTS),
                    help="registry JSON (default: tools/replay_experiments.json)")
    ap.add_argument("--only", action="append", default=[],
                    help="run only this experiment name (repeatable)")
    ap.add_argument("--list", action="store_true", help="print the registry and exit")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args()

    reg = load_registry(Path(args.experiments))
    if args.list:
        print(json.dumps({
            "min_n": reg.get("min_n"),
            "delta_r_for_power": reg.get("delta_r_for_power"),
            "search": reg.get("search") or DEFAULT_SEARCH,
            "n_search_cells": len(iter_grid(reg.get("search") or DEFAULT_SEARCH)),
            "experiments": list_experiments(reg),
        }, indent=2))
        return 0

    tape_dir = Path(args.tape) if args.tape else None
    days = _resolve_days(
        day=args.day, n_days=args.days, report_dir=None, tape_dir=tape_dir,
    )
    if args.pack and tape_dir is None:
        packed = desk_tape.pack(days)
        tape_dir = Path(packed["path"])

    tape = load_from(days, tape_dir=tape_dir)
    if args.search:
        payload = run_search(tape, registry=reg, write=not args.no_write)
        if args.json:
            print(json.dumps(payload, indent=2, default=str))
            return 0
        print(render_search_md(payload))
        paths = payload.get("paths") or {}
        if paths.get("search_md"):
            print("Wrote:")
            for k, v in paths.items():
                print(f"  {k}: {v}")
        return 0

    payload = run_days(
        days, registry=reg, only=args.only or None,
        write=not args.no_write, tape=tape,
    )

    if args.json:
        print(json.dumps(payload, indent=2, default=str))
        return 0

    print(render_md(payload))
    paths = payload.get("paths") or {}
    if paths.get("replay_md"):
        print("Wrote:")
        print(f"  md:   {paths['replay_md']}")
        print(f"  json: {paths['replay_json']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
