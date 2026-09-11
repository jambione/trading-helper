#!/usr/bin/env python3
"""RSI × EXH × MACD-gap entry AB harness (phased run-loop).

Sweeps entry-gate cells against historical ``arm_ok`` shadow arms, recomputes
CM RSI-2 / fast-%R heat / MACD hist from 1m bars (do **not** trust logged
``cm_rsi`` alone — see ``tools/rsi_counterfactual.py``), scores 30m MTM keeps,
and writes an **advisory** champion under ``benchmarks/entry_ab/``.

Does **not** write ``config/bot_config.json`` or flip live arms/exits.
Champion is research-only until Phase 1 / Sep 18 — operator applies by hand.

EXH approximation
  Fast %R only via ``signals.compute_percent_r_exhaustion`` on the day bars;
  heat_pct = clamp(100 + s_percentR, 0, 100). Slow-line / live clock-window
  EXH is not fully mirrored. Missing indicator → fail closed (skip arm).

Usage (prefer Mac mini + .venv; bars + shadow live there)::

    .venv/bin/python tools/entry_arm_ab.py --from 2026-09-01 --to 2026-09-11 --phase A
    .venv/bin/python tools/entry_arm_ab.py --from 2026-09-01 --to 2026-09-11 --phase all
    .venv/bin/python tools/entry_arm_ab.py --summarize
"""
from __future__ import annotations

import argparse
import json
import math
import os
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

ET = ZoneInfo("America/New_York")
SHADOW = ROOT / "ai_reports" / "shadow.jsonl"
OUTCOMES = ROOT / "ai_reports" / "outcomes.jsonl"
SEARCH_PATH = ROOT / "tools" / "entry_arm_ab_search.json"
OUT_DIR = ROOT / "benchmarks" / "entry_ab"

RSI_LENGTH = 2
CELL_KEYS = (
    "rsi_max", "rsi_require_rising", "rsi_allow_falling_below",
    "mistimed_heat", "mistimed_rsi_min", "mistimed_rsi_peak_min",
    "heat_min", "heat_max", "require_exh_rising",
    "require_macd", "macd_min_gap", "macd_block_narrowing",
)


# ── imports from existing tools ──────────────────────────────────────────────

def _wilder_rsi(closes: list[float], period: int) -> list[float | None]:
    from rsi_counterfactual import _wilder_rsi as _wr
    return _wr(closes, period)


def _fetch_day_bars(client, sym: str, day: str):
    from rsi_counterfactual import _fetch_day_bars as _fd
    return _fd(client, sym, day)


# ── search / cells ───────────────────────────────────────────────────────────

def load_search(path: Path | None = None) -> dict:
    p = path or SEARCH_PATH
    return json.loads(p.read_text(encoding="utf-8"))


def merge_cell(live: dict, overlay: dict) -> dict:
    cell = {k: live.get(k) for k in CELL_KEYS}
    cell["id"] = str(overlay.get("id") or "cell")
    for k in CELL_KEYS:
        if k in overlay:
            cell[k] = overlay[k]
    return cell


def phase_a_cells(search: dict) -> list[dict]:
    live = search["live"]
    out: list[dict] = []
    seen: set[str] = set()
    for raw in search.get("phase_a") or []:
        cell = merge_cell(live, raw if isinstance(raw, dict) else {"id": str(raw)})
        if cell["id"] in seen:
            continue
        seen.add(cell["id"])
        out.append(cell)
    if "LIVE" not in seen:
        out.insert(0, merge_cell(live, {"id": "LIVE"}))
    return out


def cell_fingerprint(cell: dict) -> tuple:
    return tuple(
        (k, None if cell.get(k) is None else cell.get(k)) for k in CELL_KEYS
    )


def complexity_score(cell: dict, live: dict) -> int:
    """Fewer active gates / closer to LIVE → lower (preferred on ties)."""
    n = 0
    if cell.get("rsi_max") is not None:
        n += 1
    if cell.get("rsi_require_rising"):
        n += 1
    if float(cell.get("rsi_allow_falling_below") or 0) > 0:
        n += 1
    if cell.get("mistimed_heat"):
        n += 1
    if float(cell.get("heat_min") or 0) > 0:
        n += 1
    if float(cell.get("heat_max") or 0) > 0:
        n += 1
    if cell.get("require_exh_rising"):
        n += 1
    if cell.get("require_macd"):
        n += 1
    if cell.get("macd_min_gap") is not None and cell.get("require_macd"):
        n += 1
    if cell.get("macd_block_narrowing"):
        n += 1
    drift = sum(1 for k in CELL_KEYS if cell.get(k) != live.get(k))
    return n + drift


def to_bot_config(cell: dict, search: dict) -> dict[str, Any]:
    m = search.get("bot_config_map") or {}
    out: dict[str, Any] = {}
    for k, cfg_key in m.items():
        out[cfg_key] = cell.get(k)
    return out


# ── data load ────────────────────────────────────────────────────────────────

def _et_day(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), timezone.utc).astimezone(ET).strftime(
        "%Y-%m-%d"
    )


def load_arms(day_from: str, day_to: str, shadow: Path = SHADOW) -> list[dict]:
    rows: list[dict] = []
    with shadow.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("arm_ok") is not True or not r.get("ts") or not r.get("symbol"):
                continue
            d = _et_day(float(r["ts"]))
            if d < day_from or d > day_to:
                continue
            rows.append(r)
    return rows


def load_outcomes_index(path: Path = OUTCOMES) -> dict[tuple[str, str], list[dict]]:
    """(symbol, et_day) → outcome rows with entry-ish timestamps."""
    out: dict[tuple[str, str], list[dict]] = defaultdict(list)
    if not path.exists():
        return out
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                sym = str(r.get("symbol") or "").upper().strip()
                ts = r.get("entry_ts") or r.get("ts") or r.get("opened_ts")
                if not sym or ts is None:
                    continue
                try:
                    tsf = float(ts)
                except (TypeError, ValueError):
                    continue
                out[(sym, _et_day(tsf))].append(r)
    except Exception:
        pass
    return out


def match_fill(
    outcomes_ix: dict,
    sym: str,
    day: str,
    arm_ts: float,
    window_sec: float = 180.0,
) -> dict | None:
    rows = outcomes_ix.get((sym, day)) or []
    best = None
    best_dt = window_sec + 1.0
    for r in rows:
        ts = r.get("entry_ts") or r.get("ts") or r.get("opened_ts")
        try:
            tsf = float(ts)
        except (TypeError, ValueError):
            continue
        dt = tsf - float(arm_ts)
        if dt < -30.0 or dt > window_sec:
            continue
        if abs(dt) < best_dt:
            best_dt = abs(dt)
            best = r
    return best


# ── indicator recompute ──────────────────────────────────────────────────────

def _rising(series: list[float | None], i: int, tl: int) -> bool | None:
    if i < tl:
        return None
    a, b = series[i], series[i - tl]
    if a is None or b is None:
        return None
    try:
        return float(a) > float(b)
    except (TypeError, ValueError):
        return None


def _falling(series: list[float | None], i: int, tl: int) -> bool | None:
    if i < tl:
        return None
    a, b = series[i], series[i - tl]
    if a is None or b is None:
        return None
    try:
        return float(a) < float(b)
    except (TypeError, ValueError):
        return None


def _finite(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return v


def build_day_indicators(df, *, tl: int, rte_threshold: float) -> dict[str, Any] | None:
    """Precompute RSI / EXH / MACD series for one symbol-day OHLC frame."""
    import signals
    import strategy_three_indicator as strat

    if df is None or len(df) < RSI_LENGTH + tl + 5:
        return None
    try:
        closes = [float(c) for c in df["close"]]
    except Exception:
        return None
    stamps = [t.timestamp() for t in df.index]
    rsi = _wilder_rsi(closes, RSI_LENGTH)

    # EXH: fast %R → 0–100 heat (documented approximation).
    try:
        exh_df = signals.compute_percent_r_exhaustion(
            df, dict(strat.DEFAULT_PARAMS))
        s_pr = exh_df["s_percentR"]
        heat: list[float | None] = []
        for v in s_pr:
            fv = _finite(v)
            if fv is None:
                heat.append(None)
            else:
                heat.append(max(0.0, min(100.0, 100.0 + fv)))
    except Exception:
        return None

    # MACD hist = gap.
    try:
        macd_df = signals.compute_macd(df, dict(strat.DEFAULT_PARAMS))
        hist = [_finite(v) for v in macd_df["macd_hist"]]
    except Exception:
        return None

    return {
        "stamps": stamps,
        "closes": closes,
        "rsi": rsi,
        "heat": heat,
        "macd_gap": hist,
        "tl": tl,
        "rte_threshold": float(rte_threshold),
    }


def indicators_at(day_ind: dict, ts: float) -> dict[str, Any] | None:
    stamps = day_ind["stamps"]
    tl = int(day_ind["tl"])
    i = -1
    for k, s in enumerate(stamps):
        if s <= ts:
            i = k
        else:
            break
    if i < tl:
        return None
    rsi = day_ind["rsi"][i]
    heat = day_ind["heat"][i]
    gap = day_ind["macd_gap"][i]
    if rsi is None or heat is None:
        return None
    rsi_r = _rising(day_ind["rsi"], i, tl)
    rsi_f = _falling(day_ind["rsi"], i, tl)
    exh_r = _rising(day_ind["heat"], i, tl)
    exh_f = _falling(day_ind["heat"], i, tl)
    gap_r = _rising(day_ind["macd_gap"], i, tl)
    gap_f = _falling(day_ind["macd_gap"], i, tl)
    gap_prev = day_ind["macd_gap"][i - tl] if i >= tl else None
    thr = float(day_ind["rte_threshold"])
    overbought = heat >= (100.0 - thr)
    return {
        "i": i,
        "rsi": float(rsi),
        "rsi_rising": rsi_r,
        "rsi_falling": rsi_f,
        "exh": float(heat),
        "exh_rising": exh_r,
        "exh_falling": exh_f,
        "overbought": overbought,
        "macd_gap": gap,
        "macd_gap_prev": gap_prev,
        "macd_gap_rising": gap_r,
        "macd_gap_falling": gap_f,
        "close": float(day_ind["closes"][i]),
    }


def forward_mtm_pct(
    day_ind: dict,
    i: int,
    ts: float,
    horizon_min: float,
) -> float | None:
    """30m MTM %; require ≥50% of horizon covered (rsi_counterfactual rule)."""
    stamps = day_ind["stamps"]
    closes = day_ind["closes"]
    horizon = timedelta(minutes=float(horizon_min))
    target = ts + horizon.total_seconds()
    j = None
    for k in range(i + 1, len(stamps)):
        if stamps[k] <= target:
            j = k
        else:
            break
    if j is None or stamps[j] - stamps[i] < horizon.total_seconds() * 0.5:
        return None
    c0 = closes[i]
    if c0 == 0:
        return None
    return (closes[j] - c0) / c0 * 100.0


# ── gate ─────────────────────────────────────────────────────────────────────

def gate_keep(cell: dict, ind: dict) -> tuple[bool, str]:
    """Apply research gate model. Missing required reading → fail closed."""
    rsi = ind.get("rsi")
    if rsi is None:
        return False, "no_rsi"
    exh = ind.get("exh")
    if exh is None:
        return False, "no_exh"

    rsi_max = cell.get("rsi_max")
    if rsi_max is not None:
        try:
            if float(rsi) > float(rsi_max):
                return False, "rsi_extended"
        except (TypeError, ValueError):
            return False, "rsi_extended"

    if bool(cell.get("rsi_require_rising")):
        if ind.get("rsi_rising") is not True:
            return False, "rsi_not_rising"

    # Falling floor (independent of require_rising): block falling RSI above floor.
    try:
        fall_max = float(cell.get("rsi_allow_falling_below") or 0.0)
    except (TypeError, ValueError):
        fall_max = 0.0
    if ind.get("rsi_falling") is True and float(rsi) > fall_max:
        return False, "rsi_falling_above_floor"

    # EXH min/max
    try:
        heat_min = float(cell.get("heat_min") if cell.get("heat_min") is not None else 0.0)
    except (TypeError, ValueError):
        heat_min = 0.0
    try:
        heat_max = float(cell.get("heat_max") if cell.get("heat_max") is not None else 0.0)
    except (TypeError, ValueError):
        heat_max = 0.0
    if float(exh) + 1e-9 < heat_min:
        return False, "heating_too_low"
    if heat_max > 0 and float(exh) + 1e-9 > heat_max:
        return False, "already_extended"

    if bool(cell.get("require_exh_rising")):
        if ind.get("exh_rising") is not True:
            return False, "exh_not_rising"

    # Mistimed heating chase (closest documented rule; no confirm-peak series
    # in this harness — peak ≈ pass RSI).
    if bool(cell.get("mistimed_heat")):
        heating = (
            not bool(ind.get("overbought"))
            and float(exh) + 1e-9 >= heat_min
            and (heat_max <= 0 or float(exh) + 1e-9 <= heat_max)
        )
        if heating:
            try:
                rsi_floor = float(cell.get("mistimed_rsi_min") or 0.0)
            except (TypeError, ValueError):
                rsi_floor = 0.0
            try:
                peak_floor = float(cell.get("mistimed_rsi_peak_min") or 0.0)
            except (TypeError, ValueError):
                peak_floor = 0.0
            peak = float(rsi)  # no confirm window in AB — document as approx
            if rsi_floor > 0 and float(rsi) + 1e-9 >= rsi_floor:
                return False, "mistimed_heat"
            if peak_floor > 0 and peak + 1e-9 >= peak_floor:
                return False, "mistimed_heat"

    if bool(cell.get("require_macd")):
        gap = ind.get("macd_gap")
        if gap is None:
            return False, "no_macd"
        min_gap = cell.get("macd_min_gap")
        if min_gap is not None:
            try:
                if float(gap) < float(min_gap):
                    return False, "macd_gap_low"
            except (TypeError, ValueError):
                return False, "macd_gap_low"

    if bool(cell.get("macd_block_narrowing")):
        if ind.get("macd_gap_falling") is True:
            return False, "macd_gap_narrowing"
        if ind.get("macd_gap_rising") is None and ind.get("macd_gap_falling") is None:
            return False, "macd_gap_dir_unknown"

    return True, "keep"


# ── scoring ──────────────────────────────────────────────────────────────────

def _stats(vals: list[float]) -> dict[str, Any]:
    if not vals:
        return {
            "n": 0, "mean": None, "median": None, "win_pct": None,
        }
    wins = sum(1 for v in vals if v > 0)
    return {
        "n": len(vals),
        "mean": statistics.fmean(vals),
        "median": statistics.median(vals),
        "win_pct": 100.0 * wins / len(vals),
    }


def score_cell(
    cell: dict,
    prepared: list[dict],
    *,
    live_means: dict[str, float] | None,
    min_n: int,
    live: dict,
) -> dict[str, Any]:
    """prepared items: {day, half, mtm, fill_pl, fill_r, ind}."""
    keeps: list[float] = []
    by_half: dict[str, list[float]] = {"A": [], "B": []}
    fill_pl: list[float] = []
    fill_r: list[float] = []
    blocked = 0
    for row in prepared:
        ok, _why = gate_keep(cell, row["ind"])
        if not ok:
            blocked += 1
            continue
        keeps.append(float(row["mtm"]))
        by_half[row["half"]].append(float(row["mtm"]))
        if row.get("fill_pl") is not None:
            fill_pl.append(float(row["fill_pl"]))
        if row.get("fill_r") is not None:
            fill_r.append(float(row["fill_r"]))

    st = _stats(keeps)
    ha = _stats(by_half["A"])
    hb = _stats(by_half["B"])
    live_mean = (live_means or {}).get("all")
    live_a = (live_means or {}).get("A")
    live_b = (live_means or {}).get("B")
    lift = None if st["mean"] is None or live_mean is None else st["mean"] - live_mean
    lift_a = None if ha["mean"] is None or live_a is None else ha["mean"] - live_a
    lift_b = None if hb["mean"] is None or live_b is None else hb["mean"] - live_b
    half_ok = bool(
        st["n"] >= min_n
        and lift_a is not None and lift_b is not None
        and lift_a >= 0.0 and lift_b >= 0.0
    )
    return {
        "id": cell["id"],
        "cell": {k: cell.get(k) for k in ("id",) + CELL_KEYS},
        "n": st["n"],
        "blocked": blocked,
        "mean": st["mean"],
        "median": st["median"],
        "win_pct": st["win_pct"],
        "half_a": ha,
        "half_b": hb,
        "lift_vs_live": lift,
        "lift_a": lift_a,
        "lift_b": lift_b,
        "half_ok": half_ok,
        "fill_n": len(fill_pl),
        "fill_pl_sum": sum(fill_pl) if fill_pl else None,
        "fill_r_mean": statistics.fmean(fill_r) if fill_r else None,
        "complexity": complexity_score(cell, live),
    }


def rank_key(row: dict) -> tuple:
    """Higher is better for sorting (negate complexity)."""
    return (
        1 if row.get("half_ok") else 0,
        row.get("mean") if row.get("mean") is not None else -1e9,
        row.get("median") if row.get("median") is not None else -1e9,
        row.get("win_pct") if row.get("win_pct") is not None else -1e9,
        row.get("n") or 0,
        -int(row.get("complexity") or 0),
    )


# ── prepare arms once ────────────────────────────────────────────────────────

def prepare_arms(
    arms: list[dict],
    *,
    horizon_min: float,
    tl: int,
    rte_threshold: float,
    feed_note: str = "iex",
) -> tuple[list[dict], dict[str, int]]:
    """Fetch bars once per (sym, day); attach recomputed ind + MTM + optional fill."""
    import alpaca_api as aa

    sec_path = ROOT / "config" / "secrets.json"
    sec = json.loads(sec_path.read_text(encoding="utf-8"))
    client = aa.connect_data_client(
        {"api_key": sec["api_key"], "secret_key": sec["secret_key"]})

    by_key: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in arms:
        sym = str(r["symbol"]).upper().strip()
        day = _et_day(float(r["ts"]))
        by_key[(sym, day)].append(r)

    days_sorted = sorted({d for _s, d in by_key})
    half_of = {}
    mid = max(1, len(days_sorted) // 2)
    for i, d in enumerate(days_sorted):
        # First / second half of unique ET days (ab_bench spirit).
        half_of[d] = "A" if i < mid else "B"

    outcomes_ix = load_outcomes_index()
    cache: dict[tuple[str, str], dict | None] = {}
    prepared: list[dict] = []
    skip = defaultdict(int)

    for (sym, day), rows in sorted(by_key.items()):
        if (sym, day) not in cache:
            df = _fetch_day_bars(client, sym, day)
            cache[(sym, day)] = build_day_indicators(
                df, tl=tl, rte_threshold=rte_threshold)
        day_ind = cache[(sym, day)]
        if day_ind is None:
            skip["no_bars"] += len(rows)
            continue
        for r in rows:
            ts = float(r["ts"])
            ind = indicators_at(day_ind, ts)
            if ind is None:
                skip["no_ind"] += 1
                continue
            mtm = forward_mtm_pct(day_ind, ind["i"], ts, horizon_min)
            if mtm is None:
                skip["no_forward"] += 1
                continue
            fill = match_fill(outcomes_ix, sym, day, ts)
            fill_pl = fill_r = None
            if fill:
                for k in ("realized_pl_usd", "realized_pnl", "pnl_usd", "pl_usd"):
                    if fill.get(k) is not None:
                        fill_pl = _finite(fill.get(k))
                        break
                for k in ("realized_r", "r_multiple", "r"):
                    if fill.get(k) is not None:
                        fill_r = _finite(fill.get(k))
                        break
            prepared.append({
                "symbol": sym,
                "day": day,
                "half": half_of[day],
                "ts": ts,
                "mtm": mtm,
                "ind": ind,
                "fill_pl": fill_pl,
                "fill_r": fill_r,
            })

    _ = feed_note
    return prepared, dict(skip)


# ── phase B / C helpers ──────────────────────────────────────────────────────

def _delta_vs_live(cell: dict, live: dict) -> dict:
    return {k: cell.get(k) for k in CELL_KEYS if cell.get(k) != live.get(k)}


def build_phase_b_cells(
    top_rows: list[dict],
    live: dict,
    *,
    max_cells: int = 30,
) -> list[dict]:
    """Cross RSI/EXH/MACD deltas from top Phase A cells (excl LIVE)."""
    families = {"rsi": [], "exh": [], "macd": []}
    rsi_keys = {
        "rsi_max", "rsi_require_rising", "rsi_allow_falling_below",
        "mistimed_heat", "mistimed_rsi_min", "mistimed_rsi_peak_min",
    }
    exh_keys = {"heat_min", "heat_max", "require_exh_rising"}
    macd_keys = {"require_macd", "macd_min_gap", "macd_block_narrowing"}

    for row in top_rows:
        cell = row["cell"]
        d = _delta_vs_live(cell, live)
        if not d:
            continue
        if any(k in d for k in rsi_keys):
            families["rsi"].append({k: d[k] for k in d if k in rsi_keys})
        if any(k in d for k in exh_keys):
            families["exh"].append({k: d[k] for k in d if k in exh_keys})
        if any(k in d for k in macd_keys):
            families["macd"].append({k: d[k] for k in d if k in macd_keys})

    # Dedup family overlays
    def _dedupe(overlays: list[dict]) -> list[dict]:
        seen = set()
        out = []
        for o in overlays:
            fp = tuple(sorted(o.items()))
            if fp in seen:
                continue
            seen.add(fp)
            out.append(o)
        return out

    for k in families:
        families[k] = _dedupe(families[k])
        if not families[k]:
            families[k] = [{}]  # identity

    cells: list[dict] = [merge_cell(live, {"id": "LIVE"})]
    seen_fp = {cell_fingerprint(cells[0])}
    n = 0
    for ro in families["rsi"]:
        for eo in families["exh"]:
            for mo in families["macd"]:
                if not ro and not eo and not mo:
                    continue
                overlay = {**ro, **eo, **mo}
                cid = "B_" + "_".join(
                    f"{k}={overlay[k]}" for k in sorted(overlay))[:80]
                cell = merge_cell(live, {**overlay, "id": cid})
                fp = cell_fingerprint(cell)
                if fp in seen_fp:
                    continue
                seen_fp.add(fp)
                cells.append(cell)
                n += 1
                if n >= max_cells:
                    return cells
    return cells


def neighbors(cell: dict, live: dict, round_i: int) -> list[dict]:
    """± one step on numerics; flip bools that differ from LIVE or are on."""
    out: list[dict] = []
    steps = {
        "rsi_max": [None, 50.0, 55.0, 60.0, 65.0, 70.0],
        "rsi_allow_falling_below": [0.0, 5.0, 10.0, 15.0, 20.0, 25.0],
        "heat_min": [10.0, 20.0, 30.0, 40.0, 50.0],
        "heat_max": [0.0, 60.0, 65.0, 70.0, 75.0, 80.0],
        "macd_min_gap": [None, 0.005, 0.01, 0.015, 0.02, 0.03],
        "mistimed_rsi_min": [48.0, 50.0, 52.0, 55.0],
        "mistimed_rsi_peak_min": [50.0, 52.0, 55.0, 58.0],
    }

    def _add(overlay: dict, tag: str) -> None:
        c = merge_cell(live, {**{k: cell.get(k) for k in CELL_KEYS},
                              **overlay, "id": f"C{round_i}_{tag}"})
        out.append(c)

    for key, ladder in steps.items():
        cur = cell.get(key)
        # Find nearest index
        idxs = []
        for i, v in enumerate(ladder):
            if v is None and cur is None:
                idxs.append(i)
            elif v is not None and cur is not None and abs(float(v) - float(cur)) < 1e-12:
                idxs.append(i)
        if not idxs:
            # snap to closest numeric
            if cur is None:
                idxs = [0] if ladder[0] is None else []
            else:
                nums = [(i, v) for i, v in enumerate(ladder) if v is not None]
                if nums:
                    i_best = min(nums, key=lambda iv: abs(float(iv[1]) - float(cur)))[0]
                    idxs = [i_best]
        for i0 in idxs:
            for j in (i0 - 1, i0 + 1):
                if 0 <= j < len(ladder) and ladder[j] != cur:
                    _add({key: ladder[j]}, f"{key}_{ladder[j]}")

    for bk in ("rsi_require_rising", "mistimed_heat", "require_exh_rising",
               "require_macd", "macd_block_narrowing"):
        # Flip if part of winning family (differs from live) or currently on.
        if cell.get(bk) != live.get(bk) or bool(cell.get(bk)):
            _add({bk: not bool(cell.get(bk))}, f"flip_{bk}")

    # Dedupe
    seen = set()
    uniq = []
    for c in out:
        fp = cell_fingerprint(c)
        if fp in seen or fp == cell_fingerprint(cell):
            continue
        seen.add(fp)
        uniq.append(c)
    return uniq


# ── I/O / report ─────────────────────────────────────────────────────────────

def ensure_out() -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUT_DIR


def append_results(rows: list[dict], phase: str) -> None:
    path = ensure_out() / "results.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        for r in rows:
            payload = dict(r)
            payload["phase"] = phase
            payload["ts_written"] = datetime.now(tz=ET).isoformat()
            fh.write(json.dumps(payload, default=str) + "\n")


def print_table(rows: list[dict], live_row: dict | None, title: str) -> None:
    print(f"\n=== {title} ===")
    if live_row:
        print(
            f"LIVE  n={live_row.get('n')}  mean="
            f"{_fmt(live_row.get('mean'))}%  med={_fmt(live_row.get('median'))}%  "
            f"win={_fmt(live_row.get('win_pct'), 1)}%  half_ok={live_row.get('half_ok')}"
        )
    print(
        f"{'id':<28} {'n':>5} {'mean':>8} {'med':>8} {'win%':>6} "
        f"{'half':>5} {'lift':>8}"
    )
    for r in rows[:10]:
        print(
            f"{str(r.get('id')):<28} {r.get('n') or 0:>5} "
            f"{_fmt(r.get('mean')):>8} {_fmt(r.get('median')):>8} "
            f"{_fmt(r.get('win_pct'), 1):>6} "
            f"{'Y' if r.get('half_ok') else 'n':>5} "
            f"{_fmt(r.get('lift_vs_live')):>8}"
        )


def _fmt(v: Any, nd: int = 3) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):+.{nd}f}" if nd == 3 else f"{float(v):.{nd}f}"
    except (TypeError, ValueError):
        return str(v)


def print_champion(champ: dict | None, search: dict, reason: str = "") -> None:
    print("\n=== CHAMPION (advisory — do not auto-apply) ===")
    if not champ:
        print(f"No champion. {reason}")
        return
    cell = champ["cell"]
    print(f"id={cell.get('id')}  n={champ.get('n')}  mean={_fmt(champ.get('mean'))}%  "
          f"lift={_fmt(champ.get('lift_vs_live'))}%  half_ok={champ.get('half_ok')}")
    print("bot_config keys:")
    for k, v in to_bot_config(cell, search).items():
        print(f"  {k}: {v}")


def write_summary(
    *,
    search: dict,
    live_row: dict,
    ranked: list[dict],
    champ: dict | None,
    runner: dict | None,
    meta: dict,
) -> None:
    path = ensure_out() / "summary.md"
    lines = [
        "# Entry RSI × EXH × MACD AB — summary",
        "",
        f"Window: `{meta.get('day_from')}` → `{meta.get('day_to')}` (ET inclusive).",
        f"Arms prepared: {meta.get('n_prepared')}  "
        f"(skipped: {meta.get('skip')}).",
        "",
        "**Advisory only** — does not write `config/bot_config.json`.",
        "",
        "## LIVE control",
        f"- n={live_row.get('n')} mean={_fmt(live_row.get('mean'))}% "
        f"med={_fmt(live_row.get('median'))}% win={_fmt(live_row.get('win_pct'), 1)}%",
        "",
        "## Top 10",
        "",
        "| id | n | mean | med | win% | half_ok | lift |",
        "|---|---:|---:|---:|---:|---|---:|",
    ]
    for r in ranked[:10]:
        lines.append(
            f"| {r.get('id')} | {r.get('n')} | {_fmt(r.get('mean'))} | "
            f"{_fmt(r.get('median'))} | {_fmt(r.get('win_pct'), 1)} | "
            f"{r.get('half_ok')} | {_fmt(r.get('lift_vs_live'))} |"
        )
    lines.append("")
    lines.append("## Champion")
    if champ and champ.get("half_ok"):
        lines.append(f"- id: `{champ['cell'].get('id')}`")
        lines.append(f"- mean MTM: {_fmt(champ.get('mean'))}%  "
                     f"lift vs LIVE: {_fmt(champ.get('lift_vs_live'))}%")
        lines.append("- bot_config:")
        for k, v in to_bot_config(champ["cell"], search).items():
            lines.append(f"  - `{k}`: `{v}`")
    else:
        lines.append(f"- none ({meta.get('champ_reason') or 'half-split / min_n'})")
    if runner:
        lines.append("")
        lines.append(f"## Runner-up: `{runner['cell'].get('id')}` "
                     f"mean={_fmt(runner.get('mean'))}%")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def pick_champion(
    ranked: list[dict],
    *,
    min_n: int,
    live_id: str = "LIVE",
) -> tuple[dict | None, dict | None, str]:
    eligible = [
        r for r in ranked
        if r.get("id") != live_id
        and r.get("half_ok")
        and (r.get("n") or 0) >= min_n
        and (r.get("lift_vs_live") or 0) >= 0
    ]
    if not eligible:
        return None, None, "no cell beat LIVE on both day-halves with min_n"
    eligible.sort(key=rank_key, reverse=True)
    champ = eligible[0]
    runner = eligible[1] if len(eligible) > 1 else None
    return champ, runner, ""


# ── phases ───────────────────────────────────────────────────────────────────

def run_cells(
    cells: list[dict],
    prepared: list[dict],
    search: dict,
    live: dict,
    *,
    min_n: int,
) -> tuple[list[dict], dict]:
    live_cell = merge_cell(live, {"id": "LIVE"})
    # Score LIVE first for lifts.
    live_prep_means: dict[str, float] = {}
    live_keeps_all: list[float] = []
    live_keeps_h: dict[str, list[float]] = {"A": [], "B": []}
    for row in prepared:
        ok, _ = gate_keep(live_cell, row["ind"])
        if not ok:
            continue
        live_keeps_all.append(row["mtm"])
        live_keeps_h[row["half"]].append(row["mtm"])
    if live_keeps_all:
        live_prep_means["all"] = statistics.fmean(live_keeps_all)
    for h in ("A", "B"):
        if live_keeps_h[h]:
            live_prep_means[h] = statistics.fmean(live_keeps_h[h])

    results = []
    for cell in cells:
        results.append(score_cell(
            cell, prepared, live_means=live_prep_means, min_n=min_n, live=live))
    results.sort(key=rank_key, reverse=True)
    live_row = next((r for r in results if r["id"] == "LIVE"), results[0])
    return results, live_row


def cmd_summarize() -> int:
    path = OUT_DIR / "results.jsonl"
    if not path.exists():
        print(f"no results at {path}")
        return 1
    rows = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rows.append(json.loads(line))
    if not rows:
        print("empty results")
        return 1
    # Latest phase-all-ish: prefer last written per id
    by_id = {}
    for r in rows:
        by_id[r["id"]] = r
    ranked = sorted(by_id.values(), key=rank_key, reverse=True)
    live_row = by_id.get("LIVE") or ranked[0]
    print_table(ranked, live_row, "summarize (latest per id)")
    champ_path = OUT_DIR / "champion.json"
    if champ_path.exists():
        champ = json.loads(champ_path.read_text(encoding="utf-8"))
        search = load_search()
        print_champion(champ if champ.get("half_ok") else None, search,
                       reason=champ.get("reject_reason", ""))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Entry RSI×EXH×MACD AB harness (advisory champion only)")
    ap.add_argument("--from", dest="day_from", default="2026-09-01")
    ap.add_argument("--to", dest="day_to", default=None,
                    help="inclusive ET day (default: today ET)")
    ap.add_argument("--phase", default="all",
                    choices=["A", "B", "C", "all", "summarize"])
    ap.add_argument("--summarize", action="store_true",
                    help="print latest results / champion (alias --phase summarize)")
    ap.add_argument("--search", type=Path, default=SEARCH_PATH)
    ap.add_argument("--shadow", type=Path, default=SHADOW)
    ap.add_argument("--min-n", type=int, default=None)
    ap.add_argument("--horizon-min", type=float, default=None)
    ap.add_argument("--reset-results", action="store_true",
                    help="truncate benchmarks/entry_ab/results.jsonl before run")
    args = ap.parse_args()

    if args.summarize or args.phase == "summarize":
        return cmd_summarize()

    search = load_search(args.search)
    live = search["live"]
    min_n = int(args.min_n if args.min_n is not None else search.get("min_n", 25))
    horizon = float(
        args.horizon_min if args.horizon_min is not None
        else search.get("horizon_min", 30.0))
    tl = int(search.get("trend_lookback", 2))
    rte_thr = float(search.get("rte_threshold", 20.0))
    eps = float(search.get("epsilon_mtm_pct", 0.02))
    day_to = args.day_to or datetime.now(tz=ET).strftime("%Y-%m-%d")
    day_from = args.day_from

    ensure_out()
    if args.reset_results:
        (OUT_DIR / "results.jsonl").write_text("", encoding="utf-8")

    print(f"entry_arm_ab  {day_from}..{day_to}  phase={args.phase}  "
          f"min_n={min_n}  horizon={horizon:g}m")
    print("ADVISORY ONLY — does not write bot_config / live arms.")

    arms = load_arms(day_from, day_to, args.shadow)
    print(f"shadow arm_ok in window: {len(arms)}")
    if not arms:
        print("no arms — abort")
        return 1

    prepared, skip = prepare_arms(
        arms, horizon_min=horizon, tl=tl, rte_threshold=rte_thr)
    thin = False
    if len(prepared) < min_n:
        thin_n = int(search.get("min_n_thin", 15))
        if len(prepared) >= thin_n:
            print(f"FLAG: prepared n={len(prepared)} < min_n={min_n}; "
                  f"lowering floor to {thin_n}")
            min_n = thin_n
            thin = True
        else:
            print(f"too few prepared arms ({len(prepared)}); abort")
            return 1
    print(f"prepared keeps-eligible arms: {len(prepared)}  skip={skip}")

    phases = []
    if args.phase == "all":
        phases = ["A", "B", "C"]
    else:
        phases = [args.phase]

    live_row = None
    ranked: list[dict] = []
    champ = None
    runner = None
    champ_reason = ""

    if "A" in phases:
        cells_a = phase_a_cells(search)
        print(f"\nPhase A cells: {len(cells_a)}")
        ranked, live_row = run_cells(
            cells_a, prepared, search, live, min_n=min_n)
        append_results(ranked, "A")
        print_table(ranked, live_row, "Phase A top 10")
        top = [r for r in ranked if r["id"] != "LIVE"][: int(search.get("phase_b_top_k", 5))]
        (OUT_DIR / "phase_a_top.json").write_text(
            json.dumps({"top": top, "live": live_row, "thin_min_n": thin},
                       indent=2, default=str),
            encoding="utf-8")
        champ, runner, champ_reason = pick_champion(ranked, min_n=min_n)

    if "B" in phases:
        top_path = OUT_DIR / "phase_a_top.json"
        if not top_path.exists() and not ranked:
            print("Phase B needs Phase A results (phase_a_top.json) — run A first")
            return 1
        if not ranked:
            payload = json.loads(top_path.read_text(encoding="utf-8"))
            top = payload.get("top") or []
            # Re-score only B cells; still need LIVE means from prepared.
        else:
            top = [r for r in ranked if r["id"] != "LIVE"][: int(
                search.get("phase_b_top_k", 5))]
        cells_b = build_phase_b_cells(
            top, live, max_cells=int(search.get("phase_b_max_cells", 30)))
        print(f"\nPhase B cells: {len(cells_b)}")
        ranked_b, live_row = run_cells(
            cells_b, prepared, search, live, min_n=min_n)
        append_results(ranked_b, "B")
        # Merge best-known by id
        by_id = {r["id"]: r for r in ranked}
        for r in ranked_b:
            prev = by_id.get(r["id"])
            if prev is None or rank_key(r) > rank_key(prev):
                by_id[r["id"]] = r
        ranked = sorted(by_id.values(), key=rank_key, reverse=True)
        print_table(ranked_b, live_row, "Phase B top 10")
        champ, runner, champ_reason = pick_champion(ranked, min_n=min_n)

    if "C" in phases:
        if champ is None:
            # Try from current ranked / disk
            if not ranked:
                # score LIVE-only baseline
                ranked, live_row = run_cells(
                    [merge_cell(live, {"id": "LIVE"})],
                    prepared, search, live, min_n=min_n)
            champ, runner, champ_reason = pick_champion(ranked, min_n=min_n)
        if champ is None:
            print(f"\nPhase C skipped — no champion seed ({champ_reason})")
        else:
            max_rounds = int(search.get("refine_max_rounds", 3))
            cur = champ
            for rd in range(1, max_rounds + 1):
                neigh = neighbors(cur["cell"], live, rd)
                print(f"\nPhase C round {rd}: {len(neigh)} neighbors of {cur['id']}")
                if not neigh:
                    break
                ranked_n, live_row = run_cells(
                    [merge_cell(live, {"id": "LIVE"}), *neigh],
                    prepared, search, live, min_n=min_n)
                append_results(ranked_n, f"C{rd}")
                improved = None
                for r in ranked_n:
                    if r["id"] == "LIVE":
                        continue
                    if not r.get("half_ok"):
                        continue
                    if (r.get("n") or 0) < min_n:
                        continue
                    # Must beat champion mean MTM on both halves by ε
                    ra = (r.get("half_a") or {}).get("mean")
                    rb = (r.get("half_b") or {}).get("mean")
                    ca = (cur.get("half_a") or {}).get("mean")
                    cb = (cur.get("half_b") or {}).get("mean")
                    if (
                        ra is not None and rb is not None
                        and ca is not None and cb is not None
                        and ra >= ca + eps and rb >= cb + eps
                    ):
                        if improved is None or rank_key(r) > rank_key(improved):
                            improved = r
                    # Alternate: fill-R path when enough fills
                    elif (
                        (r.get("fill_n") or 0) >= 8
                        and (cur.get("fill_n") or 0) >= 8
                        and r.get("fill_r_mean") is not None
                        and cur.get("fill_r_mean") is not None
                        and r["fill_r_mean"] >= cur["fill_r_mean"] + 0.02
                        and r.get("half_ok")
                    ):
                        if improved is None or rank_key(r) > rank_key(improved):
                            improved = r
                by_id = {r["id"]: r for r in ranked}
                for r in ranked_n:
                    prev = by_id.get(r["id"])
                    if prev is None or rank_key(r) > rank_key(prev):
                        by_id[r["id"]] = r
                ranked = sorted(by_id.values(), key=rank_key, reverse=True)
                if improved is None:
                    print(f"  no neighbor beat champion on both halves by ε={eps}")
                    break
                print(f"  promote {improved['id']} mean={_fmt(improved.get('mean'))}")
                cur = improved
            champ, runner, champ_reason = pick_champion(ranked, min_n=min_n)
            if champ is None:
                champ = cur if cur.get("half_ok") else None

    if live_row is None and ranked:
        live_row = next((r for r in ranked if r["id"] == "LIVE"), ranked[0])

    # Prefer simpler within ε of complex winner
    if champ and ranked:
        near = [
            r for r in ranked
            if r.get("half_ok")
            and r.get("id") != "LIVE"
            and (r.get("n") or 0) >= min_n
            and r.get("mean") is not None
            and champ.get("mean") is not None
            and abs(r["mean"] - champ["mean"]) <= eps
        ]
        if near:
            near.sort(key=lambda r: (r.get("complexity", 99), -rank_key(r)[1]))
            if near[0]["id"] != champ["id"] and near[0]["complexity"] < champ.get(
                    "complexity", 99):
                print(f"prefer simpler cell {near[0]['id']} within ε of {champ['id']}")
                runner = champ
                champ = near[0]

    meta = {
        "day_from": day_from,
        "day_to": day_to,
        "n_prepared": len(prepared),
        "skip": skip,
        "min_n": min_n,
        "thin_min_n": thin,
        "champ_reason": champ_reason,
    }
    if champ and champ.get("half_ok"):
        payload = {
            **champ,
            "bot_config": to_bot_config(champ["cell"], search),
            "advisory": True,
            "note": "Do not auto-apply; Phase 1 / Sep 18 checkpoint.",
            "meta": meta,
        }
        (OUT_DIR / "champion.json").write_text(
            json.dumps(payload, indent=2, default=str), encoding="utf-8")
        if runner:
            (OUT_DIR / "runner_up.json").write_text(
                json.dumps({**runner, "bot_config": to_bot_config(
                    runner["cell"], search)}, indent=2, default=str),
                encoding="utf-8")
        print_champion(champ, search)
    else:
        reject = {
            "half_ok": False,
            "reject_reason": champ_reason or "no eligible champion",
            "live": live_row,
            "meta": meta,
            "advisory": True,
        }
        (OUT_DIR / "champion.json").write_text(
            json.dumps(reject, indent=2, default=str), encoding="utf-8")
        print_champion(None, search, reason=reject["reject_reason"])

    if live_row and ranked:
        write_summary(
            search=search, live_row=live_row, ranked=ranked,
            champ=champ if champ and champ.get("half_ok") else None,
            runner=runner, meta=meta)

    print(f"\nWrote under {OUT_DIR}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
