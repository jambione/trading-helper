#!/usr/bin/env python3
"""Short gap-down names, 09:31 open to the close. Spec: docs/studies/gapdown_short_prereg.json (amended_1 + _2).

Data comes ONLY from the data-only Alpaca account in secrets_data.json (its own rate budget; never the desk's key,
never any order path). The universe is ai_reports/allsym/assets.json (ACTIVE + INACTIVE US equities, cached).

  --daily    raw + adjusted daily SIP bars for the universe       -> ai_reports/gapdown/daily.pkl
  --minute   09:30 and 09:31 SIP minute bars (adjustment=all) for each day's liquid names
                                                                  -> ai_reports/gapdown/minute.pkl (resumable)
  --score    the prereg table                                     -> ai_reports/gapdown/result.md

Signal: close of the 09:30 bar / adjusted close(d-1) - 1 <= -1% and > -10%, prior day not <= -10% (SSR), at most
20 names (largest gaps first). Short at the 09:31 bar open, cover at the official close(d). Control: every liquid
name with a 09:30-close gap in (-1%, +1%), shorted the same way. Liquidity on d-1 from RAW bars: close >= $10 and
20-day mean dollar volume >= $20M.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import pickle
import statistics as st
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta

ROOT = os.environ.get("REPO") or os.getcwd()
sys.path.insert(0, ROOT)
OUT = os.path.join(ROOT, "ai_reports", "gapdown")
LO, HI = "2015-11-01", "2026-10-15"
FIRST_DAY = date(2016, 1, 1)
HALVES = ((date(2016, 1, 1), date(2021, 1, 1)), (date(2021, 1, 1), date(2026, 10, 16)))
GAP_MAX, GAP_FLOOR, SSR = -0.01, -0.10, -0.10
TOP_N = 20
PX_MIN, DV_MIN, DV_DAYS = 10.0, 20e6, 20
MIN_UNIVERSE = 300
COST_BP, STRESS_BP, SPY_COST_BP = 20.0, 25.0, 2.0
T_BAR, MIN_SIGNAL_DAYS, POS_MONTHS = 2.0, 500, 0.55
REQ_GAP_S = 0.34                     # <= ~180 requests/min on the data account


# ── pure logic (tested) ───────────────────────────────────────────────────────

def liquid_dates(rows: list[tuple]) -> set[str]:
    """rows: RAW daily (date, close, volume) ascending. Returns the dates d whose PRIOR trading row passes
    close >= $10 and mean(close*volume) over the 20 rows ending at d-1 >= $20M."""
    out = set()
    for k in range(DV_DAYS, len(rows)):
        prev = rows[k - 1]
        if prev[1] < PX_MIN:
            continue
        win = rows[k - DV_DAYS:k]
        if sum(c * v for _, c, v in win) / DV_DAYS >= DV_MIN:
            out.add(rows[k][0])
    return out


def pick_day(names: list[dict]) -> tuple[list[dict], list[dict]]:
    """names: dicts with gap, prior_ret, ret (short return, gross). Returns (picks, control)."""
    sig = [n for n in names if GAP_FLOOR < n["gap"] <= GAP_MAX and n["prior_ret"] > SSR]
    sig.sort(key=lambda n: n["gap"])
    ctrl = [n for n in names if -0.01 < n["gap"] < 0.01]
    return sig[:TOP_N], ctrl


def clustered_t(xs: list[float]) -> float | None:
    if len(xs) < 3:
        return None
    sd = st.stdev(xs)
    return None if sd == 0 else st.fmean(xs) / (sd / math.sqrt(len(xs)))


def score_half(days: list[dict], cost_bp: float, stress_bp: float) -> dict:
    """days: per signal day {d, picks:[{sym, ret}], ctrl_ret, spy_ret}. All returns are SHORT returns except spy_ret
    (long). (a) paired gap-minus-control; (b) SPY-hedged at stress cost; (c) positive months at stress cost;
    (d) (a) after dropping the top 3 days and the top 5 names."""
    c, s = cost_bp / 1e4, stress_bp / 1e4
    gap = [st.fmean(p["ret"] for p in d["picks"]) for d in days]
    paired = [g - d["ctrl_ret"] for g, d in zip(gap, days)]
    net = [g - c for g in gap]
    hedged_s = [g - s + d["spy_ret"] - SPY_COST_BP / 1e4 for g, d in zip(gap, days)]
    months: dict[str, list[float]] = defaultdict(list)
    for g, d in zip(gap, days):
        months[d["d"][:7]].append(g - s)
    pos_m = sum(1 for v in months.values() if sum(v) > 0) / len(months) if months else 0.0
    top3 = set(sorted(range(len(paired)), key=lambda i: paired[i], reverse=True)[:3])
    contrib: dict[str, float] = defaultdict(float)
    for d in days:
        for p in d["picks"]:
            contrib[p["sym"]] += p["ret"] / len(d["picks"])
    top5 = {k for k, _ in sorted(contrib.items(), key=lambda kv: kv[1], reverse=True)[:5]}
    robust = []
    for i, d in enumerate(days):
        if i in top3:
            continue
        keep = [p["ret"] for p in d["picks"] if p["sym"] not in top5]
        if keep:
            robust.append(st.fmean(keep) - d["ctrl_ret"])
    r = {"days": len(days), "names": sum(len(d["picks"]) for d in days),
         "gross_bp": st.fmean(gap) * 1e4 if gap else None, "net_bp": st.fmean(net) * 1e4 if net else None,
         "t_net": clustered_t(net),
         "paired_bp": st.fmean(paired) * 1e4 if paired else None, "t_paired": clustered_t(paired),
         "hedged_stress_bp": st.fmean(hedged_s) * 1e4 if hedged_s else None, "t_hedged": clustered_t(hedged_s),
         "pos_months": pos_m, "robust_bp": st.fmean(robust) * 1e4 if robust else None}
    a = r["t_paired"] is not None and r["paired_bp"] > 0 and r["t_paired"] >= T_BAR
    b = r["t_hedged"] is not None and r["hedged_stress_bp"] > 0 and r["t_hedged"] >= T_BAR
    cc = pos_m >= POS_MONTHS
    dd = r["robust_bp"] is not None and r["robust_bp"] > 0
    r.update(pass_a=a, pass_b=b, pass_c=cc, pass_d=dd, underpowered=len(days) < MIN_SIGNAL_DAYS)
    return r


# ── data (data-only account) ──────────────────────────────────────────────────

def _client():
    from alpaca.data.historical import StockHistoricalDataClient
    s = json.load(open(os.path.join(ROOT, "secrets_data.json")))
    return StockHistoricalDataClient(s["alpaca_data_api_key"], s["alpaca_data_secret_key"])


def _universe() -> list[str]:
    from ticker_filters import is_common, is_not_stock
    from overnight_book import FUNDISH
    names = json.load(open(os.path.join(ROOT, "ai_reports", "allsym", "assets.json")))
    keep, etf = [], 0
    for s, n in names.items():
        if not (s.isalpha() and is_common(s)):
            continue
        if is_not_stock(s, n) or FUNDISH.search((n or "").upper()):
            etf += 1
            continue
        keep.append(s)
    print(f"universe: {len(names)} assets, {len(keep)} common stock, {etf} excluded as ETF/fund", flush=True)
    return sorted(keep)


def _bars(cl, syms, tf, start, end, adj, log):
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    for attempt in range(5):
        try:
            time.sleep(REQ_GAP_S)
            r = cl.get_stock_bars(StockBarsRequest(symbol_or_symbols=syms, timeframe=tf, start=start, end=end,
                                                   feed=DataFeed.SIP, adjustment=adj))
            return r.data or {}
        except Exception as e:  # noqa: BLE001
            log.append(f"{syms[0]}..{syms[-1]} {start:%Y-%m-%d} try {attempt}: {type(e).__name__} {str(e)[:80]}")
            time.sleep(5 * (attempt + 1))
    log.append(f"FAILED {syms[0]}..{syms[-1]} {start:%Y-%m-%d} ({len(syms)} syms)")
    return None


def fetch_daily() -> None:
    from zoneinfo import ZoneInfo
    from alpaca.data.enums import Adjustment
    from alpaca.data.timeframe import TimeFrame
    ET = ZoneInfo("America/New_York")
    os.makedirs(OUT, exist_ok=True)
    cl, log = _client(), []
    syms = _universe() + ["SPY"]
    start = datetime.strptime(LO, "%Y-%m-%d").replace(tzinfo=ET)
    end = datetime.strptime(HI, "%Y-%m-%d").replace(hour=23, tzinfo=ET)
    liquid: dict[str, list[str]] = {}
    for i in range(0, len(syms), 100):
        chunk = syms[i:i + 100]
        raw = _bars(cl, chunk, TimeFrame.Day, start, end, Adjustment.RAW, log)
        for s, rows in (raw or {}).items():
            rr = [(b.timestamp.astimezone(ET).strftime("%Y-%m-%d"), float(b.close), float(b.volume)) for b in rows]
            ld = liquid_dates(rr)
            if ld:
                liquid[s] = sorted(ld)
        if i % 2000 == 0:
            print(f"  raw {i}/{len(syms)}: {len(liquid)} names ever liquid", flush=True)
    need = sorted(set(liquid) | {"SPY"})
    adj: dict[str, dict[str, tuple]] = {}
    for i in range(0, len(need), 100):
        chunk = need[i:i + 100]
        a = _bars(cl, chunk, TimeFrame.Day, start, end, Adjustment.ALL, log)
        for s, rows in (a or {}).items():
            adj[s] = {b.timestamp.astimezone(ET).strftime("%Y-%m-%d"): (float(b.open), float(b.close)) for b in rows}
        if i % 1000 == 0:
            print(f"  adjusted {i}/{len(need)}", flush=True)
    pickle.dump({"liquid": liquid, "adj": adj, "log": log}, open(os.path.join(OUT, "daily.pkl"), "wb"))
    print(f"daily done: {len(liquid)} liquid names, {len(adj)} adjusted series, {len(log)} log lines", flush=True)


def fetch_minute() -> None:
    from zoneinfo import ZoneInfo
    from alpaca.data.enums import Adjustment
    from alpaca.data.timeframe import TimeFrame
    ET = ZoneInfo("America/New_York")
    d = pickle.load(open(os.path.join(OUT, "daily.pkl"), "rb"))
    by_day: dict[str, list[str]] = defaultdict(list)
    for s, ds in d["liquid"].items():
        for x in ds:
            if x >= FIRST_DAY.isoformat():
                by_day[x].append(s)
    path = os.path.join(OUT, "minute.pkl")
    have = pickle.load(open(path, "rb")) if os.path.exists(path) else {"days": {}, "log": []}
    cl = _client()
    todo = [x for x in sorted(by_day) if x not in have["days"]]
    print(f"minute: {len(by_day)} days, {len(todo)} to fetch", flush=True)
    for n, x in enumerate(todo):
        names = sorted(set(by_day[x]) | {"SPY"})
        y, m, dd = map(int, x.split("-"))
        t0, t1 = datetime(y, m, dd, 9, 30, tzinfo=ET), datetime(y, m, dd, 9, 31, 59, tzinfo=ET)
        got, failed = {}, 0
        for i in range(0, len(names), 200):
            r = _bars(cl, names[i:i + 200], TimeFrame.Minute, t0, t1, Adjustment.ALL, have["log"])
            if r is None:
                failed += len(names[i:i + 200])
                continue
            for s, rows in r.items():
                bm = {b.timestamp.astimezone(ET).strftime("%H:%M"): (float(b.open), float(b.close)) for b in rows}
                if "09:30" in bm and "09:31" in bm:
                    got[s] = (bm["09:30"][1], bm["09:31"][0])
        have["days"][x] = {"bars": got, "asked": len(names), "failed": failed}
        if n % 25 == 0 or n == len(todo) - 1:
            pickle.dump(have, open(path + ".tmp", "wb"))
            os.replace(path + ".tmp", path)
            print(f"  {x}: {len(got)}/{len(names)} with 09:30+09:31 bars ({n + 1}/{len(todo)})", flush=True)


# ── score ─────────────────────────────────────────────────────────────────────

def _borrow_share(syms: list[str]) -> str:
    """Share of picked symbols NOT easy-to-borrow at Alpaca today (information; read-only asset listing on the
    data-only PAPER account; historical borrow is unknowable)."""
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.enums import AssetClass
        from alpaca.trading.requests import GetAssetsRequest
        s = json.load(open(os.path.join(ROOT, "secrets_data.json")))
        tc = TradingClient(s["alpaca_data_api_key"], s["alpaca_data_secret_key"], paper=True)
        etb = {a.symbol: bool(a.easy_to_borrow) for a in tc.get_all_assets(GetAssetsRequest(asset_class=AssetClass.US_EQUITY))}
    except Exception as e:  # noqa: BLE001
        return f"not available ({type(e).__name__})"
    known = [x for x in syms if x in etb]
    if not known:
        return "no picked symbol is listed today"
    return (f"{sum(1 for x in known if not etb[x]) / len(known):.0%} of {len(known)} still-listed picked symbols are "
            f"NOT easy-to-borrow ({len(syms) - len(known)} picked symbols no longer listed)")


def build_days() -> tuple[list[dict], dict]:
    d = pickle.load(open(os.path.join(OUT, "daily.pkl"), "rb"))
    mn = pickle.load(open(os.path.join(OUT, "minute.pkl"), "rb"))
    adj = d["adj"]
    cal = sorted(adj["SPY"])
    prev = {cal[k]: cal[k - 1] for k in range(1, len(cal))}
    prev2 = {cal[k]: cal[k - 2] for k in range(2, len(cal))}
    out, stats = [], {"small_universe_days": 0, "no_0930_0931_bar": 0, "no_daily_bar": 0, "failed_syms": 0,
                      "no_signal_days": 0}
    for x in sorted(mn["days"]):
        day = mn["days"][x]
        stats["failed_syms"] += day["failed"]
        stats["no_0930_0931_bar"] += day["asked"] - day["failed"] - len(day["bars"])
        if x not in prev or x not in prev2 or "SPY" not in day["bars"]:
            continue
        p, p2 = prev[x], prev2[x]
        names = []
        for s, (c930, o931) in day["bars"].items():
            if s == "SPY":
                continue
            a = adj.get(s, {})
            if x not in a or p not in a or p2 not in a or o931 <= 0:
                stats["no_daily_bar"] += 1
                continue
            names.append({"sym": s, "gap": c930 / a[p][1] - 1, "prior_ret": a[p][1] / a[p2][1] - 1,
                          "ret": -(a[x][1] / o931 - 1), "px": a[p][1], "oc_ret": -(a[x][1] / a[x][0] - 1)})
        if len(names) < MIN_UNIVERSE:
            stats["small_universe_days"] += 1
            continue
        picks, ctrl = pick_day(names)
        if not picks or not ctrl:
            stats["no_signal_days"] += 1
            continue
        so = day["bars"]["SPY"][1]
        out.append({"d": x, "picks": picks, "ctrl_ret": st.fmean(n["ret"] for n in ctrl),
                    "spy_ret": adj["SPY"][x][1] / so - 1})
    return out, stats


def score() -> str:
    days, stats = build_days()
    L = ["# Gap-down short (prereg amended_1+2)", "",
         f"Short <= -1% gaps (09:30 close vs prior close; > -10%, no SSR), top {TOP_N}/day, 09:31 open -> close. "
         f"Control = all liquid names gapping within +-1%, same timing. Cost {COST_BP:.0f} bp (stress "
         f"{STRESS_BP:.0f}); t bar {T_BAR} day-clustered.", "",
         f"Data: {stats}", "",
         "| half | signal days | names | gross bp | net bp (t) | paired gap-ctrl bp (t) | SPY-hedged @25 bp (t) | "
         "pos months @25 | drop top3d/top5n bp | a b c d | verdict |",
         "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|"]
    f = lambda v: "-" if v is None else f"{v:+.1f}"  # noqa: E731
    verdicts = []
    for lo, hi in HALVES:
        h = [d for d in days if lo.isoformat() <= d["d"] < hi.isoformat()]
        r = score_half(h, COST_BP, STRESS_BP)
        ok = r["pass_a"] and r["pass_b"] and r["pass_c"] and r["pass_d"]
        v = "UNDERPOWERED" if r["underpowered"] else ("PASS" if ok else "FAIL")
        verdicts.append(v)
        L.append(f"| {lo}..{hi} | {r['days']} | {r['names']} | {f(r['gross_bp'])} | {f(r['net_bp'])} "
                 f"({f(r['t_net'])}) | {f(r['paired_bp'])} ({f(r['t_paired'])}) | {f(r['hedged_stress_bp'])} "
                 f"({f(r['t_hedged'])}) | {r['pos_months']:.0%} | {f(r['robust_bp'])} | "
                 f"{'Y' if r['pass_a'] else 'n'}{'Y' if r['pass_b'] else 'n'}{'Y' if r['pass_c'] else 'n'}"
                 f"{'Y' if r['pass_d'] else 'n'} | {v} |")
    overall = "PASS" if all(v == "PASS" for v in verdicts) else (
        "UNDERPOWERED" if "UNDERPOWERED" in verdicts and "FAIL" not in verdicts else "FAIL")
    L += ["", f"**Overall: {overall}** (PASS needs both halves).", "", "## Information only", ""]
    # gap bins, price bins, open-to-close, by year
    for label, key, bins in (("gap", "gap", ((-0.02, -0.01), (-0.04, -0.02), (-0.10, -0.04))),
                             ("prior close $", "px", ((10, 20), (20, 50), (50, 1e9)))):
        for a, b in bins:
            xs = [p["ret"] for d in days for p in d["picks"] if (a < p[key] <= b if key == "gap" else a <= p[key] < b)]
            if xs:
                L.append(f"- {label} {a}..{b}: {len(xs)} trades, gross {st.fmean(xs) * 1e4:+.1f} bp/trade")
    oc = [p["oc_ret"] for d in days for p in d["picks"]]
    if oc:
        L.append(f"- open-to-close (official open, look-ahead, info only): gross {st.fmean(oc) * 1e4:+.1f} bp/trade")
    years: dict[str, list[float]] = defaultdict(list)
    for d in days:
        years[d["d"][:4]].append(st.fmean(p["ret"] for p in d["picks"]) - d["ctrl_ret"])
    L.append("- paired gap-minus-control by year (bp/day): " +
             ", ".join(f"{y} {st.fmean(v) * 1e4:+.1f}" for y, v in sorted(years.items())))
    L.append(f"- borrow today: {_borrow_share(sorted({p['sym'] for d in days for p in d['picks']}))}")
    L.append("- Survivorship: tickers later reused by another company are lost; delisted names otherwise included "
             "(ACTIVE + INACTIVE assets).")
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--daily", action="store_true")
    ap.add_argument("--minute", action="store_true")
    ap.add_argument("--score", action="store_true")
    a = ap.parse_args()
    if a.daily:
        fetch_daily()
    if a.minute:
        fetch_minute()
    if a.score:
        out = score()
        print(out)
        open(os.path.join(OUT, "result.md"), "w").write(out + "\n")


if __name__ == "__main__":
    main()
