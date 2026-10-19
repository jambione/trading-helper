#!/usr/bin/env python3
"""Five-minute direction of the two lines and the gap. Spec: docs/studies/pr_dir5_prereg.json.

Read the lines at the close of minute m. Direction is the reading now minus the reading 5 minutes ago.
The return starts at the next minute's open. The call is the next 3 minutes. The next 5 minutes is a picture.

  .venv/bin/python tools/studies/pr_dir5.py            # inventory only
  .venv/bin/python tools/studies/pr_dir5.py --score    # after a result-skeptic READY
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

LAG = 5
HOLDS = (3, 5)
CALL_HOLD = 3
SIG_START, SIG_END = 9 * 60 + 35, 15 * 60 + 50
CUT = "2026-07-23"
FIRST_DAY, LAST_DAY, N_DAYS = "2026-04-28", "2026-10-15", 119
T_BAR, MIN_DAYS, MIN_MINUTES = 1.5, 30, 1000
PRIMARY = (1, 1, 1)
CELLS = (
    (1, 1, 1),
    (-1, -1, -1),
    (1, 1, -1),
    (-1, -1, 1),
    (1, -1, 1),
    (1, -1, -1),
    (-1, 1, 1),
    (-1, 1, -1),
)
CELL_NAME = {
    (1, 1, 1): "both up, gap up",
    (-1, -1, -1): "both down, gap down",
    (1, 1, -1): "both up, gap down",
    (-1, -1, 1): "both down, gap up",
    (1, -1, 1): "fast up, slow down, gap up",
    (1, -1, -1): "fast up, slow down, gap down",
    (-1, 1, 1): "fast down, slow up, gap up",
    (-1, 1, -1): "fast down, slow up, gap down",
}


def _finite(*xs) -> bool:
    return all(math.isfinite(float(v)) for v in xs)


def direction(at: dict, m: int):
    """(fast change, slow change, gap change, gap now) over LAG minutes, or None.

    Gap change equals the fast change minus the slow change. A later minute is not read.
    """
    if m not in at or (m - LAG) not in at:
        return None
    f0, s0 = at[m]
    f5, s5 = at[m - LAG]
    if not _finite(f0, s0, f5, s5):
        return None
    fd, sd = float(f0) - float(f5), float(s0) - float(s5)
    return fd, sd, fd - sd, float(f0) - float(s0)


def sign_of(x: float) -> int:
    if x > 0:
        return 1
    if x < 0:
        return -1
    return 0


def cell_of(fd: float, sd: float, gd: float):
    """The sign cell, or None when any of the three directions is flat."""
    sf, ss, sg = sign_of(fd), sign_of(sd), sign_of(gd)
    if sf == 0 or ss == 0 or sg == 0:
        return None
    return (sf, ss, sg)


def day_slope(x: np.ndarray, y: np.ndarray) -> float | None:
    if len(y) < 3 or float(np.std(x)) == 0:
        return None
    xm, ym = float(x.mean()), float(y.mean())
    var = float(((x - xm) ** 2).sum())
    if var == 0:
        return None
    return float(((x - xm) * (y - ym)).sum() / var)


def call(gross_a, t_a, ex_a, t_ex_a, cross_a, t_cross_a, days_a, n_a,
         gross_b, t_b, ex_b, t_ex_b, cross_b, t_cross_b, days_b, n_b,
         fail_share: float) -> str:
    """CORRELATION, NO CORRELATION, or UNDERPOWERED. Gross returns, no cost inside this function."""
    need = (t_a, t_ex_a, t_cross_a, t_b, t_ex_b, t_cross_b)
    if (fail_share > 0.01 or days_a < MIN_DAYS or days_b < MIN_DAYS
            or n_a < MIN_MINUTES or n_b < MIN_MINUTES or any(t is None for t in need)):
        return "UNDERPOWERED"
    ok = (gross_a > 0 and t_a >= T_BAR and ex_a > 0 and t_ex_a >= T_BAR
          and cross_a > 0 and t_cross_a >= T_BAR
          and gross_b > 0 and t_b >= T_BAR and ex_b > 0 and t_ex_b >= T_BAR
          and cross_b > 0 and t_cross_b >= T_BAR)
    return "CORRELATION" if ok else "NO CORRELATION"


def _empty_cell() -> dict:
    return {"mean": None, "n": 0, "excess": None, "cross": None}


def _day_stats(minute, fd, sd, gd, y) -> dict:
    """One day, one horizon. Cell means, excess, same-minute cross, and three slopes."""
    out = {"base": None if len(y) == 0 else float(y.mean()),
           "slope": {}, "cell": {}}
    out["slope"] = {
        "fast": day_slope(fd, y),
        "slow": day_slope(sd, y),
        "gap": day_slope(gd, y),
    }
    signs = np.array([cell_of(float(a), float(b), float(c)) for a, b, c in zip(fd, sd, gd)], dtype=object)
    for cell in CELLS:
        take = np.array([s == cell for s in signs], dtype=bool)
        mu, ex = R.slice_day(y, take)
        out["cell"][cell] = {
            "mean": mu,
            "n": int(take.sum()) if len(y) else 0,
            "excess": ex,
            "cross": R.cross_day(y, minute, take) if len(y) else None,
        }
    return out


def day_job(args) -> tuple[str, dict]:
    day, prev = args
    E.WARM_LINES = True
    lz = P.LazyDay(day, prev)
    iex, sip_f, prior = lz["iex"], lz["sip"], lz["iex_prior"]
    sips = {s: E.sip_map(f) for s, f in sip_f.items()}
    cnt = {"sampled": len(lz["board"]), "skipped_short": 0, "indicator_fail": 0, "no_history": 0}
    minutes, fds, sds, gds = [], [], [], []
    ys = {h: [] for h in HOLDS}
    for sym in sorted(lz["board"]):
        if sym not in iex or sym not in sips or len(iex[sym]) < 30:
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
        at, _prev_line = got
        sip = sips[sym]
        for m in range(SIG_START, SIG_END + 1):
            d = direction(at, m)
            if d is None:
                cnt["no_history"] += 1
                continue
            rets = {h: E.markout(sip, m + 1, h) for h in HOLDS}
            if any(v is None for v in rets.values()):
                continue
            fd, sd, gd, _gap_now = d
            minutes.append(m)
            fds.append(fd)
            sds.append(sd)
            gds.append(gd)
            for h in HOLDS:
                ys[h].append(rets[h])
    minute = np.asarray(minutes, np.int16)
    fd = np.asarray(fds, np.float64)
    sd = np.asarray(sds, np.float64)
    gd = np.asarray(gds, np.float64)
    holds = {}
    for h in HOLDS:
        holds[h] = _day_stats(minute, fd, sd, gd, np.asarray(ys[h], np.float64))
    cnt["minutes"] = int(len(minute))
    return day, {"cnt": cnt, "holds": holds}


def _collect(per: dict, days: list[str], hold: int, cell: tuple) -> dict:
    means, excess, cross, ns = [], [], [], []
    for d in days:
        row = per[d]["holds"][hold]["cell"][cell]
        if row["mean"] is None:
            continue
        means.append(row["mean"])
        ns.append(row["n"])
        if row["excess"] is not None:
            excess.append(row["excess"])
        if row["cross"] is not None:
            cross.append(row["cross"])
    mu, t, nd = R.day_t(means)
    eu, et, ed = R.day_t(excess)
    cu, ct, cd = R.day_t(cross)
    return {"mean": mu, "t": t, "days": nd, "n": int(sum(ns)),
            "excess": eu, "t_ex": et, "excess_days": ed,
            "cross": cu, "t_cross": ct, "cross_days": cd}


def _slopes(per: dict, days: list[str], hold: int) -> dict:
    out = {}
    for name in ("fast", "slow", "gap"):
        vals = []
        for d in days:
            s = per[d]["holds"][hold]["slope"][name]
            if s is not None:
                vals.append(s)
        mu, t, nd = R.day_t(vals)
        out[name] = {"mean": mu, "t": t, "days": nd}
    return out


def _base(per: dict, days: list[str], hold: int) -> dict:
    vals = [per[d]["holds"][hold]["base"] for d in days if per[d]["holds"][hold]["base"] is not None]
    mu, t, nd = R.day_t(vals)
    return {"mean": mu, "t": t, "days": nd}


def _counts(per: dict) -> dict:
    keys = ("sampled", "skipped_short", "indicator_fail", "no_history", "minutes")
    tot = {k: sum(v["cnt"][k] for v in per.values()) for k in keys}
    tot["fail_share"] = tot["indicator_fail"] / max(1, tot["sampled"])
    return tot


def _bp(x, nd=2) -> str:
    return "-" if x is None else f"{x * 1e4:+.{nd}f}"


def _tt(x) -> str:
    return "-" if x is None else f"{x:+.2f}"


def _line(label: str, row: dict) -> str:
    net3 = None if row["mean"] is None else row["mean"] - 0.0003
    net10 = None if row["mean"] is None else row["mean"] - E.COST
    return (f"{label} days {row['days']} n {row['n']} gross_bp {_bp(row['mean'])} t {_tt(row['t'])} "
            f"excess_bp {_bp(row['excess'])} excess_t {_tt(row['t_ex'])} "
            f"cross_bp {_bp(row['cross'])} cross_t {_tt(row['t_cross'])} "
            f"net3_bp {_bp(net3)} net10_bp {_bp(net10)}")


def report(per: dict, days: list[str], verdict: str) -> str:
    A = [d for d in days if d <= CUT]
    B = [d for d in days if d > CUT]
    L = ["# Five-minute direction (pr_dir5_prereg)", "",
         "Direction is the last 5 minutes. The call is both lines up and the gap up, next 3 minutes, gross.",
         "The lines are a picture of price already printed. A lineup is not a cause.",
         "",
         f"call {verdict}",
         ""]
    for hold, title in ((3, "Next 3 minutes (the call)"), (5, "Next 5 minutes (picture)")):
        L += [f"## {title}", ""]
        base_a, base_b = _base(per, A, hold), _base(per, B, hold)
        L.append(f"all minutes A gross_bp {_bp(base_a['mean'])} t {_tt(base_a['t'])} "
                 f"B gross_bp {_bp(base_b['mean'])} t {_tt(base_b['t'])}")
        slopes_a, slopes_b = _slopes(per, A, hold), _slopes(per, B, hold)
        L += ["", "Slope, bp per point of 5-minute direction. Picture.", "",
              "| direction | A bp/point (t) | B bp/point (t) |", "|---|---:|---:|"]
        for name in ("fast", "slow", "gap"):
            sa, sb = slopes_a[name], slopes_b[name]
            L.append(f"| {name} | {_bp(sa['mean'], 3)} ({_tt(sa['t'])}) | {_bp(sb['mean'], 3)} ({_tt(sb['t'])}) |")
        L += ["", "Sign cells. The first row is the call when the hold is 3 minutes.", ""]
        for cell in CELLS:
            L.append(_line(CELL_NAME[cell] + " A", _collect(per, A, hold, cell)))
            L.append(_line(CELL_NAME[cell] + " B", _collect(per, B, hold, cell)))
            L.append("")
    cnt = _counts(per)
    L.append(f"name-days sampled {cnt['sampled']} skipped_short {cnt['skipped_short']} "
             f"indicator_fail {cnt['indicator_fail']} ({cnt['fail_share']:.2%})")
    L.append(f"minutes scored {cnt['minutes']} minutes dropped for a missing 5-minute reading {cnt['no_history']}")
    return "\n".join(L)


def score(per: dict, days: list[str]) -> str:
    A = [d for d in days if d <= CUT]
    B = [d for d in days if d > CUT]
    a = _collect(per, A, CALL_HOLD, PRIMARY)
    b = _collect(per, B, CALL_HOLD, PRIMARY)
    return call(a["mean"], a["t"], a["excess"], a["t_ex"], a["cross"], a["t_cross"], a["days"], a["n"],
                b["mean"], b["t"], b["excess"], b["t_ex"], b["cross"], b["t_cross"], b["days"], b["n"],
                _counts(per)["fail_share"])


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
    jobs = [(d["day"], d._prev) for d in lz]
    per = {}
    with ProcessPoolExecutor(a.workers) as ex:
        for k, (day, row) in enumerate(ex.map(day_job, jobs)):
            per[day] = row
            if k % 10 == 0:
                print(f"  {day} ({k + 1}/{len(jobs)})", flush=True)
    verdict = score(per, days)
    md = report(per, days, verdict)
    print(md, flush=True)
    out = os.path.join(ROOT, "ai_reports", "warehouse", "pr_dir5_result.md")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        f.write(md + "\n")


if __name__ == "__main__":
    main()
