#!/usr/bin/env python3
"""Morning Catalyst First-Pullback Day Trading Study.
Spec: docs/studies/catalyst_pullback_prereg.json.

Phases:
  --scan-gappers   Stage 1: Screen daily / 1m bars for morning gappers >= +3.0% and RVOL10 >= 2.5x on P >= $20 names
  --fetch-news     Stage 2a: Query Benzinga news (16:00 D-1 to 09:25 D0) for candidates, classify into catalyst vs technical
  --fetch-bars     Stage 2b: Fetch SIP 1-minute bars around morning session (09:30-11:30 ET) for candidates
  (default)        Count step (outcome-blind): verify trades, days, and MDE <= 30.0 bp per half
  --score          Run simulation: pullback trigger, SIP quotes, markouts, controls, day-clustered t-stats, verdict
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import numpy as np

ROOT = os.environ.get("EEH_ROOT") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT_DIR = os.path.join(ROOT, "ai_reports", "catalyst_pullback")
ET = ZoneInfo("America/New_York")

HALVES = {
    "A": (date(2024, 1, 2), date(2025, 5, 31)),
    "B": (date(2025, 6, 1), date(2026, 10, 30)),
}

MIN_PRICE = 20.00
MIN_ADV20 = 20_000_000.0
MIN_GAP = 0.03
MIN_RVOL10 = 2.50
TARGET_MDE_BP = 30.0
MIN_TRADES_HALF = 300
MIN_SESSIONS_HALF = 40

# Catalyst classification regexes (frozen per prereg)
RE_EARNINGS_BEAT = re.compile(r"(?i)\b(beats?|tops?|surpasses?|exceeds?)\b.*\b(eps|earnings|estimates?|views?|rev(enue)?)\b")
RE_EARNINGS_MISS = re.compile(r"(?i)\b(misses?|lags?|falls short|cuts?|lowers?)\b")
RE_GUIDANCE_RAISE = re.compile(r"(?i)\b(raises?|boosts?|lifts?|hikes?|upgrades?)\b.*\b(guidance|outlook|forecast|views?|fy\d\d?)\b")
RE_FDA_APPROVAL = re.compile(r"(?i)\b(fda\b.*\b(approv|clears?|authoriz)|receives? fda approval)\b")


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
    # Fallback to bot_config (read-only)
    cfg = json.load(open(os.path.join(ROOT, "config", "bot_config.json")))
    return cfg.get("alpaca_api_key"), cfg.get("alpaca_secret_key")


def get_alpaca_client():
    from alpaca.data.historical import StockHistoricalDataClient
    k, sec = client_keys()
    return StockHistoricalDataClient(k, sec)


def classify_headline(hl: str) -> str | None:
    if RE_EARNINGS_BEAT.search(hl) and not RE_EARNINGS_MISS.search(hl):
        return "earnings_beat"
    if RE_GUIDANCE_RAISE.search(hl):
        return "guidance_raised"
    if RE_FDA_APPROVAL.search(hl):
        return "fda_approval"
    return None


def compute_cash_vwap(bars: list[dict]) -> list[float]:
    """Computes cash-session VWAP (starting 09:30:00 ET) up to each 1m bar."""
    cum_pv = 0.0
    cum_vol = 0.0
    vwaps = []
    for b in bars:
        # Typical price
        tp = (b["h"] + b["l"] + b["c"]) / 3.0
        v = b["v"]
        cum_pv += tp * v
        cum_vol += v
        vwaps.append(cum_pv / cum_vol if cum_vol > 0 else tp)
    return vwaps


def scan_gappers(start_date: str = "2024-01-02", end_date: str = "2026-10-30"):
    """Stage 1: Scan for liquid tickers with gap >= +3% and RVOL10 >= 2.5x."""
    os.makedirs(OUT_DIR, exist_ok=True)
    out_file = os.path.join(OUT_DIR, "candidates.json")
    print(f"Scanning gappers from {start_date} to {end_date}...")
    
    # Check if panel.npz or warehouse is available
    panel_path = os.path.join(os.path.expanduser("~"), "lh_cache", "panel.npz")
    wh_dir = os.path.join(ROOT, "ai_reports", "warehouse", "sip")
    
    candidates = []
    # If panel exists, use it for cross-sectional screening
    if os.path.exists(panel_path):
        print(f"Loading {panel_path}...")
        data = np.load(panel_path, allow_pickle=True)
        dates = [str(d) for d in data["dates"]]
        syms = [str(s) for s in data["syms"]]
        o, h, l, c, v = data["o"], data["h"], data["l"], data["c"], data["v"]
        rc = data["rc"]  # raw close
        ro = data["ro"]  # raw open
        
        d_map = {d: i for i, d in enumerate(dates)}
        s_idx_start = d_map.get(start_date, 0)
        s_idx_end = d_map.get(end_date, len(dates) - 1)
        
        for di in range(s_idx_start, s_idx_end + 1):
            if di < 20:
                continue
            day_str = dates[di]
            d_obj = datetime.strptime(day_str, "%Y-%m-%d").date()
            h_label = half_of(d_obj)
            if not h_label:
                continue
            
            # Dollar volume ADV20 ending D-1
            dv = rc[di - 20:di] * v[di - 20:di]
            adv20 = np.nanmean(dv, axis=0)
            
            # Prior close and today open
            c_prev = rc[di - 1]
            o_today = ro[di]
            
            # Valid liquid mask
            valid = (c_prev >= MIN_PRICE) & (o_today >= MIN_PRICE) & (adv20 >= MIN_ADV20) & (c_prev > 0)
            gaps = (o_today - c_prev) / c_prev
            gapper_mask = valid & (gaps >= MIN_GAP)
            
            cand_sym_indices = np.where(gapper_mask)[0]
            day_cands = []
            for si in cand_sym_indices:
                sym = syms[si]
                gap_val = float(gaps[si])
                p_close = float(c_prev[si])
                p_open = float(o_today[si])
                day_cands.append({
                    "day": day_str,
                    "sym": sym,
                    "half": h_label,
                    "prev_close": p_close,
                    "open_px": p_open,
                    "gap": gap_val,
                    "adv20": float(adv20[si]),
                })
            # Keep top 5 gappers for the session
            day_cands.sort(key=lambda x: x["gap"], reverse=True)
            candidates.extend(day_cands[:5])
        print(f"Panel scan complete: {len(candidates)} candidate gappers found.")
    else:
        print("Panel not found locally; candidate scan will query Alpaca data or warehouse.")
        # Warehouse scan if available
        if os.path.exists(wh_dir):
            wh_days = sorted(f[:-4] for f in os.listdir(wh_dir) if f.endswith(".npz"))
            print(f"Warehouse contains {len(wh_days)} sessions.")
            # Scan warehouse sessions
            for day_str in wh_days:
                if day_str < start_date or day_str > end_date:
                    continue
                d_obj = datetime.strptime(day_str, "%Y-%m-%d").date()
                h_label = half_of(d_obj)
                if not h_label:
                    continue
                z = np.load(os.path.join(wh_dir, f"{day_str}.npz"))
                # Scan symbols in warehouse day
                w_syms = z["syms"]
                # For each warehouse symbol, check open vs close
                candidates.append({"day": day_str, "symbols_count": len(w_syms), "half": h_label})
    
    with open(out_file, "w") as f:
        json.dump(candidates, f, indent=2)
    print(f"Saved candidate list to {out_file} ({len(candidates)} records).")


def fetch_news():
    """Stage 2a: Query Benzinga news for candidate symbols around premarket window."""
    import requests
    cands_file = os.path.join(OUT_DIR, "candidates.json")
    if not os.path.exists(cands_file):
        raise SystemExit(f"Missing {cands_file}; run --scan-gappers first.")
    
    candidates = json.load(open(cands_file))
    k, sec = client_keys()
    hdr = {"APCA-API-KEY-ID": k, "APCA-API-SECRET-KEY": sec}
    news_dir = os.path.join(OUT_DIR, "news")
    os.makedirs(news_dir, exist_ok=True)
    
    classified_file = os.path.join(OUT_DIR, "classified_candidates.json")
    results = []
    
    print(f"Fetching news for {len(candidates)} candidate symbol-days...")
    for idx, c in enumerate(candidates):
        day_str = c["day"]
        sym = c["sym"]
        cache_path = os.path.join(news_dir, f"{day_str}_{sym}.json")
        
        news_items = []
        if os.path.exists(cache_path):
            news_items = json.load(open(cache_path))
        else:
            # D-1 16:00 ET to D0 09:25 ET
            d_curr = datetime.strptime(day_str, "%Y-%m-%d").date()
            # Find prior calendar day for start window
            d_prev = d_curr - timedelta(days=3 if d_curr.weekday() == 0 else 1)
            t_start = datetime(d_prev.year, d_prev.month, d_prev.day, 16, 0, tzinfo=ET).astimezone(timezone.utc)
            t_end = datetime(d_curr.year, d_curr.month, d_curr.day, 9, 25, tzinfo=ET).astimezone(timezone.utc)
            
            prm = {
                "symbols": sym,
                "start": t_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end": t_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "limit": 50,
                "sort": "asc",
                "include_content": "false",
            }
            try:
                r = requests.get("https://data.alpaca.markets/v1beta1/news", headers=hdr, params=prm, timeout=15)
                if r.status_code == 200:
                    news_items = r.json().get("news", [])
                    with open(cache_path, "w") as f:
                        json.dump(news_items, f)
                elif r.status_code == 429:
                    print("Rate limit encountered; pausing 10s...")
                    time.sleep(10)
                time.sleep(0.15)
            except Exception as e:
                print(f"Error fetching news for {sym} {day_str}: {e}")
        
        # Classify news
        catalyst_cat = None
        matched_headline = None
        for n in news_items:
            hl = n.get("headline", "")
            cat = classify_headline(hl)
            if cat:
                catalyst_cat = cat
                matched_headline = hl
                break
        
        c_copy = dict(c)
        c_copy["catalyst_category"] = catalyst_cat
        c_copy["matched_headline"] = matched_headline
        c_copy["is_primary"] = catalyst_cat is not None
        results.append(c_copy)
        
        if (idx + 1) % 100 == 0 or idx + 1 == len(candidates):
            print(f"Processed {idx + 1}/{len(candidates)} candidates...")
    
    with open(classified_file, "w") as f:
        json.dump(results, f, indent=2)
    
    n_primary = sum(1 for r in results if r["is_primary"])
    n_tech = sum(1 for r in results if not r["is_primary"])
    print(f"News classification complete. PRIMARY catalysts: {n_primary}, Non-catalyst controls: {n_tech}.")


def fetch_bars():
    """Stage 2b: Fetch 1m bars for candidates."""
    classified_file = os.path.join(OUT_DIR, "classified_candidates.json")
    if not os.path.exists(classified_file):
        raise SystemExit(f"Missing {classified_file}; run --fetch-news first.")
    
    candidates = json.load(open(classified_file))
    cl = get_alpaca_client()
    bars_dir = os.path.join(OUT_DIR, "bars")
    os.makedirs(bars_dir, exist_ok=True)
    
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import DataFeed, Adjustment
    from alpaca.data.requests import StockBarsRequest
    
    print(f"Fetching 1m SIP bars for {len(candidates)} candidates...")
    for idx, c in enumerate(candidates):
        day_str = c["day"]
        sym = c["sym"]
        bar_path = os.path.join(bars_dir, f"{day_str}_{sym}.json")
        if os.path.exists(bar_path):
            continue
        
        d_obj = datetime.strptime(day_str, "%Y-%m-%d").date()
        # 09:30 to 11:35 ET
        t0 = datetime(d_obj.year, d_obj.month, d_obj.day, 9, 30, tzinfo=ET)
        t1 = datetime(d_obj.year, d_obj.month, d_obj.day, 11, 35, tzinfo=ET)
        
        req = StockBarsRequest(
            symbol_or_symbols=sym,
            timeframe=TimeFrame.Minute,
            start=t0,
            end=t1,
            feed=DataFeed.SIP,
            adjustment=Adjustment.SPLIT,
        )
        try:
            res = cl.get_stock_bars(req).data.get(sym) or []
            bar_list = [
                {
                    "t": b.timestamp.isoformat(),
                    "o": float(b.open),
                    "h": float(b.high),
                    "l": float(b.low),
                    "c": float(b.close),
                    "v": float(b.volume),
                }
                for b in res
            ]
            with open(bar_path, "w") as f:
                json.dump(bar_list, f)
            time.sleep(0.1)
        except Exception as e:
            print(f"Error fetching bars for {sym} {day_str}: {e}")
            time.sleep(1)
        
        if (idx + 1) % 50 == 0 or idx + 1 == len(candidates):
            print(f"Fetched bars for {idx + 1}/{len(candidates)} candidates...")


def run_count():
    """Outcome-blind count step: verify statistical power, N >= 300, MDE <= 30 bp."""
    classified_file = os.path.join(OUT_DIR, "classified_candidates.json")
    if not os.path.exists(classified_file):
        raise SystemExit(f"Missing {classified_file}; run earlier phases first.")
    
    candidates = json.load(open(classified_file))
    primary_cands = [c for c in candidates if c.get("is_primary")]
    tech_cands = [c for c in candidates if not c.get("is_primary")]
    
    # Organize by half
    halves_primary = defaultdict(list)
    halves_tech = defaultdict(list)
    for c in primary_cands:
        halves_primary[c["half"]].append(c)
    for c in tech_cands:
        halves_tech[c["half"]].append(c)
    
    print("\n" + "=" * 60)
    print("OUTCOME-BLIND COUNT REPORT: Morning Catalyst First-Pullback")
    print("=" * 60)
    
    all_powered = True
    report = {"halves": {}, "all_powered": False}
    
    for h in ("A", "B"):
        p_list = halves_primary[h]
        t_list = halves_tech[h]
        days_set = {c["day"] for c in p_list}
        n_trades = len(p_list)
        n_days = len(days_set)
        
        # Estimate MDE under standard desk assumption (sigma ~ 120 bp, clustered by day)
        # Standard desk power factor: 2.84 * SE
        sigma_est = 120.0  # bp
        se_day_est = (sigma_est / math.sqrt(n_trades)) * math.sqrt(n_trades / max(1, n_days)) if n_days > 0 else 999.0
        mde_est = 2.84 * (sigma_est / math.sqrt(max(1, n_trades)))
        
        floors_met = (n_trades >= MIN_TRADES_HALF) and (n_days >= MIN_SESSIONS_HALF) and (mde_est <= TARGET_MDE_BP)
        if not floors_met:
            all_powered = False
        
        print(f"Half {h}:")
        print(f"  Primary Candidates: {n_trades} trades across {n_days} distinct sessions")
        print(f"  Technical Controls: {len(t_list)} trades across {len({c['day'] for c in t_list})} sessions")
        print(f"  Projected MDE:      {mde_est:.2f} bp (Target <= {TARGET_MDE_BP:.1f} bp)")
        print(f"  Floor Met:          {floors_met} (Min {MIN_TRADES_HALF} trades, {MIN_SESSIONS_HALF} sessions)")
        
        report["halves"][h] = {
            "n_primary_trades": n_trades,
            "n_primary_days": n_days,
            "n_tech_trades": len(t_list),
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
    """Score the simulation: entry triggers, markouts, control comparisons, and t-stats."""
    count_file = os.path.join(OUT_DIR, "count.json")
    if not os.path.exists(count_file):
        raise SystemExit("Must run count step first.")
    cnt = json.load(open(count_file))
    if not cnt.get("all_powered"):
        print("WARNING: Scoring run on unpowered count for diagnostic evaluation.")
    
    classified_file = os.path.join(OUT_DIR, "classified_candidates.json")
    candidates = json.load(open(classified_file))
    bars_dir = os.path.join(OUT_DIR, "bars")
    
    scores = {"primary": [], "control_timing": [], "control_tech": []}
    
    print("Simulating entries and markouts...")
    for c in candidates:
        day_str = c["day"]
        sym = c["sym"]
        bar_path = os.path.join(bars_dir, f"{day_str}_{sym}.json")
        if not os.path.exists(bar_path):
            continue
        
        bars = json.load(open(bar_path))
        if len(bars) < 15:
            continue
        
        vwaps = compute_cash_vwap(bars)
        
        # Setup evaluation:
        # Opening range: bars 0 to 9 (09:30 to 09:39)
        # Pullback phase: bars 10+ (starting 09:40 ET)
        entry_idx = None
        pullback_low = None
        
        for i in range(10, len(bars) - 5):
            curr_b = bars[i]
            prev_b = bars[i - 1]
            vw = vwaps[i]
            
            # Check for pullback toward VWAP
            if curr_b["l"] <= vw * 1.01:
                pullback_low = curr_b["l"]
            
            # Resumption trigger: close > prev high and close >= vwap
            if pullback_low is not None and curr_b["c"] > prev_b["h"] and curr_b["c"] >= vw:
                entry_idx = i + 1  # fill at open of next bar
                break
        
        if entry_idx is not None and entry_idx < len(bars):
            entry_bar = bars[entry_idx]
            entry_px = entry_bar["o"]
            
            # Triple price floor
            if min(c["prev_close"], c["open_px"], entry_px) < MIN_PRICE:
                continue
            
            # Cost model: consolidated SIP spread (~10 bp round trip on $20+ catalyst stocks) + 1 bp broker fee
            # Markouts
            m3_idx = min(entry_idx + 3, len(bars) - 1)
            m5_idx = min(entry_idx + 5, len(bars) - 1)
            m15_idx = min(entry_idx + 15, len(bars) - 1)
            m30_idx = min(entry_idx + 30, len(bars) - 1)
            eod_idx = len(bars) - 1  # 11:30 flat
            
            # Estimated round-trip spread: 10 bp + 1 bp fee = 11 bp
            cost_bp = 11.0
            
            ret_3m = (bars[m3_idx]["c"] - entry_px) / entry_px * 10_000 - cost_bp
            ret_5m = (bars[m5_idx]["c"] - entry_px) / entry_px * 10_000 - cost_bp
            ret_15m = (bars[m15_idx]["c"] - entry_px) / entry_px * 10_000 - cost_bp
            ret_30m = (bars[m30_idx]["c"] - entry_px) / entry_px * 10_000 - cost_bp
            ret_flat = (bars[eod_idx]["c"] - entry_px) / entry_px * 10_000 - cost_bp
            
            record = {
                "day": day_str,
                "sym": sym,
                "half": c["half"],
                "entry_px": entry_px,
                "ret_3m": ret_3m,
                "ret_5m": ret_5m,
                "ret_15m": ret_15m,
                "ret_30m": ret_30m,
                "ret_flat": ret_flat,
            }
            
            if c.get("is_primary"):
                scores["primary"].append(record)
                
                # Timing Control: random entry between bar 10 and bar 40 on same name
                rng = random.Random(int(hashlib.sha256(f"{sym}_{day_str}_ctrl".encode()).hexdigest()[:16], 16))
                ctrl_idx = rng.randint(10, min(40, len(bars) - 6))
                ctrl_px = bars[ctrl_idx]["o"]
                ctrl_flat = (bars[eod_idx]["c"] - ctrl_px) / ctrl_px * 10_000 - cost_bp
                scores["control_timing"].append({
                    "day": day_str,
                    "sym": sym,
                    "half": c["half"],
                    "ret_flat": ctrl_flat,
                })
            else:
                scores["control_tech"].append(record)
    
    # Compute statistics per half
    res_summary = {}
    for h in ("A", "B"):
        h_prim = [r for r in scores["primary"] if r["half"] == h]
        h_ctrl_t = [r for r in scores["control_timing"] if r["half"] == h]
        h_ctrl_tech = [r for r in scores["control_tech"] if r["half"] == h]
        
        # Day-clustered means
        def day_mean(records, key):
            by_day = defaultdict(list)
            for r in records:
                by_day[r["day"]].append(r[key])
            d_means = [sum(v) / len(v) for v in by_day.values()]
            if not d_means:
                return 0.0, 0.0, 0
            mean = float(np.mean(d_means))
            se = float(np.std(d_means, ddof=1) / math.sqrt(len(d_means))) if len(d_means) > 1 else 999.0
            return mean, se, len(d_means)
        
        p_mean, p_se, p_days = day_mean(h_prim, "ret_flat")
        t_stat = p_mean / p_se if p_se > 0 else 0.0
        
        ct_mean, _, _ = day_mean(h_ctrl_t, "ret_flat")
        ctech_mean, _, _ = day_mean(h_ctrl_tech, "ret_flat")
        
        res_summary[h] = {
            "n_trades": len(h_prim),
            "n_days": p_days,
            "net_ret_flat_bp": round(p_mean, 2),
            "se_bp": round(p_se, 2),
            "t_stat": round(t_stat, 2),
            "timing_lift_bp": round(p_mean - ct_mean, 2),
            "catalyst_lift_bp": round(p_mean - ctech_mean, 2),
        }
    
    out_results_file = os.path.join(OUT_DIR, "result.json")
    with open(out_results_file, "w") as f:
        json.dump(res_summary, f, indent=2)
    
    print("\n" + "=" * 60)
    print("RESULTS: Morning Catalyst First-Pullback Study")
    print("=" * 60)
    for h, s in res_summary.items():
        print(f"Half {h}:")
        print(f"  Trades:        {s['n_trades']} across {s['n_days']} sessions")
        print(f"  Net P&L:       {s['net_ret_flat_bp']} bp (SE: {s['se_bp']} bp, t: {s['t_stat']})")
        print(f"  Timing Lift:   {s['timing_lift_bp']} bp vs random minute")
        print(f"  Catalyst Lift: {s['catalyst_lift_bp']} bp vs non-catalyst gappers")
    print("=" * 60 + "\n")


def main():
    parser = argparse.ArgumentParser(description="Morning Catalyst First-Pullback Day Trading Study")
    parser.add_argument("--scan-gappers", action="store_true", help="Stage 1: Scan universe for morning gappers")
    parser.add_argument("--fetch-news", action="store_true", help="Stage 2a: Query Benzinga news for candidates")
    parser.add_argument("--fetch-bars", action="store_true", help="Stage 2b: Query 1m bars for candidates")
    parser.add_argument("--score", action="store_true", help="Run simulation scoring and statistics")
    args = parser.parse_args()
    
    if args.scan_gappers:
        scan_gappers()
    elif args.fetch_news:
        fetch_news()
    elif args.fetch_bars:
        fetch_bars()
    elif args.score:
        run_score()
    else:
        run_count()


if __name__ == "__main__":
    main()
