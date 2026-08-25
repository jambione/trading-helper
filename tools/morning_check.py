#!/usr/bin/env python3
"""Did today's tape actually record what the experiments need?

Every defect found on 2026-08-23/24 was invisible until someone read the
output: learn jobs dying at interpreter startup once a Terminal closed, a
volume column reading a key no live row carries, an audit that could not
see the watchdog because pgrep hides its own ancestors, a --max-shares-m
flag accepted and ignored. None of them raised. All of them logged
cleanly. Each was found by looking.

So this is the looking, as one command. Two experiments run from
2026-08-24 and both are silent failures if their fields do not land:

  GATE 1   the min-hold exit test. Its P&L is NOT the signal for a single
           session -- the base rate is 3/13 green, so either outcome is
           consistent with the null. What matters is whether the delay
           BOUND: no discretionary exit under the hold floor, and
           min_hold_blocks above zero somewhere. Zero blocks everywhere
           means the delay never applied and the session is vacuous.

  SETUP    the operator's five-leg conjunction, plus the stage-2 timing
           state. It fires on ~5% of name-days, so ZERO setups today is a
           normal outcome and not a failure. What would be a failure is
           the columns never populating.

The verdicts are deliberately three-valued. A check that cannot run yet
reports PENDING, never PASS: pre-market rvol is legitimately absent, and
letting "too early to tell" read as healthy is how a broken instrument
survives a week.

Read-only. Exit 1 if anything FAILs.

    .venv/bin/python tools/morning_check.py
    .venv/bin/python tools/morning_check.py --day 2026-08-24
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

ET = timezone(timedelta(hours=-4))          # August is EDT
DISCRETIONARY = {"local_trail", "dead_trade", "left_overbought"}

RESULTS: list[tuple[str, str, str]] = []    # (status, name, detail)


def record(status: str, name: str, detail: str = "") -> None:
    RESULTS.append((status, name, detail))


def _et(ts: float) -> datetime:
    return datetime.fromtimestamp(float(ts), ET)


def _rows(fname: str, day: str):
    """Today's rows from a jsonl log, oldest first."""
    try:
        from ai_paths import resolve_report_dir
        p = Path(resolve_report_dir()) / fname
    except Exception:  # noqa: BLE001
        p = ROOT / "ai_reports" / fname
    if not p.exists():
        return
    with p.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            ts = r.get("ts") or r.get("entry_time")
            if not ts:
                continue
            try:
                if _et(float(ts)).strftime("%Y-%m-%d") != day:
                    continue
            except (TypeError, ValueError, OSError):
                continue
            yield r


# ------------------------------------------------------------------ GATE 1

def check_min_hold(day: str, min_hold: float) -> None:
    outs = [r for r in _rows("outcomes.jsonl", day)
            if r.get("realized_r_multiple") is not None]
    if not outs:
        record("PENDING", "min-hold respected", "no closed trades yet")
    else:
        bad = [r for r in outs
               if str(r.get("close_reason")) in DISCRETIONARY
               and r.get("hold_sec") is not None
               and float(r["hold_sec"]) < min_hold]
        if bad:
            ex = ", ".join(f"{r.get('symbol')} {r.get('close_reason')} "
                           f"{float(r['hold_sec']):.0f}s" for r in bad[:4])
            record("FAIL", "min-hold respected",
                   f"{len(bad)} discretionary exit(s) under {min_hold:.0f}s: {ex}")
        else:
            holds = [float(r["hold_sec"]) for r in outs
                     if r.get("hold_sec") is not None]
            med = statistics.median(holds) if holds else 0.0
            record("PASS", "min-hold respected",
                   f"{len(outs)} trades, median hold {med:.0f}s")

    # Did the delay actually BIND? This is the mechanism, not the P&L.
    blocks = 0
    for r in _rows("position_shadow.jsonl", day):
        try:
            blocks = max(blocks, int(r.get("min_hold_blocks") or 0))
        except (TypeError, ValueError):
            continue
    for r in outs:
        try:
            blocks = max(blocks, int(r.get("min_hold_blocks") or 0))
        except (TypeError, ValueError):
            continue
    ticks = sum(1 for _ in _rows("position_shadow.jsonl", day))
    if not ticks:
        record("PENDING", "min-hold bound", "no positions held yet")
    elif blocks > 0:
        record("PASS", "min-hold bound",
               f"max {blocks} suppressed exit(s) on a single position")
    else:
        record("WARN", "min-hold bound",
               f"{ticks} position ticks, zero suppressed exits — the delay "
               "never applied, so today adds nothing to GATE 1")


def check_ratchet_logged(day: str) -> None:
    ticks = list(_rows("position_shadow.jsonl", day))
    if not ticks:
        record("PENDING", "ratchet logged", "no positions held yet")
        return
    have = sum(1 for r in ticks if r.get("local_stop_price") is not None)
    give = sum(1 for r in ticks if r.get("give_r") is not None)
    pct = 100.0 * have / len(ticks)
    detail = (f"{len(ticks)} ticks, shelf on {pct:.0f}%, give_r on "
              f"{100.0 * give / len(ticks):.0f}%")
    # A fresh fill legitimately has no shelf yet, so this is not 100%.
    record("PASS" if pct >= 50 else "FAIL", "ratchet logged", detail)


# ------------------------------------------------------------------ SETUP

def check_rvol(day: str, rows: list[dict], now: datetime) -> None:
    if now.hour < 10:
        record("PENDING", "rvol resolving",
               "before 10:00 ET — the producer publishes rvol=None until "
               "its volume refresh resolves")
        return
    late = [r for r in rows if _et(r["ts"]).hour >= 10]
    if not late:
        record("PENDING", "rvol resolving", "no rows after 10:00 yet")
        return
    have = sum(1 for r in late if r.get("rvol") is not None)
    pct = 100.0 * have / len(late)
    sane = sum(1 for r in late if r.get("rvol_ok") is True)
    detail = (f"{pct:.0f}% of {len(late)} post-10:00 rows "
              f"({100.0 * sane / max(len(late), 1):.0f}% sane)")
    record("PASS" if pct >= 50 else "FAIL", "rvol resolving", detail)


def check_float(day: str, rows: list[dict], now: datetime) -> None:
    syms = {}
    for r in rows:
        s = r.get("symbol")
        if s:
            syms.setdefault(str(s).upper(), r.get("shares_out_m"))
            if syms[str(s).upper()] is None:
                syms[str(s).upper()] = r.get("shares_out_m")
    if not syms:
        record("PENDING", "float landing", "no rows yet")
        return
    have = sum(1 for v in syms.values() if v is not None)
    pct = 100.0 * have / len(syms)
    missing = sorted(k for k, v in syms.items() if v is None)[:6]
    detail = f"{have}/{len(syms)} symbols have a share count"
    if missing:
        detail += f"; waiting on {', '.join(missing)}"
    if now.hour < 10:
        # 10 symbols per 5-minute pass, and the watchlist churns daily.
        record("PENDING" if pct < 50 else "PASS", "float landing", detail)
    else:
        record("PASS" if pct >= 50 else "FAIL", "float landing", detail)


def check_setup_fields(day: str, rows: list[dict]) -> None:
    if not rows:
        record("PENDING", "setup fields present", "no rows yet")
        return
    need = ("setup_ok", "setup_legs", "setup_n_legs", "shares_out_m",
            "news_n_24h", "rvol_ok", "vol_session",
            "pctr_both_rising", "pctr_diverging",
            "setup_entry_ok", "setup_exit_ok")
    absent = [k for k in need if not any(k in r for r in rows[-200:])]
    if absent:
        record("FAIL", "setup fields present",
               f"missing from the row entirely: {', '.join(absent)} — the "
               "running trader predates the logging")
    else:
        record("PASS", "setup fields present", f"all {len(need)} columns emitted")


def check_setup_consistency(day: str, rows: list[dict]) -> None:
    hits = [r for r in rows if r.get("setup_ok") is True]
    bad = [r for r in hits
           if len(str(r.get("setup_legs") or "").split(",")) != 5]
    if bad:
        record("FAIL", "setup legs consistent",
               f"{len(bad)} row(s) marked ok without all five legs")
    elif hits:
        record("PASS", "setup legs consistent",
               f"{len(hits)} qualifying row(s), all five legs each")
    else:
        record("PASS", "setup legs consistent",
               "no qualifying rows yet — expected, it fires on ~5% of name-days")


def check_stage2(day: str, rows: list[dict]) -> None:
    if not rows:
        record("PENDING", "stage-2 timing", "no rows yet")
        return
    have = sum(1 for r in rows if r.get("pctr_both_rising") is not None)
    pct = 100.0 * have / len(rows)
    # pctr_slow historically reaches 31-51%; this is a producer limit.
    record("PASS" if pct >= 20 else "WARN", "stage-2 timing",
           f"both-lines state on {pct:.0f}% of rows (pctr_slow coverage)")


# ------------------------------------------------------------------ report

def summarise(rows: list[dict]) -> None:
    if not rows:
        return
    print("\n--- setup detail -------------------------------------------")
    best: dict[str, tuple[int, str, dict]] = {}
    for r in rows:
        s = str(r.get("symbol") or "")
        n = r.get("setup_n_legs")
        if not s or n is None:
            continue
        cur = best.get(s)
        if cur is None or int(n) > cur[0]:
            best[s] = (int(n), str(r.get("setup_legs") or ""), r)
    if not best:
        print("  no rows carry setup legs yet")
        return
    dist = Counter(v[0] for v in best.values())
    print("  legs met, by symbol:  " + "  ".join(
        f"{k}/5:{dist[k]}" for k in sorted(dist, reverse=True)))
    near = sorted(((v[0], s, v[1], v[2]) for s, v in best.items()),
                  key=lambda t: -t[0])[:8]
    print(f"  {'sym':<7}{'legs':>5}  {'missing':<22}{'pct':>8}{'rvol':>8}"
          f"{'shares':>9}")
    allk = {"up", "rvol", "price", "news", "float"}
    for n, s, legs, r in near:
        miss = ",".join(sorted(allk - set(legs.split(",")))) or "-"
        print(f"  {s:<7}{n:>5}  {miss:<22}"
              f"{(r.get('pct_change') or 0):>8.1f}"
              f"{(r.get('rvol') if r.get('rvol') is not None else float('nan')):>8.1f}"
              f"{(r.get('shares_out_m') if r.get('shares_out_m') is not None else float('nan')):>9.1f}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--day", default=None, help="YYYY-MM-DD, default today ET")
    args = ap.parse_args()
    now = datetime.now(ET)
    day = args.day or now.strftime("%Y-%m-%d")

    try:
        from config import load_config
        cfg = load_config() or {}
    except Exception:  # noqa: BLE001
        cfg = {}
    min_hold = float(cfg.get("ai_exit_min_hold_sec", 0) or 0)
    product = cfg.get("desk_product")

    print(f"morning check — {day}  (now {now:%H:%M} ET)")
    print(f"  desk_product={product!r}  ai_exit_min_hold_sec={min_hold:g}")

    rows = [r for r in _rows("shadow.jsonl", day) if r.get("ts")]
    print(f"  shadow rows today: {len(rows)}")

    if min_hold <= 0:
        record("WARN", "GATE 1 armed",
               "ai_exit_min_hold_sec is 0 — the exit test is NOT running")
    else:
        record("PASS", "GATE 1 armed", f"min hold {min_hold:g}s")

    check_min_hold(day, min_hold)
    check_ratchet_logged(day)
    check_rvol(day, rows, now)
    check_float(day, rows, now)
    check_setup_fields(day, rows)
    check_setup_consistency(day, rows)
    check_stage2(day, rows)

    print("\n--- checks -------------------------------------------------")
    order = {"FAIL": 0, "WARN": 1, "PENDING": 2, "PASS": 3}
    for status, name, detail in sorted(RESULTS, key=lambda t: order[t[0]]):
        print(f"  {status:<8}{name:<24}{detail}")

    summarise(rows)

    fails = [r for r in RESULTS if r[0] == "FAIL"]
    warns = [r for r in RESULTS if r[0] == "WARN"]
    print("\n" + "=" * 60)
    if fails:
        print(f"  {len(fails)} FAIL — a column the experiments need is not landing")
    elif warns:
        print(f"  {len(warns)} warning(s), no failures")
    else:
        print("  clean")
    print("  P&L is not a verdict on one session. Read eod.py for CAPTURE;")
    print("  GATE 1 needs ten sessions before it says anything.")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
