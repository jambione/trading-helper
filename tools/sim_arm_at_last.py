#!/usr/bin/env python3
"""
sim_arm_at_last.py — zone vs last-mode arm, using the live should_arm_buy gate.

No broker. Prints who would buy, who WASH still blocks, and what 0.10R
give is in dollars if R is 5% of last.

USAGE
    venv/bin/python tools/sim_arm_at_last.py
    venv/bin/python tools/sim_arm_at_last.py --shadow          # if shadow.jsonl exists
    venv/bin/python tools/sim_arm_at_last.py --day 2026-08-14
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import ai_entry_watch as ew  # noqa: E402
from ai_paths import find_report_file, resolve_report_dir  # noqa: E402


def zone_cfg(**over) -> dict[str, Any]:
    """The pullback book: wait for the band. EXH on, heat 50–90."""
    cfg = {
        "ai_watch_arm_mode": "zone",
        "ai_watch_exhaustion_rules": True,
        "ai_watch_require_exhaustion_data": False,
        "ai_watch_exhaustion_heat_min_pct": 50.0,
        "ai_watch_exhaustion_heat_max_pct": 90.0,
        "ai_watch_in_zone_ignore_fade": False,
        "ai_watch_arm_require_indicators": False,
        "ai_min_reward_risk": 0.5,
        "ai_watch_min_stop_pct": 0.0,
        "ai_watch_require_db_zone": True,
        "ai_watch_arm_below_zone": True,
        "ai_watch_ob_allow_hot": True,
        "ai_watch_cheap_price": 5.0,
        "ai_watch_synth_stop_pct": 5.0,
        "ai_watch_synth_rr": 0.6,
        "ai_entry_zone_pad_pct": 0.0,
    }
    cfg.update(over)
    return cfg


def last_cfg(**over) -> dict[str, Any]:
    """Last-mode + EXH direction (rising or OB, not cooling)."""
    cfg = zone_cfg()
    cfg.update({
        "ai_watch_arm_mode": "last",
        "ai_watch_exhaustion_rules": True,
        "ai_watch_exhaustion_heat_min_pct": 0.0,
        "ai_watch_exhaustion_heat_max_pct": 0.0,
        "ai_watch_in_zone_ignore_fade": False,
        "ai_watch_require_exhaustion_data": False,
    })
    cfg.update(over)
    return cfg


def rec(
    symbol: str,
    *,
    ask: float,
    lo: float,
    hi: float,
    stop: float,
    source: str = "momentum",
    look: str | None = None,
    pctr: float | None = -20.0,
    rising: bool = True,
    zone_kind: str = "pullback_band",
) -> dict[str, Any]:
    target = ask + 0.6 * max(0.01, ask - stop)
    rr = 0.6
    out: dict[str, Any] = {
        "symbol": symbol,
        "status": "watching",
        "source": source,
        "look_reason": look,
        "admit_look_reason": look,
        "structure": {
            "decision": "WAIT",
            "wait_kind": "wait_for_zone",
            "entry_low": lo,
            "entry_high": hi,
            "stop_price": stop,
            "target_1": target,
            "reward_risk": rr,
            "zone_kind": zone_kind,
            "synthetic": zone_kind != "double_bottom",
        },
        "indicator": {},
    }
    if pctr is not None:
        out["indicator"] = {
            "pctr": pctr,
            "pctr_rising": bool(rising),
            "pctr_falling": not rising,
        }
    return out


def verdict(ok: bool, why: str) -> str:
    return f"BUY {why}" if ok else f"NO  {why}"


def rstop_give(ask: float, stop_pct: float = 5.0, give_r: float = 0.10) -> tuple[float, float]:
    """(R dollars, 0.10R give dollars)."""
    r = ask * (stop_pct / 100.0)
    return r, r * give_r


SCENARIOS: list[tuple[str, dict[str, Any], float]] = [
    (
        "UMAC runner — last above the pullback band, EXH 37 rising",
        rec("UMAC", ask=34.50, lo=32.74, hi=33.23, stop=32.00,
            pctr=-62.4, rising=True),
        34.50,
    ),
    (
        "ONDS in-band, rvol 1.8, EXH heating",
        rec("ONDS", ask=9.11, lo=9.03, hi=9.17, stop=8.80,
            pctr=-26.4, rising=True),
        9.11,
    ),
    (
        "NMAX already OB, still rising, last above the band",
        rec("NMAX", ask=11.32, lo=11.184, hi=11.229, stop=10.646,
            pctr=-3.72, rising=True),
        11.32,
    ),
    (
        "FGI fading / cooling, still in the band",
        rec("FGI", ask=6.50, lo=6.44, hi=6.54, stop=6.20,
            pctr=-50.0, rising=False),
        6.50,
    ),
    (
        "WASHY LOOK=WASH — tanking / near lows",
        rec("WASHY", ask=8.40, lo=8.10, hi=8.30, stop=7.90,
            look="WASH", pctr=-40.0, rising=True, source="trending"),
        8.40,
    ),
    (
        "CELC last well below a stale high shelf",
        rec("CELC", ask=91.82, lo=103.4, hi=104.6, stop=99.0,
            pctr=-10.0, rising=True, zone_kind="double_bottom"),
        91.82,
    ),
    (
        "SORA cheap tape, no %R reading",
        rec("SORA", ask=3.20, lo=3.18, hi=3.31, stop=3.00,
            pctr=None, source="momentum"),
        3.20,
    ),
]


def print_scenarios() -> None:
    z, last = zone_cfg(), last_cfg()
    print("=" * 78)
    print("  ARM SIM  zone (old pullback)  vs  last + EXH direction")
    print("  last buys rising or OB-not-falling. Cooling and WASH refuse.")
    print("=" * 78)
    for title, row, ask in SCENARIOS:
        ok_z, why_z = ew.should_arm_buy(row, ask=ask, bid=ask - 0.01, cfg=z)
        ok_l, why_l = ew.should_arm_buy(row, ask=ask, bid=ask - 0.01, cfg=last)
        r, give = rstop_give(ask)
        lo = row["structure"]["entry_low"]
        hi = row["structure"]["entry_high"]
        where = (
            "IN band" if lo <= ask <= hi
            else ("ABOVE band" if ask > hi else "BELOW band")
        )
        print()
        print(f"• {title}")
        print(f"  last ${ask:.2f}  band ${lo:.2f}–${hi:.2f}  ({where})")
        print(f"  zone: {verdict(ok_z, why_z)}")
        print(f"  last: {verdict(ok_l, why_l)}")
        if ok_l:
            print(f"  if filled here: R=${r:.3f}  RSTOP give=${give:.3f} "
                  f"shelf≈${ask - give:.2f}")


def rec_from_shadow(r: dict) -> tuple[dict[str, Any], float] | None:
    try:
        ask = float(r.get("price") or r.get("last_ask") or 0)
        lo = float(r.get("entry_low") or 0)
        hi = float(r.get("entry_high") or 0)
        stop = float(r.get("stop_price") or 0)
    except (TypeError, ValueError):
        return None
    if ask <= 0 or lo <= 0 or hi <= 0:
        return None
    if stop <= 0:
        stop = ask * 0.95
    pctr = r.get("pctr")
    try:
        pctr_f = float(pctr) if pctr is not None else None
    except (TypeError, ValueError):
        pctr_f = None
    st = str(r.get("exhaustion_state") or "")
    rising = st in ("heating", "overbought") or bool(r.get("pctr_rising"))
    row = rec(
        str(r.get("symbol") or "?"),
        ask=ask, lo=lo, hi=hi, stop=stop,
        source=str(r.get("source") or "momentum"),
        look=r.get("look_reason") or r.get("admit_look_reason"),
        pctr=pctr_f,
        rising=rising,
        zone_kind=str(r.get("zone_kind") or "pullback_band"),
    )
    return row, ask


def replay_shadow(day: str | None) -> None:
    path = find_report_file("shadow.jsonl") or (resolve_report_dir() / "shadow.jsonl")
    if not path.exists():
        print()
        print("No shadow.jsonl on this machine "
              f"({path}). Run this on the mini after a session to replay the book.")
        return
    z, last = zone_cfg(), last_cfg()
    n = 0
    zone_buy = last_buy = both = only_last = only_zone = 0
    last_why: Counter[str] = Counter()
    zone_why: Counter[str] = Counter()
    wash_last = 0
    above_now_buys: list[str] = []
    seen_above: set[str] = set()
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        if day:
            try:
                d = datetime.fromtimestamp(float(o.get("ts") or 0)).strftime("%Y-%m-%d")
            except Exception:
                d = ""
            if d != day:
                continue
        got = rec_from_shadow(o)
        if got is None:
            continue
        row, ask = got
        n += 1
        ok_z, why_z = ew.should_arm_buy(row, ask=ask, bid=None, cfg=z)
        ok_l, why_l = ew.should_arm_buy(row, ask=ask, bid=None, cfg=last)
        zone_why[why_z] += 1
        last_why[why_l] += 1
        if ok_z:
            zone_buy += 1
        if ok_l:
            last_buy += 1
        if ok_z and ok_l:
            both += 1
        elif ok_l:
            only_last += 1
            hi = float(row["structure"]["entry_high"])
            if ask > hi:
                sym = row["symbol"]
                if sym not in seen_above:
                    seen_above.add(sym)
                    above_now_buys.append(sym)
        elif ok_z:
            only_zone += 1
        if why_l == "look_wash":
            wash_last += 1
    print()
    print("=" * 78)
    print(f"  SHADOW REPLAY  {path}  samples={n}" + (f"  day={day}" if day else ""))
    print("=" * 78)
    if n == 0:
        print("  no usable rows")
        return
    print(f"  zone would arm: {zone_buy}  ({100 * zone_buy / n:.1f}%)")
    print(f"  last would arm: {last_buy}  ({100 * last_buy / n:.1f}%)")
    print(f"  both={both}  last-only={only_last}  zone-only={only_zone}")
    print(f"  WASH refuses (last): {wash_last}")
    if above_now_buys:
        print(f"  names last buys ABOVE the old band: {', '.join(above_now_buys[:20])}")
    print("  last why:")
    for k, v in last_why.most_common(8):
        print(f"    {v:5d}  {k}")
    print("  zone why:")
    for k, v in zone_why.most_common(8):
        print(f"    {v:5d}  {k}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shadow", action="store_true", help="also replay shadow.jsonl")
    ap.add_argument("--day", default="", help="YYYY-MM-DD filter for shadow")
    args = ap.parse_args()
    print_scenarios()
    if args.shadow or args.day:
        replay_shadow(args.day or None)
    else:
        replay_shadow(None)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
