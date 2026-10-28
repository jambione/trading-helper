#!/usr/bin/env python3
"""open_spread_read.py — scorer for docs/studies/open_spread_replay_prereg.json (amended, amended_2, ready).

Pricing as stale_age_read (first SIP trade within 60 s, full SIP spread at entry). Window = 09:30:00-09:45:59 ET.
Per stage-1 session (<= 2026-10-27):
  added(v)       v's trades entered in the window with no REF match (same symbol within 600 s)
  added_diff     mean net added(OPEN16) - mean over seeds of mean net added(RAND_k); sessions where OPEN16 or the
                 seed-averaged RAND has 0 added trades are left out of the primary (counted)
  session_lift   sum net OPEN16 - sum net REF (bp per session at equal size)
  lift_ref       mean net OPEN16 - mean net REF (harm check)   lift_rand  mean net OPEN16 - mean over seeds RAND
  placebo        added net RAND_1 - mean(RAND_2, RAND_3) added net, x sqrt(3/2) (MDE)
A session is excluded for every arm when >= 2 window trades in any arm lack an entry spread.
PASS: added_diff point >= 0 in BOTH halves (>= 6 primary sessions per half, else UNDERPOWERED) and its one-sided
lower bound (0.975, day-clustered, t n-1) >= -5 bp over all; session_lift point >= 0 in both halves; lift_ref lower
bound >= -2 bp (+ REF_DUP penalty) in both halves; OPEN16 early share > REF's; lift_rand >= 0; >= 30 added trades and
>= 120 REF trades per half; MDE (3.08 x sd(placebo) / sqrt(n)) <= 10 bp per half. CONTROL UNMATCHED when > 1/3 of
sessions are unmatched or RAND's added trades are not within +-25% of OPEN16's. FAIL is reported as 'refuted'
(a point estimate < 0) or 'not shown' (bound only).
  (default) count   --canary   --score
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import math
import os
import statistics
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tools" / "studies"), str(ROOT / "tools"), str(ROOT)]
import late_gates_read as LR  # noqa: E402
import stale_age_read as SR  # noqa: E402
import std_scorer as S  # noqa: E402

ET = ZoneInfo("America/New_York")
OUT = Path(os.path.expanduser("~/replay_runs/open_spread"))
PREREG = ROOT / "docs" / "studies" / "open_spread_replay_prereg.json"
LAST, WIN, SEEDS = "2026-10-27", 16, (1, 2, 3)
MATCH_SEC = 600


def window(day: str) -> tuple[float, float]:
    t0 = _dt.datetime.fromisoformat(f"{day} 09:30").replace(tzinfo=ET).timestamp()
    return t0, t0 + WIN * 60


def priced(day, var, out_dir, pr, sip):
    res = LR.load(day, var, out_dir)
    if res is None:
        return None
    tr, _ = SR.price_trades(res, day, pr, sip)
    return tr


def in_window(trades, day):
    t0, t1 = window(day)
    return [t for t in trades if t0 <= t["entry_ts"] < t1]


def added(ref, g, day):
    return [t for t in in_window(g, day)
            if not any(r["symbol"] == t["symbol"] and abs(r["entry_ts"] - t["entry_ts"]) <= MATCH_SEC for r in ref)]


def mean(xs):
    xs = [x for x in xs if x is not None]
    return statistics.fmean(xs) if xs else None


def session(day, out_dir, pr, sip, canary_bp=0.0) -> dict:
    row = {"day": day}
    names = ["REF", "OPEN16"] + [f"RAND_{k}" for k in SEEDS]
    tr = {v: priced(day, v, out_dir, pr, sip) for v in names}
    if tr["REF"] is None or tr["OPEN16"] is None:
        row["excluded"] = "missing REF or OPEN16"
        return row
    if day in sip.failed:
        row["excluded"] = "SIP fetch failed"
        return row
    if any(tr[v] is not None and sum(t.get("spread") is None for t in in_window(tr[v], day)) >= 2 for v in names):
        row["excluded"] = ">= 2 window trades without an entry spread"
        return row
    if canary_bp:
        for t in tr["OPEN16"]:
            t["net"] += canary_bp
    ref, g = tr["REF"], tr["OPEN16"]
    row["n"] = {"REF": len(ref), "OPEN16": len(g)}
    row["lift_ref"] = (mean(t["net"] for t in g) or 0.0) - (mean(t["net"] for t in ref) or 0.0)
    row["session_lift"] = sum(t["net"] for t in g) - sum(t["net"] for t in ref)
    row["early"] = {"REF": len(in_window(ref, day)), "OPEN16": len(in_window(g, day))}
    a_g = added(ref, g, day)
    row["added"] = {"OPEN16": len(a_g)}
    seeds = [tr[f"RAND_{k}"] for k in SEEDS]
    cal = out_dir / f"{day}-RAND_share.json"
    row["unmatched"] = bool(cal.exists() and json.loads(cal.read_text()).get("unmatched")) or not cal.exists()
    if all(s is not None for s in seeds):
        a_r = [added(ref, s, day) for s in seeds]
        row["added"]["RAND"] = statistics.fmean(len(x) for x in a_r)
        row["lift_rand"] = (mean(t["net"] for t in g) or 0.0) - statistics.fmean(
            (mean(t["net"] for t in s) or 0.0) for s in seeds)
        m_g, m_r = mean(t["net"] for t in a_g), [mean(t["net"] for t in x) for x in a_r]
        if a_g and row["added"]["RAND"] > 0 and any(m is not None for m in m_r):
            row["added_diff"] = m_g - mean(m_r)
        if m_r[0] is not None and (m_r[1] is not None or m_r[2] is not None):
            row["placebo"] = (m_r[0] - mean(m_r[1:])) * math.sqrt(1.5)
    return row


def stage1_rows(out_dir, pr, sip, canary_bp=0.0):
    days = sorted({p.name[:10] for p in out_dir.glob("20??-??-??-REF.json")})
    rows = [session(d, out_dir, pr, sip, canary_bp) for d in days if d <= LAST]
    ok = [r for r in rows if not r.get("excluded")]
    return ok, [r for r in rows if r.get("excluded")]


def lb(vals):
    return SR.lower95([v for v in vals if v is not None])


def verdict(rows, dup_pen: float = 0.0) -> dict:
    k = math.ceil(len(rows) / 2)
    halves = {"EARLY": rows[:k], "LATE": rows[k:]}
    out = {"halves": {}}
    prim_ok = power_ok = True
    refuted = False
    for name, h in halves.items():
        prim = [r["added_diff"] for r in h if r.get("added_diff") is not None]
        plac = [r["placebo"] for r in h if r.get("placebo") is not None]
        mde = 3.08 * statistics.stdev(plac) / math.sqrt(len(h)) if len(plac) >= 3 and h else None
        harm_lb = lb([r["lift_ref"] for r in h])
        sl = mean(r["session_lift"] for r in h)
        p_mean = mean(prim)
        hh = {"sessions": len(h), "primary_sessions": len(prim), "added_diff": p_mean, "session_lift": sl,
              "harm_lower": harm_lb, "mde": mde, "ref_trades": sum(r["n"]["REF"] for r in h)}
        hh["power_ok"] = len(prim) >= 6 and mde is not None and mde <= 10.0 and hh["ref_trades"] >= 120
        hh["ok"] = (p_mean is not None and p_mean >= 0 and sl is not None and sl >= 0
                    and harm_lb is not None and harm_lb >= -(2.0 + dup_pen))
        refuted |= (p_mean is not None and p_mean < 0) or (sl is not None and sl < 0)
        out["halves"][name] = hh
        prim_ok &= hh["ok"]
        power_ok &= hh["power_ok"]
    allp = [r["added_diff"] for r in rows if r.get("added_diff") is not None]
    bound = lb(allp)
    add_g = sum(r["added"]["OPEN16"] for r in rows)
    add_r = sum(r["added"].get("RAND", 0) for r in rows)
    early = {v: sum(r["early"][v] for r in rows) for v in ("REF", "OPEN16")}
    unmatched = sum(bool(r.get("unmatched")) for r in rows) > len(rows) / 3 or not (
        add_g and abs(add_r - add_g) <= 0.25 * add_g)
    checks = {"primary_halves": prim_ok, "primary_bound": bound is not None and bound >= -5.0,
              "early_share": early["OPEN16"] > early["REF"],
              "lift_rand": (mean(r.get("lift_rand") for r in rows) or -1.0) >= 0, "added_trades": add_g >= 30}
    if not power_ok or add_g < 30:
        v = "UNDERPOWERED"
    elif unmatched:
        v = "CONTROL UNMATCHED (UNPROVEN)"
    elif all(checks.values()):
        v = "SCREEN PASS"
    elif refuted:
        v = "FAIL (refuted)"
    else:
        v = "FAIL (not shown)"
    out.update({"verdict": v, "checks": checks, "added_diff_lower": bound, "added": {"OPEN16": add_g, "RAND": add_r},
                "early_trades": early})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--canary", action="store_true")
    ap.add_argument("--score", action="store_true")
    a = ap.parse_args()
    pr, sip = LR.Pricer.live(), SR.SipEntry(SR.SipEntry.live().fetch, OUT / "sip_entry_cache.json")
    try:
        if a.canary:
            r0, _ = stage1_rows(OUT, pr, sip)
            r1, _ = stage1_rows(OUT, pr, sip, canary_bp=10.0)
            d = (mean(r["lift_ref"] for r in r1) or 0) - (mean(r["lift_ref"] for r in r0) or 0)
            res = {"delta": d, "canary_ok": abs(d - 10.0) <= 1.0}
            (OUT / "canary.json").write_text(json.dumps(res, indent=1))
            print(json.dumps(res, indent=1))
            return 0
        rows, excl = stage1_rows(OUT, pr, sip)
        cnt = {"sessions": len(rows), "excluded": [(r["day"], r["excluded"]) for r in excl],
               "added": {"OPEN16": sum(r["added"]["OPEN16"] for r in rows),
                         "RAND": sum(r["added"].get("RAND", 0) for r in rows)},
               "primary_sessions": sum(r.get("added_diff") is not None for r in rows)}
        (OUT / "count_scored.json").write_text(json.dumps(cnt, indent=1, default=str))
        if not a.score:
            print(json.dumps(cnt, indent=1, default=str))
            return 0
        d = json.loads(PREREG.read_text())
        can = json.loads((OUT / "canary.json").read_text()) if (OUT / "canary.json").exists() else {}
        fid = json.loads((OUT / "fidelity.json").read_text()) if (OUT / "fidelity.json").exists() else {}
        if (d.get("ready") or {}).get("verdict") != "READY" or not can.get("canary_ok") or not fid.get("pass"):
            print("refusing: needs a READY prereg, canary_ok and a fidelity pass", file=sys.stderr)
            return 2
        res = verdict(rows)
        (OUT / "result_stage1.json").write_text(json.dumps(res, indent=1, default=str))
        print(json.dumps(res, indent=1, default=str))
        return 0
    finally:
        pr.save()
        sip.save()


if __name__ == "__main__":
    sys.exit(main())
