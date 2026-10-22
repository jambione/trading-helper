#!/usr/bin/env python3
"""Flow-conditioned resting entries. Spec: docs/studies/flow_resting_entry_prereg.json (as amended).

When buy-side trade flow is strong (the order-flow pilot's G1), does a buy limit resting at the bid for 30 s (filled
only when round-lot SIP prints trade strictly below it, back of the queue), held 180 s and sold at the bid, make money
after costs - and does the flow condition cut the adverse selection that sank plain resting entries on 10/3?

Built on the order-flow pilot (tools/studies/order_flow_pilot.py): its features.json (G1 groups and cuts), its feature
tick cache (quotes up to T + 1 s: the entry quote, outcome-free) and its outcome quote cache ([T + 1 - 30 s,
T + 1 + 300 s]: exits). New data: SIP trades over [T + 1 s, T + 121 s] per entry (exchange, codes, tape stored).

  (default)       count step: population after the spread cap and size rule, R_G1 / R_OUT counts per half, projected
                  per-fill MDE (p = 0.5); says whether the fetch should run (MDE <= 5 bp) -> count.json
  --fetch-trades  the trade fetch (paged, cached, resumable, paced; abort > 2% failures)
  --score         verdicts; refuses without --skeptic-ready and matching module shas
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import pickle
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import order_flow_pilot as O  # noqa: E402

S, E, H = O.S, O.E, O.H
OUT_DIR = os.path.join(E.ROOT, "ai_reports", "flow_resting")
TRADE_DIR = os.path.join(OUT_DIR, "trades")
SPREAD_CAP, NOTIONAL = 0.0005, 2250.0
W_PRIMARY, W_INFO, W_FETCH = 30, (10, 60), 120
HOLD = 180
READ_MAX_AGE = 30.0
FILL_CODES = frozenset({" ", "@", "F"})          # amended (2): round lots only; any 'I' disqualifies
BAR = 0.0005
FLOOR_FILLS, FLOOR_DAYS, FILL_RATE_MIN = 150, 20, 0.30      # amended_2 (b): 150 fills (half A has 411 signals)
STRATUM_MIN = 30                                            # amended_2 (e)
DROP_MAX, DROP_GAP_MAX = 0.02, 0.02


# ── pure pieces ──────────────────────────────────────────────────────────────

def entry_quote(feature_quotes: list, T: float):
    """amended (3): the last clean quote <= T + 1 s, <= 30 s old, from the feature tick cache (outcome-free)."""
    return O.last_quote(O.clean_quotes(feature_quotes), T + 1, READ_MAX_AGE)


def in_population(q) -> bool:
    if q is None:
        return False
    _t, b, a, _bs, _as = q
    return (a - b) / ((a + b) / 2) <= SPREAD_CAP


def shares_for(mid: float) -> int:
    return int(NOTIONAL // mid) if mid > 0 else 0


def counts_for_fill(codes) -> bool:
    """A print counts toward our fill only if it is a round lot: all codes in {' ', '@', 'F'} and no 'I'."""
    codes = list(codes or [])
    return bool(codes) and "I" not in codes and all(c in FILL_CODES for c in codes)


def fill_time(trades: list, limit: float, shares: int, start: float, window: float, lit_only: bool = False,
              min_below: float = 0.0) -> float | None:
    """Back of the queue: filled at the time the cumulative size of counting prints STRICTLY below `limit` (by at
    least `min_below`) reaches `shares`, within (start, start + window]. trades: (ts, price, size, codes, exchange)."""
    cum = 0.0
    for ts, px, size, codes, exch, *_ in sorted(trades, key=lambda t: t[0]):     # cache rows also carry the tape
        if ts <= start or ts > start + window:
            continue
        if lit_only and str(exch) == "D":
            continue
        below = (px <= limit - min_below + 1e-9) if min_below > 0 else (px < limit - 1e-12)   # amended_2 (g)
        if below and counts_for_fill(codes):
            cum += size
            if cum >= shares:
                return ts
    return None


def exit_read(out_quotes: list, t: float):
    """(bid, mid) of the last clean quote <= t, <= 30 s old, else None."""
    q = O.last_quote(O.clean_quotes(out_quotes), t, READ_MAX_AGE)
    return None if q is None else (q[1], (q[1] + q[2]) / 2)


def simulate(e: dict, fq: list, oq: list, trades: list, window: float = W_PRIMARY, limit_at: str = "bid",
             lit_only: bool = False, min_below: float = 0.0) -> dict:
    """One signal: resting arm R and market arm M. A fill with no exit quote drops the SIGNAL from both (amended 4)."""
    T = O.decision_ts(e["day"], e["m"])
    q = entry_quote(fq, T)
    if q is None:
        return {"status": "no_entry_quote"}
    _t, b, a, _bs, _as = q
    mid = (a + b) / 2
    limit = b if limit_at == "bid" else math.floor(mid * 100) / 100
    sh = shares_for(mid)
    if sh < 1:
        return {"status": "size"}
    out = {"status": "ok", "spread_ticks": 1 if (a - b) <= 0.0100001 else 2}
    mx = exit_read(oq, T + 1 + HOLD)
    if mx is None:
        return {"status": "no_exit_quote"}
    out["m_net"] = mx[0] / a - 1
    tf = fill_time(trades, limit, sh, T + 1, window, lit_only, min_below)
    if tf is None:
        out.update({"filled": False, "r_net": 0.0})
        return out
    rx = exit_read(oq, tf + HOLD)
    if rx is None:
        return {"status": "no_exit_quote"}
    out.update({"filled": True, "r_net": rx[0] / limit - 1, "r_mid": rx[1] / limit - 1, "tf_wait": tf - T - 1})
    return out


# ── statistics ───────────────────────────────────────────────────────────────

def _t(vals: dict):
    return O._day_t(vals)


def day_means(rows, key, pred=lambda r: True):
    d = defaultdict(list)
    for r in rows:
        if pred(r) and r.get(key) is not None:
            d[r["day"]].append(r[key])
    return {k: statistics.mean(v) for k, v in d.items() if v}


def ub975(vals: dict):
    xs = list(vals.values())
    if len(xs) < 2:
        return None
    mu, se = statistics.mean(xs), statistics.stdev(xs) / math.sqrt(len(xs))
    return mu + S.t_ppf(0.975, len(xs) - 1) * se


def grade_half(rows: list[dict], half: str) -> dict:
    """rows: simulated signals with 'g1' in {IN, OUT}, 'half', 'day', 'sym'."""
    rin = [r for r in rows if r["half"] == half and r["g1"] == "IN"]
    rout = [r for r in rows if r["half"] == half and r["g1"] == "OUT"]
    fills_in = [r for r in rin if r["filled"]]
    sig = day_means(rin, "r_net")
    pf = day_means(fills_in, "r_net")
    gap_days = {}
    for day in {r["day"] for r in fills_in}:
        a = [r["r_mid"] for r in fills_in if r["day"] == day]
        b = [r["r_mid"] for r in rout if r["filled"] and r["day"] == day]
        if len(a) >= 3 and len(b) >= 3:
            gap_days[day] = statistics.mean(a) - statistics.mean(b)
    strata = {}
    for st in (1, 2):
        a_ = [r for r in fills_in if r["spread_ticks"] == st]
        b_ = [r for r in rout if r["filled"] and r["spread_ticks"] == st]
        strata[st] = ((statistics.mean(x["r_mid"] for x in a_) - statistics.mean(x["r_mid"] for x in b_))
                      if len(a_) >= STRATUM_MIN and len(b_) >= STRATUM_MIN else None)     # amended_2 (e)
    paired = {}
    for day in {r["day"] for r in rin}:
        rs = [r for r in rin if r["day"] == day]
        paired[day] = statistics.mean(r["r_net"] - r["m_net"] for r in rs)
    nd = defaultdict(list)
    for r in fills_in:
        nd[(r["day"], r["sym"])].append(r["r_net"])
    nd_all = [statistics.mean(v) for v in nd.values()]          # amended_2 (f): every name-day weighs the same
    contrib = Counter()
    for r in rin:
        contrib[r["sym"]] += r["r_net"]
    top_names = {n for n, _ in contrib.most_common(3)}
    top_days = set(sorted(sig, key=lambda d: sig[d], reverse=True)[:3])
    sig_mu, sig_t, ndays = _t(sig)
    pf_mu, pf_t, _ = _t(pf)
    gap_mu, gap_t, _ = _t(gap_days)
    return {"signals": len(rin), "fills": len(fills_in), "days": ndays,
            "fill_rate": len(fills_in) / len(rin) if rin else None,
            "per_signal": sig_mu, "per_signal_t": sig_t, "per_fill": pf_mu, "per_fill_t": pf_t, "per_fill_ub": ub975(pf),
            "gap": gap_mu, "gap_t": gap_t, "gap_strata": strata, "vs_market": _t(paired)[0],
            "nameday_per_fill": statistics.mean(nd_all) if nd_all else None,
            "fill_days": len(pf),
            "drop_names": _t(day_means([r for r in rin if r["sym"] not in top_names], "r_net"))[0],
            "drop_days": _t({d: v for d, v in sig.items() if d not in top_days})[0],
            "out_fill_rate": (sum(r["filled"] for r in rout) / len(rout)) if rout else None,
            "out_per_fill": _t(day_means([r for r in rout if r["filled"]], "r_net"))[0]}


def half_verdict(g: dict) -> str:
    """amended_2 (a): the t <= -2 FAIL comes before the floors; floors gate PASS only."""
    if g["per_signal_t"] is not None and g["per_signal_t"] <= -2.0:
        return "FAIL"
    if g["fills"] < FLOOR_FILLS or g["days"] < FLOOR_DAYS or g["per_signal_t"] is None:
        return "UNDERPOWERED"
    evaluated = [v for v in g["gap_strata"].values() if v is not None]
    ok = (g["per_signal"] > 0 and g["per_signal_t"] >= 2.0 and g["per_fill"] is not None and g["per_fill"] >= BAR
          and g["gap"] is not None and g["gap"] > 0 and g["gap_t"] is not None and g["gap_t"] >= 2.0
          and evaluated and all(v > 0 for v in evaluated)
          and g["vs_market"] is not None and g["vs_market"] > 0 and g["fill_rate"] >= FILL_RATE_MIN
          and g["nameday_per_fill"] is not None and g["nameday_per_fill"] > 0
          and g["drop_names"] is not None and g["drop_names"] >= 0 and g["drop_days"] is not None and g["drop_days"] >= 0)
    return "PASS" if ok else "NOT_PASSED"


def verdict(halves: list[str], ubs: list, fill_days: list | None = None) -> str:
    """amended_2 (a): the per-fill upper-bound FAIL applies whenever each half has >= 2 fill-days, regardless of floors."""
    if "FAIL" in halves:
        return "FAIL"
    if all(h == "PASS" for h in halves):
        return "PASS"
    enough = all(n is not None and n >= 2 for n in (fill_days or [2] * len(ubs)))
    if enough and all(u is not None and u < BAR for u in ubs):
        return "FAIL"
    return "UNDERPOWERED"


# ── data ─────────────────────────────────────────────────────────────────────

def load_pkl(path):
    return pickle.load(open(path, "rb")) if os.path.exists(path) else None


def population(features: dict, tick_dir: str = O.TICK_DIR) -> tuple[list[dict], Counter]:
    """Entries with G1 IN or OUT that pass the spread cap and the size rule, from the feature cache only."""
    st, pop = Counter(), []
    for e in features["entries"]:
        g1 = e["groups"]["G1"]
        if g1 is None:
            st["no_g1"] += 1
            continue
        tick = load_pkl(os.path.join(tick_dir, O.entry_key(e) + ".pkl"))
        q = entry_quote(tick["quotes"], O.decision_ts(e["day"], e["m"])) if tick else None
        if q is None:
            st["no_entry_quote"] += 1
            continue
        if not in_population(q):
            st["over_spread_cap"] += 1
            continue
        if shares_for((q[1] + q[2]) / 2) < 1:
            st["size"] += 1
            continue
        pop.append({**e, "g1": g1})
    return pop, st


def fetch_trades(cl, sym: str, T: float, sleep=time.sleep, tries: int = 5) -> list:
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockTradesRequest

    def utc(x):
        return datetime.fromtimestamp(x, timezone.utc)
    for k in range(tries):
        try:
            tr = cl.get_stock_trades(StockTradesRequest(symbol_or_symbols=sym, start=utc(T + 1), end=utc(T + 1 + W_FETCH),
                                                        feed=DataFeed.SIP, limit=None)).data.get(sym) or []
            sleep(O.REQ_GAP_S)
            return [(O._ts(t), float(t.price), float(t.size or 0), O._codes(t), str(getattr(t, "exchange", "") or ""),
                     str(getattr(t, "tape", "") or "")) for t in tr]
        except Exception as ex:  # noqa: BLE001
            print(f"  {sym} {int(T)} try {k}: {type(ex).__name__} {str(ex)[:80]}", flush=True)
            sleep(10 * (k + 1))
    raise RuntimeError(f"trade fetch failed {sym} {int(T)}")


def fetch_all(pop: list[dict], cl, out_dir: str = TRADE_DIR, sleep=time.sleep, log=print) -> Counter:
    os.makedirs(out_dir, exist_ok=True)
    st = Counter()
    for i, e in enumerate(pop):
        path = os.path.join(out_dir, O.entry_key(e) + ".pkl")
        if os.path.exists(path):
            st["cached"] += 1
            continue
        st["attempted"] += 1
        try:
            tr = fetch_trades(cl, e["sym"], O.decision_ts(e["day"], e["m"]), sleep=sleep)
        except RuntimeError as ex:
            st["failed"] += 1
            log(f"  FAILED {O.entry_key(e)}: {ex}")
            if st["attempted"] >= O.FAIL_MIN_ATTEMPTS and st["failed"] / st["attempted"] > O.FAIL_ABORT:
                log("ABORT: > 2% of entries failed")
                st["aborted"] = 1
                return st
            continue
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            pickle.dump(tr, f)
        os.replace(tmp, path)
        st["fetched"] += 1
        if st["fetched"] % 500 == 0:
            log(f"  {i + 1}/{len(pop)}: fetched {st['fetched']} ({datetime.now(E.ET):%H:%M} ET)")
    return st


def module_shas() -> dict:
    m = O.module_shas()
    m["flow_resting_entry"] = hashlib.sha256(open(os.path.abspath(__file__), "rb").read()).hexdigest()
    return m


def write(name: str, obj) -> str:
    os.makedirs(OUT_DIR, exist_ok=True)
    p = os.path.join(OUT_DIR, name)
    json.dump(obj, open(p, "w"), indent=1, default=str)
    return p


def count_step(pop: list[dict]) -> dict:
    """Outcome-blind: counts, and the projected per-fill MDE from same-name control-minute 3-bar SIP bar returns
    (the pilot's control_return), at an assumed fill rate p = 0.5: SE_fill ~ SD / sqrt(n p) per day-clustered."""
    out = {}
    by_day = defaultdict(list)
    for e in pop:
        by_day[e["day"]].append(e)
    ctrl = {}
    for day, es in sorted(by_day.items()):
        sip = H.load_raw("sip", day)
        for e in es:
            ctrl[O.entry_key(e)] = O.control_return(e["sym"], day, e["m"], H.sip_oc(sip[e["sym"]])) \
                if e["sym"] in sip else None
    for half in ("A", "B"):
        rin = [e for e in pop if e["half"] == half and e["g1"] == "IN"]
        days = defaultdict(list)
        for e in rin:
            if ctrl.get(O.entry_key(e)) is not None:
                days[e["day"]].append(ctrl[O.entry_key(e)])
        means = [statistics.mean(v) for v in days.values() if v]
        se = (statistics.stdev(means) / math.sqrt(len(means))) * math.sqrt(1 / 0.5) if len(means) >= 2 else None
        mde = (S.t_ppf(0.975, len(means) - 1) + 0.84) * se if se else None
        out[half] = {"R_G1_signals": len(rin), "R_OUT_signals": sum(1 for e in pop if e["half"] == half and e["g1"] == "OUT"),
                     "days": len(means), "projected_per_fill_mde": mde, "fetch_worthwhile": mde is not None and mde <= BAR}
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--fetch-trades", action="store_true")
    g.add_argument("--score", action="store_true")
    ap.add_argument("--skeptic-ready", default="")
    a = ap.parse_args()
    feats = json.load(open(os.path.join(O.OUT_DIR, "features.json")))
    pop, st = population(feats)
    print(f"population (G1 known, spread <= 0.05%, size >= 1): {len(pop)}; drops {dict(st)}", flush=True)
    if a.fetch_trades:
        cnt = json.load(open(os.path.join(OUT_DIR, "count.json")))
        if not all(cnt["halves"][h]["fetch_worthwhile"] for h in ("A", "B")):
            raise SystemExit("count step: projected per-fill MDE > 5 bp in a half; the fetch is not run (prereg note)")
        res = fetch_all(pop, O.data_client(), log=lambda x: print(x, flush=True))
        print(f"trade fetch done: {dict(res)}")
        return
    if not a.score:
        cnt = count_step(pop)
        for h, c in cnt.items():
            print(f"  half {h}: R_G1 signals {c['R_G1_signals']}, R_OUT {c['R_OUT_signals']}, days {c['days']}, projected "
                  f"per-fill MDE {'-' if c['projected_per_fill_mde'] is None else round(c['projected_per_fill_mde'] * 1e4, 1)} "
                  f"bp -> fetch {'yes' if c['fetch_worthwhile'] else 'NO'}")
        print(write("count.json", {"module_sha256": module_shas(), "population": len(pop), "drops": dict(st),
                                   "halves": cnt}))
        return
    if not a.skeptic_ready:
        raise SystemExit("--score needs --skeptic-ready")
    cnt = json.load(open(os.path.join(OUT_DIR, "count.json")))
    if cnt.get("module_sha256") != module_shas():
        raise SystemExit("count.json was written by different code; rerun the count step")
    rows, drops, info_rows = [], Counter(), defaultdict(list)
    for e in pop:
        key = O.entry_key(e)
        tick = load_pkl(os.path.join(O.TICK_DIR, key + ".pkl"))
        oq = load_pkl(os.path.join(O.OUTCOME_DIR, key + ".pkl"))
        tr = load_pkl(os.path.join(TRADE_DIR, key + ".pkl"))
        if oq is None or tr is None or not tr:
            drops["not_fetched"] += 1                                   # amended_2 (d): an empty file is a fetch fault
            drops[f"not_fetched_{e['g1']}"] += 1
            continue
        T = O.decision_ts(e["day"], e["m"])
        q_feat, q_out = entry_quote(tick["quotes"], T), O.last_quote(O.clean_quotes(oq), T + 1, READ_MAX_AGE)
        if q_out is None or (round(q_feat[1], 4), round(q_feat[2], 4)) != (round(q_out[1], 4), round(q_out[2], 4)):
            drops["entry_quote_mismatch"] += 1                          # amended_2 (c)
            drops[f"entry_quote_mismatch_{e['g1']}"] += 1
        r = simulate(e, tick["quotes"], oq, tr)
        if r["status"] != "ok":
            drops[r["status"]] += 1
            drops[f"{r['status']}_{e['g1']}"] += 1
            continue
        rows.append({**e, **r})
        for name, kw in (("W10", {"window": 10}), ("W60", {"window": 60}), ("mid limit", {"limit_at": "mid"}),
                         ("lit-only fills", {"lit_only": True}), ("prints >= 1c below", {"min_below": 0.01})):
            ri = simulate(e, tick["quotes"], oq, tr, **kw)
            if ri["status"] == "ok":
                info_rows[name].append({**e, **ri})
    n_pop = len(pop)
    n_g = {g_: max(1, sum(1 for e in pop if e["g1"] == g_)) for g_ in ("IN", "OUT")}
    for what in ("no_exit_quote", "not_fetched", "entry_quote_mismatch"):     # amended (4), amended_2 (c)(d)
        share = {g_: drops[f"{what}_{g_}"] / n_g[g_] for g_ in ("IN", "OUT")}
        if drops[what] > DROP_MAX * n_pop or abs(share["IN"] - share["OUT"]) > DROP_GAP_MAX:
            raise SystemExit(f"gate {what}: {drops[what]} of {n_pop}; IN {share['IN']:.3f} vs OUT {share['OUT']:.3f}")
    graded = {h: grade_half(rows, h) for h in ("A", "B")}
    halves = {h: half_verdict(graded[h]) for h in ("A", "B")}
    final = verdict([halves["A"], halves["B"]], [graded["A"]["per_fill_ub"], graded["B"]["per_fill_ub"]],
                    [graded["A"]["fill_days"], graded["B"]["fill_days"]])
    info = {name: {h: {k: grade_half(rs, h)[k] for k in ("fill_rate", "per_signal", "per_fill", "gap")} for h in ("A", "B")}
            for name, rs in info_rows.items()}
    res = {"verdict": final, "halves": halves, "graded": graded, "drops": dict(drops), "info": info,
           "skeptic_ready": a.skeptic_ready, "module_sha256": module_shas()}
    write("result.json", res)
    print(json.dumps({"verdict": final, "halves": halves, "graded": graded}, indent=1, default=str))


if __name__ == "__main__":
    main()
