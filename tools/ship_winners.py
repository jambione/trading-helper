#!/usr/bin/env python3
"""Ship rule (running list row 82) applied to a sweep's report.json. Prints the proposal; --apply edits
config/bot_config.json (one commit is still the operator's / session's step). KEEP = screen verdict KEEP and the confirm stage is
not negative (CONFIRMED, or NO_DATA/partial with mean day delta >= 0). One KEEP per knob: the larger mean day delta wins.
usage: tools/ship_winners.py ~/sweeps/<name>/report.json docs/sweeps/<name>.json [--apply]"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main(argv):
    if len(argv) < 3:
        print(__doc__); return 2
    rep = json.load(open(os.path.expanduser(argv[1]))); spec = json.load(open(argv[2])); apply = "--apply" in argv
    picks = []
    for v, r in (rep.get("variants") or {}).items():
        s, c = r.get("screen") or {}, r.get("confirm") or {}
        if s.get("verdict") != "KEEP":
            continue
        cv = c.get("verdict", "NO_DATA")
        cm = c.get("mean_day_delta")
        if cv == "NOT_CONFIRMED" or (cv == "NO_DATA" and cm is not None and cm < 0):
            print(f"HOLD {v}: screen KEEP but confirm {cv} ({cm})"); continue
        picks.append((s.get("mean_day_delta") or 0.0, v, cv, (spec["variants"].get(v) or {}).get("set") or {}))
    best = {}
    for delta, v, cv, st in sorted(picks, reverse=True):
        if not st or any(k in best for k in st):
            print(f"SKIP {v}: knob already taken or no config change"); continue
        for k, val in st.items():
            best[k] = (val, v, delta, cv)
    if not best:
        print("nothing to ship"); return 0
    for k, (val, v, delta, cv) in best.items():
        print(f"SHIP {k} = {val}   [{v}: screen mean {delta:+.1f} bp/day, confirm {cv}]")
    if apply:
        f = os.path.join(ROOT, "config/bot_config.json"); cfg = json.load(open(f))
        for k, (val, *_rest) in best.items():
            cfg[k] = val
        json.dump(cfg, open(f, "w"), indent=2); open(f, "a").write("\n")
        print("applied to config/bot_config.json - review the diff, then commit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
