#!/usr/bin/env python3
"""Sweeps and blocks on the tape. Spec: docs/studies/sweep_block_prereg.json (as amended).

At a desk-style PRE onset (T = the start of decision minute m), does net BUY intermarket-sweep volume (H1, beyond plain
signed flow) or net BUY block prints >= $100k (H2, within notional strata) in [T - 60 s, T) predict the 3-minute mid
return by >= +5 bp?

  --sample           recompute the pilot's PRE-onset pool, apply the $20 floor (IEX close of m - 1), drop onsets whose
                     windows overlap a pilot entry (same name-day, |m - m_pilot| <= 6), draw 16,000 -> sample.json
  --prestep          50 entries: ISO and block signing diagnostics (amended (4)-(5)); no feature-outcome link
  --fetch-features   SIP trades [T - 60 s, T) with exchange, quotes [T - 120 s, T + 1 s]; paged, cached, resumable
  (default)          count step: groups, strata, floors, projected MDE from control-minute bar returns -> count.json
  --fetch-outcomes   one quote window [T + 1 - 30 s, T + 301 s] per entry (refuses before a count step that cleared a
                     hypothesis)
  --score            verdicts; refuses without --skeptic-ready and matching module shas
Data-only Alpaca account; paced like the pilot.
"""
from __future__ import annotations

import argparse
import bisect
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import order_flow_pilot as OF  # noqa: E402

H, S, E = OF.H, OF.S, OF.E
OUT_DIR = os.path.join(E.ROOT, "ai_reports", "sweep_block")
TICK_DIR, OUTCOME_DIR = os.path.join(OUT_DIR, "ticks"), os.path.join(OUT_DIR, "outcomes")
SAMPLE_N, SAMPLE_TAG, PRESTEP_N, PRESTEP_TAG = 16_000, "sweep-block", 50, "sweep-block-prestep"
PLACEBO_TAG, CTRL_TAG = "sweep-block-placebo", "sweep-block-ctrl"
MIN_PX, OVERLAP_MIN = 20.0, 6
BLOCK_USD, TRF, TRF_LATE_S = 100_000.0, "D", 10.0
ISO = "F"
PRIMARY_H, HOLD = 180, 3
LIFT_BAR, COST_FLOOR, PLACEBO_MAX = 0.0005, 0.0005, 0.00025
N_TESTS = 2
FLOORS = {"H1": (1000, 20), "H2": (300, 20)}
MIN_CELL = 2


def write(name: str, obj) -> str:
    os.makedirs(OUT_DIR, exist_ok=True)
    p = os.path.join(OUT_DIR, name)
    json.dump(obj, open(p + ".tmp", "w"), indent=1, default=str)
    os.replace(p + ".tmp", p)
    return p


def jload(name: str, default=None):
    p = os.path.join(OUT_DIR, name)
    return json.load(open(p)) if os.path.exists(p) else default


# ── sample ───────────────────────────────────────────────────────────────────

def iex_close_before(rows, m: int) -> float | None:
    """The IEX close of the last bar at or before minute m - 1 (known at T)."""
    best = None
    for x in rows:
        if int(x[0]) <= m - 1:
            best = float(x[4])
        else:
            break
    return best


def fresh_pool(onsets: list[tuple], pilot: list[dict]) -> list[tuple]:
    """amended (8): drop onsets within OVERLAP_MIN minutes of a pilot entry on the same name-day."""
    pm = defaultdict(list)
    for e in pilot:
        pm[(e["day"], e["sym"])].append(e["m"])
    return [o for o in onsets if all(abs(o[2] - x) > OVERLAP_MIN for x in pm.get((o[0], o[1]), ()))]


def price_ok(pool: list[tuple]) -> list[tuple]:
    """amended (7): the $20 floor before the draw, on the IEX close of m - 1."""
    by_day = defaultdict(list)
    for o in pool:
        by_day[o[0]].append(o)
    out = []
    for day, os_ in sorted(by_day.items()):
        iex = H.load_raw("iex", day)
        for d, s, m in os_:
            px = iex_close_before(iex[s], m) if s in iex else None
            if px is not None and px >= MIN_PX:
                out.append((d, s, m))
    return out


# ── ticks ────────────────────────────────────────────────────────────────────

def compact(trades: list, quotes: list) -> dict:
    """trades (ts, price, size, codes, tape, exchange), quotes (ts, bid, ask, bid_size, ask_size)."""
    return {"trades": [(OF._ts(t), float(t.price), float(t.size or 0), OF._codes(t), str(getattr(t, "tape", "") or ""),
                        str(getattr(t, "exchange", "") or "")) for t in trades],
            "quotes": [(OF._ts(q), float(q.bid_price or 0), float(q.ask_price or 0), float(q.bid_size or 0),
                        float(q.ask_size or 0)) for q in quotes], "paged": True}


def fetch_features(entries: list[dict], cl, log=print) -> Counter:
    os.makedirs(TICK_DIR, exist_ok=True)
    st = Counter()
    for i, e in enumerate(entries):
        path = os.path.join(TICK_DIR, OF.entry_key(e) + ".pkl")
        if os.path.exists(path):
            st["cached"] += 1
            continue
        st["attempted"] += 1
        try:
            tr, qu = OF.fetch_entry(cl, e["sym"], OF.decision_ts(e["day"], e["m"]), limit=None)
        except RuntimeError as ex:
            st["failed"] += 1
            log(f"  FAILED {OF.entry_key(e)}: {ex}")
            if st["attempted"] >= OF.FAIL_MIN_ATTEMPTS and st["failed"] / st["attempted"] > OF.FAIL_ABORT:
                log("ABORT: too many failures")
                st["aborted"] = 1
                return st
            continue
        pickle.dump(compact(tr, qu), open(path + ".tmp", "wb"))
        os.replace(path + ".tmp", path)
        st["fetched"] += 1
        if st["fetched"] % 500 == 0:
            log(f"  {i + 1}/{len(entries)} fetched {st['fetched']} cached {st['cached']} failed {st['failed']}")
    return st


# ── features (pure) ──────────────────────────────────────────────────────────

def sign_trades(trades: list, qs: list, T: float) -> list[tuple]:
    """The pilot's Lee-Ready, except 'F' prints are signed against the last clean quote STRICTLY BEFORE the trade
    (amended (4)). Returns (ts, price, size, sign, codes, exchange, by_tick)."""
    qts = [q[0] for q in qs]
    out, prev_px, prev_sign = [], None, None
    for tr in sorted(trades, key=lambda t: t[0]):
        ts, px, size, codes = tr[0], tr[1], tr[2], tr[3]
        exch = tr[5] if len(tr) > 5 else ""
        if not (T - OF.WINDOW_S <= ts < T) or not codes or not all(c in OF.KEEP_CODES for c in codes):
            continue
        i = (bisect.bisect_left(qts, ts) if ISO in codes else bisect.bisect_right(qts, ts)) - 1
        sign, by_tick = None, False
        if i >= 0 and ts - qts[i] <= OF.SIGN_MAX_AGE:
            mid = (qs[i][1] + qs[i][2]) / 2
            if px > mid:
                sign = 1
            elif px < mid:
                sign = -1
            elif prev_px is not None:
                sign = 1 if px > prev_px else -1 if px < prev_px else prev_sign
                by_tick = True
        if sign is not None:
            out.append((ts, px, size, sign, codes, exch, by_tick))
            prev_sign = sign
        prev_px = px
    return out


def features(tick: dict, T: float) -> dict:
    qs = OF.clean_quotes(tick["quotes"])
    sg = sign_trades(tick["trades"], qs, T)
    vol = sum(x[2] for x in sg)
    seen = [x for x in sg if not (x[5] == TRF and x[0] >= T - TRF_LATE_S)]     # amended (5): late TRF not visible at T
    notl = sum(x[1] * x[2] for x in seen)
    ok = len(sg) >= OF.MIN_SIGNED and vol > 0
    blocks = [x for x in seen if x[1] * x[2] >= BLOCK_USD]
    q = OF.last_quote(qs, T, OF.READ_MAX_AGE)
    return {"ofi60": sum(x[2] * x[3] for x in sg) / vol if ok else None,
            "iso60": sum(x[2] * x[3] for x in sg if ISO in x[4]) / vol if ok else None,
            "notional60": notl if ok else None,
            "blk60": (sum(x[1] * x[2] * x[3] for x in blocks) / notl) if ok and notl > 0 else None,
            "n_blocks": len(blocks), "mid_T": ((q[1] + q[2]) / 2) if q else None,
            "entry_quote": OF.last_quote(qs, T + 1, OF.READ_MAX_AGE) is not None}


def tercile_cuts(xs: list[float]) -> tuple | None:
    s = sorted(x for x in xs if x is not None)
    return (s[len(s) // 3], s[(2 * len(s)) // 3]) if len(s) >= 3 else None


def tercile(x: float | None, cuts: tuple | None) -> int | None:
    if x is None or cuts is None:
        return None
    return 0 if x < cuts[0] else 1 if x < cuts[1] else 2


def assign(rows: list[dict]) -> dict:
    """Cuts on half A (outcome-blind): ISO60 top tercile with tie_u, OFI60 and notional terciles."""
    a = [r for r in rows if r["half"] == "A"]
    cuts = {"iso60": OF.top_cut([(r["iso60"], OF.tie_u(r)) for r in a if r["iso60"] is not None]),
            "ofi60": tercile_cuts([r["ofi60"] for r in a]), "notional60": tercile_cuts([r["notional60"] for r in a])}
    for r in rows:
        iso_in = None if r["iso60"] is None or cuts["iso60"] is None else \
            ("IN" if (r["iso60"], OF.tie_u(r)) >= tuple(cuts["iso60"]) else "OUT")
        blk = None if r["blk60"] is None else ("IN" if r["blk60"] > 0 else "OUT")
        r["g"] = {"H1": iso_in, "H2": blk}
        r["blk_side"] = None if not r["n_blocks"] or r["blk60"] is None else \
            ("BUY" if r["blk60"] > 0 else "SELL" if r["blk60"] < 0 else None)
        r["ofi_t"], r["notl_t"] = tercile(r["ofi60"], cuts["ofi60"]), tercile(r["notional60"], cuts["notional60"])
    return cuts


# ── statistics (pure) ────────────────────────────────────────────────────────

def t_crit(days: int) -> float | None:
    return S.t_ppf(1 - 0.025 / N_TESTS, days - 1) if days >= 2 else None


def day_stats(vals: dict) -> tuple:
    xs = [v for v in vals.values() if v is not None]
    if len(xs) < 2:
        return (xs[0] if xs else None), None, len(xs), None
    mu, sd = statistics.mean(xs), statistics.stdev(xs)
    se = sd / math.sqrt(len(xs))
    return mu, (mu / se if se > 0 else None), len(xs), (mu + S.t_ppf(0.975, len(xs) - 1) * se if se > 0 else None)


def strat_lift(rows: list[dict], hyp: str, strat: str | None, key: str, pos: str = "IN", neg: str = "OUT",
               label=None) -> tuple:
    """Per day: within each stratum (cells with >= 2 a side) mean(pos) - mean(neg); the day value is the mean over the
    strata present; day-equal. strat None = one stratum. label(r) overrides r['g'][hyp]."""
    cell = defaultdict(lambda: defaultdict(list))
    for r in rows:
        g = label(r) if label else r["g"][hyp]
        s_ = r.get(strat) if strat else 0
        if g in (pos, neg) and s_ is not None and r.get(key) is not None:
            cell[(r["day"], s_)][g].append(r[key])
    per_day = defaultdict(list)
    for (d, _s), v in cell.items():
        if len(v[pos]) >= MIN_CELL and len(v[neg]) >= MIN_CELL:
            per_day[d].append(statistics.mean(v[pos]) - statistics.mean(v[neg]))
    return day_stats({d: statistics.mean(v) for d, v in per_day.items()})


PRIMARY_STRAT = {"H1": "ofi_t", "H2": "notl_t"}


def primary(rows: list[dict], hyp: str, key: str = "y") -> tuple:
    return strat_lift(rows, hyp, PRIMARY_STRAT[hyp], key)


def nameday_lift(rows: list[dict], hyp: str) -> float | None:
    nd = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if r["g"][hyp] and r.get("y") is not None:
            nd[(r["day"], r["sym"])][r["g"][hyp]].append(r["y"])
    xs = [statistics.mean(v["IN"]) - statistics.mean(v["OUT"]) for v in nd.values() if v["IN"] and v["OUT"]]
    return statistics.mean(xs) if xs else None


def cost_check(rows: list[dict], hyp: str) -> float | None:
    """amended (6): IN mean of (gross - max(5 bp, the entry's own quoted spread)), day-equal."""
    by = defaultdict(list)
    for r in rows:
        if r["g"][hyp] == "IN" and r.get("y") is not None and r.get("spread") is not None:
            by[r["day"]].append(r["y"] - max(COST_FLOOR, r["spread"]))
    return statistics.mean(statistics.mean(v) for v in by.values()) if by else None


def drop3(rows: list[dict], hyp: str) -> tuple:
    mu_by_day = {}
    for d in {r["day"] for r in rows}:
        mu_by_day[d] = primary([r for r in rows if r["day"] == d], hyp)[0]
    top_d = set(sorted((d for d, v in mu_by_day.items() if v is not None), key=lambda d: mu_by_day[d], reverse=True)[:3])
    out_mean = defaultdict(list)
    for r in rows:
        if r["g"][hyp] == "OUT" and r.get("y") is not None:
            out_mean[r["day"]].append(r["y"])
    om = {d: statistics.mean(v) for d, v in out_mean.items()}
    share = Counter()
    for r in rows:
        if r["g"][hyp] == "IN" and r.get("y") is not None and r["day"] in om:
            share[r["sym"]] += r["y"] - om[r["day"]]
    top_n = {s_ for s_, _ in share.most_common(3)}
    return (primary([r for r in rows if r["day"] not in top_d], hyp)[0],
            primary([r for r in rows if r["sym"] not in top_n], hyp)[0])


def grade(rows: list[dict], hyp: str) -> dict:
    mu, t, days, ub = primary(rows, hyp)
    n_in = sum(1 for r in rows if r["g"][hyp] == "IN" and r.get("y") is not None)
    bar_entry = primary(rows, hyp, "bar_y")[0]
    bar_plc = primary(rows, hyp, "placebo_y")[0]
    dd, dn = drop3(rows, hyp)
    g = {"lift": mu, "t": t, "days": days, "ub": ub, "n_in": n_in, "t_crit": t_crit(days),
         "cost_check": cost_check(rows, hyp), "nameday": nameday_lift(rows, hyp),
         "latency_lag1": primary(rows, hyp, "lag1_y")[0], "bar_entry_lift": bar_entry, "bar_placebo_lift": bar_plc,
         "drop_days": dd, "drop_names": dn, "unstratified": strat_lift(rows, hyp, None, "y")[:3]}
    g["within_ofi"] = [strat_lift([r for r in rows if r["ofi_t"] == k], hyp, None, "y")[0] for k in range(3)]
    if hyp == "H1":
        g["within_notional"] = [strat_lift([r for r in rows if r["notl_t"] == k], hyp, None, "y")[0] for k in range(3)]
    else:
        g["buy_vs_sell_blocks"] = strat_lift(rows, hyp, None, "y", "BUY", "SELL", label=lambda r: r["blk_side"])[0]
    return g


def half_verdict(g: dict, hyp: str) -> str:
    if g["t"] is not None and g["t"] <= -2:
        return "FAIL"
    if g["n_in"] < FLOORS[hyp][0] or g["days"] < FLOORS[hyp][1] or g["t"] is None:
        return "UNDERPOWERED"
    guards = g["within_ofi"] + (g["within_notional"] if hyp == "H1" else [g["buy_vs_sell_blocks"]])
    ok = (g["lift"] >= LIFT_BAR and g["t"] >= g["t_crit"]
          and g["cost_check"] is not None and g["cost_check"] > 0
          and all(x is not None and x >= 0 for x in guards)
          and g["latency_lag1"] is not None and g["latency_lag1"] >= 0
          and g["bar_placebo_lift"] is not None and g["bar_placebo_lift"] < PLACEBO_MAX
          and g["bar_entry_lift"] is not None and g["bar_entry_lift"] - g["bar_placebo_lift"] >= LIFT_BAR
          and g["nameday"] is not None and g["nameday"] > 0
          and g["drop_days"] is not None and g["drop_days"] >= 0 and g["drop_names"] is not None and g["drop_names"] >= 0)
    return "PASS" if ok else "NOT_PASSED"


def final(halves: list[str], ubs: list, days: list[int]) -> str:
    if "FAIL" in halves:
        return "FAIL"
    if all(h == "PASS" for h in halves):
        return "PASS_STAGE1 (needs the warehouse_confirm run)"
    if all(n >= 2 for n in days) and all(u is not None and u < LIFT_BAR for u in ubs):
        return "FAIL"
    return "UNDERPOWERED"


# ── bars: control, placebo, entry, lag-1 ─────────────────────────────────────

def control_minute(sym: str, day: str, m: int, sip: dict) -> int | None:
    """amended (3): m + 4 .. m + 60 (never inside the outcome window)."""
    elig = [c for c in range(m + 4, m + S.CTRL_SPAN + 1) if c + HOLD - 1 < S.RTH_END and c in sip and c + HOLD - 1 in sip]
    if not elig:
        return None
    return random.Random(H._seed(sym, day, m, CTRL_TAG)).choice(elig)


def placebo_minute(sym: str, day: str, m: int, sip: dict) -> int | None:
    """amended (3) / amended_2 (2): same clock hour, outside [m - 3, m + 4] (no overlap with the feature bar m - 1 or
    the entry bars), bars at k and k + 2."""
    h0 = m // 60 * 60
    elig = [k for k in range(h0, h0 + 60) if not (m - 3 <= k <= m + 4) and k + HOLD - 1 < S.RTH_END
            and k in sip and k + HOLD - 1 in sip]
    if not elig:
        return None
    return random.Random(H._seed(sym, day, m, PLACEBO_TAG)).choice(elig)


def bar_ret(sip: dict, m: int | None, lag: int = 0) -> float | None:
    return S.net(sip, m, hold=HOLD, lag=lag, cost=0.0) if m is not None else None


# ── rows ─────────────────────────────────────────────────────────────────────

def build_rows(entries: list[dict], with_outcomes: bool) -> tuple[list[dict], Counter]:
    st, rows, by_day = Counter(), [], defaultdict(list)
    for e in entries:
        by_day[e["day"]].append(e)
    for day in sorted(by_day):
        raw = H.load_raw("sip", day)
        for e in by_day[day]:
            path = os.path.join(TICK_DIR, OF.entry_key(e) + ".pkl")
            if not os.path.exists(path):
                st["not_fetched"] += 1
                continue
            T = OF.decision_ts(day, e["m"])
            f = features(pickle.load(open(path, "rb")), T)
            if not f["entry_quote"] or f["mid_T"] is None:
                st["no_quote_at_T"] += 1
                continue
            if f["mid_T"] < MIN_PX:
                st["mid_below_20_at_T"] += 1
                continue
            sip = H.sip_oc(raw[e["sym"]]) if e["sym"] in raw else {}
            r = {**e, **f, "ctrl": bar_ret(sip, control_minute(e["sym"], day, e["m"], sip))}
            if with_outcomes:
                op = os.path.join(OUTCOME_DIR, OF.entry_key(e) + ".pkl")
                o = OF.outcomes_for(pickle.load(open(op, "rb")), T) if os.path.exists(op) else {"entry": False}
                r["y"] = o.get(f"gross{PRIMARY_H}")
                r["spread"] = o.get("spread")
                r["bar_y"] = bar_ret(sip, e["m"])
                r["lag1_y"] = bar_ret(sip, e["m"], lag=1)
                r["placebo_y"] = bar_ret(sip, placebo_minute(e["sym"], day, e["m"], sip))
                st["no_outcome"] += r["y"] is None
            rows.append(r)
    return rows, st


def count_step(rows: list[dict]) -> dict:
    out = {}
    for hyp in ("H1", "H2"):
        for half in ("A", "B"):
            rs = [r for r in rows if r["half"] == half]
            n_in = sum(1 for r in rs if r["g"][hyp] == "IN")
            mu, t, days, ub = strat_lift(rs, hyp, PRIMARY_STRAT[hyp], "ctrl")
            sd = None
            if days >= 2 and t:
                sd = abs(mu / t)                    # the day-clustered SE of the statistic on control returns
            tc = t_crit(days)
            mde = (tc + 0.84) * sd if (sd and tc) else None
            out[f"{hyp} {half}"] = {"n_in": n_in, "n_out": sum(1 for r in rs if r["g"][hyp] == "OUT"), "days": days,
                                    "projected_mde": mde,
                                    "power_ok": mde is not None and mde <= LIFT_BAR and n_in >= FLOORS[hyp][0]
                                    and days >= FLOORS[hyp][1]}
    return out


def module_shas() -> dict:
    m = OF.module_shas()
    m["sweep_block"] = hashlib.sha256(open(os.path.abspath(__file__), "rb").read()).hexdigest()
    return m


# ── pre-step (outcome-blind diagnostics) ─────────────────────────────────────

def prestep_diag(tick: dict, T: float) -> dict:
    qs = OF.clean_quotes(tick["quotes"])
    qts = [q[0] for q in qs]
    qset = set(qts)
    d = Counter()
    for tr in tick["trades"]:
        ts, px, size, codes = tr[0], tr[1], tr[2], tr[3]
        if not (T - OF.WINDOW_S <= ts < T):
            continue
        i = bisect.bisect_left(qts, ts) - 1
        if ISO in codes:
            d["iso"] += 1
            d["iso_same_ts_as_quote"] += ts in qset
            if i >= 0:
                b, a = qs[i][1], qs[i][2]
                d["iso_through_ask" if px > a else "iso_at_ask" if px == a else "iso_through_bid" if px < b
                  else "iso_at_bid" if px == b else "iso_inside"] += 1
        if px * size >= BLOCK_USD:
            d["blocks"] += 1
            d["blocks_trf"] += (len(tr) > 5 and tr[5] == TRF)
    for x in sign_trades(tick["trades"], qs, T):
        if ISO in x[4]:
            d["iso_signed"] += 1
            d["iso_tick_signed"] += x[6]
        if x[1] * x[2] >= BLOCK_USD:
            d["blocks_signed"] += 1
            d["blocks_tick_signed"] += x[6]
    return dict(d)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    for f in ("--sample", "--prestep", "--fetch-features", "--fetch-outcomes", "--score"):
        g.add_argument(f, action="store_true")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--skeptic-ready", default="")
    a = ap.parse_args()
    if a.sample:
        days = H.warehouse_days()
        ks = list(range(H.LOOKBACK, len(days)))
        onsets = []
        with ProcessPoolExecutor(max_workers=a.workers) as ex:
            for got in ex.map(OF.day_onsets, [(k, days) for k in ks], chunksize=max(1, len(ks) // a.workers)):
                onsets += got
        pilot = json.load(open(os.path.join(OF.OUT_DIR, "sample.json")))
        if len(onsets) != pilot["onsets"]:
            raise SystemExit(f"onset pool {len(onsets)} != the pilot's {pilot['onsets']}")
        fresh = fresh_pool(onsets, pilot["entries"])
        pool = price_ok(fresh)
        sample = OF.draw(pool, SAMPLE_N, SAMPLE_TAG)
        print(f"onsets {len(onsets)}; fresh {len(fresh)}; $20+ {len(pool)}; sample {len(sample)} "
              f"{dict(Counter(e['half'] for e in sample))}")
        print(write("sample.json", {"onsets": len(onsets), "fresh": len(fresh), "price_ok": len(pool),
                                    "seed_tag": SAMPLE_TAG, "entries": sample}))
        return
    smp = jload("sample.json")["entries"]
    if a.prestep:
        rng = random.Random(int(hashlib.sha256(PRESTEP_TAG.encode()).hexdigest()[:16], 16))
        cl, tot, per = OF.data_client(), Counter(), []
        for e in sorted(rng.sample(smp, PRESTEP_N), key=lambda x: (x["day"], x["sym"])):
            T = OF.decision_ts(e["day"], e["m"])
            tr, qu = OF.fetch_entry(cl, e["sym"], T, limit=None)
            dg = prestep_diag(compact(tr, qu), T)
            tot.update(dg)
            per.append({**e, **dg})
        res = {"total": dict(tot), "entries": per,
               "iso_tick_signed_share": tot["iso_tick_signed"] / max(1, tot["iso_signed"]),
               "iso_same_ts_share": tot["iso_same_ts_as_quote"] / max(1, tot["iso"]),
               "blocks_trf_share": tot["blocks_trf"] / max(1, tot["blocks"]),
               "blocks_tick_signed_share": tot["blocks_tick_signed"] / max(1, tot["blocks_signed"])}
        print(json.dumps({k: v for k, v in res.items() if k != "entries"}, indent=1))
        print(write("prestep.json", res))
        return
    if a.fetch_features:
        print(fetch_features(smp, OF.data_client()))
        return
    if a.fetch_outcomes:
        cnt = jload("count.json")
        if not cnt or cnt.get("module_sha256") != module_shas() or not any(
                cnt["count"][f"{h} A"]["power_ok"] and cnt["count"][f"{h} B"]["power_ok"] for h in ("H1", "H2")):
            raise SystemExit("no hypothesis cleared the count step (or count.json is from different code)")
        print(OF.fetch_outcomes(smp, OF.data_client(), out_dir=OUTCOME_DIR))
        return
    rows, st = build_rows(smp, with_outcomes=a.score)
    cuts = assign(rows)
    if st["not_fetched"] > 0.02 * len(smp):
        raise SystemExit(f"{st['not_fetched']} of {len(smp)} entries not fetched (> 2%); finish --fetch-features")
    keys = sorted(OF.entry_key(r) for r in rows)
    if not a.score:
        cnt = {"count": count_step(rows), "cuts": cuts, "drops": dict(st), "rows": len(rows), "row_keys": keys,
               "module_sha256": module_shas()}
        print(json.dumps(cnt, indent=1, default=str))
        print(write("count.json", cnt))
        return
    if not a.skeptic_ready:
        raise SystemExit("--score needs --skeptic-ready")
    cnt = jload("count.json")
    if not cnt or cnt.get("module_sha256") != module_shas():
        raise SystemExit("count.json missing or written by different code")
    if json.loads(json.dumps(cuts)) != cnt["cuts"] or keys != cnt["row_keys"]:
        raise SystemExit("rows or cuts differ from the count step")
    res = {"verdicts": {}, "halves": {}, "graded": {}, "drops": dict(st)}
    for hyp in ("H1", "H2"):
        if not (cnt["count"][f"{hyp} A"]["power_ok"] and cnt["count"][f"{hyp} B"]["power_ok"]):
            res["verdicts"][hyp] = "UNDERPOWERED (count step; not scored)"
            continue
        gs = {h: grade([r for r in rows if r["half"] == h], hyp) for h in ("A", "B")}
        res["graded"][hyp] = gs
        res["halves"][hyp] = {h: half_verdict(gs[h], hyp) for h in ("A", "B")}
        res["verdicts"][hyp] = final(list(res["halves"][hyp].values()), [gs[h]["ub"] for h in ("A", "B")],
                                     [gs[h]["days"] for h in ("A", "B")])
    res["skeptic_ready"], res["module_sha256"] = a.skeptic_ready, module_shas()
    write("result.json", res)
    print(json.dumps(res, indent=1, default=str))


if __name__ == "__main__":
    main()
