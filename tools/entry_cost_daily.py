#!/usr/bin/env python3
"""Entry/exit cost per fill against the real SIP quote (operator 11/06: lower the entry cost; IEX weeks vs SIP weeks).

For every desk fill in ai_reports/fills/DAY.jsonl, pull the SIP NBBO in force at the fill second (free once 15 min old)
and measure what the fill cost against the midpoint:
    buy  cost_bp = (fill / mid - 1) x 1e4      sell cost_bp = (1 - fill / mid) x 1e4
    half_spread_bp = (ask - bid) / 2 / mid x 1e4
Positive cost = paid more than mid. Writes ai_reports/entry_cost/DAY.json (cached, one per day) and prints a day line.
Never-green share per day comes from outcomes.jsonl (peak < entry + 5 bp), so the two can be compared side by side.

    .venv/bin/python tools/entry_cost_daily.py                    # yesterday-or-today after the close, plus any missing of the last 20
    .venv/bin/python tools/entry_cost_daily.py --day 2026-11-05
    .venv/bin/python tools/entry_cost_daily.py --summary          # table only, from cached days (no API)

Data budget: one SIP quote request per fill (~40-60 a day). Refuses to fetch 09:00-16:30 ET on weekdays (desk API budget).
"""
from __future__ import annotations

import argparse
import json
import statistics as st
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
ET = ZoneInfo("America/New_York")
FILLS = ROOT / "ai_reports" / "fills"
OUT = ROOT / "ai_reports" / "entry_cost"
MAX_QUOTE_AGE_SEC = 5.0


def parse_ts(s: str) -> float:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()


def cost_bp(side: str, fill: float, bid: float, ask: float) -> dict | None:
    """Pure: cost of one fill against the quote. None when the quote is unusable."""
    if not (fill > 0 and bid > 0 and ask >= bid):
        return None
    mid = (bid + ask) / 2.0
    c = (fill / mid - 1.0) * 1e4 if side == "buy" else (1.0 - fill / mid) * 1e4
    return {"cost_bp": round(c, 2), "half_spread_bp": round((ask - bid) / 2.0 / mid * 1e4, 2), "mid": round(mid, 4)}


def day_fills(day: str) -> list[dict]:
    p = FILLS / f"{day}.jsonl"
    out = []
    if not p.exists():
        return out
    for line in p.read_text().splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("event") == "fill" and r.get("status") == "filled" and r.get("filled_at") and r.get("filled_avg_price"):
            out.append({"symbol": r["symbol"], "side": str(r.get("side") or "").lower(), "fill": float(r["filled_avg_price"]),
                        "ts": parse_ts(r["filled_at"]), "qty": float(r.get("filled_qty") or 0), "type": r.get("type")})
    return out


def never_green_by_day() -> dict:
    out: dict = {}
    p = ROOT / "ai_reports" / "outcomes.jsonl"
    if not p.exists():
        return out
    for line in p.read_text().splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        e, pk = r.get("entry_price"), r.get("peak_price")
        if not e or pk is None:
            continue
        d = datetime.fromtimestamp(float(r.get("entry_time") or r.get("ts") or 0), ET).strftime("%Y-%m-%d")
        out.setdefault(d, []).append(float(pk) < float(e) * (1 + 5e-4))
    return {d: (sum(v) / len(v), len(v)) for d, v in out.items()}


def fetch_quote(client, sym: str, ts: float):
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockQuotesRequest
    end = datetime.fromtimestamp(ts + 0.001, timezone.utc)
    q = client.get_stock_quotes(StockQuotesRequest(symbol_or_symbols=sym, start=end - timedelta(seconds=MAX_QUOTE_AGE_SEC), end=end,
                                                   feed=DataFeed.SIP, limit=10000))
    rows = (q.data or {}).get(sym) or []
    rows = [x for x in rows if x.timestamp.timestamp() <= ts]
    return rows[-1] if rows else None


def blocked_now() -> bool:
    n = datetime.now(ET)
    return n.weekday() < 5 and (9, 0) <= (n.hour, n.minute) < (16, 30)


def run_day(day: str, client) -> dict:
    rows = []
    for f in day_fills(day):
        try:
            q = fetch_quote(client, f["symbol"], f["ts"])
        except Exception as e:  # noqa: BLE001
            rows.append({**f, "err": f"{type(e).__name__}: {e}"[:120]})
            continue
        c = cost_bp(f["side"], f["fill"], float(q.bid_price), float(q.ask_price)) if q else None
        rows.append({**f, **(c or {"err": "no_usable_quote"})})
        time.sleep(0.25)
    OUT.mkdir(parents=True, exist_ok=True)
    rec = {"day": day, "written": time.time(), "feed_quotes": "sip", "rows": rows}
    (OUT / f"{day}.json").write_text(json.dumps(rec))
    return rec


def summarize(rec: dict, ng: dict) -> dict:
    ok = [r for r in rec["rows"] if "cost_bp" in r]
    b = [r for r in ok if r["side"] == "buy"]
    s = [r for r in ok if r["side"] == "sell"]
    med = lambda xs: round(st.median(xs), 1) if xs else None  # noqa: E731
    g = ng.get(rec["day"])
    return {"day": rec["day"], "buys": len(b), "sells": len(s), "missing": len(rec["rows"]) - len(ok),
            "buy_cost": med([r["cost_bp"] for r in b]), "sell_cost": med([r["cost_bp"] for r in s]),
            "half_spread": med([r["half_spread_bp"] for r in ok]), "ng_share": None if not g else round(g[0], 2), "ng_n": None if not g else g[1]}


def ngtxt(s: dict) -> str:
    return "-" if s["ng_share"] is None else f"{100 * s['ng_share']:.0f}% of {s['ng_n']}"


def table(days: list[str]) -> list[str]:
    ng = never_green_by_day()
    out = ["| day | buys | buy cost vs mid (median bp) | sells | sell cost vs mid | SIP half-spread | never-green share | no quote |",
           "|---|---|---|---|---|---|---|---|"]
    for d in sorted(days, reverse=True):
        p = OUT / f"{d}.json"
        if not p.exists():
            continue
        s = summarize(json.loads(p.read_text()), ng)
        f = lambda v: "-" if v is None else f"{v:+.1f}"  # noqa: E731
        out.append(f"| {d} | {s['buys']} | {f(s['buy_cost'])} | {s['sells']} | {f(s['sell_cost'])} | "
                   f"{'-' if s['half_spread'] is None else s['half_spread']} | "
                   f"{ngtxt(s)} | {s['missing']} |")
    return out


def brief(n: int = 10) -> list[str]:
    days = sorted(p.stem for p in OUT.glob("20??-??-??.json"))[-n:] if OUT.exists() else []
    head = ["## Entry cost (fill vs SIP midpoint; IEX weeks are the baseline for the SIP month)", ""]
    return head + (table(days) if days else ["No entry-cost days yet (tools/entry_cost_daily.py runs nightly)."])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day")
    ap.add_argument("--backfill", type=int, default=20, help="also fill any missing day among the last N fill days")
    ap.add_argument("--summary", action="store_true")
    a = ap.parse_args(argv)
    if a.summary:
        print("\n".join(brief(20)))
        return 0
    if blocked_now():
        print("refused: no SIP quote pulls 09:00-16:30 ET on weekdays (desk API budget)")
        return 1
    import alpaca_api as aa
    from config import load_config_ro
    client = aa.connect_data_client(dict(load_config_ro() or {}))
    todo = [a.day] if a.day else []
    have = {p.stem for p in OUT.glob("20??-??-??.json")} if OUT.exists() else set()
    fill_days = sorted(p.stem for p in FILLS.glob("20??-??-??.jsonl"))[-a.backfill:]
    now = datetime.now(ET)
    today = now.strftime("%Y-%m-%d")
    after_close = (now.hour, now.minute) >= (16, 30)
    todo += [d for d in fill_days if d not in have and d not in todo and (d < today or after_close)]
    for d in todo:
        rec = run_day(d, client)
        print(json.dumps(summarize(rec, never_green_by_day())))
    print("\n".join(brief(20)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
