#!/usr/bin/env python3
"""Batch 2, warehouse tests T2-T7. Spec: docs/studies/batch2_prereg.json (common + tests), sharing wr_trend_trade's
desk-exact %R lines (IEX bars), SIP prices, controls and exit convention.

  T2 buy-stop at the oversold minute's high      T5 the trend trade with an EMA(3) fast line
  T3 bullish divergence (lower low, higher %R)   T6 selling-volume dry-up at the low, then a turn
  T4 %R deceleration in the bottom zone          T7 SPY leads a lagging high-correlation name (5-minute hold)

  --canary   plant +10 bp after each entry on real bars; every test must recover it (prereg 'canary')
  (default)  count step (outcome-blind)        --score  verdicts (refuses without READY, floors, same code)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import statistics
import sys
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wr_trend_trade as W  # noqa: E402

ROOT = W.ROOT
OUT = os.path.join(ROOT, "ai_reports", "batch2")
TESTS = ("T2", "T3", "T4", "T5", "T6", "T7")
SESSION_START = 570                  # episodes are maximal runs within the session (skeptic code r1 B2)
STEP = 0.001
T_BATCH = 2.69                       # 0.05/7 scored tests (T9/T10 deferred)
SPY_JUMP, CORR_MIN, CORR_LB, CORR_NEED, T7_HOLD = 0.001, 0.5, 20, 10, 5


# ── helpers ──────────────────────────────────────────────────────────────────

def real_minutes(fast: dict, lo: int, hi: int) -> list[int]:
    return [m for m in range(lo, hi + 1) if W.real(fast, m)]


def runs(fast: dict, slow: dict, lo: int, hi: int, both: bool) -> list[tuple[int, int, list[int]]]:
    """Maximal runs of REAL minutes meeting the condition (fast <= -80, or both lines <= -80); non-real minutes do
    not break a run. Returns (start, end, minutes)."""
    out, cur = [], []
    for m in real_minutes(fast, lo, hi):
        ok = fast[m] <= W.OS and (not both or slow[m] <= W.OS)
        if ok:
            cur.append(m)
        elif cur:
            out.append((cur[0], cur[-1], cur))
            cur = []
    if cur:
        out.append((cur[0], cur[-1], cur))
    return out


# ── signals (amended block): decisions only at real IEX minutes; conditions on IEX bars; fills on SIP ─────

def t2_step(fast, slow, iexf, sipf, t, state):
    """Buy-stop lifecycle (amended T2): trigger check first (SIP high >= stop + 1c in t, t in (arm, expiry]), then an
    oversold IEX minute in 09:45 .. 15:00 arms or lowers the stop (higher highs ignored)."""
    st = state.get("stop")
    if st is not None and state["arm"] < t <= state["expiry"] and t in sipf and sipf[t][1] >= st + 0.01:
        state["stop"] = None
        return t, max(st, sipf[t][0])
    if st is not None and t > state["expiry"]:
        state["stop"] = st = None
    if W.DEC_LO <= t <= W.DEC_HI and t >= state.get("free_from", 0) and W.is_oversold(fast, slow, t) and t in iexf:
        lvl = iexf[t][1] + 0.01
        if st is None or lvl < st:
            state.update(stop=lvl, arm=t, expiry=t + 10)
    return None


def sig_T3(fast, slow, iexf, t, state):
    if not (W.real(fast, t) and W.real(fast, t - 1)) or t not in iexf:
        return None
    if any(fast[m] <= W.OS for m in real_minutes(fast, t - 4, t)):
        return None                                      # an oversold run is in progress
    eps = [e for e in runs(fast, slow, SESSION_START, t - 5, both=False) if t - 30 <= e[1] <= t - 5]
    if not eps or eps[-1][0] < state.get("free_from", 0):
        return None
    _a, _b, ms = eps[-1]
    lows = [iexf[m][2] for m in ms if m in iexf]
    if lows and iexf[t][2] < min(lows) and fast[t] > min(fast[m] for m in ms) and fast[t] > fast[t - 1]:
        return t + 1
    return None


def sig_T4(fast, slow, iexf, t, state):
    if not all(W.real(fast, m) for m in (t, t - 3, t - 6)) or t not in iexf or fast[t] > W.OS:
        return None
    s1, s0 = fast[t] - fast[t - 3], fast[t - 3] - fast[t - 6]
    lows = [iexf[m][2] for m in range(t - 29, t + 1) if m in iexf]
    if s1 <= 0 and s1 > s0 + 5 and lows and iexf[t][3] <= min(lows) * 1.002 and t >= state.get("free_from", 0):
        return t + 1
    return None


def sig_T6(fast, slow, iexf, t, state):
    if not (W.real(fast, t) and W.real(fast, t - 1)) or fast[t] <= fast[t - 1]:
        return None
    if any(m not in iexf for m in range(t - 12, t + 1)):
        return None                                      # all 13 minutes must be real IEX bars
    eps = [e for e in runs(fast, slow, SESSION_START, t, both=True) if t - 20 <= e[1] <= t]
    if not eps or eps[-1][0] < state.get("free_from", 0):
        return None
    _a, b, ms = eps[-1]
    low_min = min(ms, key=lambda m: (iexf[m][2] if m in iexf else 1e18, m))
    if low_min < b - 4:
        return None
    v_now = statistics.mean(iexf[m][4] for m in range(t - 2, t + 1))
    v_ref = statistics.mean(iexf[m][4] for m in range(t - 12, t - 2))
    return t + 1 if v_ref > 0 and v_now < 0.5 * v_ref else None


# ── per name-day driver ──────────────────────────────────────────────────────

def run_rule(test, fast, slow, sipf, iexf=None, st=None):
    """Trades for one name-day: [(entry_min, entry_px, exit_min)] (exit_min = M_LAST + 1 means the close). The exit
    scan starts at the entry minute (u >= t+1, as wr_trend_trade amended (1a)); T2 starts after its fill minute."""
    iexf = iexf if iexf is not None else sipf
    st = st if st is not None else Counter()
    sip_oc = {m: (v[0], v[3]) for m, v in sipf.items()}
    out, state = [], {"free_from": 0}
    if test == "T5":
        for t in W.entries(fast, slow):
            tr = W.trade(sip_oc, t, W.exit_minute(fast, slow, t + 1))
            if tr:
                out.append((tr["entry_min"], sip_oc[tr["entry_min"]][0], tr["exit_min"]))
        return out
    last_t = W.DEC_HI + 10 if test == "T2" else W.DEC_HI
    t = W.DEC_LO - 30
    while t <= last_t:
        if test == "T2":
            hit = t2_step(fast, slow, iexf, sipf, t, state)
            e, px, start = (hit[0], hit[1], hit[0] + 1) if hit else (None, None, None)
        else:
            e = {"T3": sig_T3, "T4": sig_T4, "T6": sig_T6}[test](fast, slow, iexf, t, state) \
                if t >= W.DEC_LO else None
            if e is not None and e not in sipf:
                st[f"{test}_skip_no_entry_bar"] += 1
                e = None
            px, start = (sipf[e][0], e) if e is not None else (None, None)
        if e is None:
            t += 1
            continue
        u = W.exit_minute(fast, slow, start)
        x_min = W.M_LAST + 1 if (u is None or u + 1 > W.M_LAST) else u + 1
        out.append((e, px, x_min))
        state = {"free_from": x_min}
        t = x_min
    return out


def gross(sip_oc: dict, e: int, px: float, x_min: int) -> float | None:
    x = W.exit_px(sip_oc, x_min, e)
    return x / px - 1 if x else None


def planted(sipf: dict, from_min: int) -> dict:
    return {m: (tuple(v[i] * (1 + STEP) for i in range(4)) + (v[4],) if m >= from_min else v)
            for m, v in sipf.items()}


def spy_corr_table(days: list[str]) -> dict:
    """day -> {sym: corr of 1-minute SIP close-to-close returns with SPY that day} (pass 1; used from prior days)."""
    import indicator_confirm_hist as H
    out = {}
    for d in days:
        raw = H.load_raw("sip", d)
        if "SPY" not in raw:
            continue
        spy = {int(r[0]): float(r[4]) for r in raw["SPY"]}
        sr = {m: spy[m] / spy[m - 1] - 1 for m in spy if m - 1 in spy}
        row = {}
        for s, rows in raw.items():
            c = {int(r[0]): float(r[4]) for r in rows}
            pairs = [(c[m] / c[m - 1] - 1, sr[m]) for m in sr if m in c and m - 1 in c]
            if len(pairs) >= 100:
                a, b = np.array(pairs).T
                if a.std() > 0 and b.std() > 0:
                    row[s] = float(np.corrcoef(a, b)[0, 1])
        out[d] = row
    return out


def day_task(args) -> dict:
    day, prev_day, corr_prior, mode = args
    W.REAL.clear()
    import indicator_confirm_hist as H
    sip_raw, sip_prev = H.load_raw("sip", day), H.load_raw("sip", prev_day)
    iex, iex_prev = H.load_raw("iex", day), H.load_raw("iex", prev_day)
    sipfs = {s: {int(r[0]): (float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in rows}
             for s, rows in sip_raw.items()}
    sips = {s: {m: (v[0], v[3]) for m, v in f.items()} for s, f in sipfs.items()}
    pool = sorted(sips)
    rows, st = [], Counter()
    spy = sipfs.get("SPY", {})
    for sym in pool:
        if sym in sip_prev and len(sip_prev[sym]) and len(sip_raw[sym]):
            pc, op = float(sip_prev[sym][-1][4]), float(sip_raw[sym][0][1])
            if pc > 0 and abs(op / pc - 1) > W.SPLIT_JUMP:
                st["split_jump_skip"] += 1
                continue
        sipf = sipfs[sym]
        have_lines = sym in iex and sym in iex_prev
        if sym not in iex:
            st["no_iex_today"] += 1
        elif sym not in iex_prev:
            st["no_iex_previous_session"] += 1
        iexf = {int(r[0]): (float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in iex[sym]} \
            if sym in iex else {}
        lines7 = W.lines(iex_prev[sym], iex[sym]) if have_lines else ({}, {})
        if have_lines and not any(W.DEC_LO <= m <= W.DEC_HI for m in lines7[0]):
            st["no_warmup_lines"] += 1
            have_lines = False
        for test in TESTS:
            if test == "T7":
                if sym != "SPY" and sym not in corr_prior:
                    st["T7_warmup_excluded"] += 1
                trades = t7_trades(sym, sipf, spy, corr_prior)
            elif not have_lines:
                continue
            else:
                fast, slow = W.lines(iex_prev[sym], iex[sym], fast_span=3) if test == "T5" else lines7
                trades = run_rule(test, fast, slow, sipf, iexf, st)
            for e, px, x_min in trades:
                g = gross(sips[sym], e, px, x_min)
                if g is None:
                    st[f"{test}_no_exit_px"] += 1
                    continue
                hold = x_min - e
                row = {"test": test, "day": day, "sym": sym, "entry_min": e, "exit_min": x_min, "hold": hold,
                       "gross": g, "px": px}
                if mode == "canary":
                    pf = planted(sipf, e + 1)
                    pso = {m: (v[0], v[3]) for m, v in pf.items()}
                    row["gross_planted"] = gross(pso, e, px, x_min)
                    row["d_planted"] = W.d_control(pso, sym, day, e, hold, f"b2-{test}-d")
                row.update({"d": W.d_control(sips[sym], sym, day, e, hold, f"b2-{test}-d"),
                            "d2": W.d_control(sips[sym], sym, day, e, hold, f"b2-{test}-d2"),
                            "x": W.x_control(sips, pool, sym, day, e, x_min, f"b2-{test}-x"),
                            "x2": W.x_control(sips, pool, sym, day, e, x_min, f"b2-{test}-x2")})
                rows.append(row)
    return {"rows": rows, "stats": dict(st)}


def t7_trades(sym: str, sipf: dict, spy: dict, corr_prior: dict) -> list:
    if sym == "SPY" or not spy:
        return []
    cs = corr_prior.get(sym)
    if cs is None or cs < CORR_MIN:
        return []
    out, free_from = [], 0
    for t in range(W.DEC_LO, W.DEC_HI + 1):
        if t < free_from or not all(m in spy for m in (t, t - 1)) or not all(m in sipf for m in (t, t - 1)):
            continue
        if spy[t][3] / spy[t - 1][3] - 1 >= SPY_JUMP and sipf[t][3] / sipf[t - 1][3] - 1 <= 0:
            e = t + 1
            if e not in sipf:
                continue
            x_min = min(e + T7_HOLD, W.M_LAST + 1)
            out.append((e, sipf[e][0], x_min))
            free_from = x_min
    return out


# ── statistics ───────────────────────────────────────────────────────────────

def module_shas() -> dict:
    d = os.path.dirname(os.path.abspath(__file__))
    return {m: hashlib.sha256(open(os.path.join(d, f"{m}.py"), "rb").read()).hexdigest()
            for m in ("batch2", "wr_trend_trade", "eod_reversal", "indicator_confirm_hist")}


def write(name, obj):
    os.makedirs(OUT, exist_ok=True)
    p = os.path.join(OUT, name)
    json.dump(obj, open(p + ".tmp", "w"), indent=1, default=str)
    os.replace(p + ".tmp", p)
    return p


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--canary", action="store_true")
    ap.add_argument("--score", action="store_true")
    ap.add_argument("--skeptic-ready", default="")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--prereg-commit", default="")
    a = ap.parse_args()
    import indicator_confirm_hist as H
    days = H.warehouse_days()
    run_days = [d for d in days if W.FIRST <= d <= W.LAST]
    if a.canary:
        run_days = run_days[::11][:10]
    corr_path = os.path.join(OUT, "spy_corr.json")
    c0 = json.load(open(corr_path)) if os.path.exists(corr_path) else {}
    if c0.get("module_sha256") == module_shas():
        corr = c0["corr"]
    else:
        corr = spy_corr_table([d for d in days if d <= W.LAST])
        write("spy_corr.json", {"corr": corr, "module_sha256": module_shas()})

    def prior_corr(d):
        i = days.index(d)
        acc = defaultdict(list)
        for p in days[max(0, i - CORR_LB):i]:
            for s, v in corr.get(p, {}).items():
                acc[s].append(v)
        return {s: statistics.mean(v) for s, v in acc.items() if len(v) >= CORR_NEED}
    tasks = [(d, days[days.index(d) - 1], prior_corr(d), "canary" if a.canary else "run") for d in run_days]
    rows, st = [], Counter()
    cache = os.path.join(OUT, "rows.json")
    if not a.canary and os.path.exists(cache) and json.load(open(cache)).get("module_sha256") == module_shas():
        c = json.load(open(cache))
        rows, st = c["rows"], Counter(c["stats"])
    else:
        with ProcessPoolExecutor(max_workers=a.workers) as ex:
            for res in ex.map(day_task, tasks, chunksize=2):
                rows += res["rows"]
                st.update(res["stats"])
        if not a.canary:
            write("rows.json", {"rows": rows, "stats": dict(st), "module_sha256": module_shas()})
    by = defaultdict(list)
    for r in rows:
        by[r["test"]].append(r)
    if a.canary:
        out = {}
        for t in TESTS:
            g = [r["gross_planted"] - r["gross"] for r in by[t] if r.get("gross_planted") is not None]
            dd = [r["d_planted"] - r["d"] for r in by[t] if r.get("d_planted") is not None and r.get("d") is not None]
            out[t] = {"n": len(g), "trade_delta_bp": round(1e4 * statistics.mean(g), 2) if g else None,
                      "min_bp": round(1e4 * min(g), 2) if g else None, "max_bp": round(1e4 * max(g), 2) if g else None,
                      "d_control_delta_bp": round(1e4 * statistics.mean(dd), 2) if dd else None,
                      "pass": bool(g) and abs(statistics.mean(g) - STEP) <= 0.0001
                      and (not dd or abs(statistics.mean(dd)) < 0.00001)}
        print(json.dumps(out, indent=1))
        write("canary.json", {"canary": out, "module_sha256": module_shas()})
        return
    half = lambda r: "A" if r["day"] <= W.HALF_A_END else "B"  # noqa: E731
    if not a.score:
        cnt = {"stats": dict(st), "module_sha256": module_shas(), "prereg_commit": a.prereg_commit, "tests": {}}
        can = json.load(open(os.path.join(OUT, "canary.json"))) if os.path.exists(os.path.join(OUT, "canary.json")) \
            else {}
        cnt["canary_ok"] = can.get("module_sha256") == module_shas() and all(v["pass"] for v in can["canary"].values())
        for t in TESTS:
            cnt["tests"][t] = {}
            for h in ("A", "B"):
                hr = [r for r in by[t] if half(r) == h]
                holds = sorted(r["hold"] for r in hr)
                md, mx = W.mde(hr, "d", "d2"), W.mde(hr, "x", "x2")
                nd = len({r["day"] for r in hr})
                cnt["tests"][t][h] = {"trades": len(hr), "days": nd, "mde_d": md, "mde_x": mx,
                                      "hold_median": holds[len(holds) // 2] if holds else None,
                                      "floors_met": len(hr) >= W.FLOOR_TRADES and nd >= W.FLOOR_DAYS
                                      and md is not None and md <= W.LIFT_BAR and mx is not None and mx <= W.LIFT_BAR}
        print(json.dumps(cnt, indent=1, default=str))
        print(write("count.json", cnt))
        return
    if not a.skeptic_ready:
        raise SystemExit("--score needs --skeptic-ready")
    cnt = json.load(open(os.path.join(OUT, "count.json")))
    if cnt.get("module_sha256") != module_shas() or not cnt.get("prereg_commit") or not cnt.get("canary_ok"):
        raise SystemExit("count.json from different code, without --prereg-commit, or without a passing canary")
    res = {}
    for t in TESTS:
        if not all(cnt["tests"][t][h]["floors_met"] for h in ("A", "B")):
            res[t] = {"verdict": "UNDERPOWERED (count step; no outcome read)"}
            continue
        hv, ubs, halves = [], [], {}
        for h in ("A", "B"):
            hr = [r for r in by[t] if half(r) == h]
            ld, lx = W.dstat(hr, W.lift_d), W.dstat(hr, W.lift_x)
            net = W.dstat(hr, lambda r: r["gross"] - W.COST)
            if (ld["t"] or 0) <= -2 or (lx["t"] or 0) <= -2:
                v = "FAIL"
            elif (ld["mean"] >= W.LIFT_BAR and (ld["t"] or 0) >= 2 and lx["mean"] >= W.LIFT_BAR
                  and (lx["t"] or 0) >= 2 and net["mean"] > 0
                  and min(W.drop_days(hr, W.lift_d), W.drop_days(hr, W.lift_x)) >= 0
                  and min(W.drop_names(hr, W.lift_d), W.drop_names(hr, W.lift_x)) >= 0):
                v = "PASS"
            else:
                v = "NOT_PASSED"
            hv.append(v)
            ubs.append((ld["ub"], lx["ub"], net["ub"]))
            halves[h] = {"verdict": v, "lift_d": ld, "lift_x": lx, "net": net,
                         "net_by_cost": {f"{round(c * 1e4)}bp": W.dstat(hr, lambda r, c=c: r["gross"] - c)["mean"]
                                         for c in W.COSTS},
                         "batch_robust": min(ld["t"] or 0, lx["t"] or 0) >= T_BATCH,
                         "win_rate": sum(1 for r in hr if r["gross"] > W.COST) / max(1, len(hr)),
                         "hold_median": sorted(r["hold"] for r in hr)[len(hr) // 2] if hr else None}
        if "FAIL" in hv:
            verdict = "FAIL"
        elif all(x == "PASS" for x in hv):
            verdict = "PASS (batch-robust)" if all(halves[h]["batch_robust"] for h in ("A", "B")) else \
                "PASS (single-test), not batch-robust"
        elif any(all(u[i] is not None and u[i] < W.LIFT_BAR for u in ubs) for i in (0, 1)) or \
                all(u[2] is not None and u[2] < 0 for u in ubs):
            verdict = "FAIL"
        else:
            verdict = "UNDERPOWERED"
        res[t] = {"verdict": verdict, "halves": halves}
    res["declared"] = ["T6 episode = both lines <= -80; T3 episode = fast <= -80 only",
                       "T6 'last 5 minutes' = clock minutes b-4 .. b of the episode end b; ties go to the earliest minute",
                       "T4's 30-minute low uses the real IEX bars present in t-29 .. t",
                       "T2's stop level is set on IEX, triggered by SIP prints; T7 runs on SIP bars (an IEX rerun is "
                       "needed before any live relevance)", "episodes are maximal runs within the session"]
    res["skeptic_ready"], res["module_sha256"] = a.skeptic_ready, module_shas()
    write("result.json", res)
    print(json.dumps(res, indent=1, default=str))


if __name__ == "__main__":
    main()
