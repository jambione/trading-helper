#!/usr/bin/env python3
"""Experiment registry: one record per idea, one fixed pipeline, a generated board. Read/validate/render only (no trading side effects).

    .venv/bin/python tools/experiments.py validate     # exit 1 on a rule violation
    .venv/bin/python tools/experiments.py board        # writes docs/EXPERIMENTS.md from docs/experiments/registry.json

Pipeline: idea -> prereg -> skeptic -> tool -> smoke (new code path provably fires) -> data_check (inputs/controls sane) -> running -> verdict.
A 'pending' gate on a running/queued item is a warning on the board until evidence clears it. A record may not be 'running' without prereg/tool/smoke, nor hold a verdict (pass/fail/underpowered/void) without data_check.
Gate values: True | False | 'legacy' (pre-pipeline, grandfathered) | 'n/a'.
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REG = ROOT / "docs" / "experiments" / "registry.json"
OUT = ROOT / "docs" / "EXPERIMENTS.md"
STATUSES = ("idea", "queued", "running", "collecting", "blocked", "open", "verdict_pass", "verdict_fail", "underpowered", "void", "done", "parked")
VERDICTS = {"verdict_pass", "verdict_fail", "underpowered", "void"}
CLOSED = VERDICTS | {"done", "parked"}
LAYERS = ("exit", "entry", "sizing", "live-code", "process", "forward", "data")
OK = (True, "legacy", "n/a")


def ok(v):
    return v is True or v in ("legacy", "n/a")


def warnings(items):
    """Gates marked 'pending' on a running/queued item: allowed, but shown on the board until real evidence clears them."""
    return [f"{r['id']}: gate '{k}' pending ({r['title']})" for r in items if r.get("status") in ("running", "queued")
            for k, v in (r.get("gates") or {}).items() if v == "pending"]


def validate(items):
    errs, seen = [], set()
    for r in items:
        i = r.get("id", "?")
        if i in seen:
            errs.append(f"{i}: duplicate id")
        seen.add(i)
        if r.get("status") not in STATUSES:
            errs.append(f"{i}: bad status {r.get('status')!r}")
        if r.get("layer") not in LAYERS:
            errs.append(f"{i}: bad layer {r.get('layer')!r}")
        g = r.get("gates") or {}
        st = r.get("status")
        if st == "running" or st in VERDICTS:
            for k in ("prereg", "tool"):
                if g and not ok(g.get(k)):
                    errs.append(f"{i}: {st} without gate '{k}'")
        if st in ("running", "queued") and g and not ok(g.get("smoke")) and g.get("smoke") != "pending":
            errs.append(f"{i}: {st} without a passed smoke check")
        if st in VERDICTS and g and not ok(g.get("data_check")):
            errs.append(f"{i}: verdict {st} without a passed data_check")
        if st == "blocked" and not r.get("blocked_on"):
            errs.append(f"{i}: blocked without blocked_on")
        if st not in CLOSED and not r.get("next"):
            errs.append(f"{i}: open item without a next action")
        if not r.get("title") or not r.get("evidence"):
            errs.append(f"{i}: missing title or evidence")
    return errs


def load():
    return json.loads(REG.read_text())


def board(data):
    items = data["items"]
    by_status = Counter(r["status"] for r in items)
    lines = [f"# Experiment board (generated from docs/experiments/registry.json, {data.get('updated', '')})", "",
             "Pipeline: " + " -> ".join(data["pipeline"]) + ". Edit the registry, not this file; run `tools/experiments.py board`.", "",
             "## Counts", "", "| status | n |", "|---|---|"]
    lines += [f"| {s} | {by_status[s]} |" for s in STATUSES if by_status[s]]
    w = warnings(items)
    attn = [r for r in items if r["status"] in ("blocked", "open")]
    lines += ["", "## Needs attention", ""]
    for r in attn:
        lines.append(f"- **{r['id']}** [{r['status']}] {r['title']} - {r['evidence']} Next: {r['next']}" + (f" (blocked on: {r['blocked_on']})" if r.get("blocked_on") else ""))
    if w:
        lines += ["", "### Gate warnings (pending evidence)", ""] + [f"- {x}" for x in w]
    live = [r for r in items if r["status"] in ("running", "queued", "collecting")]
    lines += ["", "## In flight", ""]
    for r in live:
        lines.append(f"- **{r['id']}** [{r['status']}] {r['title']} - {r['evidence']} Next: {r['next']}")
    pend = [r for r in items if r["status"] == "idea"]
    lines += ["", "## Not started", ""]
    for r in pend:
        lines.append(f"- **{r['id']}** {r['title']} ({r['source']}) - {r['evidence']} Next: {r['next']}")
    lines += ["", "## By layer", ""]
    grp = defaultdict(list)
    for r in items:
        grp[r["layer"]].append(r)
    for L in LAYERS:
        if not grp[L]:
            continue
        lines += [f"### {L}", "", "| id | idea | source | status | evidence |", "|---|---|---|---|---|"]
        for r in grp[L]:
            lines.append(f"| {r['id']} | {r['title']} | {r['source']} | {r['status']} | {r['evidence']} |")
        lines.append("")
    return "\n".join(lines) + "\n"


def main(argv=None):
    a = (argv if argv is not None else sys.argv[1:]) or ["validate"]
    data = load()
    errs = validate(data["items"])
    if a[0] == "validate":
        print("\n".join(errs) if errs else f"ok: {len(data['items'])} records")
        return 1 if errs else 0
    if errs:
        print("registry invalid; fix before rendering:\n" + "\n".join(errs))
        return 1
    OUT.write_text(board(data))
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
