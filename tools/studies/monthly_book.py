#!/usr/bin/env python3
"""Monthly book: five price-only rules, top 20 held month-end to month-end, against SPY.
Spec: docs/studies/monthly_book_prereg.json. Reads ai_reports/gapdown/daily.pkl only (no API calls).

  .venv/bin/python tools/studies/monthly_book.py            # inventory
  .venv/bin/python tools/studies/monthly_book.py --score    # after a result-skeptic READY
"""
from __future__ import annotations

import argparse
import bisect
import math
import os
import pickle
import statistics as st
from collections import defaultdict

import numpy as np

ROOT = os.environ.get("REPO") or os.getcwd()
DAILY = os.path.join(ROOT, "ai_reports", "gapdown", "daily.pkl")
RESULT = os.path.join(ROOT, "ai_reports", "monthly_book_result.md")
BOOK_N = 20
HIST = 253
COST_BP = 10.0
A_FIRST, A_LAST = "2016-12-30", "2021-11-30"
B_FIRST, B_LAST = "2021-12-31", "2026-08-31"
T_PASS, T_PROMISING = 2.33, 1.5
DEAD_RET = -0.30          # a picked name with no print after t (delisting with no later price), declared in the prereg
MAX_GAP = 30              # sessions; a longer hole in a series suggests a reused ticker joining two companies
N_A, N_B = 60, 57
STOP_PCT = 0.10           # information rows: a fixed stop from entry, and a trailing stop from the best close
STOPS = ("stop10", "trail10")
DROP_BEST = 3
RULES = ("R1_smooth_momentum", "R2_near_52w_high", "R3_low_volatility", "R4_trend_low_vol", "R5_one_month_reversal")


# ── pure parts (tested) ──────────────────────────────────────────────────────

def month_ends(cal: list[str]) -> list[str]:
    """Last session of each month in a sorted calendar."""
    out = []
    for i, d in enumerate(cal):
        if i + 1 == len(cal) or cal[i + 1][:7] != d[:7]:
            out.append(d)
    return out


def scores(c: np.ndarray) -> dict[str, float | None]:
    """Rule scores from adjusted closes ending the session BEFORE the rebalance day (oldest first, >= HIST long).
    Higher is better for every rule."""
    if len(c) < HIST or not np.all(np.isfinite(c[-HIST:])) or np.any(c[-HIST:] <= 0):
        return dict.fromkeys(RULES)
    lr = np.diff(np.log(c[-HIST:]))
    v252 = float(np.std(lr[-252:], ddof=1))
    v60 = float(np.std(lr[-60:], ddof=1))
    mom = c[-22] / c[-253] - 1.0
    last = c[-1]
    trend_up = last > float(np.mean(c[-200:]))
    return {
        "R1_smooth_momentum": None if v252 <= 0 else mom / v252,
        "R2_near_52w_high": last / float(np.max(c[-252:])),
        "R3_low_volatility": -v60,
        "R4_trend_low_vol": -v60 if trend_up else None,
        "R5_one_month_reversal": -(last / c[-22] - 1.0),
    }


def pick(cands: dict[str, float | None], n: int = BOOK_N) -> list[str]:
    """Top n by score; ties broken by symbol so the pick is reproducible."""
    ok = [(s, v) for s, v in cands.items() if v is not None and math.isfinite(v)]
    ok.sort(key=lambda sv: (-sv[1], sv[0]))
    return [s for s, _ in ok[:n]]


def turnover(prev: list[str], new: list[str]) -> float:
    """Weight traded (both sides) moving from one equal-weight book to the next, ignoring drift."""
    if not prev:
        return 1.0 if new else 0.0
    wp, wn = 1.0 / len(prev), (1.0 / len(new) if new else 0.0)
    names = set(prev) | set(new)
    return sum(abs((wn if s in new else 0.0) - (wp if s in prev else 0.0)) for s in names)


def ols(y: list[float], x: list[float]) -> dict:
    """y = a + b x. Alpha, its t, beta, residual SD."""
    n = len(y)
    if n < 6:
        return {"alpha": None, "t": None, "beta": None, "resid_sd": None, "n": n}
    X = np.column_stack([np.ones(n), np.asarray(x)])
    Y = np.asarray(y)
    coef, *_ = np.linalg.lstsq(X, Y, rcond=None)
    resid = Y - X @ coef
    s2 = float(resid @ resid) / (n - 2)
    cov = s2 * np.linalg.inv(X.T @ X)
    se = math.sqrt(cov[0, 0])
    return {"alpha": float(coef[0]), "t": float(coef[0] / se) if se > 0 else None, "beta": float(coef[1]),
            "resid_sd": math.sqrt(s2), "n": n}


def without_best(y: list[float], x: list[float], k: int = DROP_BEST) -> float | None:
    """Alpha after dropping the k months with the highest book return."""
    if len(y) <= k + 6:
        return None
    keep = sorted(range(len(y)), key=lambda i: y[i])[:-k]
    return ols([y[i] for i in keep], [x[i] for i in keep])["alpha"]


def stop_return(path: list[tuple[str, float, float]], entry: float, kind: str, pct: float = STOP_PCT,
                final: float | None = None) -> tuple[float, bool]:
    """Return of one holding with a daily-close stop. path: [(date, open, close)] of the sessions after entry up to
    the exit day. A close below the stop level sells at the NEXT session's open (no same-day fill on the close that
    triggered it). `final` is the planned exit price when the stop never fires. kind: stop10 (from entry) or
    trail10 (from the best close so far)."""
    best = entry
    for i, (_d, _o, c) in enumerate(path):
        level = (entry if kind == "stop10" else best) * (1 - pct)
        if c < level:
            if i + 1 < len(path):
                return path[i + 1][1] / entry - 1.0, True
            return c / entry - 1.0, True          # fired on the last close: sold at that close
        best = max(best, c)
    end = final if final is not None else (path[-1][2] if path else entry)
    return end / entry - 1.0, False


def word(a: dict, b: dict, pooled: dict, wo_best: float | None, ex_a: float | None, ex_b: float | None) -> str:
    def pos(v):
        return v is not None and v > 0
    if (pos(a["alpha"]) and pos(b["alpha"]) and pooled["t"] is not None and pooled["t"] >= T_PASS
            and pos(ex_a) and pos(ex_b) and pos(wo_best)):
        return "PASS"
    if pos(a["alpha"]) and pos(b["alpha"]) and pooled["t"] is not None and pooled["t"] >= T_PROMISING and pos(wo_best):
        return "PROMISING"
    return "FAIL"


# ── the run ──────────────────────────────────────────────────────────────────

class Panel:
    def __init__(self, d: dict):
        self.adj = d["adj"]
        self.liquid = {s: set(v) for s, v in d["liquid"].items()}
        self.cal = sorted(self.adj["SPY"])
        self.dates = {s: sorted(v) for s, v in self.adj.items()}
        self.closes = {s: np.array([self.adj[s][x][1] for x in self.dates[s]], np.float64) for s in self.adj}
        pos = {d: i for i, d in enumerate(self.cal)}
        self.joined = set()
        for s, ds in self.dates.items():
            idx = [pos[x] for x in ds if x in pos]
            if any(b - a > MAX_GAP for a, b in zip(idx, idx[1:])):
                self.joined.add(s)

    def history(self, s: str, before: str) -> np.ndarray:
        k = bisect.bisect_left(self.dates[s], before)
        return self.closes[s][:k]

    def path(self, s: str, after: str, through: str) -> list[tuple[str, float, float]]:
        lo = bisect.bisect_right(self.dates[s], after)
        hi = bisect.bisect_right(self.dates[s], through)
        return [(x, self.adj[s][x][0], self.adj[s][x][1]) for x in self.dates[s][lo:hi]]

    def close_on_or_before(self, s: str, day: str, after: str) -> tuple[float | None, bool]:
        """(close, delisted_or_missing). Close on `day`; else the last close in (after, day)."""
        if day in self.adj[s]:
            return self.adj[s][day][1], False
        k = bisect.bisect_right(self.dates[s], day) - 1
        if k >= 0 and self.dates[s][k] > after:
            return self.closes[s][k], True
        return None, True


def survivorship(panel: Panel) -> str:
    """Per year: eligible names whose series ends before 2026-09 (died), and series excluded for a > MAX_GAP hole."""
    ends = month_ends(panel.cal)
    died: dict[str, set] = defaultdict(set)
    seen: dict[str, set] = defaultdict(set)
    for t in ends:
        if not (A_FIRST <= t <= B_LAST):
            continue
        for s in panel.adj:
            if s != "SPY" and t in panel.liquid.get(s, ()) and t in panel.adj[s]:
                seen[t[:4]].add(s)
                if panel.dates[s][-1] < "2026-09-01":
                    died[t[:4]].add(s)
    rows = ", ".join(f"{y} {len(died[y])}/{len(seen[y])}" for y in sorted(seen))
    return (f"eligible names whose series ends before 2026-09, by year (died/eligible): {rows}\n"
            f"series excluded for a hole of more than {MAX_GAP} sessions (possible reused ticker): {len(panel.joined)}")


def run(panel: Panel) -> tuple[dict, list[str]]:
    ends = month_ends(panel.cal)
    months = [(t, t2) for t, t2 in zip(ends, ends[1:]) if A_FIRST <= t <= B_LAST]
    out = {r: [] for r in RULES}
    prev = dict.fromkeys(RULES, [])
    for t, t2 in months:
        elig = {}
        for s in panel.adj:
            if s == "SPY" or s in panel.joined or t not in panel.liquid.get(s, ()) or t not in panel.adj[s]:
                continue
            h = panel.history(s, t)
            if len(h) >= HIST:
                elig[s] = scores(h)
        spy = panel.adj["SPY"][t2][1] / panel.adj["SPY"][t][1] - 1.0
        rets, dl = {}, {}
        for s in elig:
            c1, gone = panel.close_on_or_before(s, t2, t)
            # Every eligible name gets a return, so the pick never depends on what happened after t.
            rets[s] = DEAD_RET if c1 is None else c1 / panel.adj[s][t][1] - 1.0
            dl[s] = gone
        univ = st.fmean(rets.values()) if rets else None
        for r in RULES:
            book = pick({s: v[r] for s, v in elig.items()})
            gross = st.fmean(rets[s] for s in book) if book else 0.0
            to = turnover(prev[r], book)
            net = gross - to * COST_BP / 1e4
            stop_net, fired = {}, {}
            for k in STOPS:
                legs = []
                n_fired = 0
                for s in book:
                    pth = panel.path(s, t, t2)
                    if not pth:
                        legs.append(rets[s])          # never printed again: the declared -30%
                        continue
                    final = panel.adj[s][t2][1] if t2 in panel.adj[s] else None
                    rr, hit = stop_return(pth, panel.adj[s][t][1], k, final=final)
                    legs.append(rr)
                    n_fired += hit
                # A stopped name is sold early (10 bp more) and sits in cash; the rebalance cost is unchanged.
                stop_net[k] = (st.fmean(legs) if legs else 0.0) - to * COST_BP / 1e4 - n_fired / max(1, len(book)) * COST_BP / 1e4
                fired[k] = n_fired
            out[r].append({"t": t, "t2": t2, "net": net, "gross": gross, "spy": spy, "univ": univ, "book": book,
                           "turnover": to, "delisted": sum(1 for s in book if dl[s]), "eligible": len(elig),
                           "empty": not book, "stop_net": stop_net, "fired": fired})
            prev[r] = book
    return out, [t for t, _ in months]


def half_rows(rows: list[dict], first: str, last: str) -> list[dict]:
    return [m for m in rows if first <= m["t"] <= last]


def _f(x, nd=2, pct=True):
    if x is None:
        return "—"
    return f"{x * 100:+.{nd}f}%" if pct else f"{x:+.{nd}f}"


def report(out: dict) -> str:
    lines = ["# Monthly book (monthly_book_prereg)", "",
             "Top 20 by each rule at a month-end close, held to the next; net of 10 bp per side on turnover.",
             "alpha = intercept of monthly net on SPY. PASS needs alpha > 0 in both halves, pooled t >= 2.33, net-SPY > 0",
             "in both halves, and alpha > 0 without the 3 best months.", ""]
    words = []
    blocks = []
    for r in RULES:
        rows = out[r]
        a_rows, b_rows = half_rows(rows, A_FIRST, A_LAST), half_rows(rows, B_FIRST, B_LAST)
        ra = ols([m["net"] for m in a_rows], [m["spy"] for m in a_rows])
        rb = ols([m["net"] for m in b_rows], [m["spy"] for m in b_rows])
        both = a_rows + b_rows
        y, x = [m["net"] for m in both], [m["spy"] for m in both]
        rp = ols(y, x)
        wo = without_best(y, x)
        ex = {k: (st.fmean(m["net"] - m["spy"] for m in v) if v else None) for k, v in (("A", a_rows), ("B", b_rows))}
        w = word(ra, rb, rp, wo, ex["A"], ex["B"])
        words.append(f"{r}: {w}")
        mde = None if rp["resid_sd"] is None else 2 * rp["resid_sd"] / math.sqrt(rp["n"])
        ua = ols([m["univ"] for m in both if m["univ"] is not None], [m["spy"] for m in both if m["univ"] is not None])["alpha"]
        el = sorted(m["eligible"] for m in both)
        blocks += [f"## {r}: **{w}**", "",
                   "| half | months | net/mo | SPY/mo | net-SPY | alpha (t) | beta | universe/mo | beat SPY | worst | turnover | delisted |",
                   "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for tag, rr, reg in (("A", a_rows, ra), ("B", b_rows, rb), ("all", both, rp)):
            if not rr:
                continue
            blocks.append(
                f"| {tag} | {len(rr)} | {_f(st.fmean(m['net'] for m in rr))} | {_f(st.fmean(m['spy'] for m in rr))} | "
                f"{_f(st.fmean(m['net'] - m['spy'] for m in rr))} | {_f(reg['alpha'])} ({_f(reg['t'], 2, False)}) | "
                f"{_f(reg['beta'], 2, False)} | {_f(st.fmean(m['univ'] for m in rr if m['univ'] is not None))} | "
                f"{sum(1 for m in rr if m['net'] > m['spy'])}/{len(rr)} | {_f(min(m['net'] for m in rr))} | "
                f"{st.fmean(m['turnover'] for m in rr):.2f} | {sum(m['delisted'] for m in rr)} |")
        for k in STOPS:
            sa = ols([m["stop_net"][k] for m in a_rows], [m["spy"] for m in a_rows])
            sb = ols([m["stop_net"][k] for m in b_rows], [m["spy"] for m in b_rows])
            sp = ols([m["stop_net"][k] for m in both], [m["spy"] for m in both])
            blocks.append(f"information, {k} (10% {'from entry' if k == 'stop10' else 'trailing from the best close'}, "
                          f"sold next open): alpha A {_f(sa['alpha'])} B {_f(sb['alpha'])} all {_f(sp['alpha'])} "
                          f"(t {_f(sp['t'], 2, False)}); worst month {_f(min(m['stop_net'][k] for m in both))}; "
                          f"stops fired {sum(m['fired'][k] for m in both)} of {sum(len(m['book']) for m in both)} holdings")
        best = sorted(both, key=lambda m: -m["net"])[:DROP_BEST]
        blocks += ["", f"alpha without the {DROP_BEST} best months {_f(wo)}; MDE at t 2 {_f(mde)}; "
                   f"equal-weight universe alpha {_f(ua)}; rule alpha minus universe alpha "
                   f"{_f(None if rp['alpha'] is None or ua is None else rp['alpha'] - ua)}; "
                   f"eligible names per month min {el[0]} median {el[len(el) // 2]} max {el[-1]}",
                   "best months: " + "; ".join(f"{m['t']} {_f(m['net'])} ({', '.join(m['book'][:5])}...)" for m in best),
                   "by year, net-SPY: " + ", ".join(
                       f"{y} {_f(st.fmean(m['net'] - m['spy'] for m in both if m['t'][:4] == y))}"
                       for y in sorted({m['t'][:4] for m in both})), ""]
    return "\n".join(lines + [" | ".join(words), ""] + blocks)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--score", action="store_true")
    a = ap.parse_args()
    d = pickle.load(open(DAILY, "rb"))
    p = Panel(d)
    ends = month_ends(p.cal)
    n_months = sum(1 for t in ends[:-1] if A_FIRST <= t <= B_LAST)
    print(f"daily.pkl: {len(p.adj)} adjusted series, {len(p.liquid)} ever liquid; SPY {p.cal[0]}..{p.cal[-1]}; "
          f"holding months {n_months}", flush=True)
    if not a.score:
        print("(inventory only; --score after the prereg has a result-skeptic READY)")
        return
    if A_FIRST not in ends or B_LAST not in ends:
        print(f"REFUSING: {A_FIRST} or {B_LAST} is not a month-end session in SPY's calendar")
        return
    print(survivorship(p), flush=True)
    out, _ = run(p)
    bad = []
    for r in RULES:
        na = len(half_rows(out[r], A_FIRST, A_LAST))
        nb = len(half_rows(out[r], B_FIRST, B_LAST))
        empty = sum(1 for m in out[r] if m["empty"])
        if na != N_A or nb != N_B or empty:
            bad.append(f"{r}: {na} A months, {nb} B months, {empty} empty")
    if bad:
        print("REFUSING: " + "; ".join(bad))
        return
    md = survivorship(p) + "\n\n" + report(out)
    print(md, flush=True)
    with open(RESULT, "w") as f:
        f.write(md + "\n")


if __name__ == "__main__":
    main()
