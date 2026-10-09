#!/usr/bin/env python3
"""Day hold (docs/studies/day_hold_prereg.json, amended df0ef45): 2-3 morning picks from the desk's own sources,
held 10:30 -> 15:54 bar close with only a -3% loss limit and red-knife exits; vs k random names from the same lists.

Commands (on the mini, from the repo root, .venv/bin/python):
  pilot   fetch (outside 09:00-16:30 ET) and score the pilot sessions 2026-09-08 .. 2026-10-08 -> WORK/pilot.json
  forward the forward run, sessions 2026-10-09 .. today (nightly, after 16:30)  -> WORK/forward.json
  long    the 10-year daily skeleton on the lh_cache panel (no network)          -> WORK/long.json
Bars: SIP 1-minute RAW via name_history.StudyMarket (shared cache); Alpaca stamps a bar at its START.
"""
from __future__ import annotations

import collections
import hashlib
import json
import math
import os
import random
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path[:0] = [HERE, ROOT]
import bars_structure as BS  # noqa: E402
import bro_sr_wr as BRO  # noqa: E402
import name_history as NH  # noqa: E402

WORK = os.environ.get("DAY_HOLD_WORK") or os.path.join(ROOT, "ai_reports", "day_hold")
PILOT_LO, PILOT_HI = "2026-09-08", "2026-10-08"
NH_HI = "2026-10-07"                      # the history cache's month keys
SOURCES = {"trending": "trending", "agy": "AI", "xai": "AI", "bb_live": "AI", "movers": "movers",
           "momentum": "movers", "tight": "movers"}
MIN_PX, MIN_DV, CHG_LO, CHG_HI, GAP_MIN, HOLD_PCT = 10.0, 5e6, 2.0, 10.0, -1.0, 0.99
STOP, KNIFE_DROP, TOP_K, DRAWS = 0.97, 0.98, 3, 200
COSTS = (10.0, 20.0)
T_DECIDE, T_ENTRY, T_EXIT = 10 * 60 + 29, 10 * 60 + 30, 15 * 60 + 54     # bar START minutes (ET)

RESOLUTIONS = {
    "P_missing_1029": "if no bar is stamped 10:29, P = close of the last bar stamped 09:30..10:29 (counted)",
    "entry_missing_1030": "if no bar is stamped 10:30, entry = open of the first bar stamped 10:31..10:34, else the name is unpriced (counted, dropped)",
    "exit_missing_1554": "if no bar is stamped 15:54, TIME exit = close of the last bar stamped <= 15:54",
    "knife_b_window": "knife (b) needs 5 previous bars (from the entry bar on); the 'previous 5 bars' are the 5 bars before the current one",
    "corp_action": "a corporate action on D = adjusted/raw daily close factor on D differs from D-1 by > 0.01%",
    "control_mean": "C1 = mean over 200 draws of the equal-weight k-name portfolio",
    "t": "plain t across days (mean / (sd / sqrt(n)))",
    "common_stock": "fund/ETP names excluded with lh_fetch.py's FUNDISH rule on Alpaca asset names (lh_cache dump, then the desk's cached names); unknown names kept and counted",
}


def P_(*a):
    print(*a, flush=True)


def tstat(x):
    x = [v for v in x if v is not None and math.isfinite(v)]
    n = len(x)
    if n < 3:
        return {"n": n, "mean": None, "t": None, "se": None}
    m = sum(x) / n
    sd = math.sqrt(sum((v - m) ** 2 for v in x) / (n - 1))
    se = sd / math.sqrt(n) if sd > 0 else float("nan")
    return {"n": n, "mean": round(m, 2), "se": round(se, 2), "t": round(m / se, 2) if se and se == se else None}


# ------------------------------------------------------------------ pure pieces (tested)
def morning(bars):
    """bars: [[start, o, h, l, c, v]] of one day. -> features from bars stamped 09:30..10:29 or None."""
    mb = [b for b in bars if 570 <= BS.et_hm(b[0]) <= T_DECIDE]
    if not mb or BS.et_hm(mb[0][0]) != 570:
        return None
    last = mb[-1]
    return {"O": mb[0][1], "MH": max(b[2] for b in mb), "ML": min(b[3] for b in mb),
            "P": last[4], "P_fallback": BS.et_hm(last[0]) != T_DECIDE, "dv": sum(b[4] * b[5] for b in mb)}


def qualifies(f, pc, above_sma):
    if f is None or not pc:
        return False
    chg = (f["P"] / pc - 1) * 100
    gap = (f["O"] / pc - 1) * 100
    return (f["P"] >= MIN_PX and above_sma and gap >= GAP_MIN and CHG_LO <= chg <= CHG_HI
            and f["P"] >= f["O"] and f["P"] >= HOLD_PCT * f["MH"] and f["dv"] >= MIN_DV)


def simulate(bars, ml, knife=True, partial=False):
    """Hold from the open of the bar stamped 10:30 (or 10:31..10:34). -> (gross_bp, reason) or None."""
    after = [b for b in bars if T_ENTRY <= BS.et_hm(b[0]) <= T_EXIT]
    if not after or BS.et_hm(after[0][0]) > T_ENTRY + 4:
        return None
    entry = after[0][1]
    stop = entry * STOP
    half_done, half_px = False, None
    closes = []

    def ret(px, why):
        r = px / entry - 1
        if half_done:
            r = 0.5 * (half_px / entry - 1) + 0.5 * r
        return 1e4 * r, why

    for i, b in enumerate(after):
        st, o, h, l, c = b[0], b[1], b[2], b[3], b[4]
        if l <= stop:
            return ret(min(stop, o), "stop")
        if partial and not half_done and h >= entry * 1.05:
            half_done, half_px = True, entry * 1.05
        if knife and i + 1 < len(after):
            nxt_open = after[i + 1][1]
            if c < ml:
                return ret(nxt_open, "knife_ml")
            if len(closes) >= 5 and c <= KNIFE_DROP * max(closes[-5:]):
                return ret(nxt_open, "knife_drop")
        closes.append(c)
    return ret(after[-1][4], "time")


def rank_key(c):
    return (-c["families"], -c["dv"])


# ------------------------------------------------------------------ data
# lh_fetch.py's fund-name rule (the overnight universe's common-stock filter), applied to Alpaca asset names
FUNDISH = re.compile(r"\b(ETF|ETN|FUND|ISHARES|SPDR|PROSHARES|DIREXION|INVESCO|VANGUARD|INDEX|PORTFOLIO|TREASURY|BOND|MUNICIPAL|"
                     r"LEVERAGED|INVERSE|2X|3X|BULL|BEAR|WARRANTS?|RIGHTS?|UNITS?|PREFERRED|DEPOSITARY SHARES? REPR|NOTES?|DEBENTURES?|"
                     r"ACQUISITION CORP|ACQUISITION CO|SPAC|CAPITAL TRUST|TRUST UNITS?|ROYALTY TRUST|CLOSED.END|MUTUAL)\b", re.I)


def asset_names():
    """{symbol: name} from the lh_cache asset dump (active + inactive) and the desk's asset-name cache."""
    names = {}
    try:
        raw = json.load(open(os.path.expanduser("~/lh_cache/assets_raw.json")))
        for st in ("active", "inactive"):
            for a in raw.get(st, []):
                names.setdefault(a["symbol"], a.get("name") or "")
    except Exception:  # noqa: BLE001
        pass
    try:
        from ticker_filters import cached_asset_name
        names["__cached__"] = cached_asset_name
    except Exception:  # noqa: BLE001
        pass
    return names


def is_fund(sym, names):
    nm = names.get(sym)
    if not nm and callable(names.get("__cached__")):
        nm = names["__cached__"](sym)
    return None if not nm else bool(FUNDISH.search(nm))


def load_candidates(lo=PILOT_LO, hi=PILOT_HI, counts=None):
    from ticker_filters import is_common, is_levered_etp
    counts = counts if counts is not None else collections.Counter()
    names = asset_names()
    out = collections.defaultdict(dict)
    for line in open(os.path.join(ROOT, "ai_reports", "admit_range.jsonl")):
        try:
            r = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        d, s, src, ts = r.get("day"), r.get("symbol"), r.get("source"), r.get("ts")
        if not (d and s and ts and lo <= d <= hi) or src not in SOURCES:
            continue
        hm = BS.et_hm(float(ts))
        if BS.et_day(float(ts)) != d or not (4 * 60 <= hm <= 10 * 60 + 30):
            continue
        if not is_common(s) or is_levered_etp(s):
            continue
        fund = is_fund(s, names)
        if fund:
            counts["fund_or_etp_excluded"] += 1
            continue
        if fund is None:
            counts["asset_name_unknown_kept"] += 1
        c = out[d].setdefault(s, {"sym": s, "fams": set(), "ts": float(ts)})
        c["fams"].add(SOURCES[src])
        c["ts"] = min(c["ts"], float(ts))
    return out


def day_minutes(mkt, sym, day, counts, hi=PILOT_HI):
    month = day[:7]
    mf = mkt._minfile(sym)
    keys = [hi] if month == hi[:7] else [NH_HI, PILOT_HI, hi]     # a month file is complete only when keyed past it
    for k in keys:
        if f"{month}|{k}" in mf and (month < k[:7] or k == hi):
            rows = mf[f"{month}|{k}"]
            break
    else:
        rows = mkt.minute_month(sym, month, hi)
        counts["minute_month_fetched"] += 1
    return [r for r in rows if BS.et_day(r[0]) == day]


def pilot(lo=PILOT_LO, hi=PILOT_HI, out_name="pilot.json"):
    os.makedirs(WORK, exist_ok=True)
    counts = collections.Counter()
    cand = load_candidates(lo, hi, counts=counts)
    days = sorted(cand)
    mkt = NH.StudyMarket()
    syms = sorted({s for d in days for s in cand[d]} | {"SPY"})
    draw_lo = "2026-05-01"
    raw = mkt.daily_bars(syms, draw_lo, hi, "raw")
    adj = mkt.daily_bars(syms, draw_lo, hi, "all")
    spy_days = sorted(raw.get("SPY", {}))
    sessions = [d for d in days if d in raw.get("SPY", {})]
    counts["calendar_dropped_days"] = len(days) - len(sessions)
    desk = collections.defaultdict(float)
    for line in open(os.path.join(ROOT, "ai_reports", "outcomes.jsonl")):
        try:
            r = json.loads(line)
            desk[BS.et_day(float(r["entry_time"]))] += float(r.get("realized_pl_usd") or 0)
        except Exception:  # noqa: BLE001
            pass
    rows, aborted = [], []
    for d in sessions:
        i = spy_days.index(d)
        prev = spy_days[i - 1]
        hist = spy_days[max(0, i - 50):i]
        pool, missing = [], 0
        for s, c in cand[d].items():
            r, a = raw.get(s, {}), adj.get(s, {})
            if prev not in r or d not in r or prev not in a or d not in a:
                counts["no_daily"] += 1
                missing += 1
                continue
            f_prev, f_d = a[prev][3] / r[prev][3], a[d][3] / r[d][3]
            if abs(f_d / f_prev - 1) > 1e-4:
                counts["corp_action_D"] += 1
                continue
            try:
                bars = day_minutes(mkt, s, d, counts, hi)
            except BRO.FetchFail as e:
                if "429" in str(e):
                    raise SystemExit(f"aborted: 429 on {s} {d}")
                counts["minute_fetch_fail"] += 1
                missing += 1
                continue
            f = morning(bars)
            if f is None:
                counts["no_0930_bar"] += 1
                missing += 1
                continue
            counts["P_fallback"] += int(f["P_fallback"])
            closes = [a[x][3] for x in hist if x in a]
            above = len(closes) == 50 and a[prev][3] > sum(closes) / 50
            pool.append({"sym": s, "families": len(c["fams"]), "dv": f["dv"], "f": f, "bars": bars,
                         "q": qualifies(f, r[prev][3], above)})
        if cand[d] and missing / len(cand[d]) > 0.05:
            aborted.append((d, missing, len(cand[d])))
            continue
        picks = sorted([p for p in pool if p["q"]], key=rank_key)[:TOP_K]
        k = len(picks)
        elig = [p for p in pool if p["f"]["P"] >= MIN_PX and p["f"]["dv"] >= MIN_DV]
        spy_b = day_minutes(mkt, "SPY", d, counts, hi)
        spy = simulate(spy_b, ml=-1, knife=False)
        row = {"day": d, "k": k, "picks": [p["sym"] for p in picks], "n_cand": len(cand[d]), "n_elig": len(elig),
               "spy_bp": None if spy is None else round(spy[0] - 2.0, 2),
               "desk_bp": round(desk.get(d, 0.0) / 2000 * 1e4, 2)}
        for variant, kw in (("main", {}), ("noknife", {"knife": False}), ("partial", {"partial": True})):
            sims = [simulate(p["bars"], p["f"]["ML"], **kw) for p in picks]
            sims = [x for x in sims if x is not None]
            row[f"{variant}_gross"] = None if not picks else (sum(x[0] for x in sims) / len(sims) if sims else None)
            if variant == "main":
                row["reasons"] = [x[1] for x in sims]
                row["pick_gross"] = [round(x[0], 1) for x in sims]
        if k == 0:
            row["c1_gross"] = 0.0
        elif len(elig) >= k:
            sim_cache = {}
            tot = 0.0
            for j in range(DRAWS):
                rng = random.Random(int(hashlib.sha256(f"dayhold|{d}|{j}".encode()).hexdigest(), 16))
                ch = rng.sample(elig, k)
                vals = []
                for p in ch:
                    if p["sym"] not in sim_cache:
                        sim_cache[p["sym"]] = simulate(p["bars"], p["f"]["ML"])
                    if sim_cache[p["sym"]] is not None:
                        vals.append(sim_cache[p["sym"]][0])
                tot += sum(vals) / len(vals) if vals else 0.0
            row["c1_gross"] = tot / DRAWS
        else:
            row["c1_gross"] = None
            counts["c1_short_day"] += 1
        rows.append(row)
        mkt.flush()
    res = {"prereg": "docs/studies/day_hold_prereg.json (df0ef45)", "resolutions": RESOLUTIONS, "counts": dict(counts),
           "aborted_days": aborted, "days": rows}
    res["summary"] = summarize(rows)
    json.dump(res, open(os.path.join(WORK, out_name), "w"), indent=1, default=str)
    P_(json.dumps({k: v for k, v in res.items() if k not in ("days",)}, indent=1, default=str))


def summarize(rows):
    out = {}
    for cost in COSTS:
        def net(g, k):
            return None if g is None else (0.0 if k == 0 else g - cost)
        picks = [net(r["main_gross"], r["k"]) if r["k"] else 0.0 for r in rows]
        c1 = [net(r["c1_gross"], r["k"]) if r["c1_gross"] is not None else None for r in rows]
        diff = [p - c for p, c in zip(picks, c1) if p is not None and c is not None]
        inv = [r for r in rows if r["k"]]
        picks_inv = [r["main_gross"] - cost for r in inv if r["main_gross"] is not None]
        diff_inv = [r["main_gross"] - r["c1_gross"] for r in inv if r["main_gross"] is not None and r["c1_gross"] is not None]
        best = max(range(len(diff)), key=lambda i: diff[i]) if diff else None
        out[f"cost_{int(cost)}bp"] = {
            "picks_net_all": tstat(picks), "picks_minus_c1_all": tstat(diff),
            "picks_net_invested": tstat(picks_inv), "picks_minus_c1_invested": tstat(diff_inv),
            "picks_minus_c1_drop_best_day": tstat([v for i, v in enumerate(diff) if i != best]),
            "c1_net_all": tstat([c for c in c1 if c is not None])}
    out["info"] = {
        "spy_bp": tstat([r["spy_bp"] for r in rows]),
        "desk_bp_per_day": tstat([r["desk_bp"] for r in rows]),
        "noknife_gross_invested": tstat([r["noknife_gross"] for r in rows if r["k"] and r["noknife_gross"] is not None]),
        "partial_gross_invested": tstat([r["partial_gross"] for r in rows if r["k"] and r["partial_gross"] is not None]),
        "cash_days": sum(1 for r in rows if r["k"] == 0), "days": len(rows),
        "exit_reasons": dict(collections.Counter(x for r in rows for x in r.get("reasons", []))),
    }
    return out


# ------------------------------------------------------------------ long history (daily skeleton)
def long():
    import numpy as np
    import pandas as pd
    sys.path.insert(0, os.path.expanduser("~/lh_cache"))
    import lh_core as C
    if not os.path.exists(os.path.join(C.OUT, "panel.npz")):
        C.OUT = os.path.expanduser("~/lh_cache")
    P = C.load()
    o, c, cf, rc, adv = P["o"], P["c"], P["cf"], P["rc"], P["adv20"]
    dates = pd.DatetimeIndex(P["dates"])
    T, N = c.shape
    sma = pd.DataFrame(cf).rolling(50, min_periods=50).mean().values
    r1 = np.full_like(cf, np.nan)
    r1[1:] = cf[1:] / cf[:-1] - 1
    ID = c / o - 1
    rows, prev_pick = [], None
    for t in range(60, T):
        p = t - 1
        ok = (rc[p] >= MIN_PX) & (adv[p] >= 50e6) & (cf[p] > sma[p]) & np.isfinite(r1[p]) & np.isfinite(ID[t])
        uni = (rc[p] >= MIN_PX) & (adv[p] >= 50e6) & np.isfinite(ID[t])
        idx = np.where(ok)[0]
        if len(idx) < TOP_K or uni.sum() < 30:
            continue
        pick = idx[np.argsort(-r1[p, idx])][:TOP_K]
        rows.append((dates[t], float(np.mean(ID[t, pick])), float(np.mean(ID[t, uni])),
                     int(prev_pick is not None and len(set(pick) & set(prev_pick)) > 0)))
        prev_pick = pick
    df = pd.DataFrame(rows, columns=["date", "picks", "univ", "repeat"]).set_index("date")
    import overnight_rsp_hedge as H
    res = {"prereg": "docs/studies/day_hold_prereg.json long_history_secondary", "days": int(len(df)),
           "repeat_share": round(float(df["repeat"].mean()), 3)}
    for name, m in (("2016_2020", df.index.year <= 2020), ("2021_2026", df.index.year >= 2021), ("all", df.index.year > 0)):
        g = df[m]
        res[name] = {"excess_vs_universe": H.mean_stat((g["picks"] - g["univ"]).values),
                     "picks_net_10bp": H.mean_stat((g["picks"] - 10e-4).values),
                     "universe_net_10bp": H.mean_stat((g["univ"] - 10e-4).values)}
    a, b = res["2016_2020"]["excess_vs_universe"], res["2021_2026"]["excess_vs_universe"]
    res["verdict"] = "PASS" if all(x["mean_bp"] > 0 and x["t"] >= 2 for x in (a, b)) else "FAIL"
    os.makedirs(WORK, exist_ok=True)
    json.dump(res, open(os.path.join(WORK, "long.json"), "w"), indent=1, default=str)
    P_(json.dumps(res, indent=1, default=str))


def forward():
    """The pre-registered forward run: every session after the pilot through today (after hours), scored the same way.
    Read at 40 total sessions (pilot + forward); nightly numbers are direction only."""
    import datetime as _dt
    today = _dt.datetime.now(BS.ET).strftime("%Y-%m-%d") if hasattr(BS, "ET") else _dt.date.today().isoformat()
    pilot("2026-10-09", today, "forward.json")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    {"pilot": pilot, "long": long, "forward": forward}.get(cmd, lambda: P_(__doc__))()
