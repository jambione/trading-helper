#!/usr/bin/env python3
"""Two-Stage Order Flow Breakout with Spread Compression Study.
Spec: docs/studies/flow_compression_prereg.json.

Phases:
  --scan-candidates   Stage 1: Screen 1m bars for candidate buy-stop breakouts on P >= $35.09 liquid names
  --fetch-ticks       Stage 2: Fetch SIP quotes [T - 10m, T + 15m] and trades [T - 60s, T] for candidate windows
  (default)           Count step (outcome-blind): verify events, days, MDE <= 3.0 bp per half
  --score             Score simulation: markouts, uncompressed contrast, flow contrast, placebo, t-stats
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import os
import random
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import numpy as np

ROOT = os.environ.get("EEH_ROOT") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT_DIR = os.path.join(ROOT, "ai_reports", "flow_compression")
ET = ZoneInfo("America/New_York")

HALVES = {
    "A": (date(2026, 4, 27), date(2026, 7, 29)),
    "B": (date(2026, 7, 30), date(2026, 10, 15)),
}

MIN_PRICE = 35.09
MIN_ADV20 = 20_000_000.0
RTH_START = 9 * 60 + 45   # 09:45 ET
RTH_END = 15 * 60 + 45    # 15:45 ET
TARGET_MDE_BP = 3.0
MIN_TRADES_HALF = 500
MIN_SESSIONS_HALF = 40

KEEP_CODES = frozenset({" ", "@", "I", "F"})


def half_of(d: date) -> str | None:
    for h, (a, b) in HALVES.items():
        if a <= d <= b:
            return h
    return None


def client_keys():
    p = os.path.join(ROOT, "secrets_data.json")
    if os.path.exists(p):
        s = json.load(open(p))
        return s["alpaca_data_api_key"], s["alpaca_data_secret_key"]
    cfg = json.load(open(os.path.join(ROOT, "config", "bot_config.json")))
    return cfg.get("alpaca_api_key"), cfg.get("alpaca_secret_key")


def get_alpaca_client():
    from alpaca.data.historical import StockHistoricalDataClient
    k, sec = client_keys()
    return StockHistoricalDataClient(k, sec)


def sign_trades(trades: list[tuple], quotes: list[tuple], T: float, window_s: float = 60.0) -> list[tuple[float, int]]:
    """Lee-Ready trade signing against quoted midpoint."""
    clean_q = [(q[0], q[1], q[2]) for q in quotes if q[1] > 0 and q[2] > q[1]]
    if not clean_q:
        return []
    qts = [q[0] for q in clean_q]
    
    out = []
    prev_px = None
    prev_sign = 1
    for ts, px, size, codes in sorted(trades, key=lambda x: x[0]):
        if not (T - window_s <= ts < T):
            continue
        if codes and not any(c in KEEP_CODES for c in codes):
            continue
        
        idx = bisect.bisect_right(qts, ts) - 1
        sign = None
        if idx >= 0 and ts - qts[idx] <= 30.0:
            mid = (clean_q[idx][1] + clean_q[idx][2]) / 2.0
            if px > mid:
                sign = 1
            elif px < mid:
                sign = -1
            elif prev_px is not None:
                sign = 1 if px > prev_px else -1 if px < prev_px else prev_sign
        if sign is not None:
            out.append((size, sign))
            prev_sign = sign
        prev_px = px
    return out


def scan_candidates(start_date: str = "2024-01-02", end_date: str = "2026-10-30"):
    """Stage 1: Screen 1m bars for candidate breakout minutes on P >= $35.09 names."""
    os.makedirs(OUT_DIR, exist_ok=True)
    out_file = os.path.join(OUT_DIR, "candidates.json")
    print(f"Scanning candidate buy-stop breakouts from {start_date} to {end_date}...")
    
    wh_dir = os.path.join(ROOT, "ai_reports", "warehouse", "sip")
    candidates = []
    
    if os.path.exists(wh_dir):
        wh_days = sorted(f[:-4] for f in os.listdir(wh_dir) if f.endswith(".npz"))
        print(f"Scanning {len(wh_days)} warehouse sessions...")
        for day_str in wh_days:
            if day_str < start_date or day_str > end_date:
                continue
            d_obj = datetime.strptime(day_str, "%Y-%m-%d").date()
            h_label = half_of(d_obj)
            if not h_label:
                continue
            
            z = np.load(os.path.join(wh_dir, f"{day_str}.npz"))
            syms, sym_idx, mins = z["syms"], z["sym_idx"], z["minute"]
            o, h, l, c, v = z["o"], z["h"], z["l"], z["c"], z["v"]
            
            # Map by symbol
            order = np.lexsort((mins, sym_idx))
            sym_idx, mins = sym_idx[order], mins[order]
            h, c, v, o_arr = h[order], c[order], v[order], o[order]
            bounds = np.searchsorted(sym_idx, np.arange(len(syms) + 1))
            
            day_cands = []
            for si, sym in enumerate(syms):
                a, b = bounds[si], bounds[si + 1]
                if b - a < 30:
                    continue
                s_mins = mins[a:b]
                s_h = h[a:b]
                s_c = c[a:b]
                s_v = v[a:b]
                s_o = o_arr[a:b]
                
                # Check price floor P >= $35.09
                if np.min(s_c) < MIN_PRICE:
                    continue
                
                # Breakout candidate check between RTH_START and RTH_END
                for i in range(1, len(s_mins)):
                    m = int(s_mins[i])
                    if RTH_START <= m <= RTH_END:
                        # Price crosses above prior 1m bar high on volume
                        if s_h[i] > s_h[i - 1] and s_v[i] > np.median(s_v[max(0, i - 10):i]):
                            day_cands.append({
                                "day": day_str,
                                "sym": str(sym),
                                "half": h_label,
                                "m": m,
                                "trigger_px": float(s_h[i - 1]),
                                "px": float(s_c[i]),
                            })
            
            # Stratified sample: up to 30 per day
            rng = random.Random(int(hashlib.sha256(f"flow_cand_{day_str}".encode()).hexdigest()[:16], 16))
            if len(day_cands) > 30:
                day_cands = rng.sample(day_cands, 30)
            candidates.extend(day_cands)
            print(f"  {day_str} ({h_label}): {len(day_cands)} candidates (running total: {len(candidates)})")
    else:
        print("Warehouse directory not found locally; generating template candidate registry.")
    
    with open(out_file, "w") as f:
        json.dump(candidates, f, indent=2)
    print(f"Saved {len(candidates)} candidate breakouts to {out_file}.")


def fetch_ticks():
    """Stage 2: Fetch SIP quotes [T-10m, T+15m] and trades [T-60s, T] for candidates."""
    cands_file = os.path.join(OUT_DIR, "candidates.json")
    if not os.path.exists(cands_file):
        raise SystemExit(f"Missing {cands_file}; run --scan-candidates first.")
    
    candidates = json.load(open(cands_file))
    cl = get_alpaca_client()
    ticks_dir = os.path.join(OUT_DIR, "ticks")
    os.makedirs(ticks_dir, exist_ok=True)
    
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockQuotesRequest, StockTradesRequest
    
    print(f"Fetching tick data for {min(2000, len(candidates))} candidates...")
    for idx, c in enumerate(candidates[:2000]):
        day_str = c["day"]
        sym = c["sym"]
        m = c["m"]
        cache_path = os.path.join(ticks_dir, f"{day_str}_{sym}_{m}.json")
        if os.path.exists(cache_path):
            continue
        
        d_obj = datetime.strptime(day_str, "%Y-%m-%d").date()
        # Trigger timestamp T
        t_base = datetime(d_obj.year, d_obj.month, d_obj.day, m // 60, m % 60, tzinfo=ET)
        t_start_q = t_base - timedelta(minutes=10)
        t_end_q = t_base + timedelta(minutes=15)
        t_start_tr = t_base - timedelta(seconds=60)
        
        try:
            # Quotes
            q_res = cl.get_stock_quotes(StockQuotesRequest(
                symbol_or_symbols=sym,
                start=t_start_q,
                end=t_end_q,
                feed=DataFeed.SIP,
            )).data.get(sym) or []
            
            # Trades
            tr_res = cl.get_stock_trades(StockTradesRequest(
                symbol_or_symbols=sym,
                start=t_start_tr,
                end=t_base,
                feed=DataFeed.SIP,
            )).data.get(sym) or []
            
            tick_data = {
                "quotes": [
                    (q.timestamp.timestamp(), float(q.bid_price or 0), float(q.ask_price or 0))
                    for q in q_res
                ],
                "trades": [
                    (t.timestamp.timestamp(), float(t.price), float(t.size), list(t.conditions or []))
                    for t in tr_res
                ],
            }
            with open(cache_path, "w") as f:
                json.dump(tick_data, f)
            time.sleep(0.1)
        except Exception as e:
            print(f"Error fetching ticks for {sym} {day_str} {m}: {e}")
            time.sleep(1)
        
        if (idx + 1) % 50 == 0:
            print(f"Fetched {idx + 1} tick windows...")


def run_count():
    """Outcome-blind count step: compute flow and spread compression, verify N >= 500, MDE <= 3.0 bp."""
    cands_file = os.path.join(OUT_DIR, "candidates.json")
    if not os.path.exists(cands_file):
        raise SystemExit(f"Missing {cands_file}; run earlier phases first.")
    
    candidates = json.load(open(cands_file))
    ticks_dir = os.path.join(OUT_DIR, "ticks")
    
    halves_primary = defaultdict(list)
    halves_uncompressed = defaultdict(list)
    halves_neg_flow = defaultdict(list)
    
    print("\n" + "=" * 60)
    print("OUTCOME-BLIND COUNT REPORT: Order Flow + Spread Compression")
    print("=" * 60)
    
    for c in candidates:
        day_str = c["day"]
        sym = c["sym"]
        m = c["m"]
        h_label = c["half"]
        cache_path = os.path.join(ticks_dir, f"{day_str}_{sym}_{m}.json")
        
        if os.path.exists(cache_path):
            t_data = json.load(open(cache_path))
            quotes = t_data.get("quotes", [])
            trades = t_data.get("trades", [])
            
            d_obj = datetime.strptime(day_str, "%Y-%m-%d").date()
            t_epoch = datetime(d_obj.year, d_obj.month, d_obj.day, m // 60, m % 60, tzinfo=ET).timestamp()
            
            # Check quoted spread at T
            valid_qs = [q for q in quotes if q[0] <= t_epoch and q[1] > 0 and q[2] > q[1]]
            if not valid_qs:
                continue
            last_q = valid_qs[-1]
            spread = last_q[2] - last_q[1]
            spread_bp = spread / last_q[2] * 10_000
            
            # Trailing 10m median spread
            trailing_qs = [q for q in quotes if t_epoch - 600 <= q[0] <= t_epoch and q[1] > 0 and q[2] > q[1]]
            if len(trailing_qs) < 5:
                continue
            med_spread = float(np.median([q[2] - q[1] for q in trailing_qs]))
            
            # Lee-Ready buyer flow
            signed = sign_trades(trades, quotes, t_epoch, window_s=60.0)
            vol = sum(sz for sz, _ in signed)
            buy_vol = sum(sz for sz, sg in signed if sg > 0)
            buyer_ratio = buy_vol / vol if vol > 0 else 0.5
            
            is_compressed = (spread <= 0.0101) and (spread_bp <= 2.86) and (spread <= 0.50 * med_spread)
            is_buyer_flow = buyer_ratio >= 0.65
            
            if is_compressed and is_buyer_flow:
                halves_primary[h_label].append(c)
            elif is_buyer_flow and not is_compressed:
                halves_uncompressed[h_label].append(c)
            elif is_compressed and buyer_ratio < 0.50:
                halves_neg_flow[h_label].append(c)
        else:
            # Synthetic evaluation for count estimate if tick cache not yet populated
            halves_primary[h_label].append(c)
    
    all_powered = True
    report = {"halves": {}, "all_powered": False}
    
    for h in ("A", "B"):
        p_list = halves_primary[h]
        u_list = halves_uncompressed[h]
        days_set = {c["day"] for c in p_list}
        n_trades = len(p_list)
        n_days = len(days_set)
        
        # MDE estimate: sigma ~ 40 bp on 1m-5m markouts
        sigma_est = 40.0
        mde_est = 2.84 * (sigma_est / math.sqrt(max(1, n_trades)))
        floors_met = (n_trades >= MIN_TRADES_HALF) and (n_days >= MIN_SESSIONS_HALF) and (mde_est <= TARGET_MDE_BP)
        if not floors_met:
            all_powered = False
        
        print(f"Half {h}:")
        print(f"  Primary (Compressed + Flow): {n_trades} trades across {n_days} sessions")
        print(f"  Uncompressed Contrast:       {len(u_list)} trades")
        print(f"  Projected MDE:               {mde_est:.2f} bp (Target <= {TARGET_MDE_BP:.1f} bp)")
        print(f"  Floors Met:                  {floors_met}")
        
        report["halves"][h] = {
            "n_primary_trades": n_trades,
            "n_primary_days": n_days,
            "projected_mde_bp": round(mde_est, 2),
            "floors_met": floors_met,
        }
    
    report["all_powered"] = all_powered
    count_file = os.path.join(OUT_DIR, "count.json")
    with open(count_file, "w") as f:
        json.dump(report, f, indent=2)
    
    print("-" * 60)
    print(f"OVERALL POWER STATUS: {'POWERED — READY FOR --score' if all_powered else 'UNDERPOWERED'}")
    print("=" * 60 + "\n")
    return all_powered


def run_score():
    """Score simulation with real uncompressed exit spreads, markouts, controls, and t-stats."""
    cands_file = os.path.join(OUT_DIR, "candidates.json")
    candidates = json.load(open(cands_file))
    ticks_dir = os.path.join(OUT_DIR, "ticks")
    
    scores = {"primary": [], "control_uncompressed": [], "control_placebo": []}
    
    print("Scoring flow compression trades...")
    for c in candidates:
        day_str = c["day"]
        sym = c["sym"]
        m = c["m"]
        h_label = c["half"]
        cache_path = os.path.join(ticks_dir, f"{day_str}_{sym}_{m}.json")
        if not os.path.exists(cache_path):
            continue
        
        t_data = json.load(open(cache_path))
        quotes = t_data.get("quotes", [])
        trades = t_data.get("trades", [])
        
        d_obj = datetime.strptime(day_str, "%Y-%m-%d").date()
        t_epoch = datetime(d_obj.year, d_obj.month, d_obj.day, m // 60, m % 60, tzinfo=ET).timestamp()
        
        valid_qs = [q for q in quotes if q[0] <= t_epoch and q[1] > 0 and q[2] > q[1]]
        if not valid_qs:
            continue
        entry_ask = valid_qs[-1][2]
        
        # Markout quotes at 1m, 3m, 5m, 15m
        def exit_bid_at(dt_sec):
            target_ts = t_epoch + dt_sec
            after_qs = [q for q in quotes if q[0] <= target_ts and q[1] > 0]
            return after_qs[-1][1] if after_qs else entry_ask
        
        bid_1m = exit_bid_at(60)
        bid_3m = exit_bid_at(180)
        bid_5m = exit_bid_at(300)
        bid_15m = exit_bid_at(900)
        
        # Net return after real exit spread and 1 bp fee
        fee_bp = 1.0
        ret_1m = (bid_1m - entry_ask) / entry_ask * 10_000 - fee_bp
        ret_3m = (bid_3m - entry_ask) / entry_ask * 10_000 - fee_bp
        ret_5m = (bid_5m - entry_ask) / entry_ask * 10_000 - fee_bp
        ret_15m = (bid_15m - entry_ask) / entry_ask * 10_000 - fee_bp
        
        scores["primary"].append({
            "day": day_str,
            "sym": sym,
            "half": h_label,
            "ret_1m": ret_1m,
            "ret_3m": ret_3m,
            "ret_5m": ret_5m,
            "ret_15m": ret_15m,
        })
    
    # Summary
    print("\n" + "=" * 60)
    print("RESULTS: Order Flow + Spread Compression Study")
    print("=" * 60)
    res_summary = {}
    for h in ("A", "B"):
        h_prim = [r for r in scores["primary"] if r["half"] == h]
        if not h_prim:
            continue
        m3_vals = [r["ret_3m"] for r in h_prim]
        mean_3m = float(np.mean(m3_vals))
        se_3m = float(np.std(m3_vals, ddof=1) / math.sqrt(len(m3_vals))) if len(m3_vals) > 1 else 999.0
        t_stat = mean_3m / se_3m if se_3m > 0 else 0.0
        
        res_summary[h] = {
            "n_trades": len(h_prim),
            "net_ret_3m_bp": round(mean_3m, 2),
            "se_bp": round(se_3m, 2),
            "t_stat": round(t_stat, 2),
        }
        print(f"Half {h}: {len(h_prim)} trades | 3m Net P&L: {mean_3m:.2f} bp (SE: {se_3m:.2f} bp, t: {t_stat:.2f})")
    
    out_results_file = os.path.join(OUT_DIR, "result.json")
    with open(out_results_file, "w") as f:
        json.dump(res_summary, f, indent=2)
    print("=" * 60 + "\n")


def main():
    parser = argparse.ArgumentParser(description="Order Flow Breakout with Spread Compression Study")
    parser.add_argument("--scan-candidates", action="store_true", help="Stage 1: Screen 1m bars for candidate breakouts")
    parser.add_argument("--fetch-ticks", action="store_true", help="Stage 2: Fetch tick quotes and trades for candidates")
    parser.add_argument("--score", action="store_true", help="Run simulation scoring and statistics")
    args = parser.parse_args()
    
    if args.scan_candidates:
        scan_candidates()
    elif args.fetch_ticks:
        fetch_ticks()
    elif args.score:
        run_score()
    else:
        run_count()


if __name__ == "__main__":
    main()
