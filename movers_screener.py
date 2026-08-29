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

  Liquidity is a question about TODAY, not about the average. The first
  version of this floored the 20-day mean, on the theory that QNRX's 1281x
  was a divide-by-nothing. Checked against SIP it is nothing of the kind:
  31,009,292 shares traded against a 24,203 average, which is a dormant
  shell genuinely waking up — the ratio is real and it is the strongest
  signal on the list. What a small average cannot tell you is whether the
  name is tradeable, and today's dollar volume can: QNRX did $190M.
  ai_movers_min_dollar_vol floors that instead.

FEEDS
Both sides of RVOL are SIP daily bars. One feed, one granularity, per
[[volume-ratios-need-one-feed]] — but SIP rather than the IEX that
stocktwits_trending uses, because IEX is a few percent of the tape and its
20-day averages here are 313-12,565 shares. A ratio off a 502-share base is
noise wearing a decimal point (QNRX reads 269x on IEX against 1281x on SIP).
SIP daily bars are historical and available on this plan; only SIP snapshots
are 403.

One consequence to know: a movers rvol is not numerically the same statistic
as the trending panel's IEX rvol for the same name. Both are internally
consistent, this one is the true market ratio, and ai_watch_min_rvol is
applied to both — so that floor is slightly stricter here than on trending.

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


def _rth_minutes_in_window(window_min: int, now: datetime | None = None) -> int:
    """How many of the trailing *window_min* minutes were inside RTH.

    The continuity filter divides by this rather than by the window, because
    a minute the market was shut is not a minute a name failed to trade in.
    """
    now = (now or _et_now())
    if now.weekday() >= 5:
        return 0
    open_t = now.replace(hour=9, minute=30, second=0, microsecond=0)
    close_t = now.replace(hour=16, minute=0, second=0, microsecond=0)
    start = now - timedelta(minutes=max(0, window_min))
    lo = max(start, open_t)
    hi = min(now, close_t)
    if hi <= lo:
        return 0
    return int((hi - lo).total_seconds() // 60)


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
    min_dollar_vol = float(cfg.get("ai_movers_min_dollar_vol", 1_000_000) or 0)
    live_win = int(cfg.get("ai_movers_live_window_min", 60) or 60)
    min_live_pct = float(cfg.get("ai_movers_min_live_pct", 0.0) or 0.0)
    min_min_dollars = float(cfg.get("ai_movers_min_minute_dollars", 2000.0) or 0.0)
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
            start=start, limit=10000, feed=DataFeed.SIP))
        bars = getattr(df, "data", {}) or {}
    except Exception as e:  # noqa: BLE001
        print(f"[movers] daily bars failed: {e}", flush=True)

    # Tape continuity. A DAILY dollar-volume floor is a sum, and a sum cannot
    # tell a name that trades every minute from one that does its whole day in
    # three bursts. Measured 2026-08-28: RDIB cleared $12.4M total and still
    # had a 54-minute stretch with no prints at all; YDES printed in 29% of
    # RTH minutes at a $1,920 median. Both passed the dollar floor. Half that
    # session's list was untradeable and nothing here could see it.
    #
    # It matters because the working shelf sits 0.25% under the fill. On a
    # tape with holes, the next print after entry can be several tenths of a
    # percent away with nothing in between, so the stop is set by whoever
    # crosses next rather than by the move.
    #
    # A trailing window, not the session: it costs less, reflects liquidity
    # NOW, and lets a name that has just woken up qualify intraday. Median
    # per-minute dollars is deliberately not the test — RDIB's was a healthy
    # $36k. Coverage is the discriminator.
    # Only judge on minutes the market was actually open. A trailing window
    # is otherwise indistinguishable from a thin tape: run at 04:00 the window
    # covers 03:00-04:00, nothing trades in it, every name reads 0% and the
    # book empties — which is what this did the first time it ran, on a
    # Saturday. Premarket would have done the same thing every morning.
    #
    # Fewer open minutes than the floor means there is not enough tape to have
    # an opinion, so it does not form one.
    open_min = _rth_minutes_in_window(live_win)
    need_min = int(cfg.get("ai_movers_live_min_open_minutes", 20) or 20)
    live_pct: dict[str, float] = {}
    if min_live_pct > 0 and syms and open_min >= need_min:
        try:
            mdf = data.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=syms,
                timeframe=TimeFrame(1, TimeFrameUnit.Minute),
                start=datetime.now(timezone.utc) - timedelta(minutes=live_win),
                limit=100000, feed=DataFeed.SIP))
            mbars = getattr(mdf, "data", {}) or {}
            for sym in syms:
                seq = mbars.get(sym) or []
                live = sum(
                    1 for b in seq
                    if float(getattr(b, "volume", 0) or 0)
                    * float(getattr(b, "vwap", None)
                            or getattr(b, "close", 0) or 0) >= min_min_dollars)
                # Denominator is OPEN minutes, not wall-clock minutes.
                live_pct[sym] = live / float(max(1, open_min))
        except Exception as e:  # noqa: BLE001
            # No reading is no opinion. Refusing every name because one bar
            # request failed would empty the book on an API hiccup, and this
            # filter is about tape quality, not about availability.
            print(f"[movers] minute bars failed, continuity filter off "
                  f"this pass: {e}", flush=True)
            live_pct = {}

    try:
        import float_feed
        # Bounded per pass, and paced. Unbounded this blocks the 60s loop for
        # 1.1s per uncached name — on a morning with 25 fresh movers that is
        # half a minute of a screener not screening. Whatever is missed reads
        # None this pass and is picked up on the next one; the cache TTL is a
        # week, so this converges within a few minutes of the open and then
        # fetches nothing.
        float_feed.refresh(syms, limit=int(
            cfg.get("ai_movers_float_refresh_per_pass", 10) or 10),
            pace_sec=1.1)
    except Exception:  # noqa: BLE001
        pass

    rows = []
    for sym, pct, px in cand[:want]:
        seq = bars.get(sym) or []
        vol = float(getattr(seq[-1], "volume", 0) or 0) if seq else 0.0
        prior = [float(getattr(b, "volume", 0) or 0) for b in seq[:-1]][-20:]
        avg = (sum(prior) / len(prior)) if prior else 0.0
        rvol = (vol / avg) if (avg > 0 and vol > 0) else None
        dollar_vol = (vol * px) if vol else 0.0
        # Tradeable TODAY is the liquidity question. A tiny 20-day average is
        # what makes the ratio interesting, not what makes the name unsafe.
        if min_dollar_vol > 0 and dollar_vol < min_dollar_vol:
            continue
        lp = live_pct.get(sym)
        if min_live_pct > 0 and lp is not None and lp < min_live_pct:
            continue
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
            # BOTH names, deliberately. The Scan renderer reads trending_score
            # for the Score cell and for its score sort (feeds.js), while the
            # book's own ranking reads score — and these rows reuse the Trend
            # row shape, where the seed sets both. Setting only `score` left
            # every movers row showing "—" in Score and sorting as a null,
            # which looked like correct ordering purely because Alpaca returns
            # the movers pre-ranked.
            "score": round(min(10.0, pct / 5.0), 2),
            "trending_score": round(min(10.0, pct / 5.0), 2),
            "reason": f"mover {pct:+.1f}%"[:48],
            "pct_change": pct,
            "price": px,
            "rvol": rvol,
            "float_m": fl,
            "avg_vol_20d": round(avg) if avg else None,
            # Share of the trailing window's minutes that actually traded.
            # None means it was not measured this pass, which is not zero.
            "live_pct": (round(live_pct[sym], 3) if sym in live_pct else None),
            "dollar_volume": round(dollar_vol) if dollar_vol else None,
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
