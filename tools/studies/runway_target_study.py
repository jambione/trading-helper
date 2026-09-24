#!/usr/bin/env python3
"""What predicts RUNWAY: +2% before -1% within 60 minutes?

The earlier screens asked whether a moment beats a random minute on a
symmetric +1%/-1% test. The operator's question is asymmetric: does the name
run far enough to clear a 1% stop? Label per candidate moment:
  run2   first touch of +2% comes before -1% within 60 minutes (1 / 0;
         neither = 0: no runway is no runway)
  run15  same with +1.5%

Population: book-admitted names >= $20, after admission, 09:40-15:30,
every 5th minute (plus a flag for one-arm events), 2026-09-14..23.
Features, all known at the moment:
  sec_rs, sec_rs15, stk_vs_sec   sector ETF vs SPY; stock vs sector
  gap, move_open, ret_15m, range_pos, vs_vwap, vol_1m
  rvol_pace   volume so far / (20-day avg daily volume x expected share
              of the day by now) — unusual volume for THIS stock, now
  news_n, news60, above_50ma, above_20h, ret5, price, spread, mid_rise, mins

TEST 1 per feature: run2 rate, top vs bottom tercile, both date halves.
TEST 2 held out: rank moments by the features whose direction was stable
in half 1 only; grade the top third of half 2 on run2, and on the net
result of tomorrow's exits (1% seed, arm +0.3%, 0.35% from peak) after
the name-day spread.
"""
from __future__ import annotations

import collections
import math
import os
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))
sys.path.insert(0, ROOT)
import bars  # noqa: E402
import daily_filter_screen as dfs  # noqa: E402
import entry_screen as es  # noqa: E402
import exit_study as ex  # noqa: E402
import name_rank_study as nr  # noqa: E402
import sector_study as ss  # noqa: E402

DAYS = ss.DAYS
H1 = set(DAYS[:4])


def first_touch(B, i, up, dn, horizon=60):
    t, _o, h, l, c, _v = B
    px = c[i]
    hi, lo = px * (1 + up / 100), px * (1 - dn / 100)
    end = t[i] + horizon * 60
    for k in range(i + 1, len(t)):
        if t[k] > end:
            break
        if l[k] <= lo:
            return 0
        if h[k] >= hi:
            return 1
    return 0


def avg_volume(syms) -> dict:
    """{(sym, day): 20-day average daily SIP volume before that day}."""
    cl = bars.client()
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    start = datetime.strptime(DAYS[0], "%Y-%m-%d") - timedelta(days=45)
    end = datetime.now(timezone.utc) - timedelta(minutes=20)
    out = {}
    syms = sorted(syms)
    for i in range(0, len(syms), 100):
        try:
            data = cl.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=syms[i:i + 100], timeframe=TimeFrame.Day,
                start=start.replace(tzinfo=timezone.utc), end=end,
                feed=DataFeed.SIP)).data
        except Exception as e:  # noqa: BLE001
            print("avgvol fail", str(e)[:80], file=sys.stderr)
            continue
        for s, seq in data.items():
            days = [bars.day_of(b.timestamp.timestamp()) for b in seq]
            vols = [float(b.volume) for b in seq]
            for d in DAYS:
                if d in days:
                    k = days.index(d)
                    if k >= 20:
                        out[(s, d)] = sum(vols[k - 20:k]) / 20
        time.sleep(1.0)
    return out


def run():
    from config import load_config
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import morning_funnel as mf
    cfg = load_config()
    first: dict = {}
    want = es.admitted(DAYS, first)
    syms_all = sorted({s for v in want.values() for s in v})
    ind = ss.industries(syms_all)
    for d in DAYS:
        want[d] |= set(ss.ETFS) | {"SPY"}
    ext = es.fetch_ext(want)
    dfeat = dfs.daily_features(set(syms_all))
    news = dfs.news_times({d: {s for s in v if s not in ss.ETFS and s != "SPY"}
                           for d, v in want.items()})
    av = avg_volume(set(syms_all))
    cl = bars.client()
    rows = []
    for d in DAYS:
        spy = ext.get(("SPY", d))
        if spy is None:
            continue
        SB = es.to_B(spy)
        eB = {e: es.to_B(ext[(e, d)]) for e in ss.ETFS if ext.get((e, d)) is not None}
        for s in sorted(want[d]):
            if s in ss.ETFS or s == "SPY":
                continue
            df = ext.get((s, d))
            f0 = dfeat.get((s, d)) or {}
            if df is None or len(df) < 150 or f0.get("gap") is None:
                continue
            sp = nr.name_day_spread(cl, s, d)
            time.sleep(0.2)
            E = eB.get(ss.etf_for(ind.get(s)))
            indi = es.indicators(df, cfg, spy)
            B = es.to_B(df)
            t, o, h, l, c, v = B
            t_on = first.get((s, d))
            nt = news.get((s, d), [])
            avgv = av.get((s, d))
            first_rth = indi["first"]
            last_mr = -1e18
            for i in range(first_rth + 16, len(t) - 1):
                m = indi["mins"][i]
                if m < 9 * 60 + 40 or m > 15 * 60 + 30 or c[i] < 20:
                    continue
                if t_on is None or t[i] < t_on:
                    continue
                fired = "mid_rise" in es.fire(indi, i) and t[i] - last_mr >= es.COOL
                if fired:
                    last_mr = t[i]
                if i % 5 and not fired:
                    continue
                k = i - 1
                f = {"gap": f0["gap"], "above_50ma": float(bool(f0.get("above_50ma"))),
                     "above_20h": float(bool(f0.get("above_20h"))), "ret5": f0.get("ret5"),
                     "price": c[i], "spread": sp, "mins": m - 570,
                     "move_open": (c[k] / o[first_rth] - 1) * 100,
                     "ret_15m": (c[k] / c[k - 15] - 1) * 100,
                     "news_n": float(sum(1 for x in nt if x <= t[i])),
                     "news60": float(any(t[i] - 3600 <= x <= t[i] for x in nt))}
                hi_, lo_ = max(h[first_rth:k + 1]), min(l[first_rth:k + 1])
                if hi_ > lo_:
                    f["range_pos"] = (c[k] - lo_) / (hi_ - lo_) * 100
                if not math.isnan(indi["vwap"][k]):
                    f["vs_vwap"] = (c[k] / indi["vwap"][k] - 1) * 100
                rets = [(c[j] / c[j - 1] - 1) * 100 for j in range(k - 14, k + 1)]
                f["vol_1m"] = statistics.stdev(rets)
                if avgv:
                    so_far = sum(v[first_rth:k + 1])
                    frac = mf.expected_fraction(m - 570)
                    if frac > 0:
                        f["rvol_pace"] = so_far / (avgv * frac)
                if E is not None:
                    ke, ks = bars.index_at(E[0], t[i]), bars.index_at(SB[0], t[i])
                    me, msp = ss.move_since_open(E, ke), ss.move_since_open(SB, ks)
                    if me is not None and msp is not None and ke >= 15 and ks >= 15:
                        f["sec_rs"] = me - msp
                        f["sec_rs15"] = ((E[4][ke] / E[4][ke - 15]) - (SB[4][ks] / SB[4][ks - 15])) * 100
                        f["stk_vs_sec"] = f["move_open"] - me
                g, _ = ex.walk(B, i, c[i], stop_pct=1.0, arm_pct=0.3, trail_pct=0.35)
                rows.append({"day": d, "sym": s, "f": f, "mid_rise": fired,
                             "run2": first_touch(B, i, 2.0, 1.0),
                             "run15": first_touch(B, i, 1.5, 1.0),
                             "net": g - (sp if sp is not None else 0.11)})
        print(f"  {d}: {len(rows)} rows", file=sys.stderr)

    base = statistics.mean(r["run2"] for r in rows)
    base15 = statistics.mean(r["run15"] for r in rows)
    mr = [r for r in rows if r["mid_rise"]]
    print(f"RUNWAY TARGET STUDY {DAYS[0]}..{DAYS[-1]}: {len(rows)} moments "
          f"({len(mr)} one-arm events)\n")
    print(f"  base rate: +2% before -1% in 60m {base:.1%};  +1.5% before -1% {base15:.1%}")
    print(f"  one-arm events: run2 {statistics.mean(r['run2'] for r in mr):.1%}, "
          f"run15 {statistics.mean(r['run15'] for r in mr):.1%}\n")

    feats = sorted({k for r in rows for k in r["f"]})
    print(f"  {'feature':<12}{'n':>6}{'run2 lo':>9}{'hi':>7}{'z':>7}{'h1 lo/hi':>14}{'h2 lo/hi':>14}"
          f"{'net lo':>9}{'net hi':>8}  cuts")
    stable_h1 = {}
    for fn in feats:
        pts = [(r["f"][fn], r) for r in rows if r["f"].get(fn) is not None]
        if len(pts) < 300:
            continue
        xs = sorted(p[0] for p in pts)
        a, b = xs[len(xs) // 3], xs[2 * len(xs) // 3]
        lo = [r for x, r in pts if x <= a]
        hi = [r for x, r in pts if x >= b]
        if a == b:
            lo = [r for x, r in pts if x < a]
            hi = [r for x, r in pts if x >= b]
        if len(lo) < 50 or len(hi) < 50:
            continue
        pl, ph = statistics.mean(r["run2"] for r in lo), statistics.mean(r["run2"] for r in hi)
        p = (pl * len(lo) + ph * len(hi)) / (len(lo) + len(hi))
        se = math.sqrt(max(p * (1 - p), 1e-9) * (1 / len(lo) + 1 / len(hi)))
        z = (ph - pl) / se
        def half(g, hs):
            xs_ = [r["run2"] for r in g if (r["day"] in H1) == hs]
            return statistics.mean(xs_) if xs_ else float("nan")
        l1, h1_ = half(lo, True), half(hi, True)
        l2, h2_ = half(lo, False), half(hi, False)
        if not math.isnan(l1) and not math.isnan(h1_) and abs(h1_ - l1) >= 0.02:
            stable_h1[fn] = (1 if h1_ > l1 else -1, a, b)
        stable = (l1 < h1_) == (l2 < h2_) and abs(z) >= 2
        print(f"  {fn:<12}{len(pts):>6}{pl:>9.1%}{ph:>7.1%}{z:>+7.1f}"
              f"{l1:>7.1%}/{h1_:<6.1%}{l2:>7.1%}/{h2_:<6.1%}"
              f"{statistics.mean(r['net'] for r in lo):>+9.3f}{statistics.mean(r['net'] for r in hi):>+8.3f}"
              f"  {a:.3g}|{b:.3g}{'  STABLE' if stable else ''}")

    # held-out score: features chosen and oriented on half 1 only
    test = [r for r in rows if r["day"] not in H1]
    print(f"\n  HELD OUT: score from half-1 directions ({len(stable_h1)} features: "
          f"{', '.join(f'{k}{chr(43) if d > 0 else chr(45)}' for k, (d, _a, _b) in stable_h1.items())})")
    def score(r):
        sc = 0
        for fn, (dirn, a, b) in stable_h1.items():
            x = r["f"].get(fn)
            if x is None:
                continue
            sc += dirn * (1 if x >= b else (-1 if x <= a else 0))
        return sc
    test.sort(key=score)
    n3 = len(test) // 3
    for lab, grp in (("bottom third", test[:n3]), ("middle", test[n3:2 * n3]),
                     ("TOP third", test[2 * n3:])):
        mrg = [r for r in grp if r["mid_rise"]]
        print(f"    {lab:<14} n={len(grp):<5} run2 {statistics.mean(r['run2'] for r in grp):.1%}  "
              f"run15 {statistics.mean(r['run15'] for r in grp):.1%}  "
              f"net/trade {statistics.mean(r['net'] for r in grp):+.3f}%   "
              f"one-arm inside: n={len(mrg)} net "
              f"{(statistics.mean(r['net'] for r in mrg) if mrg else float('nan')):+.3f}%")
    print(f"    (half-2 base: run2 {statistics.mean(r['run2'] for r in test):.1%}, "
          f"net {statistics.mean(r['net'] for r in test):+.3f}%)")


if __name__ == "__main__":
    run()
