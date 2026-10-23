#!/usr/bin/env python3
"""Short squeeze as a forced-buyer flow. Spec: docs/studies/short_squeeze_prereg.json (as amended).

A HIGH days-to-cover (top decile of FINRA's PUBLISHED report) stock that breaks out (+3%, 2x the 20-day median volume,
a 20-day high close) is bought at the NEXT day's close and held 5 trading days; vs NORMAL-DTC (< 70th pct) breakouts
matched on week, 60-day volatility tercile and 20-day dollar-volume tercile. Beta-adjusted returns; 10 bp on HIGH only.

  --coverage   per FINRA file: rows by marketClassCode, revision / split flags (format only) -> coverage.json
  --bars       Alpaca daily bars (data-only account), adjustment=all and raw, for the in-scope symbols + SPY
  (default)    count step: reports, publication dates, eligible universe, events per group / half, drops, own-DTC
               correlation, and the PRIMARY series' SEs and MDE (no mean is printed) -> count.json
  --score      verdict; refuses without --skeptic-ready and a count step from this code that cleared the power bar
"""
from __future__ import annotations

import argparse
import bisect
import csv
import glob
import hashlib
import json
import math
import os
import pickle
import random
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("EEH_ROOT") or os.path.dirname(os.path.dirname(HERE))
SI_DIR = os.path.join(ROOT, "data", "finra_short_interest")
OUT_DIR = os.path.join(ROOT, "ai_reports", "short_squeeze")
PUB_LAG, PUB_LAG_INFO = 10, 12
MIN_PX, MIN_DV = 20.0, 20e6
HIGH_PCT, NORMAL_PCT = 0.90, 0.70
BRK_RET, BRK_VOL_X, LOOK = 0.03, 2.0, 20
BETA_DAYS, HOLD = 60, 5
COST, COST_HI = 0.0010, 0.0030
LIFT_BAR, T_PASS, MIN_EVENTS = 0.0050, 2.0, 150
H1_END = "2022-12-31"
FIRST_TRIGGER = "2020-02-01"
ETF_WORDS = ("ETF", "FUND", "TRUST", "ISHARES", "SPDR", "PROSHARES", "DIREXION", "ETN", "INDEX", "PORTFOLIO")
DROP_MAX, DROP_DIFF = 0.02, 0.01


def P(*a):
    print(*a, flush=True)


def write(name: str, obj) -> str:
    os.makedirs(OUT_DIR, exist_ok=True)
    p = os.path.join(OUT_DIR, name)
    json.dump(obj, open(p + ".tmp", "w"), indent=1, default=str)
    os.replace(p + ".tmp", p)
    return p


def jload(name: str, default=None):
    p = os.path.join(OUT_DIR, name)
    return json.load(open(p)) if os.path.exists(p) else default


# ── FINRA files (pure parsing) ───────────────────────────────────────────────

def read_si(path: str) -> list[dict]:
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f, delimiter="|"):
            rows.append(r)
    return rows


def fnum(x) -> float | None:
    try:
        return float(x) if x not in (None, "") else None
    except ValueError:
        return None


def is_etf_like(name: str) -> bool:
    """Whole-word match on the issue name (so 'Trustmark' or 'Fundamental' are not caught)."""
    words = set("".join(ch if ch.isalnum() else " " for ch in (name or "").upper()).split())
    return any(w in words for w in ETF_WORDS)


def coverage() -> dict:
    out = {}
    for p in sorted(glob.glob(os.path.join(SI_DIR, "shrt*.csv"))):
        rows = read_si(p)
        out[os.path.basename(p)[4:12]] = {"rows": len(rows), "by_code": dict(Counter(r["marketClassCode"] for r in rows)),
                                          "revision": sum(1 for r in rows if r.get("revisionFlag")),
                                          "split": sum(1 for r in rows if r.get("stockSplitFlag"))}
    return out


# ── trading calendar and bars (pure helpers) ─────────────────────────────────

def add_trading_days(days: list[str], d: str, n: int) -> str | None:
    """The trading day n sessions after the first session on or after d."""
    i = bisect.bisect_left(days, d)
    j = i + n
    return days[j] if 0 <= j < len(days) else None


def beta(rn: list[float], rm: list[float]) -> float | None:
    if len(rn) < 20 or len(rn) != len(rm):
        return None
    mm = statistics.mean(rm)
    vm = sum((x - mm) ** 2 for x in rm)
    if vm <= 0:
        return None
    mn = statistics.mean(rn)
    return sum((a - mn) * (b - mm) for a, b in zip(rn, rm)) / vm


def is_breakout(closes: list[float], vols: list[float], i: int) -> bool:
    """Day i breaks out: close +3% vs i-1, volume >= 2 x median of i-20..i-1, close >= max close of i-20..i-1."""
    if i < LOOK or closes[i - 1] <= 0:
        return False
    prior_c, prior_v = closes[i - LOOK:i], vols[i - LOOK:i]
    return (closes[i] / closes[i - 1] - 1 >= BRK_RET and vols[i] >= BRK_VOL_X * statistics.median(prior_v)
            and closes[i] >= max(prior_c))


# ── statistics (pure) ────────────────────────────────────────────────────────

def t_ppf(p: float, df: int) -> float:
    sys.path.insert(0, HERE)
    import indicator_confirm_study as S
    return S.t_ppf(p, df)


def clustered_se(vals: list[float], keys: list) -> float | None:
    """CR1 SE of the mean of vals clustered by keys."""
    n = len(vals)
    if n < 2:
        return None
    mu = sum(vals) / n
    g = defaultdict(float)
    for v, k in zip(vals, keys):
        g[k] += v - mu
    G = len(g)
    if G < 2:
        return None
    return math.sqrt(G / (G - 1) * sum(x * x for x in g.values())) / n


def nw_se(series: list[float], lags: int = 5) -> float | None:
    """Newey-West SE of the mean of a time series (weekly calendar-time series here)."""
    n = len(series)
    if n < lags + 2:
        return None
    mu = sum(series) / n
    e = [x - mu for x in series]
    s = sum(x * x for x in e) / n
    for L in range(1, lags + 1):
        w = 1 - L / (lags + 1)
        s += 2 * w * sum(e[t] * e[t - L] for t in range(L, n)) / n
    return math.sqrt(max(s, 0.0) / n)


def week_of(d: str) -> str:
    y, w, _ = datetime.strptime(d, "%Y-%m-%d").isocalendar()
    return f"{y}-W{w:02d}"


def series_stats(ev: list[dict], key: str = "diff") -> dict:
    vals = [e[key] for e in ev if e.get(key) is not None]
    use = [e for e in ev if e.get(key) is not None]
    if len(vals) < 2:
        return {"n": len(vals), "mean": None, "se": None, "t": None, "ub": None}
    se_w = clustered_se(vals, [week_of(e["d0"]) for e in use])
    se_n = clustered_se(vals, [e["sym"] for e in use])
    wk = defaultdict(list)
    for e in use:
        wk[week_of(e["d0"])].append(e[key])
    se_c = nw_se([statistics.mean(wk[w]) for w in sorted(wk)])
    ses = [x for x in (se_w, se_n, se_c) if x]
    se = max(ses) if ses else None
    mu = sum(vals) / len(vals)
    return {"n": len(vals), "mean": mu, "se": se, "se_week": se_w, "se_name": se_n, "se_cal": se_c,
            "t": mu / se if se else None, "ub": mu + 1.96 * se if se else None}


def mde(st: dict) -> float | None:
    return 2.84 * st["se"] if st.get("se") else None


def half_verdict(st: dict, extra_ok: bool) -> str:
    if st["n"] < MIN_EVENTS or st["se"] is None:
        return "UNDERPOWERED"
    if mde(st) > LIFT_BAR:
        return "UNDERPOWERED"
    if st["t"] is not None and st["t"] <= -2:
        return "FAIL"
    if st["mean"] >= LIFT_BAR and st["t"] >= T_PASS and extra_ok:
        return "PASS"
    return "FAIL"                                   # amended (9): powered and not PASS = FAIL


def final(halves: list[str]) -> str:
    if all(h == "PASS" for h in halves):
        return "PASS (held-out 2017-2019 check and a forward prereg next)"
    if "UNDERPOWERED" in halves and "FAIL" not in halves:
        return "UNDERPOWERED"
    return "FAIL"


# ── events (pure on bars) ────────────────────────────────────────────────────

def build_reports(si_rows_by_date: dict, days: list[str]) -> list[dict]:
    """Per settlement date: publication = settlement + PUB_LAG trading days; rows with revision / split flags out."""
    reps = []
    for s, rows in sorted(si_rows_by_date.items()):
        sd = f"{s[:4]}-{s[4:6]}-{s[6:]}"
        pub = add_trading_days(days, sd, PUB_LAG)
        if pub is None:
            continue
        reps.append({"settle": sd, "pub": pub, "rows": rows})
    return reps


def eligible_at(bars_raw: dict, day_idx: dict, sym: str, d: str) -> bool:
    """$20 raw close and $20M mean 20-day dollar volume on the 20 sessions ending the session before d."""
    b = bars_raw.get(sym)
    if not b:
        return False
    i = bisect.bisect_left(b["d"], d) - 1
    if i < LOOK - 1:
        return False
    if b["c"][i] < MIN_PX:
        return False
    dv = [b["c"][k] * b["v"][k] for k in range(i - LOOK + 1, i + 1)]
    return statistics.mean(dv) >= MIN_DV


def pctile_cuts(xs: list[float]) -> tuple[float, float]:
    s = sorted(xs)

    def q(p):
        k = (len(s) - 1) * p
        lo = math.floor(k)
        return s[lo] + (s[min(lo + 1, len(s) - 1)] - s[lo]) * (k - lo)
    return q(NORMAL_PCT), q(HIGH_PCT)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--coverage", action="store_true")
    g.add_argument("--bars", action="store_true")
    g.add_argument("--score", action="store_true")
    ap.add_argument("--codes", default="", help="comma-separated marketClassCode list frozen from --coverage")
    ap.add_argument("--skeptic-ready", default="")
    a = ap.parse_args()
    if a.coverage:
        cov = coverage()
        P(json.dumps({k: v for k, v in list(cov.items())[:3]}, indent=1))
        P("codes over all files:", dict(sum((Counter(v["by_code"]) for v in cov.values()), Counter())))
        P(write("coverage.json", cov))
        return
    raise SystemExit("count / score modes are added after the coverage step freezes the code list (prereg amended (3))")


if __name__ == "__main__":
    main()
