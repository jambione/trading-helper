"""Observe-mode lab overlay — ghosts, not orders.

Built for the dashboard while ``desk_product=observe``. Nothing here places
a trade. It marks:

  * how expensive the book is (spread vs the 0.10% H4 cap)
  * a **ghost H4**: if we had bought today's open with a 2% stop, where
    last sits vs that stop and vs SPY
  * how many watch rows the product vetoed

See docs/PROFIT_REDESIGN.md.
"""
from __future__ import annotations

from typing import Any

import desk_h4
import desk_product as dp

GHOST_CAP = 8


def _f(v: Any) -> float | None:
    try:
        if v is None or v == "":
            return None
        x = float(v)
        return x if x == x else None  # NaN
    except (TypeError, ValueError):
        return None


def spread_pct(bid: float | None, ask: float | None) -> float | None:
    """(ask-bid)/mid as percent (0.10 = 10 bps of mid)."""
    b, a = _f(bid), _f(ask)
    if b is None or a is None or b <= 0 or a < b:
        return None
    mid = (a + b) / 2.0
    if mid <= 0:
        return None
    return 100.0 * (a - b) / mid


def classify_spread(pct: float | None, cap: float) -> str:
    if pct is None:
        return "unknown"
    if cap > 0 and pct <= cap:
        return "cheap"
    return "wide"


def ghost_row(
    symbol: str,
    *,
    last: float | None,
    day_open: float | None,
    stop_pct: float,
    spy_chg_pct: float | None = None,
    spread_pct_v: float | None = None,
    spread_class: str = "unknown",
) -> dict[str, Any] | None:
    """Mark-to-market a 2% stop from today's open. Not a fill."""
    o, px = _f(day_open), _f(last)
    if not symbol or o is None or o <= 0 or px is None or px <= 0:
        return None
    stop = o * (1.0 - max(0.0, float(stop_pct)) / 100.0)
    pnl_pct = 100.0 * (px - o) / o
    through = px <= stop + 1e-9
    vs_spy = None
    if spy_chg_pct is not None:
        vs_spy = round(pnl_pct - float(spy_chg_pct), 3)
    return {
        "symbol": str(symbol).upper(),
        "last": round(px, 4),
        "day_open": round(o, 4),
        "stop": round(stop, 4),
        "pnl_pct": round(pnl_pct, 3),
        "vs_spy": vs_spy,
        "through_stop": through,
        "spread_pct": None if spread_pct_v is None else round(spread_pct_v, 4),
        "spread_class": spread_class,
    }


def build_lab(
    cfg: dict | None,
    tickers: list[dict] | None,
    book: dict | None = None,
) -> dict[str, Any]:
    cfg = cfg if isinstance(cfg, dict) else {}
    product = dp.product(cfg)
    stop_pct = desk_h4._f(cfg.get("h4_stop_pct"), desk_h4.DEFAULT_STOP_PCT)
    cap = desk_h4._f(cfg.get("h4_max_spread_pct"), desk_h4.DEFAULT_MAX_SPREAD_PCT)
    min_px = desk_h4._f(cfg.get("h4_min_price"), desk_h4.DEFAULT_MIN_PRICE)

    spy_chg = None
    by_sym: dict[str, dict] = {}
    for t in tickers or []:
        if not isinstance(t, dict):
            continue
        sym = str(t.get("ticker") or t.get("symbol") or "").upper()
        if not sym:
            continue
        by_sym[sym] = t
        if sym == "SPY":
            spy_chg = _f(t.get("pct_change"))

    cheap: list[dict] = []
    wide: list[dict] = []
    ghosts: list[dict] = []
    for sym, t in by_sym.items():
        if sym in {"SPY", "IWM", "QQQ"}:
            continue
        last = _f(t.get("price") or t.get("last"))
        bid, ask = _f(t.get("bid")), _f(t.get("ask"))
        sp = spread_pct(bid, ask)
        klass = classify_spread(sp, cap)
        if last is not None and last >= min_px:
            card = {
                "symbol": sym,
                "last": last,
                "spread_pct": None if sp is None else round(sp, 4),
                "spread_class": klass,
            }
            if klass == "cheap":
                cheap.append(card)
            elif klass == "wide":
                wide.append(card)
            g = ghost_row(
                sym,
                last=last,
                day_open=_f(t.get("day_open")),
                stop_pct=stop_pct,
                spy_chg_pct=spy_chg,
                spread_pct_v=sp,
                spread_class=klass,
            )
            if g:
                ghosts.append(g)

    cheap.sort(key=lambda r: (r.get("spread_pct") is None, r.get("spread_pct") or 99))
    wide.sort(key=lambda r: -(r.get("spread_pct") or 0))
    # Interesting first: still alive, then cheapest book, then biggest |move|.
    ghosts.sort(key=lambda r: (
        r["through_stop"],
        0 if r["spread_class"] == "cheap" else 1,
        -abs(r["pnl_pct"]),
    ))
    ghosts = ghosts[:GHOST_CAP]

    refused = 0
    watch_n = 0
    b = book if isinstance(book, dict) else {}
    rows = b.get("entry_book") or b.get("entry_watch") or []
    if isinstance(rows, list):
        for w in rows:
            if not isinstance(w, dict):
                continue
            watch_n += 1
            why = str(w.get("block_code") or w.get("arm_why") or w.get("blocker") or "")
            if why == dp.REASON_OBSERVE or "desk_observe" in why:
                refused += 1

    alive = sum(1 for g in ghosts if not g["through_stop"])
    stopped = sum(1 for g in ghosts if g["through_stop"])
    headline = (
        f"{product} · ghost H4 (sim from today's open, 2% stop) · "
        f"{alive} alive / {stopped} through stop · "
        f"{len(cheap)} cheap books / {len(wide)} wide"
    )
    if product == dp.OBSERVE:
        headline = "OBSERVE — not a trade. " + headline
    return {
        "product": product,
        "headline": headline,
        "refused": refused,
        "watch_n": watch_n,
        "cheap_n": len(cheap),
        "wide_n": len(wide),
        "cheap": cheap[:6],
        "wide": wide[:4],
        "ghosts": ghosts,
        "spy_chg_pct": spy_chg,
        "h4_stop_pct": stop_pct,
        "h4_max_spread_pct": cap,
        "note": "Ghost P&L is mark-to-open, not a fill. No orders.",
    }
