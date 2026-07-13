"""Summarize trades.csv: is the signal set actually making money?

    python trade_stats.py            # current trades.csv
    python trade_stats.py --all      # include rotated trades-old-*.csv

Rows with |pnl| > 10% are split out as garbage (OCR glitches / symbol
switches from before the glitch-gate and symbol-reset guards). Run this
after a few sessions with the guards live; if the clean win rate hasn't
improved, tune imbalance_buy / confirm_reads / max_spread_pct from the
per-reason and per-symbol breakdowns below instead of intuition.
"""
from __future__ import annotations

import csv
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent
GARBAGE_PCT = 10.0

REASON_BUCKETS = [
    ("target hit", "TAKE_PROFIT"),
    ("stop hit", "STOP"),
    ("giving back gains", "TRAIL_EXIT"),
    ("flow turned", "FLOW_SELL"),
]


def bucket(reason: str) -> str:
    for prefix, name in REASON_BUCKETS:
        if reason.startswith(prefix):
            return name
    return "OTHER"


def load(paths: list[Path]) -> list[dict]:
    rows = []
    for p in paths:
        if not p.exists():
            continue
        with open(p) as f:
            for r in csv.DictReader(f):
                try:
                    r["pnl"] = float(r["pnl_pct"])
                except (KeyError, ValueError):
                    continue
                r.setdefault("symbol", "")
                rows.append(r)
    return rows


def line(label: str, trades: list[float]) -> str:
    if not trades:
        return f"  {label:<14} —"
    wins = [p for p in trades if p > 0]
    losses = [p for p in trades if p <= 0]
    s = (f"  {label:<14} {len(trades):>4} trades   "
         f"win {100 * len(wins) / len(trades):3.0f}%   "
         f"avg {sum(trades) / len(trades):+6.2f}%   "
         f"total {sum(trades):+7.1f}%")
    if wins and losses:
        s += (f"   avg win {sum(wins) / len(wins):+5.2f}% / "
              f"avg loss {sum(losses) / len(losses):+5.2f}%")
    return s


def main():
    paths = [HERE / "trades.csv"]
    if "--all" in sys.argv:
        paths += sorted(HERE.glob("trades-old-*.csv"))
    rows = load(paths)
    if not rows:
        print("no trades logged yet")
        return

    garbage = [r for r in rows if abs(r["pnl"]) > GARBAGE_PCT]
    clean = [r for r in rows if abs(r["pnl"]) <= GARBAGE_PCT]
    print(f"{len(rows)} round trips from {len(paths)} file(s); "
          f"{len(garbage)} garbage (|pnl| > {GARBAGE_PCT:.0f}%: glitches / "
          "symbol switches), {0} analyzed\n".format(len(clean)))

    print("overall")
    print(line("all clean", [r["pnl"] for r in clean]))

    print("\nby exit reason")
    by_reason = defaultdict(list)
    for r in clean:
        by_reason[bucket(r["reason"])].append(r["pnl"])
    for k in sorted(by_reason, key=lambda k: -len(by_reason[k])):
        print(line(k, by_reason[k]))

    print("\nby day")
    by_day = defaultdict(list)
    for r in clean:
        by_day[r["entry_time"][:10]].append(r["pnl"])
    for k in sorted(by_day):
        print(line(k, by_day[k]))

    if any(r["symbol"] for r in clean):
        print("\nby symbol")
        by_sym = defaultdict(list)
        for r in clean:
            by_sym[r["symbol"] or "?"].append(r["pnl"])
        for k in sorted(by_sym, key=lambda k: -len(by_sym[k])):
            print(line(k, by_sym[k]))


if __name__ == "__main__":
    main()
