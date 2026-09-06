#!/usr/bin/env python3
"""Counterfactual scoreboard for Plan B burst (premarket burst + RSI-2 >= 70).

Reads signal / dry-run rows and scores the SAME signals under at least three
fill models:

  signal_close   fill at the signal bar's close (optimistic)
  next_open      fill at the next bar's open (REALISTIC / pass-fail default)
  plus_2_open    fill at the open two bars after the signal

Pass/fail uses next_open only. Win% alone is not a pass metric — see
docs/PLAN_B_BURST.md for the frozen bars.

Usage:
  python3 tools/plan_b_burst_scoreboard.py --fixture path.json
  python3 tools/plan_b_burst_scoreboard.py --signals ai_reports/strength_signals.jsonl

Fixture shape (smoke / unit tests):
  {
    "signals": [{"symbol","signal_bar_ts","decision_ts","latency_sec",
                 "fill_model","cm_rsi","day", ...}],
    "bars": {"SYM": [[ts, open, high, low, close], ...]}
  }
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Frozen with docs/PLAN_B_BURST.md — do not retune on holdout.
STOP_PCT = 5.0
ROUND_TRIP_FRICTION_PCT = 0.40
PASS_FILL_MODEL = "next_open"
MIN_SIGNALS = 30
# One 1m bar. If median decision latency exceeds this, live desk cannot
# achieve the next_open fill the pass bar assumes.
MAX_MEDIAN_LATENCY_SEC = 60.0
EOD_EXIT_MIN = 15 * 60 + 55  # flatten near the close if still open

FILL_MODELS = ("signal_close", "next_open", "plus_2_open")


def load_jsonl(path: str) -> list[dict]:
    rows: list[dict] = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def map_signal_row(row: dict) -> dict | None:
    """Normalize strength_signal / plan_b_burst rows into a scoreboard signal."""
    if not isinstance(row, dict):
        return None
    sym = str(row.get("symbol") or "").upper().strip()
    if not sym:
        return None
    bar_ts = row.get("signal_bar_ts", row.get("bar_ts"))
    try:
        bar_ts = float(bar_ts)
    except (TypeError, ValueError):
        return None
    decision_ts = row.get("decision_ts", row.get("fired_at", row.get("ts")))
    try:
        decision_ts = float(decision_ts) if decision_ts is not None else bar_ts
    except (TypeError, ValueError):
        decision_ts = bar_ts
    try:
        latency = row.get("latency_sec")
        latency = (
            float(latency)
            if latency is not None
            else round(decision_ts - bar_ts, 1)
        )
    except (TypeError, ValueError):
        latency = None
    rsi = row.get("cm_rsi", row.get("rsi"))
    return {
        "symbol": sym,
        "day": str(row.get("day") or ""),
        "signal_bar_ts": bar_ts,
        "decision_ts": decision_ts,
        "latency_sec": latency,
        "fill_model": str(row.get("fill_model") or PASS_FILL_MODEL),
        "cm_rsi": rsi,
        "rsi": rsi,
        "burst_universe": row.get("burst_universe"),
        "price": row.get("price"),
        "kind": row.get("kind"),
        "action": row.get("action"),
    }


def required_latency_fields(row: dict) -> list[str]:
    """Fields every honesty-gated signal row must carry."""
    missing = []
    for k in ("signal_bar_ts", "decision_ts", "latency_sec", "fill_model"):
        if row.get(k) is None and row.get(
            {"signal_bar_ts": "bar_ts", "decision_ts": "fired_at"}.get(k, k)
        ) is None:
            missing.append(k)
    return missing


def _bar_index(bars: list[list], signal_bar_ts: float) -> int | None:
    """Index of the signal bar (closest ts <= signal_bar_ts within 1s)."""
    best = None
    for i, b in enumerate(bars):
        try:
            ts = float(b[0])
        except (TypeError, ValueError, IndexError):
            continue
        if abs(ts - signal_bar_ts) <= 1.0:
            return i
        if ts <= signal_bar_ts:
            best = i
    return best


def fill_price(bars: list[list], signal_i: int, model: str) -> tuple[float, int] | None:
    """Return (fill_px, fill_bar_index) for a fill model, or None if impossible."""
    n = len(bars)
    if signal_i < 0 or signal_i >= n:
        return None
    try:
        if model == "signal_close":
            return float(bars[signal_i][4]), signal_i
        if model == "next_open":
            j = signal_i + 1
            if j >= n:
                return None
            return float(bars[j][1]), j
        if model == "next_bar_close":
            j = signal_i + 1
            if j >= n:
                return None
            return float(bars[j][4]), j
        if model == "plus_2_open":
            j = signal_i + 2
            if j >= n:
                return None
            return float(bars[j][1]), j
        if model == "plus_2_close":
            j = signal_i + 2
            if j >= n:
                return None
            return float(bars[j][4]), j
    except (TypeError, ValueError, IndexError):
        return None
    return None


def _et_minutes(ts: float) -> int:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    d = datetime.fromtimestamp(ts, ZoneInfo("America/New_York"))
    return d.hour * 60 + d.minute


def walk_trade(
    bars: list[list],
    fill_i: int,
    entry: float,
    *,
    stop_pct: float = STOP_PCT,
) -> dict[str, Any]:
    """Simple Plan B exit: -stop_pct hard, else flatten near EOD.

    Intentionally thin — the scoreboard's job is fill honesty across models,
    not re-optimizing the ratchet. Risk unit = stop_pct of entry (1R).
    """
    if entry <= 0 or fill_i < 0 or fill_i >= len(bars):
        return {"ok": False, "reason": "bad_entry"}
    stop = entry * (1.0 - stop_pct / 100.0)
    risk = entry - stop
    if risk <= 0:
        return {"ok": False, "reason": "bad_risk"}
    peak = entry
    exit_px = entry
    exit_ts = float(bars[fill_i][0])
    reason = "eod"
    for j in range(fill_i, len(bars)):
        ts, o, h, low, c = (float(x) for x in bars[j][:5])
        peak = max(peak, h)
        if low <= stop:
            exit_px = stop
            exit_ts = ts
            reason = "hard_stop"
            break
        exit_px = c
        exit_ts = ts
        if _et_minutes(ts) >= EOD_EXIT_MIN:
            reason = "eod"
            break
    pnl_pct = (exit_px - entry) / entry * 100.0
    r_mult = (exit_px - entry) / risk
    return {
        "ok": True,
        "entry": entry,
        "exit": exit_px,
        "exit_ts": exit_ts,
        "exit_reason": reason,
        "pnl_pct": pnl_pct,
        "r": r_mult,
        "peak": peak,
        "stop": stop,
    }


def score_signal(
    sig: dict,
    bars_by_sym: dict[str, list[list]],
    models: tuple[str, ...] = FILL_MODELS,
) -> dict[str, Any]:
    sym = sig["symbol"]
    bars = bars_by_sym.get(sym) or []
    out: dict[str, Any] = {
        "symbol": sym,
        "day": sig.get("day"),
        "signal_bar_ts": sig.get("signal_bar_ts"),
        "decision_ts": sig.get("decision_ts"),
        "latency_sec": sig.get("latency_sec"),
        "fill_model_declared": sig.get("fill_model"),
        "cm_rsi": sig.get("cm_rsi"),
        "models": {},
    }
    if not bars:
        out["error"] = "no_bars"
        return out
    si = _bar_index(bars, float(sig["signal_bar_ts"]))
    if si is None:
        out["error"] = "signal_bar_missing"
        return out
    out["signal_bar_index"] = si
    for model in models:
        fp = fill_price(bars, si, model)
        if fp is None:
            out["models"][model] = {"ok": False, "reason": "no_fill_bar"}
            continue
        px, fi = fp
        trade = walk_trade(bars, fi, px)
        trade["fill_model"] = model
        trade["fill_px"] = px
        trade["fill_bar_index"] = fi
        # Friction haircut on pct (round-trip).
        if trade.get("ok"):
            trade["pnl_pct_net"] = float(trade["pnl_pct"]) - ROUND_TRIP_FRICTION_PCT
            trade["r_net"] = float(trade["r"]) - (
                ROUND_TRIP_FRICTION_PCT / STOP_PCT
            )
        out["models"][model] = trade
    return out


def summarize(scored: list[dict], model: str = PASS_FILL_MODEL) -> dict[str, Any]:
    rs: list[float] = []
    pnl: list[float] = []
    wins = 0
    latencies: list[float] = []
    sessions: set[str] = set()
    n_ok = 0
    for row in scored:
        lat = row.get("latency_sec")
        if isinstance(lat, (int, float)) and not math.isnan(float(lat)):
            latencies.append(float(lat))
        day = row.get("day")
        if day:
            sessions.add(str(day))
        m = (row.get("models") or {}).get(model) or {}
        if not m.get("ok"):
            continue
        n_ok += 1
        r = float(m["r"])
        p = float(m["pnl_pct"])
        rs.append(r)
        pnl.append(p)
        if p > 0:
            wins += 1
    med_r = statistics.median(rs) if rs else None
    med_lat = statistics.median(latencies) if latencies else None
    sum_pnl = sum(pnl) if pnl else 0.0
    expectancy = (sum_pnl / n_ok) if n_ok else None
    win_pct = (100.0 * wins / n_ok) if n_ok else None
    return {
        "fill_model": model,
        "n_signals": len(scored),
        "n_trades": n_ok,
        "n_sessions": len(sessions),
        "sessions": sorted(sessions),
        "med_r": med_r,
        "sum_pnl_pct": sum_pnl,
        "expectancy_pct": expectancy,
        "win_pct": win_pct,
        "med_latency_sec": med_lat,
        "round_trip_friction_pct": ROUND_TRIP_FRICTION_PCT,
    }


def verdict(summary: dict, *, min_signals: int = MIN_SIGNALS) -> dict[str, Any]:
    """Frozen pass/fail from docs/PLAN_B_BURST.md. Do not retune here."""
    reasons: list[str] = []
    model = summary.get("fill_model")
    if model != PASS_FILL_MODEL:
        reasons.append("wrong_fill_model_%s" % model)

    n = int(summary.get("n_trades") or 0)
    if n < min_signals:
        reasons.append("n_trades_%d_lt_%d" % (n, min_signals))

    med_r = summary.get("med_r")
    if med_r is None or med_r <= 0:
        reasons.append("med_r_not_positive")

    sum_pnl = float(summary.get("sum_pnl_pct") or 0.0)
    exp = summary.get("expectancy_pct")
    friction_ok = (exp is not None and exp > ROUND_TRIP_FRICTION_PCT) or sum_pnl > 0
    if not friction_ok:
        reasons.append("sum_pnl_or_expectancy_not_above_friction")

    med_lat = summary.get("med_latency_sec")
    if med_lat is None:
        reasons.append("latency_missing")
    elif float(med_lat) > MAX_MEDIAN_LATENCY_SEC:
        reasons.append("median_latency_gt_one_bar")

    # Explicit FAIL shapes called out in the brief.
    win_pct = summary.get("win_pct")
    if (
        win_pct is not None
        and win_pct >= 55
        and (med_r is None or med_r <= 0)
    ):
        reasons.append("win_pct_only_med_r_nonpositive")

    passed = not reasons
    return {
        "pass": passed,
        "reasons": reasons,
        "min_signals": min_signals,
        "pass_fill_model": PASS_FILL_MODEL,
        "max_median_latency_sec": MAX_MEDIAN_LATENCY_SEC,
    }


def load_fixture(path: str) -> tuple[list[dict], dict[str, list[list]]]:
    raw = json.load(open(path))
    signals = [map_signal_row(s) for s in (raw.get("signals") or [])]
    signals = [s for s in signals if s]
    bars = {str(k).upper(): v for k, v in (raw.get("bars") or {}).items()}
    return signals, bars


def counterfactual_table(scored: list[dict]) -> dict[str, dict]:
    return {m: summarize(scored, m) for m in FILL_MODELS}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--fixture", help="JSON fixture with signals + bars")
    p.add_argument("--signals", help="jsonl of strength / plan_b rows")
    p.add_argument("--bars-json", help="optional {SYM: [[ts,o,h,l,c],...]} file")
    p.add_argument("--min-signals", type=int, default=MIN_SIGNALS)
    p.add_argument("--json", action="store_true", help="print full JSON")
    args = p.parse_args(argv)

    if not args.fixture and not args.signals:
        p.error("need --fixture or --signals")

    if args.fixture:
        signals, bars = load_fixture(args.fixture)
    else:
        signals = []
        for row in load_jsonl(args.signals):
            # Prefer raw signal rows; skip pure skip/error trade rows without a bar.
            if row.get("kind") in ("strength_trade", "plan_b_burst") and row.get(
                "action"
            ) not in (None, "would_place", "placed"):
                if row.get("signal_bar_ts") is None and row.get("bar_ts") is None:
                    continue
            m = map_signal_row(row)
            if m:
                signals.append(m)
        bars = {}
        if args.bars_json:
            bars = {
                str(k).upper(): v
                for k, v in json.load(open(args.bars_json)).items()
            }

    scored = [score_signal(s, bars) for s in signals]
    table = counterfactual_table(scored)
    primary = table[PASS_FILL_MODEL]
    v = verdict(primary, min_signals=args.min_signals)

    report = {
        "pass_fill_model": PASS_FILL_MODEL,
        "counterfactual": table,
        "verdict": v,
        "n_scored": len(scored),
        "latency_field_gaps": sum(
            1 for s in signals if required_latency_fields(s)
        ),
    }
    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 0 if v["pass"] else 1

    print("Plan B burst scoreboard")
    print("  pass/fail fill model: %s" % PASS_FILL_MODEL)
    print("  friction assumed:     %.2f%% round trip" % ROUND_TRIP_FRICTION_PCT)
    print()
    for model, s in table.items():
        mark = " <-- PASS/FAIL" if model == PASS_FILL_MODEL else ""
        print(
            "  %-14s  n=%d  med_R=%s  sum_pnl%%=%.2f  win%%=%s  med_lat=%s%s"
            % (
                model,
                s["n_trades"],
                ("%.3f" % s["med_r"]) if s["med_r"] is not None else "n/a",
                s["sum_pnl_pct"],
                ("%.0f" % s["win_pct"]) if s["win_pct"] is not None else "n/a",
                ("%.1f" % s["med_latency_sec"])
                if s["med_latency_sec"] is not None
                else "n/a",
                mark,
            )
        )
    print()
    print("VERDICT: %s" % ("PASS" if v["pass"] else "FAIL"))
    if v["reasons"]:
        for r in v["reasons"]:
            print("  - %s" % r)
    return 0 if v["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
