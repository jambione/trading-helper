#!/usr/bin/env python3
"""Analyst upgrades in regular hours (Bradley, Clarke, Lee & Ornthanalai 2014). Spec:
docs/studies/analyst_upgrade_prereg.json (amended, amended_2; common C1-C4).

Events: Benzinga headlines (Alpaca news) containing 'upgrade' and not 'downgrade', exactly one symbol, created
09:45 .. 15:30 ET, first per name-day; liquidity: prior close >= $10 and 20-day mean dollar volume >= $20M. Entry: the
first 1-minute bar starting >= T + 60 s (open >= $10); exit: the close of bar e+19 (last bar at or before). Event and
control returns are SPY-adjusted over the same minutes. Control: the same name on up to 5 previous in-window sessions
(>= 3), a seeded random minute in the same clock hour, same 20-minute hold; mean of available.

  --fetch-news   headlines per trading day (data-only account), cached
  --handcheck    print 30 seeded matches (headlines only) for the amended (2) check
  --fetch-bars   SIP 1-minute bars per event symbol (event day + 10 prior calendar sessions), daily bars, SPY minutes
  (default)      count step (outcome-blind: MDE from control - control2)
  --score        verdict; refuses without --skeptic-ready, the handcheck record and a count step from this code
"""
from __future__ import annotations

import argparse
import hashlib
import re
import json
import math
import os
import random
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ROOT = os.environ.get("EEH_ROOT") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT_DIR = os.path.join(ROOT, "ai_reports", "analyst_upgrade")
ET = ZoneInfo("America/New_York")
HALVES = {"A": (date(2025, 4, 1), date(2026, 1, 13)), "B": (date(2026, 4, 25), date(2026, 10, 15))}
T_LO, T_HI = 9 * 60 + 45, 15 * 60 + 30         # created in [09:45:00, 15:30:00)
CTRL_FROM, ENTRY_LAST = 9 * 60 + 46, 15 * 60 + 40
HOLD, DELAY_S = 20, 60
US_TICKER = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")      # amended_4: a symbol our US SIP feed can trade
MIN_PX, MIN_DV = 10.0, 20e6
COST, COSTS = 0.0005, (0.0003, 0.0005, 0.0010, 0.0020)
LIFT_BAR, T_PASS, T_BATCH = 0.0005, 2.0, 2.84
FLOOR_EVENTS, FLOOR_DAYS, CTRL_MIN, CTRL_MAX = 150, 30, 3, 5


def half_of(d: date) -> str | None:
    for h, (a, b) in HALVES.items():
        if a <= d <= b:
            return h
    return None


def weekdays() -> list[date]:
    out = []
    for a, b in HALVES.values():
        d = a
        while d <= b:
            if d.weekday() < 5:
                out.append(d)
            d += timedelta(days=1)
    return out


def is_upgrade(headline: str) -> bool:
    h = headline.lower()
    return "upgrade" in h and "downgrade" not in h


def _client_keys():
    s = json.load(open(os.path.join(ROOT, "secrets_data.json")))
    return s["alpaca_data_api_key"], s["alpaca_data_secret_key"]


def fetch_news() -> None:
    import requests
    k, sec = _client_keys()
    hdr = {"APCA-API-KEY-ID": k, "APCA-API-SECRET-KEY": sec}
    os.makedirs(os.path.join(OUT_DIR, "news"), exist_ok=True)
    failed = []
    for d in weekdays():
        p = os.path.join(OUT_DIR, "news", f"{d}.json")
        if os.path.exists(p):
            continue
        start = datetime(d.year, d.month, d.day, 0, 0, tzinfo=ET).astimezone(timezone.utc)   # whole day: dedupe first
        end = datetime(d.year, d.month, d.day, 15, 31, tzinfo=ET).astimezone(timezone.utc)
        out, tok, ok = [], None, True
        for _ in range(400):
            prm = {"start": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "limit": 50, "sort": "asc", "include_content": "false"}
            if tok:
                prm["page_token"] = tok
            for attempt in range(8):
                try:
                    r = requests.get("https://data.alpaca.markets/v1beta1/news", headers=hdr, params=prm, timeout=30)
                except Exception:  # noqa: BLE001
                    time.sleep(10)
                    continue
                if r.status_code == 429:
                    time.sleep(30)
                    continue
                break
            else:
                ok = False
                break
            if r.status_code != 200:
                ok = False
                break
            j = r.json()
            for n in j.get("news", []):
                if is_upgrade(n.get("headline", "")):
                    out.append({"id": n.get("id"), "ts": datetime.fromisoformat(n["created_at"].replace("Z", "+00:00"))
                                .timestamp(), "symbols": [str(s).upper() for s in n.get("symbols") or []],
                                "headline": n.get("headline", "")})
            tok = j.get("next_page_token")
            time.sleep(0.35)
            if not tok:
                break
        else:
            ok = False                                   # 400-page cap hit: the day is incomplete
        if not ok:
            failed.append(str(d))
            continue
        json.dump(out, open(p + ".tmp", "w"))
        os.replace(p + ".tmp", p)
        print(d, len(out), flush=True)
    json.dump({"failed": failed}, open(os.path.join(OUT_DIR, "news_fetch_log.json"), "w"))
    print("failed:", failed)


def upgrade_days() -> dict[str, set[str]]:
    """sym -> days with any matching headline (any time, any symbol count): excluded as control days."""
    out = defaultdict(set)
    for d in weekdays():
        p = os.path.join(OUT_DIR, "news", f"{d}.json")
        if os.path.exists(p):
            for n in json.load(open(p)):
                for s_ in n["symbols"]:
                    out[s_].add(str(d))
    return out


def events() -> list[dict]:
    """First matching single-symbol upgrade headline per name-day over the WHOLE day (from 00:00 ET), and only then
    the 09:45 .. 15:30 window: a name whose first upgrade came earlier that day has no event (amended_3 (1))."""
    out = []
    for d in weekdays():
        p = os.path.join(OUT_DIR, "news", f"{d}.json")
        if not os.path.exists(p):
            raise SystemExit(f"missing news day {d} (C4)")
        first = {}
        for n in sorted(json.load(open(p)), key=lambda x: x["ts"]):
            if len(n["symbols"]) != 1 or not is_upgrade(n["headline"]):
                continue
            first.setdefault(n["symbols"][0], n)
        for sym, n in sorted(first.items()):
            if not US_TICKER.match(sym):
                continue                                   # e.g. TSX:ENB: not on the US feed (amended_4)
            t = datetime.fromtimestamp(n["ts"], ET)
            m = t.hour * 60 + t.minute
            if T_LO <= m < T_HI:
                out.append({"sym": sym, "day": str(d), "ts": n["ts"], "headline": n["headline"]})
    return out


def rule_sha() -> str:
    import inspect
    return hashlib.sha256(inspect.getsource(is_upgrade).encode()).hexdigest()[:16]


def handcheck(not_upgrades: int | None) -> None:
    """Print 30 seeded matches; with --not-upgrades N, record the reviewer's count of non stock-rating upgrades."""
    ev = events()
    rng = random.Random(int(hashlib.sha256(b"upg-hand").hexdigest()[:16], 16))
    pick = rng.sample(ev, min(30, len(ev)))
    for e in pick:
        print(f"{e['day']} {e['sym']:6s} {e['headline'][:140]}")
    print(f"\n{len(ev)} events in total")
    if not_upgrades is not None:
        json.dump({"ids": [f"{e['day']}|{e['sym']}" for e in pick], "not_upgrades": not_upgrades,
                   "rule_sha": rule_sha(), "ok": not_upgrades <= 3}, open(os.path.join(OUT_DIR, "handcheck.json"), "w"),
                  indent=1)


def require_handcheck() -> None:
    p = os.path.join(OUT_DIR, "handcheck.json")
    if not os.path.exists(p):
        raise SystemExit("no handcheck.json (amended (2))")
    h = json.load(open(p))
    if h.get("rule_sha") != rule_sha() or not h.get("ok"):
        raise SystemExit("handcheck missing, failed (> 3 of 30), or for a different matching rule")


def fetch_bars() -> None:
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    cl = StockHistoricalDataClient(*_client_keys())
    bdir = os.path.join(OUT_DIR, "bars")
    os.makedirs(bdir, exist_ok=True)
    failed = []

    def get(sym, tf, a, b):
        for attempt in range(5):
            try:
                return cl.get_stock_bars(StockBarsRequest(symbol_or_symbols=sym, timeframe=tf, start=a, end=b,
                                                          feed=DataFeed.SIP, adjustment=Adjustment.RAW)
                                         ).data.get(sym, [])
            except Exception as e:  # noqa: BLE001
                print(f"  retry {sym}: {str(e)[:80]}", flush=True)
                time.sleep(15 * (attempt + 1))
        return None
    for e in events():
        p = os.path.join(bdir, f"{e['day']}_{e['sym']}.json")
        if os.path.exists(p):
            continue
        d = date.fromisoformat(e["day"])
        h = half_of(d)
        lo = max(HALVES[h][0], d - timedelta(days=12))          # controls stay inside the half (amended_2 (3))
        a = datetime(lo.year, lo.month, lo.day, 9, 30, tzinfo=ET).astimezone(timezone.utc)
        b = datetime(d.year, d.month, d.day, 16, 0, tzinfo=ET).astimezone(timezone.utc)
        mins = get(e["sym"], TimeFrame.Minute, a, b)
        h0 = HALVES[h][0]                                       # daily look-back stays inside the half (amended_3 (5))
        d_start, d_end = datetime(h0.year, h0.month, h0.day, tzinfo=ET), b - timedelta(days=1)
        # an event on the half's first day has no in-half look-back: empty, not a request (it fails liquidity)
        daily = get(e["sym"], TimeFrame.Day, d_start, d_end) if d_end > d_start else []
        if mins is None or daily is None:
            failed.append(p)
            continue
        rows = [[x.timestamp.astimezone(ET).strftime("%Y-%m-%d"),
                 x.timestamp.astimezone(ET).hour * 60 + x.timestamp.astimezone(ET).minute,
                 x.open, x.high, x.low, x.close] for x in mins]
        dl = [[x.timestamp.astimezone(ET).strftime("%Y-%m-%d"), x.close, x.volume] for x in daily]
        json.dump({"min": rows, "daily": dl}, open(p, "w"))
        time.sleep(0.35)
    spy = os.path.join(bdir, "SPY_minutes.json")
    if not os.path.exists(spy):
        rows, spy_ok = [], True
        for h, (a0, b0) in HALVES.items():
            m0 = a0 - timedelta(days=14)
            while m0 <= b0:
                m1 = min((m0.replace(day=28) + timedelta(days=4)).replace(day=1), b0 + timedelta(days=1))
                got = get("SPY", TimeFrame.Minute, datetime(m0.year, m0.month, m0.day, tzinfo=ET),
                          datetime(m1.year, m1.month, m1.day, tzinfo=ET))
                if got is None:
                    failed.append(f"SPY {m0}")
                    spy_ok = False
                    break
                rows += [[x.timestamp.astimezone(ET).strftime("%Y-%m-%d"),
                          x.timestamp.astimezone(ET).hour * 60 + x.timestamp.astimezone(ET).minute, x.open, x.close]
                         for x in got]
                m0 = m1
        if spy_ok:
            json.dump(rows, open(spy, "w"))                     # never a partial SPY file
    json.dump({"failed": failed}, open(os.path.join(OUT_DIR, "bars_fetch_log.json"), "w"))
    print("failed:", failed)


def window_ret(mins: list, day: str, entry_min: int) -> tuple[float, float, int, int] | None:
    """Entry = open of the first bar of `day` with minute >= entry_min; exit = close of the last bar <= entry + 19."""
    rows = [r for r in mins if r[0] == day]
    ent = next((r for r in rows if r[1] >= entry_min), None)
    if ent is None or ent[1] > ENTRY_LAST:
        return None
    ex = [r for r in rows if ent[1] <= r[1] <= ent[1] + HOLD - 1]
    return ent[2], ex[-1][5], ent[1], ex[-1][1]


def spy_ret(spy: dict, day: str, m0: int, m1: int) -> float | None:
    d = spy.get(day)
    if not d:
        return None
    o = next((r[2] for r in d if r[1] >= m0), None)
    c = [r[3] for r in d if r[1] <= m1]
    return c[-1] / o - 1 if o and c else None


def build_rows() -> tuple[list[dict], Counter]:
    bdir = os.path.join(OUT_DIR, "bars")
    spy_rows = json.load(open(os.path.join(bdir, "SPY_minutes.json")))
    spy = defaultdict(list)
    for r in spy_rows:
        spy[r[0]].append(r)
    rows, st = [], Counter()
    updays = upgrade_days()
    for e in events():
        p = os.path.join(bdir, f"{e['day']}_{e['sym']}.json")
        if not os.path.exists(p):
            raise SystemExit(f"no bars file {p} (C4)")
        b = json.load(open(p))
        daily = [x for x in b["daily"] if x[0] < e["day"]][-20:]
        if len(daily) < 20 or daily[-1][1] < MIN_PX or statistics.mean(x[1] * x[2] for x in daily) < MIN_DV:
            st["skip_liquidity"] += 1
            continue
        t = datetime.fromtimestamp(e["ts"] + DELAY_S, ET)
        entry_min = t.hour * 60 + t.minute + (1 if (t.second or t.microsecond) else 0)   # first bar STARTING >= T+60 s
        w = window_ret(b["min"], e["day"], entry_min)
        if w is None or w[0] < MIN_PX:
            st["skip_no_entry_or_below_10"] += 1
            continue
        s = spy_ret(spy, e["day"], w[2], w[3])
        if s is None:
            st["skip_no_spy"] += 1
            continue
        raw_ret = w[1] / w[0] - 1
        ev_ret = raw_ret - s
        hour = w[2] // 60
        h0 = str(HALVES[half_of(date.fromisoformat(e["day"]))][0])
        prior_days = sorted({r[0] for r in b["min"] if h0 <= r[0] < e["day"] and r[0] not in updays.get(e["sym"], set())}
                            )[-CTRL_MAX:]

        def ctrl(tag, single=False):
            vals = []
            days_ = prior_days
            if single and prior_days:                      # one seeded draw (power: amended_3 (3))
                rng0 = random.Random(int(hashlib.sha256(f"{e['sym']}|{e['day']}|{tag}|pick".encode()).hexdigest()[:16], 16))
                days_ = [rng0.choice(prior_days)]
            for pd in days_:
                cand = sorted({r[1] for r in b["min"] if r[0] == pd and r[1] // 60 == hour and r[1] >= CTRL_FROM
                               and r[1] + HOLD - 1 <= 959})
                if not cand:
                    continue
                rng = random.Random(int(hashlib.sha256(f"{e['sym']}|{e['day']}|{pd}|{tag}".encode())
                                        .hexdigest()[:16], 16))
                m = rng.choice(cand)
                cw = window_ret(b["min"], pd, m)
                cs = spy_ret(spy, pd, cw[2], cw[3]) if cw else None
                if cw and cs is not None:
                    vals.append(cw[1] / cw[0] - 1 - cs)
            if single:
                return vals[0] if vals else None
            return statistics.mean(vals) if len(vals) >= CTRL_MIN else None
        c1, c2, c_single = ctrl("upg-ctrl"), ctrl("upg-ctrl2"), ctrl("upg-ctrl1", single=True)
        if c1 is None:
            st["skip_too_few_control_days"] += 1
            continue
        reaction = None
        r0 = window_ret(b["min"], e["day"], datetime.fromtimestamp(e["ts"], ET).hour * 60
                        + datetime.fromtimestamp(e["ts"], ET).minute)
        if r0:
            reaction = w[0] / r0[0] - 1                   # headline-minute open -> our entry open (what the delay costs)
        rows.append({"day": e["day"], "sym": e["sym"], "half": half_of(date.fromisoformat(e["day"])),
                     "gross": ev_ret, "raw": raw_ret, "ctrl": c1, "ctrl2": c2, "ctrl_single": c_single,
                     "delay_move": reaction,
                     "target_raise": "price target" in e["headline"].lower()})
    return rows, st


def dstats(rows, key):
    import eod_reversal as E
    per = defaultdict(list)
    for x in rows:
        v = key(x)
        if v is not None:
            per[x["day"]].append(v)
    return E.stats([statistics.mean(v) for v in per.values()])


def module_shas() -> dict:
    return {"analyst_upgrade": hashlib.sha256(open(os.path.abspath(__file__), "rb").read()).hexdigest()}


def write(name, obj):
    os.makedirs(OUT_DIR, exist_ok=True)
    p = os.path.join(OUT_DIR, name)
    json.dump(obj, open(p + ".tmp", "w"), indent=1, default=str)
    os.replace(p + ".tmp", p)
    return p


def main() -> None:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for f in ("--fetch-news", "--handcheck", "--fetch-bars", "--score"):
        ap.add_argument(f, action="store_true")
    ap.add_argument("--not-upgrades", type=int, default=None)
    ap.add_argument("--skeptic-ready", default="")
    ap.add_argument("--prereg-commit", default="")
    a = ap.parse_args()
    if a.fetch_news:
        return fetch_news()
    if a.handcheck:
        return handcheck(a.not_upgrades)
    if a.fetch_bars:
        return fetch_bars()
    for lg in ("news_fetch_log.json", "bars_fetch_log.json"):
        if json.load(open(os.path.join(OUT_DIR, lg))).get("failed"):
            raise SystemExit(f"{lg} has failures (C4)")
    require_handcheck()
    rows, st = build_rows()
    if not a.score:
        cnt = {"stats": dict(st), "module_sha256": module_shas(), "prereg_commit": a.prereg_commit}
        for h in ("A", "B"):
            hr = [x for x in rows if x["half"] == h]
            per = defaultdict(list)
            for x in hr:                               # one control draw minus the ctrl2 mean (amended_3 (3))
                if x["ctrl2"] is not None and x["ctrl_single"] is not None:
                    per[x["day"]].append(x["ctrl_single"] - x["ctrl2"])
            xs = [statistics.mean(v) for v in per.values()]
            mde = (2.0 + 0.84) * statistics.stdev(xs) / math.sqrt(len(xs)) if len(xs) >= 2 else None
            days = len({x["day"] for x in hr})
            cnt[h] = {"events": len(hr), "days": days, "mde": mde,
                      "floors_met": len(hr) >= FLOOR_EVENTS and days >= FLOOR_DAYS and mde is not None
                      and mde <= LIFT_BAR}
        print(json.dumps(cnt, indent=1, default=str))
        print(write("count.json", cnt))
        return
    if not a.skeptic_ready:
        raise SystemExit("--score needs --skeptic-ready")
    cnt = json.load(open(os.path.join(OUT_DIR, "count.json")))
    if cnt.get("module_sha256") != module_shas() or not cnt.get("prereg_commit"):
        raise SystemExit("count.json from different code or without --prereg-commit")
    if not all(cnt[h]["floors_met"] for h in ("A", "B")):
        raise SystemExit("UNDERPOWERED at the count step: no outcome is read")
    res, hv, ubs, ts = {"halves": {}, "info": {}}, [], [], []
    for h in ("A", "B"):
        hr = [x for x in rows if x["half"] == h]
        lift = dstats(hr, lambda x: x["gross"] - x["ctrl"])
        net = dstats(hr, lambda x: x["gross"] - COST)
        if lift["t"] is not None and lift["t"] <= -2:      # the registered FAIL rule is on the lift only
            v = "FAIL"
        elif lift["mean"] >= LIFT_BAR and (lift["t"] or 0) >= T_PASS and net["mean"] > 0:
            v = "PASS"
        else:
            v = "NOT_PASSED"
        hv.append(v)
        ubs.append(lift["ub"])
        ts.append(lift["t"] or 0)
        res["halves"][h] = {"verdict": v, "lift": lift, "net": net,
                            "net_unhedged": dstats(hr, lambda x: x["raw"] - COST)["mean"],
                            "net_by_cost": {f"{round(c * 1e4)}bp": dstats(hr, lambda x, c=c: x["gross"] - c)["mean"]
                                            for c in COSTS}}
        res["info"][h] = {"delay_move_mean": dstats(hr, lambda x: x["delay_move"])["mean"],
                          "target_raise_lift": dstats([x for x in hr if x["target_raise"]],
                                                      lambda x: x["gross"] - x["ctrl"])["mean"]}
    verdict = ("FAIL" if "FAIL" in hv else
               ("PASS (batch-robust)" if min(ts) >= T_BATCH else "PASS (single-test), not batch-robust")
               if all(x == "PASS" for x in hv) else
               "FAIL" if all(u is not None and u < LIFT_BAR for u in ubs) else "UNDERPOWERED")
    res["verdict"] = verdict
    res["declared"] = ["downgrades (short side) and the T + 1 s entry are not built (information, dropped before "
                       "scoring); the delay-move cell reports the headline-minute open -> our entry open"]
    res["skeptic_ready"], res["module_sha256"] = a.skeptic_ready, module_shas()
    write("result.json", res)
    print(json.dumps(res, indent=1, default=str))


if __name__ == "__main__":
    main()
