"""tape_check.py — would a paper passive fill have filled live? Check it against the real SIP tape.

Alpaca paper fills a resting limit when the quote reaches it; it does not model queue position. Live, an order at
the limit price waits behind everything posted there first, so a touch is not a fill. A passive fill is
tape-confirmed only if a regular SIP trade printed THROUGH the limit while the order rested (below a buy limit,
above a sell limit): then size traded at a better price than ours and our order would have been reached. A print
exactly at the limit is a "touch" (maybe filled, maybe not); no print at or through it means paper-only.

For a fill that is not confirmed, the live-equivalent price is what the arm does when its limit does not fill:
cross at market when the wait runs out (buy at the SIP ask, sell at the SIP bid at that moment). Scorers report
the paper result and this tape-honest result side by side. Added 2026-10-03 (user: can paper data stand for live?).

Review 2026-10-03: odd lots (condition I) are excluded — they are not protected quotes and can print through the
NBBO without reaching the round-lot queue — and confirmation needs the through-prints' total size to cover our
quantity, so one 5-share print cannot confirm a 100-share fill. The window is the whole time a live order would
have rested (submit to submit + the arm's cross time), not the shorter time paper took to fill.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# Trade conditions that are not regular-session prints at the NBBO (as in the 1-cent stop study), plus odd lots.
BAD_CONDITIONS = set("TUZBGW479CHMNPQRVI")


def regular_prints(cl, sym: str, t0: float, t1: float) -> list[tuple[float, float, float]]:
    """[(epoch, price, size)] of regular round-lot SIP trades in [t0, t1]."""
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
        out.append((r.timestamp.timestamp(), float(r.price), float(getattr(r, "size", 0) or 0)))
    return out


def tape_verdict(side: str, limit: float, prints, qty: float | None = None) -> str:
    """"confirmed" (prints through the limit totalling >= qty shares), "touch" (any print at or through it,
    but not enough size) or "none". prints are (epoch, price[, size]); a missing size counts as 0."""
    lim = float(limit)
    need = float(qty) if qty and qty > 0 else 100.0
    through, at = 0.0, False
    for p in prints:
        px = float(p[1])
        sz = float(p[2]) if len(p) > 2 else 0.0
        beyond = px < lim - 1e-9 if side == "buy" else px > lim + 1e-9
        if beyond:
            through += sz
            at = True
        elif abs(px - lim) <= 1e-9:
            at = True
    if through >= need:
        return "confirmed"
    return "touch" if at else "none"


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
