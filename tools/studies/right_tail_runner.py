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

def runner_exit(bars: dict, entry_min: int, entry_px: float, stop_pct: float = STOP, arm_pct: float = ARM,
                trail_pct: float = TRAIL, eod: int = EOD_MIN) -> tuple[float, int, str] | None:
    """(exit price, exit minute, reason) walking SIP bars from entry_min + 1. None if no bar at all."""
    stop = entry_px * (1 - stop_pct)
    high = entry_px
    last = None
    for m in range(entry_min + 1, eod + 1):
        b = bars.get(m)
        if b is None:
            continue
        o, h, lo, c = b
        last = (c, m)
        if lo <= stop:
            return min(stop, o), m, "stop" if stop < entry_px else "trail"
        high = max(high, h)
        if arm_pct < 9 and high >= entry_px * (1 + arm_pct):
            stop = max(stop, high * (1 - trail_pct))
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


# ── data / scoring ───────────────────────────────────────────────────────────

def trades(datas: list[dict]) -> list[dict]:
    days = {d["day"] for d in datas}
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
        if day in days:
            out.append({"sym": str(r["symbol"]).upper(), "day": day, "entry_min": E._et_min(float(et)),
                        "entry_px": float(r["entry_price"]), "actual": float(r["exit_price"]) / float(r["entry_price"]) - 1})
    return out


def score(datas: list[dict], params=(STOP, ARM, TRAIL)) -> dict:
    by = {d["day"]: d for d in datas}
    skips = Counter()
    res = {h: {"diff": defaultdict(list), "run": defaultdict(list), "act": defaultdict(list), "rets": [],
               "act_rets": [], "reasons": Counter(), "hold": []} for h in "AB"}
    cache = {}
    for t in trades(datas):
        sipf = by[t["day"]]["sip"].get(t["sym"])
        if sipf is None:
            skips["no_sip_bars"] += 1
            continue
        key = (t["day"], t["sym"])
        if key not in cache:
            cache[key] = L.ohlc_map(sipf)
        ex = runner_exit(cache[key], t["entry_min"], t["entry_px"], *params)
        if ex is None:
            skips["no_bar_after_entry"] += 1
            continue
        run = ex[0] / t["entry_px"] - 1 - EXIT_COST
        h = "A" if t["day"] <= L.SPLIT_END_A else "B"
        x = res[h]
        x["diff"][t["day"]].append(run - t["actual"])
        x["run"][t["day"]].append(run)
        x["act"][t["day"]].append(t["actual"])
        x["rets"].append(run)
        x["act_rets"].append(t["actual"])
        x["reasons"][ex[2]] += 1
        x["hold"].append(ex[1] - t["entry_min"])
    out = {"skips": dict(skips)}
    for h, x in res.items():
        d, dt, n, nd = E.day_equal(x["diff"])
        rn, rt, _, _ = E.day_equal(x["run"])
        ok = n >= MIN_TRADES and nd >= MIN_DAYS and None not in (d, dt, rn)
        verdict = ("UNDERPOWERED" if not ok else "PASS" if d >= LIFT_BAR and dt >= 2.0 and rn > 0 else "FAIL")
        out[h] = {"diff": d, "diff_t": dt, "runner_net": rn, "runner_t": rt, "actual_net": E.day_equal(x["act"])[0],
                  "n": n, "days": nd, "reasons": dict(x["reasons"]),
                  "median_hold_min": statistics.median(x["hold"]) if x["hold"] else None,
                  "tail_runner": tail_stats(x["rets"]), "tail_actual": tail_stats(x["act_rets"]), "verdict": verdict}
    return out


def _bp(x):
    return "—" if x is None else f"{x * 1e4:+.1f}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--score", action="store_true")
    a = ap.parse_args()
    datas, ex = E.load_days()
    tr = trades(datas)
    print(f"days {len(datas)} (excluded {ex}); desk trades {len(tr)}")
    if not a.score:
        print("(inventory only; --score after a result-skeptic READY)")
        return
    r = score(datas)
    lines = [f"# Right-tail runner (stop {STOP:.0%}, trail {TRAIL:.0%} after +{ARM:.0%}, EOD 15:49)", "",
             "| half | trades | runner net (t) | actual net | runner - actual (t) | median hold | exits | verdict |",
             "|---|---:|---:|---:|---:|---:|---|---|"]
    for h in "AB":
        x = r[h]
        lines.append(f"| {h} | {x['n']} | {_bp(x['runner_net'])} ({x['runner_t'] and round(x['runner_t'], 2)}) | {_bp(x['actual_net'])} "
                     f"| {_bp(x['diff'])} ({x['diff_t'] and round(x['diff_t'], 2)}) | {x['median_hold_min']} min | {x['reasons']} | {x['verdict']} |")
    vs = [r[h]["verdict"] for h in "AB"]
    lines += ["", f"**Runner: {'PASS' if all(v == 'PASS' for v in vs) else 'FAIL' if 'FAIL' in vs else 'UNDERPOWERED'}**",
              f"skips {r['skips']}", "", "Right tail (information):"]
    for h in "AB":
        lines.append(f"- {h} runner: {r[h]['tail_runner']}")
        lines.append(f"- {h} actual: {r[h]['tail_actual']}")
    lines += ["", "Grid (information):"]
    for g in GRID:
        x = score(datas, g)
        lines.append(f"- stop {g[0]:.1%} arm {g[1] if g[1] < 9 else 'none'} trail {g[2]:.1%}: " + "; ".join(
            f"{h} runner {_bp(x[h]['runner_net'])}, vs actual {_bp(x[h]['diff'])}, >=+2% {x[h]['tail_runner'].get('share_ge_2pct', 0):.1%}" for h in "AB"))
    out = "\n".join(lines)
    print(out)
    with open(os.path.join(E.OUT_DIR, "right_tail_runner_result.md"), "w") as f:
        f.write(out + "\n")


if __name__ == "__main__":
    main()
