#!/usr/bin/env python3
"""nightly.py — the after-close checks, one after another, for one session.

Started by tools/session_snapshot.py when it closes the day (16:05 ET), in the
background. Steps run in sequence (never more than one replay at a time), and
a failed step does not stop the next:

  1. catalyst   tools/ai_catalyst_score.py (forward-return scorecard for the
                AI news catalyst shadow logger; quick; runs first)
  2. fidelity   tools/replay_session.py --fidelity (the approximate replay's
                score against live buys; writes fidelity.json in the archive)
  3. exact      tools/replay_session.py --exact with the code that booted the
                desk: the acceptance verdict (0 read misses, >=99% decisions,
                >=95% buys within 5 s) -> ai_reports/nightly/DAY/exact.json
  4. paper      (manual only: --only paper; the operator chose on 2026-09-26
                not to spend weeks accumulating it)
                tools/studies/source_optimal_study.py on the day's actual
                nominations, SIP bars and real SIP spreads, for the live %R
                trigger, the volume breakout and combination D. Each day's rows
                are appended to ai_reports/nightly/paper_book.jsonl, and the
                running totals (trade-weighted net bp, days positive) are
                rebuilt, so the SIP question is answered by accumulating
                out-of-sample days rather than by one backtest.

Writes ai_reports/nightly/DAY/summary.md.

USAGE (on the mini)
  .venv/bin/python tools/nightly.py --day 2026-09-28
  .venv/bin/python tools/nightly.py --day 2026-09-28 --only paper
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
ET = ZoneInfo("America/New_York")
TRIGGERS = ("live", "vol_brk", "combo_d")


def py() -> str:
    p = ROOT / ".venv" / "bin" / "python"
    return str(p if p.exists() else sys.executable)


def run(cmd: list[str], log: Path, timeout: float) -> int:
    with open(log, "ab") as f:
        f.write(f"\n=== {datetime.now(ET):%H:%M:%S} {' '.join(cmd)}\n".encode())
        f.flush()
        try:
            return subprocess.call(cmd, cwd=str(ROOT), stdout=f, stderr=subprocess.STDOUT,
                                   timeout=timeout, env=dict(os.environ, REPLAY_REPO=str(ROOT)))
        except subprocess.TimeoutExpired:
            f.write(b"TIMEOUT\n")
            return -9


def boot_sha(day: str) -> str | None:
    """sha of the desk process running at 09:30 (desk_io boot marker)."""
    t_open = datetime.strptime(day, "%Y-%m-%d").replace(hour=9, minute=30, tzinfo=ET).timestamp()
    d0 = datetime.strptime(day, "%Y-%m-%d")
    best = None
    for back in range(0, 11):
        w = ROOT / "ai_reports" / "sessions" / (d0 - timedelta(days=back)).strftime("%Y-%m-%d") / "wire.jsonl.gz"
        if not w.exists():
            continue
        for line in gzip.open(w, "rt"):
            if '"ch":"boot"' not in line and '"ch": "boot"' not in line:
                continue
            r = json.loads(line)
            if float(r["ts"]) <= t_open and (best is None or float(r["ts"]) > best[0]):
                best = (float(r["ts"]), r.get("sha"))
        if best:
            break
    return best[1] if best else None


def step_exact(day: str, out: Path, log: Path) -> dict:
    sha = boot_sha(day)
    if not sha:
        return {"ok": False, "why": "no desk_io boot marker before 09:30"}
    res = out / "exact.json"
    rc = run([py(), "-u", str(ROOT / "tools" / "replay_session.py"), "--exact", "--day", day,
              "--start", "09:30", "--end", "15:50", "--sha", sha, "--out", str(res)], log, 3 * 3600)
    if not res.exists():
        return {"ok": False, "why": f"exact replay produced no result (rc {rc})", "sha": sha}
    r = json.loads(res.read_text())
    io_ = r.get("io") or {}
    return {"ok": True, "sha": sha, "verdict": r.get("verdict"),
            "misses": sum((io_.get("missed") or {}).values()),
            "decision_agreement": r.get("decision_agreement"), "checks": r.get("checks"),
            "buy_recall": r.get("buy_recall"), "live_buys": r.get("live_buys"),
            "errors": r.get("errors")}


def step_paper(day: str, out: Path, log: Path) -> dict:
    hist = ROOT / "ai_reports" / "nightly" / "paper_book.jsonl"
    done = {}
    for trig in TRIGGERS:
        rc = run([py(), "-u", str(ROOT / "tools" / "studies" / "source_optimal_study.py"),
                  "--noms", "sources", "--days", day, "--trigger", trig], log, 3 * 3600)
        src = ROOT / "ai_reports" / f"source_optimal_study_sources_{trig}.json"
        if rc != 0 or not src.exists():
            done[trig] = f"failed (rc {rc})"
            continue
        rows = json.loads(src.read_text())
        with open(hist, "a") as f:
            for r in rows:
                r.pop("per_day", None)
                f.write(json.dumps({"day": day, "trigger": trig, **r}, default=str) + "\n")
        done[trig] = f"{sum(r['trades'] for r in rows)} trades"
    return {"ok": True, "steps": done, "totals": paper_totals(hist)}


def paper_totals(hist: Path) -> list[dict]:
    """Running totals per trigger x exit x source across every recorded day."""
    acc = defaultdict(lambda: {"days": set(), "trades": 0, "net_sum": 0.0, "gross_sum": 0.0,
                               "cost_sum": 0.0, "days_pos": 0})
    seen = set()
    if not hist.exists():
        return []
    rows = [json.loads(line) for line in open(hist)]
    for r in reversed(rows):  # a rerun of a day replaces its earlier rows
        k = (r["day"], r["trigger"], r["exit"], r["source"])
        if k in seen:
            continue
        seen.add(k)
        a = acc[(r["trigger"], r["exit"], r["source"])]
        n = int(r.get("trades") or 0)
        a["days"].add(r["day"])
        if n and r.get("net_bp") is not None:
            a["trades"] += n
            a["net_sum"] += r["net_bp"] * n
            a["gross_sum"] += (r.get("gross_bp") or 0) * n
            a["cost_sum"] += (r.get("cost_bp") or 0) * n
            a["days_pos"] += r["net_bp"] > 0
    out = []
    for (trig, ex, src), a in sorted(acc.items()):
        n = a["trades"]
        out.append({"trigger": trig, "exit": ex, "source": src, "days": len(a["days"]), "trades": n,
                    "gross_bp": a["gross_sum"] / n if n else None,
                    "cost_bp": a["cost_sum"] / n if n else None,
                    "net_bp": a["net_sum"] / n if n else None, "days_net_positive": a["days_pos"]})
    return out


def summary_md(day: str, res: dict) -> str:
    fmt = lambda v, f: "-" if v is None else format(v, f)  # noqa: E731
    lines = [f"# Nightly {day}", ""]
    cat = res.get("catalyst") or {}
    if cat:
        lines += [f"**AI catalyst scorecard:** rc={cat.get('rc')}", ""]
    ex = res.get("exact") or {}
    if ex.get("ok"):
        lines += [f"**Exact replay (acceptance): {ex.get('verdict')}** — sha {ex.get('sha')}, "
                  f"read misses {ex.get('misses')}, decisions {fmt(ex.get('decision_agreement'), '.1%')} "
                  f"of {ex.get('checks')}, buys {fmt(ex.get('buy_recall'), '.0%')} of {ex.get('live_buys')}"
                  + (f", errors {ex.get('errors')}" if ex.get("errors") else ""), ""]
    else:
        lines += [f"**Exact replay: not run** — {ex.get('why')}", ""]
    pb = res.get("paper") or {}
    if not pb and not cat:
        return "\n".join(lines) + "\n"
    if not pb:
        return "\n".join(lines) + "\n"
    lines += ["## SIP paper book, running totals (net of real SIP spread)", "",
              "| trigger | exit | source | days | trades | gross bp | cost bp | net bp | days net>0 |",
              "|---|---|---|---|---|---|---|---|---|"]
    for r in pb.get("totals") or []:
        lines.append(f"| {r['trigger']} | {r['exit']} | {r['source']} | {r['days']} | {r['trades']} | "
                     f"{fmt(r['gross_bp'], '+.1f')} | {fmt(r['cost_bp'], '.1f')} | "
                     f"{fmt(r['net_bp'], '+.1f')} | {r['days_net_positive']} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default=datetime.now(ET).strftime("%Y-%m-%d"))
    ap.add_argument("--only", choices=("catalyst", "fidelity", "exact", "paper"), default=None)
    args = ap.parse_args()
    out = ROOT / "ai_reports" / "nightly" / args.day
    out.mkdir(parents=True, exist_ok=True)
    log = out / "nightly.log"
    res: dict = {"day": args.day, "started": time.time()}
    if args.only in (None, "catalyst"):
        rc = run([py(), "-u", str(ROOT / "tools" / "ai_catalyst_score.py"),
                  "--asof", args.day], log, 30 * 60)
        res["catalyst"] = {"rc": rc}
    if args.only in (None, "fidelity"):
        rc = run([py(), "-u", str(ROOT / "tools" / "replay_session.py"), "--day", args.day,
                  "--start", "09:00", "--end", "15:50", "--fidelity"], log, 3 * 3600)
        res["fidelity"] = {"rc": rc}
    if args.only in (None, "exact"):
        res["exact"] = step_exact(args.day, out, log)
    if args.only == "paper":
        res["paper"] = step_paper(args.day, out, log)
    res["finished"] = time.time()
    (out / "nightly.json").write_text(json.dumps(res, indent=1, default=str))
    (out / "summary.md").write_text(summary_md(args.day, res))
    print((out / "summary.md").read_text())
    return 0


if __name__ == "__main__":
    sys.exit(main())
