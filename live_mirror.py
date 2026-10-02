#!/usr/bin/env python3
"""live_mirror.py — copy the PAPER desk's orders to the small LIVE account at 1 share.

WHY
  Alpaca paper fills at the displayed quote, instantly, at any size; live
  orders are routed and can slip, partially fill or sit, most of all at the
  open. The mirror keeps every decision on paper and copies each order to the
  live account, so each paper fill has a live twin made from the same
  decision. The gap between them is execution alone. `report` prints it.

WHAT IS COPIED (scope chosen 2026-10-02: entries, exits, protective stops)
  buy (any type, bracket legs included)  -> 1 share, same type/limit/stop/tif
  protective sell (stop, stop_limit, trailing_stop) -> the live shares held,
                                            unless one is already working
  exit sell (market, limit) / close_position -> cancel the live orders on the
                                            name, then the same exit
  cancel_order_by_id / cancel_orders     -> the mapped live order(s)
  Not copied: stop moves (the desk cancels and resubmits, which IS copied) or
  partial scale-outs (1 share cannot split). A sync pass every 15 s flattens
  a live name the paper desk no longer holds ("sync_exit"), and 15:55 ET
  flattens everything ("eod_flatten"); report shows those separately.

SAFETY
  Armed only while config/live_mirror.armed exists (optionally holding a
  YYYY-MM-DD start date) AND config/overnight_live.armed does NOT exist: the
  two tests share the $100 account, never at once. Checked per event; no
  restart. Live keys from config/secrets.json.live; refuses keys or an account
  that match the paper desk, and a live account that is not ACTIVE. Caps: 1
  share, LIVE_MIRROR_MAX_ORDER dollars per buy (default 60), and the day's
  buys within the start-of-day cash (a cash account may only buy with settled
  funds). Only touches orders it created (client_order_id "mir-...") and
  names it bought. The desk's own call returns before the mirror sees it;
  the mirror runs on its own thread and swallows its own errors, so it can
  never slow, block or break a paper order.

USAGE (on the mini)
  attached by ai_trading.py when the desk runs paper (alpaca_trader.init)
  .venv/bin/python live_mirror.py check              # read-only: arm state, account, budget
  .venv/bin/python live_mirror.py report [--day D]   # paper vs live fills, by time of day
Logs: ai_reports/live_mirror/ledger.jsonl, run.log
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import threading
import time
import traceback
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
ET = ZoneInfo("America/New_York")
ARM_FILE = ROOT / "config" / "live_mirror.armed"
OVERNIGHT_ARM_FILE = ROOT / "config" / "overnight_live.armed"
KEYS_FILE = ROOT / "config" / "secrets.json.live"
OUT = ROOT / "ai_reports" / "live_mirror"
LEDGER = OUT / "ledger.jsonl"
LOG = OUT / "run.log"
CID_PREFIX = "mir-"
MAX_ORDER = float(os.getenv("LIVE_MIRROR_MAX_ORDER", "60"))
MAX_SHARES = 1
SYNC_EVERY = 15.0
EOD_FLATTEN = (15, 55)
PROTECTIVE = {"stop", "stop_limit", "trailing_stop"}


# ── plumbing ─────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    try:
        OUT.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a") as f:
            f.write(f"{datetime.now(ET):%Y-%m-%d %H:%M:%S} {msg}\n")
    except OSError:
        pass


def append(row: dict) -> None:
    try:
        OUT.mkdir(parents=True, exist_ok=True)
        with open(LEDGER, "a") as f:
            f.write(json.dumps(row, default=str) + "\n")
    except OSError:
        pass


def _v(x):
    return str(getattr(x, "value", x)).lower() if x is not None else None


def armed(today: date | None = None) -> tuple[bool, str]:
    """(armed, why). Pure apart from two file reads."""
    if OVERNIGHT_ARM_FILE.exists():
        return False, f"{OVERNIGHT_ARM_FILE.name} present (the overnight test owns the account)"
    if not ARM_FILE.exists():
        return False, f"{ARM_FILE.name} absent"
    try:
        txt = ARM_FILE.read_text().strip()
    except OSError:
        return False, f"{ARM_FILE.name} unreadable"
    if not txt:
        return True, "armed"
    try:
        start = date.fromisoformat(txt.split()[0])
    except ValueError:
        return False, f"{ARM_FILE.name} has a bad date {txt[:20]!r}"
    today = today or datetime.now(ET).date()
    return (today >= start, "armed" if today >= start else f"arms on {start}")


def order_kind(req) -> str:
    """market | limit | stop | stop_limit | trailing_stop, from the request. Pure."""
    t = _v(getattr(req, "type", None))
    if t:
        return t
    name = type(req).__name__.lower()
    for k in ("trailingstop", "stoplimit", "stop", "limit", "market"):
        if name.startswith(k):
            return {"trailingstop": "trailing_stop", "stoplimit": "stop_limit"}.get(k, k)
    return "market"


def live_cid(desk_order, req) -> str:
    base = str(getattr(desk_order, "client_order_id", None) or getattr(req, "client_order_id", None)
               or getattr(desk_order, "id", "") or time.time_ns())
    return (CID_PREFIX + base)[:120]


def copy_request(req, qty: int, cid: str):
    """The desk's request with qty=qty, no notional, our client_order_id. Pure."""
    return req.model_copy(update={"qty": qty, "notional": None, "client_order_id": cid})


def buy_allowed(price: float | None, spent: float, start_cash: float) -> tuple[bool, str]:
    """Caps for one 1-share buy. Pure."""
    if not price or price <= 0:
        return False, "no_price"
    if price > MAX_ORDER:
        return False, f"price ${price:.2f} > cap ${MAX_ORDER:g}"
    if spent + price > 0.98 * start_cash:
        return False, f"day budget: spent ${spent:.2f} + ${price:.2f} > settled ${start_cash:.2f}"
    return True, "ok"


# ── the mirror ───────────────────────────────────────────────────────────────

class Mirror:
    def __init__(self, desk_client, desk_account: str = "", desk_key: str = ""):
        self.desk = desk_client
        self.desk_account = desk_account
        self.desk_key = desk_key
        self.q: queue.Queue = queue.Queue(maxsize=2000)
        self.live = None
        self.live_err = ""
        self.id_map: dict[str, str] = {}
        self.day = None
        self.start_cash = 0.0
        self.spent = 0.0
        self.bought: set[str] = set()
        self.last_sync = 0.0
        self.eod_done = None
        self.thread = threading.Thread(target=self._run, name="live-mirror", daemon=True)

    # called on the desk's thread: must never raise or block
    def enqueue(self, kind: str, *args) -> None:
        try:
            self.q.put_nowait((kind, time.time(), args))
        except queue.Full:
            log(f"queue full, dropped {kind}")

    def start(self) -> None:
        self.thread.start()
        log(f"mirror attached to the paper desk ({armed()[1]})")

    # ── live client ──
    def client(self):
        if self.live is not None:
            return self.live
        from alpaca.trading.client import TradingClient
        try:
            d = json.loads(KEYS_FILE.read_text())
            api, sec = str(d.get("api_key") or ""), str(d.get("secret_key") or "")
        except (OSError, ValueError):
            api = sec = ""
        if not api or not sec:
            raise RuntimeError(f"no live keys in {KEYS_FILE}")
        if self.desk_key and api == self.desk_key:
            raise RuntimeError("REFUSED: the live keys are the paper desk's keys")
        tc = TradingClient(api, sec, paper=False)
        a = tc.get_account()
        if _v(a.status) != "active" or getattr(a, "trading_blocked", False):
            raise RuntimeError(f"REFUSED: live account status {a.status}")
        if self.desk_account and str(a.account_number) == self.desk_account:
            raise RuntimeError("REFUSED: the live keys reach the paper desk's account")
        self.live = tc
        log(f"live client ready: account {a.account_number}, cash ${float(a.cash):,.2f}")
        return tc

    def _roll_day(self, tc) -> None:
        today = datetime.now(ET).date()
        if self.day == today:
            return
        a = tc.get_account()
        self.day, self.start_cash, self.spent, self.bought = today, float(a.cash), 0.0, set()
        others = [p.symbol for p in tc.get_all_positions()]
        log(f"day {today}: settled cash ${self.start_cash:,.2f}"
            + (f"; positions not ours, left alone: {others}" if others else ""))

    def _held(self, tc, sym: str) -> int:
        for p in tc.get_all_positions():
            if p.symbol == sym:
                return abs(int(float(p.qty)))
        return 0

    def _our_open(self, tc, sym: str | None = None) -> list:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=200, symbols=[sym] if sym else None)
        return [o for o in tc.get_orders(req) if str(o.client_order_id or "").startswith(CID_PREFIX)]

    def _cancel_name(self, tc, sym: str) -> int:
        n = 0
        for o in self._our_open(tc, sym):
            try:
                tc.cancel_order_by_id(o.id)
                n += 1
            except Exception as e:  # noqa: BLE001
                log(f"cancel {sym} {o.id}: {e!s:.120}")
        for _ in range(20):                       # shares held by a working order cannot be sold
            if not self._our_open(tc, sym):
                break
            time.sleep(0.1)
        return n

    def _price(self, sym: str) -> float | None:
        try:
            from alpaca.data.enums import DataFeed
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.data.requests import StockLatestTradeRequest
            d = json.loads(KEYS_FILE.read_text())
            dc = StockHistoricalDataClient(d["api_key"], d["secret_key"])
            t = dc.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=sym, feed=DataFeed.IEX))
            return float(t[sym].price)
        except Exception as e:  # noqa: BLE001
            log(f"price {sym}: {e!s:.100}")
            return None

    # ── handlers ──
    def on_submit(self, t_desk: float, req, desk_order) -> None:
        tc = self.client()
        self._roll_day(tc)
        sym = str(getattr(req, "symbol", "")).upper()
        side, kind = _v(getattr(req, "side", None)), order_kind(req)
        base = {"t_desk": t_desk, "sym": sym, "side": side, "type": kind, "tif": _v(getattr(req, "time_in_force", None)),
                "limit": getattr(req, "limit_price", None), "stop": getattr(req, "stop_price", None),
                "order_class": _v(getattr(req, "order_class", None)), "qty_desk": getattr(req, "qty", None),
                "notional_desk": getattr(req, "notional", None),
                "desk_order_id": str(getattr(desk_order, "id", "") or ""),
                "desk_cid": getattr(desk_order, "client_order_id", None)}
        if side == "buy":
            px = getattr(req, "limit_price", None) or self._price(sym)
            ok, why = buy_allowed(float(px) if px else None, self.spent, self.start_cash)
            if not ok:
                append({"event": "mirror_skip", **base, "reason": why, "ref_price": px})
                return
            qty, ref = MAX_SHARES, float(px)
        elif side == "sell":
            held = self._held(tc, sym)
            if held <= 0:
                append({"event": "mirror_skip", **base, "reason": "no live position"})
                return
            if kind in PROTECTIVE:
                if any(_v(o.side) == "sell" for o in self._our_open(tc, sym)):
                    append({"event": "mirror_skip", **base, "reason": "live protective sell already working"})
                    return
            else:
                self._cancel_name(tc, sym)
            qty, ref = held, None
        else:
            append({"event": "mirror_skip", **base, "reason": f"side {side}"})
            return
        cid = live_cid(desk_order, req)
        row = {"event": "mirror_submit", **base, "qty_live": qty, "ref_price": ref, "live_cid": cid}
        try:
            o = tc.submit_order(copy_request(req, qty, cid))
            row.update(live_order_id=str(o.id), t_live=time.time(), live_status=_v(o.status))
            if base["desk_order_id"]:
                self.id_map[base["desk_order_id"]] = str(o.id)
            if side == "buy":
                self.spent += ref
                self.bought.add(sym)
        except Exception as e:  # noqa: BLE001
            row["error"] = str(e)[:200]
        append(row)

    def on_cancel(self, t: float, desk_id: str) -> None:
        live_id = self.id_map.pop(desk_id, None)
        if not live_id:
            return
        tc = self.client()
        row = {"event": "mirror_cancel", "t_desk": t, "desk_order_id": desk_id, "live_order_id": live_id}
        try:
            tc.cancel_order_by_id(live_id)
        except Exception as e:  # noqa: BLE001
            row["error"] = str(e)[:200]           # usually "already filled"
        append(row)

    def on_cancel_all(self, t: float) -> None:
        tc = self.client()
        n = 0
        for o in self._our_open(tc):
            try:
                tc.cancel_order_by_id(o.id)
                n += 1
            except Exception as e:  # noqa: BLE001
                log(f"cancel_all {o.id}: {e!s:.120}")
        self.id_map.clear()
        append({"event": "mirror_cancel_all", "t_desk": t, "cancelled": n})

    def flatten(self, tc, sym: str, why: str, t: float | None = None) -> None:
        if self._held(tc, sym) <= 0:
            return
        self._cancel_name(tc, sym)
        row = {"event": "mirror_exit", "reason": why, "sym": sym, "t_desk": t, "t_live": time.time()}
        try:
            o = tc.close_position(sym)
            row.update(live_order_id=str(getattr(o, "id", "")), live_cid=getattr(o, "client_order_id", None))
        except Exception as e:  # noqa: BLE001
            row["error"] = str(e)[:200]
        append(row)

    def on_close(self, t: float, sym: str) -> None:
        tc = self.client()
        if sym.upper() in self.bought:
            self.flatten(tc, sym.upper(), "desk_close_position", t)

    def sync(self) -> None:
        tc = self.client()
        self._roll_day(tc)
        now = datetime.now(ET)
        if not self.bought:
            return
        if (now.hour, now.minute) >= EOD_FLATTEN and self.eod_done != now.date():
            for s in sorted(self.bought):
                self.flatten(tc, s, "eod_flatten")
            self.eod_done = now.date()
            return
        desk = {p.symbol for p in self.desk.get_all_positions() if abs(float(p.qty)) > 0}
        for s in sorted(self.bought):
            if s not in desk:
                self.flatten(tc, s, "sync_exit")

    # ── worker ──
    def handle(self, kind: str, t: float, args: tuple) -> None:
        if kind == "submit":
            self.on_submit(t, *args)
        elif kind == "cancel":
            self.on_cancel(t, *args)
        elif kind == "cancel_all":
            self.on_cancel_all(t)
        elif kind == "close":
            self.on_close(t, *args)

    def _run(self) -> None:
        while True:
            try:
                kind, t, args = self.q.get(timeout=5)
            except queue.Empty:
                kind = None
            ok = armed()[0]
            try:
                if kind and ok:
                    self.handle(kind, t, args)
                if ok and time.time() - self.last_sync >= SYNC_EVERY:
                    self.last_sync = time.time()
                    self.sync()
            except Exception as e:  # noqa: BLE001
                log(f"{kind or 'sync'}: {e!s:.200}\n{traceback.format_exc()}")
                if "REFUSED" in str(e) or "no live keys" in str(e):
                    self.live = None
                    time.sleep(60)


class MirroredClient:
    """Forwards everything to the paper client; tells the mirror about orders
    AFTER the paper call returns. A failed paper call is not mirrored."""

    def __init__(self, real, mirror: Mirror):
        self._real = real
        self._mirror = mirror

    def __getattr__(self, name):
        return getattr(self._real, name)

    def submit_order(self, order_data, *a, **k):
        o = self._real.submit_order(order_data, *a, **k)
        self._mirror.enqueue("submit", order_data, o)
        return o

    def cancel_order_by_id(self, order_id, *a, **k):
        r = self._real.cancel_order_by_id(order_id, *a, **k)
        self._mirror.enqueue("cancel", str(order_id))
        return r

    def cancel_orders(self, *a, **k):
        r = self._real.cancel_orders(*a, **k)
        self._mirror.enqueue("cancel_all")
        return r

    def close_position(self, symbol_or_asset_id, *a, **k):
        r = self._real.close_position(symbol_or_asset_id, *a, **k)
        self._mirror.enqueue("close", str(symbol_or_asset_id))
        return r


def attach(trader) -> Mirror | None:
    """Wrap alpaca_trader's client when the desk is on PAPER. Never raises."""
    try:
        if getattr(trader, "_mode", "") != "paper" or trader._client is None:
            return None
        if isinstance(trader._client, MirroredClient):
            return trader._client._mirror
        m = Mirror(trader._client, desk_account=str(getattr(trader, "_account_number", "") or ""),
                   desk_key=str(getattr(trader._client, "_api_key", "") or ""))
        trader._client = MirroredClient(trader._client, m)
        m.start()
        return m
    except Exception as e:  # noqa: BLE001
        log(f"attach failed: {e!s:.200}")
        return None


# ── read-only commands ───────────────────────────────────────────────────────

def _read_ledger() -> list[dict]:
    try:
        return [json.loads(x) for x in LEDGER.read_text().splitlines() if x.strip()]
    except OSError:
        return []


def bucket(ts: float) -> str:
    t = datetime.fromtimestamp(ts, ET)
    m = t.hour * 60 + t.minute
    return "09:30-09:45" if m < 585 else "09:45-10:30" if m < 630 else "10:30-15:00" if m < 900 else "15:00-close"


def report(day: str | None) -> None:
    from alpaca.trading.client import TradingClient

    from config import load_config
    c = load_config() or {}
    paper = TradingClient(c.get("api_key"), c.get("secret_key"), paper=True)
    d = json.loads(KEYS_FILE.read_text())
    live = TradingClient(d["api_key"], d["secret_key"], paper=False)
    rows = [r for r in _read_ledger() if r.get("event") == "mirror_submit" and r.get("live_order_id")
            and (not day or datetime.fromtimestamp(r["t_desk"], ET).date().isoformat() == day)]
    by: dict[str, list] = {}
    print(f"{'time':8s} {'sym':6s} {'side':4s} {'type':7s} {'paper':>9s} {'live':>9s} {'live-paper bp':>13s} {'lag s':>6s}")
    for r in rows:
        try:
            p = paper.get_order_by_id(r["desk_order_id"])
            lv = live.get_order_by_id(r["live_order_id"])
        except Exception as e:  # noqa: BLE001
            print(f"  {r['sym']}: {e!s:.80}")
            continue
        pf = float(p.filled_avg_price) if p.filled_avg_price else None
        lf = float(lv.filled_avg_price) if lv.filled_avg_price else None
        sgn = 1 if r["side"] == "buy" else -1
        bp = sgn * (lf / pf - 1) * 1e4 if (pf and lf) else None
        lag = (lv.filled_at - p.filled_at).total_seconds() if (p.filled_at and lv.filled_at) else None
        b = bucket(r["t_desk"])
        by.setdefault(b, []).append((bp, r["type"], bool(pf), bool(lf)))
        print(f"{datetime.fromtimestamp(r['t_desk'], ET):%H:%M:%S} {r['sym']:6s} {r['side']:4s} {r['type']:7s} "
              f"{pf or float('nan'):9.3f} {lf or float('nan'):9.3f} "
              f"{(f'{bp:+.1f}' if bp is not None else 'n/a'):>13s} {(f'{lag:+.1f}' if lag is not None else ''):>6s}")
    print("\nby time of day (positive bp = live filled WORSE than paper):")
    for b in sorted(by):
        xs = [x[0] for x in by[b] if x[0] is not None]
        lims = [x for x in by[b] if x[1] == "limit"]
        print(f"  {b}: {len(by[b])} orders, both filled {len(xs)}, mean {sum(xs) / len(xs) if xs else float('nan'):+.1f} bp"
              + (f"; limits filled paper {sum(x[2] for x in lims)}/{len(lims)} live {sum(x[3] for x in lims)}/{len(lims)}"
                 if lims else ""))
    skips = [r for r in _read_ledger() if r.get("event") == "mirror_skip"
             and (not day or datetime.fromtimestamp(r["t_desk"], ET).date().isoformat() == day)]
    if skips:
        from collections import Counter
        print("skipped: " + ", ".join(f"{k} x{v}" for k, v in Counter(r["reason"].split(":")[0] for r in skips).most_common()))


def check() -> None:
    ok, why = armed()
    print(f"mirror: {'ARMED' if ok else 'not armed'} ({why}); caps 1 share, ${MAX_ORDER:g}/buy")
    m = Mirror(None)
    try:
        tc = m.client()
        a = tc.get_account()
        print(f"live account {a.account_number} {a.status}: cash ${float(a.cash):,.2f} equity ${float(a.equity):,.2f}")
        for p in tc.get_all_positions():
            print(f"  holding {p.symbol} {p.qty}")
        print(f"  open mirror orders: {len(m._our_open(tc))}")
    except Exception as e:  # noqa: BLE001
        print(f"live client: {e}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("check", "report"))
    ap.add_argument("--day", default=None)
    a = ap.parse_args()
    check() if a.cmd == "check" else report(a.day)


if __name__ == "__main__":
    main()
