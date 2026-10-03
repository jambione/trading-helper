"""tape_check.py — would a paper passive fill have filled live? Check it against the real SIP tape.

Alpaca paper fills a resting limit when the quote reaches it; it does not model queue position. Live, an order at
the limit price waits behind everything posted there first, so a touch is not a fill. A passive fill is
tape-confirmed only if a regular SIP trade printed THROUGH the limit while the order rested (below a buy limit,
above a sell limit): then size traded at a better price than ours and our order would have been reached. A print
exactly at the limit is a "touch" (maybe filled, maybe not); no print at or through it means paper-only.

For a fill that is not confirmed, the live-equivalent price is what the arm does when its limit does not fill:
cross at market when the wait runs out (buy at the SIP ask, sell at the SIP bid at that moment). Scorers report
the paper result and this tape-honest result side by side. Added 2026-10-03 (user: can paper data stand for live?).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# Trade conditions that are not regular-session prints at the NBBO (as in the 1-cent stop study).
BAD_CONDITIONS = set("TUZBGW479CHMNPQRV")


def regular_prints(cl, sym: str, t0: float, t1: float) -> list[tuple[float, float]]:
    """[(epoch, price)] of regular SIP trades in [t0, t1]."""
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockTradesRequest
    q = cl.get_stock_trades(StockTradesRequest(
        symbol_or_symbols=sym, start=datetime.fromtimestamp(t0, timezone.utc),
        end=datetime.fromtimestamp(t1, timezone.utc) + timedelta(milliseconds=1),
        feed=DataFeed.SIP, limit=10000))
    out = []
    for r in q.data.get(sym) or []:
        if set(getattr(r, "conditions", None) or []) & BAD_CONDITIONS:
            continue
        out.append((r.timestamp.timestamp(), float(r.price)))
    return out


def tape_verdict(side: str, limit: float, prints: list[tuple[float, float]]) -> str:
    """"confirmed" (a print through the limit), "touch" (only at it) or "none"."""
    lim = float(limit)
    if side == "buy":
        if any(p < lim - 1e-9 for _, p in prints):
            return "confirmed"
        if any(abs(p - lim) <= 1e-9 for _, p in prints):
            return "touch"
    else:
        if any(p > lim + 1e-9 for _, p in prints):
            return "confirmed"
        if any(abs(p - lim) <= 1e-9 for _, p in prints):
            return "touch"
    return "none"


def honest_price(side: str, verdict: str, paper_px: float, cross_quote) -> float | None:
    """Live-equivalent fill: the paper price if tape-confirmed, else crossing at the deadline quote.

    A touch counts as not filled: live, the order may have been behind the queue at that price. cross_quote is
    (bid, ask) at the moment the arm would have crossed; None when it is unknown.
    """
    if verdict == "confirmed":
        return float(paper_px)
    if not cross_quote:
        return None
    bid, ask = cross_quote
    return float(ask) if side == "buy" else float(bid)
