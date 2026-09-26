"""edge_common.py — shared loaders for the Round-1 strategy-edge study (2026-09-26).

Data: IEX 1m bars from edge_fetch.py (ai_reports/edge_iex_bars/DAY.pkl), Claude's
Stage B universe/rows (combined_stage_b_rows.pkl), and Claude's spread model:
SIP quoted spread (ask-bid)/mid in bp, median over a few seconds before the
moment (combined_spreads.json from combined_score_study.py, plus
edge_spreads.json samples from edge_fetch.py). A market round trip is charged
the FULL spread at entry (half in, half out), exactly as combined_score_study.

Spread for an arbitrary (sym, day, T) = sym-day median of observed samples x a
time-of-day profile (median ratio of a sample to its sym-day median, per 30-min
slot). Fallbacks: sym median over all days, then a price-bucket median.
"""
from __future__ import annotations

import json
import math
import os
import pickle
import statistics
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np

ROOT = os.environ.get("REPO") or os.getcwd()
ET = ZoneInfo("America/New_York")
BARDIR = os.path.join(ROOT, "ai_reports", "edge_iex_bars")
N_IS = 40


def open_ts(day):
    return datetime.strptime(day, "%Y-%m-%d").replace(hour=9, minute=30, tzinfo=ET).timestamp()


def slot_of(tod_min):
    """30-min slot index from minutes after 09:30 of the decision time."""
    return int(max(0, min(389, tod_min)) // 30)


def load_days():
    rows = pickle.load(open(os.path.join(ROOT, "ai_reports", "combined_stage_b_rows.pkl"), "rb"))
    days = sorted(rows)
    cur = {d: sorted({r["sym"] for r in rows[d]}) for d in days}
    return days, cur, rows


def load_bars(day):
    p = os.path.join(BARDIR, f"{day}.pkl")
    return pickle.load(open(p, "rb")) if os.path.exists(p) else None


class Dense:
    """RTH dense view of one sym-day: index k = minutes after 09:30 of the BAR START
    (bar k closes at 09:31+k). Arrays length 390; C/H/L/O NaN where no IEX bar.
    Cf = forward-filled close (carrying premarket last close in), age = minutes since last bar."""

    __slots__ = ("C", "H", "L", "O", "V", "Cf", "age", "nbars", "raw", "k_raw")

    def __init__(self, B, day):
        t, o, h, l, c, v = B
        ot = open_ts(day)
        k = ((t - ot) // 60).astype(np.int64)
        self.raw = B
        self.k_raw = k
        m = (k >= 0) & (k < 390)
        self.C = np.full(390, np.nan); self.H = np.full(390, np.nan)
        self.L = np.full(390, np.nan); self.O = np.full(390, np.nan); self.V = np.zeros(390)
        kk = k[m]
        self.C[kk] = c[m]; self.H[kk] = h[m]; self.L[kk] = l[m]; self.O[kk] = o[m]; self.V[kk] = v[m]
        self.nbars = int(m.sum())
        pre = c[k < 0]
        last = pre[-1] if len(pre) else np.nan
        Cf = np.empty(390); age = np.empty(390)
        a = 999.0
        for i in range(390):
            if not np.isnan(self.C[i]):
                last = self.C[i]; a = 0.0
            else:
                a += 1.0
            Cf[i] = last; age[i] = a
        self.Cf = Cf; self.age = age

    def last_close(self):
        v = self.Cf[~np.isnan(self.Cf)]
        return float(v[-1]) if len(v) else None


class SpreadModel:
    def __init__(self, syms=None):
        store = {}
        for f in ("combined_spreads.json", "edge_spreads.json"):
            p = os.path.join(ROOT, "ai_reports", f)
            if os.path.exists(p):
                store.update(json.load(open(p)))
        obs = defaultdict(list)  # (sym, day) -> [(tod_min, bp)]
        for key, bp in store.items():
            if bp is None or bp <= 0:
                continue
            sym, ts = key.split("|")
            if syms is not None and sym not in syms:
                continue
            dt = datetime.fromtimestamp(int(ts), ET)
            day = dt.strftime("%Y-%m-%d")
            obs[(sym, day)].append((dt.hour * 60 + dt.minute - 570, float(bp)))
        self.sd = {k: statistics.median(x[1] for x in v) for k, v in obs.items()}
        bysym = defaultdict(list)
        for (s, _d), m in self.sd.items():
            bysym[s].append(m)
        self.sym = {s: statistics.median(v) for s, v in bysym.items()}
        ratios = defaultdict(list)
        for k, v in obs.items():
            if len(v) >= 2:
                for tod, bp in v:
                    ratios[slot_of(tod)].append(bp / self.sd[k])
        self.prof = {s: statistics.median(v) for s, v in ratios.items() if len(v) >= 30}
        self.n_obs = sum(len(v) for v in obs.values())
        self.obs = obs

    def profile(self, tod):
        s = slot_of(tod)
        if s in self.prof:
            return self.prof[s]
        near = min(self.prof, key=lambda x: abs(x - s)) if self.prof else None
        return self.prof[near] if near is not None else 1.0

    def base(self, sym, day, price=None):
        v = self.sd.get((sym, day))
        if v is None:
            v = self.sym.get(sym)
        if v is None:
            v = 12.0 if (price or 50) < 50 else 8.0
        return v

    def get(self, sym, day, tod, price=None):
        return self.base(sym, day, price) * self.profile(tod)


def stats(net, day, gross=None, n_days_total=None, boot=1000, seed=1):
    """n, trades/day, mean gross, mean net, median, win, day-clustered t, bootstrap 95% CI (days resampled)."""
    net = np.asarray(net, float)
    day = np.asarray(day)
    if len(net) == 0:
        return None
    ud, inv = np.unique(day, return_inverse=True)
    sums = np.bincount(inv, weights=net)
    cnts = np.bincount(inv)
    dm = sums / cnts
    nd = len(ud)
    t = dm.mean() / (dm.std(ddof=1) / math.sqrt(nd)) if nd > 2 and dm.std(ddof=1) > 0 else float("nan")
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, nd, size=(boot, nd))
    bm = sums[idx].sum(1) / cnts[idx].sum(1)
    lo, hi = np.percentile(bm, [2.5, 97.5])
    return {"n": int(len(net)), "days": int(nd),
            "per_day": len(net) / (n_days_total or nd),
            "gross": float(np.mean(gross)) if gross is not None else float("nan"),
            "net": float(net.mean()), "median": float(np.median(net)),
            "win": float((net > 0).mean()), "t": float(t), "lo": float(lo), "hi": float(hi)}


def fmt(s):
    if not s:
        return "   (no trades)"
    return (f"n {s['n']:7d} {s['per_day']:7.1f}/d  gross {s['gross']:+6.1f}  net {s['net']:+6.1f}  "
            f"med {s['median']:+6.1f}  win {s['win']:5.1%}  t {s['t']:+5.1f}  CI [{s['lo']:+6.1f},{s['hi']:+6.1f}]")
