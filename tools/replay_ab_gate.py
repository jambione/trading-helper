#!/usr/bin/env python3
"""Replay A/B gate: replay a proposed config change against the live config before it ships.

docs/PLAN_2026-10-14.md section 3 ("A/B every config change on replay before it ships").
Live config has churned faster than evidence (~3.5 ships per session vs ~12 fills),
so every change died underpowered. This makes the replay the first reader.

Both arms pin a WHOLE bot_config via replay_session --set-json: base = config/bot_config.json
at --base-sha (default HEAD), candidate = base + --set overrides (or --candidate FILE).
Same code, same recorded sessions. Mini only, after 16:15 ET, <= 3 replays on the box.

    .venv/bin/python tools/replay_ab_gate.py --set ai_no_progress_sec=90 --days 5
    .venv/bin/python tools/replay_ab_gate.py --candidate /tmp/cand.json --days 2026-10-12,2026-10-13

It is a regression gate, not a study: it answers "does this change make the replayed
book thinner, or its trades or charged day totals worse?" A change that is meant to
improve trading still needs its own prereg.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools" / "studies"))
import no_green_scratch_score as SC  # noqa: E402  (pair, charged_total, opens_per_10m, RATE_LIMIT_RE)

ET = ZoneInfo("America/New_York")
SNAP = Path(os.getenv("SESSION_SNAPSHOT_DIR") or Path.home() / "session_snapshots")
MAXP = 3
COST = 0.0010


# ── pure ─────────────────────────────────────────────────────────────────────

def parse_sets(sets: list[str]) -> dict:
    out = {}
    for kv in sets:
        k, _, v = kv.partition("=")
        try:
            out[k] = json.loads(v)
        except ValueError:
            out[k] = v
    return out


def recorded_days(n: int, snap: Path = SNAP) -> list[str]:
    days = sorted(p.name for p in snap.iterdir() if p.is_dir() and (p / "DONE").exists()) if snap.exists() else []
    return days[-n:]


def compare_day(base: dict, cand: dict) -> dict:
    pr = SC.pair(base["closed"], cand["closed"])
    return {
        "opens_base": SC.opens_per_10m(base), "opens_cand": SC.opens_per_10m(cand),
        "n_base": len(base["closed"]), "n_cand": len(cand["closed"]),
        "mean_base": statistics.mean(t["ret"] for t in base["closed"]) if base["closed"] else None,
        "mean_cand": statistics.mean(t["ret"] for t in cand["closed"]) if cand["closed"] else None,
        "paired": statistics.mean(y["ret"] - x["ret"] for x, y in pr) if pr else None,
        "n_paired": len(pr),
        "charged_base": SC.charged_total(base, COST), "charged_cand": SC.charged_total(cand, COST),
    }


def gate(per_day: dict[str, dict]) -> tuple[str, list[str]]:
    """REGRESSION if the candidate thins the book by > 10%, or loses on charged day totals on
    most days, or its per-trade mean is lower on most days. Otherwise NO-REGRESSION.
    UNDERPOWERED under 3 days or 30 closed trades per arm."""
    days = list(per_day.values())
    nb = sum(d["n_base"] for d in days)
    nc = sum(d["n_cand"] for d in days)
    if len(days) < 3 or nb < 30 or nc < 30:
        return "UNDERPOWERED", [f"{len(days)} days, {nb} / {nc} closed trades"]
    why = []
    ob = statistics.mean(d["opens_base"] for d in days)
    oc = statistics.mean(d["opens_cand"] for d in days)
    if ob > 0 and oc / ob < 0.9:
        why.append(f"opens {oc / ob:.2f}x of base")
    worse_total = sum(d["charged_cand"] < d["charged_base"] for d in days)
    if worse_total > len(days) / 2:
        why.append(f"charged (10 bp) day total worse on {worse_total}/{len(days)} days")
    worse_mean = sum(d["mean_cand"] is not None and d["mean_base"] is not None and d["mean_cand"] < d["mean_base"]
                     for d in days)
    if worse_mean > len(days) / 2:
        why.append(f"per-trade mean lower on {worse_mean}/{len(days)} days")
    return ("REGRESSION" if why else "NO-REGRESSION"), why


def report(per_day: dict[str, dict], verdict: str, why: list[str], label: str) -> str:
    bp = lambda x: "—" if x is None else f"{x * 1e4:+.1f}"  # noqa: E731
    L = [f"# Replay A/B gate - {label}", "",
         "| day | opens/10m base → cand | trades | mean/trade base → cand (bp) | paired diff (n) | charged day total base → cand (bp) |",
         "|---|---|---|---|---|---|"]
    for d, x in per_day.items():
        L.append(f"| {d} | {x['opens_base']:.2f} → {x['opens_cand']:.2f} | {x['n_base']} → {x['n_cand']} "
                 f"| {bp(x['mean_base'])} → {bp(x['mean_cand'])} | {bp(x['paired'])} ({x['n_paired']}) "
                 f"| {bp(x['charged_base'])} → {bp(x['charged_cand'])} |")
    L += ["", f"**{verdict}**" + (": " + "; ".join(why) if why else ""),
          "", "Gross replay at recorded prices, 10 bp charged per round trip in the day totals. A gate, not a study."]
    return "\n".join(L)


# ── running (mini) ───────────────────────────────────────────────────────────

def _et_hhmm() -> int:
    d = datetime.now(ET)
    return d.hour * 100 + d.minute


def _running() -> int:
    r = subprocess.run(["pgrep", "-f", "tools/replay_session.py --day"], capture_output=True, text=True)
    return len([x for x in r.stdout.split() if x.strip()]) // 2


def run(days: list[str], base_cfg: Path, cand_cfg: Path, sha: str, out: Path, force_now: bool) -> list[str]:
    if not force_now:
        while not (1615 <= _et_hhmm() or _et_hhmm() < 330):
            time.sleep(120)
    procs, problems = [], []
    for day in days:
        for arm, cfg in (("base", base_cfg), ("cand", cand_cfg)):
            if 330 <= _et_hhmm() < 1615 and not force_now:
                problems.append(f"deadline: {day} {arm} not launched")
                continue
            while _running() >= MAXP:
                time.sleep(20)
            log = open(out / f"{day}_{arm}.log", "w")
            procs.append((day, arm, subprocess.Popen(
                [sys.executable, str(ROOT / "tools" / "replay_session.py"), "--day", day, "--sha", sha,
                 "--set-json", str(cfg), "--out", str(out / f"{day}_{arm}.json")],
                cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT)))
            time.sleep(5)
    for day, arm, p in procs:
        if p.wait() != 0:
            problems.append(f"failed: {day} {arm}")
        txt = (out / f"{day}_{arm}.log").read_text(errors="replace")
        if SC.RATE_LIMIT_RE.search(txt):
            problems.append(f"rate-limited: {day} {arm}")
    return problems


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    ap.add_argument("--candidate", default=None, help="a whole candidate bot_config.json")
    ap.add_argument("--base-sha", default="HEAD")
    ap.add_argument("--days", default="5", help="N most recent recorded sessions, or a comma list")
    ap.add_argument("--now", action="store_true", help="skip the after-hours wait (only when the desk is idle)")
    a = ap.parse_args()
    if not a.set and not a.candidate:
        raise SystemExit("give --set KEY=VALUE or --candidate FILE")
    if subprocess.run(["git", "-C", str(ROOT), "diff", "--quiet", "HEAD", "--"]).returncode != 0:
        raise SystemExit("tracked files differ from HEAD - the replay exports HEAD; commit first")
    sha = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], text=True).strip()
    days = a.days.split(",") if "," in a.days or "-" in a.days else recorded_days(int(a.days))
    out = Path(f"/tmp/abgate_{datetime.now():%Y%m%d_%H%M%S}")
    out.mkdir(parents=True)
    base = json.loads(subprocess.check_output(["git", "-C", str(ROOT), "show", f"{a.base_sha}:config/bot_config.json"]))
    cand = json.load(open(a.candidate)) if a.candidate else dict(base)
    cand.update(parse_sets(a.set))
    changed = sorted(k for k in set(base) | set(cand) if base.get(k) != cand.get(k))
    if not changed:
        raise SystemExit("candidate equals base - nothing to test")
    (out / "base.json").write_text(json.dumps(base))
    (out / "cand.json").write_text(json.dumps(cand))
    label = ", ".join(f"{k}={json.dumps(cand.get(k))}" for k in changed[:8]) + (" ..." if len(changed) > 8 else "")
    print(f"out {out}  sha {sha}  days {days}  changes: {label}", flush=True)
    problems = run(days, out / "base.json", out / "cand.json", sha, out, a.now)
    per_day = {}
    for d in days:
        pb, pc = out / f"{d}_base.json", out / f"{d}_cand.json"
        if pb.exists() and pc.exists():
            per_day[d] = compare_day(json.load(open(pb)), json.load(open(pc)))
        else:
            problems.append(f"missing result: {d}")
    if problems:
        md = f"# Replay A/B gate - {label}\n\n**INCOMPLETE - no verdict.**\n\n" + "\n".join(f"- {p}" for p in problems)
    else:
        v, why = gate(per_day)
        md = report(per_day, v, why, label)
    (out / "result.md").write_text(md + "\n")
    print(md)


if __name__ == "__main__":
    main()
