#!/usr/bin/env python3
"""The two %R lines and the gap, including where they were. Spec: docs/studies/pr_path_prereg.json.

Every graded minute keeps its own readings. Nothing is a cutoff and nothing is a 'beyond' zone.
The gap is fast minus slow, so it is not stored as its own column. Both lines at each past time
keep the gap and the gap's history. Fit on half A only. Half B is scored once.

  .venv/bin/python tools/studies/pr_path.py            # inventory only
  .venv/bin/python tools/studies/pr_path.py --score    # after a result-skeptic READY
"""
from __future__ import annotations

import argparse
import math
import os
import statistics as st
import sys
from concurrent.futures import ProcessPoolExecutor

import numpy as np

ROOT = os.environ.get("REPO") or os.getcwd()
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))
import early_entry_honest as E  # noqa: E402
import prescreen_turn as P  # noqa: E402

SCALE = 100.0
LAGS = (3, 15, 30)
FILL_AHEAD = 14          # flat minutes after a real print; the 15th minute stays absent
SIG_START, SIG_END = 10 * 60, 15 * 60 + 20
CUT = "2026-07-23"
FIRST_DAY, LAST_DAY, N_DAYS = "2026-04-28", "2026-10-15", 119
COST3 = 0.0003           # information only. The grade uses E.COST (10 bp).
QUANTILE = 0.80
MIN_DAYS, MIN_MINUTES, T_BAR = 30, 1000, 1.5
# fast and slow at m, m-3, m-15, m-30, and the prior session. Gap = fast - slow at that time.
N_FEAT = 14              # 10 line readings + 3 squares + the 3-minute together product

FEAT_NAMES = (
    "fast0", "slow0",
    "fast3", "slow3",
    "fast15", "slow15",
    "fast30", "slow30",
    "fastP", "slowP",
    "fast0sq", "slow0sq", "gap0sq",
    "together3",
)

# Pictures only. Not fit columns. Each pair is (column, sign) on the scaled line readings.
GAP_NOW = ((0, 1.0), (1, -1.0))
FAST_15 = ((0, 1.0), (4, -1.0))
SLOW_15 = ((1, 1.0), (5, -1.0))
GAP_15 = ((0, 1.0), (1, -1.0), (4, -1.0), (5, 1.0))
GAP_PRIOR = ((0, 1.0), (1, -1.0), (8, -1.0), (9, 1.0))


def _finite(*xs) -> bool:
    return all(isinstance(v, (int, float, np.floating)) and math.isfinite(float(v)) for v in xs)


def features(at: dict, prev: tuple, m: int) -> list[float] | None:
    """Scaled feature row known at the close of minute m, or None if any required reading is missing.

    `at` maps a clock minute to (fast, slow). Lags are clock minutes on today's causal grid.
    `prev` is (fast, slow) at the previous session's last causal-grid minute. The gap at a time
    is fast minus slow there. It is not given its own column: that column would be an exact copy
    of a difference the two lines already carry, and the fit would have no unique answer.
    """
    need = (m, m - LAGS[0], m - LAGS[1], m - LAGS[2])
    if any(k not in at for k in need) or prev is None or not _finite(*prev):
        return None
    pairs: list[tuple[float, float]] = []
    for k in need:
        fast, slow = at[k]
        if not _finite(fast, slow):
            return None
        pairs.append((float(fast), float(slow)))
    pairs.append((float(prev[0]), float(prev[1])))
    sc = [(f / SCALE, s / SCALE) for f, s in pairs]
    f0, s0 = sc[0]
    f3, s3 = sc[1]
    g0 = f0 - s0
    row: list[float] = []
    for f, s in sc:
        row.extend((f, s))
    row.extend((f0 * f0, s0 * s0, g0 * g0, (f0 - f3) * (s0 - s3)))
    return row if len(row) == N_FEAT else None


def causal_grid(frame):
    """One row per minute that is knowable without the next print.

    Per ET session, after each real print, insert at most FILL_AHEAD flat minutes at that
    print's close. Do this even when the next print is farther away. Do not look at the next
    print to decide the fill. A minute more than FILL_AHEAD after the last real print is absent.
    Flats stop before 16:00. The overnight hole is not filled. A real print replaces a flat.
    """
    import pandas as pd
    t = pd.to_datetime(frame["time"], utc=True).dt.floor("min")
    real: dict[tuple, tuple] = {}
    for ts, o, h, l, c, v in zip(t, frame["open"], frame["high"], frame["low"],
                                 frame["close"], frame["volume"]):
        et = ts.tz_convert(E.ET)
        key = (et.date(), et.hour * 60 + et.minute)
        real[key] = (ts, float(o), float(h), float(l), float(c), float(v))
    by_date: dict = {}
    for (date, minute), row in sorted(real.items(), key=lambda kv: kv[1][0]):
        by_date.setdefault(date, []).append((minute, row))
    out = []
    for rows in by_date.values():
        filled: dict[int, tuple] = {}
        for minute, (ts, o, h, l, c, v) in rows:
            filled[minute] = (ts, o, h, l, c, v, True)
            for k in range(1, FILL_AHEAD + 1):
                fm = minute + k
                if fm >= E.RTH_END:
                    break
                if fm in filled and filled[fm][6]:
                    continue
                filled[fm] = (ts + pd.Timedelta(minutes=k), c, c, c, c, 0.0, False)
        for minute in sorted(filled):
            ts, o, h, l, c, v, _is_real = filled[minute]
            out.append((ts, o, h, l, c, v))
    out.sort(key=lambda r: r[0])
    return pd.DataFrame(out, columns=["time", "open", "high", "low", "close", "volume"])


def line_path(iex_frame, prior_frame):
    """Today's (minute -> (fast, slow)) and the previous session's last (fast, slow).

    The grid is causal_grid. The line function is told not to fill again. The previous-session
    reading is the last minute on that session's causal grid, which is known before the open.
    """
    import pandas as pd
    import signals
    if prior_frame is None or len(prior_frame) == 0 or iex_frame is None or len(iex_frame) == 0:
        return None
    day0 = pd.to_datetime(iex_frame["time"], utc=True).min()
    both = pd.concat([
        prior_frame[["time", "open", "high", "low", "close", "volume"]],
        iex_frame[["time", "open", "high", "low", "close", "volume"]],
    ])
    both = both.assign(time=pd.to_datetime(both["time"], utc=True)).sort_values("time")
    both = both.drop_duplicates("time", keep="last").reset_index(drop=True)
    cfg = dict(E.CFG)
    cfg["rte_minute_grid"] = False
    df = signals.compute_percent_r_exhaustion(causal_grid(both), cfg)
    t = pd.to_datetime(df["time"], utc=True)
    pre, today = df[t < day0], df[t >= day0]
    if pre.empty or today.empty:
        return None
    last = pre.iloc[-1]
    prev = (float(last["s_percentR"]), float(last["l_percentR"]))
    if not _finite(*prev):
        return None
    at = {}
    for ts, fast, slow in zip(t[t >= day0], today["s_percentR"], today["l_percentR"]):
        at[E._et_min(ts.timestamp())] = (float(fast), float(slow))
    return at, prev


def weighted_quantile(values: np.ndarray, weights: np.ndarray, q: float) -> float:
    """Smallest value at which the day-equal weight covered is at least q of the total."""
    order = np.argsort(values, kind="mergesort")
    v, w = values[order], weights[order]
    cw = np.cumsum(w)
    idx = int(np.searchsorted(cw, q * float(cw[-1]), side="left"))
    return float(v[min(idx, len(v) - 1)])


def fit_beta(sxx: np.ndarray, sxy: np.ndarray) -> np.ndarray | None:
    """Solve the weighted normal equations. Rank deficient -> None (do not drop a column)."""
    rank = np.linalg.matrix_rank(sxx)
    if rank < sxx.shape[0]:
        return None
    try:
        beta = np.linalg.solve(sxx, sxy)
    except np.linalg.LinAlgError:
        return None
    return beta if np.all(np.isfinite(beta)) else None


def day_t(vals: list[float]) -> tuple[float | None, float | None, int]:
    if not vals:
        return None, None, 0
    mu = st.fmean(vals)
    if len(vals) < 3 or st.stdev(vals) == 0:
        return mu, None, len(vals)
    return mu, mu / (st.stdev(vals) / math.sqrt(len(vals))), len(vals)


def grade(net10, t_net, excess, t_ex, cross, t_cross,
          days: int, excess_days: int, cross_days: int, n: int,
          singular: bool, fail_share: float) -> str:
    """PROMISING, FAIL, or UNDERPOWERED. net10 is already the slice mean minus 10 bp.

    This function does not read the 3 bp figure. Three clauses, one grade: the slice clears
    10 bp, it beats the same day's complement, and it beats other names at the same minute.
    """
    if (singular or fail_share > 0.01 or days < MIN_DAYS or excess_days < MIN_DAYS
            or cross_days < MIN_DAYS or n < MIN_MINUTES):
        return "UNDERPOWERED"
    if t_net is None or t_ex is None or t_cross is None:
        return "UNDERPOWERED"
    if (net10 > 0 and t_net >= T_BAR and excess > 0 and t_ex >= T_BAR
            and cross > 0 and t_cross >= T_BAR):
        return "PROMISING"
    return "FAIL"


def slice_day(y: np.ndarray, take: np.ndarray) -> tuple[float | None, float | None]:
    """(mean of the slice, mean of the slice minus mean of the complement).

    The excess is None when the slice is empty or the complement is empty. An empty
    complement is left out of the excess t. The excess is not versus all minutes.
    """
    if len(y) == 0 or not take.any():
        return None, None
    mu = float(y[take].mean())
    comp = ~take
    if not comp.any():
        return mu, None
    return mu, mu - float(y[comp].mean())


def cross_day(y: np.ndarray, minute: np.ndarray, take: np.ndarray) -> float | None:
    """Equal-weight mean, across minutes, of (slice names minus other graded names) at that minute.

    A minute with no slice name or no other graded name is skipped. None when no minute has both.
    """
    if len(y) == 0 or not take.any():
        return None
    excesses = []
    for m in np.unique(minute):
        sel = minute == m
        inn, out = sel & take, sel & ~take
        if not inn.any() or not out.any():
            continue
        excesses.append(float(y[inn].mean()) - float(y[out].mean()))
    if not excesses:
        return None
    return st.fmean(excesses)


def bin_lo(x: float, lo: float, hi: float, step: float) -> float | None:
    """Left edge of the closed step that contains x, or None when x is outside [lo, hi]."""
    if not math.isfinite(x) or x < lo or x > hi:
        return None
    if x == hi:
        return hi - step
    return lo + step * math.floor((x - lo) / step)


def _accumulate(sxx, sxy, X, y, w):
    xw = X * w[:, None]
    sxx += X.T @ xw
    sxy += X.T @ (w * y)


def _design(Xfeat: np.ndarray) -> np.ndarray:
    return np.column_stack([np.ones(len(Xfeat)), Xfeat])


def _linear(X: np.ndarray, terms: tuple) -> np.ndarray:
    """Raw points of a signed sum of scaled columns. Gap and gap history are sums like this."""
    x = np.zeros(len(X), dtype=np.float64)
    for col, sign in terms:
        x += sign * X[:, col]
    return x * SCALE


def day_job(args) -> tuple[str, dict]:
    day, prev, cache = args
    E.WARM_LINES = True
    lz = P.LazyDay(day, prev)
    iex, sip_f, prior = lz["iex"], lz["sip"], lz["iex_prior"]
    sips = {s: E.sip_map(f) for s, f in sip_f.items()}
    ys: list[float] = []
    xs: list[list[float]] = []
    minutes: list[int] = []
    syms: list[str] = []
    cnt = {"sampled": len(lz["board"]), "skipped_short": 0, "indicator_fail": 0, "no_history": 0}
    for sym in sorted(lz["board"]):
        if sym not in iex or sym not in sips or len(iex[sym]) < 30:
            cnt["skipped_short"] += 1
            continue
        try:
            got = line_path(iex[sym], prior.get(sym))
        except Exception:  # noqa: BLE001
            cnt["indicator_fail"] += 1
            continue
        if got is None:
            cnt["indicator_fail"] += 1
            continue
        at, prev_line = got
        sip = sips[sym]
        for m in range(SIG_START, SIG_END + 1):
            row = features(at, prev_line, m)
            if row is None:
                cnt["no_history"] += 1
                continue
            g = E.markout(sip, m + 1)
            if g is None:
                continue
            ys.append(g)
            xs.append(row)
            minutes.append(m)
            syms.append(sym)
    y = np.asarray(ys, np.float32)
    X = np.asarray(xs, np.float32).reshape(-1, N_FEAT) if xs else np.zeros((0, N_FEAT), np.float32)
    os.makedirs(cache, exist_ok=True)
    sym_arr = np.asarray(syms) if syms else np.asarray([], dtype="U1")
    np.savez(os.path.join(cache, f"{day}.npz"), y=y, X=X,
             minute=np.asarray(minutes, dtype=np.int16), sym=sym_arr)
    cnt["minutes"] = int(len(y))
    return day, cnt


def _load(cache: str, day: str):
    z = np.load(os.path.join(cache, f"{day}.npz"))
    return (z["y"].astype(np.float64), z["X"].astype(np.float64),
            z["minute"].astype(np.int16), z["sym"])


def _normal(days: list[str], cache: str) -> tuple[np.ndarray, np.ndarray]:
    k = N_FEAT + 1
    sxx, sxy = np.zeros((k, k)), np.zeros(k)
    for d in days:
        y, X, _minute, _sym = _load(cache, d)
        if len(y) == 0:
            continue
        w = np.full(len(y), 1.0 / len(y))
        _accumulate(sxx, sxy, _design(X), y, w)
    return sxx, sxy


def _scores(days: list[str], cache: str, beta: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    ss, ws = [], []
    for d in days:
        y, X, _minute, _sym = _load(cache, d)
        if len(y) == 0:
            continue
        s = _design(X) @ beta
        ss.append(s)
        ws.append(np.full(len(s), 1.0 / len(s)))
    if not ss:
        return np.zeros(0), np.zeros(0)
    return np.concatenate(ss), np.concatenate(ws)


def _empty_slice() -> dict:
    return {"mean": None, "t": None, "excess": None, "t_ex": None, "days": 0, "n": 0,
            "excess_days": 0, "cross": None, "t_cross": None, "cross_days": 0,
            "net10": None, "net3": None}


def _slice(days: list[str], cache: str, beta: np.ndarray, cutoff: float) -> dict:
    hi, excess, cross, n = [], [], [], 0
    for d in days:
        y, X, minute, _sym = _load(cache, d)
        if len(y) == 0:
            continue
        take = _design(X) @ beta >= cutoff
        mu, ex = slice_day(y, take)
        if mu is None:
            continue
        hi.append(mu)
        n += int(take.sum())
        if ex is not None:
            excess.append(ex)
        cx = cross_day(y, minute, take)
        if cx is not None:
            cross.append(cx)
    mu, t, nd = day_t(hi)
    eu, et, ed = day_t(excess)
    cu, ct, cd = day_t(cross)
    return {"mean": mu, "t": t, "excess": eu, "t_ex": et, "days": nd, "n": n,
            "excess_days": ed, "cross": cu, "t_cross": ct, "cross_days": cd,
            "net10": None if mu is None else mu - E.COST,
            "net3": None if mu is None else mu - COST3}


def _slope_on(days: list[str], cache: str, values_of) -> tuple[float | None, float | None]:
    """Day-clustered slope of return on a raw-point reading. values_of(X) -> raw points."""
    slopes = []
    for d in days:
        y, X, _minute, _sym = _load(cache, d)
        if len(y) < 3:
            continue
        x = values_of(X)
        if float(np.std(x)) == 0:
            continue
        xm, ym = float(x.mean()), float(y.mean())
        var = float(((x - xm) ** 2).sum())
        if var == 0:
            continue
        slopes.append(float(((x - xm) * (y - ym)).sum() / var))
    return day_t(slopes)[:2]


def _bins_on(days: list[str], cache: str, values_of, lo: float, hi: float) -> dict:
    """Day-equal mean gross by bin. values_of(X) -> raw points. Outside [lo, hi] is overflow."""
    acc: dict[float, list[float]] = {}
    overflow_n = 0
    for d in days:
        y, X, _minute, _sym = _load(cache, d)
        if len(y) == 0:
            continue
        raw = values_of(X)
        by: dict[float, list[float]] = {}
        for v, r in zip(raw, y):
            edge = bin_lo(float(v), lo, hi, 10.0)
            if edge is None:
                overflow_n += 1
                continue
            by.setdefault(edge, []).append(float(r))
        for edge, rs in by.items():
            acc.setdefault(edge, []).append(st.fmean(rs))
    out = {}
    for edge in sorted(acc):
        mu, t, nd = day_t(acc[edge])
        out[edge] = {"mean": mu, "t": t, "days": nd}
    out["overflow_n"] = overflow_n
    return out


def _bp(x) -> str:
    return "-" if x is None else f"{x * 1e4:+.1f}"


def _tt(x) -> str:
    return "-" if x is None else f"{x:+.2f}"


def report(beta, cutoff, a_slice, b_slice, slopes, bins, cnt, verdict: str) -> str:
    L = ["# %R path (pr_path_prereg)", "",
         "Fit on A only. The grade is the B upper slice after 10 bp, versus the complement "
         "and versus other names at the same minute. The 3 bp line and the bins are information.",
         "",
         f"grade {verdict}",
         f"cutoff {cutoff:.6f} (A weighted 80th percentile of the frozen score)",
         f"B upper days {b_slice['days']} excess_days {b_slice['excess_days']} "
         f"cross_days {b_slice['cross_days']} minutes {b_slice['n']} "
         f"mean_bp {_bp(b_slice['mean'])} net10_bp {_bp(b_slice['net10'])} t {_tt(b_slice['t'])} "
         f"excess_bp {_bp(b_slice['excess'])} excess_t {_tt(b_slice['t_ex'])} "
         f"cross_bp {_bp(b_slice['cross'])} cross_t {_tt(b_slice['t_cross'])}",
         f"B net3_bp {_bp(b_slice['net3'])} (information only, not the grade)",
         f"A upper (in-sample, not the grade) mean_bp {_bp(a_slice['mean'])} "
         f"net10_bp {_bp(a_slice['net10'])} t {_tt(a_slice['t'])} "
         f"excess_bp {_bp(a_slice['excess'])} cross_bp {_bp(a_slice['cross'])}",
         "",
         "Frozen coefficients (intercept first, then features). Gap history is fast minus slow "
         "at each lag, not its own coefficient. Not a second grade.",
         "",
         "| term | coef |",
         "|---|---:|"]
    names = ("intercept",) + FEAT_NAMES
    for name, c in zip(names, beta):
        L.append(f"| {name} | {c:.6g} |")
    L += ["", "Univariate day-clustered slope, bp per raw point. Information only.", "",
          "| reading | A slope (t) | B slope (t) |", "|---|---:|---:|"]
    for name, (sa, sb) in slopes.items():
        L.append(f"| {name} | {_bp(sa[0])} ({_tt(sa[1])}) | {_bp(sb[0])} ({_tt(sb[1])}) |")
    L.append("")
    L.append("Bins, day-equal gross bp. A value outside the stated range is overflow, not folded in. Not graded.")
    for title, table in bins.items():
        L += ["", f"### {title}", "", "| bin | A bp (days) | B bp (days) |", "|---|---:|---:|"]
        edges = sorted(k for k in table["A"] if k != "overflow_n")
        for e in edges:
            aa, bb = table["A"].get(e), table["B"].get(e)
            L.append(f"| {e:.0f} | {_bp(None if aa is None else aa['mean'])} ({0 if aa is None else aa['days']}) | "
                     f"{_bp(None if bb is None else bb['mean'])} ({0 if bb is None else bb['days']}) |")
        L.append(f"overflow minutes A {table['A'].get('overflow_n', 0)} B {table['B'].get('overflow_n', 0)}")
    L += ["",
          f"name-days sampled {cnt['sampled']} skipped_short {cnt['skipped_short']} "
          f"indicator_fail {cnt['indicator_fail']} ({cnt['fail_share']:.2%})",
          f"minutes scored {cnt['minutes']} minutes dropped for a missing lag {cnt['no_history']}"]
    return "\n".join(L)


def _counts(per: dict) -> dict:
    keys = ("sampled", "skipped_short", "indicator_fail", "no_history", "minutes")
    tot = {k: sum(v[k] for v in per.values()) for k in keys}
    tot["fail_share"] = tot["indicator_fail"] / max(1, tot["sampled"])
    return tot


def _reading(col: int | None, terms: tuple | None):
    if col is not None:
        return lambda X, col=col: X[:, col] * SCALE
    return lambda X, terms=terms: _linear(X, terms)


def score(days: list[str], cache: str):
    A = [d for d in days if d <= CUT]
    B = [d for d in days if d > CUT]
    sxx, sxy = _normal(A, cache)
    beta = fit_beta(sxx, sxy)
    singular = beta is None
    if singular:
        beta = np.zeros(N_FEAT + 1)
        cutoff = 0.0
        a_slice = b_slice = _empty_slice()
    else:
        sv, sw = _scores(A, cache, beta)
        cutoff = weighted_quantile(sv, sw, QUANTILE) if len(sv) else 0.0
        a_slice = _slice(A, cache, beta, cutoff)
        b_slice = _slice(B, cache, beta, cutoff)
    readings = (
        ("fast now", 0, None, -100.0, 0.0),
        ("slow now", 1, None, -100.0, 0.0),
        ("gap now", None, GAP_NOW, -100.0, 100.0),
        ("fast minus fast 15m", None, FAST_15, -100.0, 100.0),
        ("slow minus slow 15m", None, SLOW_15, -100.0, 100.0),
        ("gap minus gap 15m", None, GAP_15, -100.0, 100.0),
        ("gap minus gap prior", None, GAP_PRIOR, -100.0, 100.0),
    )
    slopes, bins = {}, {}
    for name, col, terms, lo, hi in readings:
        values_of = _reading(col, terms)
        slopes[name] = (_slope_on(A, cache, values_of), _slope_on(B, cache, values_of))
        bins[name] = {"A": _bins_on(A, cache, values_of, lo, hi),
                      "B": _bins_on(B, cache, values_of, lo, hi)}
    return beta, cutoff, a_slice, b_slice, slopes, bins, singular


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--score", action="store_true")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    lz = [d for d in P.usable_days() if d["day"] <= LAST_DAY]
    days = [d["day"] for d in lz]
    cut = E.split(days)
    print(f"sessions {len(days)} {days[0] if days else '-'}..{days[-1] if days else '-'}; median split {cut}; "
          f"sample {P.SAMPLE}", flush=True)
    if not a.score:
        print("(inventory only; --score after the prereg has a result-skeptic READY)")
        return
    if not (len(days) == N_DAYS and days[0] == FIRST_DAY and days[-1] == LAST_DAY and cut == CUT):
        print(f"REFUSING: window differs from the prereg (n {len(days)}, first {days[:1]}, last {days[-1:]}, cut {cut})")
        return
    import multiprocessing as mp
    mp.set_start_method("fork", force=True)
    cache = os.path.join("/tmp", f"pr_path_{os.getpid()}")
    os.makedirs(cache, exist_ok=True)
    try:
        jobs = [(d["day"], d._prev, cache) for d in lz]
        per = {}
        with ProcessPoolExecutor(a.workers) as ex:
            for k, (day, cnt) in enumerate(ex.map(day_job, jobs)):
                per[day] = cnt
                if k % 10 == 0:
                    print(f"  {day} ({k + 1}/{len(jobs)})", flush=True)
        cnt = _counts(per)
        beta, cutoff, a_slice, b_slice, slopes, bins, singular = score(days, cache)
        verdict = grade(b_slice["net10"], b_slice["t"], b_slice["excess"], b_slice["t_ex"],
                        b_slice["cross"], b_slice["t_cross"],
                        b_slice["days"], b_slice["excess_days"], b_slice["cross_days"],
                        b_slice["n"], singular, cnt["fail_share"])
        md = report(beta, cutoff, a_slice, b_slice, slopes, bins, cnt, verdict)
        print(md, flush=True)
        out = os.path.join(ROOT, "ai_reports", "warehouse", "pr_path_result.md")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w") as f:
            f.write(md + "\n")
    finally:
        for name in os.listdir(cache):
            try:
                os.remove(os.path.join(cache, name))
            except OSError:
                pass
        try:
            os.rmdir(cache)
        except OSError:
            pass


if __name__ == "__main__":
    main()
