#!/usr/bin/env python3
"""Read both %R lines against the next 3 minutes. Spec: docs/studies/pr_read_prereg.json.

Fit on sessions through 2026-07-23. Check once on the later sessions. The bet is the next
3 minutes, not a daily hold. No hand-picked chart shape.

  .venv/bin/python tools/studies/pr_read.py            # inventory only
  .venv/bin/python tools/studies/pr_read.py --score    # after a result-skeptic READY
"""
from __future__ import annotations

import argparse
import math
import os
import sys
from concurrent.futures import ProcessPoolExecutor

import numpy as np

ROOT = os.environ.get("REPO") or os.getcwd()
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))
import early_entry_honest as E  # noqa: E402
import pr_path as R  # noqa: E402
import prescreen_turn as P  # noqa: E402

HOLD = 3
LAGS = (3, 5, 15, 30)
SCALE = 100.0
QUANTILE = 0.80
LOW_QUANTILE = 0.20
SIG_START, SIG_END = 10 * 60, 15 * 60 + 56
CUT = "2026-07-23"
FIRST_DAY, LAST_DAY, N_DAYS = "2026-04-28", "2026-10-15", 119
T_BAR, MIN_DAYS, MIN_MINUTES = 1.5, 30, 1000
FEAT_NAMES = (
    "fast0", "slow0",
    "fast3", "slow3",
    "fast5", "slow5",
    "fast15", "slow15",
    "fast30", "slow30",
    "fastP", "slowP",
    "gap0sq",
    "together5",
)
N_FEAT = len(FEAT_NAMES)
# Pictures only. (column, sign) on the scaled readings. Not fit columns of their own.
GAP_NOW = ((0, 1.0), (1, -1.0))
FAST_5 = ((0, 1.0), (4, -1.0))
SLOW_5 = ((1, 1.0), (5, -1.0))
GAP_5 = ((0, 1.0), (1, -1.0), (4, -1.0), (5, 1.0))


def _finite(*xs) -> bool:
    return all(isinstance(v, (int, float, np.floating)) and math.isfinite(float(v)) for v in xs)


def features(at: dict, prev: tuple, m: int) -> list[float] | None:
    """Scaled row known at the close of minute m, or None if a lag is missing.

    Gap is fast minus slow. It is not its own linear column. together5 is the product of
    the two 5-minute changes, on the scaled lines.
    """
    need = (m,) + tuple(m - lag for lag in LAGS)
    if any(k not in at for k in need) or prev is None or not _finite(*prev):
        return None
    pairs = []
    for k in need:
        fast, slow = at[k]
        if not _finite(fast, slow):
            return None
        pairs.append((float(fast), float(slow)))
    pairs.append((float(prev[0]), float(prev[1])))
    sc = [(f / SCALE, s / SCALE) for f, s in pairs]
    f0, s0 = sc[0]
    f5, s5 = sc[2]
    g0 = f0 - s0
    row: list[float] = []
    for f, s in sc:
        row.extend((f, s))
    row.extend((g0 * g0, (f0 - f5) * (s0 - s5)))
    return row if len(row) == N_FEAT else None


def grade(gross, t, excess, t_ex, cross, t_cross, days, excess_days, cross_days, n,
          singular: bool, fail_share: float) -> str:
    """DIRECTION, NO DIRECTION, or UNDERPOWERED. These arguments are the held-out slice.

    gross is the raw 3-minute return. This function does not read a cost, a bin, or the fit half.
    """
    if (singular or fail_share > 0.01 or days < MIN_DAYS or excess_days < MIN_DAYS
            or cross_days < MIN_DAYS or n < MIN_MINUTES
            or t is None or t_ex is None or t_cross is None):
        return "UNDERPOWERED"
    if (gross > 0 and t >= T_BAR and excess > 0 and t_ex >= T_BAR
            and cross > 0 and t_cross >= T_BAR):
        return "DIRECTION"
    return "NO DIRECTION"


def day_job(args) -> tuple[str, dict]:
    day, prev, cache = args
    E.WARM_LINES = True
    lz = P.LazyDay(day, prev)
    iex, sip_f, prior = lz["iex"], lz["sip"], lz["iex_prior"]
    sips = {s: E.sip_map(f) for s, f in sip_f.items()}
    ys, xs, minutes, syms = [], [], [], []
    cnt = {"sampled": len(lz["board"]), "skipped_short": 0, "indicator_fail": 0, "no_history": 0}
    for sym in sorted(lz["board"]):
        if sym not in iex or sym not in sips:
            cnt["skipped_short"] += 1
            continue
        try:
            got = R.line_path(iex[sym], prior.get(sym))
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
            g = E.markout(sip, m + 1, HOLD)
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


def _normal(days: list[str], cache: str):
    k = N_FEAT + 1
    sxx, sxy = np.zeros((k, k)), np.zeros(k)
    for d in days:
        y, X, _minute, _sym = _load(cache, d)
        if len(y) == 0:
            continue
        w = np.full(len(y), 1.0 / len(y))
        R._accumulate(sxx, sxy, R._design(X), y, w)
    return sxx, sxy


def _scores(days, cache, beta):
    ss, ws = [], []
    for d in days:
        y, X, _minute, _sym = _load(cache, d)
        if len(y) == 0:
            continue
        ss.append(R._design(X) @ beta)
        ws.append(np.full(len(y), 1.0 / len(y)))
    if not ss:
        return np.zeros(0), np.zeros(0)
    return np.concatenate(ss), np.concatenate(ws)


def _empty_slice() -> dict:
    return {"mean": None, "t": None, "excess": None, "t_ex": None, "days": 0, "n": 0, "n_up": 0,
            "excess_days": 0, "cross": None, "t_cross": None, "cross_days": 0}


def _slice(days, cache, beta, cutoff, *, low: bool) -> dict:
    hi, excess, cross = [], [], []
    n = n_up = 0
    for d in days:
        y, X, minute, _sym = _load(cache, d)
        if len(y) == 0:
            continue
        score = R._design(X) @ beta
        take = score <= cutoff if low else score >= cutoff
        mu, ex = R.slice_day(y, take)
        if mu is None:
            continue
        hi.append(mu)
        n += int(take.sum())
        n_up += int((y[take] > 0).sum())
        if ex is not None:
            excess.append(ex)
        cx = R.cross_day(y, minute, take)
        if cx is not None:
            cross.append(cx)
    mu, t, nd = R.day_t(hi)
    eu, et, ed = R.day_t(excess)
    cu, ct, cd = R.day_t(cross)
    return {"mean": mu, "t": t, "excess": eu, "t_ex": et, "days": nd, "n": n, "n_up": n_up,
            "excess_days": ed, "cross": cu, "t_cross": ct, "cross_days": cd}


def _counts(per: dict) -> dict:
    keys = ("sampled", "skipped_short", "indicator_fail", "no_history", "minutes")
    tot = {k: sum(v[k] for v in per.values()) for k in keys}
    tot["fail_share"] = tot["indicator_fail"] / max(1, tot["sampled"])
    return tot


def _reading(col, terms):
    if col is not None:
        return lambda X, col=col: X[:, col] * SCALE
    return lambda X, terms=terms: R._linear(X, terms)


def score(days: list[str], cache: str):
    A = [d for d in days if d <= CUT]
    B = [d for d in days if d > CUT]
    sxx, sxy = _normal(A, cache)
    beta = R.fit_beta(sxx, sxy)
    singular = beta is None
    if singular:
        beta = np.zeros(N_FEAT + 1)
        hi_cut = lo_cut = 0.0
        a_slice = b_slice = b_low = _empty_slice()
    else:
        sv, sw = _scores(A, cache, beta)
        hi_cut = R.weighted_quantile(sv, sw, QUANTILE) if len(sv) else 0.0
        lo_cut = R.weighted_quantile(sv, sw, LOW_QUANTILE) if len(sv) else 0.0
        a_slice = _slice(A, cache, beta, hi_cut, low=False)
        b_slice = _slice(B, cache, beta, hi_cut, low=False)
        b_low = _slice(B, cache, beta, lo_cut, low=True)
    readings = (
        ("fast now", 0, None, -100.0, 0.0),
        ("slow now", 1, None, -100.0, 0.0),
        ("gap now", None, GAP_NOW, -100.0, 100.0),
        ("fast change 5m", None, FAST_5, -100.0, 100.0),
        ("slow change 5m", None, SLOW_5, -100.0, 100.0),
        ("gap change 5m", None, GAP_5, -100.0, 100.0),
    )
    slopes, bins = {}, {}
    for name, col, terms, lo, hi in readings:
        values_of = _reading(col, terms)
        slopes[name] = (R._slope_on(A, cache, values_of), R._slope_on(B, cache, values_of))
        bins[name] = {"A": R._bins_on(A, cache, values_of, lo, hi),
                      "B": R._bins_on(B, cache, values_of, lo, hi)}
    return beta, hi_cut, lo_cut, a_slice, b_slice, b_low, slopes, bins, singular


def _bp(x) -> str:
    return "-" if x is None else f"{x * 1e4:+.1f}"


def _tt(x) -> str:
    return "-" if x is None else f"{x:+.2f}"


def _rate(row) -> str:
    if not row["n"]:
        return "-"
    return f"{row['n_up'] / row['n']:.1%}"


def report(beta, hi_cut, a_slice, b_slice, b_low, slopes, bins, cnt, verdict: str) -> str:
    net3 = None if b_slice["mean"] is None else b_slice["mean"] - 0.0003
    net10 = None if b_slice["mean"] is None else b_slice["mean"] - E.COST
    L = ["# %R read (pr_read_prereg)", "",
         "The bet is the next 3 minutes inside the session. The score is fit on sessions through 2026-07-23.",
         "The call is the later sessions only: the minutes the fit ranked in its top fifth.",
         "The lines are a picture of price already printed.",
         "",
         f"call {verdict}",
         f"cutoff {hi_cut:.6f} (fit-session weighted 80th percentile, frozen)",
         f"check days {b_slice['days']} excess_days {b_slice['excess_days']} "
         f"cross_days {b_slice['cross_days']} minutes {b_slice['n']} up {_rate(b_slice)} "
         f"gross_bp {_bp(b_slice['mean'])} t {_tt(b_slice['t'])} "
         f"excess_bp {_bp(b_slice['excess'])} excess_t {_tt(b_slice['t_ex'])} "
         f"cross_bp {_bp(b_slice['cross'])} cross_t {_tt(b_slice['t_cross'])}",
         f"check net3_bp {_bp(net3)} net10_bp {_bp(net10)} (printed, not the call)",
         f"fit sessions, top fifth, not the call: gross_bp {_bp(a_slice['mean'])} t {_tt(a_slice['t'])} "
         f"minutes {a_slice['n']}",
         f"check, bottom fifth, picture, not the call: gross_bp {_bp(b_low['mean'])} "
         f"t {_tt(b_low['t'])} minutes {b_low['n']} up {_rate(b_low)}",
         "",
         "Frozen coefficients (intercept first). Gap history is the two lines at each time. Not a second call.",
         "",
         "| term | coef |",
         "|---|---:|"]
    for name, c in zip(("intercept",) + FEAT_NAMES, beta):
        L.append(f"| {name} | {c:.6g} |")
    L += ["", "Slope, bp per point. Picture.", "",
          "| reading | fit sessions (t) | check sessions (t) |", "|---|---:|---:|"]
    for name, (sa, sb) in slopes.items():
        L.append(f"| {name} | {_bp(sa[0])} ({_tt(sa[1])}) | {_bp(sb[0])} ({_tt(sb[1])}) |")
    L.append("")
    L.append("Bins, day-equal gross bp. Outside the stated range is overflow, not folded in. Not the call.")
    for title, table in bins.items():
        L += ["", f"### {title}", "", "| bin | fit bp (days) | check bp (days) |", "|---|---:|---:|"]
        edges = sorted(k for k in table["A"] if k != "overflow_n")
        for e in edges:
            aa, bb = table["A"].get(e), table["B"].get(e)
            L.append(f"| {e:.0f} | {_bp(None if aa is None else aa['mean'])} ({0 if aa is None else aa['days']}) | "
                     f"{_bp(None if bb is None else bb['mean'])} ({0 if bb is None else bb['days']}) |")
        L.append(f"overflow minutes fit {table['A'].get('overflow_n', 0)} check {table['B'].get('overflow_n', 0)}")
    L += ["",
          f"name-days sampled {cnt['sampled']} skipped_short {cnt['skipped_short']} "
          f"indicator_fail {cnt['indicator_fail']} ({cnt['fail_share']:.2%})",
          f"minutes scored {cnt['minutes']} minutes dropped for a missing lag {cnt['no_history']}"]
    return "\n".join(L)


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
    cache = os.path.join("/tmp", f"pr_read_{os.getpid()}")
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
        beta, hi_cut, _lo, a_slice, b_slice, b_low, slopes, bins, singular = score(days, cache)
        verdict = grade(b_slice["mean"], b_slice["t"], b_slice["excess"], b_slice["t_ex"],
                        b_slice["cross"], b_slice["t_cross"],
                        b_slice["days"], b_slice["excess_days"], b_slice["cross_days"],
                        b_slice["n"], singular, cnt["fail_share"])
        md = report(beta, hi_cut, a_slice, b_slice, b_low, slopes, bins, cnt, verdict)
        print(md, flush=True)
        out = os.path.join(ROOT, "ai_reports", "warehouse", "pr_read_result.md")
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
