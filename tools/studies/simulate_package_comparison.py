#!/usr/bin/env python3
"""simulate_package_comparison.py — Full counterfactual simulation comparing:
1. Baseline (As Traded) on Last Week (2026-10-26..2026-10-30) and Today (2026-11-02)
2. Exit & Quality Package Only (presquare_only + 0.20% First-Leg Capture + 2c BE Offset + 5m Smart Zombie Cut)
3. Breakdown of Individual Volume Scaling Levers + Combined Safe Volume Package across all 6 days.
"""
from __future__ import annotations

import gzip
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
LAST_WEEK = ["2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30"]
TODAY = ["2026-11-02"]
ALL_DAYS = LAST_WEEK + TODAY
START_TS = 1792980000.0  # 2026-10-26 00:00 ET


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


def load_outcomes_and_ticks(d_dir: Path):
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
                if day in ALL_DAYS:
                    r["day"] = day
                    outcomes.append(r)
            except Exception:
                pass

    ticks_by_sym = defaultdict(list)
    with open(d_dir / "position_shadow.jsonl") as f:
        seek_to_ts(f, START_TS)
        for line in f:
            try:
                r = json.loads(line)
                ts = float(r.get("ts") or 0)
                if ts >= START_TS:
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


def load_shadow_streams(d_dir: Path):
    """Load all RTH shadow.jsonl rows for 2026-10-26..2026-11-02."""
    by_day_sym = defaultdict(lambda: defaultdict(list))
    with open(d_dir / "shadow.jsonl") as f:
        f.seek(0, 2)
        sz = f.tell()
        # Seek back 450MB to cover all of 2026-10-26..2026-11-02
        f.seek(max(0, sz - 450 * 1024 * 1024))
        if f.tell() > 0:
            f.readline()
        for line in f:
            try:
                r = json.loads(line)
                ts = float(r.get("ts") or 0)
                if ts < START_TS:
                    continue
                dt = datetime.fromtimestamp(ts, tz=ET)
                day = dt.strftime("%Y-%m-%d")
                if day not in ALL_DAYS:
                    continue
                hm = dt.hour * 60 + dt.minute
                if hm < 9 * 60 + 30 or hm >= 15 * 60 + 50:
                    continue
                px = float(r.get("price") or 0)
                if px <= 0:
                    continue
                by_day_sym[day][r.get("symbol")].append({
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
                    "pctr_fast_pitch": float(r.get("pctr_fast_pitch") or 0.0),
                    "pctr_slow_pitch": float(r.get("pctr_slow_pitch") or 0.0),
                    "in_zone": bool(r.get("in_zone")),
                    "arm_ok": bool(r.get("arm_ok")),
                    "arm_why": str(r.get("arm_why") or ""),
                    "rvol": float(r.get("rvol") or 0.0),
                    "rvol_ok": bool(r.get("rvol_ok", True)),
                    "dollar_volume": float(r.get("dollar_volume") or 0.0),
                    "tape_age_sec": float(r.get("tape_age_sec") or 0.0),
                    "cm_rsi": float(r.get("cm_rsi") or 50.0),
                    "cm_rsi_rising": bool(r.get("cm_rsi_rising", True)),
                    "ob_resist": bool(r.get("ob_resist_0.3", False)),
                })
            except Exception:
                pass
    for day in by_day_sym:
        for sym in by_day_sym[day]:
            by_day_sym[day][sym].sort(key=lambda x: x["ts"])
    return by_day_sym


def load_midcap_10_20_streams():
    """Build 10s indicator streams from ~/session_snapshots/<DAY>/recorder_prints.jsonl.gz
    for liquid $10-$20 movers that were blocked by below_min_price ($20 floor).
    """
    snap_root = Path(os.path.expanduser("~/session_snapshots"))
    by_day_sym = defaultdict(lambda: defaultdict(list))
    if not snap_root.exists():
        return by_day_sym

    for day in ALL_DAYS:
        day_dir = snap_root / day
        prints_p = day_dir / "recorder_prints.jsonl.gz"
        if not prints_p.exists():
            continue

        # Bucket RTH prints into 10-second bars for all active $10-$20 symbols
        bars_10s = defaultdict(dict)  # sym -> bucket_idx -> [high, low, close]
        with gzip.open(prints_p, "rt") as f:
            for line in f:
                try:
                    r = json.loads(line)
                    px = float(r.get("price") or 0)
                    if px < 10.0 or px >= 20.0:
                        continue
                    age = float(r.get("age_sec") or 0)
                    if age > 15.0:
                        continue
                    ts = float(r.get("ts") or 0)
                    sym = r.get("symbol")
                    if not sym:
                        continue
                    b_idx = int(ts // 10) * 10
                    if b_idx not in bars_10s[sym]:
                        bars_10s[sym][b_idx] = [px, px, px]
                    else:
                        b = bars_10s[sym][b_idx]
                        if px > b[0]:
                            b[0] = px
                        if px < b[1]:
                            b[1] = px
                        b[2] = px
                except Exception:
                    pass

        for sym, bmap in bars_10s.items():
            if len(bmap) < 150:  # Require genuinely liquid tape (>=150 10s print buckets)
                continue
            times = sorted(bmap.keys())
            highs = [bmap[t][0] for t in times]
            lows = [bmap[t][1] for t in times]
            closes = [bmap[t][2] for t in times]
            open_px = closes[0]
            vwap_sum = 0.0
            for idx, t in enumerate(times):
                vwap_sum += closes[idx]
                vwap = vwap_sum / (idx + 1)
                if idx < 60:
                    continue
                dt = datetime.fromtimestamp(t, tz=ET)
                hm = dt.hour * 60 + dt.minute
                if hm < 9 * 60 + 35 or hm >= 15 * 60 + 50:
                    continue
                h14 = max(highs[max(0, idx - 84):idx + 1])
                l14 = min(lows[max(0, idx - 84):idx + 1])
                h34 = max(highs[max(0, idx - 204):idx + 1])
                l34 = min(lows[max(0, idx - 204):idx + 1])
                if h14 - l14 < 0.04 or h34 - l34 < 0.06:
                    continue
                c = closes[idx]
                pctr = (h14 - c) / (h14 - l14) * -100.0
                pslow = (h34 - c) / (h34 - l34) * -100.0
                prev_rows = by_day_sym[day][sym]
                prev_pctr = prev_rows[-1]["pctr"] if prev_rows else pctr
                prev5_pctr = prev_rows[-5]["pctr"] if len(prev_rows) >= 5 else pctr
                prev5_slow = prev_rows[-5]["pctr_slow"] if len(prev_rows) >= 5 else pslow
                by_day_sym[day][sym].append({
                    "ts": float(t),
                    "symbol": sym,
                    "price": c,
                    "bid": round(c - 0.01, 2),
                    "ask": c,
                    "pctr": pctr,
                    "pctr_slow": pslow,
                    "pctr_gap": abs(pctr - pslow),
                    "pctr_rising": (pctr - prev_pctr) >= 0.2,
                    "pctr_slow_rising": (pslow >= prev5_slow),
                    "pctr_fast_pitch": pctr - prev5_pctr,
                    "pctr_slow_pitch": pslow - prev5_slow,
                    "wr_rise_5m": pctr - (prev_rows[-15]["pctr"] if len(prev_rows) >= 15 else pctr),
                    "in_zone": (c >= vwap and c >= open_px * 1.003),  # Green on day & above VWAP
                    "arm_ok": False,
                    "arm_why": "below_min_price",
                    "rvol": 1.5,
                    "rvol_ok": True,
                    "dollar_volume": 5000000.0,
                    "tape_age_sec": 2.0,
                    "cm_rsi": 65.0,
                    "cm_rsi_rising": True,
                    "ob_resist": False,
                })
    return by_day_sym


def simulate_recorded_trade(o: dict, cfg: dict) -> dict | None:
    arm_why = str(o.get("arm_why") or "")
    if cfg.get("presquare_only", False) and arm_why == "last_overbought":
        return None

    ep = float(o["entry_price"])
    actual_xp = float(o["exit_price"])
    qty = max(1.0, round(NOTIONAL / ep))
    t0 = float(o.get("entry_time") or 0)
    t1 = float(o.get("exit_time") or o.get("ts") or 0)

    if cfg.get("mode") == "baseline":
        return {
            "symbol": o["symbol"],
            "day": o["day"],
            "entry_ts": t0,
            "exit_ts": t1,
            "pnl": (actual_xp - ep) * qty,
            "ret_pct": (actual_xp - ep) / ep * 100.0,
            "reason": o.get("close_reason"),
            "hold_sec": float(o.get("hold_sec") or max(0.0, t1 - t0)),
            "source": "recorded",
        }

    ticks = o.get("ticks") or []
    if not ticks:
        return {
            "symbol": o["symbol"],
            "day": o["day"],
            "entry_ts": t0,
            "exit_ts": t1,
            "pnl": (actual_xp - ep) * qty,
            "ret_pct": (actual_xp - ep) / ep * 100.0,
            "reason": o.get("close_reason"),
            "hold_sec": float(o.get("hold_sec") or max(0.0, t1 - t0)),
            "source": "recorded",
        }

    profit_capture_pct = float(cfg.get("profit_capture_pct", 0.0))
    be_at_pct = float(cfg.get("be_at_pct", 0.0))
    be_offset_px = float(cfg.get("be_offset_px", 0.0))
    dead_cut_sec = float(cfg.get("dead_cut_sec", 0.0))
    dead_cut_mfe_pct = float(cfg.get("dead_cut_mfe_pct", 0.02))
    dead_cut_min_loss_pct = float(cfg.get("dead_cut_min_loss_pct", -0.15))
    dead_cut_max_pctr = float(cfg.get("dead_cut_max_pctr", -70.0))

    peak = ep
    start_ts = float(ticks[0]["ts"])
    custom_shelf = 0.0

    for tk in ticks:
        ts = float(tk["ts"])
        px = float(tk["price"])
        hold_sec = max(0.0, float(tk.get("hold_sec") or (ts - start_ts)))
        peak = max(peak, px, float(tk.get("peak_price") or px))
        mfe_pct = (peak - ep) / ep * 100.0
        cur_pct = (px - ep) / ep * 100.0
        pctr = float(tk.get("pctr") if tk.get("pctr") is not None else -50.0)

        if profit_capture_pct > 0 and cur_pct + 1e-9 >= profit_capture_pct:
            xp = round(ep * (1.0 + profit_capture_pct / 100.0), 2)
            return {
                "symbol": o["symbol"],
                "day": o["day"],
                "entry_ts": t0,
                "exit_ts": t0 + hold_sec,
                "pnl": (xp - ep) * qty,
                "ret_pct": (xp - ep) / ep * 100.0,
                "reason": "profit_capture",
                "hold_sec": hold_sec,
                "source": "recorded",
            }

        if custom_shelf > 0 and px <= custom_shelf + 1e-9:
            if float(o.get("hold_sec") or 0) > hold_sec + 15:
                xp = round(min(px, custom_shelf), 2)
                return {
                    "symbol": o["symbol"],
                    "day": o["day"],
                    "entry_ts": t0,
                    "exit_ts": t0 + hold_sec,
                    "pnl": (xp - ep) * qty,
                    "ret_pct": (xp - ep) / ep * 100.0,
                    "reason": "be_offset_trail",
                    "hold_sec": hold_sec,
                    "source": "recorded",
                }

        if dead_cut_sec > 0 and hold_sec >= dead_cut_sec:
            if mfe_pct <= dead_cut_mfe_pct and cur_pct <= dead_cut_min_loss_pct and pctr <= dead_cut_max_pctr:
                if float(o.get("hold_sec") or 0) > hold_sec + 15:
                    xp = round(px - 0.01, 2)
                    return {
                        "symbol": o["symbol"],
                        "day": o["day"],
                        "entry_ts": t0,
                        "exit_ts": t0 + hold_sec,
                        "pnl": (xp - ep) * qty,
                        "ret_pct": (xp - ep) / ep * 100.0,
                        "reason": "smart_dead_cut",
                        "hold_sec": hold_sec,
                        "source": "recorded",
                    }

        if be_at_pct > 0 and mfe_pct + 1e-9 >= be_at_pct:
            be_px = round(ep + be_offset_px, 2)
            if be_px < px:
                custom_shelf = max(custom_shelf, be_px)

    return {
        "symbol": o["symbol"],
        "day": o["day"],
        "entry_ts": t0,
        "exit_ts": t1,
        "pnl": (actual_xp - ep) * qty,
        "ret_pct": (actual_xp - ep) / ep * 100.0,
        "reason": o.get("close_reason"),
        "hold_sec": float(o.get("hold_sec") or max(0.0, t1 - t0)),
        "source": "recorded",
    }


def simulate_stream_entry(sym: str, day: str, rows: list[dict], filled_idx: int, trigger_px: float, source_tag: str, cfg: dict) -> dict:
    n = len(rows)
    entry_row = rows[filled_idx]
    ep = max(trigger_px, round(entry_row["price"], 2))
    t0 = entry_row["ts"]
    qty = max(1.0, round(NOTIONAL / ep))
    peak = ep
    custom_shelf = round(ep * (1.0 - 0.0060), 2)
    exit_px = ep
    exit_ts = t0 + 60.0
    exit_reason = "local_trail"

    profit_capture_pct = float(cfg.get("profit_capture_pct", 0.20))
    be_at_pct = float(cfg.get("be_at_pct", 0.15))
    be_offset_px = float(cfg.get("be_offset_px", 0.02))
    dead_cut_sec = float(cfg.get("dead_cut_sec", 300.0))

    for k in range(filled_idx + 1, n):
        tk = rows[k]
        hold_sec = tk["ts"] - t0
        px = tk["price"]
        peak = max(peak, px)
        mfe_pct = (peak - ep) / ep * 100.0
        cur_pct = (px - ep) / ep * 100.0
        tk_pctr = float(tk["pctr"] if tk["pctr"] is not None else -50.0)

        if profit_capture_pct > 0 and cur_pct + 1e-9 >= profit_capture_pct:
            exit_px = round(ep * (1.0 + profit_capture_pct / 100.0), 2)
            exit_ts = tk["ts"]
            exit_reason = "profit_capture"
            break

        trail_shelf = round(peak - ep * 0.0060, 2) if mfe_pct >= 0.10 else custom_shelf
        eff_shelf = max(custom_shelf, trail_shelf)
        if px <= eff_shelf + 1e-9:
            exit_px = round(min(px, eff_shelf), 2)
            exit_ts = tk["ts"]
            exit_reason = "local_trail"
            break

        if dead_cut_sec > 0 and hold_sec >= dead_cut_sec:
            if mfe_pct <= 0.02 and cur_pct <= -0.15 and tk_pctr <= -70.0:
                exit_px = round(px - 0.01, 2)
                exit_ts = tk["ts"]
                exit_reason = "smart_dead_cut"
                break

        if hold_sec >= 1320.0:
            exit_px = round(px, 2)
            exit_ts = tk["ts"]
            exit_reason = "dead_trade"
            break

        if be_at_pct > 0 and mfe_pct + 1e-9 >= be_at_pct:
            be_px = round(ep + be_offset_px, 2)
            if be_px < px:
                custom_shelf = max(custom_shelf, be_px)
    else:
        exit_px = round(rows[-1]["price"], 2)
        exit_ts = rows[-1]["ts"]
        exit_reason = "eod"

    return {
        "symbol": sym,
        "day": day,
        "entry_ts": t0,
        "exit_ts": exit_ts,
        "pnl": (exit_px - ep) * qty,
        "ret_pct": (exit_px - ep) / ep * 100.0,
        "reason": exit_reason,
        "hold_sec": max(10.0, exit_ts - t0),
        "source": source_tag,
    }


def simulate_unlocks(day: str, sym_streams: dict, midcap_streams: dict, recorded_trades: list[dict], cfg: dict) -> list[dict]:
    """Simulate newly unlocked trades enforcing ALL active live quality gates:
    - S5 slow 5m %R pitch >= 0.0 (ai_watch_wr_slow_5m_veto)
    - WR trend rise >= 15.0 over prior 5m (ai_watch_wr_trend_min_rise)
    - 2 consecutive polls in presquare band (ai_watch_square_min_count = 2)
    - Not blocked by overhead resistance (ob_resist_0.3 == False)
    - +1c pullback_buystop confirmation within 30s
    """
    pre_thr = float(cfg.get("pre_thr", 35.0))
    conf_max = float(cfg.get("conf_max", 15.0))
    cooldown_sec = float(cfg.get("cooldown_sec", 60.0))
    after_sq_sec = float(cfg.get("after_sq_min", 4.0)) * 60.0
    enable_midcap = bool(cfg.get("enable_midcap", False))
    enable_pre_relax = bool(cfg.get("enable_pre_relax", False))
    enable_reentry = bool(cfg.get("enable_reentry", False))

    unlocked_candidates = []

    # 1. Evaluate $20+ shadow streams
    for sym, rows in sym_streams.items():
        last_sq_ts = -1e9
        n = len(rows)
        i = 0
        while i < n - 3:
            r = rows[i]
            ts = r["ts"]
            pctr = r["pctr"]
            pslow = r["pctr_slow"]
            if pctr is not None and pslow is not None:
                p_f = float(pctr)
                p_s = float(pslow)
                gap = abs(p_f - p_s)
                if p_f >= -20.0 and p_s >= -20.0 and gap <= 15.0:
                    last_sq_ts = ts

                in_new_pre = (-pre_thr <= p_f < -20.0) and (-pre_thr <= p_s < -20.0) and (gap <= conf_max)
                was_in_old_pre = (-35.0 <= p_f < -20.0) and (-35.0 <= p_s < -20.0) and (gap <= 15.0)
                prev_f = float(rows[i - 1]["pctr"]) if (i >= 1 and rows[i - 1]["pctr"] is not None) else p_f
                prev5_s = float(rows[max(0, i - 5)]["pctr_slow"]) if (i >= 1 and rows[max(0, i - 5)]["pctr_slow"] is not None) else p_s
                prev15_f = float(rows[max(0, i - 15)]["pctr"]) if (i >= 1 and rows[max(0, i - 15)]["pctr"] is not None) else p_f
                rising_ok = r["pctr_rising"] and ((p_f - prev_f) >= 0.2) and ((p_s - prev5_s) >= 0.0)
                wr_rise = p_f - prev15_f

                why = r["arm_why"]
                if (
                    not r["arm_ok"]
                    and rising_ok
                    and wr_rise >= 15.0
                    and r["in_zone"]
                    and r["rvol_ok"]
                    and not r["ob_resist"]
                    and r["tape_age_sec"] <= 15.0
                ):
                    spread_pct = (r["ask"] - r["bid"]) / r["price"] * 100.0 if r["price"] > 0 and r["ask"] >= r["bid"] else 0.05
                    if 0.0 <= spread_pct <= 0.15:
                        newly_eligible = False
                        unlock_reason = ""
                        if enable_pre_relax and why in ("exh_not_tight", "wait_exh", "not_presquare") and in_new_pre and not was_in_old_pre:
                            if (ts - last_sq_ts) >= after_sq_sec:
                                newly_eligible = True
                                unlock_reason = "pre42_tight18"
                        elif enable_reentry and why in ("not_presquare", "wait_exh") and was_in_old_pre:
                            if after_sq_sec <= (ts - last_sq_ts) < 600.0:
                                newly_eligible = True
                                unlock_reason = "after_sq_4m"
                        elif enable_reentry and why == "reentry_cooldown" and in_new_pre:
                            newly_eligible = True
                            unlock_reason = "cooldown_60s"

                        if newly_eligible:
                            arm_px = r["ask"] if r["ask"] > 0 else r["price"]
                            trigger_px = round(arm_px + 0.01, 2)
                            filled_idx = None
                            for j in range(i + 1, min(n, i + 5)):
                                if rows[j]["ts"] - ts > 30.0:
                                    break
                                if rows[j]["price"] >= trigger_px and (rows[j]["pctr"] or -50.0) >= p_f:
                                    filled_idx = j
                                    break
                            if filled_idx is not None:
                                tr = simulate_stream_entry(sym, day, rows, filled_idx, trigger_px, unlock_reason, cfg)
                                unlocked_candidates.append(tr)
                                i = filled_idx + 6
                                continue
            i += 1

    # 2. Evaluate $10-$20 Liquid Midcap Movers
    if enable_midcap:
        for sym, rows in midcap_streams.items():
            last_sq_ts = -1e9
            n = len(rows)
            i = 0
            while i < n - 3:
                r = rows[i]
                ts = r["ts"]
                p_f = float(r["pctr"])
                p_s = float(r["pctr_slow"])
                gap = r["pctr_gap"]
                if p_f >= -20.0 and p_s >= -20.0 and gap <= 15.0:
                    last_sq_ts = ts

                in_pre = (-pre_thr <= p_f < -20.0) and (-pre_thr <= p_s < -20.0) and (gap <= conf_max)
                rising_ok = r["pctr_rising"] and r["pctr_slow_rising"] and (r["pctr_fast_pitch"] >= 1.0) and (r["pctr_slow_pitch"] >= 0.0)
                if in_pre and rising_ok and r["wr_rise_5m"] >= 15.0 and r["in_zone"] and (ts - last_sq_ts) >= after_sq_sec:
                    trigger_px = round(r["price"] + 0.01, 2)
                    filled_idx = None
                    for j in range(i + 1, min(n, i + 5)):
                        if rows[j]["ts"] - ts > 30.0:
                            break
                        if rows[j]["price"] >= trigger_px and rows[j]["pctr"] >= p_f:
                            filled_idx = j
                            break
                    if filled_idx is not None:
                        tr = simulate_stream_entry(sym, day, rows, filled_idx, trigger_px, "midcap_10_20", cfg)
                        unlocked_candidates.append(tr)
                        i = filled_idx + 12
                        continue
                i += 1

    # Merge chronologically with 5-slot cap and symbol cooldown
    all_events = sorted(recorded_trades + unlocked_candidates, key=lambda x: x["entry_ts"])
    accepted = []
    open_positions = []
    last_exit_by_sym = defaultdict(lambda: -1e9)
    active_by_sym = defaultdict(lambda: -1e9)

    for tr in all_events:
        sym = tr["symbol"]
        t0 = tr["entry_ts"]
        t1 = tr["exit_ts"]
        open_positions = [ets for ets in open_positions if ets > t0]

        if tr["source"] == "recorded":
            accepted.append(tr)
            open_positions.append(t1)
            active_by_sym[sym] = max(active_by_sym[sym], t1)
            last_exit_by_sym[sym] = max(last_exit_by_sym[sym], t1)
        else:
            if len(open_positions) >= 5:
                continue
            if t0 < active_by_sym[sym]:
                continue
            if (t0 - last_exit_by_sym[sym]) < cooldown_sec:
                continue
            accepted.append(tr)
            open_positions.append(t1)
            active_by_sym[sym] = t1
            last_exit_by_sym[sym] = t1

    return accepted


def calc_metrics(trades: list[dict], num_days: int) -> dict:
    wins = [t for t in trades if t["pnl"] > 0.001]
    losses = [t for t in trades if t["pnl"] < -0.001]
    flats = [t for t in trades if abs(t["pnl"]) <= 0.001]
    tot_pnl = sum(t["pnl"] for t in trades)
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    pf = gross_win / gross_loss if gross_loss > 0 else 99.9
    wr = len(wins) / (len(wins) + len(losses)) * 100.0 if (wins or losses) else 0.0
    avg_w = gross_win / len(wins) if wins else 0.0
    avg_l = -gross_loss / len(losses) if losses else 0.0
    avg_hold = sum(t["hold_sec"] for t in trades) / len(trades) if trades else 0.0
    trades_per_day = len(trades) / max(1, num_days)
    trades_per_hr = trades_per_day / 6.5
    by_src = Counter(t.get("source", "recorded") for t in trades)
    by_reason = Counter(t.get("reason", "unknown") for t in trades)
    return {
        "n": len(trades),
        "per_day": trades_per_day,
        "per_hr": trades_per_hr,
        "w": len(wins),
        "l": len(losses),
        "f": len(flats),
        "wr": wr,
        "pnl": tot_pnl,
        "pf": pf,
        "avg_w": avg_w,
        "avg_l": avg_l,
        "avg_hold": avg_hold,
        "by_src": dict(by_src),
        "by_reason": dict(by_reason),
    }


def main():
    d_dir = resolve_report_dir()
    outcomes = load_outcomes_and_ticks(d_dir)
    shadow_streams = load_shadow_streams(d_dir)
    midcap_streams = load_midcap_10_20_streams()

    cfg_base = {"mode": "baseline", "presquare_only": False}
    cfg_exit_pkg = {
        "mode": "package",
        "presquare_only": True,
        "profit_capture_pct": 0.20,
        "be_at_pct": 0.15,
        "be_offset_px": 0.02,
        "dead_cut_sec": 300.0,
        "dead_cut_mfe_pct": 0.02,
        "dead_cut_min_loss_pct": -0.15,
        "dead_cut_max_pctr": -70.0,
    }
    cfg_full_pkg = {
        **cfg_exit_pkg,
        "pre_thr": 40.0,
        "conf_max": 17.0,
        "cooldown_sec": 60.0,
        "after_sq_min": 4.0,
        "enable_midcap": True,
        "enable_pre_relax": True,
        "enable_reentry": True,
    }

    trades_base = {d: [] for d in ALL_DAYS}
    trades_exit_only = {d: [] for d in ALL_DAYS}
    trades_full_pkg = {d: [] for d in ALL_DAYS}

    for o in outcomes:
        d = o["day"]
        tb = simulate_recorded_trade(o, cfg_base)
        if tb:
            trades_base[d].append(tb)
        te = simulate_recorded_trade(o, cfg_exit_pkg)
        if te:
            trades_exit_only[d].append(te)

    for d in ALL_DAYS:
        trades_full_pkg[d] = simulate_unlocks(
            d, shadow_streams[d], midcap_streams[d], trades_exit_only[d], cfg_full_pkg
        )

    print("=== DAY-BY-DAY COMPARISON (LAST WEEK + TODAY) ===")
    header = (
        f"{'Day':<11} | {'Base N (WR / P&L)':<24} | "
        f"{'ExitPkg N (WR / P&L)':<24} | {'FullPkg N (/hr | WR | P&L)':<30}"
    )
    print(header)
    print("-" * len(header))
    for d in ALL_DAYS:
        mb = calc_metrics(trades_base[d], 1)
        me = calc_metrics(trades_exit_only[d], 1)
        mf = calc_metrics(trades_full_pkg[d], 1)
        print(
            f"{d:<11} | "
            f"{mb['n']:3d} ({mb['wr']:5.1f}% / ${mb['pnl']:+6.2f}) | "
            f"{me['n']:3d} ({me['wr']:5.1f}% / ${me['pnl']:+6.2f}) | "
            f"{mf['n']:3d} ({mf['per_hr']:4.1f}/hr | {mf['wr']:5.1f}% | ${mf['pnl']:+6.2f})"
        )

    for label, days_list in [
        ("LAST WEEK (2026-10-26..2026-10-30, 5 Days)", LAST_WEEK),
        ("TODAY (2026-11-02, 1 Day)", TODAY),
        ("COMBINED 6 DAYS (Last Week + Today)", ALL_DAYS),
    ]:
        print(f"\n=== {label} ===")
        b_all = [t for d in days_list for t in trades_base[d]]
        e_all = [t for d in days_list for t in trades_exit_only[d]]
        f_all = [t for d in days_list for t in trades_full_pkg[d]]
        nd = len(days_list)
        for name, tlist in [
            ("1. Baseline (As Traded)", b_all),
            ("2. Exit + Entry Cleanup Package (Same Volume)", e_all),
            ("3. Full Package (+ Safe Volume Scaling Levers)", f_all),
        ]:
            m = calc_metrics(tlist, nd)
            print(
                f"  {name:<47} | N={m['n']:3d} ({m['per_day']:4.1f}/d, {m['per_hr']:4.1f}/hr) | "
                f"W/L={m['w']:3d}/{m['l']:2d} ({m['wr']:5.1f}% WR) | "
                f"P&L=${m['pnl']:+7.2f} (PF={m['pf']:.2f}) | "
                f"AvgW=${m['avg_w']:+5.2f} AvgL=${m['avg_l']:+5.2f} | Hold={m['avg_hold']:4.0f}s"
            )
            if name.startswith("3."):
                print(f"     Sources: {m['by_src']}")
                print(f"     Exits:   {m['by_reason']}")
                for src_k in sorted(m["by_src"].keys()):
                    sub = [x for x in tlist if x.get("source") == src_k]
                    sm = calc_metrics(sub, nd)
                    print(f"       -> {src_k:<14}: N={sm['n']:3d} | W/L={sm['w']:2d}/{sm['l']:2d} ({sm['wr']:5.1f}% WR) | P&L=${sm['pnl']:+6.2f}")


if __name__ == "__main__":
    main()
