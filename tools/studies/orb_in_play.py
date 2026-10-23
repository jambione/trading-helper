#!/usr/bin/env python3
"""Opening-range breakout on stocks in play (Zarattini, Barbon & Aziz 2024). Spec: docs/studies/orb_in_play_prereg.json
(amended, amended_2, amended_3; common C1-C4).

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
HALVES = {"A": ("2025-04-01", "2026-01-13"), "B": ("2026-05-26", "2026-10-15")}
FETCH = (("2025-02-01", "2026-01-13"), ("2026-04-27", "2026-10-15"))
HELD = ("2026-01-14", "2026-04-24")
TOP_UNIV, TOP_PLAY, OPEN_LB, OPEN_NEED = 1000, 20, 14, 10
MIN_PX, MIN_ATR = 10.0, 0.50
COST, COSTS = 0.0010, (0.0005, 0.0010, 0.0020)
T_PASS, T_BATCH, MDE_BAR = 2.0, 2.84, 0.0010
FETCH_DAILY_END = {"2026-01-13": "2026-01-13T16:00", "2026-10-15": "2026-10-15T16:00"}
FLOOR_TRADES, FLOOR_DAYS = 300, 20
TICK, M_FROM, M_CTRL_END, M_LAST = 0.01, 575, 900, 959


def _client():
    from alpaca.data.historical import StockHistoricalDataClient
    s = json.load(open(os.path.join(ROOT, "secrets_data.json")))
    return StockHistoricalDataClient(s["alpaca_data_api_key"], s["alpaca_data_secret_key"])


def _get(cl, syms, tf, a, b, failed, tag, raw: bool = False):
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.requests import StockBarsRequest
    for attempt in range(6):
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
    json.dump({"failed": failed}, open(os.path.join(OUT_DIR, "daily_fetch_log.json"), "w"))
    print("names", len(out), "failed", failed)


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


def fetch_open() -> None:
    from alpaca.data.timeframe import TimeFrame
    cl, failed = _client(), []
    daily = pickle.load(open(os.path.join(OUT_DIR, "daily.pkl"), "rb"))
    univ = universe(daily)
    union = sorted({s for v in univ.values() for s in v})
    days = sorted({d for v in daily.values() for d in v})
    out_p = os.path.join(OUT_DIR, "open.pkl")
    out = pickle.load(open(out_p, "rb")) if os.path.exists(out_p) else {}
    for n, d in enumerate(days):
        if d in out:
            continue
        a = datetime.fromisoformat(d).replace(hour=9, minute=30, tzinfo=ET)
        rec = {}
        ok = True
        for i in range(0, len(union), 100):
            # end is inclusive at Alpaca: 09:34:59 keeps the 09:35 bar out (skeptic r1 #1)
            got = _get(cl, union[i:i + 100], TimeFrame.Minute, a, a + timedelta(minutes=4, seconds=59), failed,
                       f"open {d} {i}")
            if got is None:
                ok = False
                break
            for s, bars in got.items():
                rec[s] = [(x.timestamp.astimezone(ET).minute, x.open, x.high, x.low, x.close, x.volume) for x in bars
                          if x.timestamp.astimezone(ET).hour == 9 and 30 <= x.timestamp.astimezone(ET).minute <= 34]
            time.sleep(0.4)
        if ok:
            out[d] = rec
        if n % 10 == 0:
            pickle.dump(out, open(out_p + ".tmp", "wb"))
            os.replace(out_p + ".tmp", out_p)
            print(d, len(rec), flush=True)
    pickle.dump(out, open(out_p + ".tmp", "wb"))
    os.replace(out_p + ".tmp", out_p)
    json.dump({"failed": failed, "union": len(union)}, open(os.path.join(OUT_DIR, "open_fetch_log.json"), "w"))
    print("failed", failed)


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
            hist = [sum(x[5] for x in opens[p][s]) for p in days[max(0, i - OPEN_LB):i] if opens[p].get(s)]
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
    for d, rows in sorted(ip.items()):
        if d in out:
            continue
        syms = [r[1] for r in rows]
        if not syms:
            continue
        a = datetime.fromisoformat(d).replace(hour=9, minute=30, tzinfo=ET)
        got = _get(cl, syms, TimeFrame.Minute, a, a.replace(hour=15, minute=59, second=59), failed, f"day {d}",
                   raw=True)                               # RAW full-day bars: range, trigger and exits (r1 #3)
        if got is None:
            continue
        out[d] = {s: [(x.timestamp.astimezone(ET).hour * 60 + x.timestamp.astimezone(ET).minute, x.open, x.high,
                       x.low, x.close) for x in bars] for s, bars in got.items()}
        time.sleep(0.4)
    pickle.dump(out, open(out_p + ".tmp", "wb"))
    os.replace(out_p + ".tmp", out_p)
    json.dump({"failed": failed}, open(os.path.join(OUT_DIR, "day_fetch_log.json"), "w"))
    print("days", len(out), "failed", failed)


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
            return {"gross": side * (fill / entry - 1), "why": "stop"}
    last = [r for r in rows if r[0] <= M_LAST]
    return {"gross": side * (last[-1][4] / entry - 1), "why": "time"}


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
                return walk(rows, j, o, o * (1 - side * sp), side, False)["gross"]
            return {**res, "trig_min": r[0], "ctrl": ctrl("orb-ctrl"), "ctrl2": ctrl("orb-ctrl2"), "entry": fill}
    return {"skip": "no_trigger"}


def build_rows() -> tuple[list[dict], Counter]:
    daily = pickle.load(open(os.path.join(OUT_DIR, "daily.pkl"), "rb"))
    opens = pickle.load(open(os.path.join(OUT_DIR, "open.pkl"), "rb"))
    full = pickle.load(open(os.path.join(OUT_DIR, "fullday.pkl"), "rb"))
    ip = in_play(daily, opens)
    rows, st = [], Counter()
    for d, top in sorted(ip.items()):
        h = next((k for k, (a, b) in HALVES.items() if a <= d <= b), None)
        if h is None:
            continue
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
            rows.append({"day": d, "sym": s, "half": h, "side": "long" if side > 0 else "short", "rank": rank, **t})
    return rows, st


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
    for lg in ("daily_fetch_log.json", "open_fetch_log.json", "day_fetch_log.json"):
        if json.load(open(os.path.join(OUT_DIR, lg))).get("failed"):
            raise SystemExit(f"{lg} has failures (C4)")
    rows, st = build_rows()
    longs = [x for x in rows if x["side"] == "long"]
    if not a.score:
        cnt = {"stats": dict(st), "module_sha256": module_shas(), "prereg_commit": a.prereg_commit}
        cut = [v for v in universe.cutoff_dv.values() if v is not None]
        cnt["top1000_cutoff_dollar_volume"] = {"min": min(cut), "median": statistics.median(cut)} if cut else None
        for h in HALVES:
            hr = [x for x in longs if x["half"] == h]
            _, per = dstats([x for x in hr if x["ctrl"] is not None],
                            lambda x: x["ctrl"] - COST)     # control leg only (amended_4)
            xs = [statistics.mean(v) for v in per.values()]
            mde = (2.0 + 0.84) * statistics.stdev(xs) / math.sqrt(len(xs)) if len(xs) >= 2 else None
            days = len({x["day"] for x in hr})
            cnt[h] = {"long_trades": len(hr), "days": days, "mde": mde,
                      "floors_met": len(hr) >= FLOOR_TRADES and days >= FLOOR_DAYS and mde is not None
                      and mde <= MDE_BAR}
        print(json.dumps(cnt, indent=1, default=str))
        print(write("count.json", cnt))
        return
    if not a.skeptic_ready:
        raise SystemExit("--score needs --skeptic-ready")
    cnt = json.load(open(os.path.join(OUT_DIR, "count.json")))
    if cnt.get("module_sha256") != module_shas() or not cnt.get("prereg_commit"):
        raise SystemExit("count.json from different code or without --prereg-commit")
    if not all(cnt[h]["floors_met"] for h in HALVES):
        raise SystemExit("UNDERPOWERED at the count step: no outcome is read")
    res, hv, ts, ubs = {"halves": {}, "info": {}}, [], [], []
    for h in HALVES:
        hr = [x for x in longs if x["half"] == h]
        net, _ = dstats(hr, lambda x: x["gross"] - COST)
        lift, _ = dstats(hr, lambda x: x["gross"] - x["ctrl"] if x["ctrl"] is not None else None)
        if net["t"] is not None and net["t"] <= -2:
            v = "FAIL"
        elif net["mean"] > 0 and (net["t"] or 0) >= T_PASS and lift["mean"] is not None and lift["mean"] >= 0:
            v = "PASS"
        elif net["mean"] > 0 and (net["t"] or 0) >= T_PASS:
            v = "SELECTION ONLY (in-play names drift, timing adds nothing)"
        else:
            v = "NOT_PASSED"
        hv.append(v)
        ts.append(net["t"] or 0)
        ubs.append(net["ub"])
        res["halves"][h] = {"verdict": v, "net": net, "lift": lift,
                            "net_by_cost": {f"{round(c * 1e4)}bp": dstats(hr, lambda x, c=c: x["gross"] - c)[0]
                                            ["mean"] for c in COSTS},
                            "exit_mix": dict(Counter(x["why"] for x in hr)),
                            "win_rate": sum(1 for x in hr if x["gross"] > COST) / max(1, len(hr))}
        sh = [x for x in rows if x["side"] == "short" and x["half"] == h]
        res["info"][h] = {"short_net": dstats(sh, lambda x: x["gross"] - COST)[0]["mean"],
                          "top10_long_net": dstats([x for x in hr if x["rank"] < 10], lambda x: x["gross"] - COST)[0]
                          ["mean"],
                          "px20_long_net": dstats([x for x in hr if x["entry"] >= 20], lambda x: x["gross"] - COST)[0]
                          ["mean"]}
    res["verdict"] = ("FAIL" if "FAIL" in hv else
                      ("PASS (batch-robust)" if min(ts) >= T_BATCH else "PASS (single-test), not batch-robust")
                      if all(x == "PASS" for x in hv) else
                      "FAIL" if all(u is not None and u <= 0 for u in ubs) else "UNDERPOWERED")
    res["declared"] = ["universe = the gap-down study's ever-liquid list (3,640 active+inactive names; a superset of "
                       "any top-1,000 dollar-volume universe); RAW prior close for the $10 floor, ATR in raw dollars, RAW "
                       "full-day bars (amended_4)",
                       "information cells not built: ATR stop, top 50, 15/30-minute ranges"]
    res["skeptic_ready"], res["module_sha256"] = a.skeptic_ready, module_shas()
    write("result.json", res)
    print(json.dumps(res, indent=1, default=str))


if __name__ == "__main__":
    main()
