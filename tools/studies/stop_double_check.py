#!/usr/bin/env python3
"""stop_double_check.py — docs/studies/stop_double_check_prereg.json.

Replay the desk's own round trips. The stop arm sells at the first ratchet-shelf
hit. The stop-and-check arm sells at a shelf hit only when the %R double-check
says sell yes. Nothing here places an order or edits the live exit.

USAGE  .venv/bin/python tools/studies/stop_double_check.py
"""
from __future__ import annotations

import json
import os
import pickle
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path[:0] = [os.path.join(ROOT, "tools", "studies"), os.path.join(ROOT, "tools"), ROOT]

import ai_positions as cp  # noqa: E402
import sell_double_check as sdc  # noqa: E402
import std_scorer as scorer  # noqa: E402

ET = ZoneInfo("America/New_York")
OUTCOMES = os.path.join(ROOT, "ai_reports", "outcomes.jsonl")
CACHE = os.path.join(ROOT, "ai_reports", "runway_bars_cache.pkl")
LOOKBACK = sdc.LOOKBACK_MIN
EOD_MIN = 15 * 60 + 50
RTH_OPEN = 9 * 60 + 30
MIN_STOP_PCT = 0.005
MAX_STOP_PCT = 0.20
MIN_DAY_N = 5
MIN_DAYS = 8
PASS_BP = 3.0
PASS_T = 2.0
PASS_POS_SHARE = 0.60
PASS_STOP_SHARE = 0.30


def et_minutes(ts: float) -> int:
    dt = datetime.fromtimestamp(float(ts), timezone.utc).astimezone(ET)
    return dt.hour * 60 + dt.minute


def et_day(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), timezone.utc).astimezone(ET).strftime("%Y-%m-%d")


def _call_at_touch(prior_rows: list[tuple[float, float, float]], price: float) -> dict | None:
    """The double-check row at a shelf touch, or None when the touch is blind."""
    if not prior_rows or price is None or price <= 0:
        return None
    fast = sdc.line_window(
        prior_rows, price, sdc.FAST_LENGTH, sdc.FAST_SPAN, LOOKBACK)
    slow = sdc.line_window(
        prior_rows, price, sdc.SLOW_LENGTH, sdc.SLOW_SPAN, LOOKBACK)
    return sdc.decide(fast, slow, lookback=LOOKBACK)


def checker_sell_yes(prior_rows: list[tuple[float, float, float]], price: float) -> bool:
    """Both lines down, or no lookback. One line down does not sell."""
    call = _call_at_touch(prior_rows, price)
    if call is None:
        return True
    return call["sell"] == "yes"


def one_line_sell(call: dict) -> bool:
    """Sell when either line is down. No lookback still sells.

    Neither line down (both up, both flat, or split between up and flat)
    declines the shelf hit. Registered in stop_double_check_one_line_prereg.json.
    """
    if call.get("trend") == "no_lookback":
        return True
    return call.get("fast_trend") == "down" or call.get("slow_trend") == "down"


def checker_one_line_sell_yes(prior_rows: list[tuple[float, float, float]], price: float) -> bool:
    """Either line down, or no lookback. Decline only when neither line is down."""
    call = _call_at_touch(prior_rows, price)
    if call is None:
        return True
    return one_line_sell(call)


def _new_pos(entry_px: float, risk: float, entry_ts: float, cfg: dict) -> dict:
    shelf = cp.initial_local_stop(entry_px, risk, cfg)
    pos = {
        "entry_price": float(entry_px),
        "entry": float(entry_px),
        "risk_per_share": float(risk),
        "stop_price": float(entry_px) - float(risk),
        "entry_stop_price": float(entry_px) - float(risk),
        "entry_ts": float(entry_ts),
        "peak_price": float(entry_px),
        "mfe_r": 0.0,
        "mae_r": 0.0,
        "entry_confirmed": True,
        "local_stop_price": float(shelf) if shelf is not None else float(entry_px) - float(risk),
    }
    ring = max(2, int(cfg.get("ai_local_trail_print_ring", 2) or 2))
    cp.note_trail_print(pos, entry_px, n=ring)
    return pos


def _advance(pos: dict, cfg: dict, ts: float, high: float, low: float, close: float) -> None:
    entry = float(pos["entry_price"])
    risk = float(pos["risk_per_share"])
    pos["last_seen_price"] = float(close)
    pos["peak_price"] = max(float(pos.get("peak_price") or entry), float(high))
    pos["mfe_r"] = max(float(pos.get("mfe_r") or 0.0), (float(pos["peak_price"]) - entry) / risk)
    pos["mae_r"] = min(float(pos.get("mae_r") or 0.0), (float(low) - entry) / risk)
    ring = max(2, int(cfg.get("ai_local_trail_print_ring", 2) or 2))
    cp.note_trail_print(pos, close, n=ring)
    want = cp.local_profit_stop(pos, cfg, now=ts)
    prev = float(pos["local_stop_price"])
    if want is not None and float(want) > prev + 1e-9:
        pos["local_stop_price"] = float(want)


def simulate(
    bars: list[tuple[float, float, float, float, float]],
    entry_ts: float,
    entry_px: float,
    risk: float,
    cfg: dict,
    *,
    use_gate: bool,
    sell_yes=checker_sell_yes,
) -> dict | None:
    """Walk one trade. ``bars`` are (ts, open, high, low, close), oldest first.

    Returns None when there is no bar after the fill and before the flatten.
    """
    if entry_px <= 0 or risk <= 0 or not bars:
        return None
    start = None
    for i, b in enumerate(bars):
        if float(b[0]) + 60.0 > float(entry_ts):
            start = i + 1 if float(b[0]) <= float(entry_ts) else i
            break
    if start is None or start >= len(bars):
        return None
    pos = _new_pos(entry_px, risk, entry_ts, cfg)
    if pos["local_stop_price"] <= 0:
        return None
    declined = 0
    for i in range(start, len(bars)):
        ts, op, hi, lo, cl = (float(x) for x in bars[i])
        if et_minutes(ts) >= EOD_MIN:
            return {
                "exit_px": cl, "exit_ts": ts, "reason": "eod",
                "declined": declined, "shelf": float(pos["local_stop_price"]),
            }
        shelf = float(pos["local_stop_price"])
        if lo <= shelf + 1e-9:
            touch = op if op <= shelf + 1e-9 else shelf
            prior = [(float(b[2]), float(b[3]), float(b[4])) for b in bars[:i]]
            allow = (not use_gate) or sell_yes(prior, touch)
            if allow:
                return {
                    "exit_px": touch, "exit_ts": ts, "reason": "stop",
                    "declined": declined, "shelf": shelf,
                }
            declined += 1
        _advance(pos, cfg, ts, hi, lo, cl)
    last = bars[-1]
    return {
        "exit_px": float(last[4]), "exit_ts": float(last[0]), "reason": "eod",
        "declined": declined, "shelf": float(pos["local_stop_price"]),
    }


def _valid_risk(entry: float, stop: float) -> float | None:
    if entry <= 0 or stop <= 0 or stop >= entry:
        return None
    pct = (entry - stop) / entry
    if pct < MIN_STOP_PCT or pct > MAX_STOP_PCT:
        return None
    return entry - stop


def load_trades(outcomes_path: str, cache: dict) -> tuple[list[dict], dict]:
    """Join outcomes to cached minute bars. ``skipped`` counts why rows dropped."""
    skipped = {
        "bad_json": 0, "no_entry": 0, "outside_rth": 0,
        "bad_risk": 0, "no_bars": 0,
    }
    out = []
    with open(outcomes_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                skipped["bad_json"] += 1
                continue
            entry = _f(row.get("entry_price"))
            ts = _f(row.get("entry_time") or row.get("ts"))
            if not entry or entry <= 0 or not ts:
                skipped["no_entry"] += 1
                continue
            minute = et_minutes(ts)
            if minute < RTH_OPEN or minute >= EOD_MIN:
                skipped["outside_rth"] += 1
                continue
            risk = _valid_risk(entry, _f(row.get("stop_price")) or 0.0)
            if risk is None:
                skipped["bad_risk"] += 1
                continue
            day = et_day(ts)
            sym = str(row.get("symbol") or "").upper()
            packed = cache.get((sym, day))
            if not packed:
                skipped["no_bars"] += 1
                continue
            bars = _ohlc(packed)
            if len(bars) < 30:
                skipped["no_bars"] += 1
                continue
            out.append({
                "symbol": sym, "day": day, "entry_ts": ts, "entry_px": entry,
                "risk": risk, "bars": bars,
                "close_reason": row.get("close_reason"),
            })
    return out, skipped


def _f(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _ohlc(packed) -> list[tuple[float, float, float, float, float]]:
    """Cache tuple is (ts, open, high, low, close, volume)."""
    ts, op, hi, lo, cl = packed[0], packed[1], packed[2], packed[3], packed[4]
    rows = []
    for i in range(len(ts)):
        try:
            rows.append((float(ts[i]), float(op[i]), float(hi[i]), float(lo[i]), float(cl[i])))
        except (TypeError, ValueError):
            continue
    rows.sort(key=lambda r: r[0])
    dedup = []
    seen = set()
    for r in rows:
        if r[0] in seen:
            continue
        seen.add(r[0])
        dedup.append(r)
    return dedup


def bp(entry: float, exit_px: float) -> float:
    return (float(exit_px) / float(entry) - 1.0) * 1e4


def score(trades: list[dict], cfg: dict, sell_yes=checker_sell_yes) -> dict:
    paired = []
    for t in trades:
        stop = simulate(
            t["bars"], t["entry_ts"], t["entry_px"], t["risk"], cfg, use_gate=False)
        gated = simulate(
            t["bars"], t["entry_ts"], t["entry_px"], t["risk"], cfg,
            use_gate=True, sell_yes=sell_yes)
        if stop is None or gated is None:
            continue
        b0 = bp(t["entry_px"], stop["exit_px"])
        b1 = bp(t["entry_px"], gated["exit_px"])
        paired.append({
            "day": t["day"], "symbol": t["symbol"],
            "stop_bp": b0, "gate_bp": b1, "diff": b1 - b0,
            "stop_reason": stop["reason"], "gate_reason": gated["reason"],
            "declined": gated["declined"],
            "entry_px": t["entry_px"],
        })
    by_day: dict[str, list] = {}
    for r in paired:
        by_day.setdefault(r["day"], []).append(r)
    scored_days = sorted(d for d, xs in by_day.items() if len(xs) >= MIN_DAY_N)
    daily = []
    for d in scored_days:
        xs = by_day[d]
        daily.append(sum(r["diff"] for r in xs) / len(xs))
    stat = scorer.day_stat(daily)
    n = len(paired)
    stop_hits = sum(r["stop_reason"] == "stop" for r in paired)
    differed = sum(abs(r["diff"]) > 1e-9 for r in paired)
    held = sum(r["declined"] > 0 for r in paired)
    ge10 = [r for r in paired if r["entry_px"] >= 10]
    return {
        "n": n,
        "stop_share": (stop_hits / n) if n else 0.0,
        "differed": differed,
        "held": held,
        "stop_bp": (sum(r["stop_bp"] for r in paired) / n) if n else float("nan"),
        "gate_bp": (sum(r["gate_bp"] for r in paired) / n) if n else float("nan"),
        "days": len(by_day),
        "scored_days": len(scored_days),
        "thin_days": sorted(d for d in by_day if d not in scored_days),
        "stat": stat,
        "pos_share": (sum(x > 0 for x in daily) / len(daily)) if daily else 0.0,
        "ge10_n": len(ge10),
        "ge10_diff": (sum(r["diff"] for r in ge10) / len(ge10)) if ge10 else float("nan"),
        "daily": list(zip(scored_days, daily)),
    }


def verdict(result: dict) -> tuple[str, str]:
    """PASS only on the pre-registered bar. Every miss is FAIL, with the reason."""
    st = result["stat"]
    misses = []
    if result["n"] <= 0:
        misses.append("no trades")
    if result["stop_share"] < PASS_STOP_SHARE:
        misses.append("shelf did not bind")
    if result["scored_days"] < MIN_DAYS:
        misses.append("fewer than 8 scored days")
    if not (st["mean"] >= PASS_BP):
        misses.append("paired mean under +3 bp")
    if not (st["t"] >= PASS_T):
        misses.append("t under 2")
    if result["pos_share"] < PASS_POS_SHARE:
        misses.append("positive on under 60% of days")
    if misses:
        return "FAIL", "; ".join(misses)
    return "PASS", "replay bar met; not a live switch"


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Shelf versus shelf plus a %R bend.")
    parser.add_argument(
        "--lines", type=int, choices=(1, 2), default=2,
        help="2 sells only when both lines have bent (the logged rule). "
             "1 sells when either line has bent.",
    )
    args = parser.parse_args()
    from config import load_config
    cfg = load_config()
    cache = pickle.load(open(CACHE, "rb"))
    trades, skipped = load_trades(OUTCOMES, cache)
    gate = checker_one_line_sell_yes if args.lines == 1 else checker_sell_yes
    label = "stop+one-line" if args.lines == 1 else "stop+check"
    result = score(trades, cfg, sell_yes=gate)
    st = result["stat"]
    call, why = verdict(result)
    print(
        f"stop vs {label}  trades {result['n']}  "
        f"days {result['days']}  scored {result['scored_days']}  skipped {skipped}"
    )
    print(
        f"  stop arm exits on the shelf {result['stop_share']:.0%}  "
        f"gate declined a hit on {result['held']} trades  "
        f"exits differ on {result['differed']}"
    )
    print(
        f"  gross bp/trade  stop {result['stop_bp']:+.2f}  "
        f"{label} {result['gate_bp']:+.2f}  "
        f"paired daily mean {st['mean']:+.2f}  t {st['t']:+.2f}  "
        f"n {st['n']}  positive days {result['pos_share']:.0%}"
    )
    print(
        f"  $10+ slice (not the verdict) n {result['ge10_n']}  "
        f"mean paired diff {result['ge10_diff']:+.2f} bp"
    )
    if result["daily"]:
        bits = " ".join(f"{d[5:]}:{v:+.1f}" for d, v in result["daily"])
        print(f"  daily paired bp  {bits}")
    print(f"  VERDICT: {call}  {why}. Live exit unchanged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
