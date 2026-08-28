#!/usr/bin/env python3
"""Server-side Alpaca movers poll — a price-and-volume seed source.

Publishes movers_stocks.json, which ai_entry_watch's movers seed reads on
every poll. Same producer/consumer shape as trending_screener.py: this
process owns the network calls and the file; the book just reads whatever
snapshot is on disk.

WHY THIS SOURCE
Every existing seed is sentiment-driven — Stocktwits heat, Discord mentions,
a research thesis. This one is not: it ranks by what actually moved and what
actually traded. Different failure modes, so its admissions are tagged
`movers` and stay separately measurable from the first session.

WHAT IT CANNOT DO
Finnhub has no universe screener (its /scan/* endpoints are single-symbol
technical scans and 403 on this key anyway), so the day-change ranking has
to come from Alpaca. That ranking is raw, and two things about it will ruin
a watchlist if they are not handled here:

  Warrants own the top of it. On 2026-08-28 the leading gainers were MIACW
  +240%, GFAIW +140%, SAIHW +97% — sub-dollar warrants the desk cannot
  trade. Eight of the top fifty. A price band alone does not remove them
  (some print above $2), so symbols are filtered by shape as well.

  RVOL divides by a dormant average. QNRX showed 1281x against a 20-day mean
  of almost nothing — arithmetically true, meaningless as a ratio, and it
  would top any RVOL sort. ai_movers_min_avg_vol floors the DENOMINATOR;
  under it, rvol is None rather than enormous, because an unknown ratio is
  not a big one.

FEEDS
RVOL numerator and denominator are both IEX daily bars — the same rule
stocktwits_trending._feed_arg states, for the same reason. A ratio built
from two feeds is not a ratio, and the desk's other RVOL readings are IEX,
so a movers row must be comparable to them and to ai_watch_min_rvol.

    python3 movers_screener.py
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import desk_core  # noqa: E402

_loaded = desk_core.load_desk_env(ROOT / "signal_engine.env")
if _loaded:
    print(f"[ENV] Loaded {len(_loaded)} setting(s) from signal_engine.env",
          flush=True)

from config import load_config  # noqa: E402

MOVERS_FILE = ROOT / "movers_stocks.json"
ET = ZoneInfo("America/New_York")

_write_json = desk_core.write_json_atomic


def _keys() -> tuple[str, str]:
    import os
    return os.getenv("ALPACA_API_KEY", ""), os.getenv("ALPACA_SECRET_KEY", "")


def is_common(sym: str) -> bool:
    """True for ordinary common stock.

    A five-letter symbol ending W/U/R/Q is a warrant, unit, right or a name
    in bankruptcy proceedings. They dominate a percent-change ranking because
    they are cheap and thin, and none of them is tradeable here.
    """
    s = str(sym or "").upper()
    if not s.isalpha() or not s or len(s) > 5:
        return False
    return not (len(s) == 5 and s[-1] in "WURQ")


def _et_now() -> datetime:
    return datetime.now(timezone.utc).astimezone(ET)


def _active_hours(now: datetime | None = None) -> bool:
    """Premarket through the close of post. Outside it the movers list is a
    frozen copy of the last session and polling it fast buys nothing."""
    now = now or _et_now()
    if now.weekday() >= 5:
        return False
    return 4 <= now.hour < 20


def fetch_rows(cfg: dict) -> list[dict]:
    """One pass: rank movers, drop what cannot be traded, enrich survivors."""
    api, sec = _keys()
    if not api or not sec:
        return []
    from alpaca.data.historical.screener import ScreenerClient
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import MarketMoversRequest, StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    from alpaca.data.enums import DataFeed

    top = int(cfg.get("ai_movers_top", 50) or 50)
    min_pct = float(cfg.get("ai_movers_min_pct_change", 10.0) or 10.0)
    lo = float(cfg.get("ai_movers_min_price", 2.0) or 2.0)
    hi = float(cfg.get("ai_movers_max_price", 20.0) or 20.0)
    min_avg_vol = float(cfg.get("ai_movers_min_avg_vol", 100_000) or 0)
    want = int(cfg.get("ai_movers_max_rows", 25) or 25)

    scr = ScreenerClient(api, sec)
    mv = scr.get_market_movers(MarketMoversRequest(top=top))
    gainers = getattr(mv, "gainers", None) or []

    cand = []
    for g in gainers:
        sym = str(getattr(g, "symbol", "") or "").upper()
        try:
            pct = float(getattr(g, "percent_change", 0) or 0)
            px = float(getattr(g, "price", 0) or 0)
        except (TypeError, ValueError):
            continue
        if pct < min_pct or not is_common(sym) or not (lo <= px <= hi):
            continue
        cand.append((sym, pct, px))
    if not cand:
        return []

    syms = [s for s, _, _ in cand][:want]
    data = StockHistoricalDataClient(api, sec)
    start = datetime.now(timezone.utc) - timedelta(days=45)
    bars: dict = {}
    try:
        df = data.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=syms, timeframe=TimeFrame(1, TimeFrameUnit.Day),
            start=start, limit=10000, feed=DataFeed.IEX))
        bars = getattr(df, "data", {}) or {}
    except Exception as e:  # noqa: BLE001
        print(f"[movers] daily bars failed: {e}", flush=True)

    try:
        import float_feed
        float_feed.refresh(syms, limit=len(syms), pace_sec=1.1)
    except Exception:  # noqa: BLE001
        pass

    rows = []
    for sym, pct, px in cand[:want]:
        seq = bars.get(sym) or []
        vol = float(getattr(seq[-1], "volume", 0) or 0) if seq else 0.0
        prior = [float(getattr(b, "volume", 0) or 0) for b in seq[:-1]][-20:]
        avg = (sum(prior) / len(prior)) if prior else 0.0
        # An unknown ratio is not a big one. Below the floor the average is
        # too small to divide by and rvol stays None.
        rvol = (vol / avg) if (avg >= min_avg_vol and avg > 0 and vol > 0) else None
        try:
            import float_feed
            fl = float_feed.float_shares(sym)
        except Exception:  # noqa: BLE001
            fl = None

        crit = ["mover"]
        if pct >= min_pct:
            crit.append("uptrend")
        if rvol is not None and rvol >= float(cfg.get("ai_watch_min_rvol", 2.0) or 2.0):
            crit.append("rvol")
        rows.append({
            "symbol": sym,
            "source": "movers",
            "agreement": True,
            "score": round(min(10.0, pct / 5.0), 2),
            "reason": f"mover {pct:+.1f}%"[:48],
            "pct_change": pct,
            "price": px,
            "rvol": rvol,
            "float_m": fl,
            "avg_vol_20d": round(avg) if avg else None,
            "dollar_volume": round(vol * px) if (vol and px) else None,
            "criteria": crit,
        })
    return rows


def main() -> None:
    cfg = load_config()
    fast = float(cfg.get("ai_movers_poll", 60.0) or 60.0)
    slow = float(cfg.get("ai_movers_poll_idle", 900.0) or 900.0)
    print(f"[movers] polling Alpaca movers every {fast:.0f}s "
          f"({slow:.0f}s outside 04:00-20:00 ET) -> {MOVERS_FILE.name}",
          flush=True)

    while True:
        cfg = load_config()
        active = _active_hours()
        try:
            rows = fetch_rows(cfg) if active else None
            if rows is not None:
                _write_json(MOVERS_FILE, {
                    "ts": time.time(),
                    "generated_et": _et_now().strftime("%Y-%m-%d %H:%M:%S"),
                    "rows": rows,
                })
                print(f"[movers] {len(rows)} row(s) "
                      f"{[r['symbol'] for r in rows][:8]}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[movers] pass failed: {e}", flush=True)
        time.sleep(float(cfg.get("ai_movers_poll", fast) or fast)
                   if active else slow)


if __name__ == "__main__":
    main()
