"""Momentum desk monitor — Discord momentum + Stocktwits + Claude + TradingView load.

Polls the dashboard /api/state feed, Stocktwits trending, and optional Claude
suggestions.
Setup column: green FOCUS when CM RSI-2 and deep %R both fire (signal_proximity).

No Alpaca buy/sell in this desk (B/S/T are Stocktwits letter keys).

Cross-platform (macOS + Windows):

  1-9     focus momentum row + load TradingView
  SPACE   focus newest momentum + load TradingView
  A-J     focus Stocktwits trending row + load TradingView
          (A = 1st under-$max panel row, B = 2nd, … J = 10th)

Focused symbol is written to repo root active_symbol.json.

Run (from repo root or this folder):
  python3 momentum-monitor/momentum_signal.py
  python3 momentum_signal.py
"""
from __future__ import annotations

import atexit
import contextlib
import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import date, datetime
from pathlib import Path
from urllib.request import Request as _UReq, urlopen

from rich.align import Align
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "tv-monitor"))

# Reading the three indicators off the TradingView window itself. Optional:
# needs macOS + Quartz + tesseract, and the desk runs fine without it — the
# buy circle just falls back to the engine's numbers.
try:
    import tv_chart_feed as chart_feed
except Exception:                                          # noqa: BLE001
    chart_feed = None

if sys.platform == "win32":
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:                                      # noqa: BLE001
        with contextlib.suppress(Exception):
            ctypes.windll.user32.SetProcessDPIAware()

# Dashboard auth helper (optional — Windows or Mac agent)
wa = None
try:
    if sys.platform == "darwin":
        import mac_agent as wa
    elif sys.platform == "win32":
        import windows_agent as wa
except Exception:                                          # noqa: BLE001
    wa = None

try:
    from session_clock import session_line
except Exception:                                          # noqa: BLE001
    session_line = None

try:
    from plyer import notification as _plyer
except Exception:                                          # noqa: BLE001
    _plyer = None

import desk_actions as desk
import spark
from desk_hotkeys import DeskHotkeys
from journal import Journal
from remote_feeds import RemoteAiSuggestions, RemoteStocktwitsTrending
from symbol_history import SymbolHistory

WatchlistReader = top_movers = None  # OCR movers retired

CONFIG_PATH = HERE / "momentum_config.json"

DEFAULTS = {
    "poll_interval": 2.0,
    "hotkey_slots": 9,
    "new_ttl": 120.0,
    "alert_new": True,
    "alert_burst": True,
    "alert_buy": False,
    "alert_st_new": True,
    "alert_st_look": True,
    "alert_cooldown": 60.0,
    "alert_notify_interval": 180.0,  # min seconds between OS notification popups
    "alert_notify_duration": 5.0,    # seconds before an OS popup auto-dismisses
    "alert_only_when_hidden": True,  # skip the popup if Terminal is frontmost
    "desktop_toast": True,
    # Alert sound. Was Windows-only (a raw 880Hz square wave); macOS got nothing
    # at all, so the desk was effectively silent except for the throttled
    # banner. Sounds are now per kind — see ALERT_SOUNDS — so a FOCUS firing and
    # a symbol merely appearing are distinguishable without looking at the
    # screen. Audition them with:  python momentum_signal.py --sounds
    "alert_sound": True,
    "alert_sound_volume": 0.35,      # 0.0-1.0; deliberately well under full
    "alert_sound_name": "Submarine",  # fallback for kinds not in ALERT_SOUNDS
    "alert_sound_min_gap": 1.5,      # global floor between sounds, on top of
                                      # alert_cooldown — that cooldown is per
                                      # (kind, symbol), so a wide burst at the
                                      # open would otherwise overlap into noise
    "alert_sound_by_kind": {},       # e.g. {"burst": "Glass"} to override one
    "watchlist_enabled": False,
    # Per-symbol sample ring (sparklines, mention trend, journal context).
    # 120 samples ≈ 4 min of tape at poll_interval 2.0s.
    "history_samples": 120,
    # Mark a price that is no longer a current print. The server publishes
    # price_age_sec = seconds since the trade itself (not since we fetched it),
    # so a quiet symbol can be seen to be quiet instead of looking live.
    "price_age_enabled": True,
    "price_stale_sec": 20.0,
    # FOCUS = CM RSI green-long AND both %R lines deep OS toward -100
    "rsi_focus_max": 35.0,       # CM RSI-2 in [0, max)
    "pctr_focus_lo": -100.0,     # both %R lines >= lo
    "pctr_focus_hi": -75.0,      # both %R lines <= hi
    # How long FOCUS has been lit. A setup that fired 12s ago and one sitting
    # for 6 minutes are different trades.
    "focus_age_enabled": True,
    "focus_age_fresh_sec": 60.0,
    "focus_age_stale_sec": 180.0,
    # Distance-to-trigger: turns the binary Setup column into a queue.
    "setup_distance_enabled": True,
    "setup_near_threshold": 0.25,
    # How much the engine's own 3-indicator completion counts toward the
    # distance vs the desk's two FOCUS legs. 0.0 = legs only.
    "setup_proximity_weight": 0.35,
    # Reordering rows by setup distance is OFF until it has been watched for
    # a few sessions — ship the column first (roadmap T1.2).
    "setup_sort_enabled": False,
    # Buy-readiness circle, top-right of the header. Counts the three chart
    # indicators (CM RSI-2, %R Trend Exhaustion, MACD) lit on the *charted*
    # symbol. Needs STRATEGY_MODE=three_indicator on the engine, else it stays
    # dim by design rather than guessing. Cut points are the engine's own
    # buy_zone/aligning boundaries, restated here so they can be tuned without
    # touching the engine — note buy_pct is quantised to 0/33/67/100, so real
    # sensitivity tuning lives in the engine's THREE_IND_* params.
    "buy_circle_enabled": True,
    "buy_circle_green_min": 100.0,
    "buy_circle_yellow_min": 67.0,
    # "chart" reads the TradingView tab title (notices manual symbol changes);
    # "hotkey" trusts only what we sent to TV ourselves.
    "buy_circle_symbol_source": "chart",
    "buy_circle_chart_poll_sec": 1.0,
    # Where the three indicator values come from. "chart" screen-reads the
    # TradingView panels — works for any charted symbol, needs no engine, and
    # agrees with what you see by construction. "engine" uses signal_proximity
    # off /api/state, which only covers symbols the engine tracks and computes
    # on Alpaca IEX rather than the chart's own feed. Falls back to "engine"
    # automatically when the screen reader is unavailable.
    "buy_circle_source": "chart",
    # Corner indicator: an arrow for direction of travel (%R + MACD) rather
    # than a dot for leg count. False keeps the dot. The leg count rides along
    # either way — "is the setup complete" and "which way is it going" are
    # different questions and the arrow answers the second.
    "buy_circle_arrow": True,
    # Relative volume column. A $2 stock on 8x volume is a different animal
    # from one on 1.1x. Source is funnel.rvol, else the row's own rvol — both
    # time-adjusted server-side (T2.1). Never synthesised here.
    "rvol_column_enabled": True,
    "rvol_hot": 3.0,
    "rvol_warm": 1.5,
    # Session journal — one JSONL record per rising edge, the only thing here
    # that produces evidence rather than a hypothesis. Runtime state: the
    # directory is gitignored.
    "journal_enabled": True,
    "journal_dir": "journal",
    "journal_flush_sec": 5.0,
    # Price shape. Building, spiking and fading all read +12% on Chg%.
    #
    # DEFAULT OFF. The idea is sound but the input is not good enough in the
    # windows that matter. dashboard.py's price loop takes Finnhub WebSocket
    # ticks when they exist, but its own comment notes "in pre-market/
    # after-hours, WebSocket is often idle" — and there the price comes from a
    # 30s Finnhub REST poll, which WINS the merge over the 5s Alpaca fallback.
    # At a 2.0s sample interval that is ~15 identical samples per real
    # observation, so a 10-block spark can be drawn from a single price.
    # A level tolerates being 30s stale; a SHAPE does not — the derivative is
    # exactly what coarse granularity destroys. Two of the three tranches
    # (07:00, 08:30) are pre-market, so the column is least trustworthy when
    # it would be used most.
    # Turn on only for regular-hours use, and see spark_min_distinct below.
    "spark_enabled": False,
    # 10 blocks ≈ 20s of tape at a 2.0s poll. The building/spiking/fading
    # distinction survives at half the width of the roadmap's 20, and the
    # column is the widest thing on the row.
    "spark_width": 10,
    # Below this many samples no spark is drawn: 3 blocks over 6s look exactly
    # like 3 blocks over 4 minutes.
    "spark_min_samples": 5,
    # Distinct price values required in the window before shape is drawn.
    # This is the self-check: the feed publishes no usable observation
    # timestamp (see below), so the only way to tell "the tape is moving" from
    # "I am sampling faster than the price updates" is to count how many
    # different prices are actually in the window. One or two distinct values
    # across ten samples means the picture would be the poll cadence, not the
    # tape — so draw nothing.
    "spark_min_distinct": 3,
    # Smallest peak-to-trough move (% of the window midpoint) allowed to draw
    # shape. Per-row min→max scaling fills the full height whatever the range,
    # so without this a stock ticking 10.00/10.01 draws the same violent
    # zigzag as one moving 30%. Set 0.0 for pure min→max.
    "spark_flat_pct": 0.1,
    # Mention acceleration arrow. The derivative is the signal, not the count.
    "mention_trend_enabled": True,
    # Floor on samples before an arrow is shown. 10 (not the roadmap's 8) so
    # that even without the server's published mention_alert_window the two
    # compared halves each span the shipped 10s window — see
    # mention_trend_floor(). Raised automatically when the server says its
    # window is wider.
    "mention_trend_min_samples": 10,
    "mention_trend_rise": 1.5,
    "mention_trend_fall": 0.6,
    # Beep when a symbol's arrow crosses into "↑↑" (recent half >= rise² over
    # the older half — 2.25x at the shipped 1.5).
    "alert_mention_flow": True,
    # …but only once the flow is at a level worth turning to look at.
    # mention_trend() calls ANY rise off a zero base "↑↑", so without a floor
    # every symbol going from no mentions to one would fire — which on this
    # feed is most of them, most of the time. This is the mean of the same
    # recent half the arrow compared, in mentions per server window. Set 0.0
    # to alert on every ↑↑; raise toward the engine's own "hot" bar
    # (PRIORITY_MENTIONS, 5) to only hear about real crowds.
    "mention_flow_alert_min": 2.0,
    # Stocktwits free trending (stocktwits.com/sentiment) — keys A-J.
    # Polled by the server's trending_screener.py and delivered in /api/state;
    # everything here is display tuning applied at render time.
    "stocktwits_enabled": True,
    "stocktwits_stocks_only": True,
    "stocktwits_max_price": 35.0,  # panel filter when price known; None = no filter
    "stocktwits_panel_limit": 10,  # max 10 → keys A-J
    "stocktwits_rvol_column": True,
    "stocktwits_avg_days": 10,     # sessions in the RVOL denominator
    "stocktwits_rvol_time_adjusted": True,
    "stocktwits_range_width": 11,  # 52w lo→hi track, cells (odd = true centre)
    # LOOK badge: heat + |%chg| + vol + 52w extreme (EXT near high / WASH near low)
    "stocktwits_look_min_abs_chg": 3.0,
    "stocktwits_look_max": 2,
    "stocktwits_look_near_high": 0.70,
    "stocktwits_look_near_low": 0.30,
    # Absolute volume floor for LOOK, on top of the panel-median test. None
    # keeps the median alone. Skipped for rows whose RVOL is unknown.
    "stocktwits_look_min_rvol": 1.5,
    # Claude suggestions panel — keys K-T. Same quote columns as ST.
    #
    # The research, the entries and the position management all run on the
    # server (claude_trader.py) and arrive in /api/state. The keys below are
    # display tuning only; the model, schedule, risk sizing and trading
    # switches live in config/bot_config.json next to the process that uses
    # them. Nothing here can place an order.
    "claude_enabled": False,
    "claude_panel_limit": 7,
    "claude_max_price": 100.0,  # panel filter: only show names under this
    # Show the desk's own Alpaca holdings + resting orders (manual B/S keys).
    # The Claude desk's book is reported separately by the server.
    "positions_panel_enabled": True,
    "positions_poll": 5.0,        # seconds between position/order refreshes
    # Adaptive limit + risk bracket + ratchet (OFF by default — paper wall).
    # Spec: docs/superpowers/specs/2026-08-04-entry-pricing-auto-limit-design.md
    "auto_limit_enabled": False,
    "auto_limit_live": False,          # must be true to auto-fire in live
    "auto_limit_signals": ["buy_zone"],
    "auto_limit_cooldown_sec": 900.0,
    "auto_limit_max_per_session": 3,
    "auto_limit_max_price": None,      # optional hard cap; else no max
    # Constructive tape gate (CM RSI / %R / MACD via signal_proximity)
    "auto_limit_require_constructive": True,
    "auto_limit_min_proximity_pct": 67.0,   # yellow+ buy completion
    "auto_limit_block_sell_signal": True,
    "auto_limit_block_pctr_falling": True,  # both %R lines falling → skip
    "auto_limit_require_buy_signal": False,  # optional stricter
    "entry_pad_max_pct": 0.15,
    "entry_max_spread_pct": 1.0,
    "buy_order_style": "auto",         # auto | limit_ask | market | policy
    # Tight stop + small size → compound BP
    "risk_pct": 0.35,                  # % of equity risked per trade
    "stop_pct": 0.40,                  # % below entry for initial stop
    "reward_r": 2.0,                   # TP distance in R
    "be_at_r": 1.0,                    # move stop to breakeven at +1R
    "lock_at_r": 2.0,                  # lock +1R at +2R unrealized
    "trail_pct": 0.0,                  # >0 swaps to broker trail after lock
    "max_notional": None,              # optional $ cap on entry notional
    "daily_loss_r": 2.0,               # halt auto when session R <= -this
    "daily_loss_halt_auto": True,
    "claude_rvol_column": True,
    "claude_avg_days": 10,
    "claude_rvol_time_adjusted": True,
    "claude_range_width": 11,
    "claude_look_min_abs_chg": 3.0,
    "claude_look_max": 2,
    "claude_look_near_high": 0.70,
    "claude_look_near_low": 0.30,
    "claude_look_min_rvol": 1.5,
    "alert_claude_new": True,
    "alert_claude_look": True,
}


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text()))
    except Exception:                                      # noqa: BLE001
        pass
    return cfg


# ── feed ─────────────────────────────────────────────────────────────────────

def _dashboard_url() -> str:
    return (wa.DASHBOARD_URL if wa else
            "https://trading.jbrasfield.com").rstrip("/")

_UA = (wa._UA if wa else
       "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def fetch_state() -> dict | None:
    """GET /api/state from the dashboard. Browser-like UA so Cloudflare
    doesn't 403 us; optional Bearer token when the server has auth on
    (managed by windows_agent — a no-op when auth is off, which is the
    default). All errors are swallowed so the Live display stays clean;
    the caller shows a 'feed down' banner instead."""
    headers = {"Accept": "application/json", "User-Agent": _UA}
    if wa:
        try:
            wa._ensure_token()
            headers.update(wa._auth_header())
        except Exception:                                  # noqa: BLE001
            pass
    url = _dashboard_url() + "/api/state"
    try:
        req = _UReq(url, headers=headers)
        with urlopen(req, timeout=6) as resp:
            return json.loads(resp.read().decode())
    except Exception:                                      # noqa: BLE001
        return None


def _signal_status(row: dict) -> str | None:
    sp = row.get("signal_proximity")
    return sp.get("status") if isinstance(sp, dict) else None


def _is_buy(row: dict) -> bool:
    return (_signal_status(row) or "").lower() in ("buy", "buy_zone")


def _sp(row: dict) -> dict:
    sp = row.get("signal_proximity")
    return sp if isinstance(sp, dict) else {}


def _fnum(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _cm_rsi_value(row: dict) -> float | None:
    """CM RSI-2 (preferred) or classic RSI from signal_proximity."""
    sp = _sp(row)
    for key in ("cm_rsi", "rsi"):
        n = _fnum(sp.get(key))
        if n is not None:
            return n
    return None


def _rsi_leg_ok(row: dict, max_lvl: float = 35.0) -> bool:
    """CM RSI green-long band: value in [0, max_lvl)."""
    rsi = _cm_rsi_value(row)
    return rsi is not None and 0.0 <= rsi < float(max_lvl)


def _pctr_leg_ok(row: dict,
                 lo: float = -100.0,
                 hi: float = -75.0) -> bool:
    """Both %R lines in [lo, hi] and falling toward -100.

    Prefers engine flag `pctr_deep_os`. Falls back to pctr + pctr_slow +
    falling flags when the flag is absent (older engine builds).
    """
    sp = _sp(row)
    if "pctr_deep_os" in sp:
        return bool(sp.get("pctr_deep_os"))

    fast = _fnum(sp.get("pctr"))
    slow = _fnum(sp.get("pctr_slow"))
    if fast is None or slow is None:
        return False
    in_band = (float(lo) <= fast <= float(hi)
               and float(lo) <= slow <= float(hi))
    falling = bool(sp.get("pctr_falling")) and bool(sp.get("pctr_slow_falling"))
    return in_band and falling


def rsi_focus_trigger(row: dict,
                      max_lvl: float = 35.0,
                      pctr_lo: float = -100.0,
                      pctr_hi: float = -75.0) -> tuple[float | None, bool]:
    """FOCUS when RSI leg AND %R deep-OS leg are both true.

    Returns (cm_rsi_or_None, is_focus). FOCUS requires:
      • CM RSI-2 in [0, max_lvl)
      • both %R lines in [pctr_lo, pctr_hi] trending toward -100
    """
    rsi = _cm_rsi_value(row)
    if rsi is None:
        return None, False
    hit = _rsi_leg_ok(row, max_lvl) and _pctr_leg_ok(row, pctr_lo, pctr_hi)
    return rsi, hit


def rsi_focus_empty_reason(row: dict) -> str:
    """Why the Setup cell is blank: 'untracked' | 'pending' | '' (has value)."""
    if not _sp(row):
        return "untracked"
    if _cm_rsi_value(row) is None:
        return "pending"
    return ""


def _pctr_pair(row: dict) -> tuple[float | None, float | None]:
    """(fast %R, slow %R) from signal_proximity."""
    sp = _sp(row)
    return _fnum(sp.get("pctr")), _fnum(sp.get("pctr_slow"))


def _setup_readout(rsi: float,
                   fast: float | None,
                   slow: float | None) -> str:
    """Compact combined readout: RSI · fast/slow %R (omit missing %R)."""
    # e.g. "3/−99/−77" or "17/−96" or "17"
    parts = [f"{rsi:.0f}"]
    if fast is not None and slow is not None:
        parts.append(f"{fast:.0f}/{slow:.0f}")
    elif fast is not None:
        parts.append(f"{fast:.0f}")
    elif slow is not None:
        parts.append(f"—/{slow:.0f}")
    return "·".join(parts) if len(parts) > 1 else parts[0]


def _focus_age_str(seconds: float | None) -> str:
    """M:SS since FOCUS lit — "0:14", "6:02", "" when not lit.

    Capped at "9:59+" so the Setup column cannot widen without bound on a
    setup that has been sitting all morning.
    """
    s = _fnum(seconds)
    if s is None or s < 0:
        return ""
    if s >= 600:
        return "9:59+"
    return f"{int(s) // 60}:{int(s) % 60:02d}"


def _focus_age_markup(seconds: float | None,
                      fresh_sec: float = 60.0,
                      stale_sec: float = 180.0) -> str:
    """Coloured age chip. A stale FOCUS should look stale."""
    txt = _focus_age_str(seconds)
    if not txt:
        return ""
    s = _fnum(seconds) or 0.0
    color = ("green" if s < float(fresh_sec)
             else "yellow" if s < float(stale_sec) else "dim")
    return f"[{color}]{txt}[/{color}]"


def _clamp01(v: float) -> float:
    return 0.0 if v < 0.0 else 1.0 if v > 1.0 else float(v)


def setup_distance(row: dict,
                   rsi_max: float = 35.0,
                   pctr_lo: float = -100.0,
                   pctr_hi: float = -75.0,
                   *,
                   proximity_weight: float = 0.35) -> float | None:
    """How far this row is from firing FOCUS.

    0.0 = firing now, 1.0 = far away, None = untracked or no CM RSI yet.

    A row that satisfies both legs returns exactly 0.0 and nothing can push
    it off zero. That matters because `proximity_pct` is the engine's
    *three*-indicator completion — it includes MACD, which FOCUS does not —
    so a genuinely firing setup with no MACD cross reports well under 100.
    Blending it into a live FOCUS would rank it behind near-misses.

    Non-firing rows are floored just above zero so a firing row always sorts
    ahead of every near-miss, including the case where the legs look in-band
    but the engine's `pctr_deep_os` / falling flags withhold the trigger.
    """
    sp = _sp(row)
    if not sp:
        return None
    rsi = _cm_rsi_value(row)
    if rsi is None:
        return None
    if rsi_focus_trigger(row, rsi_max, pctr_lo, pctr_hi)[1]:
        return 0.0

    hi = float(pctr_hi)
    lo = float(pctr_lo)
    legs: list[float] = []

    # RSI leg: how far above the green-long ceiling, over the range that
    # remains above it.
    if _rsi_leg_ok(row, rsi_max):
        legs.append(0.0)
    else:
        span = max(1e-9, 100.0 - float(rsi_max))
        legs.append(_clamp01((rsi - float(rsi_max)) / span))

    # %R leg: the worse of fast/slow, measured outside the deep-OS band.
    fast, slow = _pctr_pair(row)
    vals = [v for v in (fast, slow) if v is not None]
    if not vals:
        legs.append(1.0)
    elif _pctr_leg_ok(row, lo, hi):
        legs.append(0.0)
    else:
        span = max(1e-9, abs(hi))
        gaps = [(v - hi) / span if v > hi
                else (lo - v) / span if v < lo else 0.0 for v in vals]
        legs.append(_clamp01(max(gaps)))

    dist = sum(legs) / len(legs)

    # Blend in the engine's own completion number — do not ignore it.
    prox = _fnum(sp.get("proximity_pct"))
    if prox is not None:
        w = _clamp01(proximity_weight)
        dist = (1.0 - w) * dist + w * _clamp01(1.0 - prox / 100.0)

    # Strictly greater than a firing row's 0.0.
    return min(1.0, max(1e-3, dist))


def setup_shortfall(row: dict,
                    max_lvl: float = 35.0,
                    pctr_lo: float = -100.0,
                    pctr_hi: float = -75.0) -> str:
    """Which legs still have to move, and to where: 'rsi 38→35  %R -72→-75'.

    Only the failing legs appear. Empty string when both are satisfied.
    """
    bits = []
    rsi = _cm_rsi_value(row)
    if rsi is not None and not _rsi_leg_ok(row, max_lvl):
        bits.append(f"rsi {rsi:.0f}→{float(max_lvl):.0f}")
    if not _pctr_leg_ok(row, pctr_lo, pctr_hi):
        hi = float(pctr_hi)
        outside = [v for v in _pctr_pair(row) if v is not None and v > hi]
        if outside:
            bits.append(f"%R {max(outside):.0f}→{hi:.0f}")
    return "  ".join(bits)


# ── Buy-readiness circle ─────────────────────────────────────────────────────
# The three indicators on the chart — CM RSI-2, %R Trend Exhaustion, MACD — each
# publish one boolean in signal_proximity. Count the lit ones, colour the circle.
#
# This deliberately does NOT track the Setup column, and that is not a bug.
# FOCUS wants both %R lines deep-oversold and *falling* toward -100 ("catch it
# while it is still washing out"); the strategy's pctr_ok wants the fast line
# *rising* toward 0 ("the turn is confirmed"). Near-opposite readings of the
# same indicator, so they light at different moments on the same symbol.
# setup_distance() is the wrong basis here for the same reason its own docstring
# gives: it excludes MACD by design, and MACD is one of the three legs.

# (signal_proximity key, short name shown when the leg is dark)
CIRCLE_LEGS = (("cm_ok", "rsi"), ("pctr_ok", "%R"), ("macd_ok", "macd"))

# state → (glyph, rich style, fallback label)
CIRCLE_STYLES = {
    "go":      ("●", "bold green",   "BUY"),
    "near":    ("●", "bold yellow",  "NEAR"),
    "no":      ("●", "red",          "NO"),
    "hold":    ("◉", "bold cyan",    "HOLD"),
    "exit":    ("●", "bold magenta", "EXIT"),
    "unknown": ("○", "dim",          "—"),
}


def buy_circle(row: dict | None, cfg: dict | None = None) -> tuple[str, str]:
    """Buy-readiness of one symbol as (state, detail).

    state ∈ go | near | no | hold | exit | unknown.

    `unknown` is a fourth state on purpose. Red means we measured the setup and
    it is not there; dim means we could not measure it. Rendering absence as red
    would invent a verdict nobody computed — the rule focus_age() already
    follows for ages it did not observe.
    """
    cfg = cfg or {}
    sp = _sp(row) if isinstance(row, dict) else {}

    # Strategy guard. proximity_state() returns three different shapes sharing
    # these key names: under STRATEGY_MODE=alert, proximity_pct is *mention
    # velocity* and status is still watching/aligning/buy_zone. Reading those
    # blindly would glow green because a symbol is being talked about, with no
    # indicator basis whatsoever.
    # Three distinct reasons to stay dim, and they need different fixes:
    # untracked = the engine has no row for this symbol (chart something on the
    # desk list, or add it); wrong mode = the engine is not running the
    # three-indicator strategy; pending = tracked, bars still warming up.
    if not sp:
        return "unknown", "untracked"
    if sp.get("strategy") != "three_indicator":
        return "unknown", "wrong mode"
    if not sp.get("bars_fetched") or sp.get("cm_rsi") is None:
        return "unknown", "pending"

    # Already holding it — "is this a good time to buy" is the wrong question,
    # so it gets its own colour rather than a misleading red or green.
    if sp.get("status") == "exit_signal":
        return "exit", ""
    if sp.get("in_position"):
        return "hold", ""

    lit = [name for key, name in CIRCLE_LEGS if sp.get(key)]
    missing = [name for key, name in CIRCLE_LEGS if not sp.get(key)]
    detail = f"{len(lit)}/{len(CIRCLE_LEGS)}"
    if missing:
        detail += " " + " ".join(missing)

    pct = _fnum(sp.get("proximity_pct"))
    pct = 0.0 if pct is None else pct
    green = _fnum(cfg.get("buy_circle_green_min"))
    green = 100.0 if green is None else green
    yellow = _fnum(cfg.get("buy_circle_yellow_min"))
    yellow = 67.0 if yellow is None else yellow

    # buy_zone is the engine's own buy_signal(), which additionally requires the
    # legs to align within confirm_window. Honour it directly so the desk can
    # never read non-green while the dashboard reads BUY ZONE for the same row.
    if sp.get("status") == "buy_zone" or pct >= green:
        return "go", detail
    if pct >= yellow:
        return "near", detail
    return "no", detail


# Direction-of-travel glyphs for the corner, mirroring tv_chart_feed's verdict.
# Kept here rather than imported so the desk still renders when the screen
# reader is unavailable and the circle falls back to the engine.
TREND_STYLES = {
    "surging":  ("⇈", "bold green"),
    "rising":   ("↗", "green"),
    "conflict": ("⇄", "yellow"),      # the two indicators disagree
    "flat":     ("→", "dim"),         # nothing moving
    "falling":  ("↘", "red"),
    "sinking":  ("⇊", "bold red"),
    "unknown":  ("·", "dim"),
}


def trend_markup(trend: str | None, sym: str | None) -> str | None:
    """The corner chip: 'ZCMD ⇈'.

    Symbol and arrow, nothing else. The leg count used to ride along here, but
    two verdicts side by side is one more than the corner can carry — the
    readout strip underneath already gives R / %R / MACD with their own
    arrows, which is the same evidence in a form you can actually check.

    Returns None when there is no trend to show, so the caller can fall back
    to the dot rather than print a meaningless arrow.
    """
    if not trend or trend == "unknown":
        return None
    glyph, style = TREND_STYLES.get(trend, TREND_STYLES["unknown"])
    return f"[{style}]{sym or '—'} {glyph}[/{style}]"


def circle_markup(state: str, detail: str, sym: str | None) -> str:
    """The corner chip: 'ZCMD 2/3 macd ●', 'ZCMD pending ○'.

    The glyph goes LAST so it is the rightmost thing on the header's top
    border — the light is the whole point, and it should be where the eye
    lands rather than trailing a string of leg names.
    """
    glyph, style, label = CIRCLE_STYLES.get(state, CIRCLE_STYLES["unknown"])
    return f"[{style}]{sym or '—'} {detail or label} {glyph}[/{style}]"


class ChartSymbol:
    """The symbol on the TradingView chart, cached behind a TTL.

    tv_focus_symbol() spawns an osascript subprocess to read the browser tab
    title, so it must not run on every repaint. Falls back to the hotkey focus —
    the last symbol *we* sent to TV — when the read comes back empty, which
    covers non-Mac, no TV tab, and AppleScript errors alike (it swallows all
    three to None). The tab title is preferred because it is the only source
    that notices you changing the symbol in TradingView by hand.
    """

    def __init__(self, cfg: dict | None = None):
        cfg = cfg or {}
        self.source = str(cfg.get("buy_circle_symbol_source", "chart")).lower()
        self.ttl = float(cfg.get("buy_circle_chart_poll_sec", 2.0))
        self._sym: str | None = None
        self._read_at = 0.0

    def get(self, hotkeys, now: float) -> str | None:
        fallback = hotkeys.focus_symbol() if hotkeys is not None else None
        if self.source != "chart":
            return fallback
        if now - self._read_at >= self.ttl:
            self._read_at = now
            try:
                self._sym = desk.tv_focus_symbol()
            except Exception:                                  # noqa: BLE001
                self._sym = None
        return self._sym or fallback


class ChartWatcher:
    """Reads the TradingView window on its own thread and publishes the result.

    Capture plus a tesseract axis pass costs far more than the desk's 2s poll
    can absorb inline, and it has nothing to do with the dashboard feed, so it
    runs alongside and hands over whatever it last saw. The render loop never
    blocks on a screengrab.

    Publishes three things under one lock: a signal_proximity-shaped dict for
    buy_circle(), the R/%/M readout lines, and the charted symbol. All three
    go None together when the feed goes quiet — a half-updated readout beside
    a live circle would be worse than no readout.
    """

    def __init__(self, cfg: dict):
        self.interval = float(cfg.get("buy_circle_chart_poll_sec", 1.0))
        self._feed = chart_feed.ChartFeed() if chart_feed else None
        self._lock = threading.Lock()
        self._prox: dict | None = None
        self._readout: list[str] | None = None
        self._symbol: str | None = None
        # Seeded so the first second on screen — before the opening capture
        # and tesseract pass land — reads as "warming up" rather than as a
        # failure the user might go chasing.
        self._error: str | None = "starting"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def available(self) -> bool:
        return self._feed is not None

    def start(self) -> None:
        if not self.available or self._thread:
            return
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="chart-ocr")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._feed.poll()
                prox = self._feed.proximity()
                readout = self._feed.readout()
                sym = self._feed.symbol
                err = self._feed.last_error
            except Exception as e:                         # noqa: BLE001
                # A screengrab failure must never take the desk down with it.
                prox = readout = sym = None
                err = f"{type(e).__name__}: {e}"
            with self._lock:
                self._prox, self._readout = prox, readout
                self._symbol, self._error = sym, err
            self._stop.wait(self.interval)

    def snapshot(self) -> tuple[dict | None, list[str] | None, str | None]:
        with self._lock:
            return self._prox, self._readout, self._symbol

    def error(self) -> str | None:
        with self._lock:
            return self._error


def chart_readout_markup(lines: list[str] | None,
                         source: str = "chart") -> str | None:
    """The R / % / M readout as one strip for a panel border, source tagged.

    The tag is not decoration. Twice now a desk silently running on the engine
    has been debugged as though it were reading the screen — once chasing
    "pending" that came from bars, once an impossible MACD gap — because
    nothing on screen said which source produced the numbers. A fallback is
    invisible precisely when it matters, since both sources render values that
    look equally plausible.
    """
    if not lines:
        return None
    tag = "cyan" if source == "chart" else "yellow"
    return f"[{tag}]{source}[/{tag}][dim]  " + "   ".join(lines) + "[/dim]"


def engine_readout_markup(row: dict | None) -> str | None:
    """The engine's own three legs, so the fallback still shows its working.

    Previously the strip simply vanished on the engine path, which reads as
    "no data" rather than "different data" — and left the corner glyph as the
    only thing on screen, with nothing to say where it came from.
    """
    sp = _sp(row) if isinstance(row, dict) else {}
    if not sp:
        return None
    rsi = _fnum(sp.get("cm_rsi"))
    fast, slow = _pctr_pair(row) if isinstance(row, dict) else (None, None)
    bits = [f"R - {rsi:.0f}" if rsi is not None else "R - —"]
    if fast is not None or slow is not None:
        bits.append("% - " + ", ".join(f"{v:.0f}" for v in (fast, slow)
                                       if v is not None))
    else:
        bits.append("% - —")
    bits.append("M - " + ("✓" if sp.get("macd_ok") else "—"))
    return "[yellow]engine[/yellow][dim]  " + "   ".join(bits) + "[/dim]"


def mention_trend(hist_series: list[float],
                  engine_velocity: float | None = None,
                  rise: float = 1.5,
                  fall: float = 0.6,
                  min_samples: int = 8) -> str:
    """Direction of mention flow: "↑↑" | "↑" | "→" | "↓" | "".

    Compares the mean of the recent half of the window against the older
    half. Returns "" below `min_samples` — an arrow computed off two points
    is noise, not information.

    On `engine_velocity`: the engine's `mention_velocity` is a *level* (a
    count inside its rolling window), not a rate, so it carries no direction
    on its own and cannot "take precedence" over a computed trend. What it
    can do is contribute the freshest observation. The caller therefore picks
    ONE scale per call — pass the mention_velocity series together with the
    live engine value, or the mention_window series with None — and this
    function appends the live value as the newest sample. Mixing the two
    scales in one series would fabricate a jump.
    """
    series = [v for v in (hist_series or []) if v is not None]
    ev = _fnum(engine_velocity)
    if ev is not None:
        series = [*series, ev]
    if len(series) < max(2, int(min_samples)):
        return ""

    half = len(series) // 2
    older, recent = series[:half], series[half:]
    o = sum(older) / len(older)
    r = sum(recent) / len(recent)

    if o <= 0:
        # Coming off zero is a rise only if there is something there now.
        return "↑↑" if r > 0 else "→"
    ratio = r / o
    strong = float(rise) * float(rise)      # 1.5 -> 2.25x is "↑↑"
    if ratio >= strong:
        return "↑↑"
    if ratio >= float(rise):
        return "↑"
    if ratio <= float(fall):
        return "↓"
    return "→"


def mention_trend_floor(cfg: dict, server_cfg: dict | None = None) -> int:
    """Smallest sample count at which the trend arrow means anything.

    Each sample is a count over the server's *trailing* window
    (`mention_alert_window`, 10s by default). One mention therefore shows up
    in every sample taken during the following 10 seconds. If a compared half
    spans less than that window, the same mention lands in both halves and the
    "derivative" is measuring one event twice — a rise that isn't there.

    So require each half to span at least a full window:
        half_span = (n / 2) * poll_interval >= mention_window
    At the shipped 2.0s poll and 10s window that is 10 samples, not the
    roadmap's 8. Falls back to the configured value when the server does not
    publish its window.
    """
    want = int(cfg.get("mention_trend_min_samples", 8))
    poll = _fnum(cfg.get("poll_interval")) or 2.0
    win = _fnum((server_cfg or {}).get("mention_alert_window"))
    if win is None or win <= 0 or poll <= 0:
        return max(2, want)
    return max(2, want, math.ceil(2.0 * win / poll))


def _mention_trend_color(arrow: str) -> str:
    if arrow.startswith("↑"):
        return "green"
    if arrow.startswith("↓"):
        return "red"
    return "dim"


def _rsi_focus_cell(row: dict,
                    max_lvl: float = 35.0,
                    pctr_lo: float = -100.0,
                    pctr_hi: float = -75.0,
                    *,
                    age: float | None = None,
                    near: str | None = None,
                    fresh_sec: float = 60.0,
                    stale_sec: float = 180.0) -> str:
    """Rich markup for the Setup column (combined CM RSI + %R cue).

    Empty:
      —   engine not tracking
      …   tracked, waiting on bars / CM RSI
    Partial (no FOCUS):
      dim  17·−96/−40   RSI + both %R (full setup not ready)
      dim  17           RSI only (%R not published yet)
    Full setup:
      FOCUS  3·−99/−77  both legs true — number is RSI·fast/slow %R
      FOCUS 0:14  3·−99/−77   with `age` (T1.1)
    Near miss (`near` supplied by the caller, T1.2):
      NEAR  rsi 38→35  %R −72→−75

    `age` and `near` are opt-in parameters, not config lookups: the caller
    decides whether the features are on. Omitting both renders exactly what
    this cell has always rendered.
    """
    rsi, hit = rsi_focus_trigger(row, max_lvl, pctr_lo, pctr_hi)
    if rsi is None:
        reason = rsi_focus_empty_reason(row)
        if reason == "pending":
            return "[dim]…[/dim]"
        return "[dim]—[/dim]"
    fast, slow = _pctr_pair(row)
    readout = _setup_readout(rsi, fast, slow)
    if hit:
        chip = _focus_age_markup(age, fresh_sec, stale_sec)
        lead = "[bold black on green] FOCUS [/]"
        if chip:
            lead += f" {chip}"
        return f"{lead} [bold green]{readout}[/bold green]"
    if near:
        return (f"[bold yellow]NEAR[/bold yellow] "
                f"[yellow]{near}[/yellow]")
    return f"[dim]{readout}[/dim]"


# ── alerting ─────────────────────────────────────────────────────────────────

# macOS ships these in /System/Library/Sounds. Chosen by how often each alert
# actually fires: the frequent ones are brief and soft so a busy pre-market does
# not turn into an alarm, and only the two you would stop and look at get
# something assertive. Basso/Funk/Sosumi/Frog are deliberately unused — they are
# the jarring end of the set, and Basso is macOS's error sound.
ALERT_SOUNDS = {
    "new":      "Tink",        # a symbol appeared — very frequent, soft click
    "st_new":   "Pop",         # new on the Stocktwits panel — also frequent
    "claude_new": "Pop",         # new on the Claude panel
    "mflow":    "Bottle",      # mention flow building — hollow, mellow
    "burst":    "Submarine",   # mention burst — deep, calm, carries
    "st_look":  "Glass",       # LOOK badge — bright chime
    "claude_look": "Glass",      # LOOK badge on a Claude row
    "focus":    "Hero",        # the FOCUS setup fired — rare, worth looking up
    "buy":      "Hero",
}
DEFAULT_ALERT_SOUND = "Submarine"

_SOUND_DIR = "/System/Library/Sounds"
_last_sound_at = 0.0
_sound_lock = threading.Lock()


def sound_for(kind: str, cfg: dict | None = None) -> str:
    """Sound name for an alert kind. `alert_sound_by_kind` overrides per kind,
    `alert_sound_name` overrides the fallback."""
    cfg = cfg or {}
    overrides = cfg.get("alert_sound_by_kind") or {}
    if isinstance(overrides, dict) and kind in overrides:
        return str(overrides[kind])
    if kind in ALERT_SOUNDS:
        return ALERT_SOUNDS[kind]
    return str(cfg.get("alert_sound_name", DEFAULT_ALERT_SOUND))


def _play(path: str, volume: float) -> None:
    """Play one file and reap the process. Runs on a throwaway thread."""
    try:
        subprocess.run(["afplay", "-v", f"{volume:.2f}", path],
                       check=False, timeout=10,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:                                      # noqa: BLE001
        pass


def _beep(kind: str = "", cfg: dict | None = None) -> None:
    """Alert sound, per kind, non-blocking.

    This used to be Windows-only, so on macOS the desk was silent apart from the
    notification banner — which is throttled to one per alert_notify_interval
    (180s) and skipped entirely while the terminal is frontmost. In practice
    that meant no sound at all on the machine it runs on.

    Playback happens on a short-lived thread: afplay takes ~0.5-1.5s to finish
    and the render loop must not wait for it. The thread (rather than a bare
    Popen) is what reaps the child, so a long session does not accumulate
    zombies.

    A global minimum gap applies ON TOP of the Alerter's per-symbol cooldown.
    That cooldown is per (kind, symbol), so twenty different symbols bursting at
    the open would each be entitled to fire at once and the sounds would overlap
    into noise rather than telling you anything.
    """
    cfg = cfg or {}
    if not cfg.get("alert_sound", True):
        return

    global _last_sound_at
    now = time.time()
    gap = float(cfg.get("alert_sound_min_gap", 1.5))
    with _sound_lock:
        if now - _last_sound_at < gap:
            return
        _last_sound_at = now

    if sys.platform == "win32":
        try:
            import winsound
            # PlaySound with a system alias, not Beep(880, 180): Beep is a raw
            # square wave through the motherboard timer and is genuinely harsh.
            winsound.PlaySound("SystemAsterisk",
                               winsound.SND_ALIAS | winsound.SND_ASYNC)
        except Exception:                                  # noqa: BLE001
            pass
        return

    if sys.platform != "darwin":
        return

    name = sound_for(kind, cfg)
    path = os.path.join(_SOUND_DIR, f"{name}.aiff")
    if not os.path.exists(path):
        path = os.path.join(_SOUND_DIR, f"{DEFAULT_ALERT_SOUND}.aiff")
        if not os.path.exists(path):
            return
    try:
        volume = max(0.0, min(1.0, float(cfg.get("alert_sound_volume", 0.35))))
    except (TypeError, ValueError):
        volume = 0.35
    threading.Thread(target=_play, args=(path, volume), daemon=True).start()


# Maps $TERM_PROGRAM (set by the terminal running this script) to the process
# name macOS System Events reports for that app's frontmost check, and to the
# bundle id terminal-notifier needs to activate that app on notification click.
_TERM_APP_NAMES = {
    "Apple_Terminal": "Terminal",
    "iTerm.app": "iTerm2",
}
_TERM_BUNDLE_IDS = {
    "Apple_Terminal": "com.apple.Terminal",
    "iTerm.app": "com.googlecode.iterm2",
}
_NOTIFY_GROUP = "brasfield-momentum"


def _macos_notify(title: str, message: str, sound: bool = True,
                   auto_dismiss: float = 5.0, sound_name: str = "") -> None:
    """Notification Center banner. Prefers `terminal-notifier` (brew) so
    clicking the banner brings the monitor's terminal to the front via
    `-activate`; plain `osascript display notification` can't do that — Apple
    doesn't expose a click action to scripts, only to full app bundles.
    Falls back to osascript (no click action) if terminal-notifier is absent.

    `auto_dismiss` force-removes the banner after N seconds via a background
    timer + `-remove`, regardless of the system's Banners/Alerts style —
    macOS's own auto-hide timing isn't user- or script-configurable, so this
    is the only way to guarantee a specific duration."""
    if sys.platform != "darwin":
        return

    tn = shutil.which("terminal-notifier")
    if tn:
        cmd = [tn, "-title", title, "-message", message,
               "-group", _NOTIFY_GROUP]
        if sound:
            # Name the sound rather than taking "default": the system default
            # alert sound is whatever is set in System Settings, so the banner
            # and the desk's own alert would disagree about what an event
            # sounds like.
            cmd += ["-sound", sound_name or DEFAULT_ALERT_SOUND]
        bundle_id = _TERM_BUNDLE_IDS.get(os.environ.get("TERM_PROGRAM", ""))
        if bundle_id:
            cmd += ["-activate", bundle_id]
        try:
            subprocess.run(cmd, check=False, timeout=5,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if auto_dismiss and auto_dismiss > 0:
                def _dismiss():
                    subprocess.run([tn, "-remove", _NOTIFY_GROUP], check=False,
                                    timeout=5, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
                timer = threading.Timer(auto_dismiss, _dismiss)
                timer.daemon = True
                timer.start()
            return
        except Exception:                                  # noqa: BLE001
            pass  # fall through to osascript

    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')

    script = f'display notification "{esc(message)}" with title "{esc(title)}"'
    if sound:
        script += f' sound name "{esc(sound_name or DEFAULT_ALERT_SOUND)}"'
    try:
        subprocess.run(["osascript", "-e", script], check=False, timeout=5,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:                                      # noqa: BLE001
        pass


def _monitor_visible() -> bool:
    """True if the terminal app running this monitor is currently the
    frontmost (focused) app — i.e. the user is already looking at it, so a
    notification would be redundant. False (assume hidden) if we can't tell,
    so notifications fail open rather than going silent."""
    if sys.platform != "darwin":
        return False
    app_name = _TERM_APP_NAMES.get(os.environ.get("TERM_PROGRAM", ""))
    if not app_name:
        return False
    script = ('tell application "System Events" to get name of '
              'first application process whose frontmost is true')
    try:
        out = subprocess.run(["osascript", "-e", script], check=False,
                              timeout=5, capture_output=True, text=True)
        return out.stdout.strip() == app_name
    except Exception:                                      # noqa: BLE001
        return False


class Alerter:
    """Beep + optional desktop toast, with a per-symbol, per-kind cooldown so
    a symbol that keeps re-bursting doesn't machine-gun the speaker, plus a
    separate global throttle on OS notifications so a burst of different
    symbols/kinds still caps out at one popup per `alert_notify_interval`."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.cooldown = float(cfg.get("alert_cooldown", 60.0))
        self.toast = bool(cfg.get("desktop_toast", True))
        self.notify_interval = float(cfg.get("alert_notify_interval", 180.0))
        self.notify_duration = float(cfg.get("alert_notify_duration", 5.0))
        self.only_when_hidden = bool(cfg.get("alert_only_when_hidden", True))
        self._last_notify = 0.0
        self._last: dict[tuple[str, str], float] = {}
        self.recent: list[str] = []          # short on-screen alert log

    def fire(self, kind: str, sym: str, detail: str = ""):
        now = time.time()
        key = (kind, sym)
        if now - self._last.get(key, 0.0) < self.cooldown:
            return
        self._last[key] = now
        _beep(kind, self.cfg)
        msg = {"new": f"NEW {sym}", "burst": f"BURST {sym}",
               "buy": f"BUY {sym}", "st_new": f"NEW-ST {sym}",
               "st_look": f"LOOK {sym}",
               "claude_new": f"NEW-CLAUDE {sym}",
               "claude_look": f"LOOK-CLAUDE {sym}",
               "mflow": f"FLOW {sym}"}.get(kind, f"{kind} {sym}")
        if detail:
            msg += f"  {detail}"
        self.recent.insert(0, f"{datetime.now():%H:%M:%S}  {msg}")
        del self.recent[6:]
        if (self.toast and (now - self._last_notify) >= self.notify_interval
                and not (self.only_when_hidden and _monitor_visible())):
            self._last_notify = now
            if sys.platform == "darwin":
                # Silent banner: _beep above already played this event's sound,
                # and the banner's own would land on top of it as a double
                # strike for one alert. _beep is the single audio path, so the
                # volume and min-gap settings actually govern everything you
                # hear.
                _macos_notify(f"Momentum · {msg.split()[0]} {sym}",
                               detail or "momentum alert",
                               sound=False,
                               auto_dismiss=self.notify_duration)
            elif _plyer is not None:
                try:
                    _plyer.notify(title=f"Momentum · {msg.split()[0]} {sym}",
                                  message=detail or "momentum alert",
                                  timeout=6)
                except Exception:                          # noqa: BLE001
                    pass


# ── model ────────────────────────────────────────────────────────────────────

def row_rank(row: dict, first_seen: float, now: float,
             server_idx: int, cfg: dict) -> tuple:
    """Sort key for one momentum row — lower tuple sorts higher on screen.

    Reproduces the desk's long-standing order exactly: most recently
    first-seen symbol on top, with the server's mention-rank order (the
    index the row arrived at in /api/state) breaking ties.

    NOTE on `new_ttl`: freshness does **not** decay out of the ordering.
    A symbol first seen at 09:31 outranks one first seen at 09:30 for the
    rest of the session, however old both get. `new_ttl` governs only the
    NEW badge (`Feed.is_fresh`), never position. Anything that makes the
    freshness term expire is a behavior change, not a refactor.

    With `setup_sort_enabled` (default off) a setup-distance term is
    inserted *beneath* freshness but above server order, so it reorders only
    symbols that were first seen in the same poll — which is every symbol on
    the opening snapshot, and none thereafter. Appending it after
    `server_idx` instead would be dead weight: server_idx is unique per row,
    so nothing downstream of it can ever break a tie.

    `now` is unused today; it is the seam T5.2 plugs into.
    """
    if cfg and cfg.get("setup_sort_enabled"):
        try:
            d = setup_distance(
                row,
                float(cfg.get("rsi_focus_max", 35.0)),
                float(cfg.get("pctr_focus_lo", -100.0)),
                float(cfg.get("pctr_focus_hi", -75.0)),
                proximity_weight=float(cfg.get("setup_proximity_weight",
                                               0.35)),
            )
        except Exception:                                  # noqa: BLE001
            d = None
        # Untracked rows carry no setup information — park them with the
        # far-away rows rather than ahead of a measured near-miss.
        return (-first_seen, 1.0 if d is None else d, server_idx)
    return (-first_seen, server_idx)


class Feed:
    """Turns successive /api/state snapshots into a display-ordered list and
    the new-symbol / burst rising edges that drive alerts. Newest symbols
    ride on top for `new_ttl` seconds, then settle into the server's
    mention-rank order."""

    def __init__(self, cfg: dict):
        self.new_ttl = float(cfg.get("new_ttl", 120.0))
        self.first_seen: dict[str, float] = {}
        self.prev_burst: dict[str, bool] = {}
        self.prev_buy: dict[str, bool] = {}
        # Latch for the Mentions "↑↑" alert. Holds the *alertable* condition
        # (arrow AND level), not the raw arrow: a symbol that sat at "↑↑" on a
        # trickle and then genuinely took off has to be able to fire.
        self.prev_mention_flow: dict[str, bool] = {}
        # When FOCUS lit, per symbol currently in it. Absent = not lit.
        # A None value means "lit, but we never saw it fire" — a symbol that
        # was already in FOCUS on our first poll. We do not know its age, so
        # we must not claim one (see ingest()).
        self.focus_since: dict[str, float | None] = {}
        # Server-published config from /api/state, used to size derived
        # windows against the server's own (e.g. mention_alert_window).
        self.server_cfg: dict = {}
        # Rising edges detected on the most recent ingest, as (kind, row).
        # Deliberately independent of the alert_* flags: `alert_buy` defaults
        # false, so journaling off the alerter's call sites would silently
        # never record a buy — the most valuable record there is.
        self.edges: list[tuple[str, dict]] = []
        self.seeded = False                # suppress alerts on the first poll
        self.rows: list[dict] = []
        self.last_ok = 0.0

    def ingest(self, state: dict, now: float, alerter: Alerter, cfg: dict):
        rows = list(state.get("tickers") or [])
        self.last_ok = now
        if isinstance(state.get("config"), dict):
            self.server_cfg = state["config"]
        rsi_max = float(cfg.get("rsi_focus_max", 35.0))
        p_lo = float(cfg.get("pctr_focus_lo", -100.0))
        p_hi = float(cfg.get("pctr_focus_hi", -75.0))
        live: set[str] = set()
        edges: list[tuple[str, dict]] = []
        for r in rows:
            sym = str(r.get("ticker") or "").upper()
            if not sym:
                continue
            live.add(sym)
            # FOCUS rising edge. setdefault keeps the original stamp while
            # the setup stays lit; dropping out clears it so a re-fire
            # restarts the clock rather than resuming it.
            #
            # On the seeding poll we have not observed an edge — the setup may
            # have been lit for ten minutes before the monitor started. Record
            # None so the age renders blank instead of a fresh-looking 0:00.
            # It stays None until the setup actually drops and re-fires.
            was_lit = sym in self.focus_since
            if rsi_focus_trigger(r, rsi_max, p_lo, p_hi)[1]:
                self.focus_since.setdefault(sym, now if self.seeded else None)
                # Only a transition we actually observed is an edge. A setup
                # already lit on the seeding poll fired before we were
                # watching, so recording it at `now` would date it wrongly.
                if self.seeded and not was_lit:
                    edges.append(("focus", r))
            else:
                self.focus_since.pop(sym, None)
            burst = bool(r.get("mention_burst"))
            buy = _is_buy(r)
            is_new = sym not in self.first_seen
            if is_new:
                self.first_seen[sym] = now
            if self.seeded:
                if is_new:
                    edges.append(("new", r))
                    if cfg.get("alert_new"):
                        alerter.fire("new", sym, _detail(r))
                if burst and not self.prev_burst.get(sym):
                    edges.append(("burst", r))
                    if cfg.get("alert_burst"):
                        alerter.fire("burst", sym,
                                     f"{r.get('mention_window', '?')} in window")
                if buy and not self.prev_buy.get(sym):
                    edges.append(("buy", r))
                    if cfg.get("alert_buy"):
                        alerter.fire("buy", sym, _detail(r))
            self.prev_burst[sym] = burst
            self.prev_buy[sym] = buy

        # A symbol that left the feed must not resume its old FOCUS clock
        # when it comes back.
        for gone in [s for s in self.focus_since if s not in live]:
            del self.focus_since[gone]

        # newest first_seen on top; server order (mention rank) breaks ties
        # via stable sort. Symbols no longer in the feed drop off the list.
        order = {(r.get("ticker") or "").upper(): i for i, r in enumerate(rows)}

        def _key(r: dict) -> tuple:
            sym = (r.get("ticker") or "").upper()
            return row_rank(r, self.first_seen.get(sym, 0.0), now,
                            order.get(sym, 999), cfg)

        rows.sort(key=_key)
        self.rows = rows
        self.edges = edges
        self.seeded = True

    def detect_mention_flow(self, history, now: float, alerter,
                            cfg: dict) -> None:
        """Rising edge into "↑↑" on the Mentions arrow, per symbol.

        Deliberately not part of ingest(): the arrow is a function of the
        sample ring, and ingest() runs before this poll's sample is pushed
        into it. The render loop calls this straight after push_history(), so
        the arrow tested here is the one about to be drawn.

        It is read via _mention_arrow() — the same function the cell renders,
        not a reimplementation of it — so the beep and the screen cannot
        drift apart. Edges land in self.edges alongside new/burst/buy/focus so
        the journal records them like any other.

        No seeding guard is needed. `history` is memory-only and starts empty,
        so the arrow is "" until the sample floor is met; there is no "already
        rising before we were watching" case to suppress.
        """
        if history is None or not cfg.get("mention_trend_enabled", True):
            return
        floor = _fnum(cfg.get("mention_flow_alert_min", 2.0)) or 0.0
        live: set[str] = set()
        for r in self.rows:
            sym = str(r.get("ticker") or "").upper()
            if not sym:
                continue
            live.add(sym)
            ctx = {"cfg": cfg, "history": history, "sym": sym, "feed": self}
            # Level is only consulted for the arrow that can alert, so the
            # common row costs one arrow and nothing else.
            level = (mention_flow_level(_mention_series(r, ctx))
                     if _mention_arrow(r, ctx) == "↑↑" else None)
            hot = level is not None and level >= floor
            was_hot = self.prev_mention_flow.get(sym, False)
            self.prev_mention_flow[sym] = hot
            if not hot or was_hot:
                continue
            self.edges.append(("mflow", r))
            if cfg.get("alert_mention_flow", True):
                detail = _detail(r)
                alerter.fire("mflow", sym,
                             f"{level:.0f}/window  {detail}".rstrip())

        # A symbol that leaves the feed loses its history to prune(), so
        # holding its old latch would suppress the re-fire when it returns.
        for gone in [s for s in self.prev_mention_flow if s not in live]:
            del self.prev_mention_flow[gone]

    def is_fresh(self, sym: str, now: float) -> bool:
        return now - self.first_seen.get(sym, 0.0) <= self.new_ttl

    def in_focus(self, sym: str) -> bool:
        """True if FOCUS is lit, regardless of whether we know since when."""
        return sym in self.focus_since

    def focus_age(self, sym: str, now: float) -> float | None:
        """Seconds since FOCUS lit, or None if not lit *or* age unknown.

        Both cases render blank, which is the point: we never display an age
        we did not measure. Use in_focus() to distinguish them.
        """
        since = self.focus_since.get(sym)
        return None if since is None else max(0.0, now - since)

    def row_for(self, sym: str | None) -> dict | None:
        """Latest row for `sym`, or None when the engine is not tracking it."""
        if not sym:
            return None
        want = str(sym).upper()
        for r in self.rows:
            if str(r.get("ticker") or "").upper() == want:
                return r
        return None


def _detail(row: dict) -> str:
    px = row.get("price")
    chg = row.get("pct_change")
    bits = []
    if px is not None:
        bits.append(f"{px:.2f}")
    if chg is not None:
        bits.append(f"{chg:+.1f}%")
    return "  ".join(bits)


def push_history(history: SymbolHistory, rows: list[dict],
                 now: float) -> None:
    """Record one sample per live row, then evict symbols off the feed.

    Call this only on a poll that actually returned state. Re-pushing the
    previous rows during a feed outage would manufacture flat tape, which
    reads as "price stopped moving" rather than "we lost the feed" — the
    exact confusion a sparkline is supposed to prevent.
    """
    live: set[str] = set()
    for r in rows:
        sym = str(r.get("ticker") or "").upper()
        if not sym:
            continue
        live.add(sym)
        with contextlib.suppress(Exception):
            history.push(sym, now,
                         price=r.get("price"),
                         mention_window=r.get("mention_window"),
                         mention_velocity=_sp(r).get("mention_velocity"))
    with contextlib.suppress(Exception):
        history.prune(live)


# ── render ───────────────────────────────────────────────────────────────────

def _fmt(v, spec, none="—"):
    return format(v, spec) if v is not None else none


# ── momentum table column spec ───────────────────────────────────────────────
# The table is built from an ordered (header, add_column kwargs, cell builder)
# list so later tickets append a column instead of re-editing the render loop.
# Each builder takes (row, ctx); `ctx` is the per-row render context assembled
# once in momentum_table(). Builders must be pure — no I/O, no mutation.

def _cell_key(row: dict, ctx: dict) -> str:
    i = ctx["idx"]
    return str(i + 1) if (ctx["hotkeys_on"] and i < 9) else ""


def _cell_symbol(row: dict, ctx: dict) -> str:
    return f"[bold cyan]{ctx['sym']}[/bold cyan]"


def _cell_added(row: dict, ctx: dict) -> str:
    seen = ctx["feed"].first_seen.get(ctx["sym"])
    added = (f"{datetime.fromtimestamp(seen):%H:%M:%S}"
             if seen is not None else "—")
    return f"[dim]{added}[/dim]"


def _cell_price(row: dict, ctx: dict) -> str:
    """Price, dimmed with its age when the print is no longer current.

    The number itself is never withheld — a 40s-old price is still roughly
    right and is what you have. But it must not read as live, because on this
    desk the difference decides whether you hit a bid.
    """
    txt = _fmt(row.get("price"), ".2f")
    cfg = ctx.get("cfg") or {}
    if txt == "—" or not cfg.get("price_age_enabled", True):
        return txt
    age = _fnum(row.get("price_age_sec"))
    if age is None or age < float(cfg.get("price_stale_sec", 20.0)):
        return txt
    return f"[dim]{txt}·{age:.0f}s[/dim]"


def _cell_chg(row: dict, ctx: dict) -> str:
    chg = row.get("pct_change")
    if chg is None:
        return "—"
    cc = "green" if chg > 0 else "red" if chg < 0 else "white"
    return f"[{cc}]{_fmt(chg, '+.1f')}[/{cc}]"


def row_rvol(row: dict) -> float | None:
    """Relative volume for a row, or None when nothing trustworthy is on offer.

    Precedence: the funnel's own figure (scored from today's extended-hours
    minute bars) then the row's top-level `rvol` (same source since T2.1).
    Never falls back to `rvol_raw` — that is the naive full-day ratio, and
    reading it as a pace would understate every symbol before 16:00.
    """
    funnel = row.get("funnel")
    if isinstance(funnel, dict):
        v = _fnum(funnel.get("rvol"))
        if v is not None and v > 0:
            return v
    v = _fnum(row.get("rvol"))
    return v if (v is not None and v > 0) else None


def _age_short(sec) -> str:
    """Compact age: `47s`, `12m`, `3h`. Pre-market a thin name's last print can
    be hours old, and `9540s` does not read as a warning."""
    v = _fnum(sec)
    if v is None or v < 0:
        return "—"
    if v < 90:
        return f"{v:.0f}s"
    if v < 5400:
        return f"{v / 60:.0f}m"
    return f"{v / 3600:.0f}h"


def _rvol_text(v, hot: float = 3.0, warm: float = 1.5) -> str:
    """`8.2x` / `1.4x` / `—` from a value. Shared by both panels so the
    thresholds and colours cannot drift apart between them."""
    v = _fnum(v)
    if v is None or v <= 0:
        return "[dim]—[/dim]"
    if v >= float(hot):
        return f"[bold green]{v:.1f}x[/bold green]"
    if v >= float(warm):
        return f"[yellow]{v:.1f}x[/yellow]"
    return f"[dim]{v:.1f}x[/dim]"


def _rvol_cell(row: dict, hot: float = 3.0, warm: float = 1.5) -> str:
    """`8.2x` / `1.4x` / `—`. Never invents a value when both sources are
    missing: an absent RVOL must not read as "volume is unremarkable"."""
    return _rvol_text(row_rvol(row), hot, warm)


def _cell_rvol(row: dict, ctx: dict) -> str:
    cfg = ctx.get("cfg") or {}
    return _rvol_cell(row, float(cfg.get("rvol_hot", 3.0)),
                      float(cfg.get("rvol_warm", 1.5)))


def _cell_spark(row: dict, ctx: dict) -> str:
    """Fixed-width shape column, padded so the table cannot jitter as symbols
    accumulate history. Empty when there is not enough tape to draw honestly."""
    cfg = ctx.get("cfg") or {}
    hist = ctx.get("history")
    width = max(1, int(cfg.get("spark_width", 20)))
    if hist is None:
        return " " * width
    series = hist.series(ctx["sym"], "price")
    # Refuse to draw a picture of the polling interval. `price` updates on
    # streamed trades when there are any, but falls back to a 30s REST poll
    # pre-market — so a window of samples can hold a single real observation.
    # Counting distinct values is the only staleness check available: the feed's
    # `price_ts` is rewritten every 10Hz loop pass whether or not the price
    # changed, so it always reads fresh and detects nothing.
    if len(set(series)) < int(cfg.get("spark_min_distinct", 3)):
        return " " * width
    min_n = int(cfg.get("spark_min_samples", 5))
    txt = spark.sparkline(series, width, min_samples=min_n,
                          flat_pct=float(cfg.get("spark_flat_pct", 0.1)))
    if not txt:
        return " " * width
    d = spark.direction(series, min_n)
    color = "green" if d > 0 else "red" if d < 0 else "dim"
    return f"[{color}]{txt}[/{color}]" + " " * (width - len(txt))


def _cell_mentions(row: dict, ctx: dict) -> str:
    win = row.get("mention_window") or 0
    day = row.get("mention_count") or 0
    if not (win or day):
        # No mentions at all: "— →" would dress up an absence as a reading.
        return "—"
    arrow = _mention_arrow(row, ctx)
    if not arrow:
        return f"{win}/{day}"
    return f"{win}/{day} [{_mention_trend_color(arrow)}]{arrow}[/]"


def _mention_series(row: dict, ctx: dict) -> list:
    """The one series the Mentions arrow is computed from, live value already
    appended.

    Prefers the engine's mention_velocity series when it has accumulated
    enough samples — it is the higher-resolution number — and falls back to
    the polled mention_window series. One scale per call: the live engine
    value is only ever appended to its own series.

    Split out so the ↑↑ alert can measure the *level* of the same half the
    arrow compared without re-deciding which series that was.
    """
    hist = ctx.get("history")
    if hist is None:
        return []
    sym = ctx["sym"]
    min_n = mention_trend_floor(ctx.get("cfg") or {},
                                getattr(ctx.get("feed"), "server_cfg", None))
    engine_vel = _fnum(_sp(row).get("mention_velocity"))
    vel = list(hist.series(sym, "mention_velocity"))
    if len(vel) + (1 if engine_vel is not None else 0) >= min_n:
        return [*vel, engine_vel] if engine_vel is not None else vel
    return list(hist.series(sym, "mention_window"))


def mention_flow_level(series: list) -> float | None:
    """Mean of the recent half — the same half mention_trend() ratioed, so a
    "↑↑" and its level always describe the same samples. None when empty."""
    vals = [v for v in (series or []) if v is not None]
    if not vals:
        return None
    recent = vals[len(vals) // 2:]
    return sum(recent) / len(recent) if recent else None


def _mention_arrow(row: dict, ctx: dict) -> str:
    """Trend arrow for the Mentions cell, or "" when the feature is off or
    there is not enough tape yet."""
    cfg = ctx.get("cfg") or {}
    if ctx.get("history") is None or not cfg.get("mention_trend_enabled", True):
        return ""
    return mention_trend(
        _mention_series(row, ctx), None,
        float(cfg.get("mention_trend_rise", 1.5)),
        float(cfg.get("mention_trend_fall", 0.6)),
        mention_trend_floor(cfg, getattr(ctx.get("feed"), "server_cfg", None)))


def _cell_setup(row: dict, ctx: dict) -> str:
    cfg = ctx.get("cfg") or {}
    rsi_max = ctx["rsi_focus_max"]
    p_lo, p_hi = ctx["pctr_focus_lo"], ctx["pctr_focus_hi"]

    age = None
    if cfg.get("focus_age_enabled", True):
        feed = ctx.get("feed")
        if feed is not None and hasattr(feed, "focus_age"):
            age = feed.focus_age(ctx["sym"], ctx["now"])

    near = None
    if cfg.get("setup_distance_enabled", True):
        d = setup_distance(row, rsi_max, p_lo, p_hi,
                           proximity_weight=float(
                               cfg.get("setup_proximity_weight", 0.35)))
        if d is not None and 0.0 < d < float(
                cfg.get("setup_near_threshold", 0.25)):
            near = setup_shortfall(row, rsi_max, p_lo, p_hi) or None

    return _rsi_focus_cell(
        row, rsi_max, p_lo, p_hi, age=age, near=near,
        fresh_sec=float(cfg.get("focus_age_fresh_sec", 60.0)),
        stale_sec=float(cfg.get("focus_age_stale_sec", 180.0)))


def _cell_flags(row: dict, ctx: dict) -> str:
    flags = []
    if row.get("find_it_first"):
        flags.append("[bold black on green]🥇FIRST[/]")
    if ctx["feed"].is_fresh(ctx["sym"], ctx["now"]):
        flags.append("[bold black on cyan] NEW [/]")
    if row.get("mention_burst"):
        flags.append("[bold black on yellow]🔥BURST[/]")
    conf = row.get("confluence")
    if isinstance(conf, dict) and conf.get("count", 0) >= 2:
        flags.append(f"[magenta]⚡{conf['count']}[/magenta]")
    # Stocktwits trending rank (same list as stocktwits.com/sentiment)
    rk = ctx["st_rank"].get(ctx["sym"])
    if rk is not None:
        flags.append(f"[bold black on magenta] ST#{rk} [/]")
    return " ".join(flags)


# The historical column set, unchanged since before the roadmap. Optional
# columns are layered on by momentum_columns() rather than edited in here, so
# this stays a stable reference for the ordering regression tests.
# ratio=1 → share leftover width equally when the table expand=True fills
# the terminal. Fixed-width keys (#) stay compact; data columns flex.
MOMENTUM_COLUMNS = [
    ("#",        {"justify": "right", "style": "bold", "width": 2,
                  "no_wrap": True},                     _cell_key),
    ("Symbol",   {"ratio": 1, "no_wrap": True},         _cell_symbol),
    ("Added",    {"justify": "right", "ratio": 1, "no_wrap": True},
                                                        _cell_added),
    ("Price",    {"justify": "right", "ratio": 1, "no_wrap": True},
                                                        _cell_price),
    ("Chg%",     {"justify": "right", "ratio": 1, "no_wrap": True},
                                                        _cell_chg),
    ("Mentions", {"justify": "right", "ratio": 1, "no_wrap": True},
                                                        _cell_mentions),
    # Combined CM RSI + %R deep-OS cue (not RSI alone)
    ("Setup",    {"justify": "right", "ratio": 1, "no_wrap": True},
                                                        _cell_setup),
    ("",         {"ratio": 1, "no_wrap": True},         _cell_flags),
]

# Optional columns: (config flag, anchor headers, spec). Each is inserted
# immediately after the first anchor present at that point, so placement stays
# correct however many of the other optional columns are switched off.
OPTIONAL_COLUMNS = [
    ("rvol_column_enabled", ("Chg%",),
     ("RVOL", {"justify": "right", "ratio": 1, "no_wrap": True},
      _cell_rvol)),
    ("spark_enabled", ("RVOL", "Chg%"),
     ("Shape", {"justify": "left", "ratio": 1, "no_wrap": True},
      _cell_spark)),
]


def _desk_table_width() -> int:
    """Terminal content width for expand=True desk tables (inside a Panel)."""
    try:
        w = shutil.get_terminal_size(fallback=(120, 30)).columns
    except Exception:  # noqa: BLE001
        w = 120
    # Panel border (2) + panel padding (2) leave this for the table body.
    return max(60, int(w) - 4)


def momentum_columns(cfg: dict | None = None) -> list:
    """The column spec for a render, with flag-gated optional columns added.

    Passing no cfg yields exactly the historical set — the flags are read with
    their DEFAULTS values only when a cfg is actually supplied, so a cfg-less
    call stays a clean structural baseline for tests.
    """
    if cfg is None:
        return list(MOMENTUM_COLUMNS)
    cols = list(MOMENTUM_COLUMNS)
    for flag, anchors, spec in OPTIONAL_COLUMNS:
        if not cfg.get(flag, True):
            continue
        at = len(cols)
        for anchor in anchors:
            i = next((n for n, (h, _, _) in enumerate(cols) if h == anchor),
                     None)
            if i is not None:
                at = i + 1
                break
        cols.insert(at, spec)
    return cols


def momentum_table(feed: Feed, now: float, hz: float,
                   hotkeys_on: bool,
                   rsi_focus_max: float = 35.0,
                   pctr_focus_lo: float = -100.0,
                   pctr_focus_hi: float = -75.0,
                   st_rank: dict[str, int] | None = None,
                   columns: list | None = None,
                   history=None,
                   cfg: dict | None = None) -> Table:
    cols = momentum_columns(cfg) if columns is None else columns
    # Fill the terminal; equal ratio on flex columns (see MOMENTUM_COLUMNS).
    t = Table(expand=True, width=_desk_table_width(), padding=(0, 1))
    for header, opts, _ in cols:
        t.add_column(header, **opts)
    st_rank = st_rank or {}
    for i, r in enumerate(feed.rows):
        ctx = {
            "idx": i,
            "sym": str(r.get("ticker") or "?").upper(),
            "feed": feed,
            "now": now,
            "hotkeys_on": hotkeys_on,
            "rsi_focus_max": rsi_focus_max,
            "pctr_focus_lo": pctr_focus_lo,
            "pctr_focus_hi": pctr_focus_hi,
            "st_rank": st_rank,
            "history": history,
            "cfg": cfg or {},
        }
        cells = []
        for _, _, build in cols:
            # One bad cell must not take the desk down (ground rule 4).
            try:
                cells.append(build(r, ctx))
            except Exception:                              # noqa: BLE001
                cells.append("[dim]—[/dim]")
        t.add_row(*cells)
    if not feed.rows:
        t.add_row("", "[dim]no momentum tickers in the feed[/dim]",
                  *([""] * (len(cols) - 2)))
    return t


def _fmt_st_rvol(v, cfg: dict | None = None) -> str:
    cfg = cfg or {}
    return _rvol_text(v, float(cfg.get("rvol_hot", 3.0)),
                      float(cfg.get("rvol_warm", 1.5)))


def _session_tag(iso: str) -> str:
    """`2026-07-24` → `Fri`. Empty when the date is missing or unparseable —
    the marker is what stops the number reading as today's, so a bare number
    with no tag is not an acceptable fallback; the caller drops to `—`."""
    try:
        return date.fromisoformat(str(iso)).strftime("%a")
    except (TypeError, ValueError):
        return ""


def _st_chg_cell(row: dict) -> str:
    """%Chg for one trending row.

    Today's move when there is one, in green/red. When today has not printed,
    the number `pct_change` carries is the last close measured against itself
    — structurally 0.00%, which reads as "flat" when it means "not trading
    yet". So fall back to the last completed session's move, dimmed and
    tagged with its weekday, which is also what stocktwits.com shows in that
    column pre-market.

    Dim + tag is the whole safety property: the two numbers answer different
    questions and must never be renderable in the same style. Nothing here
    touches `pct_change`, which is what the LOOK gate reads.
    """
    chg = row.get("pct_change")
    if chg is not None and row.get("pct_is_today"):
        cc = "green" if chg >= 0 else "red"
        return f"[{cc}]{chg:+.2f}%[/{cc}]"
    prev = row.get("pct_change_prev")
    tag = _session_tag(row.get("prev_session_date"))
    if prev is not None and tag:
        return f"[dim]{prev:+.1f}% {tag}[/dim]"
    # No today session and no completed one to fall back on. The 0.00% that
    # `pct_change` would print here is the plausible wrong number.
    return "[dim]—[/dim]"


def stocktwits_panel(st: StocktwitsTrending,
                     price_by_sym: dict[str, float | None],
                     limit: int = 10,
                     hotkeys_on: bool = True,
                     cfg: dict | None = None,
                     now: float | None = None) -> Panel:
    """Stocktwits trending — website columns + letter key (A-J) + LOOK badge."""
    from stocktwits_trending import fmt_vol, range_cell

    now = time.time() if now is None else now
    rows = st.display_rows(price_by_sym,
                           limit=min(limit, len(DeskHotkeys.ST_LETTERS)),
                           now=now)
    cfg = cfg or {}
    stale_sec = float(cfg.get("price_stale_sec", 20.0))
    age_on = bool(cfg.get("price_age_enabled", True))
    rvol_on = bool(cfg.get("stocktwits_rvol_column", True))
    range_w = int(cfg.get("stocktwits_range_width", 11))
    t = Table(expand=True, width=_desk_table_width(), padding=(0, 1))
    t.add_column("Key", justify="right", style="bold", width=3, no_wrap=True)
    t.add_column("ST#", justify="right", style="bold magenta", width=3,
                 no_wrap=True)
    # Equal flex across data columns so the grid fills the terminal evenly.
    t.add_column("Symbol", ratio=1, no_wrap=True)
    t.add_column("Last", justify="right", ratio=1, no_wrap=True)
    t.add_column("%Chg", justify="right", ratio=1, no_wrap=True)
    # Named for its feed on purpose. The free Alpaca plan is IEX-only, a few
    # percent of the consolidated tape, so this is materially smaller than the
    # volume the Stocktwits site shows for the same symbol. Labelled "Volume"
    # it read as the consolidated figure and invited comparison against it.
    t.add_column("Vol·IEX", justify="right", ratio=1, no_wrap=True)
    if rvol_on:
        t.add_column("RVOL", justify="right", ratio=1, no_wrap=True)
    # One track replaces the 52w Hi / 52w Lo pair: position in the range is
    # what EXT and WASH are actually about, and two absolute prices made the
    # reader do that division. Mkt Cap is gone — it never changed a decision.
    t.add_column("52w lo→hi", justify="left", ratio=1, no_wrap=True)
    t.add_column("Score", justify="right", ratio=1, no_wrap=True)
    t.add_column("", justify="center", ratio=1, no_wrap=True)  # LOOK badge
    n_look = 0
    if not rows:
        msg = st.error or "waiting for first poll…"
        t.add_row("", "—", f"[dim]{msg}[/dim]",
                  *([""] * (len(t.columns) - 3)))
    else:
        for i, r in enumerate(rows):
            letter = (DeskHotkeys.ST_LETTERS[i].upper()
                      if hotkeys_on and i < len(DeskHotkeys.ST_LETTERS) else "")
            px = r.get("price")
            if px is None:
                px_s = "[dim]—[/dim]"
            else:
                px_s = f"${px:.2f}"
                age = r.get("price_age_sec")
                if age_on and isinstance(age, (int, float)) and age >= stale_sec:
                    px_s = f"[dim]${px:.2f}·{_age_short(age)}[/dim]"
            chg_s = _st_chg_cell(r)
            hi = r.get("high_52w")
            lo = r.get("low_52w")
            sc = r.get("trending_score")
            # Session volume only — never the 30-day average standing in for it.
            vol = r.get("vol_session")
            look_s = ""
            if r.get("look"):
                n_look += 1
                reason = r.get("look_reason") or ""
                look_s = f"[bold black on green] LOOK {reason} [/]"
            sym_s = f"[bold cyan]{r['symbol']}[/bold cyan]"
            if r.get("look"):
                sym_s = f"[bold green]{r['symbol']}[/bold green]"
            cells = [
                letter,
                str(r.get("rank") or "—"),
                sym_s,
                px_s,
                chg_s,
                fmt_vol(vol) if vol is not None else "[dim]—[/dim]",
            ]
            if rvol_on:
                cells.append(_fmt_st_rvol(r.get("rvol"), cfg))
            cells += [
                range_cell(px, lo, hi, width=range_w) or "[dim]—[/dim]",
                f"{sc:.1f}" if sc is not None else "—",
                look_s,
            ]
            t.add_row(*cells)
    stamp = ""
    if st.last_ok:
        stamp = f"  ·  {datetime.fromtimestamp(st.last_ok):%H:%M:%S}"
    # The list timestamp above is the Stocktwits poll. Quotes ride a faster
    # clock now, so the number the desk actually trades off gets its own age.
    q = ""
    q_err = getattr(st, "quotes_error", "")
    q_age = st.quote_age(now) if hasattr(st, "quote_age") else None
    if q_err:
        # Without quotes the panel is Stocktwits-only. Say it once in the title
        # rather than leaving five blank columns to be interpreted.
        q = f"  ·  [bold yellow]{q_err}[/bold yellow]"
    elif q_age is not None and q_age >= stale_sec:
        q = f"  ·  [yellow]quotes {_age_short(q_age)} old[/yellow]"
    cap = (f"  ·  max ${st.max_price:g}" if st.max_price is not None else "")
    look_n = f"  ·  {n_look} LOOK" if n_look else ""
    title = f"TRENDING  ·  A-J load TV{cap}{look_n}{stamp}{q}"
    return Panel(t, title=title, title_align="left",
                 border_style="magenta", padding=(0, 1), expand=True)


def _ai_source_cell(row: dict) -> str:
    """A = Anthropic, X = xAI, AX = both agreed. Falls back from free-form tags."""
    mark = str(row.get("source_mark") or "").upper().strip()
    if not mark:
        try:
            from ai_suggest import ai_source_mark, normalize_ai_source
            src = normalize_ai_source(row.get("source"))
            if src == "both" or row.get("agreement"):
                mark = "AX"
            else:
                mark = ai_source_mark(row.get("source")).upper()
        except Exception:  # noqa: BLE001
            mark = "?"
    if mark in ("C", "CLAUDE", "ANTHROPIC"):
        mark = "A"
    if mark in ("G", "GROK"):
        mark = "X"
    if mark in ("BOTH", "AX", "A+X", "XA"):
        return "[bold white on blue] AX [/]"
    if mark == "A":
        return "[bold magenta]A[/bold magenta]"
    if mark == "X":
        return "[bold cyan]X[/bold cyan]"
    return f"[dim]{mark or '?'}[/dim]"


def claude_panel(gs: AiSuggestions,
               price_by_sym: dict[str, float | None],
               limit: int = 10,
               hotkeys_on: bool = True,
               cfg: dict | None = None,
               now: float | None = None) -> Panel:
    """AI suggestions — same market columns as TRENDING + Src + Why + LOOK.

    Src: A = Anthropic (Claude), X = xAI (Grok).
    """
    from stocktwits_trending import fmt_vol, range_cell

    now = time.time() if now is None else now
    rows = gs.display_rows(price_by_sym,
                           limit=min(limit, len(DeskHotkeys.CLAUDE_LETTERS)),
                           now=now)
    cfg = cfg or {}
    stale_sec = float(cfg.get("price_stale_sec", 20.0))
    age_on = bool(cfg.get("price_age_enabled", True))
    rvol_on = bool(cfg.get("claude_rvol_column", True))
    range_w = int(cfg.get("claude_range_width", 11))
    t = Table(expand=True, width=_desk_table_width(), padding=(0, 1))
    t.add_column("Key", justify="right", style="bold", width=3, no_wrap=True)
    t.add_column("G#", justify="right", style="bold yellow", width=3,
                 no_wrap=True)
    t.add_column("Src", justify="center", style="bold", width=4, no_wrap=True)
    t.add_column("Symbol", ratio=1, no_wrap=True)
    t.add_column("Last", justify="right", ratio=1, no_wrap=True)
    t.add_column("%Chg", justify="right", ratio=1, no_wrap=True)
    t.add_column("Vol·IEX", justify="right", ratio=1, no_wrap=True)
    if rvol_on:
        t.add_column("RVOL", justify="right", ratio=1, no_wrap=True)
    t.add_column("52w lo→hi", justify="left", ratio=1, no_wrap=True)
    t.add_column("Score", justify="right", ratio=1, no_wrap=True)
    t.add_column("Why", justify="left", ratio=2, no_wrap=True)
    t.add_column("", justify="center", ratio=1, no_wrap=True)  # LOOK badge
    n_look = 0
    if not rows:
        # No data rows — leave the table blank and put status in the title
        # only. Putting a long error into a Symbol cell reflows every column.
        pass
    else:
        for i, r in enumerate(rows):
            letter = (DeskHotkeys.CLAUDE_LETTERS[i].upper()
                      if hotkeys_on and i < len(DeskHotkeys.CLAUDE_LETTERS) else "")
            px = r.get("price")
            if px is None:
                px_s = "[dim]—[/dim]"
            else:
                px_s = f"${px:.2f}"
                age = r.get("price_age_sec")
                if age_on and isinstance(age, (int, float)) and age >= stale_sec:
                    px_s = f"[dim]${px:.2f}·{_age_short(age)}[/dim]"
            chg_s = _st_chg_cell(r)
            hi = r.get("high_52w")
            lo = r.get("low_52w")
            sc = r.get("trending_score")
            vol = r.get("vol_session")
            look_s = ""
            if r.get("look"):
                n_look += 1
                reason = r.get("look_reason") or ""
                look_s = f"[bold black on green] LOOK {reason} [/]"
            sym_s = f"[bold cyan]{r['symbol']}[/bold cyan]"
            if r.get("look"):
                sym_s = f"[bold green]{r['symbol']}[/bold green]"
            why = (r.get("reason") or "").strip()
            why_s = f"[dim]{why[:36]}[/dim]" if why else ""
            cells = [
                letter,
                str(r.get("rank") or "—"),
                _ai_source_cell(r),
                sym_s,
                px_s,
                chg_s,
                fmt_vol(vol) if vol is not None else "[dim]—[/dim]",
            ]
            if rvol_on:
                cells.append(_fmt_st_rvol(r.get("rvol"), cfg))
            cells += [
                range_cell(px, lo, hi, width=range_w) or "[dim]—[/dim]",
                f"{sc:.1f}" if sc is not None else "—",
                why_s,
                look_s,
            ]
            t.add_row(*cells)
    stamp = ""
    if gs.last_ok:
        stamp = f"  ·  {datetime.fromtimestamp(gs.last_ok):%H:%M:%S}"
    q = ""
    q_err = getattr(gs, "quotes_error", "")
    q_age = gs.quote_age(now) if hasattr(gs, "quote_age") else None
    if q_err:
        q = f"  ·  [bold yellow]{q_err}[/bold yellow]"
    elif q_age is not None and q_age >= stale_sec:
        q = f"  ·  [yellow]quotes {_age_short(q_age)} old[/yellow]"
    cap = (f"  ·  max ${gs.max_price:g}" if gs.max_price is not None else "")
    look_n = f"  ·  {n_look} LOOK" if n_look else ""
    model = getattr(gs, "model", "") or ""
    model_s = f"  ·  {model}" if model else ""
    trade_mode = getattr(gs, "trading_mode", "off") or "off"
    if getattr(gs, "trading", False):
        if trade_mode == "paper":
            trade_s = "  ·  [bold green]PAPER trade ON[/bold green]"
        else:
            trade_s = "  ·  [yellow]trade off (need Alpaca paper keys)[/yellow]"
    else:
        trade_s = ""
    n_trades = len(getattr(gs, "last_trades", None) or [])
    trade_n = f"  ·  {n_trades} orders" if n_trades else ""
    # Status / errors go in the title — never into a table cell (that reflows
    # the whole panel into the "broken columns" look).
    status_s = ""
    if not rows:
        err = (gs.error or "waiting for first Claude poll…").replace("\n", " ")
        if len(err) > 70:
            err = err[:67] + "…"
        status_s = f"  ·  [dim]{err}[/dim]"
    title = (
        f"AI  ·  [magenta]A[/]=Anthropic  [cyan]X[/]=xAI  "
        f"[white on blue]AX[/]=both  ·  K-T load TV"
        f"{cap}{look_n}{trade_s}{trade_n}"
        f"{model_s}{stamp}{q}{status_s}"
    )
    return Panel(t, title=title, title_align="left",
                 border_style="yellow", padding=(0, 1), expand=True)


def header_panel(feed: Feed, now: float, hz: float,
                 stale: bool, circle: str | None = None,
                 readout: str | None = None) -> Panel:
    n = len(feed.rows)
    src = _dashboard_url()
    if stale:
        line = "[bold white on red]  FEED DOWN  [/]  " \
               f"[dim]can't reach {src}[/dim]"
    else:
        line = (f"[bold cyan]Brasfield Momentum[/bold cyan]   "
                f"[green]{n}[/green] symbols   "
                f"[dim]{hz:.1f} polls/s · {src}[/dim]")
    if session_line:
        line += f"   [dim]{session_line()}[/dim]"
    # The buy-readiness circle rides the top border, right-aligned — the
    # top-right corner of the whole monitor, with no extra panel or reflow.
    # The raw indicator readout sits in the bottom border directly under it,
    # so the numbers the circle was computed from are always next to it.
    return Panel(Align.center(line), border_style="red" if stale else "cyan",
                 padding=(0, 1), title=circle, title_align="right",
                 subtitle=readout, subtitle_align="right")


def _fetch_open_orders() -> list[dict]:
    """Open Alpaca orders as simple dicts. Empty list when off / error."""
    try:
        import alpaca_trader
        if not alpaca_trader.is_active():
            return []
        client = getattr(alpaca_trader, "_client", None)
        if client is None:
            return []
        try:
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus
            raw = client.get_orders(
                filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=50))
        except Exception:
            raw = client.get_orders() or []
            raw = [o for o in raw
                   if str(getattr(o, "status", "")).lower()
                   in ("new", "accepted", "pending_new", "partially_filled",
                       "orderstatus.accepted", "orderstatus.new")]
        out = []
        seen: set[tuple] = set()
        for o in raw or []:
            try:
                row = {
                    "symbol": str(getattr(o, "symbol", "") or "").upper(),
                    "side": str(getattr(o, "side", "") or "").split(".")[-1].lower(),
                    "qty": float(getattr(o, "qty", 0) or 0),
                    "filled": float(getattr(o, "filled_qty", 0) or 0),
                    "type": str(getattr(o, "type", "") or "").split(".")[-1].lower(),
                    "status": str(getattr(o, "status", "") or "").split(".")[-1].lower(),
                    "limit": float(getattr(o, "limit_price", 0) or 0) or None,
                }
                key = (row["symbol"], row["side"], row["qty"], row["limit"],
                       row["status"])
                if key in seen:
                    continue
                seen.add(key)
                out.append(row)
            except Exception:
                continue
        return out
    except Exception:
        return []


def positions_panel(positions: dict | None,
                    focus: str | None = None,
                    *,
                    open_orders: list[dict] | None = None,
                    mode: str = "paper",
                    error: str = "",
                    label: str = "POSITIONS") -> Panel:
    """Live P&L for open Alpaca positions + resting open orders.

    Claude paper buys often sit as ACCEPTED limits when the market is closed —
    those are not positions yet, so we show them under OPEN ORDERS.
    """
    positions = positions or {}
    open_orders = open_orders or []
    total_pl = sum(float(p.get("pl") or 0) for p in positions.values())
    tcol = ("green" if total_pl >= 0 else "red") if positions else "yellow"
    mode_tag = "PAPER" if str(mode).lower() == "paper" else str(mode).upper()

    body = Table(expand=False, box=None, padding=(0, 1))
    body.add_column("Symbol", style="bold cyan")
    body.add_column("Qty", justify="right")
    body.add_column("Entry", justify="right")
    body.add_column("Last", justify="right")
    body.add_column("P&L $", justify="right")
    body.add_column("P&L %", justify="right")
    body.add_column("Mkt", justify="right")

    if positions:
        for sym in sorted(positions):
            p = positions[sym]
            pl = float(p.get("pl") or 0)
            plpc = float(p.get("plpc") or 0)
            c = "green" if pl >= 0 else "red"
            marker = "▶ " if focus and sym == focus.upper() else ""
            body.add_row(
                f"{marker}{sym}",
                f"{float(p.get('qty') or 0):g}",
                f"${float(p.get('avg_entry') or 0):.2f}",
                f"${float(p.get('current') or 0):.2f}",
                f"[{c}]{'+' if pl >= 0 else ''}${pl:,.2f}[/{c}]",
                f"[{c}]{'+' if plpc >= 0 else ''}{plpc:.2f}%[/{c}]",
                f"${float(p.get('mkt_val') or 0):,.0f}",
            )
    else:
        body.add_row("[dim]—[/dim]", "[dim]flat[/dim]",
                     "", "", "", "", "")

    # Resting orders (accepted limits waiting for fill)
    orders_table = None
    if open_orders:
        orders_table = Table(expand=False, box=None, padding=(0, 1))
        orders_table.add_column("Side", justify="right")
        orders_table.add_column("Symbol", style="bold")
        orders_table.add_column("Qty", justify="right")
        orders_table.add_column("Type")
        orders_table.add_column("Limit", justify="right")
        orders_table.add_column("Status")
        for o in open_orders:
            side = (o.get("side") or "").upper()
            sc = "green" if side == "BUY" else "red"
            lim = o.get("limit")
            lim_s = f"${lim:.2f}" if lim else "—"
            orders_table.add_row(
                f"[{sc}]{side or '—'}[/{sc}]",
                str(o.get("symbol") or "—"),
                f"{float(o.get('qty') or 0):g}",
                str(o.get("type") or "—"),
                lim_s,
                str(o.get("status") or "—"),
            )

    if error:
        content = Text.from_markup(f"[dim]{error}[/dim]")
    elif orders_table is not None:
        content = Group(
            body,
            Text(""),
            Text.from_markup(
                "[bold]OPEN ORDERS[/bold]  [dim](not filled yet)[/dim]"
            ),
            orders_table,
        )
    else:
        content = body

    n_pos = len(positions)
    n_ord = len(open_orders)
    pl_s = (f"  total P&L [{tcol}]{'+' if total_pl >= 0 else ''}"
            f"${total_pl:,.2f}[/{tcol}]" if n_pos else "")
    ord_s = f"  ·  {n_ord} resting" if n_ord else ""
    title = f"{mode_tag} {label} ({n_pos}){pl_s}{ord_s}"
    return Panel(content, title=title, title_align="left",
                 border_style=tcol if n_pos else "yellow", padding=(0, 1))


def footer_panel(alerter: Alerter, hotkeys: DeskHotkeys,
                 hotkey_slots: int,
                 *, st_on: bool = True, claude_on: bool = False) -> Panel:
    lines = []
    if alerter.recent:
        lines.append("[dim]" + "   ·   ".join(alerter.recent[:3]) + "[/dim]")
    focus = hotkeys.focus_symbol() or "—"
    lines.append(
        f"[bold]FOCUS[/bold] [bold cyan]{focus}[/bold cyan]   "
        f"TV={'on' if desk.tv_load_available() else 'off'}   "
        f"[{desk.platform_label()}]"
    )
    if hotkeys.enabled:
        parts = [f"1-{hotkey_slots}/space: momentum → TV"]
        if st_on:
            parts.append("A-J: Stocktwits → TV")
        if claude_on:
            parts.append("K-T: Claude → TV")
        hint = "[dim]" + "   ·   ".join(parts) + "[/dim]"
        st = hotkeys.status()
        if st:
            hint += f"     [bold green]{st}[/bold green]"
        lines.append(hint)
    else:
        lines.append("[dim]hotkeys off — use a real Terminal on macOS/Windows[/dim]")
    return Panel(Align.center("\n".join(lines)), border_style="grey37",
                 padding=(0, 1))


# ── main ─────────────────────────────────────────────────────────────────────

STALE_AFTER = 15.0   # seconds without a good poll before the FEED DOWN banner


def main():
    # UTF-8 console (Windows cmd defaults break badge glyphs)
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8")           # type: ignore[attr-defined]
    cfg = load_config()
    interval = float(cfg.get("poll_interval", 2.0))
    hotkey_slots = min(9, int(cfg.get("hotkey_slots", 9)))
    rsi_focus_max = float(cfg.get("rsi_focus_max", 35.0))
    pctr_focus_lo = float(cfg.get("pctr_focus_lo", -100.0))
    pctr_focus_hi = float(cfg.get("pctr_focus_hi", -75.0))
    st_on = bool(cfg.get("stocktwits_enabled", True))
    st_max_px = cfg.get("stocktwits_max_price", 35.0)
    st_limit = min(10, int(cfg.get("stocktwits_panel_limit", 10)))
    claude_on = bool(cfg.get("claude_enabled", False))
    claude_max_px = cfg.get("claude_max_price", None)
    claude_limit = min(10, int(cfg.get("claude_panel_limit", 7)))
    positions_on = bool(cfg.get("positions_panel_enabled", True))
    positions_poll = max(2.0, float(cfg.get("positions_poll", 5.0)))

    console = Console()
    feed = Feed(cfg)
    alerter = Alerter(cfg)

    # Paper/live account for the POSITIONS panel and optional buy_zone auto-limit.
    # Claude desk book is separate (server). Auto-limit defaults OFF / paper-only.
    pos_mode = "off"
    auto_limit_on = bool(cfg.get("auto_limit_enabled", False))
    if positions_on or auto_limit_on:
        try:
            pos_mode = desk.init_trader(cfg) or "off"
        except Exception:
            pos_mode = "off"
        console.print(f"[dim]positions panel: alpaca mode={pos_mode}[/dim]")
    if auto_limit_on:
        live_note = "live-ok" if cfg.get("auto_limit_live") else "paper-only"
        console.print(
            f"[dim]auto-limit: ON ({live_note})  buy_zone → policy limit  "
            f"cooldown={cfg.get('auto_limit_cooldown_sec', 900)}s  "
            f"cap={cfg.get('auto_limit_max_per_session', 3)}/session[/dim]"
        )
    try:
        from auto_limit import AutoLimitState, process_rows as auto_limit_process
        _auto_limit_state = AutoLimitState()
    except Exception:                                      # noqa: BLE001
        auto_limit_process = None
        _auto_limit_state = None
    chart_symbol = ChartSymbol(cfg)
    chart_watcher = None
    if (cfg.get("buy_circle_enabled", True)
            and str(cfg.get("buy_circle_source", "chart")).lower() == "chart"):
        chart_watcher = ChartWatcher(cfg)
        chart_watcher.start()
        if not chart_watcher.available:
            chart_watcher = None
    # Say which source the circle is on. The two report failures in different
    # vocabularies, and "pending" from the engine looks identical on screen to
    # a screen-read problem — this is the line that tells them apart.
    if cfg.get("buy_circle_enabled", True):
        if chart_watcher is not None:
            console.print("[dim]buy circle: reading the TradingView window[/dim]")
        else:
            why = ("chart reader unavailable — no Quartz/tesseract"
                   if chart_feed is None else "configured for engine")
            console.print(f"[dim]buy circle: engine signal_proximity ({why})[/dim]")
    history = SymbolHistory(maxlen=int(cfg.get("history_samples", 120)))
    journal = Journal(HERE / str(cfg.get("journal_dir", "journal")),
                      flush_sec=float(cfg.get("journal_flush_sec", 5.0)),
                      enabled=bool(cfg.get("journal_enabled", True)),
                      log=console.print)
    # The desk is stopped with Ctrl+C every day: SIGINT raises
    # KeyboardInterrupt, which unwinds to sys.exit(0) in __main__ and runs
    # atexit handlers. The last few records must not be lost.
    atexit.register(journal.close)

    def _st_alert(kind: str, sym: str, detail: str) -> None:
        # Journal the edge regardless of whether it is audible — the alert
        # flags control the speaker, not the evidence.
        row = st_row_for(sym)
        journal.record(kind, sym, row, time.time(),
                       st_rank=st_rank_for(sym),
                       rvol=row.get("rvol"))
        flag = "alert_st_new" if kind == "st_new" else "alert_st_look"
        if cfg.get(flag, True):
            alerter.fire(kind, sym, detail)

    def _claude_alert(kind: str, sym: str, detail: str) -> None:
        row = claude_row_for(sym)
        journal.record(kind, sym, row, time.time(),
                       st_rank=row.get("rank"),
                       rvol=row.get("rvol"))
        flag = "alert_claude_new" if kind == "claude_new" else "alert_claude_look"
        if cfg.get(flag, True):
            alerter.fire(kind, sym, detail)

    # Refreshed each loop so the ST on_change callback can attach live
    # context. ST rows carry `price` and `pct_change` under the same key names
    # the momentum rows use, so one lookup serves rank and record alike.
    _st_ctx: dict = {"by_symbol": {}}
    _claude_ctx: dict = {"by_symbol": {}}

    def st_row_for(sym: str) -> dict:
        r = _st_ctx["by_symbol"].get((sym or "").upper())
        return r if isinstance(r, dict) else {}

    def st_rank_for(sym: str):
        return st_row_for(sym).get("rank")

    def claude_row_for(sym: str) -> dict:
        r = _claude_ctx["by_symbol"].get((sym or "").upper())
        return r if isinstance(r, dict) else {}

    hotkeys = DeskHotkeys()
    st_min_rvol = cfg.get("stocktwits_look_min_rvol", 1.5)
    st = RemoteStocktwitsTrending(
        stocks_only=bool(cfg.get("stocktwits_stocks_only", True)),
        max_price=float(st_max_px) if st_max_px is not None else None,
        look_min_abs_chg=float(cfg.get("stocktwits_look_min_abs_chg", 3.0)),
        look_max=int(cfg.get("stocktwits_look_max", 2)),
        look_near_high=float(cfg.get("stocktwits_look_near_high", 0.70)),
        look_near_low=float(cfg.get("stocktwits_look_near_low", 0.30)),
        look_min_rvol=(float(st_min_rvol) if st_min_rvol is not None else None),
        avg_days=int(cfg.get("stocktwits_avg_days", 10)),
        rvol_time_adjusted=bool(cfg.get("stocktwits_rvol_time_adjusted", True)),
    ) if st_on else None

    claude_min_rvol = cfg.get("claude_look_min_rvol", 1.5)
    gs = RemoteAiSuggestions(
        max_price=float(claude_max_px) if claude_max_px is not None else None,
        look_min_abs_chg=float(cfg.get("claude_look_min_abs_chg", 3.0)),
        look_max=int(cfg.get("claude_look_max", 2)),
        look_near_high=float(cfg.get("claude_look_near_high", 0.70)),
        look_near_low=float(cfg.get("claude_look_near_low", 0.30)),
        look_min_rvol=(float(claude_min_rvol) if claude_min_rvol is not None else None),
        avg_days=int(cfg.get("claude_avg_days", 10)),
        rvol_time_adjusted=bool(cfg.get("claude_rvol_time_adjusted", True)),
        panel_limit=claude_limit,
    ) if claude_on else None

    st_note = f"  ST={'on' if st_on else 'off'}"
    claude_note = f"  Claude={'on' if claude_on else 'off'}"
    if gs is not None and gs.trading:
        claude_note += f"/trade={gs.trading_mode}"
    console.print(
        f"[bold]Momentum desk[/bold]  {desk.platform_label()}  "
        f"TV={'on' if desk.tv_load_available() else 'off'}{st_note}{claude_note}  "
        f"— Ctrl+C to stop.\n"
        f"Polling {_dashboard_url()}/api/state"
        + (f"  ·  Stocktwits + Claude served by the dashboard"
           if (st_on or claude_on) else "")
        + "\n"
        + "[dim]1-9/space momentum → TV"
        + ("   ·   A-J Stocktwits → TV" if st_on else "")
        + ("   ·   K-T Claude → TV" if claude_on else "")
        + "[/dim]"
    )

    stamps: list[float] = []
    # Cached Alpaca holdings — refreshed on a slower clock than the 2s UI loop.
    _pos_cache: dict = {
        "positions": {},
        "orders": [],
        "error": "",
        "last": 0.0,
        "mode": pos_mode,
    }

    def _refresh_positions(now: float, force: bool = False) -> None:
        if not positions_on or _pos_cache["mode"] in ("", "off"):
            return
        if (not force and _pos_cache["last"]
                and (now - _pos_cache["last"]) < positions_poll):
            return
        _pos_cache["last"] = now
        try:
            detail = desk.positions_detail()
            _pos_cache["positions"] = detail if isinstance(detail, dict) else {}
            _pos_cache["orders"] = _fetch_open_orders()
            _pos_cache["error"] = ""
        except Exception as e:  # noqa: BLE001
            _pos_cache["error"] = str(e)[:80]
            _pos_cache["positions"] = {}
            _pos_cache["orders"] = []

    with Live(console=console, refresh_per_second=2, screen=False) as live:
        while True:
            t0 = time.time()
            state = fetch_state()
            if state is not None:
                feed.ingest(state, t0, alerter, cfg)
                push_history(history, feed.rows, t0)
                # After the push — the arrow is a function of the ring, and
                # this poll's sample has to be in it. Before the journal loop
                # so the edges it appends get recorded too.
                feed.detect_mention_flow(history, t0, alerter, cfg)
                for kind, row in feed.edges:
                    sym = str(row.get("ticker") or "").upper()
                    journal.record(kind, sym, row, t0,
                                   st_rank=st_rank_for(sym),
                                   rvol=row_rvol(row))
                # buy_zone → risk-sized policy limit + OCO bracket (default off)
                if (
                    auto_limit_on
                    and auto_limit_process is not None
                    and _auto_limit_state is not None
                    and pos_mode not in ("", "off")
                ):
                    try:
                        import desk_book as dbook
                    except Exception:                      # noqa: BLE001
                        dbook = None
                    book = dbook.load_book() if dbook else None
                    if book is not None and dbook is not None:
                        book = dbook.rollover_if_needed(book, t0)
                        # Manage open: ratchet stops / detect closes
                        try:
                            live = _pos_cache.get("positions") or {}
                            live_norm = {}
                            for k, v in live.items():
                                if isinstance(v, dict):
                                    live_norm[str(k).upper()] = {
                                        "current": v.get("current") or v.get("avg_entry"),
                                        "avg_entry": v.get("avg_entry"),
                                        "pl": v.get("pl"),
                                        "qty": v.get("qty"),
                                    }
                            book, ratchet_ev = dbook.manage_open(
                                book,
                                live_positions=live_norm,
                                cfg=cfg,
                                replace_stop_fn=desk.replace_protective_stop,
                                trail_fn=(
                                    desk.place_trail
                                    if float(cfg.get("trail_pct") or 0) > 0
                                    else None
                                ),
                                now=t0,
                            )
                            for rev in ratchet_ev:
                                console.print(
                                    f"[cyan]{rev.get('kind')} {rev.get('symbol')} "
                                    f"{rev.get('phase') or ''} "
                                    f"{rev.get('message') or rev.get('stop') or ''}"
                                    f"[/cyan]"
                                )
                                if rev.get("kind") == "desk_close":
                                    journal.record(
                                        "desk_close",
                                        str(rev.get("symbol") or ""),
                                        {}, t0,
                                        extra={
                                            "session_r": rev.get("session_r"),
                                            "halted": rev.get("halted"),
                                        },
                                    )
                            dbook.save_book(book)
                        except Exception as e:             # noqa: BLE001
                            console.print(f"[red]desk_book manage: {e}[/red]")

                    held = {
                        str(k).upper()
                        for k in (_pos_cache.get("positions") or {})
                    }
                    # Also treat book-tracked symbols as held (resting entry)
                    if book and isinstance(book.get("positions"), dict):
                        held |= {str(k).upper() for k in book["positions"]}

                    def _bracket_buy(sym: str, row: dict):
                        return desk.desk_buy_bracket(sym, row=row, cfg=cfg)

                    cfg_run = dict(cfg)
                    if book and dbook is not None:
                        if dbook.is_halted(
                            book, float(cfg.get("daily_loss_r", 2.0))
                        ):
                            cfg_run["_session_halted"] = True
                            cfg_run["_halt_reason"] = book.get("halt_reason") or "daily_halt"
                            _auto_limit_state.session_halted = True

                    try:
                        al_events = auto_limit_process(
                            feed.rows,
                            _auto_limit_state,
                            cfg=cfg_run,
                            trader_mode=pos_mode,
                            position_symbols=held,
                            buy_fn=_bracket_buy,
                            now=t0,
                        )
                    except Exception as e:                 # noqa: BLE001
                        al_events = [{
                            "kind": "auto_limit_error",
                            "symbol": "",
                            "message": str(e)[:120],
                        }]
                    for ev in al_events:
                        sym = str(ev.get("symbol") or "")
                        kind = str(ev.get("kind") or "auto_limit")
                        msg = str(ev.get("message") or ev.get("reason") or "")
                        if kind == "auto_limit":
                            try:
                                hotkeys._set(msg)  # type: ignore[attr-defined]
                            except Exception:
                                console.print(f"[green]{msg}[/green]")
                            try:
                                alerter.fire("buy", sym, msg[:80])
                            except Exception:
                                pass
                            plan = ev.get("plan")
                            if plan and book is not None and dbook is not None:
                                book = dbook.register_open(
                                    book, symbol=sym, plan=plan,
                                    buy_order_id=plan.get("buy_order_id"),
                                    now=t0,
                                )
                                dbook.save_book(book)
                            row_j = next(
                                (r for r in feed.rows
                                 if str(r.get("ticker") or "").upper() == sym),
                                {},
                            )
                            journal.record(
                                "auto_limit", sym, row_j, t0,
                                rvol=row_rvol(row_j) if row_j else None,
                                extra={"message": msg},
                            )
                        elif kind == "auto_limit_error":
                            console.print(f"[red]auto-limit {sym}: {msg}[/red]")
                            journal.record(
                                kind, sym or "?", {}, t0,
                                extra={"reason": msg},
                            )
            stale = (t0 - feed.last_ok) > STALE_AFTER if feed.last_ok else \
                    (state is None)
            stamps.append(t0)
            stamps[:] = [x for x in stamps if t0 - x <= 5]
            hz = len(stamps) / 5.0

            _refresh_positions(t0)

            # Both panels ride the /api/state payload already fetched above —
            # the server polls Stocktwits and runs Claude, not this loop. A
            # failed fetch leaves the last rows up rather than blanking the
            # panel; the header already says the feed is stale.
            if state is not None:
                if st is not None:
                    st.ingest(state.get("trending"), t0)
                if gs is not None:
                    # Prefer server-merged list (A/X/AX). Fall back to Claude-only
                    # publish so older dashboards still populate the panel.
                    ai_payload = state.get("ai_suggestions")
                    if not (isinstance(ai_payload, dict) and (
                            ai_payload.get("rows") is not None
                            or ai_payload.get("error")
                            or ai_payload.get("last_ok"))):
                        ai_payload = state.get("claude_suggestions")
                    gs.ingest(ai_payload, t0)

            # Before display_rows() fires on_change -> _st_alert.
            if st is not None:
                _st_ctx["by_symbol"] = st.by_symbol
            if gs is not None:
                _claude_ctx["by_symbol"] = gs.by_symbol

            # Prices from momentum feed (fallback for ST / Claude filter)
            price_map: dict[str, float | None] = {}
            for r in feed.rows:
                s = str(r.get("ticker") or "").upper()
                if s:
                    try:
                        price_map[s] = float(r["price"]) if r.get("price") is not None else None
                    except (TypeError, ValueError):
                        price_map[s] = None

            st_rank = {}
            st_ordered: list[str] = []
            if st is not None:
                st_rank = {sym: int(row["rank"])
                           for sym, row in st.by_symbol.items()
                           if row.get("rank") is not None}
                st_ordered = [r["symbol"] for r in
                              st.display_rows(price_map, limit=st_limit,
                                              on_change=_st_alert, now=t0)]

            claude_ordered: list[str] = []
            if gs is not None:
                claude_ordered = [r["symbol"] for r in
                                gs.display_rows(price_map, limit=claude_limit,
                                                on_change=_claude_alert, now=t0)]

            ordered = [str(r.get("ticker") or "").upper()
                       for r in feed.rows[:hotkey_slots]]
            hotkeys.update(
                ordered,
                st_ordered if st_on else None,
                claude_ordered if claude_on else None,
            )

            circle = readout_strip = None
            if cfg.get("buy_circle_enabled", True):
                # Falling back without saying so is the failure this guards.
                if chart_watcher is not None and chart_watcher.available:
                    # Read straight off the chart: works for whatever symbol
                    # is charted, needs no engine, and cannot disagree with
                    # what is on screen. `stale` is the dashboard feed, which
                    # this source does not depend on.
                    prox, lines, csym = chart_watcher.snapshot()
                    csym = csym or chart_symbol.get(hotkeys, t0)
                    if prox is None:
                        # Say why the SCREEN read failed. Handing a None to
                        # buy_circle() gets "untracked", which is the engine's
                        # vocabulary — it means "not in the tracked set", and
                        # this source has no tracked set. A wrong reason is
                        # worse than none: it sends you to fix the engine.
                        cstate = "unknown"
                        cdetail = chart_watcher.error() or "no chart read"
                    else:
                        cstate, cdetail = buy_circle(
                            {"signal_proximity": prox}, cfg)
                    readout_strip = chart_readout_markup(lines)
                    # Prefer the direction arrow when the chart gave us one: it
                    # answers "which way is this going", which a dot cannot.
                    # Falls through to the dot when the trend is unreadable, so
                    # the corner is never blank.
                    if cfg.get("buy_circle_arrow", True):
                        circle = trend_markup(
                            (prox or {}).get("chart_trend"), csym)
                else:
                    csym = chart_symbol.get(hotkeys, t0)
                    # A dead feed must not leave a stale green sitting in the
                    # corner — the row we would read is however old the last
                    # poll was, which is exactly what `stale` means.
                    erow = feed.row_for(csym)
                    cstate, cdetail = (("unknown", "stale") if stale
                                       else buy_circle(erow, cfg))
                    readout_strip = engine_readout_markup(erow)
                # The dot is the fallback: engine source, or a chart read with
                # no legible trend. Only fills in if the arrow did not.
                if circle is None:
                    circle = circle_markup(cstate, cdetail, csym)

            panels = [header_panel(feed, t0, hz, stale, circle, readout_strip)]
            panels.append(momentum_table(
                feed, t0, hz, hotkeys.enabled,
                rsi_focus_max, pctr_focus_lo, pctr_focus_hi, st_rank,
                history=history, cfg=cfg))
            if st is not None:
                panels.append(stocktwits_panel(
                    st, price_map, limit=st_limit, hotkeys_on=hotkeys.enabled,
                    cfg=cfg, now=t0))
            if gs is not None:
                panels.append(claude_panel(
                    gs, price_map, limit=claude_limit, hotkeys_on=hotkeys.enabled,
                    cfg=cfg, now=t0))
            # The AI desk's own book, reported by the server. Kept separate
            # from the manual book below: they can be different accounts, and
            # merging them would hide which one a position belongs to.
            claude_pos = ((state or {}).get("ai_positions")
                          or (state or {}).get("claude_positions")
                          or {})
            if claude_pos.get("positions") or claude_pos.get("open_orders"):
                panels.append(positions_panel(
                    claude_pos.get("positions"),
                    hotkeys.focus_symbol(),
                    open_orders=claude_pos.get("open_orders"),
                    mode=claude_pos.get("mode", "paper"),
                    error=claude_pos.get("error", ""),
                    label="AI POSITIONS",
                ))
            if positions_on and _pos_cache["mode"] not in ("", "off"):
                panels.append(positions_panel(
                    _pos_cache["positions"],
                    hotkeys.focus_symbol(),
                    open_orders=_pos_cache["orders"],
                    mode=_pos_cache["mode"],
                    error=_pos_cache["error"],
                    label="DESK POSITIONS",
                ))
            panels.append(footer_panel(
                alerter, hotkeys, hotkey_slots, st_on=st_on, claude_on=claude_on))
            live.update(Group(*panels))
            journal.maybe_flush(t0)
            time.sleep(max(0.0, interval - (time.time() - t0)))


def _audition_sounds() -> None:
    """Play each alert kind's sound in turn so they can be chosen by ear.

    Names like "Bottle" and "Sosumi" say nothing about what they sound like, and
    picking an alert tone off a list is how you end up with one you resent at
    04:00. Every sound macOS ships is listed after the mapped ones.
    """
    cfg = load_config()
    volume = float(cfg.get("alert_sound_volume", 0.35))
    console = Console()
    console.print(f"[bold]Alert sounds[/bold]  volume={volume}  "
                  f"(set alert_sound_volume / alert_sound_by_kind in "
                  f"momentum_config.json)\n")

    label = {"new": "a symbol appeared", "st_new": "new on Stocktwits",
             "mflow": "mention flow building", "burst": "mention burst",
             "st_look": "LOOK badge", "focus": "FOCUS setup fired",
             "buy": "buy signal"}
    for kind, sound in ALERT_SOUNDS.items():
        console.print(f"  {kind:9s} {sound:10s} [dim]{label.get(kind, '')}[/dim]")
        _play(os.path.join(_SOUND_DIR, f"{sound}.aiff"), volume)
        time.sleep(0.35)

    others = sorted({p[:-5] for p in os.listdir(_SOUND_DIR) if p.endswith(".aiff")}
                    - set(ALERT_SOUNDS.values())) if os.path.isdir(_SOUND_DIR) else []
    if others:
        console.print("\n[dim]Also available (unmapped):[/dim]")
        for sound in others:
            console.print(f"  {'':9s} {sound}")
            _play(os.path.join(_SOUND_DIR, f"{sound}.aiff"), volume)
            time.sleep(0.35)


if __name__ == "__main__":
    try:
        if "--sounds" in sys.argv:
            _audition_sounds()
        else:
            main()
    except KeyboardInterrupt:
        sys.exit(0)
