#!/usr/bin/env python3
"""Confirmation indicators on the desk's own opens. Spec: docs/studies/indicator_confirm_prereg.json.

Do ADX trend strength (H1, H2), OBV / A/D confirmation at a 30-minute high (H3, H4) or the pasted article's
four-way confluence (H5) pick desk arms (decision-ledger 'arm' rows with arm_ok True) that beat the same
name's random later minute by more than the arms they exclude? Each arm is scored as d = net10(arm) -
net10(its control); a cell is graded on DiD = mean d(IN) - mean d(OUT), day-equal, day-clustered t.

  (default)  count step: inventory, day exclusions, per-cell counts and the projected MDE from CONTROL
             outcomes only (no arm outcome is read); video-setup count; writes count.json
  --null     null calibration on a driftless synthetic walk with path-selected synthetic arms (30-minute
             highs); writes null.json
  --score    score H1-H5 and the information cells. Refuses unless count.json exists, null.json passed and
             --skeptic-ready names the result-skeptic READY on this script.

Runs on the mini after the close (reads ai_reports/early_entry_honest/*.pkl and ai_reports/decision_ledger).
Features use IEX 1-minute RTH bars completed before the arm minute m (SIP as information); the outcome is the
harness markout on SIP bars: open of m+1 to close of m+10, minus 10 bp. "Bar m-1" is the last present bar at
or before m-1; a feature whose window has < 80% of its minutes present is None and the arm leaves that cell.
A control is a decision minute c in m+1 .. m+60, scored the same way (entry at c+1).
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import os
import random
import statistics
import sys
import zlib
from collections import Counter, defaultdict
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import early_entry_honest as E  # noqa: E402

OUT_DIR = os.path.join(E.ROOT, "ai_reports", "indicator_confirm")

RTH_START, RTH_END = E.RTH_START, E.RTH_END
ARM_START, ARM_END = 10 * 60, 15 * 60 + 30
C1_START = 10 * 60 + 31
V_START, V_END = 10 * 60 + 30, 14 * 60 + 30
REFRACT = 15
HOLD = 10
INFO_HOLDS = (5, 30)
COST = 0.0010
CTRL_SPAN = 60
MIN_PRICE = 10.0
FIRST_DAY, LAST_DAY, HALF_A_END = "2026-09-08", "2026-10-20", "2026-09-24"
ADX_N, ADX_TREND, ADX_CHOP = 14, 25.0, 20.0
CONF_BARS = 30
COVER = 0.80
MAX_ARM_MISS = 0.05
LIFT_BAR = 0.0005
MDE_BAR = 0.0015
MIN_ARMS, MIN_DAYS, MIN_SIDE = 100, 8, 3
N_TESTS, ALPHA_ONE_SIDED = 5, 0.025
CELLS = ("H1", "H2", "H3", "H4", "H5")
SEED_TAG = "indicator-confirm-v1"
NULL_DAYS, NULL_NAMES = 120, 20


# ── Student t without scipy ──────────────────────────────────────────────────

def _betacf(a: float, b: float, x: float) -> float:
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > 1e-300 else 1e-300)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > 1e-300 else 1e-300)
        c = 1.0 + aa / c
        c = c if abs(c) > 1e-300 else 1e-300
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > 1e-300 else 1e-300)
        c = 1.0 + aa / c
        c = c if abs(c) > 1e-300 else 1e-300
        de = d * c
        h *= de
        if abs(de - 1.0) < 1e-12:
            break
    return h


def _betainc(a: float, b: float, x: float) -> float:
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    lb = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1 - x)
    if x < (a + 1) / (a + b + 2):
        return math.exp(lb) * _betacf(a, b, x) / a
    return 1.0 - math.exp(lb) * _betacf(b, a, 1 - x) / b


def t_cdf(t: float, df: int) -> float:
    tail = 0.5 * _betainc(df / 2.0, 0.5, df / (df + t * t))
    return 1.0 - tail if t >= 0 else tail


def t_ppf(p: float, df: int) -> float:
    lo, hi = -1e4, 1e4
    for _ in range(200):
        mid = (lo + hi) / 2
        if t_cdf(mid, df) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def t_crit(days: int) -> float | None:
    """One-sided Bonferroni-5 critical t at df = days - 1 (prereg multiplicity)."""
    return t_ppf(1 - ALPHA_ONE_SIDED / N_TESTS, days - 1) if days >= 2 else None


# ── indicators on present RTH bars ───────────────────────────────────────────

def rth_bars(frame) -> dict[int, tuple]:
    """ET minute -> (open, high, low, close, volume) for RTH bars; pre-market and after-hours dropped."""
    out: dict[int, tuple] = {}
    if frame is None or not len(frame):
        return out
    for t, o, h, lo, c, v in zip(frame["time"], frame["open"], frame["high"], frame["low"],
                                 frame["close"], frame["volume"]):
        m = E._et_min(t.timestamp())
        if RTH_START <= m < RTH_END:
            out[m] = (float(o), float(h), float(lo), float(c), float(v))
    return out


def wilder_adx(h, lo, c, n: int = ADX_N):
    """Wilder ADX(n), +DI(n), -DI(n). TR/+DM/-DM seeded by the sum of their first n values, ADX by the mean
    of the first n DX values: valid from index 2n-1 (28 bars for n = 14)."""
    k = len(c)
    adx, pdi, mdi = [None] * k, [None] * k, [None] * k
    tr_s = pdm_s = mdm_s = None
    seed_tr, seed_p, seed_m, dxs = [], [], [], []
    a = None
    for i in range(1, k):
        up, dn = h[i] - h[i - 1], lo[i - 1] - lo[i]
        pdm = up if up > dn and up > 0 else 0.0
        mdm = dn if dn > up and dn > 0 else 0.0
        tr = max(h[i] - lo[i], abs(h[i] - c[i - 1]), abs(lo[i] - c[i - 1]))
        if tr_s is None:
            seed_tr.append(tr)
            seed_p.append(pdm)
            seed_m.append(mdm)
            if len(seed_tr) < n:
                continue
            tr_s, pdm_s, mdm_s = sum(seed_tr), sum(seed_p), sum(seed_m)
        else:
            tr_s = tr_s - tr_s / n + tr
            pdm_s = pdm_s - pdm_s / n + pdm
            mdm_s = mdm_s - mdm_s / n + mdm
        p = 100.0 * pdm_s / tr_s if tr_s > 0 else 0.0
        q = 100.0 * mdm_s / tr_s if tr_s > 0 else 0.0
        pdi[i], mdi[i] = p, q
        dx = 100.0 * abs(p - q) / (p + q) if p + q > 0 else 0.0
        if a is None:
            dxs.append(dx)
            if len(dxs) == n:
                a = sum(dxs) / n
                adx[i] = a
        else:
            a = (a * (n - 1) + dx) / n
            adx[i] = a
    return adx, pdi, mdi


def ema(c, n: int):
    """EMA alpha 2/(n+1), seeded by the SMA of the first n values."""
    out = [None] * len(c)
    if len(c) < n:
        return out
    a = 2.0 / (n + 1)
    e = sum(c[:n]) / n
    out[n - 1] = e
    for i in range(n, len(c)):
        e = a * c[i] + (1 - a) * e
        out[i] = e
    return out


def rsi_wilder(c, n: int = 14):
    """Wilder RSI(n), seeded by the simple mean of the first n gains and losses: valid from index n."""
    out = [None] * len(c)
    if len(c) <= n:
        return out
    gains = [max(c[i] - c[i - 1], 0.0) for i in range(1, len(c))]
    losses = [max(c[i - 1] - c[i], 0.0) for i in range(1, len(c))]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n

    def val(g, l_):
        return 100.0 if l_ == 0 else 100.0 - 100.0 / (1 + g / l_)
    out[n] = val(ag, al)
    for i in range(n + 1, len(c)):
        ag = (ag * (n - 1) + gains[i - 1]) / n
        al = (al * (n - 1) + losses[i - 1]) / n
        out[i] = val(ag, al)
    return out


def vwap(h, lo, c, v):
    out, pv, vv = [], 0.0, 0.0
    for a, b, d, w in zip(h, lo, c, v):
        pv += (a + b + d) / 3.0 * w
        vv += w
        out.append(pv / vv if vv > 0 else None)
    return out


def obv(c, v):
    out, s = [], 0.0
    for i in range(len(c)):
        if i > 0:
            s += v[i] if c[i] > c[i - 1] else (-v[i] if c[i] < c[i - 1] else 0.0)
        out.append(s)
    return out


def ad_line(h, lo, c, v):
    out, s = [], 0.0
    for a, b, d, w in zip(h, lo, c, v):
        if a > b:
            s += ((d - b) - (a - d)) / (a - b) * w
        out.append(s)
    return out


def indicators(bars: dict[int, tuple]) -> dict:
    mins = sorted(bars)
    o = [bars[m][0] for m in mins]
    h = [bars[m][1] for m in mins]
    lo = [bars[m][2] for m in mins]
    c = [bars[m][3] for m in mins]
    v = [bars[m][4] for m in mins]
    adx, pdi, mdi = wilder_adx(h, lo, c)
    return {"minutes": mins, "open": o, "high": h, "low": lo, "close": c, "vol": v,
            "adx": adx, "pdi": pdi, "mdi": mdi, "obv": obv(c, v), "ad": ad_line(h, lo, c, v),
            "ema20": ema(c, 20), "ema50": ema(c, 50), "rsi": rsi_wilder(c, 14), "vwap": vwap(h, lo, c, v)}


def _span(mins, a: int, b: int) -> range:
    return range(bisect.bisect_left(mins, a), bisect.bisect_right(mins, b))


def _cover(mins, a: int, b: int) -> float:
    return len(_span(mins, a, b)) / (b - a + 1) if b >= a else 0.0


def confirm_state(ind: dict, key: str, r: int, w: range) -> str:
    """CONFIRMS / DIVERGES when the close of bar r is at the window's high; NOT_AT_HIGH otherwise."""
    if ind["close"][r] < max(ind["close"][k] for k in w):
        return "NOT_AT_HIGH"
    return "CONFIRMS" if ind[key][r] >= max(ind[key][k] for k in w) else "DIVERGES"


def c1_at(ind: dict, m: int, r: int) -> tuple[bool | None, dict | None]:
    """The article's confluence at decision minute m (r = bar m-1). None when any leg cannot be read."""
    if m < C1_START:
        return None, None
    mins = ind["minutes"]
    if _cover(mins, m - 15, m - 1) < COVER:
        return None, None
    w15 = _span(mins, m - 15, m - 1)
    r11 = bisect.bisect_right(mins, m - 11) - 1
    e50 = ind["ema50"]
    if r11 < 0 or e50[r] is None or e50[r11] is None:
        return None, None
    trend = ind["close"][r] > e50[r] and e50[r] > e50[r11]
    kl = min(w15, key=lambda k: ind["low"][k])
    support = False
    for key in ("ema20", "vwap"):
        x = ind[key]
        if any(x[k] is None for k in w15):
            continue
        if abs(ind["low"][kl] / x[kl] - 1) <= 0.003 and all(ind["close"][k] >= x[k] * 0.997 for k in w15):
            support = True
    w10 = _span(mins, m - 10, m - 2)
    rs = ind["rsi"]
    if r < 1 or not w10 or rs[r] is None or rs[r - 1] is None or any(rs[k] is None for k in w10):
        return None, None
    turn = 38.0 <= min(rs[k] for k in w10) <= 52.0 and rs[r] > rs[r - 1]
    w6 = _span(mins, m - 6, m - 2)
    if not w6:
        return None, None
    vol_up = ind["vol"][r] > statistics.mean(ind["vol"][k] for k in w6)
    legs = {"trend": trend, "support": support, "rsi_turn": turn, "vol_up": vol_up}
    return all(legs.values()), legs


def adx5_at(bars: dict[int, tuple], m: int) -> tuple:
    """Information: ADX(14) on 5-minute bars aligned to 09:30, completed by m (valid from ~11:50)."""
    buckets: dict[int, list] = defaultdict(list)
    for k in sorted(bars):
        b = (k - RTH_START) // 5
        if RTH_START + 5 * (b + 1) <= m:
            buckets[b].append(bars[k])
    rows = [buckets[b] for b in sorted(buckets)]
    if len(rows) < 2 * ADX_N:
        return None, None, None
    h = [max(x[1] for x in r) for r in rows]
    lo = [min(x[2] for x in r) for r in rows]
    c = [r[-1][3] for r in rows]
    a, p, q = wilder_adx(h, lo, c)
    return a[-1], p[-1], q[-1]


def features_at(ind: dict, m: int) -> dict:
    mins = ind["minutes"]
    r = bisect.bisect_right(mins, m - 1) - 1
    if r < 0 or _cover(mins, RTH_START, m - 1) < COVER:
        return {"ok": False}
    f = {"ok": True, "adx": ind["adx"][r], "pdi": ind["pdi"][r], "mdi": ind["mdi"][r],
         "obv": None, "ad": None}
    w = _span(mins, m - CONF_BARS, mins[r] - 1)
    if len(w) and _cover(mins, m - CONF_BARS, m - 1) >= COVER:
        f["obv"] = confirm_state(ind, "obv", r, w)
        f["ad"] = confirm_state(ind, "ad", r, w)
    f["c1"], f["c1_legs"] = c1_at(ind, m, r)
    return f


def cell_side(f: dict, cell: str, m: int) -> str | None:
    """IN / OUT for the five graded cells; None when the arm is not in the cell."""
    if not f or not f.get("ok"):
        return None
    if cell in ("H1", "H2"):
        if f["adx"] is None:
            return None
        if cell == "H1":
            return "IN" if f["adx"] >= ADX_TREND and f["pdi"] > f["mdi"] else "OUT"
        return "IN" if f["adx"] >= ADX_CHOP else "OUT"
    if cell in ("H3", "H4"):
        st = f["obv" if cell == "H3" else "ad"]
        return {"CONFIRMS": "IN", "DIVERGES": "OUT"}.get(st)
    if cell == "H5":
        if m < C1_START or f["c1"] is None:
            return None
        return "IN" if f["c1"] else "OUT"
    raise ValueError(cell)


# ── population, control, outcome ─────────────────────────────────────────────

def arms_from_rows(rows, ok_only: bool = True) -> list[tuple[str, int]]:
    """(symbol, ET minute) arms: stage 'arm' (and arm_ok True when ok_only), 10:00-15:30, one per name per
    15 minutes counted on kept rows only, so a refusal never blocks a later True arm."""
    got = []
    for r in rows:
        if r.get("stage") != "arm" or not r.get("symbol") or r.get("ts") is None:
            continue
        if ok_only and r.get("arm_ok") is not True:
            continue
        m = E._et_min(float(r["ts"]))
        if ARM_START <= m <= ARM_END:
            got.append((float(r["ts"]), str(r["symbol"]).upper(), m))
    got.sort()
    last: dict[str, int] = {}
    out = []
    for _ts, sym, m in got:
        if sym in last and m - last[sym] < REFRACT:
            continue
        last[sym] = m
        out.append((sym, m))
    return out


def ledger_rows(day: str):
    path = os.path.join(E.LEDGER_DIR, f"{day}.jsonl")
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8", errors="replace"):
        try:
            yield json.loads(line)
        except ValueError:
            continue


def price_at(sip_bars: dict[int, tuple], m: int) -> float | None:
    """SIP close of the last RTH bar at or before m-1, no older than 5 minutes."""
    for k in range(m - 1, max(RTH_START, m - 6) - 1, -1):
        if k in sip_bars:
            return sip_bars[k][3]
    return None


def control_minute(sym: str, day: str, m: int, sip: dict, lo: int = 1, hold: int = HOLD) -> int | None:
    """A decision minute c in m+lo .. m+CTRL_SPAN with entry bar c+1 and exit bar c+hold in RTH, drawn with
    the prereg seed. One control per arm, shared by every cell."""
    elig = [c for c in range(m + lo, m + CTRL_SPAN + 1)
            if c + hold < RTH_END and (c + 1) in sip and (c + hold) in sip]
    if not elig:
        return None
    seed = int(hashlib.sha256(f"{sym}|{day}|{m}|{SEED_TAG}".encode()).hexdigest()[:16], 16)
    return random.Random(seed).choice(elig)


def net(sip: dict, m: int, hold: int = HOLD, lag: int = 1, cost: float = COST) -> float | None:
    """Decision at m: SIP open of m+lag to SIP close of m+lag+hold-1, minus cost."""
    g = E.markout(sip, m + lag, hold)
    return None if g is None else g - cost


def half_of(day: str) -> str:
    return "A" if day <= HALF_A_END else "B"


# ── building the arm table ───────────────────────────────────────────────────

def build(datas: list[dict], arm_lists: dict[str, list], stats: Counter, half_end: str = HALF_A_END,
          sip_features: bool = False) -> list[dict]:
    """One record per arm: features (IEX, plus SIP when asked) and the shared control minute."""
    recs = []
    for data in sorted(datas, key=lambda d: d["day"]):
        day = data["day"]
        cache_i, cache_s = {}, {}
        for sym, m in arm_lists.get(day, []):
            if sym not in cache_i:
                bi, bs = rth_bars(data["iex"].get(sym)), rth_bars(data["sip"].get(sym))
                cache_i[sym] = (bi, indicators(bi) if bi else None)
                cache_s[sym] = (bs, indicators(bs) if bs else None, E.sip_map(data["sip"][sym])
                                if sym in data["sip"] else {})
            bi, ind = cache_i[sym]
            bs, ind_s, sip = cache_s[sym]
            if ind is None or not bs:
                stats["no_bars"] += 1
                continue
            px = price_at(bs, m)
            if px is None:
                stats["no_price"] += 1
                continue
            if px < MIN_PRICE:
                stats["under_10"] += 1
                continue
            c = control_minute(sym, day, m, sip)
            if c is None:
                stats["no_control"] += 1
                continue
            rec = {"day": day, "half": "A" if day <= half_end else "B", "sym": sym, "m": m, "c": c,
                   "f": features_at(ind, m), "sip": sip, "bars": bi}
            if sip_features:
                rec["f_sip"] = features_at(ind_s, m) if ind_s else {"ok": False}
            if not rec["f"]["ok"]:
                stats["thin_bars"] += 1
            recs.append(rec)
    return recs


def outcomes(rec: dict) -> dict:
    """Arm and control nets for the primary and the information variants (computed only when scoring)."""
    sip, m, c = rec["sip"], rec["m"], rec["c"]
    o = {"arm": net(sip, m), "ctrl": net(sip, c), "arm_gross": net(sip, m, cost=0.0),
         "ctrl_gross": net(sip, c, cost=0.0), "arm_m2": net(sip, m, lag=2)}
    for hld in INFO_HOLDS:
        o[f"arm_h{hld}"] = net(sip, m, hold=hld)
        c2 = control_minute(rec["sym"], rec["day"], m, sip, hold=hld)
        o[f"ctrl_h{hld}"] = net(sip, c2, hold=hld) if c2 is not None else None
    c3 = control_minute(rec["sym"], rec["day"], m, sip, lo=11)
    o["ctrl_nooverlap"] = net(sip, c3) if c3 is not None else None
    return o


D_VARIANTS = {
    "net10": ("arm", "ctrl"),
    "gross10": ("arm_gross", "ctrl_gross"),
    "net5": ("arm_h5", "ctrl_h5"),
    "net30": ("arm_h30", "ctrl_h30"),
    "entry_m2": ("arm_m2", "ctrl"),
    "ctrl_m11_m60": ("arm", "ctrl_nooverlap"),
}


# ── statistics ───────────────────────────────────────────────────────────────

def day_mean_t(values_by_day: dict) -> tuple[float | None, float | None, int]:
    """Day-equal mean of per-day values (a list per day is averaged first) and its day-clustered t."""
    means = []
    for v in values_by_day.values():
        if isinstance(v, list):
            if v:
                means.append(statistics.mean(v))
        elif v is not None:
            means.append(v)
    if not means:
        return None, None, 0
    mu = statistics.mean(means)
    if len(means) < 2:
        return mu, None, len(means)
    se = statistics.stdev(means) / math.sqrt(len(means))
    return mu, (mu / se if se > 0 else None), len(means)


def grade_half(recs: list[dict], side_fn, dkey: str = "net10") -> dict:
    """Per half: lift_vs_random (IN), OUT lift, DiD on days with >= MIN_SIDE arms a side, name-day DiD,
    IN mean net, and the drop-3-names / drop-3-days DiD."""
    a_key, c_key = D_VARIANTS[dkey]
    rows = []
    for r in recs:
        s = side_fn(r)
        if s is None:
            continue
        a, c = r["o"].get(a_key), r["o"].get(c_key)
        if a is None or c is None:
            continue
        rows.append((r["day"], r["sym"], s, a - c, a))
    by = {"IN": defaultdict(list), "OUT": defaultdict(list)}
    net_in = defaultdict(list)
    for day, sym, s, d, a in rows:
        by[s][day].append(d)
        if s == "IN":
            net_in[day].append(a)
    n_in = sum(len(v) for v in by["IN"].values())
    n_out = sum(len(v) for v in by["OUT"].values())
    lift, lift_t, _ = day_mean_t(by["IN"])
    out_lift, out_t, _ = day_mean_t(by["OUT"])
    in_net, _, _ = day_mean_t(net_in)

    def did(rows_, drop_names=frozenset(), drop_days=frozenset()):
        per = {"IN": defaultdict(list), "OUT": defaultdict(list)}
        nd = {"IN": defaultdict(lambda: defaultdict(list)), "OUT": defaultdict(lambda: defaultdict(list))}
        for day, sym, s, d, _a in rows_:
            if sym in drop_names or day in drop_days:
                continue
            per[s][day].append(d)
            nd[s][day][sym].append(d)
        days = sorted(d for d in per["IN"] if len(per["IN"][d]) >= MIN_SIDE and len(per["OUT"][d]) >= MIN_SIDE)
        diffs = {d: statistics.mean(per["IN"][d]) - statistics.mean(per["OUT"][d]) for d in days}
        nd_diffs = {d: statistics.mean(statistics.mean(v) for v in nd["IN"][d].values())
                    - statistics.mean(statistics.mean(v) for v in nd["OUT"][d].values()) for d in days}
        return diffs, nd_diffs, per
    diffs, nd_diffs, per = did(rows)
    did_mu, did_t, ndays = day_mean_t(diffs)
    nd_mu, _, _ = day_mean_t(nd_diffs)
    # each name's additive share of the day-equal DiD
    contrib: Counter = Counter()
    for day, sym, s, d, _a in rows:
        if day in diffs:
            share = d / len(per[s][day]) / len(diffs)
            contrib[sym] += share if s == "IN" else -share
    top_names = frozenset(n for n, _ in contrib.most_common(3))
    top_days = frozenset(sorted(diffs, key=lambda k: diffs[k], reverse=True)[:3])
    dn = day_mean_t(did(rows, drop_names=top_names)[0])[0]
    dd = day_mean_t(did(rows, drop_days=top_days)[0])[0]
    return {"n_in": n_in, "n_out": n_out, "lift": lift, "lift_t": lift_t, "out_lift": out_lift, "out_t": out_t,
            "in_net": in_net, "did": did_mu, "did_t": did_t, "days": ndays, "did_nameday": nd_mu,
            "did_drop_names": dn, "did_drop_days": dd, "t_crit": t_crit(ndays),
            "dropped_names": sorted(top_names), "dropped_days": sorted(top_days)}


def half_verdict(g: dict, cell: str) -> str:
    """SCORED halves only; a half short of arms or days is UNDERPOWERED and its outcomes are not used."""
    if g["n_in"] < MIN_ARMS or g["n_out"] < MIN_ARMS or g["days"] < MIN_DAYS or g["did"] is None \
            or g["did_t"] is None:
        return "UNDERPOWERED"
    if g["did_t"] <= -2.0:
        return "FAIL"
    ok = (g["did"] >= LIFT_BAR and g["did_t"] >= g["t_crit"]
          and g["lift"] is not None and g["lift"] >= 0
          and g["in_net"] is not None and g["in_net"] > 0
          and g["did_nameday"] is not None and g["did_nameday"] > 0
          and g["did_drop_names"] is not None and g["did_drop_names"] >= 0
          and g["did_drop_days"] is not None and g["did_drop_days"] >= 0)
    if cell == "H2":
        ok = ok and g["out_lift"] is not None and g["out_lift"] < 0
    return "PASS" if ok else "NOT_PASSED"


def final_verdict(halves: list[str], mdes: list[float | None]) -> str:
    """Prereg: PASS needs both halves PASS. FAIL if a scored half has DiD t <= -2 (half verdict FAIL), or if
    both halves are scored with projected MDE <= 15 bp and the pass is not met. Otherwise UNDERPOWERED: one
    half's own MDE never fails a cell (skeptic 10/21 #1)."""
    if "FAIL" in halves:
        return "FAIL"
    if all(h == "PASS" for h in halves):
        return "PASS"
    if all(h != "UNDERPOWERED" for h in halves) and all(m is not None and m <= MDE_BAR for m in mdes):
        return "FAIL"
    return "UNDERPOWERED"


# ── count step (outcome-blind) ───────────────────────────────────────────────

def count_step(recs: list[dict]) -> dict:
    """Counts, and (amended_3) projected SE = sqrt(2) x the day-clustered SE, over the DiD days, of each day's
    mean CONTROL net10 of IN arms minus mean CONTROL net10 of OUT arms. No arm outcome is read."""
    out = {}
    for cell in CELLS:
        out[cell] = {}
        for half in ("A", "B"):
            rs = [r for r in recs if r["half"] == half]
            side = {id(r): cell_side(r["f"], cell, r["m"]) for r in rs}
            ins = [r for r in rs if side[id(r)] == "IN"]
            outs = [r for r in rs if side[id(r)] == "OUT"]
            per_in, per_out = Counter(r["day"] for r in ins), Counter(r["day"] for r in outs)
            days = sorted(d for d in per_in if per_in[d] >= MIN_SIDE and per_out[d] >= MIN_SIDE)
            ctrl = {"IN": defaultdict(list), "OUT": defaultdict(list)}
            for side_, group in (("IN", ins), ("OUT", outs)):
                for r in group:
                    v = net(r["sip"], r["c"])
                    if v is not None:
                        ctrl[side_][r["day"]].append(v)
            diffs = [statistics.mean(ctrl["IN"][d]) - statistics.mean(ctrl["OUT"][d]) for d in days
                     if ctrl["IN"][d] and ctrl["OUT"][d]]
            se = (statistics.stdev(diffs) / math.sqrt(len(diffs))) if len(diffs) >= 2 else None
            tc = t_crit(len(days))
            proj_se = math.sqrt(2) * se if se is not None else None
            mde = (tc + 0.84) * proj_se if proj_se is not None and tc is not None else None
            powered = len(ins) >= MIN_ARMS and len(outs) >= MIN_ARMS and len(days) >= MIN_DAYS
            out[cell][half] = {"n_in": len(ins), "n_out": len(outs),
                               "in_namedays": len({(r["sym"], r["day"]) for r in ins}),
                               "days_both_sides": len(days), "t_crit": tc, "projected_se": proj_se,
                               "projected_mde": mde, "scored": powered}
    return out


def video_count(arm_lists: dict[str, list]) -> dict:
    nd = {(sym, day) for day, arms in arm_lists.items() for sym, m in arms if V_START <= m <= V_END}
    return {"arm_namedays_10_30_14_30": len(nd), "fetch": "not done: needs the operator's OK (prereg)",
            "scored": False}


# ── data ─────────────────────────────────────────────────────────────────────

def load_study_days(ok_only: bool = True) -> tuple[list[dict], dict[str, list], list, Counter]:
    datas, excluded = E.load_days()
    excluded = [(d, f"load_days: {why}") for d, why in excluded]
    keep, arm_lists, stats = [], {}, Counter()
    for data in datas:
        day = data["day"]
        if not FIRST_DAY <= day <= LAST_DAY:
            continue
        arms = arms_from_rows(ledger_rows(day), ok_only=ok_only)
        names = {s for s, _m in arms}
        if names:
            miss = sum(1 for s in names if not rth_bars(data["iex"].get(s)) or not rth_bars(data["sip"].get(s)))
            if miss / len(names) > MAX_ARM_MISS:
                excluded.append((day, f"arm name-days missing IEX/SIP bars {miss}/{len(names)}"))
                continue
        keep.append(data)
        arm_lists[day] = arms
        stats[f"arms_{half_of(day)}"] += len(arms)
    return keep, arm_lists, excluded, stats


def script_sha() -> str:
    """count.json and null.json carry the sha256 of the script that wrote them; --score refuses a mismatch."""
    return hashlib.sha256(open(os.path.abspath(__file__), "rb").read()).hexdigest()


def write_json(name: str, obj) -> str:
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, name)
    with open(path, "w") as f:
        json.dump(obj, f, indent=1, default=str)
    return path


# ── null calibration ─────────────────────────────────────────────────────────

def null_arms(frames: dict, refract: int = REFRACT) -> list[tuple[str, int]]:
    """Path-selected synthetic arms: minutes in 10:00-15:30 where the close of bar m-1 is a 30-minute high."""
    out = []
    for sym, fr in sorted(frames.items()):
        bars = rth_bars(fr)
        last = None
        for m in range(ARM_START, ARM_END + 1):
            w = [bars[k][3] for k in range(m - CONF_BARS, m - 1) if k in bars]
            if (m - 1) not in bars or not w or bars[m - 1][3] < max(w):
                continue
            if last is not None and m - last < refract:
                continue
            out.append((sym, m))
            last = m
    return out


def null_calibration(n_days: int = NULL_DAYS, n_names: int = NULL_NAMES) -> dict:
    import early_entry_honest_null as N
    datas, arm_lists, d0 = [], {}, datetime(2026, 6, 1)
    for k in range(n_days):
        day = (d0 + timedelta(days=k)).strftime("%Y-%m-%d")
        fr = {f"S{i}": N.frame(day, zlib.crc32(f"S{i}{day}".encode())) for i in range(n_names)}
        datas.append({"day": day, "iex": fr, "sip": fr})
        arm_lists[day] = null_arms(fr)
    half_end = sorted(arm_lists)[n_days // 2 - 1]
    recs = build(datas, arm_lists, Counter(), half_end=half_end)
    for r in recs:
        r["o"] = outcomes(r)
    checks = []
    for cell in CELLS:
        for half in ("A", "B"):
            g = grade_half([r for r in recs if r["half"] == half],
                           lambda r, c=cell: cell_side(r["f"], c, r["m"]))
            for stat in ("lift_t", "did_t"):
                checks.append({"cell": cell, "half": half, "stat": stat, "t": g[stat], "n_in": g["n_in"],
                               "n_out": g["n_out"], "days": g["days"]})
    k = sum(1 for c in checks if c["t"] is not None)
    # amended_2: under an unbiased method each t is ~N(0,1); a fixed |t| <= 1 on every one of ~20 stats
    # fails by chance ~99.9% of the time. The bound is Bonferroni over the stats actually computed.
    bound = t_ppf(1 - 0.025 / max(k, 1), 10_000)
    ok = k > 0 and all(abs(c["t"]) <= bound for c in checks if c["t"] is not None)
    return {"days": n_days, "names": n_names, "arms": len(recs), "bound": bound, "n_stats": k,
            "max_abs_t": max((abs(c["t"]) for c in checks if c["t"] is not None), default=None),
            "share_abs_t_le_1": (sum(1 for c in checks if c["t"] is not None and abs(c["t"]) <= 1) / k) if k else None,
            "ok": ok, "checks": checks}


# ── report ───────────────────────────────────────────────────────────────────

def _bp(x) -> str:
    return "-" if x is None else f"{x * 1e4:+.1f}"


def _t(x) -> str:
    return "-" if x is None else f"{x:+.2f}"


def info_cells(recs: list[dict], fkey: str = "f") -> dict:
    """Information only (cannot pass): pairwise ANDs, ADX terciles, +DI alone, NOT_AT_HIGH, C1 legs, 5-min ADX."""
    def f(r):
        return r.get(fkey) or {}

    def ins(r, cell):
        return cell_side(f(r), cell, r["m"]) == "IN" if cell != "C1" else cell_side(f(r), "H5", r["m"]) == "IN"

    def defined(r, cell):
        return cell_side(f(r), "H5" if cell == "C1" else cell, r["m"]) is not None
    cells = {}
    base = ("H1", "H3", "H4", "C1")
    for i, a in enumerate(base):
        for b in base[i + 1:]:
            cells[f"{a}&{b}"] = lambda r, a=a, b=b: (("IN" if ins(r, a) and ins(r, b) else "OUT")
                                                      if defined(r, a) and defined(r, b) else None)
    adx = sorted(f(r)["adx"] for r in recs if f(r).get("ok") and f(r).get("adx") is not None)
    if len(adx) >= 3:
        t1, t2 = adx[len(adx) // 3], adx[2 * len(adx) // 3]
        for name, lo, hi in (("adx_T1", -1, t1), ("adx_T2", t1, t2), ("adx_T3", t2, 1e9)):
            cells[name] = lambda r, lo=lo, hi=hi: (None if not f(r).get("ok") or f(r).get("adx") is None
                                                   else ("IN" if lo <= f(r)["adx"] < hi else "OUT"))
    cells["+DI>-DI"] = lambda r: (None if not f(r).get("ok") or f(r).get("pdi") is None
                                  else ("IN" if f(r)["pdi"] > f(r)["mdi"] else "OUT"))
    for key in ("obv", "ad"):
        cells[f"{key}_not_at_high"] = lambda r, key=key: (None if not f(r).get("ok") or f(r).get(key) is None
                                                          else ("IN" if f(r)[key] == "NOT_AT_HIGH" else "OUT"))
    for leg in ("trend", "support", "rsi_turn", "vol_up"):
        cells[f"C1_{leg}"] = lambda r, leg=leg: (None if not f(r).get("ok") or not f(r).get("c1_legs")
                                                 else ("IN" if f(r)["c1_legs"][leg] else "OUT"))
    for leg in ("trend", "support", "rsi_turn", "vol_up"):
        cells[f"C1_without_{leg}"] = lambda r, leg=leg: (
            None if not f(r).get("ok") or not f(r).get("c1_legs")
            else ("IN" if all(v for k, v in f(r)["c1_legs"].items() if k != leg) else "OUT"))
    cells["adx5_trend"] = lambda r: (None if r.get("adx5") is None or r["adx5"][0] is None
                                     else ("IN" if r["adx5"][0] >= ADX_TREND and r["adx5"][1] > r["adx5"][2]
                                           else "OUT"))
    return cells


def table(recs: list[dict], cells: dict, dkey: str = "net10", scored: dict | None = None) -> list[str]:
    """`scored` ({cell: {half: bool}}): a cell-half the count step did not score prints counts only."""
    lines = ["| cell | half | IN | OUT | days | lift vs random | t | DiD | t |", "|---|---|---|---|---|---|---|---|---|"]
    for name, fn in cells.items():
        for half in ("A", "B"):
            g = grade_half([r for r in recs if r["half"] == half], fn, dkey)
            if scored is not None and not scored.get(name, {}).get(half, True):
                lines.append(f"| {name} | {half} | {g['n_in']} | {g['n_out']} | {g['days']} | not scored | | | |")
                continue
            lines.append(f"| {name} | {half} | {g['n_in']} | {g['n_out']} | {g['days']} | {_bp(g['lift'])} | "
                         f"{_t(g['lift_t'])} | {_bp(g['did'])} | {_t(g['did_t'])} |")
    return lines


def report(res: dict) -> str:
    L = ["# Indicator confirmation on desk arms: result", "",
         "Prereg: docs/studies/indicator_confirm_prereg.json. Primary: arm_ok True arms, IEX features, SIP net10 "
         "(10 bp), DiD vs the same-name later control. bp per trade.", "",
         f"Days: {res['days']}  excluded: {res['excluded']}", f"Arm counts: {res['stats']}", "",
         "## Graded cells", "",
         "| cell | half | IN | OUT | days | DiD | t | t_crit | lift vs random | OUT lift | IN net | name-day DiD | "
         "drop 3 names | drop 3 days | proj. MDE | half |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for cell in CELLS:
        for half in ("A", "B"):
            g, v, mde = res["graded"][cell][half], res["half_verdicts"][cell][half], res["mde"][cell][half]
            L.append(f"| {cell} | {half} | {g['n_in']} | {g['n_out']} | {g['days']} | {_bp(g['did'])} | "
                     f"{_t(g['did_t'])} | {_t(g['t_crit'])} | {_bp(g['lift'])} | {_bp(g['out_lift'])} | "
                     f"{_bp(g['in_net'])} | {_bp(g['did_nameday'])} | {_bp(g['did_drop_names'])} | "
                     f"{_bp(g['did_drop_days'])} | {_bp(mde)} | {v} |")
    L += ["", "| cell | verdict |", "|---|---|"]
    L += [f"| {c} | **{res['verdicts'][c]}** |" for c in CELLS]
    L += ["", "A FAIL closes the cell only for effects >= its printed MDE; UNDERPOWERED is no evidence either way.",
          "t_crit uses df = DiD days - 1 per half (stricter than the prereg's ~3.05 / ~2.90 when a cell has fewer "
          "DiD days than the half has sessions).",
          "", "## Information only (cannot pass)", ""]
    for title, lines in res["info"].items():
        L += [f"### {title}", ""] + lines + [""]
    L += [f"Controls in m+1..m+10 (overlapping the arm's hold): {res['overlap_share']:.1%}", "",
          f"Video setups: {res['video']}"]
    return "\n".join(L)


def score(recs: list[dict], count: dict, extra: dict) -> dict:
    for r in recs:
        r["o"] = outcomes(r)
    graded, halves, verdicts, mde = {}, {}, {}, {}
    for cell in CELLS:
        graded[cell], halves[cell], mde[cell] = {}, {}, {}
        for half in ("A", "B"):
            g = grade_half([r for r in recs if r["half"] == half], lambda r, c=cell: cell_side(r["f"], c, r["m"]))
            m_ = count[cell][half]["projected_mde"]
            if not count[cell][half]["scored"]:
                g = {k: (None if not k.startswith("n_") and k != "days" else v) for k, v in g.items()}
                g["dropped_names"], g["dropped_days"] = [], []
                halves[cell][half] = "UNDERPOWERED"
            else:
                halves[cell][half] = half_verdict(g, cell)
            graded[cell][half], mde[cell][half] = g, m_
        verdicts[cell] = final_verdict([halves[cell][h] for h in ("A", "B")], [mde[cell][h] for h in ("A", "B")])
    for r in recs:
        r["adx5"] = adx5_at(r["bars"], r["m"])
    prim = {c: (lambda r, c=c: cell_side(r["f"], c, r["m"])) for c in CELLS}
    sc = {c: {h: count[c][h]["scored"] for h in ("A", "B")} for c in CELLS}
    info = {"cells (IEX features, net10)": table(recs, info_cells(recs))}
    for dkey in D_VARIANTS:
        if dkey != "net10":
            info[f"H1-H5, {dkey}"] = table(recs, prim, dkey, scored=sc)
    info["H1-H5 on SIP features"] = table(recs, {c: (lambda r, c=c: cell_side(r.get("f_sip") or {}, c, r["m"]))
                                                 for c in CELLS}, scored=sc)
    for label, rs in extra.items():
        for r in rs:
            r["o"] = outcomes(r)
        info[f"H1-H5, {label}"] = table(rs, prim, scored=sc)
    overlap = sum(1 for r in recs if r["c"] <= r["m"] + 10) / len(recs) if recs else 0.0
    return {"graded": graded, "half_verdicts": halves, "verdicts": verdicts, "mde": mde, "info": info,
            "overlap_share": overlap}


def fills_arms(days: set) -> dict[str, list]:
    out: dict[str, list] = defaultdict(list)
    for (sym, day), tss in E.desk_fills(days).items():
        for ts in sorted(tss):
            m = E._et_min(ts)
            if ARM_START <= m <= ARM_END:
                out[day].append((sym, m))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--null", action="store_true", help="run the null calibration and write null.json")
    ap.add_argument("--score", action="store_true")
    ap.add_argument("--skeptic-ready", default="", help="the result-skeptic READY on this script (required by --score)")
    a = ap.parse_args()
    if a.null:
        res = null_calibration()
        print(f"null: {res['arms']} synthetic arms, {res['n_stats']} stats, max |t| {res['max_abs_t']:.2f} "
              f"(bound {res['bound']:.2f}), share |t|<=1 {res['share_abs_t_le_1']:.0%}: "
              f"{'OK' if res['ok'] else 'NOT OK - fix the method before any score'}")
        res["script_sha256"] = script_sha()
        print(write_json("null.json", res))
        return
    datas, arm_lists, excluded, stats = load_study_days()
    days = sorted(d["day"] for d in datas)
    print(f"usable days {len(days)} ({days[0] if days else '-'} .. {days[-1] if days else '-'}), "
          f"half A <= {HALF_A_END}")
    print(f"excluded: {excluded}")
    recs = build(datas, arm_lists, stats, sip_features=a.score)
    print(f"arms (arm_ok True, 10:00-15:30, deduped): {dict(stats)}; kept {len(recs)}")
    if not a.score:
        cnt = count_step(recs)
        for cell in CELLS:
            for half in ("A", "B"):
                c = cnt[cell][half]
                print(f"  {cell} {half}: IN {c['n_in']} OUT {c['n_out']} IN name-days {c['in_namedays']} "
                      f"days {c['days_both_sides']} t_crit {_t(c['t_crit'])} MDE {_bp(c['projected_mde'])} bp "
                      f"{'scored' if c['scored'] else 'UNDERPOWERED - not scored'}")
        vid = video_count(arm_lists)
        print(f"video setups: {vid}")
        print(write_json("count.json", {"script_sha256": script_sha(), "days": days, "excluded": excluded,
                                        "stats": dict(stats),
                                        "cells": cnt, "video": vid}))
        print("(count step only; --null next, --score after a result-skeptic READY on this script)")
        return
    cpath, npath = os.path.join(OUT_DIR, "count.json"), os.path.join(OUT_DIR, "null.json")
    if not a.skeptic_ready:
        raise SystemExit("--score needs --skeptic-ready (the result-skeptic READY on this script)")
    if not os.path.exists(cpath):
        raise SystemExit("run the count step first (no count.json)")
    if not os.path.exists(npath) or not json.load(open(npath)).get("ok"):
        raise SystemExit("null calibration missing or NOT OK (run --null; fix the method if it fails)")
    count = json.load(open(cpath))
    for name, obj in (("count.json", count), ("null.json", json.load(open(npath)))):
        if obj.get("script_sha256") != script_sha():
            raise SystemExit(f"{name} was written by a different script version; rerun it with this one")
    if count["days"] != days:
        raise SystemExit("the day set changed since the count step; rerun the count step")
    all_lists = {d: arms_from_rows(ledger_rows(d), ok_only=False) for d in days}
    extra = {"all arm rows (refusals included)": build(datas, all_lists, Counter()),
             "paper fills": build(datas, fills_arms(set(days)), Counter())}
    res = score(recs, count["cells"], extra)
    res.update({"days": days, "excluded": excluded, "stats": dict(stats), "video": count["video"],
                "skeptic_ready": a.skeptic_ready})
    md = report(res)
    print(md)
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "result.md"), "w") as f:
        f.write(md + "\n")
    write_json("result.json", {k: v for k, v in res.items() if k != "info"})


if __name__ == "__main__":
    main()
