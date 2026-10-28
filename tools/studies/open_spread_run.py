#!/usr/bin/env python3
"""open_spread_run.py — runner for docs/studies/open_spread_replay_prereg.json (amended, amended_2, ready).

stale_age's harness and REF desk (late_gates_run machinery, --arm-age-from-prints, slope-now off) with this study's
outputs in ~/replay_runs/open_spread/DAY-VAR.json. Window = 09:30:00-09:45:59 ET (16 minutes).

  --phase fidelity  needs ~/replay_runs/stale_age/fidelity.json pass. 2026-10-27 REF (work dir kept): window arm
                    refusals spread_wide / spread_unknown and window buys vs the live ledger, each 50-200% (0 live =
                    0 replay); the share of window spread reads that were recorded live is printed. Then OPEN16 on
                    10/27 and 20 seeded actual-spread reads from its cache re-fetched directly (quotes <= ts) within
                    0.01 points. Writes fidelity.json.
  --phase count     OPEN16 and one RAND calibration on the first, middle and last stage-1 session; projects added
                    trades over the 22 sessions; < 30 for OPEN16 or RAND = UNDERPOWERED (count.json, no means).
  --phase primary   REF, OPEN16, OPEN16_REL, OPEN16_BLK, MDE1, SKIP16 on every session; REF_DUP first/middle/last.
  --phase controls  RAND_1..3: release share and block share bisected independently in the same passes (<= 4),
                    each matched within +-10% of OPEN16's released / blocked counts, seeds in parallel.
USAGE (mini, after the close, one batch at a time):
  .venv/bin/python tools/studies/open_spread_run.py <phase> [--jobs 3] [--stop-at 08:45] [--sha HEAD]
"""
from __future__ import annotations

import argparse
import datetime as _dt
import gzip
import json
import os
import random
import re
import shutil
import statistics
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import late_gates_run as L  # noqa: E402
import stale_age_run as SA  # noqa: E402

ET = ZoneInfo("America/New_York")
OUT = Path(os.path.expanduser("~/replay_runs/open_spread"))
LAST = "2026-10-27"
WIN = 16
PIN = {**SA.PIN, "ai_watch_open_spread_realtime_min": 0.0, "ai_watch_open_spread_mode": "both",
       "ai_watch_open_spread_window_min": 0.0, "ai_watch_open_spread_rand_release_share": 0.0,
       "ai_watch_open_spread_rand_block_share": 0.0, "ai_watch_open_spread_rand_salt": ""}
PRIMARY = {
    "REF": {},
    "OPEN16": {"ai_watch_open_spread_realtime_min": WIN},
    "OPEN16_REL": {"ai_watch_open_spread_realtime_min": WIN, "ai_watch_open_spread_mode": "release"},
    "OPEN16_BLK": {"ai_watch_open_spread_realtime_min": WIN, "ai_watch_open_spread_mode": "block"},
    "MDE1": {"ai_watch_open_spread_window_min": WIN, "ai_watch_open_spread_rand_release_share": 0.25,
             "ai_watch_open_spread_rand_salt": "OS-MDE"},
    "SKIP16": {"ai_watch_open_spread_realtime_min": WIN, "ai_watch_open_spread_mode": "skip"},
}
SEEDS = (1, 2, 3)
FIDELITY_DAY = "2026-10-27"


def use_study_globals() -> None:
    L.OUT, L.PIN, L.FLAGS, L.FIRST_DAY = OUT, PIN, SA.FLAGS, SA.FIRST


def window(day: str) -> tuple[float, float]:
    t0 = _dt.datetime.fromisoformat(f"{day} 09:30").replace(tzinfo=ET).timestamp()
    return t0, t0 + WIN * 60


def os_counts(day: str, var: str) -> dict:
    p = L.out_path(day, var)
    return (json.loads(p.read_text()).get("os_counts") or {}) if p.exists() else {}


def ledger_window(path: Path | None, day: str) -> dict:
    t0, t1 = window(day)
    out = {"spread_wide": 0, "spread_unknown": 0}
    if path and path.exists():
        for line in open(path):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("stage") == "arm" and not r.get("arm_ok") and t0 <= float(r.get("ts") or 0) < t1:
                why = r.get("arm_why")
                if why in out:
                    out[why] += 1
    return out


def window_buys(trades: list[tuple[float, str]], day: str) -> int:
    t0, t1 = window(day)
    return sum(t0 <= ts < t1 for ts, _ in trades)


def ratio_ok(rep: int, live: int) -> bool:
    return rep == 0 if live == 0 else 0.5 <= rep / live <= 2.0


def recorded_share(day: str) -> float | None:
    """Share of window sip_spread inputs the live desk recorded (what replay REF can serve without a fetch)."""
    t0, t1 = window(day)
    p = L.SNAP / day / "recorder_inputs.jsonl.gz"
    if not p.exists():
        return None
    n = 0
    try:
        with gzip.open(p, "rt") as fh:
            for line in fh:
                if '"sip_spread"' not in line:
                    continue
                r = json.loads(line)
                n += t0 <= float(r.get("ts") or 0) < t1
    except (EOFError, ValueError):
        pass
    return n


def fidelity(sha: str) -> None:
    sa = Path(os.path.expanduser("~/replay_runs/stale_age/fidelity.json"))
    if not (sa.exists() and json.loads(sa.read_text()).get("pass")):
        print("refusing: stale_age_replay fidelity has not passed (this study's REF is that desk)", file=sys.stderr)
        sys.exit(2)
    day = FIDELITY_DAY
    print(L.run_one(day, "FIDELITY", {}, sha, keep_work=True), flush=True)
    log = (OUT / "logs" / f"{day}-FIDELITY.log").read_text()
    m = re.findall(r"\[replay\] work dir: (\S+)", log)
    work = Path(m[-1]) if m else None
    rep_ref = ledger_window(work / "ai_reports" / "decision_ledger" / f"{day}.jsonl" if work else None, day)
    live_ref = ledger_window(L.SNAP / day / "decision_ledger.jsonl", day)
    res = json.loads(L.out_path(day, "FIDELITY").read_text())
    rep_buys = [(float(t["entry_ts"]), t["symbol"]) for t in (res.get("closed") or []) + (res.get("open_at_end") or [])]
    live_buys = [(float(x["ts"]), x["symbol"]) for x in map(json.loads, open(L.SNAP / day / "fills.jsonl"))
                 if x.get("event") == "fill" and str(x.get("side")).lower() == "buy"]
    checks = {k: ratio_ok(rep_ref[k], live_ref[k]) for k in rep_ref}
    checks["window_buys"] = ratio_ok(window_buys(rep_buys, day), window_buys(live_buys, day))
    if work:
        shutil.rmtree(work, ignore_errors=True)
    # OPEN16 equality: its cached actual-spread reads re-fetched directly
    print(L.run_one(day, "FIDELITY_OPEN16", PRIMARY["OPEN16"], sha), flush=True)
    sys.path.insert(0, str(L.ROOT))
    import ai_entry_watch as ew
    cache_path = Path(os.getenv("REPLAY_CACHE_DIR") or Path.home() / "replay_cache") / "sip_spread_rt.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    keys = sorted(k for k in cache if k.startswith(f"{day}|"))
    sample = random.Random("OS-FID").sample(keys, min(20, len(keys)))
    diffs = []
    for k in sample:
        _, sym, sec = k.split("|")
        ew._OS_RT_CACHE.clear()
        direct = ew.sip_spread_at(sym, float(sec))
        a, b = cache[k], direct
        diffs.append(a is None and b is None or (a is not None and b is not None and abs(a - b) <= 0.01))
    checks["open16_equal"] = len(sample) >= 20 and all(diffs)
    out = {"day": day, "sha": sha, "live_window": live_ref, "replay_window": rep_ref,
           "window_buys": {"live": window_buys(live_buys, day), "replay": window_buys(rep_buys, day)},
           "recorded_window_spread_inputs": recorded_share(day), "open16_sample": len(sample),
           "checks": checks, "pass": all(checks.values())}
    (OUT / "fidelity.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))


def added_count(day: str, var: str) -> int | None:
    """Trades entered in the window with no REF match (same symbol within 600 s)."""
    a, b = L.out_path(day, "REF"), L.out_path(day, var)
    if not (a.exists() and b.exists()):
        return None
    t0, t1 = window(day)

    def tr(p):
        d = json.loads(p.read_text())
        return [(float(t["entry_ts"]), t["symbol"]) for t in (d.get("closed") or []) + (d.get("open_at_end") or [])]
    ref, g = tr(a), tr(b)
    return sum(1 for ts, s in g if t0 <= ts < t1 and not any(rs == s and abs(rts - ts) <= 600 for rts, rs in ref))


def calibrate(day: str, sha: str, stop_at: str, names=None) -> str:
    """Two shares bisected independently in the same passes; each within +-10% of OPEN16's count or UNMATCHED."""
    v = os_counts(day, "OPEN16")
    t_rel, t_blk = int(v.get("released_variant", 0)), int(v.get("blocked_variant", 0))
    names = names or [f"RAND_{k}" for k in SEEDS]
    if t_rel == 0 and t_blk == 0:
        (OUT / f"{day}-RAND_share.json").write_text(json.dumps({"release": 0.0, "block": 0.0, "nothing": True}))
        return f"{day} RAND: OPEN16 changed nothing in the window"
    lo_r, hi_r, s_r = 0.0, 1.0, 0.5 if t_rel else 0.0
    lo_b, hi_b, s_b = 0.0, 1.0, 0.1 if t_blk else 0.0
    ok_r = ok_b = False
    for n_pass in range(4):
        if not L.run_seed_pass(day, [(n, {"ai_watch_open_spread_window_min": WIN,
                                          "ai_watch_open_spread_rand_release_share": round(s_r, 6),
                                          "ai_watch_open_spread_rand_block_share": round(s_b, 6),
                                          "ai_watch_open_spread_rand_salt": f"OS-{k}"})
                                     for k, n in zip(SEEDS, names)], sha, stop_at):
            return f"{day} RAND: stopped at {stop_at}"
        g_rel = statistics.fmean(int(os_counts(day, n).get("released_rand", 0)) for n in names)
        g_blk = statistics.fmean(int(os_counts(day, n).get("blocked_rand", 0)) for n in names)
        ok_r = abs(g_rel - t_rel) <= 0.10 * max(t_rel, 1) if t_rel else g_rel == 0
        ok_b = abs(g_blk - t_blk) <= 0.10 * max(t_blk, 1) if t_blk else g_blk == 0
        if ok_r and ok_b:
            break
        if not ok_r and t_rel:
            lo_r, hi_r = (s_r, hi_r) if g_rel < t_rel else (lo_r, s_r)
            s_r = (lo_r + hi_r) / 2
        if not ok_b and t_blk:
            lo_b, hi_b = (s_b, hi_b) if g_blk < t_blk else (lo_b, s_b)
            s_b = (lo_b + hi_b) / 2
    rec = {"release": s_r, "block": s_b, "target": [t_rel, t_blk], "got": [g_rel, g_blk],
           "unmatched": not (ok_r and ok_b)}
    (OUT / f"{day}-RAND_share.json").write_text(json.dumps(rec))
    return f"{day} RAND: {'UNMATCHED' if rec['unmatched'] else 'matched'} {rec}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=("fidelity", "count", "primary", "controls"))
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--stop-at", default="08:45")
    ap.add_argument("--sha", default="HEAD")
    a = ap.parse_args()
    use_study_globals()
    live_cfg = json.loads((L.ROOT / "config" / "bot_config.json").read_text())
    if float(live_cfg.get("ai_watch_open_spread_realtime_min", 0) or 0) != 0:
        print("refusing: ai_watch_open_spread_realtime_min is REPLAY-ONLY and is set in the live config", file=sys.stderr)
        return 2
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
        print("refusing: fidelity.json missing or not passed (NOT TESTABLE on this harness)", file=sys.stderr)
        return 2
    days = [d for d in L.eligible_days() if d <= LAST]
    if a.phase == "count":
        probe = sorted({days[0], days[len(days) // 2], days[-1]})
        L.batch([(d, v, PRIMARY[v]) for d in probe for v in ("REF", "OPEN16")], sha, a.jobs, a.stop_at)
        for d in probe:
            print(calibrate(d, sha, a.stop_at), flush=True)
        per = {d: {"OPEN16": added_count(d, "OPEN16"),
                   "RAND": statistics.fmean(added_count(d, f"RAND_{k}") or 0 for k in SEEDS)} for d in probe}
        proj = {v: statistics.fmean((per[d][v] or 0) for d in probe) * len(days) for v in ("OPEN16", "RAND")}
        res = {"probe": per, "projected_added": proj, "sessions": len(days),
               "underpowered": any(x < 30 for x in proj.values())}
        (OUT / "count.json").write_text(json.dumps(res, indent=1))
        print(json.dumps(res, indent=1))
        return 0
    cnt = OUT / "count.json"
    if not (cnt.exists() and not json.loads(cnt.read_text()).get("underpowered")):
        print("refusing: count.json missing or UNDERPOWERED (the study stops; no means are read)", file=sys.stderr)
        return 2
    if a.phase == "primary":
        L.batch([(d, v, o) for d in days for v, o in PRIMARY.items()], sha, a.jobs, a.stop_at)
        dup = sorted({days[0], days[len(days) // 2], days[-1]})
        L.batch([(d, "REF_DUP", {}) for d in dup], sha, a.jobs, a.stop_at)
    else:
        for d in days:
            print(calibrate(d, sha, a.stop_at), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
