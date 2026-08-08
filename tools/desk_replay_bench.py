#!/usr/bin/env python3
"""
desk_replay_bench.py — replay the desk's entry/exit rules over the prices it
actually observed, and A/B the runner-exit model on the same paths.

WHERE THE PRICES COME FROM
    events.jsonl `watch_skip` rows carry {symbol, ts, reason, ask} — a real
    quote the desk really saw. That is the only intraday price series on this
    box: Alpaca returns 401 for both data and trading, and rs_cache.sqlite is
    daily bars with no `open`, which cannot sequence an intraday scalp.

WHAT THE `reason` FIELD PROVES
    should_arm_buy checks indicators BEFORE zone membership, and only
    (wait_setup, hard_no, spread, above_zone, below_zone, reward_risk,
    no_structure) are logged as watch_skip. So a tick recorded `above_zone` or
    `below_zone` is PROOF the indicator gate passed at that moment — price was
    the only thing blocking. Those ticks are replayed as indicator-clear; no
    others are.

HONESTY LIMITS — read these before quoting any number
  • The series is not a tape. It samples only ticks where the name was on the
    book AND blocked, so it systematically UNDER-samples the moments price was
    inside the zone — the exact moments an entry happens.
  • One session (2026-08-05), which predates the double-bottom zone work
    (landed 2026-08-08). Every zone the desk drew that day was the offset
    fallback. The double-bottom path here is a RECONSTRUCTION: the real
    detector run over observed asks, which normally reads 1-minute bar LOWS.
  • Entry price = the observed ask. Exit price = the observed ask too, which
    flatters every exit — a real sell hits the bid.
  • Structure window is the first --split fraction of each series; the forward
    walk uses only later ticks, so no look-ahead.

USAGE
    venv/bin/python tools/desk_replay_bench.py
    venv/bin/python tools/desk_replay_bench.py --n 10 --json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ai_entry_watch as ew
from config import load_config

ET = ZoneInfo("America/New_York")
# Ticks proving the indicator gate passed (they are evaluated after it).
PRICE_ONLY_BLOCKS = {"above_zone", "below_zone"}
# Test fixtures have leaked into the live events file before (see ai_paths.py);
# these are the seeded values from tests/test_ai_positions.py.
FIXTURE = {("NVDA", 40.5), ("NVDA", 46.0), ("NVDA", 38.0)}


def load_series(path: Path) -> dict[tuple[str, str], list[tuple]]:
    out: dict[tuple[str, str], list[tuple]] = defaultdict(list)
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("kind") != "watch_skip":
            continue
        sym, ts, ask = d.get("symbol"), d.get("ts"), d.get("ask")
        if not (sym and ts and ask):
            continue
        if (sym, float(ask)) in FIXTURE:
            continue
        day = datetime.fromtimestamp(ts, ET).date().isoformat()
        out[(sym, day)].append((float(ts), float(ask), str(d.get("reason") or "")))
    for series in out.values():
        series.sort()
    return out


def et_minutes(ts: float) -> float:
    dt = datetime.fromtimestamp(ts, ET)
    return dt.hour * 60 + dt.minute


def walk_exits(entry, stop, target, path, cfg, *, eod_min, dead_min, dead_mfe):
    """Return {model: (exit_px, reason, blended_R)} for OLD and NEW runners.

    Both models share tranche A (target or stop) so only the runner differs.
    scale_out is read from config; qty cancels out of a blended R.
    """
    R = entry - stop
    scale = float(cfg.get("ai_watch_synth_scale_out_pct", 50.0)) / 100.0
    trail_pct = float(cfg.get("ai_watch_synth_trail_pct", 2.5)) / 100.0
    trail_r = float(cfg.get("ai_runner_trail_r", 1.0))

    a_done = False
    a_r = None
    peak = entry
    old_stop = new_stop = stop
    out: dict[str, tuple] = {}
    mfe_r = 0.0

    for ts, px, _reason in path:
        peak = max(peak, px)
        mfe_r = max(mfe_r, (peak - entry) / R)

        if not a_done:
            if px <= stop:                       # both tranches die together
                return {m: (stop, "stopped_out", -1.0) for m in ("old", "new")}
            if px >= target:
                a_done = True
                a_r = (target - entry) / R
                old_stop = peak * (1.0 - trail_pct)      # fixed-% trail
                new_stop = max(entry, peak - trail_r * R)  # R, breakeven floor
                continue
            if (
                dead_min > 0
                and (ts - path[0][0]) / 60.0 >= dead_min
                and mfe_r < dead_mfe
                and px <= entry * 1.001
            ):
                r = (px - entry) / R
                return {m: (px, "dead_trade", r) for m in ("old", "new")}
            if et_minutes(ts) >= eod_min:
                r = (px - entry) / R
                return {m: (px, "eod_flatten", r) for m in ("old", "new")}
            continue

        # Runner phase — ratchet each model, then test it.
        old_stop = max(old_stop, peak * (1.0 - trail_pct))
        new_stop = max(new_stop, entry, peak - trail_r * R)
        for name, lvl in (("old", old_stop), ("new", new_stop)):
            if name in out:
                continue
            if px <= lvl:
                r_run = (lvl - entry) / R
                out[name] = (lvl, "trailed_out",
                             scale * a_r + (1 - scale) * r_run)
        if len(out) == 2:
            return out
        if et_minutes(ts) >= eod_min:
            for name in ("old", "new"):
                out.setdefault(name, (px, "eod_flatten",
                                      scale * a_r + (1 - scale) * (px - entry) / R))
            return out

    last = path[-1][1] if path else entry
    for name in ("old", "new"):
        if name not in out:
            r_run = (last - entry) / R
            out[name] = (last, "path_end",
                         (scale * a_r + (1 - scale) * r_run) if a_done
                         else (last - entry) / R)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--events", default=str(ROOT / "claude_reports" / "events.jsonl"))
    ap.add_argument("--n", type=int, default=10, help="symbols to replay")
    ap.add_argument("--split", type=float, default=0.4,
                    help="fraction of each series used to build structure")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    ev = Path(args.events)
    if not ev.exists():
        print(f"no events file at {ev}", file=sys.stderr)
        return 1

    cfg = load_config() or {}
    eod = str(cfg.get("ai_eod_liquidate_time", "15:50")).split(":")
    eod_min = int(eod[0]) * 60 + int(eod[1])
    dead_min = float(cfg.get("ai_dead_trade_min", 90.0))
    dead_mfe = float(cfg.get("ai_dead_trade_mfe_r", 0.25))

    series = load_series(ev)
    ranked = sorted(series.items(), key=lambda kv: -len(kv[1]))[:args.n]

    rows = []
    for (sym, day), obs in ranked:
        cut = max(8, int(len(obs) * args.split))
        window, forward = obs[:cut], obs[cut:]
        rec: dict = {"symbol": sym, "day": day, "obs": len(obs),
                     "fwd": len(forward)}

        # 1. Does the REAL detector find a shelf in the observed prices?
        db = ew.find_double_bottom_support(
            [p for _, p, _ in window],
            swing=int(cfg.get("ai_watch_db_swing_bars", 2)),
            match_pct=float(cfg.get("ai_watch_db_match_pct", 0.4)),
            min_sep_bars=int(cfg.get("ai_watch_db_min_sep_bars", 3)),
        )
        rec["shelf"] = round(db["support"], 4) if db else None
        anchor = window[-1][1]

        zone = None
        if db:
            zone = ew.build_double_bottom_zone_structure(
                db["support"], cfg, low_a=db["low_a"], low_b=db["low_b"],
                last_price=anchor)
        rec["zone"] = ([zone["entry_low"], zone["entry_high"]] if zone else None)
        if zone is None:
            rec["result"] = "no_db_zone"       # P1a: refused; old code: offset
            rows.append(rec)
            continue

        # 2. Replay the real arm gate forward. Only ticks whose recorded reason
        #    proves the indicator gate passed are eligible.
        bull = {"cm_ok": True, "pctr_ok": True, "cm_rsi_rising": True,
                "sell_signal": False, "proximity_pct": 100.0}
        watch = {"symbol": sym, "status": "watching", "structure": zone,
                 "indicator": bull}
        entry_i = None
        for i, (ts, px, reason) in enumerate(forward):
            if reason not in PRICE_ONLY_BLOCKS:
                continue
            ok, _why = ew.should_arm_buy(watch, ask=px, bid=None, cfg=cfg)
            if ok:
                entry_i = i
                break
        if entry_i is None:
            rec["result"] = "zone_never_reached"
            rows.append(rec)
            continue

        entry = forward[entry_i][1]
        dec = ew._decision_for_place(zone, ask=entry, cfg=cfg)
        stop, target = float(dec["stop_price"]), float(dec["target_1"])
        if stop <= 0 or stop >= entry or target <= entry:
            rec["result"] = "bad_levels"
            rows.append(rec)
            continue

        res = walk_exits(entry, stop, target, forward[entry_i + 1:], cfg,
                         eod_min=eod_min, dead_min=dead_min, dead_mfe=dead_mfe)
        rec.update({
            "result": "entered",
            "entry": round(entry, 4),
            "stop": round(stop, 4),
            "target": round(target, 4),
            "stop_width_pct": round(100 * (entry - stop) / entry, 2),
            "old_R": round(res["old"][2], 3), "old_reason": res["old"][1],
            "new_R": round(res["new"][2], 3), "new_reason": res["new"][1],
        })
        rows.append(rec)

    if args.json:
        print(json.dumps(rows, indent=2))
        return 0

    print(f"Replay of {len(rows)} symbol-days — prices the desk really observed")
    print(f"config: rr={cfg.get('ai_watch_synth_rr')} "
          f"scale_out={cfg.get('ai_watch_synth_scale_out_pct')}% "
          f"old_trail={cfg.get('ai_watch_synth_trail_pct')}% "
          f"new_trail={cfg.get('ai_runner_trail_r')}R\n")
    hdr = (f"{'sym':6}{'obs':>5}{'shelf':>9}{'zone':>18}{'result':>20}"
           f"{'stop%':>7}{'oldR':>7}{'newR':>7}  exit")
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        zone = f"{r['zone'][0]:.2f}-{r['zone'][1]:.2f}" if r.get("zone") else "-"
        shelf = f"{r['shelf']:.2f}" if r.get("shelf") else "-"
        width = f"{r['stop_width_pct']:.2f}" if "stop_width_pct" in r else "-"
        old_r = f"{r['old_R']:+.2f}" if "old_R" in r else "-"
        new_r = f"{r['new_R']:+.2f}" if "new_R" in r else "-"
        print(f"{r['symbol']:6}{r['obs']:>5}{shelf:>9}{zone:>18}"
              f"{r['result']:>20}{width:>7}{old_r:>7}{new_r:>7}"
              f"  {r.get('new_reason', '')}")

    ent = [r for r in rows if r["result"] == "entered"]
    print(f"\n{'':-<40}")
    print(f"no double-bottom zone found : {sum(1 for r in rows if r['result'] == 'no_db_zone')}/{len(rows)}")
    print(f"zone drawn but never reached: {sum(1 for r in rows if r['result'] == 'zone_never_reached')}/{len(rows)}")
    print(f"entered                     : {len(ent)}/{len(rows)}")
    if ent:
        o = sum(r["old_R"] for r in ent) / len(ent)
        n = sum(r["new_R"] for r in ent) / len(ent)
        print(f"\nmean R  old (2.5% trail) {o:+.3f}   new (1R + breakeven floor) {n:+.3f}"
              f"   delta {n - o:+.3f}")
        diff = [r for r in ent if abs(r["new_R"] - r["old_R"]) > 1e-9]
        print(f"trades where the two models differ: {len(diff)}/{len(ent)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
