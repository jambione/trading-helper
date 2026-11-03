#!/usr/bin/env python3
"""Forward-return scorer for the AI catalyst shadow logger.

Read-only except its own outputs under ``ai_reports/ai_catalyst/``
(``returns_cache.jsonl``, ``scorecard.md``). No orders, no desk state.

Pre-registered primary test (fixed 2026-09-26; do not change after data starts):

> **Primary slice:** both models agree, direction = up, and both materiality >= 4 (`agree_strong` & up).
>
> **Test:** after >= 40 trading days of logging, the primary slice must beat the equal-weight liquid-universe baseline (`base_ew`) at the **5-trading-day horizon (`h_d5`)**:
> - by **more than 50 bp net** of an assumed 5 bp round trip;
> - with a **date-clustered t >= 2**;
> - with the mean net excess **also above 50 bp in each half** of the period, split chronologically by scoring day.
>
> If all three hold, the verdict is GO. Otherwise it is NO-GO.
>
> Every other slice (the SPY comparison, the other horizons, single models, confidence buckets, down calls) is secondary. Secondary slices are reported but never used for the go/no-go. The bar, the slice and the horizon are fixed as of 2026-09-26 and must not be changed after data starts arriving.

Usage::

    .venv/bin/python tools/ai_catalyst_score.py [--asof YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import re
import statistics
import sys
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import bars
from bars import ET, day_of, fetch, fetch_many, index_at

import ai_paths

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HORIZONS = ("h_1550", "h_d1", "h_d5", "h_d10")
ROUND_TRIP = 0.0005  # 5 bp as a decimal return
ROUND_TRIP_BP = 5.0
PRIMARY_MIN_DAYS = 40
PRIMARY_MIN_NET_BP = 50.0
PRIMARY_MIN_T = 2.0
PRIMARY_HORIZON = "h_d5"
DAY_LOG_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.jsonl$")

PRIMARY_TEST_VERBATIM = (
    "> **Primary slice:** both models agree, direction = up, and both "
    "materiality >= 4 (`agree_strong` & up).\n"
    ">\n"
    "> **Test:** after >= 40 trading days of logging, the primary slice must "
    "beat the equal-weight liquid-universe baseline (`base_ew`) at the "
    "**5-trading-day horizon (`h_d5`)**:\n"
    "> - by **more than 50 bp net** of an assumed 5 bp round trip;\n"
    "> - with a **date-clustered t >= 2**;\n"
    "> - with the mean net excess **also above 50 bp in each half** of the "
    "period, split chronologically by scoring day.\n"
    ">\n"
    "> If all three hold, the verdict is GO. Otherwise it is NO-GO.\n"
    ">\n"
    "> Every other slice (the SPY comparison, the other horizons, single "
    "models, confidence buckets, down calls) is secondary. Secondary slices "
    "are reported but never used for the go/no-go. The bar, the slice and "
    "the horizon are fixed as of 2026-09-26 and must not be changed after "
    "data starts arriving."
)

CalendarFn = Callable[[str, str], list[str]]
MinuteFetchManyFn = Callable[..., None]
MinuteFetchFn = Callable[..., tuple]
DailyFetchFn = Callable[[list[str], str, str], dict[str, float]]


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def side_of(direction: str | None) -> int:
    d = (direction or "").strip().lower()
    if d == "up":
        return 1
    if d == "down":
        return -1
    return 0


def materiality_bucket(m: Any) -> str | None:
    try:
        v = int(m)
    except (TypeError, ValueError):
        return None
    if v in (1, 2):
        return "1-2"
    if v == 3:
        return "3"
    if v in (4, 5):
        return "4-5"
    return None


def confidence_bucket(c: Any) -> str | None:
    try:
        v = float(c)
    except (TypeError, ValueError):
        return None
    if v < 0.5:
        return "<0.5"
    if v < 0.7:
        return "0.5-0.7"
    return ">=0.7"


def agreement_bucket(a: dict | None, b: dict | None) -> str:
    """Classify a (symbol, run) pair from the two model rows (ok only)."""
    ok_a = a is not None and str(a.get("status") or "") == "ok"
    ok_b = b is not None and str(b.get("status") or "") == "ok"
    if ok_a and not ok_b:
        return "single"
    if ok_b and not ok_a:
        return "single"
    if not ok_a and not ok_b:
        return "single"
    da = str(a.get("direction") or "").lower()
    db = str(b.get("direction") or "").lower()
    if da != db:
        return "disagree"
    if da == "neutral":
        # Same neutral direction: weak agreement (not both non-neutral).
        return "agree_weak"
    try:
        ma = int(a.get("materiality"))
        mb = int(b.get("materiality"))
    except (TypeError, ValueError):
        return "agree_weak"
    if ma >= 4 and mb >= 4:
        return "agree_strong"
    return "agree_weak"


def rth_open_ts(day: str) -> float:
    return datetime.strptime(day, "%Y-%m-%d").replace(
        hour=9, minute=30, second=0, microsecond=0, tzinfo=ET,
    ).timestamp()


def et_hm_ts(day: str, hour: int, minute: int) -> float:
    return datetime.strptime(day, "%Y-%m-%d").replace(
        hour=hour, minute=minute, second=0, microsecond=0, tzinfo=ET,
    ).timestamp()


def entry_gate_ts(scored_ts: float, day: str) -> float:
    return max(float(scored_ts), rth_open_ts(day))


def first_bar_at_or_after(stamps: list[float] | None, t0: float) -> int:
    """Index of the first bar whose start is >= t0, or -1."""
    if not stamps:
        return -1
    i = bisect.bisect_left(stamps, t0)
    if i >= len(stamps):
        return -1
    return i


def bar_at_or_before(stamps: list[float] | None, t0: float) -> int:
    return index_at(stamps, t0) if stamps else -1


def pct_return(entry_px: float | None, exit_px: float | None) -> float | None:
    if entry_px is None or exit_px is None:
        return None
    if not entry_px:
        return None
    return (float(exit_px) - float(entry_px)) / float(entry_px)


def signed_excess(side: int, r_name: float | None, r_bench: float | None) -> float | None:
    if side == 0 or r_name is None or r_bench is None:
        return None
    return side * (r_name - r_bench)


def net_of(excess: float | None, round_trip: float = ROUND_TRIP) -> float | None:
    if excess is None:
        return None
    return excess - round_trip


def to_bp(x: float | None) -> float | None:
    if x is None:
        return None
    return float(x) * 10000.0


def date_clustered_stats(daily_nets: dict[str, float]) -> dict[str, Any]:
    """Average net within each day, then t = mean / (sd / sqrt(n_days))."""
    days = sorted(daily_nets)
    vals = [daily_nets[d] for d in days]
    n = len(vals)
    if n == 0:
        return {
            "n_days": 0, "mean": None, "mean_bp": None, "t": None,
            "hit_rate": None, "sd": None, "days": [],
        }
    mu = statistics.mean(vals)
    if n >= 2:
        sd = statistics.stdev(vals)
        t = mu / (sd / math.sqrt(n)) if sd > 0 else float("inf") if mu != 0 else 0.0
    else:
        sd = 0.0
        t = None
    hits = sum(1 for v in vals if v > 0)
    return {
        "n_days": n,
        "mean": mu,
        "mean_bp": to_bp(mu),
        "t": t,
        "hit_rate": hits / n,
        "sd": sd,
        "days": days,
    }


def half_split_days(days: list[str]) -> tuple[list[str], list[str]]:
    """Chronological half split of scoring days (first half, second half)."""
    ds = sorted(days)
    if not ds:
        return [], []
    mid = len(ds) // 2
    if mid == 0:
        return [], ds
    return ds[:mid], ds[mid:]


def obs_hit_rate(nets: list[float]) -> float | None:
    if not nets:
        return None
    return sum(1 for v in nets if v > 0) / len(nets)


# ---------------------------------------------------------------------------
# Universe / paths / I/O
# ---------------------------------------------------------------------------

def catalyst_dir(report_dir: Path | None = None) -> Path:
    base = Path(report_dir) if report_dir is not None else ai_paths.resolve_report_dir()
    return base / "ai_catalyst"


def load_universe(path: Path | None = None) -> tuple[list[str], list[str]]:
    p = Path(path) if path is not None else ROOT / "config" / "liquid_universe.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    etfs = [str(s).upper() for s in (data.get("etfs") or [])]
    etf_set = set(etfs)
    stocks = [
        str(s).upper() for s in (data.get("symbols") or [])
        if str(s).upper() not in etf_set
    ]
    return stocks, etfs


def iter_day_logs(cdir: Path) -> list[Path]:
    if not cdir.is_dir():
        return []
    out = []
    for p in sorted(cdir.iterdir()):
        if not p.is_file():
            continue
        if p.name in ("runs.jsonl", "returns_cache.jsonl", "scorecard.md"):
            continue
        if p.name.startswith("dryrun"):
            continue
        if DAY_LOG_RE.match(p.name):
            out.append(p)
    return out


def load_score_rows(cdir: Path, *, status_ok_only: bool = True) -> list[dict]:
    rows: list[dict] = []
    for path in iter_day_logs(cdir):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("kind") != "score":
                continue
            if status_ok_only and str(row.get("status") or "") != "ok":
                continue
            rows.append(row)
    return rows


def load_all_score_rows(cdir: Path) -> list[dict]:
    """All kind=score rows (any status) for header failure counts."""
    return load_score_rows(cdir, status_ok_only=False)


def load_returns_cache(path: Path) -> dict[tuple[str, str], dict]:
    """Key (row_id, horizon) -> cache record."""
    out: dict[tuple[str, str], dict] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        rid = str(rec.get("row_id") or "")
        hz = str(rec.get("horizon") or "")
        if not rid or not hz:
            continue
        out[(rid, hz)] = rec
    return out


def write_returns_cache(path: Path, cache: dict[tuple[str, str], dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for (rid, hz) in sorted(cache.keys()):
        rec = dict(cache[(rid, hz)])
        rec["row_id"] = rid
        rec["horizon"] = hz
        lines.append(json.dumps(rec, separators=(",", ":")))
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def sum_catalyst_cost(report_dir: Path | None = None) -> float | None:
    base = Path(report_dir) if report_dir is not None else ai_paths.resolve_report_dir()
    path = base / "token_metrics.jsonl"
    if not path.exists():
        return None
    total = 0.0
    n = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(row.get("phase") or "") != "ai_catalyst":
            continue
        c = row.get("total_cost_usd")
        if c is None:
            continue
        try:
            total += float(c)
            n += 1
        except (TypeError, ValueError):
            continue
    return total if n else 0.0


# ---------------------------------------------------------------------------
# Calendar + daily bars (injectable)
# ---------------------------------------------------------------------------

def default_calendar(start: str, end: str) -> list[str]:
    """Trading session dates from Alpaca calendar, inclusive."""
    from alpaca.trading.client import TradingClient

    api = (os.getenv("ALPACA_API_KEY") or "").strip()
    sec = (os.getenv("ALPACA_SECRET_KEY") or "").strip()
    if not api or not sec:
        try:
            secrets = json.loads((ROOT / "config" / "secrets.json").read_text(encoding="utf-8"))
            api = str(secrets.get("api_key") or "")
            sec = str(secrets.get("secret_key") or "")
        except Exception:
            api, sec = "", ""
    if not api or not sec:
        raise RuntimeError("alpaca credentials missing for calendar")
    tc = TradingClient(api, sec, paper=True)
    cal = tc.get_calendar(start=start, end=end)
    out: list[str] = []
    for row in cal or []:
        d = getattr(row, "date", None)
        if d is None:
            continue
        if hasattr(d, "isoformat"):
            out.append(d.isoformat())
        else:
            out.append(str(d)[:10])
    return out


def trading_day_offset(
    day: str, k: int, calendar_fn: CalendarFn, *, pad_days: int = 40,
) -> str | None:
    """Return the k-th trading day after *day* (k>=1), or None if unknown."""
    if k < 1:
        return day
    start = day
    end_dt = datetime.strptime(day, "%Y-%m-%d") + timedelta(days=pad_days + k * 2)
    end = end_dt.strftime("%Y-%m-%d")
    days = [d for d in calendar_fn(start, end) if d > day]
    if len(days) < k:
        return None
    return days[k - 1]


def fetch_daily_closes(
    symbols: list[str],
    start: str,
    end: str,
    *,
    client=None,
) -> dict[str, dict[str, float]]:
    """SIP daily closes: {SYM: {YYYY-MM-DD: close}}."""
    want = [str(s).upper() for s in symbols]
    out: dict[str, dict[str, float]] = {s: {} for s in want}
    if not want:
        return out
    cl = client if client is not None else bars.client()
    if cl is None:
        return out
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    start_dt = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=ET)
    end_dt = datetime.strptime(end, "%Y-%m-%d").replace(
        hour=23, minute=59, second=59, tzinfo=ET,
    )
    chunk = 100
    for i in range(0, len(want), chunk):
        batch = want[i:i + chunk]
        try:
            df = cl.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=batch,
                timeframe=TimeFrame.Day,
                start=start_dt.astimezone(timezone.utc),
                end=end_dt.astimezone(timezone.utc),
                feed=DataFeed.SIP,
            )).df
        except Exception:
            continue
        if df is None or getattr(df, "empty", True):
            continue
        import pandas as pd
        if isinstance(df.index, pd.MultiIndex):
            for sym in df.index.get_level_values("symbol").unique():
                sub = df.xs(sym, level="symbol").sort_index()
                key = str(sym).upper()
                for ts, row in sub.iterrows():
                    if hasattr(ts, "tz_convert"):
                        d = ts.tz_convert(ET).strftime("%Y-%m-%d")
                    else:
                        d = day_of(ts.timestamp() if hasattr(ts, "timestamp") else float(ts))
                    try:
                        out.setdefault(key, {})[d] = float(row["close"])
                    except Exception:
                        continue
        elif len(batch) == 1:
            sub = df.sort_index()
            key = batch[0]
            for ts, row in sub.iterrows():
                if hasattr(ts, "tz_convert"):
                    d = ts.tz_convert(ET).strftime("%Y-%m-%d")
                else:
                    d = day_of(ts.timestamp() if hasattr(ts, "timestamp") else float(ts))
                try:
                    out.setdefault(key, {})[d] = float(row["close"])
                except Exception:
                    continue
    return out


# ---------------------------------------------------------------------------
# Return computation
# ---------------------------------------------------------------------------

def _price_at_entry(stamps, closes, scored_ts: float, day: str) -> tuple[float | None, float | None]:
    gate = entry_gate_ts(scored_ts, day)
    i = first_bar_at_or_after(stamps, gate)
    if i < 0 or not closes:
        return None, None
    return float(stamps[i]), float(closes[i])


def _price_at_1550(stamps, closes, day: str) -> tuple[float | None, float | None]:
    target = et_hm_ts(day, 15, 50)
    i = first_bar_at_or_after(stamps, target)
    if i < 0 or not closes:
        # Fall back to bar at/before 15:50 if exact missing.
        i = bar_at_or_before(stamps, target)
        if i < 0:
            return None, None
        # Reject if too early (before 15:45).
        if stamps[i] < et_hm_ts(day, 15, 45):
            return None, None
    return float(stamps[i]), float(closes[i])


def equal_weight_return(
    entry_pxs: dict[str, float],
    exit_pxs: dict[str, float],
) -> tuple[float | None, int]:
    rets = []
    for sym, ep in entry_pxs.items():
        xp = exit_pxs.get(sym)
        r = pct_return(ep, xp)
        if r is not None:
            rets.append(r)
    if not rets:
        return None, 0
    return statistics.mean(rets), len(rets)


def horizon_exit_day(day: str, horizon: str, calendar_fn: CalendarFn) -> str | None:
    if horizon == "h_1550":
        return day
    if horizon == "h_d1":
        return trading_day_offset(day, 1, calendar_fn)
    if horizon == "h_d5":
        return trading_day_offset(day, 5, calendar_fn)
    if horizon == "h_d10":
        return trading_day_offset(day, 10, calendar_fn)
    return None


def horizon_matured(asof: str, day: str, horizon: str, calendar_fn: CalendarFn) -> bool:
    exit_day = horizon_exit_day(day, horizon, calendar_fn)
    if exit_day is None:
        return False
    return asof >= exit_day


def compute_row_horizon_returns(
    row: dict,
    *,
    asof: str,
    universe_stocks: list[str],
    calendar_fn: CalendarFn,
    cache: dict[tuple[str, str], dict],
    minute_bars: dict[tuple[str, str], tuple],
    daily_closes: dict[str, dict[str, float]],
    freeze_matured: bool = True,
) -> dict[str, dict]:
    """Compute/cached returns for one score row across horizons.

    Returns {horizon: {r_name, r_base_ew, r_spy, n_base, entry_ts, exit_ts, matured}}.
    """
    rid = str(row.get("row_id") or "")
    day = str(row.get("day") or "")
    sym = str(row.get("symbol") or "").upper()
    scored_ts = float(row.get("scored_ts") or 0.0)
    out: dict[str, dict] = {}

    for hz in HORIZONS:
        key = (rid, hz)
        cached = cache.get(key)
        if (
            freeze_matured
            and cached is not None
            and cached.get("matured")
            and cached.get("r_name") is not None
        ):
            out[hz] = cached
            continue

        matured = horizon_matured(asof, day, hz, calendar_fn)
        if not matured:
            rec = {
                "row_id": rid, "horizon": hz, "matured": False,
                "r_name": None, "r_base_ew": None, "r_spy": None,
                "n_base": 0, "entry_ts": None, "exit_ts": None,
            }
            out[hz] = rec
            cache[key] = rec
            continue

        stamps_c = minute_bars.get((sym, day), (None, None))
        stamps, closes = stamps_c[0], stamps_c[1]
        entry_ts, entry_px = _price_at_entry(stamps, closes, scored_ts, day)
        if entry_ts is None or entry_px is None:
            rec = {
                "row_id": rid, "horizon": hz, "matured": True,
                "r_name": None, "r_base_ew": None, "r_spy": None,
                "n_base": 0, "entry_ts": None, "exit_ts": None,
            }
            out[hz] = rec
            cache[key] = rec
            continue

        # Universe + SPY entry prices (same entry gate).
        entry_pxs: dict[str, float] = {}
        for u in universe_stocks:
            us, uc = minute_bars.get((u, day), (None, None))
            _, upx = _price_at_entry(us, uc, scored_ts, day)
            if upx is not None:
                entry_pxs[u] = upx
        spy_stamps, spy_closes = minute_bars.get(("SPY", day), (None, None))
        _, spy_entry = _price_at_entry(spy_stamps, spy_closes, scored_ts, day)

        if hz == "h_1550":
            exit_ts, exit_px = _price_at_1550(stamps, closes, day)
            exit_pxs: dict[str, float] = {}
            for u in universe_stocks:
                us, uc = minute_bars.get((u, day), (None, None))
                _, upx = _price_at_1550(us, uc, day)
                if upx is not None:
                    exit_pxs[u] = upx
            _, spy_exit = _price_at_1550(spy_stamps, spy_closes, day)
        else:
            exit_day = horizon_exit_day(day, hz, calendar_fn)
            exit_ts = et_hm_ts(exit_day, 16, 0) if exit_day else None
            exit_px = (daily_closes.get(sym) or {}).get(exit_day) if exit_day else None
            exit_pxs = {}
            if exit_day:
                for u in universe_stocks:
                    upx = (daily_closes.get(u) or {}).get(exit_day)
                    if upx is not None:
                        exit_pxs[u] = upx
            spy_exit = (daily_closes.get("SPY") or {}).get(exit_day) if exit_day else None

        r_name = pct_return(entry_px, exit_px)
        r_base, n_base = equal_weight_return(entry_pxs, exit_pxs)
        r_spy = pct_return(spy_entry, spy_exit)
        rec = {
            "row_id": rid,
            "horizon": hz,
            "matured": True,
            "r_name": r_name,
            "r_base_ew": r_base,
            "r_spy": r_spy,
            "n_base": n_base,
            "entry_ts": entry_ts,
            "exit_ts": exit_ts,
            "entry_px": entry_px,
            "exit_px": exit_px,
        }
        out[hz] = rec
        cache[key] = rec
    return out


# ---------------------------------------------------------------------------
# Slice building / stats
# ---------------------------------------------------------------------------

def _run_key(row: dict) -> tuple[str, str, str]:
    """(day, slot_or_run, symbol) for pairing models."""
    day = str(row.get("day") or "")
    slot = str(row.get("slot") or row.get("run_ts") or "")
    sym = str(row.get("symbol") or "").upper()
    return day, slot, sym


def pair_by_run(rows: list[dict]) -> dict[tuple[str, str, str], dict[str, dict]]:
    """(day, slot, symbol) -> {model_family: row}."""
    out: dict[tuple[str, str, str], dict[str, dict]] = defaultdict(dict)
    for row in rows:
        fam = str(row.get("model_family") or "").lower()
        if not fam:
            continue
        out[_run_key(row)][fam] = row
    return out


def dedupe_symbol_day(
    items: list[dict],
    *,
    key_fields: tuple[str, ...] = ("symbol", "day"),
) -> list[dict]:
    """Keep the first qualifying item per (symbol, D), ordered by scored_ts."""
    ordered = sorted(
        items,
        key=lambda r: (
            str(r.get("day") or ""),
            float(r.get("scored_ts") or 0.0),
            str(r.get("row_id") or ""),
        ),
    )
    seen: set[tuple] = set()
    out = []
    for r in ordered:
        k = tuple(str(r.get(f) or "").upper() if f == "symbol" else str(r.get(f) or "")
                  for f in key_fields)
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


def build_observation(
    row: dict,
    hz_ret: dict,
    *,
    direction: str | None = None,
    side: int | None = None,
    agreement: str | None = None,
    extra: dict | None = None,
) -> dict:
    d = direction if direction is not None else str(row.get("direction") or "").lower()
    s = side if side is not None else side_of(d)
    r_name = hz_ret.get("r_name")
    r_base = hz_ret.get("r_base_ew")
    r_spy = hz_ret.get("r_spy")
    ex_base = signed_excess(s, r_name, r_base)
    ex_spy = signed_excess(s, r_name, r_spy)
    net_base = net_of(ex_base)
    net_spy = net_of(ex_spy)
    obs = {
        "row_id": row.get("row_id"),
        "symbol": str(row.get("symbol") or "").upper(),
        "day": str(row.get("day") or ""),
        "slot": row.get("slot"),
        "model_family": str(row.get("model_family") or "").lower(),
        "direction": d,
        "side": s,
        "materiality": row.get("materiality"),
        "confidence": row.get("confidence"),
        "sources": list(row.get("sources") or []),
        "agreement": agreement,
        "matured": bool(hz_ret.get("matured")),
        "r_name": r_name,
        "r_base_ew": r_base,
        "r_spy": r_spy,
        "n_base": hz_ret.get("n_base") or 0,
        "excess_base": ex_base,
        "excess_spy": ex_spy,
        "net_base": net_base,
        "net_spy": net_spy,
        "net_base_bp": to_bp(net_base),
        "net_spy_bp": to_bp(net_spy),
        "entry_ts": hz_ret.get("entry_ts"),
        "exit_ts": hz_ret.get("exit_ts"),
    }
    if extra:
        obs.update(extra)
    return obs


def summarize_obs(obs_list: list[dict], *, bench: str = "base") -> dict[str, Any]:
    """Dedupe (symbol,D), date-cluster, hit rate on observations."""
    usable = [
        o for o in obs_list
        if o.get("matured")
        and o.get("side")
        and (o.get("net_base") if bench == "base" else o.get("net_spy")) is not None
    ]
    usable = dedupe_symbol_day(usable)
    net_key = "net_base" if bench == "base" else "net_spy"
    by_day: dict[str, list[float]] = defaultdict(list)
    nets = []
    for o in usable:
        v = o[net_key]
        by_day[str(o["day"])].append(v)
        nets.append(v)
    daily = {d: statistics.mean(vs) for d, vs in by_day.items()}
    clustered = date_clustered_stats(daily)
    return {
        "n_days": clustered["n_days"],
        "n_names": len(usable),
        "mean_net": clustered["mean"],
        "mean_net_bp": clustered["mean_bp"],
        "t": clustered["t"],
        "hit_rate": obs_hit_rate(nets),
        "hit_rate_days": clustered["hit_rate"],
        "daily": daily,
        "obs": usable,
    }


def primary_verdict(
    summary: dict[str, Any],
    *,
    n_days_logged: int,
    half_summaries: tuple[dict, dict] | None = None,
) -> dict[str, Any]:
    if n_days_logged < PRIMARY_MIN_DAYS:
        return {
            "verdict": "NOT YET",
            "go": None,
            "reason": f"only {n_days_logged} trading days logged (need {PRIMARY_MIN_DAYS})",
            "mean_net_bp": summary.get("mean_net_bp"),
            "t": summary.get("t"),
            "half_bp": None,
        }
    mean_bp = summary.get("mean_net_bp")
    t = summary.get("t")
    half_bp = None
    if half_summaries:
        half_bp = (
            half_summaries[0].get("mean_net_bp"),
            half_summaries[1].get("mean_net_bp"),
        )
    ok_mean = mean_bp is not None and mean_bp > PRIMARY_MIN_NET_BP
    ok_t = t is not None and t >= PRIMARY_MIN_T
    ok_halves = (
        half_bp is not None
        and half_bp[0] is not None and half_bp[0] > PRIMARY_MIN_NET_BP
        and half_bp[1] is not None and half_bp[1] > PRIMARY_MIN_NET_BP
    )
    passed = bool(ok_mean and ok_t and ok_halves)
    return {
        "verdict": "GO" if passed else "NO-GO",
        "pass": passed,  # PASS/FAIL alias
        "go": passed,
        "ok_mean": ok_mean,
        "ok_t": ok_t,
        "ok_halves": ok_halves,
        "mean_net_bp": mean_bp,
        "t": t,
        "half_bp": half_bp,
        "n_days": summary.get("n_days"),
        "n_names": summary.get("n_names"),
    }


# ---------------------------------------------------------------------------
# Scorecard rendering
# ---------------------------------------------------------------------------

def _fmt_bp(x: float | None) -> str:
    if x is None:
        return "—"
    return f"{x:+.1f}"


def _fmt_t(x: float | None) -> str:
    if x is None:
        return "—"
    if math.isinf(x):
        return "inf"
    return f"{x:.2f}"


def _fmt_pct(x: float | None) -> str:
    if x is None:
        return "—"
    return f"{100.0 * x:.0f}%"


def _fmt_stats(s: dict) -> str:
    return (
        f"n_days={s.get('n_days', 0)} n_names={s.get('n_names', 0)} "
        f"mean_net_bp={_fmt_bp(s.get('mean_net_bp'))} "
        f"t={_fmt_t(s.get('t'))} hit={_fmt_pct(s.get('hit_rate'))}"
    )


def render_scorecard(payload: dict) -> str:
    lines: list[str] = []
    h = payload["header"]
    lines.append("# AI catalyst scorecard")
    lines.append("")
    lines.append(f"- **As of:** {h['asof']}")
    lines.append(f"- **Trading days logged:** {h['n_days_logged']}")
    lines.append(
        f"- **Rows ok / failed:** grok {h['ok_by_model'].get('grok', 0)}/"
        f"{h['fail_by_model'].get('grok', 0)}; "
        f"agy {h['ok_by_model'].get('agy', 0)}/"
        f"{h['fail_by_model'].get('agy', 0)}"
    )
    cost = h.get("cost_usd")
    lines.append(
        f"- **Total ai_catalyst cost to date:** "
        f"{'—' if cost is None else f'${cost:.4f}'}"
    )
    lines.append("")

    lines.append("## Pre-registered primary test")
    lines.append("")
    lines.append(PRIMARY_TEST_VERBATIM)
    lines.append("")
    pv = payload["primary"]
    lines.append(
        f"**Current primary (`agree_strong` & up, `{PRIMARY_HORIZON}` vs base_ew):** "
        f"{_fmt_stats(pv['summary'])}"
    )
    if pv["verdict"].get("half_bp"):
        hb = pv["verdict"]["half_bp"]
        lines.append(
            f"**Half-split mean net bp:** first={_fmt_bp(hb[0])} "
            f"second={_fmt_bp(hb[1])}"
        )
    lines.append(f"**Verdict:** **{pv['verdict']['verdict']}**")
    if pv["verdict"].get("reason"):
        lines.append(f"_{pv['verdict']['reason']}_")
    lines.append("")
    lines.append(
        "Note: overlapping 5- and 10-day forward windows inflate the "
        "date-clustered t a little."
    )
    lines.append("")

    lines.append("## By model x horizon")
    lines.append("")
    for model, by_hz in payload["by_model"].items():
        lines.append(f"### {model}")
        for hz in HORIZONS:
            s = by_hz.get(hz) or {}
            lines.append(f"- `{hz}`: {_fmt_stats(s)}")
        lines.append("")

    lines.append("## By direction x materiality (per model)")
    lines.append("")
    for model, buckets in payload["by_mat"].items():
        lines.append(f"### {model}")
        for key in sorted(buckets):
            lines.append(f"- {key}: {_fmt_stats(buckets[key])}")
        lines.append("")

    lines.append("## By direction x confidence (per model)")
    lines.append("")
    for model, buckets in payload["by_conf"].items():
        lines.append(f"### {model}")
        for key in sorted(buckets):
            lines.append(f"- {key}: {_fmt_stats(buckets[key])}")
        lines.append("")

    lines.append("## By agreement")
    lines.append("")
    for bucket, by_hz in payload["by_agreement"].items():
        lines.append(f"### {bucket}")
        for hz in HORIZONS:
            s = by_hz.get(hz) or {}
            lines.append(f"- `{hz}`: {_fmt_stats(s)}")
        lines.append("")

    lines.append("## Reference rows")
    lines.append("")
    lines.append(
        f"- All scored names vs base_ew (`{PRIMARY_HORIZON}`): "
        f"{_fmt_stats(payload['reference']['all'])}"
    )
    for src, s in payload["reference"]["by_source"].items():
        lines.append(f"- source `{src}`: {_fmt_stats(s)}")
    lines.append("")

    lines.append("## Split halves (primary slice)")
    lines.append("")
    lines.append(f"- First half: {_fmt_stats(payload['halves']['first'])}")
    lines.append(f"- Second half: {_fmt_stats(payload['halves']['second'])}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _warmup_minute_bars(
    symbols: list[str],
    days: list[str],
    *,
    fetch_many_fn: MinuteFetchManyFn = fetch_many,
    fetch_fn: MinuteFetchFn = fetch,
) -> dict[tuple[str, str], tuple]:
    """Return {(sym, day): (stamps, closes)} using bars cache."""
    out: dict[tuple[str, str], tuple] = {}
    for day in days:
        fetch_many_fn(symbols, day)
        for sym in symbols:
            stamps, closes = fetch_fn(sym, day)
            out[(sym, day)] = (stamps, closes)
    return out


def run_score(
    *,
    asof: str | None = None,
    report_dir: Path | None = None,
    universe_path: Path | None = None,
    calendar_fn: CalendarFn | None = None,
    fetch_many_fn: MinuteFetchManyFn | None = None,
    fetch_fn: MinuteFetchFn | None = None,
    daily_fetch_fn: DailyFetchFn | None = None,
    write_outputs: bool = True,
) -> dict[str, Any]:
    """Score all ok catalyst rows and optionally write cache + scorecard.

    All heavy deps are injectable for tests (no network).
    """
    asof = asof or datetime.now(tz=ET).strftime("%Y-%m-%d")
    cdir = catalyst_dir(report_dir)
    cdir.mkdir(parents=True, exist_ok=True)
    cal = calendar_fn or default_calendar
    fmany = fetch_many_fn or fetch_many
    f1 = fetch_fn or fetch

    stocks, _etfs = load_universe(universe_path)
    all_rows = load_all_score_rows(cdir)
    ok_rows = [r for r in all_rows if str(r.get("status") or "") == "ok"]

    ok_by_model: dict[str, int] = defaultdict(int)
    fail_by_model: dict[str, int] = defaultdict(int)
    for r in all_rows:
        fam = str(r.get("model_family") or "").lower() or "?"
        if str(r.get("status") or "") == "ok":
            ok_by_model[fam] += 1
        else:
            fail_by_model[fam] += 1

    days_logged = sorted({str(r.get("day") or "") for r in ok_rows if r.get("day")})
    n_days_logged = len(days_logged)

    cache_path = cdir / "returns_cache.jsonl"
    cache = load_returns_cache(cache_path)

    # Symbols / days we need minute bars for.
    need_syms = sorted(set(stocks) | {"SPY"} | {
        str(r.get("symbol") or "").upper() for r in ok_rows if r.get("symbol")
    })
    need_days = sorted({str(r.get("day") or "") for r in ok_rows if r.get("day")})

    minute_bars = _warmup_minute_bars(
        need_syms, need_days, fetch_many_fn=fmany, fetch_fn=f1,
    )

    # Daily closes for matured D+k exits.
    daily_closes: dict[str, dict[str, float]] = {s: {} for s in need_syms}
    if need_days:
        # Prefer an explicit D+10 when the calendar reaches that far; always
        # include *asof* so a short injected calendar still covers matured exits.
        exit10 = trading_day_offset(need_days[-1], 10, cal)
        max_end = max(x for x in (asof, exit10, need_days[-1]) if x)
        need_daily = False
        for d in need_days:
            for hz in ("h_d1", "h_d5", "h_d10"):
                if horizon_matured(asof, d, hz, cal):
                    need_daily = True
                    break
            if need_daily:
                break
        if need_daily:
            if daily_fetch_fn is not None:
                raw = daily_fetch_fn(need_syms, need_days[0], max_end)
                if raw and isinstance(next(iter(raw.values()), None), dict):
                    daily_closes = {
                        str(k).upper(): dict(v) for k, v in raw.items()
                    }  # type: ignore[arg-type]
                else:
                    daily_closes = {s: {} for s in need_syms}
            else:
                daily_closes = fetch_daily_closes(need_syms, need_days[0], max_end)

    # Per-row horizon returns.
    row_returns: dict[str, dict[str, dict]] = {}
    for row in ok_rows:
        rid = str(row.get("row_id") or "")
        row_returns[rid] = compute_row_horizon_returns(
            row,
            asof=asof,
            universe_stocks=stocks,
            calendar_fn=cal,
            cache=cache,
            minute_bars=minute_bars,
            daily_closes=daily_closes,
        )

    pairs = pair_by_run(ok_rows)

    def obs_for_row(row: dict, hz: str, **kw: Any) -> dict | None:
        rid = str(row.get("row_id") or "")
        hz_ret = (row_returns.get(rid) or {}).get(hz) or {}
        if not hz_ret:
            return None
        return build_observation(row, hz_ret, **kw)

    # --- By model x horizon ---
    by_model: dict[str, dict[str, dict]] = {}
    for model in ("grok", "agy"):
        by_model[model] = {}
        model_rows = [r for r in ok_rows if str(r.get("model_family") or "").lower() == model]
        for hz in HORIZONS:
            obs = []
            for r in model_rows:
                if side_of(r.get("direction")) == 0:
                    continue
                o = obs_for_row(r, hz)
                if o:
                    obs.append(o)
            by_model[model][hz] = summarize_obs(obs)

    # --- Direction x materiality / confidence ---
    by_mat: dict[str, dict[str, dict]] = {}
    by_conf: dict[str, dict[str, dict]] = {}
    for model in ("grok", "agy"):
        by_mat[model] = {}
        by_conf[model] = {}
        model_rows = [r for r in ok_rows if str(r.get("model_family") or "").lower() == model]
        buckets_m: dict[str, list] = defaultdict(list)
        buckets_c: dict[str, list] = defaultdict(list)
        for r in model_rows:
            d = str(r.get("direction") or "").lower()
            if side_of(d) == 0:
                continue
            o = obs_for_row(r, PRIMARY_HORIZON)
            if not o:
                continue
            mb = materiality_bucket(r.get("materiality"))
            cb = confidence_bucket(r.get("confidence"))
            if mb:
                buckets_m[f"{d}|mat={mb}"].append(o)
            if cb:
                buckets_c[f"{d}|conf={cb}"].append(o)
        for k, obs in buckets_m.items():
            by_mat[model][k] = summarize_obs(obs)
        for k, obs in buckets_c.items():
            by_conf[model][k] = summarize_obs(obs)

    # --- Agreement slices ---
    # Build one synthetic observation per (symbol, run) using the later scored_ts
    # row as the price anchor, direction from the agreed direction (or grok).
    by_agreement: dict[str, dict[str, dict]] = {
        b: {} for b in ("agree_strong", "agree_weak", "disagree", "single")
    }
    agree_units: list[dict] = []  # carry metadata for primary
    for key, models in pairs.items():
        grok_r = models.get("grok")
        agy_r = models.get("agy")
        bucket = agreement_bucket(grok_r, agy_r)
        # Anchor row: earlier scored_ts (first of day preference later via dedupe).
        candidates = [r for r in (grok_r, agy_r) if r is not None]
        if not candidates:
            continue
        anchor = min(candidates, key=lambda r: float(r.get("scored_ts") or 0.0))
        # Direction for agree_*/single; disagree excluded from directional stats.
        if bucket in ("agree_strong", "agree_weak", "single"):
            direction = str(anchor.get("direction") or "").lower()
        else:
            direction = "neutral"
        unit = {
            "row": anchor,
            "agreement": bucket,
            "direction": direction,
            "day": key[0],
            "symbol": key[2],
            "scored_ts": float(anchor.get("scored_ts") or 0.0),
            "row_id": anchor.get("row_id"),
            "sources": list(anchor.get("sources") or []),
            "materiality": anchor.get("materiality"),
            "confidence": anchor.get("confidence"),
            "model_family": "agree",
        }
        agree_units.append(unit)

    for bucket, hz_map in by_agreement.items():
        units = [u for u in agree_units if u["agreement"] == bucket]
        for hz in HORIZONS:
            obs = []
            for u in units:
                if side_of(u["direction"]) == 0:
                    continue
                o = obs_for_row(
                    u["row"], hz,
                    direction=u["direction"],
                    agreement=bucket,
                )
                if o:
                    # Ensure symbol/day from the unit.
                    o["symbol"] = u["symbol"]
                    o["day"] = u["day"]
                    o["agreement"] = bucket
                    obs.append(o)
            hz_map[hz] = summarize_obs(obs)

    # --- Primary slice: agree_strong & up @ h_d5 ---
    primary_obs = []
    for u in agree_units:
        if u["agreement"] != "agree_strong":
            continue
        if u["direction"] != "up":
            continue
        o = obs_for_row(u["row"], PRIMARY_HORIZON, direction="up", agreement="agree_strong")
        if o:
            o["symbol"] = u["symbol"]
            o["day"] = u["day"]
            primary_obs.append(o)
    primary_summary = summarize_obs(primary_obs)

    # Half split on primary
    primary_days = sorted(primary_summary.get("daily") or {})
    first_days, second_days = half_split_days(primary_days)
    first_obs = [o for o in primary_summary.get("obs") or [] if o["day"] in set(first_days)]
    second_obs = [o for o in primary_summary.get("obs") or [] if o["day"] in set(second_days)]
    # Re-summarize halves (re-cluster within half).
    half_first = summarize_obs(first_obs)
    half_second = summarize_obs(second_obs)
    verdict = primary_verdict(
        primary_summary,
        n_days_logged=n_days_logged,
        half_summaries=(half_first, half_second),
    )

    # --- Reference: all scored names (unsigned long vs base) and by source ---
    # "all scored names vs base_ew" — news-having set as long (side=+1).
    ref_all_obs = []
    for r in dedupe_symbol_day([
        {
            **r,
            "symbol": str(r.get("symbol") or "").upper(),
            "day": str(r.get("day") or ""),
        }
        for r in ok_rows
    ]):
        o = obs_for_row(r, PRIMARY_HORIZON, direction="up", side=1)
        if o:
            ref_all_obs.append(o)
    # For reference we want raw excess without requiring model direction;
    # summarize_obs filters side!=0 so side=1 is fine.
    reference_all = summarize_obs(ref_all_obs)

    by_source: dict[str, dict] = {}
    source_obs: dict[str, list] = defaultdict(list)
    for r in ok_rows:
        for src in (r.get("sources") or ["unknown"]):
            o = obs_for_row(r, PRIMARY_HORIZON, direction="up", side=1)
            if o:
                source_obs[str(src)].append(o)
    for src, obs in sorted(source_obs.items()):
        by_source[src] = summarize_obs(obs)

    cost = None
    try:
        cost = sum_catalyst_cost(report_dir)
    except Exception:
        cost = None

    payload = {
        "header": {
            "asof": asof,
            "n_days_logged": n_days_logged,
            "days_logged": days_logged,
            "ok_by_model": dict(ok_by_model),
            "fail_by_model": dict(fail_by_model),
            "cost_usd": cost,
        },
        "primary": {
            "summary": primary_summary,
            "verdict": verdict,
        },
        "by_model": by_model,
        "by_mat": by_mat,
        "by_conf": by_conf,
        "by_agreement": by_agreement,
        "reference": {
            "all": reference_all,
            "by_source": by_source,
        },
        "halves": {
            "first": half_first,
            "second": half_second,
            "first_days": first_days,
            "second_days": second_days,
        },
        "cache": cache,
        "row_returns": row_returns,
    }

    if write_outputs:
        write_returns_cache(cache_path, cache)
        scorecard_path = cdir / "scorecard.md"
        scorecard_path.write_text(render_scorecard(payload), encoding="utf-8")
        payload["scorecard_path"] = str(scorecard_path)
        payload["cache_path"] = str(cache_path)

    return payload


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="AI catalyst forward-return scorer")
    ap.add_argument("--asof", default=None, help="YYYY-MM-DD (default: today ET)")
    ap.add_argument(
        "--universe",
        default=None,
        help="Path to liquid_universe.json (default: config/liquid_universe.json)",
    )
    args = ap.parse_args(argv)
    asof = args.asof
    if asof is None:
        asof = datetime.now(tz=ET).strftime("%Y-%m-%d")
    payload = run_score(
        asof=asof,
        universe_path=Path(args.universe) if args.universe else None,
    )
    v = payload["primary"]["verdict"]["verdict"]
    print(
        f"ai_catalyst_score asof={asof} days={payload['header']['n_days_logged']} "
        f"verdict={v}"
    )
    print(f"wrote {payload.get('scorecard_path')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
