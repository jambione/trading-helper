#!/usr/bin/env python3
"""Batch 2, T1 (DIAGNOSTIC): where does the time - and the price - go between the real turn and the desk's fill?
Spec: docs/studies/batch2_prereg.json tests.T1_lag_audit + amended.T1 (1)-(4). No pass/fail.

For every paper BUY fill 2026-09-16 .. 10-23 (Alpaca orders, read only) and the last arm_ok decision-ledger row for
that symbol within 120 s before it:
  (a) minute of the lowest SIP low in the 30 minutes before the fill
  (b) minute of the lowest fast %R (IEX lines, primary; SIP lines, second row) in that window (real minutes)
  (c) the first real minute after (b) where fast rose >= 5 points off that low (the 'turn')
  (d) the arm timestamp, priced at the SIP close of the minute before it
  (e) the fill timestamp and filled_avg_price
Delays (s) and price steps (bp) a->c, c->d, d->e; forward 5/15/30-minute SIP returns from the turn close vs from the
fill (labelled NOT TRADABLE: the window is chosen knowing a fill followed). Bars: the warehouse when the day and name
are in it, else fetched (IEX + SIP, data-only account) for the fill day and the previous session.
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wr_trend_trade as W  # noqa: E402

ROOT = W.ROOT
OUT = os.path.join(ROOT, "ai_reports", "lag_audit")
ET = ZoneInfo("America/New_York")
SINCE, UNTIL, ARM_WINDOW_S, WIN, TURN = "2026-09-16", "2026-10-23", 120, 30, 5.0


def fills() -> list[dict]:
    sys.path.insert(0, ROOT)
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest
    from config import load_config
    c = load_config() or {}
    tc = TradingClient(c.get("api_key"), c.get("secret_key"), paper=True)
    since = datetime.fromisoformat(SINCE).replace(tzinfo=ET)
    until = datetime.fromisoformat(UNTIL).replace(tzinfo=ET) + timedelta(days=1)
    out = []
    while True:
        got = tc.get_orders(GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=500, after=since, until=until,
                                             direction="desc"))
        out += [o for o in got if o.filled_at and o.filled_avg_price and float(o.filled_qty or 0) > 0
                and str(getattr(o.side, "value", o.side)).lower() == "buy"]
        if len(got) < 500:
            break
        until = min(o.submitted_at for o in got)
    return [{"sym": o.symbol, "t": o.filled_at.timestamp(), "px": float(o.filled_avg_price)} for o in out]


def arm_rows(day: str) -> dict[str, list]:
    p = os.path.join(ROOT, "ai_reports", "decision_ledger", f"{day}.jsonl")
    by = defaultdict(list)
    if os.path.exists(p):
        for line in open(p):
            r = json.loads(line)
            if r.get("stage") == "arm" and r.get("arm_ok"):
                by[r["symbol"]].append(r)
    return by


def bars_for(sym: str, day: str, prev: str, wh_days: set, cl) -> dict:
    """{'sip'|'iex': (prev rows, today rows)} as arrays [minute, o, h, l, c, v]."""
    import indicator_confirm_hist as H
    out = {}
    for feed in ("sip", "iex"):
        if day in wh_days and prev in wh_days:
            t, p = H.load_raw(feed, day).get(sym), H.load_raw(feed, prev).get(sym)
            if t is not None and p is not None:
                out[feed] = (p, t)
                continue
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        rows = {}
        for d in (prev, day):
            a = datetime.fromisoformat(d).replace(hour=9, minute=30, tzinfo=ET)
            for attempt in range(5):
                try:
                    got = cl.get_stock_bars(StockBarsRequest(symbol_or_symbols=sym, timeframe=TimeFrame.Minute,
                                                             start=a, end=a.replace(hour=15, minute=59, second=59),
                                                             feed=DataFeed.SIP if feed == "sip" else DataFeed.IEX,
                                                             adjustment=Adjustment.SPLIT)).data.get(sym, [])
                    break
                except Exception:  # noqa: BLE001
                    time.sleep(10 * (attempt + 1))
            else:
                raise SystemExit(f"fetch failed {sym} {d} {feed} (C4)")
            rows[d] = np.array([[x.timestamp.astimezone(ET).hour * 60 + x.timestamp.astimezone(ET).minute, x.open,
                                 x.high, x.low, x.close, x.volume] for x in got], dtype=float).reshape(-1, 6)
            time.sleep(0.35)
        out[feed] = (rows[prev], rows[day])
    return out


def audit_one(f: dict, arm: dict | None, bars: dict) -> dict:
    tf = datetime.fromtimestamp(f["t"], ET)
    fm = tf.hour * 60 + tf.minute
    sip_today = {int(r[0]): r for r in bars["sip"][1]}
    lows = {m: sip_today[m][3] for m in range(fm - WIN, fm) if m in sip_today}
    if not lows:
        return {"skip": "no_sip_window"}
    a_min = min(lows, key=lambda m: (lows[m], m))
    out = {"sym": f["sym"], "fill_min": fm, "fill_px": f["px"], "a_min": a_min, "a_px": lows[a_min]}
    for feed in ("iex", "sip"):
        prev, today = bars[feed]
        if not len(prev) or not len(today):
            out[f"{feed}_turn"] = None
            continue
        fast, slow = W.lines(prev, today)
        rm = [m for m in range(fm - WIN, fm) if W.real(fast, m)]
        if not rm:
            out[f"{feed}_turn"] = None
            continue
        b = min(rm, key=lambda m: (fast[m], m))
        turn = next((m for m in rm if m > b and fast[m] >= fast[b] + TURN), None)
        out[f"{feed}_low_min"], out[f"{feed}_turn"] = b, turn
    tn = out.get("iex_turn")
    if tn is not None and tn in sip_today:
        out["turn_px"] = sip_today[tn][4]
        for k in (5, 15, 30):
            j = max((m for m in sip_today if m <= tn + k), default=None)
            jf = max((m for m in sip_today if m <= fm + k), default=None)
            out[f"fwd{k}_from_turn"] = sip_today[j][4] / out["turn_px"] - 1 if j else None
            out[f"fwd{k}_from_fill"] = sip_today[jf][4] / f["px"] - 1 if jf else None
    if arm is not None:
        ta = datetime.fromtimestamp(arm["ts"], ET)
        am = ta.hour * 60 + ta.minute
        prior = max((m for m in sip_today if m < am), default=None)
        out.update({"arm_min": am, "arm_s_before_fill": f["t"] - arm["ts"],
                    "arm_px": sip_today[prior][4] if prior else None,
                    "arm_kind": arm.get("arm_why") or arm.get("arm_bucket") or "unknown"})
    return out


def q(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    return {"n": len(xs), "p25": round(xs[len(xs) // 4], 2), "median": round(xs[len(xs) // 2], 2),
            "p75": round(xs[(3 * len(xs)) // 4], 2)}


def main() -> None:
    import indicator_confirm_hist as H
    from alpaca.data.historical import StockHistoricalDataClient
    s = json.load(open(os.path.join(ROOT, "secrets_data.json")))
    cl = StockHistoricalDataClient(s["alpaca_data_api_key"], s["alpaca_data_secret_key"])
    wh = set(H.warehouse_days())
    cal = sorted(wh | {(datetime.fromisoformat(SINCE) + timedelta(days=i)).strftime("%Y-%m-%d")
                       for i in range(60) if (datetime.fromisoformat(SINCE) + timedelta(days=i)).weekday() < 5})
    st, rows = Counter(), []
    fl = fills()
    st["fills"] = len(fl)
    arms_cache = {}
    for f in sorted(fl, key=lambda x: x["t"]):
        day = datetime.fromtimestamp(f["t"], ET).strftime("%Y-%m-%d")
        if not (SINCE <= day <= UNTIL):
            continue
        prev = max(d for d in cal if d < day)
        if day not in arms_cache:
            arms_cache = {day: arm_rows(day)}
        cand = [r for r in arms_cache[day].get(f["sym"], []) if 0 <= f["t"] - r["ts"] <= ARM_WINDOW_S]
        arm = max(cand, key=lambda r: r["ts"]) if cand else None
        st["arm_matched" if arm else "arm_unmatched"] += 1
        try:
            bars = bars_for(f["sym"], day, prev, wh, cl)
        except SystemExit as e:
            st["fetch_failed"] += 1
            print(e, flush=True)
            continue
        r = audit_one(f, arm, bars)
        if "skip" in r:
            st[r["skip"]] += 1
            continue
        if r.get("iex_turn") is None:
            st["no_turn_before_fill"] += 1
        rows.append(r)
    res = {"stats": dict(st), "label": "DIAGNOSTIC; forward returns from the turn are NOT TRADABLE (hindsight window)"}
    for lab, sel in (("all", lambda r: True), ("presquare", lambda r: "presquare" in str(r.get("arm_kind"))),
                     ("square", lambda r: "square" in str(r.get("arm_kind")) and "presquare" not in
                      str(r.get("arm_kind")))):
        rs = [r for r in rows if sel(r)]
        res[lab] = {
            "n": len(rs),
            "sec_low_to_turn_iex": q([(r["iex_turn"] - r["a_min"]) * 60 for r in rs if r.get("iex_turn") is not None]),
            "sec_turn_to_arm": q([(r["arm_min"] - r["iex_turn"]) * 60 for r in rs
                                  if r.get("iex_turn") is not None and r.get("arm_min") is not None]),
            "sec_arm_to_fill": q([r.get("arm_s_before_fill") for r in rs]),
            "feed_component_sec_iex_minus_sip_turn": q([(r["iex_turn"] - r["sip_turn"]) * 60 for r in rs
                                                        if r.get("iex_turn") is not None
                                                        and r.get("sip_turn") is not None]),
            "bp_low_to_turn": q([(r["turn_px"] / r["a_px"] - 1) * 1e4 for r in rs if r.get("turn_px")]),
            "bp_turn_to_arm": q([(r["arm_px"] / r["turn_px"] - 1) * 1e4 for r in rs
                                 if r.get("turn_px") and r.get("arm_px")]),
            "bp_arm_to_fill": q([(r["fill_px"] / r["arm_px"] - 1) * 1e4 for r in rs if r.get("arm_px")]),
            "bp_low_to_fill": q([(r["fill_px"] / r["a_px"] - 1) * 1e4 for r in rs]),
            "NOT_TRADABLE_fwd": {f"fwd{k}": {"from_turn_bp": q([(r.get(f"fwd{k}_from_turn") or 0) * 1e4 for r in rs
                                                                if r.get(f"fwd{k}_from_turn") is not None]),
                                             "from_fill_bp": q([(r.get(f"fwd{k}_from_fill") or 0) * 1e4 for r in rs
                                                                if r.get(f"fwd{k}_from_fill") is not None])}
                                 for k in (5, 15, 30)},
            "arm_kinds": dict(Counter(str(r.get("arm_kind")) for r in rs))}
    os.makedirs(OUT, exist_ok=True)
    json.dump(res, open(os.path.join(OUT, "result.json"), "w"), indent=1, default=str)
    json.dump(rows, open(os.path.join(OUT, "rows.json"), "w"), default=str)
    print(json.dumps(res, indent=1, default=str))


if __name__ == "__main__":
    main()
