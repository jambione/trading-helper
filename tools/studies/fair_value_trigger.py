#!/usr/bin/env python3
"""Fair-value cheapness as the entry TRIGGER. Spec: docs/studies/fair_value_trigger_prereg.json (as amended).

Buy the minute an admitted name turns CHEAP versus its fair value (Z crosses below -1; Z = the fair_value_timing model,
unchanged), hold 3 bars, and compare with the same name's later minute (d) and a random other eligible name at the same
minute (x). P1 = liquid board names (arm rows 2026-09-08 .. 10-15, SIP spread <= 0.05% at T); P2 = the 400-name
warehouse sample over 110 sessions.

  --fidelity   the per-minute Z equals fair_value_timing.day_task's z at every row of ai_reports/fair_value/features.json
  --triggers   onsets on every minute, refractory, then eligibility -> triggers.json (no outcome)
  --fetch-p1   SIP trades [T - 60 s, T) and quotes [T - 120 s, T + 1 s] for every P1 trigger (data account)
  (default)    count step (spread cap on P1, floors, x availability, projected MDE from control returns) -> count.json
  --score      verdicts; refuses without --skeptic-ready and matching module shas
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import pickle
import random
import statistics
import sys
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fair_value_timing as FV  # noqa: E402
import flow_resting_entry as FR  # noqa: E402
import three_minute_reread as T3  # noqa: E402

O, S, E, H = FV.O, FV.S, FV.E, FV.H
OUT_DIR = os.path.join(E.ROOT, "ai_reports", "fv_trigger")
P1_TICKS = os.path.join(OUT_DIR, "p1_ticks")
P1_FIRST, P1_LAST, P1_HALF_A_END = "2026-09-08", "2026-10-15", "2026-09-25"
M_LO, M_HI, REFRACT = 600, 927, 15
HOLD, COST = 3, 0.0005
SAME_TAG, CROSS_TAG, PLACEBO_TAG = "fv-trigger-v1", "fv-trigger-cross", "fv-trigger-placebo"
FLOORS = {"P1": (300, 10), "P2": (1000, 20)}
X_MISSING_MAX = 0.20
N_TESTS = 2
LIFT_BAR = 0.0005


# ── the model, per minute ────────────────────────────────────────────────────

def z_series_day(args) -> tuple[str, dict, dict]:
    """(day, {sym: {minute: z}} for minutes M_LO - 1 .. M_HI, {sym: reason}) - fair_value_timing.day_task's model,
    computed for every minute instead of only entry minutes."""
    day, days, syms, spy = args
    k = days.index(day)
    prior = days[k - FV.LOOKBACK:k]
    if len(prior) < FV.LOOKBACK:
        return day, {}, {s: "short_lookback" for s in syms}
    raws = [H.load_raw("sip", d) for d in prior]
    today = H.load_raw("sip", day)
    names = sorted(set.intersection(*[set(r) for r in raws]) & set(today))
    if not names:
        return day, {}, {s: "coverage" for s in syms}
    Xp = np.column_stack([np.concatenate([FV.minute_returns(r[s]) for r in raws]) for s in names])
    Xt = np.column_stack([FV.minute_returns(today[s]) for s in names])
    spy_p = np.concatenate([FV.spy_returns(spy.get(d, {})) for d in prior])
    spy_t = FV.spy_returns(spy.get(day, {}))
    cov_ok = np.array([FV.coverage(Xp[:, j]) >= FV.COVER for j in range(len(names))])
    col = {s: j for j, s in enumerate(names)}
    cand = [j for j in range(len(names)) if cov_ok[j]]
    sub = Xp[:, cand]
    out, why = {}, {}
    for s in syms:
        j = col.get(s)
        if j is None or not cov_ok[j]:
            why[s] = "coverage"
            continue
        ranked = FV.top_peers(sub, cand.index(j), max((FV.K,) + FV.K_INFO))
        peers = [cand[r] for r in ranked[:FV.K]]
        bp, bt = FV.basket(Xp, peers), FV.basket(Xt, peers)
        be = FV.fit_betas(Xp[:, j], spy_p, bp)
        if be is None:
            why[s] = "fit"
            continue
        rp = Xp[:, j] - be[0] * spy_p - be[1] * bp
        rt = Xt[:, j] - be[0] * spy_t - be[1] * bt
        sd = FV.block_sd(rp, FV.WIN)
        out[s] = {m: FV.z_at(rt, m, sd) for m in range(M_LO - 1, M_HI + 1)}
        why[s] = "ok"
    return day, out, why


def onsets(zs: dict, lo: int = M_LO, hi: int = M_HI, cut: float = FV.Z_CHEAP, below: bool = True) -> list[int]:
    """Minutes m in [lo, hi] where Z crosses the cut (Z(m) <= cut < Z(m-1), or >= for RICH), 15-minute refractory,
    computed on ALL minutes (amended (1))."""
    out, last = [], None
    for m in range(lo, hi + 1):
        z, zp = zs.get(m), zs.get(m - 1)
        if z is None or zp is None:
            continue
        hit = (z <= cut < zp) if below else (z >= cut > zp)
        if hit and (last is None or m - last >= REFRACT):
            out.append(m)
            last = m
    return out


# ── eligibility, controls, outcomes ──────────────────────────────────────────

def board_minutes(day: str) -> dict[str, set]:
    """sym -> ET minutes with an 'arm' row (any arm_ok) - amended (1)."""
    out = defaultdict(set)
    for r in S.ledger_rows(day):
        if r.get("stage") == "arm" and r.get("symbol") and r.get("ts") is not None:
            out[str(r["symbol"]).upper()].add(E._et_min(float(r["ts"])))
    return out


def control_minute(sym: str, day: str, m: int, sip: dict) -> int | None:
    elig = [c for c in range(m + 1, m + S.CTRL_SPAN + 1) if T3._ok(sip, c, HOLD)]
    if not elig:
        return None
    return random.Random(H._seed(sym, day, m, SAME_TAG)).choice(elig)


def cross_name(sym: str, day: str, m: int, pool: list[str], sipm) -> str | None:
    if not pool:
        return None
    rng = random.Random(H._seed(sym, day, m, CROSS_TAG))
    ok = [n for n in pool if n != sym and T3._ok(sipm(n), m, HOLD)]
    return rng.choice(sorted(ok)) if ok else None


def outcome(sipm_sym: dict, m: int, c: int | None, xm: dict | None) -> dict:
    o = {"arm": T3.net(sipm_sym, m, hold=HOLD, cost=COST), "ctrl": T3.net(sipm_sym, c, hold=HOLD, cost=COST) if c else None,
         "cross": T3.net(xm, m, hold=HOLD, cost=COST) if xm is not None else None,
         "arm_lag1": T3.net(sipm_sym, m, hold=HOLD, lag=1, cost=COST),
         "ctrl_lag1": T3.net(sipm_sym, c, hold=HOLD, lag=1, cost=COST) if c else None}
    return o


def half_of(pop: str, day: str) -> str:
    return ("A" if day <= P1_HALF_A_END else "B") if pop == "P1" else H.half_of(day)


# ── statistics ───────────────────────────────────────────────────────────────

def t_crit2(days: int) -> float | None:
    return S.t_ppf(1 - 0.025 / N_TESTS, days - 1) if days >= 2 else None


def lift(rows: list[dict], a: str, c: str) -> tuple:
    per = defaultdict(list)
    for r in rows:
        if r["o"].get(a) is not None and r["o"].get(c) is not None:
            per[r["day"]].append(r["o"][a] - r["o"][c])
    means = {d: statistics.mean(v) for d, v in per.items() if v}
    mu, t, n = O._day_t(means)
    xs = list(means.values())
    se = statistics.stdev(xs) / math.sqrt(len(xs)) if len(xs) >= 2 else None
    ub = mu + S.t_ppf(0.975, n - 1) * se if (mu is not None and se) else None
    return mu, t, n, ub, means, sum(len(v) for v in per.values())


def grade(rows: list[dict]) -> dict:
    d_mu, d_t, n_days, d_ub, d_means, n = lift(rows, "arm", "ctrl")
    x_mu, x_t, x_days, x_ub, _, nx = lift(rows, "arm", "cross")
    net = O._day_t({d: statistics.mean(v) for d, v in _by_day(rows, "arm").items()})[0]
    nd = defaultdict(list)
    for r in rows:
        if r["o"]["arm"] is not None and r["o"]["ctrl"] is not None:
            nd[(r["day"], r["sym"])].append(r["o"]["arm"] - r["o"]["ctrl"])
    nameday = statistics.mean(statistics.mean(v) for v in nd.values()) if nd else None
    share = Counter()
    for r in rows:
        if r["o"]["arm"] is not None and r["o"]["ctrl"] is not None:
            share[r["sym"]] += (r["o"]["arm"] - r["o"]["ctrl"]) / max(1, n)
    top_names = {s for s, _ in share.most_common(3)}
    top_days = set(sorted(d_means, key=lambda d: d_means[d], reverse=True)[:3])
    return {"n": n, "days": n_days, "lift_d": d_mu, "t_d": d_t, "ub_d": d_ub, "lift_x": x_mu, "t_x": x_t, "ub_x": x_ub,
            "n_x": nx, "net": net, "nameday": nameday, "t_crit": t_crit2(n_days),
            "drop_names": lift([r for r in rows if r["sym"] not in top_names], "arm", "ctrl")[0],
            "drop_days": O._day_t({d: v for d, v in d_means.items() if d not in top_days})[0],
            "lag1": lift(rows, "arm_lag1", "ctrl_lag1")[0]}


def _by_day(rows, key):
    d = defaultdict(list)
    for r in rows:
        if r["o"].get(key) is not None:
            d[r["day"]].append(r["o"][key])
    return d


def half_verdict(g: dict, pop: str) -> str:
    if (g["t_d"] is not None and g["t_d"] <= -2) or (g["t_x"] is not None and g["t_x"] <= -2):
        return "FAIL"
    fl_n, fl_d = FLOORS[pop]
    if g["n"] < fl_n or g["days"] < fl_d or g["t_d"] is None or g["t_x"] is None:
        return "UNDERPOWERED"
    ok = (g["lift_d"] >= LIFT_BAR and g["t_d"] >= g["t_crit"] and g["lift_x"] >= LIFT_BAR and g["t_x"] >= g["t_crit"]
          and g["net"] is not None and g["net"] > 0 and g["nameday"] is not None and g["nameday"] > 0
          and g["drop_names"] is not None and g["drop_names"] >= 0 and g["drop_days"] is not None and g["drop_days"] >= 0)
    return "PASS" if ok else "NOT_PASSED"


def final(halves: list[str], ubs: list[tuple], days: list[int], lag1: list) -> str:
    if "FAIL" in halves:
        return "FAIL"
    if all(h == "PASS" for h in halves):
        return "PASS" if all(x is not None and x > 0 for x in lag1) else "PASS_BOUNCE"
    if all(n >= 2 for n in days) and all(u[0] is not None and u[1] is not None and u[0] < LIFT_BAR and u[1] < LIFT_BAR
                                         for u in ubs):
        return "FAIL"
    return "UNDERPOWERED"


# ── run helpers ──────────────────────────────────────────────────────────────

def module_shas() -> dict:
    m = O.module_shas()
    for name, mod in (("fair_value_timing", FV), ("three_minute_reread", T3), ("flow_resting_entry", FR)):
        m[name] = hashlib.sha256(open(os.path.abspath(mod.__file__), "rb").read()).hexdigest()
    m["fair_value_trigger"] = hashlib.sha256(open(os.path.abspath(__file__), "rb").read()).hexdigest()
    return m


def write(name: str, obj) -> str:
    os.makedirs(OUT_DIR, exist_ok=True)
    p = os.path.join(OUT_DIR, name)
    json.dump(obj, open(p, "w"), indent=1, default=str)
    return p


def trigger_day(args) -> dict:
    """Onsets for P2 (sample names) and P1 (board names) of one day; outcome-free."""
    day, days, spy, p1 = args
    raw_today = H.load_raw("sip", day)
    p_iex = H.load_raw("iex", days[days.index(day) - 1])
    sample = H.sample_names(day, [s for s in H.load_raw("iex", day) if s in raw_today and s in p_iex])
    board = board_minutes(day) if p1 else {}
    syms = sorted(set(sample) | set(board))
    _d, zs, why = z_series_day((day, days, syms, spy))
    trig, rich, st = [], [], Counter()
    for s in sample:
        for m in onsets(zs.get(s, {})):
            trig.append({"pop": "P2", "day": day, "sym": s, "m": m, "z": zs[s][m]})
        for m in onsets(zs.get(s, {}), cut=FV.Z_RICH, below=False):
            rich.append({"pop": "P2", "day": day, "sym": s, "m": m})
    if p1:
        st["board_names"] += len(board)
        st["board_excluded"] += sum(1 for s in board if why.get(s) != "ok")
        for s, mins in board.items():
            for m in onsets(zs.get(s, {})):
                if m in mins:
                    trig.append({"pop": "P1", "day": day, "sym": s, "m": m, "z": zs[s][m]})
    return {"day": day, "triggers": trig, "rich": rich, "stats": dict(st)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    for f in ("--fidelity", "--triggers", "--fetch-p1", "--score"):
        g.add_argument(f, action="store_true")
    ap.add_argument("--skeptic-ready", default="")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    spy = pickle.load(open(FV.SPY_PATH, "rb"))
    days = H.warehouse_days()
    if a.fidelity:
        feats = json.load(open(os.path.join(FV.OUT_DIR, "features.json")))["entries"]
        by_day = defaultdict(list)
        for r in feats:
            if r.get("z") is not None:
                by_day[r["day"]].append(r)
        bad = n = 0
        with ProcessPoolExecutor(max_workers=a.workers) as ex:
            for day, zs, _why in ex.map(z_series_day, [(d, days, sorted({r["sym"] for r in rs}), spy)
                                                        for d, rs in sorted(by_day.items())]):
                for r in by_day[day]:
                    n += 1
                    z = zs.get(r["sym"], {}).get(r["m"])
                    if z is None or abs(z - r["z"]) > 1e-9:
                        bad += 1
        print(f"fidelity: {n - bad} of {n} rows equal (bad {bad})")
        write("fidelity.json", {"rows": n, "bad": bad, "ok": bad == 0, "module_sha256": module_shas()})
        if bad:
            raise SystemExit(1)
        return
    if a.triggers:
        fid = json.load(open(os.path.join(OUT_DIR, "fidelity.json")))
        if not fid.get("ok"):
            raise SystemExit("fidelity check not passed")
        ks = list(range(FV.LOOKBACK, len(days)))
        trig, rich, st = [], [], Counter()
        with ProcessPoolExecutor(max_workers=a.workers) as ex:
            for res in ex.map(trigger_day, [(days[k], days, spy, P1_FIRST <= days[k] <= P1_LAST) for k in ks]):
                trig += res["triggers"]
                rich += res["rich"]
                st.update(res["stats"])
        print(f"triggers {Counter(t['pop'] for t in trig)}; rich onsets {len(rich)}; {dict(st)}")
        write("triggers.json", {"module_sha256": module_shas(), "triggers": trig, "rich": rich, "stats": dict(st)})
        return
    trig = json.load(open(os.path.join(OUT_DIR, "triggers.json")))
    p1 = [t for t in trig["triggers"] if t["pop"] == "P1"]
    if a.fetch_p1:
        os.makedirs(P1_TICKS, exist_ok=True)
        cl = O.data_client()
        for i, t in enumerate(p1):
            path = os.path.join(P1_TICKS, O.entry_key(t) + ".pkl")
            if os.path.exists(path):
                continue
            tr, qu = O.fetch_entry(cl, t["sym"], O.decision_ts(t["day"], t["m"]))
            pickle.dump(O.compact(tr, qu), open(path + ".tmp", "wb"))
            os.replace(path + ".tmp", path)
            if (i + 1) % 200 == 0:
                print(f"  {i + 1}/{len(p1)}", flush=True)
        print(f"P1 ticks for {len(p1)} triggers")
        return
    # P1 spread cap (amended (4)): NBBO at T = the last clean quote <= the start of m, <= 30 s old
    keep, st = [], Counter()
    for t in trig["triggers"]:
        if t["pop"] == "P1":
            tick = FR.load_pkl(os.path.join(P1_TICKS, O.entry_key(t) + ".pkl"))
            q = O.last_quote(O.clean_quotes(tick["quotes"]), O.decision_ts(t["day"], t["m"]), 30.0) if tick else None
            if tick is None:
                st["p1_not_fetched"] += 1
                continue
            if q is None or (q[2] - q[1]) / ((q[1] + q[2]) / 2) > FR.SPREAD_CAP:
                st["p1_over_cap_or_no_quote"] += 1
                continue
        keep.append(t)
    by_day = defaultdict(list)
    for t in keep:
        by_day[t["day"]].append(t)
    rows = []
    for day, ts in sorted(by_day.items()):
        sip = H.load_raw("sip", day)
        sipm = {}

        def sm(n):
            if n not in sipm:
                sipm[n] = H.sip_oc(sip[n]) if n in sip else {}
            return sipm[n]
        board = board_minutes(day) if any(t["pop"] == "P1" for t in ts) else {}
        p2_pool = sorted({t2["sym"] for t2 in ts if t2["pop"] == "P2"}
                         | set(H.sample_names(day, [s for s in H.load_raw("iex", day) if s in sip])))
        for t in ts:
            if not (T3._ok(sm(t["sym"]), t["m"], HOLD)):
                st[f"{t['pop']}_no_bars"] += 1
                continue
            c = control_minute(t["sym"], day, t["m"], sm(t["sym"]))
            if t["pop"] == "P1":
                pool = sorted(s for s, mins in board.items() if any(t["m"] - 15 <= x <= t["m"] for x in mins))
            else:
                pool = p2_pool
            x = cross_name(t["sym"], day, t["m"], pool, sm)
            if x is None:
                st[f"{t['pop']}_no_x"] += 1
            row = {**t, "half": half_of(t["pop"], day), "c": c,
                   "c_on_board": (c in board.get(t["sym"], set())) if (t["pop"] == "P1" and c) else None}
            if a.score:
                row["o"] = outcome(sm(t["sym"]), t["m"], c, sm(x) if x else None)
            else:
                row["ctrl"] = T3.net(sm(t["sym"]), c, hold=HOLD, cost=COST) if c else None
                row["has_x"] = x is not None
            rows.append(row)
    if not a.score:
        cnt = {}
        for pop in ("P1", "P2"):
            for h in ("A", "B"):
                rs = [r for r in rows if r["pop"] == pop and r["half"] == h]
                ctl = defaultdict(list)
                for r in rs:
                    if r["ctrl"] is not None:
                        ctl[r["day"]].append(r["ctrl"])
                means = [statistics.mean(v) for v in ctl.values()]
                se = statistics.stdev(means) / math.sqrt(len(means)) * math.sqrt(2) if len(means) >= 2 else None
                tc = t_crit2(len(means))
                x_missing = (sum(1 for r in rs if not r["has_x"]) / len(rs)) if rs else None
                cnt[f"{pop} {h}"] = {"triggers": len(rs), "days": len(means),
                                     "projected_mde": (tc + 0.84) * se if (se and tc) else None,
                                     "x_missing_share": x_missing,
                                     "floors_met": len(rs) >= FLOORS[pop][0] and len(means) >= FLOORS[pop][1]
                                     and (x_missing is not None and x_missing <= X_MISSING_MAX)}
        for k_, v in cnt.items():
            print(f"  {k_}: {v}")
        print(f"drops {dict(st)}; board stats {trig['stats']}")
        print(write("count.json", {"module_sha256": module_shas(), "count": cnt, "drops": dict(st)}))
        return
    if not a.skeptic_ready:
        raise SystemExit("--score needs --skeptic-ready")
    cnt = json.load(open(os.path.join(OUT_DIR, "count.json")))
    if cnt.get("module_sha256") != module_shas():
        raise SystemExit("count.json was written by different code; rerun the count step")
    res = {"graded": {}, "halves": {}, "verdicts": {}}
    for pop in ("P1", "P2"):
        gs = {h: grade([r for r in rows if r["pop"] == pop and r["half"] == h]) for h in ("A", "B")}
        hv = {h: (half_verdict(gs[h], pop) if cnt["count"][f"{pop} {h}"]["floors_met"] or
                  (gs[h]["t_d"] is not None and gs[h]["t_d"] <= -2) else "UNDERPOWERED") for h in ("A", "B")}
        res["graded"][pop], res["halves"][pop] = gs, hv
        res["verdicts"][pop] = final([hv["A"], hv["B"]], [(gs[h]["ub_d"], gs[h]["ub_x"]) for h in ("A", "B")],
                                     [gs[h]["days"] for h in ("A", "B")], [gs[h]["lag1"] for h in ("A", "B")])
    # placebo: random pseudo-onsets matched in count per name-day (P2), lift_d t per half
    plc = {}
    for h in ("A", "B"):
        rs = [r for r in rows if r["pop"] == "P2" and r["half"] == h]
        n_nd = Counter((r["day"], r["sym"]) for r in rs)
        pr = []
        by_d = defaultdict(list)
        for (day, sym), n_ in n_nd.items():
            by_d[day].append((sym, n_))
        for day, items in sorted(by_d.items()):
            raw = H.load_raw("sip", day)                     # one load per day
            for sym, n_ in sorted(items):
                sip = H.sip_oc(raw[sym]) if sym in raw else {}
                cand = [m for m in range(M_LO, M_HI + 1) if T3._ok(sip, m, HOLD)]
                rng = random.Random(H._seed(sym, day, PLACEBO_TAG))
                for m in rng.sample(cand, min(n_, len(cand))):
                    c = control_minute(sym, day, m, sip)
                    pr.append({"day": day, "sym": sym, "o": {"arm": T3.net(sip, m, hold=HOLD, cost=COST),
                                                              "ctrl": T3.net(sip, c, hold=HOLD, cost=COST) if c else None}})
        plc[h] = lift(pr, "arm", "ctrl")[1]
    res["placebo"] = {"t": plc, "bound": S.t_ppf(1 - 0.025 / 2, 50),
                      "ok": all(t is not None and abs(t) <= S.t_ppf(1 - 0.025 / 2, 50) for t in plc.values())}
    res["info"] = {"P1 d on on-board control minutes": {h: lift([r for r in rows if r["pop"] == "P1" and r["half"] == h
                                                                 and r.get("c_on_board")], "arm", "ctrl")[:3] for h in ("A", "B")},
                   "P1 off-board control share": {h: (lambda rs: sum(1 for r in rs if r.get("c_on_board") is False) / max(1, len(rs)))(
                       [r for r in rows if r["pop"] == "P1" and r["half"] == h]) for h in ("A", "B")}}
    res["drops"], res["skeptic_ready"], res["module_sha256"] = dict(st), a.skeptic_ready, module_shas()
    write("result.json", res)
    print(json.dumps({k: res[k] for k in ("verdicts", "halves", "placebo", "info")}, indent=1, default=str))
    print(json.dumps(res["graded"], indent=1, default=str))


if __name__ == "__main__":
    main()
