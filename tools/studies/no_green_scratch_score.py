#!/usr/bin/env python3
"""Score the evening replay batch for three preregs:

  docs/studies/no_green_scratch_prereg.json    np90, np60 vs np180 (paired)
  docs/studies/no_momentum_seats_prereg.json   nomom vs np180 (unpaired)
  docs/studies/keep_falling_seats_prereg.json  keepfall vs np180 (opens + non-inferiority)

Reads {day}_{arm}.json from --dir (scripts/no_green_scratch_replays.sh). Any
missing day/arm, failed run, or a result from other code/config makes the batch
INCOMPLETE: no verdict is printed on partial inputs. Read-only.

    .venv/bin/python tools/studies/no_green_scratch_score.py --dir /tmp/ngs_X --sha abc1234
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
from collections import Counter

DAYS = ["2026-09-30", "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06",
        "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-12", "2026-10-13"]
HALF_A = set(DAYS[:5])
CONTROL = "np180"
ARMS = ("np180", "np90", "np60", "nomom", "keepfall")
PAIRED = ("np90", "np60")
MATCH_SEC = 90.0
MIN_MATCHED_HALF = 30
MIN_TRADES_HALF = 50
COSTS = (0.0010, 0.0003)          # charged day totals: 10 bp primary, 3 bp sensitivity
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── pure ─────────────────────────────────────────────────────────────────────

def pair(control: list[dict], arm: list[dict], window: float = MATCH_SEC) -> list[tuple[dict, dict]]:
    """Match each control trade to at most one arm trade: same symbol, entries within `window` s,
    nearest first."""
    used: set[int] = set()
    out = []
    for c in sorted(control, key=lambda t: t["entry_ts"]):
        best, best_d = None, None
        for i, a in enumerate(arm):
            if i in used or a["symbol"] != c["symbol"]:
                continue
            d = abs(a["entry_ts"] - c["entry_ts"])
            if d <= window and (best_d is None or d < best_d):
                best, best_d = i, d
        if best is not None:
            used.add(best)
            out.append((c, arm[best]))
    return out


def day_equal(per_day: dict) -> tuple[float | None, float | None, int]:
    vals = list(per_day.values())
    if len(vals) < 3:
        return (statistics.mean(vals) if vals else None), None, len(vals)
    mu, sd = statistics.mean(vals), statistics.stdev(vals)
    return mu, (mu / (sd / math.sqrt(len(vals))) if sd > 0 else None), len(vals)


def opens_per_10m(res: dict) -> float:
    n_slots = max(1, len(res.get("slots") or {}))
    return (len(res.get("closed") or []) + len(res.get("open_at_end") or [])) / n_slots


def charged_total(res: dict, cost: float) -> float:
    return sum(t["ret"] - cost for t in res.get("closed") or [])


def verdict(paired_mu, paired_t, half_mu: dict, half_n: dict, days_not_worse: int, opens_ratio) -> str:
    """Hard time scratch (np90 / np60)."""
    if any(half_n.get(h, 0) < MIN_MATCHED_HALF for h in "AB"):
        return "UNDERPOWERED"
    ok = (paired_mu is not None and paired_mu > 0 and paired_t is not None and paired_t >= 2.0
          and all((half_mu.get(h) or 0) > 0 for h in "AB")
          and days_not_worse >= 6
          and opens_ratio is not None and opens_ratio >= 0.9)
    return "PASS" if ok else "FAIL"


def nomom_verdict(mu, t, half_mu: dict, half_n: dict, days_not_worse: int, opens_ratio) -> str:
    if any(half_n.get(h, 0) < MIN_TRADES_HALF for h in "AB"):
        return "UNDERPOWERED"
    ok = (mu is not None and mu > 0 and t is not None and t >= 2.0
          and all((half_mu.get(h) or 0) > 0 for h in "AB")
          and days_not_worse >= 6 and opens_ratio is not None and opens_ratio >= 0.9)
    return "PASS" if ok else "FAIL"


def keepfall_verdict(open_mu, open_t, opens_ratio, half_trade_mu: dict, half_n: dict) -> str:
    if any(half_n.get(h, 0) < MIN_TRADES_HALF for h in "AB"):
        return "UNDERPOWERED"
    ok = (open_mu is not None and open_mu > 0 and open_t is not None and open_t >= 2.0
          and opens_ratio is not None and opens_ratio >= 1.10
          and all(half_trade_mu.get(h) is not None and half_trade_mu[h] >= -0.0001 for h in "AB"))
    return "PASS" if ok else "FAIL"


def completeness(results: dict, sha: str | None, failures: list[str]) -> list[str]:
    """Problems that make the batch INCOMPLETE. results: {(day, arm): json}."""
    probs = [f"failed run: {f}" for f in failures]
    probs += [f"missing {d} {a}" for d in DAYS for a in ARMS if (d, a) not in results]
    shas = {str(r.get("sha") or "")[:7] for r in results.values()}
    cfgs = {r.get("set_json_sha") for r in results.values()}
    if sha and any(not s.startswith(sha[:7]) for s in shas):
        probs.append(f"results from other code: {sorted(shas)}")
    if None in cfgs or len(cfgs) > 1:
        probs.append(f"results not all pinned to one --set-json config: {sorted(map(str, cfgs))}")
    return probs


# ── scoring ──────────────────────────────────────────────────────────────────

def bp(x):
    return "—" if x is None else f"{x * 1e4:+.1f}"


def tt(x):
    return "—" if x is None else f"{x:+.2f}"


def live_no_progress_share() -> dict:
    """Information: live share of closes by no_progress per day (10/08-10/12 ran 180 s)."""
    p = os.path.join(ROOT, "ai_reports", "outcomes.jsonl")
    out: dict = {}
    if not os.path.exists(p):
        return out
    from datetime import datetime
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    cnt: dict = {}
    for line in open(p):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if not r.get("entry_time") or not r.get("total_qty"):
            continue
        day = datetime.fromtimestamp(float(r["entry_time"]), et).strftime("%Y-%m-%d")
        if day in ("2026-10-08", "2026-10-09", "2026-10-12"):
            c = cnt.setdefault(day, [0, 0])
            c[0] += 1
            c[1] += (r.get("closing_reason") or r.get("close_reason")) == "no_progress"
    for d, (n, k) in cnt.items():
        out[d] = k / n if n else None
    return out


def score(d: str, sha: str | None = None) -> str:
    results = {}
    for day in DAYS:
        for arm in ARMS:
            p = os.path.join(d, f"{day}_{arm}.json")
            if os.path.exists(p):
                results[(day, arm)] = json.load(open(p))
    fail_p = os.path.join(d, "failures.txt")
    failures = open(fail_p).read().split("\n") if os.path.exists(fail_p) else []
    failures = [f for f in failures if f.strip()]
    probs = completeness(results, sha, failures)
    L = ["# Evening replay batch", ""]
    rl = {}
    for day in DAYS:
        for arm in ARMS:
            lp = os.path.join(d, f"{day}_{arm}.log")
            if os.path.exists(lp):
                n429 = len(re.findall(r"429|too many requests", open(lp, errors="replace").read()))
                if n429:
                    rl[f"{day} {arm}"] = n429
    if rl:
        L.append(f"Rate-limit lines in logs (names may be data-starved): {rl}")
    if probs:
        L += ["**INCOMPLETE - no verdicts.**", ""] + [f"- {p}" for p in probs]
        return "\n".join(L)

    ctrl = {day: results[(day, CONTROL)] for day in DAYS}
    L += ["## Hard time scratch (no_green_scratch_prereg.json)", "",
          "| arm | pairs | paired diff (bp) | t | half A | half B | charged day total >= control (10 bp) | opens vs control | verdict |",
          "|---|---:|---:|---:|---:|---:|---:|---:|---|"]
    info = []
    for arm in PAIRED:
        per_day, halves, n_half = {}, {"A": [], "B": []}, Counter()
        not_worse, op_c, op_a, n_pairs, np_arm, np_ctl = 0, [], [], 0, 0, 0
        pairs_by_day, ndiff, cut_green = {}, {}, []
        for day in DAYS:
            c, a = ctrl[day], results[(day, arm)]
            pr = pair(c["closed"], a["closed"])
            pairs_by_day[day] = len(pr)
            n_pairs += len(pr)
            if pr:
                per_day[day] = statistics.mean(y["ret"] - x["ret"] for x, y in pr)
                h = "A" if day in HALF_A else "B"
                halves[h].append(per_day[day])
                n_half[h] += len(pr)
                cut_green += [x["ret"] - y["ret"] for x, y in pr
                              if y["reason"] == "no_progress" and x["reason"] != "no_progress"]
            not_worse += charged_total(a, COSTS[0]) >= charged_total(c, COSTS[0])
            op_c.append(opens_per_10m(c))
            op_a.append(opens_per_10m(a))
            ndiff[day] = len(a["closed"]) - len(c["closed"])
            np_arm += sum(t["reason"] == "no_progress" for t in a["closed"])
            np_ctl += sum(t["reason"] == "no_progress" for t in c["closed"])
        mu, t, _n = day_equal(per_day)
        hm = {h: (statistics.mean(v) if v else None) for h, v in halves.items()}
        ratio = statistics.mean(op_a) / statistics.mean(op_c) if statistics.mean(op_c) > 0 else None
        v = verdict(mu, t, hm, n_half, not_worse, ratio)
        if np_arm <= np_ctl:
            v = f"VACUOUS ({v}): arm no_progress exits {np_arm} <= control {np_ctl}"
        L.append(f"| {arm} | {n_pairs} | {bp(mu)} | {tt(t)} | {bp(hm['A'])} | {bp(hm['B'])} | {not_worse}/10 "
                 f"| {'—' if ratio is None else f'{ratio:.2f}x'} | {v} |")
        info.append(f"- {arm}: pairs per day {pairs_by_day}; trade-count diff per day {ndiff}; no_progress exits "
                    f"{np_arm} vs {np_ctl}; trades the scratch cut that control did not: {len(cut_green)}, control minus "
                    f"arm on those {bp(statistics.mean(cut_green)) if cut_green else '—'} bp")
        for cost in COSTS[1:]:
            nw = sum(charged_total(results[(day, arm)], cost) >= charged_total(ctrl[day], cost) for day in DAYS)
            info.append(f"  - charged at {cost * 1e4:.0f} bp: day total >= control on {nw}/10")

    # nomom
    per_day, halves, n_half = {}, {"A": [], "B": []}, Counter()
    not_worse, op_c, op_a = 0, [], []
    for day in DAYS:
        c, a = ctrl[day], results[(day, "nomom")]
        if c["closed"] and a["closed"]:
            per_day[day] = (statistics.mean(x["ret"] for x in a["closed"])
                            - statistics.mean(x["ret"] for x in c["closed"]))
            halves["A" if day in HALF_A else "B"].append(per_day[day])
        n_half["A" if day in HALF_A else "B"] += min(len(c["closed"]), len(a["closed"]))
        not_worse += charged_total(a, COSTS[0]) >= charged_total(c, COSTS[0])
        op_c.append(opens_per_10m(c))
        op_a.append(opens_per_10m(a))
    mu, t, _n = day_equal(per_day)
    hm = {h: (statistics.mean(v) if v else None) for h, v in halves.items()}
    ratio = statistics.mean(op_a) / statistics.mean(op_c) if statistics.mean(op_c) > 0 else None
    L += ["", "## No momentum seats (no_momentum_seats_prereg.json)", "",
          "| per-trade diff (bp) | t | half A | half B | charged day total >= control | opens vs control | verdict |",
          "|---:|---:|---:|---:|---:|---:|---|",
          f"| {bp(mu)} | {tt(t)} | {bp(hm['A'])} | {bp(hm['B'])} | {not_worse}/10 "
          f"| {'—' if ratio is None else f'{ratio:.2f}x'} | {nomom_verdict(mu, t, hm, n_half, not_worse, ratio)} |"]

    # keepfall
    op_diff, tr_half, n_half = {}, {"A": [], "B": []}, Counter()
    op_c, op_a = [], []
    for day in DAYS:
        c, a = ctrl[day], results[(day, "keepfall")]
        op_diff[day] = opens_per_10m(a) - opens_per_10m(c)
        op_c.append(opens_per_10m(c))
        op_a.append(opens_per_10m(a))
        h = "A" if day in HALF_A else "B"
        if c["closed"] and a["closed"]:
            tr_half[h].append(statistics.mean(x["ret"] for x in a["closed"])
                              - statistics.mean(x["ret"] for x in c["closed"]))
        n_half[h] += min(len(c["closed"]), len(a["closed"]))
    omu, ot, _n = day_equal(op_diff)
    ratio = statistics.mean(op_a) / statistics.mean(op_c) if statistics.mean(op_c) > 0 else None
    thm = {h: (statistics.mean(v) if v else None) for h, v in tr_half.items()}
    L += ["", "## Keep falling seats (keep_falling_seats_prereg.json)", "",
          "| opens/10m diff | t | opens ratio | per-trade diff half A (bp) | half B | verdict |",
          "|---:|---:|---:|---:|---:|---|",
          f"| {'—' if omu is None else f'{omu:+.2f}'} | {tt(ot)} | {'—' if ratio is None else f'{ratio:.2f}x'} "
          f"| {bp(thm['A'])} | {bp(thm['B'])} | {keepfall_verdict(omu, ot, ratio, thm, n_half)} |"]

    live = live_no_progress_share()
    rep_share = {day: (sum(t["reason"] == "no_progress" for t in ctrl[day]["closed"]) / len(ctrl[day]["closed"])
                       if ctrl[day]["closed"] else None) for day in ("2026-10-08", "2026-10-09", "2026-10-12")}
    info.append(f"- fidelity: no_progress share of closes, live {({k: round(v, 2) for k, v in live.items() if v is not None})} "
                f"vs replay np180 {({k: round(v, 2) for k, v in rep_share.items() if v is not None})}")
    return "\n".join(L + ["", "### Information only", ""] + info)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--sha", default=None, help="code sha every result must carry")
    a = ap.parse_args()
    print(score(a.dir, a.sha))


if __name__ == "__main__":
    main()
