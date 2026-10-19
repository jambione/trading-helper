#!/usr/bin/env python3
"""Does the bar that just closed mark a run. Spec: docs/studies/pr_run_prereg.json.

Six rows, each reported on both halves. The cutoff comes from half A only. No %R.

  .venv/bin/python tools/studies/pr_run.py            # inventory only
  .venv/bin/python tools/studies/pr_run.py --score    # after a result-skeptic READY
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
RUN30 = 0.0030
SIG_START, SIG_END = 9 * 60 + 31, 15 * 60 + 56
CUT = "2026-07-23"
FIRST_DAY, LAST_DAY, N_DAYS = "2026-04-28", "2026-10-15", 119
MIN_DAYS, MIN_MINUTES, MIN_CUT = 30, 1000, 1000
LO, HI = 0.20, 0.80
ROWS = ("ret1_hi", "ret1_lo", "ret3_hi", "ret3_lo", "range_hi", "range_lo")
TITLES = {
    "ret1_hi": "Bar that just closed was up, top fifth of the earlier sessions.",
    "ret1_lo": "Bar that just closed was down, bottom fifth of the earlier sessions.",
    "ret3_hi": "Last 3 minutes were up, top fifth of the earlier sessions.",
    "ret3_lo": "Last 3 minutes were down, bottom fifth of the earlier sessions.",
    "range_hi": "Bar that just closed was wide, top fifth of the earlier sessions.",
    "range_lo": "Bar that just closed was quiet, bottom fifth of the earlier sessions.",
}


def _finite(*xs) -> bool:
    return all(math.isfinite(float(v)) for v in xs)


def features(bars: dict, m: int):
    """(1-minute return, 3-minute return or None, range) from bar m and bar m-3 only."""
    b = bars.get(m)
    if b is None:
        return None
    o, h, l, c = b
    if not _finite(o, h, l, c) or o <= 0 or c <= 0 or h < l:
        return None
    ret1 = c / o - 1.0
    rng = (h - l) / c
    prev = bars.get(m - 3)
    ret3 = None
    if prev is not None and _finite(prev[3]) and prev[3] > 0:
        ret3 = c / prev[3] - 1.0
    return ret1, ret3, rng


def cutoffs(ret1, ret3, rng):
    """20th and 80th percentile of each feature. The return is not an argument."""
    def q(x):
        x = np.asarray(x, np.float64)
        x = x[np.isfinite(x)]
        if len(x) < MIN_CUT:
            return None
        lo, hi = np.quantile(x, [LO, HI])
        return float(lo), float(hi)
    return q(ret1), q(ret3), q(rng)


def describe(share_a, base_a, days_a, n_a, share_b, base_b, days_b, n_b) -> str:
    """How the row's run share compares with the base share. No cost."""
    if (share_a is None or share_b is None or base_a is None or base_b is None
            or days_a < MIN_DAYS or days_b < MIN_DAYS or n_a < MIN_MINUTES or n_b < MIN_MINUTES):
        return "THIN"
    da, db = share_a - base_a, share_b - base_b
    if da > 0 and db > 0:
        return "HIGHER"
    if da < 0 and db < 0:
        return "LOWER"
    return "NOT THE SAME"


def _bars(frame) -> dict:
    out = {}
    for t, o, h, l, c in zip(frame["time"], frame["open"], frame["high"], frame["low"], frame["close"]):
        out[E._et_min(t.timestamp())] = (float(o), float(h), float(l), float(c))
    return out


def day_job(args) -> tuple[str, dict]:
    day, prev, cache = args
    lz = P.LazyDay(day, prev)
    cnt = {"sampled": len(lz["board"]), "skipped_short": 0, "minutes": 0}
    ret1, ret3, rng, y = [], [], [], []
    for sym in sorted(lz["board"]):
        frame = lz["sip"].get(sym)
        if frame is None:
            cnt["skipped_short"] += 1
            continue
        bars = _bars(frame)
        sip = {m: (b[0], b[3]) for m, b in bars.items()}
        for m in range(SIG_START, SIG_END + 1):
            got = features(bars, m)
            if got is None:
                continue
            ret = E.markout(sip, m + 1, HOLD)
            if ret is None:
                continue
            r1, r3, rg = got
            ret1.append(r1)
            ret3.append(np.nan if r3 is None else r3)
            rng.append(rg)
            y.append(ret)
    arrs = {k: np.asarray(v, np.float32) for k, v in
            (("ret1", ret1), ("ret3", ret3), ("rng", rng), ("y", y))}
    np.savez(os.path.join(cache, f"{day}.npz"), **arrs)
    cnt["minutes"] = int(len(y))
    return day, cnt


def _load(cache: str, day: str):
    z = np.load(os.path.join(cache, f"{day}.npz"))
    return z["ret1"], z["ret3"], z["rng"], z["y"]


def _fit_cutoffs(cache: str, days: list[str]):
    cols = [[], [], []]
    for d in days:
        r1, r3, rg, _y = _load(cache, d)
        cols[0].append(r1)
        cols[1].append(r3)
        cols[2].append(rg)
    return cutoffs(np.concatenate(cols[0]), np.concatenate(cols[1]), np.concatenate(cols[2]))


def _blank() -> dict:
    return {"mean": None, "n": 0, "n_run": 0, "n_up": 0, "share": None, "base": None, "diff": None}


def _slice_day(y: np.ndarray, take: np.ndarray, base_share: float) -> dict:
    n = int(take.sum()) if len(y) else 0
    if n == 0:
        return _blank()
    yy = y[take]
    n_run = int((yy > RUN30).sum())
    share = n_run / n
    return {"mean": float(yy.mean()), "n": n, "n_run": n_run,
            "n_up": int((yy > 0).sum()), "share": share, "base": base_share,
            "diff": share - base_share}


def _packs(cache: str, day: str, cuts) -> dict:
    r1, r3, rg, y = _load(cache, day)
    y = y.astype(np.float64)
    base_n = int(len(y))
    base_share = float((y > RUN30).mean()) if base_n else None
    base_mean = float(y.mean()) if base_n else None
    masks = {}
    if cuts[0] is not None:
        lo, hi = cuts[0]
        masks["ret1_hi"] = r1 >= hi
        masks["ret1_lo"] = r1 <= lo
    if cuts[1] is not None:
        lo, hi = cuts[1]
        ok = np.isfinite(r3)
        masks["ret3_hi"] = ok & (r3 >= hi)
        masks["ret3_lo"] = ok & (r3 <= lo)
    if cuts[2] is not None:
        lo, hi = cuts[2]
        masks["range_hi"] = rg >= hi
        masks["range_lo"] = rg <= lo
    out = {"base": {"n": base_n, "n_run": int((y > RUN30).sum()) if base_n else 0,
                    "mean": base_mean, "share": base_share}}
    for name in ROWS:
        take = masks.get(name)
        out[name] = _blank() if take is None or base_share is None else _slice_day(y, take, base_share)
    return out


def _collect(per: dict, days: list[str], key: str) -> dict:
    means, diffs = [], []
    ns = n_run = n_up = 0
    for d in days:
        row = per[d][key]
        if row["mean"] is None:
            continue
        means.append(row["mean"])
        diffs.append(row["diff"])
        ns += row["n"]
        n_run += row["n_run"]
        n_up += row["n_up"]
    mu, t, nd = R.day_t(means)
    du, dt, _ = R.day_t(diffs)
    return {"mean": mu, "t": t, "days": nd, "n": ns,
            "share": None if ns == 0 else n_run / ns,
            "up": None if ns == 0 else n_up / ns,
            "diff": du, "t_diff": dt}


def _base(per: dict, days: list[str]) -> dict:
    ns = n_run = 0
    means = []
    for d in days:
        b = per[d]["base"]
        if not b["n"]:
            continue
        ns += b["n"]
        n_run += b["n_run"]
        means.append(b["mean"])
    mu, t, nd = R.day_t(means)
    return {"n": ns, "share": None if ns == 0 else n_run / ns, "mean": mu, "t": t, "days": nd}


def _counts(per_cnt: dict) -> dict:
    return {k: sum(v[k] for v in per_cnt.values()) for k in ("sampled", "skipped_short", "minutes")}


def _bp(x) -> str:
    return "-" if x is None else f"{x * 1e4:+.1f}"


def _tt(x) -> str:
    return "-" if x is None else f"{x:+.2f}"


def _pct(x) -> str:
    return "-" if x is None else f"{x:.1%}"


def _pts(x) -> str:
    return "-" if x is None else f"{x * 100:+.2f} pts"


def report(per: dict, days: list[str], cuts) -> str:
    fit = [d for d in days if d <= CUT]
    check = [d for d in days if d > CUT]
    lines = ["# The bar that just closed (pr_run_prereg)", "",
             "A run is the next 3 minutes up more than 30 bp. The base share is every scored minute.",
             "The fifths are taken from the earlier sessions only. Fit is through 2026-07-23.",
             "pooled_gap is the gap the word uses. day_gap is the average of each day's own gap, and day_gap_t is its t.",
             ""]
    words = []
    blocks = []
    ba, bb = _base(per, fit), _base(per, check)
    for name in ROWS:
        a, b = _collect(per, fit, name), _collect(per, check, name)
        word = describe(a["share"], ba["share"], a["days"], a["n"], b["share"], bb["share"], b["days"], b["n"])
        words.append(f"{name}: {word}")
        blocks += [
            f"### {TITLES[name]}", "", word,
            f"fit days {a['days']} n {a['n']} run {_pct(a['share'])} up {_pct(a['up'])} "
            f"vs base {_pct(ba['share'])} pooled_gap {_pts(None if a['share'] is None or ba['share'] is None else a['share'] - ba['share'])} "
            f"day_gap {_pts(a['diff'])} day_gap_t {_tt(a['t_diff'])} "
            f"gross_bp {_bp(a['mean'])} t {_tt(a['t'])}",
            f"check days {b['days']} n {b['n']} run {_pct(b['share'])} up {_pct(b['up'])} "
            f"vs base {_pct(bb['share'])} pooled_gap {_pts(None if b['share'] is None or bb['share'] is None else b['share'] - bb['share'])} "
            f"day_gap {_pts(b['diff'])} day_gap_t {_tt(b['t_diff'])} "
            f"gross_bp {_bp(b['mean'])} t {_tt(b['t'])}",
            ""]
    lines.append(" | ".join(words))
    lines += ["",
              f"base fit run {_pct(ba['share'])} gross_bp {_bp(ba['mean'])} t {_tt(ba['t'])} n {ba['n']}",
              f"base check run {_pct(bb['share'])} gross_bp {_bp(bb['mean'])} t {_tt(bb['t'])} n {bb['n']}",
              ""]
    labels = ("1-minute return", "3-minute return", "bar range")
    for label, cut in zip(labels, cuts):
        if cut is None:
            lines.append(f"cutoff {label}: missing")
        else:
            lines.append(f"cutoff {label}: 20th {_bp(cut[0])} bp, 80th {_bp(cut[1])} bp")
    lines += ["", "10 bp and 3 bp are not in the word. The trail is not simulated.", ""]
    lines.extend(blocks)
    return "\n".join(lines)


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
    cache = os.path.join("/tmp", f"pr_run_{os.getpid()}")
    os.makedirs(cache, exist_ok=True)
    try:
        jobs = [(d["day"], d._prev, cache) for d in lz]
        cnts = {}
        with ProcessPoolExecutor(a.workers) as ex:
            for k, (day, cnt) in enumerate(ex.map(day_job, jobs)):
                cnts[day] = cnt
                if k % 10 == 0:
                    print(f"  {day} ({k + 1}/{len(jobs)})", flush=True)
        fit = [d for d in days if d <= CUT]
        cuts = _fit_cutoffs(cache, fit)
        per = {d: _packs(cache, d, cuts) for d in days}
        md = report(per, days, cuts)
        tot = _counts(cnts)
        md += (f"\nname-days sampled {tot['sampled']} skipped_short {tot['skipped_short']}\n"
               f"minutes scored {tot['minutes']}\n")
        print(md, flush=True)
        out = os.path.join(ROOT, "ai_reports", "warehouse", "pr_run_result.md")
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
