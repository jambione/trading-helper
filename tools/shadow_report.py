#!/usr/bin/env python3
"""
shadow_report.py — measure the desk's decisions without needing a fill.

outcomes.jsonl only grows when a trade closes. On 2026-08-06 the desk built
530 zones across 31 symbols and filled nothing, so every gate that day was
unmeasurable. claude_reports/shadow.jsonl records one sample per watched
symbol per poll — the decision, and the price that tested it — off the quote
the poller already fetched. This turns those samples into three answers:

  1. SELECTION   forward return after admission, sliced by feature.
                 Does an EXT tag / high rvol / a momentum flag actually
                 precede favourable movement? (roadmap P1-1)

  2. ZONE REACH  how often price ever reaches the zone the desk drew, how
                 long it takes, and whether target or stop came first.
                 A zone nothing touches is not a filter, it is a refusal.
                 (roadmap P1-2 — target vs the 15:50 flatten)

  3. GATE COST   for samples where price WAS in the zone and the arm gate
                 refused: what happened next. This is the only evidence
                 available on whether cm_ok / pctr_ok / cm_rsi_rising /
                 sell_signal earn their place, and it cannot come from
                 outcomes.jsonl by construction — a blocked trade leaves no
                 outcome.

WHAT THIS IS NOT
    Not P&L. There is no entry discipline, no slippage, no partial fill and
    no stop management here — a forward return from the sampled price is not
    what the trade would have made. It answers "was this worth looking at",
    not "would this have paid". Execution belongs in tools/ab_bench.py.

    Not randomized either: the desk chose every symbol it watched. Treat
    everything here as a hypothesis, and the same underpowered warnings from
    tools/outcome_slice.py apply — see required_n there.

USAGE
    venv/bin/python tools/shadow_report.py
    venv/bin/python tools/shadow_report.py --horizon 30 --by look_reason
    venv/bin/python tools/shadow_report.py --json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "tools"))

from ai_paths import find_report_file, resolve_report_dir  # noqa: E402

# Resolve the way the desk writes: ai_paths prefers ai_reports/ and falls back
# to claude_reports/. A hardcoded legacy path agrees with the writers only
# until something creates the primary tree, then reports frozen data silently.
SHADOW = (find_report_file("shadow.jsonl")
          or resolve_report_dir() / "shadow.jsonl")


def load(path: Path = SHADOW) -> list[dict]:
    rows: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return rows
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if isinstance(r, dict) and r.get("symbol") and r.get("price") is not None:
            rows.append(r)
    rows.sort(key=lambda r: (r.get("symbol") or "", r.get("ts") or 0))
    return rows


def by_episode(rows: list[dict]) -> dict[tuple, list[dict]]:
    """Group samples into (symbol, admit_ts) episodes.

    Keyed on admit_ts as well as symbol because a name can be admitted,
    dropped and re-admitted within a session — pooling those would blend two
    different decisions into one series.
    """
    eps: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        eps[(r["symbol"], r.get("admit_ts"))].append(r)
    for v in eps.values():
        v.sort(key=lambda r: r.get("ts") or 0)
    return eps


def forward_return(series: list[dict], start_i: int, horizon_sec: float) -> float | None:
    """Pct change from series[start_i] to the last sample within *horizon_sec*.

    None when the episode ends before the horizon — a truncated window is
    missing data, not a flat return, and averaging zeros in would bias every
    slice toward nothing-happened.
    """
    t0 = series[start_i].get("ts") or 0
    p0 = series[start_i].get("price")
    if not p0:
        return None
    last = None
    for r in series[start_i + 1:]:
        if (r.get("ts") or 0) - t0 > horizon_sec:
            break
        last = r
    if last is None or not last.get("price"):
        return None
    if (last.get("ts") or 0) - t0 < horizon_sec * 0.5:
        return None  # window barely opened; not a measurement
    return (float(last["price"]) - float(p0)) / float(p0) * 100.0


def episode_summary(series: list[dict], horizon_sec: float) -> dict[str, Any]:
    first = series[0]
    prices = [float(r["price"]) for r in series if r.get("price")]
    touched = any(r.get("in_zone") for r in series)
    t_first = first.get("ts") or 0
    touch_ts = next((r.get("ts") for r in series if r.get("in_zone")), None)

    # Did the desk ever refuse while price was actually in the zone?
    blocked_in_zone = [r for r in series
                       if r.get("in_zone") and r.get("arm_ok") is False]
    armed = any(r.get("arm_ok") is True for r in series)

    # Gate cost is measured from the REFUSAL, not from admission — the
    # question is what happened after the desk declined, and by then price has
    # already travelled from wherever it was admitted. Measuring from
    # admission answers a different question and understates a gate that
    # blocks late in a move.
    block_i = next((i for i, r in enumerate(series)
                    if r.get("in_zone") and r.get("arm_ok") is False), None)
    blocked_fwd = (forward_return(series, block_i, horizon_sec)
                   if block_i is not None else None)

    lo = first.get("entry_low")
    hi = first.get("entry_high")
    stop = first.get("stop_price")
    tgt = first.get("target_1")
    hit_target = bool(tgt and prices and max(prices) >= float(tgt))
    hit_stop = bool(stop and prices and min(prices) <= float(stop))

    return {
        "symbol": first["symbol"],
        "samples": len(series),
        "minutes": round(((series[-1].get("ts") or 0) - t_first) / 60.0, 1),
        "source": first.get("source"),
        "look_reason": first.get("look_reason"),
        "rvol": first.get("rvol"),
        "criteria": first.get("criteria") or [],
        "cm_rsi_rising": first.get("cm_rsi_rising"),
        "entry_hour_et": first.get("entry_hour_et"),
        "zone": [lo, hi],
        "zone_touched": touched,
        "minutes_to_touch": (round((touch_ts - t_first) / 60.0, 1)
                             if touch_ts else None),
        "armed": armed,
        "blocked_in_zone": len(blocked_in_zone),
        "block_reasons": sorted({str(r.get("arm_why") or "")
                                 for r in blocked_in_zone} - {""}),
        "blocked_fwd_pct": blocked_fwd,
        "fwd_return_pct": forward_return(series, 0, horizon_sec),
        "max_excursion_pct": (round((max(prices) - prices[0]) / prices[0] * 100, 3)
                              if prices else None),
        "min_excursion_pct": (round((min(prices) - prices[0]) / prices[0] * 100, 3)
                              if prices else None),
        "hit_target_first": hit_target and not hit_stop,
        "hit_stop_first": hit_stop and not hit_target,
    }


def _agg(eps: list[dict], key: str) -> dict[str, dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for e in eps:
        if key == "criteria":
            for c in (e.get("criteria") or ["(none)"]):
                groups[str(c)].append(e)
        else:
            groups[str(e.get(key))].append(e)
    out = {}
    for label, g in sorted(groups.items()):
        fwd = [e["fwd_return_pct"] for e in g if e["fwd_return_pct"] is not None]
        out[label] = {
            "episodes": len(g),
            "fwd_n": len(fwd),
            "fwd_mean_pct": round(statistics.fmean(fwd), 3) if fwd else None,
            "fwd_win_pct": (round(100.0 * sum(1 for x in fwd if x > 0) / len(fwd), 1)
                            if fwd else None),
            "zone_touch_pct": round(
                100.0 * sum(1 for e in g if e["zone_touched"]) / len(g), 1),
            "armed_pct": round(100.0 * sum(1 for e in g if e["armed"]) / len(g), 1),
        }
    return out


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--path", default=str(SHADOW))
    ap.add_argument("--horizon", type=float, default=30.0,
                    help="forward-return horizon in minutes (default 30)")
    ap.add_argument("--by", action="append", dest="keys",
                    default=None, help="slice key (repeatable)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    rows = load(Path(args.path))
    eps_raw = by_episode(rows)
    eps = [episode_summary(s, args.horizon * 60.0)
           for s in eps_raw.values() if s]
    keys = args.keys or ["source", "look_reason", "cm_rsi_rising", "criteria"]

    if args.json:
        print(json.dumps({
            "samples": len(rows), "episodes": len(eps),
            "horizon_min": args.horizon,
            "slices": {k: _agg(eps, k) for k in keys},
            "episode_detail": eps,
        }, indent=2, default=str))
        return

    print(f"\nshadow log: {args.path}")
    print(f"samples: {len(rows)}   episodes: {len(eps)}   "
          f"forward horizon: {args.horizon:.0f}m")
    if not eps:
        print("\nNothing logged yet. Samples accrue once the watch poller runs "
              "with ai_shadow_log_enabled and the book is non-empty.")
        return

    touched = [e for e in eps if e["zone_touched"]]
    armed = [e for e in eps if e["armed"]]
    blocked = [e for e in eps if e["blocked_in_zone"]]
    print("\n── 2. ZONE REACHABILITY " + "─" * 42)
    print(f"  episodes whose price reached the zone : {len(touched)}/{len(eps)}"
          f" ({100.0*len(touched)/len(eps):.0f}%)")
    mins = [e["minutes_to_touch"] for e in touched if e["minutes_to_touch"] is not None]
    if mins:
        print(f"  median minutes to first touch        : {statistics.median(mins):.1f}")
    tgt = sum(1 for e in eps if e["hit_target_first"])
    stp = sum(1 for e in eps if e["hit_stop_first"])
    print(f"  target reached before stop            : {tgt}")
    print(f"  stop reached before target            : {stp}")

    print("\n── 3. GATE COST (price in zone, desk refused) " + "─" * 21)
    print(f"  episodes blocked while in zone        : {len(blocked)}")
    print(f"  episodes that armed                   : {len(armed)}")
    if blocked:
        reasons: dict[str, int] = defaultdict(int)
        for e in blocked:
            for r in e["block_reasons"]:
                reasons[r] += 1
        print("    (forward return measured FROM the refusal, not from admission)")
        for r, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
            sub = [e["blocked_fwd_pct"] for e in blocked
                   if r in e["block_reasons"] and e["blocked_fwd_pct"] is not None]
            mean = f"{statistics.fmean(sub):+.2f}%" if sub else "—"
            flag = ""
            if sub and statistics.fmean(sub) > 0:
                flag = "   <- refused, then price ROSE (gate may be costing)"
            elif sub and statistics.fmean(sub) < 0:
                flag = "   <- refused, then price FELL (gate may be earning)"
            print(f"    {r:<22} n={n:<4} fwd {mean}{flag}")

    print("\n── 1. SELECTION (forward return after admission) " + "─" * 18)
    for key in keys:
        table = _agg(eps, key)
        print(f"\n  by {key}")
        print(f"    {'group':<16}{'eps':>5}{'fwdN':>6}{'fwd%':>9}"
              f"{'win%':>7}{'touch%':>8}{'armed%':>8}")
        for label, s in table.items():
            fm = f"{s['fwd_mean_pct']:+.3f}" if s["fwd_mean_pct"] is not None else "—"
            fw = f"{s['fwd_win_pct']:.0f}" if s["fwd_win_pct"] is not None else "—"
            print(f"    {label:<16}{s['episodes']:>5}{s['fwd_n']:>6}{fm:>9}"
                  f"{fw:>7}{s['zone_touch_pct']:>8.0f}{s['armed_pct']:>8.0f}")

    print("\nForward return is not P&L — no entry discipline, no slippage, no "
          "stop management.\nIt says what was worth looking at, not what would "
          "have paid. Confirm in tools/ab_bench.py.")


if __name__ == "__main__":
    main()
