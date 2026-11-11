#!/usr/bin/env python3
"""pick_ledger.py - do the operator's own stock picks beat a fair control? (docs/RESTRUCTURE_PLAN_2026-11-11.md WP4,
docs/studies/pick_ledger_prereg.json)

Record a pick BEFORE the outcome, then let time score it. Nothing here places an order.

    python3 tools/pick_ledger.py add OKLO --thesis "why I think it runs" --months 6 --sell-if "what would make me sell"
    python3 tools/pick_ledger.py list
    .venv/bin/python tools/pick_ledger.py score          # needs Alpaca data: run on the mini

Rules (fixed in the prereg, enforced here):
- A pick is stamped with the time it was recorded. Its entry is the OPEN of the first session that starts after that time.
  There is no way to enter a past date. `--retro` records a pick made after the fact; it is listed and NEVER scored.
- The same symbol recorded again within 10 calendar days is refused (one idea, one pick).
- Scored at 21, 63 and 126 sessions (about 1, 3 and 6 months), open to open, adjusted prices, as the RATIO
  (1 + pick) / (1 + control) - 1 against SPY, and against the mean of 30 seeded random names from the liquid list.
  A pick is scored at its horizon whether or not the operator still holds it: the question is whether the PICK was good.
- The verdict is read once, on the FIRST 30 scoreable picks in the order they were recorded, when all 30 have a closed
  63-session window. Later picks never change it. Until then `score` shows each pick's progress and says NOT YET.

The ledger is docs/pick_ledger.jsonl. Commit it after every `add`: git history is the proof a pick came before its outcome.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(os.environ.get("REPO") or Path(__file__).resolve().parents[1])
LEDGER = ROOT / "docs" / "pick_ledger.jsonl"
ET = ZoneInfo("America/New_York")
HORIZONS = (21, 63, 126)
PRIMARY, NEED, N_CONTROL, DEDUPE_DAYS = 63, 30, 30, 10


# ---------- ledger ----------
def read_ledger(path: Path = LEDGER) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def add_pick(symbol: str, thesis: str, months: float, sell_if: str, *, now: datetime | None = None, retro: bool = False,
             path: Path = LEDGER) -> dict:
    sym = symbol.strip().upper()
    if not re.fullmatch(r"[A-Z]{1,5}(\.[A-Z])?", sym):
        raise ValueError(f"'{symbol}' is not a stock symbol")
    if sym in ("SPY", "QQQ"):
        raise ValueError("the index is the control, not a pick")
    if len(thesis.strip()) < 15:
        raise ValueError("write the thesis in a full sentence (at least 15 characters): it is what gets judged later")
    if len(sell_if.strip()) < 8:
        raise ValueError("say what would make you sell (at least 8 characters)")
    if not 0.5 <= months <= 36:
        raise ValueError("intended hold must be between 0.5 and 36 months")
    now = (now or datetime.now(ET)).astimezone(ET)
    for p in read_ledger(path):
        if p["symbol"] == sym and not p.get("retro") and not retro:
            if abs((now - datetime.fromisoformat(p["recorded_at"])).days) < DEDUPE_DAYS:
                raise ValueError(f"{sym} was already recorded on {p['recorded_at'][:10]} (one idea, one pick; wait {DEDUPE_DAYS} days)")
    rec = {"id": hashlib.sha1(f"{sym}|{now.isoformat()}".encode()).hexdigest()[:10], "symbol": sym, "recorded_at": now.isoformat(timespec="seconds"),
           "thesis": thesis.strip(), "hold_months": months, "sell_if": sell_if.strip(), "retro": bool(retro)}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(rec) + "\n")
    return rec


# ---------- scoring (pure) ----------
def entry_index(days: list[str], recorded_at: str) -> int | None:
    """Index of the first session whose 09:30 ET open is after the record time; None when it has not happened yet."""
    t = datetime.fromisoformat(recorded_at).astimezone(ET)
    for i, d in enumerate(days):
        y, m, dd = map(int, d.split("-"))
        if datetime(y, m, dd, 9, 30, tzinfo=ET) > t:
            return i
    return None


def span(bars: dict[str, tuple], days: list[str], a: int, b: int) -> float | None:
    """Simple return, open of days[a] to open of days[b]; a name with no bar at b is sold at its last close before b."""
    e = bars.get(days[a])
    if not e:
        return None
    x = bars.get(days[b])
    if x:
        return x[0] / e[0] - 1.0
    for k in range(b - 1, a - 1, -1):
        if days[k] in bars:
            return bars[days[k]][1] / e[0] - 1.0
    return None


def control_names(pick: dict, universe: list[str], n: int = N_CONTROL) -> list[str]:
    pool = sorted(s for s in universe if s != pick["symbol"])
    return random.Random(pick["id"]).sample(pool, min(n, len(pool)))


def score_pick(pick: dict, days: list[str], bars: dict[str, dict], universe: list[str]) -> dict:
    """days = sessions with an SPY bar, ascending. bars = {symbol: {day: (open, close)}} adjusted."""
    out = {"id": pick["id"], "symbol": pick["symbol"], "recorded_at": pick["recorded_at"], "status": "retro (never scored)" if pick.get("retro") else "", "h": {}}
    if pick.get("retro"):
        return out
    a = entry_index(days, pick["recorded_at"])
    if a is None or pick["symbol"] not in bars or days[a] not in bars[pick["symbol"]]:
        out["status"] = "waiting for its first open" if a is None else "no bar on the entry day (not scoreable)"
        return out
    out["entry_day"], out["entry_px"] = days[a], bars[pick["symbol"]][days[a]][0]
    ctl = control_names(pick, universe)
    last = len(days) - 1
    for h in HORIZONS:
        b = a + h
        closed = b <= last
        bb = min(b, last)
        if bb <= a:
            continue
        r, s = span(bars[pick["symbol"]], days, a, bb), span(bars["SPY"], days, a, bb)
        cs = [x for x in (span(bars.get(c, {}), days, a, bb) for c in ctl) if x is not None]
        if r is None or s is None:
            continue
        out["h"][h] = {"closed": closed, "sessions": bb - a, "pick": r, "spy": s, "vs_spy": (1 + r) / (1 + s) - 1,
                       "vs_random": ((1 + r) / (1 + sum(cs) / len(cs)) - 1) if len(cs) >= 10 else None, "n_control": len(cs)}
    out["status"] = "scored"
    return out


def verdict(scored: list[dict], h: int = PRIMARY, need: int = NEED) -> dict:
    """The first `need` scoreable picks in record order; read only when every one of them has a closed window."""
    ok = [s for s in sorted(scored, key=lambda s: s["recorded_at"]) if s["status"] == "scored"][:need]
    closed = [s for s in ok if s["h"].get(h, {}).get("closed")]
    res = {"horizon": h, "scoreable": len(ok), "closed": len(closed), "need": need}
    if len(ok) < need or len(closed) < need:
        return {**res, "verdict": f"NOT YET ({len(closed)} of {need} picks have a closed {h}-session window)"}
    x = [s["h"][h]["vs_spy"] for s in closed]
    y = [s["h"][h]["vs_random"] for s in closed if s["h"][h]["vs_random"] is not None]
    m = sum(x) / len(x)
    sd = math.sqrt(sum((v - m) ** 2 for v in x) / (len(x) - 1))
    t = m / (sd / math.sqrt(len(x))) if sd > 0 else float("nan")
    ahead = sum(v > 0 for v in x) / len(x)
    med = sorted(x)[len(x) // 2]
    res.update(mean_vs_spy=m, t=t, ahead=ahead, median_vs_spy=med, mean_vs_random=(sum(y) / len(y)) if y else None,
               best_share=(max(x) / sum(x)) if sum(x) > 0 else None)
    passed = m > 0 and t >= 2.0 and ahead > 0.5 and med > 0 and (not y or sum(y) / len(y) > 0)
    res["verdict"] = "PASS" if passed else "FAIL"
    return res


# ---------- data (mini only) ----------
def fetch(symbols: list[str], start: datetime) -> dict[str, dict]:
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    s = json.load(open(ROOT / "secrets_data.json"))
    cl = StockHistoricalDataClient(s["alpaca_data_api_key"], s["alpaca_data_secret_key"])
    out: dict[str, dict] = {}
    end = datetime.now(ET) - timedelta(minutes=20)
    for i in range(0, len(symbols), 50):
        r = cl.get_stock_bars(StockBarsRequest(symbol_or_symbols=symbols[i:i + 50], timeframe=TimeFrame.Day, start=start, end=end,
                                               feed=DataFeed.SIP, adjustment=Adjustment.ALL))
        for sym, rows in (r.data or {}).items():
            out[sym] = {b.timestamp.astimezone(ET).strftime("%Y-%m-%d"): (float(b.open), float(b.close)) for b in rows}
    return out


def liquid_universe() -> list[str]:
    """Names liquid on the last day of the liquidity table (extended nightly by WP2). Empty when the table is not here."""
    p = ROOT / "ai_reports" / "gapdown" / "daily.pkl"
    if not p.exists():
        return []
    import pickle
    d = pickle.load(open(p, "rb"))
    last = max(d["adj"]["SPY"])
    return sorted(s for s, ds in d["liquid"].items() if ds and ds[-1] == last)


# ---------- cli ----------
def pct(v) -> str:
    return "   n/a" if v is None else f"{100 * v:+6.1f}%"


def cmd_score() -> int:
    picks = read_ledger()
    if not picks:
        print("no picks recorded yet")
        return 0
    uni = liquid_universe()
    live = [p for p in picks if not p.get("retro")]
    start = min(datetime.fromisoformat(p["recorded_at"]) for p in picks) - timedelta(days=5)
    ctl = sorted({c for p in live for c in control_names(p, uni)})
    bars = fetch(sorted({p["symbol"] for p in live} | {"SPY"} | set(ctl)), start)
    days = sorted(bars["SPY"])
    scored = [score_pick(p, days, bars, uni) for p in picks]
    print(f"{len(picks)} picks; control universe {len(uni)} liquid names; prices through {days[-1]}\n")
    print(f"{'symbol':<7}{'recorded':<12}{'entry':<12}" + "".join(f"{f'{h}s pick':>9}{'vs SPY':>8}{'vs rand':>8}{'':>2}" for h in HORIZONS))
    for s in scored:
        if s["status"] != "scored":
            print(f"{s['symbol']:<7}{s['recorded_at'][:10]:<12}{s['status']}")
            continue
        row = f"{s['symbol']:<7}{s['recorded_at'][:10]:<12}{s['entry_day']:<12}"
        for h in HORIZONS:
            x = s["h"].get(h)
            row += f"{'':>27}" if not x else f"{pct(x['pick']):>9}{pct(x['vs_spy']):>8}{pct(x['vs_random']):>8}{'' if x['closed'] else '*':>2}"
        print(row)
    print("\n* = window still open (marked to the latest open; information only, not part of any verdict)")
    v = verdict(scored)
    print(f"\nVERDICT at {PRIMARY} sessions on the first {NEED} picks: {v['verdict']}")
    if "mean_vs_spy" in v:
        print(f"  mean vs SPY {pct(v['mean_vs_spy'])}  median {pct(v['median_vs_spy'])}  t {v['t']:+.2f}  ahead of SPY {100 * v['ahead']:.0f}%  "
              f"mean vs random names {pct(v['mean_vs_random'])}  best single pick = {pct(v['best_share'])} of the total")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Record stock picks before the outcome and score them against SPY.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="record a pick now")
    a.add_argument("symbol")
    a.add_argument("--thesis", required=True)
    a.add_argument("--months", type=float, required=True, help="how long you intend to hold")
    a.add_argument("--sell-if", required=True, help="what would make you sell")
    a.add_argument("--retro", action="store_true", help="a pick made after the fact: listed, never scored")
    sub.add_parser("list")
    sub.add_parser("score")
    args = ap.parse_args(argv)
    if args.cmd == "add":
        try:
            r = add_pick(args.symbol, args.thesis, args.months, args.sell_if, retro=args.retro)
        except ValueError as e:
            print(f"not recorded: {e}")
            return 1
        print(f"recorded {r['symbol']} at {r['recorded_at']} ({'RETRO, never scored' if r['retro'] else 'entry = the next session open'}).\n"
              f"Now commit it:  git add docs/pick_ledger.jsonl && git commit -m 'pick: {r['symbol']}' && git push")
        return 0
    if args.cmd == "list":
        for p in read_ledger():
            print(f"{p['recorded_at'][:16]}  {p['symbol']:<6} {p['hold_months']:g} mo  {'RETRO  ' if p.get('retro') else ''}{p['thesis']}  | sell if: {p['sell_if']}")
        return 0
    return cmd_score()


if __name__ == "__main__":
    sys.exit(main())
