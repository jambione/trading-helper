#!/usr/bin/env python3
"""leash_replay.py — pin the desk's real fills and re-run the LIVE trail code
second by second on SIP trade prints, under exit-config variants.

The time-decay leash steps every ai_local_trail_decay_idle_sec (8 s live), so
1-minute bars cannot test it. This drives ai_positions.local_profit_stop, the
function live uses, once per simulated second:
  * entries are pinned: the logged fill price, time and plan stop (risk)
  * every second: the last SIP print feeds note_trail_print and the
    excursions, then local_profit_stop(pos, cfg, now=second) raises the stop
  * exit when any print in the next second trades at or under the stop (a
    broker stop order), filled at the SIP bid at that moment, so the exit
    pays the spread as live does. The 1R plan stop, a 2-hour cap and 15:50
    also exit at the bid.
  * dead_trade (ai_dead_trade_min, ai_dead_trade_mfe_r) as the replay models it
  * A' honors ai_exit_min_hold_sec (90 s) on the trail; A does not, because
    live sells through a broker stop order the min hold never gates
Not modelled: left_overbought (needs the engine's %R stream) and the T1
scale-out. Baseline A is scored against what live booked on the same fills,
as the fidelity check.

Result per variant: mean net per trade in bp of entry (from the real fill to
the bid at exit), win rate, median hold, the same by day, and a count of how
the trade ended.

USAGE (on the mini, after the close; SIP trades and quotes cached in ai_reports/)
  .venv/bin/python tools/studies/leash_replay.py --days 2026-09-23,2026-09-24,2026-09-25,2026-09-28,2026-09-29
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import pickle
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)
import bars  # noqa: E402
import ai_positions as cp  # noqa: E402
from config import load_config  # noqa: E402

CACHE = os.path.join(ROOT, "ai_reports", "leash_replay_cache.pkl")
MAX_HOLD = 120 * 60
EOD_MIN = 15 * 60 + 50

VARIANTS = [
    ("A  live config", {}),
    ("A' live + 90s min hold", {"_min_hold": 90.0}),
    ("1a leash idle 60s", {"ai_local_trail_decay_idle_sec": 60.0}),
    ("1b leash idle 120s", {"ai_local_trail_decay_idle_sec": 120.0}),
    ("2  red leash off", {"ai_local_trail_decay_red_step_r": 0.0}),
    ("3  time decay off", {"ai_local_trail_time_decay_enabled": False}),
    ("4a trail ~1%", {"ai_local_trail_give_r": 0.20, "ai_local_trail_give_max_pct": 1.0,
                      "ai_local_trail_peak_give_pct": 1.0}),
    ("4b trail ~2%", {"ai_local_trail_give_r": 0.40, "ai_local_trail_give_max_pct": 2.0,
                      "ai_local_trail_peak_give_pct": 2.0}),
    ("3+4b decay off, ~2%", {"ai_local_trail_time_decay_enabled": False,
                             "ai_local_trail_give_r": 0.40, "ai_local_trail_give_max_pct": 2.0,
                             "ai_local_trail_peak_give_pct": 2.0}),
]

# --set timing: how fast the ratchet tightens. Live (bot_config.json, 10/1):
# idle 8 s, green step 0.05R, red step 0 (off since 9/30), give 0.12R.
TIMING_VARIANTS = [
    ("A  live config", {}),
    ("A' live + 90s min hold", {"_min_hold": 90.0}),
    ("i4   idle 4s", {"ai_local_trail_decay_idle_sec": 4.0}),
    ("i15  idle 15s", {"ai_local_trail_decay_idle_sec": 15.0}),
    ("i30  idle 30s", {"ai_local_trail_decay_idle_sec": 30.0}),
    ("i60  idle 60s", {"ai_local_trail_decay_idle_sec": 60.0}),
    ("i120 idle 120s", {"ai_local_trail_decay_idle_sec": 120.0}),
    ("s.025 step 0.025R", {"ai_local_trail_decay_step_r": 0.025}),
    ("s.10 step 0.10R", {"ai_local_trail_decay_step_r": 0.10}),
    ("i30+s.025 slow both", {"ai_local_trail_decay_idle_sec": 30.0, "ai_local_trail_decay_step_r": 0.025}),
    ("off  time decay off", {"ai_local_trail_time_decay_enabled": False}),
    ("g.20 give 0.20R", {"ai_local_trail_give_r": 0.20}),
    ("g.30 give 0.30R", {"ai_local_trail_give_r": 0.30}),
    ("h30  min hold 30s", {"_min_hold": 30.0}),
    ("h180 min hold 180s", {"_min_hold": 180.0}),
    ("h300 min hold 300s", {"_min_hold": 300.0}),
]


def load_cache():
    try:
        return pickle.load(open(CACHE, "rb"))
    except (OSError, EOFError, pickle.UnpicklingError):
        return {"trades": {}, "bid": {}}


def save_cache(c):
    tmp = CACHE + ".tmp"
    pickle.dump(c, open(tmp, "wb"))
    os.replace(tmp, CACHE)


def fetch_seconds(cl, sym, t0, t1):
    """[(second, last_px, min_px)] from SIP trades, one row per second with prints."""
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockTradesRequest
    out = {}
    got = cl.get_stock_trades(StockTradesRequest(
        symbol_or_symbols=sym, start=datetime.fromtimestamp(t0, timezone.utc),
        end=datetime.fromtimestamp(t1, timezone.utc), feed=DataFeed.SIP)).data.get(sym) or []
    for tr in got:
        # drop odd lots and out-of-sequence prints the tape does not trade on
        conds = set(tr.conditions or [])
        if conds & {"I", "Z", "U", "T"}:
            continue
        s = int(tr.timestamp.timestamp())
        px = float(tr.price)
        if s in out:
            last, lo = out[s]
            out[s] = (px, min(lo, px))
        else:
            out[s] = (px, px)
    return sorted((s, v[0], v[1]) for s, v in out.items())


def bid_at(cl, cache, sym, ts):
    key = (sym, int(ts))
    if key in cache["bid"]:
        return cache["bid"][key]
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockQuotesRequest
    end = datetime.fromtimestamp(ts, timezone.utc)
    q = []
    try:
        q = cl.get_stock_quotes(StockQuotesRequest(
            symbol_or_symbols=sym, start=end - timedelta(seconds=15), end=end,
            feed=DataFeed.SIP)).data.get(sym) or []
    except Exception as e:  # noqa: BLE001
        print(f"  quote {sym}: {e}"[:120], flush=True)
    q = [x for x in q if x.bid_price and x.ask_price and x.ask_price >= x.bid_price]
    b = float(q[-1].bid_price) if q else None
    cache["bid"][key] = b
    time.sleep(0.15)
    return b


def new_pos(f, cfg):
    entry, risk = f["entry"], f["risk"]
    p = {"entry": entry, "entry_price": entry, "entry_time": f["t0"],
         "entry_stop_price": entry - risk, "stop_price": entry - risk,
         "risk_per_share": risk, "peak_price": entry, "mfe_r": 0.0, "mae_r": 0.0,
         "trail_prints": [entry], "trail_last": None, "last_seen_price": entry,
         "features": {}, "spread_r": f.get("spread_r")}
    loc = cp.initial_local_stop(entry, risk, cfg, spread_r=f.get("spread_r"))
    p["local_stop_price"] = loc if loc else entry - risk
    return p


def walk(f, secs, cfg, ring):
    """Return (exit_ts, trigger_px, why)."""
    p = new_pos(f, cfg)
    entry, risk, t0 = f["entry"], f["risk"], f["t0"]
    dead_min = float(cfg.get("ai_dead_trade_min", 22.0) or 0.0)
    dead_mfe = float(cfg.get("ai_dead_trade_mfe_r", 0.10) or 0.0)
    # _min_hold: honor ai_exit_min_hold_sec on the trail. Live sells through a
    # broker stop order that the min hold does not gate, so A leaves it off.
    min_hold = float(cfg.get("_min_hold", 0.0) or 0.0)
    by_s = {s: (last, lo) for s, last, lo in secs}
    last_px = entry
    end = int(t0) + MAX_HOLD
    for s in range(int(t0) + 1, end + 1):
        if bars.et_minutes(s) >= EOD_MIN:
            return s, last_px, "eod"
        row = by_s.get(s)
        stop = float(p["local_stop_price"])
        if row is not None:
            last, lo = row
            if lo <= entry - risk + 1e-9:
                return s, lo, "plan_stop"
            if lo <= stop + 1e-9 and s - t0 >= min_hold:
                return s, stop, "trail"
            last_px = last
            cp.note_trail_print(p, last, n=ring)
            p["last_seen_price"] = last
            p["peak_price"] = max(float(p["peak_price"]), last)
            p["mfe_r"] = max(float(p["mfe_r"]), (p["peak_price"] - entry) / risk)
            p["mae_r"] = min(float(p["mae_r"]), (last - entry) / risk)
        want = cp.local_profit_stop(p, cfg, now=float(s))
        if want is not None and want > float(p["local_stop_price"]) + 1e-9:
            p["local_stop_price"] = want
        if (dead_min > 0 and float(p["local_stop_price"]) <= entry + 1e-9
                and (s - t0) / 60 >= dead_min and p["mfe_r"] < dead_mfe):
            return s, last_px, "dead_trade"
    return end, last_px, "time_cap"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", required=True, help="comma-separated ET dates")
    ap.add_argument("--set", choices=("default", "timing"), default="default",
                    help="timing = sweep how fast the ratchet tightens")
    args = ap.parse_args()
    global VARIANTS
    if args.set == "timing":
        VARIANTS = TIMING_VARIANTS
    days = set(args.days.split(","))
    base = load_config()
    ring = max(2, int(base.get("ai_local_trail_print_ring", 3) or 3))
    cl = bars.client()
    cache = load_cache()

    fills = []
    for line in open(os.path.join(ROOT, "ai_reports", "outcomes.jsonl")):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        t0, e, st = r.get("entry_time"), r.get("entry_price"), r.get("stop_price")
        if not isinstance(t0, (int, float)) or not e or not st or bars.day_of(t0) not in days:
            continue
        e, st = float(e), float(st)
        if not 0 < st < e:
            continue
        m = bars.et_minutes(t0)
        if m < 9 * 60 + 30 or m >= EOD_MIN:
            continue
        fills.append({"sym": r["symbol"], "day": bars.day_of(t0), "t0": float(t0), "entry": e,
                      "risk": e - st, "spread_r": r.get("spread_r"),
                      "live_pl": r.get("realized_pl_usd"), "qty": r.get("total_qty"),
                      "live_exit": r.get("exit_price"), "live_hold": r.get("hold_sec")})
    print(f"{len(fills)} fills on {sorted(days)}", flush=True)

    for k, f in enumerate(fills, 1):
        key = (f["sym"], int(f["t0"]))
        if key not in cache["trades"]:
            t1 = f["t0"] + MAX_HOLD + 60
            try:
                cache["trades"][key] = fetch_seconds(cl, f["sym"], f["t0"] - 5, t1)
            except Exception as e:  # noqa: BLE001
                print(f"  trades {f['sym']}: {e}"[:120], flush=True)
                continue
            time.sleep(0.3)
            if k % 20 == 0:
                save_cache(cache)
                print(f"  trades {k}/{len(fills)}", flush=True)
    save_cache(cache)

    results = {}
    for label, over in VARIANTS:
        cfg = copy.deepcopy(base)
        cfg.update(over)
        rows = []
        for f in fills:
            secs = cache["trades"].get((f["sym"], int(f["t0"])))
            if not secs:
                continue
            xs, trig, why = walk(f, secs, cfg, ring)
            b = bid_at(cl, cache, f["sym"], xs)
            px = min(trig, b) if (b and why in ("trail", "plan_stop")) else (b or trig)
            rows.append({"day": f["day"], "net": (px / f["entry"] - 1) * 1e4,
                         "hold": xs - f["t0"], "why": why, "f": f})
        results[label] = rows
        save_cache(cache)

    print("\nPINNED-FILL EXIT REPLAY (live trail code, 1 s SIP prints, exit at the SIP bid)")
    print("  net = real fill -> bid at exit, bp of entry (cost of the round trip included)\n")
    a = results[VARIANTS[0][0]]
    live = [r for r in a if r["f"]["live_exit"]]
    if live:
        lnet = statistics.mean((float(r["f"]["live_exit"]) / r["f"]["entry"] - 1) * 1e4 for r in live)
        print(f"  LIVE booked on these fills: mean {lnet:+.1f} bp, median hold "
              f"{statistics.median(float(r['f']['live_hold'] or 0) for r in live):.0f}s  (n={len(live)})")
    all_days = sorted({r["day"] for r in a})
    print(f"  {'variant':<24}{'n':>5}{'mean':>8}{'median':>8}{'win':>6}{'hold med':>10}  by day (mean bp)"
          f"{'':>4}exits")
    for label, _ in VARIANTS:
        rows = results[label]
        if not rows:
            continue
        v = [r["net"] for r in rows]
        dm = defaultdict(list)
        for r in rows:
            dm[r["day"]].append(r["net"])
        why = Counter(r["why"] for r in rows)
        print(f"  {label:<24}{len(v):>5}{statistics.mean(v):>+8.1f}{statistics.median(v):>+8.1f}"
              f"{sum(x > 0 for x in v) / len(v):>6.0%}{statistics.median(r['hold'] for r in rows):>9.0f}s  "
              + " ".join(f"{d[5:]} {statistics.mean(dm[d]):+5.0f}" for d in all_days)
              + "   " + ", ".join(f"{k} {n}" for k, n in why.most_common()))


if __name__ == "__main__":
    main()
