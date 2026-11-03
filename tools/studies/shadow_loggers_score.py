#!/usr/bin/env python3
"""Score the two shadow loggers (operator 2026-11-03 "build loggers").

READ ONLY. Never places, cancels or changes an order. Run on the mini AFTER
the close (it uses the historical SIP data key, ALPACA_DATA_API_KEY when set,
so it does not spend the desk's trading budget).

1. exits  - would a sell limit at the SIP midpoint have filled within a collar?
   Source: ai_reports/fills/<day>.jsonl (every desk SELL submit + its fill,
   logged since 2026-09-16), so no live logger is needed and past sessions
   score too. For each exit, at the submit instant t0:
     * NBBO = last SIP quote at or before t0; limit = mid rounded to the cent.
     * STRICT fill: a SIP print strictly ABOVE the limit inside the collar
       (a print AT the limit may be someone else's queue). LENIENT (>=) is
       reported beside it, never as the headline.
     * Unfilled at the collar: sells at the SIP bid at t0 + collar (a market
       order then). The real market fill is what it is compared with.
     delta_bp = (simulated price - actual fill) / actual fill. Positive = the
     midpoint route would have been better.

2. pullback - forward moves after each pullback-shadow trigger
   (ai_reports/pullback_shadow/<day>.jsonl, ai_watch_pullback_shadow).
   Entry = OPEN of the first 1-minute SIP bar after the trigger (never the
   shadow quote: shadow prices inflate backtests). Forward close-to-entry at
   5/15/30 min, 15-min MFE/MAE, against 5 same-name same-day random minutes
   (seeded) as the control. Gross, plus net at a 3 bp round trip.

Usage (mini, repo root):
    .venv/bin/python tools/studies/shadow_loggers_score.py exits --day 2026-11-03
    .venv/bin/python tools/studies/shadow_loggers_score.py exits --since 2026-10-26
    .venv/bin/python tools/studies/shadow_loggers_score.py pullback --day 2026-11-04
Writes ai_reports/shadow_scores/<kind>_<first>_<last>.json.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import statistics
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
for p in (str(ROOT), str(ROOT / "tools")):
    if p not in sys.path:
        sys.path.insert(0, p)

ET = ZoneInfo("America/New_York")
REPORTS = ROOT / "ai_reports"
RT_COST_BP = 3.0
HORIZONS = (5, 15, 30)


# ---------------------------------------------------------------- pure parts
def cent(x: float) -> float:
    return math.floor(float(x) * 100.0 + 0.5) / 100.0


def nbbo_at(quotes: list[tuple[float, float, float]], t: float) -> tuple[float, float] | None:
    """Last sane (bid, ask) at or before t. quotes = [(ts, bid, ask)] sorted."""
    best = None
    for ts, b, a in quotes:
        if ts > t:
            break
        if b > 0 and a > 0 and a >= b:
            best = (b, a)
    return best


def simulate_mid_exit(t0: float, quotes: list, trades: list, actual_fill: float,
                      collar_sec: float = 10.0) -> dict | None:
    """One exit. quotes [(ts,bid,ask)], trades [(ts,price)], both sorted.
    None when there is no NBBO at t0 or at the collar."""
    q0 = nbbo_at(quotes, t0)
    if q0 is None or not (actual_fill > 0):
        return None
    bid0, ask0 = q0
    limit = cent((bid0 + ask0) / 2.0)
    window = [p for ts, p in trades if t0 < ts <= t0 + collar_sec]
    strict = any(p > limit + 1e-9 for p in window)
    lenient = any(p >= limit - 1e-9 for p in window)
    q1 = nbbo_at(quotes, t0 + collar_sec)
    if q1 is None:
        return None
    timeout_px = q1[0]  # market sell at the collar = the bid then
    sim = limit if strict else timeout_px
    lsim = limit if lenient else timeout_px
    return {
        "bid0": bid0, "ask0": ask0, "limit": limit,
        "spread_bp": round((ask0 - bid0) / ((ask0 + bid0) / 2.0) * 1e4, 2),
        "filled_strict": strict, "filled_lenient": lenient,
        "sim_px": sim,
        "delta_bp": round((sim - actual_fill) / actual_fill * 1e4, 2),
        "delta_lenient_bp": round((lsim - actual_fill) / actual_fill * 1e4, 2),
        "actual_vs_bid0_bp": round((actual_fill - bid0) / bid0 * 1e4, 2),
    }


# Exit reasons that cut a failing trade: the name is falling, so waiting costs.
URGENT_REASONS = frozenset({"dead_trade", "no_progress", "double_check", "fill_through_stop",
                            "exh_falling_flatten", "stale_data"})
# Routed to market as well: the close must not wait into the closing print.
MARKET_REASONS = URGENT_REASONS | {"eod_liquidate"}
REACT_SEC = 3.0          # the desk's book tick: a bail / timeout acts one tick after it is seen
IEX_MAX_AGE_SEC = 15.0   # ai_watch_quote_max_age_sec: an older IEX quote is no quote


def cent_up(x: float) -> float:
    """Round a sell limit UP to the cent (the cautious side for a sell), exactly."""
    from decimal import ROUND_CEILING, Decimal
    return float(Decimal(repr(round(float(x), 6))).quantize(Decimal("0.01"), rounding=ROUND_CEILING))


def session_end(t: float) -> float:
    d = datetime.fromtimestamp(float(t), ET)
    return d.replace(hour=16, minute=0, second=0, microsecond=0).timestamp()


def nbbo_at_age(quotes: list, t: float, max_age: float) -> tuple[float, float] | None:
    """Last sane (bid, ask) at or before t, no older than max_age."""
    best = None
    for ts, b, a in quotes:
        if ts > t:
            break
        if b > 0 and a > 0 and a >= b:
            best = (ts, b, a)
    if best is None or t - best[0] > max_age:
        return None
    return best[1], best[2]


def simulate_policy_exit(t0: float, sip_quotes: list, sip_trades: list, iex_quotes: list,
                         actual_fill: float, *, reason: str | None, collar_sec: float = 10.0,
                         min_spread_bp: float = 4.0, bail_cents: float = 1.0,
                         fill_rule: str = "trade_through", react_sec: float = REACT_SEC) -> dict | None:
    """Policy P1 v2 (midpoint_policy_prereg.json, amended_1).

    Every DECISION uses what the desk can see live: the IEX quote (<= 15 s old) at t0
    and IEX quotes during the wait. The OUTCOME uses the full SIP tape.
      market (delta 0 = the fill that happened): reason unknown / urgent / eod, no fresh
        IEX quote, IEX book one cent wide, IEX spread < min_spread_bp.
      passive: sell limit at the IEX mid rounded UP to the cent.
        fill_rule trade_through: a SIP print strictly above the limit fills at the limit.
        fill_rule bid_cross: the SIP bid at or above the limit fills at the limit.
        bail: an IEX bid at or below the starting IEX bid minus bail_cents, before a fill,
          sells at the SIP bid react_sec later.
        timeout: sells at the SIP bid react_sec after the collar.
      The wait never runs past 16:00:00 ET.
    Also returns delta2_bp vs the SIP bid at t0 (a market sell with no paper delay).
    None when there is no SIP quote at t0 or at the execution instant."""
    if not (actual_fill > 0):
        return None
    sip0 = nbbo_at(sip_quotes, t0)
    if sip0 is None:
        return None
    sip_bid0 = sip0[0]
    end_cap = session_end(t0)
    iex0 = nbbo_at_age(iex_quotes, t0, IEX_MAX_AGE_SEC)
    route = "passive"
    spread_bp = None
    if reason is None:
        route = "market:unknown_reason"
    elif reason in MARKET_REASONS:
        route = "market:urgent" if reason in URGENT_REASONS else "market:eod"
    elif iex0 is None:
        route = "market:no_iex"
    else:
        ib, ia = iex0
        spread_bp = (ia - ib) / ((ia + ib) / 2.0) * 1e4
        if ia - ib <= 0.0105:
            route = "market:one_tick"
        elif spread_bp < min_spread_bp:
            route = "market:tight"
    base = {"route": route, "reason": reason, "sip_bid0": sip_bid0, "fill_rule": fill_rule,
            "iex_bid0": iex0[0] if iex0 else None, "iex_ask0": iex0[1] if iex0 else None,
            "iex_spread_bp": round(spread_bp, 2) if spread_bp is not None else None}
    if route != "passive":
        return {**base, "sim_px": actual_fill, "outcome": "market", "delta_bp": 0.0, "delta2_bp": 0.0}
    ib, ia = iex0
    limit = cent_up((ib + ia) / 2.0)
    end = min(t0 + collar_sec, end_cap)
    ev = [(ts, 0, p) for ts, p in sip_trades if t0 < ts <= end]
    ev += [(ts, 1, b) for ts, b, a in iex_quotes if t0 < ts <= end and b > 0 and a >= b]
    ev += [(ts, 2, b) for ts, b, a in sip_quotes if t0 < ts <= end and b > 0 and a >= b]
    ev.sort()
    sim = outcome = t_exec = None
    if limit <= sip_bid0 + 1e-9:
        # Marketable on arrival (stale / wide IEX put the limit at or under the real bid):
        # it fills at once at the SIP bid, not at the limit.
        sim, outcome = sip_bid0, "filled_marketable"
        ev = []
    for ts, kind, v in ev:
        if fill_rule == "trade_through" and kind == 0 and v > limit + 1e-9:
            q = nbbo_at(sip_quotes, ts)
            sim, outcome = max(limit, q[0] if q else limit), "filled"
            break
        if fill_rule == "bid_cross" and kind == 2 and v >= limit - 1e-9:
            sim, outcome = max(limit, v), "filled"
            break
        if kind == 1 and bail_cents and v <= ib - bail_cents / 100.0 + 1e-9:
            outcome, t_exec = "bail", min(ts + react_sec, end_cap)
            break
    if sim is None:
        if outcome is None:
            outcome, t_exec = "timeout", min(end + react_sec, end_cap)
        q = nbbo_at(sip_quotes, t_exec)
        if q is None:
            return None
        sim = q[0]
    return {**base, "limit": limit, "sim_px": sim, "outcome": outcome,
            "delta_bp": round((sim - actual_fill) / actual_fill * 1e4, 2),
            "delta2_bp": round((sim - sip_bid0) / sip_bid0 * 1e4, 2)}


def predict_limit_fill(t_start: float, t_end: float, limit: float, sip_quotes: list,
                       sip_trades: list, fill_rule: str) -> bool:
    """Calibration: would a resting sell limit at `limit` have filled in (t_start, t_end]?"""
    if fill_rule == "trade_through":
        return any(t_start < ts <= t_end and p > limit + 1e-9 for ts, p in sip_trades)
    return any(t_start < ts <= t_end and b >= limit - 1e-9 for ts, b, a in sip_quotes if b > 0 and a >= b)


def reason_for_exit(outcomes: list[dict], symbol: str, t0: float, window: float = 20.0) -> str | None:
    """close_reason of the desk outcome whose exit_time is nearest t0 (within window s)."""
    best = None
    for o in outcomes:
        if str(o.get("symbol", "")).upper() != symbol:
            continue
        try:
            dt = abs(float(o.get("exit_time")) - t0)
        except (TypeError, ValueError):
            continue
        if dt <= window and (best is None or dt < best[0]):
            best = (dt, o.get("close_reason") or o.get("closing_reason"))
    return best[1] if best else None


_TICKER_RE = __import__("re").compile(r"^[A-Z][A-Z.]{0,5}$")
_JUNK_SYMBOLS = frozenset({"TEST", "FAKE", "MOCK", "XXX"})


def _iso_ts(v) -> float | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def pair_sells(rows: list[dict]) -> list[dict]:
    """Market SELL submits joined to their fill by order_id (fills ledger rows).

    t0 is the broker's submitted_at (the ledger ts can lag it by seconds); fill_lag is
    filled_at - submitted_at. after_limit marks a market sell sent within 30 s after a
    SELL_LIMIT on the same name (the 10/05-10/15 exit_test fallbacks: selected on an
    adverse move). Junk symbols (TEST, ws-1, ...) are dropped."""
    subs = {r.get("order_id"): r for r in rows if r.get("event") == "submit" and r.get("order_id")}
    limit_ts: dict[str, list[float]] = {}
    for r in subs.values():
        if "LIMIT" in str(r.get("action", "")).upper() and "SELL" in str(r.get("action", "")).upper():
            limit_ts.setdefault(str(r.get("symbol")).upper(), []).append(float(r["ts"]))
    out = []
    for r in rows:
        if r.get("event") != "fill" or str(r.get("side", "")).lower() != "sell":
            continue
        s = subs.get(r.get("order_id"))
        if not s or str(s.get("action", "")).upper() != "SELL":
            continue
        sym = str(s.get("symbol") or r.get("symbol")).upper()
        if not _TICKER_RE.match(sym) or sym in _JUNK_SYMBOLS:
            continue
        try:
            px = float(r.get("filled_avg_price") or 0)
            qty = float(r.get("filled_qty") or 0)
        except (TypeError, ValueError):
            continue
        if px <= 0 or qty <= 0:
            continue
        sub_at = _iso_ts(r.get("submitted_at"))
        fill_at = _iso_ts(r.get("filled_at"))
        t0 = sub_at if sub_at is not None else float(s["ts"])
        out.append({"symbol": sym, "t0": t0, "ledger_ts": float(s["ts"]),
                    "fill_lag": (fill_at - sub_at) if (fill_at and sub_at) else None,
                    "after_limit": any(0 <= float(s["ts"]) - lt <= 30 for lt in limit_ts.get(sym, [])),
                    "qty": qty, "fill": px, "note": s.get("note"), "order_id": r.get("order_id")})
    out.sort(key=lambda x: x["t0"])
    return out


def limit_sells(rows: list[dict]) -> list[dict]:
    """The desk's real SELL_LIMIT orders, for calibration: limit, window, actual fill.
    The window ends at the next market SELL on the name within 60 s (the fallback),
    else 12 s after submit (cross_sec 10 + one ~2 s book tick, the desk's real wait)."""
    subs = [r for r in rows if r.get("event") == "submit" and "LIMIT" in str(r.get("action", "")).upper()
            and "SELL" in str(r.get("action", "")).upper()]
    mkts = [r for r in rows if r.get("event") == "submit" and str(r.get("action", "")).upper() == "SELL"]
    fills = {r.get("order_id"): r for r in rows if r.get("event") == "fill"}
    out = []
    for s in subs:
        sym = str(s.get("symbol")).upper()
        lim = None
        note = str(s.get("note") or "")
        if "lmt=" in note:
            try:
                lim = float(note.split("lmt=")[1].split()[0])
            except ValueError:
                lim = None
        if lim is None:
            try:
                lim = float(s.get("price") or 0) or None
            except (TypeError, ValueError):
                lim = None
        if not lim:
            continue
        f = fills.get(s.get("order_id"))
        t_start = (_iso_ts(f.get("submitted_at")) if f else None) or float(s["ts"])
        nxt = [float(m["ts"]) for m in mkts if str(m.get("symbol")).upper() == sym and 0 < float(m["ts"]) - float(s["ts"]) <= 60]
        t_end = min(nxt) if nxt else t_start + 12.0  # cross_sec 10 + one book tick (ai_positions.py)
        filled = bool(f and float(f.get("filled_qty") or 0) > 0)
        out.append({"symbol": sym, "t_start": t_start, "t_end": t_end, "limit": lim, "filled": filled,
                    "order_id": s.get("order_id"), "day": s.get("day")})
    return out


def forward_from_bars(bars: list[tuple], t_sig: float) -> dict | None:
    """bars [(ts_minute_start, o, h, l, c)] sorted. Entry = open of the first
    bar starting after t_sig."""
    idx = next((i for i, b in enumerate(bars) if b[0] > t_sig), None)
    if idx is None:
        return None
    e = float(bars[idx][1])
    if e <= 0:
        return None
    t_e = bars[idx][0]
    res = {"entry_ts": t_e, "entry": e}
    for h in HORIZONS:
        tgt = t_e + h * 60
        past = [b for b in bars[idx:] if b[0] + 60 <= tgt + 1e-6]
        if not past or past[-1][0] + 60 < tgt - 61:
            res[f"fwd{h}"] = None
            continue
        res[f"fwd{h}"] = (float(past[-1][4]) - e) / e * 1e4
    w = [b for b in bars[idx:] if b[0] < t_e + 15 * 60]
    res["mfe15"] = (max(float(b[2]) for b in w) - e) / e * 1e4 if w else None
    res["mae15"] = (min(float(b[3]) for b in w) - e) / e * 1e4 if w else None
    return res


def control_times(sym: str, day: str, bars: list[tuple], k: int = 5) -> list[float]:
    """k seeded random minutes 10:00-15:25 ET that have bars."""
    seed = int(hashlib.sha256(f"{sym}|{day}".encode()).hexdigest()[:12], 16)
    rng = random.Random(seed)
    pool = [b[0] for b in bars
            if "10:00" <= datetime.fromtimestamp(b[0], ET).strftime("%H:%M") <= "15:25"]
    if not pool:
        return []
    return [rng.choice(pool) - 1.0 for _ in range(k)]


def _mean(v):
    v = [x for x in v if x is not None]
    return (sum(v) / len(v)) if v else None


def _t(v):
    v = [x for x in v if x is not None]
    if len(v) < 3:
        return None
    sd = statistics.stdev(v)
    return (statistics.mean(v) / (sd / math.sqrt(len(v)))) if sd > 0 else None


# ---------------------------------------------------------------- data access
def _client():
    import fill_truth_report as ft
    ft._load_env()
    from alpaca.data.historical import StockHistoricalDataClient
    k = os.getenv("ALPACA_DATA_API_KEY") or os.getenv("ALPACA_API_KEY")
    s = os.getenv("ALPACA_DATA_SECRET_KEY") or os.getenv("ALPACA_SECRET_KEY")
    return StockHistoricalDataClient(k, s)


def _ts(x) -> float:
    return x.timestamp() if hasattr(x, "timestamp") else float(x)


def fetch_quotes_trades(cl, sym: str, t0: float, collar: float, iex: bool = False):
    """SIP quotes + trades from t0-30 s to t0+collar+REACT_SEC+3 s; with iex=True also
    the IEX quotes (what the desk sees live) over the same window."""
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockQuotesRequest, StockTradesRequest
    start = datetime.fromtimestamp(t0 - 30, tz=ET)
    end = datetime.fromtimestamp(t0 + collar + REACT_SEC + 3, tz=ET)

    def _q(feed):
        q = cl.get_stock_quotes(StockQuotesRequest(symbol_or_symbols=sym, start=start, end=end, feed=feed))
        qd = (getattr(q, "data", {}) or {}).get(sym, []) or []
        return sorted((_ts(x.timestamp), float(x.bid_price or 0), float(x.ask_price or 0)) for x in qd)
    quotes = _q(DataFeed.SIP)
    tr = cl.get_stock_trades(StockTradesRequest(symbol_or_symbols=sym, start=start, end=end, feed=DataFeed.SIP))
    td = (getattr(tr, "data", {}) or {}).get(sym, []) or []
    trades = sorted((_ts(x.timestamp), float(x.price)) for x in td if x.price)
    if iex:
        return quotes, trades, _q(DataFeed.IEX)
    return quotes, trades


def fetch_day_bars(cl, sym: str, day: str):
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
    r = cl.get_stock_bars(StockBarsRequest(symbol_or_symbols=sym, timeframe=TimeFrame.Minute,
                                           start=d.replace(hour=9, minute=30), end=d.replace(hour=16),
                                           feed=DataFeed.SIP))
    rows = (getattr(r, "data", {}) or {}).get(sym, []) or []
    return sorted((_ts(b.timestamp), float(b.open), float(b.high), float(b.low), float(b.close)) for b in rows)


def _read_jsonl(p: Path) -> list[dict]:
    out = []
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _days(args) -> list[str]:
    if args.day:
        return [args.day]
    d0 = datetime.strptime(args.since, "%Y-%m-%d")
    d1 = datetime.strptime(args.until, "%Y-%m-%d") if args.until else datetime.now(ET).replace(tzinfo=None)
    out = []
    while d0 <= d1:
        if d0.weekday() < 5:
            out.append(d0.strftime("%Y-%m-%d"))
        d0 += timedelta(days=1)
    return out


# ---------------------------------------------------------------- commands
def eligible(r: dict) -> bool:
    """Registered exclusions (amended_1): fallbacks after a SELL_LIMIT, paper fills that
    took over 10 s (the delta would measure paper's delay, not the policy)."""
    return not r.get("after_limit") and (r.get("fill_lag") is None or r["fill_lag"] <= 10.0)


def cohen_kappa(pred: list[bool], act: list[bool]) -> float | None:
    """Agreement beyond chance between predicted and actual fills."""
    n = len(pred)
    if n == 0:
        return None
    po = sum(1 for a, b in zip(pred, act) if a == b) / n
    pp, pa = sum(pred) / n, sum(act) / n
    pe = pp * pa + (1 - pp) * (1 - pa)
    return None if pe >= 1 else (po - pe) / (1 - pe)


def cmd_calib(args) -> dict:
    """Score the desk's real SELL_LIMIT orders under both fill rules."""
    cl = _client()
    rows = []
    for day in _days(args):
        for o in limit_sells(_read_jsonl(REPORTS / "fills" / f"{day}.jsonl")):
            try:
                q, tr = fetch_quotes_trades(cl, o["symbol"], o["t_start"], max(10.0, o["t_end"] - o["t_start"]))
            except Exception as e:  # noqa: BLE001
                rows.append({**o, "error": str(e)[:120]})
                continue
            time.sleep(args.sleep)
            rows.append({**o, **{f"pred_{rule}": predict_limit_fill(o["t_start"], o["t_end"], o["limit"], q, tr, rule)
                                 for rule in ("trade_through", "bid_cross")}})
    ok = [r for r in rows if "error" not in r]
    summ = {"kind": "calib", "n": len(rows), "n_scored": len(ok),
            "actual_fill_rate": _mean([1.0 if r["filled"] else 0.0 for r in ok])}
    for rule in ("trade_through", "bid_cross"):
        k = f"pred_{rule}"
        summ[rule] = {"pred_fill_rate": _mean([1.0 if r[k] else 0.0 for r in ok]),
                      "kappa": cohen_kappa([bool(r[k]) for r in ok], [bool(r["filled"]) for r in ok]),
                      "agree": _mean([1.0 if r[k] == r["filled"] else 0.0 for r in ok]),
                      "pred_yes_actual_no": sum(1 for r in ok if r[k] and not r["filled"]),
                      "pred_no_actual_yes": sum(1 for r in ok if not r[k] and r["filled"])}
    return {"summary": summ, "rows": rows}


def cmd_exits(args) -> dict:
    cl = _client()
    rows = []
    for day in _days(args):
        sells = pair_sells(_read_jsonl(REPORTS / "fills" / f"{day}.jsonl"))
        outc = [o for o in _read_jsonl(REPORTS / "tapes" / day / "outcomes.jsonl") if o.get("exit_time")]
        for s in sells:
            hm = datetime.fromtimestamp(s["t0"], ET).strftime("%H:%M")
            if not ("09:30" <= hm < "16:00"):
                continue
            try:
                got = fetch_quotes_trades(cl, s["symbol"], s["t0"], max([args.collar] + list(args.policy_collars)),
                                          iex=bool(args.policy))
            except Exception as e:  # noqa: BLE001
                rows.append({**s, "day": day, "error": str(e)[:120]})
                continue
            quotes, trades = got[0], got[1]
            time.sleep(args.sleep)
            sim = simulate_mid_exit(s["t0"], quotes, trades, s["fill"], args.collar)
            row = {**s, "day": day, **(sim or {"no_nbbo": True})}
            if args.policy:
                iexq = got[2]
                why = reason_for_exit(outc, s["symbol"], s["ledger_ts"])
                row["reason"] = why
                for rule in ("trade_through", "bid_cross"):
                    for c in sorted(set([args.collar] + list(args.policy_collars))):
                        row[f"p1_{rule}_c{int(c)}"] = simulate_policy_exit(
                            s["t0"], quotes, trades, iexq, s["fill"], reason=why, collar_sec=c, fill_rule=rule)
            rows.append(row)
    scored = [r for r in rows if "delta_bp" in r]
    summ = {
        "kind": "exits", "collar_sec": args.collar, "n_exits": len(rows), "n_scored": len(scored),
        "n_error": sum(1 for r in rows if "error" in r), "n_no_nbbo": sum(1 for r in rows if r.get("no_nbbo")),
        "fill_rate_strict": _mean([1.0 if r["filled_strict"] else 0.0 for r in scored]),
        "fill_rate_lenient": _mean([1.0 if r["filled_lenient"] else 0.0 for r in scored]),
        "delta_bp_mean": _mean([r["delta_bp"] for r in scored]),
        "delta_bp_median": statistics.median([r["delta_bp"] for r in scored]) if scored else None,
        "delta_bp_t": _t([r["delta_bp"] for r in scored]),
        "delta_lenient_bp_mean": _mean([r["delta_lenient_bp"] for r in scored]),
        "unfilled_delta_bp_mean": _mean([r["delta_bp"] for r in scored if not r["filled_strict"]]),
        "actual_vs_bid0_bp_mean": _mean([r["actual_vs_bid0_bp"] for r in scored]),
        "spread_bp_median": statistics.median([r["spread_bp"] for r in scored]) if scored else None,
        "by_day": {},
    }
    for day in sorted({r["day"] for r in scored}):
        v = [r for r in scored if r["day"] == day]
        summ["by_day"][day] = {"n": len(v), "fill_strict": _mean([1.0 if r["filled_strict"] else 0.0 for r in v]),
                               "delta_bp": _mean([r["delta_bp"] for r in v])}
    if args.policy:
        summ["policy"] = {}
        keys = sorted({k for r in rows for k in r if k.startswith("p1_")})
        for k in keys:
            v = [r for r in rows if isinstance(r.get(k), dict) and eligible(r)]
            pas = [r[k] for r in v if r[k]["route"] == "passive"]
            summ["policy"][k] = {
                "n": len(v), "n_passive": len(pas),
                "routes": {x: sum(1 for r in v if r[k]["route"] == x) for x in sorted({r[k]["route"] for r in v})},
                "outcomes_passive": {o: sum(1 for x in pas if x["outcome"] == o) for o in ("filled", "bail", "timeout")},
                "delta_bp_per_exit": _mean([r[k]["delta_bp"] for r in v]),
                "delta2_bp_per_exit": _mean([r[k]["delta2_bp"] for r in v]),
                "delta_bp_per_passive_exit": _mean([x["delta_bp"] for x in pas]),
            }
        summ["excluded"] = {"after_limit": sum(1 for r in rows if r.get("after_limit")),
                            "fill_lag_over_10s": sum(1 for r in rows if (r.get("fill_lag") or 0) > 10)}
    return {"summary": summ, "rows": rows}


def cmd_pullback(args) -> dict:
    cl = _client()
    rows = []
    for day in _days(args):
        sig = _read_jsonl(REPORTS / "pullback_shadow" / f"{day}.jsonl")
        by_sym: dict[str, list[dict]] = {}
        for r in sig:
            by_sym.setdefault(str(r.get("symbol")).upper(), []).append(r)
        for sym, rs in by_sym.items():
            try:
                bars = fetch_day_bars(cl, sym, day)
            except Exception as e:  # noqa: BLE001
                rows.extend({**r, "error": str(e)[:120]} for r in rs)
                continue
            time.sleep(args.sleep)
            ctl = [forward_from_bars(bars, t) for t in control_times(sym, day, bars)]
            ctl = [c for c in ctl if c]
            for r in rs:
                f = forward_from_bars(bars, float(r["ts"]))
                if not f:
                    rows.append({**r, "no_bars": True})
                    continue
                out = {**r, **f}
                for h in HORIZONS:
                    cm = _mean([c.get(f"fwd{h}") for c in ctl])
                    out[f"ctl{h}"] = cm
                    out[f"lift{h}"] = (f[f"fwd{h}"] - cm) if (f[f"fwd{h}"] is not None and cm is not None) else None
                rows.append(out)
    scored = [r for r in rows if "entry" in r]

    def block(v):
        b = {"n": len(v)}
        for h in HORIZONS:
            b[f"fwd{h}_bp"] = _mean([r.get(f"fwd{h}") for r in v])
            b[f"net{h}_bp"] = (b[f"fwd{h}_bp"] - RT_COST_BP) if b[f"fwd{h}_bp"] is not None else None
            b[f"lift{h}_bp"] = _mean([r.get(f"lift{h}") for r in v])
            b[f"lift{h}_t"] = _t([r.get(f"lift{h}") for r in v])
        b["mfe15_bp"] = _mean([r.get("mfe15") for r in v])
        b["mae15_bp"] = _mean([r.get("mae15") for r in v])
        b["struct_stop_bp_median"] = statistics.median([r["struct_stop_bp"] for r in v if r.get("struct_stop_bp") is not None]) if v else None
        b["struct_stop_hit15"] = _mean([1.0 if (r.get("mae15") is not None and r.get("struct_stop_bp") is not None
                                               and -r["mae15"] >= r["struct_stop_bp"]) else 0.0 for r in v])
        return b

    summ = {"kind": "pullback", "rt_cost_bp": RT_COST_BP, "n_signals": len(rows), "n_scored": len(scored),
            "all": block(scored),
            "main_armed": block([r for r in scored if r.get("main_arm_ok")]),
            "main_not_armed": block([r for r in scored if not r.get("main_arm_ok")]),
            "by_day": {d: block([r for r in scored if r.get("day") == d]) for d in sorted({r.get("day") for r in scored})}}
    return {"summary": summ, "rows": rows}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("kind", choices=["exits", "pullback", "calib"])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--day")
    g.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("--collar", type=float, default=10.0)
    ap.add_argument("--sleep", type=float, default=0.35, help="seconds between data calls")
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--policy", action="store_true",
                    help="also score policy P1 (docs/studies/midpoint_policy_prereg.json)")
    ap.add_argument("--policy-collars", type=float, nargs="*", default=[5.0],
                    help="extra collars for P1 besides --collar")
    ap.add_argument("--tag", default="", help="suffix for the output file name")
    args = ap.parse_args(argv)
    res = {"exits": cmd_exits, "pullback": cmd_pullback, "calib": cmd_calib}[args.kind](args)
    print(json.dumps(res["summary"], indent=2, default=lambda x: round(x, 3) if isinstance(x, float) else str(x)))
    if not args.no_write:
        days = _days(args)
        out = REPORTS / "shadow_scores"
        out.mkdir(parents=True, exist_ok=True)
        p = out / f"{args.kind}_{days[0]}_{days[-1]}{('_' + args.tag) if args.tag else ''}.json"
        p.write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
        print(f"wrote {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
