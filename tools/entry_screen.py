#!/usr/bin/env python3
"""entry_screen.py — does any entry trigger beat a random minute in our names?

The bar every rule has to clear is the CONTROL: a random minute (every 5th)
in the same name-days. On 09-16..09-22 book names >= $10 went +1% before -1%
56% of the time from a random minute, while the square the desk arms on got
52%. A trigger is only worth shipping if it beats the control, in BOTH date
halves, by more than noise.

POPULATION  names the book admitted (admit_funnel kept_symbols) on the given
            days, price >= $10 at the event. SIP 1m bars incl. extended hours
            (04:00-16:00) so the 112-bar slow %R is valid at the open.
EVENTS      evaluated 09:40-15:30 at the close of the signal bar (the moment
            it is known), at most one event per rule per name per 15 minutes.
METRICS     up1 (+1% before -1%), up05, ret at 15/30/60m, best move in 30m.
            "net30" subtracts a 0.20% round trip (today's measured cost).
VERDICT     PASS = up1 and ret30 both above control in each half, and the
            pooled up1 difference has z >= 2. Anything else is not evidence.

Not a tape: 1-minute bars, fills at the signal bar's close, no spread.

USAGE (on the mini)
    .venv/bin/python tools/entry_screen.py
    .venv/bin/python tools/entry_screen.py --days 2026-09-16 2026-09-17 ...
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import os
import pickle
import statistics
import sys
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)
import bars  # noqa: E402
import runway_study as rs  # noqa: E402

EXT_CACHE = os.path.join(ROOT, "ai_reports", "ext_bars_cache.pkl")
DEFAULT_DAYS = ["2026-09-16", "2026-09-17", "2026-09-18", "2026-09-21", "2026-09-22"]
COOL = 15 * 60
COST = 0.20
MIN_PX = 10.0


# ── data ────────────────────────────────────────────────────────────────
def admitted(days: list[str], first: dict | None = None) -> dict:
    """{day: {sym}}; fills *first* with {(sym, day): first admit ts}."""
    out = collections.defaultdict(set)
    t0 = time.mktime(time.strptime(min(days), "%Y-%m-%d"))
    dset = set(days)
    for line in open(os.path.join(ROOT, "ai_reports", "events.jsonl")):
        if '"admit_funnel"' not in line:
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        ts = float(e.get("ts") or 0)
        if ts < t0 or bars.day_of(ts) not in dset:
            continue
        for s in e.get("kept_symbols") or []:
            if rs.SYM_RE.match(str(s)):
                out[bars.day_of(ts)].add(str(s))
                if first is not None:
                    k = (str(s), bars.day_of(ts))
                    first[k] = min(first.get(k, ts), ts)
    return out


def fetch_ext(want: dict) -> dict:
    """{(sym, day): DataFrame of 1m OHLCV 04:00-16:00}, cached on disk."""
    try:
        with open(EXT_CACHE, "rb") as f:
            cache = pickle.load(f)
    except Exception:
        cache = {}
    cl = bars.client()
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    import pandas as pd
    for day, syms in sorted(want.items()):
        need = sorted(s for s in syms if (s, day) not in cache)
        d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=bars.ET)
        for i in range(0, len(need), 100):
            chunk = need[i:i + 100]
            try:
                df = cl.get_stock_bars(StockBarsRequest(
                    symbol_or_symbols=chunk, timeframe=TimeFrame(1, TimeFrameUnit.Minute),
                    start=d.replace(hour=4, minute=0).astimezone(timezone.utc),
                    end=d.replace(hour=16, minute=0).astimezone(timezone.utc),
                    limit=2_000_000, extended_hours=True, feed=DataFeed.SIP)).df
            except Exception as e:  # noqa: BLE001
                print(f"  ext fail {day}: {str(e)[:80]}", file=sys.stderr)
                continue
            got = {}
            if df is not None and not df.empty:
                if not isinstance(df.index, pd.MultiIndex):
                    df = pd.concat({chunk[0]: df}, names=["symbol"])
                for s in df.index.get_level_values("symbol").unique():
                    got[str(s)] = df.xs(s, level="symbol").sort_index()[
                        ["open", "high", "low", "close", "volume"]]
            for s in chunk:
                cache[(s, day)] = got.get(s)
            print(f"  ext {day}: {len(got)}/{len(chunk)}", file=sys.stderr)
            time.sleep(1.0)
    with open(EXT_CACHE, "wb") as f:
        pickle.dump(cache, f)
    return cache


# ── rules ───────────────────────────────────────────────────────────────
def indicators(df, cfg, spy):
    """Per-bar arrays every rule reads. RTH-anchored VWAP and ranges."""
    import numpy as np
    import signals
    x = signals.compute_percent_r_exhaustion(df, cfg)
    ts = np.array([t.timestamp() for t in df.index])
    mins = np.array([bars.et_minutes(t) for t in ts])
    rth = mins >= 570
    c, h, l, v = (df[k].to_numpy(dtype=float) for k in ("close", "high", "low", "volume"))
    pv = np.where(rth, c * v, 0.0).cumsum()
    vv = np.where(rth, v, 0.0).cumsum()
    vwap = np.where(vv > 0, pv / np.maximum(vv, 1e-9), np.nan)
    first = int(np.argmax(rth)) if rth.any() else len(ts)
    day_open = df["open"].to_numpy()[first] if first < len(ts) else np.nan
    spy_rel = np.full(len(ts), np.nan)
    if spy is not None and first < len(ts):
        s_ts = np.array([t.timestamp() for t in spy.index])
        s_c = spy["close"].to_numpy(dtype=float)
        s_first = int(np.argmax(np.array([bars.et_minutes(t) for t in s_ts]) >= 570))
        s_open = spy["open"].to_numpy()[s_first]
        j = np.searchsorted(s_ts, ts, side="right") - 1
        ok = j >= 0
        spy_move = np.where(ok, s_c[np.clip(j, 0, None)] / s_open - 1, np.nan)
        spy_rel = (c / day_open - 1 - spy_move) * 100
    return {"ts": ts, "mins": mins, "rth": rth, "c": c, "h": h, "l": l, "v": v,
            "vwap": vwap, "first": first,
            "fast": x["s_percentR"].to_numpy(), "slow": x["l_percentR"].to_numpy(),
            "tight": x["rte_tight"].fillna(False).to_numpy(),
            "conf": float(cfg.get("rte_confluence_max", 15) or 15),
            "spy_rel": spy_rel}


def fire(ind, i: int) -> list[str]:
    """Rule names that fire on bar i (bar i is complete)."""
    import numpy as np
    c, h, l, v, vw = ind["c"], ind["h"], ind["l"], ind["v"], ind["vwap"]
    f, s = ind["fast"], ind["slow"]
    first, m = ind["first"], ind["mins"][i]
    out = []
    if i < first + 10 or any(np.isnan(x) for x in (f[i], s[i], f[i - 1], s[i - 1],
                                                    f[i - 2], s[i - 2], vw[i])):
        return out
    above = c[i] > vw[i]
    lo30 = max(first, i - 30)
    vol_pace = v[i] / max(1e-9, float(np.mean(v[lo30:i]))) if i > lo30 else 0.0
    # 1. what the desk arms on today
    if ind["tight"][i] and not ind["tight"][i - 1]:
        out.append("square")
    # 2. fast %R crosses -50 up with the slow line rising
    if f[i - 1] <= -50 < f[i] and s[i] > s[i - 1]:
        out.append("mid_rise")
        if above:
            out.append("mid_rise_vwap")
    # 3. breakout: close above the prior 30 RTH bars' high on 1.5x volume pace
    if i - lo30 >= 20 and c[i] > float(np.max(h[lo30:i])) and vol_pace >= 1.5:
        out.append("breakout30")
    # 4. VWAP reclaim after holding under it
    if (c[i] > vw[i] and c[i - 1] <= vw[i - 1]
            and sum(c[k] < vw[k] for k in range(max(first, i - 10), i)) >= 5):
        out.append("vwap_reclaim")
    # 5. pullback to VWAP inside an uptrend, then a turn up
    if (i - first >= 30 and vw[i] > vw[i - 30]
            and sum(c[k] > vw[k] for k in range(i - 30, i)) >= 20
            and min(l[i - 3:i + 1]) <= vw[i] * 1.001 and c[i] > c[i - 1] and above):
        out.append("vwap_pullback")
    # 6. opening-range breakouts (first close above the OR high)
    for n, name in ((15, "orb15"), (30, "orb30")):
        if m >= 570 + n and i - first >= n:
            orh = float(np.max(h[first:first + n]))
            if c[i] > orh and max(c[first + n:i], default=-1) <= orh:
                out.append(name)
    # 7. three rising 5-minute lows, fired at a 5-minute boundary
    if (m - 570) % 5 == 4 and i - first >= 15:
        lows = [min(l[k - 4:k + 1]) for k in (i - 10, i - 5, i)]
        if lows[0] < lows[1] < lows[2] and above:
            out.append("higher_lows")
    # 9. the desk's live heating arm (ai_entry_watch._exhaustion_allows_buy,
    #    square arm off): exhaustion >= 40% (fast >= -60), fast and slow both
    #    rising, |fast - slow| <= confluence, prints rising (1m close up).
    #    "heating_cap80" also refuses exhaustion >= 80% (the overbought zone),
    #    i.e. ai_watch_exhaustion_heat_max_pct = 80.
    conf = ind["conf"]
    heat = (f[i] >= -60 and f[i] > f[i - 1] and s[i] > s[i - 1]
            and abs(f[i] - s[i]) <= conf and c[i] > c[i - 1])
    heat_prev = (f[i - 1] >= -60 and f[i - 1] > f[i - 2] and s[i - 1] > s[i - 2]
                 and abs(f[i - 1] - s[i - 1]) <= conf and c[i - 1] > c[i - 2])
    if heat and not heat_prev:
        out.append("heating_live")
        if f[i] < -20:
            out.append("heating_cap80")
    # 8. strength vs SPY turning up: rel move up >= 0.5pp in 15m and positive
    r = ind["spy_rel"]
    if i - 15 >= first and not np.isnan(r[i]) and not np.isnan(r[i - 15]):
        if r[i] > 0 and r[i] - r[i - 15] >= 0.5 and r[i - 1] - r[i - 16] < 0.5:
            out.append("rs_turn")
    return out


def to_B(df):
    return ([t.timestamp() for t in df.index], list(df["open"]), list(df["high"]),
            list(df["low"]), list(df["close"]), list(df["volume"]))


# ── report ──────────────────────────────────────────────────────────────
def stats(rows):
    def m(k):
        v = [r[k] for r in rows if r.get(k) is not None]
        return rs.mean(v) if v else None
    return {"n": len(rows), "up1": m("tp_1.0"), "up05": m("tp_0.5"), "r15": m("ret_15"),
            "r30": m("ret_30"), "r60": m("ret_60"), "up30": m("up_30")}


def z_prop(p1, n1, p0, n0):
    if None in (p1, p0) or n1 < 5 or n0 < 5:
        return None
    p = (p1 * n1 + p0 * n0) / (n1 + n0)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n0))
    return (p1 - p0) / se if se > 0 else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", nargs="+", default=DEFAULT_DAYS)
    ap.add_argument("--all-seen", action="store_true",
                    help="every name the seed pipeline saw (admit ledger), not "
                         "only admitted ones: more events, same rules")
    args = ap.parse_args()
    from config import load_config
    cfg = load_config()
    # Events count only AFTER the name was on the book (or first seen, with
    # --all-seen). Without this, a name admitted at 11:00 because it broke
    # out at 09:45 and ran credits that breakout with the run that got it
    # admitted: orb15 read 74.6% on admitted names and 55.0% on all names.
    first: dict = {}
    want = admitted(args.days, first)
    if args.all_seen:
        for d in args.days:
            p = os.path.join(ROOT, "ai_reports", "admit_ledger", f"{d}.jsonl")
            if not os.path.exists(p):
                continue
            for line in open(p):
                i = line.find('"symbol": "')
                if i < 0:
                    continue
                sym = line[i + 11:line.find('"', i + 11)]
                if not rs.SYM_RE.match(sym):
                    continue
                want[d].add(sym)
                j = line.find('"ts": ')
                if j >= 0:
                    try:
                        ts = float(line[j + 6:line.find(",", j)])
                    except ValueError:
                        continue
                    first[(sym, d)] = min(first.get((sym, d), ts), ts)
    for d in args.days:
        want[d].add("SPY")
    print("admitted name-days:", sum(len(v) for v in want.values()) - len(args.days),
          file=sys.stderr)
    cache = fetch_ext(want)
    half1 = set(sorted(args.days)[:(len(args.days) + 1) // 2])

    res = collections.defaultdict(lambda: {"h1": [], "h2": []})
    for day, syms in want.items():
        spy = cache.get(("SPY", day))
        for s in syms:
            if s == "SPY":
                continue
            df = cache.get((s, day))
            if df is None or len(df) < 150:
                continue
            ind = indicators(df, cfg, spy)
            B = to_B(df)
            half = "h1" if day in half1 else "h2"
            last = collections.defaultdict(lambda: -1e18)
            t_on = first.get((s, day))
            for i in range(1, len(B[0])):
                mm = ind["mins"][i]
                if mm < 9 * 60 + 40 or mm > 15 * 60 + 30 or B[4][i] < MIN_PX:
                    continue
                if t_on is None or ind["ts"][i] < t_on:
                    continue
                names = fire(ind, i)
                if i % 5 == 0:
                    names = names + ["~control"]
                for name in names:
                    if name != "~control" and ind["ts"][i] - last[name] < COOL:
                        continue
                    last[name] = ind["ts"][i]
                    sc = rs.score_path(B, i, B[4][i])
                    if sc:
                        res[name][half].append(sc)

    ctrl = {h: stats(res["~control"][h]) for h in ("h1", "h2")}
    ctrl_all = stats(res["~control"]["h1"] + res["~control"]["h2"])
    order = ["~control"] + sorted((k for k in res if k != "~control"),
                                  key=lambda k: -(stats(res[k]["h1"] + res[k]["h2"])["up1"] or 0))
    pct = lambda x: "—" if x is None else f"{x:.1%}"
    sgn = lambda x: "—" if x is None else f"{x:+.3f}"
    print(f"ENTRY SCREEN  days {', '.join(sorted(args.days))}  (halves split after "
          f"{max(half1)})  price >= ${MIN_PX:.0f}  cost {COST:.2f}%/round trip\n")
    print(f"{'rule':<15}{'n':>6}{'up1':>7}{'Δctl':>7}{'z':>6}{'up1 h1/h2':>13}"
          f"{'r15':>8}{'r30':>8}{'r60':>8}{'net30':>8}{'r30 h1/h2':>16}{'up30':>8}  verdict")
    for k in order:
        a = stats(res[k]["h1"] + res[k]["h2"])
        s1, s2 = stats(res[k]["h1"]), stats(res[k]["h2"])
        if not a["n"]:
            continue
        d = (a["up1"] - ctrl_all["up1"]) if (a["up1"] is not None and ctrl_all["up1"] is not None) else None
        z = z_prop(a["up1"], a["n"], ctrl_all["up1"], ctrl_all["n"]) if k != "~control" else None
        verdict = ""
        if k != "~control":
            beats = all(
                s[h] is not None and c[h] is not None and s[h] > c[h]
                for s, c in ((s1, ctrl["h1"]), (s2, ctrl["h2"])) for h in ("up1", "r30"))
            verdict = ("PASS" if beats and z is not None and z >= 2
                       else "worse" if (z is not None and z <= -2) else "no evidence")
            if a["n"] < 100:
                verdict += " (n<100)"
        net = (a["r30"] - COST) if a["r30"] is not None else None
        print(f"{k:<15}{a['n']:>6}{pct(a['up1']):>7}"
              f"{('—' if d is None or k == '~control' else f'{d * 100:+.1f}'):>7}"
              f"{('—' if z is None else f'{z:+.1f}'):>6}"
              f"{pct(s1['up1']) + '/' + pct(s2['up1']):>13}"
              f"{sgn(a['r15']):>8}{sgn(a['r30']):>8}{sgn(a['r60']):>8}{sgn(net):>8}"
              f"{sgn(s1['r30']) + '/' + sgn(s2['r30']):>16}{sgn(a['up30']):>8}  {verdict}")


if __name__ == "__main__":
    main()
