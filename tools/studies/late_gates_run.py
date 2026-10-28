#!/usr/bin/env python3
"""late_gates_run.py — runner for docs/studies/late_gates_replay_prereg.json (amended, amended_2, ready).

Runs tools/replay_session.py on the mini, one pinned sha for every run, outputs to ~/replay_runs/late_gates/DAY-VAR.json.
Resumable: an existing output is never rerun. Stops launching new replays at --stop-at (ET) so nothing runs into the open.

  --phase fidelity  amendment 7 / 11 / 13: replay 2026-10-27 on its recorded config with the pinned flags and
                    REPLAY_KEEP_WORK=1; write fidelity.json (buy recall/precision vs live fills, S5/WR refusal counts
                    and symbol overlap vs the live decision ledger) and the gate verdict. Every other phase refuses
                    until this verdict is written and passes.
  --phase primary   REF, S5, WR, BOTH, MDE1 per eligible session, then the two-pass seeded controls
                    (S5_RAND_1..3, WR_RAND_1..3), then the REF determinism duplicates.
  --phase info      sensitivity (S5m2, S5p2, WR10, WR20) and the Leg-1 pair (REF_L1, S5_L1, WR_L1).

USAGE (mini, after the close, not while scripts/nightly.sh runs):
  scripts/late_gates_replay.sh fidelity|primary|info [--jobs 3] [--stop-at 08:45]
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parents[2]
SNAP = Path(os.path.expanduser("~/session_snapshots"))
OUT = Path(os.path.expanduser("~/replay_runs/late_gates"))
FIRST_DAY = "2026-09-24"
WR_FIRST = "2026-10-12"
FIDELITY_DAY = "2026-10-27"
S5_EXCLUDED = ("2026-10-23", "2026-10-26")   # amendment 9: run (information), not scored

# amendment 13: the nightly fidelity flags (09:00 start, paint on), plus the pins of amendment 8.
FLAGS = ["--start", "09:00", "--end", "15:50", "--step", "2.0", "--engine", "recorded", "--exits", "live",
         "--live-staleness"]
# amendments 1 and 3: every variant pins presquare_only and the 10/27 desk regime; REF has every gate off.
PIN = {
    "ai_watch_presquare_only": True,
    "ai_watch_leg1_rth_gate": False,
    "ai_max_positions": 5,
    "ai_max_position_pct": 20,
    "ai_risk_pct": 1.0,
    "ai_watch_min_price": 20,
    "ai_position_slot_equity": 60,
    "ai_watch_wr_trend_min_rise": 0,
    "ai_watch_wr_slow_5m_veto": False,
    "ai_watch_wr_slow_5m_min_pitch": 0.0,
    "ai_watch_wr_rand_refuse_share": 0,
    "ai_watch_wr_rand_salt": "",
}
# Leg-1 live knobs of 9d83ca73 (information pair only).
LEG1 = {
    "ai_watch_leg1_rth_gate": True,
    "ai_watch_leg1_min_price": 25.0,
    "ai_watch_leg1_min_day_chg": 1.0,
    "ai_watch_leg1_max_day_chg": 4.0,
    "ai_watch_leg1_max_time": "13:00",
    "ai_watch_leg1_min_vol_mult": {"10:30": 2.2, "11:30": 1.8, "13:00": 1.5},
    "ai_watch_leg1_max_spread_pct": 0.08,
}
S5_ON = {"ai_watch_wr_slow_5m_veto": True, "ai_watch_wr_slow_5m_min_pitch": 0.0}
WR_ON = {"ai_watch_wr_trend_min_rise": 15}

PRIMARY_PASS1 = {
    "REF": {},
    "S5": S5_ON,
    "BOTH": {**S5_ON, **WR_ON},
    "MDE1": {"ai_watch_wr_rand_refuse_share": 0.25, "ai_watch_wr_rand_salt": "MDE-1"},   # amendment 15
}
WR_PASS1 = {"WR": WR_ON}
INFO_ALL = {
    "S5m2": {**S5_ON, "ai_watch_wr_slow_5m_min_pitch": -2.0},
    "S5p2": {**S5_ON, "ai_watch_wr_slow_5m_min_pitch": 2.0},
    "REF_L1": LEG1,
    "S5_L1": {**LEG1, **S5_ON},
}
INFO_WR = {
    "WR10": {"ai_watch_wr_trend_min_rise": 10},
    "WR20": {"ai_watch_wr_trend_min_rise": 20},
    "WR_L1": {**LEG1, **WR_ON},
}


def eligible_days() -> list[str]:
    return sorted(p.name for p in SNAP.iterdir()
                  if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.name) and p.name >= FIRST_DAY and (p / "DONE").exists())


def out_path(day: str, var: str) -> Path:
    return OUT / f"{day}-{var}.json"


def n_trades(day: str, var: str) -> int | None:
    p = out_path(day, var)
    if not p.exists():
        return None
    d = json.loads(p.read_text())
    return len(d.get("closed") or []) + len(d.get("open_at_end") or [])


def set_args(overrides: dict) -> list[str]:
    cfg = {**PIN, **overrides}
    out: list[str] = []
    for k in sorted(cfg):
        out += ["--set", f"{k}={json.dumps(cfg[k])}"]
    return out


def past_stop(stop_at: str) -> bool:
    h, m = (int(x) for x in stop_at.split(":"))
    now = datetime.now(ET)
    return now.weekday() < 5 and (now.hour, now.minute) >= (h, m) and now.hour < 16


def run_one(day: str, var: str, overrides: dict | None, sha: str, *, keep_work: bool = False,
            raw_sets: list[str] | None = None) -> str:
    p = out_path(day, var)
    if p.exists():
        return f"{day} {var}: exists"
    sets = raw_sets if raw_sets is not None else set_args(overrides or {})
    cmd = [str(ROOT / ".venv" / "bin" / "python"), str(ROOT / "tools" / "replay_session.py"), "--day", day,
           "--sha", sha, *FLAGS, *sets, "--out", str(p) + ".part"]
    env = dict(os.environ)
    if keep_work:
        env["REPLAY_KEEP_WORK"] = "1"
    log = OUT / "logs" / f"{day}-{var}.log"
    t0 = time.time()
    with open(log, "w") as fh:
        fh.write(" ".join(cmd) + "\n")
        fh.flush()
        rc = subprocess.call(cmd, cwd=str(ROOT), stdout=fh, stderr=subprocess.STDOUT, env=env)
    part = Path(str(p) + ".part")
    if rc == 0 and part.exists():
        part.rename(p)
        return f"{day} {var}: ok {time.time() - t0:.0f}s, {n_trades(day, var)} trades"
    return f"{day} {var}: FAILED rc {rc} (log {log})"


def batch(jobs: list[tuple], sha: str, n: int, stop_at: str) -> None:
    with cf.ThreadPoolExecutor(max_workers=n) as ex:
        futs = []
        for day, var, ov in jobs:
            futs.append(ex.submit(lambda d=day, v=var, o=ov: (
                f"{d} {v}: skipped, past {stop_at} ET" if past_stop(stop_at) else run_one(d, v, o, sha))))
        for f in cf.as_completed(futs):
            print(f.result(), flush=True)


def run_seed_pass(day: str, runs: list[tuple[str, dict]], sha: str, stop_at: str) -> bool:
    """One calibration pass: rerun every seed of a control at its new share, all seeds in parallel. False (nothing
    launched) once past stop_at."""
    if past_stop(stop_at):
        return False
    for name, _ in runs:
        p = out_path(day, name)
        if p.exists():
            p.unlink()
    with cf.ThreadPoolExecutor(max_workers=max(1, len(runs))) as ex:
        for msg in ex.map(lambda r: run_one(day, r[0], r[1], sha), runs):
            print(msg, flush=True)
    return True


def rand_share(day: str, gate: str) -> float | None:
    """1 - n(G)/n(REF) for that session (prereg replay.variants)."""
    ref, g = n_trades(day, "REF"), n_trades(day, gate)
    if ref is None or g is None:
        return None
    return max(0.0, round(1 - g / ref, 4)) if ref else 0.0


def fidelity_ok() -> bool:
    p = OUT / "fidelity.json"
    return p.exists() and bool(json.loads(p.read_text()).get("pass"))


# ── fidelity (amendments 7, 11, 13) ────────────────────────────────────────────────────────────────────────────
S5_CODES = {"wr_slow_5m_falling"}
WR_CODES = {"wr_not_trending", "wr_no_history", "wr_src_mismatch"}


def ledger_refusals(path: Path, t0: float, t1: float) -> dict[str, dict[str, int]]:
    """{gate: {symbol: rows}} for arm rows refused by each gate inside [t0, t1]."""
    out: dict[str, dict[str, int]] = {"S5": {}, "WR": {}}
    if not path.exists():
        return out
    for line in open(path):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("stage") != "arm" or r.get("arm_ok"):
            continue
        ts = float(r.get("ts") or 0)
        if not (t0 <= ts <= t1):
            continue
        why = r.get("arm_why")
        gate = "S5" if why in S5_CODES else "WR" if why in WR_CODES else None
        if gate:
            sym = str(r.get("symbol") or "").upper()
            out[gate][sym] = out[gate].get(sym, 0) + 1
    return out


def buy_match(live: list[tuple[float, str]], rep: list[tuple[float, str]]) -> int:
    """Greedy nearest match within 90 s, same rule as replay_session's fidelity."""
    used, matched = set(), 0
    for lt, ls in sorted(live):
        best = None
        for j, (rt, rs) in enumerate(rep):
            if j in used or rs != ls or abs(rt - lt) > 90:
                continue
            if best is None or abs(rt - lt) < abs(rep[best][0] - lt):
                best = j
        if best is not None:
            used.add(best)
            matched += 1
    return matched


def fidelity(sha: str) -> None:
    day = FIDELITY_DAY
    var = "FIDELITY"
    # Recorded config (S5 and WR on as live), pinned flags only: no --set at all.
    print(run_one(day, var, None, sha, keep_work=True, raw_sets=[]), flush=True)
    res = json.loads(out_path(day, var).read_text())
    log = (OUT / "logs" / f"{day}-{var}.log").read_text()
    m = re.findall(r"\[replay\] work dir: (\S+)", log)
    work = Path(m[-1]) if m else None
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
    t0 = d.replace(hour=9, minute=0).timestamp()
    t1 = d.replace(hour=15, minute=50).timestamp()
    live_ref = ledger_refusals(SNAP / day / "decision_ledger.jsonl", t0, t1)
    rep_ref = ledger_refusals(work / "ai_reports" / "decision_ledger" / f"{day}.jsonl", t0, t1) if work else \
        {"S5": {}, "WR": {}}
    live_buys = []
    for line in open(SNAP / day / "fills.jsonl"):
        r = json.loads(line)
        if r.get("event") == "fill" and str(r.get("side")).lower() == "buy" and t0 <= float(r["ts"]) <= t1:
            live_buys.append((float(r["ts"]), str(r["symbol"]).upper()))
    rep_buys = [(float(t["entry_ts"]), str(t["symbol"]).upper())
                for t in (res.get("closed") or []) + (res.get("open_at_end") or [])]
    matched = buy_match(live_buys, rep_buys)
    recall = matched / len(live_buys) if live_buys else None
    precision = matched / len(rep_buys) if rep_buys else None
    gates = {}
    for g in ("S5", "WR"):
        lv, rp = sum(live_ref[g].values()), sum(rep_ref[g].values())
        ls = set(live_ref[g])
        overlap = len(ls & set(rep_ref[g])) / len(ls) if ls else None
        ratio = rp / lv if lv else None
        testable = bool(ratio is not None and 0.5 <= ratio <= 2.0 and overlap is not None and overlap >= 0.5)
        gates[g] = {"live_rows": lv, "replay_rows": rp, "ratio": ratio, "live_symbols": len(ls),
                    "symbol_overlap": overlap, "testable": testable}
    out = {"day": day, "sha": sha, "work_dir": str(work) if work else None, "live_buys": len(live_buys),
           "replay_buys": len(rep_buys), "matched_90s": matched, "recall": recall, "precision": precision,
           "recall_ok": bool(recall is not None and recall >= 0.85), "gates": gates}
    out["pass"] = out["recall_ok"] and any(v["testable"] for v in gates.values())
    (OUT / "fidelity.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=("fidelity", "primary", "info"))
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--stop-at", default="08:45", help="ET; no replay is launched at or after this on a weekday")
    ap.add_argument("--sha", default="HEAD")
    a = ap.parse_args()
    sha = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", a.sha], text=True).strip()
    (OUT / "logs").mkdir(parents=True, exist_ok=True)
    pinned = OUT / "sha.txt"
    if pinned.exists() and pinned.read_text().strip() != sha:
        print(f"refusing: this study is pinned to {pinned.read_text().strip()}, not {sha}", file=sys.stderr)
        return 2
    pinned.write_text(sha + "\n")
    if a.phase == "fidelity":
        fidelity(sha)
        return 0
    if not fidelity_ok():
        print("refusing: fidelity.json missing or not passed (prereg amendments 7 / 13)", file=sys.stderr)
        return 2
    days = eligible_days()
    wr_days = [d for d in days if d >= WR_FIRST]
    if a.phase == "primary":
        jobs = [(d, v, o) for d in days for v, o in PRIMARY_PASS1.items()]
        jobs += [(d, v, o) for d in wr_days for v, o in WR_PASS1.items()]
        batch(jobs, sha, a.jobs, a.stop_at)
        jobs = []
        for gate, ov, gdays in (("S5", {}, days), ("WR", {}, wr_days)):
            for d in gdays:
                share = rand_share(d, gate)
                if share is None:
                    continue
                for k in (1, 2, 3):   # amendment 5: three salted seeds per gate
                    jobs.append((d, f"{gate}_RAND_{k}",
                                 {"ai_watch_wr_rand_refuse_share": share, "ai_watch_wr_rand_salt": f"{gate}-{k}"}))
        if days:   # amendment 8: REF twice on the first, middle and last session
            for d in sorted({days[0], days[len(days) // 2], days[-1]}):
                jobs.append((d, "REF_DUP", {}))
        batch(jobs, sha, a.jobs, a.stop_at)
    else:
        jobs = [(d, v, o) for d in days for v, o in INFO_ALL.items()]
        jobs += [(d, v, o) for d in wr_days for v, o in INFO_WR.items()]
        batch(jobs, sha, a.jobs, a.stop_at)
    return 0


if __name__ == "__main__":
    sys.exit(main())
