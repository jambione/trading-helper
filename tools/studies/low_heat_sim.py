#!/usr/bin/env python3
"""low_heat_sim - what a 3-slot desk would have done trading heat < 20 turns (operator 11/19 'can we run a simulation
on Low heat < 20 opens'). INFORMATION ONLY: runs after low_heat_rising is scored and cannot change its verdict.

Book: 3 slots, $3,200 per trade, one position per name, signals taken in time order when a slot is free; entry at the
open of t+1; cost = the real SIP spread at entry (fetched on demand, shared cache), spread > 15 bp skipped. Two exits:
the desk bracket (+20 / -50 bp within 15 min) and hold to 15:55 with a -3% stop. Baselines on the same book: today's
1-minute box (pre-square) signals and random minutes from low_heat_rising's events.

    .venv/bin/python tools/studies/low_heat_sim.py        # -> ai_reports/low_heat_sim/report.md (+ days.json)
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))

OUT = ROOT / "ai_reports" / "low_heat_sim"
SIMQ = OUT / "quotes_sim.json"
SLOTS, DOLLARS, GATE_BP, UP, DN, H = 3, 3200.0, 15.0, 20.0, 50.0, 15
DECIDE_END = "2026-09-23"


def bracket_exit(o, h, l, c, t):
    """(gross bp, exit minute) for +20/-50 within 15 min from open t+1; stop first on a two-touch minute. Pure."""
    if t + H >= 390:
        return None
    e = o[t + 1]
    if not (np.isfinite(e) and e > 0):
        return None
    up, dn = e * (1 + UP / 1e4), e * (1 - DN / 1e4)
    last, lm = e, t + 1
    for m in range(t + 1, t + H + 1):
        if np.isfinite(l[m]) and l[m] <= dn:
            return -DN, m
        if np.isfinite(h[m]) and h[m] >= up:
            return UP, m
        if np.isfinite(c[m]):
            last, lm = c[m], m
    return float((last / e - 1) * 1e4), t + H


def hold_exit(o, h, l, c, t, stop=0.03, t_out=385):
    if t + 1 > t_out:
        return None
    e = o[t + 1]
    if not (np.isfinite(e) and e > 0):
        return None
    sp, last = e * (1 - stop), e
    for m in range(t + 1, t_out + 1):
        if np.isfinite(l[m]) and l[m] <= sp:
            return float(((min(sp, o[m]) if np.isfinite(o[m]) else sp) / e - 1) * 1e4), m
        if np.isfinite(c[m]):
            last = c[m]
    return float((last / e - 1) * 1e4), t_out


def run_day(signals, exit_fn, bars, spread_fn):
    """signals: [(t, sym)] time-ordered -> trades [{sym, t, exit_m, net_bp, pnl}] on a SLOTS-slot book. Pure given fns."""
    open_until, trades = {}, []
    for t, sym in sorted(signals):
        for s in [s for s, m in open_until.items() if m <= t]:
            del open_until[s]
        if sym in open_until or len(open_until) >= SLOTS:
            continue
        b = bars.get(sym)
        if b is None:
            continue
        ex = exit_fn(*b, t)
        if ex is None:
            continue
        sp = spread_fn(sym, t)
        if sp is None or sp > GATE_BP:
            continue
        net = ex[0] - sp
        trades.append({"sym": sym, "t": t, "exit_m": ex[1], "net_bp": round(net, 2), "pnl": round(DOLLARS * net / 1e4, 2)})
        open_until[sym] = ex[1]
    return trades


def fetch_parallel(keys, n=4):
    """Fetch SIP spreads for [(day, sym, t)] with n workers into per-worker files, then merge into SIMQ."""
    from multiprocessing import Process
    import news_source as ns
    plan = [("lhs", d, s, t, 0.0) for d, s, t in keys]
    parts = [OUT / f"quotes_sim_part{i}.json" for i in range(n)]
    procs = [Process(target=ns.fetch_quotes, args=(plan[i::n],), kwargs={"path": parts[i]}) for i in range(n)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
    simq = json.loads(SIMQ.read_text()) if SIMQ.exists() else {}
    for p in parts:
        if p.exists():
            simq.update(json.loads(p.read_text()))
    SIMQ.write_text(json.dumps(simq))


def summary(days):
    pnl = np.array([d["pnl"] for d in days])
    if not len(pnl):
        return {}
    eq = np.cumsum(pnl)
    dd = float(np.max(np.maximum.accumulate(eq) - eq)) if len(eq) else 0.0
    nt = sum(d["n"] for d in days)
    wins = sum(d["wins"] for d in days)
    return {"days": len(days), "total_pnl": round(float(pnl.sum()), 2), "pnl_per_day": round(float(pnl.mean()), 2),
            "green_days_pct": round(100 * float((pnl > 0).mean()), 1), "max_drawdown": round(dd, 2),
            "opens_per_day": round(nt / len(days), 1), "win_pct": round(100 * wins / nt, 1) if nt else None,
            "net_bp_per_trade": round(float(sum(d["bp_sum"] for d in days) / nt), 2) if nt else None}


def main():
    from relative_strength_scan import load_day
    from evening_family import WH_SIP
    import news_source as ns
    ev = json.loads((ROOT / "ai_reports" / "low_heat_rising" / "events.json").read_text())
    arms = {"heat<20": [r for r in ev if r["arm"] == "low_heat" and r.get("heat") is not None and r["heat"] < 20],
            "open box (today)": [r for r in ev if r["arm"] == "presquare"],
            "random minute": [r for r in ev if r["arm"] == "random"]}
    OUT.mkdir(parents=True, exist_ok=True)
    files = {p.stem: p for p in WH_SIP.glob("20??-??-??.npz")}
    days = sorted({r["day"] for r in ev})
    for rnd in range(6):
        main_q = json.load(open(ns.QUOTES)) if ns.QUOTES.exists() else {}
        simq = json.loads(SIMQ.read_text()) if SIMQ.exists() else {}
        missing = set()

        def spread_fn_for(day):
            def f(sym, t):
                k = ns.qkey(day, sym, t)
                if k in main_q:
                    return main_q[k]
                if k in simq:
                    return simq[k]
                missing.add((day, sym, t))
                return 0.0          # provisional this round only; fetched and re-run before anything is reported
            return f
        res = defaultdict(list)
        for d in days:
            day = load_day(files[d])
            syms = {r["sym"] for v in arms.values() for r in v if r["day"] == d}
            bars = {s_: tuple(day[k][day["idx"][s_]] for k in "ohlc") for s_ in syms if s_ in day["idx"]}
            sf = spread_fn_for(d)
            for name, rows in arms.items():
                sig = [(r["t"], r["sym"]) for r in rows if r["day"] == d]
                for exname, fn in (("bracket", bracket_exit), ("hold", hold_exit)):
                    tr = run_day(sig, fn, bars, sf)
                    res[(name, exname)].append({"day": d, "n": len(tr), "pnl": round(sum(x["pnl"] for x in tr), 2),
                                                "wins": sum(x["net_bp"] > 0 for x in tr), "bp_sum": sum(x["net_bp"] for x in tr)})
        print("round", rnd, "missing quotes", len(missing), flush=True)
        if not missing:
            break
        fetch_parallel(sorted(missing))
    else:
        print("WARNING: quotes still missing after 6 rounds; those signals were priced at 0 spread", flush=True)
    out, L = {}, ["# Heat < 20 book simulation (information only)", "",
                  "3 slots, $3,200 per trade, real SIP spread at entry (> 15 bp skipped), one position per name.", "",
                  "| Signals | Exit | Period | $/day | total $ | green days % | opens/day | win % | net bp/trade | max drawdown $ |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
    for (name, exname), v in res.items():
        for per, sel in (("4/28-9/23", [x for x in v if x["day"] <= DECIDE_END]), ("9/24-11/18", [x for x in v if x["day"] > DECIDE_END])):
            s = summary(sel)
            out[f"{name}|{exname}|{per}"] = s
            if s:
                L.append(f"| {name} | {exname} | {per} | {s['pnl_per_day']} | {s['total_pnl']} | {s['green_days_pct']} | "
                         f"{s['opens_per_day']} | {s['win_pct']} | {s['net_bp_per_trade']} | {s['max_drawdown']} |")
    (OUT / "days.json").write_text(json.dumps({f"{k[0]}|{k[1]}": v for k, v in res.items()}))
    (OUT / "summary.json").write_text(json.dumps(out, indent=1))
    (OUT / "report.md").write_text("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
