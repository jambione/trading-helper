#!/usr/bin/env python3
"""slope_now_read.py — scorer for the PRE-REGISTERED slope-now gate (docs/studies/slope_now_replay_prereg.json,
amended, amended_2, ready). This scores the fixed rule as registered; the winners-vs-losers exploration is
slope_now_explore.py and has no pass.

Costing as late_gates_read (full SIP spread at entry; repriced check at the SIP 1m close of the entry minute;
open_at_end marked at 15:49). Sessions: every REF in ~/replay_runs/slope_now except 2026-10-27 (fidelity day, not
scored), in date order, capped at 40; a session needs >= 5 REF trades; a session whose REF pitch presence share
(arm rows with all three pitch fields fresh) is more than 15 points off 10/27's is excluded (amended 6).
Per session: lift_ref = mean net(SLOPE) - mean net(REF); lift_rand = mean net(SLOPE) - mean over seeds of
mean net(CRAND_k) when the seed-averaged CRAND count is within +-15% of SLOPE's (amended_2 (1)).
PASS: lift_ref >= +3 bp (+ REF_DUP penalty) with t >= 2.0; > 0 in both halves; lift_rand > 0 with t >= 2.0 (dropped
only if the net refused share and the removed share are both < 0.05); repriced lift_ref >= +3 with t >= 2.0 and > 0
in both halves; >= 300 REF and >= 150 SLOPE trades. Power per half: MDE = 3.08 x max(sd(RAND avg - REF),
sd(MDE1 - REF)) / sqrt(sessions) <= 6 bp and >= 10 sessions, else UNDERPOWERED (FAIL at the 40 cap). More than a
third of sessions without lift_rand = CONTROL UNMATCHED (UNPROVEN). Either half <= 0 = FAIL.
  (default) count   --canary   --score
USAGE (mini, after the close): .venv/bin/python tools/studies/slope_now_read.py [--canary | --score]
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tools" / "studies"), str(ROOT / "tools"), str(ROOT)]
import late_gates_read as LR  # noqa: E402
import std_scorer as S  # noqa: E402

OUT = Path(os.path.expanduser("~/replay_runs/slope_now"))
PREREG = ROOT / "docs" / "studies" / "slope_now_replay_prereg.json"
FIDELITY_DAY = "2026-10-27"
SEEDS = (1, 2, 3)
T_MIN, BAR, MDE_MAX, MIN_HALF, CAP, PRESENCE_TOL = 2.0, 3.0, 6.0, 10, 40, 0.15
PITCH = ("pctr_fast_pitch", "pctr_slow_pitch", "pctr_slow_pitch_prev")


def presence(out_dir: Path, day: str) -> float | None:
    """Share of REF arm rows with all three pitch fields present and fresh (<= 12 s)."""
    p = out_dir / f"{day}-REF-arms.jsonl.gz"
    if not p.exists():
        return None
    n = ok = 0
    with gzip.open(p, "rt") as fh:
        for line in fh:
            r = json.loads(line)
            n += 1
            pts = r.get("pctr_pitch_ts")
            fresh = pts is not None and 0 <= float(r.get("ts") or 0) - float(pts) <= 12.0
            ok += fresh and all(r.get(k) is not None for k in PITCH)
    return ok / n if n else None


def scored_days(out_dir: Path) -> list[str]:
    days = sorted({p.name[:10] for p in out_dir.glob("20??-??-??-REF.json")})
    return [d for d in days if d != FIDELITY_DAY][:CAP]


def session(day: str, out_dir: Path, pr, canary_bp: float = 0.0, base_presence: float | None = None) -> dict:
    names = ["REF", "SLOPE", "MDE1", "EXISTS", "SLOPE_OPEN"] + [f"CRAND_{k}" for k in SEEDS] + \
            [f"RAND_{k}" for k in SEEDS]
    raw = {v: LR.load(day, v, out_dir) for v in names}
    row = {"day": day, "missing": [v for v in names if raw[v] is None]}
    if raw["REF"] is None or raw["SLOPE"] is None:
        row["excluded"] = "missing REF or SLOPE"
        return row
    pres = presence(out_dir, day)
    row["presence"] = pres
    if base_presence is not None and pres is not None and abs(pres - base_presence) > PRESENCE_TOL:
        row["excluded"] = f"pitch presence {pres:.2f} vs {base_presence:.2f} on {FIDELITY_DAY}"
        return row
    tr = {v: LR.trades(raw[v], day, pr) for v in names if raw[v] is not None}
    if canary_bp:
        for t in tr["SLOPE"]:
            t["ret"] += canary_bp / 1e4
            t["gross"] += canary_bp
            if t["gross_rp"] is not None:
                t["gross_rp"] += canary_bp
    ch = LR.charge(tr, pr)
    row["n"] = {v: len(x) for v, x in tr.items()}
    if ch["excluded"]:
        row["excluded"] = ch["excluded"]
        return row
    if len(tr["REF"]) < 5:
        row["excluded"] = f"ref_trades {len(tr['REF'])} < 5"
        return row
    ref, g = tr["REF"], tr["SLOPE"]
    cl = LR.classify(ref, g)
    row["classes"] = {k: cl[k] for k in ("same", "delayed", "removed", "new")}
    row["delays"] = cl["delays"]
    row["share_net"] = max(0.0, 1 - len(g) / len(ref))
    row["share_removed"] = cl["removed"] / len(ref)
    m_ref, m_g = LR.mean_of(ref) or 0.0, LR.mean_of(g)
    row["lift_ref"] = (m_g if m_g is not None else 0.0) - m_ref
    rp_ref, rp_g = LR.mean_of(ref, "net_rp"), LR.mean_of(g, "net_rp")
    row["lift_ref_rp"] = None if rp_ref is None or rp_g is None else rp_g - rp_ref
    crand = [tr[f"CRAND_{k}"] for k in SEEDS if f"CRAND_{k}" in tr]
    if len(crand) == len(SEEDS):
        n_c = statistics.fmean(len(s) for s in crand)
        row["crand_count_ok"] = abs(n_c - len(g)) <= 0.15 * max(len(g), 1)
        m_c = statistics.fmean((LR.mean_of(s) or 0.0) for s in crand)
        row["lift_rand"] = ((m_g or 0.0) - m_c) if row["crand_count_ok"] else None
    rand = [tr[f"RAND_{k}"] for k in SEEDS if f"RAND_{k}" in tr]
    if len(rand) == len(SEEDS):
        row["ctl_rand"] = statistics.fmean((LR.mean_of(s) or 0.0) for s in rand) - m_ref
    if "MDE1" in tr:
        row["ctl_mde1"] = (LR.mean_of(tr["MDE1"]) or 0.0) - m_ref
    for v in ("EXISTS", "SLOPE_OPEN"):
        if v in tr:
            row[f"lift_{v}"] = (LR.mean_of(tr[v]) or 0.0) - m_ref
    row["m"] = {"REF": m_ref, "SLOPE": m_g}
    return row


def mde(rows: list[dict]) -> float | None:
    vals = []
    for key in ("ctl_rand", "ctl_mde1"):
        xs = [r[key] for r in rows if r.get(key) is not None]
        if len(xs) >= 3:
            vals.append(3.08 * statistics.stdev(xs) / math.sqrt(len(rows)))
    return max(vals) if vals else None


def rows_for(out_dir: Path, pr, canary_bp: float = 0.0):
    base = presence(out_dir, FIDELITY_DAY)
    rows, excl = [], []
    for d in scored_days(out_dir):
        r = session(d, out_dir, pr, canary_bp, base)
        (excl if r.get("excluded") else rows).append(r)
    return rows, excl


def count(rows: list[dict], excl: list[dict]) -> dict:
    a, b = LR.halves(rows)
    n_ref = sum(r["n"]["REF"] for r in rows)
    n_g = sum(r["n"]["SLOPE"] for r in rows)
    out = {"scored_sessions": len(rows), "half_sessions": [len(a), len(b)],
           "excluded": [(r["day"], r["excluded"]) for r in excl], "trades": {"REF": n_ref, "SLOPE": n_g},
           "share_net_pooled": (1 - n_g / n_ref) if n_ref else None,
           "share_removed_pooled": (sum(r["classes"]["removed"] for r in rows) / n_ref) if n_ref else None,
           "classes": {k: sum(r["classes"][k] for r in rows) for k in ("same", "delayed", "removed", "new")},
           "no_lift_rand": [r["day"] for r in rows if r.get("lift_rand") is None],
           "mde_half": [mde(a), mde(b)]}
    out["power_ok"] = (all(m is not None and m <= MDE_MAX for m in out["mde_half"])
                       and min(out["half_sessions"]) >= MIN_HALF)
    return out


def verdict(rows: list[dict], cnt: dict, dup_pen: float) -> dict:
    a, b = LR.halves(rows)
    st = S.day_stat([r["lift_ref"] for r in rows])
    st_rp = S.day_stat([r["lift_ref_rp"] for r in rows])
    ha, hb = (S.day_stat([r["lift_ref"] for r in h])["mean"] for h in (a, b))
    hra, hrb = (S.day_stat([r["lift_ref_rp"] for r in h])["mean"] for h in (a, b))
    rand = S.day_stat([r["lift_rand"] for r in rows if r.get("lift_rand") is not None])
    uninformative = (cnt["share_net_pooled"] is not None and cnt["share_net_pooled"] < 0.05
                     and cnt["share_removed_pooled"] is not None and cnt["share_removed_pooled"] < 0.05)
    unmatched = (not uninformative) and len(cnt["no_lift_rand"]) > len(rows) / 3
    bar = BAR + dup_pen
    checks = {
        "lift_ref": st["mean"] >= bar and st["t"] >= T_MIN,
        "both_halves": ha > 0 and hb > 0,
        "lift_rand": True if uninformative else (rand["mean"] > 0 and rand["t"] >= T_MIN),
        "trades": cnt["trades"]["REF"] >= 300 and cnt["trades"]["SLOPE"] >= 150,
        "repriced": st_rp["mean"] >= bar and st_rp["t"] >= T_MIN and hra > 0 and hrb > 0,
    }
    if not cnt["power_ok"]:
        v = "FAIL (UNDERPOWERED at the cap)" if len(rows) >= CAP else "UNDERPOWERED (not scored)"
    elif ha <= 0 or hb <= 0:
        v = "FAIL"
    elif unmatched:
        v = "CONTROL UNMATCHED (UNPROVEN)"
    else:
        v = "PASS" if all(checks.values()) else "FAIL"
    m_g = [r["m"]["SLOPE"] for r in rows if r["m"].get("SLOPE") is not None]
    ex = S.day_stat([r.get("lift_EXISTS") for r in rows])
    return {"verdict": v, "checks": checks, "rand_uninformative": uninformative, "bar_bp": bar,
            "lift_ref": st, "lift_ref_halves": [ha, hb], "lift_ref_repriced": st_rp, "repriced_halves": [hra, hrb],
            "lift_rand": rand, "slope_mean_net": statistics.fmean(m_g) if m_g else None,
            "info": {"EXISTS": ex, "SLOPE_OPEN": S.day_stat([r.get("lift_SLOPE_OPEN") for r in rows]),
                     "pitch_history_reading": ("the effect is the pitch-history requirement, not the slope"
                                               if math.isfinite(ex["mean"]) and math.isfinite(st["mean"])
                                               and ex["mean"] >= st["mean"] else None),
                     "median_delay_min": (statistics.median([d for r in rows for d in r["delays"]])
                                          if any(r["delays"] for r in rows) else None)}}


def dup_penalty(out_dir: Path, pr) -> float:
    worst = 0.0
    for p in sorted(out_dir.glob("20??-??-??-REF_DUP.json")):
        d = p.name[:10]
        tr = {"R": LR.trades(LR.load(d, "REF", out_dir), d, pr), "D": LR.trades(LR.load(d, "REF_DUP", out_dir), d, pr)}
        LR.charge(tr, pr)
        worst = max(worst, abs((LR.mean_of(tr["D"]) or 0.0) - (LR.mean_of(tr["R"]) or 0.0)))
    return worst if worst > 1.0 else 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--canary", action="store_true")
    ap.add_argument("--score", action="store_true")
    a = ap.parse_args()
    pr = LR.Pricer.live()
    try:
        if a.canary:
            r0, _ = rows_for(OUT, pr)
            r1, _ = rows_for(OUT, pr, canary_bp=10.0)
            delta = S.day_stat([r["lift_ref"] for r in r1])["mean"] - S.day_stat([r["lift_ref"] for r in r0])["mean"]
            res = {"delta": delta, "canary_ok": bool(math.isfinite(delta) and abs(delta - 10.0) <= 1.0)}
            (OUT / "canary.json").write_text(json.dumps(res, indent=1))
            print(json.dumps(res, indent=1))
            return 0
        rows, excl = rows_for(OUT, pr)
        cnt = count(rows, excl)
        (OUT / "count.json").write_text(json.dumps(cnt, indent=1, default=str))
        if not a.score:
            print(json.dumps(cnt, indent=1, default=str))
            return 0
        d = json.loads(PREREG.read_text())
        can = json.loads((OUT / "canary.json").read_text()) if (OUT / "canary.json").exists() else {}
        fid = json.loads((OUT / "fidelity.json").read_text()) if (OUT / "fidelity.json").exists() else {}
        if (d.get("ready") or {}).get("verdict") != "READY" or not can.get("canary_ok") or not fid.get("pass"):
            print("refusing: needs a READY prereg, canary_ok and a fidelity pass", file=sys.stderr)
            return 2
        res = verdict(rows, cnt, dup_penalty(OUT, pr))
        (OUT / "result.json").write_text(json.dumps(res, indent=1, default=str))
        print(json.dumps(res, indent=1, default=str))
        return 0
    finally:
        pr.save()


if __name__ == "__main__":
    sys.exit(main())
