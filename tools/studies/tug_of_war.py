#!/usr/bin/env python3
"""tug_of_war.py - the operator's candle "tug of war" as an entry filter (11/11, two annotated charts).

His reading: a candle's body is the middle of the rope and the wicks are the two teams. If the middle of the rope is in the
same zone as five minutes ago, do not enter (SIDEWAYS). If the bears are pulling it (bodies stepping down, long upper
wicks), do not enter (BEARS). Enter only while the bulls are pulling. He also asks whether the count of green bars just
before the open matters, as a sign of buying into a stall.

Measures, all from the last 5 completed 1-minute bars (decision at the close of bar m, bars m-4..m, nothing later):
  rope   = (body middle now - body middle 5 bars ago) / average bar height of those bars
           BULLS >= +1.0   SIDEWAYS between -1.0 and +1.0   BEARS <= -1.0
  wicks  = upper wick share = sum of upper wicks / sum of all wicks   (BEAR wicks >= 0.65, BULL wicks <= 0.35)
  greens = how many of the 5 bars closed up;  full = how many were "full" (body at least 70% of the bar)
  higher = how many of the last 4 steps made a higher body middle

Two tests:
  A. the desk's current arm on the warehouse (tools/studies/signals/current_square.py), split by each measure: does the
     arm do better while the bulls are pulling, and worse sideways or with bears? Read PER TRADE.
  B. the desk's OWN entries since 10/08 (ai_reports/outcomes.jsonl), state measured on warehouse bars up to the minute
     before the entry: what would "prevent enter" have kept and dropped?

    .venv/bin/python tools/studies/tug_of_war.py [--holds 3,15]            # mini
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import warnings
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE / "signals")]
import harness as H
from _lib import roll
from pattern_battery import _bars, _lag

ET = ZoneInfo("America/New_York")
N = 5


def measures(day: dict) -> dict:
    """Each (n, 390), column m built from bars m-4..m only."""
    with np.errstate(all="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        o, h, l, c, _v, live = _bars(day)
        mid = (o + c) / 2.0
        height = h - l
        avg_h = roll(height, N, np.nanmean)
        rope = (mid - _lag(mid, N - 1)) / np.where(avg_h > 0, avg_h, np.nan)
        up, dn = h - np.maximum(o, c), np.minimum(o, c) - l
        su, sd = roll(up, N, np.nansum), roll(dn, N, np.nansum)
        wick = np.where(su + sd > 0, su / (su + sd), np.nan)
        green = (c > o).astype(float)
        full = ((c > o) & (np.abs(c - o) >= 0.7 * height) & (height > 0)).astype(float)
        step = (mid > _lag(mid, 1)).astype(float)
        ok = live & (roll(live.astype(float), N, np.nansum) >= N)
        out = {"rope": rope, "wick": wick, "greens": roll(green, N, np.nansum), "full": roll(full, N, np.nansum),
               "higher": roll(step, N - 1, np.nansum)}
        return {k: np.where(ok, v, np.nan) for k, v in out.items()}


def candles(day: dict, k: int) -> dict:
    """Clock-aligned k-minute candles from the 1-minute bars (09:30, 09:30+k, ...), (n, 390 // k)."""
    n, T = day["c"].shape
    w = T // k
    with np.errstate(all="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        def cut(x):
            return x[:, :w * k].reshape(n, w, k)
        o, h, l, c, v = (cut(np.asarray(day[q], float)) for q in ("o", "h", "l", "c", "v"))
        have = ~np.isnan(c)
        first = np.argmax(have, axis=2)
        last = k - 1 - np.argmax(have[:, :, ::-1], axis=2)
        ii, jj = np.meshgrid(np.arange(n), np.arange(w), indexing="ij")
        any_ = have.any(axis=2)
        return {"o": np.where(any_, o[ii, jj, first], np.nan), "h": np.nanmax(h, axis=2), "l": np.nanmin(l, axis=2),
                "c": np.where(any_, c[ii, jj, last], np.nan), "v": np.nansum(v, axis=2)}


def measures_tf(day: dict, k: int = 1) -> dict:
    """The same measures on k-minute candles, mapped back to minutes: minute m sees only candles that have fully closed
    by the end of minute m (the chart's last finished candle), never the one still forming."""
    if k == 1:
        return measures(day)
    mk = measures(candles(day, k))
    T = day["c"].shape[1]
    j = (np.arange(T) + 1) // k - 1                              # index of the last finished k-minute candle at minute m
    out = {}
    for name, a in mk.items():
        full = np.full((a.shape[0], T), np.nan)
        ok = (j >= 0) & (j < a.shape[1])
        full[:, ok] = a[:, j[ok]]
        out[name] = full
    return out


def buckets(m: dict) -> dict[str, np.ndarray]:
    """Named states -> bool masks. NaN never matches."""
    with np.errstate(all="ignore"):
        r, w, g, f, hi = m["rope"], m["wick"], m["greens"], m["full"], m["higher"]
        b = {"rope: BULLS pulling (>= +1)": r >= 1.0, "rope: SIDEWAYS (-1..+1)": (r > -1.0) & (r < 1.0), "rope: BEARS pulling (<= -1)": r <= -1.0,
             "rope: strong bulls (>= +2)": r >= 2.0,
             "wicks: bull wicks (upper <= 35%)": w <= 0.35, "wicks: balanced": (w > 0.35) & (w < 0.65), "wicks: bear wicks (upper >= 65%)": w >= 0.65,
             "greens: 0-1 of 5": g <= 1, "greens: 2-3 of 5": (g >= 2) & (g <= 3), "greens: 4 of 5": g == 4, "greens: 5 of 5": g == 5,
             "full greens: 0": f == 0, "full greens: 1-2": (f >= 1) & (f <= 2), "full greens: 3+": f >= 3,
             "higher steps: 4 of 4": hi == 4, "higher steps: 0-1 of 4": hi <= 1}
        b["OPERATOR RULE: enter (bulls pulling, not bear wicks)"] = (r >= 1.0) & ~(w >= 0.65)
        b["OPERATOR RULE: prevent (sideways, bears, or bear wicks)"] = (r < 1.0) | (w >= 0.65)
        return b


def test_a(files, holds, cfg, k=1):
    import current_square as CS
    names = list(buckets({k: np.zeros((1, 1)) for k in ("rope", "wick", "greens", "full", "higher")}))
    print(f"A. THE DESK'S ARM on the warehouse, split by tug-of-war state on {k}-MINUTE candles ({files[1].stem}..{files[-1].stem}); numbers are bp")
    for hold in holds:
        base = H.run(CS.signal, hold, files, cfg, check=False)
        print(f"\n  hold {hold} m   ALL arm entries: {base['trades']} trades, per trade gross {base['gross_trade']:+.2f}, vs same-minute peers {base['lift']['mean']:+.2f}")
        print(f"  {'state at the arm':<52}{'trades':>8}{'share':>7}{'gross/trade':>12}{'vs peers':>9}{'day-t':>7}{'halves':>15}")
        for name in names:
            def fn(day, prev, name=name):
                with np.errstate(all="ignore"), warnings.catch_warnings():
                    warnings.simplefilter("ignore", RuntimeWarning)
                    return CS.signal(day, prev) & np.nan_to_num(buckets(measures_tf(day, k))[name]).astype(bool)
            r = H.run(fn, hold, files, cfg, check=(hold == holds[0] and name == names[0]))
            if r["verdict"] == "NO TRADES":
                continue
            print(f"  {name:<52}{r['trades']:>8}{100 * r['trades'] / max(1, base['trades']):>6.0f}%{r['gross_trade']:>+12.2f}{r['lift']['mean']:>+9.2f}"
                  f"{r['lift']['t']:>+7.2f}{r['lift_halves'][0]:>+8.2f}/{r['lift_halves'][1]:<+6.2f}", flush=True)


def test_b(wh: Path, outcomes: Path, k: int = 1):
    rows = []
    cache: dict[str, tuple | None] = {}
    for ln in open(outcomes):
        try:
            o = json.loads(ln)
        except ValueError:
            continue
        e, x, t = o.get("entry_price"), o.get("exit_price"), o.get("entry_time")
        if not (e and x and t) or o.get("strategy") != "day_scalp_v0" or e < 20:
            continue
        d = datetime.fromtimestamp(t, ET)
        day = d.strftime("%Y-%m-%d")
        if day < "2026-10-08":
            continue
        if day not in cache:
            f = wh / f"{day}.npz"
            if f.exists():
                raw = H.load_day(f)
                with np.errstate(all="ignore"):
                    cache[day] = ({s: i for i, s in enumerate(raw["syms"])}, buckets(measures_tf(raw, k)), None)
            else:
                cache[day] = None
        if cache[day] is None:
            continue
        idx, bk, _rope = cache[day]
        m = d.hour * 60 + d.minute - H.M0 - 1                              # the last bar that had closed before the entry
        i = idx.get(o["symbol"])
        if i is None or m < N * k or m >= H.NBAR:
            continue
        rows.append({"day": day, "bp": (x / e - 1) * 1e4, **{k: bool(v[i, m]) for k, v in bk.items()}})
    if not rows:
        print("\nB. no desk entries could be matched to warehouse bars")
        return
    allbp = np.array([r["bp"] for r in rows])
    days = sorted({r["day"] for r in rows})
    print(f"\nB. THE DESK'S OWN ENTRIES since 10/08 matched to bars: {len(rows)} trades on {len(days)} days, mean {allbp.mean():+.2f} bp per trade (exit vs entry, spread included)")
    print(f"  {f'state in the last 5 finished {k}-minute candles':<52}{'trades':>8}{'share':>7}{'bp/trade':>10}{'win':>6}{'rest bp':>9}{'diff':>8}{'day-t':>7}{'halves (diff)':>16}")
    for name in [k for k in rows[0] if k not in ("day", "bp")]:
        a = [r for r in rows if r[name]]
        b = [r for r in rows if not r[name]]
        if len(a) < 15 or len(b) < 15:
            print(f"  {name:<52}{len(a):>8}   too few")
            continue
        by: dict[str, list] = defaultdict(lambda: [[], []])
        for r in rows:
            by[r["day"]][0 if r[name] else 1].append(r["bp"])
        diffs = [(d, np.mean(v[0]) - np.mean(v[1])) for d, v in sorted(by.items()) if v[0] and v[1]]
        dv = np.array([x for _d, x in diffs])
        t = dv.mean() / (dv.std(ddof=1) / math.sqrt(len(dv))) if len(dv) > 3 and dv.std() > 0 else float("nan")
        mid = days[len(days) // 2]

        def half(lo):
            xa = [r["bp"] for r in a if (r["day"] < mid) == lo]
            xb = [r["bp"] for r in b if (r["day"] < mid) == lo]
            return (np.mean(xa) - np.mean(xb)) if xa and xb else float("nan")
        ma, mb = np.mean([r["bp"] for r in a]), np.mean([r["bp"] for r in b])
        print(f"  {name:<52}{len(a):>8}{100 * len(a) / len(rows):>6.0f}%{ma:>+10.2f}{100 * np.mean([r['bp'] > 0 for r in a]):>5.0f}%{mb:>+9.2f}{ma - mb:>+8.2f}{t:>+7.2f}"
              f"{half(True):>+9.2f}/{half(False):<+6.2f}")


FWD_SINCE, FWD_MIN_FLAGGED, FWD_MIN_ALL = "2026-11-12", 40, 400
FWD = (("PRIMARY   10-minute candles: 4 of 4 stepping higher", 10, "higher steps: 4 of 4"),
       ("SECONDARY  5-minute candles: 4 or 5 of 5 green", 5, "greens: 4+ of 5"))


def desk_rows(wh: Path, outcomes: Path, k: int, since: str) -> list[dict]:
    """The desk's entries on or after `since`, each with its tug-of-war states on k-minute candles finished before the entry."""
    rows, cache = [], {}
    for ln in open(outcomes):
        try:
            o = json.loads(ln)
        except ValueError:
            continue
        e, x, t = o.get("entry_price"), o.get("exit_price"), o.get("entry_time")
        if not (e and x and t) or o.get("strategy") != "day_scalp_v0" or e < 20:
            continue
        d = datetime.fromtimestamp(t, ET)
        day = d.strftime("%Y-%m-%d")
        if day < since:
            continue
        if day not in cache:
            f = wh / f"{day}.npz"
            cache[day] = None
            if f.exists():
                raw = H.load_day(f)
                with np.errstate(all="ignore"):
                    bk = buckets(measures_tf(raw, k))
                    bk["greens: 4+ of 5"] = bk["greens: 4 of 5"] | bk["greens: 5 of 5"]
                cache[day] = ({s: i for i, s in enumerate(raw["syms"])}, bk)
        if cache[day] is None:
            continue
        idx, bk = cache[day]
        m = d.hour * 60 + d.minute - H.M0 - 1
        i = idx.get(o["symbol"])
        if i is None or m < N * k or m >= H.NBAR:
            continue
        rows.append({"day": day, "bp": (x / e - 1) * 1e4, **{name: bool(v[i, m]) for name, v in bk.items()}})
    return rows


def forward(wh: Path, outcomes: Path) -> str:
    """docs/studies/tug_of_war_forward_prereg.json: counts only until due, then ONE read."""
    out = [f"Tug-of-war forward test on desk entries from {FWD_SINCE} (written {datetime.now(ET):%Y-%m-%d %H:%M} ET)"]
    for label, k, name in FWD:
        rows = desk_rows(wh, outcomes, k, FWD_SINCE)
        flag = [r for r in rows if r[name]]
        rest = [r for r in rows if not r[name]]
        if len(flag) < FWD_MIN_FLAGGED or len(rows) < FWD_MIN_ALL:
            out.append(f"  {label}: NOT DUE ({len(flag)} of {FWD_MIN_FLAGGED} flagged entries, {len(rows)} of {FWD_MIN_ALL} entries in all)")
            continue
        days = sorted({r["day"] for r in rows})
        mid = days[len(days) // 2]
        by = defaultdict(lambda: [[], []])
        for r in rows:
            by[r["day"]][0 if r[name] else 1].append(r["bp"])
        dv = np.array([np.mean(v[0]) - np.mean(v[1]) for _d, v in sorted(by.items()) if v[0] and v[1]])
        t = dv.mean() / (dv.std(ddof=1) / math.sqrt(len(dv))) if len(dv) > 3 and dv.std() > 0 else float("nan")

        def half(first):
            a = [r["bp"] for r in flag if (r["day"] < mid) == first]
            b = [r["bp"] for r in rest if (r["day"] < mid) == first]
            return (np.mean(a) - np.mean(b)) if a and b else float("nan")
        diff = float(np.mean([r["bp"] for r in flag]) - np.mean([r["bp"] for r in rest]))
        h1, h2 = half(True), half(False)
        ok = diff <= -3.0 and h1 < 0 and h2 < 0 and t <= -2.0
        out.append(f"  {label}: {'CONFIRMED (flagged entries do worse)' if ok else 'NOT CONFIRMED'}  flagged {len(flag)} trades {np.mean([r['bp'] for r in flag]):+.2f} bp, "
                   f"rest {len(rest)} trades {np.mean([r['bp'] for r in rest]):+.2f} bp, diff {diff:+.2f} (need <= -3), day-t {t:+.2f} (need <= -2), halves {h1:+.2f} / {h2:+.2f}")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--warehouse", default=str(H.WH))
    ap.add_argument("--holds", default="3,15")
    ap.add_argument("--holdout", type=int, default=20)
    ap.add_argument("--skip-a", action="store_true")
    ap.add_argument("--tf", type=int, default=1, help="candle size in minutes (1, 5, 10)")
    ap.add_argument("--forward", action="store_true", help="the pre-registered forward test on fresh desk entries (counts until due)")
    a = ap.parse_args(argv)
    if a.forward:
        text = forward(Path(a.warehouse), H.ROOT / "ai_reports" / "outcomes.jsonl")
        (H.ROOT / "ai_reports" / "tug_of_war_forward.txt").write_text(text + "\n")
        print(text)
        return 0
    files = sorted(Path(a.warehouse).glob("*.npz"))
    dev = files[:-a.holdout] if a.holdout > 0 else files
    cfg = {**H.DEFAULTS, "holdout": a.holdout, "family": 18, "min_trades": 0, "min_days": 0}
    if not a.skip_a:
        test_a(dev, [int(x) for x in a.holds.split(",")], cfg, a.tf)
    test_b(Path(a.warehouse), H.ROOT / "ai_reports" / "outcomes.jsonl", a.tf)
    return 0


if __name__ == "__main__":
    sys.exit(main())
