#!/usr/bin/env python3
"""wr_trigger_study.py — does a Williams %R trigger see runway coming?

The operator's manual read: when BOTH %R lines are rising, a green run is
coming. The desk's trigger is narrower: fast %R crosses up through -50 while
the slow line happens to be rising. This scores several triggers against a
random-minute control on the same names and days, on what the operator
actually wants — runway:

  runway    +1% before -1% within 60 min (SIP 1m highs/lows, lows first)
  mfe30     best high within 30 min, %
  fwd N     close-to-close return N min later, bp (gross; costs ~8-16 bp)

Triggers (at the close of minute i; one per name per 15 min):
  live       fast crosses -50, slow rising (live: slow %R(112) above 2 bars ago)
  both_up    first minute both lines rise, fast starting below -50
  fast50     fast crosses -50, no slow condition
  slow_turn  slow line turns from falling to rising
  both50     slow crosses above -50 while fast is above -50
  vol_brk    volume > 3x its 20-min mean and close > the prior 15-min high
  live_vol   live trigger with volume > 2x its 20-min mean
  random     random minutes, same names and days (the control)

Universe: every name in ai_reports/source_study_bars.pkl (8 days, ~200
names/day, SIP 1m from 04:00 — built by source_optimal_study.py), $20-$100,
09:45-15:30 ET.
"""
from __future__ import annotations

import math
import os
import pickle
import random
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
import mid_rise_runway_study as mr  # noqa: E402

ET = ZoneInfo("America/New_York")
CACHE = os.path.join(ROOT, "ai_reports", "source_study_bars.pkl")
IEX_CACHE = os.path.join(ROOT, "ai_reports", "wr_study_iex_bars.pkl")
# WR_FEED=iex: detect every trigger on IEX 1m bars (what the live desk sees),
# score the outcome on SIP bars at the same minute.
FEED = os.environ.get("WR_FEED", "sip")
T0, T1 = 9 * 60 + 45, 15 * 60 + 30
REFRACT = 15  # minutes between two signals of one trigger on one name


def et_min(ts):
    d = datetime.fromtimestamp(ts, ET)
    return d.hour * 60 + d.minute


def lines(B):
    """Fast and slow %R exactly as the live engine builds them
    (signals.compute_percent_r_exhaustion with rte_slow_timeframe unset, i.e.
    TradingView's native layout): fast %R(21) EWM 7, slow %R(112) EWM 3, both
    on 1m bars; 'rising' = above its value 2 bars earlier (trend_lookback 2)."""
    t, o, h, l, c, v = B
    fast = mr.percent_r_series(h, l, c, 21, 7.0)
    slow = mr.percent_r_series(h, l, c, 112, 3.0)
    srise = [i >= 2 and slow[i] is not None and slow[i - 2] is not None and slow[i] > slow[i - 2]
             for i in range(len(c))]
    return fast, slow, srise


def signals(B):
    t, o, h, l, c, v = B
    fast, slow, srise = lines(B)
    out = defaultdict(list)
    for i in range(21, len(c) - 1):
        m = et_min(t[i] + 60)
        if not (T0 <= m <= T1) or not (20 <= c[i] <= 100):
            continue
        f0, f1 = fast[i - 1], fast[i]
        if f0 is None or f1 is None:
            continue
        x50 = f0 <= -50 < f1
        vm = statistics.mean(v[i - 20:i]) if i >= 20 else 0
        if x50 and srise[i]:
            out["live"].append(i)
        if x50:
            out["fast50"].append(i)
        frise = fast[i - 2] is not None and f1 > fast[i - 2]
        frise_prev = i >= 3 and fast[i - 3] is not None and f0 > fast[i - 3]
        both = frise and srise[i]
        both_prev = frise_prev and srise[i - 1]
        if both and not both_prev and f0 < -50:
            out["both_up"].append(i)
        if srise[i] and not srise[i - 1]:
            out["slow_turn"].append(i)
        s0, s1 = slow[i - 1], slow[i]
        if s0 is not None and s1 is not None and s0 <= -50 < s1 and f1 > -50:
            out["both50"].append(i)
        if vm > 0 and v[i] > 3 * vm and c[i] > max(h[i - 15:i]):
            out["vol_brk"].append(i)
        if x50 and srise[i] and vm > 0 and v[i] > 2 * vm:
            out["live_vol"].append(i)
    for k, idx in out.items():
        kept, last = [], -10 ** 9
        for i in idx:
            if i - last >= REFRACT:
                kept.append(i)
                last = i
        out[k] = kept
    return out


DELAY = int(os.environ.get("WR_DELAY", "0"))  # enter N minutes after the signal


def label(B, i):
    t, o, h, l, c, v = B
    i = min(i + DELAY, len(c) - 2)
    px = c[i]
    res = {}
    for n in (5, 15, 30):
        j = i + n
        res[f"fwd{n}"] = (c[j] / px - 1) * 1e4 if j < len(c) and t[j] - t[i] <= n * 60 * 1.5 else None
    j30 = min(len(c) - 1, i + 30)
    res["mfe30"] = (max(h[i + 1:j30 + 1]) / px - 1) * 100 if j30 > i else None
    run = 0
    for k in range(i + 1, min(len(c), i + 61)):
        if l[k] <= px * 0.99:
            break
        if h[k] >= px * 1.01:
            run = 1
            break
    res["runway"] = run
    return res


def summarize(rows):
    def m(k):
        xs = [r[k] for r in rows if r.get(k) is not None]
        return (statistics.mean(xs), statistics.pstdev(xs) / math.sqrt(len(xs))) if len(xs) > 1 else (None, None)
    return {k: m(k) for k in ("runway", "mfe30", "fwd5", "fwd15", "fwd30")}


def iex_bars(cache):
    iex = {}
    if os.path.exists(IEX_CACHE):
        iex = pickle.load(open(IEX_CACHE, "rb"))
    want = defaultdict(list)
    for (sym, day), rec in cache.items():
        if rec and rec[0] and (sym, day) not in iex:
            want[day].append(sym)
    if want:
        import time as _t
        from datetime import timezone
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
        sys.path.insert(0, ROOT)
        import ai_entry_watch as ew
        cl = ew._data_client()
        for day, syms in sorted(want.items()):
            d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
            for i in range(0, len(syms), 50):
                batch = syms[i:i + 50]
                got = {}
                try:
                    bs = cl.get_stock_bars(StockBarsRequest(
                        symbol_or_symbols=batch, timeframe=TimeFrame(1, TimeFrameUnit.Minute),
                        start=d.replace(hour=4).astimezone(timezone.utc),
                        end=d.replace(hour=16, minute=5).astimezone(timezone.utc), feed=DataFeed.IEX))
                    for sym, rows in (bs.data or {}).items():
                        got[sym] = ([r.timestamp.timestamp() for r in rows], [float(r.open) for r in rows],
                                    [float(r.high) for r in rows], [float(r.low) for r in rows],
                                    [float(r.close) for r in rows], [float(r.volume) for r in rows])
                except Exception as e:  # noqa: BLE001
                    print(f"  iex {day}: {e}"[:140])
                for s_ in batch:
                    iex[(s_, day)] = got.get(s_)
                _t.sleep(1.0)
            print(f"  iex bars {day}: {len(syms)}")
        pickle.dump(iex, open(IEX_CACHE, "wb"))
    return iex


def main():
    cache = pickle.load(open(CACHE, "rb"))
    iex = iex_bars(cache) if FEED == "iex" else {}
    days = sorted({d for _s, d in cache})
    half = set(days[: len(days) // 2])
    rng = random.Random(7)
    ev = defaultdict(list)
    for (sym, day), rec in cache.items():
        if not rec or not rec[0]:
            continue
        B = rec[0]
        if FEED == "iex":
            Bi = iex.get((sym, day))
            if not Bi or len(Bi[0]) < 150:
                continue
            pos = {ts: j for j, ts in enumerate(B[0])}
            sig = {k: [pos[Bi[0][i]] for i in idx if Bi[0][i] in pos]
                   for k, idx in signals(Bi).items()}
        else:
            sig = signals(B)
        for k, idx in sig.items():
            for i in idx:
                ev[k].append({"day": day, **label(B, i)})
        # control: random eligible minutes, as many as the live trigger found here (min 2)
        t, c = B[0], B[4]
        elig = [i for i in range(21, len(c) - 1)
                if T0 <= et_min(t[i] + 60) <= T1 and 20 <= c[i] <= 100]
        for i in rng.sample(elig, min(len(elig), max(2, len(sig.get("live", []))))):
            ev["random"].append({"day": day, **label(B, i)})
    base = summarize(ev["random"])
    print(f"detect on {FEED}; entry delay {DELAY} min; days {days[0]}..{days[-1]} ({len(days)}); names x days {len(cache)}; "
          f"gross, no costs (spread ~8-16 bp round trip)\n")
    print(f"{'trigger':10} {'n':>6} {'runway':>7} {'vs rnd':>7} {'mfe30%':>7} "
          f"{'fwd5':>6} {'fwd15':>6} {'fwd30':>6} {'fwd15 t':>8} {'h1 fwd15':>9} {'h2 fwd15':>9}")
    order = ["random", "live", "both_up", "fast50", "slow_turn", "both50", "vol_brk", "live_vol"]
    for k in order:
        rows = ev.get(k, [])
        if not rows:
            continue
        s = summarize(rows)
        r = s["runway"][0]
        f15, se15 = s["fwd15"]
        b15 = base["fwd15"][0]
        tval = (f15 - b15) / se15 if se15 else None
        h1 = [x["fwd15"] for x in rows if x["day"] in half and x["fwd15"] is not None]
        h2 = [x["fwd15"] for x in rows if x["day"] not in half and x["fwd15"] is not None]
        fmt = lambda v, f: "-" if v is None else format(v, f)  # noqa: E731
        print(f"{k:10} {len(rows):6d} {r:7.1%} {r / base['runway'][0]:6.2f}x "
              f"{fmt(s['mfe30'][0], '7.2f')} {fmt(s['fwd5'][0], '+6.1f')} {fmt(f15, '+6.1f')} "
              f"{fmt(s['fwd30'][0], '+6.1f')} {fmt(tval if k != 'random' else None, '+8.1f')} "
              f"{fmt(statistics.mean(h1) if h1 else None, '+9.1f')} {fmt(statistics.mean(h2) if h2 else None, '+9.1f')}")


if __name__ == "__main__":
    main()
