#!/usr/bin/env python3
"""Calendar-flow family on SPY and IWM. Spec: docs/studies/calendar_flows_prereg.json.

Close-to-close daily returns from Yahoo adjusted closes (as month_end_rebal.py). Each window is scored as one
difference per year (in-window mean daily return minus the mean of days in no window), t across years, in both
halves (split 2010-01-01). Prereg amended_1.

  --fetch   download and cache the closes (ai_reports/calendar_flows/closes.pkl)
  --score   score (only after a result-skeptic READY on the prereg)
"""
from __future__ import annotations

import argparse
import math
import os
import pickle
import statistics as st
from datetime import date, timedelta

ROOT = os.getcwd()
CACHE = os.path.join(ROOT, "ai_reports", "calendar_flows", "closes.pkl")
SPLIT = date(2010, 1, 1)
T_BAR = 2.6
START = {"SPY": "1993-02-01", "IWM": "2000-06-01"}


# ── pure window logic (tested) ────────────────────────────────────────────────

def month_key(d: date) -> tuple[int, int]:
    return d.year, d.month


def _easter(y: int) -> date:
    """Gregorian Easter (anonymous algorithm)."""
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month = (h + l_ - 7 * m + 114) // 31
    day = ((h + l_ - 7 * m + 114) % 31) + 1
    return date(y, month, day)


def _nth_weekday(y: int, m: int, wd: int, n: int) -> date:
    d = date(y, m, 1)
    d += timedelta(days=(wd - d.weekday()) % 7)
    return d + timedelta(days=7 * (n - 1))


def _last_weekday(y: int, m: int, wd: int) -> date:
    d = (date(y, m + 1, 1) if m < 12 else date(y + 1, 1, 1)) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - wd) % 7)


def _observed(d: date, saturday_to_friday: bool = True) -> date | None:
    if d.weekday() == 5:
        return d - timedelta(days=1) if saturday_to_friday else None
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def nyse_holidays(y: int) -> set[date]:
    """SCHEDULED NYSE full-day holidays (prereg amended_1 #11): unscheduled closures (2001-09-11..14, Sandy 2012,
    national days of mourning) are not holidays. New Year's Day falling on a Saturday is not observed on Friday."""
    out = {_observed(date(y, 1, 1), saturday_to_friday=False), _nth_weekday(y, 2, 0, 3),
           _easter(y) - timedelta(days=2), _last_weekday(y, 5, 0), _observed(date(y, 7, 4)),
           _nth_weekday(y, 9, 0, 1), _nth_weekday(y, 11, 3, 4), _observed(date(y, 12, 25))}
    if y >= 1998:
        out.add(_nth_weekday(y, 1, 0, 3))
    if y >= 2022:
        out.add(_observed(date(y, 6, 19)))
    return {d for d in out if d is not None}


def window_flags(days: list[date]) -> dict[str, list[bool]]:
    """Per trading day, which windows it belongs to (prereg amended_1). TOM and QE EXCLUDE the month's last trading
    day (that day is month-end, the separate passed lead). 'NONE' marks days in no window at all (incl. month-end):
    the out-of-window comparison set."""
    n = len(days)
    idx_in_month: list[int] = []
    from_end: list[int] = []
    i = 0
    while i < n:
        j = i
        while j < n and month_key(days[j]) == month_key(days[i]):
            j += 1
        for k in range(i, j):
            idx_in_month.append(k - i + 1)
            from_end.append(j - k)
        i = j
    tom = [idx_in_month[k] <= 3 for k in range(n)]                       # first 3 trading days (last day excluded)
    qe = [from_end[k] == 2 and days[k].month in (3, 6, 9, 12) for k in range(n)]   # 2nd-to-last day of the quarter
    hol = set()
    for y in range(days[0].year, days[-1].year + 1):
        hol |= nyse_holidays(y)
    prehol = [False] * n
    for k in range(n - 1):
        nxt_trade = days[k + 1]
        if any(days[k] < h < nxt_trade for h in hol):
            prehol[k] = True
    taxjan = [(days[k].month == 12 and from_end[k] <= 3) or (days[k].month == 1 and idx_in_month[k] <= 5)
              for k in range(n)]
    opex = []
    for k in range(n):
        d = days[k]
        first = d.replace(day=1)
        third_fri = first + timedelta(days=(4 - first.weekday()) % 7 + 14)
        opex.append(third_fri - timedelta(days=4) <= d <= third_fri)
    month_end = [from_end[k] == 1 for k in range(n)]
    none = [not (tom[k] or qe[k] or prehol[k] or taxjan[k] or opex[k] or month_end[k]) for k in range(n)]
    return {"TOM": tom, "QE": qe, "PREHOL": prehol, "TAXJAN": taxjan, "OPEX": opex, "NONE": none}


MIN_YEARS = 8          # per half, else UNDERPOWERED
MIN_IN_DAYS = 30       # in-window days per half, else UNDERPOWERED


def score_cell(days: list[date], rets: list[float], flag: list[bool], none: list[bool],
               lo: date | None, hi: date | None) -> dict:
    """One difference per YEAR (prereg amended_1 #12): mean in-window daily return minus mean of days in no window,
    that year; t across years. Strategy line is information only (#13)."""
    by_year: dict[int, tuple[list, list]] = {}
    for k in range(1, len(days)):
        d = days[k]
        if (lo and d < lo) or (hi and d >= hi):
            continue
        ins, outs = by_year.setdefault(d.year, ([], []))
        if flag[k]:
            ins.append(rets[k])
        elif none[k]:
            outs.append(rets[k])
    diffs = [st.fmean(a) - st.fmean(b) for a, b in by_year.values() if a and b]
    n_in = sum(len(a) for a, _ in by_year.values())
    t = None
    if len(diffs) >= 3 and st.stdev(diffs) > 0:
        t = st.fmean(diffs) / (st.stdev(diffs) / math.sqrt(len(diffs)))
    all_in = [x for a, _ in by_year.values() for x in a]
    all_out = [x for _, b in by_year.values() for x in b]
    return {"years": len(diffs), "n_in": n_in, "in_bp": st.fmean(all_in) * 1e4 if all_in else None,
            "out_bp": st.fmean(all_out) * 1e4 if all_out else None,
            "diff_bp": st.fmean(diffs) * 1e4 if diffs else None, "t": t,
            "underpowered": len(diffs) < MIN_YEARS or n_in < MIN_IN_DAYS}


# ── data and report ───────────────────────────────────────────────────────────

def fetch() -> dict:
    import yfinance as yf
    out = {}
    for sym, start in START.items():
        df = yf.download(sym, start=start, end="2026-10-16", auto_adjust=True, progress=False)["Close"]
        s = df[sym] if hasattr(df, "columns") else df
        out[sym] = [(i.date(), float(v)) for i, v in s.dropna().items()]
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    pickle.dump(out, open(CACHE, "wb"))
    return out


def score() -> str:
    data = pickle.load(open(CACHE, "rb"))
    lines = ["# Calendar-flow family (Yahoo adjusted closes; prereg amended_1)", "",
             f"t bar {T_BAR} across YEARS (one difference per year); halves split {SPLIT}; out-of-window = days in no "
             "window (month-end excluded); UNDERPOWERED under " f"{MIN_YEARS} years or {MIN_IN_DAYS} in-window days", "",
             "| ETF | window | half | years | in-window days | in bp/day | none bp/day | diff bp (t, years) | verdict |",
             "|---|---|---|---:|---:|---:|---:|---:|---|"]
    for sym, rows in data.items():
        days = [d for d, _ in rows]
        px = [p for _, p in rows]
        rets = [0.0] + [px[k] / px[k - 1] - 1 for k in range(1, len(px))]
        flags = window_flags(days)
        for w in ("TOM", "QE", "PREHOL", "TAXJAN", "OPEX"):
            if w == "TAXJAN" and sym != "IWM":
                continue
            halves = [score_cell(days, rets, flags[w], flags["NONE"], None, SPLIT),
                      score_cell(days, rets, flags[w], flags["NONE"], SPLIT, None)]
            powered = [h for h in halves if not h["underpowered"]]
            if any(h["t"] is None or h["t"] < T_BAR for h in powered):
                verdict = "FAIL"                      # a powered half that misses fails the cell outright
            elif len(powered) < 2:
                verdict = "UNDERPOWERED"
            else:
                verdict = "PASS"
            for name, h in zip(("A <2010", "B 2010+"), halves):
                f = lambda x: "-" if x is None else f"{x:+.1f}"  # noqa: E731
                lines.append(f"| {sym} | {w} | {name} | {h['years']} | {h['n_in']} | {f(h['in_bp'])} | {f(h['out_bp'])} | "
                             f"{f(h['diff_bp'])} ({f(h['t'])}) | {verdict} |")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--score", action="store_true")
    a = ap.parse_args()
    if a.fetch:
        fetch()
    if a.score:
        out = score()
        print(out)
        with open(os.path.join(os.path.dirname(CACHE), "result.md"), "w") as fh:
            fh.write(out + "\n")


if __name__ == "__main__":
    main()
