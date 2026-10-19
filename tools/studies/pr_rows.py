#!/usr/bin/env python3
"""Five %R combination rows. Spec: docs/studies/pr_rows_prereg.json.

Each row is reported on both halves. The return is the next 3 minutes. There is no single call.

  .venv/bin/python tools/studies/pr_rows.py            # inventory only
  .venv/bin/python tools/studies/pr_rows.py --score    # after a result-skeptic READY
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
HOLD_LONG = 5
FLOOR = -80.0
SLOW_STILL = -60.0
HOOK_RISE = 5.0
TOP = -20.0
TIGHT = 15.0
PIN_GAP = 20.0
RUN10 = 0.0010
RUN30 = 0.0030
SIG_START, SIG_END = 9 * 60 + 36, 15 * 60 + 56
CUT = "2026-07-23"
FIRST_DAY, LAST_DAY, N_DAYS = "2026-04-28", "2026-10-15", 119
MIN_DAYS, MIN_MINUTES = 30, 1000
ROWS = ("hook", "square_on", "square_stay", "pinned", "falling")
TITLES = {
    "hook": "Hook. First minute the fast line turns up off the floor, slow line still low.",
    "square_on": "Square starts. First minute both lines are touching at or above -20.",
    "square_stay": "Square continues. The same touch after that first minute.",
    "pinned": "Fast line near 0, slow line at least 20 points below, fast line not falling.",
    "falling": "Both lines at or below -80, within 15 points, both still falling.",
}


def _finite(*xs) -> bool:
    return all(math.isfinite(float(v)) for v in xs)


def _pt(at: dict, m: int):
    if m not in at:
        return None
    fast, slow = at[m]
    if not _finite(fast, slow):
        return None
    return float(fast), float(slow)


def raw_state(f0, s0, f1, s1, f3, s3, f5, s5):
    """(hook, square, pinned, falling) from this minute and its lags. No onset yet."""
    gap = abs(f0 - s0)
    hook = f5 <= FLOOR and f0 >= f5 + HOOK_RISE and s0 <= SLOW_STILL
    square = f0 >= TOP and s0 >= TOP and gap <= TIGHT and f0 > f1
    pinned = f0 >= TOP and s0 <= f0 - PIN_GAP and f0 >= f3
    falling = f0 <= FLOOR and s0 <= FLOOR and gap <= TIGHT and f0 < f5 and s0 < s5
    return hook, square, pinned, falling


def combine(now, prev):
    """Five row flags. now and prev are the four (fast, slow) pairs the lags need.

    prev is the same four pairs one minute earlier. The hook and the square count the
    first minute only. Pinned and falling count every minute of the state.
    """
    if now is None or prev is None:
        return None
    if not _finite(*[v for pair in now + prev for v in pair]):
        return None
    (f0, s0), (f1, s1), (f3, s3), (f5, s5) = now
    hook, square, pinned, falling = raw_state(f0, s0, f1, s1, f3, s3, f5, s5)
    (p0, q0), (p1, q1), (p3, q3), (p5, q5) = prev
    hook_prev, square_prev, _, _ = raw_state(p0, q0, p1, q1, p3, q3, p5, q5)
    return (
        hook and not hook_prev,
        square and not square_prev,
        square and square_prev,
        pinned,
        falling,
    )


def reading(at: dict, m: int):
    """Row flags at m. Indexes m and earlier only."""
    now = (_pt(at, m), _pt(at, m - 1), _pt(at, m - 3), _pt(at, m - 5))
    prev = (_pt(at, m - 1), _pt(at, m - 2), _pt(at, m - 4), _pt(at, m - 6))
    if any(p is None for p in now) or any(p is None for p in prev):
        return None
    return combine(now, prev)


def describe(mean_a, days_a, n_a, mean_b, days_b, n_b) -> str:
    """How the two halves compare. Gross means only. No cost and no other row."""
    if (mean_a is None or mean_b is None or days_a < MIN_DAYS or days_b < MIN_DAYS
            or n_a < MIN_MINUTES or n_b < MIN_MINUTES):
        return "THIN"
    if mean_a > 0 and mean_b > 0:
        return "SAME WAY UP"
    if mean_a < 0 and mean_b < 0:
        return "SAME WAY DOWN"
    if mean_a == 0 and mean_b == 0:
        return "FLAT BOTH"
    return "NOT THE SAME"


def _blank() -> dict:
    return {"mean": None, "n": 0, "n_up": 0, "n_10": 0, "n_30": 0,
            "mean5": None, "excess": None, "cross": None}


def _pack(y: np.ndarray, y5: np.ndarray, minute: np.ndarray, take: np.ndarray) -> dict:
    mu, ex = R.slice_day(y, take)
    n = int(take.sum()) if len(y) else 0
    if n == 0 or mu is None:
        return _blank()
    yy = y[take]
    tail = y5[take]
    ok = np.isfinite(tail)
    return {
        "mean": mu,
        "n": n,
        "n_up": int((yy > 0).sum()),
        "n_10": int((yy > RUN10).sum()),
        "n_30": int((yy > RUN30).sum()),
        "mean5": float(tail[ok].mean()) if ok.any() else None,
        "excess": ex,
        "cross": R.cross_day(y, minute, take),
    }


def day_job(args) -> tuple[str, dict]:
    day, prev = args
    E.WARM_LINES = True
    lz = P.LazyDay(day, prev)
    iex, sip_f, prior = lz["iex"], lz["sip"], lz["iex_prior"]
    sips = {s: E.sip_map(f) for s, f in sip_f.items()}
    cnt = {"sampled": len(lz["board"]), "skipped_short": 0, "indicator_fail": 0, "no_history": 0}
    minutes, ys, y5s = [], [], []
    flags = {name: [] for name in ROWS}
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
            got_rows = reading(at, m)
            if got_rows is None:
                cnt["no_history"] += 1
                continue
            ret = E.markout(sip, m + 1, HOLD)
            if ret is None:
                continue
            longer = E.markout(sip, m + 1, HOLD_LONG)
            minutes.append(m)
            ys.append(ret)
            y5s.append(np.nan if longer is None else longer)
            for name, on in zip(ROWS, got_rows):
                flags[name].append(on)
    minute = np.asarray(minutes, np.int16)
    y = np.asarray(ys, np.float64)
    y5 = np.asarray(y5s, np.float64)
    cnt["minutes"] = int(len(minute))
    out = {"cnt": cnt}
    for name in ROWS:
        out[name] = _pack(y, y5, minute, np.asarray(flags[name], dtype=bool))
    return day, out


def _collect(per: dict, days: list[str], key: str) -> dict:
    means, excess, cross, means5 = [], [], [], []
    ns = ups = n10 = n30 = 0
    for d in days:
        row = per[d][key]
        if row["mean"] is None:
            continue
        means.append(row["mean"])
        ns += row["n"]
        ups += row["n_up"]
        n10 += row["n_10"]
        n30 += row["n_30"]
        if row["excess"] is not None:
            excess.append(row["excess"])
        if row["cross"] is not None:
            cross.append(row["cross"])
        if row["mean5"] is not None:
            means5.append(row["mean5"])
    mu, t, nd = R.day_t(means)
    m5, t5, _ = R.day_t(means5)
    eu, et, ed = R.day_t(excess)
    cu, ct, cd = R.day_t(cross)

    def share(k):
        return None if ns == 0 else k / ns

    return {"mean": mu, "t": t, "days": nd, "n": ns,
            "up": share(ups), "over10": share(n10), "over30": share(n30),
            "mean5": m5, "t5": t5,
            "excess": eu, "t_ex": et, "excess_days": ed,
            "cross": cu, "t_cross": ct, "cross_days": cd}


def _counts(per: dict) -> dict:
    keys = ("sampled", "skipped_short", "indicator_fail", "no_history", "minutes")
    tot = {k: sum(v["cnt"][k] for v in per.values()) for k in keys}
    tot["fail_share"] = tot["indicator_fail"] / max(1, tot["sampled"])
    return tot


def _bp(x) -> str:
    return "-" if x is None else f"{x * 1e4:+.1f}"


def _tt(x) -> str:
    return "-" if x is None else f"{x:+.2f}"


def _pct(x) -> str:
    return "-" if x is None else f"{x:.1%}"


def _half(row: dict) -> str:
    net10 = None if row["mean"] is None else row["mean"] - E.COST
    net3 = None if row["mean"] is None else row["mean"] - 0.0003
    return (f"days {row['days']} n {row['n']} up {_pct(row['up'])} "
            f"over10 {_pct(row['over10'])} over30 {_pct(row['over30'])} "
            f"gross_bp {_bp(row['mean'])} t {_tt(row['t'])} "
            f"gross5_bp {_bp(row['mean5'])} t5 {_tt(row['t5'])} "
            f"excess_bp {_bp(row['excess'])} excess_t {_tt(row['t_ex'])} "
            f"cross_bp {_bp(row['cross'])} cross_t {_tt(row['t_cross'])} "
            f"net3_bp {_bp(net3)} net10_bp {_bp(net10)} (printed)")


def report(per: dict, days: list[str]) -> str:
    fit = [d for d in days if d <= CUT]
    check = [d for d in days if d > CUT]
    lines = ["# %R combination rows (pr_rows_prereg)", "",
             "Each row is the next 3 minutes inside the session. Fit sessions are through 2026-07-23.",
             "Check sessions are after that. The word on a row compares those two gross means.",
             "over10 and over30 are the share of the row's minutes up more than 10 bp and 30 bp.",
             "gross5 is the next 5 minutes, printed beside the 3-minute mean. The trail is not simulated.",
             ""]
    words = []
    blocks = []
    for name in ROWS:
        a = _collect(per, fit, name)
        b = _collect(per, check, name)
        word = describe(a["mean"], a["days"], a["n"], b["mean"], b["days"], b["n"])
        words.append(f"{name}: {word}")
        blocks += [f"### {TITLES[name]}", "", word, f"fit {_half(a)}", f"check {_half(b)}", ""]
    lines.append(" | ".join(words))
    lines.append("")
    lines.extend(blocks)
    cnt = _counts(per)
    lines.append(
        f"name-days sampled {cnt['sampled']} skipped_short {cnt['skipped_short']} "
        f"indicator_fail {cnt['indicator_fail']} ({cnt['fail_share']:.2%})")
    lines.append(f"minutes scored {cnt['minutes']} minutes dropped for a missing lag {cnt['no_history']}")
    if cnt["fail_share"] > 0.01:
        lines.append("Indicator misses are above 1%. The rows above use the names whose lines computed.")
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
    jobs = [(d["day"], d._prev) for d in lz]
    per = {}
    with ProcessPoolExecutor(a.workers) as ex:
        for k, (day, row) in enumerate(ex.map(day_job, jobs)):
            per[day] = row
            if k % 10 == 0:
                print(f"  {day} ({k + 1}/{len(jobs)})", flush=True)
    md = report(per, days)
    print(md, flush=True)
    out = os.path.join(ROOT, "ai_reports", "warehouse", "pr_rows_result.md")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        f.write(md + "\n")


if __name__ == "__main__":
    main()
