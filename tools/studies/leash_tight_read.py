#!/usr/bin/env python3
"""leash_tight_read.py — tight_trail_replay_prereg.json amended_13: the two leash variants on TIGHT trades.
Reference = np_lob (same np180 + lob switches, live leash), so the difference isolates the leash change; base is shown too.
Tight trade = symbol admitted from the 'tight' source that day (ai_reports/admit_range.jsonl). Net = replay ret minus the full
SIP spread at entry (replay_costing.entry_spread_bp, cached). Per day: variant minus np_lob mean tight net; t across days.
Bar (prereg): >= +3 bp, t >= 2, negative on <= 20% of days, >= 60 reference tight trades; below 60 = direction only.
USAGE (mini, after the nightly replay): .venv/bin/python tools/studies/leash_tight_read.py DAY [DAY ...]"""
import json, math, os, statistics, sys
from collections import defaultdict
ROOT = os.getcwd()
sys.path[:0] = [os.path.join(ROOT, "tools", "studies"), os.path.join(ROOT, "tools"), ROOT]
import replay_costing as RC  # noqa: E402

VARS = ("np_lob", "np_lob_slow30", "np_lob_step02", "base")
days = sys.argv[1:]
tight = defaultdict(set)
for l in open(os.path.join(ROOT, "ai_reports", "admit_range.jsonl")):
    try:
        r = json.loads(l)
    except Exception:
        continue
    if r.get("source") == "tight" and r.get("day") in days:
        tight[r["day"]].add(r["symbol"])
cache, box = RC.load_cache(), {}
net = defaultdict(lambda: defaultdict(list))
for d in days:
    for v in VARS:
        p = f"/tmp/tt_run/{d}-{v}.json"
        if not os.path.exists(p):
            continue
        for t in json.load(open(p)).get("closed") or []:
            if t.get("ret") is None or t["symbol"] not in tight[d]:
                continue
            spr = RC.entry_spread_bp(cache, t["symbol"], t["entry_ts"], box)
            if spr is not None:
                net[v][d].append(t["ret"] * 1e4 - spr)
RC.save_cache(cache)
n_ref = sum(len(x) for x in net["np_lob"].values())
print(f"tight leash read {days[0]}..{days[-1]}: reference np_lob tight trades {n_ref} (bar needs >= 60)")
for v in VARS:
    allx = [x for d in days for x in net[v].get(d, [])]
    if allx:
        print(f"  {v:15s} tight trades {len(allx):4d} net {statistics.fmean(allx):+.1f} bp/trade")
for v in ("np_lob_slow30", "np_lob_step02"):
    diffs = [statistics.fmean(net[v][d]) - statistics.fmean(net["np_lob"][d]) for d in days if net[v].get(d) and net["np_lob"].get(d)]
    if not diffs:
        continue
    m = statistics.fmean(diffs)
    t = m / (statistics.stdev(diffs) / math.sqrt(len(diffs))) if len(diffs) > 2 and statistics.stdev(diffs) > 0 else float("nan")
    neg = sum(x < 0 for x in diffs) / len(diffs)
    ok = n_ref >= 60 and m >= 3 and t >= 2 and neg <= 0.2
    print(f"  {v} minus np_lob: {m:+.1f} bp/tight trade over {len(diffs)} days (t {t:+.2f}, negative days {neg:.0%}) -> "
          + ("PASS: earns the paper A/B" if ok else ("direction only (fewer than 60 reference trades)" if n_ref < 60 else "FAIL")))
