#!/usr/bin/env python3
"""desk_vs_spy - the desk's standing scoreboard: each round trip's return MINUS what SPY did over the same holding period.

Operator 11/20: "the system we are building shouldn't matter if the market is down." A long-only book's raw P&L swings
with the index, so the number to judge is name-minus-SPY, per trade and per day. On 11/20 the first 10 trades averaged
-28 bp raw, -17 bp of it SPY and -12 bp the names underperforming it.

    .venv/bin/python tools/desk_vs_spy.py                       # today (live fills)
    .venv/bin/python tools/desk_vs_spy.py --day 2026-11-19 --day 2026-11-18
    .venv/bin/python tools/desk_vs_spy.py --replay ai_reports/x/2026-11-18_standard.json   # a replay_session.py output
    .venv/bin/python tools/desk_vs_spy.py --days 2026-10-26 2026-10-27 2026-10-28 2026-10-29 --label standard_week

Round trips: ai_reports/fills/DAY.jsonl, FIFO per symbol (partial fills split). SPY: the warehouse's SPY 1-minute opens
for the day (relative_strength_scan.load_day, which falls back to sip_hedge); for a day not in the warehouse yet (today),
the stack logger's SPY snapshots (ai_reports/stack_log, IEX). Output: ai_reports/desk_vs_spy/<label or day>.json.
Read-only: no API calls, no config, no orders.
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))
OUT = ROOT / "ai_reports" / "desk_vs_spy"
ET = ZoneInfo("America/New_York")


# ---------------------------------------------------------------- pure pieces

def round_trips(fill_rows: list[dict], day: str) -> list[dict]:
    """FIFO buy -> sell pairing per symbol from fill events. -> [{day, sym, entry_ts, exit_ts, entry, exit, qty}]. Pure."""
    q = defaultdict(deque)
    out = []
    for r in fill_rows:
        if r.get("event") != "fill" or not r.get("filled_qty"):
            continue
        s, px, qty, ts = r["symbol"], float(r["filled_avg_price"]), float(r["filled_qty"]), float(r["ts"])
        if r["side"] == "buy":
            q[s].append([ts, px, qty])
            continue
        while qty > 1e-9 and q[s]:
            b = q[s][0]
            m = min(qty, b[2])
            out.append({"day": day, "sym": s, "entry_ts": b[0], "exit_ts": ts, "entry": b[1], "exit": px, "qty": m})
            b[2] -= m
            qty -= m
            if b[2] <= 1e-9:
                q[s].popleft()
    return out


def minute_index(ts: float, day: str) -> int:
    """Minutes since 09:30 ET on `day` for epoch `ts`, clipped to the session. Pure."""
    t0 = datetime.fromisoformat(day + "T09:30").replace(tzinfo=ET).timestamp()
    return int(min(max((ts - t0) // 60, 0), 389))


def spy_ret_from_opens(spy_o, ts0: float, ts1: float, day: str):
    """SPY return between two timestamps from a 390-vector of 1-minute OPENS (the open of the minute each ts falls in;
    NaN gaps use the last earlier finite open). None if unavailable. Pure."""
    o = np.asarray(spy_o, float)

    def at(m):
        j = np.where(np.isfinite(o[: m + 1]))[0]
        return float(o[j[-1]]) if len(j) else None
    a, b = at(minute_index(ts0, day)), at(minute_index(ts1, day))
    return None if not a or not b else b / a - 1


def spy_ret_from_snaps(snaps, ts0: float, ts1: float):
    """SPY return from the stack logger's (ts, px) snapshots: nearest snapshot to each timestamp (within 3 minutes). Pure."""
    if not snaps:
        return None

    def near(t):
        s = min(snaps, key=lambda x: abs(x[0] - t))
        return s[1] if abs(s[0] - t) <= 180 else None
    a, b = near(ts0), near(ts1)
    return None if not a or not b else b / a - 1


def score(trips: list[dict], spy_of) -> dict:
    """trips with a SPY return -> summary: raw, SPY, name-minus-SPY (bp, mean), share beating SPY, dollars raw / net of
    the market. spy_of(trip) -> return or None. Pure."""
    rows = []
    for t in trips:
        s = spy_of(t)
        if s is None or not t["entry"] > 0:
            continue
        raw = t["exit"] / t["entry"] - 1
        notional = t["entry"] * t["qty"]
        rows.append({**t, "raw_bp": raw * 1e4, "spy_bp": s * 1e4, "vs_bp": (raw - s) * 1e4,
                     "usd": (t["exit"] - t["entry"]) * t["qty"], "usd_vs": notional * (raw - s)})
    if not rows:
        return {"n": 0}
    raw, spy, vs = (np.array([r[k] for r in rows]) for k in ("raw_bp", "spy_bp", "vs_bp"))
    return {"n": len(rows), "days": len({r["day"] for r in rows}), "raw_bp": round(float(raw.mean()), 1),
            "spy_bp": round(float(spy.mean()), 1), "vs_spy_bp": round(float(vs.mean()), 1),
            "beat_spy_pct": round(100 * float((vs > 0).mean()), 1), "win_raw_pct": round(100 * float((raw > 0).mean()), 1),
            "usd_raw": round(sum(r["usd"] for r in rows), 2), "usd_net_of_market": round(sum(r["usd_vs"] for r in rows), 2),
            "dropped_no_spy": len(trips) - len(rows)}


# ---------------------------------------------------------------- I/O

def load_fills(day: str) -> list[dict]:
    p = ROOT / "ai_reports" / "fills" / f"{day}.jsonl"
    out = []
    if p.exists():
        for line in open(p):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def warehouse_spy_opens(day: str):
    try:
        from relative_strength_scan import load_day
        from evening_family import WH_SIP
    except Exception:                                # noqa: BLE001
        return None
    p = WH_SIP / f"{day}.npz"
    if not p.exists():
        return None
    d = load_day(p)
    i = d["idx"].get("SPY")
    return None if i is None else d["o"][i]


def logger_spy_snaps(day: str):
    p = ROOT / "ai_reports" / "stack_log" / f"{day}.jsonl.gz"
    if not p.exists():
        return []
    snaps = []
    for line in gzip.open(p, "rt"):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        e = (r.get("etfs") or {}).get("SPY")
        if e and e.get("px"):
            snaps.append((float(r["ts"]), float(e["px"])))
    return snaps


def spy_getter(day: str):
    """Warehouse minute opens if the day is there, else the stack logger's snapshots. -> (fn(trip)->ret, source)."""
    o = warehouse_spy_opens(day)
    if o is not None and np.isfinite(np.asarray(o, float)).sum() > 100:
        return (lambda t: spy_ret_from_opens(o, t["entry_ts"], t["exit_ts"], day)), "warehouse SIP opens"
    snaps = logger_spy_snaps(day)
    return (lambda t: spy_ret_from_snaps(snaps, t["entry_ts"], t["exit_ts"])), "stack logger IEX snapshots"


def replay_trips(path: Path) -> list[dict]:
    """A replay_session.py output -> trips (its 'closed' list: entry_ts, exit_ts, entry_px, exit_px; 1 unit each)."""
    d = json.load(open(path))
    day = d["day"]
    out = []
    for c in d.get("closed", []):
        if c.get("entry_ts") and c.get("exit_ts") and c.get("entry_px") and c.get("exit_px"):
            out.append({"day": day, "sym": c["symbol"], "entry_ts": float(c["entry_ts"]), "exit_ts": float(c["exit_ts"]),
                        "entry": float(c["entry_px"]), "exit": float(c["exit_px"]), "qty": 1.0})
    return out


def run(days: list[str], replays: list[str], label: str | None) -> dict:
    trips, srcs, spy_of_day = [], {}, {}
    for day in days:
        trips += round_trips(load_fills(day), day)
    for rp in replays:
        trips += replay_trips(Path(rp))
    for day in sorted({t["day"] for t in trips}):
        spy_of_day[day], srcs[day] = spy_getter(day)
    per_day = {d: score([t for t in trips if t["day"] == d], spy_of_day[d]) for d in sorted(spy_of_day)}
    total = score(trips, lambda t: spy_of_day[t["day"]](t))
    res = {"label": label, "spy_source": srcs, "total": total, "per_day": per_day}
    OUT.mkdir(parents=True, exist_ok=True)
    name = label or "_".join(sorted({t["day"] for t in trips})[:1] + sorted({t["day"] for t in trips})[-1:]) or "empty"
    (OUT / f"{name}.json").write_text(json.dumps(res, indent=1))
    return res


def print_table(res: dict) -> None:
    hdr = f"{'':12}{'n':>4}{'raw bp':>9}{'SPY bp':>9}{'vs SPY':>9}{'beat SPY':>10}{'$ raw':>9}{'$ vs mkt':>10}"
    print(hdr)
    for k, s in list(res["per_day"].items()) + [("TOTAL", res["total"])]:
        if not s.get("n"):
            print(f"{k:12}{0:>4}")
            continue
        print(f"{k:12}{s['n']:>4}{s['raw_bp']:>9}{s['spy_bp']:>9}{s['vs_spy_bp']:>9}{str(s['beat_spy_pct'])+'%':>10}"
              f"{s['usd_raw']:>9}{s['usd_net_of_market']:>10}")
    print("SPY source:", res["spy_source"])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--day", action="append", default=[])
    ap.add_argument("--days", nargs="*", default=[])
    ap.add_argument("--replay", action="append", default=[])
    ap.add_argument("--label")
    a = ap.parse_args(argv)
    days = a.day + a.days
    if not days and not a.replay:
        days = [datetime.now(ET).strftime("%Y-%m-%d")]
    print_table(run(days, a.replay, a.label))
    return 0


if __name__ == "__main__":
    sys.exit(main())
