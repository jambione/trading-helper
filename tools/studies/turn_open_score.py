#!/usr/bin/env python3
"""Score the turn-open sweep. Spec: docs/studies/turn_open_prereg.json.

Inputs: DIR/DAY_ARM.json from scripts/turn_open_sweep.sh (replay_session output). Net bp per trade = return - the
full SIP quoted spread at entry (replay_costing.entry_spread_bp, charged once; no NBBO -> that day's pooled median).
Two price lines: the replay's dashboard-price fill, and a SIP re-price (entry and exit at the SIP 1-minute open
after each timestamp). The words need both. VALUE is weighted by position size (pct / 20), so 20 positions at 5%
are not counted as four times live's exposure.
USAGE (mini, repo root, after 16:15 ET; SIP bars from the data-only account):
  TH_DATA_SECRETS=$PWD/secrets_data.json .venv/bin/python tools/studies/turn_open_score.py --dir /tmp/turn_open_DATE
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import statistics
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = os.getcwd()
sys.path[:0] = [os.path.join(ROOT, "tools", "studies"), os.path.join(ROOT, "tools"), ROOT]

ET = ZoneInfo("America/New_York")
ARMS = ("live", "turn5", "turn20")
TURNS = ("turn5", "turn20")
REF = "live"
WEIGHT = {"live": 1.0, "turn5": 1.0, "turn20": 0.25}      # position pct / 20
HALF_A = ("2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09")
HALF_B = ("2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16")
STOP_SLIP_BP = 7.0
MIN_DAYS, MIN_TRADES = 8, 100
T_PASS, T_PROMISING, T_VALUE = 2.0, 1.5, 1.5
TURN_WHY = ("any_turn", "last_any_turn")
SNAP_BASE = Path(os.getenv("SESSION_SNAPSHOT_DIR") or Path.home() / "session_snapshots")


def day_stat(xs: list[float]) -> dict:
    n = len(xs)
    if n == 0:
        return {"mean": None, "t": None, "n": 0, "sd": None}
    mu = statistics.fmean(xs)
    if n < 3:
        return {"mean": mu, "t": None, "n": n, "sd": None}
    sd = statistics.stdev(xs)
    return {"mean": mu, "t": (mu / (sd / math.sqrt(n))) if sd > 0 else None, "n": n, "sd": sd}


def mde(sd: float | None, n: int, t: float = T_PASS) -> float | None:
    """Smallest day-mean a day-clustered t >= `t` can detect, at one standard error per t unit."""
    return None if sd is None or n < 3 else t * sd / math.sqrt(n)


def stop_exit(t: dict) -> bool:
    r = str(t.get("reason") or "").lower()
    return any(k in r for k in ("stop", "trail", "ratchet"))


def word(net_a, net_b, net_all_t, sip_a, sip_b, sip_all_t, val_a, val_b, val_t, days: int, trades: int,
         sval_a=None, sval_b=None, sval_t=None) -> str:
    def pos(x):
        return x is not None and x > 0
    if days < MIN_DAYS or trades < MIN_TRADES:
        return "UNDERPOWERED"

    def holds(a, b, t, bar):
        return pos(a) and pos(b) and t is not None and t >= bar
    if (holds(net_a, net_b, net_all_t, T_PASS) and holds(sip_a, sip_b, sip_all_t, T_PASS)
            and holds(val_a, val_b, val_t, T_VALUE) and holds(sval_a, sval_b, sval_t, T_VALUE)):
        return "PASS"
    if holds(net_a, net_b, net_all_t, T_PROMISING) and holds(sip_a, sip_b, sip_all_t, T_PROMISING):
        return "PROMISING"
    return "FAIL"


def lane_problems(arm: str, closed_by_day: dict[str, list[dict]]) -> list[str]:
    """The turn arms must open through the turn lane and nothing else, on every scored day."""
    out = []
    for d, closed in sorted(closed_by_day.items()):
        whys = Counter(str(t.get("arm_why") or "?") for t in closed)
        turns = sum(v for k, v in whys.items() if k in TURN_WHY)
        other = {k: v for k, v in whys.items() if k not in TURN_WHY}
        if turns == 0:
            out.append(f"{arm} {d}: no turn-lane opens")
        if other:
            out.append(f"{arm} {d}: non-turn opens {other}")
    return out


class SipBars:
    """SIP 1-minute opens per symbol-day from the data-only account, cached in DIR/sip_bars.json."""

    def __init__(self, path: str):
        self.path = path
        self.cache = json.load(open(path)) if os.path.exists(path) else {}
        self.cl = None

    def _client(self):
        if self.cl is None:
            if not os.path.exists(os.environ.get("TH_DATA_SECRETS", "")):
                sys.exit("REFUSING: set TH_DATA_SECRETS to the data-only account's secrets_data.json")
            import bars as B
            self.cl = B.client()
        return self.cl

    def opens(self, sym: str, day: str) -> list[tuple[float, float]]:
        key = f"{sym}|{day}"
        if key not in self.cache:
            from alpaca.data.enums import DataFeed
            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
            d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
            df = self._client().get_stock_bars(StockBarsRequest(
                symbol_or_symbols=sym, timeframe=TimeFrame(1, TimeFrameUnit.Minute), feed=DataFeed.SIP,
                start=d.replace(hour=9, minute=30).astimezone(timezone.utc),
                end=d.replace(hour=16, minute=1).astimezone(timezone.utc))).df
            rows = []
            if df is not None and not df.empty:
                if "symbol" in (df.index.names or []):
                    df = df.xs(sym, level="symbol")
                rows = [(t.timestamp(), float(o)) for t, o in zip(df.index, df["open"])]
            self.cache[key] = rows
        return [tuple(x) for x in self.cache[key]]

    def open_after(self, sym: str, ts: float) -> float | None:
        day = datetime.fromtimestamp(ts, ET).strftime("%Y-%m-%d")
        for t, o in self.opens(sym, day):
            if t > ts and o > 0:
                return o
        return None

    def save(self):
        json.dump(self.cache, open(self.path, "w"))


def trade_lines(t: dict, spread: float | None, sip: SipBars) -> dict | None:
    if t.get("ret") is None or spread is None:
        return None
    net = t["ret"] * 1e4 - spread
    e, x = sip.open_after(t["symbol"], float(t["entry_ts"])), sip.open_after(t["symbol"], float(t["exit_ts"]))
    net_sip = None if e is None or x is None else (x / e - 1) * 1e4 - spread
    return {"net": net, "sip": net_sip, "slip": net - (STOP_SLIP_BP if stop_exit(t) else 0.0),
            "synthetic": bool(t.get("synthetic")), "hold": float(t["exit_ts"]) - float(t["entry_ts"])}


def recorded_live_buys(day: str) -> int | None:
    p = SNAP_BASE / day / "fills.jsonl"
    if not p.exists():
        return None
    t0 = datetime.strptime(day, "%Y-%m-%d").replace(hour=9, minute=30, tzinfo=ET).timestamp()
    t1 = datetime.strptime(day, "%Y-%m-%d").replace(hour=15, minute=50, tzinfo=ET).timestamp()
    n = 0
    for line in open(p):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("event") == "fill" and str(r.get("side")).lower() == "buy" and t0 <= float(r.get("ts") or 0) <= t1:
            n += 1
    return n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    a = ap.parse_args()
    import replay_costing as RC
    cache, box = RC.load_cache(), {}
    spread_of = lambda s, ts: RC.entry_spread_bp(cache, s, ts, box)  # noqa: E731
    sip = SipBars(os.path.join(a.dir, "sip_bars.json"))
    days = sorted({os.path.basename(p)[:10] for p in glob.glob(os.path.join(a.dir, "20??-??-??_*.json"))})
    lines = {arm: {} for arm in ARMS}
    raw_closed = {arm: {} for arm in ARMS}
    seats = {arm: {} for arm in ARMS}
    reasons = {arm: Counter() for arm in ARMS}
    incomplete = []
    for d in days:
        raw = {arm: (json.load(open(p)) if os.path.exists(p := os.path.join(a.dir, f"{d}_{arm}.json")) else None)
               for arm in ARMS}
        if any(v is None for v in raw.values()):
            incomplete.append(d)
            continue
        pooled = [s for v in raw.values() for t in (v.get("closed") or [])
                  if (s := spread_of(t["symbol"], float(t["entry_ts"]))) is not None]
        med = statistics.median(pooled) if pooled else None
        for arm in ARMS:
            closed = raw[arm].get("closed") or []
            raw_closed[arm][d] = closed
            rows = []
            for t in closed:
                s = spread_of(t["symbol"], float(t["entry_ts"]))
                r = trade_lines(t, med if s is None else s, sip)
                if r is not None:
                    rows.append(r)
                reasons[arm][str(t.get("reason") or "?")] += 1
            lines[arm][d] = rows
            slots = raw[arm].get("slots") or {}
            books = [float(v.get("book") or 0) for v in slots.values() if isinstance(v, dict)]
            seats[arm][d] = statistics.fmean(books) if books else None
    RC.save_cache(cache)
    sip.save()
    scored = sorted(lines[REF])
    print(f"days found {len(days)}; scored {len(scored)} {scored}; incomplete {incomplete}\n")
    problems = [p for arm in TURNS for p in lane_problems(arm, raw_closed[arm])]
    if problems:
        print("REFUSING: the turn lane did not run as registered:\n  " + "\n  ".join(problems))
        return

    def mean_of(rows, key):
        xs = [r[key] for r in rows if r[key] is not None]
        return statistics.fmean(xs) if xs else None

    f = lambda x: "—" if x is None else f"{x:+.1f}"  # noqa: E731
    g = lambda x: "—" if x is None else f"{x:+.2f}"  # noqa: E731
    print("| arm | slice | days | trades | net/trade (t) | SIP re-priced (t) | -7 bp stops | no synthetic | value/day weighted (t) | MDE at t 2 | seats |")
    print("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    st = {}
    for arm in ARMS:
        for tag, sl in (("all", scored), ("A", [d for d in scored if d in HALF_A]), ("B", [d for d in scored if d in HALF_B])):
            if not sl:
                continue
            net = day_stat([m for d in sl if (m := mean_of(lines[arm][d], "net")) is not None])
            sp = day_stat([m for d in sl if (m := mean_of(lines[arm][d], "sip")) is not None])
            sl7 = day_stat([m for d in sl if (m := mean_of(lines[arm][d], "slip")) is not None])
            ns = day_stat([m for d in sl if (m := mean_of([r for r in lines[arm][d] if not r["synthetic"]], "net")) is not None])
            val = day_stat([WEIGHT[arm] * sum(r["net"] for r in lines[arm][d]) for d in sl])
            seat = [seats[arm][d] for d in sl if seats[arm][d] is not None]
            st[(arm, tag)] = {"net": net, "sip": sp, "days": len(sl), "trades": sum(len(lines[arm][d]) for d in sl)}
            print(f"| {arm} | {tag} | {len(sl)} | {st[(arm, tag)]['trades']} | {f(net['mean'])} ({g(net['t'])}) | "
                  f"{f(sp['mean'])} ({g(sp['t'])}) | {f(sl7['mean'])} | {f(ns['mean'])} | {f(val['mean'])} ({g(val['t'])}) | "
                  f"{f(mde(net['sd'], net['n']))} | {f(statistics.fmean(seat)) if seat else '—'} |")
    print("\nA FAIL does not rule out a per-trade edge below the MDE column.")
    print("\nSIP re-price misses (trades with no SIP bar after entry or exit, left out of the SIP lines):")
    for arm in ARMS:
        n_all = sum(len(lines[arm][d]) for d in scored)
        n_miss = sum(1 for d in scored for r in lines[arm][d] if r["sip"] is None)
        print(f"  {arm}: {n_miss} of {n_all}")
    print("\nPaired weighted VALUE vs live, bp/day (dashboard price | SIP re-price):")
    words = []

    def value(arm, d, key):
        return WEIGHT[arm] * sum(r[key] for r in lines[arm][d] if r[key] is not None)
    for arm in TURNS:
        diffs, sdiffs = {}, {}
        for tag in ("all", "A", "B"):
            sl = [d for d in scored if tag == "all" or d in (HALF_A if tag == "A" else HALF_B)]
            diffs[tag] = day_stat([value(arm, d, "net") - value(REF, d, "net") for d in sl])
            sdiffs[tag] = day_stat([value(arm, d, "sip") - value(REF, d, "sip") for d in sl])
            print(f"  {arm} - live [{tag}]: {f(diffs[tag]['mean'])} (t {g(diffs[tag]['t'])}) | "
                  f"{f(sdiffs[tag]['mean'])} (t {g(sdiffs[tag]['t'])}), {diffs[tag]['n']} days")
        w = word(st[(arm, "A")]["net"]["mean"], st[(arm, "B")]["net"]["mean"], st[(arm, "all")]["net"]["t"],
                 st[(arm, "A")]["sip"]["mean"], st[(arm, "B")]["sip"]["mean"], st[(arm, "all")]["sip"]["t"],
                 diffs["A"]["mean"], diffs["B"]["mean"], diffs["all"]["t"],
                 st[(arm, "all")]["days"], st[(arm, "all")]["trades"],
                 sdiffs["A"]["mean"], sdiffs["B"]["mean"], sdiffs["all"]["t"])
        words.append(f"{arm}: {w}")
    print("\nOpens per day, replay vs the recorded live desk:")
    for d in scored:
        print(f"  {d}: live recorded {recorded_live_buys(d)}; replay " +
              ", ".join(f"{arm} {len(raw_closed[arm][d])}" for arm in ARMS))
    print("\nExit reasons:")
    for arm in ARMS:
        print(f"  {arm}: {dict(reasons[arm].most_common(8))}")
    print("\n**" + " | ".join(words) + "**")


if __name__ == "__main__":
    main()
