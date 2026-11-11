#!/usr/bin/env python3
"""pick_positions.py - PAPER positions for the fact-sheet picker's weekly picks (operator 11/11: "build option 1 with paper positions").

Every pick in ~/pick_ledgers/facts is bought in the desk's Alpaca PAPER account on its entry day (the first session after it
was recorded), in equal dollars, and sold 21 sessions later. This is a display of the test, not the test: the verdict still
comes from the ledger (docs/studies/fact_picker_prereg.json), which uses the official open. Paper fills come a couple of
minutes after the open, so the two differ slightly. There is no early-sell rule: a pick is held its 21 sessions.

    .venv/bin/python tools/pick_positions.py sync          # weekdays 09:32 ET (com.jambi.pick-positions): buy today's entries, sell what is due
    .venv/bin/python tools/pick_positions.py sync --dry    # print what it would do; no order
    .venv/bin/python tools/pick_positions.py status        # how each holding is doing -> ~/pick_ledgers/POSITIONS.txt

PAPER ONLY: the client is always built with paper=True and the run refuses if the keys are the live account's.
A pick whose entry day was missed (machine off at the open) is never bought late; it is marked missed and stays in the ledger test.
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import auto_picker as A  # noqa: E402
import pick_ledger as P  # noqa: E402

ET = A.ET
LEDGER_NAME = "facts"
HOLD = P.EARLY                      # 21 sessions, the test's own window
SLOTS = 15                          # 3 picks a week held 21 sessions = at most 15 at once; each gets equity / 15


def state_path() -> Path:
    return A.HOME / LEDGER_NAME / "positions.json"


def load_state() -> dict:
    f = state_path()
    return json.loads(f.read_text()) if f.exists() else {}


def save_state(st: dict) -> None:
    f = state_path()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(st, indent=1) + "\n")


# ---------- pure planning ----------
def entry_day(recorded_at: str, sessions: list[str]) -> str | None:
    """The first session whose 09:30 ET open comes after the pick was recorded (the ledger's own entry rule)."""
    rec = datetime.fromisoformat(recorded_at)
    for d in sessions:
        if datetime.combine(date.fromisoformat(d), dtime(9, 30), ET) > rec:
            return d
    return None


def exit_day(entry: str, sessions: list[str], hold: int = HOLD) -> str | None:
    i = sessions.index(entry) + hold if entry in sessions else None
    return sessions[i] if i is not None and i < len(sessions) else None


def plan(picks: list[dict], state: dict, sessions: list[str], today: str) -> list[tuple[str, str]]:
    """[(action, pick id)] for today: 'buy' a pick entering today, mark 'missed' one whose entry day has passed unbought,
    'sell' a holding whose 21st session has come. Nothing is ever bought late."""
    out = []
    for p in picks:
        if p.get("retro"):
            continue
        st = state.get(p["id"])
        if st is None:
            e = entry_day(p["recorded_at"], sessions)
            if e == today:
                out.append(("buy", p["id"]))
            elif e is not None and e < today:
                out.append(("missed", p["id"]))
        elif st.get("status") == "open":
            x = exit_day(st["entry_day"], sessions)
            if x is not None and x <= today:
                out.append(("sell", p["id"]))
    return out


def size(equity: float, cash: float, slots: int = SLOTS) -> float:
    """Equal dollars per pick, never more than the cash on hand (no borrowing)."""
    return max(0.0, round(min(equity / slots, cash), 2))


# ---------- broker (paper only) ----------
def client():
    import overnight_book as ob
    from alpaca.trading.client import TradingClient
    api, sec = ob.desk_keys()
    if not api or not sec:
        raise SystemExit("no paper keys in config/secrets.json")
    if api == ob.live_keys()[0]:
        raise SystemExit("REFUSED: these are the live account's keys")
    return TradingClient(api, sec, paper=True)


def session_list(tc, start: date, end: date) -> list[str]:
    from alpaca.trading.requests import GetCalendarRequest
    return [str(c.date) for c in tc.get_calendar(GetCalendarRequest(start=start, end=end))]


def refresh_fills(tc, state: dict) -> None:
    for st in state.values():
        for leg in ("buy", "sell"):
            oid = st.get(f"{leg}_order")
            if oid and st.get(f"{leg}_px") is None:
                try:
                    o = tc.get_order_by_id(oid)
                except Exception:  # noqa: BLE001
                    continue
                if o.filled_avg_price:
                    st[f"{leg}_px"], st[f"{leg}_qty"] = float(o.filled_avg_price), float(o.filled_qty or 0)
                elif str(o.status).split(".")[-1].lower() in ("canceled", "expired", "rejected") and leg == "buy":
                    st["status"] = "not filled"


def sync(dry: bool = False, now: datetime | None = None) -> int:
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest
    now = now or datetime.now(ET)
    today = now.strftime("%Y-%m-%d")
    picks = P.read_ledger(A.ledger_path(LEDGER_NAME))
    state = load_state()
    tc = client()
    refresh_fills(tc, state)
    if not picks:
        print("no picks in the ledger yet")
        return 0
    first = min(datetime.fromisoformat(p["recorded_at"]) for p in picks).date()
    sessions = session_list(tc, first - timedelta(days=7), now.date() + timedelta(days=60))
    todo = plan(picks, state, sessions, today)
    by_id = {p["id"]: p for p in picks}
    is_open = bool(tc.get_clock().is_open)
    for action, pid in todo:
        sym = by_id[pid]["symbol"]
        if action == "missed":
            print(f"{sym}: entry day passed without a buy; not bought late")
            if not dry:
                state[pid] = {"symbol": sym, "status": "missed", "entry_day": entry_day(by_id[pid]["recorded_at"], sessions)}
            continue
        if not is_open:
            print(f"{sym}: {action} is due but the market is closed; nothing sent")
            continue
        if action == "buy":
            acct = tc.get_account()
            dollars = size(float(acct.equity), float(acct.cash))
            asset = tc.get_asset(sym)
            if dollars < 50 or not asset.tradable:
                print(f"{sym}: not bought (cash for this slot ${dollars:,.0f}, tradable {asset.tradable})")
                if not dry:
                    state[pid] = {"symbol": sym, "status": "not bought", "entry_day": today}
                continue
            if not asset.fractionable:
                print(f"{sym}: no fractional shares on paper; not bought (kept in the ledger test)")
                if not dry:
                    state[pid] = {"symbol": sym, "status": "not bought", "entry_day": today}
                continue
            print(f"{sym}: BUY ${dollars:,.2f} on paper" + (" (dry run)" if dry else ""))
            if not dry:
                o = tc.submit_order(MarketOrderRequest(symbol=sym, notional=dollars, side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
                                                       client_order_id=f"facts-buy-{pid}"))
                state[pid] = {"symbol": sym, "status": "open", "entry_day": today, "dollars": dollars, "buy_order": str(o.id),
                              "buy_px": None, "buy_qty": None}
                save_state(state)
        else:
            st = state[pid]
            qty = st.get("buy_qty")
            if not qty:
                print(f"{sym}: sell is due but the buy fill is unknown; skipped")
                continue
            print(f"{sym}: SELL {qty} on paper after {HOLD} sessions" + (" (dry run)" if dry else ""))
            if not dry:
                o = tc.submit_order(MarketOrderRequest(symbol=sym, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY,
                                                       client_order_id=f"facts-sell-{pid}"))
                st.update(status="closed", exit_day=today, sell_order=str(o.id), sell_px=None)
                save_state(state)
    if not todo:
        print("nothing due today")
    if not dry:
        save_state(state)
    return 0


# ---------- the holdings check ----------
def rows_for(state: dict, bars: dict[str, dict], days: list[str]) -> list[dict]:
    """One row per position: return since the paper fill (or the entry open if the fill is unknown) against SPY over the same days."""
    out = []
    for st in state.values():
        if st.get("status") not in ("open", "closed"):
            out.append({"symbol": st["symbol"], "status": st.get("status", "?"), "entry_day": st.get("entry_day", "")})
            continue
        b, spy, e = bars.get(st["symbol"], {}), bars.get("SPY", {}), st["entry_day"]
        if e not in b or e not in spy:
            out.append({"symbol": st["symbol"], "status": st["status"], "entry_day": e, "note": "no price yet"})
            continue
        last = st.get("exit_day") if st["status"] == "closed" and st.get("exit_day") in b else max(d for d in days if d in b)
        px0 = st.get("buy_px") or b[e][0]
        px1 = (st.get("sell_px") or b[last][0]) if st["status"] == "closed" else b[last][1]
        m1 = spy[last][0] if st["status"] == "closed" else spy[last][1]
        r, m = px1 / px0 - 1, m1 / spy[e][0] - 1
        held = days.index(last) - days.index(e)
        out.append({"symbol": st["symbol"], "status": st["status"], "entry_day": e, "held": held, "ret": r, "spy": m,
                    "vs_spy": (1 + r) / (1 + m) - 1, "dollars": st.get("dollars", 0.0), "pnl": st.get("dollars", 0.0) * r})
    return out


def render(rows: list[dict], equity: float | None, asof: str) -> str:
    out = [f"Fact-sheet picks: PAPER positions as of {asof}. No real money. Each pick is held {HOLD} sessions, then sold.", ""]
    for label in ("open", "closed"):
        part = [r for r in rows if r["status"] == label and "ret" in r]
        if not part:
            continue
        out.append(f"{label.upper()} ({len(part)})")
        out.append(f"{'symbol':<7}{'bought':<12}{'days':>5}{'return':>9}{'S&P':>8}{'vs S&P':>8}{'paper $':>10}")
        for r in sorted(part, key=lambda r: (r["entry_day"], r["symbol"])):
            out.append(f"{r['symbol']:<7}{r['entry_day']:<12}{r['held']:>5}{P.pct(r['ret']):>9}{P.pct(r['spy']):>8}{P.pct(r['vs_spy']):>8}{r['pnl']:>+10.2f}")
        ahead = sum(r["vs_spy"] > 0 for r in part)
        out.append(f"  {ahead} of {len(part)} ahead of the S&P; average vs S&P {P.pct(sum(r['vs_spy'] for r in part) / len(part))}; "
                   f"paper profit ${sum(r['pnl'] for r in part):+,.2f}")
        out.append("")
    other = [r for r in rows if "ret" not in r]
    if other:
        out.append("NOT HELD: " + ", ".join(f"{r['symbol']} ({r.get('note') or r['status']})" for r in other))
    if not rows:
        out.append("no positions yet")
    if equity is not None:
        out.append(f"\nPaper account value: ${equity:,.2f}")
    return "\n".join(out)


def status() -> int:
    state = load_state()
    equity = None
    try:
        tc = client()
        refresh_fills(tc, state)
        save_state(state)
        equity = float(tc.get_account().equity)
    except BaseException as e:  # noqa: BLE001
        print(f"(broker not reachable: {e!s:.100})")
    held = sorted({st["symbol"] for st in state.values() if st.get("status") in ("open", "closed")})
    rows = []
    if held:
        start = min(datetime.fromisoformat(st["entry_day"]) for st in state.values() if st.get("entry_day")) - timedelta(days=10)
        bars = P.fetch([*held, "SPY"], start.replace(tzinfo=ET))
        rows = rows_for(state, bars, sorted(bars["SPY"]))
    else:
        rows = rows_for(state, {}, [])
    text = render(rows, equity, datetime.now(ET).strftime("%Y-%m-%d %H:%M ET"))
    A.HOME.mkdir(parents=True, exist_ok=True)
    (A.HOME / "POSITIONS.txt").write_text(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    cmd = (sys.argv[1:] or ["status"])[0]
    sys.exit(sync(dry="--dry" in sys.argv) if cmd == "sync" else status())
