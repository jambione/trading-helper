#!/usr/bin/env python3
"""floor_bands - what would a $2 or $5 price floor add? (operator 11/17: "run a simulation of lowering the floor").

Read-only. The replay cannot answer it: the movers/tight screeners drop sub-$20 names before the desk records its sources
(11/05 floor replays: 0 trades under $20). Here the desk's entry signal is found directly in minute bars:

  universe   $2-20: ai_reports/warehouse/sip_small, each day's own members only (the `uni` flag; older files hold only
             that day's members). $20+: the tight screener's universe (top 400 liquid, ETFs out) from warehouse/sip.
  admission  day change >= 1.5% at the signal minute (today's rule), price band at the signal minute
  spread     each name's median quoted spread from ai_reports/combined_spreads.json (bp of mid). The desk refuses a name
             whose spread is above 15 bp (0.15%) or unknown, so does this study ("gate"); "nogate" shows what it removes.
  signal     the desk's live pre-square arm, fast %R 21, refractory 30 (evening_family.presquare_bars)
  trade      buy open t+1, sell close t+10; net = gross - the name's full spread (buy the ask, sell the bid)

    .venv/bin/python tools/studies/floor_bands.py           # -> ai_reports/floor_bands/result.json
"""
from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))
from relative_strength_scan import load_day  # noqa: E402
from board_structure_family import universe  # noqa: E402
from evening_family import minute_grid, presquare_bars, first_true, WH_SIP, ALL_ETF  # noqa: E402

WH_SMALL = ROOT / "ai_reports" / "warehouse" / "sip_small"
SPREADS = ROOT / "ai_reports" / "combined_spreads.json"
OUT = ROOT / "ai_reports" / "floor_bands"
BASE_PCT, H, GATE_BP, MINUTES = 1.5, 10, 15.0, 346
BANDS = (("2-5", 2, 5), ("5-10", 5, 10), ("10-20", 10, 20), ("20+", 20, 400))


def band_of(px):
    for name, lo, hi in BANDS:
        if lo <= px < hi:
            return name
    return None


def load_spreads(path=SPREADS):
    """{SYM: median spread bp} from {"SYM|ts": bp}."""
    acc = defaultdict(list)
    for k, v in json.load(open(path)).items():
        try:
            acc[k.split("|")[0]].append(float(v))
        except (TypeError, ValueError):
            continue
    return {s: statistics.median(v) for s, v in acc.items() if v}


def last_close(row):
    v = row[~np.isnan(row)]
    return float(v[-1]) if len(v) else float("nan")


def members(path):
    z = np.load(path, allow_pickle=True)
    syms = [str(s) for s in z["syms"]]
    if "uni" in z.files:
        return {s for s, u in zip(syms, z["uni"]) if bool(u)}
    return set(syms)


def signals(day, prev, names, lo, hi):
    """[(sym, t, px, gross_bp)] for pre-square signals of `names` admitted by today's rule inside [lo, hi)."""
    out = []
    for s in names:
        i, j = day["idx"].get(s), prev["idx"].get(s)
        if i is None or j is None:
            continue
        pc = last_close(prev["c"][j])
        if not pc > 0:
            continue
        cc, hh, ll, off = minute_grid(prev["c"][j], prev["h"][j], prev["l"][j], day["c"][i], day["h"][i], day["l"][i])
        for t in first_true(presquare_bars(cc, hh, ll, off, 21)):
            px = day["c"][i][t]
            if np.isnan(px) or not (lo <= px < hi) or (px / pc - 1) * 100 < BASE_PCT:
                continue
            if t + H >= 390 or np.isnan(day["o"][i][t + 1]) or np.isnan(day["c"][i][t + H]) or day["o"][i][t + 1] <= 0:
                continue
            out.append((s, int(t), float(px), float((day["c"][i][t + H] / day["o"][i][t + 1] - 1) * 1e4)))
    return out


def summarize(trades, n_days):
    """trades: [{band, gross, spread, gated}] -> per band, gated and not."""
    res = {}
    for name, _, _ in BANDS:
        for label, keep in (("gate", lambda x: x["gated"]), ("nogate", lambda x: x["spread"] is not None)):
            xs = [x for x in trades if x["band"] == name and keep(x)]
            if not xs:
                res[f"{name}_{label}"] = {"n": 0}
                continue
            net = np.array([x["gross"] - x["spread"] for x in xs])
            res[f"{name}_{label}"] = {
                "n": len(xs), "per_10m": round(len(xs) / max(1, n_days) / MINUTES * 10, 3),
                "gross_bp": round(float(np.mean([x["gross"] for x in xs])), 2),
                "spread_bp": round(float(np.median([x["spread"] for x in xs])), 2),
                "net_bp": round(float(net.mean()), 2), "win_pct": round(float((net > 0).mean() * 100), 1)}
        res[f"{name}_unknown_spread"] = sum(1 for x in trades if x["band"] == name and x["spread"] is None)
    return res


def main():
    sp = load_spreads()
    days = sorted(set(f.stem for f in WH_SMALL.glob("20??-??-??.npz")) & set(f.stem for f in WH_SIP.glob("20??-??-??.npz")))
    trades, n_days, prev_s, prev_b = [], 0, None, None
    for d in days:
        ds, db = load_day(WH_SMALL / f"{d}.npz"), load_day(WH_SIP / f"{d}.npz")
        if prev_s is not None and prev_b is not None:
            n_days += 1
            small = sorted(members(WH_SMALL / f"{d}.npz"))
            big = universe(db, prev_b, set(ALL_ETF))
            got = [(s, t, px, g, "s") for s, t, px, g in signals(ds, prev_s, small, 2.0, 20.0)] + \
                  [(s, t, px, g, "b") for s, t, px, g in signals(db, prev_b, big, 20.0, 400.0)]
            for s, t, px, g, _src in got:
                spr = sp.get(s)
                trades.append({"day": d, "sym": s, "t": t, "band": band_of(px), "gross": round(g, 2),
                               "spread": None if spr is None else round(spr, 2),
                               "gated": spr is not None and spr <= GATE_BP})
            print(d, len(got), flush=True)
        prev_s, prev_b = ds, db
    res = {"n_days": n_days, "gate_bp": GATE_BP, "spread_names": len(sp), "summary": summarize(trades, n_days)}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "result.json").write_text(json.dumps(res, indent=1))
    print(json.dumps(res["summary"], indent=1))


if __name__ == "__main__":
    main()
