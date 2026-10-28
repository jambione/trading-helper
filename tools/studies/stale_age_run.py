#!/usr/bin/env python3
"""stale_age_run.py — runner for docs/studies/stale_age_replay_prereg.json (amended, amended_2, ready).

Reuses tools/studies/late_gates_run.py (run_one / batch / trade counts) with this study's output folder, pins and
flags. Every run: --arm-age-from-prints and ai_watch_arm_age_mode=age, so the arm judges tape age from the recorded
prints (amended_2 #1) at the two arm sites only (amended_2 #2). Outputs ~/replay_runs/stale_age/DAY-VAR.json.

  --phase fidelity  2026-10-27 (gap rule on) and 2026-10-13 (pre-gap era, gap rule off in that run only): the REF
                    replay's 15-30 s arm checks refused / accepted (arm_age_counts) against the live ledger's
                    (tape_age_sec in (15, 30], tape_src stale_tape / stream), each 50-200%, and buy recall >= 0.85.
                    Writes fidelity.json; the other phases refuse until it passes.
  --phase primary   REF, AGE30, MDE1 on every session; REF_DUP on the first, middle and last.
  --phase controls  RREL_1..3: release share calibrated by count of released checks against AGE30's
                    (amended #5): start at s = released(AGE30) / stale_base_over15(REF), bisect (<= 4 passes) until
                    the seed-averaged released count is within +-10%.
  --phase info      AGE60.
USAGE (mini, after the close, not while another replay batch runs):
  .venv/bin/python tools/studies/stale_age_run.py <phase> [--jobs 3] [--stop-at 08:45] [--sha HEAD]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import late_gates_run as L  # noqa: E402

OUT = Path(os.path.expanduser("~/replay_runs/stale_age"))
FIRST = "2026-09-28"                       # sessions with a wire recording
FIDELITY = {"2026-10-27": True, "2026-10-13": False}     # day -> gap rule on in the fidelity run
FLAGS = ["--start", "09:00", "--end", "15:50", "--step", "2.0", "--engine", "recorded", "--exits", "live",
         "--arm-age-from-prints"]
PIN = {
    **L.PIN,
    "ai_watch_wr_trend_min_rise": 15,
    "ai_watch_wr_slow_5m_veto": True,
    "ai_watch_wr_slow_5m_min_pitch": 0.0,
    "ai_watch_slope_now_gate": False,              # amended_slope_off (operator 10/28): the live desk has it off
    "ai_watch_slope_now_missing_refuses": False,
    "ai_watch_tape_gap_mult": 1.5,
    "ai_watch_tape_gap_max_sec": 60.0,
    "ai_watch_decision_max_age_sec": 15.0,
    "ai_watch_quote_max_age_sec": 15.0,
    "ai_watch_arm_age_mode": "age",
    "ai_watch_arm_max_age_sec": 0.0,
    "ai_watch_stale_rand_release_share": 0.0,
    "ai_watch_stale_rand_release_salt": "",
}
PRIMARY = {"REF": {}, "AGE30": {"ai_watch_arm_max_age_sec": 30.0},
           "MDE1": {"ai_watch_stale_rand_release_share": 0.25, "ai_watch_stale_rand_release_salt": "SA-MDE"}}
INFO = {"AGE60": {"ai_watch_arm_max_age_sec": 60.0}}
SEEDS = (1, 2, 3)


def use_study_globals() -> None:
    L.OUT, L.PIN, L.FLAGS, L.FIRST_DAY = OUT, PIN, FLAGS, FIRST


def counts(day: str, var: str) -> dict:
    p = L.out_path(day, var)
    return (json.loads(p.read_text()).get("arm_age_counts") or {}) if p.exists() else {}


def live_band_counts(day: str) -> dict:
    out = {"refused": 0, "accepted": 0}
    for line in open(L.SNAP / day / "decision_ledger.jsonl"):
        r = json.loads(line)
        if r.get("stage") != "arm" or r.get("tape_age_sec") is None:
            continue
        a = float(r["tape_age_sec"])
        if 15.0 < a <= 30.0:
            src = str(r.get("tape_src") or "")
            if src == "stale_tape":
                out["refused"] += 1
            elif src == "stream":
                out["accepted"] += 1
    return out


def fidelity(sha: str) -> None:
    res = {"sha": sha, "days": {}}
    for day, gap_on in FIDELITY.items():
        var = "FIDELITY" if gap_on else "FIDELITY_NOGAP"
        ov = {} if gap_on else {"ai_watch_tape_gap_mult": 0.0}
        print(L.run_one(day, var, ov, sha), flush=True)
        rep = counts(day, var)
        live = live_band_counts(day)
        r_ref = rep.get("age15_30_refused", 0) / live["refused"] if live["refused"] else None
        r_acc = rep.get("age15_30_accepted", 0) / live["accepted"] if live["accepted"] else None
        out = json.loads(L.out_path(day, var).read_text())
        rep_buys = [(float(t["entry_ts"]), str(t["symbol"]).upper())
                    for t in (out.get("closed") or []) + (out.get("open_at_end") or [])]
        live_buys = []
        for line in open(L.SNAP / day / "fills.jsonl"):
            x = json.loads(line)
            if x.get("event") == "fill" and str(x.get("side")).lower() == "buy":
                live_buys.append((float(x["ts"]), str(x["symbol"]).upper()))
        recall = L.buy_match(live_buys, rep_buys) / len(live_buys) if live_buys else None
        ok_band = all(v is not None and 0.5 <= v <= 2.0 for v in ((r_ref, r_acc) if gap_on else (r_ref,)))
        res["days"][day] = {"gap_rule": gap_on, "live": live, "replay": {k: rep.get(k, 0) for k in
                            ("age15_30_refused", "age15_30_accepted", "checks")}, "ratio_refused": r_ref,
                            "ratio_accepted": r_acc, "buy_recall": recall,
                            "pass": bool(ok_band and recall is not None and recall >= 0.85)}
    res["pass"] = all(d["pass"] for d in res["days"].values())
    (OUT / "fidelity.json").write_text(json.dumps(res, indent=1))
    print(json.dumps(res, indent=1))


def released(day: str, var: str, key: str) -> int:
    return int(counts(day, var).get(key, 0) or 0)


def calibrate(day: str, sha: str, stop_at: str) -> str:
    target = released(day, "AGE30", "released_ceiling")
    pool = released(day, "REF", "stale_base_over15")
    if target <= 0 or pool <= 0:
        return f"{day} RREL: nothing to match (AGE30 released {target}, pool {pool})"
    lo, hi, s = 0.0, 1.0, min(1.0, target / pool)
    for k in range(4):
        names = [f"RREL_{j}" for j in SEEDS]
        if not L.run_seed_pass(day, [(n, {"ai_watch_stale_rand_release_share": round(s, 6),
                                          "ai_watch_stale_rand_release_salt": f"SA-{j}"})
                                     for j, n in zip(SEEDS, names)], sha, stop_at):
            return f"{day} RREL: stopped at {stop_at}"
        got = sum(released(day, n, "released_rand") for n in names) / len(names)
        if abs(got - target) <= 0.10 * target:
            (OUT / f"{day}-RREL_share.json").write_text(json.dumps({"share": s, "target": target, "got": got,
                                                                    "passes": k + 1}))
            return f"{day} RREL: matched s={s:.4f} got {got:.1f} vs {target}"
        if got < target:
            lo = s
        else:
            hi = s
        s = (lo + hi) / 2
    (OUT / f"{day}-RREL_share.json").write_text(json.dumps({"share": s, "target": target, "unmatched": True}))
    return f"{day} RREL: UNMATCHED after 4 passes"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=("fidelity", "primary", "controls", "info"))
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--stop-at", default="08:45")
    ap.add_argument("--sha", default="HEAD")
    a = ap.parse_args()
    use_study_globals()
    sha = subprocess.check_output(["git", "-C", str(L.ROOT), "rev-parse", a.sha], text=True).strip()
    (OUT / "logs").mkdir(parents=True, exist_ok=True)
    pinned = OUT / "sha.txt"
    if pinned.exists() and pinned.read_text().strip() != sha:
        print(f"refusing: this study is pinned to {pinned.read_text().strip()}, not {sha}", file=sys.stderr)
        return 2
    pinned.write_text(sha + "\n")
    if a.phase == "fidelity":
        fidelity(sha)
        return 0
    fp = OUT / "fidelity.json"
    if not (fp.exists() and json.loads(fp.read_text()).get("pass")):
        print("refusing: fidelity.json missing or not passed", file=sys.stderr)
        return 2
    days = L.eligible_days()
    if a.phase == "primary":
        L.batch([(d, v, o) for d in days for v, o in PRIMARY.items()], sha, a.jobs, a.stop_at)
        dup = sorted({days[0], days[len(days) // 2], days[-1]}) if days else []
        L.batch([(d, "REF_DUP", {}) for d in dup], sha, a.jobs, a.stop_at)
    elif a.phase == "controls":
        for d in days:
            print(calibrate(d, sha, a.stop_at), flush=True)
    else:
        L.batch([(d, v, o) for d in days for v, o in INFO.items()], sha, a.jobs, a.stop_at)
    return 0


if __name__ == "__main__":
    sys.exit(main())
