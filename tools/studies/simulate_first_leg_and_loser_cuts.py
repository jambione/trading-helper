#!/usr/bin/env python3
"""simulate_first_leg_and_loser_cuts.py — Counterfactual tick-by-tick simulation
using the live position_shadow.jsonl tick stream and outcomes.jsonl fills.
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.environ.get("EEH_ROOT") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)

from ai_paths import resolve_report_dir

ET = ZoneInfo("America/New_York")
NOTIONAL = 1914.0 * 0.20  # $382.80 per slot (5 slots @ 20% equity)


def load_data():
    d_dir = resolve_report_dir()
    days = ["2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30", "2026-11-02"]
    outcomes = []
    with open(d_dir / "outcomes.jsonl") as f:
        for line in f:
            try:
                r = json.loads(line)
                ts = r.get("exit_time") or r.get("ts")
                if not ts or r.get("realized_pl_usd") is None:
                    continue
                dt = datetime.fromtimestamp(float(ts), tz=ET)
                day = dt.strftime("%Y-%m-%d")
                if day in days:
                    r["day"] = day
                    outcomes.append(r)
            except Exception:
                pass

    ticks_by_sym = defaultdict(list)
    with open(d_dir / "position_shadow.jsonl") as f:
        f.seek(0, 2)
        sz = f.tell()
        f.seek(max(0, sz - 50 * 1024 * 1024))
        f.readline()
        for line in f:
            try:
                r = json.loads(line)
                ts = float(r.get("ts") or 0)
                if ts >= 1792980000:
                    ticks_by_sym[r.get("symbol")].append(r)
            except Exception:
                pass

    for o in outcomes:
        sym = o["symbol"]
        t0 = float(o.get("entry_time") or 0)
        t1 = float(o.get("exit_time") or o.get("ts") or 0)
        ep = float(o.get("entry_price") or 0)
        tks = [
            t for t in ticks_by_sym[sym]
            if (t0 - 5) <= float(t["ts"]) <= (t1 + 5)
            and abs(float(t.get("entry_price") or 0) - ep) < 0.05
            and t.get("price") is not None and float(t.get("price") or 0) > 0
        ]
        tks.sort(key=lambda x: float(x["ts"]))
        o["ticks"] = tks

    return outcomes


def simulate_trade(o: dict, cfg: dict) -> dict | None:
    """Replay a single trade's tick stream under counterfactual `cfg`."""
    arm_why = str(o.get("arm_why") or "")
    if cfg.get("presquare_only", False) and arm_why == "last_overbought":
        return None

    ep = float(o["entry_price"])
    actual_xp = float(o["exit_price"])
    qty = max(1.0, round(NOTIONAL / ep))

    if cfg.get("mode") == "baseline":
        return {
            "symbol": o["symbol"],
            "day": o["day"],
            "pnl": (actual_xp - ep) * qty,
            "ret_pct": (actual_xp - ep) / ep * 100.0,
            "reason": o.get("close_reason"),
            "hold_sec": float(o.get("hold_sec") or 0),
        }

    ticks = o.get("ticks") or []
    if not ticks:
        return {
            "symbol": o["symbol"],
            "day": o["day"],
            "pnl": (actual_xp - ep) * qty,
            "ret_pct": (actual_xp - ep) / ep * 100.0,
            "reason": o.get("close_reason"),
            "hold_sec": float(o.get("hold_sec") or 0),
        }

    profit_capture_pct = float(cfg.get("profit_capture_pct", 0.0))
    be_at_pct = float(cfg.get("be_at_pct", 0.0))
    be_lock_pct = float(cfg.get("be_lock_pct", 0.0))
    max_stop_pct = float(cfg.get("max_stop_pct", 0.0))
    dead_cut_sec = float(cfg.get("dead_cut_sec", 0.0))
    dead_cut_mfe_pct = float(cfg.get("dead_cut_mfe_pct", 0.02))
    dead_cut_min_loss_pct = float(cfg.get("dead_cut_min_loss_pct", -0.15))
    dead_cut_max_pctr = float(cfg.get("dead_cut_max_pctr", 0.0))

    peak = ep
    t0 = float(ticks[0]["ts"])
    custom_shelf = ep * (1.0 - max_stop_pct / 100.0) if max_stop_pct > 0 else 0.0

    for tk in ticks:
        ts = float(tk["ts"])
        px = float(tk["price"])
        hold_sec = max(0.0, float(tk.get("hold_sec") or (ts - t0)))
        peak = max(peak, px, float(tk.get("peak_price") or px))
        mfe_pct = (peak - ep) / ep * 100.0
        cur_pct = (px - ep) / ep * 100.0
        pctr = float(tk.get("pctr") if tk.get("pctr") is not None else -50.0)

        # 1. Hard First-Leg Profit Capture (selling into strength at target)
        if profit_capture_pct > 0 and cur_pct + 1e-9 >= profit_capture_pct:
            xp = round(ep * (1.0 + profit_capture_pct / 100.0), 2)
            return {
                "symbol": o["symbol"],
                "day": o["day"],
                "pnl": (xp - ep) * qty,
                "ret_pct": (xp - ep) / ep * 100.0,
                "reason": "profit_capture",
                "hold_sec": hold_sec,
            }

        # 2. Check custom_shelf
        if custom_shelf > 0 and px <= custom_shelf + 1e-9:
            if float(o.get("hold_sec") or 0) > hold_sec + 15:
                xp = round(min(px, custom_shelf) - 0.01, 2)
                return {
                    "symbol": o["symbol"],
                    "day": o["day"],
                    "pnl": (xp - ep) * qty,
                    "ret_pct": (xp - ep) / ep * 100.0,
                    "reason": "custom_stop",
                    "hold_sec": hold_sec,
                }

        # 3. Smart Dead-Trade / Zombie Cut (only when never went green AND momentum broke)
        if dead_cut_sec > 0 and hold_sec >= dead_cut_sec:
            if mfe_pct <= dead_cut_mfe_pct and cur_pct <= dead_cut_min_loss_pct:
                if dead_cut_max_pctr == 0.0 or pctr <= dead_cut_max_pctr:
                    if float(o.get("hold_sec") or 0) > hold_sec + 15:
                        xp = round(px - 0.01, 2)
                        return {
                            "symbol": o["symbol"],
                            "day": o["day"],
                            "pnl": (xp - ep) * qty,
                            "ret_pct": (xp - ep) / ep * 100.0,
                            "reason": "smart_dead_cut",
                            "hold_sec": hold_sec,
                        }

        # 4. Ratchet custom_shelf for next tick
        if be_at_pct > 0 and mfe_pct + 1e-9 >= be_at_pct:
            be_px = round(ep * (1.0 + be_lock_pct / 100.0), 2)
            if be_px < px:
                custom_shelf = max(custom_shelf, be_px)

    return {
        "symbol": o["symbol"],
        "day": o["day"],
        "pnl": (actual_xp - ep) * qty,
        "ret_pct": (actual_xp - ep) / ep * 100.0,
        "reason": o.get("close_reason"),
        "hold_sec": float(o.get("hold_sec") or 0),
    }


def summarize(outcomes: list[dict], cfg: dict, days_filter: list[str]) -> dict:
    res = []
    for o in outcomes:
        if o["day"] not in days_filter:
            continue
        r = simulate_trade(o, cfg)
        if r is not None:
            res.append(r)
    wins = [x for x in res if x["pnl"] > 0.001]
    losses = [x for x in res if x["pnl"] < -0.001]
    tot_pnl = sum(x["pnl"] for x in res)
    wr = len(wins) / (len(wins) + len(losses)) * 100.0 if (wins or losses) else 0.0
    avg_w = sum(x["pnl"] for x in wins) / len(wins) if wins else 0.0
    avg_l = sum(x["pnl"] for x in losses) / len(losses) if losses else 0.0
    avg_hold = sum(x["hold_sec"] for x in res) / len(res) if res else 0.0
    return {
        "n": len(res),
        "w": len(wins),
        "l": len(losses),
        "wr": wr,
        "pnl": tot_pnl,
        "avg_w": avg_w,
        "avg_l": avg_l,
        "avg_hold": avg_hold,
    }


def run():
    outcomes = load_data()
    today = ["2026-11-02"]
    last_week = ["2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30"]
    all_days = last_week + today

    arms = [
        ("0. Baseline (As Traded @ 20% Sizing)", {
            "mode": "baseline",
            "presquare_only": False,
        }),
        ("1. Entry Cleanup Only (presquare_only=True)", {
            "mode": "baseline",
            "presquare_only": True,
        }),
        ("2. First-Leg Profit Capture (+0.20% Target)", {
            "presquare_only": True,
            "profit_capture_pct": 0.20,
        }),
        ("3. First-Leg Capture (+0.20%) + BE Lock (+0.03% @ +0.15% MFE)", {
            "presquare_only": True,
            "profit_capture_pct": 0.20,
            "be_at_pct": 0.15,
            "be_lock_pct": 0.03,
        }),
        ("4. Smart Loser Cut (5m Zombie Cut when MFE<=0.02% & %R<=-70)", {
            "presquare_only": True,
            "dead_cut_sec": 300.0,
            "dead_cut_mfe_pct": 0.02,
            "dead_cut_min_loss_pct": -0.15,
            "dead_cut_max_pctr": -70.0,
        }),
        ("5. Cap Initial Stop at -0.40% (vs -0.60%) + Smart 5m Cut", {
            "presquare_only": True,
            "max_stop_pct": 0.40,
            "dead_cut_sec": 300.0,
            "dead_cut_mfe_pct": 0.02,
            "dead_cut_min_loss_pct": -0.15,
            "dead_cut_max_pctr": -70.0,
        }),
        ("6. RECOMMENDED COMBINED (Entry + First-Leg +0.20% + Smart Cut)", {
            "presquare_only": True,
            "profit_capture_pct": 0.20,
            "be_at_pct": 0.15,
            "be_lock_pct": 0.03,
            "dead_cut_sec": 300.0,
            "dead_cut_mfe_pct": 0.02,
            "dead_cut_min_loss_pct": -0.15,
            "dead_cut_max_pctr": -70.0,
        }),
    ]

    for period_name, d_list in [
        ("TODAY (2026-11-02)", today),
        ("LAST WEEK (2026-10-26 .. 2026-10-30)", last_week),
        ("ALL 6 SESSIONS COMBINED (10/26 .. 11/02)", all_days),
    ]:
        print("\n" + "=" * 108)
        print(f"SIMULATION PERIOD: {period_name}")
        print("=" * 108)
        print(f"{'STRATEGY ARM':62s} | {'N':3s} | {'W/L':7s} | {'WIN %':6s} | {'NET PNL':9s} | {'AVG W':6s} | {'AVG L':6s} | {'HOLD':5s}")
        print("-" * 108)
        for name, cfg in arms:
            s = summarize(outcomes, cfg, d_list)
            wl = f"{s['w']}W/{s['l']}L"
            print(f"{name:62s} | {s['n']:3d} | {wl:7s} | {s['wr']:5.1f}% | ${s['pnl']:+8.2f} | ${s['avg_w']:+5.2f} | ${s['avg_l']:+5.2f} | {s['avg_hold']:4.0f}s")


if __name__ == "__main__":
    run()
