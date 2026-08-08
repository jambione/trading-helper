#!/usr/bin/env python3
"""
signal_report.py — are the Discord-side signals worth acting on?

The desk has three signal producers reading the Discord window: Bullish Bob's
call-outs (the header's "Suggests:" chip), the scanner's price-spike alerts, and
mention bursts. Until signal_shadow.jsonl existed, none of them reached
shadow.jsonl or rejects.jsonl — trading candidates come from `trending` and
`momentum`, and the Discord path fed only the watchlist and the UI. So the
question the Suggests chip invites every time it lights up ("is this call worth
taking?") had no data behind it at all.

This turns the counterfactual price track into that answer:

  FORWARD RETURN   per signal type, the median and hit rate at +5/+15/+30m
                   measured from the price at signal time.

  COVERAGE         how many signals could be measured at all. Call-outs are
                   deliberately kept out of the watchlist, so some never get a
                   quote — an unmeasurable signal is reported, not dropped,
                   because silent absence is how a dead feed looks healthy.

Read it as evidence, not as a verdict: a handful of call-outs on one session is
a sample size of a handful, and the header prints n so it cannot be forgotten.

USAGE
    venv/bin/python tools/signal_report.py
    venv/bin/python tools/signal_report.py --day 2026-08-11
    venv/bin/python tools/signal_report.py --signal bb_live --horizons 5,15,30
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai_paths import find_report_file, resolve_report_dir  # noqa: E402

SIGNAL_NAMES = ("bb_live", "price_spike", "mention_burst")


def _path() -> Path:
    return find_report_file("signal_shadow.jsonl") or (
        resolve_report_dir() / "signal_shadow.jsonl")


def load_samples(day: date | None) -> list[dict]:
    """Rows for `day` (ET-naive UTC date on the row's own stamp), or all rows."""
    path = _path()
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if day is not None:
            ts = r.get("ts")
            if not ts:
                continue
            if datetime.fromtimestamp(ts, timezone.utc).date() != day:
                continue
        out.append(r)
    return out


def group_episodes(rows: list[dict]) -> dict[tuple, list[dict]]:
    """One episode per (ticker, signal, signal_at) — the samples of one signal."""
    eps: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        key = (r.get("ticker"), r.get("signal"), r.get("signal_at"))
        eps[key].append(r)
    for samples in eps.values():
        samples.sort(key=lambda r: r.get("elapsed_sec") or 0)
    return eps


def _at_horizon(samples: list[dict], minutes: float, tol: float = 90.0):
    """Price nearest to `minutes` after the signal, within `tol` seconds.

    A tolerance rather than an exact match because sampling is periodic and the
    price loop stalls whenever the market data does. Returning None instead of
    the closest-whatever keeps a 3-minute-old price from being reported as the
    30-minute one.
    """
    want = minutes * 60.0
    best, best_gap = None, None
    for s in samples:
        if s.get("price") is None:
            continue
        gap = abs((s.get("elapsed_sec") or 0) - want)
        if gap <= tol and (best_gap is None or gap < best_gap):
            best, best_gap = s, gap
    return best


def episode_returns(samples: list[dict], horizons: list[float]) -> dict:
    """Forward return per horizon for one signal, as a fraction of entry."""
    entry = next((s.get("entry_price") for s in samples
                  if s.get("entry_price") is not None), None)
    out: dict = {"entry_price": entry, "n_samples": len(samples), "rets": {}}
    if not entry:
        return out
    for h in horizons:
        s = _at_horizon(samples, h)
        if s:
            out["rets"][h] = (s["price"] - entry) / entry
    return out


def summarise(rows: list[dict], horizons: list[float]) -> dict:
    eps = group_episodes(rows)
    by_signal: dict[str, dict] = {}
    for (ticker, signal, _at), samples in eps.items():
        b = by_signal.setdefault(signal, {"n": 0, "measurable": 0,
                                          "rets": defaultdict(list),
                                          "tickers": set()})
        b["n"] += 1
        b["tickers"].add(ticker)
        er = episode_returns(samples, horizons)
        if er["entry_price"]:
            b["measurable"] += 1
        for h, v in er["rets"].items():
            b["rets"][h].append(v)
    return by_signal


def _fmt_pct(v) -> str:
    return "  n/a " if v is None else f"{v * 100:+6.2f}%"


def print_report(by_signal: dict, horizons: list[float], day: date | None) -> None:
    label = day.isoformat() if day else "all sessions"
    print("=" * 74)
    print(f"  DISCORD SIGNAL QUALITY — {label}")
    print("  forward return from the price at signal time")
    print("=" * 74)
    if not by_signal:
        print("\n  No signal samples recorded.")
        print("  Expected before the first call-out or scanner alert of the session.")
        print("=" * 74)
        return

    head = "  signal          n   meas  syms " + " ".join(
        f"{h:>7.0f}m" for h in horizons)
    print("\n" + head)
    for signal in SIGNAL_NAMES:
        b = by_signal.get(signal)
        if not b:
            continue
        cells = []
        for h in horizons:
            vals = b["rets"].get(h) or []
            cells.append(_fmt_pct(statistics.median(vals) if vals else None) + " ")
        print(f"  {signal:<13} {b['n']:>3}  {b['measurable']:>4}  "
              f"{len(b['tickers']):>4} " + " ".join(cells))

    print("\n  HIT RATE  (share of signals where price was higher)")
    for signal in SIGNAL_NAMES:
        b = by_signal.get(signal)
        if not b:
            continue
        cells = []
        for h in horizons:
            vals = b["rets"].get(h) or []
            if vals:
                hit = sum(1 for v in vals if v > 0) / len(vals)
                cells.append(f"{hit * 100:5.0f}% ({len(vals):>2})")
            else:
                cells.append("   n/a     ")
        print(f"  {signal:<13} " + " ".join(cells))

    unmeasured = sum(b["n"] - b["measurable"] for b in by_signal.values())
    if unmeasured:
        print(f"\n  ⚠ {unmeasured} signal(s) had no price at signal time and could not")
        print("    be measured. Call-outs are deliberately kept out of the watchlist,")
        print("    so a name nothing else surfaced never gets a quote.")
    total = sum(b["n"] for b in by_signal.values())
    if total < 20:
        print(f"\n  Sample is {total} signal(s) — read as an early read, not a verdict.")
    print("=" * 74)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--day", help="UTC date YYYY-MM-DD (default: all)")
    ap.add_argument("--signal", choices=SIGNAL_NAMES, help="restrict to one signal")
    ap.add_argument("--horizons", default="5,15,30",
                    help="minutes after the signal, comma separated")
    args = ap.parse_args()

    day = date.fromisoformat(args.day) if args.day else None
    horizons = [float(x) for x in args.horizons.split(",") if x.strip()]

    rows = load_samples(day)
    if args.signal:
        rows = [r for r in rows if r.get("signal") == args.signal]
    print_report(summarise(rows, horizons), horizons, day)


if __name__ == "__main__":
    main()
