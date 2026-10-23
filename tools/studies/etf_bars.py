"""Shared helpers for the SPY/QQQ video-setup studies (fence_bar, pm_power_bar, anchor_power_bar): load the
etf_minute cache, build clock bars, simulate a stop/target/15:59 bracket, seeded controls, day-clustered stats."""
from __future__ import annotations

import glob
import hashlib
import math
import os
import random
import statistics
from collections import defaultdict

import numpy as np

ROOT = os.environ.get("EEH_ROOT") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CACHE = os.path.join(ROOT, "ai_reports", "etf_minute")
RTH0, RTH_END, LAST_MIN = 570, 960, 959
HALF_A_END, B_RESTART = "2020-12-31", "2026-04-27"
TICK = 0.01


def load(sym: str) -> dict[str, np.ndarray]:
    """day -> rows [minute, o, h, l, c, v] (04:00-15:59), sorted."""
    per = defaultdict(list)
    for p in sorted(glob.glob(os.path.join(CACHE, sym, "*.npz"))):
        z = np.load(p, allow_pickle=False)
        for d, r in zip(z["day"], z["rows"]):
            per[str(d)].append(r)
    return {d: np.array(sorted(rs, key=lambda x: x[0])) for d, rs in sorted(per.items())}


HELD_LO, HELD_HI, LAST_DAY = "2026-01-14", "2026-04-24", "2026-10-15"
EARLY_CLOSE_VOL_RATIO = 0.15


def is_full_session(r: np.ndarray) -> bool:
    """A 13:00 early close leaves only thin extended-hours prints after 13:00 (the cache keeps 04:00-15:59). Full
    session = an opening bar exists AND volume 13:00-15:59 >= 15% of volume 09:30-12:59 (normal afternoons trade
    roughly as much as mornings; half-day after-hours prints are a few percent)."""
    m, v = r[:, 0], r[:, 5]
    if not ((m >= RTH0) & (m < RTH0 + 5)).any():
        return False
    am = v[(m >= RTH0) & (m < 780)].sum()
    pm = v[(m >= 780) & (m < RTH_END)].sum()
    return am > 0 and pm >= EARLY_CLOSE_VOL_RATIO * am


def in_scope(day: str) -> bool:
    """Scored days only: never the held-out window, never after the registered last day."""
    return not (HELD_LO <= day <= HELD_HI) and day <= LAST_DAY


def rth(r: np.ndarray) -> np.ndarray:
    return r[(r[:, 0] >= RTH0) & (r[:, 0] < RTH_END)]


def clock_bars(r: np.ndarray, width: int, need: int) -> list:
    """RTH clock bars of `width` minutes: list indexed from 09:30 of (o, h, l, c, end_minute) or None when fewer than
    `need` 1-minute bars are present."""
    out = []
    byk = defaultdict(list)
    for row in r:
        k = int(row[0] - RTH0) // width
        byk[k].append(row)
    for k in range((RTH_END - RTH0) // width):
        rows = byk.get(k, [])
        if len(rows) < need:
            out.append(None)
            continue
        out.append((rows[0][1], max(x[2] for x in rows), min(x[3] for x in rows), rows[-1][4],
                    RTH0 + width * (k + 1) - 1))
    return out


def first_after(r: np.ndarray, minute: int) -> int | None:
    k = int(np.searchsorted(r[:, 0], minute, side="right"))
    return k if k < len(r) else None


def simulate(r: np.ndarray, e: int, side: int, stop: float, target: float) -> dict:
    """Bracket from 1-minute RTH row e (its open is the fill). side +1 long / -1 short. Stop fills at the worse of
    stop and the bar open; target at the better of target and open; both in one bar = stop; else the 15:59 close."""
    entry = float(r[e, 1])
    for k in range(e, len(r)):
        m, o, h, lo, c = r[k, :5]
        if m > LAST_MIN:
            break
        if side > 0:
            hit_s, hit_t = lo <= stop, (h >= target + TICK or o > target)
            if hit_s:
                return _ret(entry, min(stop, o), side, "stop", m)
            if hit_t:
                return _ret(entry, max(target, o), side, "target", m)
        else:
            hit_s, hit_t = h >= stop, (lo <= target - TICK or o < target)
            if hit_s:
                return _ret(entry, max(stop, o), side, "stop", m)
            if hit_t:
                return _ret(entry, min(target, o), side, "target", m)
    last = r[r[:, 0] <= LAST_MIN]
    return _ret(entry, float(last[-1, 4]), side, "time", float(last[-1, 0]))


def _ret(entry: float, exit_: float, side: int, why: str, m) -> dict:
    return {"gross": side * (exit_ / entry - 1), "why": why, "exit_min": int(m), "entry": entry}


def control(r: np.ndarray, sym: str, day: str, after: int, until: int, side: int, stop_pct: float,
            tgt_pct: float, tag: str) -> dict | None:
    """Seeded random entry minute in (after, until] on present 1-minute bars; same percent stop/target."""
    cand = [k for k in range(len(r)) if after < r[k, 0] <= until]
    if not cand:
        return None
    rng = random.Random(int(hashlib.sha256(f"{sym}|{day}|{tag}".encode()).hexdigest()[:16], 16))
    e = rng.choice(cand)
    o = float(r[e, 1])
    return simulate(r, e, side, o * (1 - side * stop_pct), o * (1 + side * tgt_pct))


def half_of(day: str) -> str:
    return "A" if day <= HALF_A_END else "B"


def t_ppf(p: float, df: int) -> float:
    import indicator_confirm_study as S
    return S.t_ppf(p, df)


def day_stats(rows: list[dict], key) -> dict:
    per = defaultdict(list)
    for x in rows:
        v = key(x)
        if v is not None:
            per[x["day"]].append(v)
    xs = [statistics.mean(v) for v in per.values()]
    n = len(xs)
    if n < 2:
        return {"days": n, "mean": xs[0] if xs else None, "t": None, "ub": None, "n": sum(map(len, per.values()))}
    mu, se = statistics.mean(xs), statistics.stdev(xs) / math.sqrt(n)
    return {"days": n, "mean": mu, "t": mu / se if se else None,
            "ub": mu + t_ppf(0.975, n - 1) * se if se else None, "n": sum(map(len, per.values())),
            "means": dict(zip(per.keys(), xs))}


def drop_best_days(rows: list[dict], key, k: int = 3) -> float | None:
    s = day_stats(rows, key)
    if not s.get("means"):
        return None
    top = set(sorted(s["means"], key=lambda d: s["means"][d], reverse=True)[:k])
    return day_stats([x for x in rows if x["day"] not in top], key)["mean"]


def drop_best_month(rows: list[dict], key) -> float | None:
    s = day_stats(rows, key)
    if not s.get("means"):
        return None
    mon = defaultdict(list)
    for d, v in s["means"].items():
        mon[d[:7]].append(v)
    best = max(mon, key=lambda m: statistics.mean(mon[m]))
    return day_stats([x for x in rows if x["day"][:7] != best], key)["mean"]


def mde(rows: list[dict]) -> float | None:
    s = day_stats([x for x in rows if x.get("ctrl") is not None and x.get("ctrl2") is not None],
                  lambda x: x["ctrl"] - x["ctrl2"])
    if not s.get("means") or s["days"] < 2:
        return None
    sd = statistics.stdev(list(s["means"].values()))
    return (2.0 + 0.84) * sd / math.sqrt(s["days"])


def primary_verdict(rows: list[dict], cost: float, bar: float = 0.0005, floor_days: int = 300,
                    t_batch: float = 2.84) -> dict:
    """Per half: net (after cost) > 0 t >= 2 AND lift (trade - control) >= bar with t >= 2 AND drop-3 both >= 0."""
    res, hv = {}, []
    for h in ("A", "B"):
        hr = [x for x in rows if half_of(x["day"]) == h]
        net = day_stats(hr, lambda x: x["gross"] - cost)
        lift = day_stats(hr, lambda x: x["gross"] - x["ctrl"] if x.get("ctrl") is not None else None)
        m = mde(hr)
        powered = net["days"] >= floor_days and m is not None and m <= bar
        d_net = drop_best_days(hr, lambda x: x["gross"] - cost)
        d_lift = drop_best_days(hr, lambda x: x["gross"] - x["ctrl"] if x.get("ctrl") is not None else None)
        if (net["t"] is not None and net["t"] <= -2) or (lift["t"] is not None and lift["t"] <= -2):
            v = "FAIL"
        elif not powered:
            v = "UNDERPOWERED"
        elif (net["mean"] > 0 and (net["t"] or 0) >= 2 and lift["mean"] >= bar and (lift["t"] or 0) >= 2
              and d_net is not None and d_net >= 0 and d_lift is not None and d_lift >= 0):
            v = "PASS"
        else:
            v = "NOT_PASSED"
        hv.append(v)
        res[h] = {"verdict": v, "trades": len(hr), "net": {k: net[k] for k in ("days", "mean", "t", "ub")},
                  "lift": {k: lift[k] for k in ("days", "mean", "t", "ub")}, "mde": m, "powered": powered,
                  "drop3_net": d_net, "drop3_lift": d_lift,
                  "drop_best_month_lift": drop_best_month(hr, lambda x: x["gross"] - x["ctrl"]
                                                          if x.get("ctrl") is not None else None),
                  "batch_t_met": min(net["t"] or 0, lift["t"] or 0) >= t_batch}
    if "FAIL" in hv:
        v = "FAIL"
    elif all(x == "PASS" for x in hv):
        v = "PASS (batch-robust)" if all(res[h]["batch_t_met"] for h in ("A", "B")) else \
            "PASS (single-test), not batch-robust"
    elif all(res[h]["lift"]["ub"] is not None and res[h]["lift"]["ub"] < bar for h in ("A", "B")):
        v = "FAIL"
    else:
        v = "UNDERPOWERED"
    res["verdict"] = v
    return res


class SizeRef:
    """Median range of the SAME clock bar over the prior `n` kept sessions (needs >= `need`). Call ref() for today's
    bars BEFORE add() with today's ranges. reset() at the held-out restart (held-out sessions are never kept)."""

    def __init__(self, n: int = 10, need: int = 5):
        self.n, self.need, self.hist = n, need, defaultdict(list)

    def reset(self) -> None:
        self.hist.clear()

    def ref(self, k: int) -> float | None:
        xs = self.hist.get(k, [])
        return statistics.median(xs) if len(xs) >= self.need else None

    def add(self, bars: list) -> None:
        for k, b in enumerate(bars):
            if b is not None:
                lst = self.hist[k]
                lst.append(b[1] - b[2])
                if len(lst) > self.n:
                    lst.pop(0)


def power_bar(b, ref: float | None, side: int) -> bool:
    """Range >= 2 x same-clock median, body >= 60% of range, closes in the outer 20% in the trade's direction."""
    if b is None or ref is None or ref <= 0:
        return False
    o, h, lo, c = b[:4]
    rng = h - lo
    if rng < 2.0 * ref or rng <= 0 or abs(c - o) < 0.6 * rng:
        return False
    return (c > o and c >= h - 0.2 * rng) if side > 0 else (c < o and c <= lo + 0.2 * rng)
