#!/usr/bin/env python3
"""momentum_premarket (docs/studies/momentum_premarket_prereg.json) - COUNT MODE ONLY. Read-only; free historical SIP minute bars.

Counts first, before any return is read (prereg 'power'): how many momentum-alert name-days there are per session and price band,
how many were liquid premarket (bars with volume in the 5 minutes up to the alert, and minutes with prints between the alert and
09:20), and how many of the alert names could even have been seated by the live lane. NO price change after the alert is computed
or printed here. The scoring modes (canary, score) are NOT built: see the prereg's 'amended_scope' (11/06): a faithful offline replay
of the live arm needs the signal engine's indicator map, so the scored sample is the SIP-month forward dry run.

    .venv/bin/python tools/studies/momentum_premarket.py count          # mini only (Alpaca keys); refuses 09:00-16:30 ET weekdays
Writes ai_reports/momentum_premarket/count.json and prints a table.
"""
from __future__ import annotations

import gzip
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
ET = ZoneInfo("America/New_York")
SESS = ROOT / "ai_reports" / "sessions"
OUT = ROOT / "ai_reports" / "momentum_premarket"
BANDS = [("<2", 0, 2), ("2-5", 2, 5), ("5-10", 5, 10), ("10-20", 10, 20), ("20+", 20, 1e9)]
WIN = (4 * 3600, 9 * 3600 + 15 * 60)  # alert window in seconds: 04:00:00 - 09:15:00 ET inclusive (prereg universe.alerts)
LEVERED = ("BULL", "BEAR", "3X", "2X", "ULTRA", "PROSHARES", "DIREXION")


def first_alerts(day: str) -> dict:
    """First alert per symbol in the alert window: {sym: ts}. Lines are the paid momentum list (discord.jsonl.gz)."""
    p = SESS / day / "discord.jsonl.gz"
    if not p.exists():
        return {}
    out = {}
    with gzip.open(p, "rt", errors="replace") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            sym = str(r.get("symbol") or "").upper().strip()
            ts = r.get("ts")
            if not sym or ts is None:
                continue
            m = datetime.fromtimestamp(ts, ET)
            secs = m.hour * 3600 + m.minute * 60 + m.second
            if WIN[0] <= secs <= WIN[1] and sym not in out:
                out[sym] = float(ts)
    return out


def band_of(px: float) -> str:
    for lab, lo, hi in BANDS:
        if lo <= px < hi:
            return lab
    return "?"


def classify(bars: list, ts: float) -> dict | None:
    """Counts at/before the alert plus a liquidity count after it. bars: [(epoch, o, h, l, c, v)] sorted. No returns."""
    minute = ts - (ts % 60)
    before = [b for b in bars if b[0] <= minute]
    if not before:
        return None
    last = before[-1]
    if minute - last[0] > 300:                      # no print within 5 minutes of the alert: price unusable (prereg: dropped)
        return {"usable": False}
    cut = datetime.fromtimestamp(ts, ET).replace(hour=9, minute=20, second=0, microsecond=0).timestamp()
    after = [b for b in bars if minute < b[0] < cut]
    return {"usable": True, "px": float(last[4]), "bars_5m": sum(1 for b in before if minute - b[0] <= 300 and b[5] > 0),
            "bars_total_pre": len(before), "after_prints": sum(1 for b in after if b[5] > 0), "after_minutes": int(max(0, (cut - minute) // 60))}


def fetch_day(client, day: str, syms: list) -> dict:
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    s0 = datetime.fromisoformat(day + "T04:00:00").replace(tzinfo=ET)
    s1 = datetime.fromisoformat(day + "T09:30:00").replace(tzinfo=ET)
    out = {}
    for i in range(0, len(syms), 25):
        df = client.get_stock_bars(StockBarsRequest(symbol_or_symbols=syms[i:i + 25], timeframe=TimeFrame.Minute, start=s0, end=s1,
                                                    feed=DataFeed.SIP)).df
        if df is None or df.empty:
            continue
        for s in df.index.get_level_values(0).unique():
            x = df.xs(s, level=0)
            out[s] = [(t.timestamp(), float(o), float(h), float(lo), float(c), float(v)) for t, o, h, lo, c, v in
                      zip(x.index, x["open"], x["high"], x["low"], x["close"], x["volume"])]
        time.sleep(0.3)
    return out


def blocked_now() -> bool:
    n = datetime.now(ET)
    return n.weekday() < 5 and (9, 0) <= (n.hour, n.minute) < (16, 30)


def run_count() -> int:
    if blocked_now():
        print("refused: no Alpaca pulls 09:00-16:30 ET on weekdays (desk API budget)")
        return 1
    import alpaca_api as aa
    from config import load_config_ro
    client = aa.connect_data_client(dict(load_config_ro() or {}))
    today = datetime.now(ET).strftime("%Y-%m-%d")
    days = sorted(p.name for p in SESS.glob("20??-??-??") if (p / "discord.jsonl.gz").exists() and p.name < today)
    rows, excluded = [], []
    for d in days:
        al = first_alerts(d)
        if not al or min(al.values()) > datetime.fromisoformat(d + "T07:00:00").replace(tzinfo=ET).timestamp():
            excluded.append(d)                       # prereg: a session with no alert line before 07:00 ET is excluded (recorder not up)
            continue
        bars = fetch_day(client, d, sorted(al))
        for sym, ts in al.items():
            c = classify(bars.get(sym, []), ts)
            rows.append({"day": d, "sym": sym, "ts": ts, **(c or {"usable": False})})
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "count.json").write_text(json.dumps({"written": time.time(), "excluded_sessions": excluded, "rows": rows}))
    print(report(rows, excluded))
    return 0


def report(rows: list, excluded: list) -> str:
    by_day = defaultdict(list)
    for r in rows:
        by_day[r["day"]].append(r)
    use = [r for r in rows if r.get("usable")]
    lines = [f"Momentum alerts, premarket window 04:00-09:15 ET: {len(rows)} name-days over {len(by_day)} sessions "
             f"({len(excluded)} sessions excluded: no alert line before 07:00 ET); usable price at the alert {len(use)} ({len(rows) - len(use)} dropped)", ""]
    lines += ["| price band | name-days | per session | traded in the 5 min before the alert | median minutes with prints alert->09:20 |", "|---|---|---|---|---|"]
    for lab, lo, hi in BANDS:
        g = [r for r in use if lo <= r["px"] < hi]
        if not g:
            continue
        liq = sum(1 for r in g if r["bars_5m"] >= 2)
        med = sorted(r["after_prints"] for r in g)[len(g) // 2]
        lines.append(f"| {lab} | {len(g)} | {len(g) / max(1, len(by_day)):.1f} | {liq} ({100 * liq / len(g):.0f}%) | {med} |")
    p2 = [r for r in use if r["px"] >= 2]
    lines += ["", f"$2+ name-days {len(p2)}; at least 2 traded minutes in the 5 min before the alert {sum(1 for r in p2 if r['bars_5m'] >= 2)}; "
              f"sessions with at least one $2+ name {len({r['day'] for r in p2})}. "
              "These are the ceilings for the scored sample: the live arm will take a fraction of them (prereg power rule: >= 150 filled arm trades)."]
    return "\n".join(lines)


def main(argv=None) -> int:
    a = (argv if argv is not None else sys.argv[1:]) or ["count"]
    if a[0] == "count":
        return run_count()
    print("only 'count' is built (see the prereg's amended_scope)")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
