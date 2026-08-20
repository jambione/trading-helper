#!/usr/bin/env python3
"""Is there a feature a slot ranker could actually rank on?

slot_contention.py established that the names turned away at max_positions beat
the names holding the slots on 54% of contested moments over 5 days (n=3363,
median +0.09% over 15m) — real, but thin. 54% is only worth building against if
some *observable at the moment of the skip* separates the winner from the loser.
If the edge is spread evenly across every feature, no rule can reach it.

For each contested moment this pairs the skipped candidate against the book that
was actually held, reads both sides' features from the shadow log, and asks one
question per feature:

    had the desk preferred the name with the higher (or lower) value of this
    feature, what would it have gained per contested moment?

That is the number a ranker would capture, in the same units as the +0.09% it is
chasing. A feature whose swap-gain is indistinguishable from the unconditional
+0.09% carries no information — it is just re-describing the base rate.

Read-only. Usage:
    python3 tools/slot_ranker_signal.py [--days N] [--horizon-min N]
"""
from __future__ import annotations

import argparse
import bisect
import json
import os
import statistics
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
ET = ZoneInfo("America/New_York")
EVENTS = os.path.join(ROOT, "ai_reports", "events.jsonl")
OUTCOMES = os.path.join(ROOT, "ai_reports", "outcomes.jsonl")
SHADOW = os.path.join(ROOT, "ai_reports", "shadow.jsonl")

# Numeric candidate features carried on every shadow row. Booleans are folded
# to 0/1 so one comparison covers both kinds.
FEATURES = [
    "score", "rvol", "pct_change", "cm_rsi", "pctr", "proximity_pct",
    "exhaustion", "spread_r", "window_span_min", "tape_age_sec",
    "cm_ok", "pctr_ok", "cm_rsi_rising", "in_zone",
]
# How far from the skip a shadow row may sit and still describe that instant.
MATCH_TOL_SEC = 45.0


def _jsonl(path):
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def _day(ts):
    return datetime.fromtimestamp(
        float(ts), timezone.utc).astimezone(ET).strftime("%Y-%m-%d")


def _num(v):
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # drop NaN


def load_shadow(days: set[str]) -> dict[str, tuple[list[float], list[dict]]]:
    """symbol -> (sorted timestamps, feature rows) for the days in range.

    Only the columns in FEATURES are kept; the raw file is tens of MB and the
    rest of each row is not part of this question.
    """
    by_sym: dict[str, list[tuple[float, dict]]] = {}
    if not os.path.exists(SHADOW):
        return {}
    with open(SHADOW, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            ts = d.get("ts")
            sym = str(d.get("symbol") or "").upper()
            if not ts or not sym:
                continue
            try:
                ts = float(ts)
            except (TypeError, ValueError):
                continue
            if _day(ts) not in days:
                continue
            row = {}
            for k in FEATURES:
                v = _num(d.get(k))
                if v is not None:
                    row[k] = v
            if row:
                by_sym.setdefault(sym, []).append((ts, row))
    out = {}
    for sym, rows in by_sym.items():
        rows.sort(key=lambda r: r[0])
        out[sym] = ([r[0] for r in rows], [r[1] for r in rows])
    return out


def features_at(shadow, sym: str, ts: float) -> dict | None:
    """The shadow row nearest *ts*, or None if the nearest is too far away."""
    hit = shadow.get(str(sym).upper())
    if not hit:
        return None
    stamps, rows = hit
    i = bisect.bisect_left(stamps, ts)
    best, best_gap = None, None
    for k in (i - 1, i):
        if 0 <= k < len(stamps):
            gap = abs(stamps[k] - ts)
            if best_gap is None or gap < best_gap:
                best, best_gap = rows[k], gap
    if best is None or best_gap > MATCH_TOL_SEC:
        return None
    return best


def _fetch(client, sym, day, cache):
    if (sym, day) in cache:
        return cache[(sym, day)]
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    from alpaca.data.enums import DataFeed

    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
    df = None
    try:
        df = client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=sym,
            timeframe=TimeFrame(1, TimeFrameUnit.Minute),
            start=d.replace(hour=8, minute=0).astimezone(timezone.utc),
            end=d.replace(hour=16, minute=30).astimezone(timezone.utc),
            limit=10000, extended_hours=True, feed=DataFeed.IEX,
        )).df
        import pandas as pd
        if df is not None and not df.empty and isinstance(df.index, pd.MultiIndex):
            df = df.xs(sym, level="symbol")
        df = None if df is None or df.empty else df.sort_index()
    except Exception:
        df = None
    cache[(sym, day)] = df
    return df


def _fwd(df, t0: float, horizon: float):
    """Pct change from the bar at t0 to the last bar within the horizon."""
    if df is None:
        return None
    stamps = [t.timestamp() for t in df.index]
    closes = [float(v) for v in df["close"]]
    i = None
    for k, s in enumerate(stamps):
        if s <= t0:
            i = k
        else:
            break
    if i is None:
        return None
    j = None
    for k in range(i + 1, len(stamps)):
        if stamps[k] <= t0 + horizon:
            j = k
        else:
            break
    if j is None or stamps[j] - stamps[i] < horizon * 0.5:
        return None
    return (closes[j] - closes[i]) / closes[i] * 100.0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=5)
    ap.add_argument("--horizon-min", type=float, default=15.0)
    args = ap.parse_args()
    horizon = args.horizon_min * 60.0

    events = [r for r in _jsonl(EVENTS) if r.get("ts")]
    outcomes = [r for r in _jsonl(OUTCOMES)
                if r.get("entry_time") and r.get("exit_time")]
    if not events:
        print("no events log")
        return 1
    days = sorted({_day(r["ts"]) for r in events})[-args.days:]
    dayset = set(days)
    skips = [r for r in events
             if r.get("kind") == "watch_skip"
             and str(r.get("reason")) == "max_positions"
             and _day(r["ts"]) in dayset]
    if not skips:
        print("no max_positions skips in range")
        return 1
    print(f"{len(skips)} max_positions skips across {days[0]}..{days[-1]}")

    shadow = load_shadow(dayset)
    print(f"shadow rows indexed for {len(shadow)} symbols")

    sec = json.load(open(os.path.join(ROOT, "config", "secrets.json")))
    import alpaca_api as aa
    client = aa.connect_data_client(
        {"api_key": sec["api_key"], "secret_key": sec["secret_key"]})

    cache = {}
    # One record per contested moment that has both sides fully described.
    moments = []
    no_feat = 0
    for ev in skips:
        ts = float(ev["ts"])
        day = _day(ts)
        sym = str(ev.get("symbol") or "").upper()
        if not sym:
            continue
        s_fwd = _fwd(_fetch(client, sym, day, cache), ts, horizon)
        if s_fwd is None:
            continue
        s_feat = features_at(shadow, sym, ts)
        if not s_feat:
            no_feat += 1
            continue
        held = [o for o in outcomes
                if float(o["entry_time"]) <= ts <= float(o["exit_time"])]
        h_rets, h_feats = [], []
        for o in held:
            hs = str(o["symbol"]).upper()
            v = _fwd(_fetch(client, hs, day, cache), ts, horizon)
            f = features_at(shadow, hs, ts)
            if v is not None and f:
                h_rets.append(v)
                h_feats.append(f)
        if not h_rets:
            continue
        moments.append({
            "ret_diff": s_fwd - statistics.fmean(h_rets),
            "skipped": s_feat,
            "held": h_feats,
        })

    if not moments:
        print("no contested moments with features on both sides")
        return 1

    base = [m["ret_diff"] for m in moments]
    print(f"\n{len(moments)} contested moments with features on both sides "
          f"({no_feat} skipped for no shadow row within {MATCH_TOL_SEC:g}s)")
    print(f"unconditional swap (always take the skipped name):"
          f"  median {statistics.median(base):+.3f}%  "
          f"mean {statistics.fmean(base):+.3f}%  "
          f"win {100.0 * sum(1 for x in base if x > 0) / len(base):.0f}%")
    print("\nThat row is the number to beat. A feature only earns a ranker if")
    print("preferring it does better than taking the skipped name every time.\n")

    print(f"{'feature':<18} {'n':>5} {'rule':<8} {'median':>9} {'mean':>9} {'win':>6}")
    print("-" * 62)
    rows = []
    for feat in FEATURES:
        # Gain from preferring the higher-valued name: when the skipped name
        # ranks higher we take it (+ret_diff); when the book ranks higher we
        # keep the book (0 — no swap, no gain).
        for rule, sign in (("higher", 1.0), ("lower", -1.0)):
            gains = []
            for m in moments:
                s = m["skipped"].get(feat)
                hv = [h.get(feat) for h in m["held"] if h.get(feat) is not None]
                if s is None or not hv:
                    continue
                if sign * s > sign * statistics.fmean(hv):
                    gains.append(m["ret_diff"])   # swap in the skipped name
                else:
                    gains.append(0.0)             # keep the book
            if len(gains) < 30:
                continue
            rows.append((feat, rule, len(gains), statistics.median(gains),
                         statistics.fmean(gains),
                         100.0 * sum(1 for x in gains if x > 0) / len(gains)))
    # Rank by mean gain — the median is 0 for any rule that swaps under half
    # the time, so it cannot order them.
    rows.sort(key=lambda r: -r[4])
    for feat, rule, n, med, mean, win in rows:
        print(f"{feat:<18} {n:>5} {rule:<8} {med:>+8.3f}% {mean:>+8.3f}% {win:>5.0f}%")

    print("\nRead it against the unconditional row: a rule that swaps on half")
    print("the moments and captures about half the unconditional mean is")
    print("splitting the base rate, not finding signal. A rule worth building")
    print("has a mean gain at or above the unconditional one on fewer swaps.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
