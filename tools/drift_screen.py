#!/usr/bin/env python3
"""Does a universe drift, or does it only wiggle?

Before hunting for an entry gate it is worth knowing whether one *can*
exist. A gate selects trades; it cannot create direction. If favorable and
adverse travel are symmetric after entry, the tape is a driftless walk and
no gate — RSI, %R, heat, RVOL, or anything else — will produce a positive
expectancy from it. The exit geometry (the 0.10R ratchet) is downstream of
that question and cannot answer it.

The measurement, per (universe, horizon):

    MFE = max(high) - entry      over the horizon, % of entry
    MAE = entry - min(low)       over the horizon, % of entry
    net = close(end) - entry

Under the null — a driftless walk — MFE and MAE are drawn from the same
distribution, so MFE/MAE = 1 and median net = 0. Drift shows up as
MFE - MAE > 0. No costs are charged here on purpose: this measures the
field, not a strategy. Costs decide whether a *found* edge is tradable,
and 2026-08 measured them at ~0.287% of price round trip.

Independence discipline matches the rest of the lab. Samples inside one
name-day overlap heavily, so the default stride is the horizon itself
(non-overlapping windows) and the unit of independence is the **session**,
never the sample. n is reported but never carries the verdict alone.

Universes resolve per-day from what was knowable that day:

    shadow:all / shadow:momentum / shadow:trending / shadow:research
        names the desk actually watched that session, from
        ai_reports/shadow.jsonl, optionally from that name's admit_ts
        onward (--eligible-within), which is the same discipline
        tools/desk_null.py uses.
    rs        rs_ratings.json — HINDSIGHT, its as_of is a single date
    liquid    the megacap list from tools/h4_screen.py

Read-only. Writes nothing but its own screen JSON. Usage (mini, venv):

    .venv/bin/python tools/drift_screen.py
    .venv/bin/python tools/drift_screen.py --universe shadow:momentum
    .venv/bin/python tools/drift_screen.py --horizons 5,15,30,60 --days 20
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from math import comb
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from ai_paths import resolve_report_dir  # noqa: E402

SCREEN_DIR = Path(ROOT) / "ai_reports" / "screens"

# RTH in UTC. August is EDT (UTC-4); the screen refuses to run outside DST
# rather than silently sampling the wrong window.
RTH_START_UTC = (13, 30)
RTH_END_UTC = (19, 50)  # 15:50 ET, the desk's flatten

RESEARCH_SOURCES = {"xai", "anthropic"}

# Verdict gates. Deliberately the same shape as desk_null so a PASS here
# cannot be softer than a PASS anywhere else in the lab.
MIN_N = 30
MIN_SESSIONS = 5
MAX_SESSION_SHARE = 0.50
MIN_SIGMA = 2.0
MAX_SIGN_P = 0.05


def _sign_p(green: int, n: int) -> float:
    """One-sided binomial tail, P(X >= green | p=0.5)."""
    if n <= 0:
        return 1.0
    return sum(comb(n, i) for i in range(green, n + 1)) / (2.0 ** n)


def _day_of(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%d")


def select_symbols(syms: list[str], limit: int, *, seed: int = 20260822) -> list[str]:
    """Cap the fetch without letting the alphabet choose the sample.

    ``sorted(syms)[:limit]`` looks harmless and is not: on 2026-08-22 it cut
    a 314-name universe at KTOS and silently dropped 164 names — 52% of it —
    so every screen ran on A-K only. TEM, the name that prompted the
    latency-chain work, was never in the sample.

    A seeded sample is representative and still reproducible; the warning
    fires whenever the cap actually bites, because a screen quietly reading
    half a universe is worse than one that refuses to run.
    """
    uniq = sorted(set(syms))
    if limit <= 0 or len(uniq) <= limit:
        return uniq
    import random
    picked = sorted(random.Random(seed).sample(uniq, limit))
    print(f"  NOTE: universe is {len(uniq)} symbols, capped at {limit} — "
          f"sampling {limit} at random (seed {seed}), not the first {limit} "
          f"alphabetically. Raise --limit-symbols to screen all of them.")
    return picked


def load_shadow_universe(days: int, want_source: str | None) -> dict[str, dict[str, float]]:
    """day -> {symbol: earliest admit ts}. Empty when the log is absent."""
    path = Path(resolve_report_dir()) / "shadow.jsonl"
    if not path.exists():
        return {}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days + 5)).timestamp()
    out: dict[str, dict[str, float]] = defaultdict(dict)
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            ts = r.get("ts")
            sym = r.get("symbol") or r.get("ticker")
            if not ts or not sym:
                continue
            try:
                ts = float(ts)
            except (TypeError, ValueError):
                continue
            if ts < cutoff:
                continue
            src = str(r.get("source") or "").lower()
            if want_source and want_source != "all":
                if want_source == "research":
                    if src not in RESEARCH_SOURCES:
                        continue
                elif src != want_source:
                    continue
            day = _day_of(ts)
            sym = str(sym).upper()
            admit = r.get("admit_ts")
            try:
                admit = float(admit) if admit else ts
            except (TypeError, ValueError):
                admit = ts
            prev = out[day].get(sym)
            out[day][sym] = admit if prev is None else min(prev, admit)
    return dict(out)


def load_file_universe(kind: str, days: int) -> dict[str, dict[str, float]]:
    """Static lists (rs, liquid) replayed across the recent sessions."""
    if kind == "liquid":
        import h4_screen
        syms = list(h4_screen.LIQUID_FALLBACK)
    elif kind == "rs":
        import desk_h4
        rows = desk_h4.load_rs_rows(Path(ROOT) / "rs_ratings.json")
        syms = [str(r.get("ticker") or r.get("symbol") or "").upper()
                for r in desk_h4.filter_universe(rows, {})]
        syms = [s for s in syms if s]
    else:
        return {}
    if not syms:
        return {}
    end = datetime.now(timezone.utc).date()
    out: dict[str, dict[str, float]] = {}
    d = end - timedelta(days=days)
    while d <= end:
        if d.weekday() < 5:
            out[d.isoformat()] = {s: 0.0 for s in syms}
        d += timedelta(days=1)
    return out


def fetch_minutes(symbols: list[str], start: datetime, end: datetime) -> dict[str, list[dict]]:
    """symbol -> oldest-first minute bars. Empty dict without a data client."""
    try:
        import alpaca_api as aa
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        from config import load_config
        cfg = load_config()
        client = aa.connect_data_client(cfg)
        if client is None:
            return {}
        feed_kw = aa._get_feed_arg(cfg)
    except Exception:
        return {}
    out: dict[str, list[dict]] = {}
    chunk = 20
    for i in range(0, len(symbols), chunk):
        batch = symbols[i:i + chunk]
        try:
            req = StockBarsRequest(
                symbol_or_symbols=batch, timeframe=TimeFrame.Minute,
                start=start, end=end, adjustment="split", **feed_kw)
            data = getattr(client.get_stock_bars(req), "data", None) or {}
        except Exception as e:  # noqa: BLE001
            print(f"  fetch failed for {batch[:3]}...: {type(e).__name__}")
            continue
        for sym, bars in data.items():
            rows = out.setdefault(str(sym).upper(), [])
            for b in bars or []:
                ts = getattr(b, "timestamp", None)
                if ts is None:
                    continue
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                rows.append({
                    "t": ts.timestamp(),
                    "day": ts.astimezone(timezone.utc).strftime("%Y-%m-%d"),
                    "hm": (ts.astimezone(timezone.utc).hour,
                           ts.astimezone(timezone.utc).minute),
                    "h": float(b.high), "l": float(b.low), "c": float(b.close),
                    "o": float(b.open),
                })
    for rows in out.values():
        rows.sort(key=lambda r: r["t"])
    return out


def _in_rth(bar: dict) -> bool:
    return RTH_START_UTC <= bar["hm"] <= RTH_END_UTC


def sample_excursions(bars: list[dict], day: str, horizon: int, stride: int,
                      after_ts: float, rth_only: bool) -> list[dict]:
    """Non-overlapping (by default) windows on one name-day."""
    path = [b for b in bars if b["day"] == day and b["t"] >= after_ts]
    if rth_only:
        path = [b for b in path if _in_rth(b)]
    out = []
    i = 0
    while i + horizon <= len(path):
        w = path[i:i + horizon]
        entry = w[0]["o"]
        if entry <= 0:
            i += stride
            continue
        out.append({
            "day": day,
            "mfe": 100.0 * (max(b["h"] for b in w) - entry) / entry,
            "mae": 100.0 * (entry - min(b["l"] for b in w)) / entry,
            "net": 100.0 * (w[-1]["c"] - entry) / entry,
        })
        i += stride
    return out


def score(rows: list[dict]) -> dict:
    """Verdict on drift. The session, not the sample, is the unit."""
    if not rows:
        return {"verdict": "EMPTY", "n": 0}
    diff = [r["mfe"] - r["mae"] for r in rows]
    net = [r["net"] for r in rows]
    n = len(rows)
    by_day: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        by_day[r["day"]].append(r["net"])
    sessions = len(by_day)
    top_share = max(len(v) for v in by_day.values()) / n if n else 1.0
    green = sum(1 for v in by_day.values() if statistics.fmean(v) > 0)
    mu = statistics.fmean(diff)
    sd = statistics.pstdev(diff) if n > 1 else 0.0
    se = sd / (n ** 0.5) if n > 1 and sd else 0.0
    sigma = mu / se if se else 0.0
    p = _sign_p(green, sessions)
    med_mfe = statistics.median(r["mfe"] for r in rows)
    med_mae = statistics.median(r["mae"] for r in rows)

    fails = []
    if n < MIN_N:
        fails.append(f"n<{MIN_N}")
    if sessions < MIN_SESSIONS:
        fails.append(f"sessions<{MIN_SESSIONS}")
    if top_share > MAX_SESSION_SHARE:
        fails.append(f"one session {top_share:.0%} of n")
    if mu <= 0:
        fails.append("MFE<=MAE")
    elif sigma < MIN_SIGMA:
        fails.append(f"{sigma:.1f}sigma<{MIN_SIGMA}")
    if p > MAX_SIGN_P:
        fails.append(f"session sign p={p:.2f}")

    if not fails:
        verdict = "DRIFT"
    elif mu <= 0:
        verdict = "NO_DRIFT"
    elif n < MIN_N or sessions < MIN_SESSIONS:
        verdict = "UNDERPOWERED"
    else:
        verdict = "FAIL"
    return {
        "verdict": verdict, "n": n, "sessions": sessions,
        "median_mfe": med_mfe, "median_mae": med_mae,
        "mfe_over_mae": (med_mfe / med_mae) if med_mae else None,
        "mean_mfe_minus_mae": mu, "sigma": sigma,
        "median_net": statistics.median(net),
        "sessions_green": green, "sign_p": p,
        "max_session_share": top_share,
        "why": "; ".join(fails) or "all gates clear",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--universe", default="shadow:all",
                    help="shadow:all|shadow:momentum|shadow:trending|"
                         "shadow:research|rs|liquid, or a comma list")
    ap.add_argument("--days", type=int, default=20)
    ap.add_argument("--horizons", default="5,15,30,60,120",
                    help="minutes, comma separated")
    ap.add_argument("--stride", type=int, default=0,
                    help="minutes between samples; 0 = horizon "
                         "(non-overlapping, the honest default)")
    ap.add_argument("--eligible-within", action="store_true",
                    help="only sample at/after that name's admit_ts")
    ap.add_argument("--include-premarket", action="store_true")
    ap.add_argument("--limit-symbols", type=int, default=400,
                    help="0 = no cap; over the cap a seeded random sample "
                         "is taken, never the alphabetical head")
    args = ap.parse_args()

    horizons = [int(x) for x in args.horizons.split(",") if x.strip()]
    universes = [u.strip() for u in args.universe.split(",") if u.strip()]
    rth_only = not args.include_premarket

    plans: dict[str, dict[str, dict[str, float]]] = {}
    for u in universes:
        if u.startswith("shadow:"):
            plans[u] = load_shadow_universe(args.days, u.split(":", 1)[1])
        else:
            plans[u] = load_file_universe(u, args.days)
        if not plans[u]:
            print(f"  {u}: no universe resolved (log or file missing)")

    syms = select_symbols(
        [s for p in plans.values() for d in p.values() for s in d],
        args.limit_symbols)
    if not syms:
        print("nothing to screen")
        return 0
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days + 5)
    print(f"drift screen  universes={universes}  horizons={horizons}min  "
          f"stride={'horizon' if not args.stride else args.stride}  "
          f"symbols={len(syms)}  window={start.date()}..{end.date()}")
    print(f"  RTH only: {rth_only}   eligible-within: {args.eligible_within}")
    bars = fetch_minutes(syms, start, end)
    if not bars:
        print("  no Alpaca data client — run on the mini with .venv.")
        return 0
    print(f"  bars for {len(bars)}/{len(syms)} symbols\n")

    hdr = (f"{'universe':<20}{'horiz':>6}{'n':>7}{'sess':>6}{'medMFE':>9}"
           f"{'medMAE':>9}{'MFE/MAE':>9}{'MFE-MAE':>9}{'sigma':>7}"
           f"{'medNet':>9}{'green':>7}{'verdict':>14}")
    print(hdr)
    print("-" * len(hdr))
    payload = {}
    for u in universes:
        plan = plans.get(u) or {}
        if not plan:
            continue
        for hz in horizons:
            stride = args.stride or hz
            rows = []
            for day, members in plan.items():
                for sym, admit in members.items():
                    b = bars.get(sym)
                    if not b:
                        continue
                    after = admit if (args.eligible_within and admit) else 0.0
                    rows.extend(sample_excursions(b, day, hz, stride, after,
                                                  rth_only))
            s = score(rows)
            payload[f"{u}@{hz}m"] = s
            if s["verdict"] == "EMPTY":
                print(f"{u:<20}{hz:>6}{0:>7}   (no samples)")
                continue
            print(f"{u:<20}{hz:>6}{s['n']:>7}{s['sessions']:>6}"
                  f"{s['median_mfe']:>9.3f}{s['median_mae']:>9.3f}"
                  f"{(s['mfe_over_mae'] or 0):>9.2f}{s['mean_mfe_minus_mae']:>9.3f}"
                  f"{s['sigma']:>7.2f}{s['median_net']:>9.3f}"
                  f"{s['sessions_green']}/{s['sessions']:<5}"
                  f"{s['verdict']:>14}")
    print("\nnull: a driftless walk gives MFE/MAE = 1.00 and median net = 0.")
    print("DRIFT is permission to search for an entry gate on that universe.")
    print("It is not a strategy, and no cost is charged here "
          "(2026-08 round trip ~0.287% of price).")

    SCREEN_DIR.mkdir(parents=True, exist_ok=True)
    day = datetime.now().strftime("%Y-%m-%d")
    outp = SCREEN_DIR / f"drift_{day}.json"
    outp.write_text(json.dumps({
        "day": day, "universes": universes, "horizons": horizons,
        "stride": args.stride or "horizon", "days": args.days,
        "rth_only": rth_only, "eligible_within": args.eligible_within,
        "results": payload,
    }, indent=2, default=str), encoding="utf-8")
    print(f"wrote {outp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
