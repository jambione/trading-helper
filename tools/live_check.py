#!/usr/bin/env python3
"""live_check.py — is today's setup working? Prints a status line, then FLAG lines.

Run on the mini during RTH (every ~15 min). Checks the four pillars plus health:
  book      entry_watch_state.json size >= 10 after 09:45
  price     every fill today inside ai_watch_min_price .. ai_max_price
  runway    fills after 09:47 carry features.rvol_pace_sip (observe gate stamping)
  capture   trades that armed the trail (+arm%) exit near the trail, not far below it
  gates     spread/gap *_unknown refusals are not the bulk of recent refusals
  engine    signal_state.json fresh; no watchdog WEDGED restart in the window
  activity  first fill by 10:30; brake not tripped
Exit 0 always; "FLAG" lines are what needs a human.

USAGE  .venv/bin/python tools/live_check.py [--window-min 15] [--day YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import bars  # noqa: E402
from config import load_config  # noqa: E402

UNKNOWN = ("spread_unknown", "gap_unknown")
GATES = ("spread_wide", "spread_unknown", "gapped_down", "gap_unknown", "engine_stale",
         "rvol_pace_low", "rvol_pace_unknown")


def _j(path, default):
    try:
        with open(os.path.join(ROOT, path), encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def _tail_lines(path, max_bytes=40_000_000):
    p = os.path.join(ROOT, path)
    if not os.path.exists(p):
        return []
    with open(p, "rb") as f:
        size = f.seek(0, 2)
        f.seek(max(0, size - max_bytes))
        return f.read().decode("utf-8", "replace").splitlines()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window-min", type=float, default=15.0)
    ap.add_argument("--day", help="grade another day's fills (clock checks still use now)")
    args = ap.parse_args()
    cfg = load_config()
    now = time.time()
    et = datetime.now(bars.ET)
    day = args.day or et.strftime("%Y-%m-%d")
    mins = et.hour * 60 + et.minute
    win0 = now - args.window_min * 60
    flags: list[str] = []
    notes: list[str] = []
    rth = 9 * 60 + 30 <= mins < 16 * 60

    # ── engine / watchdog ────────────────────────────────────────────────
    try:
        eng_age = now - os.path.getmtime(os.path.join(ROOT, "signal_state.json"))
    except OSError:
        eng_age = None
    if rth and (eng_age is None or eng_age > 90):
        flags.append(f"engine: signal_state.json {'missing' if eng_age is None else f'{eng_age:.0f}s old'}"
                     " — engine stalled or down")
    wd = [l for l in _tail_lines("logs/watchdog.log", 2_000_000)[-400:] if "WEDGED" in l]
    if wd:
        hhmm = [l.split()[1] for l in wd if len(l.split()) > 1]
        recent = [h for h in hhmm if h[:5] >= datetime.fromtimestamp(win0, bars.ET).strftime("%H:%M")
                  and h[:5] <= et.strftime("%H:%M")]
        if recent:
            flags.append(f"engine: watchdog restarted a hung engine at {', '.join(recent)}")

    # ── book ─────────────────────────────────────────────────────────────
    book = _j("ai_reports/entry_watch_state.json", {})
    book = book if isinstance(book, dict) else {}
    n_book = len(book)
    book_px = [r.get("price") for r in book.values() if isinstance(r, dict)]
    if rth and mins >= 9 * 60 + 45 and n_book < 10:
        flags.append(f"book: {n_book} names (want >= 10) — $20-$100 band may be starving supply")

    # ── fills ────────────────────────────────────────────────────────────
    lo = float(cfg.get("ai_watch_min_price") or 0)
    hi = float(cfg.get("ai_max_price") or 0)
    arm_pct = float(cfg.get("ai_local_trail_peak_give_pct") or 0.35)
    closed = []
    for line in _tail_lines("ai_reports/outcomes.jsonl", 5_000_000):
        try:
            r = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        t = r.get("entry_time")
        if isinstance(t, (int, float)) and bars.day_of(t) == day:
            closed.append(r)
    pos = _j("ai_positions_state.json", {})
    open_pos = [p for p in (pos.get("positions") or []) if isinstance(p, dict)]
    realized_r = pos.get("realized_r_today")

    def _px(p):
        for k in ("entry_price", "avg_entry_price", "avg_entry", "fill_price"):
            if p.get(k):
                try:
                    return float(p[k])
                except (TypeError, ValueError):
                    pass
        return None

    for p in closed + open_pos:
        px = _px(p)
        if px is not None and ((lo and px < lo - 1e-9) or (hi and px > hi + 1e-9)):
            flags.append(f"price: {p.get('symbol')} filled at ${px:.2f}, outside ${lo:.0f}-${hi:.0f}")

    obs_on = bool(cfg.get("ai_watch_rvol_pace_observe"))
    no_pace = [r.get("symbol") for r in closed
               if obs_on and bars.et_minutes(r["entry_time"]) >= 9 * 60 + 47
               and (r.get("features") or {}).get("rvol_pace_sip") is None]
    if no_pace:
        flags.append(f"runway: {len(no_pace)} fill(s) after 09:47 with no rvol_pace_sip stamp: "
                     f"{', '.join(no_pace[:8])} — observe gate not stamping")

    # capture: armed trades (peak >= entry*(1+0.3%)) must exit near peak*(1-give)
    arm_trigger = 0.30
    tol = 0.30   # extra % below the trail tolerated for market-sell slippage
    bad_cap = []
    armed = green_armed = 0
    for r in closed:
        e, x, pk = r.get("entry_price"), r.get("exit_price"), r.get("peak_price")
        if not (e and x and pk):
            continue
        if pk < e * (1 + arm_trigger / 100):
            continue
        armed += 1
        green_armed += x >= e
        floor = pk * (1 - (arm_pct + tol) / 100)
        if x < floor:
            bad_cap.append(f"{r.get('symbol')} peak +{(pk / e - 1) * 100:.2f}% exit "
                           f"{(x / e - 1) * 100:+.2f}% ({r.get('close_reason')})")
    for b in bad_cap:
        flags.append(f"capture: trail not honored — {b}")

    # ── gate refusals in the window ──────────────────────────────────────
    gate_c: collections.Counter = collections.Counter()
    pace_obs = 0
    for line in _tail_lines("ai_reports/events.jsonl", 30_000_000):
        if '"ts"' not in line:
            continue
        if not any(g in line for g in GATES) and "rvol_pace_observe" not in line:
            continue
        try:
            e = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        ts = float(e.get("ts") or 0)
        if ts < win0:
            continue
        if e.get("kind") == "rvol_pace_observe" or e.get("event") == "rvol_pace_observe":
            pace_obs += 1
            continue
        for k in ("reason", "why", "block"):
            if e.get(k) in GATES:
                gate_c[e[k]] += 1
                break
    # Arm refusals live on the book record (block_code), not in events.jsonl.
    blocks: collections.Counter = collections.Counter()
    for s, r in book.items():
        if not isinstance(r, dict) or not r.get("block_code"):
            continue
        if float(r.get("block_ts") or 0) >= win0:
            blocks[r["block_code"]] += 1
            if r["block_code"] in GATES:
                gate_c[r["block_code"]] += 1
    unk = sum(gate_c[u] for u in UNKNOWN)
    tot = sum(gate_c.values())
    if rth and mins >= 9 * 60 + 50 and unk >= 20 and unk > 0.5 * tot:
        flags.append(f"gates: {unk}/{tot} refusals in last {args.window_min:.0f}m are *_unknown "
                     f"({dict(gate_c)}) — SIP data missing, not a verdict on names")
    if rth and obs_on and mins >= 9 * 60 + 50 and n_book >= 5 and pace_obs == 0 and tot == 0:
        notes.append("no rvol_pace_observe events in window (fine if nothing reached the arm)")

    # ── activity / brake ─────────────────────────────────────────────────
    if rth and mins >= 10 * 60 + 30 and not closed and not open_pos:
        flags.append("activity: no fills yet after 10:30")
    if isinstance(realized_r, (int, float)) and realized_r <= -float(cfg.get("ai_daily_loss_limit_r") or 3):
        flags.append(f"brake: realized {realized_r:+.2f}R — daily loss brake tripped, no more entries")

    pl = sum(float(r.get("realized_pl_usd") or 0) for r in closed)
    wins = sum(1 for r in closed if float(r.get("realized_pl_usd") or 0) > 0)
    print(f"[live_check {et:%H:%M} ET] book {n_book} | fills {len(closed)} closed + {len(open_pos)} open"
          f" | win {wins}/{len(closed)} | P/L ${pl:+.2f}"
          f" | armed {armed} (green {green_armed}) | book blocks {dict(blocks) or '{}'}"
          f" | engine {('%.0fs' % eng_age) if eng_age is not None else '?'}")
    for n in notes:
        print(f"note: {n}")
    for f in flags:
        print(f"FLAG {f}")
    if not flags:
        print("OK — nothing to flag")


if __name__ == "__main__":
    main()
