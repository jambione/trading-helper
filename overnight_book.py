#!/usr/bin/env python3
"""overnight_book.py — paper pilot of the one daily-horizon edge the studies found.

Every session: buy the top 20 liquid names by 12-1 momentum in the closing
auction (market-on-close), sell them all in the next opening auction
(market-on-open). docs/studies/LONGER_HOLDS_2026-09-27.md measured ~16 bp a
night gross out of sample; tools/studies/auction_print_check.py confirmed the
backtest's prices are the auction crosses (-1.1 bp). This pilot answers what
the backtest cannot: do real auction orders fill at the cross, every night,
and does the live edge match the backtest?

RULES (same as the backtest, lh_core.py / lh_run.py)
  universe  active, tradable US common stock on NYSE/NASDAQ/AMEX; no ETPs,
            funds, SPACs, preferreds, units, warrants (ticker_filters + name
            screen)
  liquid    last close >= $5 and 20-day average dollar volume >= $50M
  signal    12-1 momentum: close 21 sessions ago / close 252 sessions ago - 1,
            adjusted daily SIP bars
  pick      top 20 by the signal, equal dollars ($1,000 each by default)
  One deliberate difference: the backtest's liquid screen included the buy
  day's own volume. Here every input is from sessions already closed, so the
  plan is fixed before the day starts.

ISOLATION
  Trades ONLY with the overnight paper account's keys, from
  config/secrets.json.overnight ({"api_key": ..., "secret_key": ...}) or
  OVERNIGHT_ALPACA_API_KEY / OVERNIGHT_ALPACA_SECRET_KEY. Market data uses the
  desk's keys (read-only). Refuses to trade if the overnight keys reach the
  desk's own account, and never uses paper=False.

SCHEDULE (ET, from Alpaca's trading calendar, so holidays and early closes hold)
  plan       06:30          picks from data through the last closed session
  buy        close - 20 min  CLS market buys (Alpaca's MOC cutoff is 15:50)
  sell       open - 15 min   OPG market sells of every position (cutoff 09:28)
  reconcile  close + 30 min / open + 20 min   fills vs the official auction prints

USAGE (on the mini)
  .venv/bin/python overnight_book.py plan [--day YYYY-MM-DD]
  .venv/bin/python overnight_book.py buy  [--dry-run]
  .venv/bin/python overnight_book.py sell [--dry-run]
  .venv/bin/python overnight_book.py reconcile
  .venv/bin/python overnight_book.py status
  .venv/bin/python overnight_book.py run            # the scheduler loop
Logs: ai_reports/overnight/ (plan_DAY.json, ledger.jsonl, nights.jsonl, run.log)
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))
from ticker_filters import is_common, is_levered_etp  # noqa: E402

ET = ZoneInfo("America/New_York")
OUT = ROOT / "ai_reports" / "overnight"
LEDGER = OUT / "ledger.jsonl"
NIGHTS = OUT / "nights.jsonl"
STATE = OUT / "state.json"
LOG = OUT / "run.log"

TOP_N = 20
DOLLARS = 1000.0
MAX_PRICE = 2000.0          # above this even one share is > 2x the target size
MAX_BOOK = TOP_N * DOLLARS * 1.5
MIN_PRICE = 5.0
MIN_ADV = 50e6
ADV_DAYS, ADV_MIN = 20, 15
SKIP, LOOK = 21, 252
OK_EX = {"NYSE", "NASDAQ", "AMEX"}
FUNDISH = re.compile(
    r"\b(ETF|ETN|FUND|ISHARES|SPDR|PROSHARES|DIREXION|INVESCO|VANGUARD|INDEX|PORTFOLIO|TREASURY|BOND|MUNICIPAL|"
    r"LEVERAGED|INVERSE|2X|3X|BULL|BEAR|WARRANTS?|RIGHTS?|UNITS?|PREFERRED|DEPOSITARY SHARES? REPR|NOTES?|DEBENTURES?|"
    r"ACQUISITION CORP|ACQUISITION CO|SPAC|CAPITAL TRUST|TRUST UNITS?|ROYALTY TRUST|CLOSED.END|MUTUAL)\b", re.I)
BUY_LEAD = timedelta(minutes=20)
SELL_LEAD = timedelta(minutes=15)


# ── plumbing ─────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    line = f"{datetime.now(ET):%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def append(path: Path, row: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(row, default=str) + "\n")


def desk_keys() -> tuple[str, str]:
    d = json.loads((ROOT / "config" / "secrets.json").read_text())
    return str(d.get("api_key") or ""), str(d.get("secret_key") or "")


def overnight_keys() -> tuple[str, str]:
    api = (os.getenv("OVERNIGHT_ALPACA_API_KEY") or "").strip()
    sec = (os.getenv("OVERNIGHT_ALPACA_SECRET_KEY") or "").strip()
    if api and sec:
        return api, sec
    p = ROOT / "config" / "secrets.json.overnight"
    if p.exists():
        d = json.loads(p.read_text())
        return str(d.get("api_key") or ""), str(d.get("secret_key") or "")
    return "", ""


def data_client():
    from alpaca.data.historical import StockHistoricalDataClient
    return StockHistoricalDataClient(*desk_keys())


def desk_trading_client():
    from alpaca.trading.client import TradingClient
    return TradingClient(*desk_keys(), paper=True)


def book_client():
    """The overnight paper account, after the isolation guard."""
    from alpaca.trading.client import TradingClient
    api, sec = overnight_keys()
    if not api or not sec:
        raise SystemExit("no overnight keys: put them in config/secrets.json.overnight")
    if api == desk_keys()[0]:
        raise SystemExit("REFUSED: the overnight keys are the desk's keys")
    tc = TradingClient(api, sec, paper=True)
    mine = tc.get_account().account_number
    try:
        desk = desk_trading_client().get_account().account_number
    except Exception:  # noqa: BLE001
        desk = None
    if desk and mine == desk:
        raise SystemExit("REFUSED: the overnight keys reach the desk's account")
    return tc


def calendar(tc, start: date, end: date) -> list:
    from alpaca.trading.requests import GetCalendarRequest
    return tc.get_calendar(GetCalendarRequest(start=start, end=end))


def session(tc, day: date):
    """(open_dt, close_dt) in ET for *day*, or None when the market is shut."""
    for c in calendar(tc, day, day):
        if c.date == day:
            o, cl = c.open, c.close
            o = o.replace(tzinfo=ET) if o.tzinfo is None else o.astimezone(ET)
            cl = cl.replace(tzinfo=ET) if cl.tzinfo is None else cl.astimezone(ET)
            return o, cl
    return None


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(s: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(s, indent=1, default=str))


# ── plan ─────────────────────────────────────────────────────────────────────

def universe(tc) -> dict[str, str]:
    from alpaca.trading.enums import AssetClass, AssetStatus
    from alpaca.trading.requests import GetAssetsRequest
    out = {}
    for a in tc.get_all_assets(GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY)):
        ex = str(getattr(a.exchange, "value", a.exchange))
        nm = a.name or ""
        s = a.symbol
        if (ex in OK_EX and a.tradable and is_common(s) and not is_levered_etp(s, nm)
                and not FUNDISH.search(nm)):
            out[s] = nm
    return out


def daily_closes(syms: list[str], start: date, end: date) -> dict[str, dict[str, tuple[float, float]]]:
    """{sym: {YYYY-MM-DD: (adj close, adj volume)}} from SIP daily bars."""
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    dc = data_client()
    out: dict = {}
    for i in range(0, len(syms), 200):
        chunk = syms[i:i + 200]
        for attempt in range(3):
            try:
                got = dc.get_stock_bars(StockBarsRequest(
                    symbol_or_symbols=chunk, timeframe=TimeFrame.Day,
                    start=datetime.combine(start, datetime.min.time(), ET).astimezone(timezone.utc),
                    end=datetime.combine(end, datetime.max.time(), ET).astimezone(timezone.utc),
                    adjustment=Adjustment.ALL, feed=DataFeed.SIP)).data
                break
            except Exception as e:  # noqa: BLE001
                log(f"bars chunk {i}: {e!s:.120} (try {attempt + 1})")
                time.sleep(5)
        else:
            continue
        for s, bs in (got or {}).items():
            out[s] = {b.timestamp.astimezone(ET).strftime("%Y-%m-%d"): (float(b.close), float(b.volume))
                      for b in bs}
        time.sleep(0.5)
    return out


def plan(tc, day: date) -> dict:
    """Top-20 12-1 momentum liquid names for *day*, from sessions before it."""
    sess = [c.date for c in calendar(tc, day - timedelta(days=420), day)]
    prior = [d for d in sess if d < day]
    if len(prior) < LOOK + 5:
        raise RuntimeError(f"calendar too short: {len(prior)} sessions")
    prior = prior[-(LOOK + 5):]
    keys = [d.isoformat() for d in prior]
    uni = universe(tc)
    log(f"plan {day}: universe {len(uni)} names; fetching {len(keys)} sessions of daily bars")
    bars = daily_closes(sorted(uni), prior[0], prior[-1])
    rows = []
    for s, by in bars.items():
        cf, last, series = [], None, []
        for k in keys:
            if k in by:
                last = by[k][0]
            cf.append(last)
            series.append(by.get(k))
        if cf[-1] is None or cf[-SKIP] is None or cf[-LOOK] is None or cf[-LOOK] <= 0:
            continue
        if series[-1] is None:                       # no bar yesterday: not trading
            continue
        dv = [x[0] * x[1] for x in series[-ADV_DAYS:] if x is not None]
        if len(dv) < ADV_MIN:
            continue
        adv = sum(dv) / len(dv)
        px = series[-1][0]
        if px < MIN_PRICE or adv < MIN_ADV:
            continue
        rows.append({"sym": s, "mom": cf[-SKIP] / cf[-LOOK] - 1, "adv20": adv, "last_close": px})
    if len(rows) < 30:
        raise RuntimeError(f"only {len(rows)} liquid names; refusing to plan")
    rows.sort(key=lambda r: -r["mom"])
    p = {"day": day.isoformat(), "made": datetime.now(ET).isoformat(), "data_through": keys[-1],
         "n_liquid": len(rows), "picks": rows[:TOP_N]}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"plan_{day.isoformat()}.json").write_text(json.dumps(p, indent=1))
    log(f"plan {day}: {len(rows)} liquid names; picks "
        + " ".join(f"{r['sym']}({r['mom']:+.0%})" for r in rows[:TOP_N]))
    return p


def load_plan(day: date) -> dict | None:
    try:
        return json.loads((OUT / f"plan_{day.isoformat()}.json").read_text())
    except (OSError, ValueError):
        return None


# ── orders ───────────────────────────────────────────────────────────────────

def latest_prices(syms: list[str]) -> dict[str, float]:
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockLatestTradeRequest
    got = data_client().get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=syms, feed=DataFeed.IEX))
    return {s: float(t.price) for s, t in (got or {}).items() if t and t.price}


def buy(tc, day: date, dry: bool = False) -> None:
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest
    p = load_plan(day)
    if not p:
        p = plan(tc, day)
    held = {x.symbol: float(x.qty) for x in tc.get_all_positions()}
    if held:
        log(f"buy {day}: WARNING positions still open from before: {held}")
    syms = [r["sym"] for r in p["picks"]]
    px = latest_prices(syms)
    total = 0.0
    for s in syms:
        if s in held:
            log(f"buy {day}: {s} already held, skipped")
            continue
        price = px.get(s)
        if not price:
            log(f"buy {day}: {s} no price, skipped")
            continue
        qty = math.floor(DOLLARS / price)
        if qty == 0 and price <= MAX_PRICE:
            qty = 1
        if qty == 0:
            log(f"buy {day}: {s} ${price:.2f} too high for one share, skipped")
            continue
        if total + qty * price > MAX_BOOK:
            log(f"buy {day}: book cap ${MAX_BOOK:,.0f} reached at {s}, stopped")
            break
        total += qty * price
        cid = f"on-{day.isoformat()}-{s}-buy"
        row = {"event": "submit", "night": day.isoformat(), "sym": s, "side": "buy", "qty": qty,
               "ref_price": price, "client_order_id": cid, "dry_run": dry}
        if not dry:
            try:
                o = tc.submit_order(MarketOrderRequest(symbol=s, qty=qty, side=OrderSide.BUY,
                                                       time_in_force=TimeInForce.CLS, client_order_id=cid))
                row["order_id"] = str(o.id)
            except Exception as e:  # noqa: BLE001
                row["error"] = str(e)[:200]
        if dry:
            print(f"  DRY RUN would submit: MOC buy {qty} {s} (~${qty * price:,.0f} at ${price:.2f})")
        else:
            append(LEDGER, row)
    log(f"buy {day}: {'DRY RUN ' if dry else ''}{len(syms)} picks, ~${total:,.0f} submitted as MOC")


def sell(tc, day: date, dry: bool = False) -> None:
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest
    if not dry:
        tc.cancel_orders()
    pos = tc.get_all_positions()
    for x in pos:
        qty = abs(int(float(x.qty)))
        if qty == 0 or str(getattr(x.side, "value", x.side)).lower() != "long":
            log(f"sell {day}: {x.symbol} qty {x.qty} side {x.side} left alone")
            continue
        cid = f"on-{day.isoformat()}-{x.symbol}-sell"
        row = {"event": "submit", "night_end": day.isoformat(), "sym": x.symbol, "side": "sell", "qty": qty,
               "client_order_id": cid, "dry_run": dry}
        if not dry:
            try:
                o = tc.submit_order(MarketOrderRequest(symbol=x.symbol, qty=qty, side=OrderSide.SELL,
                                                       time_in_force=TimeInForce.OPG, client_order_id=cid))
                row["order_id"] = str(o.id)
            except Exception as e:  # noqa: BLE001
                row["error"] = str(e)[:200]
        if dry:
            print(f"  DRY RUN would submit: MOO sell {qty} {x.symbol}")
        else:
            append(LEDGER, row)
    log(f"sell {day}: {'DRY RUN ' if dry else ''}{len(pos)} positions submitted as MOO")


# ── reconcile ────────────────────────────────────────────────────────────────

def our_orders(tc, prefix: str) -> list:
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest
    got = tc.get_orders(GetOrdersRequest(status=QueryOrderStatus.ALL, limit=500))
    return [o for o in got if str(o.client_order_id or "").startswith(prefix)]


def crosses(syms: list[str], day: date, leg: str) -> dict:
    import auction_print_check as apc  # puts tools/ on sys.path for its own imports
    cl = data_client()
    d = day.isoformat()
    if leg == "close":
        return apc.auction_prints(cl, syms, d, 15, 59, 16, 2, apc.CLOSE_CODES, prefer="M")
    return apc.auction_prints(cl, syms, d, 9, 29, 9, 32, apc.OPEN_CODES, prefer="Q")


def reconcile(tc, day: date, leg: str) -> None:
    """Log fills vs the official cross for one leg ('buy' on *day*, 'sell' on *day*)."""
    tag = f"on-{day.isoformat()}-"
    orders = [o for o in our_orders(tc, tag) if str(o.client_order_id).endswith(f"-{leg}")]
    if not orders:
        log(f"reconcile {leg} {day}: no orders")
        return
    syms = [o.symbol for o in orders]
    cx = crosses(syms, day, "close" if leg == "buy" else "open")
    for o in orders:
        fill = float(o.filled_avg_price) if o.filled_avg_price else None
        c = cx.get(o.symbol)
        row = {"event": "fill", "day": day.isoformat(), "leg": leg, "sym": o.symbol,
               "status": str(getattr(o.status, "value", o.status)), "qty": float(o.qty or 0),
               "filled_qty": float(o.filled_qty or 0), "fill": fill,
               "filled_at": str(o.filled_at) if o.filled_at else None,
               "cross": c[0] if c else None, "cross_size": c[1] if c else None,
               "fill_vs_cross_bp": ((fill / c[0] - 1) * 1e4 if (fill and c) else None)}
        append(LEDGER, row)
    filled = [o for o in orders if o.filled_avg_price]
    diffs = [(float(o.filled_avg_price) / cx[o.symbol][0] - 1) * 1e4 for o in filled if o.symbol in cx]
    log(f"reconcile {leg} {day}: {len(filled)}/{len(orders)} filled; fill vs cross "
        + (f"mean {sum(diffs) / len(diffs):+.2f} bp, max |{max(abs(x) for x in diffs):.2f}| bp" if diffs else "n/a"))
    if leg == "sell":
        night_summary(day)


def night_summary(sell_day: date) -> None:
    """Pair the sells on *sell_day* with their buys and score the night."""
    rows = [json.loads(x) for x in LEDGER.read_text().splitlines() if x.strip()]
    fills = [r for r in rows if r.get("event") == "fill"]
    sells = {r["sym"]: r for r in fills if r["leg"] == "sell" and r["day"] == sell_day.isoformat()}
    buys = {}
    for r in fills:
        if r["leg"] == "buy" and r["day"] < sell_day.isoformat():
            buys[r["sym"]] = r                      # latest buy before the sell day wins
    pairs = [(buys[s], sells[s]) for s in sells if s in buys and buys[s]["fill"] and sells[s]["fill"]]
    if not pairs:
        return
    ret_fill = [(b2["fill"] / b1["fill"] - 1) * 1e4 for b1, b2 in pairs]
    ret_cross = [(b2["cross"] / b1["cross"] - 1) * 1e4 for b1, b2 in pairs if b1["cross"] and b2["cross"]]
    pnl = sum((b2["fill"] - b1["fill"]) * b2["filled_qty"] for b1, b2 in pairs)
    night = {"night_end": sell_day.isoformat(), "names": len(pairs),
             "mean_bp_fills": sum(ret_fill) / len(ret_fill),
             "mean_bp_crosses": (sum(ret_cross) / len(ret_cross)) if ret_cross else None,
             "pnl_usd": round(pnl, 2), "backtest_expect_bp": 16.1}
    append(NIGHTS, night)
    log(f"night ending {sell_day}: {len(pairs)} names, {night['mean_bp_fills']:+.1f} bp on fills "
        f"({night['mean_bp_crosses'] if night['mean_bp_crosses'] is None else round(night['mean_bp_crosses'], 1)} "
        f"on crosses), ${pnl:+.2f}")


def status(tc) -> None:
    a = tc.get_account()
    print(f"account {a.account_number} equity ${float(a.equity):,.2f} cash ${float(a.cash):,.2f}")
    for x in tc.get_all_positions():
        print(f"  {x.symbol:<6} {x.qty:>6} @ {float(x.avg_entry_price):.2f}  mkt ${float(x.market_value):,.2f}")
    if NIGHTS.exists():
        n = [json.loads(x) for x in NIGHTS.read_text().splitlines() if x.strip()]
        if n:
            m = sum(x["mean_bp_fills"] for x in n) / len(n)
            print(f"nights {len(n)}: mean {m:+.1f} bp/night on fills (backtest 16.1), "
                  f"P&L ${sum(x['pnl_usd'] for x in n):+,.2f}, green {sum(x['mean_bp_fills'] > 0 for x in n)}/{len(n)}")


# ── scheduler ────────────────────────────────────────────────────────────────

def run() -> None:
    tc = book_client()
    log("run: overnight book scheduler started")
    while True:
        try:
            now = datetime.now(ET)
            today = now.date()
            st = load_state()
            done = st.setdefault(today.isoformat(), {})
            ses = session(tc, today)
            if ses:
                op, cl = ses
                steps = [
                    ("plan", now.replace(hour=6, minute=30, second=0, microsecond=0), lambda: plan(tc, today)),
                    ("sell", op - SELL_LEAD, lambda: sell(tc, today)),
                    ("reconcile_sell", op + timedelta(minutes=20), lambda: reconcile(tc, today, "sell")),
                    ("buy", cl - BUY_LEAD, lambda: buy(tc, today)),
                    ("reconcile_buy", cl + timedelta(minutes=30), lambda: reconcile(tc, today, "buy")),
                ]
                for name, when, fn in steps:
                    # one attempt per step per day; a step whose window passed by more
                    # than 10 minutes (the process was down) is skipped, not run late,
                    # except reconcile, which is safe any time later that day
                    late = now - when > timedelta(minutes=10) and not name.startswith("reconcile")
                    if name in done or now < when:
                        continue
                    if late:
                        done[name] = f"skipped late at {now:%H:%M}"
                        log(f"{name} {today}: window missed, skipped")
                    else:
                        try:
                            fn()
                            done[name] = f"ok {now:%H:%M}"
                        except Exception as e:  # noqa: BLE001
                            done[name] = f"error {e!s:.120}"
                            log(f"{name} {today}: ERROR {e!s:.200}\n{traceback.format_exc()}")
                    save_state(st)
        except Exception as e:  # noqa: BLE001
            log(f"run loop error: {e!s:.200}")
        time.sleep(30)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("plan", "buy", "sell", "reconcile", "status", "run"))
    ap.add_argument("--day", default=None, help="ET date, default today")
    ap.add_argument("--leg", choices=("buy", "sell"), default=None, help="reconcile one leg")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    day = date.fromisoformat(args.day) if args.day else datetime.now(ET).date()
    if args.cmd == "plan":
        # planning needs no orders; fall back to the desk's read-only client when
        # the overnight keys are not installed yet
        try:
            tc = book_client()
        except SystemExit as e:
            log(f"plan: {e}; using the desk's keys read-only for calendar and assets")
            tc = desk_trading_client()
        plan(tc, day)
        return
    tc = book_client()
    if args.cmd == "buy":
        buy(tc, day, dry=args.dry_run)
    elif args.cmd == "sell":
        sell(tc, day, dry=args.dry_run)
    elif args.cmd == "reconcile":
        for leg in ([args.leg] if args.leg else ["sell", "buy"]):
            reconcile(tc, day, leg)
    elif args.cmd == "status":
        status(tc)
    elif args.cmd == "run":
        run()


if __name__ == "__main__":
    main()
