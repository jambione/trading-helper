#!/usr/bin/env python3
"""Verify the profit redesign when the cash session is closed.

Alpaca will not accept the desk's bracket entries outside RTH, and the
live book should not be buying anyway (``desk_product=observe``). This
script is the after-hours path:

  1. Offline gates — no keys, no orders. Product veto, H4 simulator,
     fill-truth day filter, universe/spread.
  2. ``--lab`` — daily bars + H4 screen + EOD. Works 24/7 on the mini.
     Places nothing.

    .venv/bin/python tools/after_hours_smoke.py
    .venv/bin/python tools/after_hours_smoke.py --lab

See docs/PROFIT_REDESIGN.md §12.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

ET = ZoneInfo("America/New_York")


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


def _rth_open(now: datetime | None = None) -> bool:
    now = now or datetime.now(ET)
    if now.weekday() >= 5:
        return False
    m = now.hour * 60 + now.minute
    return (9 * 60 + 30) <= m < (16 * 60)


def run_offline_checks() -> list[Check]:
    """Pure checks. Safe on a laptop with no Alpaca keys."""
    out: list[Check] = []

    import desk_h4
    import desk_product as dp
    import h4_screen as h4s
    import ai_entry_watch as ew
    from fill_truth_report import _pair_round_trips, closed_on_day
    from datetime import date

    out.append(Check(
        "observe_veto",
        dp.arm_block_reason({"desk_product": "observe"}) == dp.REASON_OBSERVE,
        "desk_observe",
    ))
    out.append(Check(
        "omitted_product_is_legacy",
        dp.arm_block_reason({}) is None,
        "partial cfg must not surprise unit tests",
    ))
    out.append(Check(
        "h4_off_until_flag",
        dp.arm_block_reason({"desk_product": "h4_swing"}) == dp.REASON_H4_OFF,
        "ai_h4_paper default false",
    ))
    out.append(Check(
        "unknown_product_fails_closed",
        dp.product({"desk_product": "magic"}) == dp.OBSERVE,
        "unknown → observe",
    ))

    rec = {
        "symbol": "AAA",
        "status": "watching",
        "admit_ts": 1.0,
        "structure": {
            "decision": "BUY",
            "entry_low": 9.5, "entry_high": 10.5,
            "stop_price": 9.5, "target_1": 11.0, "reward_risk": 1.0,
            "zone_kind": "at_last", "synthetic": True,
        },
        "indicator": {
            "pctr": -10.0, "pctr_rising": True, "pctr_ok": True,
            "cm_ok": True, "cm_rsi": 20.0, "cm_rsi_rising": True,
        },
    }
    ok, why = ew.should_arm_buy(
        rec, ask=10.0, bid=9.99,
        cfg={"desk_product": "observe", "ai_watch_arm_mode": "last",
             "ai_min_reward_risk": 0.5, "ai_watch_synth_rr": 1.0,
             "ai_watch_synth_stop_pct": 5.0},
        now=1.0,
    )
    out.append(Check("should_arm_observe", (not ok) and why == "desk_observe", why))

    row = {"ticker": "HPE", "price": 48.0, "avg_dollar_vol_50d": 8e7, "rs_rating": 90}
    u_ok, u_why = desk_h4.universe_row_ok(row, {})
    out.append(Check("h4_universe_liquid", u_ok, u_why))
    thin = dict(row, price=3.0)
    t_ok, t_why = desk_h4.universe_row_ok(thin, {})
    out.append(Check("h4_universe_rejects_cheap", (not t_ok) and t_why == "h4_price", t_why))

    sp_ok, _ = desk_h4.spread_ok(10.00, 10.005, {"h4_max_spread_pct": 0.10})
    wide_ok, wide_why = desk_h4.spread_ok(10.00, 10.05, {"h4_max_spread_pct": 0.10})
    out.append(Check("h4_spread_tight_ok", sp_ok, "5 bps"))
    out.append(Check("h4_spread_wide_block", (not wide_ok) and wide_why == "h4_spread", wide_why))

    bars = [
        {"date": "2026-08-10", "open": 10.0, "high": 10.2, "low": 9.5, "close": 9.8},
        {"date": "2026-08-11", "open": 9.7, "high": 9.9, "low": 9.6, "close": 9.9},
        {"date": "2026-08-12", "open": 10.2, "high": 10.4, "low": 10.1, "close": 10.3},
        {"date": "2026-08-13", "open": 10.4, "high": 10.6, "low": 10.3, "close": 10.5},
    ]
    swings = h4s.simulate_hold(bars, hold_days=2, stop_pct=2.0, haircut_pct=0.20)
    out.append(Check(
        "h4_sim_stop",
        bool(swings) and swings[0]["stopped"] and abs(swings[0]["fwd"] + 2.0) < 1e-9,
        f"n={len(swings)} first_fwd={swings[0]['fwd'] if swings else None}",
    ))

    keep, drop = desk_h4.partition_state({
        "AAA": {"strategy": "h4_swing"},
        "BBB": {"strategy": "day_scalp_v0"},
    })
    out.append(Check(
        "h4_survives_sod_partition",
        "AAA" in keep and "BBB" in drop,
        f"keep={list(keep)} drop={list(drop)}",
    ))

    fills = [
        {"symbol": "AAA", "side": "buy", "filled_qty": 10,
         "filled_avg_price": 10.0, "filled_at": "2026-08-21T14:00:00Z"},
        {"symbol": "AAA", "side": "sell", "filled_qty": 10,
         "filled_avg_price": 11.0, "filled_at": "2026-08-21T18:00:00Z"},
    ]
    closed = _pair_round_trips(fills)
    hit = closed_on_day(closed, date(2026, 8, 21))
    miss = closed_on_day(closed, date(2026, 8, 20))
    out.append(Check(
        "fill_truth_sell_time",
        len(hit) == 1 and miss == [],
        "sell_time on 8/21, not exit_time",
    ))

    now = datetime.now(ET)
    out.append(Check(
        "clock_reports_session",
        True,
        f"{now:%Y-%m-%d %H:%M ET} RTH={'open' if _rth_open(now) else 'closed'}",
    ))
    return out


def _py() -> str:
    cand = os.path.join(ROOT, ".venv", "bin", "python")
    return cand if os.path.isfile(cand) else sys.executable


def have_alpaca() -> bool:
    try:
        import bars
        return bars.client() is not None
    except Exception:
        return False


def run_lab() -> list[Check]:
    """Daily-bar H4 screen + EOD. Read-only. Mini / venv."""
    out: list[Check] = []
    if not have_alpaca():
        out.append(Check("alpaca_client", False, "no keys — run on the mini"))
        return out
    out.append(Check("alpaca_client", True, "secrets.json api_key"))

    py = _py()
    jobs = [
        ("h4_screen", [py, os.path.join(ROOT, "tools", "h4_screen.py"),
                       "--days", "20"]),
        ("eod", [py, os.path.join(ROOT, "tools", "eod.py"), "--days", "1"]),
    ]
    for name, cmd in jobs:
        try:
            p = subprocess.run(
                cmd, cwd=ROOT, capture_output=True, text=True, timeout=300,
            )
            text = (p.stdout or "") + (p.stderr or "")
            tail = "\n".join(text.strip().splitlines()[-8:])
            out.append(Check(name, p.returncode == 0, tail or f"rc={p.returncode}"))
        except subprocess.TimeoutExpired:
            out.append(Check(name, False, "timed out (300s)"))
        except OSError as e:
            out.append(Check(name, False, str(e)))
    return out


def render(title: str, checks: list[Check]) -> int:
    print("=" * 66)
    print(f"  {title}")
    print("=" * 66)
    failed = 0
    for c in checks:
        mark = "PASS" if c.ok else "FAIL"
        if not c.ok:
            failed += 1
        print(f"  {mark:<4}  {c.name:<28}  {c.detail}")
    print("-" * 66)
    print(f"  {len(checks) - failed}/{len(checks)} passed")
    return failed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--lab", action="store_true",
                    help="also run H4 screen + EOD (needs Alpaca, mini)")
    args = ap.parse_args()

    failed = render("AFTER-HOURS SMOKE — offline (no orders)", run_offline_checks())
    if args.lab:
        failed += render("AFTER-HOURS SMOKE — lab (read-only Alpaca)", run_lab())
    elif not have_alpaca():
        print("  lab skipped (no Alpaca client). On the mini: "
              ".venv/bin/python tools/after_hours_smoke.py --lab")
    else:
        print("  Alpaca keys present. Pass --lab to run H4 screen + EOD.")

    print()
    print("  This script never places an order. Brackets are refused")
    print("  outside RTH even if someone set desk_force.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
