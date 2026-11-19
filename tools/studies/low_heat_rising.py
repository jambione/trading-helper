#!/usr/bin/env python3
"""low_heat_rising - buy an up-day name when its dashboard heat is low and turning up (operator 11/19).
Prereg: docs/studies/low_heat_rising_prereg.json.

    .venv/bin/python tools/studies/low_heat_rising.py events
    .venv/bin/python tools/studies/quotes_parallel.py low_heat_rising all 5      # after 16:05 only
    .venv/bin/python tools/studies/low_heat_rising.py score
"""
from __future__ import annotations

import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))

from opening_square import day_t, summ  # noqa: E402  (same scoring helpers)

OUT = ROOT / "ai_reports" / "low_heat_rising"
DECIDE = ("2026-04-28", "2026-09-23")
CONFIRM = ("2026-09-24", "2026-11-18")
T_LO, T_HI, REFRACT, HEAT_MAX = 15, 330, 30, 40.0
GATE_BP, CAP = 15.0, 4000
EDGE = {"desk": 2.0, "hold": 10.0}


def heat_turns(cc, hh, ll, off):
    """Today's bars where heat (100 + fast %R) < 40 and rising on t and t-1 but not t-2. Pure."""
    from bar_signal_family import wr_smoothed
    heat = 100.0 + wr_smoothed(hh, ll, cc, n=21, span=7)
    prev = np.concatenate(([np.nan], heat[:-1]))
    with np.errstate(invalid="ignore"):
        up = np.isfinite(heat) & np.isfinite(prev) & (heat - prev > 0.05)
    i = np.arange(off + 2, len(cc))
    hit = i[(heat[i] < HEAT_MAX) & up[i] & up[i - 1] & ~up[i - 2]] - off
    return [int(t) for t in hit if T_LO <= t <= T_HI], heat[off:]


def hold_exit(o, l, c, t, stop=0.03, t_out=385):
    """Entry open t+1; -3% stop (fill at the stop or a gapped-through open); else close t_out. Pure."""
    if t + 1 > t_out:
        return None
    e = o[t + 1]
    if not (np.isfinite(e) and e > 0):
        return None
    sp, last = e * (1 - stop), e
    for m in range(t + 1, t_out + 1):
        if np.isfinite(l[m]) and l[m] <= sp:
            return float(((min(sp, o[m]) if np.isfinite(o[m]) else sp) / e - 1) * 1e4)
        if np.isfinite(c[m]):
            last = c[m]
    return float((last / e - 1) * 1e4)


def events():
    from relative_strength_scan import load_day
    from board_structure_family import universe
    from evening_family import ALL_ETF, WH_SIP, minute_grid, presquare_bars, first_true
    from holistic_entry import bracket, COST
    from ticker_filters import is_levered_etp
    import news_source as ns
    rows, prev = [], None
    for fp in sorted(WH_SIP.glob("20??-??-??.npz")):
        day = load_day(fp)
        if prev is None or not (DECIDE[0] <= fp.stem <= CONFIRM[1]) or day["idx"].get("SPY") is None:
            prev = day
            continue
        for s in universe(day, prev, set(ALL_ETF)):
            if is_levered_etp(s):
                continue
            i, j = day["idx"][s], prev["idx"].get(s)
            if j is None:
                continue
            pc = ns.last_close(prev["c"][j])
            if not pc > 0:
                continue
            o, h, l, c = (day[k][i] for k in "ohlc")
            cc, hh, ll, off = minute_grid(prev["c"][j], prev["h"][j], prev["l"][j], c, h, l)

            def ok(t):
                return np.isfinite(c[t]) and 20 <= c[t] <= 400 and (c[t] / pc - 1) * 100 >= 1.5

            def add(arm, t, heat_t=None):
                b, hx = bracket(o, h, l, c, t), hold_exit(o, l, c, t)
                if b is None or hx is None:
                    return
                rows.append({"arm": arm, "day": fp.stem, "sym": s, "t": int(t), "desk": round(float(b) + COST, 2),
                             "hold": round(hx, 2), "heat": None if heat_t is None else round(float(heat_t), 1),
                             "day_pct": round(float((c[t] / pc - 1) * 100), 2)})
            turns, heat = heat_turns(cc, hh, ll, off)
            kept = first_true([t for t in turns if ok(t)], REFRACT)
            for t in kept:
                add("low_heat", t, heat[t])
            if kept:
                rt = T_LO + int(hashlib.sha256(f"lhr_r|{fp.stem}|{s}".encode()).hexdigest(), 16) % (T_HI - T_LO + 1)
                if ok(rt):
                    add("random", rt, heat[rt])
            for t in first_true([t for t in presquare_bars(cc, hh, ll, off, 21) if T_LO <= t <= T_HI and ok(t)], REFRACT):
                add("presquare", t, heat[t])
        print(fp.stem, len(rows), flush=True)
        prev = day
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "events.json").write_text(json.dumps(rows))


def sample(rows):
    """<= CAP per (arm, period) by the lowest sha256('lhr'|day|sym|t). Pure."""
    by = defaultdict(list)
    for r in rows:
        by[(r["arm"], r["day"] <= DECIDE[1])].append(r)
    out = []
    for v in by.values():
        out += sorted(v, key=lambda r: hashlib.sha256(f"lhr|{r['day']}|{r['sym']}|{r['t']}".encode()).hexdigest())[:CAP]
    return out


def plan(which="all"):
    rows = sample(json.loads((OUT / "events.json").read_text()))
    return sorted({("lhr", r["day"], r["sym"], r["t"], 0.0) for r in rows})


def score():
    import news_source as ns
    q = json.load(open(ns.QUOTES)) if ns.QUOTES.exists() else {}
    rows = sample(json.loads((OUT / "events.json").read_text()))
    out, cnt = {}, defaultdict(lambda: {"noquote": 0, "gated": 0})
    traded = defaultdict(list)
    for r in rows:
        per = "decide" if r["day"] <= DECIDE[1] else "confirm"
        sp = q.get(ns.qkey(r["day"], r["sym"], r["t"]))
        if sp is None:
            cnt[(r["arm"], per)]["noquote"] += 1
            continue
        if sp > GATE_BP:
            cnt[(r["arm"], per)]["gated"] += 1
            continue
        for ex in ("desk", "hold"):
            traded[(r["arm"], ex, per)].append({**r, "net": r[ex] - sp})
        if r["arm"] == "low_heat":
            band = "heat_lt25" if (r["heat"] or 0) < 25 else "heat_25_40"
            mv = "day_1.5_3" if r["day_pct"] <= 3 else "day_gt3"
            tags = [band, mv] + (["heat_lt20"] if (r["heat"] if r["heat"] is not None else 99) < 20 else [])
            for tag in tags:
                for ex in ("desk", "hold"):
                    traded[(f"low_heat|{tag}", ex, per)].append({**r, "net": r[ex] - sp})
    for k, v in traded.items():
        out["|".join(k)] = summ(v)
    for k, v in cnt.items():
        out["counts|" + "|".join(k)] = v
    verdicts = {}
    for ex in ("desk", "hold"):
        d, cf = out.get(f"low_heat|{ex}|decide", {}), out.get(f"low_heat|{ex}|confirm", {})
        ok = (d.get("n", 0) >= 300 and (d.get("mean_net") or -99) >= EDGE[ex] and (d.get("t_day") or -99) >= 2.5
              and (d.get("half1") or -1) > 0 and (d.get("half2") or -1) > 0 and (cf.get("mean_net") or -1) > 0)
        verdicts[ex] = ["PASS" if ok else "FAIL", f"net {d.get('mean_net')} (need +{EDGE[ex]}) t {d.get('t_day')} halves "
                        f"{d.get('half1')}/{d.get('half2')} n {d.get('n')} confirm {cf.get('mean_net')}"]
        r_ = out.get(f"random|{ex}|decide", {})
        if r_.get("n"):
            verdicts[ex].append(f"lift vs random {round((d.get('mean_net') or 0) - r_['mean_net'], 2)}")
    for ex in ("desk", "hold"):                      # amended_1: heat < 20 cells, t >= 2.7
        d, cf = out.get(f"low_heat|heat_lt20|{ex}|decide", {}), out.get(f"low_heat|heat_lt20|{ex}|confirm", {})
        ok = (d.get("n", 0) >= 300 and (d.get("mean_net") or -99) >= EDGE[ex] and (d.get("t_day") or -99) >= 2.7
              and (d.get("half1") or -1) > 0 and (d.get("half2") or -1) > 0 and (cf.get("mean_net") or -1) > 0)
        verdicts[f"heat_lt20|{ex}"] = ["PASS" if ok else "FAIL", f"net {d.get('mean_net')} (need +{EDGE[ex]}) t {d.get('t_day')} "
                                       f"(need 2.7) halves {d.get('half1')}/{d.get('half2')} n {d.get('n')} confirm {cf.get('mean_net')}"]
    out["VERDICT"] = verdicts
    (OUT / "score.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(verdicts, indent=1))
    return out


if __name__ == "__main__":
    {"events": events, "score": score}[sys.argv[1]]()
