#!/usr/bin/env python3
"""entry_scorecard.py — Jonathan's entry yardstick for live fills or replay results.

Yardstick (docs/ENTRY_OPTIMIZATION_2026-09-25.md): share of 09:40-15:50 with
>=1 and >=2 positions open, avg/max open, opens, share of 10-minute slots with
at least one open, gross and net-of-spread P/L per trade (bp; replay net uses the
name_selection_study price-tier spread), win rate, per-symbol
opens. Replay P/L is gross at the recorded price (no spread / slippage).

USAGE
  python tools/studies/entry_scorecard.py --fills ~/session_snapshots/2026-09-25/fills.jsonl
  python tools/studies/entry_scorecard.py /tmp/eo/d25_base.json /tmp/eo/d25_room.json ...
  (add --start 13:10 for a partial window; --json for machine output)
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def at(day: str, hhmm: str) -> float:
    h, m = map(int, hhmm.split(":"))
    return datetime.strptime(day, "%Y-%m-%d").replace(hour=h, minute=m, tzinfo=ET).timestamp()


def trips_from_fills(path: Path) -> tuple[str, list[dict]]:
    """Pair buy/sell fills per symbol (FIFO) into round trips."""
    rows = [json.loads(x) for x in open(path) if x.strip()]
    fills = sorted((r for r in rows if r.get("event") == "fill"), key=lambda r: r["ts"])
    day = fills[0]["day"] if fills else ""
    openq: dict[str, list] = defaultdict(list)
    out = []
    for r in fills:
        s = r["symbol"].upper()
        px = float(r.get("filled_avg_price") or 0)
        q = float(r.get("filled_qty") or r.get("qty") or 0)
        if str(r.get("side")).lower() == "buy":
            openq[s].append([r["ts"], px, q])
        elif openq[s]:
            t0, p0, q0 = openq[s].pop(0)
            out.append({"symbol": s, "entry_ts": t0, "exit_ts": r["ts"], "entry": p0,
                        "exit": px, "ret": px / p0 - 1 if p0 else 0.0, "usd": (px - p0) * q0})
    for s, lst in openq.items():
        for t0, p0, q0 in lst:
            out.append({"symbol": s, "entry_ts": t0, "exit_ts": None, "entry": p0, "ret": None})
    return day, out


def trips_from_replay(path: Path) -> tuple[str, list[dict], dict]:
    d = json.loads(path.read_text())
    trips = []
    for p in d.get("closed", []) + d.get("open_at_end", []):
        trips.append({"symbol": p.get("symbol"), "entry_ts": p.get("entry_ts"),
                      "exit_ts": p.get("exit_ts"), "ret": p.get("ret"),
                      "reason": p.get("reason"), "synthetic": p.get("synthetic"),
                      "entry": p.get("entry_px")})
    return d["day"], trips, d


def cost_pct(px) -> float:
    """name_selection_study's spread estimate by price tier (percent)."""
    px = float(px or 0)
    return 0.10 if px >= 20 else 0.20 if px >= 10 else 0.40 if px >= 5 else 1.00


def score(day: str, trips: list[dict], start: str = "09:40", end: str = "15:50") -> dict:
    t0, t1 = at(day, start), at(day, end)
    step = 10.0
    n = int((t1 - t0) // step)
    counts = []
    for i in range(n):
        t = t0 + i * step
        c = sum(1 for p in trips if p["entry_ts"] <= t < (p["exit_ts"] or t1 + 1))
        counts.append(c)
    in_win = [p for p in trips if t0 <= p["entry_ts"] < t1]
    slots = int((t1 - t0) // 600)
    hit = {int((p["entry_ts"] - t0) // 600) for p in in_win}
    rets = [p["ret"] for p in trips if p.get("ret") is not None]
    # Live fills already paid the spread; replay fills are at the recorded price.
    nets = [p["ret"] - (0 if "usd" in p else cost_pct(p.get("entry")) / 100)
            for p in trips if p.get("ret") is not None]
    by_sym = Counter(p["symbol"] for p in trips)
    return {
        "day": day, "window": f"{start}-{end}", "opens": len(trips), "opens_in_window": len(in_win),
        "opens_per_10m": round(len(in_win) / max(1, slots), 2),
        "slots_with_open": round(len(hit) / max(1, slots), 3),
        "ge1": round(sum(c >= 1 for c in counts) / max(1, n), 3),
        "ge2": round(sum(c >= 2 for c in counts) / max(1, n), 3),
        "avg_open": round(sum(counts) / max(1, n), 2), "max_open": max(counts or [0]),
        "closed": len(rets), "win": round(sum(r > 0 for r in rets) / max(1, len(rets)), 3),
        "bp_per_trade": round(1e4 * sum(rets) / max(1, len(rets)), 1),
        "sum_pct": round(100 * sum(rets), 2),
        "net_bp_per_trade": round(1e4 * sum(nets) / max(1, len(nets)), 1),
        "net_sum_pct": round(100 * sum(nets), 2),
        "usd": round(sum(p.get("usd") or 0 for p in trips), 2) if any("usd" in p for p in trips) else None,
        "names": len(by_sym), "top_names": by_sym.most_common(6),
        "synthetic": sum(1 for p in trips if p.get("synthetic")),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("results", nargs="*")
    ap.add_argument("--fills")
    ap.add_argument("--start", default="09:40")
    ap.add_argument("--end", default="15:50")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    out = []
    if a.fills:
        day, trips = trips_from_fills(Path(a.fills))
        out.append(("live", score(day, trips, a.start, a.end), trips))
    for r in a.results:
        day, trips, _ = trips_from_replay(Path(r))
        out.append((Path(r).stem, score(day, trips, a.start, a.end), trips))
    if a.json:
        print(json.dumps([{"name": n, **s, "trips": t} for n, s, t in out], default=str))
        return 0
    hdr = f"{'variant':<14}{'opens':>6}{'/10m':>6}{'slot%':>6}{'>=1':>6}{'>=2':>6}{'avg':>6}{'max':>4}{'win':>6}{'bp/tr':>7}{'net':>7}{'netsum%':>8}  top names"
    print(hdr)
    for n, s, _ in out:
        print(f"{n:<14}{s['opens_in_window']:>6}{s['opens_per_10m']:>6}{s['slots_with_open']:>6.0%}"
              f"{s['ge1']:>6.0%}{s['ge2']:>6.0%}{s['avg_open']:>6}{s['max_open']:>4}{s['win']:>6.0%}"
              f"{s['bp_per_trade']:>7}{s['net_bp_per_trade']:>7}{s['net_sum_pct']:>8}  "
              + " ".join(f"{k}{v}" for k, v in s['top_names']))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
