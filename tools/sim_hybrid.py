#!/usr/bin/env python3
"""
sim_hybrid.py — end-to-end sim of the HYBRID edge mode.

    ai_edge_mode: exhaustion_scalp     # overbought-only arm
    ai_exit_left_overbought: false     # but keep the continuation exit

The 08-11 A/B split cleanly: continuation's *exit* change earned +1.814R over 8
trades, while its *arm* widening more than doubled entries at half the forward
return. The heat_min sweep then showed no threshold rescues the heating tail —
the good heating arms sit at %R 55-65 and the bad ones at 65-80, so raising the
floor keeps the wrong half. The hybrid keeps the half that paid.

SECTIONS
  1. ARM      three-way gate comparison (scalp / continuation / hybrid) over
              in-zone shadow samples. Hybrid should equal scalp exactly.
  2. ADMIT    closes the open inference from the A/B: did each left_overbought
              trade ACTUALLY pass an overbought-only gate near entry, or was it
              armed on heating? Trades that fail never happen under hybrid.
  3. BOOK     three books priced side by side — live (scalp arm + LOB exit),
              continuation (wide arm + no LOB), hybrid (narrow arm + no LOB).

HONESTY
  • Admission is reconstructed from shadow polls near entry, not from the live
    arm decision — polls are sparse, so a trade can look unarmable simply
    because no poll landed in the window. Both a strict (closest-poll) and a
    loose (any-poll-in-window) reading are reported; they disagree, and that
    disagreement is the error bar.
  • Exit counterfactual is section_exit's, with all its limits: T1/stop only
    count if shadow printed those prices, shadow is ask-like, and wash thrash
    and re-entry are not modelled.
  • n=8 trades. This is one day. A hypothesis, not a verdict.

Nightly ranking of this overlay (and the others) is tools/replay_ab.py.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "tools"))

import sim_edge_mode_ab as sim

import ai_entry_watch as ew


def hybrid_cfg() -> dict[str, Any]:
    return {
        "ai_edge_mode": "exhaustion_scalp",
        "ai_exit_left_overbought": False,
        "ai_watch_exhaustion_rules": True,
        "ai_watch_require_exhaustion_data": True,
        "rte_threshold": 20,
        "ai_watch_exhaustion_heat_min_pct": 50.0,
    }


def section_arm3(shadow: list[dict]) -> dict[str, Any]:
    """Three-way arm gate comparison. Hybrid must match scalp exactly."""
    sc, cc, hc = sim.scalp_cfg(), sim.cont_cfg(), hybrid_cfg()
    in_zone = [r for r in shadow if r.get("in_zone") is True]

    by_sym: dict[str, list[dict]] = defaultdict(list)
    for r in shadow:
        by_sym[str(r.get("symbol") or "")].append(r)
    for s in by_sym:
        by_sym[s].sort(key=lambda x: float(x.get("ts") or 0))

    n_s = n_c = n_h = 0
    fwd_s: list[float] = []
    fwd_h: list[float] = []
    mismatch = 0
    for r in in_zone:
        rec = sim.rec_from_shadow(r)
        ok_s, _ = ew.exhaustion_allows_buy(rec, sc)
        ok_c, _ = ew.exhaustion_allows_buy(rec, cc)
        ok_h, _ = ew.exhaustion_allows_buy(rec, hc)
        n_s += ok_s
        n_c += ok_c
        n_h += ok_h
        if ok_h != ok_s:
            mismatch += 1
        fr = sim.fwd_ret(by_sym, str(r.get("symbol") or ""), r.get("ts"))
        if fr is not None:
            if ok_s:
                fwd_s.append(fr)
            if ok_h:
                fwd_h.append(fr)
    return {
        "in_zone_n": len(in_zone),
        "scalp_arms": n_s,
        "cont_arms": n_c,
        "hybrid_arms": n_h,
        "hybrid_vs_scalp_mismatch": mismatch,
        "fwd_scalp_mean": sim.mean(fwd_s),
        "fwd_hybrid_mean": sim.mean(fwd_h),
        "fwd_scalp_n": len(fwd_s),
        "fwd_hybrid_n": len(fwd_h),
    }


def admits(
    sh: dict[str, list[dict]],
    sym: str,
    t_entry: float,
    cfg: dict,
    *,
    lookback_s: float = 300.0,
    lookahead_s: float = 60.0,
) -> dict[str, Any]:
    """Would *cfg* have armed this symbol around its live entry?

    Returns both readings: `loose` (any poll in the window arms) and `strict`
    (the poll closest to entry arms). Sparse polling makes neither definitive.
    """
    rows = [
        r for r in sh.get(sym, [])
        if t_entry - lookback_s <= float(r.get("ts") or 0) <= t_entry + lookahead_s
    ]
    if not rows:
        return {"loose": None, "strict": None, "n_polls": 0,
                "pctr_at_entry": None, "state_at_entry": None}

    loose = False
    for r in rows:
        ok, _ = ew.exhaustion_allows_buy(sim.rec_from_shadow(r), cfg)
        loose = loose or ok

    closest = min(rows, key=lambda r: abs(float(r.get("ts") or 0) - t_entry))
    rec = sim.rec_from_shadow(closest)
    strict, _ = ew.exhaustion_allows_buy(rec, cfg)
    return {
        "loose": loose,
        "strict": strict,
        "n_polls": len(rows),
        "pctr_at_entry": ew.exhaustion_pct(rec),
        "state_at_entry": ew.exhaustion_state(rec, cfg),
    }


def section_book(
    outcomes: list[dict],
    shadow: list[dict],
    exit_res: dict[str, Any],
    *,
    lookback_s: float,
) -> dict[str, Any]:
    """Price the three books over the left_overbought trades."""
    sh: dict[str, list[dict]] = defaultdict(list)
    for o in shadow:
        if o.get("price") is None:
            continue
        sh[str(o.get("symbol") or "")].append(o)
    for s in sh:
        sh[s].sort(key=lambda x: float(x["ts"]))

    entry_t: dict[str, list[float]] = defaultdict(list)
    for o in outcomes:
        if o.get("close_reason") == "left_overbought":
            entry_t[str(o.get("symbol") or "")].append(
                float(o.get("entry_time") or o.get("ts") or 0))
    for s in entry_t:
        entry_t[s].sort()

    hc = hybrid_cfg()
    used: Counter[str] = Counter()
    rows = []
    for r in exit_res["left_overbought_rows"]:
        if r["cont_exit"] == "no_shadow":
            continue
        sym = r["symbol"]
        i = used[sym]
        used[sym] += 1
        t_entry = entry_t[sym][i] if i < len(entry_t[sym]) else 0.0
        a = admits(sh, sym, t_entry, hc, lookback_s=lookback_s)

        live_r = float(r["live_r"])
        dpr = None
        if r["live_pl"] is not None and abs(live_r) > 1e-9:
            dpr = float(r["live_pl"]) / live_r

        rows.append({
            **r,
            "t_entry": t_entry,
            "admit_loose": a["loose"],
            "admit_strict": a["strict"],
            "n_polls": a["n_polls"],
            "pctr_at_entry": a["pctr_at_entry"],
            "state_at_entry": a["state_at_entry"],
            "dollar_per_r": dpr,
        })

    def book(mode: str) -> dict[str, Any]:
        """mode: live | cont | hybrid_loose | hybrid_strict"""
        tot_r = 0.0
        tot_d = 0.0
        n = 0
        for r in rows:
            if mode == "live":
                take, rr = True, r["live_r"]
            elif mode == "cont":
                take, rr = True, r["cont_r"]
            else:
                key = "admit_loose" if mode.endswith("loose") else "admit_strict"
                take, rr = bool(r[key]), r["cont_r"]
            if not take:
                continue
            n += 1
            tot_r += rr
            if r["dollar_per_r"] is not None:
                tot_d += r["dollar_per_r"] * rr
        return {"n": n, "sum_r": tot_r, "sum_usd": tot_d}

    return {
        "rows": rows,
        "books": {m: book(m) for m in
                  ("live", "cont", "hybrid_loose", "hybrid_strict")},
    }


def section_daybook(
    outcomes: list[dict],
    bk_rows: list[dict],
) -> dict[str, Any]:
    """Whole-session P&L by entry state, and the hybrid's effect on it.

    Computed here rather than by hand because the hand version got it wrong:
    reading outcomes.jsonl unfiltered mixes sessions (the file spans 08-04
    onward), which invented a cohort of "blind" entries that belong to other
    days. `outcomes` MUST already be day-filtered by the caller.

    Hybrid rules applied per row:
      heating entry            -> never taken (overbought-only arm gate)
      overbought + LOB close   -> taken, re-priced by the continuation exit
      overbought + other close -> unchanged (LOB was not what closed it)
      adopted orphan           -> unchanged. evaluate_positions keeps
                                  left_overbought enabled for adopted rows even
                                  when the edge mode turns it off globally, so
                                  the hybrid does not re-price them.

    Historical rows label adoption as state "overbought" with no %R value;
    rows written after that fix say "adopted" outright. Both are recognised.
    """
    # Pair LOB outcomes with their counterfactual rows, in section_exit order.
    lob_rows = [r for r in bk_rows]
    lob_i = 0

    live_by_state: dict[str, list[tuple[float, float]]] = defaultdict(list)
    detail: list[dict[str, Any]] = []
    live_total = 0.0
    hybrid_total = 0.0

    for o in outcomes:
        R = o.get("realized_r_multiple")
        pl = o.get("realized_pl_usd")
        sym = str(o.get("symbol") or "")
        state = o.get("entry_exhaustion_state")
        ex = o.get("entry_exhaustion")
        is_lob = o.get("close_reason") == "left_overbought"

        row = lob_rows[lob_i] if (is_lob and lob_i < len(lob_rows)) else None
        if is_lob:
            lob_i += 1

        if R is None or pl is None:
            detail.append({"symbol": sym, "state": state, "skip": "no_result"})
            continue

        R = float(R)
        pl = float(pl)
        adopted = state == "adopted" or (state == "overbought" and ex is None)
        key = "adopted (no %R)" if adopted else str(state or "none")
        live_by_state[key].append((R, pl))
        live_total += pl

        dpr = (pl / R) if abs(R) > 1e-9 else None
        if state == "heating":
            taken, h_usd, why = False, 0.0, "skipped: heating"
        elif adopted:
            taken, h_usd, why = True, pl, "unchanged: adopted keeps LOB"
        elif row is not None and dpr is not None:
            taken, h_usd = True, dpr * float(row["cont_r"])
            why = f"re-priced: {row['cont_exit']}"
        else:
            taken, h_usd, why = True, pl, "unchanged"
        hybrid_total += h_usd
        detail.append({
            "symbol": sym, "state": key, "live_usd": pl,
            "hybrid_usd": h_usd, "taken": taken, "why": why,
        })

    by_state = {
        k: {
            "n": len(v),
            "sum_r": sum(a for a, _ in v),
            "sum_usd": sum(b for _, b in v),
            "win_pct": 100.0 * sum(1 for a, _ in v if a > 0) / len(v),
        }
        for k, v in live_by_state.items()
    }
    return {
        "n_outcomes": len(outcomes),
        "by_state": by_state,
        "detail": detail,
        "live_total_usd": live_total,
        "hybrid_total_usd": hybrid_total,
        "swing_usd": hybrid_total - live_total,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="hybrid edge mode end-to-end sim")
    ap.add_argument("--day", default="2026-08-11")
    ap.add_argument("--dead-min", type=float, default=20.0)
    ap.add_argument("--dead-mfe-r", type=float, default=0.25)
    ap.add_argument("--lookback", type=float, default=300.0,
                    help="seconds before entry to search for an arming poll")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    shadow = sim.load_jsonl_day(sim._report("shadow.jsonl"), args.day, ("ts",))
    outcomes = sim.load_jsonl_day(
        sim._report("outcomes.jsonl"), args.day,
        ("exit_time", "ts", "entry_time"))
    if not shadow:
        print(f"ERROR: no shadow rows for {args.day}", file=sys.stderr)
        return 1

    arm = section_arm3(shadow)
    exit_res = sim.section_exit(
        outcomes, shadow,
        dead_min=args.dead_min, dead_mfe_r=args.dead_mfe_r)
    bk = section_book(outcomes, shadow, exit_res, lookback_s=args.lookback)
    daybook = section_daybook(outcomes, bk["rows"])

    if args.json:
        print(json.dumps(
            {"day": args.day, "arm": arm, "book": bk, "daybook": daybook},
            indent=2, default=str))
        return 0

    print("=" * 78)
    print(f"HYBRID SIM — {args.day}")
    print("  exhaustion_scalp arm gate  +  continuation exit (no left_overbought)")
    print("=" * 78)
    print()

    print("1) ARM GATE — three-way")
    print(f"   in-zone samples:  {arm['in_zone_n']}")
    print(f"   scalp arms:       {arm['scalp_arms']}")
    print(f"   continuation:     {arm['cont_arms']}")
    print(f"   hybrid arms:      {arm['hybrid_arms']}")
    print(f"   hybrid vs scalp mismatches: {arm['hybrid_vs_scalp_mismatch']}"
          f"  {'(identical, as designed)' if not arm['hybrid_vs_scalp_mismatch'] else '(!! UNEXPECTED)'}")
    print(f"   30m fwd scalp:  n={arm['fwd_scalp_n']} "
          f"mean={arm['fwd_scalp_mean']:+.3f}%")
    print(f"   30m fwd hybrid: n={arm['fwd_hybrid_n']} "
          f"mean={arm['fwd_hybrid_mean']:+.3f}%")
    print()

    print("2) ADMISSION — would an overbought-only gate have armed these entries?")
    print(f"   (poll window: -{args.lookback:.0f}s .. +60s around live entry)")
    print(f"   {'sym':<7} {'polls':<6} {'%R@entry':<10} {'state':<12} "
          f"{'strict':<8} {'loose':<7} {'cont_R':<8}")
    for r in bk["rows"]:
        pr = f"{r['pctr_at_entry']:.1f}" if r["pctr_at_entry"] is not None else "n/a"
        print(f"   {r['symbol']:<7} {r['n_polls']:<6} {pr:<10} "
              f"{r['state_at_entry']!s:<12} {r['admit_strict']!s:<8} "
              f"{r['admit_loose']!s:<7} {r['cont_r']:+.3f}")
    print()

    print("3) BOOKS — same 8 trades, three rule sets")
    b = bk["books"]
    print(f"   {'book':<24} {'trades':<8} {'sum R':<10} {'approx $':<10}")
    for key, label in (
        ("live", "live (scalp+LOB exit)"),
        ("cont", "continuation (Opt A)"),
        ("hybrid_loose", "hybrid (loose admit)"),
        ("hybrid_strict", "hybrid (strict admit)"),
    ):
        v = b[key]
        print(f"   {label:<24} {v['n']:<8} {v['sum_r']:+.3f}     "
              f"${v['sum_usd']:+.2f}")
    print()

    print(f"4) WHOLE SESSION — all {daybook['n_outcomes']} outcomes for {args.day}")
    print(f"   {'entry state':<20} {'n':<4} {'sum R':<10} {'sum $':<10} win%")
    for k, v in sorted(daybook["by_state"].items(), key=lambda kv: -kv[1]["n"]):
        print(f"   {k:<20} {v['n']:<4} {v['sum_r']:+9.3f} {v['sum_usd']:+9.2f}"
              f"  {v['win_pct']:.0f}%")
    print()
    print(f"   {'live session':<20} ${daybook['live_total_usd']:+.2f}")
    print(f"   {'hybrid session':<20} ${daybook['hybrid_total_usd']:+.2f}")
    print(f"   {'swing':<20} ${daybook['swing_usd']:+.2f}")
    print()
    for d in daybook["detail"]:
        if d.get("skip"):
            print(f"     {d['symbol']:<7} {d['state']!s:<18} (no result recorded)")
        else:
            print(f"     {d['symbol']:<7} {d['state']:<18} "
                  f"live ${d['live_usd']:+8.2f} -> hybrid ${d['hybrid_usd']:+8.2f}"
                  f"   {d['why']}")
    print()

    print("HONESTY")
    print("  Admission is reconstructed from sparse polls, not the live arm decision;")
    print("  strict vs loose disagreement IS the error bar.")
    print("  Exit counterfactual inherits section_exit's limits (ask-like shadow,")
    print("  T1/stop only if printed, no wash thrash or re-entry).")
    print("  n=8 trades on one day. Hypothesis, not verdict.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
