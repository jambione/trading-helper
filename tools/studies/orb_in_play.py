#!/usr/bin/env python3
"""Opening-range breakout on stocks in play (Zarattini, Barbon & Aziz 2024). Spec: docs/studies/orb_in_play_prereg.json
(amended .. amended_5; common C1-C4). amended_5: sessions 2016-02-01 .. 2026-10-15 (held-out 01-14 .. 04-24 never
fetched, 04-27 .. 05-22 warm-up), halves H1 2016-2020 / H2 2021-2026, pooled power gate, per-day look-back fetch union,
SPY-hedged lift, coverage gates, month-clustered t, drop-best-year, SELECTION ONLY / DECAYED / COST-FRAGILE labels.

Universe per day t: names from the gap-down study's ever-liquid list (3,640 active + inactive US common stocks), prior
split-adjusted close > $10, ATR(14) > $0.50, ranked by prior 20-day median dollar volume; top 1,000. In-play score =
09:30-09:34 volume / mean of that window over the prior 14 sessions (>= 10 present). Top 20; LONG candidates = first
5-minute candle green. Buy stop at the range high + $0.01 from 09:35 (fill max(trigger, open)); stop = range low (fill
min(stop, open); a bar touching both after the trigger counts as stopped); exit at the last close <= 15:59. Control:
a seeded minute in (trigger, min(trigger + 60, 15:00)], same % stop, same exit. Cost 10 bp binding.

  --fetch-daily   daily OHLCV for the ever-liquid list over both windows (+ look-back), data-only account
  --fetch-open    09:30-09:34 minute bars for the union of every name ever in the top 1,000
  --fetch-day     full-day minute bars for each day's top 20
  (default)       count step (outcome-blind); --score after a result-skeptic READY
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
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ROOT = os.environ.get("EEH_ROOT") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT_DIR = os.path.join(ROOT, "ai_reports", "orb_in_play")
ET = ZoneInfo("America/New_York")
# amended_5: 2016-2026 for power; halves by date; the held-out window is never fetched; 04-27..05-22 warm-up only
HALVES = {"H1": ("2016-02-01", "2020-12-31"), "H2": ("2021-01-04", "2026-10-15")}
FETCH = (("2015-12-01", "2026-01-13"), ("2026-04-27", "2026-10-15"))
SEG2, WARM_END = "2026-04-27", "2026-05-22"
SPY, MAX_SPY_REFUSED, MIN_CUT_DV = "SPY", 0.005, 20e6
HELD = ("2026-01-14", "2026-04-24")
TOP_UNIV, TOP_PLAY, OPEN_LB, OPEN_NEED = 1000, 20, 14, 10
MIN_PX, MIN_ATR = 10.0, 0.50
COST, COSTS = 0.0010, (0.0005, 0.0010, 0.0020)
T_PASS, T_BATCH, MDE_BAR = 2.0, 2.73, 0.0010
FETCH_DAILY_END = {"2026-01-13": "2026-01-13T16:00", "2026-10-15": "2026-10-15T16:00"}
FLOOR_TRADES, FLOOR_DAYS = 300, 20
TICK, M_FROM, M_CTRL_END, M_LAST = 0.01, 575, 900, 959


def _client():
    from alpaca.data.historical import StockHistoricalDataClient
    s = json.load(open(os.path.join(ROOT, "secrets_data.json")))
    return StockHistoricalDataClient(s["alpaca_data_api_key"], s["alpaca_data_secret_key"])


REQUESTS = [0]


def segment(d: str) -> int:
    """Sessions after the held-out gap form their own segment: look-backs never bridge 01-13 -> 04-27."""
    return 1 if d >= SEG2 else 0


def half_of(d: str) -> str | None:
    if HELD[0] <= d <= WARM_END:
        return None
    return next((k for k, (a, b) in HALVES.items() if a <= d <= b), None)


def _get(cl, syms, tf, a, b, failed, tag, raw: bool = False):
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.requests import StockBarsRequest
    for attempt in range(6):
        REQUESTS[0] += 1
        try:
            return cl.get_stock_bars(StockBarsRequest(symbol_or_symbols=syms, timeframe=tf, start=a, end=b,
                                                      feed=DataFeed.SIP,
                                                      adjustment=Adjustment.RAW if raw else Adjustment.SPLIT)).data
        except Exception as e:  # noqa: BLE001
            print(f"  retry {tag} {attempt + 1}: {str(e)[:80]}", flush=True)
            time.sleep(15 * (attempt + 1))
    failed.append(tag)
    return None


def ever_liquid() -> list[str]:
    d = pickle.load(open(os.path.join(ROOT, "ai_reports", "gapdown", "daily.pkl"), "rb"))
    return sorted(d["liquid"])


def fetch_daily() -> None:
    from alpaca.data.timeframe import TimeFrame
    cl, failed, out, raw_close = _client(), [], {}, {}
    syms = ever_liquid()
    for a0, b0 in FETCH:
        a = datetime.fromisoformat(a0).replace(tzinfo=ET)
        b = datetime.fromisoformat(FETCH_DAILY_END[b0]).replace(tzinfo=ET)       # never the held-out 01-14 bar
        for i in range(0, len(syms), 100):
            ch = syms[i:i + 100]
            for raw in (False, True):
                got = _get(cl, ch, TimeFrame.Day, a, b, failed, f"daily {a0} {i} raw={raw}", raw=raw)
                if got is None:
                    continue
                for s, bars in got.items():
                    for x in bars:
                        d = x.timestamp.astimezone(ET).strftime("%Y-%m-%d")
                        if HELD[0] <= d <= HELD[1]:
                            continue
                        if raw:
                            raw_close.setdefault(s, {})[d] = x.close
                        else:
                            out.setdefault(s, {})[d] = (x.open, x.high, x.low, x.close, x.volume)
                time.sleep(0.4)
    os.makedirs(OUT_DIR, exist_ok=True)
    pickle.dump(raw_close, open(os.path.join(OUT_DIR, "daily_raw_close.pkl"), "wb"))
    pickle.dump(out, open(os.path.join(OUT_DIR, "daily.pkl"), "wb"))
    json.dump({"failed": failed, "requests": REQUESTS[0]}, open(os.path.join(OUT_DIR, "daily_fetch_log.json"), "w"))
    print("names", len(out), "failed", failed, "requests", REQUESTS[0])


def universe(daily: dict, raw_close: dict | None = None) -> dict[str, list[str]]:
    """day -> top 1,000 names by prior 20-day median dollar volume (split-invariant) among RAW prior close > $10 and
    ATR(14) > $0.50 in raw dollars (adjusted ATR x raw/adjusted close on the prior day). Prior days only."""
    if raw_close is None:
        raw_close = pickle.load(open(os.path.join(OUT_DIR, "daily_raw_close.pkl"), "rb"))
    days = sorted({d for v in daily.values() for d in v})
    idx = {d: i for i, d in enumerate(days)}
    per = defaultdict(list)
    for s, v in daily.items():
        ds = sorted(v)
        for j in range(20, len(ds)):
            t = ds[j]
            prior = ds[j - 20:j]
            if idx[prior[-1]] != idx[t] - 1:
                continue                                  # needs yesterday's bar
            if segment(prior[0]) != segment(t):
                continue                                  # amended_5: no look-back across the held-out gap
            o, h, lo, c, vol = v[prior[-1]]
            rc = raw_close.get(s, {}).get(prior[-1])
            if rc is None or rc <= MIN_PX:
                continue
            scale = rc / c if c else 1.0
            trs = []
            for k in range(j - 14, j):
                pc = v[ds[k - 1]][3]
                hh, ll = v[ds[k]][1], v[ds[k]][2]
                trs.append(max(hh - ll, abs(hh - pc), abs(ll - pc)))
            if statistics.mean(trs) * scale <= MIN_ATR:
                continue
            dv = statistics.median(v[p][3] * v[p][4] for p in prior)
            per[t].append((dv, s))
    out = {}
    for t, x in per.items():
        top = sorted(x, reverse=True)[:TOP_UNIV]
        out[t] = [s for _, s in top]
        universe.cutoff_dv[t] = top[-1][0] if top else None           # superset check (skeptic r1 #6)
    return out


universe.cutoff_dv = {}


def unions(univ: dict[str, list[str]]) -> dict[str, list[str]]:
    """amended_5: day d is fetched for every name in the top 1,000 on any session in [d, d+14] of its segment (what
    the 14-session look-back of later days needs)."""
    days = sorted(univ)
    out = {}
    for i, d in enumerate(days):
        names = set()
        for p in days[i:i + OPEN_LB + 1]:
            if segment(p) == segment(d):
                names |= set(univ[p])
        out[d] = sorted(names)
    return out


def fetch_open() -> None:
    from alpaca.data.timeframe import TimeFrame
    cl, failed = _client(), []
    daily = pickle.load(open(os.path.join(OUT_DIR, "daily.pkl"), "rb"))
    univ = universe(daily)
    un = unions(univ)
    out_p, un_p = os.path.join(OUT_DIR, "open.pkl"), os.path.join(OUT_DIR, "open_union.pkl")
    out = pickle.load(open(out_p, "rb")) if os.path.exists(out_p) else {}
    stored = pickle.load(open(un_p, "rb")) if os.path.exists(un_p) else {}
    old = sorted({s for d in out if d in univ for s in univ[d]})       # the 10/24 run fetched its whole-window union
    for d in out:
        stored.setdefault(d, old)
    for n, d in enumerate(sorted(un)):
        if d in out:
            continue
        a = datetime.fromisoformat(d).replace(hour=9, minute=30, tzinfo=ET)
        rec, ok, syms = {}, True, un[d]
        for i in range(0, len(syms), 100):
            # end is inclusive at Alpaca: 09:34:59 keeps the 09:35 bar out (skeptic r1 #1)
            got = _get(cl, syms[i:i + 100], TimeFrame.Minute, a, a + timedelta(minutes=4, seconds=59), failed,
                       f"open {d} {i}")
            if got is None:
                ok = False
                break
            for s, bars in got.items():
                rec[s] = [(x.timestamp.astimezone(ET).minute, x.open, x.high, x.low, x.close, x.volume) for x in bars
                          if x.timestamp.astimezone(ET).hour == 9 and 30 <= x.timestamp.astimezone(ET).minute <= 34]
            time.sleep(0.4)
        if ok:
            out[d], stored[d] = rec, syms
        if n % 10 == 0:
            for obj, path in ((out, out_p), (stored, un_p)):
                pickle.dump(obj, open(path + ".tmp", "wb"))
                os.replace(path + ".tmp", path)
            print(d, len(rec), "requests", REQUESTS[0], flush=True)
    for obj, path in ((out, out_p), (stored, un_p)):
        pickle.dump(obj, open(path + ".tmp", "wb"))
        os.replace(path + ".tmp", path)
    json.dump({"failed": failed, "requests": REQUESTS[0]}, open(os.path.join(OUT_DIR, "open_fetch_log.json"), "w"))
    print("failed", failed, "requests", REQUESTS[0])


def lookback_misses(univ: dict, opens: dict, stored: dict) -> dict:
    """amended_5: (d, s, p) misses = a top-1,000 name on d whose look-back session p was not requested for it; and
    sessions of the window missing from open.pkl."""
    days = sorted(univ)
    misses = 0
    for i, d in enumerate(days):
        prev = [p for p in days[max(0, i - OPEN_LB):i] if segment(p) == segment(d)]
        for s in univ[d]:
            misses += sum(1 for p in prev if s not in set(stored.get(p, ())))
    missing_sessions = [d for d in days if d not in opens]
    return {"misses": misses, "missing_sessions": missing_sessions}


def in_play(daily: dict, opens: dict) -> dict[str, list[tuple]]:
    """day -> [(score, sym, (o, h, l, c) of the 5-minute candle)] top 20 (prior-14-session opening volume)."""
    univ = universe(daily)
    days = sorted(opens)
    out = {}
    for i, d in enumerate(days):
        if d not in univ:
            continue
        rows = []
        for s in univ[d]:
            today = opens[d].get(s)
            if not today or len(today) != 5:
                continue                                   # exactly the five 09:30-09:34 minutes
            hist = [sum(x[5] for x in opens[p][s]) for p in days[max(0, i - OPEN_LB):i]
                if segment(p) == segment(d) and opens[p].get(s)]
            if len(hist) < OPEN_NEED or statistics.mean(hist) <= 0:
                continue
            vol = sum(x[5] for x in today)
            cand = (today[0][1], max(x[2] for x in today), min(x[3] for x in today), today[-1][4])
            rows.append((vol / statistics.mean(hist), s, cand))
        out[d] = sorted(rows, key=lambda r: (-r[0], r[1]))[:TOP_PLAY]
    return out


def fetch_day() -> None:
    from alpaca.data.timeframe import TimeFrame
    cl, failed = _client(), []
    daily = pickle.load(open(os.path.join(OUT_DIR, "daily.pkl"), "rb"))
    opens = pickle.load(open(os.path.join(OUT_DIR, "open.pkl"), "rb"))
    ip = in_play(daily, opens)
    out_p = os.path.join(OUT_DIR, "fullday.pkl")
    out = pickle.load(open(out_p, "rb")) if os.path.exists(out_p) else {}
    n = 0
    for d, rows in sorted(ip.items()):
        have = out.get(d)
        if have is not None and SPY in have:
            continue
        syms = [SPY] if have is not None else [r[1] for r in rows] + [SPY]     # amended_5: SPY for the hedge
        a = datetime.fromisoformat(d).replace(hour=9, minute=30, tzinfo=ET)
        got = _get(cl, syms, TimeFrame.Minute, a, a.replace(hour=15, minute=59, second=59), failed, f"day {d}",
                   raw=True)                               # RAW full-day bars: range, trigger and exits (r1 #3)
        if got is None:
            continue
        rec = dict(have or {})
        rec.update({s: [(x.timestamp.astimezone(ET).hour * 60 + x.timestamp.astimezone(ET).minute, x.open, x.high,
                         x.low, x.close) for x in bars] for s, bars in got.items()})
        out[d] = rec
        n += 1
        if n % 10 == 0:
            pickle.dump(out, open(out_p + ".tmp", "wb"))
            os.replace(out_p + ".tmp", out_p)
            print(d, "requests", REQUESTS[0], flush=True)
        time.sleep(0.4)
    pickle.dump(out, open(out_p + ".tmp", "wb"))
    os.replace(out_p + ".tmp", out_p)
    json.dump({"failed": failed, "requests": REQUESTS[0]}, open(os.path.join(OUT_DIR, "day_fetch_log.json"), "w"))
    print("days", len(out), "failed", failed, "requests", REQUESTS[0])


def walk(rows: list, k: int, entry: float, stop: float, side: int, from_trigger: bool) -> dict:
    """From row k (filled at entry): stop when low <= stop (fill min(stop, open); on the trigger bar a touch of the
    stop counts as stopped at the stop); else the last close <= 15:59."""
    for j in range(k, len(rows)):
        m, o, h, lo, c = rows[j]
        if m > M_LAST:
            break
        hit = lo <= stop if side > 0 else h >= stop
        if hit:
            fill = stop if (j == k and from_trigger) else (min(stop, o) if side > 0 else max(stop, o))
            return {"gross": side * (fill / entry - 1), "why": "stop", "exit_min": m}
    last = [r for r in rows if r[0] <= M_LAST]
    return {"gross": side * (last[-1][4] / entry - 1), "why": "time", "exit_min": last[-1][0]}


def trade(rows: list, cand: tuple, side: int, sym: str, day: str) -> dict:
    o5, hi, lo, c5 = cand
    level = hi + TICK if side > 0 else lo - TICK
    stop = lo if side > 0 else hi
    for k, r in enumerate(rows):
        if r[0] < M_FROM:
            continue
        if r[0] > M_LAST:
            break
        if (r[2] >= level) if side > 0 else (r[3] <= level):
            fill = max(level, r[1]) if side > 0 else min(level, r[1])
            if fill < MIN_PX:
                return {"skip": "below_10"}
            res = walk(rows, k, fill, stop, side, True)
            sp = abs(fill - stop) / fill

            def ctrl(tag):
                cand_k = [j for j in range(len(rows)) if r[0] < rows[j][0] <= min(r[0] + 60, M_CTRL_END)]
                if not cand_k:
                    return None
                rng = random.Random(int(hashlib.sha256(f"{sym}|{day}|{tag}".encode()).hexdigest()[:16], 16))
                j = rng.choice(cand_k)
                o = rows[j][1]
                w = walk(rows, j, o, o * (1 - side * sp), side, False)
                return w["gross"], rows[j][0], w["exit_min"]
            c1, c2 = ctrl("orb-ctrl"), ctrl("orb-ctrl2")
            return {**res, "trig_min": r[0], "ctrl": c1[0] if c1 else None, "ctrl_in": c1[1] if c1 else None,
                    "ctrl_out": c1[2] if c1 else None, "ctrl2": c2[0] if c2 else None, "entry": fill}
    return {"skip": "no_trigger"}


def spy_ret(spy: dict, m_in: int, m_out: int):
    """SPY close of the exit bar / SPY open of the entry bar - 1 (amended_5 hedge_def); None if a bar is missing."""
    a, b = spy.get(m_in), spy.get(m_out)
    return (b[1] / a[0] - 1) if a and b and a[0] > 0 else None


def build_rows(excluded_years: set | None = None) -> tuple[list[dict], Counter, set]:
    daily = pickle.load(open(os.path.join(OUT_DIR, "daily.pkl"), "rb"))
    opens = pickle.load(open(os.path.join(OUT_DIR, "open.pkl"), "rb"))
    full = pickle.load(open(os.path.join(OUT_DIR, "fullday.pkl"), "rb"))
    ip = in_play(daily, opens)
    rows, st, refused = [], Counter(), set()
    for d, top in sorted(ip.items()):
        h = half_of(d)
        if h is None or (excluded_years and d[:4] in excluded_years):
            continue
        spy = {b[0]: (b[1], b[4]) for b in full.get(d, {}).get(SPY, [])}
        for rank, (sc, s, _cand_adj) in enumerate(top):
            bars = full.get(d, {}).get(s)
            if not bars:
                st["no_fullday_bars"] += 1
                continue
            first5 = [b for b in bars if 570 <= b[0] <= 574]
            if len(first5) != 5:
                st["fullday_missing_opening_minutes"] += 1
                continue
            # the 5-minute candle from the RAW full-day bars (the opens file is adjusted: volume score only)
            cand = (first5[0][1], max(b[2] for b in first5), min(b[3] for b in first5), first5[-1][4])
            side = 1 if cand[3] > cand[0] else (-1 if cand[3] < cand[0] else 0)
            if side == 0:
                st["doji_first_candle"] += 1
                continue
            t = trade(bars, cand, side, s, d)
            if "skip" in t:
                st[f"{'long' if side > 0 else 'short'}_skip_{t['skip']}"] += 1
                continue
            hs = spy_ret(spy, t["trig_min"], t["exit_min"])
            hc = spy_ret(spy, t["ctrl_in"], t["ctrl_out"]) if t["ctrl"] is not None else None
            if hs is None or (t["ctrl"] is not None and hc is None):
                refused.add(d)
                continue
            t["hedged"] = t["gross"] - side * hs
            t["ctrl_hedged"] = t["ctrl"] - side * hc if t["ctrl"] is not None else None
            rows.append({"day": d, "sym": s, "half": h, "side": "long" if side > 0 else "short", "rank": rank, **t})
    rows = [x for x in rows if x["day"] not in refused]           # a missing SPY bar refuses the whole day
    st["spy_refused_days"] = len(refused)
    return rows, st, refused


def coverage(daily: dict, opens: dict, full: dict) -> dict:
    """amended_5 coverage_step (a)-(e), by year, before any outcome."""
    univ = universe(daily)
    ip = in_play(daily, opens)
    by_y = defaultdict(lambda: {"nd": 0, "nd5": 0, "ratio": [], "t20": 0, "t20_bars": 0})
    for d, names in univ.items():
        if half_of(d) is None:
            continue
        y = by_y[d[:4]]
        for s in names:
            y["nd"] += 1
            ob = opens.get(d, {}).get(s)
            if ob and len(ob) == 5:
                y["nd5"] += 1
                dv = daily.get(s, {}).get(d)
                if dv and dv[4]:
                    y["ratio"].append(sum(x[5] for x in ob) / dv[4])
        for _sc, s, _c in ip.get(d, []):
            y["t20"] += 1
            y["t20_bars"] += 1 if full.get(d, {}).get(s) else 0
    last = max(d for v in daily.values() for d in v)
    died = Counter(max(v)[:4] for v in daily.values() if v and max(v) < last)
    out = {}
    for yr, y in sorted(by_y.items()):
        out[yr] = {"a_open5_share": y["nd5"] / y["nd"] if y["nd"] else None,
                   "b_open_vol_over_daily_median": statistics.median(y["ratio"]) if y["ratio"] else None,
                   "c_top20_fullday_share": y["t20_bars"] / y["t20"] if y["t20"] else None,
                   "d_names_died": died.get(yr, 0)}
    excluded = sorted(yr for yr, v in out.items() if v["a_open5_share"] is not None and v["a_open5_share"] < 0.90)
    later = [died.get(str(y), 0) for y in (2017, 2018, 2019)]
    if "2016" in out and later and died.get("2016", 0) < 0.5 * statistics.mean(later):
        excluded = sorted(set(excluded) | {"2016"})
    bad_c = sorted(yr for yr, v in out.items() if v["c_top20_fullday_share"] is not None
                   and v["c_top20_fullday_share"] < 0.98)
    low_cut = sorted(d for d, v in universe.cutoff_dv.items() if half_of(d) and v is not None and v < MIN_CUT_DV)
    return {"by_year": out, "excluded_years": excluded, "c_failed_years": bad_c, "e_low_cutoff_days": low_cut}


def dstats(rows, key):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import eod_reversal as E
    per = defaultdict(list)
    for x in rows:
        v = key(x)
        if v is not None:
            per[x["day"]].append(v)
    return E.stats([statistics.mean(v) for v in per.values()]), per


def module_shas():
    return {"orb_in_play": hashlib.sha256(open(os.path.abspath(__file__), "rb").read()).hexdigest()}


def write(name, obj):
    os.makedirs(OUT_DIR, exist_ok=True)
    p = os.path.join(OUT_DIR, name)
    json.dump(obj, open(p + ".tmp", "w"), indent=1, default=str)
    os.replace(p + ".tmp", p)
    return p


def mde_of(rows) -> float | None:
    _, per = dstats([x for x in rows if x["ctrl"] is not None], lambda x: x["ctrl"] - COST)   # control leg (amended_4)
    xs = [statistics.mean(v) for v in per.values()]
    return (2.0 + 0.84) * statistics.stdev(xs) / math.sqrt(len(xs)) if len(xs) >= 2 else None


def month_t(rows, key) -> dict:
    """amended_5 month_t_def: per-month means of the day means; t across months, df = months - 1."""
    _, per = dstats(rows, key)
    months = defaultdict(list)
    for d, v in per.items():
        months[d[:7]].append(statistics.mean(v))
    ms = [statistics.mean(v) for v in months.values()]
    if len(ms) < 2:
        return {"months": len(ms), "mean": None, "t": None}
    se = statistics.stdev(ms) / math.sqrt(len(ms))
    return {"months": len(ms), "mean": statistics.mean(ms), "t": statistics.mean(ms) / se if se else None}


def drop_best_year(rows, key) -> dict:
    _, per = dstats(rows, key)
    years = defaultdict(list)
    for d, v in per.items():
        years[d[:4]].append(statistics.mean(v))
    best = max(years, key=lambda y: statistics.mean(years[y])) if years else None
    rest = [x for y, v in years.items() if y != best for x in v]
    return {"best_year": best, "mean_without": statistics.mean(rest) if rest else None}


def verdict(longs: list[dict]) -> dict:
    """amended_5 change_pass / selection_only / supersedes, plus the follow-on labels."""
    net = lambda x: x["gross"] - COST                                                      # noqa: E731
    hnet = lambda x: x["hedged"] - COST                                                    # noqa: E731
    hlift = lambda x: x["hedged"] - x["ctrl_hedged"] if x["ctrl_hedged"] is not None else None   # noqa: E731
    pooled, _ = dstats(longs, net)
    mt = month_t(longs, net)
    halves = {}
    for h in HALVES:
        hr = [x for x in longs if x["half"] == h]
        halves[h] = {"net": dstats(hr, net)[0], "hedged_lift": dstats(hr, hlift)[0],
                     "net_20bp": dstats(hr, lambda x: x["gross"] - 0.0020)[0]["mean"],
                     "raw_lift": dstats(hr, lambda x: x["gross"] - x["ctrl"] if x["ctrl"] is not None else None)[0]}
    p_hnet, p_hlift = dstats(longs, hnet)[0], dstats(longs, hlift)[0]
    dby = drop_best_year(longs, net)
    c1 = (pooled["mean"] or 0) > 0 and (pooled["t"] or 0) >= T_PASS and (mt["t"] or 0) >= T_PASS
    c2 = all((halves[h]["net"]["mean"] or 0) > 0 and (halves[h]["hedged_lift"]["mean"] or -1) >= 0 for h in HALVES)
    c3 = (p_hnet["mean"] or 0) > 0 and (p_hlift["mean"] if p_hlift["mean"] is not None else -1) >= 0
    c4 = (dby["mean_without"] or 0) > 0
    fail = ((pooled["t"] is not None and pooled["t"] <= -2)
            or any(halves[h]["net"]["t"] is not None and halves[h]["net"]["t"] <= -2 for h in HALVES)
            or (pooled["ub"] is not None and pooled["ub"] <= 0))
    lift_neg = any((halves[h]["hedged_lift"]["mean"] or 0) < 0 for h in HALVES) or (p_hlift["mean"] or 0) < 0
    if fail:
        label = "FAIL"
    elif c1 and c2 and c3 and c4:
        robust = (pooled["t"] or 0) >= T_BATCH and all((halves[h]["net"]["t"] or 0) > 0 for h in HALVES)
        label = "PASS (batch-robust)" if robust else "PASS (single-test), not batch-robust"
    elif c1 and c4 and lift_neg:
        label = "SELECTION ONLY (in-play names drift, timing adds nothing)"
    else:
        label = "NOT PASSED"
    tags = []
    if label.startswith("PASS"):
        late = dstats([x for x in longs if x["day"] >= "2024-01-01"], net)[0]
        if (late["mean"] or 0) <= 0:
            tags.append("DECAYED, no forward")
        if (halves["H1"]["net_20bp"] or 0) <= 0:
            tags.append("COST-FRAGILE")
        tags.append("SIP-only: a forward test needs an IEX-score cell (2021+) or paid SIP before any desk use")
    return {"label": label, "tags": tags, "checks": {"1_pooled_day_and_month_t": c1, "2_halves_net_and_hedged_lift": c2,
            "3_pooled_hedged": c3, "4_drop_best_year": c4}, "pooled_net": pooled, "month_t": mt, "halves": halves,
            "pooled_hedged_net": p_hnet, "pooled_hedged_lift": p_hlift, "drop_best_year": dby}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for f in ("--fetch-daily", "--fetch-open", "--fetch-day", "--score"):
        ap.add_argument(f, action="store_true")
    ap.add_argument("--skeptic-ready", default="")
    ap.add_argument("--prereg-commit", default="")
    a = ap.parse_args()
    if a.fetch_daily:
        return fetch_daily()
    if a.fetch_open:
        return fetch_open()
    if a.fetch_day:
        return fetch_day()
    reqs = {}
    for lg in ("daily_fetch_log.json", "open_fetch_log.json", "day_fetch_log.json"):
        j = json.load(open(os.path.join(OUT_DIR, lg)))
        if j.get("failed"):
            raise SystemExit(f"{lg} has failures (C4)")
        reqs[lg] = j.get("requests")
    if not a.score:
        daily = pickle.load(open(os.path.join(OUT_DIR, "daily.pkl"), "rb"))
        opens = pickle.load(open(os.path.join(OUT_DIR, "open.pkl"), "rb"))
        full = pickle.load(open(os.path.join(OUT_DIR, "fullday.pkl"), "rb"))
        stored = pickle.load(open(os.path.join(OUT_DIR, "open_union.pkl"), "rb"))
        univ = universe(daily)
        lb = lookback_misses({d: v for d, v in univ.items()}, opens, stored)
        if lb["misses"] or lb["missing_sessions"]:
            raise SystemExit(f"look-back check failed: {lb['misses']} misses, "
                             f"{len(lb['missing_sessions'])} sessions missing (refetch)")
        cov = coverage(daily, opens, full)
        if cov["e_low_cutoff_days"]:
            raise SystemExit(f"ABORT (coverage e): 1,000th-name cutoff < $20M on {cov['e_low_cutoff_days'][:5]}...")
        if cov["c_failed_years"]:
            raise SystemExit(f"coverage (c) failed for {cov['c_failed_years']}: refetch; if it still fails, ABORT")
        rows, st, refused = build_rows(set(cov["excluded_years"]))
        scored_days = {d for d in univ if half_of(d) and d[:4] not in cov["excluded_years"]}
        if scored_days and len(refused) / len(scored_days) > MAX_SPY_REFUSED:
            raise SystemExit(f"ABORT: {len(refused)} days refused for missing SPY bars (> 0.5%)")
        longs = [x for x in rows if x["side"] == "long"]
        cnt = {"stats": dict(st), "module_sha256": module_shas(), "prereg_commit": a.prereg_commit,
               "requests": reqs, "coverage": cov, "lookback": {"misses": 0, "missing_sessions": 0}}
        cut = [v for d, v in universe.cutoff_dv.items() if v is not None and half_of(d)]
        cnt["top1000_cutoff_dollar_volume"] = {"min": min(cut), "median": statistics.median(cut)} if cut else None
        for h in HALVES:
            hr = [x for x in longs if x["half"] == h]
            days = len({x["day"] for x in hr})
            cnt[h] = {"long_trades": len(hr), "days": days, "mde": mde_of(hr),
                      "floors_met": len(hr) >= FLOOR_TRADES and days >= FLOOR_DAYS}
        cnt["pooled_mde"] = mde_of(longs)
        cnt["powered"] = (all(cnt[h]["floors_met"] for h in HALVES) and cnt["pooled_mde"] is not None
                          and cnt["pooled_mde"] <= MDE_BAR)
        print(json.dumps(cnt, indent=1, default=str))
        print(write("count.json", cnt))
        return
    if not a.skeptic_ready:
        raise SystemExit("--score needs --skeptic-ready")
    cnt = json.load(open(os.path.join(OUT_DIR, "count.json")))
    if cnt.get("module_sha256") != module_shas() or not cnt.get("prereg_commit"):
        raise SystemExit("count.json from different code or without --prereg-commit")
    if not cnt.get("powered"):
        raise SystemExit("UNDERPOWERED at the count step: no outcome is read")
    rows, st, _ = build_rows(set(cnt["coverage"]["excluded_years"]))
    longs = [x for x in rows if x["side"] == "long"]
    res = {"verdict": verdict(longs), "info": {}}
    net = lambda x: x["gross"] - COST                                                      # noqa: E731
    years = sorted({x["day"][:4] for x in longs})
    res["info"]["by_year_net"] = {y: dstats([x for x in longs if x["day"][:4] == y], net)[0]["mean"] for y in years}
    res["info"]["paper_window_2016_2023"] = dstats([x for x in longs if x["day"] < "2024-01-01"], net)[0]
    res["info"]["after_2024"] = dstats([x for x in longs if x["day"] >= "2024-01-01"], net)[0]
    res["info"]["px20_long_net"] = dstats([x for x in longs if x["entry"] >= 20], net)[0]["mean"]
    res["info"]["top10_long_net"] = dstats([x for x in longs if x["rank"] < 10], net)[0]["mean"]
    res["info"]["short_net"] = dstats([x for x in rows if x["side"] == "short"], net)[0]["mean"]
    res["info"]["net_by_cost"] = {f"{round(c * 1e4)}bp": dstats(longs, lambda x, c=c: x["gross"] - c)[0]["mean"]
                                  for c in COSTS}
    res["declared"] = ["universe = the gap-down study's ever-liquid list (active + inactive; reused tickers lost, "
                       "heaviest 2016-18); RAW prior close for the $10 floor, ATR in raw dollars, RAW full-day bars",
                       "beta-1 SPY hedge binding; no by-year / post-publication / $20+ / hedge-variant cell is "
                       "promotable without its own prereg",
                       "held-out confirmation (~70 sessions, MDE ~46 bp) can only falsify (t <= -2)"]
    res["skeptic_ready"], res["module_sha256"], res["stats"] = a.skeptic_ready, module_shas(), dict(st)
    write("result.json", res)
    print(json.dumps(res, indent=1, default=str))


if __name__ == "__main__":
    main()
