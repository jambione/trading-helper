"""Synthetic bars / data for the history-study tests (no network). Not a test module."""
from __future__ import annotations

import collections
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))
sys.path.insert(0, ROOT)
import bars_structure as BS  # noqa: E402

DAY = "2026-09-15"   # a Tuesday


def T(hh, mm, ss=0, day=DAY):
    return BS.et_ts(day, hh, mm, ss)


def bar(start, o, h, l, c, size_min=1, v=1000):
    return BS.Bar(start, start + size_min * 60, o, h, l, c, v)


def expand(start, O, H, L, C, n):
    """n 1-min bars whose aggregate is (O, H, L, C): path stays at O, the high is printed at minute n//3, the low at
    minute 2n//3, and the last minute closes at C."""
    out, prev = [], O
    for k in range(n):
        c = C if k == n - 1 else O
        o = prev
        h, l = max(o, c), min(o, c)
        if k == n // 3:
            h = H
        if k == (2 * n) // 3:
            l = L
        out.append(bar(start + 60 * k, o, h, l, c))
        prev = c
    return out


def from15(spec, day=DAY, start_hm=(9, 30)):
    """spec = [(O, H, L, C), ...] consecutive 15-min bars from start_hm -> 1-min Bars."""
    t0 = BS.et_ts(day, *start_hm)
    out = []
    for i, (O, H, L, C) in enumerate(spec):
        out += expand(t0 + 900 * i, O, H, L, C, 15)
    return out


def from5(spec, day=DAY, start_hm=(9, 30)):
    t0 = BS.et_ts(day, *start_hm)
    out = []
    for i, (O, H, L, C) in enumerate(spec):
        out += expand(t0 + 300 * i, O, H, L, C, 5)
    return out


def flat(px, t_from, t_to, eps=0.0):
    """doji 1-min bars at px from t_from (inclusive) to t_to (exclusive)."""
    return [bar(t, px, px + eps, px - eps, px) for t in range(int(t_from), int(t_to), 60)]


def bars60(day, spec):
    """spec = [(O, H, L, C)] RTH 60-min bars of `day` (09:30-10:30 ... 15:30-16:00 half bar)."""
    o = BS.et_ts(day, 9, 30)
    cl = BS.et_ts(day, 16)
    out = []
    for i, (O, H, L, C) in enumerate(spec):
        s = o + 3600 * i
        out.append(BS.Bar(s, min(s + 3600, cl), O, H, L, C, 1000))
    return out


def to_rows(bars1):
    return [[b.start, b.o, b.h, b.l, b.c, b.v] for b in bars1]


def price_at(bars1, t):
    xs = [b for b in bars1 if b.end <= t]
    return xs[-1].c if xs else bars1[0].o


class FakeData:
    """the MarketData interface: sessions, close_ts, minutes, daily, factor, quote."""

    def __init__(self, days, minutes=None, daily=None, daily_adj=None, quote_fn=None):
        self.days = sorted(days)
        self.mins = minutes or {}
        self.raw = daily or {}
        self.adj = daily_adj or self.raw
        self.quote_fn = quote_fn
        self.quote_log = []

    def sessions(self):
        return self.days

    def close_ts(self, day):
        return BS.et_ts(day, 16)

    def minutes(self, sym, day):
        return (self.mins.get(sym) or {}).get(day, [])

    def daily(self, sym, day, adjusted=False):
        return ((self.adj if adjusted else self.raw).get(sym) or {}).get(day)

    def factor(self, sym, day):
        a, r = self.daily(sym, day, True), self.daily(sym, day, False)
        return (a[3] / r[3]) if (a and r and r[3]) else None

    def quote(self, sym, t):
        self.quote_log.append((sym, t))
        return self.quote_fn(sym, t) if self.quote_fn else [t, 99.99, 100.01]


def weekdays(lo, n):
    from datetime import datetime, timedelta
    d, out = datetime.strptime(lo, "%Y-%m-%d"), []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return out


def random_walk_day(day, start_px, rng, drift=0.0, vol=0.0008):
    """RTH 1-min rows (plus a few premarket rows) of a random walk."""
    rows, px = [], start_px
    for m in range(4 * 60, 4 * 60 + 5):
        t = BS.et_ts(day, 0) + m * 60
        rows.append([t, px, px * 1.001, px * 0.999, px, 100])
    for k in range(390):
        t = BS.et_ts(day, 9, 30) + 60 * k
        o = px
        px = max(1.0, px * (1 + drift + vol * rng.gauss(0, 1)))
        h = max(o, px) * (1 + abs(rng.gauss(0, vol / 3)))
        l = min(o, px) * (1 - abs(rng.gauss(0, vol / 3)))
        rows.append([t, round(o, 4), round(h, 4), round(l, 4), round(px, 4), 1000])
    return rows


class FakeStudyMarket:
    """the StudyMarket surface load_inputs / build_* use. Records quote requests."""

    def __init__(self, days, syms, seed=7, start_px=None):
        self.days = days
        self.syms = syms
        self.requests, self.fails = 0, {}
        self.quote_log = []
        rng = random.Random(seed)
        self.rows = collections.defaultdict(dict)
        self.daily_raw = collections.defaultdict(dict)
        for s in syms + ["SPY"]:
            px = (start_px or {}).get(s, 50.0 + 10 * len(s))
            for d in days:
                drift = rng.choice([0.0004, -0.0002, 0.0001])
                rr = random_walk_day(d, px, rng, drift)
                self.rows[s][d] = rr
                rth = [r for r in rr if r[0] >= BS.et_ts(d, 9, 30)]
                self.daily_raw[s][d] = [rth[0][1], max(r[2] for r in rth), min(r[3] for r in rth), rth[-1][4],
                                        1e6 * (10 + syms.index(s) if s in syms else 1)]
                px = rth[-1][4]

    def calendar(self, lo, hi):
        return {d: [BS.et_ts(d, 9, 30), BS.et_ts(d, 16)] for d in self.days if lo <= d <= hi}

    def assets(self):
        return {s: {"class": "us_equity", "exchange": "NYSE", "name": f"{s} Corp Common Stock", "status": "active"}
                for s in self.syms}

    def daily_bars(self, syms, lo, hi, adjustment):
        return {s: dict(self.daily_raw[s]) for s in syms if s in self.daily_raw}

    def minute_month(self, sym, month, hi_day):
        return [r for d, rr in self.rows.get(sym, {}).items() if d[:7] == month for r in rr]

    def quote_ts(self, sym, t):
        self.quote_log.append((sym, t))
        d = BS.et_day(t)
        rr = [r for r in self.rows.get(sym, {}).get(d, []) if r[0] + 60 <= t]
        px = rr[-1][4] if rr else 50.0
        return [t - 1.0, px * 0.9999, px * 1.0001]

    def flush(self):
        pass
