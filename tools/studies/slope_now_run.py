#!/usr/bin/env python3
"""slope_now_run.py — runner for docs/studies/slope_now_replay_prereg.json (amended, amended_2, ready).

late_gates harness (tools/replay_session.py, recorded engine, --live-staleness, paint on, one pinned --sha) with this
study's outputs in ~/replay_runs/slope_now/DAY-VAR.json. Every REF run keeps its work dir just long enough to copy the
arm rows (with the pitch the gate would read) to DAY-REF-arms.jsonl.gz; that file is the exploration data set
(winners vs losers by pitch, operator 10/28) and the work dir is then removed.

  --phase fidelity  2026-10-27 REF vs the live entry_watch_state snapshots (amended 1, amended_2 (2)(3)): for every
                    replay arm check 09:30-15:50, the live row for the same symbol with |last_poll_ts - ts| <= 5 s.
                    (a) presence share of each pitch field within +-10 points; (b) sign agreement >= 80% on fast > 0,
                    slow > 0, slow >= slow_prev; (c) median |replay - live| <= 1.0 pt/min for fast and slow;
                    (d) replay gate refusals (rule applied to the replay row, staleness included) 50-200% of the rule
                    applied to the matched live rows (no staleness). Writes fidelity.json.
  --phase ref       REF only (with its arm rows saved): the exploration data set.
  --phase primary   REF, SLOPE, SLOPE_OPEN, EXISTS, MDE1 on every session; REF_DUP on the first, middle and last.
  --phase controls  CRAND_1..3 calibrated by trade count to SLOPE (+-15%, bisection, <= 4 passes) and RAND_1..3
                    (wr_rand episodes, share = 1 - n(SLOPE)/n(REF)).
USAGE (mini, after the close, one batch at a time):
  .venv/bin/python tools/studies/slope_now_run.py <phase> [--jobs 3] [--stop-at 08:45] [--sha HEAD]
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import late_gates_run as L  # noqa: E402

OUT = Path(os.path.expanduser("~/replay_runs/slope_now"))
FIRST = "2026-09-28"                       # sessions with a wire recording (amended 5)
FIDELITY_DAY = "2026-10-27"
PIN = {
    **L.PIN,
    "ai_watch_wr_trend_min_rise": 15,
    "ai_watch_wr_slow_5m_veto": True,
    "ai_watch_wr_slow_5m_min_pitch": 0.0,
    "ai_watch_slope_now_gate": False,
    "ai_watch_slope_now_missing_refuses": True,
    "ai_watch_slope_exists_only": False,
    "ai_watch_slope_rand_share": 0.0,
    "ai_watch_slope_rand_salt": "",
    # amended 6: the knobs that decide whether a pitch exists, at their 10/27 values
    "ai_watch_poll_sec": 10.0,
    "ai_watch_engine_exh_max_age_sec": 8.0,
    "ai_watch_exhaustion_engine_only": True,
    "ai_watch_stream_bars_live": True,
}
PRIMARY = {
    "REF": {},
    "SLOPE": {"ai_watch_slope_now_gate": True},
    "SLOPE_OPEN": {"ai_watch_slope_now_gate": True, "ai_watch_slope_now_missing_refuses": False},
    "EXISTS": {"ai_watch_slope_exists_only": True},
    "MDE1": {"ai_watch_wr_rand_refuse_share": 0.25, "ai_watch_wr_rand_salt": "SN-MDE"},
}
SEEDS = (1, 2, 3)
PITCH = ("pctr_fast_pitch", "pctr_slow_pitch", "pctr_slow_pitch_prev")
ARM_KEEP = ("ts", "symbol", "arm_ok", "arm_why", "ask", "pctr", "pctr_slow", "pctr_gap", "pctr_src",
            "pctr_fast_pitch", "pctr_slow_pitch", "pctr_fast_pitch_prev", "pctr_slow_pitch_prev", "pctr_pitch_ts")


def use_study_globals() -> None:
    L.OUT, L.PIN, L.FLAGS, L.FIRST_DAY = OUT, PIN, L.FLAGS, FIRST


def work_dir(day: str, var: str) -> Path | None:
    log = OUT / "logs" / f"{day}-{var}.log"
    m = re.findall(r"\[replay\] work dir: (\S+)", log.read_text()) if log.exists() else []
    return Path(m[-1]) if m else None


def arm_rows(path: Path | None) -> list[dict]:
    out = []
    if path and path.exists():
        for line in open(path):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("stage") == "arm":
                out.append(r)
    return out


def ref_with_arms(day: str, var: str, sha: str) -> str:
    """REF (or REF_DUP) with the work dir kept only to save its arm rows; the dir is removed afterwards."""
    msg = L.run_one(day, var, {}, sha, keep_work=True)
    wd = work_dir(day, var)
    if wd and wd.exists():
        rows = arm_rows(wd / "ai_reports" / "decision_ledger" / f"{day}.jsonl")
        if var == "REF":
            with gzip.open(OUT / f"{day}-REF-arms.jsonl.gz", "wt") as fh:
                for r in rows:
                    fh.write(json.dumps({k: r.get(k) for k in ARM_KEEP}) + "\n")
        shutil.rmtree(wd, ignore_errors=True)
    return msg


def gate_refuses(r: dict, *, stale: bool) -> bool:
    """The PRIMARY rule (missing refuses) on a ledger or snapshot row; stale=True applies the 12 s pitch age."""
    import ai_entry_watch as ew
    ind = {k: r.get(k) for k in PITCH}
    ind["pctr_pitch_ts"] = r.get("pctr_pitch_ts") if stale else float(r.get("ts") or 0)
    rec = {"indicator": ind}
    return ew.slope_now_refusal(rec, float(r.get("ts") or 0), {"ai_watch_slope_now_gate": True,
                                                                 "ai_watch_slope_now_missing_refuses": True}) is not None


def live_rows(day: str) -> dict[str, list[tuple[float, dict]]]:
    """{symbol: [(last_poll_ts, pitch fields)]} from the live entry_watch_state snapshots."""
    out: dict[str, dict[float, dict]] = {}
    with gzip.open(L.SNAP / day / "state_snapshots.jsonl.gz", "rt") as fh:
        for line in fh:
            if '"ai_reports/entry_watch_state.json"' not in line[:200]:
                continue
            r = json.loads(line)
            for sym, row in (r.get("data") or {}).items():
                if not isinstance(row, dict) or row.get("last_poll_ts") is None:
                    continue
                ind = row.get("indicator") or {}
                out.setdefault(sym.upper(), {})[float(row["last_poll_ts"])] = {k: ind.get(k) for k in PITCH}
    return {s: sorted(d.items()) for s, d in out.items()}


def match_live(rep: list[dict], live: dict, window: float = 5.0) -> list[tuple[dict, dict]]:
    import bisect
    pairs = []
    for r in rep:
        rows = live.get(str(r.get("symbol") or "").upper())
        if not rows:
            continue
        t = float(r.get("ts") or 0)
        ts = [x[0] for x in rows]
        i = bisect.bisect_left(ts, t)
        best = min((rows[j] for j in (i - 1, i) if 0 <= j < len(rows)), key=lambda x: abs(x[0] - t), default=None)
        if best and abs(best[0] - t) <= window:
            pairs.append((r, best[1]))
    return pairs


def fidelity_stats(pairs: list[tuple[dict, dict]]) -> dict:
    n = len(pairs)
    out: dict = {"matched_checks": n, "presence": {}, "sign_agree": {}, "median_abs": {}}
    ok = n > 0
    for k in PITCH:
        rp = sum(r.get(k) is not None for r, _ in pairs) / n if n else 0.0
        lp = sum(lv.get(k) is not None for _, lv in pairs) / n if n else 0.0
        out["presence"][k] = {"replay": round(rp, 3), "live": round(lp, 3), "ok": abs(rp - lp) <= 0.10}
        ok &= out["presence"][k]["ok"]
    conds = {"fast_gt0": lambda x: x["pctr_fast_pitch"] > 0, "slow_gt0": lambda x: x["pctr_slow_pitch"] > 0,
             "slow_ge_prev": lambda x: x["pctr_slow_pitch"] >= x["pctr_slow_pitch_prev"]}
    need = {"fast_gt0": ("pctr_fast_pitch",), "slow_gt0": ("pctr_slow_pitch",),
            "slow_ge_prev": ("pctr_slow_pitch", "pctr_slow_pitch_prev")}
    for c, f in conds.items():
        both = [(r, lv) for r, lv in pairs if all(r.get(k) is not None and lv.get(k) is not None for k in need[c])]
        agree = sum(f(r) == f(lv) for r, lv in both) / len(both) if both else None
        out["sign_agree"][c] = {"n": len(both), "share": agree, "ok": agree is not None and agree >= 0.80}
        ok &= out["sign_agree"][c]["ok"]
    for k in ("pctr_fast_pitch", "pctr_slow_pitch"):
        d = [abs(r[k] - lv[k]) for r, lv in pairs if r.get(k) is not None and lv.get(k) is not None]
        med = statistics.median(d) if d else None
        out["median_abs"][k] = {"value": med, "ok": med is not None and med <= 1.0}
        ok &= out["median_abs"][k]["ok"]
    rep_ref = sum(gate_refuses(r, stale=True) for r, _ in pairs)
    live_ref = sum(gate_refuses({**lv, "ts": r.get("ts")}, stale=False) for r, lv in pairs)
    ratio = rep_ref / live_ref if live_ref else None
    out["refusals"] = {"replay": rep_ref, "live": live_ref, "ratio": ratio,
                       "ok": ratio is not None and 0.5 <= ratio <= 2.0}
    ok &= out["refusals"]["ok"]
    out["pass"] = bool(ok)
    return out


def fidelity(sha: str) -> None:
    day = FIDELITY_DAY
    print(L.run_one(day, "FIDELITY", {}, sha, keep_work=True), flush=True)
    wd = work_dir(day, "FIDELITY")
    rep = arm_rows(wd / "ai_reports" / "decision_ledger" / f"{day}.jsonl" if wd else None)
    from datetime import datetime
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    t0 = datetime.fromisoformat(f"{day} 09:30").replace(tzinfo=et).timestamp()
    t1 = datetime.fromisoformat(f"{day} 15:50").replace(tzinfo=et).timestamp()
    rep = [r for r in rep if t0 <= float(r.get("ts") or 0) <= t1]
    out = {"day": day, "sha": sha, "replay_checks": len(rep), **fidelity_stats(match_live(rep, live_rows(day)))}
    (OUT / "fidelity.json").write_text(json.dumps(out, indent=1))
    if wd:
        shutil.rmtree(wd, ignore_errors=True)
    print(json.dumps(out, indent=1))


def calibrate_crand(day: str, sha: str, stop_at: str) -> str:
    """amended_2 (1): bisect the CRAND share until the seed-averaged trade count is within +-15% of SLOPE's."""
    target = L.n_trades(day, "SLOPE")
    ref = L.n_trades(day, "REF")
    if target is None or ref is None:
        return f"{day} CRAND: needs REF and SLOPE"
    names = [f"CRAND_{k}" for k in SEEDS]
    lo, hi = 0.0, 1.0
    s = max(0.0, min(1.0, 1 - target / ref)) if ref else 0.0
    for n_pass in range(4):
        if not L.run_seed_pass(day, [(n, {"ai_watch_slope_rand_share": round(s, 6),
                                          "ai_watch_slope_rand_salt": f"SC-{k}"})
                                     for k, n in zip(SEEDS, names)], sha, stop_at):
            return f"{day} CRAND: stopped at {stop_at}"
        got = statistics.fmean(L.n_trades(day, n) or 0 for n in names)
        if abs(got - target) <= 0.15 * max(target, 1):
            (OUT / f"{day}-CRAND_share.json").write_text(json.dumps({"share": s, "target": target, "got": got,
                                                                    "passes": n_pass + 1}))
            return f"{day} CRAND: matched s={s:.4f} got {got:.1f} vs {target}"
        if got > target:           # too many trades: refuse more
            lo = s
        else:
            hi = s
        s = (lo + hi) / 2
    (OUT / f"{day}-CRAND_share.json").write_text(json.dumps({"share": s, "target": target, "unmatched": True}))
    return f"{day} CRAND: UNMATCHED after 4 passes"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=("fidelity", "ref", "primary", "controls"))
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--stop-at", default="08:45")
    ap.add_argument("--sha", default="HEAD")
    ap.add_argument("--explore-only", action="store_true",
                    help="phase ref only: run REF for the exploration tables even when fidelity failed (no gate is scored)")
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
    explore_only = a.explore_only and a.phase == "ref"
    if explore_only:
        # 10/28: fidelity failed on pitch PRESENCE (replay 39% vs live 70%); prices, trades and spreads are unaffected.
        # The REF runs then feed only the information tables (range, range-per-spread); pitch splits are flagged.
        print("explore-only: fidelity not passed; REF feeds the information tables only, never the gate test", flush=True)
    elif not (fp.exists() and json.loads(fp.read_text()).get("pass")):
        print("refusing: fidelity.json missing or not passed (SLOPE NOT TESTABLE IN REPLAY)", file=sys.stderr)
        return 2
    days = L.eligible_days()
    if a.phase in ("ref", "primary"):
        # REF first, alone: its saved arm rows are the exploration data set (operator 10/28: bring the exploration
        # forward); 'primary' then adds the gate variants (REF outputs already present are skipped).
        import concurrent.futures as cf
        with cf.ThreadPoolExecutor(max_workers=a.jobs) as ex:
            futs = [ex.submit(lambda d=d: f"{d} REF: skipped, past {a.stop_at} ET" if L.past_stop(a.stop_at)
                              else ref_with_arms(d, "REF", sha)) for d in days]
            for f in cf.as_completed(futs):
                print(f.result(), flush=True)
        if a.phase == "ref":
            return 0
        L.batch([(d, v, o) for d in days for v, o in PRIMARY.items() if v != "REF"], sha, a.jobs, a.stop_at)
        dup = sorted({days[0], days[len(days) // 2], days[-1]}) if days else []
        L.batch([(d, "REF_DUP", {}) for d in dup], sha, a.jobs, a.stop_at)
    else:
        for d in days:
            print(calibrate_crand(d, sha, a.stop_at), flush=True)
            s = L.rand_share(d, "SLOPE")
            if s is not None:
                L.batch([(d, f"RAND_{k}", {"ai_watch_wr_rand_refuse_share": s, "ai_watch_wr_rand_salt": f"SN-{k}"})
                         for k in SEEDS], sha, a.jobs, a.stop_at)
    return 0


if __name__ == "__main__":
    sys.exit(main())
