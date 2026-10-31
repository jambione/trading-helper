#!/usr/bin/env python3
"""entry_slip_diagnostic.py — Diagnostic analysis of entry slippage (Row 63).

Analyzes the 626 primary trades (2026-09-11..10-26) and 698 total trades (through 10-29).
Deconstructs slippage against decision price (features.ask), dispatch latency, quoted spread,
price levels, and time of day.
"""
from __future__ import annotations

import datetime as _dt
import json
import math
import os
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tools" / "studies"), str(ROOT / "tools"), str(ROOT)]
import marketable_limit_hist as M

ET = ZoneInfo("America/New_York")


def quantiles(vals: list[float], qs=(0.10, 0.25, 0.50, 0.75, 0.90)) -> dict[str, float]:
    if not vals:
        return {}
    s = sorted(vals)
    n = len(s)
    out = {}
    for q in qs:
        idx = (n - 1) * q
        lo = math.floor(idx)
        hi = math.ceil(idx)
        val = s[lo] if lo == hi else s[lo] + (s[hi] - s[lo]) * (idx - lo)
        out[f"p{int(q*100)}"] = round(val, 3)
    return out


def summary_stats(vals: list[float]) -> dict[str, float]:
    if not vals:
        return {"n": 0}
    return {
        "n": len(vals),
        "mean": round(statistics.fmean(vals), 3),
        "std": round(statistics.stdev(vals), 3) if len(vals) > 1 else 0.0,
        "min": round(min(vals), 3),
        **quantiles(vals),
        "max": round(max(vals), 3),
    }


def analyze_trades(trades: list[dict], by_raw: dict) -> dict:
    slips = [t["slip"] for t in trades]
    latencies = [t["latency"] for t in trades]
    nets = [t["net"] for t in trades]
    selections = [t["selection"] for t in trades]
    prices = [t["buy"] for t in trades]
    qtys = [t["qty"] for t in trades]
    notionals = [t["buy"] * t["qty"] for t in trades]

    # Quoted spread proxy: (ref - bid) / ref * 1e4
    spreads_bp = [t["spread_bp"] for t in trades if t["spread_bp"] is not None]
    spreads_cents = [t["spread_cents"] for t in trades if t["spread_cents"] is not None]

    # Slip in cents: buy - ref
    slips_cents = [t["buy"] - t["ref"] for t in trades]

    # Slip buckets
    slip_buckets = {
        "negative (fill < ref)": [t for t in trades if t["slip"] < 0],
        "zero (0 <= slip <= 1 bp)": [t for t in trades if 0 <= t["slip"] <= 1.0],
        "mild (1 < slip <= 5 bp)": [t for t in trades if 1.0 < t["slip"] <= 5.0],
        "blocked (5 < slip <= 10 bp)": [t for t in trades if 5.0 < t["slip"] <= 10.0],
        "large (10 < slip <= 20 bp)": [t for t in trades if 10.0 < t["slip"] <= 20.0],
        "severe (> 20 bp)": [t for t in trades if t["slip"] > 20.0],
    }

    bucket_summary = {}
    for name, group in slip_buckets.items():
        bucket_summary[name] = {
            "n": len(group),
            "share_pct": round(len(group) / len(trades) * 100, 1),
            "mean_slip_bp": round(statistics.fmean([t["slip"] for t in group]), 2) if group else 0.0,
            "mean_slip_cents": round(statistics.fmean([t["buy"] - t["ref"] for t in group]), 4) if group else 0.0,
            "mean_net_bp": round(statistics.fmean([t["net"] for t in group]), 2) if group else 0.0,
            "mean_selection_bp": round(statistics.fmean([t["selection"] for t in group]), 2) if group else 0.0,
            "mean_latency_s": round(statistics.fmean([t["latency"] for t in group]), 3) if group else 0.0,
            "mean_spread_bp": round(statistics.fmean([t["spread_bp"] for t in group if t["spread_bp"] is not None]), 2) if any(t["spread_bp"] is not None for t in group) else None,
        }

    # Latency brackets
    latency_brackets = {
        "< 0.5s": [t for t in trades if t["latency"] < 0.5],
        "0.5s - 1.0s": [t for t in trades if 0.5 <= t["latency"] < 1.0],
        "1.0s - 2.0s": [t for t in trades if 1.0 <= t["latency"] < 2.0],
        "2.0s - 5.0s": [t for t in trades if 2.0 <= t["latency"] < 5.0],
        ">= 5.0s": [t for t in trades if t["latency"] >= 5.0],
    }
    latency_summary = {}
    for name, group in latency_brackets.items():
        latency_summary[name] = {
            "n": len(group),
            "share_pct": round(len(group) / len(trades) * 100, 1),
            "blocked_n": sum(t["blocked"] for t in group),
            "blocked_rate_pct": round(sum(t["blocked"] for t in group) / max(1, len(group)) * 100, 1),
            "mean_slip_bp": round(statistics.fmean([t["slip"] for t in group]), 2) if group else 0.0,
            "median_slip_bp": round(statistics.median([t["slip"] for t in group]), 2) if group else 0.0,
            "mean_net_bp": round(statistics.fmean([t["net"] for t in group]), 2) if group else 0.0,
        }

    # Time of day (ET hour)
    by_hour = defaultdict(list)
    for t in trades:
        dt = _dt.datetime.fromtimestamp(t["entry_time"], ET)
        hr_bin = f"{dt.hour:02d}:{dt.minute // 30 * 30:02d}"
        by_hour[hr_bin].append(t)
    tod_summary = {}
    for hr_bin in sorted(by_hour):
        group = by_hour[hr_bin]
        tod_summary[hr_bin] = {
            "n": len(group),
            "blocked_n": sum(t["blocked"] for t in group),
            "blocked_pct": round(sum(t["blocked"] for t in group) / len(group) * 100, 1),
            "mean_slip_bp": round(statistics.fmean([t["slip"] for t in group]), 2),
            "mean_spread_bp": round(statistics.fmean([t["spread_bp"] for t in group if t["spread_bp"] is not None]), 2) if any(t["spread_bp"] is not None for t in group) else None,
        }

    # Price tiers
    price_brackets = {
        "$20 - $30": [t for t in trades if 20 <= t["buy"] < 30],
        "$30 - $50": [t for t in trades if 30 <= t["buy"] < 50],
        "$50 - $100": [t for t in trades if 50 <= t["buy"] < 100],
        ">= $100": [t for t in trades if t["buy"] >= 100],
    }
    price_summary = {}
    for name, group in price_brackets.items():
        price_summary[name] = {
            "n": len(group),
            "blocked_n": sum(t["blocked"] for t in group),
            "blocked_pct": round(sum(t["blocked"] for t in group) / max(1, len(group)) * 100, 1),
            "mean_slip_bp": round(statistics.fmean([t["slip"] for t in group]), 2) if group else 0.0,
            "mean_slip_cents": round(statistics.fmean([t["buy"] - t["ref"] for t in group]), 4) if group else 0.0,
            "mean_spread_bp": round(statistics.fmean([t["spread_bp"] for t in group if t["spread_bp"] is not None]), 2) if any(t["spread_bp"] is not None for t in group) else None,
            "mean_spread_cents": round(statistics.fmean([t["spread_cents"] for t in group if t["spread_cents"] is not None]), 4) if any(t["spread_cents"] is not None for t in group) else None,
        }

    # Blocked vs Unblocked breakdown
    blocked = [t for t in trades if t["blocked"]]
    unblocked = [t for t in trades if not t["blocked"]]

    return {
        "n_trades": len(trades),
        "slip_stats_bp": summary_stats(slips),
        "slip_stats_cents": summary_stats(slips_cents),
        "latency_stats_sec": summary_stats(latencies),
        "spread_stats_bp": summary_stats(spreads_bp),
        "spread_stats_cents": summary_stats(spreads_cents),
        "net_stats_bp": summary_stats(nets),
        "selection_stats_bp": summary_stats(selections),
        "notional_stats_usd": summary_stats(notionals),
        "by_slip_bucket": bucket_summary,
        "by_latency": latency_summary,
        "by_time_of_day": tod_summary,
        "by_price_tier": price_summary,
        "blocked_decomposition": {
            "n_blocked": len(blocked),
            "blocked_rate_pct": round(len(blocked) / len(trades) * 100, 2),
            "blocked_mean_slip_bp": round(statistics.fmean([t["slip"] for t in blocked]), 2),
            "blocked_median_slip_bp": round(statistics.median([t["slip"] for t in blocked]), 2),
            "blocked_mean_slip_cents": round(statistics.fmean([t["buy"] - t["ref"] for t in blocked]), 4),
            "blocked_mean_spread_bp": round(statistics.fmean([t["spread_bp"] for t in blocked if t["spread_bp"] is not None]), 2) if any(t["spread_bp"] is not None for t in blocked) else None,
            "blocked_mean_spread_cents": round(statistics.fmean([t["spread_cents"] for t in blocked if t["spread_cents"] is not None]), 4) if any(t["spread_cents"] is not None for t in blocked) else None,
            "unblocked_mean_slip_bp": round(statistics.fmean([t["slip"] for t in unblocked]), 2),
            "unblocked_median_slip_bp": round(statistics.median([t["slip"] for t in unblocked]), 2),
            "unblocked_mean_spread_bp": round(statistics.fmean([t["spread_bp"] for t in unblocked if t["spread_bp"] is not None]), 2) if any(t["spread_bp"] is not None for t in unblocked) else None,
            "blocked_mean_net_bp": round(statistics.fmean([t["net"] for t in blocked]), 2),
            "unblocked_mean_net_bp": round(statistics.fmean([t["net"] for t in unblocked]), 2),
            "blocked_mean_selection_bp": round(statistics.fmean([t["selection"] for t in blocked]), 2),
            "unblocked_mean_selection_bp": round(statistics.fmean([t["selection"] for t in unblocked]), 2),
        },
    }


def main():
    raw_path = ROOT / "ai_reports" / "outcomes.jsonl"
    raw = [json.loads(x) for x in open(raw_path) if x.strip()]
    rows, drops = M.rows_from(raw)

    by_raw = {}
    for r in raw:
        try:
            by_raw[(str(r.get("symbol")), round(float(r.get("entry_time")), 3))] = r
        except (TypeError, ValueError):
            continue

    st = Counter()
    scored = []
    for day in sorted({r["day"] for r in rows}):
        fills = M.fills_for(day)
        for r in [x for x in rows if x["day"] == day]:
            m = M.match_with_time(r, fills)
            if m is None:
                st["unmatched"] += 1
                continue
            if m[0] < M.FLOOR:
                st["below_floor"] += 1
                continue
            s_row = M.score_row(dict(r, buy=m[0], sell=m[1], send=m[2]))
            s_row["latency"] = s_row["send"] - s_row["entry_time"]
            if s_row.get("bid") and s_row["bid"] > 0:
                s_row["spread_bp"] = (s_row["ref"] - s_row["bid"]) / s_row["ref"] * 1e4
                s_row["spread_cents"] = s_row["ref"] - s_row["bid"]
            else:
                s_row["spread_bp"] = None
                s_row["spread_cents"] = None
            scored.append(s_row)

    primary = [r for r in scored if r["day"] <= M.PRIMARY_LAST]

    res_primary = analyze_trades(primary, by_raw)
    res_all = analyze_trades(scored, by_raw)

    out = {
        "primary_sample_period": f"{M.FIRST}..{M.PRIMARY_LAST}",
        "primary_summary": res_primary,
        "all_sample_period": f"{M.FIRST}..{M.LAST}",
        "all_summary": res_all,
    }

    out_file = Path(os.path.expanduser("~/replay_runs/marketable_limit/slip_diagnostic.json"))
    out_file.write_text(json.dumps(out, indent=2))
    print(f"Wrote diagnostic results to {out_file}")

    # Print executive summary to stdout
    print("\n" + "="*80)
    print("ENTRY SLIPPAGE DIAGNOSTIC REPORT (Row 63)")
    print("="*80)
    p_decomp = res_primary["blocked_decomposition"]
    print(f"Primary Trades: {res_primary['n_trades']} trades across {M.P1[0]}..{M.PRIMARY_LAST}")
    print(f"Overall Mean Slip: {res_primary['slip_stats_bp']['mean']} bp (median {res_primary['slip_stats_bp']['p50']} bp)")
    print(f"Mean Dispatch Latency: {res_primary['latency_stats_sec']['mean']} s (median {res_primary['latency_stats_sec']['p50']} s, p90 {res_primary['latency_stats_sec']['p90']} s)")
    print(f"Blocked Trades (>5 bp slip): {p_decomp['n_blocked']} / {res_primary['n_trades']} ({p_decomp['blocked_rate_pct']}%)")
    print(f"  • Blocked Mean Slip: +{p_decomp['blocked_mean_slip_bp']} bp (+${p_decomp['blocked_mean_slip_cents']:.3f}/sh)")
    print(f"  • Blocked Mean Quoted Spread: {p_decomp['blocked_mean_spread_bp']} bp (${p_decomp['blocked_mean_spread_cents']:.3f}/sh)")
    print(f"  • Blocked Net P&L: {p_decomp['blocked_mean_net_bp']} bp vs Unblocked {p_decomp['unblocked_mean_net_bp']} bp")
    print(f"  • Blocked Selection P&L: {p_decomp['blocked_mean_selection_bp']} bp vs Unblocked {p_decomp['unblocked_mean_selection_bp']} bp")

    print("\n--- SLIPPAGE BUCKETS ---")
    for b_name, b_data in res_primary["by_slip_bucket"].items():
        print(f"  {b_name:<30}: {b_data['n']:>3} trades ({b_data['share_pct']:>4.1f}%) | mean slip: {b_data['mean_slip_bp']:>6.2f} bp | mean net: {b_data['mean_net_bp']:>6.2f} bp | mean lat: {b_data['mean_latency_s']:>5.3f}s")

    print("\n--- LATENCY BRACKETS ---")
    for l_name, l_data in res_primary["by_latency"].items():
        print(f"  {l_name:<15}: {l_data['n']:>3} trades ({l_data['share_pct']:>4.1f}%) | blocked: {l_data['blocked_n']:>3} ({l_data['blocked_rate_pct']:>4.1f}%) | mean slip: {l_data['mean_slip_bp']:>6.2f} bp | median slip: {l_data['median_slip_bp']:>6.2f} bp")

    print("\n--- PRICE BRACKETS ---")
    for p_name, p_data in res_primary["by_price_tier"].items():
        print(f"  {p_name:<15}: {p_data['n']:>3} trades ({p_data['blocked_pct']:>4.1f}% blk) | mean slip: {p_data['mean_slip_bp']:>5.2f} bp (${p_data['mean_slip_cents']:.3f}) | mean spread: {p_data['mean_spread_bp']:>5.2f} bp (${p_data['mean_spread_cents']:.3f})")

    print("\n--- TIME OF DAY (FIRST 30-MIN BLOCKS) ---")
    for t_name, t_data in list(res_primary["by_time_of_day"].items())[:8]:
        print(f"  {t_name:<10}: {t_data['n']:>3} trades ({t_data['blocked_pct']:>4.1f}% blk) | mean slip: {t_data['mean_slip_bp']:>6.2f} bp | mean spread: {t_data['mean_spread_bp']} bp")


if __name__ == "__main__":
    main()
