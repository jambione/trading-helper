#!/usr/bin/env python3
"""
swing_screener.py — Swing-trade candidate screener.

Data sources (no paid third party — uses what the app already has):
  • Alpaca  — daily bars for the universe + all technicals (oversold, reversal
    confirmation, relative volume, support) via our own signals.py indicators.
  • Finnhub — fundamentals (stock/metric: PE, PB, current ratio, debt/equity),
    analyst consensus (stock/recommendation), and the earnings calendar.

Funnel (quota-aware — Finnhub free tier is ~60 calls/min):
  1. Universe = curated liquid names + Alpaca movers (losers/most-actives).
  2. One batched Alpaca daily-bars pull → compute technicals locally → keep only
     names passing the cheap technical gate (price < ceiling, oversold+turning,
     rel-vol). Alpaca serves bars for the whole market — no symbol whitelist.
  3. For the survivors only, pull Finnhub fundamentals/rating/earnings.
  4. Score, rank, write swing_candidates.json. The dashboard overlays live
     Discord confluence at serve time.

"Projected 2–3× EPS growth" is proxied by peTTM / forwardPE (= forwardEPS /
trailingEPS, the growth the market/analysts are pricing in), since Finnhub's free
tier has no multi-year forward EPS estimate. R:R target is the 52-week high
(Finnhub price targets are premium-gated).

Runs on a schedule (default 08:00 / 12:30 / 20:00 ET) — NOT a constant loop; it
sleeps in between. The dashboard's refresh button triggers run_screen on demand.

Standalone:  python swing_screener.py --once
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import tempfile
import time
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from config import load_config
from signals import ema, rsi, compute_macd, calc_rvol
import alpaca_api

ET = ZoneInfo("America/New_York")
log = logging.getLogger("swing")

FINNHUB_BASE = "https://finnhub.io/api/v1"
ALPACA_SCREENER = "https://data.alpaca.markets/v1beta1/screener/stocks"
SWING_FILE = Path(__file__).parent / "swing_candidates.json"
UNIVERSE_FILE = Path(__file__).parent / "swing_universe.json"

_HTTP_TIMEOUT = 12

# Curated liquid US names that frequently trade under $100 — the "undervalued +
# strong balance sheet + analyst-rated" space the strategy targets. Alpaca movers
# are added on top for oversold discovery. Override with swing_universe.json.
DEFAULT_UNIVERSE = [
    "F", "BAC", "WFC", "T", "VZ", "PFE", "KO", "CSCO", "INTC", "KMI",
    "HBAN", "KEY", "RF", "SOFI", "PLTR", "GM", "CCL", "NCLH", "AAL", "DAL",
    "UAL", "MU", "ON", "VALE", "GOLD", "KGC", "CLF", "X", "AA", "WBD",
    "PARA", "KDP", "KHC", "GILD", "WBA", "SNAP", "NIO", "GRAB", "UBER", "CVE",
    "SLB", "HAL", "DVN", "KVUE", "VTRS", "CMCSA", "PYPL", "C", "USB", "MOS",
    "CFG", "ET", "NEM", "AMCR", "PCG", "BMY", "KR", "DOW", "APA", "CNC",
]

# Analyst consensus → rank (higher = more bullish).
_RATING_RANK = {"strong sell": 0, "sell": 1, "hold": 2, "buy": 3, "strong buy": 4}


class _Degraded(Exception):
    """An endpoint was unavailable (paywalled / rate-limited); record a skipped
    criterion and keep going rather than sinking the whole candidate."""


# ── Finnhub HTTP ──────────────────────────────────────────────────────────────

def _finnhub_get(path: str, cfg: dict, **params):
    """GET a Finnhub endpoint. Raises _Degraded on 401/402/403/429 so one gated
    field never sinks a candidate."""
    params["token"] = cfg.get("finnhub_key", "")
    try:
        r = requests.get(f"{FINNHUB_BASE}/{path}", params=params, timeout=_HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise _Degraded(str(e)) from e
    if r.status_code in (401, 402, 403, 429):
        raise _Degraded(f"{r.status_code} on {path}")
    r.raise_for_status()
    return r.json()


# ── Alpaca bars ───────────────────────────────────────────────────────────────

_DATA_CLIENT = None

def _data_client(cfg: dict):
    global _DATA_CLIENT
    if _DATA_CLIENT is None:
        try:
            _DATA_CLIENT = alpaca_api.connect_data_client(cfg)
        except Exception as e:
            log.warning("[swing] alpaca connect failed: %s", e)
            _DATA_CLIENT = None
    return _DATA_CLIENT


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def fetch_daily_bars_batch(symbols: list[str], cfg: dict) -> dict[str, pd.DataFrame]:
    """Daily OHLCV for many symbols in a few batched Alpaca requests. Returns
    {symbol → DataFrame(oldest→newest, close/high/low/volume)}."""
    client = _data_client(cfg)
    if client is None or not symbols:
        return {}
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    lookback = int(cfg.get("swing_bar_lookback_days", 150))
    start = datetime.now(timezone.utc) - timedelta(days=lookback)
    feed = alpaca_api._get_feed_arg(cfg)
    out: dict[str, pd.DataFrame] = {}

    for chunk in _chunks(symbols, 100):
        req = StockBarsRequest(
            symbol_or_symbols=chunk,
            timeframe=TimeFrame(1, TimeFrameUnit.Day),
            start=start,
            **feed,
        )
        try:
            df = client.get_stock_bars(req).df
        except Exception as e:
            log.warning("[swing] alpaca bars chunk failed: %s", e)
            continue
        if df is None or df.empty:
            continue
        if isinstance(df.index, pd.MultiIndex):
            for sym in chunk:
                try:
                    sub = df.xs(sym, level="symbol")
                except KeyError:
                    continue
                out[sym] = _clean_bars(sub)
        else:
            out[chunk[0]] = _clean_bars(df)
    return {s: d for s, d in out.items() if not d.empty}


def _clean_bars(df: pd.DataFrame) -> pd.DataFrame:
    df = df[["close", "high", "low", "volume"]].copy()
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    return df.dropna(subset=["close"]).reset_index(drop=True)


def fetch_daily_bars(sym: str, cfg: dict) -> pd.DataFrame:
    """Single-symbol daily bars (for ad-hoc use / tests)."""
    return fetch_daily_bars_batch([sym], cfg).get(sym, pd.DataFrame())


# ── Universe ──────────────────────────────────────────────────────────────────

def screen_universe(cfg: dict) -> list[str]:
    """Curated liquid names + Alpaca movers (losers where oversold bounces live,
    plus most-actives), deduped. swing_universe.json overrides entirely. The price
    ceiling and every other criterion are enforced downstream from bars/fundamentals
    we fetch anyway."""
    if UNIVERSE_FILE.exists():
        try:
            syms = json.loads(UNIVERSE_FILE.read_text(encoding="utf-8"))
            if isinstance(syms, list) and syms:
                return [str(s).upper() for s in syms]
        except Exception as e:
            log.warning("[swing] bad swing_universe.json (%s) — using default", e)

    syms: list[str] = list(DEFAULT_UNIVERSE)
    max_price = float(cfg.get("swing_max_price", 100.0))
    headers = {"APCA-API-KEY-ID": cfg.get("api_key", ""),
               "APCA-API-SECRET-KEY": cfg.get("secret_key", "")}
    for path, key in (("movers?top=50", "losers"), ("most-actives?top=50", "most_actives")):
        try:
            r = requests.get(f"{ALPACA_SCREENER}/{path}", headers=headers, timeout=_HTTP_TIMEOUT)
            if r.status_code != 200:
                continue
            for row in r.json().get(key, []):
                sym, price = row.get("symbol"), row.get("price")
                # skip sub-$1 junk; keep names under the ceiling (price may be absent
                # on most-actives → let the bars price-filter decide later)
                if sym and (price is None or 1.0 <= price < max_price):
                    syms.append(sym)
        except requests.RequestException as e:
            log.warning("[swing] alpaca movers %s failed: %s", path, e)

    seen: dict[str, None] = {}
    for s in syms:
        seen.setdefault(s, None)
    return list(seen)


# ── Technicals (via signals.py — same as the live engine) ─────────────────────

def _technicals(df: pd.DataFrame, cfg: dict) -> dict:
    if df.empty or len(df) < 30:
        return {}
    close = df["close"]
    rsi_series = rsi(close, 14)
    rsi_now, rsi_prev = float(rsi_series.iloc[-1]), float(rsi_series.iloc[-2])

    macd = compute_macd(df, cfg)
    hist_now, hist_prev = float(macd["macd_hist"].iloc[-1]), float(macd["macd_hist"].iloc[-2])

    ema_short = float(ema(close, int(cfg.get("ema_short", 8))).iloc[-1])
    close_now, close_prev = float(close.iloc[-1]), float(close.iloc[-2])

    oversold_thr = float(cfg.get("swing_rsi_oversold", 30.0))
    oversold = rsi_now < oversold_thr
    recently_oversold = bool((rsi_series.tail(5) < oversold_thr).any())

    # Turn-up signals tied to a real up-move — in a pure decline the MACD histogram
    # drifts up toward zero on its own, so it's only trusted on a green bar. This is
    # the falling-knife guard.
    bar_up = close_now > close_prev
    rsi_hooking = recently_oversold and rsi_now > rsi_prev
    macd_turning = bar_up and hist_now > hist_prev
    reclaim_ema = close_now > ema_short
    reversal_signals = [n for n, on in (("rsi_hook", rsi_hooking),
                                        ("macd_turn", macd_turning),
                                        ("ema_reclaim", reclaim_ema)) if on]

    rel_vol = float(calc_rvol(df).iloc[-1])
    support = float(df["low"].tail(20).min())

    return {
        "price": round(close_now, 2),
        "rsi": round(rsi_now, 1),
        "oversold": oversold,
        "reversal_confirmed": oversold and bool(reversal_signals),
        "reversal_signals": reversal_signals,
        "rel_vol": round(rel_vol, 2),
        "support": round(support, 2),
    }


# ── Finnhub fundamentals ──────────────────────────────────────────────────────

def add_fundamentals(sym: str, c: dict, cfg: dict) -> dict:
    """Enrich a technically-qualified candidate with Finnhub fundamentals, analyst
    consensus, R:R (52-wk-high target), and the earnings-window flag. Each block
    degrades independently (gated endpoint → field None + a `skipped` marker)."""
    wk52_high = None
    # Valuation + balance sheet + projected EPS-growth proxy
    try:
        m = (_finnhub_get("stock/metric", cfg, symbol=sym, metric="all") or {}).get("metric", {})
        c["pe"] = m.get("peTTM") or m.get("peBasicExclExtraTTM")
        c["pb"] = m.get("pbQuarterly") or m.get("pbAnnual") or m.get("pb")
        c["current_ratio"] = m.get("currentRatioQuarterly") or m.get("currentRatioAnnual")
        c["debt_to_equity"] = (m.get("totalDebt/totalEquityQuarterly")
                               or m.get("longTermDebt/equityQuarterly"))
        pe_ttm, fwd_pe = m.get("peTTM"), m.get("forwardPE")
        # peTTM / forwardPE = forwardEPS / trailingEPS = the projected growth multiple.
        c["eps_growth"] = round(pe_ttm / fwd_pe, 2) if (pe_ttm and fwd_pe and fwd_pe > 0) else None
        wk52_high = m.get("52WeekHigh")
    except _Degraded:
        c["skipped"].append("metric")
        c["eps_growth"] = None

    # Analyst consensus + improving-trend flag (proxy for a fresh upgrade)
    try:
        rec = _finnhub_get("stock/recommendation", cfg, symbol=sym)
        c["rating"], c["recent_upgrade"] = _consensus_and_trend(rec)
    except _Degraded:
        c["skipped"].append("rating")
        c["rating"] = None
        c["recent_upgrade"] = False

    # Reward:risk — target = 52-week high (Finnhub price targets are premium)
    c["target"] = wk52_high
    price, support = c.get("price"), c.get("support")
    if wk52_high and price and support and price > support and wk52_high > price:
        c["rr"] = round((wk52_high - price) / (price - support), 2)
    else:
        c["rr"] = None

    # Earnings inside the swing window
    try:
        today = date.today()
        horizon = today + timedelta(days=int(cfg.get("swing_earnings_window_days", 14)))
        cal = _finnhub_get("calendar/earnings", cfg, symbol=sym,
                           **{"from": str(today), "to": str(horizon)})
        rows = cal.get("earningsCalendar", []) if isinstance(cal, dict) else []
        c["earnings_in_window"] = bool(rows)
    except _Degraded:
        c["skipped"].append("earnings")
        c["earnings_in_window"] = False

    return c


def _rec_score(row: dict) -> float | None:
    total = sum(row.get(k, 0) for k in ("strongBuy", "buy", "hold", "sell", "strongSell"))
    if not total:
        return None
    return (5 * row.get("strongBuy", 0) + 4 * row.get("buy", 0) + 3 * row.get("hold", 0)
            + 2 * row.get("sell", 0) + 1 * row.get("strongSell", 0)) / total


def _consensus_and_trend(rec) -> tuple[str | None, bool]:
    """From Finnhub's monthly recommendation buckets (newest first): a consensus
    label, plus whether analyst bullishness improved vs the prior month (our
    free-tier stand-in for a discrete upgrade event)."""
    if not isinstance(rec, list) or not rec:
        return None, False
    score = _rec_score(rec[0])
    if score is None:
        return None, False
    rating = ("Strong Buy" if score >= 4.5 else "Buy" if score >= 3.5
              else "Hold" if score >= 2.5 else "Sell" if score >= 1.5 else "Strong Sell")
    improving = False
    if len(rec) > 1:
        prev = _rec_score(rec[1])
        improving = prev is not None and score > prev
    return rating, improving


# ── Gate + scoring ────────────────────────────────────────────────────────────

def _technical_gate(c: dict, cfg: dict) -> bool:
    """Bars-only hard filters — used to skip Finnhub calls for names that can't
    qualify. score_candidate re-checks these; this is purely a quota optimization."""
    price = c.get("price")
    if price is None or price >= float(cfg.get("swing_max_price", 100.0)):
        return False
    if c.get("rsi") is not None and not c.get("reversal_confirmed"):
        return False
    if c.get("rel_vol") is not None and c["rel_vol"] < float(cfg.get("swing_rel_vol_min", 1.5)):
        return False
    return True


def _rating_ok(rating: str | None, minimum: str) -> bool:
    if not rating:                       # unknown (degraded) → don't reject
        return True
    return _RATING_RANK.get(rating.lower(), 2) >= _RATING_RANK.get(minimum.lower(), 3)


def score_candidate(c: dict, cfg: dict) -> float | None:
    """Weighted rank score, or None to reject on a hard filter. Filters only reject
    on criteria we could actually measure (degraded fields pass through)."""
    price = c.get("price")
    if price is None or price >= float(cfg.get("swing_max_price", 100.0)):
        return None
    if c.get("rsi") is not None and not c.get("reversal_confirmed"):
        return None
    if c.get("rr") is not None and c["rr"] < float(cfg.get("swing_min_rr", 2.0)):
        return None
    if c.get("rel_vol") is not None and c["rel_vol"] < float(cfg.get("swing_rel_vol_min", 1.5)):
        return None
    eps_g = c.get("eps_growth")
    if eps_g is not None and not (
        float(cfg.get("swing_min_eps_growth", 2.0)) <= eps_g <= float(cfg.get("swing_max_eps_growth", 3.0))
    ):
        return None
    if not _rating_ok(c.get("rating"), str(cfg.get("swing_min_rating", "Buy"))):
        return None

    oversold_thr = float(cfg.get("swing_rsi_oversold", 30.0))
    score = 0.0
    score += max(0.0, oversold_thr - (c.get("rsi") or oversold_thr))
    score += _RATING_RANK.get((c.get("rating") or "").lower(), 2)
    score += (c.get("rr") or 0.0)
    score += (c.get("eps_growth") or 0.0)
    score += min(3.0, (c.get("rel_vol") or 1.0))
    if c.get("recent_upgrade"):
        score += 2.0
    if c.get("earnings_in_window"):
        score -= 1.0
    return round(score, 3)


def enrich(sym: str, cfg: dict) -> dict | None:
    """Full single-symbol pipeline (bars → technicals → gate → fundamentals). Used
    ad-hoc and in tests; run_screen uses the batched path."""
    df = fetch_daily_bars(sym, cfg)
    tech = _technicals(df, cfg)
    if not tech:
        return None
    c: dict = {"ticker": sym, "skipped": [], **tech}
    if not _technical_gate(c, cfg):
        return None
    return add_fundamentals(sym, c, cfg)


# ── Orchestration ─────────────────────────────────────────────────────────────

def run_screen(cfg: dict) -> list[dict]:
    """Universe → batched Alpaca bars → technical gate → Finnhub fundamentals for
    survivors → score/rank → write swing_candidates.json. Callable from the
    scheduler loop or the dashboard's refresh button."""
    symbols = screen_universe(cfg)
    bars_map = fetch_daily_bars_batch(symbols, cfg)
    log.info("[swing] universe=%d, bars=%d", len(symbols), len(bars_map))

    survivors: list[dict] = []
    for sym, df in bars_map.items():
        tech = _technicals(df, cfg)
        if not tech:
            continue
        c = {"ticker": sym, "skipped": [], **tech}
        if _technical_gate(c, cfg):
            survivors.append(c)
    log.info("[swing] technical survivors=%d", len(survivors))

    scored: list[dict] = []
    for c in survivors[: int(cfg.get("swing_enrich_cap", 40))]:
        try:
            add_fundamentals(c["ticker"], c, cfg)
        except Exception as e:
            log.warning("[swing] %s fundamentals error: %s", c["ticker"], e)
            continue
        s = score_candidate(c, cfg)
        if s is None:
            continue
        c["score"] = s
        scored.append(c)

    scored.sort(key=lambda r: r["score"], reverse=True)
    top = scored[: int(cfg.get("swing_limit", 12))]
    _write_swing(top)
    log.info("[swing] wrote %d candidates", len(top))
    return top


def _write_swing(candidates: list[dict]) -> None:
    SWING_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=SWING_FILE.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"updated": time.time(), "candidates": candidates}, f, indent=2)
        except Exception:
            os.close(fd)
            raise
        Path(tmp_path).replace(SWING_FILE)
        tmp_path = None
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# ── Scheduler (3×/day, not a constant loop) ───────────────────────────────────

def _parse_run_times(cfg: dict) -> list[tuple[int, int]]:
    out = []
    for s in cfg.get("swing_run_times", ["08:00", "12:30", "20:00"]):
        try:
            hh, mm = str(s).split(":")
            out.append((int(hh), int(mm)))
        except (ValueError, AttributeError):
            log.warning("[swing] bad run time %r — ignored", s)
    return sorted(out) or [(8, 0), (12, 30), (20, 0)]


def _seconds_until_next_run(cfg: dict, now: datetime | None = None) -> float:
    """Seconds to sleep until the next scheduled ET run time."""
    now = now or datetime.now(ET)
    times = _parse_run_times(cfg)
    todays = [now.replace(hour=h, minute=m, second=0, microsecond=0) for h, m in times]
    upcoming = [t for t in todays if t > now]
    nxt = upcoming[0] if upcoming else todays[0] + timedelta(days=1)
    return max(1.0, (nxt - now).total_seconds())


def main() -> None:
    parser = argparse.ArgumentParser(description="Swing-trade candidate screener")
    parser.add_argument("--once", action="store_true", help="run one screen and exit")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    cfg = load_config()
    if not cfg.get("finnhub_key"):
        log.warning("[swing] no finnhub_key — fundamentals will degrade to technicals only")

    if args.once:
        run_screen(cfg)
        return

    log.info("[swing] started — scheduled runs at %s ET (sleeps in between)",
             ", ".join(cfg.get("swing_run_times", [])))
    while True:
        cfg = load_config()                 # pick up config-panel edits
        sleep_s = _seconds_until_next_run(cfg)
        log.info("[swing] next run in %.1f min", sleep_s / 60.0)
        time.sleep(sleep_s)
        try:
            run_screen(load_config())
        except Exception as e:
            log.warning("[swing] run failed: %s", e)
        time.sleep(60)                       # clear the trigger minute before recomputing


if __name__ == "__main__":
    main()
