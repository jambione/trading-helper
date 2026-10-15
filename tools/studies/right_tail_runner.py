#!/usr/bin/env python3
"""Right-tail runner: would letting our real trades run (wide stop, wide trail, no leash) catch big winners the
leash cuts? Spec: docs/studies/right_tail_runner_prereg.json.

For every desk fill (outcomes.jsonl, entry_path 'watch') on the cached sessions, keep the real entry (fill price,
minute) and replace the exit with the runner rule on SIP 1-minute bars from the minute after the entry:
  hard stop  entry x (1 - STOP)          a bar whose low reaches it exits at min(stop, that bar's open)
  trail      once a high >= entry x (1 + ARM), stop = max(stop, running high x (1 - TRAIL))
  time       SIP close of 15:49 at the latest
Runner exits pay EXIT_COST (half a typical spread); the real exit is the desk's actual fill.

  (default)  inventory: trades, days, bars present; no returns
  --score    after a result-skeptic READY
Cache only (ai_reports/early_entry_honest/<day>.pkl SIP bars) + ai_reports/outcomes.jsonl.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import early_entry_honest as E  # noqa: E402
import limit_below_study as L  # noqa: E402

STOP, ARM, TRAIL = 0.02, 0.01, 0.01
EXIT_COST = 0.0005
EOD_MIN = 15 * 60 + 49
LIFT_BAR = 0.0005
MIN_TRADES, MIN_DAYS = 100, 6
GRID = ((0.01, 0.005, 0.005), (0.02, 0.01, 0.01), (0.03, 0.02, 0.015), (0.03, 9.99, 0.0))   # last = stop only, hold


# ── pure ─────────────────────────────────────────────────────────────────────

LO_DAY, HI_DAY = "2026-09-08", "2026-10-13"     # skeptic #5: the registered period, enforced
MAX_SKIP = 0.05


def runner_exit(bars: dict, entry_min: int, entry_px: float, stop_pct: float = STOP, arm_pct: float = ARM,
                trail_pct: float = TRAIL, eod: int = EOD_MIN, worse: bool = True) -> tuple[float, int, str] | None:
    """(exit price, exit minute, reason). The entry bar's LOW is checked against the hard stop (its high never arms
    the trail; skeptic #3). worse=True (graded, skeptic #4): each later bar first updates the running high and the
    trail, THEN tests its low against the new stop; worse=False tests the low first (information). None = no bar."""
    stop = entry_px * (1 - stop_pct)
    armed = False
    eb = bars.get(entry_min)
    if eb is not None and eb[2] <= stop:
        return stop, entry_min, "stop"
    high = entry_px
    last = None
    for m in range(entry_min + 1, eod + 1):
        b = bars.get(m)
        if b is None:
            continue
        o, h, lo, c = b
        last = (c, m)

        def arm(hh):
            nonlocal high, stop, armed
            high = max(high, hh)
            if arm_pct < 9 and high >= entry_px * (1 + arm_pct):
                stop = max(stop, high * (1 - trail_pct))
                armed = True
        if worse:
            arm(h)
        if lo <= stop:
            return min(stop, o), m, "trail" if armed else "stop"
        if not worse:
            arm(h)
    if last is None:
        return None
    return last[0], last[1], "eod"


def tail_stats(rets: list[float]) -> dict:
    if not rets:
        return {}
    srt = sorted(rets, reverse=True)
    k = max(1, len(srt) // 20)
    total = sum(rets)
    return {"share_ge_2pct": sum(1 for r in rets if r >= 0.02) / len(rets),
            "share_ge_5pct": sum(1 for r in rets if r >= 0.05) / len(rets),
            "top5pct_sum_bp": sum(srt[:k]) * 1e4, "total_sum_bp": total * 1e4,
            "max_bp": srt[0] * 1e4}


def max_concurrent(spans: list[tuple[int, int]]) -> int:
    ev = sorted([(a, 1) for a, _ in spans] + [(b, -1) for _, b in spans])
    cur = best = 0
    for _, d in ev:
        cur += d
        best = max(best, cur)
    return best


# ── data / scoring ───────────────────────────────────────────────────────────

def load_fills() -> list[dict]:
    out = []
    for line in open(E.OUTCOMES):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        et, xt = r.get("entry_time"), r.get("exit_time")
        if not et or not xt or r.get("entry_path") != "watch" or not r.get("total_qty"):
            continue
        if not r.get("entry_price") or not r.get("exit_price"):
            continue
        day = datetime.fromtimestamp(float(et), E.ET).strftime("%Y-%m-%d")
        if not (LO_DAY <= day <= HI_DAY):
            continue
        out.append({"sym": str(r["symbol"]).upper(), "day": day, "entry_ts": float(et),
                    "entry_min": E._et_min(float(et)), "exit_min": E._et_min(float(xt)),
                    "entry_px": float(r["entry_price"]), "exit_px": float(r["exit_price"]),
                    "close_reason": r.get("close_reason"), "slip_r": r.get("exit_slippage_r"),
                    "stop_px": r.get("stop_price")})
    return out


def measured_stop_cost(fills: list[dict]) -> float:
    """Median live slippage on local_trail exits, as a fraction of price (skeptic #2): exit_slippage_r x risk / entry."""
    xs = []
    for f in fills:
        if f["close_reason"] != "local_trail" or f["slip_r"] is None or not f["stop_px"]:
            continue
        risk = f["entry_px"] - float(f["stop_px"])
        if risk > 0:
            xs.append(abs(float(f["slip_r"])) * risk / f["entry_px"])
    return statistics.median(xs) if xs else 0.0


def trades(datas: list[dict]) -> tuple[list[dict], dict]:
    """First fill per (day, symbol) - a runner holds from it, so later re-entries could not happen (skeptic #1).
    Returns (first fills, {(day, sym): [all fills]})."""
    days = {d["day"] for d in datas}
    allf = [f for f in load_fills() if f["day"] in days]
    by = defaultdict(list)
    for f in allf:
        by[(f["day"], f["sym"])].append(f)
    first = [sorted(v, key=lambda x: x["entry_ts"])[0] for v in by.values()]
    return first, by


def score(datas: list[dict], params=(STOP, ARM, TRAIL), *, worse: bool = True, stop_cost: float = 0.0) -> dict:
    by_day = {d["day"]: d for d in datas}
    first, groups = trades(datas)
    skips = {h: Counter() for h in "AB"}
    res = {h: {"d_fill": defaultdict(list), "d_sip": defaultdict(list), "run": defaultdict(list),
               "act": defaultdict(list), "rets": [], "act_rets": [], "reasons": Counter(), "hold": [],
               "spans": defaultdict(list), "all_in_hold": [], "n_first": 0} for h in "AB"}
    cache = {}
    for t in first:
        h = "A" if t["day"] <= L.SPLIT_END_A else "B"
        res[h]["n_first"] += 1
        if t["entry_min"] >= EOD_MIN:
            skips[h]["entry_after_1549"] += 1
            continue
        sipf = by_day[t["day"]]["sip"].get(t["sym"])
        if sipf is None:
            skips[h]["no_sip_bars"] += 1
            continue
        key = (t["day"], t["sym"])
        if key not in cache:
            cache[key] = L.ohlc_map(sipf)
        bars = cache[key]
        ex = runner_exit(bars, t["entry_min"], t["entry_px"], *params, worse=worse)
        xb = bars.get(t["exit_min"])
        if ex is None or xb is None:
            skips[h]["no_bar_after_entry_or_exit_bar"] += 1
            continue
        cost = EXIT_COST + (stop_cost if ex[2] in ("stop", "trail") else 0.0)
        run = ex[0] / t["entry_px"] - 1 - cost
        act_fill = t["exit_px"] / t["entry_px"] - 1
        act_sip = xb[3] / t["entry_px"] - 1 - EXIT_COST          # the actual exit minute on the same SIP model
        x = res[h]
        x["d_fill"][t["day"]].append(run - act_fill)
        x["d_sip"][t["day"]].append(run - act_sip)
        x["run"][t["day"]].append(run)
        x["act"][t["day"]].append(act_fill)
        x["rets"].append(run)
        x["act_rets"].append(act_fill)
        x["reasons"][ex[2]] += 1
        x["hold"].append(ex[1] - t["entry_min"])
        x["spans"][t["day"]].append((t["entry_min"], ex[1]))
        x["all_in_hold"].append(sum(f["exit_px"] / f["entry_px"] - 1 for f in groups[key] if f["entry_min"] <= ex[1]))
    out = {}
    for h, x in res.items():
        df, dft, n, nd = E.day_equal(x["d_fill"])
        ds, dst, _, _ = E.day_equal(x["d_sip"])
        rn, rt, _, _ = E.day_equal(x["run"])
        sk = sum(skips[h].values())
        skip_share = sk / x["n_first"] if x["n_first"] else 1.0
        ok = n >= MIN_TRADES and nd >= MIN_DAYS and skip_share <= MAX_SKIP and None not in (df, dft, ds, dst, rn)
        passed = ok and df >= LIFT_BAR and dft >= 2.0 and ds >= LIFT_BAR and dst >= 2.0 and rn > 0
        verdict = "UNDERPOWERED" if not ok else ("PASS" if passed else "FAIL")
        out[h] = {"vs_fill": df, "vs_fill_t": dft, "vs_sip": ds, "vs_sip_t": dst, "runner_net": rn, "runner_t": rt,
                  "actual_net": E.day_equal(x["act"])[0], "n": n, "days": nd, "skips": dict(skips[h]),
                  "skip_share": skip_share, "reasons": dict(x["reasons"]),
                  "median_hold_min": statistics.median(x["hold"]) if x["hold"] else None,
                  "max_runners_open": max((max_concurrent(v) for v in x["spans"].values()), default=0),
                  "all_actual_trades_in_hold_bp": (statistics.fmean(x["all_in_hold"]) * 1e4) if x["all_in_hold"] else None,
                  "tail_runner": tail_stats(x["rets"]), "tail_actual": tail_stats(x["act_rets"]), "verdict": verdict}
    return out


def _bp(x):
    return "—" if x is None else f"{x * 1e4:+.1f}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--score", action="store_true")
    a = ap.parse_args()
    datas, ex = E.load_days()
    tr, groups = trades(datas)
    print(f"days {len(datas)} (excluded {ex}); first fills per name-day {len(tr)} (of {sum(len(v) for v in groups.values())} fills)")
    if not a.score:
        print("(inventory only; --score after a result-skeptic READY)")
        return
    sc = measured_stop_cost(load_fills())
    lines = [f"# Right-tail runner (stop {STOP:.0%}, trail {TRAIL:.0%} after +{ARM:.0%}, EOD 15:49; first fill per name-day)", "",
             f"measured live stop/trail slippage (median, local_trail exits): {sc * 1e4:.1f} bp", ""]
    verdicts = []
    for label, kw in (("graded at 5 bp exit cost", {}), ("graded at 5 bp + measured stop slippage", {"stop_cost": sc})):
        r = score(datas, **kw)
        lines += [f"## {label}", "",
                  "| half | trades | runner net (t) | actual net | runner - actual fill (t) | runner - actual on SIP (t) | median hold | max open | exits | skips | verdict |",
                  "|---|---:|---:|---:|---:|---:|---:|---:|---|---|---|"]
        for h in "AB":
            x = r[h]
            lines.append(f"| {h} | {x['n']} | {_bp(x['runner_net'])} ({x['runner_t'] and round(x['runner_t'], 2)}) | {_bp(x['actual_net'])} "
                         f"| {_bp(x['vs_fill'])} ({x['vs_fill_t'] and round(x['vs_fill_t'], 2)}) | {_bp(x['vs_sip'])} ({x['vs_sip_t'] and round(x['vs_sip_t'], 2)}) "
                         f"| {x['median_hold_min']} min | {x['max_runners_open']} | {x['reasons']} | {x['skips']} | {x['verdict']} |")
            verdicts.append(x["verdict"])
        lines.append("")
    lines += [f"**Runner: {'PASS' if all(v == 'PASS' for v in verdicts) else 'FAIL' if 'FAIL' in verdicts else 'UNDERPOWERED'}** (both halves, both cost levels)",
              "", "Right tail (information, graded run at 5 bp):"]
    r = score(datas)
    for h in "AB":
        lines.append(f"- {h} runner: {r[h]['tail_runner']}")
        lines.append(f"- {h} actual: {r[h]['tail_actual']}; all actual trades in the name during the runner's hold: {_bp((r[h]['all_actual_trades_in_hold_bp'] or 0) / 1e4)}")
    x = score(datas, worse=False)
    lines.append("- low-before-high bar ordering (information): " + "; ".join(f"{h} runner {_bp(x[h]['runner_net'])}, vs fill {_bp(x[h]['vs_fill'])}" for h in "AB"))
    lines += ["", "Grid (information; no cell can be promoted without a new prereg):"]
    for g in GRID:
        x = score(datas, g)
        lines.append(f"- stop {g[0]:.1%} arm {g[1] if g[1] < 9 else 'none'} trail {g[2]:.1%}: " + "; ".join(
            f"{h} runner {_bp(x[h]['runner_net'])}, vs fill {_bp(x[h]['vs_fill'])}, >=+2% {x[h]['tail_runner'].get('share_ge_2pct', 0):.1%}" for h in "AB"))
    out = "\n".join(lines)
    print(out)
    with open(os.path.join(E.OUT_DIR, "right_tail_runner_result.md"), "w") as f:
        f.write(out + "\n")


if __name__ == "__main__":
    main()
