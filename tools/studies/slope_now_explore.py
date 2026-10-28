#!/usr/bin/env python3
"""slope_now_explore.py — winners vs losers by %R pitch at the buy (operator 10/28: "come back to that when we have
the data"). EXPLORATION ONLY, on the explore window 2026-09-28..2026-10-13; later sessions are sealed for the
confirmation of the ONE rule this exploration nominates (to be pre-registered before any of them is read).

Each REF trade from slope_now_run (~/replay_runs/slope_now/DAY-REF.json, priced as stale_age_read: SIP entry print,
full SIP spread) is joined to its arm row in DAY-REF-arms.jsonl.gz: the last ARM_OK row for the symbol at or before
the entry, within 90 s. The pitch fields come from that row (stale pitch > 12 s = missing, as the gate).

Tables (n, mean net bp, win %, day-clustered t): by slow pitch band, fast pitch band, slow pitch change (slow - prev),
%R level band, the gate's three conditions, and pitch missing. Every cell is information; nothing here passes.
USAGE (mini): .venv/bin/python tools/studies/slope_now_explore.py [--out explore.json]
"""
from __future__ import annotations

import argparse
import bisect
import gzip
import json
import os
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tools" / "studies"), str(ROOT / "tools"), str(ROOT)]
import late_gates_read as LR  # noqa: E402
import stale_age_read as SR  # noqa: E402
import std_scorer as S  # noqa: E402

OUT = Path(os.path.expanduser("~/replay_runs/slope_now"))
EXPLORE_FIRST, EXPLORE_LAST = "2026-09-28", "2026-10-13"
JOIN_SEC, STALE_SEC = 90.0, 12.0


def arms(path: Path) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    if path.exists():
        with gzip.open(path, "rt") as fh:
            for line in fh:
                r = json.loads(line)
                if r.get("arm_ok"):
                    out.setdefault(str(r.get("symbol") or "").upper(), []).append(r)
    for v in out.values():
        v.sort(key=lambda r: float(r.get("ts") or 0))
    return out


def join(trades: list[dict], arm_ok: dict[str, list[dict]]) -> list[dict]:
    out = []
    for t in trades:
        rows = arm_ok.get(str(t["symbol"]).upper()) or []
        ts = [float(r.get("ts") or 0) for r in rows]
        i = bisect.bisect_right(ts, t["entry_ts"]) - 1
        r = rows[i] if i >= 0 and t["entry_ts"] - ts[i] <= JOIN_SEC else None
        x = {**t, "joined": r is not None}
        if r:
            pts = r.get("pctr_pitch_ts")
            fresh = pts is not None and 0 <= float(r["ts"]) - float(pts) <= STALE_SEC
            for k in ("pctr_fast_pitch", "pctr_slow_pitch", "pctr_slow_pitch_prev"):
                x[k] = r.get(k) if fresh else None
            x["pctr"] = r.get("pctr")
        out.append(x)
    return out


def band(v, edges, fmt="{:+g}") -> str:
    if v is None:
        return "missing"
    for lo, hi in zip(edges, edges[1:]):
        if lo <= v < hi:
            return f"[{fmt.format(lo)},{fmt.format(hi)})"
    return f">={fmt.format(edges[-1])}" if v >= edges[-1] else f"<{fmt.format(edges[0])}"


def features(x: dict) -> dict[str, str]:
    fp, sp, spp = x.get("pctr_fast_pitch"), x.get("pctr_slow_pitch"), x.get("pctr_slow_pitch_prev")
    dsp = None if sp is None or spp is None else sp - spp
    gate = ("missing" if None in (fp, sp, spp) else
            "pass" if fp > 0 and sp > 0 and sp >= spp else "refuse")
    return {"slow_pitch": band(sp, [-2, 0, 2, 5]), "fast_pitch": band(fp, [-5, 0, 5, 15]),
            "slow_change": band(dsp, [-3, -1, 0, 1]), "pctr_level": band(x.get("pctr"), [-50, -35, -20, -10], "{:g}"),
            "gate_rule": gate}


def cell(rows: list[dict]) -> dict:
    by_day: dict[str, list[float]] = {}
    for r in rows:
        by_day.setdefault(r["day"], []).append(r["net"])
    st = S.day_stat([statistics.fmean(v) for v in by_day.values()]) if by_day else {"mean": None, "t": None}
    nets = [r["net"] for r in rows]
    return {"n": len(rows), "days": len(by_day), "mean_net": statistics.fmean(nets) if nets else None,
            "win": sum(n > 0 for n in nets) / len(nets) if nets else None, "day_mean": st.get("mean"), "t": st.get("t")}


def tables(rows: list[dict]) -> dict:
    out: dict = {"all": cell(rows)}
    feats = [features(r) for r in rows]
    for f in feats[0] if feats else []:
        groups: dict[str, list[dict]] = {}
        for r, ft in zip(rows, feats):
            groups.setdefault(ft[f], []).append(r)
        out[f] = {k: cell(v) for k, v in sorted(groups.items())}
    return out


def explore_days(out_dir: Path) -> list[str]:
    days = sorted({p.name[:10] for p in out_dir.glob("20??-??-??-REF.json")})
    return [d for d in days if EXPLORE_FIRST <= d <= EXPLORE_LAST]


def collect(out_dir: Path, pr, sip) -> tuple[list[dict], list]:
    rows, skipped = [], []
    for d in explore_days(out_dir):
        res = LR.load(d, "REF", out_dir)
        if res is None or d in sip.failed:
            skipped.append((d, "missing REF or SIP fetch failed"))
            continue
        tr, _ = SR.price_trades(res, d, pr, sip)
        for x in join(tr, arms(out_dir / f"{d}-REF-arms.jsonl.gz")):
            rows.append({**x, "day": d})
    return rows, skipped


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(OUT / "explore.json"))
    a = ap.parse_args()
    pr, sip = LR.Pricer.live(), SR.SipEntry(SR.SipEntry.live().fetch, OUT / "sip_entry_cache.json")
    try:
        rows, skipped = collect(OUT, pr, sip)
        res = {"window": [EXPLORE_FIRST, EXPLORE_LAST], "sessions": len({r["day"] for r in rows}),
               "skipped": skipped, "joined_share": (sum(r["joined"] for r in rows) / len(rows)) if rows else None,
               "tables": tables(rows), "note": "exploration only; confirmation sessions 10/14 onward are sealed"}
        Path(a.out).write_text(json.dumps(res, indent=1, default=str))
        print(json.dumps(res, indent=1, default=str))
        return 0
    finally:
        pr.save()
        sip.save()


if __name__ == "__main__":
    sys.exit(main())
