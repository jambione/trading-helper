#!/usr/bin/env python3
"""runway_study.py — did the desk's fills have room to run, and does anything
logged at entry predict which ones did?

WHY NOT outcomes.mfe_r
    mfe_r is measured only while the position was held, and the shelf ends
    most holds inside two minutes. It is censored by the very exit whose
    usefulness depends on it. Runway here is measured off SIP 1-minute bars
    over fixed horizons after the fill, whether or not the desk still held.

WHAT IS MEASURED (per fill, from the fill price in outcomes.jsonl)
    up_H    highest 1m high in (entry minute, entry minute + H] vs fill, %
    dn_H    lowest 1m low over the same window vs fill, %
    ret_H   last close inside the window vs fill, %
    tp_X    +X% touched before -X% (a same-bar tie counts as the loss)

    Bars start at the minute AFTER the entry minute: the entry bar's high may
    have printed before the fill, and counting it would be look-ahead.

CONTROL
    Every 5th RTH minute of the same symbol on the same day, from its first
    fill through 15:30, scored the same way from that minute's close.
    Minutes before the first fill are excluded: they contain the run-up that
    put the name on the book, which the desk could never have traded. It answers "did our timing
    add runway to a name we'd have picked anyway", which is the only thing the
    arming gates can influence. Names stay the same, timing is what changes.

FEATURES
    Entry-time features the desk logged, plus bar features built only from
    bars COMPLETED before the fill (no look-ahead). Each is split into
    terciles. A feature counts as predictive only if the top-vs-bottom
    difference has |t| >= 2 on the full sample AND the same sign in both date
    halves. With ~25 features, one |t|>2 is expected by chance alone.

Not a tape: 1-minute bars, no spread, no intra-bar order.

USAGE (on the mini; needs config/secrets.json)
    python3 tools/runway_study.py [--since 2026-08-04] [--json out.json]
    python3 tools/runway_study.py --since 2026-09-15 --by git_version

    --by git_version lists every deploy in the order it first traded, with
    realized R over all its fills and bar runway over those with bars. Today's
    bars stop 16 minutes ago (SIP delay), so the newest fills score late.
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import pickle
import re
import statistics
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)
import bars  # noqa: E402

HORIZONS = (5, 15, 30, 60)          # minutes
TOUCH = (0.5, 1.0, 2.0)             # percent, symmetric first-touch
CACHE = os.path.join(ROOT, "ai_reports", "runway_bars_cache.pkl")
SYM_RE = re.compile(r"^[A-Z]{1,5}$")


# ── bars ────────────────────────────────────────────────────────────────
def _load_cache() -> dict:
    try:
        with open(CACHE, "rb") as f:
            return pickle.load(f)
    except Exception:
        return {}


def _save_cache(c: dict) -> None:
    tmp = CACHE + ".tmp"
    with open(tmp, "wb") as f:
        pickle.dump(c, f)
    os.replace(tmp, CACHE)


def fetch_days(want: dict, cache: dict) -> None:
    """Fill cache[(sym, day)] with (t, o, h, l, c, v) SIP 1m RTH bars, or None.

    One multi-symbol request per day, paced at one per second: the live
    engine shares these data keys, and a per-symbol loop is a few hundred
    requests a minute against a 200/min limit.
    """
    cl = bars.client()
    if cl is None:
        raise SystemExit("no Alpaca data client (config/secrets.json)")
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    from alpaca.data.enums import DataFeed
    import pandas as pd

    today = bars.day_of(time.time())
    for n, (day, syms) in enumerate(sorted(want.items())):
        # Today's bars are still growing: always refetch, never trust cache.
        syms = sorted(s for s in syms if day == today or (s, day) not in cache)
        if not syms:
            continue
        d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=bars.ET)
        end = d.replace(hour=16, minute=0).astimezone(timezone.utc)
        if day == today:
            # The data plan refuses SIP newer than 15 minutes. Windows that
            # run past this end are dropped by score_path's truncation rule.
            end = min(end, datetime.now(timezone.utc) - timedelta(minutes=16))
        got: dict = {}
        try:
            df = cl.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=syms,
                timeframe=TimeFrame(1, TimeFrameUnit.Minute),
                start=d.replace(hour=9, minute=30).astimezone(timezone.utc),
                end=end,
                limit=1000000, extended_hours=False, feed=DataFeed.SIP,
            )).df
            if df is not None and not df.empty:
                if not isinstance(df.index, pd.MultiIndex):
                    df = pd.concat({syms[0]: df}, names=["symbol"])
                for sym in df.index.get_level_values("symbol").unique():
                    sub = df.xs(sym, level="symbol").sort_index()
                    got[str(sym)] = ([t.timestamp() for t in sub.index],
                                     [float(x) for x in sub["open"]],
                                     [float(x) for x in sub["high"]],
                                     [float(x) for x in sub["low"]],
                                     [float(x) for x in sub["close"]],
                                     [float(x) for x in sub["volume"]])
        except Exception as e:  # missing data, not a flat day
            print(f"  bars fail {day}: {str(e)[:100]}", file=sys.stderr)
            continue  # leave uncached so a rerun retries
        for sym in syms:
            cache[(sym, day)] = got.get(sym)
        _save_cache(cache)
        print(f"  bars {day} ({n + 1}/{len(want)}): {len(got)}/{len(syms)}",
              file=sys.stderr)
        time.sleep(1.0)


# ── scoring ─────────────────────────────────────────────────────────────
def score_path(B, i0: int, px: float) -> dict | None:
    """Score bars strictly after index i0 against price px."""
    t, _o, h, l, c, _v = B
    if px <= 0 or i0 < 0:
        return None
    out: dict = {}
    t0 = t[i0]
    for H in HORIZONS:
        j = bisect.bisect_right(t, t0 + H * 60) - 1
        if j <= i0 or (t[j] - t0) < H * 60 * 0.5:
            out[f"up_{H}"] = out[f"dn_{H}"] = out[f"ret_{H}"] = None
            continue
        out[f"up_{H}"] = (max(h[i0 + 1:j + 1]) / px - 1) * 100
        out[f"dn_{H}"] = (min(l[i0 + 1:j + 1]) / px - 1) * 100
        out[f"ret_{H}"] = (c[j] / px - 1) * 100
    # first touch, up to 60 minutes
    jmax = bisect.bisect_right(t, t0 + 60 * 60) - 1
    for X in TOUCH:
        up, dn = px * (1 + X / 100), px * (1 - X / 100)
        res = None
        for k in range(i0 + 1, jmax + 1):
            hit_dn = l[k] <= dn
            hit_up = h[k] >= up
            if hit_dn:
                res = 0
                break
            if hit_up:
                res = 1
                break
        out[f"tp_{X}"] = res  # None = neither within 60m
    return out


def bar_features(B, i_entry: int, fill: float) -> dict:
    """Features from bars completed before the fill minute only."""
    t, o, h, l, c, v = B
    k = i_entry - 1  # last completed bar
    f: dict = {}
    if k < 0:
        return f
    f["chase_pct"] = (fill / c[k] - 1) * 100
    if k >= 5:
        f["ret_5m_prior"] = (c[k] / c[k - 5] - 1) * 100
    if k >= 15:
        f["ret_15m_prior"] = (c[k] / c[k - 15] - 1) * 100
    f["move_since_open"] = (c[k] / o[0] - 1) * 100
    hi, lo = max(h[:k + 1]), min(l[:k + 1])
    if hi > lo:
        f["range_pos"] = (fill - lo) / (hi - lo) * 100
    f["off_day_high_pct"] = (fill / hi - 1) * 100
    if k >= 10:
        rets = [(c[m] / c[m - 1] - 1) * 100 for m in range(max(1, k - 14), k + 1) if c[m - 1]]
        if len(rets) >= 5:
            f["vol_1m_15"] = statistics.stdev(rets)
        recent = sum(v[k - 4:k + 1]) / 5
        base = sum(v[:k - 4]) / max(1, k - 4)
        if base > 0:
            f["vol_surge_5m"] = recent / base
        pv = sum(c[m] * v[m] for m in range(k + 1))
        vv = sum(v[:k + 1])
        if vv > 0:
            f["vs_vwap_pct"] = (fill / (pv / vv) - 1) * 100
    f["log_price"] = math.log10(fill)
    return f


# ── stats ───────────────────────────────────────────────────────────────
def q(xs, p):
    xs = sorted(xs)
    if not xs:
        return None
    k = (len(xs) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def mean(xs):
    return sum(xs) / len(xs) if xs else None


def tstat(a, b):
    if len(a) < 5 or len(b) < 5:
        return None
    va, vb = statistics.variance(a), statistics.variance(b)
    se = math.sqrt(va / len(a) + vb / len(b))
    return (mean(a) - mean(b)) / se if se > 0 else None


def summarize(rows, label):
    lines = [f"\n== {label} (n={len(rows)}) =="]
    for H in HORIZONS:
        up = [r[f"up_{H}"] for r in rows if r.get(f"up_{H}") is not None]
        dn = [r[f"dn_{H}"] for r in rows if r.get(f"dn_{H}") is not None]
        rt = [r[f"ret_{H}"] for r in rows if r.get(f"ret_{H}") is not None]
        if not up:
            continue
        lines.append(
            f"  {H:>2}m  up med {q(up,.5):+.2f}% (p25 {q(up,.25):+.2f} p75 {q(up,.75):+.2f})"
            f"  dn med {q(dn,.5):+.2f}%  ret mean {mean(rt):+.2f}% med {q(rt,.5):+.2f}%"
            f"  up≥0.5% {sum(x>=0.5 for x in up)/len(up):.0%}"
            f"  up≥1% {sum(x>=1 for x in up)/len(up):.0%}"
            f"  up≥2% {sum(x>=2 for x in up)/len(up):.0%}  n={len(up)}")
    for X in TOUCH:
        d = [r[f"tp_{X}"] for r in rows if r.get(f"tp_{X}") is not None]
        if d:
            lines.append(f"  first-touch ±{X}%: up first {mean(d):.1%} (n={len(d)}, "
                         f"{sum(r.get(f'tp_{X}') is None for r in rows)} untouched)")
    return lines


def _subject(ver: str) -> str:
    """One-line commit subject for a git_version like '599e94e+'."""
    h = ver.rstrip("+")
    if not re.match(r"^[0-9a-f]{6,40}$", h):
        return ""
    try:
        return subprocess.run(["git", "log", "-1", "--format=%s", h], cwd=ROOT,
                              capture_output=True, text=True, timeout=5
                              ).stdout.strip()[:60]
    except Exception:
        return ""


def group_table(by: str, rows: list, fills: list, ctrl_r30: dict, tgt: str) -> list:
    """Per-group realized R (every fill) and bar runway (fills with bars).

    ``vs ctl`` is ret_30 minus the same symbol-day's post-first-fill median,
    so a version that merely traded a better day is not credited with it.
    Versions are listed in the order they first traded; a trailing '+'
    means the tree was dirty when it ran.
    """
    allr: dict = {}
    for r in rows:
        allr.setdefault(r["_group"][by], []).append(r)
    scored: dict = {}
    for f in fills:
        scored.setdefault(f["group"][by], []).append(f)
    if by == "git_version":
        order = sorted(allr, key=lambda g: min(r["entry_time"] for r in allr[g]))
    else:
        order = sorted(allr, key=lambda g: -len(allr[g]))
    out = [f"\n== BY {by.upper()} (realized R on every fill; runway on fills with bars) ==",
           f"  {'group':<12}{'fills':>6}{'win%':>6}{'avg R':>8}{'sum R':>7}"
           f"{'bars':>6}{tgt + ' med':>12}{'tp1':>6}{'ret30':>7}{'vs ctl':>8}{'t':>6}"
           f"  {'first fill':<12}{'last fill':<12}"]
    for g in order:
        rs = allr[g]
        R = [r["realized_r_multiple"] for r in rs
             if isinstance(r.get("realized_r_multiple"), (int, float))]
        fs = scored.get(g, [])
        u = [f[tgt] for f in fs if f.get(tgt) is not None]
        tp = [f["tp_1.0"] for f in fs if f.get("tp_1.0") is not None]
        r30 = [f["ret_30"] for f in fs if f.get("ret_30") is not None]
        d = [f["ret_30"] - ctrl_r30[(f["symbol"], f["day"])] for f in fs
             if f.get("ret_30") is not None and (f["symbol"], f["day"]) in ctrl_r30]
        t = None
        if len(d) >= 5 and statistics.stdev(d) > 0:
            t = mean(d) / (statistics.stdev(d) / math.sqrt(len(d)))
        ts = [r["entry_time"] for r in rs]
        fmt = lambda x: time.strftime("%m-%d %H:%M", time.localtime(x))
        nan = float("nan")
        line = (f"  {g[:12]:<12}{len(rs):>6}"
                f"{(sum(x > 0 for x in R) / len(R)) if R else nan:>6.0%}"
                f"{mean(R) if R else nan:>+8.3f}{sum(R):>+7.2f}{len(fs):>6}"
                f"{q(u, .5) if u else nan:>+11.2f}%{mean(tp) if tp else nan:>6.0%}"
                f"{mean(r30) if r30 else nan:>+7.2f}{mean(d) if d else nan:>+8.2f}"
                f"{t if t is not None else nan:>+6.1f}"
                f"  {fmt(min(ts)):<12}{fmt(max(ts)):<12}")
        if by == "git_version":
            line += _subject(g)
        if len(rs) < 30:
            line += "  [n<30: anecdote]"
        out.append(line)
    return out


# ── main ────────────────────────────────────────────────────────────────
ENTRY_FEATS = ("score", "rvol", "pct_change", "cm_rsi", "pctr", "proximity_pct",
               "entry_hour_et", "dwell_sec", "spread_r")
TOP_FEATS = ("entry_exhaustion", "entry_slippage_r")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-08-04")
    ap.add_argument("--json")
    ap.add_argument("--by", action="append", choices=("source", "git_version", "config_fp"),
                    help="group fills by this outcome field (repeatable; default source)")
    ap.add_argument("--target", default="up_15",
                    help="runway metric features are tested against")
    args = ap.parse_args()

    rows = []
    for line in open(os.path.join(ROOT, "ai_reports", "outcomes.jsonl")):
        try:
            r = json.loads(line)
        except Exception:
            continue
        sym = str(r.get("symbol") or "")
        et, px = r.get("entry_time"), r.get("entry_price")
        if not SYM_RE.match(sym) or not isinstance(et, (int, float)) or not px:
            continue
        if bars.day_of(et) < args.since:
            continue
        m = bars.et_minutes(et)
        if m < 9 * 60 + 31 or m > 15 * 60 + 30:
            continue  # premarket / last half hour: no forward window
        rows.append(r)
    print(f"fills in window: {len(rows)}", file=sys.stderr)

    cache = _load_cache()
    want: dict = {}
    for r in rows:
        want.setdefault(bars.day_of(r["entry_time"]), set()).add(r["symbol"])
    fetch_days(want, cache)

    fills, ctrl = [], []
    for r in rows:
        r["_group"] = {
            "source": str((r.get("features") or {}).get("source") or r.get("source")),
            "git_version": str(r.get("git_version") or "unknown"),
            "config_fp": str(r.get("config_fp") or "unknown"),
        }
        B = cache.get((r["symbol"], bars.day_of(r["entry_time"])))
        if not B:
            continue
        i = bars.index_at(B[0], r["entry_time"])
        if i < 0:
            continue
        s = score_path(B, i, float(r["entry_price"]))
        if not s:
            continue
        feats = dict(bar_features(B, i, float(r["entry_price"])))
        fe = r.get("features") or {}
        for k in ENTRY_FEATS:
            if isinstance(fe.get(k), (int, float)):
                feats[k] = float(fe[k])
        for k in TOP_FEATS:
            if isinstance(r.get(k), (int, float)):
                feats[k] = float(r[k])
        s.update(symbol=r["symbol"], day=bars.day_of(r["entry_time"]),
                 t0=float(r["entry_time"]),
                 source=fe.get("source") or r.get("source"),
                 r=r.get("realized_r_multiple"), feats=feats,
                 group=r["_group"],
                 rel_up=None)
        if feats.get("vol_1m_15"):
            s["rel_up"] = (s["up_15"] / feats["vol_1m_15"]
                           if s.get("up_15") is not None else None)
        fills.append(s)

    # Same symbol-day control, every 5th minute to keep it cheap. Only
    # minutes AFTER the name's first fill that day: a name joins the book
    # because it already ran, and whole-session minutes would hand the
    # control that run-up (the WITHIN hindsight that inflated
    # admission_null to 4.4σ on 2026-08-20).
    tgt = args.target
    first_fill: dict = {}
    for f in fills:
        k = (f["symbol"], f["day"])
        first_fill[k] = min(first_fill.get(k, f["t0"]), f["t0"])
    ctrl, ctrl_med, ctrl_r30 = [], {}, {}
    for key in sorted(first_fill):
        B = cache[key]
        vals, r30 = [], []
        for i in range(0, len(B[0]), 5):
            m = bars.et_minutes(B[0][i])
            if B[0][i] >= first_fill[key] and m <= 15 * 60 + 30:
                s = score_path(B, i, B[4][i])
                if s:
                    ctrl.append(s)
                    if s.get(tgt) is not None:
                        vals.append(s[tgt])
                    if s.get("ret_30") is not None:
                        r30.append(s["ret_30"])
        if vals:
            ctrl_med[key] = statistics.median(vals)
        if r30:
            ctrl_r30[key] = statistics.median(r30)

    out = []
    out += summarize(fills, "DESK FILLS (from fill price)")
    out += summarize(ctrl, "CONTROL: same symbol-days after first fill, every 5th minute (from close)")

    # timing: each fill against its own symbol-day's median control
    diffs = [f[tgt] - ctrl_med[(f["symbol"], f["day"])] for f in fills
             if f.get(tgt) is not None and (f["symbol"], f["day"]) in ctrl_med]
    if len(diffs) > 5:
        se = statistics.stdev(diffs) / math.sqrt(len(diffs))
        out.append(f"\n== TIMING: fill {tgt} minus same-symbol-day median control =="
                   f"\n  mean {mean(diffs):+.3f}pp  median {q(diffs,.5):+.3f}pp"
                   f"  t={mean(diffs)/se:+.2f}  n={len(diffs)}")

    for by in (args.by or ["source"]):
        out += group_table(by, rows, fills, ctrl_r30, tgt)

    # feature terciles with split-half
    days_sorted = sorted({f["day"] for f in fills})
    mid = days_sorted[len(days_sorted) // 2]
    fnames = sorted({k for f in fills for k in f["feats"]})
    res = []
    for fn in fnames:
        pts = [(f["feats"][fn], f[tgt], f["day"], f.get("tp_1.0"), f.get("ret_30"))
               for f in fills if fn in f["feats"] and f.get(tgt) is not None]
        if len(pts) < 60:
            continue
        xs = sorted(p[0] for p in pts)
        a, b = q(xs, 1 / 3), q(xs, 2 / 3)
        lo = [p for p in pts if p[0] <= a]
        hi = [p for p in pts if p[0] >= b]
        md = [p for p in pts if a < p[0] < b]
        t_all = tstat([p[1] for p in hi], [p[1] for p in lo])
        t1 = tstat([p[1] for p in hi if p[2] < mid], [p[1] for p in lo if p[2] < mid])
        t2 = tstat([p[1] for p in hi if p[2] >= mid], [p[1] for p in lo if p[2] >= mid])
        tp = lambda g: mean([p[3] for p in g if p[3] is not None]) or 0.0
        r30 = lambda g: mean([p[4] for p in g if p[4] is not None]) or 0.0
        stable = (t_all is not None and abs(t_all) >= 2 and t1 is not None and t2 is not None
                  and (t1 > 0) == (t2 > 0) == (t_all > 0))
        res.append(dict(feature=fn, n=len(pts), cut_lo=a, cut_hi=b,
                        lo=mean([p[1] for p in lo]),
                        mid=mean([p[1] for p in md]) if md else float("nan"),
                        hi=mean([p[1] for p in hi]),
                        tp_lo=tp(lo), tp_hi=tp(hi), r30_lo=r30(lo), r30_hi=r30(hi),
                        t=t_all, t_h1=t1, t_h2=t2, stable=stable))
    res.sort(key=lambda d: -abs(d["t"] or 0))
    out.append(f"\n== FEATURES: mean {tgt} by tercile, top vs bottom (split at {mid}) ==")
    out.append(f"  {'feature':<18}{'n':>5} {'lo':>7}{'mid':>7}{'hi':>7}  {'tp1 lo/hi':>11}"
               f"  {'ret30 lo/hi':>13}  {'t':>6}{'t_h1':>6}{'t_h2':>6}  cuts")
    for d in res:
        out.append(
            f"  {d['feature']:<18}{d['n']:>5} {d['lo']:>+7.2f}{d['mid']:>+7.2f}{d['hi']:>+7.2f}"
            f"  {d['tp_lo']:>5.0%}/{d['tp_hi']:<5.0%}  {d['r30_lo']:>+6.2f}/{d['r30_hi']:<+6.2f}"
            f"  {d['t'] or 0:>+6.2f}{d['t_h1'] or 0:>+6.2f}{d['t_h2'] or 0:>+6.2f}"
            f"  {d['cut_lo']:.3g}|{d['cut_hi']:.3g}{'  STABLE' if d['stable'] else ''}")

    print("\n".join(out))
    if args.json:
        json.dump(dict(fills=[{k: v for k, v in f.items()} for f in fills],
                       features=res), open(args.json, "w"), default=str)


if __name__ == "__main__":
    main()
