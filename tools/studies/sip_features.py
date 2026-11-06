#!/usr/bin/env python3
"""Weekend item 2 (docs/WEEKEND_AGENDA_2026-11-07.md): do IEX bars hide the entry signal? EXPLORATORY, read-only, free historical bars.

For every desk entry since 2026-10-15 (the clean volume-ratio era) it rebuilds the entry-shape measures that the forward tests log
(rs_rise15_bp, rs_er15, rs_pos15, rs_pos30, rs_rng15_bp, rs_since_hi15, rs_near_hi15) twice from historical 1-minute bars: once from the
SIP feed (dense) and once from the IEX feed (sparse, what the live desk reads), with the SAME formulas and the SAME gap rules as
ai_entry_watch._range_shape_fields (closed bars only, the last n bars must span <= n+3 minutes). Then, per measure and feed:
  coverage (share of entries with a valid value), AUC of never-green on the measure (0.5 = none), AUC in each date half,
  never-green rate and mean net bp (exit vs entry less 3 bp) in the low / mid / high tercile.
It answers: does SIP sharpen a measure (AUC further from 0.5, both halves) or only fill gaps (coverage)? Nothing here is a gate. The forward
tests keep their own definitions; a result here may NOT change them.

    .venv/bin/python tools/studies/sip_features.py fetch     # mini, after 16:30 ET weekdays or any time on weekends; caches bars
    .venv/bin/python tools/studies/sip_features.py report    # table from the cache -> docs/SIP_FEATURES.md
"""
from __future__ import annotations

import pickle
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))
ET = ZoneInfo("America/New_York")
CACHE = ROOT / "ai_reports" / "sip_features"
MEASURES = ("rs_rise15_bp", "rs_er15", "rs_pos15", "rs_pos30", "rs_rng15_bp", "rs_since_hi15", "rs_near_hi15")


def shape(bars, px, t):
    """The live formulas (ai_entry_watch._range_shape_fields) on bars [(epoch, o, h, l, c, v)] sorted. Pure. {} when not computable."""
    try:
        if not bars or len(bars) < 40 or not px or px <= 0:
            return {}
        cut = t - (t % 60)
        b = [x for x in bars if float(x[0]) < cut]
        if len(b) < 30 or float(b[-1][0]) < cut - 180:
            return {}
        out = {}
        for n in (15, 30):
            w = b[-n:]
            if float(w[-1][0]) - float(w[0][0]) > 60 * (n + 3):
                continue
            hi = max(float(x[2]) for x in w)
            lo = min(float(x[3]) for x in w)
            hi, lo = max(hi, px), min(lo, px)
            out[f"rs_pos{n}"] = round((px - lo) / (hi - lo), 3) if hi > lo else None
            if n == 15:
                cl = [float(x[4]) for x in w]
                out["rs_rng15_bp"] = round((hi - lo) / px * 1e4, 1)
                out["rs_rise15_bp"] = round((px / cl[0] - 1) * 1e4, 1)
                path = sum(abs(cl[i] - cl[i - 1]) for i in range(1, len(cl)))
                out["rs_er15"] = round(abs(cl[-1] - cl[0]) / path, 3) if path > 0 else 0.0
                cmax = max(cl)
                out["rs_since_hi15"] = len(cl) - 1 - cl.index(cmax)
                out["rs_near_hi15"] = sum(1 for c_ in cl if c_ >= cmax * 0.999)
        return out
    except Exception:  # noqa: BLE001
        return {}


def load_trades():
    from early_path import trades_since
    tr, _ = trades_since()
    return tr


def fetch():
    import alpaca_api as aa
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from config import load_config_ro
    client = aa.connect_data_client(dict(load_config_ro() or {}))
    CACHE.mkdir(parents=True, exist_ok=True)
    tr = load_trades()
    by_day = {}
    for t in tr:
        by_day.setdefault(t["day"], set()).add(t["sym"])
    now = datetime.now(ET)
    done = 0
    for day, syms in sorted(by_day.items()):
        if day >= now.strftime("%Y-%m-%d"):
            continue                                     # today's bars are not final / SIP is delayed 15 min on the free plan
        for feed in ("sip", "iex"):
            f = CACHE / f"{day}_{feed}.pkl"
            if f.exists():
                continue
            s0 = datetime.fromisoformat(day + "T04:00:00").replace(tzinfo=ET)
            s1 = datetime.fromisoformat(day + "T16:00:00").replace(tzinfo=ET)
            out, sl = {}, sorted(syms)
            for i in range(0, len(sl), 20):
                try:
                    df = client.get_stock_bars(StockBarsRequest(symbol_or_symbols=sl[i:i + 20], timeframe=TimeFrame.Minute, start=s0, end=s1,
                                                                feed=DataFeed.SIP if feed == "sip" else DataFeed.IEX)).df
                except Exception as e:  # noqa: BLE001
                    print("ERR", day, feed, str(e)[:100])
                    continue
                if df is None or df.empty:
                    continue
                for s in df.index.get_level_values(0).unique():
                    x = df.xs(s, level=0)
                    out[s] = [(t.timestamp(), float(o), float(h), float(lo), float(c), float(v)) for t, o, h, lo, c, v in
                              zip(x.index, x["open"], x["high"], x["low"], x["close"], x["volume"])]
                time.sleep(0.3)
            pickle.dump(out, open(f, "wb"))
            done += 1
    print(f"fetched {done} day-feed files; trades {len(tr)}; days {len(by_day)}")
    return 0


def build_rows():
    tr = load_trades()
    cache = {}
    rows = []
    for t in tr:
        r = {"d": t["day"], "ng": t["never_green"], "bp": t["net_bp"] - 3.0, "sip": {}, "iex": {}}
        for feed in ("sip", "iex"):
            f = CACHE / f"{t['day']}_{feed}.pkl"
            if (t["day"], feed) not in cache:
                cache[(t["day"], feed)] = pickle.load(open(f, "rb")) if f.exists() else None
            c = cache[(t["day"], feed)]
            if c is None:
                r[feed] = None
                continue
            r[feed] = shape(c.get(t["sym"], []), t["entry"], t["fill_ts"])
        if r["sip"] is not None and r["iex"] is not None:
            rows.append(r)
    return rows


def report():
    from ng_profile import auc
    rows = build_rows()
    if not rows:
        print("no cached bars: run 'fetch' first")
        return 1
    days = sorted({r["d"] for r in rows})
    mid = days[len(days) // 2]
    lines = [f"# SIP vs IEX entry measures (exploratory), written {datetime.now(ET):%Y-%m-%d %H:%M} ET", "",
             f"{len(rows)} desk entries {days[0]} to {days[-1]}, never-green {100 * sum(r['ng'] for r in rows) / len(rows):.0f}%. Same formulas and gap rules as the live logger. "
             "AUC < 0.5 means never-greens have LOWER values. Nothing here is a gate or changes a forward test.", "",
             "| measure | feed | coverage | AUC (NG) | half 1 / 2 | NG rate low / mid / high third | net bp low / mid / high |", "|---|---|---|---|---|---|---|"]
    for m in MEASURES:
        for feed in ("sip", "iex"):
            rs = [r for r in rows if r[feed] and r[feed].get(m) is not None]
            cov = len(rs) / len(rows)
            if len(rs) < 60:
                lines.append(f"| {m} | {feed.upper()} | {100 * cov:.0f}% | too few | | | |")
                continue
            pos = [r[feed][m] for r in rs if r["ng"]]
            neg = [r[feed][m] for r in rs if not r["ng"]]
            a = auc(pos, neg)
            ha = []
            for h in (lambda d: d < mid, lambda d: d >= mid):
                p = [r[feed][m] for r in rs if h(r["d"]) and r["ng"]]
                q = [r[feed][m] for r in rs if h(r["d"]) and not r["ng"]]
                ha.append(auc(p, q) if len(p) >= 15 and len(q) >= 15 else float("nan"))
            v = sorted(r[feed][m] for r in rs)
            c1, c2 = v[len(v) // 3], v[(2 * len(v)) // 3]
            g = {"low": [r for r in rs if r[feed][m] <= c1], "mid": [r for r in rs if c1 < r[feed][m] <= c2], "high": [r for r in rs if r[feed][m] > c2]}
            ng = " / ".join(f"{100 * sum(x['ng'] for x in g[k]) / max(1, len(g[k])):.0f}%" for k in ("low", "mid", "high"))
            nt = " / ".join(f"{sum(x['bp'] for x in g[k]) / max(1, len(g[k])):+.1f}" for k in ("low", "mid", "high"))
            lines.append(f"| {m} | {feed.upper()} | {100 * cov:.0f}% | {a:.2f} | {ha[0]:.2f} / {ha[1]:.2f} | {ng} | {nt} |")
    out = "\n".join(lines) + "\n"
    (ROOT / "docs" / "SIP_FEATURES.md").write_text(out)
    print(out)
    return 0


def main(argv=None):
    a = (argv if argv is not None else sys.argv[1:]) or ["report"]
    return fetch() if a[0] == "fetch" else report()


if __name__ == "__main__":
    raise SystemExit(main())
