#!/usr/bin/env python3
"""Do the levels a manual trader reads (S/R, VWAP, tide, squeeze) separate the desk's good arms from bad ones?

Pre-registered: docs/studies/INDICATOR_TEST_PLAN_2026-10-02.md (commit 3ccedd5, before any result).

POPULATIONS (RTH, price >= $10)
  E  square events (signal_timing.events_for, live config) on admitted names, 2026-09-04..10-02,
     09:40-15:30 ET, one per name per 15 min. Known at the close of bar i; entered at the open of bar i+1.
  F  filled desk BUYs on the paper account since 2026-09-23. Features use the last bar completed before the fill.
  C  control: every 5th RTH minute 09:40-15:30 of the same admitted name-days.

OUTCOME  net15 = (close of the bar 15 min after entry, capped 15:55) / entry - 1 - 0.20% (the $10+ round-trip
         cost tier). For F also the realized round trip (FIFO buy->sell fills). Primary statistic: the event's
         net15 minus the mean net15 of the control minutes in the same name-day and clock hour.

FEATURES (only data available at the decision minute; SIP 1m bars with extended hours from 04:00)
  L1 res_pct      % to the nearest level above: prior-day high, premarket high, today's high, next whole dollar
     res_<lvl>    % to each of those levels (None when price is already above it)
  L2 sup_pct      % to the nearest level below: VWAP, prior close, premarket low, PDH/PMH once cleared
     rr           res_pct / sup_pct
  L3 pdh_state / pmh_state   held (above for the last >= 5 closes) / pressing (within 0.3% under) / below / just_broke
  L4 hvn_above / hvn_below   % to the nearest high-volume price bin (0.25% bins, prior RTH + today RTH so far,
                             bin volume >= 2x the median non-empty bin within +-5%)
  V1 vwap_pct, min_since_reclaim
  R1 hod_room     % below today's RTH high
  M1 spy15, spy30, qqq15, breadth15 (share of the day's admitted names up over the last 15 min)
  M2 rs_open (name minus SPY since 09:30), rs30 (last 30 min)
  L5 lux_res / lux_sup / lux_state   last confirmed 15/15 swing high / low (LuxAlgo "S&R Levels with Breaks")
     ch_res / ch_sup / ch_inside      LonesomeTheBlue "Support Resistance Channels": 10/10 pivots in 290 bars,
                                      width 5% of the 300-bar range, strength pivots*20 + touches, top 6 non-overlapping
  S1 sqz1 / sqz5  LazyBear squeeze released in the last 3 bars (1m) / 2 bars (5m) with momentum > 0 and rising

SPLIT  alternate trading days A/B; tercile cut points come from half A and are applied to both.
PASS   matched-control lift >= +5 bp, day-clustered t >= 2 in BOTH halves, n >= 100 per half-cell.
       A pass here only earns the 60-day re-check; it is not a gate.

Run on the mini after the close:
  .venv/bin/python tools/studies/indicator_levels_study.py            # fetch + features + report
  .venv/bin/python tools/studies/indicator_levels_study.py --report   # report from the saved rows
"""
from __future__ import annotations

import collections
import json
import math
import os
import pickle
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if not os.path.isdir(os.path.join(ROOT, "ai_reports")):   # copied to /tmp and run from the repo
    ROOT = os.getcwd()
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)
import bars  # noqa: E402
import runway_study as rs  # noqa: E402
import signal_timing as st  # noqa: E402

START, END = "2026-09-04", "2026-10-02"
FILLS_FROM = datetime(2026, 9, 23, tzinfo=timezone.utc)
COST = 0.0020
HOLD = 15
OUT = os.path.join(ROOT, "ai_reports", "indicator_levels")
CACHE = os.path.join(OUT, "ext.pkl")
SHARED = os.path.join(ROOT, "ai_reports", "ext_bars_cache.pkl")
os.makedirs(OUT, exist_ok=True)


def at(day: str, hh: int, mm: int) -> float:
    y, m, d = map(int, day.split("-"))
    return datetime(y, m, d, hh, mm, tzinfo=bars.ET).timestamp()


# ---------------------------------------------------------------- data

def trading_days() -> list[str]:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import GetCalendarRequest
    from config import load_config
    c = load_config() or {}
    tc = TradingClient(c.get("api_key"), c.get("secret_key"), paper=True)
    cal = tc.get_calendar(GetCalendarRequest(start=datetime(2026, 8, 25).date(), end=datetime(2026, 10, 2).date()))
    return sorted(str(x.date) for x in cal)


def admitted(days: set[str]) -> dict[str, set[str]]:
    out = collections.defaultdict(set)
    for line in open(os.path.join(ROOT, "ai_reports", "events.jsonl")):
        if '"admit_funnel"' not in line:
            continue
        try:
            e = json.loads(line)
            d = bars.day_of(float(e["ts"]))
        except Exception:  # noqa: BLE001
            continue
        if d in days:
            for s in e.get("kept_symbols") or []:
                if rs.SYM_RE.match(str(s)):
                    out[d].add(str(s))
    return out


def complete(df, day: str) -> bool:
    return df is not None and len(df) > 0 and df.index[-1].timestamp() >= at(day, 15, 50)


def fetch_ext(want: dict[str, set[str]]) -> dict:
    """SIP 1m bars 04:00-16:00 per (sym, day). Seeded from the shared cache, but a frame that stops before
    15:50 (fetched intraday) is refetched rather than trusted."""
    try:
        cache = pickle.load(open(CACHE, "rb"))
    except Exception:  # noqa: BLE001
        cache = {}
    try:
        shared = pickle.load(open(SHARED, "rb"))
    except Exception:  # noqa: BLE001
        shared = {}
    for day, syms in want.items():
        for s in syms:
            if (s, day) not in cache and complete(shared.get((s, day)), day):
                cache[(s, day)] = shared[(s, day)]
    import pandas as pd
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    cl = bars.client()
    fails = 0
    for day, syms in sorted(want.items()):
        need = sorted(s for s in syms if (s, day) not in cache)
        d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=bars.ET)
        for i in range(0, len(need), 100):
            chunk = need[i:i + 100]
            df = None
            for attempt in range(4):
                try:
                    df = cl.get_stock_bars(StockBarsRequest(
                        symbol_or_symbols=chunk, timeframe=TimeFrame(1, TimeFrameUnit.Minute),
                        start=d.replace(hour=4).astimezone(timezone.utc),
                        end=d.replace(hour=16).astimezone(timezone.utc),
                        limit=2_000_000, extended_hours=True, feed=DataFeed.SIP)).df
                    break
                except Exception as e:  # noqa: BLE001  (429s are retried, then counted, never silently dropped)
                    print(f"  ext fail {day} try {attempt}: {str(e)[:80]}", file=sys.stderr)
                    time.sleep(5 * (attempt + 1))
            if df is None:
                fails += len(chunk)
                continue
            got = {}
            if not df.empty:
                if not isinstance(df.index, pd.MultiIndex):
                    df = pd.concat({chunk[0]: df}, names=["symbol"])
                for s in df.index.get_level_values("symbol").unique():
                    got[str(s)] = df.xs(s, level="symbol").sort_index()[["open", "high", "low", "close", "volume"]]
            for s in chunk:
                cache[(s, day)] = got.get(s)
            print(f"  ext {day}: {len(got)}/{len(chunk)}", file=sys.stderr)
            time.sleep(1.0)
    pickle.dump(cache, open(CACHE, "wb"))
    print(f"fetch failures (symbol-days): {fails}", file=sys.stderr)
    return cache


def fills() -> list[dict]:
    """Paper BUY fills with their FIFO-matched sells -> realized round-trip return."""
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest
    from config import load_config
    c = load_config() or {}
    tc = TradingClient(c.get("api_key"), c.get("secret_key"), paper=True)
    orders, until = [], datetime.now(timezone.utc)
    while True:
        got = tc.get_orders(GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=500, after=FILLS_FROM,
                                             until=until, direction="desc"))
        orders += [o for o in got if o.filled_at and o.filled_avg_price and float(o.filled_qty or 0) > 0]
        if len(got) < 500:
            break
        until = min(o.submitted_at for o in got)
    orders.sort(key=lambda o: o.filled_at)
    lots = collections.defaultdict(list)   # sym -> open buy lots [row, qty_left]
    rows = []
    for o in orders:
        side = str(getattr(o.side, "value", o.side)).lower()
        q, px, t = float(o.filled_qty), float(o.filled_avg_price), o.filled_at.timestamp()
        if side == "buy":
            r = {"sym": o.symbol, "t": t, "fill": px, "day": bars.day_of(t), "qty": q, "sold": 0.0, "proceeds": 0.0}
            rows.append(r)
            lots[o.symbol].append([r, q])
        else:
            left = q
            while left > 1e-9 and lots[o.symbol]:
                lot = lots[o.symbol][0]
                take = min(left, lot[1])
                lot[0]["sold"] += take
                lot[0]["proceeds"] += take * px
                lot[1] -= take
                left -= take
                if lot[1] <= 1e-9:
                    lots[o.symbol].pop(0)
    for r in rows:
        r["realized"] = (r["proceeds"] / r["sold"] / r["fill"] - 1) if r["sold"] >= 0.99 * r["qty"] else None
    return rows


# ---------------------------------------------------------------- features

def ema(x: np.ndarray, n: int) -> np.ndarray:
    out = np.empty_like(x)
    a = 2 / (n + 1)
    out[0] = x[0]
    for k in range(1, len(x)):
        out[k] = a * x[k] + (1 - a) * out[k - 1]
    return out


def squeeze(h, l, c, n=20, bb=2.0, kc=1.5):
    """LazyBear SQZMOM_LB: (sqz_on, val) arrays."""
    import pandas as pd
    H, L, C = pd.Series(h), pd.Series(l), pd.Series(c)
    basis, dev = C.rolling(n).mean(), bb * C.rolling(n).std(ddof=0)
    tr = pd.concat([H - L, (H - C.shift()).abs(), (L - C.shift()).abs()], axis=1).max(axis=1)
    rng = tr.rolling(n).mean()
    on = ((basis - dev) > (basis - kc * rng)) & ((basis + dev) < (basis + kc * rng))
    src = C - ((H.rolling(n).max() + L.rolling(n).min()) / 2 + basis) / 2
    xs = np.arange(n)

    def lr(w):
        if np.isnan(w).any():
            return np.nan
        b, a = np.polyfit(xs, w, 1)
        return a + b * (n - 1)
    val = src.rolling(n).apply(lr, raw=True)
    return on.to_numpy(), val.to_numpy()


def released(on, val, i, look):
    for k in range(max(1, i - look + 1), i + 1):
        if on[k - 1] and not on[k] and not math.isnan(val[i]) and val[i] > 0 and val[i] > val[i - 1]:
            return True
    return False


class NameDay:
    def __init__(self, df, prev):
        self.t = np.array([x.timestamp() for x in df.index])
        self.o, self.h, self.l, self.c, self.v = (df[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close", "volume"))
        day = bars.day_of(self.t[0])
        self.day = day
        self.rth0 = int(np.searchsorted(self.t, at(day, 9, 30)))
        pm = slice(0, self.rth0)
        self.pmh = float(self.h[pm].max()) if self.rth0 > 0 else None
        self.pml = float(self.l[pm].min()) if self.rth0 > 0 else None
        self.pdh = self.pdl = self.pdc = None
        self.prev_prof = None
        if prev is not None and len(prev):
            pt = np.array([x.timestamp() for x in prev.index])
            pday = bars.day_of(pt[0])
            m = (pt >= at(pday, 9, 30)) & (pt < at(pday, 16, 0))
            if m.any():
                self.pdh, self.pdl = float(prev["high"].to_numpy()[m].max()), float(prev["low"].to_numpy()[m].min())
                self.pdc = float(prev["close"].to_numpy()[m][-1])
                tp = (prev["high"].to_numpy() + prev["low"].to_numpy() + prev["close"].to_numpy())[m] / 3
                self.prev_prof = (tp, prev["volume"].to_numpy()[m])
        tp = (self.h + self.l + self.c) / 3
        rv = np.where(np.arange(len(self.t)) >= self.rth0, self.v, 0.0)
        self.cum_pv, self.cum_v = np.cumsum(tp * rv), np.cumsum(rv)
        self.tp = tp
        self.hod = np.maximum.accumulate(np.where(np.arange(len(self.t)) >= self.rth0, self.h, -np.inf))
        self.on1, self.val1 = squeeze(self.h, self.l, self.c)
        import pandas as pd
        f5 = df.resample("5min", label="left", closed="left").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
        self.t5 = np.array([x.timestamp() for x in f5.index])
        self.on5, self.val5 = squeeze(f5["high"].to_numpy(), f5["low"].to_numpy(), f5["close"].to_numpy())
        self.pivot_levels(prev)

    def pivot_levels(self, prev):
        """Swing pivots on prior-day + today 1m bars. A pivot at k is known only at bar k+R (no look-ahead)."""
        if prev is not None and len(prev):
            H = np.concatenate([prev["high"].to_numpy(dtype=float), self.h])
            L = np.concatenate([prev["low"].to_numpy(dtype=float), self.l])
            C = np.concatenate([prev["close"].to_numpy(dtype=float), self.c])
            self.off = len(prev)
        else:
            H, L, C, self.off = self.h, self.l, self.c, 0
        self.H, self.L, self.C = H, L, C

        def piv(x, left, right, hi):
            out = []
            for k in range(left, len(x) - right):
                w = x[k - left:k + right + 1]
                j = int(w.argmax()) if hi else int(w.argmin())   # first extreme in the window: one pivot per plateau
                if j == left:
                    out.append((k + right, k, float(x[k])))   # (known_at, at, level)
            return out
        self.lux_ph, self.lux_pl = piv(H, 15, 15, True), piv(L, 15, 15, False)
        self.ch_ph, self.ch_pl = piv(H, 10, 10, True), piv(L, 10, 10, False)

    def pivot_features(self, i: int, px: float) -> dict:
        g = i + self.off                          # index into the joined series
        f = {}
        ph = [p for p in self.lux_ph if p[0] <= g]
        pl = [p for p in self.lux_pl if p[0] <= g]
        f["lux_res"] = (ph[-1][2] / px - 1) * 100 if ph else None     # < 0: price is above the last swing high
        f["lux_sup"] = (1 - pl[-1][2] / px) * 100 if pl else None     # < 0: price is below the last swing low
        f["lux_state"] = None
        if ph and pl:
            f["lux_state"] = "above_res" if px > ph[-1][2] else "below_sup" if px < pl[-1][2] else "inside"
        # SR channels: pivots in the last 290 bars, width 5% of the 300-bar range, strength = pivots*20 + touches
        lo = max(0, g - 290)
        pv = sorted([p[2] for p in self.ch_ph + self.ch_pl if lo <= p[1] and p[0] <= g])
        f["ch_res"] = f["ch_sup"] = None
        f["ch_inside"] = None
        if len(pv) >= 2:
            a = max(0, g - 299)
            width = (self.H[a:g + 1].max() - self.L[a:g + 1].min()) * 0.05
            chans = []
            for base in pv:
                members = [x for x in pv if base <= x <= base + width]
                top, bot = max(members), min(members)
                touch = int(((self.H[lo:g + 1] >= bot) & (self.L[lo:g + 1] <= top)).sum())
                chans.append((len(members) * 20 + touch, bot, top))
            chans.sort(reverse=True)
            keep = []
            for s, bot, top in chans:
                if s >= 20 * 1 and all(top < kb or bot > kt for _, kb, kt in keep):
                    keep.append((s, bot, top))
                if len(keep) == 6:
                    break
            above = [bot for _, bot, top in keep if bot > px]
            below = [top for _, bot, top in keep if top < px]
            f["ch_res"] = (min(above) / px - 1) * 100 if above else None
            f["ch_sup"] = (1 - max(below) / px) * 100 if below else None
            f["ch_inside"] = any(bot <= px <= top for _, bot, top in keep)
        return f

    def idx_before(self, ts: float) -> int:
        """Last bar completed by ts."""
        return int(np.searchsorted(self.t + 60, ts, side="right")) - 1

    def vwap(self, i):
        return self.cum_pv[i] / self.cum_v[i] if i >= self.rth0 and self.cum_v[i] > 0 else None

    def features(self, i: int, tide: dict) -> dict:
        px = float(self.c[i])
        pct = lambda lvl: None if lvl is None else (lvl / px - 1) * 100
        f = {"px": px}
        hod = float(self.hod[i]) if i >= self.rth0 else None
        whole = math.floor(px) + 1.0
        above = {"pdh": self.pdh, "pmh": self.pmh, "hod": hod, "dollar": whole}
        for k, lvl in above.items():
            f[f"res_{k}"] = pct(lvl) if lvl is not None and lvl > px else None
        ups = [v for v in (f[f"res_{k}"] for k in above) if v is not None and v > 0.05]
        f["res_pct"] = min(ups) if ups else None
        vw = self.vwap(i)
        below = [vw, self.pdc, self.pml]
        if self.pdh and px > self.pdh:
            below.append(self.pdh)
        if self.pmh and px > self.pmh:
            below.append(self.pmh)
        downs = [-(pct(x)) for x in below if x is not None and x < px]
        f["sup_pct"] = min(downs) if downs else None
        f["rr"] = f["res_pct"] / f["sup_pct"] if f["res_pct"] and f["sup_pct"] and f["sup_pct"] > 0.02 else None
        for name, lvl in (("pdh", self.pdh), ("pmh", self.pmh)):
            if lvl is None:
                f[f"{name}_state"] = None
            elif px > lvl and i >= 5 and (self.c[i - 4:i + 1] > lvl).all():
                f[f"{name}_state"] = "held"
            elif px > lvl:
                f[f"{name}_state"] = "just_broke"
            elif px >= lvl * 0.997:
                f[f"{name}_state"] = "pressing"
            else:
                f[f"{name}_state"] = "below"
        # L4 volume profile
        tps, vols = [self.tp[self.rth0:i + 1]], [self.v[self.rth0:i + 1]]
        if self.prev_prof is not None:
            tps.append(self.prev_prof[0])
            vols.append(self.prev_prof[1])
        tp, vv = np.concatenate(tps), np.concatenate(vols)
        w = px * 0.0025
        m = np.abs(tp - px) <= px * 0.05
        f["hvn_above"] = f["hvn_below"] = None
        if m.sum() >= 20:
            b = np.floor((tp[m] - px) / w).astype(int) + 21          # bins -21..+20 -> 0..41
            prof = np.bincount(b, weights=vv[m], minlength=42)
            nz = prof[prof > 0]
            med = float(np.median(nz)) if len(nz) else 0.0
            hv = np.nonzero(prof >= 2 * med)[0] - 21 if med > 0 else np.array([], dtype=int)
            up, dn = hv[hv >= 1], hv[hv <= -1]
            f["hvn_above"] = float(up.min()) * 0.25 if len(up) else None
            f["hvn_below"] = -float(dn.max()) * 0.25 if len(dn) else None
        # V1
        f["vwap_pct"] = (px / vw - 1) * 100 if vw else None
        f["min_since_reclaim"] = None
        if vw:
            vws = self.cum_pv[self.rth0:i + 1] / np.maximum(self.cum_v[self.rth0:i + 1], 1e-9)
            below_idx = np.nonzero(self.c[self.rth0:i + 1] <= vws)[0]
            if px > vw:
                f["min_since_reclaim"] = float(i - self.rth0 - below_idx[-1]) if len(below_idx) else float(i - self.rth0 + 1)
        f["hod_room"] = (1 - px / hod) * 100 if hod else None
        # M1/M2
        t_close = float(self.t[i]) + 60
        f.update({k: tide.get((k, t_close)) for k in ("spy15", "spy30", "qqq15", "breadth15")})
        o930 = self.o[self.rth0] if self.rth0 < len(self.o) else None
        spy_open = tide.get(("spy_open", t_close))
        f["rs_open"] = ((px / o930 - 1) * 100 - spy_open) if o930 and spy_open is not None else None
        j = self.idx_before(t_close - 30 * 60)
        f["rs30"] = ((px / self.c[j] - 1) * 100 - tide[("spy30", t_close)]) if j >= 0 and tide.get(("spy30", t_close)) is not None else None
        # S1
        f["sqz1"] = bool(released(self.on1, self.val1, i, 3))
        k5 = int(np.searchsorted(self.t5 + 300, t_close, side="right")) - 1
        f["sqz5"] = bool(k5 >= 1 and released(self.on5, self.val5, k5, 2))
        f.update(self.pivot_features(i, px))
        return f

    def net15(self, i_entry: int, entry_px: float | None = None) -> float | None:
        if i_entry >= len(self.t):
            return None
        px = entry_px or float(self.o[i_entry])
        tx = min(float(self.t[i_entry]) + HOLD * 60, at(self.day, 15, 55))
        j = int(np.searchsorted(self.t, tx, side="right")) - 1
        if j <= i_entry - (0 if entry_px else 1) or px <= 0:
            return None
        return float(self.c[j]) / px - 1 - COST


def build_tide(day: str, cache: dict, syms: set[str]) -> dict:
    """(key, t_close) -> value for SPY/QQQ returns and admitted-name breadth over RTH minutes."""
    out = {}
    series = {}
    for s in ("SPY", "QQQ"):
        df = cache.get((s, day))
        if df is not None:
            series[s] = df["close"]
    grid = [at(day, 9, 30) + 60 * k for k in range(1, 391)]
    import pandas as pd
    idx = pd.DatetimeIndex([datetime.fromtimestamp(g - 60, tz=timezone.utc) for g in grid])

    def aligned(ser):
        return ser.reindex(ser.index.union(idx)).ffill().reindex(idx).to_numpy()
    sp = aligned(series["SPY"]) if "SPY" in series else None
    qq = aligned(series["QQQ"]) if "QQQ" in series else None
    names = []
    for s in syms:
        df = cache.get((s, day))
        if df is not None and len(df) > 100:
            names.append(aligned(df["close"]))
    M = np.vstack(names) if names else None
    spy_o = None
    if "SPY" in series:
        r = cache[("SPY", day)]
        rth = r[r.index >= datetime.fromtimestamp(at(day, 9, 30), tz=timezone.utc)]
        spy_o = float(rth["open"].iloc[0]) if len(rth) else None
    for k, g in enumerate(grid):
        if sp is not None:
            if k >= 15:
                out[("spy15", g)] = (sp[k] / sp[k - 15] - 1) * 100
            if k >= 30:
                out[("spy30", g)] = (sp[k] / sp[k - 30] - 1) * 100
            if spy_o:
                out[("spy_open", g)] = (sp[k] / spy_o - 1) * 100
        if qq is not None and k >= 15:
            out[("qqq15", g)] = (qq[k] / qq[k - 15] - 1) * 100
        if M is not None and k >= 15:
            a, b = M[:, k], M[:, k - 15]
            ok = ~(np.isnan(a) | np.isnan(b))
            if ok.sum() >= 5:
                out[("breadth15", g)] = float((a[ok] > b[ok]).mean())
    return out


# ---------------------------------------------------------------- main

def collect():
    from config import load_config
    cfg = load_config()
    cal = trading_days()
    days = [d for d in cal if START <= d <= END]
    prev_of = {d: cal[cal.index(d) - 1] for d in days}
    adm = admitted(set(days))
    F = [r for r in fills() if r["fill"] >= 10]
    want = collections.defaultdict(set)
    for d in days:
        want[d] |= adm.get(d, set()) | {"SPY", "QQQ"}
    for r in F:
        want[r["day"]].add(r["sym"])
    for d in list(want):
        want[prev_of[d]] |= want[d] - {"SPY", "QQQ"}
    cache = fetch_ext(want)
    print(f"days {len(days)}  admitted name-days {sum(len(v) for v in adm.values())}  fills>=$10 {len(F)}", file=sys.stderr)
    rows = []
    fills_by = collections.defaultdict(list)
    for r in F:
        fills_by[(r["sym"], r["day"])].append(r)
    for d in days:
        tide = build_tide(d, cache, adm.get(d, set()))
        for s in sorted(adm.get(d, set()) | {r["sym"] for r in F if r["day"] == d}):
            df = cache.get((s, d))
            if df is None or len(df) < 150:
                continue
            try:
                nd = NameDay(df, cache.get((s, prev_of[d])))
            except Exception as e:  # noqa: BLE001
                print(f"  nameday fail {s} {d}: {e}", file=sys.stderr)
                continue
            if s in adm.get(d, set()):
                ev = st.events_for(df, cfg)
                for i in ev["events"].get("square", []):
                    if nd.c[i] < 10:
                        continue
                    rows.append({"pop": "E", "sym": s, "day": d, "t": float(nd.t[i]) + 60,
                                 "net15": nd.net15(i + 1), **nd.features(i, tide)})
                for i in range(nd.rth0, len(nd.t), 5):
                    m = bars.et_minutes(nd.t[i])
                    if 9 * 60 + 40 <= m <= 15 * 60 + 30 and nd.c[i] >= 10:
                        rows.append({"pop": "C", "sym": s, "day": d, "t": float(nd.t[i]) + 60,
                                     "net15": nd.net15(i + 1), **nd.features(i, tide)})
            for r in fills_by.get((s, d), []):
                i = nd.idx_before(r["t"])
                if i < nd.rth0:
                    continue
                j = int(np.searchsorted(nd.t, r["t"], side="right")) - 1
                rows.append({"pop": "F", "sym": s, "day": d, "t": r["t"], "net15": nd.net15(j, r["fill"]),
                             "realized": r["realized"], **nd.features(i, tide)})
        print(f"  {d}: rows {len(rows)}", file=sys.stderr)
    json.dump({"days": days, "rows": rows}, open(os.path.join(OUT, "rows.json"), "w"))
    return days, rows


NUM = ["res_pct", "res_pdh", "res_pmh", "res_hod", "res_dollar", "sup_pct", "rr", "hvn_above", "hvn_below",
       "vwap_pct", "min_since_reclaim", "hod_room", "lux_res", "lux_sup", "ch_res", "ch_sup", "spy15", "spy30", "qqq15", "breadth15", "rs_open", "rs30"]
CAT = ["pdh_state", "pmh_state", "sqz1", "sqz5", "lux_state", "ch_inside"]
TEST = {"res_pct": "L1", "res_pdh": "L1", "res_pmh": "L1", "res_hod": "L1", "res_dollar": "L1", "sup_pct": "L2",
        "rr": "L2", "pdh_state": "L3", "pmh_state": "L3", "hvn_above": "L4", "hvn_below": "L4", "vwap_pct": "V1",
        "min_since_reclaim": "V1", "hod_room": "R1", "spy15": "M1", "spy30": "M1", "qqq15": "M1", "breadth15": "M1",
        "rs_open": "M2", "rs30": "M2", "sqz1": "S1", "sqz5": "S1", "lux_res": "L5", "lux_sup": "L5",
        "lux_state": "L5", "ch_res": "L5", "ch_sup": "L5", "ch_inside": "L5"}


def dct(per_day: dict) -> tuple[float | None, float | None, int]:
    """Mean over all observations, and the day-clustered t (mean of day means / its SE)."""
    allv = [x for v in per_day.values() for x in v]
    if not allv:
        return None, None, 0
    dm = [statistics.mean(v) for v in per_day.values() if v]
    t = statistics.mean(dm) / (statistics.stdev(dm) / math.sqrt(len(dm))) if len(dm) > 2 and statistics.stdev(dm) > 0 else None
    return statistics.mean(allv), t, len(allv)


def report(days, rows):
    half = {d: "AB"[k % 2] for k, d in enumerate(days)}
    ctrl = collections.defaultdict(list)
    for r in rows:
        if r["pop"] == "C" and r["net15"] is not None:
            ctrl[(r["sym"], r["day"], bars.et_minutes(r["t"] - 60) // 60)].append(r["net15"])
    cm = {k: statistics.mean(v) for k, v in ctrl.items()}
    for r in rows:
        if r["net15"] is not None:
            base = cm.get((r["sym"], r["day"], bars.et_minutes(r["t"] - 60) // 60))
            r["lift"] = r["net15"] - base if base is not None else None
    lines = []
    P = lambda x: "—" if x is None else f"{x * 1e4:+.1f}"
    T = lambda x: "—" if x is None else f"{x:+.1f}"
    for pop, name in (("E", "square events"), ("F", "desk fills")):
        rs_ = [r for r in rows if r["pop"] == pop and r.get("lift") is not None]
        lines.append(f"\n## {name}: n={len(rs_)}")
        for h in "AB":
            pd_ = collections.defaultdict(list)
            for r in rs_:
                if half[r["day"]] == h:
                    pd_[r["day"]].append(r["lift"])
            m, t, n = dct(pd_)
            lines.append(f"- half {h}: all, lift vs matched control {P(m)} bp (t {T(t)}, n {n})")
        if pop == "F":
            rz = [r["realized"] for r in rs_ if r.get("realized") is not None]
            lines.append(f"- realized round trip, all fills: {P(statistics.mean(rz))} bp (n {len(rz)})")
        lines.append("\n| test | feature | cell | A lift bp (t, n) | B lift bp (t, n) | A net15 | B net15 |"
                     + (" realized A/B |" if pop == "F" else "") + " pass |")
        lines.append("|---|---|---|---|---|---|---|" + ("---|" if pop == "F" else "") + "---|")
        for feat in NUM + CAT:
            if feat in NUM:
                av = sorted(r[feat] for r in rs_ if half[r["day"]] == "A" and r.get(feat) is not None)
                if len(av) < 30:
                    continue
                q1, q2 = av[len(av) // 3], av[2 * len(av) // 3]
                cells = [(f"low <{q1:.2f}", lambda x, q1=q1: x < q1),
                         (f"mid", lambda x, q1=q1, q2=q2: q1 <= x < q2),
                         (f"high >={q2:.2f}", lambda x, q2=q2: x >= q2),
                         ("missing", None)]
            else:
                vals = sorted({str(r.get(feat)) for r in rs_})
                cells = [(v, lambda x, v=v: str(x) == v) for v in vals]
            for label, fn in cells:
                out = []
                for h in "AB":
                    pd_, nd_, rz = collections.defaultdict(list), collections.defaultdict(list), []
                    for r in rs_:
                        if half[r["day"]] != h:
                            continue
                        x = r.get(feat)
                        hit = (x is None) if fn is None else (x is not None and fn(x)) if feat in NUM else fn(x)
                        if hit:
                            pd_[r["day"]].append(r["lift"])
                            nd_[r["day"]].append(r["net15"])
                            if r.get("realized") is not None:
                                rz.append(r["realized"])
                    out.append((dct(pd_), dct(nd_), statistics.mean(rz) if rz else None))
                (ma, ta, na), (mb, tb, nb) = out[0][0], out[1][0]
                if na + nb < 20:
                    continue
                ok = all(m is not None and m >= 5e-4 and t is not None and t >= 2 and n >= 100
                         for m, t, n in (out[0][0], out[1][0]))
                row = (f"| {TEST[feat]} | {feat} | {label} | {P(ma)} ({T(ta)}, {na}) | {P(mb)} ({T(tb)}, {nb}) | "
                       f"{P(out[0][1][0])} | {P(out[1][1][0])} |")
                if pop == "F":
                    row += f" {P(out[0][2])} / {P(out[1][2])} |"
                lines.append(row + (" **PASS** |" if ok else " |"))
    txt = "\n".join(lines)
    open(os.path.join(OUT, "report.md"), "w").write(txt)
    print(txt)


def main():
    if "--report" in sys.argv:
        z = json.load(open(os.path.join(OUT, "rows.json")))
        days, rows = z["days"], z["rows"]
    else:
        days, rows = collect()
    print({p: sum(1 for r in rows if r["pop"] == p) for p in "ECF"}, file=sys.stderr)
    report(days, rows)


if __name__ == "__main__":
    main()
