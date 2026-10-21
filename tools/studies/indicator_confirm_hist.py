#!/usr/bin/env python3
"""Confirmation indicators on history, powered. Spec: docs/studies/indicator_confirm_hist_prereg.json.

H1-H5: do ADX trend / chop, OBV or A/D confirmation, or the article's confluence pick desk-style pre-square (PRE)
entries on a 400-name liquid sample that beat both the same name's later minute (d) and a random other name at the
same minute (x), by more than the entries they exclude? V: do the 10/8 video setups S3-S7 (S2 dropped, operator
10/21) pay as standalone 10-minute entries on every warehouse name with 10 prior SIP sessions?

Data: the warehouse (ai_reports/warehouse/{iex,sip}/<day>.npz: RTH 1-minute bars, split-adjusted, no quotes). The
first 10 sessions are look-back only. Features and verdict pieces come from indicator_confirm_study (desk study),
PRE from early_entry_honest (warm %R lines, as prescreen_turn), setups from structure_pullback's scanners with the
prereg's warehouse adaptation (R-bound price = SIP close of the bar ending at t, no stale-quote rule, prior close
and low from the prior session's SIP bars).

  (default)    count step: per H cell, V setup and half: entries, name-days, days, projected MDE from CONTROL
               outcomes only, the control's own IN-OUT gap, V detector funnels -> count.json
  --null       synthetic driftless walk (amended_2: continuous across sessions, 0.05-0.15%/min per name, 2,500
               names x 40 sessions + look-back): PRE onsets on warm %R, features, both controls, H DiD_d / DiD_x and
               V lift_d / lift_x; every statistic present with >= 300 entries and every |t| within the Bonferroni
               bound -> null.json
  --placebo    real data, IN / OUT by a seeded coin flip per entry for every H cell; same bound -> placebo.json
  --handcheck  5 seeded entries per V setup with their 1/5/15/60-minute bars -> handcheck.md (for the reviewer)
  --score      everything; refuses unless count / null / placebo were written by THIS script (sha256) and passed,
               handcheck.md exists, and --skeptic-ready names the result-skeptic READY on this script
  --days N     limit to the first N scored sessions (smoke runs; never for the verdict)
  --workers    processes (default 4; nice the run on the mini)

Runs on the mini after the close (no Alpaca calls).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import statistics
import sys
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bars_structure as BS  # noqa: E402
import early_entry_honest as E  # noqa: E402
import indicator_confirm_study as S  # noqa: E402
import structure_pullback as SP  # noqa: E402

WH = os.path.join(E.ROOT, "ai_reports", "warehouse")
OUT_DIR = os.path.join(E.ROOT, "ai_reports", "indicator_confirm_hist")

SAMPLE = 400
LOOKBACK = 10
HALF_A_END = "2026-07-29"
V_SETUPS = ("S3", "S4", "S5", "S6", "S7")
CELLS = S.CELLS
MIN_ENTRIES, MIN_DAYS, MIN_SIDE = 300, 20, 3
PRE_ONSET_LO, PRE_ONSET_HI = S.ARM_START - 1, S.ARM_END - 1     # decision m = onset + 1 in 10:00-15:30
REFRACT = 15
LIFT_BAR = 0.0005
BOARD_UP = 0.02
TIME_SPLIT = 11 * 60 + 25
SAME_TAG, CROSS_TAG, SAMPLE_TAG = "indicator-hist-v1", "indicator-hist-cross", "indicator-hist-sample"
NULL_DAYS, NULL_NAMES, NULL_START = 40, 2500, "2026-01-05"
NULL_VOL = (0.0005, 0.0015)          # per-name 1-minute volatility, so S3 consolidations (<= 1% over an hour) occur
NULL_MIN_N = 300                     # amended_2: every null / placebo statistic needs >= 300 entries
HANDCHECK_N, HANDCHECK_DAYS = 5, 3
NOT_COMPUTED = {
    "S7 with the indication-high exit read as a markout at the same 10 minutes":
        "identical to S7's primary net10: a 10-minute markout has no exit level, so there is nothing to compute",
}

# x = arm vs the same-minute cross-name control; entry at m = onset + 1 (prescreen's timing). Added at import so the
# desk study's grading and table code can read them; indicator_confirm_study.py itself is not edited.
S.D_VARIANTS.setdefault("cross", ("arm", "cross"))
S.D_VARIANTS.setdefault("entry_m0", ("arm_m0", "ctrl"))


# ── bar sources ──────────────────────────────────────────────────────────────

def warehouse_days() -> list[str]:
    have = [{x[:-4] for x in os.listdir(os.path.join(WH, f)) if x.endswith(".npz")} for f in ("iex", "sip")]
    return sorted(have[0] & have[1])


def load_raw(feed: str, day: str) -> dict[str, np.ndarray]:
    """sym -> array of rows (minute, o, h, l, c, v), sorted by minute, RTH only (the warehouse is RTH-only)."""
    z = np.load(os.path.join(WH, feed, f"{day}.npz"), allow_pickle=False)
    syms, idx, mins = z["syms"], z["sym_idx"], z["minute"]
    rows = np.column_stack([mins.astype(float), z["o"], z["h"], z["l"], z["c"], z["v"]]).astype(float)
    order = np.lexsort((mins, idx))
    idx, rows = idx[order], rows[order]
    bounds = np.searchsorted(idx, np.arange(len(syms) + 1))
    out = {}
    for k, s in enumerate(syms):
        a, b = bounds[k], bounds[k + 1]
        if b > a:
            r = rows[a:b]
            r = r[(r[:, 0] >= S.RTH_START) & (r[:, 0] < S.RTH_END)]
            if len(r):
                out[str(s)] = r
    return out


_NULL_LEVEL: dict[tuple[int, int], np.ndarray] = {}


def _null_day(i: int, n: int) -> np.ndarray:
    """Log returns (n names x 390 minutes) of synthetic session i; seeded by the session index only."""
    vol = NULL_VOL[0] + (NULL_VOL[1] - NULL_VOL[0]) * np.random.default_rng(7).random(n)
    return np.random.default_rng(1000 + i).standard_normal((n, S.RTH_END - S.RTH_START)) * vol[:, None]


def null_raw(_feed: str, day: str, n: int | None = None) -> dict[str, np.ndarray]:
    """amended_2 null: a driftless log walk per name, continuous across sessions (each open = the previous close),
    per-name 1-minute volatility in NULL_VOL, IEX = SIP, every RTH minute present, volume lognormal with 5% x3
    spikes. Session levels are cached per process so each session's returns are drawn once."""
    n = n or NULL_NAMES
    i = null_days().index(day)
    if (0, n) not in _NULL_LEVEL:
        _NULL_LEVEL[(0, n)] = np.log(20 + 80 * np.random.default_rng(11).random(n))
    for j in range(1, i + 1):
        if (j, n) not in _NULL_LEVEL:
            _NULL_LEVEL[(j, n)] = _NULL_LEVEL[(j - 1, n)] + _null_day(j - 1, n).sum(axis=1)
    z = _null_day(i, n)
    lc = _NULL_LEVEL[(i, n)][:, None] + np.cumsum(z, axis=1)
    c = np.exp(lc)
    o = np.exp(np.concatenate([_NULL_LEVEL[(i, n)][:, None], lc[:, :-1]], axis=1))
    rng = np.random.default_rng(5000 + i)
    vol = NULL_VOL[0] + (NULL_VOL[1] - NULL_VOL[0]) * np.random.default_rng(7).random(n)
    wick = np.abs(rng.standard_normal((2, n, z.shape[1]))) * vol[None, :, None] * 0.5
    h = np.maximum(o, c) * (1 + wick[0])
    lo = np.minimum(o, c) * (1 - wick[1])
    v = rng.lognormal(6.0, 0.8, z.shape) * np.where(rng.random(z.shape) < 0.05, 3.0, 1.0)
    mins = np.arange(S.RTH_START, S.RTH_END, dtype=float)
    return {f"S{k}": np.column_stack([mins, o[k], h[k], lo[k], c[k], v[k]]) for k in range(n)}


SOURCES = {"wh": load_raw, "null": null_raw}
_RAW: dict[tuple, dict] = {}
_S60: dict[tuple, "Session60"] = {}


def get_raw(src: str, feed: str, day: str, keep: set[str]) -> dict:
    """Per-process cache; workers get contiguous blocks of days, so each look-back session is loaded and aggregated
    once per worker. Days outside `keep` are evicted. Null IEX and SIP are the same walk."""
    for key in [k for k in _RAW if k[2] not in keep]:
        del _RAW[key]
    for key in [k for k in _S60 if k[1] not in keep]:
        del _S60[key]
    feed_key = "sip" if src == "null" else feed
    if (src, feed_key, day) not in _RAW:
        _RAW[(src, feed_key, day)] = SOURCES[src](feed_key, day)
    return _RAW[(src, feed_key, day)]


def bars_dict(r: np.ndarray) -> dict[int, tuple]:
    return {int(x[0]): (x[1], x[2], x[3], x[4], x[5]) for x in r}


def sip_oc(r: np.ndarray) -> dict[int, tuple]:
    """minute -> (open, close), the shape early_entry_honest.markout reads."""
    return {int(x[0]): (x[1], x[4]) for x in r}


def frame(day: str, r: np.ndarray):
    import pandas as pd
    base = pd.Timestamp(day, tz=E.ET)
    t = [(base + pd.Timedelta(minutes=int(m))).tz_convert("UTC") for m in r[:, 0]]
    return pd.DataFrame({"time": t, "open": r[:, 1], "high": r[:, 2], "low": r[:, 3], "close": r[:, 4],
                         "volume": r[:, 5]})


def bs_rows(day: str, r: np.ndarray) -> list:
    """bars_structure 1-minute rows: [start_ts, o, h, l, c, v]."""
    t0 = BS.et_ts(day, 0)
    return [[t0 + 60.0 * x[0], x[1], x[2], x[3], x[4], x[5]] for x in r]


def sample_names(day: str, names: list[str], n: int = SAMPLE) -> list[str]:
    rng = random.Random(int(hashlib.sha256(f"{day}|{SAMPLE_TAG}".encode()).hexdigest()[:16], 16))
    names = sorted(names)
    return names if len(names) <= n else sorted(rng.sample(names, n))


def half_of(day: str, half_end: str | None = None) -> str:
    return "A" if day <= (half_end or HALF_A_END) else "B"


# ── entries and controls ─────────────────────────────────────────────────────

def pre_minutes(ser: dict) -> list[int]:
    """Decision minutes m = PRE onset bar + 1 in 10:00-15:30, 15-minute refractory on onsets (every minute counts
    as on the board, as prescreen_turn)."""
    m, f, s = ser["minutes"], ser["fast"], ser["slow"]
    flags = [E.pre_flag(f, s, i) for i in range(len(m))]
    onsets, _off = E.fires(m, flags, set(m), start=PRE_ONSET_LO, end=PRE_ONSET_HI, refract=REFRACT)
    return [x + 1 for x in onsets]


def _seed(*parts) -> int:
    return int(hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:16], 16)


def _eligible(sip: dict, c: int, hold: int) -> bool:
    return c + hold < S.RTH_END and (c + 1) in sip and (c + hold) in sip


def control_minute(sym: str, day: str, m: int, sip: dict, hold: int = S.HOLD, lo: int = 1) -> int | None:
    """Same eligibility as the desk study's control (decision minute c in m+lo .. m+60, entry c+1, exit c+hold in
    RTH), with this prereg's seed tag."""
    elig = [c for c in range(m + lo, m + S.CTRL_SPAN + 1) if _eligible(sip, c, hold)]
    if not elig:
        return None
    return random.Random(_seed(sym, day, m, SAME_TAG)).choice(elig)


def cross_name(sym: str, day: str, m: int, pool: list[str], sipm, hold: int = S.HOLD, tries: int = 64) -> str | None:
    """A random OTHER pool name with bars at m+1 and m+hold, seed sha256(symbol|day|m|indicator-hist-cross).
    Rejection sampling from the sorted pool is uniform over the eligible names and avoids scanning ~1,850 names per
    entry; a full scan backs it up."""
    if not pool:
        return None
    rng = random.Random(_seed(sym, day, m, CROSS_TAG))

    def ok(n):
        return n != sym and _eligible(sipm(n), m, hold)
    for _ in range(tries):
        n = pool[rng.randrange(len(pool))]
        if ok(n):
            return n
    elig = [n for n in pool if ok(n)]
    return rng.choice(elig) if elig else None


def outcomes(sipm: dict, m: int, c: int | None, xm: dict | None, sym: str, day: str, full: bool) -> dict:
    """Arm, same-name control and cross control nets; the information variants only when `full`."""
    o = {"arm": S.net(sipm, m), "ctrl": S.net(sipm, c) if c is not None else None,
         "cross": S.net(xm, m) if xm is not None else None}
    if not full:
        return o
    o.update({"arm_gross": S.net(sipm, m, cost=0.0), "ctrl_gross": S.net(sipm, c, cost=0.0) if c is not None else None,
              "arm_m2": S.net(sipm, m, lag=2), "arm_m0": S.net(sipm, m, lag=0)})
    for hld in S.INFO_HOLDS:
        o[f"arm_h{hld}"] = S.net(sipm, m, hold=hld)
        c2 = control_minute(sym, day, m, sipm, hold=hld)
        o[f"ctrl_h{hld}"] = S.net(sipm, c2, hold=hld) if c2 is not None else None
    c3 = control_minute(sym, day, m, sipm, lo=11)
    o["ctrl_nooverlap"] = S.net(sipm, c3) if c3 is not None else None
    return o


# ── V setups ─────────────────────────────────────────────────────────────────

class Session60:
    """Per-name 60-minute bars, low and last close of one session (cached across the days that use it)."""

    def __init__(self, day: str, r: np.ndarray):
        b1 = BS.minute_bars(bs_rows(day, r), day)
        self.b60 = BS.aggregate(b1, day, 60)
        self.low = min(b.l for b in b1) if b1 else None
        self.close = b1[-1].c if b1 else None


def session60(src: str, day: str, sym: str, r: np.ndarray) -> Session60:
    if (src, day, sym) not in _S60:
        _S60[(src, day, sym)] = Session60(day, r)
    return _S60[(src, day, sym)]


def v_context(sym: str, day: str, today: np.ndarray, prior: list[tuple[str, Session60]]):
    """structure_pullback.Ctx on warehouse bars (prereg V adaptation)."""
    b1 = BS.minute_bars(bs_rows(day, today), day)
    series = BS.concat_sessions([s.b60 for _d, s in prior] + [BS.aggregate(b1, day, 60)])
    ends = [b.end for b in b1]

    def entry(t):
        # (a) the R-bound price is the SIP close of the 1-minute bar ending at t (last completed bar by t); (b) no
        # stale-quote rule.
        k = np.searchsorted(ends, t, side="right") - 1
        return (b1[k].c, 0.0) if k >= 0 else None
    _prev_day, prev = prior[-1]
    return SP.Ctx(sym, day, b1, series, [d for d, _s in prior[-5:]], prev.low, prev.close, entry)


def v_entries(ctx, counts: Counter) -> list[tuple[str, int, dict]]:
    """(setup, decision minute m = ET minute of the setup bar's end, signal) for each setup's first entry."""
    out = []
    for s in V_SETUPS:
        sig = SP.SCANNERS[s](ctx, counts)
        if sig and not sig.get("stale"):
            out.append((s, BS.et_hm(sig["t"]), sig))
    return out


# ── one session ──────────────────────────────────────────────────────────────

def process_day(args) -> dict:
    """One scored session. mode: count (control outcomes only), placebo (PRE outcomes, no V), handcheck (V signals
    with bars, no outcomes), score / null (everything)."""
    k, days, src, mode = args
    day, prev = days[k], days[k - 1]
    prior_days = days[k - LOOKBACK:k]
    keep = set(prior_days) | {day}
    full = mode == "score"
    counts: Counter = Counter()
    sip = get_raw(src, "sip", day, keep)
    iex = get_raw(src, "iex", day, keep)
    p_iex, p_sip = get_raw(src, "iex", prev, keep), get_raw(src, "sip", prev, keep)
    sipm_cache: dict[str, dict] = {}

    def sipm(n):
        if n not in sipm_cache:
            sipm_cache[n] = sip_oc(sip[n]) if n in sip else {}
        return sipm_cache[n]

    # V: every name with today's SIP bars and all 10 prior SIP sessions
    v_rows, v_by_sym = [], defaultdict(list)
    if mode != "placebo":
        prior_raw = [(d, get_raw(src, "sip", d, keep)) for d in prior_days]
        v_pool = [s for s in sorted(sip) if all(s in r for _d, r in prior_raw)]
        counts["v_short_lookback"] += len(sip) - len(v_pool)
        for sym in v_pool:
            counts["v_namedays"] += 1
            ctx = v_context(sym, day, sip[sym], [(d, session60(src, d, sym, r[sym])) for d, r in prior_raw])
            for setup, m, sig in v_entries(ctx, counts):
                v_by_sym[sym].append((setup, m))
                if mode == "handcheck":      # light rows; bars are rebuilt for the picked entries only
                    v_rows.append({"day": day, "sym": sym, "m": m, "setup": setup,
                                   "sig": {k_: v_ for k_, v_ in sig.items() if isinstance(v_, (int, float, str))}})
                    continue
                c = control_minute(sym, day, m, sipm(sym))
                if c is None:
                    counts[f"{setup}_no_control"] += 1
                    continue
                row = {"day": day, "sym": sym, "m": m, "setup": setup}
                if mode == "count":
                    row["ctrl"] = S.net(sipm(sym), c)
                else:
                    x = cross_name(sym, day, m, v_pool, sipm)
                    row["o"] = outcomes(sipm(sym), m, c, sipm(x) if x else None, sym, day, full)
                v_rows.append(row)
    if mode == "handcheck":
        return {"day": day, "pre": [], "v": v_rows, "counts": counts, "sample": 0, "base": {}}

    # PRE on the 400-name sample
    pre_rows = []
    names = sample_names(day, [s for s in iex if s in sip and s in p_iex])
    E.WARM_LINES = True
    base = {c: {"IN": [0.0, 0], "OUT": [0.0, 0]} for c in CELLS}
    for sym in names:
        ser = E.name_day_series(frame(day, iex[sym]), frame(prev, p_iex[sym][-E.CON_PRIOR_BARS:]))
        ind = S.indicators(bars_dict(iex[sym]))
        ind_s = S.indicators(bars_dict(sip[sym])) if full else None
        prev_close = float(p_sip[sym][-1, 4]) if (full and sym in p_sip) else None
        for m in pre_minutes(ser):
            counts["pre_onsets"] += 1
            c = control_minute(sym, day, m, sipm(sym))
            if c is None:
                counts["pre_no_control"] += 1
                continue
            f = S.features_at(ind, m)
            if not f["ok"]:
                counts["pre_thin_bars"] += 1
            row = {"day": day, "sym": sym, "m": m, "c": c,
                   "sides": {cell: S.cell_side(f, cell, m) for cell in CELLS}}
            if mode == "count":
                row["ctrl"] = S.net(sipm(sym), c)
            else:
                x = cross_name(sym, day, m, names, sipm)
                row["o"] = outcomes(sipm(sym), m, c, sipm(x) if x else None, sym, day, full)
            if full:
                px = S.price_at(bars_dict(sip[sym]), m)
                row.update({"f": f, "f_sip": S.features_at(ind_s, m),
                            "adx5": S.adx5_at(bars_dict(iex[sym]), m),
                            "board_like": (px / prev_close - 1 >= BOARD_UP) if (px and prev_close) else None,
                            "early": m < TIME_SPLIT,
                            "v_recent": sorted({s_ for s_, mv in v_by_sym.get(sym, []) if m - 15 <= mv <= m - 1})})
            pre_rows.append(row)
        if full:
            # information: H1-H5 as filters on every 5th RTH minute (10:00-15:30) of the sample, accumulated per day
            sm = sipm(sym)
            for m in range(S.ARM_START, S.ARM_END + 1, 5):
                c = control_minute(sym, day, m, sm)
                a = S.net(sm, m)
                if c is None or a is None:
                    continue
                cn = S.net(sm, c)
                if cn is None:
                    continue
                f = S.features_at(ind, m)
                for cell in CELLS:
                    side = S.cell_side(f, cell, m)
                    if side:
                        base[cell][side][0] += a - cn
                        base[cell][side][1] += 1
    return {"day": day, "pre": pre_rows, "v": v_rows, "counts": counts, "sample": len(names), "base": base}


def run_days(days: list[str], ks: list[int], src: str, mode: str, workers: int, quiet: bool = False) -> dict:
    pre, v, counts, per_day, base = [], [], Counter(), {}, {}
    chunk = max(1, math.ceil(len(ks) / max(workers, 1)))   # contiguous blocks keep each worker's look-back cache warm
    tasks = [(k, days, src, mode) for k in ks]
    if workers <= 1:
        results = map(process_day, tasks)
        ex = None
    else:
        ex = ProcessPoolExecutor(max_workers=workers)
        results = ex.map(process_day, tasks, chunksize=chunk)
    try:
        for res in results:
            pre += res["pre"]
            v += res["v"]
            counts.update(res["counts"])
            base[res["day"]] = res["base"]
            per_day[res["day"]] = {"sample": res["sample"], "pre": len(res["pre"]), "v": len(res["v"])}
            if not quiet:
                print(f"  {res['day']}: sample {res['sample']}, PRE {len(res['pre'])}, V {len(res['v'])}", flush=True)
    finally:
        if ex is not None:
            ex.shutdown()
    return {"pre": pre, "v": v, "counts": counts, "per_day": per_day, "base": base}


# ── count step ───────────────────────────────────────────────────────────────

def _day_t(values: dict[str, float]) -> tuple[float | None, float | None, int]:
    xs = list(values.values())
    if len(xs) < 2:
        return (xs[0] if xs else None), None, len(xs)
    mu, se = statistics.mean(xs), statistics.stdev(xs) / math.sqrt(len(xs))
    return mu, (mu / se if se > 0 else None), len(xs)


def count_h(rows: list[dict], half_end: str | None = None) -> dict:
    """Per cell and half: counts, projected MDE = (t_crit + 0.84) x sqrt(2) x the day-clustered SE of per-day
    (IN control mean - OUT control mean), and that control gap's own mean and t."""
    out = {}
    for cell in CELLS:
        out[cell] = {}
        for half in ("A", "B"):
            ctl = {"IN": defaultdict(list), "OUT": defaultdict(list)}
            names = set()
            for r in rows:
                s = r["sides"][cell]
                if half_of(r["day"], half_end) != half or s is None:
                    continue
                if r["ctrl"] is not None:
                    ctl[s][r["day"]].append(r["ctrl"])
                if s == "IN":
                    names.add((r["sym"], r["day"]))
            n_in = sum(len(v) for v in ctl["IN"].values())
            n_out = sum(len(v) for v in ctl["OUT"].values())
            days = [d for d in ctl["IN"] if len(ctl["IN"][d]) >= MIN_SIDE and len(ctl["OUT"][d]) >= MIN_SIDE]
            gap = {d: statistics.mean(ctl["IN"][d]) - statistics.mean(ctl["OUT"][d]) for d in days}
            g_mu, g_t, nd = _day_t(gap)
            se = statistics.stdev(gap.values()) / math.sqrt(nd) if nd >= 2 else None
            tc = S.t_crit(nd)
            mde = (tc + 0.84) * math.sqrt(2) * se if se is not None and tc is not None else None
            out[cell][half] = {"n_in": n_in, "n_out": n_out, "in_namedays": len(names), "days_both_sides": nd,
                               "t_crit": tc, "projected_mde": mde, "ctrl_gap_in_minus_out": g_mu,
                               "ctrl_gap_t": g_t,
                               "scored": n_in >= MIN_ENTRIES and nd >= MIN_DAYS}
    return out


def count_v(rows: list[dict], half_end: str | None = None) -> dict:
    out = {}
    for setup in V_SETUPS:
        out[setup] = {}
        for half in ("A", "B"):
            ctl = defaultdict(list)
            names = set()
            for r in rows:
                if r["setup"] == setup and half_of(r["day"], half_end) == half and r["ctrl"] is not None:
                    ctl[r["day"]].append(r["ctrl"])
                    names.add((r["sym"], r["day"]))
            means = {d: statistics.mean(v) for d, v in ctl.items()}
            n = sum(len(v) for v in ctl.values())
            se = statistics.stdev(means.values()) / math.sqrt(len(means)) if len(means) >= 2 else None
            tc = S.t_crit(len(means))
            mde = (tc + 0.84) * math.sqrt(2) * se if se is not None and tc is not None else None
            out[setup][half] = {"n": n, "namedays": len(names), "days": len(means), "t_crit": tc,
                                "projected_mde": mde, "scored": n >= MIN_ENTRIES and len(means) >= MIN_DAYS}
    return out


def funnels(counts: Counter) -> dict:
    return {s: {k[len(s) + 1:]: v for k, v in sorted(counts.items()) if k.startswith(s + "_")} for s in V_SETUPS}


# ── grading ──────────────────────────────────────────────────────────────────

def upper_bound(mu: float | None, t: float | None, days: int) -> float | None:
    """One-sided 97.5% upper bound: estimate + t(0.975, days - 1) x SE, with SE = |mu / t|."""
    if mu is None or t is None or t == 0 or days < 2:
        return None
    return mu + S.t_ppf(0.975, days - 1) * abs(mu / t)


def with_rows(recs: list[dict], half: str, half_end: str) -> list[dict]:
    """grade_half reads r['o']; it also needs 'day' and 'sym'."""
    return [r for r in recs if half_of(r["day"], half_end) == half and "o" in r]


def grade_h(recs: list[dict], cell: str, half: str, half_end: str | None = None, side_fn=None) -> dict:
    fn = side_fn or (lambda r: r["sides"][cell])
    rs = with_rows(recs, half, half_end)
    gd, gx = S.grade_half(rs, fn, "net10"), S.grade_half(rs, fn, "cross")
    return {"d": gd, "x": gx, "ub_d": upper_bound(gd["did"], gd["did_t"], gd["days"]),
            "ub_x": upper_bound(gx["did"], gx["did_t"], gx["days"])}


def h_half_verdict(g: dict, cell: str) -> str:
    gd, gx = g["d"], g["x"]
    # prereg power: < 300 IN entries or < 20 days, on d AND on x, is UNDERPOWERED (no OUT floor; skeptic r2 #6)
    if any(g_["n_in"] < MIN_ENTRIES or g_["days"] < MIN_DAYS or g_["did_t"] is None for g_ in (gd, gx)):
        return "UNDERPOWERED"
    if gd["did_t"] <= -2.0 or gx["did_t"] <= -2.0:
        return "FAIL"
    # the history prereg's d bar (not the desk study's half_verdict, which carries the desk floor of 100 OUT arms)
    ok_d = (gd["did"] is not None and gd["did"] >= LIFT_BAR and gd["did_t"] >= gd["t_crit"]
            and gd["lift"] is not None and gd["lift"] >= 0
            and gd["in_net"] is not None and gd["in_net"] > 0
            and gd["did_nameday"] is not None and gd["did_nameday"] > 0
            and gd["did_drop_names"] is not None and gd["did_drop_names"] >= 0
            and gd["did_drop_days"] is not None and gd["did_drop_days"] >= 0
            and (cell != "H2" or (gd["out_lift"] is not None and gd["out_lift"] < 0)))
    ok_x = gx["did"] is not None and gx["did"] >= LIFT_BAR and gx["did_t"] >= gx["t_crit"]
    return "PASS" if ok_d and ok_x else "NOT_PASSED"


def v_grade(recs: list[dict], setup: str, half: str, half_end: str | None = None) -> dict:
    rs = [r for r in recs if r["setup"] == setup and half_of(r["day"], half_end) == half and "o" in r]
    out = {}
    for key, ck in (("d", "ctrl"), ("x", "cross")):
        per = defaultdict(list)
        net_ = defaultdict(list)
        names = defaultdict(list)
        for r in rs:
            a, c = r["o"]["arm"], r["o"][ck]
            if a is None or c is None:
                continue
            per[r["day"]].append(a - c)
            net_[r["day"]].append(a)
            names[r["sym"]].append((r["day"], a - c))
        means = {d: statistics.mean(v) for d, v in per.items()}
        mu, t, nd = _day_t(means)
        g = {"n": sum(len(v) for v in per.values()), "days": nd, "lift": mu, "t": t, "t_crit": S.t_crit(nd),
             "ub": upper_bound(mu, t, nd)}
        if key == "d":
            contrib = Counter({s: sum(dd / len(per[d]) for d, dd in v) for s, v in names.items()})
            top = {s for s, _ in contrib.most_common(3)}
            dn = defaultdict(list)
            for r in rs:
                a, c = r["o"]["arm"], r["o"]["ctrl"]
                if a is not None and c is not None and r["sym"] not in top:
                    dn[r["day"]].append(a - c)
            top_days = set(sorted(means, key=lambda d: means[d], reverse=True)[:3])
            g.update({"net": _day_t({d: statistics.mean(v) for d, v in net_.items()})[0],
                      "lift_nameday": mu,      # one entry per setup per name-day, so name-day-equal = entry-equal
                      "drop_names": _day_t({d: statistics.mean(v) for d, v in dn.items() if v})[0],
                      "drop_days": _day_t({d: v for d, v in means.items() if d not in top_days})[0]})
        out[key] = g
    return out


def v_half_verdict(g: dict) -> str:
    gd, gx = g["d"], g["x"]
    if any(g_["n"] < MIN_ENTRIES or g_["days"] < MIN_DAYS or g_["t"] is None for g_ in (gd, gx)):
        return "UNDERPOWERED"
    if gd["t"] <= -2.0 or gx["t"] <= -2.0:
        return "FAIL"
    ok = (gd["lift"] >= LIFT_BAR and gd["t"] >= gd["t_crit"] and gx["lift"] >= LIFT_BAR and gx["t"] >= gx["t_crit"]
          and gd["net"] is not None and gd["net"] > 0 and gd["lift_nameday"] > 0
          and gd["drop_names"] is not None and gd["drop_names"] >= 0
          and gd["drop_days"] is not None and gd["drop_days"] >= 0)
    return "PASS" if ok else "NOT_PASSED"


def cell_verdict(halves: list[str], bounds: list[tuple]) -> str:
    """Prereg: FAIL if a scored half has t <= -2 on d or x (amended_2: this wins over the d / x disagreement clause,
    e.g. d at the PASS bar with x at t <= -2 is FAIL), or if in BOTH halves the 97.5% upper bound is below +5 bp on
    BOTH d and x; PASS if both halves pass (d and x); otherwise UNDERPOWERED (incl. d / x disagreement)."""
    if "FAIL" in halves:
        return "FAIL"
    if all(h == "PASS" for h in halves):
        return "PASS"
    if all(h != "UNDERPOWERED" for h in halves) and all(
            ud is not None and ux is not None and ud < LIFT_BAR and ux < LIFT_BAR for ud, ux in bounds):
        return "FAIL"
    return "UNDERPOWERED"


def all_t(recs_pre: list[dict], recs_v: list[dict], half_end: str, side_fns: dict | None = None) -> list[dict]:
    """Every H DiD t (d and x) and V lift t (d and x) per half, for the null and the placebo."""
    out = []
    for cell in CELLS:
        for half in ("A", "B"):
            g = grade_h(recs_pre, cell, half, half_end, (side_fns or {}).get(cell))
            out += [_check(cell, half, "DiD_d", g["d"]["did"], g["d"]["did_t"], g["d"]["n_in"], g["d"]["days"]),
                    _check(cell, half, "DiD_x", g["x"]["did"], g["x"]["did_t"], g["x"]["n_in"], g["x"]["days"])]
    for setup in V_SETUPS if recs_v is not None else ():
        for half in ("A", "B"):
            g = v_grade(recs_v, setup, half, half_end)
            out += [_check(setup, half, "lift_d", g["d"]["lift"], g["d"]["t"], g["d"]["n"], g["d"]["days"]),
                    _check(setup, half, "lift_x", g["x"]["lift"], g["x"]["t"], g["x"]["n"], g["x"]["days"])]
    return out


def _check(cell, half, stat, est, t, n, days) -> dict:
    se = abs(est / t) if (est is not None and t) else None
    return {"cell": cell, "half": half, "stat": stat, "t": t, "n_in": n, "days": days,
            "est_bp": None if est is None else est * 1e4, "se_bp": None if se is None else se * 1e4}


def bound_check(checks: list[dict]) -> dict:
    """amended_2: every statistic must exist and rest on >= NULL_MIN_N entries (a blank or thin cell makes the
    calibration NOT OK instead of being skipped); then every |t| within the two-sided Bonferroni bound."""
    ts = [c["t"] for c in checks if c["t"] is not None]
    k = len(ts)
    bound = S.t_ppf(1 - 0.025 / max(k, 1), 10_000)
    missing = [f"{c['cell']} {c['half']} {c['stat']}" for c in checks if c["t"] is None]
    thin = [f"{c['cell']} {c['half']} {c['stat']} n={c['n_in']}" for c in checks if (c["n_in"] or 0) < NULL_MIN_N]
    return {"n_stats": k, "bound": bound, "max_abs_t": max((abs(t) for t in ts), default=None),
            "share_abs_t_le_1": (sum(1 for t in ts if abs(t) <= 1) / k) if k else None,
            "missing": missing, "thin": thin,
            "ok": k > 0 and not missing and not thin and all(abs(t) <= bound for t in ts), "checks": checks}


# ── null, placebo, handcheck ─────────────────────────────────────────────────

def null_days(n: int = NULL_DAYS + LOOKBACK) -> list[str]:
    d0 = datetime.strptime(NULL_START, "%Y-%m-%d")
    return [(d0 + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n)]


def run_null(workers: int) -> dict:
    days = null_days()
    ks = list(range(LOOKBACK, len(days)))
    res = run_days(days, ks, "null", "null", workers, quiet=True)
    half_end = days[LOOKBACK + NULL_DAYS // 2 - 1]
    out = bound_check(all_t(res["pre"], res["v"], half_end))
    out.update({"days": NULL_DAYS, "names": NULL_NAMES, "pre": len(res["pre"]), "v": len(res["v"]),
                "half_end": half_end})
    return out


def coin(r: dict) -> str:
    return "IN" if random.Random(_seed(r["sym"], r["day"], r["m"], "placebo")).random() < 0.5 else "OUT"


def run_placebo(days: list[str], ks: list[int], workers: int) -> dict:
    res = run_days(days, ks, "wh", "placebo", workers, quiet=True)
    fns = {c: (lambda r, c=c: coin(r) if r["sides"][c] is not None else None) for c in CELLS}
    out = bound_check(all_t(res["pre"], None, None, fns))
    out.update({"pre": len(res["pre"])})
    return out


def _bars_md(title: str, bars, t: float, before: int, after: int) -> list[str]:
    k = max((i for i, b in enumerate(bars) if b.end <= t), default=-1)
    lo, hi = max(0, k - before + 1), min(len(bars), k + 1 + after)
    lines = [f"{title} (bar ending at t marked *)", "", "| start ET | o | h | l | c | v |", "|---|---|---|---|---|---|"]
    for i in range(lo, hi):
        b = bars[i]
        mark = "*" if i == k else ""
        lines.append(f"| {datetime.fromtimestamp(b.start, BS.ET):%m-%d %H:%M}{mark} | {b.o:.4f} | {b.h:.4f} | "
                     f"{b.l:.4f} | {b.c:.4f} | {b.v:.0f} |")
    return lines + [""]


def build_ctx(src: str, days: list[str], k: int, sym: str):
    day, prior_days = days[k], days[k - LOOKBACK:k]
    keep = set(prior_days) | {day}
    prior = [(d, session60(src, d, sym, get_raw(src, "sip", d, keep)[sym])) for d in prior_days]
    return v_context(sym, day, get_raw(src, "sip", day, keep)[sym], prior)


def run_handcheck(days: list[str], ks: list[int], workers: int) -> str:
    pick_ks = sorted(random.Random(_seed("handcheck-days")).sample(ks, min(HANDCHECK_DAYS, len(ks))))
    res = run_days(days, pick_ks, "wh", "handcheck", workers, quiet=True)
    L = ["# Hand check: 5 seeded entries per V setup", "",
         "Check each against structure_pullback_prereg.json (setup definitions) and the history prereg's V "
         "adaptation. t = the setup bar's end; decision minute m = t; entry at the SIP open of m + 1.", ""]
    for setup in V_SETUPS:
        rows = sorted((r for r in res["v"] if r["setup"] == setup), key=lambda r: (r["day"], r["sym"], r["m"]))
        pick = random.Random(_seed("handcheck", setup)).sample(rows, min(HANDCHECK_N, len(rows)))
        L += [f"## {setup} ({len(rows)} entries on {', '.join(days[k] for k in pick_ks)})", ""]
        for r in pick:
            t = r["sig"]["t"]
            ctx = build_ctx("wh", days, days.index(r["day"]), r["sym"])
            L += [f"### {setup} {r['sym']} {r['day']} t {datetime.fromtimestamp(t, BS.ET):%H:%M}", "",
                  f"signal: {json.dumps(r['sig'], default=str)}", ""]
            L += _bars_md("60-minute (look-back + today)", ctx.b60, t, 14, 0)
            L += _bars_md("15-minute", ctx.b15, t, 12, 0)
            L += _bars_md("5-minute", ctx.b5, t, 12, 0)
            L += _bars_md("1-minute", ctx.b1, t, 10, 0)
    return "\n".join(L)


# ── score ────────────────────────────────────────────────────────────────────

def _bp(x) -> str:
    return "-" if x is None else f"{x * 1e4:+.1f}"


def _t(x) -> str:
    return "-" if x is None else f"{x:+.2f}"


def base_table(base: dict, half_end: str) -> list[str]:
    """H1-H5 as filters on every 5th RTH minute (accumulated per day): lift and DiD, day-equal."""
    L = ["| cell | half | IN | OUT | days | lift vs random | t | DiD | t |", "|---|---|---|---|---|---|---|---|---|"]
    for cell in CELLS:
        for half in ("A", "B"):
            lift, did, n_in, n_out = {}, {}, 0, 0
            for day, acc in base.items():
                if half_of(day, half_end) != half or not acc:
                    continue
                (si, ni), (so, no) = acc[cell]["IN"], acc[cell]["OUT"]
                n_in += ni
                n_out += no
                if ni:
                    lift[day] = si / ni
                if ni >= MIN_SIDE and no >= MIN_SIDE:
                    did[day] = si / ni - so / no
            lm, lt, _ = _day_t(lift)
            dm, dt, nd = _day_t(did)
            L.append(f"| {cell} | {half} | {n_in} | {n_out} | {nd} | {_bp(lm)} | {_t(lt)} | {_bp(dm)} | {_t(dt)} |")
    return L


def drop_top_namedays(recs: list[dict], cell: str, share: float = 0.01) -> list[dict]:
    """Drop the top `share` of name-days by their additive share of the day-equal DiD (information)."""
    per = {"IN": defaultdict(int), "OUT": defaultdict(int)}
    for r in recs:
        s = r["sides"][cell]
        if s and r["o"]["arm"] is not None and r["o"]["ctrl"] is not None:
            per[s][r["day"]] += 1
    contrib: Counter = Counter()
    for r in recs:
        s = r["sides"][cell]
        if not s or r["o"]["arm"] is None or r["o"]["ctrl"] is None:
            continue
        d = r["o"]["arm"] - r["o"]["ctrl"]
        w = d / per[s][r["day"]]
        contrib[(r["sym"], r["day"])] += w if s == "IN" else -w
    n = max(1, int(len(contrib) * share))
    top = {k for k, _ in contrib.most_common(n)}
    return [r for r in recs if (r["sym"], r["day"]) not in top]


def score(res: dict, count: dict) -> dict:
    pre, v = res["pre"], res["v"]
    for r in pre:
        r["half"] = half_of(r["day"])          # the desk study's table() splits on r['half']
    graded, halves, verdicts = {}, {}, {}
    for cell in CELLS:
        graded[cell], halves[cell] = {}, {}
        for half in ("A", "B"):
            g = grade_h(pre, cell, half)
            if not count["h"][cell][half]["scored"]:
                halves[cell][half] = "UNDERPOWERED"
                g = {"masked": True, "n_in": g["d"]["n_in"], "n_out": g["d"]["n_out"], "days": g["d"]["days"]}
            else:
                halves[cell][half] = h_half_verdict(g, cell)
            graded[cell][half] = g
        hv = [halves[cell][h] for h in ("A", "B")]
        bounds = [(graded[cell][h].get("ub_d"), graded[cell][h].get("ub_x")) for h in ("A", "B")]
        verdicts[cell] = cell_verdict(hv, bounds)
    for setup in V_SETUPS:
        graded[setup], halves[setup] = {}, {}
        for half in ("A", "B"):
            g = v_grade(v, setup, half)
            if not count["v"][setup][half]["scored"]:
                halves[setup][half] = "UNDERPOWERED"
                g = {"masked": True, "n": g["d"]["n"], "days": g["d"]["days"]}
            else:
                halves[setup][half] = v_half_verdict(g)
            graded[setup][half] = g
        bounds = [(graded[setup][h].get("d", {}).get("ub"), graded[setup][h].get("x", {}).get("ub"))
                  for h in ("A", "B")]
        verdicts[setup] = cell_verdict([halves[setup][h] for h in ("A", "B")], bounds)
    sc = {c: {h: count["h"][c][h]["scored"] for h in ("A", "B")} for c in CELLS}
    prim = {c: (lambda r, c=c: r["sides"][c]) for c in CELLS}
    info = {"cells (IEX features, net10)": S.table(pre, S.info_cells(pre))}
    for dkey in ("gross10", "net5", "net30", "entry_m2", "entry_m0", "ctrl_m11_m60"):
        info[f"H1-H5, {dkey}"] = S.table(pre, prim, dkey, scored=sc)
    info["H1-H5 on SIP features"] = S.table(pre, {c: (lambda r, c=c: S.cell_side(r.get("f_sip") or {}, c, r["m"]))
                                                  for c in CELLS}, scored=sc)
    info["H1-H5 on every 5th minute (base population)"] = base_table(res["base"], None)
    info["V setups as filters on PRE (setup entry in m-15 .. m-1)"] = S.table(
        pre, {f"V_{s}": (lambda r, s=s: "IN" if s in r["v_recent"] else "OUT") for s in V_SETUPS})
    for label, keep in (("board-like name-days (up >= 2% at m)", lambda r: r.get("board_like") is True),
                        ("before 11:25", lambda r: r["early"]), ("at or after 11:25", lambda r: not r["early"])):
        info[f"H1-H5, {label}"] = S.table([r for r in pre if keep(r)], prim, scored=sc)
    info["H1-H5 after dropping the top 1% of name-days"] = S.table(
        [], {}) + [line for c in CELLS for line in S.table(drop_top_namedays(pre, c), {c: prim[c]}, scored=sc)[2:]]
    in_net3 = {c: {h: (graded[c][h]["d"]["in_net"] + S.COST - 0.0003)
                   if not graded[c][h].get("masked") and graded[c][h]["d"]["in_net"] is not None else None
                   for h in ("A", "B")} for c in CELLS}
    return {"graded": graded, "halves": halves, "verdicts": verdicts, "info": info, "in_net_3bp": in_net3,
            "overlap_share": (sum(1 for r in pre if r["c"] <= r["m"] + S.HOLD) / len(pre)) if pre else 0,
            "not_computed": NOT_COMPUTED}


def report(res: dict, meta: dict) -> str:
    L = ["# Indicator confirmation on history: result", "",
         "Prereg: docs/studies/indicator_confirm_hist_prereg.json. d = vs the same name's later minute, x = vs a "
         "random other name at the same minute; net10 at 10 bp; bp per trade.", "",
         f"Sessions {meta['sessions'][0]} .. {meta['sessions'][-1]} ({len(meta['sessions'])}), half A <= {HALF_A_END}. "
         f"Counts: {meta['counts']}", "", "## H1-H5 (filters on PRE entries)", "",
         "| cell | half | IN | OUT | days | DiD d | t | DiD x | t | t_crit | 97.5% UB d / x | lift d | OUT lift d | "
         "IN net | name-day DiD | drop 3 names / days | half |", "|" + "---|" * 17]
    for cell in CELLS:
        for half in ("A", "B"):
            g = res["graded"][cell][half]
            if g.get("masked"):
                L.append(f"| {cell} | {half} | {g['n_in']} | {g['n_out']} | {g['days']} | not scored |" + " |" * 11
                         + f" {res['halves'][cell][half]} |")
                continue
            d, x = g["d"], g["x"]
            L.append(f"| {cell} | {half} | {d['n_in']} | {d['n_out']} | {d['days']} | {_bp(d['did'])} | "
                     f"{_t(d['did_t'])} | {_bp(x['did'])} | {_t(x['did_t'])} | {_t(d['t_crit'])} | "
                     f"{_bp(g['ub_d'])} / {_bp(g['ub_x'])} | {_bp(d['lift'])} | {_bp(d['out_lift'])} | "
                     f"{_bp(d['in_net'])} | {_bp(d['did_nameday'])} | {_bp(d['did_drop_names'])} / "
                     f"{_bp(d['did_drop_days'])} | {res['halves'][cell][half]} |")
    L += ["", "## V setups (standalone entries)", "",
          "| setup | half | n | days | lift d | t | lift x | t | t_crit | 97.5% UB d / x | net | drop 3 names / days | half |",
          "|" + "---|" * 13]
    for setup in V_SETUPS:
        for half in ("A", "B"):
            g = res["graded"][setup][half]
            if g.get("masked"):
                L.append(f"| {setup} | {half} | {g['n']} | {g['days']} | not scored |" + " |" * 8
                         + f" {res['halves'][setup][half]} |")
                continue
            d, x = g["d"], g["x"]
            L.append(f"| {setup} | {half} | {d['n']} | {d['days']} | {_bp(d['lift'])} | {_t(d['t'])} | "
                     f"{_bp(x['lift'])} | {_t(x['t'])} | {_t(d['t_crit'])} | {_bp(d['ub'])} / {_bp(x['ub'])} | "
                     f"{_bp(d['net'])} | {_bp(d['drop_names'])} / {_bp(d['drop_days'])} | {res['halves'][setup][half]} |")
    L += ["", "| cell | verdict |", "|---|---|"] + [f"| {c} | **{v}** |" for c, v in res["verdicts"].items()]
    L += ["", "FAIL = a half with t <= -2, or the 97.5% upper bound below +5 bp on d and x in both halves: closed for "
          "effects >= 5 bp. UNDERPOWERED is no evidence either way. t_crit uses df = days - 1.", "",
          f"IN net at a 3 bp round trip: {json.dumps({c: {h: _bp(v) for h, v in hs.items()} for c, hs in res['in_net_3bp'].items()})}",
          "", "## Information only (cannot pass)", ""]
    for title, lines in res["info"].items():
        L += [f"### {title}", ""] + lines + [""]
    L += [f"Same-name controls in m+1 .. m+10 (overlapping the entry's hold): {res['overlap_share']:.1%}", "",
          f"Not computed: {res['not_computed']}"]
    return "\n".join(L)


# ── main ─────────────────────────────────────────────────────────────────────

def script_sha() -> str:
    return hashlib.sha256(open(os.path.abspath(__file__), "rb").read()).hexdigest()


def module_shas() -> dict[str, str]:
    """sha256 of every module whose code shapes the numbers (skeptic r2 #2): a later edit to the desk study or the
    harness invalidates count / null / placebo."""
    import signals
    mods = {"indicator_confirm_hist": sys.modules[__name__], "indicator_confirm_study": S, "early_entry_honest": E,
            "structure_pullback": SP, "bars_structure": BS, "signals": signals}
    return {k: hashlib.sha256(open(os.path.abspath(m.__file__), "rb").read()).hexdigest() for k, m in mods.items()}


def write(name: str, obj) -> str:
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, name)
    with open(path, "w") as f:
        if isinstance(obj, str):
            f.write(obj + "\n")
        else:
            json.dump(obj, f, indent=1, default=str)
    return path


def score_gate(skeptic_ready: str, sessions: list[str]) -> dict:
    if not skeptic_ready:
        raise SystemExit("--score needs --skeptic-ready (the result-skeptic READY on this script)")
    need = {}
    for name in ("count.json", "null.json", "placebo.json"):
        p = os.path.join(OUT_DIR, name)
        if not os.path.exists(p):
            raise SystemExit(f"{name} missing: run its step first")
        obj = json.load(open(p))
        if obj.get("script_sha256") != script_sha() or obj.get("module_sha256") != module_shas():
            raise SystemExit(f"{name} was written by a different script or module version; rerun it with this one")
        if name != "count.json" and not obj.get("ok"):
            raise SystemExit(f"{name} is NOT OK: fix the method before any score")
        need[name] = obj
    if not os.path.exists(os.path.join(OUT_DIR, "handcheck.md")):
        raise SystemExit("handcheck.md missing: run --handcheck and have it reviewed")
    if need["count.json"]["scored_sessions"] != sessions:
        raise SystemExit("the session set changed since the count step; rerun the count step")
    return need["count.json"]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    for flag in ("--null", "--placebo", "--handcheck", "--score"):
        g.add_argument(flag, action="store_true")
    ap.add_argument("--days", type=int, default=0, help="limit to the first N scored sessions (smoke run only)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--skeptic-ready", default="")
    a = ap.parse_args()
    if a.null:
        res = run_null(a.workers)
        res["script_sha256"], res["module_sha256"] = script_sha(), module_shas()
        print(f"null: PRE {res['pre']}, V {res['v']}, {res['n_stats']} t values, max |t| {res['max_abs_t']:.2f} "
              f"(bound {res['bound']:.2f}), share |t|<=1 {res['share_abs_t_le_1']:.0%}, missing {res['missing']}, "
              f"thin {res['thin']}: {'OK' if res['ok'] else 'NOT OK - fix the method before any score'}")
        print(write("null.json", res))
        return
    days = warehouse_days()
    ks = list(range(LOOKBACK, len(days)))
    if a.days:
        ks = ks[:a.days]
    sessions = [days[k] for k in ks]
    print(f"warehouse {len(days)} sessions ({days[0]} .. {days[-1]}); scored {len(ks)}: "
          f"{sessions[0]} .. {sessions[-1]}; half A <= {HALF_A_END}", flush=True)
    if a.placebo:
        res = run_placebo(days, ks, a.workers)
        res["script_sha256"], res["module_sha256"] = script_sha(), module_shas()
        print(f"placebo: PRE {res['pre']}, {res['n_stats']} t values, max |t| {res['max_abs_t']:.2f} "
              f"(bound {res['bound']:.2f}), share |t|<=1 {res['share_abs_t_le_1']:.0%}, missing {res['missing']}, "
              f"thin {res['thin']}: "
              f"{'OK' if res['ok'] else 'NOT OK - fix the method before any score'}")
        print(write("placebo_smoke.json" if a.days else "placebo.json", res))
        return
    if a.handcheck:
        print(write("handcheck.md", run_handcheck(days, ks, a.workers)))
        return
    if a.score:
        count = score_gate(a.skeptic_ready, sessions)
        res = run_days(days, ks, "wh", "score", a.workers, quiet=True)
        out = score(res, count)
        md = report(out, {"sessions": sessions, "counts": dict(res["counts"])})
        print(md)
        write("result.md", md)
        write("result.json", {k: v for k, v in out.items() if k != "info"} | {"skeptic_ready": a.skeptic_ready,
                                                                               "script_sha256": script_sha()})
        return
    res = run_days(days, ks, "wh", "count", a.workers)
    ch, cv, fn = count_h(res["pre"]), count_v(res["v"]), funnels(res["counts"])
    print(f"counts: { {k: n for k, n in res['counts'].items() if k[:2] not in V_SETUPS} }")
    for cell in CELLS:
        for half in ("A", "B"):
            c = ch[cell][half]
            print(f"  {cell} {half}: IN {c['n_in']} OUT {c['n_out']} IN name-days {c['in_namedays']} "
                  f"days {c['days_both_sides']} MDE {_bp(c['projected_mde'])} bp; control gap IN-OUT "
                  f"{_bp(c['ctrl_gap_in_minus_out'])} bp (t {_t(c['ctrl_gap_t'])}) "
                  f"{'scored' if c['scored'] else 'UNDERPOWERED - not scored'}")
    for setup in V_SETUPS:
        for half in ("A", "B"):
            c = cv[setup][half]
            print(f"  {setup} {half}: entries {c['n']} name-days {c['namedays']} days {c['days']} "
                  f"MDE {_bp(c['projected_mde'])} bp {'scored' if c['scored'] else 'UNDERPOWERED - not scored'}")
        print(f"    funnel {fn[setup]}")
    print(write("count_smoke.json" if a.days else "count.json",
                {"script_sha256": script_sha(), "module_sha256": module_shas(), "scored_sessions": sessions, "per_day": res["per_day"],
                 "counts": dict(res["counts"]), "h": ch, "v": cv, "funnels": fn}))
    print("(count step only; no entry outcome read)")


if __name__ == "__main__":
    main()
