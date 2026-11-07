#!/usr/bin/env python3
"""Broker truth report (operator 11/07): the scoreboard neither Claude nor Argon produces.

Reads ONLY Alpaca's own records for the paper account: FILL activities (every execution) and portfolio history (daily equity).
Pairs buys and sells per symbol first-in-first-out into round trips, then reports per ISO week: round trips, win rate, average
win and loss in bp, net bp per round trip, realized dollars, and Alpaca's own equity change for the same week as a cross-check.
No desk logs, no simulation, no cost assumption: fills already carry the spread paid. Read-only on the account.

    .venv/bin/python tools/broker_truth.py fetch     # mini (desk keys): caches fills + equity under ai_reports/broker_truth/
    .venv/bin/python tools/broker_truth.py report    # -> docs/BROKER_TRUTH.md
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from collections import defaultdict, deque
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
ET = ZoneInfo("America/New_York")
CACHE = ROOT / "ai_reports" / "broker_truth"
FILLS = CACHE / "fills.jsonl"
EQUITY = CACHE / "equity.json"
START = "2026-08-01"


# ---------------- pure pieces ----------------
def round_trips(fills):
    """fills: [{symbol, side ('buy'/'sell'), qty, price, t}] in time order -> round trips, FIFO per symbol, long side.
    A round trip closes when the open quantity returns to zero; partial fills are volume-weighted. Sells with no open
    long are counted in `unmatched` (shorts or fills before the cache start), never invented."""
    book = defaultdict(deque)          # symbol -> deque of [qty, price, t]
    open_rt = {}                       # symbol -> {qty_in, cost_in, t_in, qty_out, cash_out, t_out}
    out, unmatched = [], 0.0
    for f in sorted(fills, key=lambda x: x["t"]):
        s, q, px = f["symbol"], float(f["qty"]), float(f["price"])
        if f["side"] == "buy":
            book[s].append([q, px, f["t"]])
            rt = open_rt.setdefault(s, {"qty_in": 0.0, "cost_in": 0.0, "t_in": f["t"], "qty_out": 0.0, "cash_out": 0.0, "t_out": None})
            rt["qty_in"] += q
            rt["cost_in"] += q * px
            continue
        left = q
        while left > 1e-9 and book[s]:
            lot = book[s][0]
            take = min(left, lot[0])
            lot[0] -= take
            left -= take
            rt = open_rt[s]
            rt["qty_out"] += take
            rt["cash_out"] += take * px
            rt["t_out"] = f["t"]
            if lot[0] <= 1e-9:
                book[s].popleft()
            if not book[s]:
                e = rt["cost_in"] / rt["qty_in"]
                x = rt["cash_out"] / rt["qty_out"]
                out.append({"symbol": s, "qty": rt["qty_out"], "entry": e, "exit": x, "t_in": rt["t_in"], "t_out": rt["t_out"],
                            "pnl": rt["cash_out"] - rt["cost_in"], "bp": (x / e - 1) * 1e4})
                del open_rt[s]
        unmatched += left
    still_open = {s: sum(l[0] for l in b) for s, b in book.items() if b}
    return out, unmatched, still_open


def round_trips_by_day(fills):
    """Pair within each ET trading day (the desk is intraday: 0 overnight trips). An orphan - a buy lot left open at the day's end, or a sell
    with no open buy (from a mid-stream paper-account reset) - is REPORTED with its date and never carried forward, so it cannot swallow the
    next days' round trips for that symbol (11/07 audit: 6 orphaned lots had swallowed 22 trips and moved the total by $18.86).
    -> (round_trips, [(day, symbol, qty, kind)] orphans)."""
    by = defaultdict(list)
    for f in fills:
        by[iso_day(f["t"])].append(f)
    out, orphans = [], []
    for day in sorted(by):
        rts, unmatched_total, still_open = round_trips(by[day])
        out += rts
        for sym, q in still_open.items():
            orphans.append((day, sym, q, "open buy at day end"))
        if unmatched_total > 1e-9:
            orphans.append((day, "*", unmatched_total, "sell shares with no open buy (shares)"))
    return out, orphans


def iso_week(t):
    d = dt.datetime.fromtimestamp(t, ET).date()
    y, w, _ = d.isocalendar()
    monday = d - dt.timedelta(days=d.weekday())
    return f"{y}-W{w:02d}", monday.isoformat()


def weekly(rts):
    """-> [(week, monday, stats)] sorted by week. bp stats are per round trip; dollars are summed."""
    g = defaultdict(list)
    for r in rts:
        g[iso_week(r["t_out"])].append(r)
    rows = []
    for (wk, mon), rs in sorted(g.items()):
        wins = [r["bp"] for r in rs if r["pnl"] > 0]
        losses = [r["bp"] for r in rs if r["pnl"] <= 0]
        rows.append((wk, mon, {"n": len(rs), "win": len(wins) / len(rs), "avg_win": sum(wins) / len(wins) if wins else 0.0,
                               "avg_loss": sum(losses) / len(losses) if losses else 0.0, "bp": sum(r["bp"] for r in rs) / len(rs),
                               "pnl": sum(r["pnl"] for r in rs), "overnight": sum(1 for r in rs if iso_day(r["t_in"]) != iso_day(r["t_out"]))}))
    return rows


def iso_day(t):
    return dt.datetime.fromtimestamp(t, ET).strftime("%Y-%m-%d")


# ---------------- broker I/O (mini only) ----------------
def _client():
    from alpaca.trading.client import TradingClient
    from config import load_config
    cfg = load_config() or {}
    return TradingClient(cfg.get("api_key"), cfg.get("secret_key"), paper=True)


def fetch():
    tc = _client()
    CACHE.mkdir(parents=True, exist_ok=True)
    out, token, n = [], None, 0
    while True:
        params = {"after": START + "T00:00:00Z", "page_size": 100, "direction": "asc"}
        if token:
            params["page_token"] = token
        rows = tc.get("/account/activities/FILL", params) or []
        for a in rows:
            ts = dt.datetime.fromisoformat(str(a["transaction_time"]).replace("Z", "+00:00")).timestamp()
            out.append({"id": a.get("id"), "order_id": a.get("order_id"), "symbol": str(a["symbol"]), "side": str(a["side"]).lower(),
                        "qty": float(a["qty"]), "price": float(a["price"]), "t": ts})
        n += 1
        if len(rows) < 100:
            break
        token = rows[-1]["id"]
    with open(FILLS, "w") as f:
        for r in out:
            f.write(json.dumps(r) + "\n")
    hist = tc.get("/account/portfolio/history", {"period": "all", "timeframe": "1D"}) or {}
    EQUITY.write_text(json.dumps(hist))
    print(f"fills {len(out)} ({n} pages); equity points {len(hist.get('equity') or [])}")
    return 0


def archive_fills():
    """Broker order fills the desk saved each day (ai_reports/fills/DAY.jsonl, event=fill, status=filled): order-level
    filled_qty and filled_avg_price as Alpaca reported them at the time. Covers the earlier paper account(s)."""
    out = []
    for f in sorted((ROOT / "ai_reports" / "fills").glob("20??-??-??.jsonl")):
        for line in f.read_text().splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("event") != "fill" or r.get("status") != "filled" or not r.get("filled_at") or not r.get("filled_avg_price"):
                continue
            try:
                ts = dt.datetime.fromisoformat(str(r["filled_at"]).replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
            out.append({"order_id": r.get("order_id"), "symbol": str(r["symbol"]), "side": str(r.get("side") or "").lower(),
                        "qty": float(r.get("filled_qty") or r.get("qty") or 0), "price": float(r["filled_avg_price"]), "t": ts})
    return out


def cross_check(alpaca, archive):
    """Order-level agreement on the days both sources cover. -> dict of counts and worst differences."""
    days = {iso_day(f["t"]) for f in alpaca}
    agg = {}
    for f in alpaca:
        k = f.get("order_id")
        a = agg.setdefault(k, {"qty": 0.0, "cash": 0.0, "symbol": f["symbol"], "side": f["side"]})
        a["qty"] += f["qty"]
        a["cash"] += f["qty"] * f["price"]
    arch = {f["order_id"]: f for f in archive if iso_day(f["t"]) in days}
    both = [k for k in agg if k in arch]
    qty_bad = [k for k in both if abs(agg[k]["qty"] - arch[k]["qty"]) > 1e-6]
    px_diff = [abs(agg[k]["cash"] / agg[k]["qty"] - arch[k]["price"]) / arch[k]["price"] * 1e4 for k in both if agg[k]["qty"] > 0]
    return {"days": sorted(days), "alpaca_orders": len(agg), "archive_orders": len(arch), "matched": len(both),
            "only_alpaca": len([k for k in agg if k not in arch]), "only_archive": len([k for k in arch if k not in agg]),
            "qty_mismatch": len(qty_bad), "max_px_diff_bp": max(px_diff) if px_diff else None}


def equity_by_week():
    if not EQUITY.exists():
        return {}
    h = json.loads(EQUITY.read_text())
    pts = [(t, e) for t, e in zip(h.get("timestamp") or [], h.get("equity") or []) if e is not None]
    last = {}
    for t, e in pts:
        last[iso_week(t)] = float(e)       # equity at the last daily point of each week
    return last


def report():
    fills = [json.loads(l) for l in open(FILLS)] if FILLS.exists() else []
    if not fills:
        print("no fills cached: run 'fetch' on the mini first")
        return 1
    sells_short = sum(1 for f in fills if "short" in f["side"])
    fl = [{**f, "side": "sell" if f["side"].startswith("sell") else "buy"} for f in fills]
    rts, orphans = round_trips_by_day(fl)
    unmatched = sum(o[2] for o in orphans if o[1] == "*")
    still_open = {o[1]: o[2] for o in orphans if o[1] != "*"}
    wk = weekly(rts)
    eq = equity_by_week()
    keys = sorted(eq)
    lines = [f"# Broker truth (Alpaca paper account), written {dt.datetime.now(ET):%Y-%m-%d %H:%M} ET", "",
             "Source: Alpaca FILL activities and Alpaca portfolio history only. No desk logs, no simulation, no cost assumption",
             "(fills already include the spread). Round trips = buys and sells paired first-in-first-out per symbol.", "",
             f"Fills {len(fills)} (short-sale fills {sells_short}); round trips {len(rts)}; sell quantity with no open buy {unmatched:.0f} shares; "
             f"positions still open {len(still_open)}.", "",
             "| week (Mon) | round trips | win rate | avg win bp | avg loss bp | net bp / trip | realized $ | cumulative $ | Alpaca equity, week end | equity change | overnight trips |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    cum, prev_eq = 0.0, None
    for w, mon, s in wk:
        cum += s["pnl"]
        e = eq.get((w, mon))
        if e is None:
            cands = [k for k in keys if k[0] <= w]
            e = eq[cands[-1]] if cands else None
        chg = f"{e - prev_eq:+,.2f}" if (e is not None and prev_eq is not None) else ""
        lines.append(f"| {mon} | {s['n']} | {100 * s['win']:.0f}% | {s['avg_win']:+.1f} | {s['avg_loss']:+.1f} | {s['bp']:+.1f} | "
                     f"{s['pnl']:+,.2f} | {cum:+,.2f} | {'' if e is None else f'{e:,.2f}'} | {chg} | {s['overnight']} |")
        prev_eq = e if e is not None else prev_eq
    arch = archive_fills()
    if arch:
        cc = cross_check(fills, arch)
        lines += ["", "## Earlier weeks: broker fills archived by the desk (the account above was created 2026-11-04)", "",
                  f"Validation on the days both sources cover ({', '.join(cc['days'])}): Alpaca orders {cc['alpaca_orders']}, archived orders "
                  f"{cc['archive_orders']}, matched {cc['matched']}, only in Alpaca {cc['only_alpaca']}, only in archive {cc['only_archive']}, "
                  f"quantity mismatches {cc['qty_mismatch']}, worst average-price difference "
                  f"{'n/a' if cc['max_px_diff_bp'] is None else f'{cc["max_px_diff_bp"]:.2f} bp'}. If these agree, the archive below is trustworthy.", ""]
        art, aorph = round_trips_by_day([{**f, "side": "sell" if f["side"].startswith("sell") else "buy"} for f in arch])
        aun = sum(o[2] for o in aorph if o[1] == "*")
        aop = {(o[0], o[1]): o[2] for o in aorph if o[1] != "*"}
        lines += [f"Archived fills {len(arch)} orders, {iso_day(min(f['t'] for f in arch))} to {iso_day(max(f['t'] for f in arch))}; round trips {len(art)}; "
                  f"sell quantity with no open buy {aun:.0f} shares; open-buy orphans at a day end {len(aop)}. Pairing resets each trading day (intraday desk); orphans are listed, never carried forward.", "",
                  "| week (Mon) | round trips | win rate | avg win bp | avg loss bp | net bp / trip | realized $ | cumulative $ | overnight trips |",
                  "|---|---|---|---|---|---|---|---|---|"]
        cum = 0.0
        for w, mon, st in weekly(art):
            cum += st["pnl"]
            lines.append(f"| {mon} | {st['n']} | {100 * st['win']:.0f}% | {st['avg_win']:+.1f} | {st['avg_loss']:+.1f} | {st['bp']:+.1f} | "
                         f"{st['pnl']:+,.2f} | {cum:+,.2f} | {st['overnight']} |")
    if arch and aorph:
        lines += ["", "Orphans in the archive (mid-stream paper-account resets or partial lots): " + "; ".join(f"{d} {sym} {q:g} ({k})" for d, sym, q, k in aorph[:30])]
    lines += ["", "Cross-check: realized $ per week should track the equity change, apart from open positions at week end,",
              "deposits/resets and fees. A large gap in a week means one of the two sources is wrong for that week; look before trusting it."]
    out = "\n".join(lines) + "\n"
    (ROOT / "docs" / "BROKER_TRUTH.md").write_text(out)
    print(out)
    return 0


def main(argv=None):
    a = (argv if argv is not None else sys.argv[1:]) or ["report"]
    return fetch() if a[0] == "fetch" else report()


if __name__ == "__main__":
    raise SystemExit(main())
