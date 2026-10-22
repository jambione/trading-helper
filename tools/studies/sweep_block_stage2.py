#!/usr/bin/env python3
"""Sweeps and blocks, stage 2: combinations. Spec: docs/studies/sweep_block_prereg.json "stage2" (registered before any
outcome was fetched).

Runs only after stage 1 (sweep_block.py --score) and only for a hypothesis whose stage-1 primary lift is > 0 in both
halves. COMBO = the hypothesis's IN label AND a cell condition (C1 location, C2 context, C3 follow-through inside the
window); scored as COMBO vs every other entry with that hypothesis's stage-1 stratified statistic, outcomes and halves.
Refuses without --skeptic-ready and a stage-1 result written by the same sweep_block code.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pickle
import statistics
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sweep_block as SB  # noqa: E402

OF, H, S = SB.OF, SB.H, SB.S
RTH0 = 570
SPLIT_S = 30.0
CELLS = ("C1_LOCATION", "C2_CONTEXT", "C3_FOLLOW_THROUGH")
FLOOR_IN, FLOOR_DAYS = 300, 20


# ── conditions (pure) ────────────────────────────────────────────────────────

def bar_context(rows_np, m: int) -> dict:
    """From SIP 1-minute bars [minute, o, h, l, c, v]: the day's high over RTH bars before minute m - 1, VWAP over RTH
    bars through m - 1 (close x volume), and the first RTH bar's open."""
    hi, pv, v, open0 = None, 0.0, 0.0, None
    for x in rows_np:
        mm = int(x[0])
        if mm < RTH0:
            continue
        if open0 is None and mm <= m - 1:
            open0 = float(x[1])
        if mm <= m - 2:
            hi = float(x[2]) if hi is None else max(hi, float(x[2]))
        if mm <= m - 1:
            pv += float(x[4]) * float(x[5])
            v += float(x[5])
    return {"hod_before": hi, "vwap": pv / v if v > 0 else None, "open0": open0}


def window_extras(tick: dict, T: float) -> dict:
    """Highest kept print in [T - 60 s, T); volume-weighted price of buy-signed ISO prints and of buy-signed blocks
    (late TRF excluded) in [T - 60 s, T - 30 s)."""
    qs = OF.clean_quotes(tick["quotes"])
    sg = SB.sign_trades(tick["trades"], qs, T)
    vis = [x for x in sg if not (x[5] == SB.TRF and x[0] >= T - SB.TRF_LATE_S)]     # stage2_amended (3)
    max_px = max((x[1] for x in vis), default=None)
    early = [x for x in sg if x[0] < T - SPLIT_S and x[3] == 1]
    iso = [x for x in early if SB.ISO in x[4]]
    blk = [x for x in early if x[1] * x[2] >= SB.BLOCK_USD and not (x[5] == SB.TRF and x[0] >= T - SB.TRF_LATE_S)]

    def vwp(xs):
        v = sum(x[2] for x in xs)
        return sum(x[1] * x[2] for x in xs) / v if v > 0 else None
    return {"max_px": max_px, "early_buy_iso_vwp": vwp(iso), "early_buy_blk_vwp": vwp(blk)}


def conditions(r: dict, hyp: str) -> dict:
    """None = not computable (no SIP bars / no prints): the entry leaves that cell (stage2_amended (4))."""
    bars = r.get("has_bars", True)
    c1 = (None if not bars or r.get("max_px") is None or r.get("hod_before") is None
          else r["max_px"] >= r["hod_before"])
    c2 = (None if not bars or r.get("vwap") is None or r.get("open0") is None
          else (r["mid_T"] > r["vwap"] and r["mid_T"] > r["open0"]))
    early = r.get("early_buy_iso_vwp" if hyp == "H1" else "early_buy_blk_vwp")
    c3 = early is not None and r["mid_T"] >= early
    return {"C1_LOCATION": c1, "C2_CONTEXT": c2, "C3_FOLLOW_THROUGH": c3}


def combo_label(r: dict, hyp: str, cell: str) -> str | None:
    if r["g"][hyp] is None:
        return None
    c = r["cond"][hyp][cell]
    if c is None:
        return None
    return "IN" if (r["g"][hyp] == "IN" and c) else "OUT"


def guard_labels(hyp: str, cell: str) -> tuple:
    """stage2_amended (1): (a) COMBO vs IN-without-condition; (b) COMBO vs condition-without-IN."""
    def a(r):
        c = r["cond"][hyp][cell]
        if r["g"][hyp] != "IN" or c is None:
            return None
        return "IN" if c else "OUT"

    def b(r):
        c = r["cond"][hyp][cell]
        if r["g"][hyp] is None or not c:
            return None
        return "IN" if r["g"][hyp] == "IN" else "OUT"
    return a, b


# ── grading (reuses stage 1's statistic) ─────────────────────────────────────

def relabel(rows: list[dict], hyp: str, cell: str) -> list[dict]:
    """Copies whose g[hyp] is the COMBO label, so stage 1's primary / cost / drop-3 code applies unchanged."""
    out = []
    for r in rows:
        x = dict(r)
        x["g"] = dict(r["g"])
        x["g"][hyp] = combo_label(r, hyp, cell)
        out.append(x)
    return out


def t_crit(days: int, k: int) -> float | None:
    return S.t_ppf(1 - 0.025 / k, days - 1) if days >= 2 else None


def grade_cell(rows: list[dict], hyp: str, cell: str, k: int = 6) -> dict:
    rs = relabel(rows, hyp, cell)
    mu, t, days, ub = SB.primary(rs, hyp)
    dd, dn = SB.drop3(rs, hyp)
    la, lb = guard_labels(hyp, cell)
    ga = SB.strat_lift(rows, hyp, SB.PRIMARY_STRAT[hyp], "y", label=la)
    gb = SB.strat_lift(rows, hyp, SB.PRIMARY_STRAT[hyp], "y", label=lb)
    return {"lift": mu, "t": t, "days": days, "ub": ub, "t_crit": t_crit(days, k),
            "n_combo": sum(1 for r in rs if r["g"][hyp] == "IN" and r.get("y") is not None),
            "cost_check": SB.cost_check(rs, hyp), "latency_lag1": SB.primary(rs, hyp, "lag1_y")[0],
            "drop_days": dd, "drop_names": dn, "nameday": SB.nameday_lift(rs, hyp),
            "bar_entry_lift": SB.primary(rs, hyp, "bar_y")[0], "bar_placebo_lift": SB.primary(rs, hyp, "placebo_y")[0],
            "guard_vs_in_only": ga[:3], "guard_vs_condition_only": gb[:3]}


def cell_verdict(g: dict) -> str:
    if g["t"] is not None and g["t"] <= -2:
        return "FAIL"
    if (g["n_combo"] < FLOOR_IN or g["days"] < FLOOR_DAYS or g["t"] is None
            or g["guard_vs_in_only"][2] < FLOOR_DAYS or g["guard_vs_condition_only"][2] < FLOOR_DAYS):
        return "UNDERPOWERED"
    ok = (g["guard_vs_in_only"][0] is not None and g["guard_vs_in_only"][0] >= SB.LIFT_BAR
          and g["guard_vs_condition_only"][0] is not None and g["guard_vs_condition_only"][0] >= SB.LIFT_BAR
          and g["nameday"] is not None and g["nameday"] > 0
          and g["bar_placebo_lift"] is not None and g["bar_placebo_lift"] < SB.PLACEBO_MAX
          and g["bar_entry_lift"] is not None and g["bar_entry_lift"] - g["bar_placebo_lift"] >= SB.LIFT_BAR
          and g["lift"] >= SB.LIFT_BAR and g["t"] >= g["t_crit"] and g["cost_check"] is not None and g["cost_check"] > 0
          and g["latency_lag1"] is not None and g["latency_lag1"] >= 0
          and g["drop_days"] is not None and g["drop_days"] >= 0 and g["drop_names"] is not None and g["drop_names"] >= 0)
    return "PASS" if ok else "NOT_PASSED"


def final(halves: list[str], ubs: list, days: list[int]) -> str:
    if "FAIL" in halves:
        return "FAIL"
    if all(h == "PASS" for h in halves):
        return "PASS_STAGE2 (needs the warehouse_confirm run)"
    if all(n >= 2 for n in days) and all(u is not None and u < SB.LIFT_BAR for u in ubs):
        return "FAIL"
    return "UNDERPOWERED"


def qualifying(stage1: dict) -> list[str]:
    """The registered gate: the stage-1 primary lift > 0 in both halves (scored hypotheses only)."""
    out = []
    for hyp in ("H1", "H2"):
        gs = stage1.get("graded", {}).get(hyp)
        if gs and all(gs[h]["lift"] is not None and gs[h]["lift"] > 0 for h in ("A", "B")):
            out.append(hyp)
    return out


def module_shas() -> dict:
    m = SB.module_shas()
    m["sweep_block_stage2"] = hashlib.sha256(open(os.path.abspath(__file__), "rb").read()).hexdigest()
    return m


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skeptic-ready", default="")
    a = ap.parse_args()
    if not a.skeptic_ready:
        raise SystemExit("needs --skeptic-ready")
    stage1 = SB.jload("result.json")
    if not stage1 or stage1.get("module_sha256") != SB.module_shas():
        raise SystemExit("no stage-1 result from this sweep_block code")
    hyps = qualifying(stage1)
    res = {"gate": {"qualifying": hyps, "stage1_verdicts": stage1["verdicts"]}, "cells": {}}
    if not hyps:
        res["note"] = "no hypothesis passed the stage-2 gate (lift > 0 in both halves); stage 2 not run"
        print(json.dumps(res, indent=1))
        SB.write("stage2.json", {**res, "module_sha256": module_shas()})
        return
    smp = SB.jload("sample.json")["entries"]
    rows, _st = SB.build_rows(smp, with_outcomes=True)
    cuts = SB.assign(rows)
    cnt = SB.jload("count.json")
    if json.loads(json.dumps(cuts)) != cnt["cuts"] or sorted(OF.entry_key(r) for r in rows) != cnt["row_keys"]:
        raise SystemExit("rows or cuts differ from stage 1's count step")
    by_day = defaultdict(list)
    for r in rows:
        by_day[r["day"]].append(r)
    for day, rs in by_day.items():
        raw = H.load_raw("sip", day)
        for r in rs:
            r["has_bars"] = r["sym"] in raw
            r.update(bar_context(raw[r["sym"]], r["m"]) if r["has_bars"] else {})
            tick = pickle.load(open(os.path.join(SB.TICK_DIR, OF.entry_key(r) + ".pkl"), "rb"))
            r.update(window_extras(tick, OF.decision_ts(day, r["m"])))
            r["cond"] = {h: conditions(r, h) for h in ("H1", "H2")}
    k = 6                                                  # stage2_amended (5): fixed
    res["no_bars"] = sum(1 for r in rows if not r["has_bars"])
    for hyp in hyps:
        for cell in CELLS:
            gs = {h: grade_cell([r for r in rows if r["half"] == h], hyp, cell, k) for h in ("A", "B")}
            hv = {h: cell_verdict(gs[h]) for h in ("A", "B")}
            res["cells"][f"{hyp} {cell}"] = {"graded": gs, "halves": hv,
                                             "verdict": final(list(hv.values()), [gs[h]["ub"] for h in ("A", "B")],
                                                              [gs[h]["days"] for h in ("A", "B")]),
                                             "condition_none": {h: sum(1 for r in rows if r["half"] == h and
                                                                       r["cond"][hyp][cell] is None) for h in ("A", "B")},
                                             "condition_share": {h: sum(1 for r in rows if r["half"] == h and
                                                                        r["cond"][hyp][cell]) /
                                                                 max(1, sum(1 for r in rows if r["half"] == h))
                                                                 for h in ("A", "B")}}
    res["k"], res["skeptic_ready"], res["module_sha256"] = k, a.skeptic_ready, module_shas()
    SB.write("stage2.json", res)
    print(json.dumps(res, indent=1, default=str))


if __name__ == "__main__":
    main()
