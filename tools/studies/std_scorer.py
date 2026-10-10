"""std_scorer.py — the shared, non-anticipating scorer for studies (Data Audit 2026-10-10; ROOM_HOD_OOS_2026-10-09.md).

Rules it enforces:
  1. Every DECISION gets equal weight inside a day. Never average within a name-day first: when a name can enter and leave
     the cell during the day, the number of decisions depends on the future path (the room-HOD artifact).
  2. Days are the unit for the t (decisions on one day share the market). The t is conservative: the larger of the plain and
     Newey-West (lag 5) standard errors over the day series.
  3. A day is never dropped because of an outcome. Variant comparisons pair the SAME entries; unmatched entries are reported,
     and dollar totals count a day with no trades as $0.
  4. preflight() runs before any scoring: raw-vs-adjusted price ratio, dropped names by reason, rate-limit errors, coverage.

Functions take plain dicts so any study can use them:
  decision = {"day": "2026-10-09", "name": "AAPL", "ts": 1791..., "hour": 11, "ret_bp": 12.3}
"""
from __future__ import annotations

import math
from collections import defaultdict


def nw_se(xs: list[float], lag: int = 5) -> float:
    n = len(xs)
    m = sum(xs) / n
    u = [x - m for x in xs]
    g0 = sum(v * v for v in u) / n
    s = g0
    for k in range(1, min(lag, n - 1) + 1):
        gk = sum(u[i] * u[i - k] for i in range(k, n)) / n
        s += 2 * (1 - k / (lag + 1)) * gk
    return math.sqrt(max(s, 0.0) / n)


def day_stat(values: list[float], lag: int = 5) -> dict:
    """Mean of a per-day series with a conservative SE (max of plain and Newey-West)."""
    xs = [v for v in values if v is not None and math.isfinite(v)]
    n = len(xs)
    if n < 3:
        return {"n": n, "mean": (sum(xs) / n if n else float("nan")), "se": float("nan"), "t": float("nan")}
    m = sum(xs) / n
    plain = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1)) / math.sqrt(n)
    se = max(plain, nw_se(xs, lag))
    return {"n": n, "mean": m, "se": se, "t": (m / se if se > 0 else float("nan"))}


def cell_vs_control(decisions: list[dict], in_cell, cost_bp: float = 0.0) -> dict:
    """Cell decisions vs same-day, same-hour control decisions from OTHER names, every decision weighted equally.
    Per day: mean over that day's cell decisions of (ret - cost - mean control ret - cost at its day-hour, other names).
    -> {"days": {day: value}, "stat": day_stat, "n_cell": ..., "abs": day_stat of cell net}"""
    ctl = defaultdict(lambda: defaultdict(list))            # (day, hour) -> name -> [ret]
    for d in decisions:
        if not in_cell(d):
            ctl[(d["day"], d["hour"])][d["name"]].append(d["ret_bp"])
    tot = {k: (sum(sum(v) for v in names.values()), sum(len(v) for v in names.values())) for k, names in ctl.items()}
    per_day, per_day_abs = defaultdict(list), defaultdict(list)
    for d in decisions:
        if not in_cell(d):
            continue
        k = (d["day"], d["hour"])
        s, n = tot.get(k, (0.0, 0))
        own = ctl.get(k, {}).get(d["name"], [])
        s, n = s - sum(own), n - len(own)
        per_day_abs[d["day"]].append(d["ret_bp"] - cost_bp)
        if n > 0:
            per_day[d["day"]].append(d["ret_bp"] - s / n)
    days = {k: sum(v) / len(v) for k, v in per_day.items()}
    return {"days": days, "stat": day_stat([days[k] for k in sorted(days)]),
            "abs": day_stat([sum(v) / len(v) for k, v in sorted(per_day_abs.items())]),
            "n_cell": sum(len(v) for v in per_day_abs.values())}


def paired_variants(ref: list[dict], var: list[dict], all_days: list[str], match_sec: float = 5.0) -> dict:
    """Same-entry comparison of two replay variants. Trades: {"day", "symbol", "entry_ts", "net_bp"}.
    Matched = same day and symbol, entry within match_sec. Per day: mean (var - ref) over matched entries.
    Dollars: per day total net at $1k per trade for each variant, a day with no trades = $0 (never dropped)."""
    by = defaultdict(list)
    for t in ref:
        by[(t["day"], t["symbol"])].append(t)
    used, diffs, unmatched_var = set(), defaultdict(list), 0
    for t in var:
        best = None
        for j, r in enumerate(by.get((t["day"], t["symbol"]), [])):
            key = (t["day"], t["symbol"], j)
            if key in used or abs(r["entry_ts"] - t["entry_ts"]) > match_sec:
                continue
            best = (key, r)
            break
        if best is None:
            unmatched_var += 1
            continue
        used.add(best[0])
        diffs[t["day"]].append(t["net_bp"] - best[1]["net_bp"])
    dollars = {d: (sum(t["net_bp"] for t in var if t["day"] == d) - sum(t["net_bp"] for t in ref if t["day"] == d)) / 1e4 * 1000
               for d in all_days}
    days = {d: sum(v) / len(v) for d, v in diffs.items()}
    return {"paired_days": days, "paired": day_stat([days[d] for d in sorted(days)]),
            "dollars_days": dollars, "dollars": day_stat([dollars[d] for d in all_days]),
            "matched": sum(len(v) for v in diffs.values()), "unmatched_var": unmatched_var,
            "unmatched_ref": len(ref) - len(used), "days_without_matches": [d for d in all_days if d not in days]}


def preflight(rows: list[dict], *, raw_key: str | None = None, adj_key: str | None = None, floor: float | None = None,
              fetch_errors: int = 0, rate_limited: int = 0, expected_days: list[str] | None = None,
              drops: dict | None = None) -> dict:
    """Run before scoring. rows: one per name-day (or decision) with prices. Returns a report and a list of problems;
    any problem means the study should stop (or state it) before scoring."""
    problems, rep = [], {"rows": len(rows), "fetch_errors": fetch_errors, "rate_limited": rate_limited, "drops": drops or {}}
    if rate_limited:
        problems.append(f"{rate_limited} rate-limited requests: names may be silently missing")
    if raw_key and adj_key:
        ratios = [r[adj_key] / r[raw_key] for r in rows if r.get(raw_key) and r.get(adj_key)]
        off = sum(1 for x in ratios if x > 1.05 or x < 0.95)
        rep["adj_raw_ratio_off"] = off
        if off:
            problems.append(f"{off} rows where adjusted and raw prices differ by > 5% (splits): apply price floors to RAW prices")
        if floor is not None:
            below = sum(1 for r in rows if r.get(raw_key) is not None and r[raw_key] < floor
                        and r.get(adj_key) is not None and r[adj_key] >= floor)
            rep["raw_below_floor_adj_above"] = below
            if below:
                problems.append(f"{below} rows pass the ${floor} floor only on ADJUSTED prices")
    if expected_days is not None:
        seen = {r.get("day") for r in rows}
        missing = [d for d in expected_days if d not in seen]
        rep["missing_days"] = missing
        if missing:
            problems.append(f"{len(missing)} expected days have no rows")
    if not rows:
        problems.append("no rows: vacuous run")
    rep["problems"] = problems
    return rep
