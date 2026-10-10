#!/usr/bin/env python3
"""supertrend_forward_read.py — docs/studies/supertrend_exit_forward_prereg.json. Scored with tools/studies/std_scorer.py.

Reads the nightly replay files /tmp/tt_run/DAY-{np_lob,np_lob_st,np_lob_nodecay}.json for sessions from 2026-10-12, keeps TIGHT trades
(first admit_range row per name-day has source 'tight' and ts <= entry_ts), costs each trade with the full SIP spread at entry (no NBBO:
one pooled median across the variants for that session, counted), runs the vacuity check, and prints the paired / no-harm / attribution
legs. Before 35 scored sessions it prints counts and direction only and NO verdict.
USAGE (mini, repo root, after the nightly replay): .venv/bin/python tools/studies/supertrend_forward_read.py
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import statistics
import sys

ROOT = os.getcwd()
sys.path[:0] = [os.path.join(ROOT, "tools", "studies"), os.path.join(ROOT, "tools"), ROOT]
import replay_costing as RC  # noqa: E402
import std_scorer as S  # noqa: E402

FROM, N_READ, N_MAX = "2026-10-12", 45, 60
VARS = ("np_lob", "np_lob_st", "np_lob_nodecay")


def tight_first_rows(days):
    first = {}
    for line in open(os.path.join(ROOT, "ai_reports", "admit_range.jsonl")):
        try:
            r = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        d, s = r.get("day"), r.get("symbol")
        if d in days and s and r.get("ts") is not None:
            k = (d, s)
            if k not in first or float(r["ts"]) < first[k][0]:
                first[k] = (float(r["ts"]), r.get("source"))
    return first


def desk_sessions(start):
    """Desk sessions = days with a recording, counted against N_MAX whether or not a replay exists."""
    return sorted(os.path.basename(p) for p in glob.glob(os.path.join(ROOT, "ai_reports", "sessions", "20??-??-??"))
                  if os.path.basename(p) >= start)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--restart-from", default=FROM,
                    help="CONFIG RULE: the first session after a live change to the np180 / lob / leash / SuperTrend knobs")
    a = ap.parse_args()
    start = max(FROM, a.restart_from)
    days = sorted({os.path.basename(p)[:10] for p in glob.glob("/tmp/tt_run/20??-??-??-np_lob.json")
                   if os.path.basename(p)[:10] >= start})
    first = tight_first_rows(set(days))
    cache, box = RC.load_cache(), {}
    counts = {"excluded_not_tight_at_entry": {v: 0 for v in VARS}, "no_nbbo_pooled_median": 0, "vacuous_sessions": [],
              "no_quote_sessions": []}
    trades = {v: [] for v in VARS}
    scored = []
    for d in days:
        raw = {}
        for v in VARS:
            p = f"/tmp/tt_run/{d}-{v}.json"
            raw[v] = json.load(open(p)) if os.path.exists(p) else None
        st_closed = (raw["np_lob_st"] or {}).get("closed") or []
        ref_closed = (raw["np_lob"] or {}).get("closed") or []
        if (raw["np_lob"] is None or raw["np_lob_st"] is None or raw["np_lob_nodecay"] is None
                or not any(t.get("reason") == "supertrend" for t in st_closed)
                or [(t["symbol"], t["entry_ts"], t.get("exit_ts")) for t in st_closed]
                == [(t["symbol"], t["entry_ts"], t.get("exit_ts")) for t in ref_closed]):
            counts["vacuous_sessions"].append(d)
            continue
        scored.append(d)
        counts.setdefault("sessions", []).append({"day": d, "sha": ((raw["np_lob_st"] or {}).get("sha") or "")[:8],
                                                  "overrides": (raw["np_lob_st"] or {}).get("overrides")})
        day_rows = {v: [] for v in VARS}
        spreads = []
        for v in VARS:
            for t in (raw[v] or {}).get("closed") or []:
                if t.get("ret") is None:
                    continue
                f = first.get((d, t["symbol"]))
                if not f or f[1] != "tight" or f[0] > float(t["entry_ts"]):
                    counts["excluded_not_tight_at_entry"][v] += 1
                    continue
                spr = RC.entry_spread_bp(cache, t["symbol"], t["entry_ts"], box)
                if spr is not None:
                    spreads.append(spr)
                day_rows[v].append((t, spr))
        if not spreads:
            counts["no_quote_sessions"].append(d)
            scored.remove(d)
            counts["vacuous_sessions"].append(d)
            continue
        med = statistics.median(spreads)
        for v in VARS:
            for t, spr in day_rows[v]:
                if spr is None:
                    counts["no_nbbo_pooled_median"] += 1
                    spr = med
                trades[v].append({"day": d, "symbol": t["symbol"], "entry_ts": float(t["entry_ts"]),
                                  "net_bp": t["ret"] * 1e4 - spr, "reason": t.get("reason"),
                                  "hold": (t.get("exit_ts") or t["entry_ts"]) - t["entry_ts"]})
    RC.save_cache(cache)
    main_leg = S.paired_variants(trades["np_lob"], trades["np_lob_st"], scored)
    attrib = S.paired_variants(trades["np_lob_nodecay"], trades["np_lob_st"], scored)
    nodecay = S.paired_variants(trades["np_lob"], trades["np_lob_nodecay"], scored)
    n = len(scored)
    n_desk = len(desk_sessions(start))
    print(f"SuperTrend exit forward read from {start}: scored sessions {n} (read at {N_READ}); desk sessions {n_desk} (max {N_MAX}); "
          f"vacuous {len(counts['vacuous_sessions'])}; {counts}")
    print(f"  trades: " + ", ".join(f"{v} {len(trades[v])}" for v in VARS) +
          f" | matched st/np_lob {main_leg['matched']} (unmatched st {main_leg['unmatched_var']}, np_lob {main_leg['unmatched_ref']}, "
          f"ambiguous {main_leg['ambiguous']}; attribution leg matched {attrib['matched']}, ambiguous {attrib['ambiguous']})")
    early = sum(1 for t in trades["np_lob_st"] if t["reason"] == "supertrend" and t["hold"] <= 60)
    print(f"  SuperTrend exits within 60 s of entry: {early}")
    if n < N_READ and n_desk >= N_MAX:
        print(f"  VERDICT: UNDERPOWERED = FAIL ({n} scored sessions by {n_desk} desk sessions)")
        return
    if n < N_READ:
        print(f"  direction only (no verdict before {N_READ} sessions): paired st - np_lob {main_leg['paired']['mean']:+.2f} bp over "
              f"{main_leg['paired']['n']} sessions")
        return
    pd_ = [main_leg["paired_days"][d] for d in scored if d in main_leg["paired_days"]]
    halves = [S.day_stat(pd_[0::2]), S.day_stat(pd_[1::2])]
    m, t = main_leg["paired"]["mean"], main_leg["paired"]["t"]
    ok = (m >= 3 and t >= 2 and all(h["mean"] > 0 for h in halves) and main_leg["dollars"]["mean"] >= 0
          and attrib["paired"]["mean"] > 0)
    print(f"  PRIMARY paired st - np_lob {m:+.2f} bp (t {t:+.2f}); halves {halves[0]['mean']:+.2f} / {halves[1]['mean']:+.2f}; "
          f"no-harm dollars/session {main_leg['dollars']['mean']:+.2f}; st - nodecay {attrib['paired']['mean']:+.2f}")
    print(f"  information: nodecay - np_lob {nodecay['paired']['mean']:+.2f}; drop best session "
          f"{S.day_stat(sorted(pd_)[:-1])['mean']:+.2f}; negative sessions {sum(x < 0 for x in pd_)}/{len(pd_)}")
    print(f"  VERDICT: {'PASS (skeptic review next; then a paper A/B on tight names)' if ok else 'FAIL'}")


if __name__ == "__main__":
    main()
