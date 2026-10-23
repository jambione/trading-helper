#!/usr/bin/env python3
"""Fibonacci golden-pocket pullback, practitioner method. Spec: docs/studies/fib_pullback_prereg.json (as amended).

Swing low L -> high H (>= 2%, H at or after 09:45); the first pullback bar whose low reaches the zone (close above the
break level) arms a 5-bar window for a bullish reaction bar (close > open, close > previous high, close > VWAP); entry at
the next open; stop on a close below the break level, target H (+$0.01 to fill), 30-minute time stop, 5 bp. Control: the
same % bracket at a seeded bullish, above-VWAP minute of the same name-day and clock hour.

  (default) count step: trades per variant and half, controls found, MDE from control minus a second control
  --score   verdict; refuses without --skeptic-ready and a count step from this code
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import statistics
import sys
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ROOT = os.environ.get("EEH_ROOT") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT_DIR = os.path.join(ROOT, "ai_reports", "fib_pullback")
FIRST, LAST, HALF_A_END = "2026-05-11", "2026-10-15", "2026-07-29"
SWING_MIN, H_FROM = 0.02, 9 * 60 + 45
DEC_LO, DEC_HI = 10 * 60, 15 * 60 + 15
WINDOW, TIME_STOP, GAP_MAX, CTRL_SEP = 5, 30, 5, 15
MIN_PX, INFO_PX, TICK = 10.0, 20.0, 0.01
COST, COSTS = 0.0005, (0.0003, 0.0005, 0.0010, 0.0020)
LIFT_BAR, T_PASS, T_SPEC = 0.0005, 2.0, 1.0
FLOOR_TRADES, FLOOR_DAYS = 300, 20
MAX_PER_DAY = 3
ZONES = {"GP": (0.5, 0.618), "SHALLOW": (0.236, 0.382), "DEEP": (0.70, 0.786)}
VARIANTS = ("GP", "SHALLOW", "DEEP", "NO_CONFIRM", "NO_VWAP", "RANDOM_DEPTH")


# ── the rule (pure) ──────────────────────────────────────────────────────────

def vwaps(bars: list) -> list:
    """Session VWAP through each bar (typical price x volume from the first bar)."""
    out, pv, v = [], 0.0, 0.0
    for b in bars:
        tp = (b[2] + b[3] + b[4]) / 3
        pv += tp * b[5]
        v += b[5]
        out.append(pv / v if v > 0 else b[4])
    return out


def bullish(bars: list, t: int) -> bool:
    return t >= 1 and bars[t][4] > bars[t][1] and bars[t][4] > bars[t - 1][2]


def zone_bounds(variant: str, depth_lo: float | None = None) -> tuple[float, float]:
    if variant in ("GP", "NO_CONFIRM", "NO_VWAP"):
        return ZONES["GP"]
    if variant == "RANDOM_DEPTH":
        return depth_lo, depth_lo + 0.118
    return ZONES[variant]


def detect(bars: list, variant: str = "GP", depth_lo: float | None = None) -> list[dict]:
    """bars: [(minute, o, h, l, c, v)] RTH, sorted. Signals per the amended prereg; one per swing (per L), <= 3/day."""
    zlo, zhi = zone_bounds(variant, depth_lo)
    vw = vwaps(bars)
    out, traded = [], set()
    L = H = None
    H_min = None
    phase, touch_t = "seek", None
    for t, b in enumerate(bars):
        m, o, h, lo, c = b[0], b[1], b[2], b[3], b[4]
        # decide on bar t with the swing built from bars < t
        if L is not None and H is not None and H / L - 1 >= SWING_MIN and H_min >= H_FROM and L not in traded:
            R = H - L
            upper, brk = H - zlo * R, (L if variant == "DEEP" else H - 0.786 * R)
            if phase == "seek" and h <= H and lo <= upper and c >= brk:
                phase, touch_t = "confirm", t
            if phase == "confirm":
                if c < brk:
                    phase = "broken"
                elif variant == "NO_CONFIRM":
                    if DEC_LO <= m <= DEC_HI and c > vw[t] and t + 1 < len(bars):
                        out.append(_signal(bars, t, H, L, brk, variant))
                        traded.add(L)
                    phase = "wait_high"
                elif t - touch_t < WINDOW:
                    ok = bullish(bars, t) and lo >= brk and (variant == "NO_VWAP" or c > vw[t])
                    if ok and DEC_LO <= m <= DEC_HI and t + 1 < len(bars):
                        out.append(_signal(bars, t, H, L, brk, variant))
                        traded.add(L)
                        phase = "wait_high"
                if phase == "confirm" and t - touch_t >= WINDOW - 1:
                    phase = "wait_high"
        # update the swing with bar t
        if L is None or lo < L:
            L, H, H_min, phase = lo, None, None, "seek"
        elif H is None or h > H:
            H, H_min = h, m
            if phase in ("broken", "wait_high", "confirm"):
                phase = "seek"
        if len(out) >= MAX_PER_DAY:
            break
    return out


def _signal(bars: list, t: int, H: float, L: float, brk: float, variant: str) -> dict:
    return {"dec": t, "dec_min": bars[t][0], "entry_i": t + 1, "H": H, "L": L, "brk": brk, "variant": variant}


def simulate(bars: list, e: int, stop_close_below: float, target: float) -> dict:
    """Entry at the open of bar e; checks from bar e; target needs high >= target + TICK (filled at target, or the open
    if it opens above); a close below the stop exits at the next bar's open; the target wins a bar that does both; time
    stop at the close of the last bar at or before entry + 29 min; a gap > GAP_MAX minutes exits at the next open."""
    entry = bars[e][1]
    end_min = bars[e][0] + TIME_STOP - 1
    k = e
    while k < len(bars):
        m, o, h, lo, c = bars[k][0], bars[k][1], bars[k][2], bars[k][3], bars[k][4]
        if m > end_min:
            return {"exit": bars[k - 1][4], "why": "time", "bars": k - e}
        if k > e and m - bars[k - 1][0] > GAP_MAX:
            return {"exit": o, "why": "gap", "bars": k - e}
        if h >= target + TICK:
            return {"exit": max(target, o) if k > e else target, "why": "target", "bars": k - e + 1}
        if c < stop_close_below:
            if k + 1 < len(bars):
                return {"exit": bars[k + 1][1], "why": "stop", "bars": k - e + 1}
            return {"exit": c, "why": "stop_eod", "bars": k - e + 1}
        k += 1
    return {"exit": bars[-1][4], "why": "eod", "bars": k - e}


def trade(bars: list, sig: dict) -> dict | None:
    e = sig["entry_i"]
    o = bars[e][1]
    if o >= sig["H"]:
        return {"skip": "open_at_or_above_H"}
    if o < MIN_PX:
        return {"skip": "below_10"}
    if bars[e][0] + TIME_STOP - 1 > 15 * 60 + 59:
        return {"skip": "no_room"}
    r = simulate(bars, e, sig["brk"], sig["H"])
    gross = r["exit"] / o - 1
    return {"entry_min": bars[e][0], "entry_px": o, "gross": gross, "net": gross - COST, "why": r["why"],
            "hold": r["bars"], "stop_pct": (o - sig["brk"]) / o, "target_pct": (sig["H"] - o) / o}


def control(bars: list, sig: dict, tr: dict, sym: str, day: str, tag: str) -> dict | None:
    """Seeded bullish, above-VWAP decision bar in the same clock hour, >= 15 min from the trade's decision bar, in the
    decision window with 30 minutes of room; the SAME % stop and % target and time stop."""
    vw = vwaps(bars)
    hr = sig["dec_min"] // 60
    cand = [t for t in range(1, len(bars) - 1)
            if bars[t][0] // 60 == hr and abs(bars[t][0] - sig["dec_min"]) >= CTRL_SEP and DEC_LO <= bars[t][0] <= DEC_HI
            and bullish(bars, t) and bars[t][4] > vw[t] and bars[t + 1][0] + TIME_STOP - 1 <= 15 * 60 + 59
            and bars[t + 1][1] >= MIN_PX]
    if not cand:
        return None
    t = random.Random(int(hashlib.sha256(f"{sym}|{day}|{sig['dec_min']}|{tag}".encode()).hexdigest()[:16], 16)).choice(cand)
    o = bars[t + 1][1]
    r = simulate(bars, t + 1, o * (1 - tr["stop_pct"]), o * (1 + tr["target_pct"]))
    g = r["exit"] / o - 1
    return {"gross": g, "net": g - COST, "why": r["why"]}


# ── day task (warehouse) ─────────────────────────────────────────────────────

def day_task(args) -> dict:
    day, days = args
    import indicator_confirm_hist as H_
    raw = H_.load_raw("sip", day)
    p_iex = H_.load_raw("iex", days[days.index(day) - 1])
    names = H_.sample_names(day, [s for s in H_.load_raw("iex", day) if s in raw and s in p_iex])
    rows, st = [], Counter()
    for sym in names:
        bars = [tuple(float(x) for x in r) for r in raw[sym]]
        bars = [(int(b[0]),) + b[1:] for b in bars]
        for v in VARIANTS:
            dl = None
            if v == "RANDOM_DEPTH":
                rng = random.Random(int(hashlib.sha256(f"{sym}|{day}|fib-depth".encode()).hexdigest()[:16], 16))
                choices = [x / 1000 for x in range(236, 786 - 118 + 1, 2) if x / 1000 + 0.118 <= 0.5 or x / 1000 >= 0.618]
                dl = rng.choice(choices)
            for sig in detect(bars, v, dl):
                tr = trade(bars, sig)
                if tr is None or "skip" in tr:
                    st[f"{v}_skip_{(tr or {}).get('skip')}"] += 1
                    continue
                c1 = control(bars, sig, tr, sym, day, "fib-gp-ctrl")
                c2 = control(bars, sig, tr, sym, day, "fib-gp-ctrl2")
                if c1 is None:
                    st[f"{v}_no_control"] += 1
                rows.append({"day": day, "sym": sym, "variant": v, "dec_min": sig["dec_min"], **tr,
                             "ctrl": c1["net"] if c1 else None, "ctrl2": c2["net"] if c2 else None,
                             "ctrl_gross": c1["gross"] if c1 else None, "swing": sig["H"] / sig["L"] - 1})
    return {"rows": rows, "stats": dict(st)}


# ── statistics (pure) ────────────────────────────────────────────────────────

def t_ppf(p: float, df: int) -> float:
    import indicator_confirm_study as S
    return S.t_ppf(p, df)


def day_stats(rows: list[dict], key) -> dict:
    per = defaultdict(list)
    for r in rows:
        v = key(r)
        if v is not None:
            per[r["day"]].append(v)
    xs = [statistics.mean(v) for v in per.values()]
    n = len(xs)
    if n < 2:
        return {"days": n, "mean": xs[0] if xs else None, "t": None, "ub": None, "n": sum(map(len, per.values()))}
    mu, se = statistics.mean(xs), statistics.stdev(xs) / math.sqrt(n)
    return {"days": n, "mean": mu, "t": mu / se if se else None, "ub": mu + t_ppf(0.975, n - 1) * se if se else None,
            "n": sum(map(len, per.values())), "means": dict(zip(per.keys(), xs))}


def diff(r: dict):
    return r["net"] - r["ctrl"] if r.get("ctrl") is not None else None


def no_overlap(rows: list[dict]) -> list[dict]:
    """At scoring only: no new entry in a name while its previous trade (same variant) is open."""
    out, busy = [], {}
    for r in sorted(rows, key=lambda x: (x["day"], x["sym"], x["entry_min"])):
        k = (r["day"], r["sym"])
        if busy.get(k, -1) >= r["entry_min"]:
            continue
        out.append(r)
        busy[k] = r["entry_min"] + r["hold"]
    return out


def spec_stats(gp: list[dict], other: list[dict]) -> dict:
    """Per day (GP - control) minus (OTHER - control), days with both; day-clustered."""
    a, b = day_stats(gp, diff).get("means", {}), day_stats(other, diff).get("means", {})
    xs = [a[d] - b[d] for d in a if d in b]
    if len(xs) < 2:
        return {"days": len(xs), "mean": None, "t": None}
    mu, se = statistics.mean(xs), statistics.stdev(xs) / math.sqrt(len(xs))
    return {"days": len(xs), "mean": mu, "t": mu / se if se else None}


def drop3(rows: list[dict], by: str) -> float | None:
    tot = Counter()
    for r in rows:
        d = diff(r)
        if d is not None:
            tot[r[by]] += d
    top = {x for x, _ in tot.most_common(3)}
    return day_stats([r for r in rows if r[by] not in top], diff)["mean"]


def half_verdict(gp: list[dict]) -> tuple[str, dict]:
    s = day_stats(gp, diff)
    net = day_stats(gp, lambda r: r["net"])["mean"]
    if s["t"] is not None and s["t"] <= -2:
        return "FAIL", s
    if s["n"] < FLOOR_TRADES or s["days"] < FLOOR_DAYS or s["t"] is None:
        return "UNDERPOWERED", s
    ok = (s["mean"] >= LIFT_BAR and s["t"] >= T_PASS and net is not None and net > 0
          and (drop3(gp, "day") or -1) >= 0 and (drop3(gp, "sym") or -1) >= 0)
    return ("PASS" if ok else "NOT_PASSED"), s


def final(halves: list[str], ubs: list, days: list[int]) -> str:
    if "FAIL" in halves:
        return "FAIL"
    if all(h == "PASS" for h in halves):
        return "PASS"
    if all(n >= 2 for n in days) and all(u is not None and u < LIFT_BAR for u in ubs):
        return "FAIL"
    return "UNDERPOWERED"


def module_shas() -> dict:
    return {"fib_pullback": hashlib.sha256(open(os.path.abspath(__file__), "rb").read()).hexdigest()}


def write(name: str, obj) -> str:
    os.makedirs(OUT_DIR, exist_ok=True)
    p = os.path.join(OUT_DIR, name)
    json.dump(obj, open(p + ".tmp", "w"), indent=1, default=str)
    os.replace(p + ".tmp", p)
    return p


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--score", action="store_true")
    ap.add_argument("--skeptic-ready", default="")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    import indicator_confirm_hist as H_
    days = H_.warehouse_days()
    run = [d for d in days if FIRST <= d <= LAST and days.index(d) >= 1]
    rows, st = [], Counter()
    cache = os.path.join(OUT_DIR, "rows.json")
    if os.path.exists(cache) and json.load(open(cache)).get("module_sha256") == module_shas():
        c = json.load(open(cache))
        rows, st = c["rows"], Counter(c["stats"])
    else:
        with ProcessPoolExecutor(max_workers=a.workers) as ex:
            for res in ex.map(day_task, [(d, days) for d in run], chunksize=2):
                rows += res["rows"]
                st.update(res["stats"])
        write("rows.json", {"rows": rows, "stats": dict(st), "module_sha256": module_shas()})
    by = defaultdict(list)
    for r in rows:
        by[(r["variant"], "A" if r["day"] <= HALF_A_END else "B")].append(r)
    if not a.score:
        cnt = {"stats": dict(st), "module_sha256": module_shas(),
               "prereg_commit": os.popen(f"git -C {ROOT} log -1 --format=%h -- docs/studies/fib_pullback_prereg.json").read().strip()}
        for (v, h), rs in sorted(by.items()):
            cd = defaultdict(list)
            for r in rs:
                if r.get("ctrl") is not None and r.get("ctrl2") is not None:
                    cd[r["day"]].append(r["ctrl"] - r["ctrl2"])
            xs = [statistics.mean(x) for x in cd.values()]
            sd = statistics.stdev(xs) if len(xs) >= 2 else None
            mde = (t_ppf(0.975, len(xs) - 1) + 0.84) * sd / math.sqrt(len(xs)) if sd else None
            cnt[f"{v} {h}"] = {"trades": len(rs), "days": len({r['day'] for r in rs}),
                               "with_control": sum(1 for r in rs if r.get("ctrl") is not None), "mde": mde,
                               "floors_met": len(rs) >= FLOOR_TRADES and len({r['day'] for r in rs}) >= FLOOR_DAYS}
        print(json.dumps(cnt, indent=1, default=str))
        print(write("count.json", cnt))
        return
    if not a.skeptic_ready:
        raise SystemExit("--score needs --skeptic-ready")
    cnt = json.load(open(os.path.join(OUT_DIR, "count.json")))
    if cnt.get("module_sha256") != module_shas():
        raise SystemExit("count.json from different code")
    res = {"halves": {}, "specificity": {}, "info": {}}
    hv, ubs, dys = [], [], []
    for h in ("A", "B"):
        gp = no_overlap(by[("GP", h)])
        v, s = half_verdict(gp)
        hv.append(v)
        ubs.append(s["ub"])
        dys.append(s["days"])
        res["halves"][h] = {"verdict": v, **{k: s[k] for k in ("mean", "t", "ub", "days", "n")},
                            "net_by_cost": {f"{int(cst * 1e4)}bp": day_stats(gp, lambda r, c=cst: r["gross"] - c)["mean"]
                                            for cst in COSTS},
                            "trades_before_overlap_rule": len(by[("GP", h)]), "trades": len(gp)}
        res["specificity"][h] = {o: spec_stats(gp, no_overlap(by[(o, h)])) for o in ("SHALLOW", "DEEP")}
        res["info"][h] = {v2: day_stats(no_overlap(by[(v2, h)]), diff)["mean"] for v2 in VARIANTS if v2 != "GP"}
        res["info"][h]["exit_mix"] = dict(Counter(r["why"] for r in gp))
        res["info"][h]["win_rate"] = sum(1 for r in gp if r["net"] > 0) / max(1, len(gp))
        res["info"][h]["price_ge_20"] = day_stats([r for r in gp if r["entry_px"] >= INFO_PX], diff)["mean"]
    res["verdict"] = final(hv, ubs, dys)
    spec = all(res["specificity"][h][o]["mean"] is not None and res["specificity"][h][o]["mean"] >= 0
               and (res["specificity"][h][o]["t"] or 0) >= T_SPEC for h in ("A", "B") for o in ("SHALLOW", "DEEP"))
    if res["verdict"] == "PASS":
        res["verdict"] = "PASS (Fibonacci-specific)" if spec else "PASS (pullback edge, not Fibonacci-specific)"
    res["skeptic_ready"], res["module_sha256"] = a.skeptic_ready, module_shas()
    write("result.json", res)
    print(json.dumps(res, indent=1, default=str))


if __name__ == "__main__":
    main()
