"""Active US-equity asset list without alpaca-py's Asset model.

alpaca-py validates `exchange` against a fixed enum, so one new venue code (TXSE, the Texas Stock Exchange, from about
2026-10-20) failed the WHOLE list: the movers full-market scan, the overnight book, the tight screener, the catalyst
scan and the swing screener all lost their universe, silently in most places. This reads the raw /v2/assets rows
through the caller's own client (paper or live, its keys and base URL) and returns plain objects with the fields
those callers use (symbol, name, exchange as a plain string, tradable), so an unknown venue is just another string.
"""
from __future__ import annotations

from types import SimpleNamespace


def active_us_equities(tc) -> list[SimpleNamespace]:
    """Raw active US-equity assets via tc.get (RESTClient); raises whatever the request raises."""
    rows = tc.get("/assets", {"status": "active", "asset_class": "us_equity"}) or []
    return [SimpleNamespace(symbol=str(r.get("symbol") or ""), name=str(r.get("name") or ""),
                            exchange=str(r.get("exchange") or ""), tradable=bool(r.get("tradable")),
                            shortable=bool(r.get("shortable")), easy_to_borrow=bool(r.get("easy_to_borrow")))
            for r in rows if isinstance(r, dict)]
