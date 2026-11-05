#!/usr/bin/env python3
"""Where does the live-vs-replay gap come from? (operator 11/05)
Matches each replay baseline trade (~/sweeps/<name>/<day>_baseline.json 'closed') to the live outcome for the same symbol whose
entry_time is within 180 s of the replay's entry_ts, then splits the per-trade return difference into
  entry: -(live entry / replay entry - 1)  (> 0 when live bought cheaper)
  exit : (live exit / replay exit - 1)     (> 0 when live sold higher)
  total ~ live return - replay return (return = exit/entry - 1, no cost), plus hold time and exit-reason mix.
Matched trades only (replay reproduces ~60% of live entries); live-only and replay-only trades are counted, not scored.
usage: live_vs_replay_gap.py [sweep_dir ...]   (default: ~/sweeps/night_grid_2026-11-04)"""
from __future__ import annotations

import collections
import glob
import json
import os
import re
import statistics as st
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ET = ZoneInfo("America/New_York")
TOL_S = 180.0


def live_trades():
    out, seen = collections.defaultdict(list), set()
    for p in sorted(glob.glob(os.path.join(ROOT, "ai_reports/tapes/2026-*/outcomes.jsonl"))) + [os.path.join(ROOT, "ai_reports/outcomes.jsonl")]:
        try:
            lines = open(p).read().splitlines()
        except OSError:
            continue
        for l in lines:
            try:
                o = json.loads(l)
            except ValueError:
                continue
            e, x, t, s = o.get("entry_price"), o.get("exit_price"), o.get("entry_time"), str(o.get("symbol") or "")
            if not (isinstance(e, (int, float)) and e > 0 and x and t) or not re.fullmatch(r"[A-Z]{1,5}", s):
                continue
            k = (s, round(t))
            if k in seen:
                continue
            seen.add(k)
            d = datetime.fromtimestamp(t, ET).strftime("%Y-%m-%d")
            out[d].append({"s": s, "t": float(t), "e": float(e), "x": float(x), "xt": float(o.get("exit_time") or t),
                           "reason": str(o.get("close_reason") or "?")})
    return out


def main(dirs):
    live = live_trades()
    rows, n_replay, n_live_only = [], 0, 0
    for d in dirs:
        for f in sorted(glob.glob(os.path.join(os.path.expanduser(d), "2026-*_baseline.json"))):
            day = os.path.basename(f)[:10]
            try:
                closed = json.load(open(f))["closed"]
            except Exception:  # noqa: BLE001
                continue
            used = set()
            for a in closed:
                n_replay += 1
                c = [(abs(l["t"] - a["entry_ts"]), i, l) for i, l in enumerate(live.get(day, ())) if l["s"] == a["symbol"]
                     and abs(l["t"] - a["entry_ts"]) <= TOL_S and (day, i) not in used]
                if not c:
                    continue
                _, i, l = min(c, key=lambda z: z[0]); used.add((day, i))
                er, xr = float(a["entry_px"]), float(a["exit_px"])
                rows.append({"day": day, "sym": a["symbol"],
                             "entry_bp": -(l["e"] / er - 1) * 1e4, "exit_bp": (l["x"] / xr - 1) * 1e4,
                             "ret_live": (l["x"] / l["e"] - 1) * 1e4, "ret_replay": (xr / er - 1) * 1e4,
                             "hold_live": l["xt"] - l["t"], "hold_replay": float(a["exit_ts"]) - float(a["entry_ts"]),
                             "r_live": l["reason"], "r_replay": str(a.get("reason") or "?")})
    if not rows:
        print("no matched trades"); return 1
    m = lambda k: st.mean(r[k] for r in rows)
    print(f"replay trades {n_replay}, matched to a live entry within {TOL_S:.0f} s: {len(rows)} on {len({r['day'] for r in rows})} days")
    print(f"mean return   live {m('ret_live'):+.1f} bp | replay {m('ret_replay'):+.1f} bp | gap {m('ret_live')-m('ret_replay'):+.1f} bp")
    print(f"  entry component (live bought cheaper = +): {m('entry_bp'):+.1f} bp   exit component (live sold higher = +): {m('exit_bp'):+.1f} bp")
    print(f"  win rate live {100*sum(r['ret_live']>0 for r in rows)/len(rows):.0f}% | replay {100*sum(r['ret_replay']>0 for r in rows)/len(rows):.0f}%")
    print(f"  median hold live {st.median(r['hold_live'] for r in rows):.0f} s | replay {st.median(r['hold_replay'] for r in rows):.0f} s")
    by = collections.defaultdict(list)
    for r in rows:
        by[r["r_live"]].append(r)
    print("by LIVE exit reason (n, live ret, replay ret, exit component):")
    for k, v in sorted(by.items(), key=lambda kv: -len(kv[1]))[:6]:
        print(f"  {k:18s} n{len(v):3d} live {st.mean(x['ret_live'] for x in v):+6.1f} replay {st.mean(x['ret_replay'] for x in v):+6.1f} exit {st.mean(x['exit_bp'] for x in v):+6.1f}")
    bad = sorted(rows, key=lambda r: r["ret_live"] - r["ret_replay"])[:5]
    print("5 largest live shortfalls:", [(r["day"][5:], r["sym"], round(r["ret_live"] - r["ret_replay"])) for r in bad])
    for lab, sel in (("entry component < -5 bp", lambda r: r["entry_bp"] < -5), ("exit component < -5 bp", lambda r: r["exit_bp"] < -5)):
        v = [r for r in rows if sel(r)]
        print(f"{lab}: {len(v)} of {len(rows)} trades ({100*len(v)/len(rows):.0f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or ["~/sweeps/night_grid_2026-11-04"]))
