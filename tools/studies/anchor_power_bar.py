#!/usr/bin/env python3
"""Power bar off the anchor line (20-SMA) on SPY + QQQ. Spec: docs/studies/anchor_power_bar_prereg.json (amended;
skeptic READY r2). 2-minute bars; SMA20 of 2-minute closes continuous across kept sessions (restarted 2026-04-27).
LONG candidate (ending 10:00 .. 15:29, >= 15 earlier bars): same-clock power bar, |open - A_prev| <= 0.15% and
low <= A_prev x 1.001, close > the highest high of the previous 15 bars, SMA20 >= A_prev. Entry next 1-minute open;
stop = bar low - $0.01; target 2R; else 15:59. One open trade per symbol, at most 3 per day; controls only for trades
taken, drawn in (end, min(end + 60, 15:30)].

  (default) count step (outcome-blind); --score after a result-skeptic READY
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import sys
from collections import Counter, deque

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import etf_bars as B  # noqa: E402

OUT_DIR = os.path.join(B.ROOT, "ai_reports", "anchor_power_bar")
SYMS = ("SPY", "QQQ")
FIRST_K, LAST_END, CTRL_END, LEFT = 15, 929, 930, 15
NEAR_OPEN, NEAR_LOW, MAX_PER_DAY = 0.0015, 0.001, 3
COST, COSTS, FLOOR_DAYS = 0.0002, (0.0001, 0.0002, 0.0005, 0.0010), 300


def candidates(bars2, sma_at, size):
    """Yield (k, bar) for every long candidate in time order."""
    for k in range(FIRST_K, len(bars2)):
        b = bars2[k]
        if b is None or b[4] > LAST_END or b[4] < 599:
            continue
        a_prev, a_now = sma_at[k - 1], sma_at[k]
        if a_prev is None or a_now is None:
            continue
        if not (abs(b[0] - a_prev) <= NEAR_OPEN * a_prev and b[2] <= a_prev * (1 + NEAR_LOW)):
            continue
        left = [x[1] for x in bars2[k - LEFT:k] if x is not None]
        if not left or b[3] <= max(left):
            continue
        if a_now < a_prev:
            continue
        if B.power_bar(b, size.ref(k), 1):
            yield k, b


def run_symbol(sym):
    rows, st = [], Counter()
    size, closes, restarted = B.SizeRef(), deque(maxlen=20), False
    for day, r_all in B.load(sym).items():
        if not B.in_scope(day):
            st["out_of_scope_day"] += 1
            continue
        if day >= B.B_RESTART and not restarted:
            size.reset()
            closes.clear()
            restarted = True
        if not B.is_full_session(r_all, day):
            st["early_close_dropped"] += 1
            continue
        r = B.rth(r_all)
        bars2 = B.clock_bars(r, 2, 2)
        sma_at = []
        for b in bars2:
            if b is not None:
                closes.append(b[3])
            sma_at.append(statistics.mean(closes) if len(closes) == 20 else None)
        busy_until, taken = -1, 0
        for k, b in candidates(bars2, sma_at, size):
            if b[4] < busy_until:
                st["ignored_while_open"] += 1
                continue
            if taken >= MAX_PER_DAY:
                st["cap"] += 1
                break
            e = B.first_after(r, b[4])
            if e is None:
                st["skip_no_entry_bar"] += 1
                continue
            o = float(r[e, 1])
            stop = b[2] - B.TICK
            if o <= stop:
                st["skip_open_at_or_below_stop"] += 1
                continue
            tgt = o + 2 * (o - stop)
            res = B.simulate(r, e, 1, stop, tgt)
            sp, tp = (o - stop) / o, 2 * (o - stop) / o
            c1 = B.control(r, sym, day, b[4], min(b[4] + 60, CTRL_END), 1, sp, tp, f"apb-ctrl|{k}")
            if c1 is None:
                st["drop_no_control_window"] += 1
                continue
            c2 = B.control(r, sym, day, b[4], min(b[4] + 60, CTRL_END), 1, sp, tp, f"apb-ctrl2|{k}")
            rows.append({"day": day, "sym": sym, "half": B.half_of(day), "end": b[4], **res,
                         "ctrl": c1["gross"], "ctrl2": c2["gross"] if c2 else None})
            busy_until, taken = res["exit_min"], taken + 1
        size.add(bars2)
    return rows, st


def module_shas():
    d = os.path.dirname(os.path.abspath(__file__))
    return {m: hashlib.sha256(open(os.path.join(d, f"{m}.py"), "rb").read()).hexdigest()
            for m in ("anchor_power_bar", "etf_bars")}


def write(name, obj):
    os.makedirs(OUT_DIR, exist_ok=True)
    p = os.path.join(OUT_DIR, name)
    json.dump(obj, open(p + ".tmp", "w"), indent=1, default=str)
    os.replace(p + ".tmp", p)
    return p


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--score", action="store_true")
    ap.add_argument("--skeptic-ready", default="")
    ap.add_argument("--prereg-commit", default="")
    a = ap.parse_args()
    if json.load(open(os.path.join(B.CACHE, "fetch_log.json"))).get("failed"):
        raise SystemExit("fetch has failures")
    cache = os.path.join(OUT_DIR, "rows.json")
    if os.path.exists(cache) and json.load(open(cache)).get("module_sha256") == module_shas():
        c = json.load(open(cache))
        rows, st = c["rows"], Counter(c["stats"])
    else:
        rows, st = [], Counter()
        for sym in SYMS:
            rs, s = run_symbol(sym)
            rows += rs
            st.update({f"{sym}_{k}": v for k, v in s.items()})
        write("rows.json", {"rows": rows, "stats": dict(st), "module_sha256": module_shas()})
    if any(k.endswith("out_of_scope_day") and v for k, v in st.items()):
        raise SystemExit("held-out or post-window days present in the cache")
    if not a.score:
        cnt = {"stats": dict(st), "module_sha256": module_shas(), "prereg_commit": a.prereg_commit}
        for h in ("A", "B"):
            hr = [x for x in rows if x["half"] == h]
            m = B.mde(hr)
            days = len({x["day"] for x in hr})
            cnt[h] = {"long_trades": len(hr), "long_days": days, "mde": m,
                      "floors_met": days >= FLOOR_DAYS and m is not None and m <= 0.0005}
        print(json.dumps(cnt, indent=1, default=str))
        print(write("count.json", cnt))
        return
    if not a.skeptic_ready:
        raise SystemExit("--score needs --skeptic-ready")
    cnt = json.load(open(os.path.join(OUT_DIR, "count.json")))
    if cnt.get("module_sha256") != module_shas() or not cnt.get("prereg_commit"):
        raise SystemExit("count.json from different code or without --prereg-commit")
    if not all(cnt[h]["floors_met"] for h in ("A", "B")):
        raise SystemExit("UNDERPOWERED at the count step: no outcome is read")
    res = {"primary_long": B.primary_verdict(rows, COST, floor_days=FLOOR_DAYS), "info": {}}
    for h in ("A", "B"):
        hr = [x for x in rows if x["half"] == h]
        res["info"][f"win_rate_{h}"] = sum(1 for x in hr if x["gross"] > 0) / max(1, len(hr))
        res["info"][f"exit_mix_{h}"] = dict(Counter(x["why"] for x in hr))
        res["info"][f"net_by_cost_{h}"] = {f"{round(c * 1e4)}bp": B.day_stats(hr, lambda x, c=c: x["gross"] - c)
                                           ["mean"] for c in COSTS}
    res["verdict"] = res["primary_long"]["verdict"]
    res["declared"] = ["information cells not built and dropped before scoring: 2-bar combined power bars, W pattern, "
                       "SMA-close exit, warehouse stocks, R multiple",
                       "control seeds are 'apb-ctrl|<bar index>' (unique per trade on multi-trade days)"]
    res["skeptic_ready"], res["module_sha256"] = a.skeptic_ready, module_shas()
    write("result.json", res)
    print(json.dumps(res, indent=1, default=str))


if __name__ == "__main__":
    main()
