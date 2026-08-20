#!/usr/bin/env python3
"""Can the desk trust its CM RSI-2? Measure it, per day, from shadow.jsonl.

The entry rule is a band AND a turn — "trending up from 0 to 50, never
trending down" — so trusting the indicator means trusting two things:

  1. INTERNAL CONSISTENCY. Does the published ``cm_rsi_rising`` actually
     describe the published ``cm_rsi`` series? These were computed on
     different frames for a long time (level recomputed locally off Alpaca
     IEX bars, direction left as the engine published it off its own frame),
     and a level from one series paired with a turn from another cannot be
     trusted by a rule that reads both. This needs no external reference: the
     log contradicts itself or it does not.

     ``cm_rsi_rising`` is RSI-2 now against RSI-2 ``trend_lookback`` bars back
     — 2 one-minute bars by default — so each row is compared against the same
     symbol's reading ~120s earlier, not against the previous poll.

  2. PROVENANCE. What drew the bars: the Finnhub trade stream (``realtime``)
     or the REST fallback (``alpaca``)? The engine flips per ticker
     mid-session, so a gate that does not check this is silently reading two
     different data sources. Rows logged before bars_src shipped show as
     "unknown", which is itself the finding for those days.

Read-only. Usage:
    python3 tools/rsi_trust.py [--days N] [--path ai_reports/shadow.jsonl]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from bisect import bisect_left
from collections import Counter, defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PATH = os.path.join(ROOT, "ai_reports", "shadow.jsonl")

# Bars back that cm_rsi_rising looks over (strategy_three_indicator
# trend_lookback = 2) times 60s. A row is compared against the reading closest
# to this far behind it.
TURN_WINDOW_SEC = 120.0
# How far apart the matched pair may actually be before the comparison is not
# measuring the same window any more.
MATCH_TOLERANCE_SEC = 45.0
# RSI-2 is jumpy; below this the "direction" is noise, not a turn.
FLAT_EPS = 1.0


def _load(path: str) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("ts") and r.get("symbol"):
                rows.append(r)
    return rows


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).astimezone(ET).strftime("%Y-%m-%d")


def _consistency(rows: list[dict]) -> dict:
    """Does cm_rsi_rising agree with what cm_rsi actually did?"""
    by_sym: dict[str, list[tuple[float, float, bool]]] = defaultdict(list)
    for r in rows:
        lvl = r.get("cm_rsi")
        if lvl is None:
            continue
        try:
            by_sym[str(r["symbol"])].append(
                (float(r["ts"]), float(lvl), bool(r.get("cm_rsi_rising"))))
        except (TypeError, ValueError):
            continue

    agree = contradict = flat = unmatched = 0
    examples: list[str] = []
    for sym, series in by_sym.items():
        series.sort(key=lambda x: x[0])
        stamps = [s[0] for s in series]
        for i, (ts, lvl, rising) in enumerate(series):
            target = ts - TURN_WINDOW_SEC
            j = bisect_left(stamps, target)
            best = None
            for cand in (j - 1, j, j + 1):
                if 0 <= cand < len(series) and cand != i:
                    d = abs(stamps[cand] - target)
                    if best is None or d < best[0]:
                        best = (d, cand)
            if best is None or best[0] > MATCH_TOLERANCE_SEC:
                unmatched += 1
                continue
            prev_lvl = series[best[1]][1]
            delta = lvl - prev_lvl
            if abs(delta) < FLAT_EPS:
                flat += 1
                continue
            actually_rising = delta > 0
            if actually_rising == rising:
                agree += 1
            else:
                contradict += 1
                if len(examples) < 6:
                    when = datetime.fromtimestamp(ts, timezone.utc).astimezone(ET)
                    examples.append(
                        f"{when.strftime('%H:%M:%S')} {sym:<6} "
                        f"{prev_lvl:.1f} -> {lvl:.1f} ({delta:+.1f}) "
                        f"but published rising={rising}")
    return {
        "agree": agree, "contradict": contradict, "flat": flat,
        "unmatched": unmatched, "examples": examples,
    }


def _provenance(rows: list[dict]) -> Counter:
    c = Counter()
    for r in rows:
        if r.get("cm_rsi") is None:
            continue
        c[str(r.get("cm_rsi_src") or "unknown")] += 1
    return c


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", default=DEFAULT_PATH)
    ap.add_argument("--days", type=int, default=5)
    ap.add_argument(
        "--since",
        help="ET clock time (HH:MM) to start from on the most recent day. Use "
             "this to isolate the window after a restart or a config change — "
             "a whole-day number mixes the before and the after and reports "
             "neither.")
    args = ap.parse_args()

    rows = _load(args.path)
    if not rows:
        print(f"no usable rows in {args.path}")
        return 1

    by_day: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_day[_day(float(r["ts"]))].append(r)

    if args.since:
        newest = sorted(by_day)[-1]
        hh, _, mm = args.since.partition(":")
        cutoff = int(hh) * 60 + int(mm or 0)
        kept = []
        for r in by_day[newest]:
            d = datetime.fromtimestamp(
                float(r["ts"]), timezone.utc).astimezone(ET)
            if d.hour * 60 + d.minute >= cutoff:
                kept.append(r)
        by_day = {f"{newest} (from {args.since} ET)": kept}

    print("CM RSI-2 TRUST REPORT")
    print("=" * 72)
    print("Does the published direction describe the published level series?")
    print("A contradiction means the two came off different frames — the pair")
    print("an 'in the band AND turning up' rule cannot be built on.\n")

    for day in sorted(by_day)[-args.days:]:
        day_rows = by_day[day]
        withr = [r for r in day_rows if r.get("cm_rsi") is not None]
        cons = _consistency(day_rows)
        checked = cons["agree"] + cons["contradict"]
        prov = _provenance(day_rows)

        print(f"── {day} ──  {len(day_rows)} rows, {len(withr)} with an RSI "
              f"({100.0 * len(withr) / max(1, len(day_rows)):.0f}% coverage)")
        if checked:
            pct = 100.0 * cons["contradict"] / checked
            verdict = "TRUSTWORTHY" if pct < 5.0 else (
                "SUSPECT" if pct < 20.0 else "NOT TRUSTWORTHY")
            print(f"   direction vs level: {cons['agree']} agree, "
                  f"{cons['contradict']} CONTRADICT of {checked} compared "
                  f"({pct:.1f}%)  → {verdict}")
        else:
            print("   direction vs level: no comparable pairs")
        print(f"   (flat/ignored {cons['flat']}, unmatched {cons['unmatched']})")
        srcs = ", ".join(f"{k}={v}" for k, v in prov.most_common()) or "none"
        print(f"   bars behind the reading: {srcs}")
        for ex in cons["examples"]:
            print(f"      · {ex}")
        print()

    print("A reading is trustworthy enough to gate entries when the direction")
    print("contradicts the level on well under 5% of comparisons AND the bars")
    print("are the live tape (bars_src=realtime), not the REST fallback.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
