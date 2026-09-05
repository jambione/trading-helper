#!/usr/bin/env python3
"""Record a discretionary pick, at the moment it is made.

WHY THIS IS A COMMAND AND NOT A SPREADSHEET
    The entire value of these rows is that the timestamp is written before
    the outcome exists. A list typed up at the end of the day cannot be
    distinguished from a list of the trades that worked, and that is exactly
    the question being tested. So the clock here is the system clock, never
    an argument, and the file is append-only.

WHAT TO RECORD
    Both sides. ``mark_pick.py ABCD`` for a name you would take, and
    ``mark_pick.py ABCD --pass`` for one you looked at and rejected. The
    passes are worth as much as the takes: they hold constant what was on
    the screen, which no synthetic control can do. A take-only log can be
    compared to the pool; a take-and-pass log can be compared to the
    operator's own attention.

    Nothing has to be traded. A pick is a prediction, not an order.

USAGE
    .venv/bin/python tools/mark_pick.py UMAC
    .venv/bin/python tools/mark_pick.py UMAC --note "held the 9:47 high"
    .venv/bin/python tools/mark_pick.py ONDS --pass --note "spread too wide"
    .venv/bin/python tools/mark_pick.py --list

Scored by tools/score_picks.py, against the same bar that failed every
logged feature on 2026-09-05.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

ET = ZoneInfo("America/New_York")
PICKS = os.path.join(ROOT, "ai_reports", "picks.jsonl")
SHADOW = os.path.join(ROOT, "ai_reports", "shadow.jsonl")

# Fields copied off the desk's own view at pick time. Not used to score the
# pick — used afterwards, and only if the picks clear the bar, to ask what
# the operator was seeing that the gate was not.
SNAP = ("price", "exhaustion", "exhaustion_state", "pctr", "pctr_rising",
        "cm_rsi", "cm_rsi_rising", "macd_gap", "macd_gap_rising",
        "macd_sep_ratio", "rvol", "source", "arm_ok", "arm_why",
        "proximity_pct", "spread_r")

# A shadow row older than this is a different moment, not this one. Stale
# features are worse than absent ones: they look like evidence.
FRESH_SEC = 120.0


def _tail(path: str, nbytes: int = 4_000_000) -> list[str]:
    try:
        size = os.path.getsize(path)
    except OSError:
        return []
    with open(path, "rb") as fh:
        if size > nbytes:
            fh.seek(size - nbytes)
            fh.readline()          # drop the partial line
        return fh.read().decode("utf-8", "replace").splitlines()


def snapshot(symbol: str, now: float) -> dict | None:
    """The desk's most recent view of *symbol*, if it is actually recent."""
    best = None
    for line in reversed(_tail(SHADOW)):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if str(r.get("symbol") or "").upper() != symbol:
            continue
        try:
            ts = float(r.get("ts"))
        except (TypeError, ValueError):
            continue
        if now - ts > FRESH_SEC:
            break                  # rows are ordered; older ones are staler
        best = {k: r.get(k) for k in SNAP if r.get(k) is not None}
        best["shadow_age_sec"] = round(now - ts, 1)
        break
    return best


def add(symbol: str, action: str, note: str, conviction: int | None) -> dict:
    now = time.time()
    row = {
        "ts": now,
        "ts_et": datetime.fromtimestamp(now, ET).isoformat(timespec="seconds"),
        "symbol": symbol,
        "action": action,
        "note": note or None,
        "conviction": conviction,
        "features": snapshot(symbol, now),
    }
    os.makedirs(os.path.dirname(PICKS), exist_ok=True)
    with open(PICKS, "a") as fh:
        fh.write(json.dumps(row) + "\n")
    return row


def listing() -> int:
    rows = []
    try:
        for line in open(PICKS):
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    except FileNotFoundError:
        print("no picks yet — " + PICKS)
        return 0
    takes = sum(1 for r in rows if r.get("action") == "take")
    print(f"{len(rows)} rows  ({takes} take / {len(rows) - takes} pass)  {PICKS}\n")
    for r in rows[-40:]:
        feat = r.get("features") or {}
        bits = []
        if feat.get("exhaustion") is not None:
            bits.append(f"EXH {float(feat['exhaustion']):.0f}")
        if feat.get("cm_rsi") is not None:
            bits.append(f"RSI {float(feat['cm_rsi']):.0f}")
        if feat.get("price") is not None:
            bits.append(f"${float(feat['price']):.2f}")
        print("  %s  %-5s %-6s %-22s %s"
              % (str(r.get("ts_et"))[:19], r.get("action"), r.get("symbol"),
                 " ".join(bits) or "(no desk view)", r.get("note") or ""))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("symbol", nargs="?", help="ticker you are calling")
    ap.add_argument("--pass", dest="passed", action="store_true",
                    help="you looked at it and would NOT take it")
    ap.add_argument("--note", default="", help="what you saw, in your words")
    ap.add_argument("--conviction", type=int, choices=(1, 2, 3),
                    help="1 marginal .. 3 strong; optional, scored separately")
    ap.add_argument("--list", action="store_true", help="show what is logged")
    a = ap.parse_args()

    if a.list:
        return listing()
    if not a.symbol:
        ap.error("give a ticker, or --list")
    sym = a.symbol.upper().strip()
    if not sym.replace(".", "").isalnum() or len(sym) > 8:
        ap.error(f"{sym!r} does not look like a ticker")

    row = add(sym, "pass" if a.passed else "take", a.note, a.conviction)
    feat = row.get("features")
    seen = ("desk view attached" if feat
            else "no desk view (name not on the book, or run from the wrong host)")
    print(f"{row['action'].upper():5s} {sym}  {row['ts_et']}  — {seen}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
