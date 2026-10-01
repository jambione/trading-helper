#!/usr/bin/env python3
"""month_end_book.py — paper pilot of the month-end rebalancing trade.

docs/studies/MONTH_END_REBAL_2026-10-01.md: when stocks beat bonds over the
month, balanced funds sell stocks into the month-end close, and the reverse.
Holding SPY at -sign(S) over the month's LAST session made +20.6 bp a trade
(t 3.7) over 289 months, 2002-2026. The pre-registered 3-session cell failed
its validate split, so this one-session cell is a post-hoc pick; the pilot is
how it earns trust.

RULE
  S        SPY minus TLT return from the prior month's last close to the
           second-to-last session of this month (T-1). Live, at decide time,
           from the latest IEX trades against adjusted SIP daily closes; the
           backtest's S (official T-1 closes) is logged beside it.
  enter    T-1 close: SPY long if S < 0, short if S > 0
  exit     T close (the month's last session)

SCHEDULE (ET, from Alpaca's trading calendar, so holidays and early closes hold)
  MONTHEND_ORDER_MODE=market (default; Alpaca paper does not run auctions)
    decide  close - 5 min   enter  close - 2 min   (market, DAY)
    exit    close - 2 min on T
  MONTHEND_ORDER_MODE=auction (a real account)
    decide and enter at close - 15 min (MOC, before the 15:50 cutoff)
    exit    close - 15 min on T (MOC)
  reconcile  close + 30 min on T-1 and T (official closing crosses)

SCORING
  Scored on the PLAN, as the overnight book is: -sign(S) x SPY official close
  cross T-1 -> T, minus 2.26 bp. Paper fills are logged beside it as plumbing.
  The plan is scored every month even with no orders.

ISOLATION
  Orders ONLY with this book's own paper account, from
  config/secrets.json.monthend ({"api_key": ..., "secret_key": ...}) or
  MONTHEND_ALPACA_API_KEY / MONTHEND_ALPACA_SECRET_KEY. Without them it runs
  score-only. Refuses keys or accounts shared with the desk, the overnight
  paper book or the live test. Never paper=False. Market data and the calendar
  use the desk's keys read-only.

USAGE (on the mini)
  .venv/bin/python month_end_book.py status
  .venv/bin/python month_end_book.py decide [--day YYYY-MM-DD] [--dry-run]
  .venv/bin/python month_end_book.py enter|exit [--dry-run]
  .venv/bin/python month_end_book.py reconcile [--day YYYY-MM-DD]
  .venv/bin/python month_end_book.py run          # the scheduler loop
Logs: ai_reports/month_end/ (plan_YYYY-MM.json, ledger.jsonl, months.jsonl, state.json, run.log)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))
import overnight_book as ob  # keys, clients, calendar; no side effects

ET = ob.ET
OUT = ROOT / "ai_reports" / "month_end"
LEDGER = OUT / "ledger.jsonl"
MONTHS = OUT / "months.jsonl"
STATE = OUT / "state.json"
LOG = OUT / "run.log"
SYM, BOND = "SPY", "TLT"
COST_BP = 2.26            # SPY closing-auction round trip, as in the study
BACKTEST_BP = 20.6
EQUITY_FRAC = float(os.getenv("MONTHEND_EQUITY_FRAC", "0.98"))
ORDER_MODE = os.getenv("MONTHEND_ORDER_MODE", "market").strip().lower()
ORDER_STEPS = frozenset({"enter", "exit"})


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


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def save_json(path: Path, d: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(d, indent=1, default=str))


def plan_path(month: str) -> Path:
    return OUT / f"plan_{month}.json"


def monthend_keys() -> tuple[str, str]:
    api = (os.getenv("MONTHEND_ALPACA_API_KEY") or "").strip()
    sec = (os.getenv("MONTHEND_ALPACA_SECRET_KEY") or "").strip()
    if api and sec:
        return api, sec
    p = ROOT / "config" / "secrets.json.monthend"
    if p.exists():
        d = json.loads(p.read_text())
        return str(d.get("api_key") or ""), str(d.get("secret_key") or "")
    return "", ""


def book_client():
    """This book's own paper account after the isolation guard, or None (score-only)."""
    from alpaca.trading.client import TradingClient
    api, sec = monthend_keys()
    if not api or not sec:
        return None
    others = {k for k in (ob.desk_keys()[0], ob.overnight_keys()[0], ob.live_keys()[0]) if k}
    if api in others:
        raise SystemExit("REFUSED: the month-end keys are the desk's, the overnight book's or the live test's")
    tc = TradingClient(api, sec, paper=True)
    mine = tc.get_account().account_number
    accts = set()
    for k, s in (ob.desk_keys(), ob.overnight_keys()):
        if k and s:
            try:
                accts.add(TradingClient(k, s, paper=True).get_account().account_number)
            except Exception as e:  # noqa: BLE001
                log(f"isolation check: could not read an account ({e!s:.80})")
    if mine in accts:
        raise SystemExit("REFUSED: the month-end keys reach the desk's or the overnight book's account")
    return tc


# ── calendar ─────────────────────────────────────────────────────────────────

def sessions(cal, start: date, end: date) -> list[tuple[date, datetime, datetime]]:
    out = []
    for c in ob.calendar(cal, start, end):
        o, cl = c.open, c.close
        o = o.replace(tzinfo=ET) if o.tzinfo is None else o.astimezone(ET)
        cl = cl.replace(tzinfo=ET) if cl.tzinfo is None else cl.astimezone(ET)
        out.append((c.date, o, cl))
    return sorted(out)


def role(days: list[date], day: date) -> str | None:
    """'entry' on the month's second-to-last session, 'exit' on its last, else None.
    *days* = trading days in order, covering *day* through past the month end. Pure."""
    if day not in days:
        return None
    left = [d for d in days if d > day and (d.year, d.month) == (day.year, day.month)]
    if not any((d.year, d.month) > (day.year, day.month) for d in days):
        return None                      # cannot see where the month ends
    return "exit" if not left else "entry" if len(left) == 1 else None


def month_sessions(cal, day: date) -> tuple[str | None, date | None, date | None, datetime | None]:
    """(role, entry_day, exit_day, today's close) for *day*."""
    ss = sessions(cal, day - timedelta(days=40), day + timedelta(days=12))
    days = [d for d, _o, _c in ss]
    r = role(days, day)
    close = next((c for d, _o, c in ss if d == day), None)
    if r is None:
        return None, None, None, close
    i = days.index(day)
    return (r, day if r == "entry" else days[i - 1], days[i + 1] if r == "entry" else day, close)


def prior_month_end(cal, day: date) -> date:
    ss = [d for d, _o, _c in sessions(cal, day - timedelta(days=45), day)]
    return max(d for d in ss if (d.year, d.month) != (day.year, day.month))


# ── rule ─────────────────────────────────────────────────────────────────────

def signal(spy0: float, tlt0: float, spy1: float, tlt1: float) -> float:
    """Month-to-date SPY minus TLT return. Pure."""
    return (spy1 / spy0 - 1) - (tlt1 / tlt0 - 1)


def side_for(s: float) -> str | None:
    return "short" if s > 0 else "long" if s < 0 else None


def score(s: float, cx_entry: float, cx_exit: float) -> float:
    """Net bp of -sign(S) SPY from close cross to close cross. Pure."""
    d = -1 if s > 0 else 1 if s < 0 else 0
    return d * (cx_exit / cx_entry - 1) * 1e4 - (COST_BP if d else 0.0)


def adjusted_close(day: date) -> dict[str, float]:
    bars = ob.daily_closes([SYM, BOND], day, day)
    return {s: v[day.isoformat()][0] for s, v in bars.items() if day.isoformat() in v}


def close_cross(syms: list[str], day: date, close: datetime) -> dict:
    import auction_print_check as apc
    t0, t1 = close - timedelta(minutes=1), close + timedelta(minutes=2)
    return apc.auction_prints(ob.data_client(), syms, day.isoformat(), t0.hour, t0.minute, t1.hour, t1.minute,
                              apc.CLOSE_CODES, prefer="M")


# ── steps ────────────────────────────────────────────────────────────────────

def decide(cal, day: date, dry: bool = False) -> dict:
    r, entry, exit_, _cl = month_sessions(cal, day)
    if r != "entry":
        raise SystemExit(f"{day} is not the month's second-to-last session")
    d0 = prior_month_end(cal, day)
    base = adjusted_close(d0)
    px = ob.latest_prices([SYM, BOND])
    if len(base) < 2 or len(px) < 2:
        raise SystemExit(f"decide {day}: missing prices base={base} live={px}")
    s = signal(base[SYM], base[BOND], px[SYM], px[BOND])
    p = {"month": day.strftime("%Y-%m"), "entry_day": entry.isoformat(), "exit_day": exit_.isoformat(),
         "base_day": d0.isoformat(), "base": base, "live": px, "decided_at": datetime.now(ET).isoformat(),
         "S_live": s, "side": side_for(s), "mode": ORDER_MODE}
    log(f"decide {day}: SPY {(px[SYM] / base[SYM] - 1):+.2%} vs TLT {(px[BOND] / base[BOND] - 1):+.2%} "
        f"since {d0} -> S {s * 1e4:+.0f} bp -> {p['side'] or 'flat'} SPY into the {exit_} close")
    if not dry:
        save_json(plan_path(p["month"]), p)
    return p


def enter(tc, day: date, dry: bool = False) -> None:
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest
    p = load_json(plan_path(day.strftime("%Y-%m")))
    if not p or p.get("entry_day") != day.isoformat():
        raise SystemExit(f"enter {day}: no plan for today (run decide first)")
    if not p.get("side"):
        log(f"enter {day}: S is exactly 0, no trade")
        return
    held = [x for x in tc.get_all_positions() if x.symbol == SYM]
    if held:
        log(f"enter {day}: WARNING {SYM} already held ({held[0].qty}); no new order")
        return
    a = tc.get_account()
    px = p["live"][SYM]
    qty = int(EQUITY_FRAC * float(a.equity) // px)
    side = OrderSide.BUY if p["side"] == "long" else OrderSide.SELL
    cid = f"me-{day.isoformat()}-{SYM}-enter"
    row = {"event": "submit", "month": p["month"], "leg": "enter", "sym": SYM, "side": side.value, "qty": qty,
           "ref_price": px, "client_order_id": cid, "dry_run": dry}
    if qty <= 0:
        row["error"] = f"qty 0 (equity {a.equity})"
    elif not dry:
        try:
            tif = TimeInForce.CLS if ORDER_MODE == "auction" else TimeInForce.DAY
            o = tc.submit_order(MarketOrderRequest(symbol=SYM, qty=qty, side=side, time_in_force=tif,
                                                   client_order_id=cid))
            row["order_id"] = str(o.id)
        except Exception as e:  # noqa: BLE001
            row["error"] = str(e)[:200]
    if not dry:
        append(LEDGER, row)
    log(f"enter {day}: {'DRY RUN ' if dry else ''}{side.value} {qty} {SYM} (~${qty * px:,.0f}) "
        f"as {'MOC' if ORDER_MODE == 'auction' else 'market'}" + (f"; ERROR {row['error']}" if row.get("error") else ""))


def exit_(tc, day: date, dry: bool = False) -> None:
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest
    pos = [x for x in tc.get_all_positions() if x.symbol == SYM]
    if not pos:
        log(f"exit {day}: no {SYM} position")
        return
    x = pos[0]
    qty = abs(int(float(x.qty)))
    long_ = str(getattr(x.side, "value", x.side)).lower() == "long"
    side = OrderSide.SELL if long_ else OrderSide.BUY
    cid = f"me-{day.isoformat()}-{SYM}-exit"
    row = {"event": "submit", "month": day.strftime("%Y-%m"), "leg": "exit", "sym": SYM, "side": side.value,
           "qty": qty, "client_order_id": cid, "dry_run": dry}
    if not dry:
        try:
            tif = TimeInForce.CLS if ORDER_MODE == "auction" else TimeInForce.DAY
            o = tc.submit_order(MarketOrderRequest(symbol=SYM, qty=qty, side=side, time_in_force=tif,
                                                   client_order_id=cid))
            row["order_id"] = str(o.id)
        except Exception as e:  # noqa: BLE001
            row["error"] = str(e)[:200]
        append(LEDGER, row)
    log(f"exit {day}: {'DRY RUN ' if dry else ''}{side.value} {qty} {SYM} to flatten the "
        f"{'long' if long_ else 'short'}" + (f"; ERROR {row['error']}" if row.get("error") else ""))


def fills(tc, day: date, leg: str) -> dict | None:
    if tc is None:
        return None
    orders = ob.our_orders(tc, f"me-{day.isoformat()}-{SYM}-{leg}")
    return ob.aggregate_orders(orders).get(SYM) if orders else None


def reconcile(cal, tc, day: date) -> None:
    r, _entry, _exit, close = month_sessions(cal, day)
    if r is None:
        log(f"reconcile {day}: not an entry or exit day")
        return
    month = day.strftime("%Y-%m")
    p = load_json(plan_path(month))
    if r == "entry":
        cx = close_cross([SYM, BOND], day, close)
        base = p.get("base") or adjusted_close(prior_month_end(cal, day))
        adj = adjusted_close(day)
        p["cross_entry"] = cx.get(SYM, [None])[0]
        if len(base) == 2 and len(adj) == 2:
            p["S_backtest"] = signal(base[SYM], base[BOND], adj[SYM], adj[BOND])
            p["sign_agrees"] = (p.get("side") == side_for(p["S_backtest"])) if p.get("side") else None
        f = fills(tc, day, "enter")
        p["entry_fill"] = f
        save_json(plan_path(month), p)
        log(f"reconcile entry {day}: SPY close cross {p['cross_entry']}, backtest S "
            f"{(p.get('S_backtest') or 0) * 1e4:+.0f} bp (live {(p.get('S_live') or 0) * 1e4:+.0f}, "
            f"sign agrees: {p.get('sign_agrees')}); paper fill {f and f.get('fill')}")
        return
    if not p:
        log(f"reconcile exit {day}: no plan for {month}; nothing to score")
        return
    cx = close_cross([SYM], day, close).get(SYM, [None])[0]
    if not p.get("cross_entry"):
        p["cross_entry"] = close_cross([SYM], date.fromisoformat(p["entry_day"]),
                                       month_sessions(cal, date.fromisoformat(p["entry_day"]))[3]).get(SYM, [None])[0]
    row = {"month": month, "entry_day": p["entry_day"], "exit_day": day.isoformat(), "side": p.get("side"),
           "S_live_bp": (p.get("S_live") or 0) * 1e4, "S_backtest_bp": (p.get("S_backtest") or 0) * 1e4,
           "cross_entry": p.get("cross_entry"), "cross_exit": cx, "backtest_bp": BACKTEST_BP}
    if row["cross_entry"] and cx and p.get("S_live") is not None:
        row["plan_bp"] = score(p["S_live"], row["cross_entry"], cx)
        if p.get("S_backtest") is not None:
            row["backtest_rule_bp"] = score(p["S_backtest"], row["cross_entry"], cx)
    fe, fx = p.get("entry_fill") or fills(tc, date.fromisoformat(p["entry_day"]), "enter"), fills(tc, day, "exit")
    if fe and fx and fe.get("fill") and fx.get("fill"):
        d = 1 if p.get("side") == "long" else -1
        q = min(fe["filled_qty"], fx["filled_qty"])
        row["paper"] = {"qty": q, "entry": fe["fill"], "exit": fx["fill"],
                        "bp": d * (fx["fill"] / fe["fill"] - 1) * 1e4, "pnl": d * q * (fx["fill"] - fe["fill"])}
    append(MONTHS, row)
    log(f"score {month}: {row.get('side')} SPY {row['cross_entry']} -> {cx}: plan "
        f"{row.get('plan_bp', float('nan')):+.1f} bp net (backtest mean {BACKTEST_BP:+.1f})"
        + (f"; paper {row['paper']['bp']:+.1f} bp ${row['paper']['pnl']:+,.2f}" if row.get("paper") else "; no paper fills"))


def status(cal, tc) -> None:
    today = datetime.now(ET).date()
    ss = [d for d, _o, _c in sessions(cal, today, today + timedelta(days=70))]
    nxt = next(d for d in ss if role(ss, d) == "entry")
    print(f"orders: {'paper account ' + tc.get_account().account_number if tc else 'NONE (score-only; add config/secrets.json.monthend)'}"
          f"; mode {ORDER_MODE}")
    print(f"next entry {nxt} (decide at close - {'15' if ORDER_MODE == 'auction' else '5'} min), exit "
          f"{next(d for d in ss if d > nxt)}")
    if tc:
        for x in tc.get_all_positions():
            print(f"  position {x.symbol} {x.qty} {x.side}")
    for line in Path(MONTHS).read_text().splitlines()[-6:] if MONTHS.exists() else []:
        r = json.loads(line)
        print(f"  {r['month']}: {r.get('side')} plan {r.get('plan_bp', float('nan')):+.1f} bp"
              + (f", paper {r['paper']['bp']:+.1f} bp" if r.get("paper") else ""))


def schedule(r: str, close: datetime) -> list[tuple[str, datetime]]:
    auction = ORDER_MODE == "auction"
    lead = timedelta(minutes=15) if auction else timedelta(minutes=2)
    if r == "entry":
        return [("decide", close - (timedelta(minutes=15) if auction else timedelta(minutes=5))),
                ("enter", close - lead), ("reconcile", close + timedelta(minutes=30))]
    return [("exit", close - lead), ("reconcile", close + timedelta(minutes=30))]


def step(name: str, cal, tc, day: date) -> None:
    if name == "decide":
        decide(cal, day)
    elif name == "enter":
        enter(tc, day)
    elif name == "exit":
        exit_(tc, day)
    else:
        reconcile(cal, tc, day)


def run() -> None:
    cal = ob.desk_trading_client()
    tc = book_client()
    log(f"run: month-end book started ({'orders on ' + tc.get_account().account_number if tc else 'score-only'}, "
        f"mode {ORDER_MODE})")
    cached: tuple[date, tuple] | None = None
    while True:
        try:
            now = datetime.now(ET)
            today = now.date()
            if not cached or cached[0] != today:
                cached = (today, month_sessions(cal, today))
            r, _e, _x, close = cached[1]
            if r:
                st = load_json(STATE)
                done = st.setdefault(today.isoformat(), {})
                for name, when in schedule(r, close):
                    if name in done or now < when:
                        continue
                    late = now - when > timedelta(minutes=10) and name != "reconcile"
                    if late:
                        done[name] = f"skipped late at {now:%H:%M}"
                        log(f"{name} {today}: window missed, skipped")
                    elif name in ORDER_STEPS and tc is None:
                        done[name] = "skipped: score-only (no month-end keys)"
                        log(f"{name} {today}: score-only, no order")
                    else:
                        try:
                            step(name, cal, tc, today)
                            done[name] = f"ok {now:%H:%M}"
                        except BaseException as e:  # SystemExit from a step must not kill the loop
                            if isinstance(e, KeyboardInterrupt):
                                raise
                            done[name] = f"error {e!s:.120}"
                            log(f"{name} {today}: ERROR {e!s:.200}\n{traceback.format_exc()}")
                    save_json(STATE, st)
        except Exception as e:  # noqa: BLE001
            log(f"run loop error: {e!s:.200}")
        time.sleep(30)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("status", "decide", "enter", "exit", "reconcile", "run"))
    ap.add_argument("--day", default=None, help="ET date, default today")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    day = date.fromisoformat(args.day) if args.day else datetime.now(ET).date()
    if args.cmd == "run":
        return run()
    cal = ob.desk_trading_client()
    if args.cmd == "decide":
        decide(cal, day, dry=args.dry_run)
        return
    tc = book_client()
    if args.cmd == "status":
        status(cal, tc)
    elif args.cmd == "reconcile":
        reconcile(cal, tc, day)
    else:
        if tc is None:
            raise SystemExit("no month-end keys: put them in config/secrets.json.monthend")
        (enter if args.cmd == "enter" else exit_)(tc, day, dry=args.dry_run)


if __name__ == "__main__":
    main()
