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

  Levered / inverse ETPs own the rest of a raw percent ranking. On
  2026-09-03 the book ate TSLL, MSTX, MST, CONL, CIF*, HODU, CRCG, CSEX
  — 2x/3x and single-stock levered products a long-only common-stock
  desk cannot trade. Same shape of filter as warrants: a cheap ticker
  test plus a small denylist, no network.

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
from ticker_filters import is_common, is_levered_etp  # noqa: E402

MOVERS_FILE = ROOT / "movers_stocks.json"
ET = ZoneInfo("America/New_York")

_write_json = desk_core.write_json_atomic

# Re-export for callers/tests that import movers_screener.is_* directly.
__all__ = ("is_common", "is_levered_etp")


def _keys() -> tuple[str, str]:
    import os
    return os.getenv("ALPACA_API_KEY", ""), os.getenv("ALPACA_SECRET_KEY", "")


def _et_now() -> datetime:
    return datetime.now(timezone.utc).astimezone(ET)


def _active_hours(now: datetime | None = None) -> bool:
    """Premarket through the close of post. Outside it the movers list is a
    frozen copy of the last session and polling it fast buys nothing."""
    now = now or _et_now()
    if now.weekday() >= 5:
        return False
    return 4 <= now.hour < 20


# Names seen on the movers list at any point in THIS session, and the ET day
# they belong to. Alpaca caps top at 50 and ranks by percent change, so a name
# up 22% is evicted the moment fifty others are up more — while still meeting
# every criterion the desk has. Wholesale replacement drops it for losing a
# ranking contest, which is not a rule it failed.
#
# Retention is not memory of a number: a retained symbol is re-measured from
# the same daily bars as everything else and must pass every filter again.
# What is remembered is only that it is worth re-measuring.
_session_syms: set[str] = set()
_session_day: str = ""


def _session_reset_if_new_day(now: datetime | None = None) -> None:
    """Yesterday's movers are not today's. Clear on the ET date turning over."""
    global _session_syms, _session_day
    day = (now or _et_now()).strftime("%Y-%m-%d")
    if day != _session_day:
        _session_syms = set()
        _session_day = day


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


# ── Full-market scan ────────────────────────────────────────────────────────
# Both Alpaca lists above are ranked contests: the top 50 gainers are small
# caps and the top actives are cheap volume or flat megacaps. On 2026-09-23
# (SPY -0.8%) they offered 3-5 names at $10-$100 while a snapshot of the whole
# tradable universe held 51 that were up 1.5%+ on RVOL >= 1 (energy, CRWV,
# IONQ, IR, KMX, ZM...). The book was thin for want of supply, not gates.
#
# The scan only nominates. Its numbers are IEX (snapshot latest trade vs prior
# close; IEX day volume vs IEX prior-day volume, time-adjusted, one feed on
# both sides), good enough to pick names worth measuring. Every nominee then
# goes through the same SIP daily bars and filters as every other candidate.
SCAN_LEDGER = ROOT / "ai_reports" / "scan_ledger.jsonl"
_scan_universe: list[str] = []
_scan_universe_day: str = ""
_scan_last_ts: float = 0.0
_scan_last: list[dict] = []


def _bar_day(bar) -> str:
    """ET calendar day of an Alpaca bar, '' when it has no usable timestamp."""
    ts = getattr(bar, "timestamp", None)
    try:
        return ts.astimezone(ET).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        return ""


def _rth_fraction(now: datetime | None = None) -> float | None:
    """Share of the regular session elapsed, or None outside 09:35-16:00."""
    now = now or _et_now()
    if now.weekday() >= 5:
        return None
    m = now.hour * 60 + now.minute
    if m < 9 * 60 + 35 or m >= 16 * 60:
        return None
    return max(0.05, min(1.0, (m - 570) / 390.0))


def scan_select(snaps: dict, *, lo: float, hi: float, min_pct: float,
                min_rvol: float, min_iex_dollars: float, frac: float,
                top: int) -> list[dict]:
    """Pick green, liquid names from {sym: (price, prev_close, vol, prev_vol)}.

    Pure so it can be tested without a network. Ranked by IEX dollar volume:
    the scan exists to add names the book can actually trade.
    """
    out = []
    for sym, t in snaps.items():
        try:
            px, pc, vol, pv = (float(x) if x is not None else 0.0 for x in t)
        except (TypeError, ValueError):
            continue
        if px <= 0 or pc <= 0 or pv <= 0 or not (lo <= px <= hi):
            continue
        pct = (px / pc - 1.0) * 100.0
        if pct < min_pct:
            continue
        rvol = (vol / pv) / max(frac, 0.05)
        if rvol < min_rvol:
            continue
        iex_dollars = px * vol
        if iex_dollars < min_iex_dollars:
            continue
        out.append({"symbol": sym, "iex_pct": round(pct, 2),
                    "iex_rvol": round(rvol, 2), "price": round(px, 4),
                    "iex_dollars": round(iex_dollars)})
    out.sort(key=lambda r: -r["iex_dollars"])
    return out[:max(0, top)]


def _load_scan_universe(api: str, sec: str) -> list[str]:
    """Tradable common-stock symbols, refreshed once per ET day."""
    global _scan_universe, _scan_universe_day
    day = _et_now().strftime("%Y-%m-%d")
    if _scan_universe and _scan_universe_day == day:
        return _scan_universe
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import AssetClass, AssetStatus
    from alpaca.trading.requests import GetAssetsRequest
    assets = None
    for paper in (True, False):
        try:
            assets = TradingClient(api, sec, paper=paper).get_all_assets(
                GetAssetsRequest(asset_class=AssetClass.US_EQUITY,
                                 status=AssetStatus.ACTIVE))
            break
        except Exception:  # noqa: BLE001
            continue
    if not assets:
        return _scan_universe
    ok_ex = {"NYSE", "NASDAQ", "ARCA", "AMEX", "BATS"}
    _scan_universe = sorted(
        a.symbol for a in assets
        if a.tradable and str(a.exchange).split(".")[-1] in ok_ex
        and is_common(a.symbol) and not is_levered_etp(a.symbol, a.name or ""))
    _scan_universe_day = day
    print(f"[movers] scan universe: {len(_scan_universe)} symbols", flush=True)
    return _scan_universe


def universe_scan(cfg: dict, api: str, sec: str, lo: float, hi: float,
                  min_pct: float) -> list[dict]:
    """Nominees from a snapshot of the whole universe, cached between scans."""
    global _scan_last_ts, _scan_last
    if not bool(cfg.get("ai_movers_universe_scan", False)):
        return []
    frac = _rth_fraction()
    if frac is None:
        return []
    every = float(cfg.get("ai_movers_scan_sec", 120.0) or 120.0)
    if _scan_last_ts and time.time() - _scan_last_ts < every:
        return _scan_last
    syms = _load_scan_universe(api, sec)
    if not syms:
        return _scan_last
    from alpaca.data.enums import DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockSnapshotRequest
    cl = StockHistoricalDataClient(api, sec)
    snaps: dict = {}
    for i in range(0, len(syms), 500):
        try:
            got = cl.get_stock_snapshot(StockSnapshotRequest(
                symbol_or_symbols=syms[i:i + 500], feed=DataFeed.IEX))
        except Exception as e:  # noqa: BLE001
            print(f"[movers] scan chunk failed: {str(e)[:100]}", flush=True)
            time.sleep(1.0)
            continue
        for s, v in (got or {}).items():
            try:
                snaps[s] = (v.latest_trade.price, v.previous_daily_bar.close,
                            v.daily_bar.volume, v.previous_daily_bar.volume)
            except Exception:  # noqa: BLE001
                continue
        time.sleep(0.3)  # the live engine shares these data keys
    picked = scan_select(
        snaps, lo=lo, hi=hi, min_pct=min_pct,
        min_rvol=float(cfg.get("ai_movers_scan_min_rvol", 1.0) or 0.0),
        min_iex_dollars=float(
            cfg.get("ai_movers_scan_min_iex_dollars", 500_000) or 0.0),
        frac=frac, top=int(cfg.get("ai_movers_scan_top", 25) or 25))
    _scan_last_ts, _scan_last = time.time(), picked
    try:
        now, day = time.time(), _et_now().strftime("%Y-%m-%d")
        with open(SCAN_LEDGER, "a", encoding="utf-8") as f:
            f.writelines(json.dumps({"ts": now, "day": day, **r}) + "\n"
                         for r in picked)
    except Exception:  # noqa: BLE001
        pass
    print(f"[movers] scan: {len(snaps)} snaps -> {len(picked)} nominees "
          f"{[r['symbol'] for r in picked][:10]}", flush=True)
    return picked


def fetch_rows(cfg: dict) -> list[dict]:
    """One pass: rank movers, drop what cannot be traded, enrich survivors."""
    api, sec = _keys()
    if not api or not sec:
        return []
    from alpaca.data.historical.screener import ScreenerClient
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import (MarketMoversRequest, MostActivesRequest,
                                      StockBarsRequest)
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
    keep_session = bool(cfg.get("ai_movers_session_append", True))
    max_session = int(cfg.get("ai_movers_session_max", 40) or 40)

    _session_reset_if_new_day()

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
        name = str(getattr(g, "name", "") or getattr(g, "company_name", "") or "")
        if (pct < min_pct or not is_common(sym) or is_levered_etp(sym, name)
                or not (lo <= px <= hi)):
            continue
        cand.append((sym, pct, px))
    origin: dict[str, str] = {s: "gainers" for s, _, _ in cand}

    # Full-market scan nominees go BEFORE most-actives: the candidate list is
    # truncated at want + max_session below, and 100 actives would push
    # anything appended after them past the cut. Unpriced on purpose, so they
    # are re-measured from the same SIP daily bars as every other name.
    for r in universe_scan(cfg, api, sec, lo, hi, min_pct):
        if r["symbol"] not in origin:
            cand.append((r["symbol"], None, None))
            origin[r["symbol"]] = "scan"

    # Second feed, ranked by VOLUME rather than percent change. The gainers
    # list is capped at 50 by Alpaca and its weakest member was +15.0% on
    # 2026-08-28 — above the desk's own 10% floor — so every name between the
    # floor and the day's cut is structurally invisible to it, and the cut
    # rises on an active morning. most-actives sees a different slice: names
    # trading heavily that are not among the fifty biggest movers.
    #
    # These arrive without a percent change, so they are carried as unpriced
    # candidates and measured from daily bars alongside everything else.
    if bool(cfg.get("ai_movers_use_most_actives", True)):
        try:
            ma = scr.get_most_actives(MostActivesRequest(
                by="volume", top=int(cfg.get("ai_movers_actives_top", 50) or 50)))
            have = {x[0] for x in cand}
            for a in (getattr(ma, "most_actives", None) or []):
                sym = str(getattr(a, "symbol", "") or "").upper()
                name = str(getattr(a, "name", "") or getattr(a, "company_name", "") or "")
                if (sym and sym not in have and is_common(sym)
                        and not is_levered_etp(sym, name)):
                    cand.append((sym, None, None))
                    have.add(sym)
                    origin.setdefault(sym, "actives")
        except Exception as e:  # noqa: BLE001
            print(f"[movers] most-actives failed: {e}", flush=True)

    ranked = {s for s, _, _ in cand if _ is not None}
    if keep_session:
        _session_syms.update(ranked)
        # Carry forward names seen earlier today that have since been ranked
        # out. Their pct/price are recomputed below from daily bars — the
        # ranking's own numbers are unavailable for a name it no longer lists.
        for sym in sorted(_session_syms - ranked)[:max(0, max_session - len(cand))]:
            cand.append((sym, None, None))

    if not cand:
        return []

    syms = [s for s, _, _ in cand][:want + max_session]
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

    time_adj = bool(cfg.get("rvol_time_adjusted", True))
    today = _et_now().strftime("%Y-%m-%d")
    _now_et = _et_now()
    mins_open = (_now_et - _now_et.replace(
        hour=9, minute=30, second=0, microsecond=0)).total_seconds() / 60.0
    rows = []
    for sym, pct, px in cand[:want + max_session]:
        seq = bars.get(sym) or []
        if pct is None or px is None:
            # Carried forward, so the ranking gave us nothing. Rebuild the day
            # change from the bars every other name is measured with: latest
            # close against the prior session's. No bars, no opinion, no row.
            if len(seq) < 2:
                continue
            try:
                px = float(getattr(seq[-1], "close", 0) or 0)
                prev_close = float(getattr(seq[-2], "close", 0) or 0)
            except (TypeError, ValueError):
                continue
            if px <= 0 or prev_close <= 0:
                continue
            pct = (px / prev_close - 1.0) * 100.0
            # Re-gated, not grandfathered. A name that has faded below the
            # floor, or out of the price band, leaves the book on its own
            # numbers rather than on how it ranked an hour ago.
            if pct < min_pct or not (lo <= px <= hi) or is_levered_etp(sym):
                _session_syms.discard(sym)
                continue
        vol = float(getattr(seq[-1], "volume", 0) or 0) if seq else 0.0
        prior = [float(getattr(b, "volume", 0) or 0) for b in seq[:-1]][-20:]
        avg = (sum(prior) / len(prior)) if prior else 0.0
        rvol_raw = (vol / avg) if (avg > 0 and vol > 0) else None
        rvol = rvol_raw
        # Pace, not a full-day ratio. Today's partial SIP volume against a
        # full-day average read CRWV at 0.75 at 12:57 on 2026-09-23 while its
        # pace was ~3x, so every movers row was held to a stricter floor than
        # trending (which time-adjusts) and scan nominees died as thin_rvol.
        # Same helper as trending and the dashboard. Only when the latest bar
        # is today's: before the open it is yesterday's completed total.
        if (rvol_raw is not None and time_adj and seq
                and _bar_day(seq[-1]) == today):
            try:
                import tools.morning_funnel as mf
                rvol = mf.rvol_pair(vol, avg, mins_open, time_adjusted=True)[0]
            except Exception:  # noqa: BLE001
                rvol = rvol_raw
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
            "rvol_raw": rvol_raw,
            "float_m": fl,
            "avg_vol_20d": round(avg) if avg else None,
            # Share of the trailing window's minutes that actually traded.
            # None means it was not measured this pass, which is not zero.
            "live_pct": (round(live_pct[sym], 3) if sym in live_pct else None),
            # False once the top-50 ranking has evicted it; the row survives
            # on its own numbers. Makes "why is this still here" answerable.
            "ranked": sym in ranked,
            # Which feed nominated it: gainers | scan | actives | carry. The
            # book relabels every row source=movers, so scan fills are
            # separated later by joining on ai_reports/scan_ledger.jsonl.
            "origin": origin.get(sym, "carry"),
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
                n_scan = sum(1 for r in rows if r.get("origin") == "scan")
                print(f"[movers] {len(rows)} row(s) ({n_scan} from scan) "
                      f"{[r['symbol'] for r in rows][:12]}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[movers] pass failed: {e}", flush=True)
        time.sleep(float(cfg.get("ai_movers_poll", fast) or fast)
                   if active else slow)


if __name__ == "__main__":
    main()
