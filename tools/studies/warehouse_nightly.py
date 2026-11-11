#!/usr/bin/env python3
"""warehouse_nightly.py - append every new session to the study warehouses (restructure plan WP2, operator 11/11).

Run from scripts/nightly.sh after the close (data-only Alpaca account, nice 19). Each night it:

  1. extends the daily liquidity table into ai_reports/gapdown/daily_live.pkl. The original daily.pkl is NEVER
     written: older studies cite it and must stay reproducible. Adjusted prices change whenever a name splits or pays a
     dividend, so a name whose overlap with the stored series no longer matches is refetched in full.
  2. appends each missing session to
       ai_reports/warehouse/sip        liquid names (close >= $10, 20-day dollar volume >= $20M on the prior session)
       ai_reports/warehouse/sip_top    the 60 largest by prior-session dollar volume
       ai_reports/warehouse/sip_small  $2-20, 20-day dollar volume >= $5M, top 300 by prior-session range
       ai_reports/warehouse/sip_hedge  SPY and QQQ
     A day file is never overwritten. sip_top and sip_small files also carry tomorrow's names (already known tonight,
     because membership uses the prior session) and a `uni` flag for today's.

    .venv/bin/python tools/studies/warehouse_nightly.py [--max-days 40] [--dry-run]

Known limit: minute bars are split-adjusted as of the night they were fetched. A later split leaves that name's older
day files on the old scale; the harness only compares a name with its own prior session, so the effect is one bad
prior-close for that name on the split day.
"""
from __future__ import annotations

import argparse
import os
import pickle
import shutil
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(os.environ.get("REPO") or Path(__file__).resolve().parents[2])
sys.path[:0] = [str(ROOT), str(ROOT / "tools" / "studies")]
ET = ZoneInfo("America/New_York")
WH = ROOT / "ai_reports" / "warehouse"
TABLE, LIVE = ROOT / "ai_reports" / "gapdown" / "daily.pkl", ROOT / "ai_reports" / "gapdown" / "daily_live.pkl"
TOMORROW = "9999-12-31"                     # placeholder session after the last real one
TOL = 1e-4                                  # an overlap that differs by more than 1 bp means the adjusted series moved


# ---------- pure pieces (tested) ----------
def adj_moved(old: dict, new: dict, tol: float = TOL) -> bool:
    """True when the stored adjusted series and the freshly fetched one disagree on a shared day (split or dividend since)."""
    for day, (o, c) in new.items():
        if day in old:
            oo, oc = old[day]
            if abs(o / oo - 1) > tol or abs(c / oc - 1) > tol:
                return True
    return False


def new_liquid_dates(raw_rows: list[tuple], after: str, liquid_dates) -> list[str]:
    """Liquid dates later than `after`, from RAW (date, close, volume) rows that include at least 20 sessions before them."""
    return sorted(d for d in liquid_dates(raw_rows) if d > after)


def missing_sessions(have: set[str], sessions: list[str], after: str, max_days: int) -> list[str]:
    """Sessions later than `after` with no file yet, oldest first, at most max_days."""
    return [d for d in sessions if d > after and d not in have][:max_days]


def last_file_day(d: Path) -> str:
    days = sorted(f.stem for f in d.glob("*.npz")) if d.exists() else []
    return days[-1] if days else ""


# ---------- data ----------
def fetch_daily(cl, syms, start, end, adjustment, log, per=100):
    import gapdown_short as G
    from alpaca.data.timeframe import TimeFrame
    out = {}
    for i in range(0, len(syms), per):
        data = G._bars(cl, syms[i:i + per], TimeFrame.Day, start, end, adjustment, log)
        if data is None:
            raise SystemExit(f"daily fetch failed: {log[-1]}")
        out.update(data)
    return out


def extend_table(cl, start_day: str, log: list[str]) -> dict:
    """-> the live table {'liquid', 'adj', 'raw' (recent high/low/close/volume), 'days', 'through'}; saved to daily_live.pkl."""
    import gapdown_short as G
    from alpaca.data.enums import Adjustment
    with open(LIVE if LIVE.exists() else TABLE, "rb") as fh:
        d = pickle.load(fh)
    last = max(d["adj"]["SPY"])
    y, m, dd = map(int, min(start_day, last).split("-"))
    start = datetime(y, m, dd, tzinfo=ET) - timedelta(days=75)             # 20+ sessions before the first new day, with room
    end = datetime.now(ET) - timedelta(minutes=20)
    syms = G._universe() + ["SPY", "QQQ"]
    raw = fetch_daily(cl, syms, start, end, Adjustment.RAW, log, per=80)
    recent, added = {}, 0
    for s, rows in raw.items():
        rr = [(b.timestamp.astimezone(ET).strftime("%Y-%m-%d"), float(b.close), float(b.volume)) for b in rows]
        recent[s] = {b.timestamp.astimezone(ET).strftime("%Y-%m-%d"): (float(b.high), float(b.low), float(b.close), float(b.volume)) for b in rows}
        if s in ("SPY", "QQQ"):
            continue
        fresh = new_liquid_dates(rr, last, G.liquid_dates)
        if fresh:
            d["liquid"][s] = sorted(set(d["liquid"].get(s, [])) | set(fresh))
            added += len(fresh)
    need = sorted(set(d["liquid"]) | {"SPY"})
    adj = fetch_daily(cl, need, start, end, Adjustment.ALL, log, per=80)
    moved = []
    for s in need:
        new = {b.timestamp.astimezone(ET).strftime("%Y-%m-%d"): (float(b.open), float(b.close)) for b in adj.get(s, [])}
        old = d["adj"].get(s)
        if not new:
            continue
        if old is None or adj_moved(old, new):
            moved.append(s)
        else:
            old.update(new)
    if moved:                                                              # a split or dividend: take the whole series again
        full = fetch_daily(cl, moved, datetime(2015, 11, 1, tzinfo=ET), end, Adjustment.ALL, log, per=3)
        for s in moved:
            rows = full.get(s, [])
            if rows:
                d["adj"][s] = {b.timestamp.astimezone(ET).strftime("%Y-%m-%d"): (float(b.open), float(b.close)) for b in rows}
    d["raw"], d["days"], d["through"] = recent, sorted(recent.get("SPY", {})), max(recent.get("SPY", {"": 0}))
    d["log"] = list(d.get("log", []))[-200:] + [x for x in log if str(x).startswith("FAILED")]
    tmp = LIVE.with_suffix(".tmp")
    with open(tmp, "wb") as fh:
        pickle.dump(d, fh)
    os.replace(tmp, LIVE)
    print(f"table: through {d['through']}, {added} new liquid name-days, {len(moved)} adjusted series refetched in full", flush=True)
    return d


def save_day(out: Path, day: str, pk: dict, today_names: set[str] | None = None) -> bool:
    path = out / f"{day}.npz"
    if path.exists():                                                      # never overwrite a day
        return False
    if today_names is not None:
        pk["uni"] = np.array([str(s) in today_names for s in pk["syms"]], dtype=bool)
    tmp = out / f"{day}.tmp.npz"
    np.savez_compressed(tmp, **pk)
    os.replace(tmp, path)
    return True


def main(argv=None) -> int:
    import warehouse_fetch as W
    import warehouse_small as WS
    import warehouse_top as WT
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-days", type=int, default=40, help="most sessions to append per warehouse in one run")
    ap.add_argument("--dry-run", action="store_true", help="extend the table and print what would be fetched")
    a = ap.parse_args(argv)
    t0 = time.time()
    for name in ("sip", "sip_top", "sip_small", "sip_hedge"):
        (WH / name).mkdir(parents=True, exist_ok=True)
    fresh = WH / "sip_fresh"                                               # the 10/16-11/10 one-off sample joins the main warehouse
    if fresh.exists():
        for f in sorted(fresh.glob("*.npz")):
            if not (WH / "sip" / f.name).exists():
                shutil.copy2(f, WH / "sip" / f.name)
    lasts = {n: last_file_day(WH / n) for n in ("sip", "sip_top", "sip_small", "sip_hedge")}
    start_day = min(v for v in lasts.values() if v) if any(lasts.values()) else datetime.now(ET).strftime("%Y-%m-%d")
    cl, log = W._client(), []
    d = extend_table(cl, start_day, log)
    sessions = [x for x in d["days"] if x <= d["through"]]
    raw = d["raw"]
    hlcv = {s: rows for s, rows in raw.items() if s not in ("SPY", "QQQ")}
    # Membership uses the PRIOR session, so tomorrow's names are already known tonight: a placeholder day after the last
    # session lets the pickers return them, and tonight's file carries their bars as tomorrow's "yesterday".
    ahead = sessions + [TOMORROW]
    top = WT.pick({s: {day: (r[2], r[3]) for day, r in rows.items()} for s, rows in hlcv.items()}, ahead, 60)
    small = WS.pick(hlcv, ahead)
    recent_liquid = {s: set(ds[-120:]) for s, ds in d["liquid"].items()}
    liquid = {day: sorted(s for s, ds in recent_liquid.items() if day in ds) for day in sessions}
    nxt = {ahead[i]: ahead[i + 1] for i in range(len(ahead) - 1)}
    plan = {
        "sip": lambda day: (liquid.get(day, []), None),
        "sip_top": lambda day: (sorted(set(top.get(day, [])) | set(top.get(nxt.get(day, ""), []))), set(top.get(day, []))),
        "sip_small": lambda day: (sorted(set(small.get(day, [])) | set(small.get(nxt.get(day, ""), []))), set(small.get(day, []))),
        "sip_hedge": lambda day: (["QQQ", "SPY"], None),
    }
    wrote = 0
    for name, names_for in plan.items():
        have = {f.stem for f in (WH / name).glob("*.npz")}
        todo = missing_sessions(have, sessions, lasts[name] or sessions[-2], a.max_days)
        for day in todo:
            names, today = names_for(day)
            if name in ("sip_top", "sip_small") and not today:
                continue
            if len(names) < (2 if name == "sip_hedge" else 20):
                print(f"  {name} {day}: only {len(names)} names, skipped", flush=True)
                continue
            if a.dry_run:
                print(f"  would fetch {name} {day}: {len(names)} names")
                continue
            pk, lg = W.fetch_day(cl, day, names, "sip")
            if pk is None or len(pk["syms"]) < 0.5 * len(names):
                print(f"  {name} {day}: INCOMPLETE {(lg or [''])[-1]}", flush=True)
                continue
            wrote += save_day(WH / name, day, pk, today)
            print(f"  {name} {day}: {len(pk['syms'])} names", flush=True)
    print(f"warehouse_nightly done: {wrote} day files written in {time.time() - t0:.0f} s; "
          + ", ".join(f"{n} through {last_file_day(WH / n)}" for n in plan), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
