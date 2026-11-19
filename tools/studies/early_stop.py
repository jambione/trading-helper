#!/usr/bin/env python3
"""early_stop - the standard futility screen for warehouse studies (docs/TEST_STANDARD.md, 'Early stop', 11/19).

Score the first quarter of a study's days (by date) first. If that screen is clearly failing - mean net below
(pass bar - 2 bp) AND day-clustered t <= -1.0 - stop and record FAIL-early; otherwise run the full study, whose own pass
bar still decides. A screen can only STOP a study, never pass one, so it cannot raise the false-positive rate; it only
saves the time spent finishing ideas that are already clearly dead.

    from early_stop import screen_days, screen
    first = screen_days(all_days)                 # the screen's days
    verdict, st = screen(rows_on_first_days, key="net", pass_mean=2.0)
"""
from __future__ import annotations

import math
from collections import defaultdict

SCREEN_FRAC, MARGIN_BP, STOP_T = 0.25, 2.0, -1.0


def screen_days(days, frac=SCREEN_FRAC):
    """The first `frac` of the sorted days (at least 5). Pure."""
    d = sorted(set(days))
    return d[: max(5, int(math.ceil(len(d) * frac)))]


def _day_t(rows, key):
    per = defaultdict(list)
    for r in rows:
        per[r["day"]].append(r[key])
    m = [sum(v) / len(v) for v in per.values()]
    if len(m) < 3:
        return None, (sum(m) / len(m) if m else None)
    mu = sum(m) / len(m)
    sd = math.sqrt(sum((x - mu) ** 2 for x in m) / (len(m) - 1))
    return (mu / (sd / math.sqrt(len(m))) if sd > 0 else None), mu


def screen(rows, key="net", pass_mean=2.0, margin=MARGIN_BP, stop_t=STOP_T, min_n=50):
    """rows on the screen days -> ('STOP' | 'CONTINUE', stats). STOP only if the mean is below pass_mean - margin AND
    the day-clustered t (of the mean vs 0) is <= stop_t, with >= min_n rows. Pure."""
    n = len(rows)
    if n < min_n:
        return "CONTINUE", {"n": n, "why": f"screen too small ({n} < {min_n})"}
    mean = sum(r[key] for r in rows) / n
    t, _ = _day_t(rows, key)
    st = {"n": n, "mean": round(mean, 2), "t_day": None if t is None else round(t, 2),
          "stop_if": f"mean < {pass_mean - margin:g} and t <= {stop_t:g}"}
    if mean < pass_mean - margin and t is not None and t <= stop_t:
        return "STOP", st
    return "CONTINUE", st


GROSS_Z = 2.0


def gross_screen(rows, key="gross", pass_mean=2.0, z=GROSS_Z, min_n=100):
    """Gross-first screen (TEST_STANDARD 'Gross-first', 11/19): run on ALL candidate rows BEFORE any spread is fetched.
    Net = gross - spread with spread >= 0, so a cell whose gross cannot reach the NET pass bar even at its upper bound
    (mean + z x day-clustered SE) cannot pass after costs: SKIP its quotes. -> ('SKIP_QUOTES' | 'QUOTE', stats). Pure.
    Like screen(), it can only stop a cell, never pass one."""
    n = len(rows)
    if n < min_n:
        return "QUOTE", {"n": n, "why": f"too few rows to screen ({n} < {min_n})"}
    per = defaultdict(list)
    for r in rows:
        per[r["day"]].append(r[key])
    m = [sum(v) / len(v) for v in per.values()]
    mean = sum(r[key] for r in rows) / n
    if len(m) < 3:
        return "QUOTE", {"n": n, "why": "fewer than 3 days"}
    mu = sum(m) / len(m)
    se = math.sqrt(sum((x - mu) ** 2 for x in m) / (len(m) - 1)) / math.sqrt(len(m))
    ucb = mean + z * se
    st = {"n": n, "gross_mean": round(mean, 2), "gross_ucb": round(ucb, 2), "skip_if": f"gross ucb < {pass_mean:g}"}
    return ("SKIP_QUOTES" if ucb < pass_mean else "QUOTE"), st
