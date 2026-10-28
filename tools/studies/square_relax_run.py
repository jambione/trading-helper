#!/usr/bin/env python3
"""square_relax_run.py — runner for docs/studies/square_relax_prereg.json (amended, amended_2, margin, ready,
screen_first, stage2_monitoring).

Same harness as stale_age_run (late_gates_run machinery, --arm-age-from-prints, the 10/28 live desk pinned) with this
study's outputs in ~/replay_runs/square_relax/DAY-VAR.json. Stage 1 (the screen) runs on the recorded sessions.

  --phase fidelity  2026-10-27 REF replay vs live decision_ledger: share of arm rows by code (codes >= 1% of live rows
                    within 0.5-2x; rarer codes: replay share <= 1%), buy recall >= 0.85, and the signed median of
                    replay minus live arm time over matched buys within +-10 s. Writes fidelity.json.
  --phase primary   REF, TIGHT20, PRE45, MDE1 on every session; REF_DUP on the first, middle and last.
  --phase controls  RAND_<v>_1..3 for v in TIGHT20, PRE45: release share calibrated so the seed-averaged released
                    checks (sq_counts.released_rand) are within +-10% of v's released checks (sq_counts.released_variant),
                    bisection, <= 4 passes per seed set.
  --phase info      TIGHT25, PRE40.
USAGE (mini, after the close, not while another replay batch runs):
  .venv/bin/python tools/studies/square_relax_run.py <phase> [--jobs 3] [--stop-at 08:45] [--sha HEAD] [--last 2026-10-27]
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import late_gates_run as L  # noqa: E402
import stale_age_run as SA  # noqa: E402

OUT = Path(os.path.expanduser("~/replay_runs/square_relax"))
PIN = dict(SA.PIN)                       # the stale_age REF desk: gates, gap rule, arm age from prints
PRIMARY = {"REF": {}, "TIGHT20": {"ai_watch_presquare_confluence_max": 20.0},
           "PRE45": {"ai_watch_presquare_pre_thr": 45.0},
           "MDE1": {"ai_watch_presquare_rand_release_share": 0.25, "ai_watch_presquare_rand_release_salt": "SQ-MDE"}}
CONTROLLED = ("TIGHT20", "PRE45")
INFO = {"TIGHT25": {"ai_watch_presquare_confluence_max": 25.0}, "PRE40": {"ai_watch_presquare_pre_thr": 40.0}}
SEEDS = (1, 2, 3)
FIDELITY_DAY = "2026-10-27"


def use_study_globals() -> None:
    L.OUT, L.PIN, L.FLAGS, L.FIRST_DAY = OUT, PIN, SA.FLAGS, SA.FIRST


def sq(day: str, var: str) -> dict:
    p = L.out_path(day, var)
    return (json.loads(p.read_text()).get("sq_counts") or {}) if p.exists() else {}


def code_shares(rows) -> Counter:
    c = Counter(rows)
    n = sum(c.values()) or 1
    return Counter({k: v / n for k, v in c.items()})


def fidelity(sha: str) -> None:
    day = FIDELITY_DAY
    print(L.run_one(day, "FIDELITY", {}, sha, keep_work=True), flush=True)
    log = (OUT / "logs" / f"{day}-FIDELITY.log").read_text()
    import re
    m = re.findall(r"\[replay\] work dir: (\S+)", log)
    work = Path(m[-1]) if m else None

    def arm_codes(path):
        out = []
        if path and path.exists():
            for line in open(path):
                r = json.loads(line)
                if r.get("stage") == "arm":
                    out.append("ARM_OK" if r.get("arm_ok") else str(r.get("arm_why")))
        return out
    live = code_shares(arm_codes(L.SNAP / day / "decision_ledger.jsonl"))
    rep = code_shares(arm_codes(work / "ai_reports" / "decision_ledger" / f"{day}.jsonl" if work else None))
    codes = {}
    ok_codes = True
    for k in set(live) | set(rep):
        lv, rv = live.get(k, 0.0), rep.get(k, 0.0)
        ok = (0.5 <= rv / lv <= 2.0) if lv >= 0.01 else (rv <= 0.01)
        codes[k] = {"live": round(lv, 4), "replay": round(rv, 4), "ok": ok}
        ok_codes &= ok
    res = json.loads(L.out_path(day, "FIDELITY").read_text())
    rep_buys = sorted((float(t["entry_ts"]), str(t["symbol"]).upper())
                      for t in (res.get("closed") or []) + (res.get("open_at_end") or []))
    live_buys = sorted((float(x["ts"]), str(x["symbol"]).upper())
                       for x in map(json.loads, open(L.SNAP / day / "fills.jsonl"))
                       if x.get("event") == "fill" and str(x.get("side")).lower() == "buy")
    matched = L.buy_match(live_buys, rep_buys)
    offs = []
    used = set()
    for lt, ls in live_buys:
        best = min(((abs(rt - lt), j, rt - lt) for j, (rt, rs) in enumerate(rep_buys)
                    if rs == ls and j not in used and abs(rt - lt) <= 90), default=None)
        if best:
            used.add(best[1])
            offs.append(best[2])
    recall = matched / len(live_buys) if live_buys else None
    med_off = statistics.median(offs) if offs else None
    out = {"day": day, "sha": sha, "codes": codes, "codes_ok": ok_codes, "buy_recall": recall,
           "median_offset_s": med_off,
           "pass": bool(ok_codes and recall is not None and recall >= 0.85 and med_off is not None and abs(med_off) <= 10)}
    (OUT / "fidelity.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))


def calibrate(day: str, v: str, sha: str, stop_at: str) -> str:
    target = int(sq(day, v).get("released_variant", 0) or 0)
    if target <= 0:
        return f"{day} RAND_{v}: {v} released nothing; no control needed"
    lo, hi, s = 0.0, 1.0, 0.5
    names = [f"RAND_{v}_{k}" for k in SEEDS]
    for n_pass in range(4):
        for k, name in zip(SEEDS, names):
            p = L.out_path(day, name)
            if p.exists():
                p.unlink()
            if L.past_stop(stop_at):
                return f"{day} RAND_{v}: stopped at {stop_at}"
            print(L.run_one(day, name, {"ai_watch_presquare_rand_release_share": round(s, 6),
                                        "ai_watch_presquare_rand_release_salt": f"SQ-{v}-{k}"}, sha), flush=True)
        got = statistics.fmean(int(sq(day, n).get("released_rand", 0) or 0) for n in names)
        if abs(got - target) <= 0.10 * target:
            (OUT / f"{day}-RAND_{v}_share.json").write_text(json.dumps({"share": s, "target": target, "got": got,
                                                                       "passes": n_pass + 1}))
            return f"{day} RAND_{v}: matched s={s:.4f} got {got:.1f} vs {target}"
        if got < target:
            lo = s
        else:
            hi = s
        s = (lo + hi) / 2
    (OUT / f"{day}-RAND_{v}_share.json").write_text(json.dumps({"share": s, "target": target, "unmatched": True}))
    return f"{day} RAND_{v}: UNMATCHED after 4 passes"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=("fidelity", "primary", "controls", "info"))
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--stop-at", default="08:45")
    ap.add_argument("--sha", default="HEAD")
    ap.add_argument("--last", default="2026-10-27", help="last session for stage 1 (the recorded screen)")
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
    days = [d for d in L.eligible_days() if d <= a.last]
    if a.phase == "primary":
        L.batch([(d, v, o) for d in days for v, o in PRIMARY.items()], sha, a.jobs, a.stop_at)
        dup = sorted({days[0], days[len(days) // 2], days[-1]}) if days else []
        L.batch([(d, "REF_DUP", {}) for d in dup], sha, a.jobs, a.stop_at)
    elif a.phase == "controls":
        for d in days:
            for v in CONTROLLED:
                print(calibrate(d, v, sha, a.stop_at), flush=True)
    else:
        L.batch([(d, v, o) for d in days for v, o in INFO.items()], sha, a.jobs, a.stop_at)
    return 0


if __name__ == "__main__":
    sys.exit(main())
