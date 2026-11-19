#!/usr/bin/env python3
"""quotes_parallel - fetch a study's SIP spread plan with N workers (each its own cache file), then merge into the
shared news_source quotes cache. Request latency (~2 s), not the rate limit, was the bottleneck (11/19).

    .venv/bin/python tools/studies/quotes_parallel.py breadth_wave wave 3
    .venv/bin/python tools/studies/quotes_parallel.py movers_cap all 3
Never run while another process writes the shared cache.
"""
from __future__ import annotations

import hashlib
import json
import sys
from multiprocessing import Process
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))
import news_source as ns  # noqa: E402


def plan_for(study, which):
    if study == "breadth_wave":
        import breadth_wave as B
        rows = B.sample(B.load_events())
        if which != "all":
            rows = [r for r in rows if r["wave"] == (which == "wave")]
        return sorted({("bw", r["day"], r["sym"], r["t"], 0.0) for r in rows})
    if study == "movers_cap":
        import movers_cap as M
        sets = M.plan_sets(json.loads((M.OUT / "signals.json").read_text()))
        return sorted({("mc", r["day"], r["sym"], r["t"], 0.0) for v in sets.values() for r in v})
    if study == "options_callflow":
        import options_callflow as O
        return sorted({("ocf", r["day"], r["sym"], r["t"], 0.0) for r in O.load_events()})
    if study == "leaders_source":
        import leaders_source as LS
        return LS.plan()
    raise SystemExit(f"unknown study {study}")


def shard(plan, i, n):
    return [p for p in plan if int(hashlib.sha256(ns.qkey(p[1], p[2], p[3]).encode()).hexdigest(), 16) % n == i]


def merge(parts):
    main = json.load(open(ns.QUOTES)) if ns.QUOTES.exists() else {}
    for p in parts:
        if p.exists():
            main.update(json.load(open(p)))
    tmp = ns.QUOTES.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(main))
    tmp.replace(ns.QUOTES)
    return len(main)


def main():
    study, which, n = sys.argv[1], sys.argv[2], int(sys.argv[3])
    plan = plan_for(study, which)
    have = set(json.load(open(ns.QUOTES))) if ns.QUOTES.exists() else set()
    tag = "" if study == "breadth_wave" else f"_{study}"     # own part files per study so two fetches never share one
    parts = [ns.OUT / f"quotes_part{tag}{i}.json" for i in range(n)]
    print(study, which, "plan", len(plan), "already cached", sum(ns.qkey(p[1], p[2], p[3]) in have for p in plan), flush=True)
    procs = [Process(target=ns.fetch_quotes, args=(shard(plan, i, n),), kwargs={"path": parts[i], "skip": have}) for i in range(n)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
    print("merged cache size", merge(parts), flush=True)


if __name__ == "__main__":
    main()
