#!/usr/bin/env python3
"""etf_flow - does 60-second signed SIP trade flow predict SPY / QQQ over the next 3 minutes, where the spread is ~0.2 bp?
Pass bar: docs/studies/etf_flow_prereg.json. Read-only. Run on the mini (data account).

    .venv/bin/python tools/studies/etf_flow.py fetch    # trades in [T-60 s, T) per moment -> ai_reports/etf_flow/ticks/
    .venv/bin/python tools/studies/etf_flow.py score    # -> ai_reports/etf_flow/result.json
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))

ET = ZoneInfo("America/New_York")
OUT = ROOT / "ai_reports" / "etf_flow"
TICKS = OUT / "ticks"
HEDGE = ROOT / "ai_reports" / "warehouse" / "sip_hedge"
SYMS = ("SPY", "QQQ")
FIRST_DAY, LAST_DAY = "2026-04-28", "2026-11-17"
MINUTES = tuple(range(30, 351, 10))
KEEP_CODES = frozenset({" ", "@", "I", "F"})
WINDOW_S, GAP_S = 60, 0.4
HORIZONS = {"60s": 0, "3m": 2, "10m": 9, "30m": 29}


def tick_sign(prices):
    """Tick rule: +1 up-tick, -1 down-tick, zero-tick carries the last sign, first trade 0. Pure."""
    out, last, prev = [], 0, None
    for p in prices:
        if prev is not None:
            if p > prev:
                last = 1
            elif p < prev:
                last = -1
        out.append(last if prev is not None else 0)
        prev = p
    return out


def imbalance(trades):
    """[(ts, price, size)] in time order -> sum(sign*size)/sum(size), or None. Pure."""
    if len(trades) < 2:
        return None
    signs = tick_sign([p for _, p, _ in trades])
    tot = sum(s for _, _, s in trades[1:])
    if tot <= 0:
        return None
    return sum(g * s for g, (_, _, s) in zip(signs[1:], trades[1:])) / tot


def days():
    return [p.stem for p in sorted(HEDGE.glob("20??-??-??.npz")) if FIRST_DAY <= p.stem <= LAST_DAY]


def decision_ts(day, m):
    h, mi = divmod(570 + m, 60)
    return datetime.fromisoformat(day).replace(hour=h, minute=mi, tzinfo=ET).timestamp()


def fetch():
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockTradesRequest
    from alpaca.data.enums import DataFeed
    k = json.load(open(os.environ.get("TH_DATA_SECRETS") or ROOT / "secrets_data.json"))
    cl = StockHistoricalDataClient(k["alpaca_data_api_key"], k["alpaca_data_secret_key"])
    TICKS.mkdir(parents=True, exist_ok=True)
    utc = lambda x: datetime.fromtimestamp(x, timezone.utc)  # noqa: E731
    only = sys.argv[2:] or list(SYMS)          # e.g. 'fetch SPY' and 'fetch QQQ' as two workers
    for d in days():
        for sym in [x for x in SYMS if x in only]:
            p = TICKS / f"{d}_{sym}.json"
            if p.exists():
                continue
            rows = {}
            for m in MINUTES:
                T = decision_ts(d, m)
                for attempt in range(5):
                    try:
                        tr = cl.get_stock_trades(StockTradesRequest(symbol_or_symbols=sym, start=utc(T - WINDOW_S), end=utc(T),
                                                                    feed=DataFeed.SIP, limit=50_000)).data.get(sym) or []
                        break
                    except Exception as e:  # noqa: BLE001
                        print(f"  {d} {sym} {m} try {attempt}: {str(e)[:80]}", flush=True)
                        time.sleep(10 * (attempt + 1))
                else:
                    tr = None
                if tr is None:
                    continue
                keep = [(t.timestamp.timestamp(), float(t.price), float(t.size)) for t in tr
                        if all(c in KEEP_CODES for c in (t.conditions or [" "]))]
                keep.sort(key=lambda x: x[0])
                rows[str(m)] = {"n": len(keep), "imb": imbalance(keep), "raw_n": len(tr)}
                time.sleep(GAP_S)
            p.write_text(json.dumps(rows))
            print(d, sym, len(rows), flush=True)


def outcomes(day_npz, sym):
    from relative_strength_scan import load_day
    d = load_day(day_npz)
    i = d["idx"].get(sym)
    if i is None:
        return None
    return d["o"][i], d["c"][i]


def ret_bp(o, c, m, k):
    e, x = o[m], c[m + k] if m + k < len(c) else np.nan
    if not (np.isfinite(e) and np.isfinite(x) and e > 0):
        return None
    return (x / e - 1) * 1e4


def tercile_cuts(vals):
    v = np.array([x for x in vals if x is not None])
    return (float(np.quantile(v, 1 / 3)), float(np.quantile(v, 2 / 3))) if len(v) >= 30 else None


def day_t(rows, key="net"):
    per = defaultdict(list)
    for r in rows:
        per[r["day"]].append(r[key])
    m = [np.mean(v) for v in per.values()]
    if len(m) < 3 or np.std(m, ddof=1) == 0:
        return None
    return round(float(np.mean(m) / (np.std(m, ddof=1) / math.sqrt(len(m)))), 2)


def summ(rows, key="net"):
    if not rows:
        return {"n": 0}
    x = np.array([r[key] for r in rows])
    return {"n": len(rows), "mean": round(float(x.mean()), 3), "win": round(100 * float((x > 0).mean()), 1),
            "t_day": day_t(rows, key)}


def verdict(test_g, test_rest, train_g):
    if test_g.get("n", 0) < 1000:
        return "UNDERPOWERED", [f"n {test_g.get('n', 0)} (need 1000)"]
    f = []
    if test_g["mean"] < 1.0:
        f.append(f"edge {test_g['mean']} bp (need +1.0)")
    if test_rest.get("n") and test_g["mean"] - test_rest["mean"] < 1.0:
        f.append(f"lift {round(test_g['mean'] - test_rest['mean'], 3)} (need +1.0)")
    if (test_g.get("t_day") or 0) < 2.0:
        f.append(f"t {test_g.get('t_day')}")
    if not (train_g.get("n") and train_g["mean"] > 0):
        f.append(f"train {train_g.get('mean')}")
    return ("PASS" if not f else "FAIL"), f


def score():
    ds = days()
    half = set(ds[: len(ds) // 2])
    rows = []
    for d in ds:
        for sym in SYMS:
            p = TICKS / f"{d}_{sym}.json"
            if not p.exists():
                continue
            feat = json.loads(p.read_text())
            oc = outcomes(HEDGE / f"{d}.npz", sym)
            if oc is None:
                continue
            o, c = oc
            for m in MINUTES:
                f = feat.get(str(m))
                if not f or f.get("imb") is None:
                    continue
                rec = {"day": d, "sym": sym, "m": m, "imb": f["imb"], "train": d in half}
                ok = True
                for h, k in HORIZONS.items():
                    r = ret_bp(o, c, m, k)
                    if r is None:
                        ok = False
                        break
                    rec[h] = r
                if not ok:
                    continue
                spread1 = 0.01 / o[m] * 1e4
                rec["net"] = rec["3m"] - spread1
                rec["net2"] = rec["3m"] - 2 * spread1
                rows.append(rec)
    res = {"days": len(ds), "rows": len(rows), "by": {}}
    groups = {}
    for sym in SYMS:
        cuts = tercile_cuts([r["imb"] for r in rows if r["sym"] == sym and r["train"]])
        res["by"][f"{sym}_cuts"] = cuts
        for r in rows:
            if r["sym"] == sym and cuts:
                r["g"] = "top" if r["imb"] >= cuts[1] else ("bottom" if r["imb"] <= cuts[0] else "mid")
    rows = [r for r in rows if "g" in r]
    for part in ("train", "test"):
        sel = [r for r in rows if r["train"] == (part == "train")]
        groups[part] = {"top": [r for r in sel if r["g"] == "top"], "rest": [r for r in sel if r["g"] != "top"],
                        "bottom": [r for r in sel if r["g"] == "bottom"], "all": sel}
        for gname, g in groups[part].items():
            res["by"][f"{part}|{gname}|net"] = summ(g)
        for h in HORIZONS:
            res["by"][f"{part}|top|gross_{h}"] = summ(groups[part]["top"], h)
            res["by"][f"{part}|rest|gross_{h}"] = summ(groups[part]["rest"], h)
        res["by"][f"{part}|top|net_2c"] = summ(groups[part]["top"], "net2")
        for sym in SYMS:
            res["by"][f"{part}|top|{sym}"] = summ([r for r in groups[part]["top"] if r["sym"] == sym])
            res["by"][f"{part}|rest|{sym}"] = summ([r for r in groups[part]["rest"] if r["sym"] == sym])
        for name, a, b in (("10:00-11:00", 30, 90), ("11:00-13:00", 90, 210), ("13:00-15:20", 210, 351)):
            res["by"][f"{part}|top|{name}"] = summ([r for r in groups[part]["top"] if a <= r["m"] < b])
    res["verdict"] = verdict(res["by"]["test|top|net"], res["by"]["test|rest|net"], res["by"]["train|top|net"])
    n_days = max(1, len({r["day"] for r in groups["test"]["all"]}))
    res["info_gated_moments_per_day_test"] = round(len(groups["test"]["top"]) / n_days, 1)
    res["generated"] = datetime.now(ET).isoformat(timespec="minutes")
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "result.json").write_text(json.dumps(res, indent=1))
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    {"fetch": fetch, "score": score}[sys.argv[1]]()
