#!/usr/bin/env python3
"""uptrend_pullback_hist.py — docs/studies/uptrend_pullback_prereg.json (amended, amended_2, ready).

The operator's pattern (10/30 TCOM chart): slow %R high (an uptrend) while the fast %R dips into oversold and turns up.
Warehouse IEX lines (fast 21 / EMA 7, slow 112 / EMA 3, previous session as warm-up), SIP markouts. FIRE at decision
minute t (09:45..15:20) when (1) slow(t) >= -20, (2) fast exists on every minute t-4..t and min <= -80, (3) fast(t) >
fast(t-1) + 0.05, (4) fast(t-1) <= -60; onset only, 15-minute refractory per name. Price filter: SIP close of t in
[$20, $1,000]; levered ETPs, split-jump days and bad series out.

gross_3 = SIP close t+3 / SIP open t+1 - 1. Controls (amended / amended_2): d = same name, seeded minute c in t+4..t+63
(open c+1 to close c+3); u = same name, an uptrend minute with no pullback nearby (u_exact); x = another eligible name at
the same minute (x_exact). Bounce check = close t+1 -> close t+4 minus the same for d. Net at 10 bp (PASS) / 5 bp
(PROMISING).

  count   events, days, no_u, no_price_t and the MDE of per-day lift_d per half (no means)
  score   refuses unless count is powered; the verdict and information lines
USAGE (mini, low priority; warehouse only, no Alpaca calls):
  nice -n 19 .venv/bin/python tools/studies/uptrend_pullback_hist.py count|score [--workers 2]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import pitch_open as P  # noqa: E402
import std_scorer as S  # noqa: E402

FIRST, LAST, HALF_A_END = "2026-05-11", "2026-10-15", "2026-07-29"
OUT = os.path.join(P.ROOT, "ai_reports", "uptrend_pullback")
PX_LO, PX_HI, EPS = 20.0, 1000.0, 0.05
LIFT_BAR, T_PASS, T_PROM, COST, COST_PROM = 5.0, 2.0, 1.5, 10.0, 5.0
MIN_EVENTS, MIN_DAYS, MDE_BAR, MAX_NO_U = 300, 30, 5.0, 0.20
# primary plus the information rules: (slow test, dip threshold)
RULES = {"primary": ("ge", -20.0, -80.0), "slow_ge_-30": ("ge", -30.0, -80.0), "slow_ge_-10": ("ge", -10.0, -80.0),
         "dip_-70": ("ge", -20.0, -70.0), "dip_-90": ("ge", -20.0, -90.0), "no_uptrend_slow_le_-50": ("le", -50.0, -80.0)}


def cond(fast: dict, slow: dict, m: int, rule=RULES["primary"]) -> bool:
    kind, slow_thr, dip = rule
    s = slow.get(m)
    if s is None or (s < slow_thr if kind == "ge" else s > slow_thr):
        return False
    win = [fast.get(k) for k in range(m - 4, m + 1)]
    if any(v is None for v in win) or min(win) > dip:
        return False
    return win[-1] > win[-2] + EPS and win[-2] <= -60.0


def g(sip: dict, a_min: int, a_idx: int, b_min: int, b_idx: int):
    """price ratio - 1 from sip[a_min][a_idx] to sip[b_min][b_idx] (idx 0 = open, 1 = close); None if missing."""
    a, b = sip.get(a_min), sip.get(b_min)
    if not a or not b or a[a_idx] <= 0 or b[b_idx] <= 0:
        return None
    return (b[b_idx] / a[a_idx] - 1) * 1e4


def g3(sip: dict, t: int):
    return g(sip, t + 1, 0, t + 3, 1)


def d_minute(sip: dict, sym: str, day: str, t: int):
    cand = [c for c in range(t + 4, t + 64) if c + 3 < P.RTH_END and g3(sip, c) is not None]
    return P.seeded("UTP-d", sym, day, t).choice(cand) if cand else None


def holding(fast: dict, slow: dict) -> set:
    """Every minute where the primary conditions (1)-(4) hold (not only onsets): u's 'no fire nearby' test."""
    return {m for m in range(P.DEC_LO - 15, P.DEC_HI + 16) if cond(fast, slow, m)}


def u_minute(fast: dict, slow: dict, sip: dict, sym: str, day: str, t: int, hold: set | None = None):
    """amended_2 u_exact."""
    hold = holding(fast, slow) if hold is None else hold
    cand = [m for m in range(P.DEC_LO, P.DEC_HI + 1)
            if (slow.get(m) is not None and slow[m] >= -20.0)
            and not any(k in hold for k in range(m - 15, m + 16))
            and not (t - 25 <= m <= t + 3)
            and g3(sip, m) is not None]
    if not cand:
        return None
    hour = (t + 1) // 60
    same = [m for m in cand if (m + 1) // 60 == hour]
    return P.seeded("UTP-u", sym, day, t).choice(same or cand)


def inverse_exit(fast: dict, sip: dict, t: int):
    """Operator's inverse exit (information): close of the first k >= t+2 with fast(k-1) >= -20 and a turn down."""
    for k in range(t + 2, min(t + 30, P.RTH_END - 1) + 1):
        a, b = fast.get(k - 1), fast.get(k)
        if a is not None and b is not None and a >= -20.0 and b < a - EPS:
            return g(sip, t + 1, 0, k, 1)
    return g(sip, t + 1, 0, min(t + 30, P.RTH_END - 1), 1)


def day_task(args) -> dict:
    day, prev = args
    import indicator_confirm_hist as H
    from ticker_filters import is_levered_etp
    lf_t, lf_p, sip_t, sip_p = H.load_raw("iex", day), H.load_raw("iex", prev), H.load_raw("sip", day), H.load_raw("sip", prev)
    st, lines, sips = Counter(), {}, {}
    for sym in sorted(lf_t):
        if sym not in lf_p or not P.series_ok(lf_t[sym]) or not P.series_ok(lf_p[sym]) or sym not in sip_t:
            st["bad_series"] += 1
            continue
        if is_levered_etp(sym):
            st["etp"] += 1
            continue
        if sym in sip_p and len(sip_p[sym]) and len(sip_t[sym]):
            pc, op = float(sip_p[sym][-1][4]), float(sip_t[sym][0][1])
            if pc > 0 and abs(op / pc - 1) > P.SPLIT_JUMP:
                st["split_jump"] += 1
                continue
        lines[sym] = P.lines(lf_p[sym], lf_t[sym])
        sips[sym] = {int(r[0]): (float(r[1]), float(r[4])) for r in sip_t[sym]}
    pool = sorted(lines)

    def eligible(sym, t):
        c = sips[sym].get(t)
        return c is not None and PX_LO <= c[1] <= PX_HI and g3(sips[sym], t) is not None

    events, holds = [], {}
    for name, rule in RULES.items():
        for sym in pool:
            fast, slow = lines[sym]
            sip = sips[sym]
            for t in P.fires(lambda m: cond(fast, slow, m, rule)):
                close_t = sip.get(t)
                if close_t is None:
                    st[f"{name}:no_price_t"] += 1
                    continue
                if not PX_LO <= close_t[1] <= PX_HI:
                    st[f"{name}:price_out"] += 1
                    continue
                gross = g3(sip, t)
                if gross is None:
                    st[f"{name}:no_markout"] += 1
                    continue
                ev = {"rule": name, "day": day, "sym": sym, "t": t, "gross3": gross,
                      "gross10": g(sip, t + 1, 0, t + 10, 1)}
                c = d_minute(sip, sym, day, t)
                ev["lift_d"] = gross - g3(sip, c) if c is not None else None
                if name == "primary":
                    if c is not None:
                        b_e, b_d = g(sip, t + 1, 1, t + 4, 1), g(sip, c + 1, 1, c + 4, 1)
                        ev["bounce"] = b_e - b_d if b_e is not None and b_d is not None else None
                    u = u_minute(fast, slow, sip, sym, day, t, holds.setdefault(sym, holding(fast, slow)))
                    ev["lift_u"] = gross - g3(sip, u) if u is not None else None
                    others = [s for s in pool if s != sym and eligible(s, t)]
                    x = P.seeded("UTP-x", sym, day, t).choice(others) if others else None
                    ev["lift_x"] = gross - g3(sips[x], t) if x is not None else None
                    ev["inverse_exit"] = inverse_exit(fast, sip, t)
                    ev["early"] = t <= 630
                events.append(ev)
    return {"events": events, "stats": dict(st)}


# ── statistics (pure) ───────────────────────────────────────────────────────────────────────────────────────────────
def half(day: str) -> str:
    return "A" if day <= HALF_A_END else "B"


def per_day(rows, f) -> list[float]:
    per: dict[str, list[float]] = {}
    for r in rows:
        v = f(r)
        if v is not None:
            per.setdefault(r["day"], []).append(v)
    return [statistics.fmean(v) for _, v in sorted(per.items())]


def stat(vals) -> dict:
    s = S.day_stat(vals)
    return {"days": s["n"], "mean": s["mean"], "t": s["t"]}


def mde(vals) -> float | None:
    return 2.84 * statistics.stdev(vals) / math.sqrt(len(vals)) if len(vals) >= 2 else None


def count_half(rows) -> dict:
    n = len(rows)
    ld = per_day(rows, lambda r: r.get("lift_d"))
    m = mde(ld)
    no_u = sum(r.get("lift_u") is None for r in rows)
    return {"events": n, "days": len({r["day"] for r in rows}), "no_u": no_u, "no_u_share": no_u / n if n else None,
            "mde": m, "powered": n >= MIN_EVENTS and len(ld) >= MIN_DAYS and m is not None and m <= MDE_BAR}


def verdict(rows) -> dict:
    halves = {}
    for h in ("A", "B"):
        hr = [r for r in rows if half(r["day"]) == h]
        c = count_half(hr)
        q = {k: stat(per_day(hr, lambda r, k=k: r.get(k))) for k in ("lift_d", "lift_x", "lift_u", "bounce")}
        q["net10"] = stat(per_day(hr, lambda r: r["gross3"] - COST))
        q["net5"] = stat(per_day(hr, lambda r: r["gross3"] - COST_PROM))
        ld, lt = q["lift_d"]["mean"], q["lift_d"]["t"]
        u_ok = c["no_u_share"] is not None and c["no_u_share"] <= MAX_NO_U
        pos = lambda k: q[k]["mean"] is not None and q[k]["mean"] > 0                       # noqa: E731
        h_pass = (c["powered"] and ld is not None and ld >= LIFT_BAR and (lt or 0) >= T_PASS and pos("lift_x")
                  and pos("lift_u") and u_ok and pos("net10") and pos("bounce") and q["bounce"]["mean"] >= ld / 2)
        h_prom = (ld is not None and ld > 0 and (lt or 0) >= T_PROM and pos("lift_x") and pos("lift_u")
                  and pos("net5") and pos("bounce"))
        halves[h] = {"count": c, "q": q, "pass": h_pass, "promising": h_prom, "u_ok": u_ok}
    if not all(halves[h]["count"]["powered"] for h in halves):
        label = "UNDERPOWERED"
    elif all(halves[h]["pass"] for h in halves):
        label = "PASS"
    elif all(halves[h]["promising"] for h in halves):
        label = "PROMISING"
    else:
        label = "FAIL"
    return {"label": label, "halves": halves}


def info(events) -> dict:
    out = {}
    for name in RULES:
        rs = [e for e in events if e["rule"] == name]
        out[name] = {h: {"events": sum(half(e["day"]) == h for e in rs),
                         "lift_d": stat(per_day([e for e in rs if half(e["day"]) == h], lambda r: r.get("lift_d")))["mean"],
                         "gross3": stat(per_day([e for e in rs if half(e["day"]) == h], lambda r: r["gross3"]))["mean"]}
                     for h in ("A", "B")}
    prim = [e for e in events if e["rule"] == "primary"]
    out["primary_extra"] = {
        "early_09_45_10_30_lift_d": stat(per_day([e for e in prim if e.get("early")], lambda r: r.get("lift_d")))["mean"],
        "rest_lift_d": stat(per_day([e for e in prim if not e.get("early")], lambda r: r.get("lift_d")))["mean"],
        "gross10": stat(per_day(prim, lambda r: r.get("gross10")))["mean"],
        "inverse_exit_gross": stat(per_day(prim, lambda r: r.get("inverse_exit")))["mean"],
        "fixed_3min_gross": stat(per_day(prim, lambda r: r["gross3"]))["mean"],
        "lift_d_on_u_scored": stat(per_day([e for e in prim if e.get("lift_u") is not None],
                                           lambda r: r.get("lift_d")))["mean"]}
    return out


def collect(workers: int):
    import indicator_confirm_hist as H
    days = H.warehouse_days()
    run = [(d, days[i - 1]) for i, d in enumerate(days) if FIRST <= d <= LAST and i >= 1]
    events, st = [], Counter()
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for i, res in enumerate(ex.map(day_task, run, chunksize=2), 1):
            events.extend(res["events"])
            st.update(res["stats"])
            print(f"session {i}/{len(run)}", file=sys.stderr, flush=True)
    return events, st, len(run)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=("count", "score"))
    ap.add_argument("--workers", type=int, default=2)
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    ev_p = os.path.join(OUT, "events.json")
    if a.mode == "count":
        events, st, n = collect(a.workers)
        json.dump(events, open(ev_p, "w"))
        prim = [e for e in events if e["rule"] == "primary"]
        cnt = {"sessions": n, "stats": dict(st), **{h: count_half([e for e in prim if half(e["day"]) == h])
                                                    for h in ("A", "B")}}
        cnt["powered"] = cnt["A"]["powered"] and cnt["B"]["powered"]
        json.dump(cnt, open(os.path.join(OUT, "count.json"), "w"), indent=1)
        print(json.dumps(cnt, indent=1))
        return 0
    cnt = json.load(open(os.path.join(OUT, "count.json")))
    if not cnt.get("powered"):
        print("UNDERPOWERED at the count step: no outcome is read", file=sys.stderr)
        return 2
    events = json.load(open(ev_p))
    prim = [e for e in events if e["rule"] == "primary"]
    res = {"verdict": verdict(prim), "info": info(events),
           "note": "warehouse IEX lines, SIP markouts, completed bars; information cells are not promotable"}
    json.dump(res, open(os.path.join(OUT, "result.json"), "w"), indent=1, default=str)
    print(json.dumps(res["verdict"], indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
