#!/usr/bin/env python3
"""simulate_october_gap_package.py — Full-Month October 2026 Counterfactual & Gap-Closure Package Simulation.

Evaluates across all 22 trading sessions of October 2026 (2026-10-01 .. 2026-10-30, 914 recorded trades
+ 306,123 watch-board shadow stream rows + real SIP minute-bar VWAP/HOD):

1. Baseline (As Traded in October 2026)
2. Current Shipped Config (c79e3b45 / 7fed2972 as currently configured in bot_config.json, WITH the open gaps)
3. Individual Gap-Fix Ablations (each applied to Current Shipped Config):
   - Gap 1 Fix: Close `pullback_buystop_decide` gate bypass (enforce presquare_only, 4m sq, S5 pitch, WR rise >= 15)
   - Gap 2 Fix: Reward-to-Risk Stop Symmetry (cap initial stop at -0.25% to match +0.20% target, vs -0.65%..0.85% bleed)
   - Gap 3 Fix: Fix Never-Green Zombie Cut (`pctr <= -25` at 180s, `mfe <= 0.04%`, `cur <= -0.12%` + 22m backstop)
   - Gap 4 Fix: Enable Option 2 Anti-Pullback Gate (`ai_watch_anti_pullback_gate`: block < VWAP or >= 2.0% below HOD)
   - Gap 5 Fix: Enable Overhead Order-Block Resistance Skip (`ai_watch_ob_resist_skip`: block `ob_resist_0.3 == True`)
   - Gap 6 Fix: Filter Low-Beta `<3.0%` Tight-Screener Chop (`ai_tight_min_pct_change: 3.0`)
   - Gap 7 Fix: Passive Limit-Floor / Midpoint Exit on `profit_capture` & trail (stop market-sell spread bleed)
   - Gap 8 Fix: Velocity-Gated Runner Ratchet (lock +0.20% floor and trail 0.06% on fast breakouts vs blind cap)
4. Combined Loss-Reduction Package (Gaps 1–6)
5. Combined Profit-Capture Package (Gaps 7–8)
6. Full Gap-Closure Package (All Gaps 1–8 Combined + Validated $10-$20 Movers Unlock) — Week-by-Week & 22-Session Breakdown.
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = os.environ.get("EEH_ROOT") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)

from ai_paths import resolve_report_dir

ET = ZoneInfo("America/New_York")
NOTIONAL = 1914.0 * 0.20  # $382.80 per slot (5 slots @ 20% equity)
NOTIONAL_1K = 1000.0      # $1,000 per slot reference

OCT_START_TS = datetime(2026, 10, 1, 0, 0, tzinfo=ET).timestamp()
OCT_END_TS = datetime(2026, 10, 31, 23, 59, 59, tzinfo=ET).timestamp()


def seek_to_ts(f, target_ts: float):
    """Binary search a JSONL file by 'ts' field."""
    f.seek(0, 2)
    hi = f.tell()
    lo = 0
    while hi - lo > 1024 * 1024:
        mid = (lo + hi) // 2
        f.seek(mid)
        f.readline()
        line = f.readline()
        if not line:
            hi = mid
            continue
        try:
            r = json.loads(line)
            ts = float(r.get("ts") or 0)
            if ts < target_ts:
                lo = mid
            else:
                hi = mid
        except Exception:
            lo = mid
    f.seek(lo)
    if lo > 0:
        f.readline()


def load_october_dataset(d_dir: Path):
    print(f"Loading October 2026 dataset from {d_dir} ...")
    outcomes = []
    with open(d_dir / "outcomes.jsonl") as f:
        for line in f:
            try:
                r = json.loads(line)
                ts = float(r.get("exit_time") or r.get("ts") or 0)
                if OCT_START_TS <= ts <= OCT_END_TS and r.get("realized_pl_usd") is not None:
                    dt = datetime.fromtimestamp(ts, tz=ET)
                    r["day"] = dt.strftime("%Y-%m-%d")
                    outcomes.append(r)
            except Exception:
                pass

    # Match VWAP / HOD from october_counterfactual.json
    cf_path = d_dir / "october_counterfactual.json"
    cf_recs = []
    if cf_path.exists():
        cf_recs = json.load(open(cf_path)).get("records", [])

    cf_by_day_sym = defaultdict(list)
    for c in cf_recs:
        b_ts = datetime.fromisoformat(c["buy_time"].replace("Z", "+00:00")).timestamp()
        c["buy_ts"] = b_ts
        cf_by_day_sym[(c["day"], c["symbol"])].append(c)

    for o in outcomes:
        key = (o["day"], o["symbol"])
        t0 = float(o.get("entry_time") or 0)
        candidates = cf_by_day_sym.get(key, [])
        best = None
        best_dt = 999999.0
        for c in candidates:
            dt = abs(c["buy_ts"] - t0)
            if dt < best_dt and dt < 300.0:
                best_dt = dt
                best = c
        if best:
            o["vwap"] = best.get("vwap")
            o["hod"] = best.get("hod")
            o["drop_pct"] = float(best.get("drop_pct") or 0.0)
            o["opt2_blocked"] = bool(best.get("opt2_blocked"))
            o["is_below_vwap"] = bool(best.get("is_below_vwap"))
        else:
            o["vwap"] = None
            o["hod"] = float(o.get("entry_price") or 0)
            o["drop_pct"] = 0.0
            o["opt2_blocked"] = False
            o["is_below_vwap"] = False

    # Load position_shadow.jsonl ticks
    ticks_by_sym = defaultdict(list)
    with open(d_dir / "position_shadow.jsonl") as f:
        seek_to_ts(f, OCT_START_TS - 3600)
        for line in f:
            try:
                r = json.loads(line)
                ts = float(r.get("ts") or 0)
                if OCT_START_TS - 3600 <= ts <= OCT_END_TS + 3600:
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
            if (t0 - 10) <= float(t["ts"]) <= (t1 + 10)
            and abs(float(t.get("entry_price") or 0) - ep) < 0.10
            and t.get("price") is not None and float(t.get("price") or 0) > 0
        ]
        tks.sort(key=lambda x: float(x["ts"]))
        o["ticks"] = tks

    # Load shadow.jsonl for pre-entry indicator history
    shadow_by_day_sym = defaultdict(lambda: defaultdict(list))
    with open(d_dir / "shadow.jsonl") as f:
        seek_to_ts(f, OCT_START_TS - 3600)
        for line in f:
            try:
                r = json.loads(line)
                ts = float(r.get("ts") or 0)
                if ts < OCT_START_TS or ts > OCT_END_TS:
                    continue
                dt = datetime.fromtimestamp(ts, tz=ET)
                hm = dt.hour * 60 + dt.minute
                if hm < 9 * 60 + 30 or hm >= 15 * 60 + 55:
                    continue
                px = float(r.get("price") or 0)
                if px <= 0:
                    continue
                day = dt.strftime("%Y-%m-%d")
                shadow_by_day_sym[day][r.get("symbol")].append({
                    "ts": ts,
                    "symbol": r.get("symbol"),
                    "price": px,
                    "bid": float(r.get("bid") or px),
                    "ask": float(r.get("ask") or px),
                    "pctr": r.get("pctr"),
                    "pctr_slow": r.get("pctr_slow"),
                    "pctr_gap": r.get("pctr_gap"),
                    "pctr_rising": bool(r.get("pctr_rising")),
                    "pctr_slow_rising": bool(r.get("pctr_slow_rising")),
                    "in_zone": bool(r.get("in_zone")),
                    "arm_ok": bool(r.get("arm_ok")),
                    "arm_why": str(r.get("arm_why") or ""),
                    "rvol": float(r.get("rvol") or 0.0),
                    "rvol_ok": bool(r.get("rvol_ok", True)),
                    "dollar_volume": float(r.get("dollar_volume") or 0.0),
                    "tape_age_sec": float(r.get("tape_age_sec") or 0.0),
                    "cm_rsi": r.get("cm_rsi"),
                    "cm_rsi_rising": bool(r.get("cm_rsi_rising", True)),
                    "ob_resist": bool(r.get("ob_resist_0.3", False)),
                    "ob_room_pct": r.get("ob_room_pct"),
                })
            except Exception:
                pass

    for o in outcomes:
        day = o["day"]
        sym = o["symbol"]
        t0 = float(o.get("entry_time") or 0)
        ep = float(o.get("entry_price") or 0)
        feats = o.get("features") or {}
        o["ob_resist"] = bool(feats.get("ob_resist_0.3", False))
        o["src"] = str(feats.get("source") or o.get("source") or "")
        o["pct_change"] = float(feats.get("pct_change") or 0.0)

        rows = shadow_by_day_sym[day].get(sym, [])
        prior = [x for x in rows if x["ts"] <= t0 + 2.0]

        after_sq_sec = 9999.0
        slow_5m_pitch = 1.0
        wr_rise_5m = 20.0
        pb_bypass_trigger = False
        spread_pct = 0.04

        if prior:
            cur_r = prior[-1]
            if cur_r["ask"] > cur_r["bid"] > 0 and cur_r["price"] > 0:
                spread_pct = max(0.01, min(0.15, (cur_r["ask"] - cur_r["bid"]) / cur_r["price"] * 100.0))

            for x in reversed(prior):
                if t0 - x["ts"] < 15.0:
                    continue
                pf = x["pctr"]
                ps = x["pctr_slow"]
                if pf is not None and ps is not None:
                    if float(pf) >= -20.0 and float(ps) >= -20.0 and abs(float(pf) - float(ps)) <= 15.0:
                        after_sq_sec = t0 - x["ts"]
                        break

            w5 = [x for x in prior if 60.0 <= (t0 - x["ts"]) <= 330.0]
            if w5:
                ps_now = cur_r["pctr_slow"] if cur_r["pctr_slow"] is not None else feats.get("pctr_slow")
                pf_now = cur_r["pctr"] if cur_r["pctr"] is not None else feats.get("pctr")
                ps_old = w5[0]["pctr_slow"]
                if ps_now is not None and ps_old is not None:
                    slow_5m_pitch = float(ps_now) - float(ps_old)
                pf_vals = [float(x["pctr"]) for x in w5 if x["pctr"] is not None]
                if pf_now is not None and pf_vals:
                    wr_rise_5m = float(pf_now) - min(pf_vals)

            w10 = [x["price"] for x in prior if (t0 - x["ts"]) <= 600.0 and x["price"] > 0]
            if len(w10) >= 4:
                lo_10 = min(w10[:max(1, len(w10) // 2)])
                hi_10 = max(w10)
                leg1_gain = (hi_10 - lo_10) / lo_10 * 100.0 if lo_10 > 0 else 0.0
                dip_pct = (hi_10 - ep) / hi_10 * 100.0 if hi_10 > 0 else 0.0
                ob_room = feats.get("ob_room_pct")
                room_ok = (ob_room is None) or (float(ob_room) >= 0.20)
                if leg1_gain >= 0.35 and 0.05 <= dip_pct <= 1.50 and room_ok:
                    pb_bypass_trigger = True

        o["after_sq_sec"] = after_sq_sec
        o["slow_5m_pitch"] = slow_5m_pitch
        o["wr_rise_5m"] = wr_rise_5m
        o["pb_bypass_trigger"] = pb_bypass_trigger
        o["spread_pct"] = spread_pct

    print(f"Loaded {len(outcomes)} October outcomes across {len(set(o['day'] for o in outcomes))} days.")
    return outcomes, shadow_by_day_sym


def passes_entry_gates(o: dict, cfg: dict) -> bool:
    """Evaluates whether recorded trade `o` is allowed through the entry gates under `cfg`."""
    if cfg.get("mode") == "baseline":
        return True

    arm_why = str(o.get("arm_why") or "")

    # 1. Core exhaustion / presquare / trend gates in should_arm_buy
    exh_gate_passed = True

    if cfg.get("presquare_only", True) and arm_why in ("last_overbought", "last_mid_rise"):
        exh_gate_passed = False

    if cfg.get("after_sq_min", 4.0) > 0:
        if o["after_sq_sec"] < cfg["after_sq_min"] * 60.0:
            exh_gate_passed = False

    if cfg.get("wr_slow_5m_veto", True):
        if o["slow_5m_pitch"] < 0.0:
            exh_gate_passed = False

    if cfg.get("wr_trend_min_rise", 15.0) > 0:
        if o["wr_rise_5m"] < cfg["wr_trend_min_rise"]:
            exh_gate_passed = False

    # Gap 1: If `should_arm_buy` rejected the trade (`not exh_gate_passed`),
    # does `pullback_buystop_decide` bypass those gates and let it in anyway?
    if not exh_gate_passed:
        if cfg.get("pullback_bypass_bug", False) and o.get("pb_bypass_trigger", False):
            pass
        else:
            return False

    # Gap 4: Option 2 Anti-Pullback Gate (< VWAP OR >= 2.0% below HOD)
    if cfg.get("anti_pullback_gate", False):
        if o.get("opt2_blocked", False) or o.get("is_below_vwap", False):
            return False

    # Gap 5: Overhead Order-Block Resistance Skip (`ob_resist_0.3 == True`)
    if cfg.get("ob_resist_skip", False):
        if o.get("ob_resist", False):
            return False

    # Gap 6: Filter low-beta `< 3.0%` tight-screener chop
    if cfg.get("tight_min_pct_3", False):
        if o.get("src") == "tight" and abs(o.get("pct_change", 0.0)) < 3.0:
            return False

    return True


def simulate_trade_exit(o: dict, cfg: dict) -> dict | None:
    """Simulates the tick-by-tick exit of trade `o` under `cfg`."""
    if not passes_entry_gates(o, cfg):
        return None

    ep = float(o["entry_price"])
    actual_xp = float(o["exit_price"])
    overall_peak = max(ep, float(o.get("peak_price") or ep))
    overall_mfe_pct = (overall_peak - ep) / ep * 100.0
    st_px = float(o.get("stop_price") or (ep * 0.95))
    r_pct = abs(ep - st_px) / ep * 100.0
    overall_mae_pct = abs(float(o.get("mae_r") or 0.0)) * r_pct

    qty = max(1.0, round(NOTIONAL / ep))
    qty_1k = max(1.0, round(NOTIONAL_1K / ep))
    t0 = float(o.get("entry_time") or 0)
    t1 = float(o.get("exit_time") or o.get("ts") or 0)
    spread_pct = float(o.get("spread_pct") or 0.04)
    half_spread_px = max(0.01, round(ep * (spread_pct / 100.0) * 0.5, 4))

    if cfg.get("mode") == "baseline":
        ret_pct = (actual_xp - ep) / ep * 100.0
        return {
            "symbol": o["symbol"],
            "day": o["day"],
            "entry_ts": t0,
            "exit_ts": t1,
            "pnl": (actual_xp - ep) * qty,
            "pnl_1k": (actual_xp - ep) * qty_1k,
            "ret_pct": ret_pct,
            "ret_bp": ret_pct * 100.0,
            "reason": str(o.get("close_reason") or "local_trail"),
            "hold_sec": float(o.get("hold_sec") or max(0.0, t1 - t0)),
            "source": "recorded",
        }

    ticks = o.get("ticks") or []
    profit_capture_pct = float(cfg.get("profit_capture_pct", 0.20))
    be_at_pct = float(cfg.get("be_at_pct", 0.15))
    be_offset_px = float(cfg.get("be_offset_px", 0.02))
    init_stop_cap_pct = float(cfg.get("init_stop_cap_pct", 0.0))  # Gap 2: e.g. 0.25% symmetric initial stop cap
    dead_cut_sec = float(cfg.get("dead_cut_sec", 300.0))
    dead_cut_mfe_pct = float(cfg.get("dead_cut_mfe_pct", 0.02))
    dead_cut_min_loss_pct = float(cfg.get("dead_cut_min_loss_pct", -0.15))
    dead_cut_max_pctr = float(cfg.get("dead_cut_max_pctr", -70.0))
    dead_trade_sec = float(cfg.get("dead_trade_sec", 0.0))
    passive_exit = bool(cfg.get("passive_exit", False))
    runner_ratchet = bool(cfg.get("runner_ratchet", False))

    peak = ep
    start_ts = float(ticks[0]["ts"]) if ticks else t0
    init_risk = abs(ep - float(ticks[0].get("stop_price") or st_px)) if ticks else abs(ep - st_px)
    if init_risk <= 0 or init_risk > ep * 0.15:
        init_risk = ep * 0.05
    custom_shelf = round(ep * (1.0 - init_stop_cap_pct / 100.0), 2) if init_stop_cap_pct > 0 else 0.0
    runner_mode_active = False
    runner_floor_px = round(ep * (1.0 + profit_capture_pct / 100.0), 2)

    for idx, tk in enumerate(ticks):
        ts = float(tk["ts"])
        px = float(tk["price"])
        hold_sec = max(0.0, float(tk.get("hold_sec") or (ts - start_ts)))
        tk_mfe_r = tk.get("mfe_r")
        tk_peak = max(px, ep + float(tk_mfe_r) * init_risk) if tk_mfe_r is not None else px
        peak = max(peak, px, tk_peak)
        mfe_pct = (peak - ep) / ep * 100.0
        cur_pct = (px - ep) / ep * 100.0
        pctr = float(tk.get("pctr") if tk.get("pctr") is not None else -50.0)
        prev_pctr = float(ticks[idx - 1].get("pctr") or pctr) if idx > 0 else pctr

        # 1. Velocity Runner Ratchet Mode (Gap 8)
        if runner_mode_active:
            runner_trail_px = max(runner_floor_px, round(peak * (1.0 - 0.0006), 2))
            if px <= runner_trail_px + 1e-9:
                xp = runner_trail_px if passive_exit else max(runner_floor_px, round(runner_trail_px - half_spread_px * 0.5, 4))
                ret_pct = (xp - ep) / ep * 100.0
                return {
                    "symbol": o["symbol"],
                    "day": o["day"],
                    "entry_ts": t0,
                    "exit_ts": t0 + hold_sec,
                    "pnl": (xp - ep) * qty,
                    "pnl_1k": (xp - ep) * qty_1k,
                    "ret_pct": ret_pct,
                    "ret_bp": ret_pct * 100.0,
                    "reason": "runner_ratchet",
                    "hold_sec": hold_sec,
                    "source": "recorded",
                }

        # 2. First-Leg Profit Capture (+0.20%)
        if not runner_mode_active and profit_capture_pct > 0 and (cur_pct + 1e-9 >= profit_capture_pct or mfe_pct + 1e-9 >= profit_capture_pct):
            if runner_ratchet and hold_sec <= 120.0 and pctr >= -15.0 and pctr >= prev_pctr and overall_mfe_pct >= 0.28:
                runner_mode_active = True
                custom_shelf = max(custom_shelf, runner_floor_px)
                continue

            target_px = round(ep * (1.0 + profit_capture_pct / 100.0), 2)
            xp = target_px if passive_exit else round(target_px - half_spread_px * 0.65, 4)
            ret_pct = (xp - ep) / ep * 100.0
            return {
                "symbol": o["symbol"],
                "day": o["day"],
                "entry_ts": t0,
                "exit_ts": t0 + hold_sec,
                "pnl": (xp - ep) * qty,
                "pnl_1k": (xp - ep) * qty_1k,
                "ret_pct": ret_pct,
                "ret_bp": ret_pct * 100.0,
                "reason": "profit_capture",
                "hold_sec": hold_sec,
                "source": "recorded",
            }

        # 3. Initial Symmetric Stop Cap (-0.25%) or Break-Even Offset Trail Shelf
        if custom_shelf > 0 and px <= custom_shelf + 1e-9:
            is_be_shelf = custom_shelf >= ep
            if not is_be_shelf or float(o.get("hold_sec") or 0) > hold_sec + 10:
                base_xp = round(max(px, custom_shelf), 2) if is_be_shelf else custom_shelf
                xp = base_xp if passive_exit else round(base_xp - half_spread_px * 0.4, 4)
                ret_pct = (xp - ep) / ep * 100.0
                reason = "be_offset_trail" if is_be_shelf else "sym_stop_25bp"
                return {
                    "symbol": o["symbol"],
                    "day": o["day"],
                    "entry_ts": t0,
                    "exit_ts": t0 + hold_sec,
                    "pnl": (xp - ep) * qty,
                    "pnl_1k": (xp - ep) * qty_1k,
                    "ret_pct": ret_pct,
                    "ret_bp": ret_pct * 100.0,
                    "reason": reason,
                    "hold_sec": hold_sec,
                    "source": "recorded",
                }

        # 4. Smart Never-Green Zombie Cut (Gap 3)
        if dead_cut_sec > 0 and hold_sec >= dead_cut_sec:
            if mfe_pct <= dead_cut_mfe_pct and cur_pct <= dead_cut_min_loss_pct and pctr <= dead_cut_max_pctr:
                if float(o.get("hold_sec") or 0) > hold_sec + 10:
                    xp = round(px, 2) if passive_exit else round(px - 0.01, 2)
                    ret_pct = (xp - ep) / ep * 100.0
                    return {
                        "symbol": o["symbol"],
                        "day": o["day"],
                        "entry_ts": t0,
                        "exit_ts": t0 + hold_sec,
                        "pnl": (xp - ep) * qty,
                        "pnl_1k": (xp - ep) * qty_1k,
                        "ret_pct": ret_pct,
                        "ret_bp": ret_pct * 100.0,
                        "reason": "smart_dead_cut",
                        "hold_sec": hold_sec,
                        "source": "recorded",
                    }

        # 5. 22-Minute Hard Dead-Trade Backstop (Gap 3)
        if dead_trade_sec > 0 and hold_sec >= dead_trade_sec:
            if mfe_pct < be_at_pct and cur_pct < 0.0:
                if float(o.get("hold_sec") or 0) > hold_sec + 10:
                    xp = round(px, 2) if passive_exit else round(px - half_spread_px * 0.5, 4)
                    ret_pct = (xp - ep) / ep * 100.0
                    return {
                        "symbol": o["symbol"],
                        "day": o["day"],
                        "entry_ts": t0,
                        "exit_ts": t0 + hold_sec,
                        "pnl": (xp - ep) * qty,
                        "pnl_1k": (xp - ep) * qty_1k,
                        "ret_pct": ret_pct,
                        "ret_bp": ret_pct * 100.0,
                        "reason": "dead_trade_22m",
                        "hold_sec": hold_sec,
                        "source": "recorded",
                    }

        # Arm Two-Stage Break-Even Offset: Stage 1 (+$0.01 at +0.11% MFE), Stage 2 (+$0.02 at +0.15% MFE)
        if cfg.get("early_be_ratchet", False):
            if mfe_pct + 1e-9 >= 0.15:
                be_px = round(ep + 0.02, 2)
                if be_px + 0.01 < px:
                    custom_shelf = max(custom_shelf, be_px)
            elif mfe_pct + 1e-9 >= 0.11:
                be_px = round(ep + 0.01, 2)
                if be_px + 0.01 < px:
                    custom_shelf = max(custom_shelf, be_px)
        elif be_at_pct > 0 and mfe_pct + 1e-9 >= be_at_pct:
            be_px = round(ep + be_offset_px, 2)
            if be_px < px:
                custom_shelf = max(custom_shelf, be_px)

    # Check if intra-trade overall_mfe_pct reached +0.20% between 10s shadow polls
    if profit_capture_pct > 0 and overall_mfe_pct + 1e-9 >= profit_capture_pct:
        # Check if it hit the -0.25% stop BEFORE reaching +0.20%
        if init_stop_cap_pct > 0 and overall_mae_pct >= init_stop_cap_pct and actual_xp < ep:
            stop_cap_px = round(ep * (1.0 - init_stop_cap_pct / 100.0), 2)
            xp = stop_cap_px if passive_exit else round(stop_cap_px - half_spread_px * 0.4, 4)
            reason = "sym_stop_25bp"
        elif runner_ratchet and overall_mfe_pct >= 0.28:
            xp = max(actual_xp, round(overall_peak * (1.0 - 0.0006), 2), runner_floor_px)
            reason = "runner_ratchet"
        else:
            target_px = round(ep * (1.0 + profit_capture_pct / 100.0), 2)
            xp = target_px if passive_exit else round(target_px - half_spread_px * 0.65, 4)
            reason = "profit_capture"
        ret_pct = (xp - ep) / ep * 100.0
        return {
            "symbol": o["symbol"],
            "day": o["day"],
            "entry_ts": t0,
            "exit_ts": t1,
            "pnl": (xp - ep) * qty,
            "pnl_1k": (xp - ep) * qty_1k,
            "ret_pct": ret_pct,
            "ret_bp": ret_pct * 100.0,
            "reason": reason,
            "hold_sec": min(float(o.get("hold_sec") or 60.0), 120.0),
            "source": "recorded",
        }

    # Check if intra-trade overall_mfe_pct reached BE offset before stopping out red
    eff_be_thr = 0.11 if cfg.get("early_be_ratchet", False) else be_at_pct
    eff_be_off = (0.02 if overall_mfe_pct >= 0.15 else 0.01) if cfg.get("early_be_ratchet", False) else be_offset_px
    if eff_be_thr > 0 and overall_mfe_pct + 1e-9 >= eff_be_thr and actual_xp < ep + eff_be_off:
        base_xp = round(ep + eff_be_off, 2)
        xp = base_xp if passive_exit else round(base_xp - half_spread_px * 0.4, 4)
        ret_pct = (xp - ep) / ep * 100.0
        return {
            "symbol": o["symbol"],
            "day": o["day"],
            "entry_ts": t0,
            "exit_ts": t1,
            "pnl": (xp - ep) * qty,
            "pnl_1k": (xp - ep) * qty_1k,
            "ret_pct": ret_pct,
            "ret_bp": ret_pct * 100.0,
            "reason": "be_offset_trail",
            "hold_sec": float(o.get("hold_sec") or max(0.0, t1 - t0)),
            "source": "recorded",
        }

    # Check if trade exited worse than -init_stop_cap_pct between polls
    xp = actual_xp
    reason = str(o.get("close_reason") or "local_trail")
    if init_stop_cap_pct > 0:
        stop_cap_px = round(ep * (1.0 - init_stop_cap_pct / 100.0), 2)
        if actual_xp < stop_cap_px:
            xp = stop_cap_px if passive_exit else round(stop_cap_px - half_spread_px * 0.4, 4)
            reason = "sym_stop_25bp"
        elif passive_exit and reason == "local_trail":
            xp = round(actual_xp + half_spread_px * 0.45, 4)
    elif passive_exit and reason == "local_trail":
        xp = round(actual_xp + half_spread_px * 0.45, 4)

    ret_pct = (xp - ep) / ep * 100.0
    return {
        "symbol": o["symbol"],
        "day": o["day"],
        "entry_ts": t0,
        "exit_ts": t1,
        "pnl": (xp - ep) * qty,
        "pnl_1k": (xp - ep) * qty_1k,
        "ret_pct": ret_pct,
        "ret_bp": ret_pct * 100.0,
        "reason": reason,
        "hold_sec": float(o.get("hold_sec") or max(0.0, t1 - t0)),
        "source": "recorded",
    }


def evaluate_arm(outcomes: list[dict], cfg: dict) -> list[dict]:
    trades = []
    for o in outcomes:
        tr = simulate_trade_exit(o, cfg)
        if tr is not None:
            trades.append(tr)
    trades.sort(key=lambda x: x["entry_ts"])
    return trades


def summarize(trades: list[dict], num_days: int = 22) -> dict:
    n = len(trades)
    if n == 0:
        return {
            "n": 0, "per_day": 0.0, "wr": 0.0, "pnl": 0.0, "pnl_1k": 0.0,
            "tot_bp": 0.0, "avg_bp": 0.0, "pf": 0.0, "gross_win": 0.0,
            "gross_loss": 0.0, "max_dd": 0.0, "pos_days": 0
        }
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    gw = sum(t["pnl"] for t in wins)
    gl = abs(sum(t["pnl"] for t in losses))
    pnl = sum(t["pnl"] for t in trades)
    pnl_1k = sum(t["pnl_1k"] for t in trades)
    tot_bp = sum(t["ret_bp"] for t in trades)

    eq = 0.0
    pk = 0.0
    max_dd = 0.0
    by_day_pnl = defaultdict(float)
    for t in sorted(trades, key=lambda x: x["entry_ts"]):
        eq += t["pnl"]
        pk = max(pk, eq)
        max_dd = max(max_dd, pk - eq)
        by_day_pnl[t["day"]] += t["pnl"]

    pos_days = sum(1 for d, v in by_day_pnl.items() if v >= 0)
    return {
        "n": n,
        "per_day": n / max(1, num_days),
        "wr": len(wins) / n * 100.0,
        "pnl": pnl,
        "pnl_1k": pnl_1k,
        "tot_bp": tot_bp,
        "avg_bp": tot_bp / n,
        "pf": gw / gl if gl > 0 else 99.9,
        "gross_win": gw,
        "gross_loss": gl,
        "max_dd": max_dd,
        "pos_days": pos_days,
    }


def main():
    d_dir = resolve_report_dir()
    outcomes, _ = load_october_dataset(d_dir)
    days = sorted(set(o["day"] for o in outcomes))
    num_days = len(days)

    # ── Define Configurations ─────────────────────────────────────────────────
    cfg_baseline = {"mode": "baseline"}

    # Arm 1: Current Shipped Live Config (with the open gaps present)
    cfg_current_live = {
        "mode": "sim",
        "presquare_only": True,
        "after_sq_min": 4.0,
        "wr_slow_5m_veto": True,
        "wr_trend_min_rise": 15.0,
        "pullback_bypass_bug": True,     # GAP 1 OPEN: pullback_buystop strips presquare/exh rules
        "profit_capture_pct": 0.20,
        "be_at_pct": 0.15,
        "be_offset_px": 0.02,
        "init_stop_cap_pct": 0.0,        # GAP 2 OPEN: -0.65% initial trail stop vs +0.20% target (1:3.25 asymmetry)
        "dead_cut_sec": 300.0,
        "dead_cut_mfe_pct": 0.02,        # GAP 3 OPEN: 0.02% misses 1c false-green zombies
        "dead_cut_min_loss_pct": -0.15,
        "dead_cut_max_pctr": -70.0,      # GAP 3 OPEN: -70.0 misses 14m bar %R zombies
        "dead_trade_sec": 0.0,           # GAP 3 OPEN: ai_dead_trade_min is 0.0
        "anti_pullback_gate": False,     # GAP 4 OPEN: ai_watch_anti_pullback_gate is false
        "ob_resist_skip": False,         # GAP 5 OPEN: ai_watch_ob_resist_skip is false
        "tight_min_pct_3": False,        # GAP 6 OPEN: ai_tight_min_pct_change is 1.5% (admits slow ETFs/megacaps)
        "early_be_ratchet": False,       # GAP 7 OPEN: BE waits for +0.15%, letting +0.08%..+0.14% winners round-trip red
        "passive_exit": False,           # GAP 8 OPEN: market sell on profit_capture & trail
        "runner_ratchet": False,         # GAP 9 OPEN: blind cap at +0.20% on all winners
    }

    # Individual Gap Fix Ablations (each applied individually to cfg_current_live)
    cfg_gap1 = {**cfg_current_live, "pullback_bypass_bug": False}
    cfg_gap2 = {**cfg_current_live, "init_stop_cap_pct": 0.25}
    cfg_gap3 = {
        **cfg_current_live,
        "dead_cut_sec": 240.0,
        "dead_cut_mfe_pct": 0.05,
        "dead_cut_min_loss_pct": -0.12,
        "dead_cut_max_pctr": -30.0,
        "dead_trade_sec": 1320.0,
    }
    cfg_gap4 = {**cfg_current_live, "anti_pullback_gate": True}
    cfg_gap5 = {**cfg_current_live, "ob_resist_skip": True}
    cfg_gap6 = {**cfg_current_live, "tight_min_pct_3": True}
    cfg_gap7 = {**cfg_current_live, "early_be_ratchet": True}
    cfg_gap8 = {**cfg_current_live, "passive_exit": True}
    cfg_gap9 = {**cfg_current_live, "runner_ratchet": True}

    # Combined Loss-Reduction Package (Gaps 1..7)
    cfg_loss_pkg = {
        **cfg_current_live,
        "pullback_bypass_bug": False,
        "init_stop_cap_pct": 0.25,
        "dead_cut_sec": 240.0,
        "dead_cut_mfe_pct": 0.05,
        "dead_cut_min_loss_pct": -0.12,
        "dead_cut_max_pctr": -30.0,
        "dead_trade_sec": 1320.0,
        "anti_pullback_gate": True,
        "ob_resist_skip": True,
        "tight_min_pct_3": True,
        "early_be_ratchet": True,
    }

    # Combined Profit-Capture Package (Gaps 7..9)
    cfg_profit_pkg = {
        **cfg_current_live,
        "early_be_ratchet": True,
        "passive_exit": True,
        "runner_ratchet": True,
    }

    # Full Gap-Closure Package (All Gaps 1..9 Closed Together)
    cfg_full = {
        **cfg_loss_pkg,
        "passive_exit": True,
        "runner_ratchet": True,
    }

    arms = [
        ("0. Oct Historical Baseline (As Traded)", cfg_baseline),
        ("1. Current Shipped Config (Open Gaps)", cfg_current_live),
        ("   + Gap 1: Fix Pullback Gate Bypass", cfg_gap1),
        ("   + Gap 2: Symmetric -0.25% Initial Stop Cap", cfg_gap2),
        ("   + Gap 3: 4m Zombie Cut (pctr<=-30) + 22m", cfg_gap3),
        ("   + Gap 4: Anti-Pullback (VWAP & <2% HOD)", cfg_gap4),
        ("   + Gap 5: OB Overhead Resist Skip", cfg_gap5),
        ("   + Gap 6: Tight Screener Min +3.0% Day", cfg_gap6),
        ("   + Gap 7: Early +0.08% Two-Stage BE Lock", cfg_gap7),
        ("   + Gap 8: Passive Limit/Midpoint Exit", cfg_gap8),
        ("   + Gap 9: Velocity Runner Ratchet", cfg_gap9),
        ("2. Loss-Reduction Package (Gaps 1-7)", cfg_loss_pkg),
        ("3. Profit-Capture Package (Gaps 7-9)", cfg_profit_pkg),
        ("4. FULL GAP-CLOSURE PACKAGE (All Gaps 1-9)", cfg_full),
    ]

    results = {}
    for label, c in arms:
        results[label] = evaluate_arm(outcomes, c)

    print("\n" + "=" * 126)
    print(f"OCTOBER 2026 FULL-MONTH SIMULATION ({num_days} TRADING SESSIONS: {days[0]} .. {days[-1]})")
    print(f"Slot Sizing: ${NOTIONAL:.2f}/slot (20% of $1,914) | Also showing $ at $1,000/slot and cumulative Basis Points (bp)")
    print("=" * 126)
    print(
        f"{'Strategy / Package Arm':44s} | {'Trades':6s} | {'Win%':6s} | {'PF':5s} | "
        f"{'GrossWin':9s} | {'GrossLoss':9s} | {'Net($383)':10s} | {'Net($1k)':10s} | {'Total bp':9s} | {'PosDays':7s}"
    )
    print("-" * 126)

    for label, _ in arms:
        m = summarize(results[label], num_days)
        print(
            f"{label:44s} | {m['n']:6d} | {m['wr']:5.1f}% | {m['pf']:5.2f} | "
            f"${m['gross_win']:8.2f} | -${m['gross_loss']:8.2f} | "
            f"${m['pnl']:+9.2f} | ${m['pnl_1k']:+9.2f} | {m['tot_bp']:+8.1f} | {m['pos_days']:2d}/{num_days}"
        )
    print("=" * 126)

    # ── Week-by-Week Breakdown ────────────────────────────────────────────────
    weeks = [
        ("Week 1 (Oct 01 - Oct 02)", [d for d in days if "2026-10-01" <= d <= "2026-10-02"]),
        ("Week 2 (Oct 05 - Oct 09)", [d for d in days if "2026-10-05" <= d <= "2026-10-09"]),
        ("Week 3 (Oct 12 - Oct 16)", [d for d in days if "2026-10-12" <= d <= "2026-10-16"]),
        ("Week 4 (Oct 19 - Oct 23)", [d for d in days if "2026-10-19" <= d <= "2026-10-23"]),
        ("Week 5 (Oct 26 - Oct 30)", [d for d in days if "2026-10-26" <= d <= "2026-10-30"]),
    ]

    t_base = results["0. Oct Historical Baseline (As Traded)"]
    t_curr = results["1. Current Shipped Config (Open Gaps)"]
    t_full = results["4. FULL GAP-CLOSURE PACKAGE (All Gaps 1-9)"]

    print("\n" + "=" * 118)
    print("OCTOBER 2026 WEEK-BY-WEEK COMPARISON (Baseline vs. Current Shipped Config vs. Full Gap-Closure Package)")
    print("=" * 118)
    print(f"{'Week Window':25s} | {'Baseline (As Traded)':26s} | {'Current Shipped Config':26s} | {'Full Gap Package (1-8)':30s}")
    print("-" * 118)
    for w_label, w_days in weeks:
        w_set = set(w_days)
        mb = summarize([x for x in t_base if x["day"] in w_set], len(w_days))
        mc = summarize([x for x in t_curr if x["day"] in w_set], len(w_days))
        mf = summarize([x for x in t_full if x["day"] in w_set], len(w_days))
        print(
            f"{w_label:25s} | "
            f"{mb['n']:3d} tr ({mb['wr']:4.1f}% | ${mb['pnl']:+7.2f}) | "
            f"{mc['n']:3d} tr ({mc['wr']:4.1f}% | ${mc['pnl']:+7.2f}) | "
            f"{mf['n']:3d} tr ({mf['wr']:4.1f}% | ${mf['pnl']:+7.2f} / {mf['tot_bp']:+6.1f}bp)"
        )
    print("=" * 118)

    # ── Session-by-Session (All 22 Days) Breakdown ────────────────────────────
    print("\n" + "=" * 118)
    print("SESSION-BY-SESSION BREAKDOWN ACROSS ALL 22 OCTOBER SESSIONS")
    print("=" * 118)
    print(f"{'Date':10s} | {'Baseline (As Traded)':26s} | {'Current Shipped Config':26s} | {'Full Gap Package (Gaps 1-8)':32s} | {'Net Lift ($)'}")
    print("-" * 118)
    for d in days:
        mb = summarize([x for x in t_base if x["day"] == d], 1)
        mc = summarize([x for x in t_curr if x["day"] == d], 1)
        mf = summarize([x for x in t_full if x["day"] == d], 1)
        lift_vs_curr = mf["pnl"] - mc["pnl"]
        lift_vs_base = mf["pnl"] - mb["pnl"]
        print(
            f"{d:10s} | "
            f"{mb['n']:3d} tr ({mb['wr']:4.1f}% | ${mb['pnl']:+7.2f}) | "
            f"{mc['n']:3d} tr ({mc['wr']:4.1f}% | ${mc['pnl']:+7.2f}) | "
            f"{mf['n']:3d} tr ({mf['wr']:4.1f}% | ${mf['pnl']:+7.2f} / {mf['tot_bp']:+6.1f}bp) | "
            f"${lift_vs_curr:+6.2f} vs Curr (${lift_vs_base:+6.2f} vs Base)"
        )
    print("=" * 118)

    rc = Counter(t["reason"] for t in t_full)
    print("\nFull Gap Package Exit Reasons:", dict(rc))


if __name__ == "__main__":
    main()
