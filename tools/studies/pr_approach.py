#!/usr/bin/env python3
"""Sooner %R open. Spec: docs/studies/pr_approach_prereg.json.

The call is one cell. At the close of minute m the absolute gap is still wider than 15,
both lines are below 0 and higher than they were 5 minutes ago, and the absolute gap is
smaller than it was 5 minutes ago. The return is the next 3 minutes, net of 10 bp.

  .venv/bin/python tools/studies/pr_approach.py            # inventory only
  .venv/bin/python tools/studies/pr_approach.py --score    # after a result-skeptic READY
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
HOLD = 3
APART = 15.0          # live confluence. Wider than this is the minute the desk refuses.
TOP = 20.0            # live overbought band. The fast-leaving picture uses it. The call does not.
FLAT = 1.0            # picture only: a line that moved by at most this many points is flat.
SIG_START, SIG_END = 9 * 60 + 35, 15 * 60 + 56
CUT = "2026-07-23"
FIRST_DAY, LAST_DAY, N_DAYS = "2026-04-28", "2026-10-15", 119
T_BAR, MIN_DAYS, MIN_MINUTES = 1.5, 30, 1000


def _finite(*xs) -> bool:
    return all(math.isfinite(float(v)) for v in xs)


def flags(f0: float, s0: float, f5: float, s5: float):
    """(approach, fast-leaving, close-and-flat), or None when a reading is not finite.

    Approach is the only call: still apart, both lines below 0 and higher than 5 minutes
    ago, absolute gap smaller than 5 minutes ago. The other two are pictures.
    """
    if not _finite(f0, s0, f5, s5):
        return None
    f0, s0, f5, s5 = float(f0), float(s0), float(f5), float(s5)
    gap0, gap5 = abs(f0 - s0), abs(f5 - s5)
    fd, sd = f0 - f5, s0 - s5
    approach = gap0 > APART and gap0 < gap5 and fd > 0 and sd > 0 and f0 < 0 and s0 < 0
    leaving = f5 >= -TOP and fd < 0
    flat = gap0 <= APART and abs(fd) <= FLAT and abs(sd) <= FLAT
    return approach, leaving, flat


def reading(at: dict, m: int):
    """Flags at minute m from m and m-5 only. A later minute is not read."""
    if m not in at or (m - LAG) not in at:
        return None
    f0, s0 = at[m]
    f5, s5 = at[m - LAG]
    return flags(f0, s0, f5, s5)


def grade(net_a, t_a, ex_a, t_ex_a, cross_a, t_cross_a, days_a, ex_days_a, cross_days_a, n_a,
          net_b, t_b, ex_b, t_ex_b, cross_b, t_cross_b, days_b, ex_days_b, cross_days_b, n_b,
          fail_share: float) -> str:
    """PROMISING, FAIL, or UNDERPOWERED.

    Each argument is already one half. net is the day-mean minus 10 bp. This function
    does not read the 3 bp figure, the up-rate, or either picture.
    """
    counts = (days_a, ex_days_a, cross_days_a, days_b, ex_days_b, cross_days_b)
    ts = (t_a, t_ex_a, t_cross_a, t_b, t_ex_b, t_cross_b)
    if (fail_share > 0.01 or n_a < MIN_MINUTES or n_b < MIN_MINUTES
            or any(c < MIN_DAYS for c in counts) or any(t is None for t in ts)):
        return "UNDERPOWERED"
    ok = (net_a > 0 and t_a >= T_BAR and ex_a > 0 and t_ex_a >= T_BAR
          and cross_a > 0 and t_cross_a >= T_BAR
          and net_b > 0 and t_b >= T_BAR and ex_b > 0 and t_ex_b >= T_BAR
          and cross_b > 0 and t_cross_b >= T_BAR)
    return "PROMISING" if ok else "FAIL"


def _blank() -> dict:
    return {"mean": None, "n": 0, "n_up": 0, "excess": None, "cross": None}


def _slice(y: np.ndarray, minute: np.ndarray, take: np.ndarray) -> dict:
    mu, ex = R.slice_day(y, take)
    up = int((y[take] > 0).sum()) if take.any() else 0
    return {
        "mean": mu,
        "n": int(take.sum()) if len(y) else 0,
        "n_up": up,
        "excess": ex,
        "cross": R.cross_day(y, minute, take) if len(y) else None,
    }


def day_job(args) -> tuple[str, dict]:
    day, prev = args
    E.WARM_LINES = True
    lz = P.LazyDay(day, prev)
    iex, sip_f, prior = lz["iex"], lz["sip"], lz["iex_prior"]
    sips = {s: E.sip_map(f) for s, f in sip_f.items()}
    cnt = {"sampled": len(lz["board"]), "skipped_short": 0, "indicator_fail": 0, "no_history": 0}
    minutes, ys, approach, leaving, flat = [], [], [], [], []
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
        at, _prev_line = got
        sip = sips[sym]
        for m in range(SIG_START, SIG_END + 1):
            got_flags = reading(at, m)
            if got_flags is None:
                cnt["no_history"] += 1
                continue
            ret = E.markout(sip, m + 1, HOLD)
            if ret is None:
                continue
            minutes.append(m)
            ys.append(ret)
            approach.append(got_flags[0])
            leaving.append(got_flags[1])
            flat.append(got_flags[2])
    minute = np.asarray(minutes, np.int16)
    y = np.asarray(ys, np.float64)
    cnt["minutes"] = int(len(minute))
    return day, {
        "cnt": cnt,
        "approach": _slice(y, minute, np.asarray(approach, dtype=bool)),
        "leaving": _slice(y, minute, np.asarray(leaving, dtype=bool)),
        "flat": _slice(y, minute, np.asarray(flat, dtype=bool)),
    }


def _collect(per: dict, days: list[str], key: str) -> dict:
    means, excess, cross, ns, ups = [], [], [], [], []
    for d in days:
        row = per[d][key]
        if row["mean"] is None:
            continue
        means.append(row["mean"])
        ns.append(row["n"])
        ups.append(row["n_up"])
        if row["excess"] is not None:
            excess.append(row["excess"])
        if row["cross"] is not None:
            cross.append(row["cross"])
    mu, t, nd = R.day_t(means)
    nu, nt, _ = R.day_t([m - E.COST for m in means])
    eu, et, ed = R.day_t(excess)
    cu, ct, cd = R.day_t(cross)
    n = int(sum(ns))
    up = int(sum(ups))
    return {"mean": mu, "t": t, "net": nu, "t_net": nt, "days": nd, "n": n,
            "n_up": up, "up_rate": None if n == 0 else up / n,
            "excess": eu, "t_ex": et, "excess_days": ed,
            "cross": cu, "t_cross": ct, "cross_days": cd}


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
    rate = "-" if row["up_rate"] is None else f"{row['up_rate']:.1%}"
    return (f"{label} days {row['days']} n {row['n']} up {rate} "
            f"gross_bp {_bp(row['mean'])} t {_tt(row['t'])} "
            f"net10_bp {_bp(row['net'])} net_t {_tt(row['t_net'])} "
            f"excess_bp {_bp(row['excess'])} excess_t {_tt(row['t_ex'])} "
            f"cross_bp {_bp(row['cross'])} cross_t {_tt(row['t_cross'])} "
            f"net3_bp {_bp(net3)}")


def grade_of(per: dict, days: list[str]) -> str:
    A = [d for d in days if d <= CUT]
    B = [d for d in days if d > CUT]
    a = _collect(per, A, "approach")
    b = _collect(per, B, "approach")
    return grade(
        a["net"], a["t_net"], a["excess"], a["t_ex"], a["cross"], a["t_cross"],
        a["days"], a["excess_days"], a["cross_days"], a["n"],
        b["net"], b["t_net"], b["excess"], b["t_ex"], b["cross"], b["t_cross"],
        b["days"], b["excess_days"], b["cross_days"], b["n"],
        _counts(per)["fail_share"])


def report(per: dict, days: list[str], verdict: str) -> str:
    A = [d for d in days if d <= CUT]
    B = [d for d in days if d > CUT]
    L = ["# Sooner %R open (pr_approach_prereg)", "",
         "The call is lines still more than 15 points apart, both below 0 and higher than 5 minutes ago,",
         "gap smaller than 5 minutes ago. Next 3 minutes, net of 10 bp, each half.",
         "The lines are a picture of price already printed. A lineup is not a cause.",
         "Fast leaving the top, and close-and-flat, are pictures. They are not the call.",
         "",
         f"call {verdict}",
         ""]
    for key, title in (("approach", "Approach (the call)"),
                       ("leaving", "Fast leaving the top (picture)"),
                       ("flat", "Close and flat (picture)")):
        L.append(f"## {title}")
        L.append(_line("A", _collect(per, A, key)))
        L.append(_line("B", _collect(per, B, key)))
        L.append("")
    cnt = _counts(per)
    L.append(f"name-days sampled {cnt['sampled']} skipped_short {cnt['skipped_short']} "
             f"indicator_fail {cnt['indicator_fail']} ({cnt['fail_share']:.2%})")
    L.append(f"minutes scored {cnt['minutes']} minutes dropped for a missing 5-minute reading {cnt['no_history']}")
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
    jobs = [(d["day"], d._prev) for d in lz]
    per = {}
    with ProcessPoolExecutor(a.workers) as ex:
        for k, (day, row) in enumerate(ex.map(day_job, jobs)):
            per[day] = row
            if k % 10 == 0:
                print(f"  {day} ({k + 1}/{len(jobs)})", flush=True)
    verdict = grade_of(per, days)
    md = report(per, days, verdict)
    print(md, flush=True)
    out = os.path.join(ROOT, "ai_reports", "warehouse", "pr_approach_result.md")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        f.write(md + "\n")


if __name__ == "__main__":
    main()
