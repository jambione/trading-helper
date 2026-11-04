#!/usr/bin/env python3
"""sweep_replays.py: test many config variants against recorded sessions, fast, in two stages.

    .venv/bin/python tools/sweep_replays.py run    docs/sweeps/<name>.json [--stage screen|confirm|both]
                                                   [--offline] [--workers 8]
    .venv/bin/python tools/sweep_replays.py report docs/sweeps/<name>.json

How it saves time (2026-11-04):
  * Per day, the BASELINE arm runs first (online, one baseline at a time: it is the arm
    that downloads). Every variant of that day starts after it, reading the shared
    on-disk caches (tools/replay_session.JsonCache), so variants cost simulation only.
  * --offline (REPLAY_OFFLINE=1): no network at all, so it can run during market hours at
    nice 19 without touching the desk's Alpaca budget. Only days already warmed online.
    Each run reports its offline cache misses; a variant with misses is flagged.
  * Resumable: a run whose result JSON exists is skipped.

Two stages, bars fixed here (not after looking):
  SCREEN  (recent days):   KEEP if mean day delta > 0 AND >= 60% of days better.
  CONFIRM (older days, only variants that were KEPT): CONFIRMED if mean day delta > 0,
           day-t >= 1.5, AND both halves of the confirm days (by date) have mean > 0.
  Day delta = sum over the day of (variant net - baseline net), net = trade bp - 3 bp.

A CONFIRMED config-only variant can go live as a config edit (hot reload, no restart).
Replays reproduce ~60% of live entries and cannot judge fills or spread: execution changes
and changes to which names get seated still need a short live read after CONFIRMED.

Spec (JSON):
  {"name": "...", "sha": "HEAD", "start": "09:30", "end": "15:50",
   "baseline": {"live_book": true, "set": {}},
   "variants": {"hold_off": {"live_book": true, "set": {"ai_trail_exit_trend_hold": false}}},
   "screen_days": ["2026-10-26", ...], "confirm_days": ["2026-09-24", ...]}
Results: ~/sweeps/<name>/<day>_<arm>.json, REPORT.md, report.json, shipped_config.json.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import statistics as st
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
ET = ZoneInfo("America/New_York")
RT_BP = 3.0
SCREEN_MIN_SHARE = 0.60
CONFIRM_MIN_T = 1.5


# ---------------------------------------------------------------- scoring (pure)
def trades(path: Path) -> list[dict] | None:
    if not path.exists():
        return None
    j = json.loads(path.read_text())
    return [t for t in (j.get("closed") or []) if t.get("entry_px")]


def offline_misses(path: Path) -> int:
    try:
        v = json.loads(path.read_text()).get("vacuity") or {}
    except (OSError, ValueError):
        return 0
    return int(v.get("offline_cache_misses") or 0) + int(v.get("offline_blocked_connects") or 0)


def bp(t: dict) -> float:
    return float(t["ret"]) * 1e4


def day_net(tr: list[dict]) -> float:
    return sum(bp(t) - RT_BP for t in tr)


def tstat(v: list[float]) -> float | None:
    if len(v) < 3:
        return None
    s = st.stdev(v)
    return st.mean(v) / (s / math.sqrt(len(v))) if s > 0 else None


def never_green_change(base: list[dict], arm: list[dict]) -> tuple[int, float | None]:
    """For baseline trades whose peak stayed under entry +5 bp, the mean change in bp of the
    same entry (symbol, entry within 5 s) in the variant."""
    g = []
    for t in base:
        pk = t.get("peak_px")
        if pk is None or (float(pk) / float(t["entry_px"]) - 1) * 1e4 >= 5:
            continue
        m = [o for o in arm if o["symbol"] == t["symbol"] and abs(o["entry_ts"] - t["entry_ts"]) <= 5]
        if m:
            g.append(bp(m[0]) - bp(t))
    return len(g), (st.mean(g) if g else None)


def compare(per_day: dict[str, tuple[list[dict], list[dict]]]) -> dict:
    """per_day: {day: (baseline trades, variant trades)} -> summary."""
    days = sorted(per_day)
    deltas = {d: day_net(per_day[d][1]) - day_net(per_day[d][0]) for d in days}
    base = [t for d in days for t in per_day[d][0]]
    arm = [t for d in days for t in per_day[d][1]]
    ng = [never_green_change(*per_day[d]) for d in days]
    ng_n = sum(n for n, _ in ng)
    ng_mean = (sum(n * m for n, m in ng if m is not None) / ng_n) if ng_n else None
    half = len(days) // 2
    h1 = [deltas[d] for d in days[:half]] if half else []
    h2 = [deltas[d] for d in days[half:]]
    return {
        "days": len(days), "per_day": deltas,
        "mean_day_delta": st.mean(deltas.values()) if deltas else None,
        "day_t": tstat(list(deltas.values())),
        "share_days_better": (sum(1 for x in deltas.values() if x > 0) / len(deltas)) if deltas else None,
        "half_1_mean": st.mean(h1) if h1 else None, "half_2_mean": st.mean(h2) if h2 else None,
        "base_n": len(base), "arm_n": len(arm),
        "base_net_per_trade": (st.mean(bp(t) - RT_BP for t in base) if base else None),
        "arm_net_per_trade": (st.mean(bp(t) - RT_BP for t in arm) if arm else None),
        "arm_win": (sum(1 for t in arm if bp(t) > 0) / len(arm)) if arm else None,
        "never_green_n": ng_n, "never_green_change_bp": ng_mean,
    }


def screen_verdict(c: dict) -> str:
    if not c["days"]:
        return "NO_DATA"
    ok = (c["mean_day_delta"] or 0) > 0 and (c["share_days_better"] or 0) >= SCREEN_MIN_SHARE
    return "KEEP" if ok else "DROP"


def confirm_verdict(c: dict) -> str:
    if c["days"] < 4:
        return "NO_DATA"
    ok = ((c["mean_day_delta"] or 0) > 0 and (c["day_t"] or 0) >= CONFIRM_MIN_T
          and (c["half_1_mean"] or 0) > 0 and (c["half_2_mean"] or 0) > 0)
    return "CONFIRMED" if ok else "NOT_CONFIRMED"


# ---------------------------------------------------------------- running
def _set_args(sets: dict) -> list[str]:
    out = []
    for k, v in (sets or {}).items():
        out += ["--set", f"{k}={v if isinstance(v, str) else json.dumps(v)}"]
    return out


class Sweep:
    def __init__(self, spec_path: Path, out_root: Path | None = None):
        self.spec = json.loads(Path(spec_path).read_text())
        self.name = self.spec["name"]
        self.out = (out_root or Path.home() / "sweeps") / self.name
        self.out.mkdir(parents=True, exist_ok=True)
        sha = self.spec.get("sha") or "HEAD"
        self.sha = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "--short", sha], text=True).strip()
        cfg = self.out / "shipped_config.json"
        if not cfg.exists():
            shutil.copy2(ROOT / "config" / "bot_config.json", cfg)
        self.cfg = cfg

    def arms(self) -> dict[str, dict]:
        return {"baseline": self.spec.get("baseline") or {}, **(self.spec.get("variants") or {})}

    def path(self, day: str, arm: str) -> Path:
        return self.out / f"{day}_{arm}.json"

    def cmd(self, day: str, arm: str) -> list[str]:
        a = self.arms()[arm]
        py = str(ROOT / ".venv" / "bin" / "python")
        c = ["nice", "-n", "19", py, "-u", str(ROOT / "tools" / "replay_session.py"), "--day", day,
             "--sha", self.sha, "--set-json", str(self.cfg)]
        c += _set_args({**(self.spec.get("base_set") or {}), **(a.get("set") or {})})
        c += ["--start", self.spec.get("start", "09:30"), "--end", self.spec.get("end", "15:50"), "--exits", "live"]
        if a.get("live_book", True):
            c.append("--live-book")
        return c + ["--out", str(self.path(day, arm))]

    def run(self, days: list[str], workers: int, offline: bool, no_start: tuple[int, int] | None,
            log=print) -> None:
        env = dict(os.environ, REPLAY_MIN_FREE_GB="8", PYTHONHASHSEED="0")
        sec = ROOT / "secrets_data.json"
        if sec.exists():
            env["TH_DATA_SECRETS"] = str(sec)
        if offline:
            env["REPLAY_OFFLINE"] = "1"
        todo = [(d, a) for d in days for a in self.arms()]
        tries: dict[tuple, int] = {}
        running: dict[tuple, subprocess.Popen] = {}
        while True:
            for k, p in list(running.items()):
                if p.poll() is not None:
                    running.pop(k)
                    log(f"done {k[0]} {k[1]} rc={p.returncode} {'ok' if self.path(*k).exists() else 'NO RESULT'}")
            pending = [k for k in todo if not self.path(*k).exists() and k not in running and tries.get(k, 0) < 2]
            if not pending and not running:
                break
            hm = int(datetime.now(ET).strftime("%H%M"))
            blocked = no_start is not None and no_start[0] <= hm < no_start[1] and not offline
            baseline_busy = any(a == "baseline" for _, a in running)
            started = False
            for k in pending:
                if blocked or len(running) >= workers:
                    break
                d, a = k
                if a == "baseline":
                    if baseline_busy and not offline:
                        continue           # one downloader at a time
                elif not self.path(d, "baseline").exists():
                    continue               # variants wait for their day's warm baseline
                tries[k] = tries.get(k, 0) + 1
                logf = open(self.out / f"{d}_{a}.log", "w")
                running[k] = subprocess.Popen(self.cmd(d, a), cwd=str(ROOT), env=env, stdout=logf,
                                              stderr=subprocess.STDOUT)
                log(f"start {d} {a} try {tries[k]}")
                baseline_busy = baseline_busy or a == "baseline"
                started = True
            if blocked and not running:
                log("no-start window and nothing running: stopping")
                break
            time.sleep(5 if started else 15)

    def report(self) -> dict:
        res = {"name": self.name, "sha": self.sha, "variants": {}}
        for stage in ("screen", "confirm"):
            days = self.spec.get(f"{stage}_days") or []
            for arm in self.spec.get("variants") or {}:
                per = {}
                miss = 0
                for d in days:
                    b, v = trades(self.path(d, "baseline")), trades(self.path(d, arm))
                    if b is not None and v is not None:
                        per[d] = (b, v)
                        miss += offline_misses(self.path(d, arm)) + offline_misses(self.path(d, "baseline"))
                c = compare(per)
                c["offline_misses"] = miss
                c["verdict"] = screen_verdict(c) if stage == "screen" else confirm_verdict(c)
                res["variants"].setdefault(arm, {})[stage] = c
        (self.out / "report.json").write_text(json.dumps(res, indent=1, default=float))
        lines = [f"# Sweep {self.name} (sha {self.sha}, written {datetime.now(ET):%Y-%m-%d %H:%M} ET)", "",
                 "Net = trade bp - 3 bp. Day delta = variant net - baseline net, summed per day.",
                 f"SCREEN keep: mean day delta > 0 and >= {SCREEN_MIN_SHARE:.0%} of days better. "
                 f"CONFIRM: mean > 0, day-t >= {CONFIRM_MIN_T}, both halves > 0.", "",
                 "| variant | stage | verdict | days | mean day delta bp | day-t | days better | net/trade base -> arm | never-green change (n) | offline misses |",
                 "|---|---|---|---|---|---|---|---|---|---|"]

        def f(x, p=1):
            return "-" if x is None else f"{x:+.{p}f}"
        for arm, stg in res["variants"].items():
            for stage, c in stg.items():
                better = "-" if c["share_days_better"] is None else f"{c['share_days_better']:.0%}"
                lines.append(f"| {arm} | {stage} | **{c['verdict']}** | {c['days']} | {f(c['mean_day_delta'], 0)} | "
                             f"{f(c['day_t'], 2)} | {better} | "
                             f"{f(c['base_net_per_trade'])} -> {f(c['arm_net_per_trade'])} | "
                             f"{f(c['never_green_change_bp'])} ({c['never_green_n']}) | {c['offline_misses']} |")
        (self.out / "REPORT.md").write_text("\n".join(lines) + "\n")
        return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["run", "report"])
    ap.add_argument("spec")
    ap.add_argument("--stage", choices=["screen", "confirm", "both"], default="both")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--no-start", default="0730-1600",
                    help="HHMM-HHMM ET window with no new ONLINE starts (protects the desk's API budget); '' = none")
    a = ap.parse_args(argv)
    sw = Sweep(Path(a.spec))
    if a.cmd == "run":
        ns = None
        if a.no_start:
            lo, hi = a.no_start.split("-")
            ns = (int(lo), int(hi))
        stages = ["screen", "confirm"] if a.stage == "both" else [a.stage]
        for stage in stages:
            days = sw.spec.get(f"{stage}_days") or []
            if stage == "confirm":
                keep = {k for k, v in (sw.report()["variants"]).items() if v.get("screen", {}).get("verdict") == "KEEP"}
                if not keep:
                    print("confirm: no variant passed the screen")
                    break
                full = dict(sw.spec["variants"])
                sw.spec["variants"] = {k: v for k, v in full.items() if k in keep}
            sw.run(days, a.workers, a.offline, ns)
            if stage == "confirm":
                sw.spec["variants"] = full
    sw.report()
    print((sw.out / "REPORT.md").read_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
