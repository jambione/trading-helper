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
  OVERNIGHT_ORDER_MODE=market (default; paper does not run auctions)
    sell        open + 1 min    market sells of every position
    sell_topup  open + 3 min    market sells of whatever is still held
    buy         close - 2 min   market buys
    buy_topup   close - 30 s    the unfilled remainder of any finished buy
  OVERNIGHT_ORDER_MODE=auction (a real account)
    sell           open - 15 min   OPG sells (cutoff 09:28)
    sell_check     open - 5 min    OPG for any position no accepted sell covers
    sell_fallback  open + 1 min    market sell of anything still held
    buy            close - 20 min  CLS buys (Alpaca's MOC cutoff is 15:50)
    buy_check      close - 15 min  CLS for any name missing / rejected
    buy_fallback   close - 2 min   market buy of any name still uncovered
  reconcile  close + 30 min / open + 20 min   fills vs the official auction prints

SCORING
  A night is scored on the PLAN, not on paper fills: every planned name from
  the official closing cross to the next official opening cross, $1,000
  each. Alpaca paper does not run auctions (it fills CLS/OPG at the quote,
  partly, and expires the rest), so paper fills are logged beside the score
  as plumbing, never as the result.
  The night's headline is the BOOK actually held: the picks that passed the
  intraday filter (OVERNIGHT_INTRADAY_MIN, default -1%: a pick down more than
  1% from today's open when the buy runs is not bought). All 20 are scored
  beside it every night, so the filter's value is measured, not assumed.

USAGE (on the mini)
  .venv/bin/python overnight_book.py plan [--day YYYY-MM-DD]
  .venv/bin/python overnight_book.py buy  [--dry-run]
  .venv/bin/python overnight_book.py sell [--dry-run]
  .venv/bin/python overnight_book.py topup --leg buy|sell [--dry-run]
  .venv/bin/python overnight_book.py reconcile
  .venv/bin/python overnight_book.py score --day SELL_DAY   # (re)score one night
  .venv/bin/python overnight_book.py status
  .venv/bin/python overnight_book.py snapshot       # refresh the dashboard file
  .venv/bin/python overnight_book.py run            # the scheduler loop
  OVERNIGHT_ACCOUNT=live OVERNIGHT_LIVE_ENABLE=yes .venv/bin/python overnight_book.py check
                                                   # read-only look at the LIVE test account
LIVE (real money, off by default): see ACCOUNT / live_client() / _buy_live() and
scripts/com.jambi.overnight-live.plist. Auctions only, hard caps, every other night.
Logs: ai_reports/overnight/ (plan_DAY.json, ledger.jsonl, nights.jsonl, run.log)
Dashboard: snapshot.json, rewritten after every step and every 15 minutes;
ai_trader publishes it on /api/state as "overnight" (a file read, no broker call).
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
# Which account this process runs. "paper" (the default) is the pilot.
# "live" is the real-money auction test and is OFF unless all three hold:
# OVERNIGHT_ACCOUNT=live, OVERNIGHT_LIVE_ENABLE=yes, and keys in
# config/secrets.json.live that are neither the desk's nor the paper book's.
# Live keeps its own ledger, nights, state and log, so the two never mix.
ACCOUNT = os.getenv("OVERNIGHT_ACCOUNT", "paper").strip().lower()
LIVE = ACCOUNT == "live"
OUT = ROOT / "ai_reports" / ("overnight_live" if LIVE else "overnight")
LEDGER = OUT / "ledger.jsonl"
NIGHTS = OUT / "nights.jsonl"
STATE = OUT / "state.json"
LOG = OUT / "run.log"
SNAPSHOT = OUT / "snapshot.json"
SNAPSHOT_EVERY = 15 * 60
BACKTEST_BP = 16.1
# Opening balance, for the dashboard's "since start": the paper account
# (PA36S0LLDMZY) opened at $25,000, the live test account at $100.
START_EQUITY = float(os.getenv("OVERNIGHT_START_EQUITY", "100" if LIVE else "25000"))
PAPER_OUT = ROOT / "ai_reports" / "overnight"
LIVE_OUT = ROOT / "ai_reports" / "overnight_live"

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
# How orders go in. "auction": MOC buys / MOO sells, which a real account
# fills at the official cross. "market": plain market orders just before the
# close and just after the open, each with a top-up for anything unfilled.
# Paper needs "market": Alpaca paper does not run auctions (2026-09-30: of 20
# MOC buys 5 filled in full, 10 got nothing). The night is scored on the
# crosses either way, so the mode changes only what the paper account holds.
ORDER_MODE = os.getenv("OVERNIGHT_ORDER_MODE", "market").strip().lower()
# Real money only ever trades the auctions: market orders cost ~31 bp a round
# trip (2026-10-01 SIP-quote study), more than the ~16 bp edge.
if LIVE:
    ORDER_MODE = "auction"
# Live caps. One share per name, $25 per order, $100 per night by default:
# the test is whether auction orders fill at the cross, not what they earn.
LIVE_MAX_BOOK = float(os.getenv("OVERNIGHT_LIVE_MAX_BOOK", "100"))
LIVE_MAX_ORDER = float(os.getenv("OVERNIGHT_LIVE_MAX_ORDER", "25"))
LIVE_MAX_SHARES = int(os.getenv("OVERNIGHT_LIVE_MAX_SHARES", "1"))
# The arm switch, separate from installing the live agent: an installed but
# disarmed live scheduler plans, reads the account and refreshes the
# dashboard, and skips every order step. `touch` this file to arm, `rm` to
# disarm; checked before each order step, so no restart either way.
ARMED_FILE = ROOT / "config" / "overnight_live.armed"
ORDER_STEPS = frozenset({"sell", "sell_check", "sell_fallback", "sell_topup",
                         "buy", "buy_check", "buy_fallback", "buy_topup"})


def live_armed() -> bool:
    """Paper is always armed. Live only while the arm file exists."""
    return (not LIVE) or ARMED_FILE.exists()
MKT_BUY_BEFORE_CLOSE = timedelta(minutes=2)
MKT_BUY_TOPUP_BEFORE_CLOSE = timedelta(seconds=30)
MKT_SELL_AFTER_OPEN = timedelta(minutes=1)
MKT_SELL_TOPUP_AFTER_OPEN = timedelta(minutes=3)
# Drop a pick that is down more than this open -> now when the buy runs.
# OOS 2022+ at 15:55 (/tmp/eod_1555.py): all 20 +16.5 bp/night; dropping
# names under -1% +21.6 bp with ~61% of names (open->15:55 quintiles
# -2.8 .. +35.8 bp, t 5.3). Intraday losers keep losing overnight. Set
# OVERNIGHT_INTRADAY_MIN=off to buy all 20. Validated for a 15:55 decision;
# auction mode buys at 15:40, earlier than tested.
_IM = os.getenv("OVERNIGHT_INTRADAY_MIN", "-0.01").strip().lower()
INTRADAY_MIN = None if _IM in ("", "off", "none") else float(_IM)
OPEN_STATUSES = {"new", "accepted", "pending_new", "partially_filled", "accepted_for_bidding",
                 "held", "pending_replace", "pending_cancel", "calculated"}


def schedule(op: datetime, cl: datetime, mode: str | None = None) -> list[tuple[str, datetime]]:
    """(step, when) for one session, in the order they run. Pure."""
    mode = (mode or ORDER_MODE)
    if mode == "auction":
        # check: re-send anything missing/rejected as an auction order before
        # the cutoff (MOO 09:28, MOC 15:50); fallback: whatever is still
        # uncovered goes as a market order, so no name sits out the night
        # because one order bounced. A halted name cannot be helped.
        return [("sell", op - SELL_LEAD), ("sell_check", op - timedelta(minutes=5)),
                ("sell_fallback", op + MKT_SELL_AFTER_OPEN),
                ("reconcile_sell", op + timedelta(minutes=20)),
                ("buy", cl - BUY_LEAD), ("buy_check", cl - timedelta(minutes=15)),
                ("buy_fallback", cl - MKT_BUY_BEFORE_CLOSE),
                ("reconcile_buy", cl + timedelta(minutes=30))]
    return [("sell", op + MKT_SELL_AFTER_OPEN), ("sell_topup", op + MKT_SELL_TOPUP_AFTER_OPEN),
            ("reconcile_sell", op + timedelta(minutes=20)),
            ("buy", cl - MKT_BUY_BEFORE_CLOSE), ("buy_topup", cl - MKT_BUY_TOPUP_BEFORE_CLOSE),
            ("reconcile_buy", cl + timedelta(minutes=30))]


def _status(o) -> str:
    return str(getattr(o.status, "value", o.status)).lower()


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


def live_keys() -> tuple[str, str]:
    api = (os.getenv("OVERNIGHT_LIVE_ALPACA_API_KEY") or "").strip()
    sec = (os.getenv("OVERNIGHT_LIVE_ALPACA_SECRET_KEY") or "").strip()
    if api and sec:
        return api, sec
    p = ROOT / "config" / "secrets.json.live"
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
    """The overnight paper account (or, in live mode, the live test account),
    after the isolation guard."""
    from alpaca.trading.client import TradingClient
    if LIVE:
        return live_client()
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


def live_client():
    """The real-money test account. Refuses unless every lock is open."""
    from alpaca.trading.client import TradingClient
    if os.getenv("OVERNIGHT_LIVE_ENABLE", "").strip().lower() != "yes":
        raise SystemExit("REFUSED: live mode needs OVERNIGHT_LIVE_ENABLE=yes")
    api, sec = live_keys()
    if not api or not sec:
        raise SystemExit("no live keys: put them in config/secrets.json.live")
    if api in (desk_keys()[0], overnight_keys()[0]):
        raise SystemExit("REFUSED: the live keys are the desk's or the paper book's")
    tc = TradingClient(api, sec, paper=False)
    a = tc.get_account()
    if str(getattr(a.status, "value", a.status)).upper() != "ACTIVE":
        raise SystemExit(f"REFUSED: live account status is {a.status}")
    if getattr(a, "trading_blocked", False) or getattr(a, "account_blocked", False):
        raise SystemExit("REFUSED: live account is blocked")
    others = set()
    for mk in (desk_trading_client,):
        try:
            others.add(mk().get_account().account_number)
        except Exception:  # noqa: BLE001
            pass
    try:
        api_p, sec_p = overnight_keys()
        if api_p:
            others.add(TradingClient(api_p, sec_p, paper=True).get_account().account_number)
    except Exception:  # noqa: BLE001
        pass
    if a.account_number in others:
        raise SystemExit("REFUSED: the live keys reach the desk's or the paper book's account")
    return tc


def size_live(syms: list[str], prices: dict, held: set, cash: float,
              max_book: float, max_order: float, max_shares: int) -> list[tuple[str, int, float]]:
    """[(sym, qty, price)] for the live test, in pick order. Pure.

    Whole shares only (Alpaca takes no fractional auction orders), at most
    *max_shares* and *max_order* per name, never past *max_book* or the cash
    on hand. A pick that does not fit is skipped, not resized, so the next
    cheaper name can still go in.
    """
    out, total = [], 0.0
    room = min(max_book, max(0.0, cash))
    for s in syms:
        p = prices.get(s)
        if s in held or not p or p <= 0:
            continue
        qty = min(max_shares, math.floor(max_order / p))
        if qty < 1 or total + qty * p > room:
            continue
        out.append((s, qty, p))
        total += qty * p
    return out


def live_cash(a) -> float:
    """Money the live test may spend: buying power, never more than cash.

    Not non_marginable_buying_power: on the $100 cash account (multiplier 1)
    Alpaca reports it as 0 (2026-10-01 check), which sized every pick to
    nothing. Alpaca's fields are strings, and "0" is truthy, so the old
    `x or cash` fallback never fell back.
    """
    def f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0
    bp, cash = f(getattr(a, "buying_power", 0)), f(getattr(a, "cash", 0))
    return max(0.0, min(bp, cash) if cash > 0 else bp)


def sold_today(ledger: list[dict], day: date) -> bool:
    """True when this account submitted a sell on *day*. Pure. A small live
    account is cash-only: the morning's proceeds settle T+1, so buying with
    them the same afternoon risks a good-faith violation. Live therefore
    trades every other night."""
    return any(r.get("event") == "submit" and r.get("side") == "sell"
               and r.get("night_end") == day.isoformat() and not r.get("dry_run")
               and not r.get("error") for r in ledger)


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
    rows = rank(bars, keys)
    if len(rows) < 30:
        raise RuntimeError(f"only {len(rows)} liquid names; refusing to plan")
    p = {"day": day.isoformat(), "made": datetime.now(ET).isoformat(), "data_through": keys[-1],
         "n_liquid": len(rows), "picks": rows[:TOP_N]}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"plan_{day.isoformat()}.json").write_text(json.dumps(p, indent=1))
    log(f"plan {day}: {len(rows)} liquid names; picks "
        + " ".join(f"{r['sym']}({r['mom']:+.0%})" for r in rows[:TOP_N]))
    return p


def rank(bars: dict, keys: list[str]) -> list[dict]:
    """Liquid names ranked by 12-1 momentum, best first. Pure: *bars* is
    {sym: {YYYY-MM-DD: (adj close, adj volume)}}, *keys* the sessions the
    plan may see (oldest first, ending the session before the buy day).
    tools/studies/overnight_plan_replay.py replays this on past days."""
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
    rows.sort(key=lambda r: -r["mom"])
    return rows


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


def session_opens(syms: list[str], day: date) -> dict[str, float]:
    """Today's 09:30 open per name from SIP 1m bars. SIP serves bars >= 15
    minutes old, so by the afternoon buy the 09:30 bar is available; the
    IEX first print can sit 0.6% off the official open."""
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    st = datetime.combine(day, datetime.min.time(), ET).replace(hour=9, minute=30)
    got = data_client().get_stock_bars(StockBarsRequest(
        symbol_or_symbols=syms, timeframe=TimeFrame.Minute, start=st.astimezone(timezone.utc),
        end=(st + timedelta(minutes=1)).astimezone(timezone.utc), feed=DataFeed.SIP)).data
    return {s: float(bs[0].open) for s, bs in (got or {}).items() if bs}


def intraday_filter(syms: list[str], opens: dict, prices: dict,
                    floor: float | None) -> tuple[list[str], list[dict]]:
    """(names to buy, one row per pick). Pure. A pick down more than *floor*
    open -> now is dropped. A pick with no open or price is KEPT and marked:
    unknown is not evidence of weakness, and keeping it is the unfiltered
    book the backtest scores."""
    kept, rows = [], []
    for s in syms:
        o, p = opens.get(s), prices.get(s)
        chg = (p / o - 1) if (o and p) else None
        drop = floor is not None and chg is not None and chg < floor
        rows.append({"sym": s, "open": o, "price": p,
                     "intraday": None if chg is None else round(chg, 5),
                     "kept": not drop, "unknown": chg is None})
        if not drop:
            kept.append(s)
    return kept, rows


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
    if INTRADAY_MIN is not None:
        try:
            opens = session_opens(syms, day)
        except Exception as e:  # noqa: BLE001
            log(f"buy {day}: opens unavailable ({e!s:.100}); intraday filter skipped, buying all")
            opens = {}
        syms, frows = intraday_filter(syms, opens, px, INTRADAY_MIN)
        dropped = [r for r in frows if not r["kept"]]
        unknown = [r["sym"] for r in frows if r["unknown"]]
        if not dry:
            append(LEDGER, {"event": "filter", "night": day.isoformat(), "floor": INTRADAY_MIN,
                            "kept": syms, "rows": frows})
        log(f"buy {day}: intraday filter {INTRADAY_MIN:+.1%}: kept {len(syms)}/{len(frows)}"
            + (f"; dropped " + " ".join(f"{r['sym']}({r['intraday']:+.1%})" for r in dropped) if dropped else "")
            + (f"; no open/price for {','.join(unknown)} (kept)" if unknown else ""))
    if LIVE:
        _buy_live(tc, day, syms, px, held, dry)
        return
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
                tif = TimeInForce.CLS if ORDER_MODE == "auction" else TimeInForce.DAY
                o = tc.submit_order(MarketOrderRequest(symbol=s, qty=qty, side=OrderSide.BUY,
                                                       time_in_force=tif, client_order_id=cid))
                row["order_id"] = str(o.id)
            except Exception as e:  # noqa: BLE001
                row["error"] = str(e)[:200]
        if dry:
            print(f"  DRY RUN would submit: {_how('buy')} {qty} {s} (~${qty * price:,.0f} at ${price:.2f})")
        else:
            append(LEDGER, row)
    log(f"buy {day}: {'DRY RUN ' if dry else ''}{len(syms)} picks, ~${total:,.0f} submitted as {_how('buy')}")


def _buy_live(tc, day: date, syms: list[str], px: dict, held: dict, dry: bool) -> None:
    """Live test: settlement check, hard caps, MOC only."""
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest
    if sold_today(_read_jsonl(LEDGER), day):
        log(f"buy {day}: LIVE settlement: sold this morning, proceeds settle T+1; no buy tonight")
        return
    cash = live_cash(tc.get_account())
    picks = size_live(syms, px, set(held), cash, LIVE_MAX_BOOK, LIVE_MAX_ORDER, LIVE_MAX_SHARES)
    if not dry:
        # The book actually held is what gets scored (night_summary reads the
        # last filter row of the night).
        append(LEDGER, {"event": "filter", "night": day.isoformat(), "floor": INTRADAY_MIN,
                        "kept": [s for s, _q, _p in picks], "live_caps": {
                            "max_book": LIVE_MAX_BOOK, "max_order": LIVE_MAX_ORDER,
                            "max_shares": LIVE_MAX_SHARES, "cash": cash}})
    for s, qty, price in picks:
        cid = f"on-{day.isoformat()}-{s}-buy"
        row = {"event": "submit", "night": day.isoformat(), "sym": s, "side": "buy", "qty": qty,
               "ref_price": price, "client_order_id": cid, "dry_run": dry, "live": True}
        if dry:
            print(f"  DRY RUN would submit LIVE: MOC buy {qty} {s} (~${qty * price:,.2f})")
            continue
        try:
            o = tc.submit_order(MarketOrderRequest(symbol=s, qty=qty, side=OrderSide.BUY,
                                                   time_in_force=TimeInForce.CLS, client_order_id=cid))
            row["order_id"] = str(o.id)
        except Exception as e:  # noqa: BLE001
            row["error"] = str(e)[:200]
        append(LEDGER, row)
    log(f"buy {day}: LIVE {'DRY RUN ' if dry else ''}{len(picks)} names "
        f"{' '.join(f'{s}x{q}' for s, q, _p in picks)} ~${sum(q * p for _s, q, p in picks):,.2f} "
        f"(cash ${cash:,.2f}, caps ${LIVE_MAX_BOOK:g}/${LIVE_MAX_ORDER:g}/{LIVE_MAX_SHARES} sh) as MOC")


def check(tc, day: date) -> None:
    """Read-only first look: account, positions, and tonight's buy as a dry run."""
    a = tc.get_account()
    print(f"account {a.account_number} ({ACCOUNT}) status {a.status} equity ${float(a.equity):,.2f} "
          f"cash ${float(a.cash):,.2f} buying power ${float(a.buying_power):,.2f}")
    for x in tc.get_all_positions():
        print(f"  holding {x.symbol} {x.qty} @ {float(x.avg_entry_price):.2f}")
    print(f"order mode {ORDER_MODE}; schedule: "
          + ", ".join(f"{n} {w:%H:%M}" for n, w in schedule(*session(tc, day)))
          if session(tc, day) else "market closed today")
    buy(tc, day, dry=True)


def _how(side: str) -> str:
    if ORDER_MODE == "auction":
        return "MOC" if side == "buy" else "MOO"
    return "market"


def buy_topup(tc, day: date, dry: bool = False, auction: bool = False, tag: str = "buy_topup") -> None:
    """Re-send the unfilled remainder of each planned buy whose order is done.

    Targets are the first submit's qty per name (the ledger), so a submit
    that errored still counts. A name with an order still working (an
    accepted MOC waits as "new" until 16:00) is left alone. *auction* sends
    the remainder as MOC (the 15:45 check), else as a market order.
    """
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest
    targets = {r["sym"]: int(r["qty"]) for r in _read_jsonl(LEDGER)
               if r.get("event") == "submit" and r.get("side") == "buy" and r.get("night") == day.isoformat()
               and not r.get("dry_run") and r.get("client_order_id", "").endswith("-buy")}
    orders = [o for o in our_orders(tc, f"on-{day.isoformat()}-") if "-buy" in str(o.client_order_id)]
    need = topup_needs(targets, orders)
    for s, (rem, n) in sorted(need.items()):
        cid = f"on-{day.isoformat()}-{s}-buy-r{n}"
        row = {"event": "submit", "night": day.isoformat(), "sym": s, "side": "buy", "qty": rem,
               "client_order_id": cid, "dry_run": dry, "topup": True, "step": tag}
        if dry:
            print(f"  DRY RUN would submit: {'MOC' if auction else 'market'} buy remainder {rem} {s}")
            continue
        try:
            o = tc.submit_order(MarketOrderRequest(symbol=s, qty=rem, side=OrderSide.BUY,
                                                   time_in_force=TimeInForce.CLS if auction else TimeInForce.DAY,
                                                   client_order_id=cid))
            row["order_id"] = str(o.id)
        except Exception as e:  # noqa: BLE001
            row["error"] = str(e)[:200]
        append(LEDGER, row)
    log(f"{tag} {day}: {'DRY RUN ' if dry else ''}"
        + (", ".join(f"{s} +{r}" for s, (r, _n) in sorted(need.items())) if need else "nothing unfilled"))


def topup_needs(targets: dict[str, int], orders: list) -> dict[str, tuple[int, int]]:
    """{sym: (shares still to buy, orders so far)} for names whose orders are
    all done and short of the target. Pure (orders only need symbol, status,
    filled_qty)."""
    by: dict[str, list] = {}
    for o in orders:
        by.setdefault(o.symbol, []).append(o)
    out = {}
    for s, want in targets.items():
        mine = by.get(s, [])
        if any(_status(o) in OPEN_STATUSES for o in mine):
            continue                                  # still working
        got = sum(float(o.filled_qty or 0) for o in mine)
        rem = int(want - got)
        if rem > 0:
            out[s] = (rem, len(mine))
    return out


def sell(tc, day: date, dry: bool = False, attempt: int = 0, tif: str | None = None,
         tag: str | None = None) -> None:
    """Sell every long position. attempt 0 is the scheduled sell; 1+ is the
    top-up. Sells size off qty_available (shares not already held by an open
    order), never qty: a second sell sized off qty while the first still
    works would sell the same shares twice, and on this margin account that
    is a short."""
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest
    if not dry and attempt == 0:
        tc.cancel_orders()
    pos = tc.get_all_positions()
    sent = 0
    for x in pos:
        avail = getattr(x, "qty_available", None)
        qty = abs(int(float(avail if avail is not None else x.qty)))
        if qty == 0 or str(getattr(x.side, "value", x.side)).lower() != "long":
            if qty:
                log(f"sell {day}: {x.symbol} qty {x.qty} side {x.side} left alone")
            continue
        sent += 1
        cid = f"on-{day.isoformat()}-{x.symbol}-sell" + (f"-r{attempt}" if attempt else "")
        row = {"event": "submit", "night_end": day.isoformat(), "sym": x.symbol, "side": "sell", "qty": qty,
               "client_order_id": cid, "dry_run": dry}
        if not dry:
            try:
                kind = tif or ("opg" if ORDER_MODE == "auction" else "day")
                o = tc.submit_order(MarketOrderRequest(symbol=x.symbol, qty=qty, side=OrderSide.SELL,
                                                       time_in_force=TimeInForce.OPG if kind == "opg"
                                                       else TimeInForce.DAY, client_order_id=cid))
                row["order_id"] = str(o.id)
            except Exception as e:  # noqa: BLE001
                row["error"] = str(e)[:200]
        if dry:
            print(f"  DRY RUN would submit: {_how('sell')} sell {qty} {x.symbol}")
        else:
            append(LEDGER, row)
    tag = tag or ("sell" if not attempt else "sell_topup")
    how = {"opg": "MOO", "day": "market"}.get(tif or "", _how("sell"))
    log(f"{tag} {day}: {'DRY RUN ' if dry else ''}{sent} positions submitted as {how}")


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
    orders = [o for o in our_orders(tc, tag) if f"-{leg}" in str(o.client_order_id)]
    if not orders:
        log(f"reconcile {leg} {day}: no orders")
        if leg == "sell":
            night_summary(day)      # the plan is scored even when paper filled nothing
        return
    syms = sorted({o.symbol for o in orders})
    cx = crosses(syms, day, "close" if leg == "buy" else "open")
    agg = aggregate_orders(orders)
    for s in syms:
        a = agg[s]
        fill, c = a["fill"], cx.get(s)
        row = {"event": "fill", "day": day.isoformat(), "leg": leg, "sym": s,
               "status": a["status"], "orders": a["orders"], "qty": a["qty"],
               "filled_qty": a["filled_qty"], "fill": fill, "filled_at": a["filled_at"],
               "cross": c[0] if c else None, "cross_size": c[1] if c else None,
               "fill_vs_cross_bp": ((fill / c[0] - 1) * 1e4 if (fill and c) else None)}
        append(LEDGER, row)
    filled = [s for s in syms if agg[s]["fill"]]
    diffs = [(agg[s]["fill"] / cx[s][0] - 1) * 1e4 for s in filled if s in cx]
    log(f"reconcile {leg} {day}: {len(filled)}/{len(syms)} names filled; fill vs cross "
        + (f"mean {sum(diffs) / len(diffs):+.2f} bp, max |{max(abs(x) for x in diffs):.2f}| bp" if diffs else "n/a"))
    if leg == "sell":
        night_summary(day)


def score_plan(picks: list[str], close_cx: dict, open_cx: dict) -> dict:
    """The night as the backtest measures it: every planned name, official
    closing cross -> next official opening cross, equal DOLLARS each. Pure.

    Alpaca paper does not run auctions: it fills CLS/OPG orders at the quote,
    partly, and expires the rest (2026-09-30: 5 of 20 filled in full, 10 got
    nothing, 1-share MU included). Paper fills therefore say which names the
    simulator chose, not what the strategy earned, so the headline number
    never depends on them.
    """
    per = {}
    for s in picks:
        c, o = close_cx.get(s), open_cx.get(s)
        if c and o and c[0] > 0:
            per[s] = (o[0] / c[0] - 1) * 1e4
    mean = sum(per.values()) / len(per) if per else None
    return {"n_plan": len(picks), "n_scored": len(per),
            "missing": sorted(set(picks) - set(per)),
            "mean_bp_plan": mean,
            "pnl_plan_usd": round(sum(DOLLARS * bp / 1e4 for bp in per.values()), 2) if per else None,
            "per_name_bp": {s: round(v, 1) for s, v in sorted(per.items())}}


def aggregate_orders(orders: list) -> dict[str, dict]:
    """One row per name from all of its orders (the first plus any top-ups):
    shares filled, their volume-weighted price, and the last order's status.
    Pure; the first order's qty is the target."""
    out: dict[str, dict] = {}
    for o in sorted(orders, key=lambda o: str(o.client_order_id)):
        a = out.setdefault(o.symbol, {"qty": float(o.qty or 0), "filled_qty": 0.0, "notional": 0.0,
                                      "orders": 0, "status": None, "filled_at": None})
        q = float(o.filled_qty or 0)
        if q and o.filled_avg_price:
            a["filled_qty"] += q
            a["notional"] += q * float(o.filled_avg_price)
            a["filled_at"] = str(o.filled_at) if o.filled_at else a["filled_at"]
        a["orders"] += 1
        a["status"] = _status(o)
    for a in out.values():
        a["fill"] = a["notional"] / a["filled_qty"] if a["filled_qty"] else None
        if a["filled_qty"] >= a["qty"] > 0:
            a["status"] = "filled"
        del a["notional"]
    return out


def fifo_matches(ledger: list[dict], sell_day: str) -> list[dict]:
    """Paper sells on *sell_day* matched first-in-first-out to the buy lots
    they closed. Pure.

    A sell that only partly fills leaves shares that a later morning's sell
    closes; pairing by "latest buy before the sell day" priced those shares
    at the next night's buy. Lots carry their own price and day here. One
    fill row per (day, leg, sym) counts: a re-run reconcile appends a
    duplicate, and the last one wins.
    """
    rows: dict[tuple, dict] = {}
    for r in ledger:
        if r.get("event") == "fill" and r.get("fill") and float(r.get("filled_qty") or 0) > 0:
            rows[(r["day"], r["leg"], r["sym"])] = r
    out = []
    for sym in sorted({k[2] for k in rows}):
        lots = []                                   # [buy_day, qty left, px, cross]
        for (day, leg, s), r in sorted(rows.items(), key=lambda kv: (kv[0][0], kv[0][1] == "buy")):
            if s != sym or day > sell_day:
                continue
            q = float(r["filled_qty"])
            if leg == "buy":
                lots.append([day, q, float(r["fill"]), r.get("cross")])
                continue
            while q > 1e-9 and lots:                # sells close the oldest lots first
                take = min(q, lots[0][1])
                if day == sell_day:
                    out.append({"sym": sym, "qty": take, "buy": lots[0][2], "sell": float(r["fill"]),
                                "buy_day": lots[0][0], "buy_cross": lots[0][3], "sell_cross": r.get("cross")})
                lots[0][1] -= take
                q -= take
                if lots[0][1] <= 1e-9:
                    lots.pop(0)
    return out


def night_summary(sell_day: date, fetch=None) -> None:
    """Score the night ending *sell_day* on the plan, with paper fills beside it."""
    fetch = fetch or crosses
    plans = sorted(p for p in OUT.glob("plan_*.json") if p.stem[5:] < sell_day.isoformat())
    if not plans:
        log(f"night ending {sell_day}: no plan before it; nothing to score")
        return
    plan_row = json.loads(plans[-1].read_text())
    buy_day = date.fromisoformat(plan_row["day"])
    picks = [r["sym"] for r in plan_row.get("picks", [])]
    night = {"night_end": sell_day.isoformat(), "plan_day": buy_day.isoformat(),
             "backtest_expect_bp": BACKTEST_BP}
    close_cx, open_cx = fetch(picks, buy_day, "close"), fetch(picks, sell_day, "open")
    night.update(score_plan(picks, close_cx, open_cx))
    # The book actually held: the picks that passed the intraday filter that
    # night. Nights without a filter row (filter off, or before it existed)
    # held all 20, so the book is the plan.
    flt = [r for r in _read_jsonl(LEDGER) if r.get("event") == "filter" and r.get("night") == buy_day.isoformat()]
    if flt:
        bk = score_plan(flt[-1]["kept"], close_cx, open_cx)
        night.update({"filter_floor": flt[-1].get("floor"), "n_book": bk["n_plan"],
                      "n_book_scored": bk["n_scored"], "mean_bp_book": bk["mean_bp_plan"],
                      "pnl_book_usd": bk["pnl_plan_usd"]})
    else:
        night.update({"filter_floor": None, "n_book": night["n_plan"], "n_book_scored": night["n_scored"],
                      "mean_bp_book": night["mean_bp_plan"], "pnl_book_usd": night["pnl_plan_usd"]})

    # Paper fills, secondary: what the paper account actually earned on the
    # shares it sold this morning, each matched to the lot it came from.
    lots = fifo_matches(_read_jsonl(LEDGER), sell_day.isoformat())
    by_sym: dict[str, list] = {}
    for m in lots:
        by_sym.setdefault(m["sym"], []).append(m)
    ret_fill, ret_cross = [], []
    for ms in by_sym.values():
        q = sum(m["qty"] for m in ms)
        cost = sum(m["qty"] * m["buy"] for m in ms)
        ret_fill.append((sum(m["qty"] * m["sell"] for m in ms) / cost - 1) * 1e4)
        if all(m["buy_cross"] and m["sell_cross"] for m in ms):
            ret_cross.append((sum(m["qty"] * m["sell_cross"] for m in ms)
                              / sum(m["qty"] * m["buy_cross"] for m in ms) - 1) * 1e4)
    night.update({
        "names": len(by_sym),
        "mean_bp_fills": (sum(ret_fill) / len(ret_fill)) if ret_fill else None,
        "mean_bp_crosses": (sum(ret_cross) / len(ret_cross)) if ret_cross else None,
        "pnl_usd": round(sum(m["qty"] * (m["sell"] - m["buy"]) for m in lots), 2),
        "carried_lots": sorted({m["sym"] for m in lots if m["buy_day"] != night["plan_day"]}),
    })
    # One row per night: a re-score (the `score` command, a rerun reconcile)
    # replaces the earlier row instead of double-counting the night.
    kept = [n for n in _read_jsonl(NIGHTS) if n.get("night_end") != night["night_end"]]
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = NIGHTS.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(n, default=str) + "\n" for n in kept + [night]))
    os.replace(tmp, NIGHTS)
    mb, mp = night["mean_bp_book"], night["mean_bp_plan"]
    log(f"night ending {sell_day}: book {night['n_book_scored']}/{night['n_book']} names "
        + (f"{mb:+.1f} bp at the crosses, ${night['pnl_book_usd']:+.2f} at ${DOLLARS:,.0f}/name" if mb is not None
           else "unscored (no crosses)")
        + (f" (all 20: {mp:+.1f} bp)" if mp is not None else "")
        + f"; paper filled {night['names']} names, ${night['pnl_usd']:+.2f}"
        + (f" (carried lots: {','.join(night['carried_lots'])})" if night["carried_lots"] else "")
        + (f" missing {','.join(night['missing'])}" if night["missing"] else ""))


def night_bp(n: dict) -> float | None:
    """A scored night's headline bp: the book held, at the crosses; then the
    whole plan; paper fills only for nights logged before plan scoring."""
    for k in ("mean_bp_book", "mean_bp_plan", "mean_bp_fills"):
        if n.get(k) is not None:
            return n[k]
    return None


def night_pnl(n: dict) -> float:
    for k in ("pnl_book_usd", "pnl_plan_usd", "pnl_usd"):
        if n.get(k) is not None:
            return float(n[k])
    return 0.0


def _fill_rows(ledger: list[dict]) -> dict[tuple, dict]:
    rows = {}
    for r in ledger:
        if r.get("event") == "fill":
            rows[(r["day"], r["leg"], r["sym"])] = r          # a re-run reconcile: last wins
    return rows


def _vs_cross_bp(leg: str, fill, cross) -> float | None:
    """Cost against the official cross, bp, positive = worse: a buy paid
    more than the cross, a sell got less."""
    if not fill or not cross:
        return None
    return ((fill / cross - 1) if leg == "buy" else (cross / fill - 1)) * 1e4


def compare_rows(paper: list[dict], live: list[dict]) -> dict:
    """Every live auction order beside the paper account's market-order fill
    of the same name, same day, same leg. Pure.

    The question the live test exists to answer is whether real MOC/MOO
    orders fill, in full, at the official cross. Paper cannot say (it does
    not run auctions); it does show what market orders cost on the same
    names, which is the alternative.
    """
    p, l = _fill_rows(paper), _fill_rows(live)
    rows = []
    for (day, leg, sym), r in sorted(l.items()):
        pr = p.get((day, leg, sym)) or {}
        cross = r.get("cross") or pr.get("cross")
        full = bool(r.get("fill")) and float(r.get("filled_qty") or 0) >= float(r.get("qty") or 0) > 0
        rows.append({"day": day, "leg": leg, "sym": sym, "status": r.get("status"),
                     "qty": r.get("qty"), "filled_qty": r.get("filled_qty"), "full": full,
                     "live_fill": r.get("fill"), "cross": cross,
                     "live_vs_cross_bp": _vs_cross_bp(leg, r.get("fill"), cross),
                     "paper_fill": pr.get("fill"),
                     "paper_vs_cross_bp": _vs_cross_bp(leg, pr.get("fill"), cross)})
    lv = [x["live_vs_cross_bp"] for x in rows if x["live_vs_cross_bp"] is not None]
    pv = [x["paper_vs_cross_bp"] for x in rows if x["paper_vs_cross_bp"] is not None]
    totals = {"orders": len(rows), "full": sum(x["full"] for x in rows),
              "live_mean_bp": (sum(lv) / len(lv)) if lv else None, "live_n": len(lv),
              "paper_mean_bp": (sum(pv) / len(pv)) if pv else None, "paper_n": len(pv)}
    return {"rows": rows, "totals": totals}


def compare(print_it: bool = True) -> dict:
    """Live vs paper vs the official cross, from both books' ledgers."""
    out = compare_rows(_read_jsonl(PAPER_OUT / "ledger.jsonl"), _read_jsonl(LIVE_OUT / "ledger.jsonl"))
    LIVE_OUT.mkdir(parents=True, exist_ok=True)
    tmp = LIVE_OUT / "compare.tmp"
    tmp.write_text(json.dumps(out, default=str))
    os.replace(tmp, LIVE_OUT / "compare.json")
    if print_it:
        f = lambda v, d=1: "-" if v is None else f"{v:+.{d}f}"
        print(f"{'day':10} {'leg':4} {'sym':6} {'status':16} {'qty':>7} {'live':>9} {'cross':>9} "
              f"{'live bp':>8} {'paper':>9} {'paper bp':>8}")
        for x in out["rows"]:
            print(f"{x['day']:10} {x['leg']:4} {x['sym']:6} {str(x['status']):16} "
                  f"{str(x['filled_qty']) + '/' + str(x['qty']):>7} {f(x['live_fill'], 2):>9} {f(x['cross'], 2):>9} "
                  f"{f(x['live_vs_cross_bp']):>8} {f(x['paper_fill'], 2):>9} {f(x['paper_vs_cross_bp']):>8}")
        t = out["totals"]
        print(f"live auction orders {t['orders']}, filled in full {t['full']}; vs cross: live "
              f"{f(t['live_mean_bp'])} bp (n={t['live_n']}), paper market {f(t['paper_mean_bp'])} bp (n={t['paper_n']})")
    return out


def status(tc) -> None:
    a = tc.get_account()
    print(f"account {a.account_number} equity ${float(a.equity):,.2f} cash ${float(a.cash):,.2f}")
    for x in tc.get_all_positions():
        print(f"  {x.symbol:<6} {x.qty:>6} @ {float(x.avg_entry_price):.2f}  mkt ${float(x.market_value):,.2f}")
    if NIGHTS.exists():
        n = [x for x in _read_jsonl(NIGHTS) if night_bp(x) is not None]
        if n:
            m = sum(night_bp(x) for x in n) / len(n)
            print(f"nights {len(n)}: mean {m:+.1f} bp/night at the crosses (backtest {BACKTEST_BP}), "
                  f"P&L ${sum(night_pnl(x) for x in n):+,.2f} at ${DOLLARS:,.0f}/name, "
                  f"green {sum(night_bp(x) > 0 for x in n)}/{len(n)}; "
                  f"paper fills ${sum(x.get('pnl_usd') or 0 for x in n):+,.2f}")


# ── dashboard snapshot ───────────────────────────────────────────────────────

def _read_jsonl(path: Path) -> list[dict]:
    try:
        return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    except (OSError, ValueError):
        return []


def build_snapshot(plan_row: dict | None, ledger: list[dict], nights: list[dict],
                   positions: list[dict], account: dict, state_today: dict,
                   next_step: dict | None, now: datetime) -> dict:
    """What the dashboard's Overnight book strip shows. Pure: no I/O.

    *ledger* rows are overnight_book's own (submit / fill events). The book
    is the latest buy night: its orders, their fills against the official
    cross, and the sells that closed them the next morning.
    """
    buys = [r for r in ledger if r.get("side") == "buy" and r.get("event") == "submit"]
    night = max((r["night"] for r in buys if r.get("night")), default=None)
    rows: dict[str, dict] = {}
    if night:
        for r in buys:
            if r.get("night") == night:
                rows[r["sym"]] = {"sym": r["sym"], "qty": r.get("qty"), "ref_price": r.get("ref_price"),
                                  "error": r.get("error")}
        fills = [r for r in ledger if r.get("event") == "fill"]
        for r in fills:
            if r.get("sym") not in rows:
                continue
            if r.get("leg") == "buy" and r.get("day") == night:
                rows[r["sym"]].update(buy_status=r.get("status"), buy_fill=r.get("fill"),
                                      buy_cross=r.get("cross"), buy_vs_cross_bp=r.get("fill_vs_cross_bp"),
                                      filled_qty=r.get("filled_qty"))
            elif r.get("leg") == "sell" and r.get("day", "") > night:
                rows[r["sym"]].update(sell_status=r.get("status"), sell_fill=r.get("fill"),
                                      sell_cross=r.get("cross"), sell_vs_cross_bp=r.get("fill_vs_cross_bp"))
        for v in rows.values():
            b, sfill = v.get("buy_fill"), v.get("sell_fill")
            v["night_bp"] = (sfill / b - 1) * 1e4 if (b and sfill) else None
    held = {str(x.get("symbol")): x for x in positions}
    for sym, x in held.items():
        row = rows.setdefault(sym, {"sym": sym})
        row["held_qty"] = x.get("qty")
        row["avg_price"] = x.get("avg_entry_price")
        row["market_value"] = x.get("market_value")
    done = [n for n in nights if night_bp(n) is not None]
    totals = None
    if done:
        totals = {"nights": len(done),
                  "mean_bp": sum(night_bp(n) for n in done) / len(done),
                  "pnl_usd": round(sum(night_pnl(n) for n in done), 2),
                  "paper_pnl_usd": round(sum(n.get("pnl_usd") or 0 for n in done), 2),
                  "all20_mean_bp": (sum(n["mean_bp_plan"] for n in done if n.get("mean_bp_plan") is not None)
                                    / max(1, sum(n.get("mean_bp_plan") is not None for n in done))),
                  "green": sum(night_bp(n) > 0 for n in done)}
    return {
        "updated": now.timestamp(),
        "account": {"equity": account.get("equity"), "cash": account.get("cash"),
                    "last_equity": account.get("last_equity"), "start_equity": START_EQUITY},
        "plan": ({"day": plan_row.get("day"), "data_through": plan_row.get("data_through"),
                  "n_liquid": plan_row.get("n_liquid"),
                  "picks": [{"sym": r["sym"], "mom": r["mom"]} for r in plan_row.get("picks", [])]}
                 if plan_row else None),
        "book_night": night,
        "rows": sorted(rows.values(), key=lambda r: r["sym"]),
        "holding": len(held),
        "nights": done[-10:][::-1],
        "totals": totals,
        "backtest_bp": BACKTEST_BP,
        "order_mode": ORDER_MODE,
        "account_kind": ACCOUNT,
        "intraday_min": INTRADAY_MIN,
        "next_step": next_step,
        "errors": {k: v for k, v in (state_today or {}).items() if str(v).startswith("error")},
    }


def _next_step(tc, now: datetime) -> dict | None:
    """The next scheduled step, looking up to a week ahead."""
    for k in range(8):
        day = now.date() + timedelta(days=k)
        ses = session(tc, day)
        if not ses:
            continue
        op, cl = ses
        plan_at = datetime.combine(day, datetime.min.time(), ET).replace(hour=6, minute=30)
        steps = [("plan", plan_at)] + [(n, w) for n, w in schedule(op, cl) if n in ("sell", "buy")]
        for name, when in steps:
            if when > now:
                return {"name": name, "at": when.timestamp(), "mode": ORDER_MODE}
    return None


def write_snapshot(tc) -> None:
    now = datetime.now(ET)
    try:
        acct = tc.get_account()
        account = {"equity": float(acct.equity), "cash": float(acct.cash),
                   "last_equity": float(acct.last_equity) if acct.last_equity else None}
        positions = [{"symbol": x.symbol, "qty": float(x.qty), "avg_entry_price": float(x.avg_entry_price),
                      "market_value": float(x.market_value or 0)} for x in tc.get_all_positions()]
    except Exception as e:  # noqa: BLE001
        log(f"snapshot: account read failed: {e!s:.120}")
        account, positions = {}, []
    plans = sorted(OUT.glob("plan_*.json"))
    plan_row = None
    if plans:
        try:
            plan_row = json.loads(plans[-1].read_text())
        except (OSError, ValueError):
            plan_row = None
    try:
        nxt = _next_step(tc, now)
    except Exception:  # noqa: BLE001
        nxt = None
    snap = build_snapshot(plan_row, _read_jsonl(LEDGER), _read_jsonl(NIGHTS), positions, account,
                          load_state().get(now.date().isoformat(), {}), nxt, now)
    if LIVE:
        try:
            snap["compare"] = compare(print_it=False)
        except Exception as e:  # noqa: BLE001
            log(f"snapshot: compare failed: {e!s:.120}")
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = SNAPSHOT.with_suffix(".tmp")
    tmp.write_text(json.dumps(snap, default=str))
    os.replace(tmp, SNAPSHOT)


# ── scheduler ────────────────────────────────────────────────────────────────

def run() -> None:
    tc = book_client()
    log("run: overnight book scheduler started")
    last_snap = 0.0
    while True:
        try:
            now = datetime.now(ET)
            today = now.date()
            st = load_state()
            done = st.setdefault(today.isoformat(), {})
            ses = session(tc, today)
            if ses:
                op, cl = ses
                fns = {
                    "sell": lambda: sell(tc, today),
                    "sell_topup": lambda: sell(tc, today, attempt=1),
                    "reconcile_sell": lambda: reconcile(tc, today, "sell"),
                    "buy": lambda: buy(tc, today),
                    "buy_topup": lambda: buy_topup(tc, today),
                    "buy_check": lambda: buy_topup(tc, today, auction=True, tag="buy_check"),
                    "buy_fallback": lambda: buy_topup(tc, today, tag="buy_fallback"),
                    "sell_check": lambda: sell(tc, today, attempt=1, tif="opg", tag="sell_check"),
                    "sell_fallback": lambda: sell(tc, today, attempt=2, tif="day", tag="sell_fallback"),
                    "reconcile_buy": lambda: reconcile(tc, today, "buy"),
                }
                steps = [("plan", now.replace(hour=6, minute=30, second=0, microsecond=0), lambda: plan(tc, today))]
                steps += [(name, when, fns[name]) for name, when in schedule(op, cl)]
                for name, when, fn in steps:
                    if (name in ORDER_STEPS and name not in done and now >= when
                            and not live_armed()):
                        # Not "late": a disarmed step is skipped on purpose and
                        # logged once, so the ledger says why no order went in.
                        done[name] = f"skipped: live not armed at {now:%H:%M}"
                        log(f"{name} {today}: LIVE not armed ({ARMED_FILE.name} absent); no order")
                        save_state(st)
                        continue
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
                    last_snap = 0.0                 # a step ran: refresh the dashboard now
            if time.time() - last_snap >= SNAPSHOT_EVERY:
                write_snapshot(tc)
                last_snap = time.time()
        except Exception as e:  # noqa: BLE001
            log(f"run loop error: {e!s:.200}")
        time.sleep(30)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("plan", "buy", "sell", "topup", "reconcile", "score", "status", "snapshot",
                                    "check", "compare", "run"))
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
    if args.cmd == "compare":
        compare()
        return
    if args.cmd == "score":
        # --day is the morning the night ended (the sell day); needs no broker
        night_summary(day)
        return
    tc = book_client()
    if (LIVE and args.cmd in ("buy", "sell", "topup") and not args.dry_run
            and not live_armed()):
        raise SystemExit(f"REFUSED: live not armed (touch {ARMED_FILE} to arm)")
    if args.cmd == "buy":
        buy(tc, day, dry=args.dry_run)
    elif args.cmd == "sell":
        sell(tc, day, dry=args.dry_run)
    elif args.cmd == "topup":
        if args.leg == "sell":
            sell(tc, day, dry=args.dry_run, attempt=1)
        else:
            buy_topup(tc, day, dry=args.dry_run)
    elif args.cmd == "reconcile":
        for leg in ([args.leg] if args.leg else ["sell", "buy"]):
            reconcile(tc, day, leg)
    elif args.cmd == "check":
        check(tc, day)
    elif args.cmd == "status":
        status(tc)
        write_snapshot(tc)
    elif args.cmd == "snapshot":
        write_snapshot(tc)
    elif args.cmd == "run":
        run()


if __name__ == "__main__":
    main()
