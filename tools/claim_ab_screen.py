#!/usr/bin/env python3
"""Claim A vs Claim B vs the high-RSI survivor — same exit, same tape.

Frozen definitions (do not edit after looking):

  SURVIVOR  EXH crosses UP through 75; first bar within 20 where
            RSI-2 >= 90 and EXH >= 60.  (the +0.86% in-sample rule)

  CLAIM_A   Same setup; first bar within 20 where RSI-2 <= 20 and
            EXH >= 60.  (the pullback / inverse — already reported −1.25%)

  CLAIM_B   EXH still below 75, rising, in [40, 75), and the day has not
            yet printed EXH >= 75; RSI-2 in [0, 50] and rising.
            "Trending toward overbought with RSI still low."

Fill: next bar's open. Also scored at +1 bar open (delay).
Exit (identical for all): EXH leaves overbought (reached >=80, then
close < 80); 2% ratchet under running high; -5% hard; 120-bar cap.
Haircut: 0.20% round trip on every trade.
Universe: shadow first-watch name-days in the window (point-in-time).
One entry per symbol per day, first that qualifies.

Read-only. Does not write bot_config. Does not arm.

    .venv/bin/python tools/claim_ab_screen.py
    .venv/bin/python tools/claim_ab_screen.py --from 2026-08-24 --to 2026-09-04
"""
from __future__ import annotations

import argparse
import math
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import bars
import desk_null as N
import shadow_report as sr

ET = ZoneInfo("America/New_York")
EXH_N = 21
RSI_N = 2
WAIT = 20
CROSS = 75.0
EXH_FLOOR = 60.0
OB_ARM = 80.0
RSI_HIGH = 90.0
RSI_LOW = 20.0
B_EXH_LO = 40.0
B_EXH_HI = 75.0
B_RSI_MAX = 50.0
RATCHET = 0.02
HARD = 0.05
MAX_BARS = 120
HAIRCUT = 0.20
RTH_LO = 9 * 60 + 35
RTH_HI = 15 * 60 + 30

_OHLCV: dict[tuple, tuple | None] = {}


def _fetch_ohlcv(sym: str, day: str, feed: str):
    """(stamps, opens, highs, lows, closes) or Nones."""
    key = (sym, day, feed)
    if key in _OHLCV:
        return _OHLCV[key]
    cl = bars.client()
    if cl is None:
        _OHLCV[key] = None
        return None
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    from alpaca.data.enums import DataFeed
    import pandas as pd

    out = None
    try:
        d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
        df = cl.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=sym,
            timeframe=TimeFrame(1, TimeFrameUnit.Minute),
            start=d.replace(hour=9, minute=25).astimezone(timezone.utc),
            end=d.replace(hour=16, minute=5).astimezone(timezone.utc),
            limit=10000, extended_hours=False,
            feed=DataFeed.SIP if feed == "sip" else DataFeed.IEX,
        )).df
        if df is not None and not df.empty:
            if isinstance(df.index, pd.MultiIndex):
                df = df.xs(sym, level="symbol")
            df = df.sort_index()
            out = (
                [t.timestamp() for t in df.index],
                [float(v) for v in df["open"]],
                [float(v) for v in df["high"]],
                [float(v) for v in df["low"]],
                [float(v) for v in df["close"]],
            )
    except Exception:
        out = None
    _OHLCV[key] = out
    return out


def _warm(syms: list[str], day: str, feed: str, chunk: int = 50) -> None:
    want = [s for s in syms if (s, day, feed) not in _OHLCV]
    if not want:
        return
    cl = bars.client()
    if cl is None:
        for s in want:
            _OHLCV[(s, day, feed)] = None
        return
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    from alpaca.data.enums import DataFeed
    import pandas as pd

    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
    for i in range(0, len(want), chunk):
        batch = want[i:i + chunk]
        try:
            df = cl.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=batch,
                timeframe=TimeFrame(1, TimeFrameUnit.Minute),
                start=d.replace(hour=9, minute=25).astimezone(timezone.utc),
                end=d.replace(hour=16, minute=5).astimezone(timezone.utc),
                limit=10000, extended_hours=False,
                feed=DataFeed.SIP if feed == "sip" else DataFeed.IEX,
            )).df
        except Exception:
            for s in batch:
                _OHLCV[(s, day, feed)] = None
            continue
        got: dict[str, tuple] = {}
        if df is not None and not df.empty:
            if isinstance(df.index, pd.MultiIndex):
                for sym in df.index.get_level_values("symbol").unique():
                    sub = df.xs(sym, level="symbol").sort_index()
                    got[str(sym).upper()] = (
                        [t.timestamp() for t in sub.index],
                        [float(v) for v in sub["open"]],
                        [float(v) for v in sub["high"]],
                        [float(v) for v in sub["low"]],
                        [float(v) for v in sub["close"]],
                    )
            elif len(batch) == 1:
                sub = df.sort_index()
                got[batch[0]] = (
                    [t.timestamp() for t in sub.index],
                    [float(v) for v in sub["open"]],
                    [float(v) for v in sub["high"]],
                    [float(v) for v in sub["low"]],
                    [float(v) for v in sub["close"]],
                )
        for s in batch:
            _OHLCV[(s, day, feed)] = got.get(s)


def exh_series(highs, lows, closes, n: int = EXH_N) -> list[float | None]:
    out: list[float | None] = []
    for i in range(len(closes)):
        lo = max(0, i - n + 1)
        hh = max(highs[lo:i + 1])
        ll = min(lows[lo:i + 1])
        if hh <= ll:
            out.append(None)
            continue
        out.append(100.0 * (closes[i] - ll) / (hh - ll))
    return out


def rsi_series(closes: list[float], period: int = RSI_N) -> list[float | None]:
    """Wilder RSI aligned to closes; index 0 is None (needs a prior close)."""
    period = max(2, int(period))
    alpha = 1.0 / period
    out: list[float | None] = [None]
    up = down = 0.0
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gain = delta if delta > 0 else 0.0
        loss = -delta if delta < 0 else 0.0
        if i == 1:
            up, down = gain, loss
        else:
            up = alpha * gain + (1.0 - alpha) * up
            down = alpha * loss + (1.0 - alpha) * down
        if down == 0:
            out.append(100.0)
        elif up == 0:
            out.append(0.0)
        else:
            out.append(100.0 - (100.0 / (1.0 + up / down)))
    return out


def _rth(ts: float) -> bool:
    m = bars.et_minutes(ts)
    return RTH_LO <= m <= RTH_HI


def find_cross_setup(exh: list[float | None]) -> list[int]:
    """Bar indices where EXH crosses up through CROSS."""
    out = []
    for i in range(1, len(exh)):
        a, b = exh[i - 1], exh[i]
        if a is None or b is None:
            continue
        if a < CROSS <= b:
            out.append(i)
    return out


def first_trigger_after(cross_i: int, exh, rsi, pred, wait: int = WAIT):
    """First bar in (cross_i, cross_i+wait] where pred(i) and EXH >= floor."""
    hi = min(len(exh) - 1, cross_i + wait)
    for i in range(cross_i + 1, hi + 1):
        e, r = exh[i], rsi[i]
        if e is None or r is None:
            continue
        if e < EXH_FLOOR:
            continue
        if pred(r):
            return i
    return None


def find_claim_b(exh, rsi, stamps, start_i: int) -> int | None:
    """First bar: EXH rising in [40,75), never yet OB today; RSI<=50 rising.

    ``day_max`` walks the whole session so a name that already tagged OB
    before first-watch is not treated as "going into" the zone.
    """
    day_max = -1.0
    for i in range(len(exh)):
        e = exh[i]
        if e is not None:
            prior_max = day_max
            day_max = max(day_max, e)
        else:
            prior_max = day_max
        if i < start_i or i < 1:
            continue
        if not _rth(stamps[i]):
            continue
        ep, r, rp = exh[i - 1], rsi[i], rsi[i - 1]
        if e is None or ep is None or r is None or rp is None:
            continue
        # prior_max is EXH max on bars before this one
        if prior_max >= B_EXH_HI:
            continue
        if not (B_EXH_LO <= e < B_EXH_HI):
            continue
        if e <= ep:
            continue
        if not (0.0 <= r <= B_RSI_MAX):
            continue
        if r <= rp:
            continue
        return i
    return None

def simulate(stamps, opens, highs, lows, closes, exh, entry_i: int,
             delay: int = 0) -> float | None:
    """Pct return after haircut. delay=0 → next open; delay=1 → one bar later."""
    fill_i = entry_i + 1 + delay
    if fill_i >= len(opens) or fill_i < 0:
        return None
    entry = opens[fill_i]
    if not entry or entry <= 0:
        return None
    hard = entry * (1.0 - HARD)
    run_hi = entry
    armed_ob = False
    last = min(len(closes) - 1, fill_i + MAX_BARS)
    for j in range(fill_i, last + 1):
        run_hi = max(run_hi, highs[j])
        stop = run_hi * (1.0 - RATCHET)
        # optimistic: stop at trigger if low tags it
        if lows[j] <= hard:
            px = hard
            return (px - entry) / entry * 100.0 - HAIRCUT
        if lows[j] <= stop and j > fill_i:
            px = stop
            return (px - entry) / entry * 100.0 - HAIRCUT
        e = exh[j]
        if e is not None and e >= OB_ARM:
            armed_ob = True
        if armed_ob and e is not None and e < OB_ARM:
            # leave overbought on this close → fill next open if any
            if j + 1 < len(opens) and opens[j + 1] > 0:
                px = opens[j + 1]
            else:
                px = closes[j]
            return (px - entry) / entry * 100.0 - HAIRCUT
    px = closes[last]
    return (px - entry) / entry * 100.0 - HAIRCUT


def _sign_p(pos: int, n: int) -> float | None:
    if n <= 0:
        return None
    return sum(math.comb(n, i) for i in range(pos, n + 1)) / (2 ** n)


def summarize(trades: list[dict], label: str) -> None:
    print(f"\n=== {label}")
    if not trades:
        print("  n=0")
        return
    rets = [t["ret"] for t in trades]
    delay = [t["ret_delay"] for t in trades if t["ret_delay"] is not None]
    by_day: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        by_day[t["day"]].append(t["ret"])
    sess = {d: statistics.median(v) for d, v in by_day.items()}
    pos = sum(1 for m in sess.values() if m > 0)
    win = 100.0 * sum(1 for r in rets if r > 0) / len(rets)
    print(f"  n={len(trades)}  name-days fired "
          f"{len({(t['sym'], t['day']) for t in trades})}")
    print(f"  mean {statistics.fmean(rets):+.3f}%  "
          f"median {statistics.median(rets):+.3f}%  "
          f"win {win:.0f}%")
    print(f"  sessions {pos}/{len(sess)} positive  "
          f"p={_sign_p(pos, len(sess)):.4f}")
    meds = " ".join(f"{d[-5:]}:{sess[d]:+.2f}" for d in sorted(sess))
    print(f"  per-session median  {meds}")
    if delay:
        dpos = sum(1 for t in trades
                   if t["ret_delay"] is not None and t["ret_delay"] > 0)
        # session medians on delay fill
        bd: dict[str, list[float]] = defaultdict(list)
        for t in trades:
            if t["ret_delay"] is not None:
                bd[t["day"]].append(t["ret_delay"])
        ds = {d: statistics.median(v) for d, v in bd.items()}
        dpos_s = sum(1 for m in ds.values() if m > 0)
        print(f"  delay+1bar  mean {statistics.fmean(delay):+.3f}%  "
              f"median {statistics.median(delay):+.3f}%  "
              f"sessions {dpos_s}/{len(ds)}")
    # TOD of entries
    buckets = defaultdict(int)
    for t in trades:
        m = bars.et_minutes(t["signal_ts"])
        if m < 10 * 60:
            buckets["09:35-10:00"] += 1
        elif m < 12 * 60:
            buckets["10:00-12:00"] += 1
        elif m < 14 * 60:
            buckets["12:00-14:00"] += 1
        else:
            buckets["14:00-15:30"] += 1
    print("  entry TOD  " + "  ".join(f"{k}={v}" for k, v in sorted(buckets.items())))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--from", dest="day_from", default="2026-08-24")
    ap.add_argument("--to", dest="day_to", default="2026-09-04")
    ap.add_argument("--feed", default="sip", choices=("sip", "iex"))
    args = ap.parse_args()

    why = N.require_bars_client()
    if why:
        print(why)
        return 1

    rows = sr.load()
    if not rows:
        print("no shadow log")
        return 1
    days = sorted({
        bars.day_of(r["ts"]) for r in rows
        if r.get("ts") and args.day_from <= bars.day_of(r["ts"]) <= args.day_to
    })
    if not days:
        print(f"no shadow days in {args.day_from}..{args.day_to}")
        return 1
    dayset = set(days)
    rows = [r for r in rows if r.get("ts") and bars.day_of(r["ts"]) in dayset]
    fw = N.first_watch_map(rows)
    print(f"claim A/B screen  {days[0]}..{days[-1]}  feed={args.feed}")
    print(f"  watched name-days={len(fw)}  haircut={HAIRCUT}%  "
          f"exit=leave-OB/{RATCHET:.0%}-ratchet/-{HARD:.0%}/120m")
    print("  DOES NOT ARM.\n")

    by_day: dict[str, list[str]] = defaultdict(list)
    for (sym, day), _ in fw.items():
        by_day[day].append(sym)
    for day in days:
        syms = sorted(set(by_day.get(day) or []))
        print(f"  warming {day}: {len(syms)} names …", flush=True)
        _warm(syms, day, args.feed)

    results = {"survivor": [], "claim_a": [], "claim_b": []}
    scored = 0
    for (sym, day), t_watch in sorted(fw.items()):
        pack = _fetch_ohlcv(sym, day, args.feed)
        if not pack or not pack[0]:
            continue
        stamps, opens, highs, lows, closes = pack
        exh = exh_series(highs, lows, closes)
        rsi = rsi_series(closes)
        scored += 1

        # earliest RTH index at/after first watch
        start_i = 0
        for i, ts in enumerate(stamps):
            if ts >= t_watch and _rth(ts):
                start_i = i
                break
        else:
            continue

        crosses = [i for i in find_cross_setup(exh) if i >= start_i and _rth(stamps[i])]

        # survivor + claim A share the cross setup
        for cross_i in crosses:
            # survivor
            if not any(t["sym"] == sym and t["day"] == day
                       for t in results["survivor"]):
                ti = first_trigger_after(
                    cross_i, exh, rsi, lambda r: r >= RSI_HIGH)
                if ti is not None and _rth(stamps[ti]) and stamps[ti] >= t_watch:
                    r0 = simulate(stamps, opens, highs, lows, closes, exh, ti, 0)
                    r1 = simulate(stamps, opens, highs, lows, closes, exh, ti, 1)
                    if r0 is not None:
                        results["survivor"].append({
                            "sym": sym, "day": day, "ret": r0,
                            "ret_delay": r1, "signal_ts": stamps[ti],
                            "exh": exh[ti], "rsi": rsi[ti],
                        })
            # claim A
            if not any(t["sym"] == sym and t["day"] == day
                       for t in results["claim_a"]):
                ti = first_trigger_after(
                    cross_i, exh, rsi, lambda r: r <= RSI_LOW)
                if ti is not None and _rth(stamps[ti]) and stamps[ti] >= t_watch:
                    r0 = simulate(stamps, opens, highs, lows, closes, exh, ti, 0)
                    r1 = simulate(stamps, opens, highs, lows, closes, exh, ti, 1)
                    if r0 is not None:
                        results["claim_a"].append({
                            "sym": sym, "day": day, "ret": r0,
                            "ret_delay": r1, "signal_ts": stamps[ti],
                            "exh": exh[ti], "rsi": rsi[ti],
                        })

        # claim B — independent of the cross; first of day only
        bi = find_claim_b(exh, rsi, stamps, start_i)
        if bi is not None and stamps[bi] >= t_watch:
            r0 = simulate(stamps, opens, highs, lows, closes, exh, bi, 0)
            r1 = simulate(stamps, opens, highs, lows, closes, exh, bi, 1)
            if r0 is not None:
                results["claim_b"].append({
                    "sym": sym, "day": day, "ret": r0,
                    "ret_delay": r1, "signal_ts": stamps[bi],
                    "exh": exh[bi], "rsi": rsi[bi],
                })

    print(f"\n  scored name-days with bars: {scored}/{len(fw)}")
    summarize(results["survivor"],
              "SURVIVOR  EXH cross 75 → RSI-2 >= 90")
    summarize(results["claim_a"],
              "CLAIM A   EXH cross 75 → RSI-2 <= 20  (pullback)")
    summarize(results["claim_b"],
              "CLAIM B   EXH rising in [40,75), never yet OB; RSI-2 <= 50 rising")

    print("\nPASS bar (desk): n>=30, >=5 sessions, session sign p<=0.05, "
          "median net > 0. This screen is in-sample on the search window — "
          "a green cell is not an arm.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
