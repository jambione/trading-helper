"""Stocktwits free trending poll for the momentum monitor.

Public JSON (no key required today):
  GET https://api.stocktwits.com/api/2/trending/symbols.json

Mirrors https://stocktwits.com/sentiment columns where possible:
  rank, symbol, last, %chg, volume, 52w high/low, market cap (+ score/watchers).

Live last / %chg / session volume come from Alpaca snapshots (free with your
keys). 52w + mkt cap come from Stocktwits fundamentals on the same payload.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional
from urllib.request import Request, urlopen

TRENDING_URL = "https://api.stocktwits.com/api/2/trending/symbols.json"
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36 "
    "BrasfieldMomentum/1.0"
)

ROOT = Path(__file__).resolve().parent.parent


def _f(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _load_env() -> None:
    env_path = ROOT / "signal_engine.env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().split("#")[0].strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def parse_trending_payload(data: dict) -> list[dict[str, Any]]:
    """Normalize API payload → list of equity/crypto rows with rank + fundies."""
    out: list[dict[str, Any]] = []
    for s in data.get("symbols") or []:
        if not isinstance(s, dict):
            continue
        sym = str(s.get("symbol") or s.get("symbol_display") or "").upper().strip()
        if not sym:
            continue
        iclass = str(s.get("instrument_class") or "").lower()
        fund = s.get("fundamentals") if isinstance(s.get("fundamentals"), dict) else {}
        # ST MarketCapitalization is typically in $millions (e.g. 6238 → $6.24B)
        mcap_m = _f(fund.get("MarketCapitalization") or fund.get("MarketCap"))
        mcap = mcap_m * 1_000_000.0 if mcap_m is not None else None
        out.append({
            "symbol": sym,
            "rank": int(s.get("rank") or 0) or None,
            "title": s.get("title") or "",
            "trending_score": _f(s.get("trending_score")),
            "watchlist_count": int(s.get("watchlist_count") or 0),
            "instrument_class": iclass or "stock",
            # Common stocks only (tradeable equities). Drop ADRs / depositary
            # receipts (e.g. NOK), ETFs, crypto, commodities, etc.
            "is_equity": iclass in ("", "stock"),
            "is_crypto": (
                iclass in ("cryptocurrency", "crypto")
                or sym.endswith(".X")
            ),
            "market_cap": mcap,
            "high_52w": _f(fund.get("HighPriceLast52Weeks")),
            "low_52w": _f(fund.get("LowPriceLast52Weeks")),
            "avg_vol": _f(fund.get("AverageDailyVolumeLastMonth")
                          or fund.get("AverageDailyVolumeLast3Months")),
            "exchange": s.get("exchange") or "",
            # filled by Alpaca enrich
            "price": None,
            "pct_change": None,
            "volume": None,
        })
    out.sort(key=lambda r: (r.get("rank") is None, r.get("rank") or 999))
    return out


def fetch_trending(timeout: float = 10.0) -> list[dict[str, Any]]:
    """HTTP GET trending symbols. Empty list on any failure."""
    try:
        req = Request(
            TRENDING_URL,
            headers={"Accept": "application/json", "User-Agent": _UA},
        )
        with urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        if not isinstance(data, dict):
            return []
        return parse_trending_payload(data)
    except Exception:
        return []


def enrich_with_alpaca(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach last, %chg (vs prev close), session volume via Alpaca snapshots."""
    if not rows:
        return rows
    _load_env()
    api = os.getenv("ALPACA_API_KEY", "")
    sec = os.getenv("ALPACA_SECRET_KEY", "")
    if not api or not sec:
        return rows
    syms = [r["symbol"] for r in rows if r.get("symbol") and not r.get("is_crypto")]
    if not syms:
        return rows
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockSnapshotRequest
        from alpaca.data.enums import DataFeed

        client = StockHistoricalDataClient(api, sec)
        # batch — Alpaca accepts list
        snaps = client.get_stock_snapshot(
            StockSnapshotRequest(symbol_or_symbols=syms, feed=DataFeed.IEX)
        )
    except Exception:
        return rows

    by = {r["symbol"]: r for r in rows}
    # snaps may be dict-like
    items = snaps.items() if hasattr(snaps, "items") else []
    for sym, snap in items:
        row = by.get(str(sym).upper())
        if not row:
            continue
        try:
            daily = getattr(snap, "daily_bar", None)
            prev = getattr(snap, "previous_daily_bar", None)
            latest = getattr(snap, "latest_trade", None)
            px = None
            if latest is not None and getattr(latest, "price", None):
                px = float(latest.price)
            elif daily is not None and getattr(daily, "close", None):
                px = float(daily.close)
            row["price"] = px
            if daily is not None:
                vol = getattr(daily, "volume", None)
                row["volume"] = float(vol) if vol is not None else None
            if px and prev is not None and getattr(prev, "close", None):
                prev_c = float(prev.close)
                if prev_c > 0:
                    row["pct_change"] = (px - prev_c) / prev_c * 100.0
        except Exception:
            continue
    return rows


def fmt_mcap(v: Optional[float]) -> str:
    if v is None or v <= 0:
        return "—"
    if v >= 1e12:
        return f"${v / 1e12:.2f}T"
    if v >= 1e9:
        return f"${v / 1e9:.2f}B"
    if v >= 1e6:
        return f"${v / 1e6:.1f}M"
    return f"${v:,.0f}"


def fmt_vol(v: Optional[float]) -> str:
    if v is None:
        return "—"
    if v >= 1e9:
        return f"{v / 1e9:.2f}B"
    if v >= 1e6:
        return f"{v / 1e6:.2f}M"
    if v >= 1e3:
        return f"{v / 1e3:.1f}K"
    return f"{v:.0f}"


class StocktwitsTrending:
    """Throttled cache of Stocktwits trending (default poll ~60s)."""

    def __init__(self, poll_interval: float = 60.0, stocks_only: bool = True,
                 max_price: Optional[float] = 30.0,
                 enrich_quotes: bool = True):
        self.poll_interval = max(15.0, float(poll_interval))
        self.stocks_only = bool(stocks_only)
        self.max_price = float(max_price) if max_price is not None else None
        self.enrich_quotes = bool(enrich_quotes)
        self.rows: list[dict[str, Any]] = []
        self.by_symbol: dict[str, dict[str, Any]] = {}
        self.last_ok: float = 0.0
        self.last_attempt: float = 0.0
        self.error: str = ""

    def refresh(self, now: Optional[float] = None) -> bool:
        now = now if now is not None else time.time()
        if self.last_attempt and (now - self.last_attempt) < self.poll_interval:
            return False
        self.last_attempt = now
        rows = fetch_trending()
        if not rows:
            self.error = "fetch failed or empty"
            return False
        if self.stocks_only:
            rows = [r for r in rows if r.get("is_equity") and not r.get("is_crypto")]
        if self.enrich_quotes:
            rows = enrich_with_alpaca(rows)
        self.rows = rows
        self.by_symbol = {r["symbol"]: r for r in rows}
        self.last_ok = now
        self.error = ""
        return True

    def rank_of(self, symbol: str) -> Optional[int]:
        r = self.by_symbol.get((symbol or "").upper())
        if not r:
            return None
        return r.get("rank")

    def display_rows(self, price_by_sym: Optional[dict[str, Optional[float]]] = None,
                     limit: int = 12) -> list[dict[str, Any]]:
        """
        Rows for the monitor panel.

        Prefer Alpaca-enriched price on the row; fall back to price_by_sym
        from the momentum feed. Apply max_price when a price is known.
        """
        price_by_sym = price_by_sym or {}
        out = []
        for r in self.rows:
            sym = r["symbol"]
            px = r.get("price")
            if px is None and sym in price_by_sym:
                px = price_by_sym.get(sym)
            if px is not None and self.max_price is not None and px >= self.max_price:
                continue
            row = {**r, "price": px, "price_known": px is not None}
            out.append(row)
            if len(out) >= limit:
                break
        return out
