#!/usr/bin/env python3
"""Does arriving late actually cost money? Trade by trade.

The entire GATE 2 entry plan rests on one unverified link: `captured` is
high (0.71), therefore the desk is late, therefore lateness is why it
loses. The first two are measured. The third is assumed.

This tests it on the 344 real fills. Two instruments, deliberately:

  before   the % run-up from the prior close into the anchor instant.
           Entirely backward-looking, knowable at admission, and shares
           NO term with the realized outcome. This is the clean test.

  captured (admit_px - pclose) / (day_high_after - pclose). This is the
           metric the plan actually cites — but its denominator contains
           the post-anchor day high, which is also what makes a trade
           profitable. So captured and R are mechanically coupled, and a
           negative correlation here is partly tautological.

Read the two asymmetrically. If `captured` fails to predict R even with
the coupling working in its favour, the latency thesis is dead. If
`before` predicts R, the thesis survives in the form that matters — a
quantity you could actually gate on.

Anchored twice: at admission (the plan's claim) and at the fill (where
the trade's own arithmetic starts).

Session is the unit of independence, so every headline number gets a
paired session sign test underneath it. Read-only. Mini + .venv.
"""
from __future__ import annotations

import json
import math
import os
import statistics
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import drift_screen as DS            # noqa: E402
import admission_latency as AL       # noqa: E402

DAYS = 20
REPORTS = DS.resolve_report_dir()


# ---------------------------------------------------------------- statistics

def _rank(vals: list[float]) -> list[float]:
    """Average ranks, so ties do not manufacture correlation."""
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    out = [0.0] * len(vals)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        r = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            out[order[k]] = r
        i = j + 1
    return out


def spearman(xs: list[float], ys: list[float]) -> tuple[float, float, int]:
    """rho, t-statistic in sigma, n."""
    n = len(xs)
    if n < 4:
        return float("nan"), float("nan"), n
    rx, ry = _rank(xs), _rank(ys)
    mx, my = statistics.mean(rx), statistics.mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    dy = math.sqrt(sum((b - my) ** 2 for b in ry))
    if dx == 0 or dy == 0:
        return float("nan"), float("nan"), n
    rho = num / (dx * dy)
    if abs(rho) >= 1.0:
        return rho, float("inf"), n
    t = rho * math.sqrt((n - 2) / (1 - rho * rho))
    return rho, t, n


def binom_p(k: int, n: int) -> float:
    """Two-sided sign-test p for k successes in n at p=0.5."""
    if n == 0:
        return float("nan")
    c = lambda a, b: math.comb(a, b)  # noqa: E731
    tail = sum(c(n, i) for i in range(0, min(k, n - k) + 1)) / (2.0 ** n)
    return min(1.0, 2.0 * tail)


# ---------------------------------------------------------------- data

def load_fills() -> list[dict]:
    out = []
    for ln in open(os.path.join(REPORTS, "outcomes.jsonl"),
                   encoding="utf-8", errors="replace"):
        ln = ln.strip()
        if not ln:
            continue
        try:
            o = json.loads(ln)
        except ValueError:
            continue
        r, t, s = o.get("realized_r_multiple"), o.get("entry_time"), o.get("symbol")
        if r is None or not t or not s:
            continue
        out.append({
            "symbol": str(s).upper(),
            "entry_time": float(t),
            "day": DS._day_of(float(t)),
            "r": float(r),
            "mfe_r": o.get("mfe_r"),
            "mae_r": o.get("mae_r"),
            "source": o.get("source") or "unknown",
            "hold_sec": o.get("hold_sec"),
        })
    return out


def load_admits() -> dict[tuple, float]:
    """(symbol, day) -> earliest admit_ts."""
    out: dict[tuple, float] = {}
    for ln in open(os.path.join(REPORTS, "shadow.jsonl"),
                   encoding="utf-8", errors="replace"):
        ln = ln.strip()
        if not ln:
            continue
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        a, s, ts = r.get("admit_ts"), r.get("symbol"), r.get("ts")
        if not a or not s or not ts:
            continue
        k = (str(s).upper(), DS._day_of(float(ts)))
        a = float(a)
        if k not in out or a < out[k]:
            out[k] = a
    return out


def anchor_metrics(bars: list[dict], day: str, anchor: float,
                   pclose: float) -> tuple[float | None, float | None]:
    """(before %, captured) at *anchor*, both from the RTH path."""
    path = [b for b in bars if b["day"] == day and DS._in_rth(b)]
    at = [b for b in path if b["t"] >= anchor]
    if len(path) < 5 or not at or pclose <= 0:
        return None, None
    px = at[0]["o"]
    if px <= 0:
        return None, None
    before = 100.0 * (px - pclose) / pclose
    captured = None
    if before > 0:
        best = max(b["h"] for b in at)
        span = max(best, px) - pclose
        captured = ((px - pclose) / span) if span > 0 else None
    return before, captured


# ---------------------------------------------------------------- reporting

def table(rows: list[dict], key: str, label: str, nbuckets: int = 4) -> None:
    vals = [(r[key], r) for r in rows if r.get(key) is not None]
    if len(vals) < nbuckets * 5:
        print(f"  {label}: n={len(vals)} — too thin to bucket\n")
        return
    vals.sort(key=lambda t: t[0])
    size = len(vals) // nbuckets
    print(f"  {label}")
    print(f"    {'bucket':<20}{'n':>5}{'medR':>9}{'meanR':>9}{'win%':>7}"
          f"{'medMFEr':>9}{'sessions +':>12}")
    for i in range(nbuckets):
        chunk = vals[i * size:(i + 1) * size] if i < nbuckets - 1 else vals[i * size:]
        rs = [c[1] for c in chunk]
        lo, hi = chunk[0][0], chunk[-1][0]
        byday = defaultdict(list)
        for r in rs:
            byday[r["day"]].append(r["r"])
        green = sum(1 for d in byday if statistics.median(byday[d]) > 0)
        mfe = [float(r["mfe_r"]) for r in rs if r.get("mfe_r") is not None]
        print(f"    {f'{lo:.2f}..{hi:.2f}':<20}{len(rs):>5}"
              f"{statistics.median(r['r'] for r in rs):>9.3f}"
              f"{statistics.mean(r['r'] for r in rs):>9.3f}"
              f"{100.0 * sum(1 for r in rs if r['r'] > 0) / len(rs):>7.0f}"
              f"{(statistics.median(mfe) if mfe else float('nan')):>9.3f}"
              f"{green}/{len(byday):<10}")
    print()


def paired_sessions(rows: list[dict], key: str, label: str) -> None:
    """Within each session, does the FRESH half beat the LATE half?

    Splitting inside the day removes the day effect, which is the only
    thing twelve sessions of one August tape can be trusted to carry.
    """
    byday = defaultdict(list)
    for r in rows:
        if r.get(key) is not None:
            byday[r["day"]].append(r)
    wins = n = 0
    deltas = []
    detail = []
    for d in sorted(byday):
        rs = sorted(byday[d], key=lambda r: r[key])
        if len(rs) < 6:
            continue
        h = len(rs) // 2
        lo = statistics.median(r["r"] for r in rs[:h])     # fresher / less run-up
        hi = statistics.median(r["r"] for r in rs[-h:])    # later / more run-up
        n += 1
        deltas.append(lo - hi)
        if lo > hi:
            wins += 1
        detail.append((d, len(rs), lo, hi))
    if not n:
        print(f"  {label}: no session had 6+ trades\n")
        return
    print(f"  {label} — fresh half minus late half, by session")
    for d, k, lo, hi in detail:
        print(f"    {d}  n={k:<4} fresh {lo:>7.3f}   late {hi:>7.3f}   "
              f"delta {lo - hi:>+7.3f}")
    print(f"    fresh half wins {wins}/{n} sessions  (sign p={binom_p(wins, n):.3f})"
          f"   median delta {statistics.median(deltas):+.3f} R\n")


def main() -> int:
    fills = load_fills()
    admits = load_admits()
    days = sorted({f["day"] for f in fills})
    print(f"fills with realized R: {len(fills)}   sessions: {len(days)}   "
          f"({days[0]}..{days[-1]})")

    syms = sorted({f["symbol"] for f in fills})
    from datetime import datetime, timedelta, timezone
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=DAYS + 10)
    minutes = DS.fetch_minutes(syms, start, end)
    if not minutes:
        print("no data client — run on the mini with .venv")
        return 0
    daily = AL.fetch_daily(syms, start.date().isoformat(), end.date().isoformat())
    print(f"bars {len(minutes)}/{len(syms)}  daily {len(daily)}/{len(syms)}")

    rows = []
    no_admit = no_bars = no_pc = 0
    for f in fills:
        b, d = minutes.get(f["symbol"]), daily.get(f["symbol"])
        if not b or not d:
            no_bars += 1
            continue
        pc = AL.prior_close(d, f["day"])
        if not pc:
            no_pc += 1
            continue
        a = admits.get((f["symbol"], f["day"]))
        if a:
            f["before_admit"], f["captured_admit"] = anchor_metrics(b, f["day"], a, pc)
        else:
            no_admit += 1
            f["before_admit"] = f["captured_admit"] = None
        f["before_fill"], f["captured_fill"] = anchor_metrics(
            b, f["day"], f["entry_time"], pc)
        rows.append(f)
    print(f"resolved {len(rows)}  (dropped: {no_bars} no bars, "
          f"{no_pc} no prior close; {no_admit} without admit_ts)\n")

    print("=" * 74)
    print("RANK CORRELATION vs realized R   (negative = later is worse)")
    print("=" * 74)
    print(f"  {'instrument':<26}{'n':>5}{'rho':>9}{'sigma':>9}   note")
    for key, note in (
        ("before_admit", "clean — no shared term with R"),
        ("before_fill", "clean"),
        ("captured_admit", "COUPLED — shares the day high with R"),
        ("captured_fill", "COUPLED"),
    ):
        pairs = [(r[key], r["r"]) for r in rows if r.get(key) is not None]
        if len(pairs) < 10:
            print(f"  {key:<26}{len(pairs):>5}   too thin")
            continue
        rho, t, n = spearman([p[0] for p in pairs], [p[1] for p in pairs])
        print(f"  {key:<26}{n:>5}{rho:>9.3f}{t:>9.2f}   {note}")
    print()

    print("=" * 74)
    print("BUCKETED OUTCOMES")
    print("=" * 74)
    table(rows, "before_admit", "pre-admission run-up % (low = fresh)")
    table(rows, "captured_admit", "captured at admission (low = fresh)")

    print("=" * 74)
    print("SESSION-PAIRED (the unit of independence)")
    print("=" * 74)
    paired_sessions(rows, "before_admit", "pre-admission run-up")
    paired_sessions(rows, "captured_admit", "captured at admission")

    print("=" * 74)
    print("WITHIN SOURCE — does freshness pay holding the seed path fixed?")
    print("=" * 74)
    bysrc = defaultdict(list)
    for r in rows:
        bysrc[str(r["source"])].append(r)
    for src in sorted(bysrc, key=lambda s: -len(bysrc[s])):
        rs = bysrc[src]
        if len(rs) < 30:
            continue
        pairs = [(r["before_admit"], r["r"]) for r in rs
                 if r.get("before_admit") is not None]
        if len(pairs) < 20:
            continue
        rho, t, n = spearman([p[0] for p in pairs], [p[1] for p in pairs])
        cap = [r["captured_admit"] for r in rs if r.get("captured_admit") is not None]
        print(f"\n{src}  n={len(rs)}  medR={statistics.median(r['r'] for r in rs):+.3f}"
              f"  med captured={statistics.median(cap) if cap else float('nan'):.3f}")
        print(f"  run-up vs R:  rho={rho:+.3f}  sigma={t:+.2f}  (n={n})")
        table(rs, "before_admit", f"  {src}: run-up buckets", nbuckets=3)

    print("=" * 74)
    print("A negative rho on `before_*` means the desk is paid for arriving")
    print("earlier. A null there, with `captured` also null despite its")
    print("mechanical advantage, means lateness is not what costs the money.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
