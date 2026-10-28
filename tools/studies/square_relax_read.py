#!/usr/bin/env python3
"""square_relax_read.py — scorer for docs/studies/square_relax_prereg.json (amended, amended_2, margin, ready,
screen_first, stage2_monitoring).

Trade pricing is stale_age_read's (first SIP trade within 60 s of the replayed decision; failed fetch = session not
scored; > 10% no-print = excluded; full SIP spread charged). For each primary v in TIGHT20, PRE45 and session:
  lift_ref   mean net(v) - mean net(REF)
  lift_rand  mean net(v) - mean over seeds of mean net(RAND_<v>_k)   (none when the calibration is UNMATCHED)
  moved      matched pairs (same symbol, nearest within 600 s) whose entries differ by >= 30 s; MOVED SHARE = moved
             pairs / REF trades; TIMING = median and mean of v - REF seconds over moved pairs
  lift_moved per half: mean net of moved v trades minus their matched REF trades
Stage 1 (the screen): the scored recorded sessions <= 2026-10-27 split by count, EARLY = first half, LATE = rest.
PASS per v (Bonferroni over 2, one-sided bounds at 0.975, day-clustered): TIMING median and mean <= -1.0 min;
MOVED SHARE >= 10%; in BOTH halves lift_ref's lower bound >= -M and its point estimate >= 0 (M = 2 bp, raised by any
REF_DUP difference > 1 bp); lift_moved >= -5 bp in both halves; lift_rand >= 0; >= 120 REF and v trades per half;
MDE <= 6 bp per half; the neighbour (TIGHT25 for TIGHT20, PRE40 for PRE45) not 'costs value' (upper bound < -M in
either half). UNPROVEN when power is met but the bound misses with an estimate >= 0; CONTROL UNMATCHED when more than
a third of sessions have no lift_rand.
  (default) count   --canary   --score   --stage2-check DAY (the per-session harm / futility check)
USAGE (mini, after the close): .venv/bin/python tools/studies/square_relax_read.py [--canary | --score | --stage2-check DAY]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tools" / "studies"), str(ROOT / "tools"), str(ROOT)]
import late_gates_read as LR  # noqa: E402
import stale_age_read as SR  # noqa: E402
import std_scorer as S  # noqa: E402

OUT = Path(os.path.expanduser("~/replay_runs/square_relax"))
PREREG = ROOT / "docs" / "studies" / "square_relax_prereg.json"
STAGE1_LAST = "2026-10-27"
PRIMARIES = {"TIGHT20": "TIGHT25", "PRE45": "PRE40"}
SEEDS = (1, 2, 3)
M, MOVE_SEC, FLOOR = 2.0, SR.MOVE_SEC, 120


def ub95(vals):
    st = S.day_stat([v for v in vals if v is not None])
    if not st["n"] or not math.isfinite(st.get("se", float("nan"))):
        return None
    return st["mean"] + SR.t975(st["n"]) * st["se"]


def priced(day, var, out_dir, pr, sip):
    res = LR.load(day, var, out_dir)
    return None if res is None else SR.price_trades(res, day, pr, sip)


def session(day, v, out_dir, pr, sip, canary_bp=0.0) -> dict:
    row = {"day": day, "v": v}
    ref = priced(day, "REF", out_dir, pr, sip)
    g = priced(day, v, out_dir, pr, sip)
    if ref is None or g is None:
        row["excluded"] = "missing REF or variant"
        return row
    if day in sip.failed:
        row["excluded"] = "SIP fetch failed"
        return row
    (rt, rnp), (gt, gnp) = ref, g
    tot = len(rt) + len(gt) + rnp + gnp
    if tot and (rnp + gnp) / tot > 0.10:
        row["excluded"] = f"no SIP print {rnp + gnp}/{tot}"
        return row
    if len(rt) < 5:
        row["excluded"] = f"ref_trades {len(rt)} < 5"
        return row
    if canary_bp:
        for x in gt:
            x["net"] += canary_bp
    pairs = SR.match_pairs(rt, gt)
    moved = [(i, j, dt) for i, j, dt in pairs if abs(dt) >= MOVE_SEC]
    row.update({"n": {"REF": len(rt), v: len(gt)}, "lift_ref": SR.mean_net(gt) - SR.mean_net(rt),
                "timing": [dt for _, _, dt in moved], "moved_share": len(moved) / len(rt),
                "moved_diffs": [gt[j]["net"] - rt[i]["net"] for i, j, _ in moved]})
    seeds = [priced(day, f"RAND_{v}_{k}", out_dir, pr, sip) for k in SEEDS]
    cal = out_dir / f"{day}-RAND_{v}_share.json"
    unmatched = (not cal.exists()) or json.loads(cal.read_text()).get("unmatched")
    if all(x is not None for x in seeds) and not unmatched:
        rand = statistics.fmean(SR.mean_net(x[0]) or 0.0 for x in seeds)
        row["lift_rand"] = SR.mean_net(gt) - rand
        row["ctl_rand"] = rand - SR.mean_net(rt)
    mde1 = priced(day, "MDE1", out_dir, pr, sip)
    if mde1 and mde1[0]:
        row["ctl_mde1"] = SR.mean_net(mde1[0]) - SR.mean_net(rt)
    return row


def neighbour_lifts(day, nb, out_dir, pr, sip):
    ref, g = priced(day, "REF", out_dir, pr, sip), priced(day, nb, out_dir, pr, sip)
    if not ref or not g or len(ref[0]) < 5 or not g[0]:
        return None
    return SR.mean_net(g[0]) - SR.mean_net(ref[0])


def stage1_rows(v, out_dir, pr, sip, canary_bp=0.0):
    days = sorted({p.name[:10] for p in out_dir.glob("20??-??-??-REF.json")} )
    days = [d for d in days if d <= STAGE1_LAST]
    rows = [session(d, v, out_dir, pr, sip, canary_bp) for d in days]
    ok = [r for r in rows if not r.get("excluded")]
    k = math.ceil(len(ok) / 2)
    return ok[:k], ok[k:], [r for r in rows if r.get("excluded")]


def dup_m(out_dir, pr, sip) -> float:
    worst = 0.0
    for p in sorted(out_dir.glob("20??-??-??-REF_DUP.json")):
        d = p.name[:10]
        a, b = priced(d, "REF", out_dir, pr, sip), priced(d, "REF_DUP", out_dir, pr, sip)
        if a and b and a[0] and b[0]:
            worst = max(worst, abs(SR.mean_net(b[0]) - SR.mean_net(a[0])))
    return M + (worst if worst > 1.0 else 0.0)


def verdict(v, early, late, nb_early, nb_late, margin) -> dict:
    halves = {"EARLY": early, "LATE": late}
    out = {"v": v, "margin": margin, "halves": {}}
    timing_all = [dt for h in (early, late) for r in h for dt in r["timing"]]
    med = statistics.median(timing_all) / 60 if timing_all else None
    mean = statistics.fmean(timing_all) / 60 if timing_all else None
    moved = statistics.fmean(r["moved_share"] for h in (early, late) for r in h) if early or late else None
    rows_all = early + late
    lrand_rows = [r for r in rows_all if r.get("lift_rand") is not None]
    unmatched = len(lrand_rows) < (2 / 3) * len(rows_all)
    lrand = S.day_stat([r["lift_rand"] for r in lrand_rows])
    ok_halves, power_ok, unproven = True, True, False
    for name, h in halves.items():
        lr = [r["lift_ref"] for r in h]
        st = S.day_stat(lr)
        lb = SR.lower95(lr)
        md = [x for r in h for x in r["moved_diffs"]]
        n_ref = sum(r["n"]["REF"] for r in h)
        n_v = sum(r["n"][v] for r in h)
        mde = LR.mde(h)
        hok = (lb is not None and lb >= -margin and st["mean"] >= 0
               and (statistics.fmean(md) if md else -1e9) >= -5.0)
        pw = n_ref >= FLOOR and n_v >= FLOOR and mde is not None and mde <= 6.0
        out["halves"][name] = {"sessions": len(h), "lift_ref": st, "lower95": lb, "moved_lift": statistics.fmean(md) if md else None,
                               "trades": [n_ref, n_v], "mde": mde, "ok": hok, "power_ok": pw}
        ok_halves &= hok
        power_ok &= pw
        if pw and not hok and st["mean"] >= 0:
            unproven = True
    nb_costs = any(ub is not None and ub < -margin for ub in (ub95(nb_early), ub95(nb_late)))
    checks = {"timing": med is not None and mean is not None and med <= -1.0 and mean <= -1.0,
              "moved_share": moved is not None and moved >= 0.10, "halves": ok_halves,
              "lift_rand": (not unmatched) and lrand["mean"] >= 0, "neighbour_ok": not nb_costs}
    if not power_ok:
        vv = "UNDERPOWERED"
    elif unmatched:
        vv = "CONTROL UNMATCHED (UNPROVEN)"
    elif all(checks.values()):
        vv = "SCREEN PASS"
    elif unproven and checks["timing"] and checks["moved_share"] and not nb_costs:
        vv = "UNPROVEN"
    else:
        vv = "SCREEN FAIL"
    out.update({"timing_median_min": med, "timing_mean_min": mean, "moved_share": moved, "lift_rand": lrand,
                "checks": checks, "verdict": vv})
    return out


def stage2_check(day, v, out_dir, pr, sip, prior: list[dict]) -> dict:
    """Per-session harm check (every forward session) and futility check (from the 3rd)."""
    r = session(day, v, out_dir, pr, sip)
    if r.get("excluded"):
        return {"day": day, "v": v, "excluded": r["excluded"]}
    med = statistics.median(r["timing"]) if r["timing"] else 0.0
    harm = (med >= 0) or (r["n"][v] < 0.70 * r["n"]["REF"]) or (r["lift_ref"] < -10.0)
    rows = prior + [r]
    futile = len(rows) >= 3 and (ub95([x["lift_ref"] for x in rows]) or 0.0) < 0
    return {"day": day, "v": v, "timing_median_s": med, "trades": r["n"], "lift_ref": r["lift_ref"],
            "harm": harm, "futile": futile, "stop": harm or futile}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--canary", action="store_true")
    ap.add_argument("--score", action="store_true")
    ap.add_argument("--stage2-check", default=None)
    a = ap.parse_args()
    pr, sip = LR.Pricer.live(), SR.SipEntry(SR.SipEntry.live().fetch, OUT / "sip_entry_cache.json")
    try:
        if a.stage2_check:
            log = OUT / "stage2_checks.jsonl"
            prior = {v: [] for v in PRIMARIES}
            if log.exists():
                for line in open(log):
                    x = json.loads(line)
                    if not x.get("excluded") and x.get("day") < a.stage2_check:
                        prior[x["v"]].append(session(x["day"], x["v"], OUT, pr, sip))
            with open(log, "a") as fh:
                for v in PRIMARIES:
                    res = stage2_check(a.stage2_check, v, OUT, pr, sip, prior[v])
                    fh.write(json.dumps(res) + "\n")
                    print(json.dumps(res))
            return 0
        if a.canary:
            res = {}
            for v in PRIMARIES:
                e0, l0, _ = stage1_rows(v, OUT, pr, sip)
                e1, l1, _ = stage1_rows(v, OUT, pr, sip, canary_bp=10.0)
                for lab, x, y in (("EARLY", e0, e1), ("LATE", l0, l1)):
                    res[f"{v}_{lab}"] = S.day_stat([r["lift_ref"] for r in y])["mean"] - S.day_stat([r["lift_ref"] for r in x])["mean"]
            res["canary_ok"] = all(abs(x - 10.0) <= 1.0 for k, x in res.items())
            (OUT / "canary.json").write_text(json.dumps(res, indent=1))
            print(json.dumps(res, indent=1))
            return 0
        cnt = {}
        for v in PRIMARIES:
            e, l, ex = stage1_rows(v, OUT, pr, sip)
            cnt[v] = {"sessions": [len(e), len(l)], "excluded": [(r["day"], r["excluded"]) for r in ex],
                      "trades": [[sum(r["n"]["REF"] for r in h), sum(r["n"][v] for r in h)] for h in (e, l)],
                      "moved_share": statistics.fmean(r["moved_share"] for r in e + l) if e or l else None,
                      "mde": [LR.mde(e), LR.mde(l)]}
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
        margin = dup_m(OUT, pr, sip)
        result = {}
        for v, nb in PRIMARIES.items():
            e, l, _ = stage1_rows(v, OUT, pr, sip)
            nbe = [neighbour_lifts(r["day"], nb, OUT, pr, sip) for r in e]
            nbl = [neighbour_lifts(r["day"], nb, OUT, pr, sip) for r in l]
            result[v] = verdict(v, e, l, nbe, nbl, margin)
        (OUT / "result_stage1.json").write_text(json.dumps(result, indent=1, default=str))
        print(json.dumps(result, indent=1, default=str))
        return 0
    finally:
        pr.save()
        sip.save()


if __name__ == "__main__":
    sys.exit(main())
