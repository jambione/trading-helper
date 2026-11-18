#!/usr/bin/env python3
"""flow_exit - does SIP order flow in a trade's first 1-2 minutes separate the trades that fall straight to the stop from the dips
that recover? Pass bar: docs/studies/flow_exit_prereg.json (written before any tick was fetched). Read-only; data-only keys.

    .venv/bin/python tools/studies/flow_exit.py      # -> ai_reports/flow_exit/result.json (ticks cached in ai_reports/flow_exit/ticks)
"""
from __future__ import annotations

import json
import math
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "studies"))
from loser_breakdown import pair_fifo, auc  # noqa: E402

ET = ZoneInfo("America/New_York")
FILLS = ROOT / "ai_reports" / "broker_truth" / "fills.jsonl"
OUT = ROOT / "ai_reports" / "flow_exit"
TICKS = OUT / "ticks"
LO, HI, TRAIN_END, TEST_START = "2026-09-16", "2026-11-18", "2026-10-23", "2026-10-26"
SKIP = {"2026-11-12", "2026-11-13", "2026-11-16"}
WS, KS, EXIT_COST, PRE = (60, 120), (-0.2, -0.4, -0.6), 3.0, 5.0


def signed_flow(ticks, t_fill, w):
    """ticks [(ts, px, size)] sorted -> ((buy-sell)/(buy+sell) over (t_fill, t_fill+w], last px at t_fill+w). Tick rule; zero tick
    keeps the previous sign; trades before t_fill only seed the rule. (None, None) when nothing traded in the window."""
    prev_px, sign, buy, sell, last = None, 0, 0.0, 0.0, None
    for ts, px, sz in ticks:
        if ts > t_fill + w:
            break
        if prev_px is not None:
            if px > prev_px:
                sign = 1
            elif px < prev_px:
                sign = -1
        prev_px = px
        if ts >= t_fill:
            last = px
            if sign > 0:
                buy += sz
            elif sign < 0:
                sell += sz
    if last is None or buy + sell <= 0:
        return None, last
    return (buy - sell) / (buy + sell), last


def rule_delta(tr, flow, last_px, w, k, use_flow=True):
    """bp change vs the actual exit if the rule sells at fill + w; 0 when it does not act."""
    if last_px is None or tr["t_out"] <= tr["t_in"] + w:
        return 0.0
    if not last_px < tr["px_in"]:
        return 0.0
    if use_flow and (flow is None or flow > k):
        return 0.0
    cut = (last_px / tr["px_in"] - 1) * 1e4 - EXIT_COST
    return cut - tr["ret_bp"]


def load_trades():
    """Alpaca FILL activities (broker_truth/fills.jsonl, from 11/04) plus, for earlier days, the order-level fills the desk
    archived (tools/broker_truth.archive_fills; cross-checked 0.00 bp on the overlap days)."""
    sys.path.insert(0, str(ROOT / "tools"))
    from broker_truth import archive_fills
    fills = defaultdict(list)
    for line in open(FILLS):
        f = json.loads(line)
        d = datetime.fromtimestamp(f["t"], ET).date().isoformat()
        if LO <= d <= HI and d not in SKIP:
            fills[d].append(f)
    have = set(fills)
    for f in archive_fills():
        d = datetime.fromtimestamp(f["t"], ET).date().isoformat()
        if LO <= d <= HI and d not in SKIP and d not in have:
            fills[d].append(f)
    out = []
    for d in sorted(fills):
        for t in pair_fifo(fills[d]):
            m = datetime.fromtimestamp(t["t_in"], ET)
            if 9 * 60 + 31 <= m.hour * 60 + m.minute <= 15 * 60 + 45:
                out.append({**t, "day": d})
    return out


def fetch_ticks(trades, pause=0.3):
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockTradesRequest
    from alpaca.data.enums import DataFeed
    k = json.load(open(ROOT / "secrets_data.json"))
    cl = StockHistoricalDataClient(k["alpaca_data_api_key"], k["alpaca_data_secret_key"])
    TICKS.mkdir(parents=True, exist_ok=True)
    by_day = defaultdict(list)
    for t in trades:
        by_day[t["day"]].append(t)
    cache = {}
    for d, ts in sorted(by_day.items()):
        p = TICKS / f"{d}.json"
        day_c = json.load(open(p)) if p.exists() else {}
        todo = [t for t in ts if f"{t['symbol']}|{round(t['t_in'], 3)}" not in day_c]
        for t in todo:
            key = f"{t['symbol']}|{round(t['t_in'], 3)}"
            a = datetime.fromtimestamp(t["t_in"] - PRE, ET)
            b = datetime.fromtimestamp(t["t_in"] + max(WS) + 1, ET)
            try:
                r = cl.get_stock_trades(StockTradesRequest(symbol_or_symbols=t["symbol"], start=a, end=b, feed=DataFeed.SIP, limit=10000))
                rows = (r.data or {}).get(t["symbol"], [])
                day_c[key] = [[x.timestamp.timestamp(), float(x.price), float(x.size)] for x in rows]
            except Exception as e:  # noqa: BLE001
                print("ERR", d, t["symbol"], str(e)[:80], flush=True)
                time.sleep(5)
                continue
            time.sleep(pause)
        p.write_text(json.dumps(day_c))
        cache.update(day_c)
        print(d, len(ts), "trades,", len(todo), "fetched", flush=True)
    return cache


def score(trades, cache, w, k, use_flow=True):
    per_day = defaultdict(list)
    cut = []
    for t in trades:
        ticks = cache.get(f"{t['symbol']}|{round(t['t_in'], 3)}") or []
        fl, last = signed_flow(ticks, t["t_in"], w)
        dlt = rule_delta(t, fl, last, w, k, use_flow)
        per_day[t["day"]].append(dlt)
        if dlt != 0.0:
            cut.append((t, dlt))
    means = [np.mean(v) for v in per_day.values()]
    n = sum(len(v) for v in per_day.values())
    book = float(sum(sum(v) for v in per_day.values()) / max(1, n))
    sd = float(np.std(means, ddof=1)) if len(means) > 2 else 0.0
    return {"W": w, "K": k if use_flow else None, "trades": n, "cut": len(cut),
            "book_delta_bp": round(book, 2),
            "t_day": round(float(np.mean(means) / (sd / math.sqrt(len(means)))), 2) if sd > 0 else None,
            "cut_stopouts": sum(1 for t, _ in cut if t["ret_bp"] <= -40), "cut_winners": sum(1 for t, _ in cut if t["ret_bp"] > 0),
            "mean_delta_per_cut": round(float(np.mean([d for _, d in cut])), 1) if cut else None}


def main():
    trades = load_trades()
    cache = fetch_ticks(trades)
    train = [t for t in trades if t["day"] <= TRAIN_END]
    test = [t for t in trades if t["day"] >= TEST_START]
    grid = [score(train, cache, w, k) for w in WS for k in KS]
    best = max(grid, key=lambda g: g["book_delta_bp"])
    w, k = best["W"], best["K"]
    flow_t = score(test, cache, w, k)
    plain_t = score(test, cache, w, k, use_flow=False)
    fails = []
    if flow_t["cut"] < 40:
        verdict, fails = "UNDERPOWERED", [f"test cuts {flow_t['cut']} (need 40)"]
    else:
        if flow_t["book_delta_bp"] < 1.0:
            fails.append(f"book: {flow_t['book_delta_bp']} bp/trade (need +1.0)")
        if (flow_t["t_day"] or 0) < 2.0:
            fails.append(f"significance: day t {flow_t['t_day']} (need 2.0)")
        if flow_t["book_delta_bp"] < plain_t["book_delta_bp"] + 1.0:
            fails.append(f"beats_plain: flow {flow_t['book_delta_bp']} vs plain {plain_t['book_delta_bp']}")
        if best["book_delta_bp"] <= 0:
            fails.append(f"both_halves: train {best['book_delta_bp']}")
        verdict = "PASS" if not fails else "FAIL"
    # information: flow AUC for straight-to-stop among trades below entry at W (all trades)
    pos, neg = [], []
    for t in trades:
        fl, last = signed_flow(cache.get(f"{t['symbol']}|{round(t['t_in'], 3)}") or [], t["t_in"], w)
        if fl is None or last is None or not last < t["px_in"] or t["t_out"] <= t["t_in"] + w:
            continue
        (pos if t["ret_bp"] <= -40 else neg).append(-fl)      # lower flow -> more likely straight to stop
    res = {"verdict": verdict, "fails": fails, "chosen": {"W": w, "K": k}, "train_grid": grid,
           "test_flow": flow_t, "test_plain": plain_t,
           "info_auc_low_flow_predicts_stopout": round(auc(pos, neg), 3) if pos and neg else None,
           "info_below_entry_at_W": {"stopouts": len(pos), "others": len(neg)},
           "n_trades": len(trades), "n_train": len(train), "n_test": len(test),
           "generated": datetime.now(ET).isoformat(timespec="minutes")}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "result.json").write_text(json.dumps(res, indent=1))
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
