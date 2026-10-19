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
EDGE_T = 2.0
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


def describe(a: dict, b: dict) -> str:
    """How the row's run share compares with the base share, per half. No cost.

    Each half is {share, base, drop, drop_base, edge_t, close_edge, days, n}. A wider bar
    raises the share that runs up 30 bp and the share that falls 30 bp together, so HIGHER
    also needs the day-clustered run-minus-drop gap at t >= EDGE_T. A row that only raises
    both, or whose edge is inside noise, is WIDER, not an open. An edge that is gone when
    the entry is the close of m+1 instead of its first print is BOUNCE, not an open."""
    for h in (a, b):
        if (any(h[k] is None for k in ("share", "base", "drop", "drop_base"))
                or h["days"] < MIN_DAYS or h["n"] < MIN_MINUTES):
            return "THIN"
    run = [h["share"] - h["base"] for h in (a, b)]
    drop = [h["drop"] - h["drop_base"] for h in (a, b)]
    if all(r > 0 and r > d and h["edge_t"] is not None and h["edge_t"] >= EDGE_T
           for r, d, h in zip(run, drop, (a, b))):
        if all(h["close_edge"] is not None and h["close_edge"] > 0 for h in (a, b)):
            return "HIGHER"
        return "BOUNCE"
    if all(r > 0 for r in run):
        return "WIDER"
    if all(r < 0 for r in run):
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
    ret1, ret3, rng, y, yc = [], [], [], [], []
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
            late = E.markout_close(sip, m + 1, HOLD)
            yc.append(np.nan if late is None else late)
            ret1.append(r1)
            ret3.append(np.nan if r3 is None else r3)
            rng.append(rg)
            y.append(ret)
    arrs = {k: np.asarray(v, np.float32) for k, v in
            (("ret1", ret1), ("ret3", ret3), ("rng", rng), ("y", y), ("yc", yc))}
    np.savez(os.path.join(cache, f"{day}.npz"), **arrs)
    cnt["minutes"] = int(len(y))
    return day, cnt


def _load(cache: str, day: str):
    z = np.load(os.path.join(cache, f"{day}.npz"))
    return z["ret1"], z["ret3"], z["rng"], z["y"], z["yc"]


def _fit_cutoffs(cache: str, days: list[str]):
    cols = [[], [], []]
    for d in days:
        r1, r3, rg, _y, _yc = _load(cache, d)
        cols[0].append(r1)
        cols[1].append(r3)
        cols[2].append(rg)
    return cutoffs(np.concatenate(cols[0]), np.concatenate(cols[1]), np.concatenate(cols[2]))


def _blank() -> dict:
    return {"mean": None, "n": 0, "n_run": 0, "n_drop": 0, "n_up": 0, "share": None,
            "diff": None, "drop_diff": None, "base_n": 0, "base_run": 0, "base_drop": 0}


def _slice_day(y: np.ndarray, take: np.ndarray, universe: np.ndarray) -> dict:
    """The row's minutes against the base of the minutes it could have been drawn from."""
    take = take & universe
    n, base_n = int(take.sum()), int(universe.sum())
    if n == 0 or base_n == 0:
        return _blank()
    yy, yb = y[take], y[universe]
    n_run, n_drop = int((yy > RUN30).sum()), int((yy < -RUN30).sum())
    base_run, base_drop = int((yb > RUN30).sum()), int((yb < -RUN30).sum())
    return {"mean": float(yy.mean()), "n": n, "n_run": n_run, "n_drop": n_drop,
            "n_up": int((yy > 0).sum()), "share": n_run / n,
            "diff": n_run / n - base_run / base_n, "drop_diff": n_drop / n - base_drop / base_n,
            "base_n": base_n, "base_run": base_run, "base_drop": base_drop}


def _packs(cache: str, day: str, cuts) -> dict:
    r1, r3, rg, y, yc = _load(cache, day)
    y, yc = y.astype(np.float64), yc.astype(np.float64)
    base_n = int(len(y))
    every = np.ones(base_n, bool)
    has3 = np.isfinite(r3)
    masks = {}
    if cuts[0] is not None:
        lo, hi = cuts[0]
        masks["ret1_hi"] = (r1 >= hi, every)
        masks["ret1_lo"] = (r1 <= lo, every)
    if cuts[1] is not None:
        lo, hi = cuts[1]
        masks["ret3_hi"] = (has3 & (r3 >= hi), has3)
        masks["ret3_lo"] = (has3 & (r3 <= lo), has3)
    if cuts[2] is not None:
        lo, hi = cuts[2]
        masks["range_hi"] = (rg >= hi, every)
        masks["range_lo"] = (rg <= lo, every)
    out = {"base": {"n": base_n, "n_run": int((y > RUN30).sum()),
                    "n_drop": int((y < -RUN30).sum()),
                    "mean": float(y.mean()) if base_n else None}}
    late_ok = np.isfinite(yc)
    for name in ROWS:
        got = masks.get(name)
        if got is None or not base_n:
            out[name] = out[name + "@close"] = _blank()
            continue
        take, universe = got
        out[name] = _slice_day(y, take, universe)
        out[name + "@close"] = _slice_day(np.where(late_ok, yc, 0.0), take, universe & late_ok)
    return out


def _collect(per: dict, days: list[str], key: str) -> dict:
    means, diffs, edges = [], [], []
    ns = n_run = n_drop = n_up = bn = brun = bdrop = 0
    for d in days:
        row = per[d][key]
        if row["mean"] is None:
            continue
        means.append(row["mean"])
        diffs.append(row["diff"])
        edges.append(row["diff"] - row["drop_diff"])
        ns += row["n"]
        n_run += row["n_run"]
        n_drop += row["n_drop"]
        n_up += row["n_up"]
        bn += row["base_n"]
        brun += row["base_run"]
        bdrop += row["base_drop"]
    mu, t, nd = R.day_t(means)
    du, dt, _ = R.day_t(diffs)
    eu, et, _ = R.day_t(edges)
    return {"mean": mu, "t": t, "days": nd, "n": ns,
            "share": None if ns == 0 else n_run / ns,
            "drop": None if ns == 0 else n_drop / ns,
            "base": None if bn == 0 else brun / bn,
            "drop_base": None if bn == 0 else bdrop / bn, "base_n": bn,
            "edge": eu, "t_edge": et,
            "up": None if ns == 0 else n_up / ns,
            "diff": du, "t_diff": dt}


def _base(per: dict, days: list[str]) -> dict:
    ns = n_run = n_drop = 0
    means = []
    for d in days:
        b = per[d]["base"]
        if not b["n"]:
            continue
        ns += b["n"]
        n_run += b["n_run"]
        n_drop += b["n_drop"]
        means.append(b["mean"])
    mu, t, nd = R.day_t(means)
    return {"n": ns, "share": None if ns == 0 else n_run / ns,
            "drop": None if ns == 0 else n_drop / ns, "mean": mu, "t": t, "days": nd}


def _counts(per_cnt: dict) -> dict:
    return {k: sum(v[k] for v in per_cnt.values()) for k in ("sampled", "skipped_short", "minutes")}


def _bp(x) -> str:
    return "-" if x is None else f"{x * 1e4:+.1f}"


def _tt(x) -> str:
    return "-" if x is None else f"{x:+.2f}"


def _pct(x) -> str:
    return "-" if x is None else f"{x:.1%}"


def _gap(x, base):
    return None if x is None or base is None else x - base


def _pts(x) -> str:
    return "-" if x is None else f"{x * 100:+.2f} pts"


def _half(row: dict, late: dict) -> dict:
    return {"share": row["share"], "base": row["base"], "drop": row["drop"],
            "drop_base": row["drop_base"], "edge_t": row["t_edge"], "close_edge": late["edge"],
            "days": row["days"], "n": row["n"]}


def _net(x, cost):
    return None if x is None else x - cost


def _half_line(tag: str, r: dict, late: dict) -> list[str]:
    return [
        f"{tag} days {r['days']} n {r['n']} run {_pct(r['share'])} drop {_pct(r['drop'])} up {_pct(r['up'])} "
        f"vs base run {_pct(r['base'])} drop {_pct(r['drop_base'])} n {r['base_n']} "
        f"pooled_gap {_pts(_gap(r['share'], r['base']))} drop_gap {_pts(_gap(r['drop'], r['drop_base']))} "
        f"day_gap {_pts(r['diff'])} day_gap_t {_tt(r['t_diff'])} "
        f"day_edge {_pts(r['edge'])} day_edge_t {_tt(r['t_edge'])} "
        f"gross_bp {_bp(r['mean'])} t {_tt(r['t'])} "
        f"net10_bp {_bp(_net(r['mean'], 0.0010))} net3_bp {_bp(_net(r['mean'], 0.0003))}",
        f"{tag} close-entry days {late['days']} n {late['n']} run {_pct(late['share'])} "
        f"drop {_pct(late['drop'])} day_edge {_pts(late['edge'])} day_edge_t {_tt(late['t_edge'])} "
        f"gross_bp {_bp(late['mean'])}",
    ]


def report(per: dict, days: list[str], cuts) -> str:
    fit = [d for d in days if d <= CUT]
    check = [d for d in days if d > CUT]
    lines = ["# The bar that just closed (pr_run_prereg)", "",
             "A run is the next 3 minutes up more than 30 bp. Each row's base is every scored minute it",
             "could have been drawn from (the ret3 rows: minutes with a close 3 minutes earlier).",
             "The fifths are taken from the earlier sessions only. Fit is through 2026-07-23.",
             "pooled_gap is the run gap; drop_gap is the same gap for a fall of more than 30 bp.",
             "HIGHER needs the run gap above the drop gap, day_edge_t >= 2, on both halves, and a",
             "close-entry day_edge above 0 on both halves. WIDER: runs rose, no clear edge.",
             "BOUNCE: the edge is there at the first print of m+1 and gone at its close.",
             "day_gap is the average of each day's own run gap. day_edge is each day's run gap minus drop gap.",
             "close-entry enters at the close of m+1 and exits at the same close of m+3. Information line.",
             "Bars and returns are SIP. Live reads IEX. A HIGHER needs an IEX-feature replication before use.",
             ""]
    words = []
    blocks = []
    ba, bb = _base(per, fit), _base(per, check)
    for name in ROWS:
        a, b = _collect(per, fit, name), _collect(per, check, name)
        la, lb = _collect(per, fit, name + "@close"), _collect(per, check, name + "@close")
        word = describe(_half(a, la), _half(b, lb))
        words.append(f"{name}: {word}")
        blocks += [f"### {TITLES[name]}", "", word, *_half_line("fit", a, la), *_half_line("check", b, lb), ""]
    lines.append(" | ".join(words))
    lines += ["",
              f"all minutes fit run {_pct(ba['share'])} drop {_pct(ba['drop'])} gross_bp {_bp(ba['mean'])} t {_tt(ba['t'])} n {ba['n']}",
              f"all minutes check run {_pct(bb['share'])} drop {_pct(bb['drop'])} gross_bp {_bp(bb['mean'])} t {_tt(bb['t'])} n {bb['n']}",
              ""]
    labels = ("1-minute return", "3-minute return", "bar range")
    for label, cut in zip(labels, cuts):
        if cut is None:
            lines.append(f"cutoff {label}: missing")
        else:
            lines.append(f"cutoff {label}: 20th {_bp(cut[0])} bp, 80th {_bp(cut[1])} bp")
    lines += ["", "net10_bp and net3_bp are gross minus 10 bp and 3 bp. They are printed and are not in the word.",
              "The trail is not simulated.", ""]
    lines.extend(blocks)
    return "\n".join(lines)


def day_minutes_line(cnts: dict) -> str:
    """Per-day minutes, so a thin day is visible next to the word."""
    mins = sorted(v["minutes"] for v in cnts.values())
    if not mins:
        return "minutes per day: none"
    thin = sum(1 for m in mins if m < MIN_MINUTES)
    return (f"minutes per day min {mins[0]} median {mins[len(mins) // 2]} max {mins[-1]}; "
            f"days under {MIN_MINUTES}: {thin}")


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
               f"minutes scored {tot['minutes']}\n{day_minutes_line(cnts)}\n")
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
