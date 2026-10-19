#!/usr/bin/env python3
"""Two candles: does the shape of the last two bars mark a run. Spec: docs/studies/pr_two_prereg.json.

The scoring, words, base, and close-entry line are pr_run's, unchanged. Only the rows differ:
two bars in a row, speeding up, slowing down, a turn, and the line through two closes.

  .venv/bin/python tools/studies/pr_two.py            # inventory only
  .venv/bin/python tools/studies/pr_two.py --score    # after a result-skeptic READY
"""
from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ProcessPoolExecutor

import numpy as np

ROOT = os.environ.get("REPO") or os.getcwd()
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))
import early_entry_honest as E  # noqa: E402
import pr_run as R  # noqa: E402
import prescreen_turn as P  # noqa: E402

HOLD, RUN30 = R.HOLD, R.RUN30
SIG_START, SIG_END = 9 * 60 + 32, 15 * 60 + 56
CUT, FIRST_DAY, LAST_DAY, N_DAYS = R.CUT, R.FIRST_DAY, R.LAST_DAY, R.N_DAYS
LO, HI = R.LO, R.HI
ROWS = ("up2", "down2", "accel_up", "accel_down", "slow_up", "slow_down", "turn_up", "turn_down", "line2_hi", "line2_lo")
TITLES = {
    "up2": "Both bars up (each at least the half-A median size).",
    "down2": "Both bars down (each at least the half-A median size).",
    "accel_up": "Both bars up, the last one bigger: speeding up.",
    "accel_down": "Both bars down, the last one bigger: speeding up.",
    "slow_up": "Both bars up, the last one smaller: slowing down.",
    "slow_down": "Both bars down, the last one smaller: slowing down.",
    "turn_up": "A down bar in the bottom fifth, then an up bar in the top fifth.",
    "turn_down": "An up bar in the top fifth, then a down bar in the bottom fifth.",
    "line2_hi": "The line through the last two closes rising, top fifth.",
    "line2_lo": "The line through the last two closes falling, bottom fifth.",
}


def features2(bars: dict, m: int):
    """(bar m return, bar m-1 return, close m / close m-2 - 1 or None) from bars m, m-1, m-2 only."""
    b, p = bars.get(m), bars.get(m - 1)
    if b is None or p is None:
        return None
    for o, h, l, c in (b, p):
        if not R._finite(o, h, l, c) or o <= 0 or c <= 0 or h < l:
            return None
    r, rp = b[3] / b[0] - 1.0, p[3] / p[0] - 1.0
    pp = bars.get(m - 2)
    line2 = None
    if pp is not None and R._finite(pp[3]) and pp[3] > 0:
        line2 = b[3] / pp[3] - 1.0
    return r, rp, line2


def cutoffs2(r, line2):
    """Half-A floor (median |r|), r's 20th/80th, line2's 20th/80th. The return is not an argument."""
    r = np.asarray(r, np.float64)
    r = r[np.isfinite(r)]
    l2 = np.asarray(line2, np.float64)
    l2 = l2[np.isfinite(l2)]
    if len(r) < R.MIN_CUT or len(l2) < R.MIN_CUT:
        return None
    lo, hi = np.quantile(r, [LO, HI])
    llo, lhi = np.quantile(l2, [LO, HI])
    return {"floor": float(np.median(np.abs(r))), "r": (float(lo), float(hi)), "line2": (float(llo), float(lhi))}


def masks(r, rp, l2, cuts) -> dict:
    """{row: (take, universe)}. The two-bar rows' universe is every scored minute (all have bar m-1);
    the line2 rows' universe is minutes with a close two bars back."""
    f = cuts["floor"]
    every = np.ones(len(r), bool)
    up, dn = r >= f, r <= -f
    pup, pdn = rp >= f, rp <= -f
    lo, hi = cuts["r"]
    has2 = np.isfinite(l2)
    llo, lhi = cuts["line2"]
    return {
        "up2": (up & pup, every), "down2": (dn & pdn, every),
        "accel_up": (up & pup & (r > rp), every), "accel_down": (dn & pdn & (r < rp), every),
        "slow_up": (up & pup & (r < rp), every), "slow_down": (dn & pdn & (r > rp), every),
        "turn_up": ((rp <= lo) & (r >= hi), every), "turn_down": ((rp >= hi) & (r <= lo), every),
        "line2_hi": (has2 & (l2 >= lhi), has2), "line2_lo": (has2 & (l2 <= llo), has2),
    }


def day_job(args) -> tuple[str, dict]:
    day, prev, cache = args
    lz = P.LazyDay(day, prev)
    cnt = {"sampled": len(lz["board"]), "skipped_short": 0, "minutes": 0}
    r, rp, l2, y, yc = [], [], [], [], []
    for sym in sorted(lz["board"]):
        frame = lz["sip"].get(sym)
        if frame is None:
            cnt["skipped_short"] += 1
            continue
        bars = R._bars(frame)
        sip = {m: (b[0], b[3]) for m, b in bars.items()}
        for m in range(SIG_START, SIG_END + 1):
            got = features2(bars, m)
            if got is None:
                continue
            ret = E.markout(sip, m + 1, HOLD)
            if ret is None:
                continue
            late = E.markout_close(sip, m + 1, HOLD)
            a, b, c = got
            r.append(a)
            rp.append(b)
            l2.append(np.nan if c is None else c)
            y.append(ret)
            yc.append(np.nan if late is None else late)
    np.savez(os.path.join(cache, f"{day}.npz"), **{k: np.asarray(v, np.float32) for k, v in
             (("r", r), ("rp", rp), ("l2", l2), ("y", y), ("yc", yc))})
    cnt["minutes"] = len(y)
    return day, cnt


def _load(cache, day):
    z = np.load(os.path.join(cache, f"{day}.npz"))
    return z["r"], z["rp"], z["l2"], z["y"], z["yc"]


def _packs(cache, day, cuts) -> dict:
    r, rp, l2, y, yc = _load(cache, day)
    y, yc = y.astype(np.float64), yc.astype(np.float64)
    out = {"base": {"n": int(len(y)), "n_run": int((y > RUN30).sum()), "n_drop": int((y < -RUN30).sum()),
                    "mean": float(y.mean()) if len(y) else None}}
    late_ok = np.isfinite(yc)
    for name, (take, universe) in masks(r, rp, l2, cuts).items():
        out[name] = R._slice_day(y, take, universe)
        out[name + "@close"] = R._slice_day(np.where(late_ok, yc, 0.0), take, universe & late_ok)
    return out


def report(per: dict, days: list[str], cuts) -> str:
    fit = [d for d in days if d <= CUT]
    check = [d for d in days if d > CUT]
    lines = ["# Two candles (pr_two_prereg)", "",
             "Scoring, base, words, and the close-entry line are pr_run's. Fit is through 2026-07-23.",
             f"An up or down bar is at least the half-A median bar size ({R._bp(cuts['floor'])} bp).",
             f"Fifths: bar return {R._bp(cuts['r'][0])} / {R._bp(cuts['r'][1])} bp; "
             f"two-close line {R._bp(cuts['line2'][0])} / {R._bp(cuts['line2'][1])} bp.", ""]
    words, blocks = [], []
    for name in ROWS:
        a, b = R._collect(per, fit, name), R._collect(per, check, name)
        la, lb = R._collect(per, fit, name + "@close"), R._collect(per, check, name + "@close")
        w = R.describe(R._half(a, la), R._half(b, lb))
        words.append(f"{name}: {w}")
        blocks += [f"### {TITLES[name]}", "", w, *R._half_line("fit", a, la), *R._half_line("check", b, lb), ""]
    ba, bb = R._base(per, fit), R._base(per, check)
    lines += [" | ".join(words), "",
              f"all minutes fit run {R._pct(ba['share'])} drop {R._pct(ba['drop'])} n {ba['n']}",
              f"all minutes check run {R._pct(bb['share'])} drop {R._pct(bb['drop'])} n {bb['n']}", ""]
    return "\n".join(lines + blocks)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--score", action="store_true")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    lz = [d for d in P.usable_days() if d["day"] <= LAST_DAY]
    days = [d["day"] for d in lz]
    cut = E.split(days)
    print(f"sessions {len(days)} {days[0] if days else '-'}..{days[-1] if days else '-'}; median split {cut}", flush=True)
    if not a.score:
        print("(inventory only; --score after the prereg has a result-skeptic READY)")
        return
    if not (len(days) == N_DAYS and days[0] == FIRST_DAY and days[-1] == LAST_DAY and cut == CUT):
        print(f"REFUSING: window differs from the prereg (n {len(days)}, cut {cut})")
        return
    import multiprocessing as mp
    mp.set_start_method("fork", force=True)
    cache = os.path.join("/tmp", f"pr_two_{os.getpid()}")
    os.makedirs(cache, exist_ok=True)
    try:
        cnts = {}
        with ProcessPoolExecutor(a.workers) as ex:
            for k, (day, cnt) in enumerate(ex.map(day_job, [(d["day"], d._prev, cache) for d in lz])):
                cnts[day] = cnt
                if k % 10 == 0:
                    print(f"  {day} ({k + 1}/{len(lz)})", flush=True)
        fit = [d for d in days if d <= CUT]
        cols = [_load(cache, d) for d in fit]
        cuts = cutoffs2(np.concatenate([c[0] for c in cols]), np.concatenate([c[2] for c in cols]))
        if cuts is None:
            print("REFUSING: too few half-A minutes for the cutoffs")
            return
        per = {d: _packs(cache, d, cuts) for d in days}
        md = report(per, days, cuts) + "\n" + R.day_minutes_line(cnts) + "\n"
        print(md, flush=True)
        out = os.path.join(ROOT, "ai_reports", "warehouse", "pr_two_result.md")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w") as f:
            f.write(md)
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
