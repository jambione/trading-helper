#!/usr/bin/env python3
"""Fair-value timing layer. Spec: docs/studies/fair_value_timing_prereg.json (as amended).

Among desk-style pre-square entries (the order-flow pilot's population, spread <= 0.05%), are the ones where the name
is temporarily CHEAP versus its own fair value (OLS on SPY and its 10 most-correlated warehouse peers, fitted on the
prior 10 sessions) better over the next 3 minutes than the rest? F1 = Z <= -1; F2 = Z <= -1 AND order-flow G1 = IN.
Z = the 30-minute residual sum / the SD of 30-minute residual sums over the prior 10 sessions.

  --fetch-spy   SPY SIP 1-minute RTH bars for every warehouse (and warehouse_confirm) session -> spy.pkl
  (default)     features (Z per entry) + outcome-blind count step -> features.json, count.json
  --score       verdicts from the pilot's outcome cache; refuses without --skeptic-ready and matching shas
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
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import flow_resting_entry as FR  # noqa: E402
import order_flow_pilot as O  # noqa: E402

S, E, H = O.S, O.E, O.H
OUT_DIR = os.path.join(E.ROOT, "ai_reports", "fair_value")
SPY_PATH = os.path.join(OUT_DIR, "spy.pkl")
LOOKBACK, K, K_INFO = 10, 10, (5, 20)
WIN, WIN_INFO, MIN_IN_WIN = 30, 60, 24
COVER, MIN_PEERS, MIN_BLOCKS = 0.80, 5, 10
Z_CHEAP, Z_RICH = -1.0, 1.0
BAR, BAR_CENTRAL = 0.0005, 0.0011
FLOORS, FLOOR_DAYS, N_TESTS = {"F1": 1000, "F2": 300}, 20, 2
MINUTES = 390                                   # 09:30 .. 15:59


# ── returns and the fair-value model ─────────────────────────────────────────

def minute_returns(rows: np.ndarray) -> np.ndarray:
    """Log return per RTH minute index 0..389 (09:30 = 0); NaN unless this minute and the previous one both have
    bars; the 09:30 bar's return is excluded (amended (5))."""
    out = np.full(MINUTES, np.nan)
    if rows is None or not len(rows):
        return out
    c = np.full(MINUTES, np.nan)
    idx = rows[:, 0].astype(int) - S.RTH_START
    ok = (idx >= 0) & (idx < MINUTES)
    c[idx[ok]] = rows[ok, 4]
    with np.errstate(invalid="ignore", divide="ignore"):
        out[1:] = np.log(c[1:] / c[:-1])
    out[0] = np.nan
    return out


def spy_returns(spy_day: dict) -> np.ndarray:
    rows = np.array([[m, 0, 0, 0, c, 0] for m, c in sorted(spy_day.items())], dtype=float) if spy_day else None
    return minute_returns(rows)


def coverage(x: np.ndarray) -> float:
    return float(np.mean(~np.isnan(x[np.arange(len(x)) % MINUTES != 0]))) if len(x) else 0.0


def top_peers(X: np.ndarray, i: int, k: int) -> list[int]:
    """Indices of the k columns most correlated with column i over minutes present in both (NaN-aware)."""
    M = ~np.isnan(X)
    Xz = np.nan_to_num(X)
    xi, mi = Xz[:, i], M[:, i]
    J = M & mi[:, None]
    n = np.maximum(J.sum(0), 1)
    sx, sy = (Xz * J).sum(0), (xi[:, None] * J).sum(0)
    sxy, sxx, syy = (Xz * xi[:, None] * J).sum(0), (Xz * Xz * J).sum(0), ((xi ** 2)[:, None] * J).sum(0)
    corr = (sxy - sx * sy / n) / np.sqrt(np.maximum((sxx - sx * sx / n) * (syy - sy * sy / n), 1e-30))
    corr[i] = -np.inf
    corr[J.sum(0) < 100] = -np.inf
    return [int(j) for j in np.argsort(-corr)[:k]]


def basket(X: np.ndarray, cols: list[int]) -> np.ndarray:
    sub = X[:, cols]
    n = (~np.isnan(sub)).sum(1)
    with np.errstate(invalid="ignore"):
        b = np.nanmean(sub, axis=1)
    b[n < MIN_PEERS] = np.nan
    return b


def fit_betas(y: np.ndarray, spy: np.ndarray, bk: np.ndarray) -> tuple[float, float] | None:
    ok = ~(np.isnan(y) | np.isnan(spy) | np.isnan(bk))
    if ok.sum() < 200:
        return None
    A = np.column_stack([spy[ok], bk[ok]])
    b, *_ = np.linalg.lstsq(A, y[ok], rcond=None)
    return float(b[0]), float(b[1])


def block_sd(resid_prior: np.ndarray, win: int = WIN) -> float | None:
    """SD of non-overlapping `win`-minute residual sums over the prior sessions (blocks inside a session, >= 80% of
    the block's minutes valid)."""
    sums = []
    for s0 in range(0, len(resid_prior), MINUTES):
        sess = resid_prior[s0:s0 + MINUTES]
        for b0 in range(1, MINUTES - win + 1, win):
            blk = sess[b0:b0 + win]
            if np.sum(~np.isnan(blk)) >= int(math.ceil(0.8 * win)):
                sums.append(float(np.nansum(blk)))
    return statistics.stdev(sums) if len(sums) >= MIN_BLOCKS else None


def z_at(resid_today: np.ndarray, m: int, sd: float | None, win: int = WIN) -> float | None:
    """Residual sum over the `win` completed minutes m - win .. m - 1 / sd; None if < 80% valid."""
    if sd is None or sd <= 0:
        return None
    i1 = m - S.RTH_START
    blk = resid_today[max(0, i1 - win):i1]
    if len(blk) < win or np.sum(~np.isnan(blk)) < int(math.ceil(0.8 * win)):
        return None
    return float(np.nansum(blk)) / sd


# ── per day ──────────────────────────────────────────────────────────────────

def day_task(args) -> list[dict]:
    """Z (and information variants) for every population entry of one session."""
    day, days, entries, spy = args
    k = days.index(day)
    prior = days[k - LOOKBACK:k]
    if len(prior) < LOOKBACK:
        return [{**e, "z": None, "why": "short_lookback"} for e in entries]
    raws = [H.load_raw("sip", d) for d in prior]
    today = H.load_raw("sip", day)
    names = sorted(set.intersection(*[set(r) for r in raws]) & set(today))
    Xp = np.column_stack([np.concatenate([minute_returns(r[s]) for r in raws]) for s in names]) if names else None
    Xt = np.column_stack([minute_returns(today[s]) for s in names]) if names else None
    spy_p = np.concatenate([spy_returns(spy.get(d, {})) for d in prior])
    spy_t = spy_returns(spy.get(day, {}))
    cov_ok = np.array([coverage(Xp[:, j]) >= COVER for j in range(len(names))]) if names else np.array([])
    col = {s: j for j, s in enumerate(names)}
    out, cache = [], {}
    cand = [j for j in range(len(names)) if cov_ok[j]]
    for e in entries:
        j = col.get(e["sym"])
        if j is None or not cov_ok[j]:
            out.append({**e, "z": None, "why": "coverage"})
            continue
        if e["sym"] not in cache:
            sub = Xp[:, cand]
            ji = cand.index(j)
            ranked = top_peers(sub, ji, max((K,) + K_INFO))
            res = {}
            for kk in (K,) + K_INFO:
                peers = [cand[r] for r in ranked[:kk]]
                bp, bt = basket(Xp, peers), basket(Xt, peers)
                be = fit_betas(Xp[:, j], spy_p, bp)
                if be is None:
                    res[kk] = None
                    continue
                rp = Xp[:, j] - be[0] * spy_p - be[1] * bp
                rt = Xt[:, j] - be[0] * spy_t - be[1] * bt
                res[kk] = (rt, block_sd(rp, WIN), block_sd(rp, WIN_INFO), be)
            cache[e["sym"]] = res
        res = cache[e["sym"]]
        if res.get(K) is None:
            out.append({**e, "z": None, "why": "fit"})
            continue
        rt, sd30, sd60, be = res[K]
        row = {**e, "z": z_at(rt, e["m"], sd30), "z60": z_at(rt, e["m"], sd60, WIN_INFO),
               "beta_spy": be[0], "beta_peer": be[1]}
        for kk in K_INFO:
            row[f"z_k{kk}"] = z_at(res[kk][0], e["m"], res[kk][1]) if res.get(kk) else None
        row["why"] = "ok" if row["z"] is not None else "window"
        out.append(row)
    return out


def sides(r: dict) -> dict:
    z, g1 = r.get("z"), r["groups"]["G1"]
    f1 = None if z is None else ("IN" if z <= Z_CHEAP else "OUT")
    f2 = None if (z is None or g1 is None) else ("IN" if (z <= Z_CHEAP and g1 == "IN") else "OUT")
    return {"F1": f1, "F2": f2}


# ── count step ───────────────────────────────────────────────────────────────

def t_crit2(days: int) -> float | None:
    return S.t_ppf(1 - 0.025 / N_TESTS, days - 1) if days >= 2 else None


def count_step(rows: list[dict]) -> dict:
    out = {}
    by_day = defaultdict(list)
    for r in rows:
        by_day[r["day"]].append(r)
    ctrl = {}
    for day, rs in sorted(by_day.items()):
        sip = H.load_raw("sip", day)
        for r in rs:
            ctrl[O.entry_key(r)] = O.control_return(r["sym"], day, r["m"], H.sip_oc(sip[r["sym"]])) \
                if r["sym"] in sip else None
    for f in ("F1", "F2"):
        out[f] = {}
        for half in ("A", "B"):
            per = {"IN": defaultdict(list), "OUT": defaultdict(list)}
            for r in rows:
                s_ = r["sides"][f]
                c = ctrl.get(O.entry_key(r))
                if r["half"] == half and s_ and c is not None:
                    per[s_][r["day"]].append(c)
            n_in = sum(len(v) for v in per["IN"].values())
            days = [d for d in per["IN"] if len(per["IN"][d]) >= 3 and len(per["OUT"][d]) >= 3]
            diffs = [statistics.mean(per["IN"][d]) - statistics.mean(per["OUT"][d]) for d in days]
            se = statistics.stdev(diffs) / math.sqrt(len(diffs)) if len(diffs) >= 2 else None
            tc = t_crit2(len(diffs))
            out[f][half] = {"n_in": n_in, "n_out": sum(len(v) for v in per["OUT"].values()), "days": len(days),
                            "projected_mde": (tc + 0.84) * se if (se and tc) else None,
                            "floors_met": n_in >= FLOORS[f] and len(days) >= FLOOR_DAYS,
                            "opens_per_day": n_in / max(1, len({r["day"] for r in rows if r["half"] == half}))}
    miss = Counter((r["groups"]["G1"], r["z"] is None) for r in rows)
    out["missing_z_share_by_G1"] = {g: miss[(g, True)] / max(1, miss[(g, True)] + miss[(g, False)]) for g in ("IN", "OUT")}
    return out


# ── score ────────────────────────────────────────────────────────────────────

def same_hour_did(rows: list[dict], f: str, half: str, key: str) -> float | None:
    """Per day: the mean over clock hours (>= 3 a side) of IN - OUT; day-equal mean (amended (3))."""
    per = defaultdict(lambda: {"IN": [], "OUT": []})
    for r in rows:
        s_ = r["sides"][f]
        if r["half"] == half and s_ and r.get(key) is not None:
            per[(r["day"], r["m"] // 60)][s_].append(r[key])
    by_day = defaultdict(list)
    for (day, _h), v in per.items():
        if len(v["IN"]) >= 3 and len(v["OUT"]) >= 3:
            by_day[day].append(statistics.mean(v["IN"]) - statistics.mean(v["OUT"]))
    return O._day_t({d: statistics.mean(v) for d, v in by_day.items()})[0]


def grade(rows: list[dict], f: str, half: str) -> dict:
    key = f"gross{O.PRIMARY_H}"
    fn = (lambda r, f=f: r["sides"][f])
    g = O.gated_minus_rest(rows, "G1", half, key, side_fn=fn)
    g["t_crit"] = t_crit2(g["days"])
    contrib = Counter()
    live = [r for r in rows if r["half"] == half and r["sides"][f] and r.get(key) is not None]
    cnt = Counter((r["day"], r["sides"][f]) for r in live)
    nd = max(1, len({r["day"] for r in live}))
    for r in live:
        w = r[key] / cnt[(r["day"], r["sides"][f])] / nd
        contrib[r["sym"]] += w if r["sides"][f] == "IN" else -w
    top_names = frozenset(n for n, _ in contrib.most_common(3))
    g["drop_names"] = O.gated_minus_rest(rows, "G1", half, key, drop_names=top_names, side_fn=fn)["diff"]
    per_day = {}
    for day in {r["day"] for r in live}:
        a = [r[key] for r in live if r["day"] == day and r["sides"][f] == "IN"]
        b = [r[key] for r in live if r["day"] == day and r["sides"][f] == "OUT"]
        if len(a) >= 3 and len(b) >= 3:
            per_day[day] = statistics.mean(a) - statistics.mean(b)
    top_days = frozenset(sorted(per_day, key=lambda d: per_day[d], reverse=True)[:3])
    g["drop_days"] = O.gated_minus_rest(rows, "G1", half, key, drop_days=top_days, side_fn=fn)["diff"]
    g["same_hour"] = same_hour_did(rows, f, half, key)
    if f == "F2":
        inside = [dict(r, sides={**r["sides"], "X": None if r["z"] is None else ("IN" if r["z"] <= Z_CHEAP else "OUT")})
                  for r in rows if r["groups"]["G1"] == "IN"]
        g["inside_g1"] = O.gated_minus_rest(inside, "G1", half, key, side_fn=lambda r: r["sides"]["X"])["diff"]
    return g


def half_verdict(g: dict, f: str) -> str:
    if g["n_in"] < FLOORS[f] or g["days"] < FLOOR_DAYS or g["t"] is None:
        return "UNDERPOWERED"
    if g["t"] <= -2.0:
        return "FAIL"
    ok = (g["diff"] >= BAR and g["t"] >= g["t_crit"] and g["in_net"] is not None and g["in_net"] > 0
          and g["nameday"] is not None and g["nameday"] > 0 and g["drop_names"] is not None and g["drop_names"] >= 0
          and g["drop_days"] is not None and g["drop_days"] >= 0 and g["same_hour"] is not None and g["same_hour"] > 0)
    if f == "F2":
        ok = ok and g.get("inside_g1") is not None and g["inside_g1"] > 0
    return "PASS" if ok else "NOT_PASSED"


def final(halves: list[str], ubs: list) -> str:
    if "FAIL" in halves:
        return "FAIL"
    if all(h == "PASS" for h in halves):
        return "PASS"
    if all(h != "UNDERPOWERED" for h in halves) and all(u is not None and u < BAR for u in ubs):
        return "FAIL"
    return "UNDERPOWERED"


def placebo(rows: list[dict]) -> dict:
    """Coin-flip IN / OUT per entry on the F1 population: |t| of the difference per half (a calibration check)."""
    def coin(r):
        if r["sides"]["F1"] is None:
            return None
        u = int(hashlib.sha256(f"{r['sym']}|{r['day']}|{r['m']}|fv-placebo".encode()).hexdigest()[:16], 16) / 2 ** 64
        return "IN" if u < 1 / 3 else "OUT"
    key = f"gross{O.PRIMARY_H}"
    res = {h: O.gated_minus_rest(rows, "G1", h, key, side_fn=coin) for h in ("A", "B")}
    bound = S.t_ppf(1 - 0.025 / 2, 50)
    return {"t": {h: res[h]["t"] for h in res}, "bound": bound,
            "ok": all(res[h]["t"] is not None and abs(res[h]["t"]) <= bound for h in res)}


# ── data / main ──────────────────────────────────────────────────────────────

def fetch_spy(days: list[str]) -> dict:
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    cl = O.data_client()
    spy = pickle.load(open(SPY_PATH, "rb")) if os.path.exists(SPY_PATH) else {}
    for d in days:
        if d in spy:
            continue
        y, m, dd = map(int, d.split("-"))
        t0 = datetime(y, m, dd, 9, 30, tzinfo=E.ET)
        r = cl.get_stock_bars(StockBarsRequest(symbol_or_symbols="SPY", timeframe=TimeFrame.Minute, start=t0,
                                               end=t0 + timedelta(hours=6, minutes=30), feed=DataFeed.SIP)).data.get("SPY") or []
        spy[d] = {E._et_min(b.timestamp.timestamp()): float(b.close) for b in r}
        time.sleep(O.REQ_GAP_S)
    os.makedirs(OUT_DIR, exist_ok=True)
    pickle.dump(spy, open(SPY_PATH, "wb"))
    return spy


def module_shas() -> dict:
    m = O.module_shas()
    m["fair_value_timing"] = hashlib.sha256(open(os.path.abspath(__file__), "rb").read()).hexdigest()
    return m


def write(name: str, obj) -> str:
    os.makedirs(OUT_DIR, exist_ok=True)
    p = os.path.join(OUT_DIR, name)
    json.dump(obj, open(p, "w"), indent=1, default=str)
    return p


def build_rows(workers: int) -> tuple[list[dict], Counter]:
    feats = json.load(open(os.path.join(O.OUT_DIR, "features.json")))
    st, pop = Counter(), []
    for e in feats["entries"]:
        tick = FR.load_pkl(os.path.join(O.TICK_DIR, O.entry_key(e) + ".pkl"))
        q = FR.entry_quote(tick["quotes"], O.decision_ts(e["day"], e["m"])) if tick else None
        if not FR.in_population(q):
            st["over_cap_or_no_quote"] += 1
            continue
        pop.append(e)
    spy = pickle.load(open(SPY_PATH, "rb"))
    days = H.warehouse_days()
    by_day = defaultdict(list)
    for e in pop:
        by_day[e["day"]].append(e)
    rows = []
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for got in ex.map(day_task, [(d, days, es, spy) for d, es in sorted(by_day.items())]):
            rows += got
    for r in rows:
        r["sides"] = sides(r)
        st[f"z_{r['why']}"] += 1
    return rows, st


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--fetch-spy", action="store_true")
    g.add_argument("--score", action="store_true")
    ap.add_argument("--skeptic-ready", default="")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    if a.fetch_spy:
        days = H.warehouse_days()
        conf = os.path.join(E.ROOT, "ai_reports", "warehouse_confirm", "sip")
        if os.path.isdir(conf):
            days += sorted(x[:-4] for x in os.listdir(conf) if x.endswith(".npz"))
        spy = fetch_spy(sorted(set(days)))
        print(f"SPY bars for {len(spy)} sessions -> {SPY_PATH}")
        return
    if not a.score:
        rows, st = build_rows(a.workers)
        cnt = count_step(rows)
        print(f"rows {len(rows)}; {dict(st)}")
        for f in ("F1", "F2"):
            for h in ("A", "B"):
                c = cnt[f][h]
                print(f"  {f} {h}: IN {c['n_in']} OUT {c['n_out']} days {c['days']} MDE "
                      f"{'-' if c['projected_mde'] is None else round(c['projected_mde'] * 1e4, 1)} bp floors {c['floors_met']} "
                      f"opens/day {c['opens_per_day']:.1f}")
        print(f"missing-Z share by G1: {cnt['missing_z_share_by_G1']}")
        write("features.json", {"module_sha256": module_shas(),
                                "entries": [{k: r.get(k) for k in ("day", "sym", "m", "half", "groups", "z", "z60", "z_k5",
                                                                   "z_k20", "why")} for r in rows]})
        print(write("count.json", {"module_sha256": module_shas(), "drops": dict(st), "count": cnt}))
        return
    if not a.skeptic_ready:
        raise SystemExit("--score needs --skeptic-ready")
    cnt = json.load(open(os.path.join(OUT_DIR, "count.json")))
    if cnt.get("module_sha256") != module_shas():
        raise SystemExit("count.json was written by different code; rerun the count step")
    feats = json.load(open(os.path.join(OUT_DIR, "features.json")))["entries"]
    rows = []
    for r in feats:
        oq = FR.load_pkl(os.path.join(O.OUTCOME_DIR, O.entry_key(r) + ".pkl"))
        if oq is None:
            continue
        o = O.outcomes_for(oq, O.decision_ts(r["day"], r["m"]))
        if not o["entry"]:
            continue
        rows.append({**r, **o, "sides": sides(r)})
    res = {"graded": {}, "halves": {}, "verdicts": {}}
    for f in ("F1", "F2"):
        res["graded"][f] = {h: grade(rows, f, h) for h in ("A", "B")}
        res["halves"][f] = {h: half_verdict(res["graded"][f][h], f) if cnt["count"][f][h]["floors_met"] else "UNDERPOWERED"
                            for h in ("A", "B")}
        res["verdicts"][f] = final([res["halves"][f][h] for h in ("A", "B")],
                                   [res["graded"][f][h]["ub"] for h in ("A", "B")])
        res["graded"][f]["vs_central_11bp"] = {h: (res["graded"][f][h]["diff"] or 0) >= BAR_CENTRAL for h in ("A", "B")}
    key = f"gross{O.PRIMARY_H}"
    rich = (lambda r: None if r.get("z") is None else ("IN" if r["z"] >= Z_RICH else "OUT"))
    z2 = (lambda r: None if r.get("z") is None else ("IN" if r["z"] <= -2 else "OUT"))
    info = {"RICH (z >= +1) minus rest": {h: O._bp_t(O.gated_minus_rest(rows, "G1", h, key, side_fn=rich)) for h in ("A", "B")},
            "z <= -2": {h: O._bp_t(O.gated_minus_rest(rows, "G1", h, key, side_fn=z2)) for h in ("A", "B")}}
    for zk in ("z60", "z_k5", "z_k20"):
        fn = (lambda r, zk=zk: None if r.get(zk) is None else ("IN" if r[zk] <= Z_CHEAP else "OUT"))
        info[f"F1 with {zk}"] = {h: O._bp_t(O.gated_minus_rest(rows, "G1", h, key, side_fn=fn)) for h in ("A", "B")}
    res["info"] = info
    res["placebo"] = placebo(rows)
    res["skeptic_ready"] = a.skeptic_ready
    res["module_sha256"] = module_shas()
    write("result.json", res)
    print(json.dumps({k: res[k] for k in ("verdicts", "halves", "placebo")}, indent=1, default=str))
    print(json.dumps(res["graded"], indent=1, default=str)[:6000])
    print(json.dumps(info, indent=1))


if __name__ == "__main__":
    main()
