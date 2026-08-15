#!/usr/bin/env python3
"""
sim_edge_mode_ab.py — A/B simulation of edge modes on a real session's data.

Compares **exhaustion_scalp** (overbought arm + left_overbought exit) vs
**continuation** (Option A: heating|overbought arm, no left_overbought,
T1 / stop / dead_trade) using that day's shadow + outcomes.

SECTIONS
  1. ARM GATE     in-zone shadow samples: who would arm under each mode,
                  and 30m forward return after those samples.
  2. EXIT A/B     for live left_overbought outcomes, counterfactual hold
                  on later shadow prices (T1 / stop / dead_trade / session_end).
  3. TRENDING     seed + inclusion A/B: legacy EXT-only vs post-08-11 path
                  (no hard EXT, score/day-chg/rvol claims, looser rvol floor).
  4. UNIT SMOKE   synthetic %R checks that rules fire as coded.

HONESTY
  • Shadow is poll samples, not a full tape; prices are closer to ask than bid.
  • Position samples end when the live desk exited; exit counterfactual extends
    with later shadow quotes for the same symbol.
  • T1/stop only count if shadow printed those prices.
  • trending_stocks.json is a point-in-time snapshot (often EOD), not the full
    intraday feed — seed counts are directional, not exact morning membership.
  • Does not re-simulate wash thrash, re-entry loops, or broker partials.
  • One day is a hypothesis, not a verdict.

USAGE
    venv/bin/python tools/sim_edge_mode_ab.py
    venv/bin/python tools/sim_edge_mode_ab.py --day 2026-08-11
    venv/bin/python tools/sim_edge_mode_ab.py --day 2026-08-11 --json

Nightly ranking of this overlay (and the others) is tools/replay_ab.py.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import ai_entry_watch as ew  # noqa: E402
from ai_paths import find_report_file, resolve_report_dir  # noqa: E402


def _report(name: str) -> Path:
    return find_report_file(name) or (resolve_report_dir() / name)


def day_of(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d")
    except Exception:
        return ""


def mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def scalp_cfg() -> dict[str, Any]:
    return {
        "ai_edge_mode": "exhaustion_scalp",
        "ai_watch_exhaustion_rules": True,
        "ai_watch_require_exhaustion_data": True,
        "rte_threshold": 20,
        "ai_watch_exhaustion_heat_min_pct": 50.0,
    }


def cont_cfg() -> dict[str, Any]:
    return {
        "ai_edge_mode": "continuation",
        "ai_exit_left_overbought": False,
        "ai_watch_exhaustion_rules": True,
        "ai_watch_require_exhaustion_data": True,
        "rte_threshold": 20,
        "ai_watch_exhaustion_heat_min_pct": 50.0,
    }


def rec_from_shadow(r: dict) -> dict:
    """Minimal record for exhaustion_allows_buy / exhaustion_exit_now."""
    ind: dict[str, Any] = {}
    pctr = r.get("pctr")
    if pctr is not None:
        try:
            ind["pctr"] = float(pctr)
        except (TypeError, ValueError):
            pass
    st = str(r.get("exhaustion_state") or "")
    if st == "heating":
        ind["pctr_rising"] = True
        ind["pctr_falling"] = False
    elif st == "cooling":
        ind["pctr_rising"] = False
        ind["pctr_falling"] = True
    elif st == "overbought":
        ind["pctr_rising"] = False
        ind["pctr_falling"] = False
    return {
        "symbol": r.get("symbol"),
        "indicator": ind,
        "exhaustion": r.get("exhaustion"),
    }


def load_jsonl_day(path: Path, day: str, ts_keys: tuple[str, ...] = ("ts",)) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        for k in ts_keys:
            if day_of(o.get(k)) == day:
                out.append(o)
                break
    return out


def fwd_ret(
    by_sym: dict[str, list[dict]],
    sym: str,
    ts: Any,
    horizon_sec: float = 1800.0,
) -> float | None:
    rows = by_sym.get(sym) or []
    px0 = px1 = None
    t0 = float(ts)
    for r in rows:
        t = float(r.get("ts") or 0)
        p = r.get("price")
        if p is None:
            continue
        if t >= t0 and px0 is None:
            px0 = float(p)
        if t >= t0:
            px1 = float(p)
        if t >= t0 + horizon_sec:
            break
    if px0 and px1 and px0 > 0:
        return (px1 - px0) / px0 * 100.0
    return None


def section_arm(
    shadow: list[dict],
    day: str,
) -> dict[str, Any]:
    sc, cc = scalp_cfg(), cont_cfg()
    in_zone = [r for r in shadow if r.get("in_zone") is True]
    by_sym: dict[str, list[dict]] = defaultdict(list)
    for r in shadow:
        by_sym[str(r.get("symbol") or "")].append(r)
    for s in by_sym:
        by_sym[s].sort(key=lambda x: float(x.get("ts") or 0))

    arm_scalp: Counter[str] = Counter()
    arm_cont: Counter[str] = Counter()
    both = only_cont = only_scalp = neither = 0
    fwd_s: list[float] = []
    fwd_c: list[float] = []
    fwd_only_c: list[float] = []

    for r in in_zone:
        rec = rec_from_shadow(r)
        ok_s, why_s = ew.exhaustion_allows_buy(rec, sc)
        ok_c, why_c = ew.exhaustion_allows_buy(rec, cc)
        arm_scalp[f"ARM:{why_s}" if ok_s else why_s] += 1
        arm_cont[f"ARM:{why_c}" if ok_c else why_c] += 1
        fr = fwd_ret(by_sym, str(r.get("symbol") or ""), r.get("ts"))
        if ok_s and ok_c:
            both += 1
            if fr is not None:
                fwd_s.append(fr)
                fwd_c.append(fr)
        elif ok_c and not ok_s:
            only_cont += 1
            if fr is not None:
                fwd_only_c.append(fr)
                fwd_c.append(fr)
        elif ok_s and not ok_c:
            only_scalp += 1
            if fr is not None:
                fwd_s.append(fr)
        else:
            neither += 1

    return {
        "day": day,
        "in_zone_n": len(in_zone),
        "both": both,
        "continuation_only": only_cont,
        "scalp_only": only_scalp,
        "neither": neither,
        "scalp_reasons": arm_scalp.most_common(12),
        "cont_reasons": arm_cont.most_common(12),
        "fwd_scalp_n": len(fwd_s),
        "fwd_scalp_mean_pct": mean(fwd_s),
        "fwd_cont_n": len(fwd_c),
        "fwd_cont_mean_pct": mean(fwd_c),
        "fwd_cont_only_n": len(fwd_only_c),
        "fwd_cont_only_mean_pct": mean(fwd_only_c),
    }


def section_exit(
    outcomes: list[dict],
    shadow: list[dict],
    *,
    dead_min: float = 20.0,
    dead_mfe_r: float = 0.25,
) -> dict[str, Any]:
    sh: dict[str, list[dict]] = defaultdict(list)
    for o in shadow:
        if o.get("price") is None:
            continue
        sh[str(o.get("symbol") or "")].append(o)
    for s in sh:
        sh[s].sort(key=lambda x: float(x["ts"]))

    rows: list[dict[str, Any]] = []
    for o in outcomes:
        if o.get("close_reason") != "left_overbought":
            continue
        sym = str(o.get("symbol") or "")
        entry = float(o.get("entry_price") or 0)
        stop = float(o.get("stop_price") or 0)
        target = float(o.get("target_1") or 0)
        exit_px = float(o.get("exit_price") or o.get("exit_price_approx") or 0)
        live_r = o.get("realized_r_multiple")
        live_pl = o.get("realized_pl_usd")
        t_entry = float(o.get("entry_time") or o.get("ts") or 0)
        if not entry or not exit_px:
            continue
        risk = (entry - stop) if stop and stop < entry else entry * 0.05
        if risk <= 0:
            risk = entry * 0.05
        if live_r is None:
            live_r = (exit_px - entry) / risk
        live_r = float(live_r)

        ticks = [r for r in sh.get(sym, []) if float(r["ts"]) >= t_entry - 1]
        if not ticks:
            rows.append({
                "symbol": sym,
                "live_r": live_r,
                "live_pl": live_pl,
                "cont_r": live_r,
                "cont_exit": "no_shadow",
                "delta_r": 0.0,
                "mfe_r": 0.0,
                "hold_min": 0.0,
            })
            continue

        mfe = 0.0
        cont_r = 0.0
        cont_why = "session_end"
        hold_s = 0.0
        for r in ticks:
            t = float(r["ts"])
            px = float(r["price"])
            hold_s = t - t_entry
            rr = (px - entry) / risk
            mfe = max(mfe, rr)
            if stop and px <= stop:
                cont_r, cont_why = (stop - entry) / risk, "stop"
                break
            if target and px >= target:
                cont_r, cont_why = (target - entry) / risk, "target_1"
                break
            if hold_s >= dead_min * 60 and mfe < dead_mfe_r and px <= entry * 1.001:
                cont_r, cont_why = rr, "dead_trade"
                break
        else:
            px = float(ticks[-1]["price"])
            cont_r = (px - entry) / risk
            cont_why = "session_end"
            hold_s = float(ticks[-1]["ts"]) - t_entry

        d = cont_r - live_r
        rows.append({
            "symbol": sym,
            "live_r": live_r,
            "live_pl": live_pl,
            "cont_r": cont_r,
            "cont_exit": cont_why,
            "delta_r": d,
            "mfe_r": mfe,
            "hold_min": hold_s / 60.0,
        })

    good = [r for r in rows if r["cont_exit"] != "no_shadow"]
    dollar = 0.0
    for r in good:
        if r["live_pl"] is not None and abs(r["live_r"]) > 1e-9:
            dollar += (float(r["live_pl"]) / float(r["live_r"])) * r["delta_r"]

    other = [
        {
            "symbol": o.get("symbol"),
            "close_reason": o.get("close_reason"),
            "realized_r": o.get("realized_r_multiple"),
            "realized_pl": o.get("realized_pl_usd"),
        }
        for o in outcomes
        if o.get("close_reason") != "left_overbought"
    ]

    return {
        "left_overbought_rows": rows,
        "n_scored": len(good),
        "sum_live_r": sum(r["live_r"] for r in good) if good else 0.0,
        "sum_cont_r": sum(r["cont_r"] for r in good) if good else 0.0,
        "sum_delta_r": sum(r["delta_r"] for r in good) if good else 0.0,
        "mean_delta_r": mean([r["delta_r"] for r in good]),
        "cont_better_n": sum(1 for r in good if r["delta_r"] > 1e-9),
        "approx_delta_usd": dollar,
        "cont_exit_mix": dict(Counter(r["cont_exit"] for r in good)),
        "other_outcomes": other,
        "dead_min": dead_min,
        "dead_mfe_r": dead_mfe_r,
    }


def section_smoke() -> dict[str, Any]:
    heat = {
        "symbol": "X",
        "indicator": {"pctr": -30.0, "pctr_rising": True, "pctr_falling": False},
    }
    left = {
        "symbol": "X",
        "indicator": {"pctr": -40.0},
        "exh_was_overbought": True,
    }
    sc, cc = scalp_cfg(), cont_cfg()
    return {
        "heating_scalp": list(ew.exhaustion_allows_buy(heat, sc)),
        "heating_cont": list(ew.exhaustion_allows_buy(heat, cc)),
        "left_ob_scalp_exit": list(ew.exhaustion_exit_now(dict(left), sc)),
        "left_ob_cont_exit": list(ew.exhaustion_exit_now(dict(left), cc)),
    }


def _norm_rvol(raw: Any) -> float | None:
    try:
        if raw is None:
            return None
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if v > 10.0:
        v = v / 100.0
    return v


def _pct_or_none(raw: Any) -> float | None:
    try:
        if raw is None:
            return None
        return float(raw)
    except (TypeError, ValueError):
        return None


def _trending_rows_from_file(path: Path) -> tuple[list[dict], dict[str, Any]]:
    """Load equity rows from trending_stocks.json; return (rows, meta)."""
    meta: dict[str, Any] = {"path": str(path), "exists": path.exists()}
    if not path.exists():
        return [], meta
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        meta["error"] = str(e)
        return [], meta
    meta["updated"] = raw.get("updated") or raw.get("last_ok")
    if meta["updated"] is not None:
        try:
            meta["updated_day"] = day_of(meta["updated"])
        except Exception:
            meta["updated_day"] = None
    rows_out: list[dict] = []
    for r in raw.get("rows") or []:
        if not isinstance(r, dict):
            continue
        if r.get("is_crypto") is True:
            continue
        if r.get("is_equity") is False:
            continue
        sym = str(r.get("symbol") or r.get("ticker") or "").upper().strip()
        if not sym or not sym[0].isalpha():
            continue
        try:
            score = float(r.get("trending_score", r.get("score") or 0) or 0)
        except (TypeError, ValueError):
            score = 0.0
        pct = _pct_or_none(r.get("pct_change"))
        # file may store fraction (0.12) or percent (12); shadow uses percent.
        if pct is not None and abs(pct) < 1.0 and pct != 0:
            # Heuristic: |pct|<1 often means fraction for large moves only —
            # keep as-is if looks like already percent from screener (e.g. 0.5%).
            pass
        look = str(r.get("look_reason") or "").strip().upper()
        rvol = _norm_rvol(r.get("rvol") if r.get("rvol") is not None else r.get("rvol_raw"))
        rows_out.append({
            "symbol": sym,
            "score": score,
            "pct_change": pct,
            "look_reason": look,
            "rvol": rvol,
            "price": r.get("price"),
        })
    meta["n_equity"] = len(rows_out)
    return rows_out, meta


def seed_legacy_ext_only(
    row: dict,
    *,
    min_score: float = 5.0,
    min_rvol: float = 2.0,
) -> tuple[bool, str]:
    """Pre-08-11 trending seed: EXT + score>min + green day + thin-rvol skip."""
    if row.get("look_reason") == "WASH":
        return False, "wash"
    if row.get("look_reason") != "EXT":
        return False, "not_ext"
    if float(row.get("score") or 0) <= min_score:
        return False, "low_score"
    pct = row.get("pct_change")
    if pct is None or float(pct) <= 0:
        return False, "not_uptrend"
    rvol = row.get("rvol")
    if rvol is not None and min_rvol > 0 and float(rvol) < min_rvol:
        return False, "thin_rvol"
    return True, "seed_legacy"


def seed_new_path(
    row: dict,
    *,
    min_score: float = 5.0,
    min_pct: float = 15.0,
    min_rvol: float = 1.5,
    require_ext: bool = False,
) -> tuple[bool, str]:
    """Post-08-11 trending seed (matches ai_entry_watch.desk_candidate_rows)."""
    if row.get("look_reason") == "WASH":
        return False, "wash"
    if require_ext and row.get("look_reason") != "EXT":
        return False, "not_ext"
    pct = row.get("pct_change")
    if pct is not None and float(pct) <= 0:
        return False, "not_uptrend"
    rvol = row.get("rvol")
    if rvol is not None and min_rvol > 0 and float(rvol) < min_rvol:
        return False, "thin_rvol"
    score_ok = float(row.get("score") or 0) > min_score
    pct_ok = pct is not None and float(pct) >= min_pct
    rvol_ok = rvol is not None and min_rvol > 0 and float(rvol) >= min_rvol
    if not (score_ok or pct_ok or rvol_ok):
        return False, "no_claim"
    why = []
    if score_ok:
        why.append("score")
    if pct_ok:
        why.append("pct")
    if rvol_ok:
        why.append("rvol")
    if row.get("look_reason") == "EXT":
        why.append("ext")
    return True, "+".join(why) if why else "seed_new"


def section_trending(
    day: str,
    shadow: list[dict],
    rejects: list[dict],
    *,
    trending_path: Path | None = None,
    max_seed: int = 20,
) -> dict[str, Any]:
    """A/B legacy EXT-only trending seed vs new conversion path."""
    path = trending_path or (_ROOT / "trending_stocks.json")
    file_rows, meta = _trending_rows_from_file(path)

    legacy_seed: list[dict] = []
    new_seed: list[dict] = []
    legacy_fail: Counter[str] = Counter()
    new_fail: Counter[str] = Counter()
    for r in file_rows:
        ok_l, why_l = seed_legacy_ext_only(r)
        if ok_l:
            legacy_seed.append(r)
        else:
            legacy_fail[why_l] += 1
        ok_n, why_n = seed_new_path(r)
        if ok_n:
            new_seed.append({**r, "seed_why": why_n})
        else:
            new_fail[why_n] += 1

    # Cap like desk_candidate_rows (first N in file order).
    legacy_capped = legacy_seed[:max_seed]
    new_capped = new_seed[:max_seed]
    legacy_syms = {r["symbol"] for r in legacy_capped}
    new_syms = {r["symbol"] for r in new_capped}
    only_new = sorted(new_syms - legacy_syms)
    only_legacy = sorted(legacy_syms - new_syms)
    both_seed = sorted(legacy_syms & new_syms)

    # Inclusion A/B on new-seed rows
    cfg_old = {
        "ai_watch_require_uptrend": True,
        "ai_watch_require_indicators": False,
        "ai_watch_min_price": 1.0,
        "ai_min_dollar_volume": 0.0,
        "ai_watch_min_rvol": 2.0,
        "ai_watch_trending_min_rvol": 2.0,
        "ai_watch_require_look_ext": True,
    }
    cfg_new = {
        "ai_watch_require_uptrend": True,
        "ai_watch_require_indicators": False,
        "ai_watch_min_price": 1.0,
        "ai_min_dollar_volume": 0.0,
        "ai_watch_min_rvol": 2.0,
        "ai_watch_trending_min_rvol": 1.5,
        "ai_watch_require_look_ext": False,
    }

    def as_watch_row(r: dict) -> dict:
        crit = ["trending"]
        if float(r.get("score") or 0) > 5:
            crit.append("score")
        if r.get("pct_change") is not None and float(r["pct_change"]) > 0:
            crit.append("uptrend")
        if r.get("look_reason") == "EXT":
            crit.append("ext")
        if r.get("rvol") is not None:
            crit.append("rvol")
        return {
            "symbol": r["symbol"],
            "source": "trending",
            "price": r.get("price") or 10.0,
            "pct_change": r.get("pct_change"),
            "rvol": r.get("rvol"),
            "look_reason": r.get("look_reason") or "",
            "criteria": crit,
            "score": r.get("score"),
        }

    incl_old_ok: list[str] = []
    incl_new_ok: list[str] = []
    incl_old_why: Counter[str] = Counter()
    incl_new_why: Counter[str] = Counter()
    # Evaluate inclusion on union of seeds
    for r in file_rows:
        if r["symbol"] not in legacy_syms and r["symbol"] not in new_syms:
            continue
        row = as_watch_row(r)
        ok_o, _m, why_o = ew.passes_inclusion(row, cfg_old, indicators=None)
        ok_n, _m, why_n = ew.passes_inclusion(row, cfg_new, indicators=None)
        if r["symbol"] in legacy_syms:
            if ok_o:
                incl_old_ok.append(r["symbol"])
            else:
                incl_old_why[why_o or "fail"] += 1
        if r["symbol"] in new_syms:
            if ok_n:
                incl_new_ok.append(r["symbol"])
            else:
                incl_new_why[why_n or "fail"] += 1

    # Live day: shadow trending / arms / fills from reasons in trades if any
    shadow_tr = sorted({
        str(r.get("symbol") or "")
        for r in shadow if r.get("source") == "trending"
    })
    shadow_tr_armed = sorted({
        str(r.get("symbol") or "")
        for r in shadow
        if r.get("source") == "trending" and r.get("arm_ok")
    })
    reject_tr = Counter()
    reject_syms: set[str] = set()
    for r in rejects:
        if r.get("source") != "trending":
            continue
        reject_syms.add(str(r.get("symbol") or ""))
        reject_tr[str(r.get("reason") or "?")] += 1

    # Forward return for only_new symbols if they appear on any shadow later
    by_sym: dict[str, list[dict]] = defaultdict(list)
    for r in shadow:
        by_sym[str(r.get("symbol") or "")].append(r)
    for s in by_sym:
        by_sym[s].sort(key=lambda x: float(x.get("ts") or 0))

    only_new_fwd: list[tuple[str, float | None]] = []
    for sym in only_new:
        rows = by_sym.get(sym) or []
        if not rows:
            only_new_fwd.append((sym, None))
            continue
        # first sample of the day
        fr = fwd_ret(by_sym, sym, rows[0].get("ts"), 1800.0)
        only_new_fwd.append((sym, fr))

    return {
        "day": day,
        "file_meta": meta,
        "file_equity_n": len(file_rows),
        "legacy_seed_n": len(legacy_capped),
        "legacy_seed_syms": sorted(legacy_syms),
        "legacy_fail": legacy_fail.most_common(12),
        "new_seed_n": len(new_capped),
        "new_seed_syms": sorted(new_syms),
        "new_seed_why": Counter(r.get("seed_why") or "?" for r in new_capped).most_common(12),
        "new_fail": new_fail.most_common(12),
        "both_seed": both_seed,
        "only_new": only_new,
        "only_legacy": only_legacy,
        "incl_old_ok_n": len(set(incl_old_ok)),
        "incl_old_ok": sorted(set(incl_old_ok)),
        "incl_old_fail": incl_old_why.most_common(8),
        "incl_new_ok_n": len(set(incl_new_ok)),
        "incl_new_ok": sorted(set(incl_new_ok)),
        "incl_new_fail": incl_new_why.most_common(8),
        "live_shadow_trending_n": len(shadow_tr),
        "live_shadow_trending": shadow_tr,
        "live_shadow_armed": shadow_tr_armed,
        "live_reject_trending_syms": len(reject_syms),
        "live_reject_reasons": reject_tr.most_common(10),
        "only_new_fwd_30m": only_new_fwd,
        "only_new_fwd_mean_pct": mean([
            f for _, f in only_new_fwd if f is not None
        ]),
        "max_seed": max_seed,
    }


def print_report(
    day: str,
    arm: dict,
    exit_: dict,
    smoke: dict,
    trending: dict | None = None,
) -> None:
    print("=" * 72)
    print(f"SIMULATION A/B — {day}")
    print("  exhaustion_scalp  vs  continuation (Option A)")
    print("=" * 72)

    print(f"\n1) ARM GATE on in-zone shadow samples  n={arm['in_zone_n']}")
    print(f"   both arm:          {arm['both']}")
    print(f"   continuation only: {arm['continuation_only']}  "
          f"(earlier entries Option A unlocks)")
    print(f"   scalp only:        {arm['scalp_only']}")
    print(f"   neither:           {arm['neither']}")
    print(f"   scalp arm reasons (top): {arm['scalp_reasons'][:8]}")
    print(f"   cont  arm reasons (top): {arm['cont_reasons'][:8]}")
    ms, mc, mo = (
        arm["fwd_scalp_mean_pct"],
        arm["fwd_cont_mean_pct"],
        arm["fwd_cont_only_mean_pct"],
    )
    if ms is not None:
        print(f"   30m fwd% when scalp would arm:  "
              f"n={arm['fwd_scalp_n']} mean={ms:.3f}%")
    else:
        print("   scalp fwd: n/a")
    if mc is not None:
        print(f"   30m fwd% when cont would arm:   "
              f"n={arm['fwd_cont_n']} mean={mc:.3f}%")
    else:
        print("   cont fwd: n/a")
    if mo is not None:
        print(f"   30m fwd% cont-ONLY arms:        "
              f"n={arm['fwd_cont_only_n']} mean={mo:.3f}%")
    else:
        print("   cont-only fwd: n/a")

    print(f"\n2) EXIT A/B — hold after left_overbought "
          f"(dead_trade {exit_['dead_min']:g}m / MFE<{exit_['dead_mfe_r']})")
    print(f"   {'sym':6} {'live_R':>8} {'live$':>9} {'cont_R':>8} "
          f"{'cont_exit':14} {'ΔR':>7} {'MFE':>7} {'hold_m':>7}")
    for r in exit_["left_overbought_rows"]:
        pl = r["live_pl"]
        pl_s = f"{pl:.2f}" if pl is not None else "—"
        print(
            f"   {r['symbol']:6} {r['live_r']:+8.3f} {pl_s:>9} "
            f"{r['cont_r']:+8.3f} {r['cont_exit']:14} "
            f"{r['delta_r']:+7.3f} {r['mfe_r']:+7.3f} {r['hold_min']:7.1f}"
        )
    n = exit_["n_scored"]
    if n:
        print("-" * 72)
        print(f"   n={n} left_overbought trades re-scored")
        print(f"   sum live R:  {exit_['sum_live_r']:+.3f}")
        print(f"   sum cont R:  {exit_['sum_cont_r']:+.3f}")
        print(f"   sum ΔR:      {exit_['sum_delta_r']:+.3f}")
        m = exit_["mean_delta_r"]
        if m is not None:
            print(f"   mean ΔR:     {m:+.3f}")
        print(f"   cont better: {exit_['cont_better_n']} / {n}")
        print(f"   approx Δ$ (scale by live $/R): "
              f"${exit_['approx_delta_usd']:+.2f}")
        print(f"   cont exit mix: {exit_['cont_exit_mix']}")
    else:
        print("   (no left_overbought outcomes that day)")

    if exit_["other_outcomes"]:
        print("\n   Other live exits (not re-scored by LOB hold):")
        for o in exit_["other_outcomes"]:
            print(
                f"     {str(o.get('symbol') or '?'):6} "
                f"{str(o.get('close_reason') or '?'):20} "
                f"R={o.get('realized_r')} pl={o.get('realized_pl')}"
            )

    if trending is not None:
        print(f"\n3) TRENDING SEED / ADMIT A/B")
        fm = trending.get("file_meta") or {}
        print(f"   file: {fm.get('path')}  equity_rows={trending.get('file_equity_n')}")
        if fm.get("updated_day"):
            print(f"   snapshot day≈{fm.get('updated_day')}  "
                  f"(point-in-time, not full session feed)")
        print(f"   legacy EXT-only seed:  n={trending['legacy_seed_n']}  "
              f"{trending['legacy_seed_syms']}")
        print(f"     fail reasons: {trending['legacy_fail']}")
        print(f"   new conversion seed:   n={trending['new_seed_n']}  "
              f"{trending['new_seed_syms']}")
        print(f"     seed claims: {trending['new_seed_why']}")
        print(f"     fail reasons: {trending['new_fail']}")
        print(f"   both: {len(trending['both_seed'])}  "
              f"only_new: {len(trending['only_new'])}  "
              f"only_legacy: {len(trending['only_legacy'])}")
        if trending["only_new"]:
            print(f"   only_new symbols: {trending['only_new']}")
            mo = trending.get("only_new_fwd_mean_pct")
            meas = sum(1 for _, f in trending.get("only_new_fwd_30m") or [] if f is not None)
            if mo is not None:
                print(f"   only_new 30m shadow fwd (when symbol on book): "
                      f"n={meas} mean={mo:.3f}%")
            else:
                print("   only_new 30m shadow fwd: n/a (not on shadow that day)")
        print(f"   inclusion ok  legacy-seed set: {trending['incl_old_ok_n']}  "
              f"{trending['incl_old_ok']}")
        print(f"   inclusion ok  new-seed set:    {trending['incl_new_ok_n']}  "
              f"{trending['incl_new_ok']}")
        print(f"   live shadow trending: n={trending['live_shadow_trending_n']}  "
              f"{trending['live_shadow_trending']}")
        print(f"   live shadow arm_ok:   {trending['live_shadow_armed']}")
        print(f"   live trending rejects: "
              f"syms={trending['live_reject_trending_syms']}  "
              f"{trending['live_reject_reasons']}")

    print("\n4) UNIT SMOKE")
    print(f"   heating 70% scalp: {tuple(smoke['heating_scalp'])}")
    print(f"   heating 70% cont:  {tuple(smoke['heating_cont'])}")
    print(f"   left OB scalp exit:{tuple(smoke['left_ob_scalp_exit'])}")
    print(f"   left OB cont exit: {tuple(smoke['left_ob_cont_exit'])}")

    print("\nHONESTY")
    print("  Shadow is poll samples, not a full tape; exit uses ask-like prices.")
    print("  T1/stop only if shadow printed those prices.")
    print("  Trending file is a snapshot, not the full intraday list.")
    print("  Does not re-simulate wash thrash or re-entry.")
    print("  One day is a hypothesis for live trading, not a verdict.")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="A/B simulation: exhaustion_scalp vs continuation (Option A)",
    )
    ap.add_argument(
        "--day",
        default=None,
        help="ET calendar day YYYY-MM-DD (default: latest day with outcomes "
             "or shadow; else today local)",
    )
    ap.add_argument(
        "--dead-min",
        type=float,
        default=20.0,
        help="dead_trade minutes for continuation exit sim (default 20)",
    )
    ap.add_argument(
        "--dead-mfe-r",
        type=float,
        default=0.25,
        help="max MFE (R) to still count as dead trade (default 0.25)",
    )
    ap.add_argument(
        "--trending-file",
        default=None,
        help="path to trending_stocks.json (default: repo root)",
    )
    ap.add_argument(
        "--no-trending",
        action="store_true",
        help="skip section 3 (trending seed A/B)",
    )
    ap.add_argument("--json", action="store_true", help="machine-readable dump")
    args = ap.parse_args()

    shadow_path = _report("shadow.jsonl")
    outcomes_path = _report("outcomes.jsonl")
    rejects_path = _report("rejects.jsonl")

    day = args.day
    if not day:
        # Prefer most recent day present in outcomes, else shadow, else today.
        days: Counter[str] = Counter()
        for p, keys in (
            (outcomes_path, ("exit_time", "ts", "entry_time")),
            (shadow_path, ("ts",)),
        ):
            if not p.exists():
                continue
            for line in p.read_text().splitlines()[-5000:]:
                if not line.strip():
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for k in keys:
                    d = day_of(o.get(k))
                    if d:
                        days[d] += 1
                        break
            if days:
                break
        day = days.most_common(1)[0][0] if days else datetime.now().strftime("%Y-%m-%d")

    shadow = load_jsonl_day(shadow_path, day, ("ts",))
    outcomes = load_jsonl_day(
        outcomes_path, day, ("exit_time", "ts", "entry_time"),
    )
    rejects = load_jsonl_day(rejects_path, day, ("ts",))

    arm = section_arm(shadow, day)
    exit_ = section_exit(
        outcomes, shadow,
        dead_min=float(args.dead_min),
        dead_mfe_r=float(args.dead_mfe_r),
    )
    smoke = section_smoke()
    trending = None
    if not args.no_trending:
        tpath = Path(args.trending_file) if args.trending_file else None
        trending = section_trending(day, shadow, rejects, trending_path=tpath)

    payload = {
        "day": day,
        "arm": arm,
        "exit": exit_,
        "trending": trending,
        "smoke": smoke,
        "paths": {
            "shadow": str(shadow_path),
            "outcomes": str(outcomes_path),
            "rejects": str(rejects_path),
        },
    }

    if args.json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        print_report(day, arm, exit_, smoke, trending=trending)
        if not shadow:
            print(f"WARNING: no shadow rows for {day} ({shadow_path})",
                  file=sys.stderr)
        if not outcomes:
            print(f"WARNING: no outcomes for {day} ({outcomes_path})",
                  file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
