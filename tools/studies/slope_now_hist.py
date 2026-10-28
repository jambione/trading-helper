#!/usr/bin/env python3
"""slope_now_hist.py — historical-bar companion to slope_now_explore.py (operator 10/28: explore with history too).
EXPLORATION ONLY; nothing here passes. Sessions 2026-05-11..2026-10-13; 10/14 onward stays sealed for the
confirmation of the ONE rule the exploration nominates.

Events are pitch_open's empty-square fires (the desk's arm shape on completed IEX 1-minute lines, fast 21/EMA 7,
slow 112/EMA 3, the previous session as warm-up, 09:45-15:20 decisions, 15-minute refractory), entered at the next
minute's SIP open and held 10 minutes (SIP close). Pitch = line(t) - line(t-1) in points per minute; the slow pitch
a minute earlier = slow(t-1) - slow(t-2). Controls as pitch_open: d = the same name at a random later minute (within
60), x = another name at the same minute. Lifts are gross minus control, so spread cost cancels.

Tables per feature band (slow pitch, fast pitch, slow change, %R level, the gate's rule), each for half A
(05-11..07-29), half B (07-30..10-13) and both: n, days, mean gross, lift_d, lift_x (bp), day-clustered t.
A pattern worth pre-registering must show the same sign in both halves against both controls.
USAGE (mini, after the close; reads the local bar warehouse, no Alpaca calls):
  .venv/bin/python tools/studies/slope_now_hist.py [--workers 3]
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pitch_open as P  # noqa: E402
import std_scorer as S  # noqa: E402

FIRST, LAST, HALF_A_END = "2026-05-11", "2026-10-13", "2026-07-29"
OUT = os.path.join(P.ROOT, "ai_reports", "slope_now_hist")


def pitches(fast: dict, slow: dict, t: int):
    """(fast pitch, slow pitch, slow pitch a minute earlier) in points/min, None where a minute is missing."""
    def d(line, m):
        return round(line[m] - line[m - 1], 1) if m in line and (m - 1) in line else None
    return d(fast, t), d(slow, t), d(slow, t - 1)


def events(day: str, sym: str, fast: dict, slow: dict, sip: dict, sips: dict, pool: list, cache: dict) -> list[dict]:
    rows = []
    for t in P.fires(lambda m: P.empty_square(fast, slow, m)):
        e = t + 1
        if P.miss_reason(sip, e, P.HOLD):
            continue
        g = P.gross_at(sip, e, P.HOLD)
        dd = P.draw_d(sip, sym, day, e, P.HOLD, "sn-d")
        xx = P.draw_x(sips, pool, cache, sym, day, e, P.HOLD, "sn-x")
        if g is None or dd is None or xx is None:
            continue
        fp, sp, spp = pitches(fast, slow, t)
        rows.append({"day": day, "sym": sym, "t": t, "fp": fp, "sp": sp, "spp": spp,
                     "fast": round(fast[t], 1), "slow": round(slow[t], 1),
                     "gross": g * 1e4, "lift_d": (g - dd) * 1e4, "lift_x": (g - xx) * 1e4})
    return rows


def day_task(args) -> dict:
    day, prev_day = args
    import indicator_confirm_hist as H
    sip_raw, lf_today, lf_prev = H.load_raw("sip", day), H.load_raw("iex", day), H.load_raw("iex", prev_day)
    sip_prev = H.load_raw("sip", prev_day)
    sips = {s: {int(r[0]): (float(r[1]), float(r[4])) for r in rows} for s, rows in sip_raw.items()}
    pool, cache, st, out = sorted(sips), {}, Counter(), []
    for sym in pool:
        if sym not in lf_today or sym not in lf_prev:
            st["no_line_feed"] += 1
            continue
        if not P.series_ok(lf_today[sym]) or not P.series_ok(lf_prev[sym]):
            st["bad_line_series"] += 1
            continue
        if sym in sip_prev and len(sip_prev[sym]) and len(sip_raw[sym]):
            pc, op = float(sip_prev[sym][-1][4]), float(sip_raw[sym][0][1])
            if pc > 0 and abs(op / pc - 1) > P.SPLIT_JUMP:
                st["split_jump"] += 1
                continue
        fast, slow = P.lines(lf_prev[sym], lf_today[sym])
        out.extend(events(day, sym, fast, slow, sips[sym], sips, pool, cache))
    return {"rows": out, "stats": dict(st)}


def band(v, edges) -> str:
    if v is None:
        return "missing"
    if v < edges[0]:
        return f"<{edges[0]:+g}"
    for lo, hi in zip(edges, edges[1:]):
        if lo <= v < hi:
            return f"[{lo:+g},{hi:+g})"
    return f">={edges[-1]:+g}"


def features(r: dict) -> dict[str, str]:
    fp, sp, spp = r["fp"], r["sp"], r["spp"]
    dsp = None if sp is None or spp is None else sp - spp
    rule = ("missing" if None in (fp, sp, spp) else
            "pass" if fp > 0 and sp > 0 and sp >= spp else "refuse")
    return {"slow_pitch": band(sp, [-2, 0, 2, 5]), "fast_pitch": band(fp, [-5, 0, 5, 15]),
            "slow_change": band(dsp, [-3, -1, 0, 1]), "fast_level": band(r["fast"], [-35, -25, -20]),
            "gate_rule": rule}


def cell(rows: list[dict]) -> dict:
    out = {"n": len(rows), "days": len({r["day"] for r in rows})}
    for k in ("gross", "lift_d", "lift_x"):
        per: dict[str, list[float]] = {}
        for r in rows:
            per.setdefault(r["day"], []).append(r[k])
        st = S.day_stat([statistics.fmean(v) for v in per.values()])
        out[k] = {"mean": st["mean"], "t": st["t"]}
    return out


def tables(rows: list[dict]) -> dict:
    halves = {"A": [r for r in rows if r["day"] <= HALF_A_END], "B": [r for r in rows if r["day"] > HALF_A_END],
              "all": rows}
    out: dict = {h: {"all": cell(rs)} for h, rs in halves.items()}
    for h, rs in halves.items():
        feats = [features(r) for r in rs]
        for f in ("slow_pitch", "fast_pitch", "slow_change", "fast_level", "gate_rule"):
            groups: dict[str, list[dict]] = {}
            for r, ft in zip(rs, feats):
                groups.setdefault(ft[f], []).append(r)
            out[h][f] = {k: cell(v) for k, v in sorted(groups.items())}
    return out


def consistent(tab: dict) -> list[dict]:
    """Cells with the same sign of lift_d and lift_x in both halves (information; the shortlist to look at)."""
    out = []
    for f, cells in tab["all"].items():
        if f == "all":
            continue
        for k in cells:
            a, b = tab["A"].get(f, {}).get(k), tab["B"].get(f, {}).get(k)
            if not a or not b or min(a["n"], b["n"]) < 30:
                continue
            signs = {(x[c]["mean"] > 0) for x in (a, b) for c in ("lift_d", "lift_x")}
            if len(signs) == 1:
                out.append({"feature": f, "band": k, "sign": "+" if signs.pop() else "-",
                            "A": {c: a[c]["mean"] for c in ("lift_d", "lift_x")},
                            "B": {c: b[c]["mean"] for c in ("lift_d", "lift_x")}, "n": [a["n"], b["n"]]})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=3)
    a = ap.parse_args()
    import indicator_confirm_hist as H
    days = H.warehouse_days()
    run = [(d, days[i - 1]) for i, d in enumerate(days) if FIRST <= d <= LAST and i >= 1]
    rows, st = [], Counter()
    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        for i, res in enumerate(ex.map(day_task, run, chunksize=2), 1):
            rows.extend(res["rows"])
            st.update(res["stats"])
            print(f"session {i}/{len(run)}", file=sys.stderr, flush=True)
    tab = tables(rows)
    res = {"window": [FIRST, LAST], "sessions": len(run), "events": len(rows), "skips": dict(st), "tables": tab,
           "same_sign_both_halves": consistent(tab),
           "note": "exploration only; 10/14 onward sealed; lifts are gross minus control (cost cancels)"}
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "explore.json"), "w") as fh:
        json.dump(res, fh, indent=1, default=str)
    print(json.dumps({k: res[k] for k in ("sessions", "events", "same_sign_both_halves")}, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
