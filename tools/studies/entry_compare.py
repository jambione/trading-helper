#!/usr/bin/env python3
"""entry_compare - one side-by-side table of every way to open tested on 11/19, on the same days, costs and metrics
(operator 11/19 'need to rethink our open box %R overbought open strategy' -> 'Yes, compare tonight').

Recomputes from each study's own events with the shared SIP quote cache, so every row uses the same rules:
net = gross - real spread at entry, spread > 15 bp not traded, DECIDE 4/28-9/23 and CONFIRM 9/24-11/18 shown apart,
day-clustered t, entries per day, and net by time of day. Registered verdicts stay in each study's own score.json;
this table is a comparison, not a new verdict.

    .venv/bin/python tools/studies/entry_compare.py      # -> docs/studies/ENTRY_COMPARE_2026-11-19.md
"""
from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))

REP = ROOT / "ai_reports"
DOC = ROOT / "docs" / "studies" / "ENTRY_COMPARE_2026-11-19.md"
DECIDE_END = "2026-09-23"
GATE = 15.0
BUCKETS = ((0, 30, "09:30-10:00"), (30, 90, "10:00-11:00"), (90, 210, "11:00-13:00"), (210, 330, "13:00-15:00"),
           (330, 390, "15:00-16:00"))


def day_t(rows):
    per = defaultdict(list)
    for r in rows:
        per[r["day"]].append(r["net"])
    m = [np.mean(v) for v in per.values()]
    if len(m) < 3 or np.std(m, ddof=1) == 0:
        return None
    return round(float(np.mean(m) / (np.std(m, ddof=1) / math.sqrt(len(m)))), 2)


def summarize(rows):
    """rows: [{day, t, net}] -> n, mean, win, t, entries/day, by time of day. Pure."""
    if not rows:
        return {"n": 0}
    x = np.array([r["net"] for r in rows])
    days = {r["day"] for r in rows}
    tod = {}
    for lo, hi, name in BUCKETS:
        sel = [r["net"] for r in rows if lo <= r["t"] < hi]
        if len(sel) >= 30:
            tod[name] = (round(float(np.mean(sel)), 1), len(sel))
    return {"n": len(rows), "mean": round(float(x.mean()), 2), "win": round(100 * float((x > 0).mean()), 1),
            "t": day_t(rows), "per_day": round(len(rows) / len(days), 2), "tod": tod}


def with_spread(rows, key, quotes, qkey):
    """Attach net = row[key] - spread; drop no-quote and spread > GATE. Pure."""
    out = []
    for r in rows:
        if r.get(key) is None:
            continue
        sp = quotes.get(qkey(r["day"], r["sym"], r["t"]))
        if sp is None or sp > GATE:
            continue
        out.append({"day": r["day"], "t": r["t"], "net": r[key] - sp})
    return out


def split(rows):
    return [r for r in rows if r["day"] <= DECIDE_END], [r for r in rows if r["day"] > DECIDE_END]


def load(path):
    p = REP / path
    return json.loads(p.read_text()) if p.exists() else None


def collect(quotes, qkey):
    entries = []        # (label, exit, rows)
    osq = load("opening_square/events.json")
    if osq:
        for arm, label in (("square", "Square at the open (09:30-09:58)"), ("presquare", "Open box, 09:30-09:58 (today's rule)"),
                           ("random", "Random minute 09:30-09:58 (control)"), ("square_late", "Square after 10:00")):
            entries.append((label, "15-min bracket", with_spread([r for r in osq if r["arm"] == arm], "gross", quotes, qkey)))
    lhr = load("low_heat_rising/events.json")
    if lhr:
        for arm, label in (("low_heat", "Low heat turning up (< 40)"), ("presquare", "Open box, all day (today's rule)"),
                           ("random", "Random minute (control)")):
            for ex, exname in (("desk", "15-min bracket"), ("hold", "hold to 15:55")):
                entries.append((label, exname, with_spread([r for r in lhr if r["arm"] == arm], ex, quotes, qkey)))
        lo20 = [r for r in lhr if r["arm"] == "low_heat" and r.get("heat") is not None and r["heat"] < 20]
        for ex, exname in (("desk", "15-min bracket"), ("hold", "hold to 15:55")):
            entries.append(("Low heat turning up (< 20)", exname, with_spread(lo20, ex, quotes, qkey)))
    gates = load("arm_gates_check/features.json")
    if gates:
        both = [r for r in gates if not r["A_blocks"] and not r["B_blocks"]]
        entries.append(("Open box with both gates (live until 11:45)", "15-min bracket", with_spread(both, "gross", quotes, qkey)))
        entries.append(("Open box, no gates", "15-min bracket", with_spread(gates, "gross", quotes, qkey)))
    return entries


def morning_picks_rows():
    s = load("morning_picks_hold/score.json")
    if not s:
        return []
    out = []
    for r, v in s.get("main", {}).items():
        d = v.get("decide", {})
        c = v.get("confirm", {})
        out.append((r, d, c, s.get("verdicts", {}).get(r, ["?"])[0]))
    return out


def verdicts():
    out = {}
    for name, path in (("opening square", "opening_square/score.json"), ("low heat", "low_heat_rising/score.json"),
                       ("morning picks", "morning_picks_hold/score.json"), ("arm gates", "arm_gates_check/score.json"),
                       ("options call flow", "options_callflow/result.json")):
        s = load(path)
        if s is None:
            out[name] = "not scored yet"
            continue
        v = s.get("VERDICT") or s.get("verdicts") or s.get("verdict")
        out[name] = json.dumps(v)[:300]
    return out


def render(entries, mph, verd):
    L = ["# Ways to open, side by side (11/19)", "",
         "Every row: real SIP spread at entry, spread > 15 bp not traded, net = gross - spread. DECIDE = 4/28-9/23, "
         "CONFIRM = 9/24-11/18. t is day-clustered. Registered pass/fail verdicts are each study's own (listed at the end); "
         "this table compares them on one scale.", "",
         "| Way to open | Exit | DECIDE net bp | t | win % | per day | CONFIRM net bp | n (D / C) |",
         "|---|---|---|---|---|---|---|---|"]
    tods = []
    for label, ex, rows in entries:
        d, c = split(rows)
        sd, sc = summarize(d), summarize(c)
        if not sd.get("n"):
            L.append(f"| {label} | {ex} | not scored yet | | | | | |")
            continue
        L.append(f"| {label} | {ex} | {sd['mean']:+.2f} | {sd['t']} | {sd['win']} | {sd['per_day']} | "
                 f"{sc.get('mean', 'n/a') if not sc.get('n') else format(sc['mean'], '+.2f')} | {sd['n']} / {sc.get('n', 0)} |")
        tods.append((label, ex, sd.get("tod", {})))
    if mph:
        L += ["", "## Pick the name, then hold (5 names at 10:00, hold to 15:55, -3% stop; bp per DAY, equal weight)", "",
              "| Ranker | DECIDE net bp/day | vs random same-list | t | CONFIRM net | vs random | verdict |", "|---|---|---|---|---|---|---|"]
        for r, d, c, v in mph:
            L.append(f"| {r} | {d.get('net')} | {d.get('diff')} | {d.get('diff_t')} | {c.get('net')} | {c.get('diff')} | {v} |")
    L += ["", "## Net by time of day (DECIDE, bp per trade, n)", "", "| Way to open | Exit | " +
          " | ".join(b[2] for b in BUCKETS) + " |", "|---|---|" + "---|" * len(BUCKETS)]
    for label, ex, tod in tods:
        L.append(f"| {label} | {ex} | " + " | ".join(f"{tod[b[2]][0]:+.1f} ({tod[b[2]][1]})" if b[2] in tod else "" for b in BUCKETS) + " |")
    L += ["", "## Registered verdicts", ""] + [f"- **{k}:** {v}" for k, v in verd.items()]
    return "\n".join(L) + "\n"


def main():
    import news_source as ns
    quotes = json.load(open(ns.QUOTES)) if ns.QUOTES.exists() else {}
    md = render(collect(quotes, ns.qkey), morning_picks_rows(), verdicts())
    DOC.write_text(md)
    print(md)


if __name__ == "__main__":
    main()
