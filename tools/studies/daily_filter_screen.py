#!/usr/bin/env python3
"""Do daily trend or a news catalyst separate good one-arm entries from bad?

Population: book-admitted names, events after admission, >= $20, 09:40-15:30
(same as tools/entry_screen.py). Two event kinds per name-day:
  mid_rise   the one arm (fast %R -50 cross, slow rising)
  ~control   every 5th minute (a random minute in the same names)

Daily features (SIP daily bars, known before the open):
  above_20h   yesterday's close >= highest close of the 20 days before it
  above_50ma  yesterday's close > 50-day simple average of closes
  ret5        5-day return to yesterday's close, %
  gap         today's open vs yesterday's close, %
Catalyst (Alpaca news API):
  news        >= 1 story on the symbol from 16:00 the prior day to the event

For each feature bucket: up1 (+1% before -1%) and r30 for mid_rise and
control, per date half. A useful filter lifts up1 above the all-names control
(the bar every entry must clear) in BOTH halves.
"""
from __future__ import annotations

import collections
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)
import bars  # noqa: E402
import entry_screen as es  # noqa: E402
import runway_study as rs  # noqa: E402

DAYS = ["2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18",
        "2026-09-21", "2026-09-22", "2026-09-23"]
H1 = set(DAYS[:4])


def daily_features(syms: set[str]) -> dict:
    """{(sym, day): feature dict} from SIP daily bars."""
    cl = bars.client()
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    start = datetime.strptime(DAYS[0], "%Y-%m-%d") - timedelta(days=110)
    # The plan refuses SIP newer than 15 minutes: end 20 minutes ago.
    end = datetime.now(timezone.utc) - timedelta(minutes=20)
    out: dict = {}
    syms = sorted(syms)
    for i in range(0, len(syms), 100):
        chunk = syms[i:i + 100]
        try:
            resp = cl.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=chunk, timeframe=TimeFrame.Day,
                start=start.replace(tzinfo=timezone.utc), end=end,
                feed=DataFeed.SIP, limit=100000))
        except Exception as e:  # noqa: BLE001
            print("daily fail", str(e)[:80], file=sys.stderr)
            continue
        data = getattr(resp, "data", {}) or {}
        for s, seq in data.items():
            days = [bars.day_of(b.timestamp.timestamp()) for b in seq]
            closes = [float(b.close) for b in seq]
            opens = [float(b.open) for b in seq]
            for d in DAYS:
                if d not in days:
                    continue
                k = days.index(d)
                if k < 51:
                    continue
                prev = closes[k - 1]
                f = {"above_20h": prev >= max(closes[k - 21:k - 1]),
                     "above_50ma": prev > sum(closes[k - 51:k - 1]) / 50,
                     "ret5": (prev / closes[k - 6] - 1) * 100,
                     "gap": (opens[k] / prev - 1) * 100}
                out[(s, d)] = f
        time.sleep(1.0)
    return out


def news_times(want: dict) -> dict:
    """{(sym, day): sorted story timestamps from 16:00 the prior day}."""
    from alpaca.data.historical.news import NewsClient
    from alpaca.data.requests import NewsRequest
    import json
    sec = json.load(open(os.path.join(ROOT, "config", "secrets.json")))
    nc = NewsClient(sec["api_key"], sec["secret_key"])
    out = collections.defaultdict(list)
    for d, syms in want.items():
        day = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=bars.ET)
        prior = day - timedelta(days=3 if day.weekday() == 0 else 1)
        start = prior.replace(hour=16).astimezone(timezone.utc)
        end = day.replace(hour=15, minute=30).astimezone(timezone.utc)
        syms = sorted(s for s in syms if s != "SPY")
        for i in range(0, len(syms), 40):
            chunk = ",".join(syms[i:i + 40])
            token, pages = None, 0
            while pages < 30:
                try:
                    kw = dict(symbols=chunk, start=start, end=end, limit=50,
                              include_content=False)
                    if token:
                        kw["page_token"] = token
                    resp = nc.get_news(NewsRequest(**kw))
                except Exception as e:  # noqa: BLE001
                    print("news fail", d, str(e)[:80], file=sys.stderr)
                    break
                items = getattr(resp, "news", None)
                if items is None:
                    items = (getattr(resp, "data", {}) or {}).get("news", [])
                for n in items or []:
                    ts = n.created_at.timestamp()
                    for s in (n.symbols or []):
                        if s in syms:
                            out[(s, d)].append(ts)
                token = getattr(resp, "next_page_token", None)
                pages += 1
                if not token:
                    break
                time.sleep(0.4)
            time.sleep(0.4)
    return {k: sorted(v) for k, v in out.items()}


def main():
    from config import load_config
    cfg = load_config()
    first: dict = {}
    want = es.admitted(DAYS, first)
    for d in DAYS:
        want[d].add("SPY")
    ext = es.fetch_ext(want)
    allsyms = {s for v in want.values() for s in v if s != "SPY"}
    print("name-days:", sum(len(v) - 1 for v in want.values()), "symbols:", len(allsyms),
          file=sys.stderr)
    dfeat = daily_features(allsyms)
    news = news_times(want)
    print("daily feats:", len(dfeat), "name-days with any news:", len(news), file=sys.stderr)

    rows = []   # (kind, day, sym, feats, score)
    for d, syms in want.items():
        spy = ext.get(("SPY", d))
        for s in syms:
            if s == "SPY":
                continue
            df = ext.get((s, d))
            if df is None or len(df) < 150:
                continue
            ind = es.indicators(df, cfg, spy)
            B = es.to_B(df)
            t_on = first.get((s, d))
            base = dfeat.get((s, d))
            last = -1e18
            for i in range(2, len(B[0]) - 1):
                m = ind["mins"][i]
                if m < 9 * 60 + 40 or m > 15 * 60 + 30 or B[4][i] < 20:
                    continue
                if t_on is None or ind["ts"][i] < t_on:
                    continue
                kinds = []
                if i % 5 == 0:
                    kinds.append("~control")
                if "mid_rise" in es.fire(ind, i) and ind["ts"][i] - last >= es.COOL:
                    last = ind["ts"][i]
                    kinds.append("mid_rise")
                if not kinds:
                    continue
                sc = rs.score_path(B, i, B[4][i])
                if not sc:
                    continue
                nt = news.get((s, d), [])
                f = dict(base or {})
                f["news"] = any(t <= ind["ts"][i] for t in nt)
                f["news60"] = any(ind["ts"][i] - 3600 <= t <= ind["ts"][i] for t in nt)
                f["news_n"] = sum(1 for t in nt if t <= ind["ts"][i])
                for k in kinds:
                    rows.append((k, d, s, f, sc))

    ctrl_all = [r for r in rows if r[0] == "~control"]
    base_up = rs.mean([r[4]["tp_1.0"] for r in ctrl_all if r[4].get("tp_1.0") is not None])
    print(f"BASELINE: random minute, all names >= $20: up1 {base_up:.1%}  (n={len(ctrl_all)})\n")

    def cell(rs_):
        u = [r[4]["tp_1.0"] for r in rs_ if r[4].get("tp_1.0") is not None]
        r3 = [r[4]["ret_30"] for r in rs_ if r[4].get("ret_30") is not None]
        if len(u) < 15:
            return f"{'n<15':>26}"
        return f"{rs.mean(u):>6.1%} {rs.mean(r3):>+7.3f} n={len(u):<5}"

    def z(rs_):
        u = [r[4]["tp_1.0"] for r in rs_ if r[4].get("tp_1.0") is not None]
        if len(u) < 15:
            return float("nan")
        p = rs.mean(u)
        return (p - base_up) / math.sqrt(base_up * (1 - base_up) / len(u))

    splits = [
        ("above 20-day high", lambda f: f.get("above_20h") is True),
        ("NOT above 20-day high", lambda f: f.get("above_20h") is False),
        ("above 50-day avg", lambda f: f.get("above_50ma") is True),
        ("below 50-day avg", lambda f: f.get("above_50ma") is False),
        ("5-day return > +5%", lambda f: (f.get("ret5") or 0) > 5),
        ("5-day return < 0", lambda f: f.get("ret5") is not None and f["ret5"] < 0),
        ("gap up > +2%", lambda f: (f.get("gap") or 0) > 2),
        ("gap flat -1..+1%", lambda f: f.get("gap") is not None and -1 <= f["gap"] <= 1),
        ("gap down < -1%", lambda f: f.get("gap") is not None and f["gap"] < -1),
        ("news before event", lambda f: f.get("news") is True),
        ("no news", lambda f: f.get("news") is False),
        ("news in last 60 min", lambda f: f.get("news60") is True),
        ("3+ stories since close", lambda f: (f.get("news_n") or 0) >= 3),
        ("above 50ma AND news", lambda f: f.get("above_50ma") is True and f.get("news") is True),
        ("above 20h AND news", lambda f: f.get("above_20h") is True and f.get("news") is True),
    ]
    print(f"{'bucket':<24}{'kind':<10}{'ALL up1  r30':>26}{'half 1':>26}{'half 2':>26}{'z vs base':>11}")
    for name, pred in splits:
        for kind in ("~control", "mid_rise"):
            sel = [r for r in rows if r[0] == kind and pred(r[3])]
            a = [r for r in sel if r[1] in H1]
            b = [r for r in sel if r[1] not in H1]
            print(f"{name:<24}{kind:<10}{cell(sel):>26}{cell(a):>26}{cell(b):>26}{z(sel):>+11.1f}")
        print()


if __name__ == "__main__":
    main()
