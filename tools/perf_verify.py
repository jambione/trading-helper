#!/usr/bin/env python3
"""Are the six audit remedies actually in force, right now?

HANDOFF §9B found one dead field (`price_age_sec`) disabling three
staleness guards, a 0.25s ratchet reading a 1.5s cache, two realtime guards
that had blocked nothing, an undeclared shelf tick, a 20s arm gate fed by a
2s sync, and setup legs that select nothing.

Config can be edited and processes can be stale, so none of that stays
fixed by having been fixed once. This asserts each remedy against the LIVE
system and says PASS / FAIL / PENDING — never guessing, and never calling
something PASS because it could not be checked.

Run it after any restart, and again once RTH is under way: several checks
are legitimately PENDING before 09:30, because premarket the desk's price
IS yesterday's close (§5G2) and every age is correctly enormous.

Read-only. Usage:
    .venv/bin/python tools/perf_verify.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

ET = ZoneInfo("America/New_York")
_rows: list[tuple[str, str, str]] = []


def check(name: str, verdict: str, detail: str = "") -> None:
    _rows.append((verdict, name, detail))


def rth_now() -> bool:
    n = datetime.now(ET)
    if n.weekday() >= 5:
        return False
    m = n.hour * 60 + n.minute
    return 9 * 60 + 30 <= m < 16 * 60


def main() -> int:
    now_et = datetime.now(ET)
    print(f"perf_verify — {now_et:%Y-%m-%d %H:%M:%S} ET  "
          f"({'RTH' if rth_now() else 'outside RTH'})\n")

    # ── processes ────────────────────────────────────────────────────────
    try:
        ps = subprocess.run(["ps", "-A", "-o", "args="],
                            capture_output=True, text=True).stdout
    except Exception:  # noqa: BLE001
        ps = ""
    for proc in ("dashboard.py", "ai_trader.py", "signal_engine.py",
                 "tools/watchdog.py"):
        alive = any(proc in ln and "grep" not in ln for ln in ps.splitlines())
        check(f"process {proc}", "PASS" if alive else "FAIL",
              "" if alive else "not running — desk is unsupervised")

    # ── config remedies (3,4,5,6) ────────────────────────────────────────
    try:
        from config import load_config
        cfg = load_config()
    except Exception as e:  # noqa: BLE001
        cfg = {}
        check("load_config", "FAIL", f"{type(e).__name__}: {e}")

    shelf = cfg.get("ai_shelf_tick_sec")
    book = cfg.get("ai_book_tick_sec")
    raw_declared = False
    try:
        import json
        raw = json.load(open(os.path.join(ROOT, "config", "bot_config.json")))
        raw_declared = "ai_shelf_tick_sec" in raw
    except Exception:  # noqa: BLE001
        pass
    # A shelf tick >= the book tick folds the shelf INTO the book tick, which
    # is the silent 4x slowdown remedy 3 exists to prevent.
    ok3 = raw_declared and shelf and book and 0 < float(shelf) < float(book)
    check("R3 shelf tick declared and faster than book tick",
          "PASS" if ok3 else "FAIL",
          f"shelf={shelf} book={book} declared_in_bot_config={raw_declared}")

    # R4 guards THE LEVER — whichever indicator currently opens positions.
    #
    # It used to check require_live_pctr and require_realtime_rsi, and by 8/27
    # it was failing on a system that was not broken: MACD replaced both on
    # 8/26 (ai_watch_arm_require_macd), CM RSI-2 is no longer an arm gate at
    # all (ai_watch_arm_require_cm_rsi=False), so its freshness knob guards
    # nothing, and %R now decides only inside the MACD/EXH confluence
    # override. A check that fails for a stale reason trains you to ignore it.
    rt_macd = bool(cfg.get("ai_watch_require_realtime_macd"))
    arm_macd = bool(cfg.get("ai_watch_arm_require_macd"))
    if not arm_macd:
        check("R4 lever freshness guard armed", "FAIL",
              "ai_watch_arm_require_macd=False — no lever to guard")
    else:
        check("R4 lever freshness guard armed", "PASS" if rt_macd else "FAIL",
              f"lever=MACD require_realtime_macd={rt_macd}"
              f" (cm_rsi arm gate off, so its knob is inert)")

    # The override is the most permissive path in the entry gate: it returns
    # an automatic yes and skips both macd_min_gap and the separation test.
    # Its %R leg is read straight off the record, so it does NOT pass through
    # exhaustion_allows_buy's require_live_pctr provenance check. Reported,
    # not enforced — the override is the operator's rule and tightening it
    # changes what trades.
    if bool(cfg.get("ai_watch_macd_exh_override")):
        check("R4b override EXH provenance", "WARN",
              "confluence override reads pctr_rising directly; "
              "require_live_pctr does not cover it")

    poll = float(cfg.get("ai_watch_poll_sec") or 0)
    check("R5 arm cadence tightened", "PASS" if 0 < poll <= 10.0 else "FAIL",
          f"ai_watch_poll_sec={poll}")

    try:
        import watchdog as wd
        news_iv = float(getattr(wd, "NEWS_INTERVAL_SEC", 0))
    except Exception:  # noqa: BLE001
        news_iv = 0.0
    check("R6 catalyst cache cadence", "PASS" if 0 < news_iv <= 120 else "FAIL",
          f"NEWS_INTERVAL_SEC={news_iv}")

    # ── R1: price_age_sec populated, and the guards keying off it ────────
    try:
        import ai_entry_watch as ew
        state = ew.dashboard_state(force=True)
        tick = state.get("tickers") or []
        aged = [r for r in tick if r.get("price_age_sec") is not None]
        if not tick:
            check("R1a price_age_sec populated", "PENDING",
                  "no tickers on the wire yet")
        else:
            frac = len(aged) / len(tick)
            check("R1a price_age_sec populated",
                  "PASS" if frac >= 0.8 else "FAIL",
                  f"{len(aged)}/{len(tick)} tickers carry an age")
            if aged:
                ages = sorted(float(r["price_age_sec"]) for r in aged)
                med = ages[len(ages) // 2]
                if rth_now():
                    check("R1c ages are live in RTH",
                          "PASS" if med <= 60 else "FAIL",
                          f"median price_age_sec={med:.1f}s")
                else:
                    check("R1c ages are live in RTH", "PENDING",
                          f"median={med:.0f}s — premarket this SHOULD be "
                          f"large; §5G2 says the price is yesterday's close")
    except Exception as e:  # noqa: BLE001
        check("R1a price_age_sec populated", "FAIL", f"{type(e).__name__}: {e}")

    # The guards themselves, exercised directly rather than inferred.
    try:
        import ai_entry_watch as ew
        import ai_positions as cp
        unknown = ew._row_tape_stale({"last_ask_src": "rest",
                                      "last_ask_age_sec": None})
        fresh = ew._row_tape_stale({"last_ask_src": "rest",
                                    "last_ask_age_sec": 1.0})
        old = ew._row_tape_stale({"last_ask_src": "rest",
                                  "last_ask_age_sec": 9999.0})
        ok = (unknown is True and fresh is False and old is True)
        check("R1b arm guard fails closed on unknown age",
              "PASS" if ok else "FAIL",
              f"unknown->{unknown} fresh->{fresh} old->{old}")
        src = open(os.path.join(ROOT, "ai_positions.py"),
                   encoding="utf-8").read()
        i = src.index("def _fresh_tape_px")
        body = src[i:i + 1600]
        shelf_closed = "if age is None:" in body and "return None" in body
        check("R1b shelf guard fails closed on unknown age",
              "PASS" if shelf_closed else "FAIL",
              "unknown age must skip the pass, not accept the price")
        assert cp is not None
    except Exception as e:  # noqa: BLE001
        check("R1b guards fail closed", "FAIL", f"{type(e).__name__}: {e}")

    # ── R2 evidence: is the shelf's price actually the faster feed? ───────
    try:
        import shadow_report as sr
        rows = sr.load()
        recent = [r for r in rows
                  if r.get("ts") and time.time() - float(r["ts"]) < 900]
        withcols = [r for r in recent if "ind_snapshot_age_sec" in r]
        if not recent:
            check("R2 instrumentation present", "PENDING", "no recent rows")
        elif not withcols:
            check("R2 instrumentation present", "FAIL",
                  "ai_trader has not picked up the freshness columns")
        else:
            snaps = [float(r["ind_snapshot_age_sec"]) for r in withcols
                     if r.get("ind_snapshot_age_sec") is not None]
            bars = [float(r["bars_age_sec"]) for r in withcols
                    if r.get("bars_age_sec") is not None]
            check("R2 instrumentation present", "PASS",
                  f"{len(withcols)}/{len(recent)} recent rows carry both ages")
            if snaps:
                s = sorted(snaps)
                check("R2 transport age measured", "PASS",
                      f"ind_snapshot_age_sec median "
                      f"{s[len(s)//2]:.2f}s (cache TTL bounds this)")
            if bars:
                b = sorted(bars)
                check("R2 engine tape age measured", "PASS",
                      f"bars_age_sec median {b[len(b)//2]:.2f}s")
    except Exception as e:  # noqa: BLE001
        check("R2 instrumentation present", "FAIL", f"{type(e).__name__}: {e}")

    # ── report ───────────────────────────────────────────────────────────
    order = {"FAIL": 0, "WARN": 1, "PENDING": 2, "PASS": 3}
    for verdict, name, detail in sorted(_rows, key=lambda r: order.get(r[0], 4)):
        mark = {"PASS": "OK  ", "FAIL": "FAIL",
                "WARN": "WARN", "PENDING": "...."}.get(verdict, "????")
        print(f"  {mark} {name}")
        if detail:
            print(f"         {detail}")
    fails = sum(1 for v, _, _ in _rows if v == "FAIL")
    warns = sum(1 for v, _, _ in _rows if v == "WARN")
    pend = sum(1 for v, _, _ in _rows if v == "PENDING")
    print(f"\n  {len(_rows)} checks — {fails} FAIL, {warns} WARN, {pend} PENDING")
    if pend and not rth_now():
        print("  Re-run after 09:30: the PENDING checks need a live session.")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
