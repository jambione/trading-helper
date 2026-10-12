#!/usr/bin/env python3
"""Sell double-check for open positions. Read-only: it does not place orders.

For each open name, rebuild the desk's two %R lines (fast 21 / EWM 7, slow
112 / EWM 3) on today's minute bars and fold the current price in as the
live close, the same step ``ai_entry_watch._live_percent_r_line`` uses.
The call uses that live reading together with the prior lookback minutes.
A line is trending down when the live reading has come off the high of
that window by any amount. The VZ 1-minute rollover (both lines bending
off a high near the top of the pane, still around -27) is a sell. No
minimum point move.

  both lines trending down                         → sell yes
  both up, both flat, or only one line turned     → sell no
  no full lookback, no bars, or no live price      → sell yes

Dust smaller than the desk's ``rte_direction_eps`` (0.05) is not a turn.

Exit 1 when any open name is sell yes, 0 when every name is sell no or
the book is empty.

Usage (on the mini, from the repo):

    python3 tools/sell_double_check.py
    python3 tools/sell_double_check.py --json
    python3 tools/sell_double_check.py --lookback 10
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

ET = ZoneInfo("America/New_York")

LOOKBACK_MIN = 10
# Desk rte_direction_eps. A smaller dip is not a turn.
DIRECTION_EPS = 0.05
FAST_LENGTH = 21
FAST_SPAN = 7.0
SLOW_LENGTH = 112
SLOW_SPAN = 3.0


def _raw_percent_r(hh: float, ll: float, close: float) -> float | None:
    span = hh - ll
    if span <= 0:
        return None
    return -100.0 * (hh - close) / span


def smoothed_at_closes(
    rows: list[tuple[float, float, float]],
    length: int,
    span: float,
) -> list[float | None]:
    """Smoothed %R at each closed bar. None until the window is full.

    Same raw window and EWM as ``_live_percent_r_line``: undefined bars
    (zero range) are skipped, and the smoother carries forward.
    """
    length = max(2, int(length))
    alpha = 2.0 / (max(1.0, float(span)) + 1.0)
    out: list[float | None] = [None] * len(rows)
    sm: float | None = None
    for i in range(len(rows)):
        if i + 1 < length:
            continue
        win = rows[i - length + 1:i + 1]
        hh = max(r[0] for r in win)
        ll = min(r[1] for r in win)
        raw = _raw_percent_r(hh, ll, win[-1][2])
        if raw is None:
            continue
        sm = raw if sm is None else alpha * raw + (1.0 - alpha) * sm
        out[i] = sm
    return out


def live_value(
    rows: list[tuple[float, float, float]],
    price: float,
    length: int,
    span: float,
) -> float | None:
    """Current smoothed %R with ``price`` as the forming close.

    Delegates to the desk function so this report and the live book share
    one realtime reading. ``clock_range`` (a short window that is not a
    real %R of ``length``) is not a reading.
    """
    from ai_entry_watch import _live_percent_r_line

    got = _live_percent_r_line(
        rows, float(price), int(length), float(span), 0.0, min_range=int(length))
    if got is None or got[3] != "live":
        return None
    return float(got[0])


def line_window(
    rows: list[tuple[float, float, float]],
    price: float,
    length: int,
    span: float,
    lookback: int,
) -> list[float] | None:
    """Last ``lookback - 1`` closed readings plus the live reading.

    None when either piece is missing. A hole in the closed tail is a
    missing lookback, not a value we invent.
    """
    lookback = int(lookback)
    if lookback < 2 or price is None or price <= 0 or not rows:
        return None
    live = live_value(rows, price, length, span)
    if live is None:
        return None
    need = lookback - 1
    hist = smoothed_at_closes(rows, length, span)
    if len(hist) < need:
        return None
    tail = hist[-need:]
    if any(v is None for v in tail):
        return None
    return [float(v) for v in tail] + [live]


def line_trend(values: list[float] | None, eps: float = DIRECTION_EPS) -> str:
    """``up``, ``down``, ``sideways``, or ``no_lookback``.

    Down means the live reading (the last point) has come off the high of
    the rest of the window by more than ``eps``. Any such dip counts. Up
    means the live reading is a new high. Sitting on the high is sideways.
    """
    if not values or any(v is None for v in values) or len(values) < 2:
        return "no_lookback"
    live = float(values[-1])
    prior_high = max(float(v) for v in values[:-1])
    if live < prior_high - abs(eps):
        return "down"
    if live > prior_high + abs(eps):
        return "up"
    return "sideways"


def decide(
    fast: list[float] | None,
    slow: list[float] | None,
    *,
    lookback: int,
    eps: float = DIRECTION_EPS,
) -> dict:
    """Sell call from the two lookback windows. Each window ends at the live reading."""
    lookback = int(lookback)
    fast_w = list(fast[-lookback:]) if fast else None
    slow_w = list(slow[-lookback:]) if slow else None
    if (
        not fast_w or not slow_w
        or len(fast_w) < lookback or len(slow_w) < lookback
    ):
        fast_trend = line_trend(fast_w, eps) if fast_w and len(fast_w) >= lookback else "no_lookback"
        slow_trend = line_trend(slow_w, eps) if slow_w and len(slow_w) >= lookback else "no_lookback"
        return _row(
            sell="yes", trend="no_lookback",
            fast_trend=fast_trend, slow_trend=slow_trend,
            fast=fast_w, slow=slow_w, lookback=lookback,
            reason="no %R lookback",
        )
    fast_trend = line_trend(fast_w, eps)
    slow_trend = line_trend(slow_w, eps)
    if fast_trend == "no_lookback" or slow_trend == "no_lookback":
        trend, sell, reason = "no_lookback", "yes", "no %R lookback"
    elif fast_trend == "down" and slow_trend == "down":
        trend, sell, reason = "down", "yes", "both %R lines trending down"
    elif fast_trend == "up" and slow_trend == "up":
        trend, sell, reason = "up", "no", "both %R lines trending up"
    else:
        trend, sell, reason = "sideways", "no", "up or sideways"
    return _row(
        sell=sell, trend=trend,
        fast_trend=fast_trend, slow_trend=slow_trend,
        fast=fast_w, slow=slow_w, lookback=lookback, reason=reason,
    )


def _row(*, sell, trend, fast_trend, slow_trend, fast, slow, lookback, reason) -> dict:
    def end(vals, i):
        if not vals or len(vals) <= i or vals[i] is None:
            return None
        return round(float(vals[i]), 2)

    return {
        "sell": sell,
        "trend": trend,
        "fast_trend": fast_trend,
        "slow_trend": slow_trend,
        "fast_now": end(fast, -1),
        "slow_now": end(slow, -1),
        "fast_then": end(fast, 0),
        "slow_then": end(slow, 0),
        "lookback": lookback,
        "reason": reason,
    }


def closed_rows(
    stamps: list[float] | None,
    highs: list[float],
    lows: list[float],
    closes: list[float],
    now: float | None,
) -> list[tuple[float, float, float]]:
    """Closed minutes only. The current minute is the live price, not a bar."""
    minute = None if now is None else (int(float(now)) // 60) * 60
    rows: list[tuple[float, float, float]] = []
    for i, (h, lo, c) in enumerate(zip(highs, lows, closes)):
        if (
            minute is not None and stamps is not None and i < len(stamps)
            and float(stamps[i]) >= minute
        ):
            continue
        rows.append((float(h), float(lo), float(c)))
    return rows


def evaluate_symbol(
    symbol: str,
    bars: tuple | None,
    price: float | None,
    *,
    lookback: int = LOOKBACK_MIN,
    eps: float = DIRECTION_EPS,
    now: float | None = None,
    fast_length: int = FAST_LENGTH,
    fast_span: float = FAST_SPAN,
    slow_length: int = SLOW_LENGTH,
    slow_span: float = SLOW_SPAN,
) -> dict:
    """One position. ``bars`` is ``(stamps, highs, lows, closes)`` or None."""
    base = {
        "symbol": str(symbol or "").upper(),
        "price": None if price is None else round(float(price), 4),
        "lookback": int(lookback),
    }
    if price is None or float(price) <= 0:
        base.update(decide(None, None, lookback=lookback, eps=eps))
        base["trend"] = "no_realtime"
        base["reason"] = "no realtime price"
        base["sell"] = "yes"
        return base
    if not bars or len(bars) != 4 or not bars[1]:
        base.update(decide(None, None, lookback=lookback, eps=eps))
        base["reason"] = "no bars"
        return base
    stamps, highs, lows, closes = bars
    rows = closed_rows(stamps, list(highs), list(lows), list(closes), now)
    fast = line_window(rows, float(price), fast_length, fast_span, lookback)
    slow = line_window(rows, float(price), slow_length, slow_span, lookback)
    base.update(decide(fast, slow, lookback=lookback, eps=eps))
    return base


def iter_open(state: dict | None, broker: dict | None) -> list[dict]:
    """Open names. Broker wins when it answered; otherwise the managed book.

    A broker ``{}`` is a flat book. ``None`` means the broker did not answer.
    """
    out: list[dict] = []
    if isinstance(broker, dict):
        for sym, row in broker.items():
            if not isinstance(row, dict):
                continue
            qty = _num(row.get("qty"))
            if qty is None or qty <= 0:
                continue
            px = _num(row.get("current") or row.get("current_price"))
            out.append({
                "symbol": str(sym).upper(),
                "qty": qty,
                "price": px if px and px > 0 else None,
            })
        out.sort(key=lambda r: r["symbol"])
        return out
    for sym, pos in (state or {}).items():
        if not isinstance(pos, dict):
            continue
        if pos.get("closing_reason"):
            continue
        qty = _num(pos.get("qty"))
        if qty is None:
            qty = (_num(pos.get("qty_a")) or 0) + (_num(pos.get("qty_b")) or 0)
        if qty <= 0:
            continue
        out.append({"symbol": str(sym).upper(), "qty": qty, "price": None})
    out.sort(key=lambda r: r["symbol"])
    return out


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def lengths_from_cfg(cfg: dict | None) -> dict:
    cfg = cfg or {}
    def _i(key, default):
        try:
            return max(2, int(cfg.get(key) or default))
        except (TypeError, ValueError):
            return default
    def _f(key, default):
        try:
            return float(cfg.get(key) or default)
        except (TypeError, ValueError):
            return default
    return {
        "fast_length": _i("rte_fast_length", FAST_LENGTH),
        "fast_span": _f("rte_fast_ewm_span", FAST_SPAN),
        "slow_length": _i("rte_slow_native_length", SLOW_LENGTH),
        "slow_span": _f("rte_slow_ewm_span", SLOW_SPAN),
    }


def fetch_session_bars(symbols: list[str], now_et: datetime) -> dict[str, tuple] | None:
    """1-minute bars from 04:00 ET, so the slow line can be warm by the open.

    None when the data client cannot be asked. Missing symbols are absent,
    which the caller treats as no bars.
    """
    import alpaca_api
    from tools import bars

    client = bars.client()
    if client is None or not symbols:
        return None if client is None else {}
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    start = now_et.replace(hour=4, minute=0, second=0, microsecond=0)
    if now_et < start:
        return {}
    try:
        from config import load_config
        cfg = load_config()
    except Exception:  # noqa: BLE001
        cfg = {}
    try:
        df = client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=list(symbols),
            timeframe=TimeFrame(1, TimeFrameUnit.Minute),
            start=start.astimezone(timezone.utc),
            extended_hours=True,
            **alpaca_api._get_feed_arg(cfg),
        )).df
    except Exception:  # noqa: BLE001
        return None
    return _frames_to_bars(df, symbols)


def _frames_to_bars(df, symbols: list[str]) -> dict[str, tuple]:
    import pandas as pd
    if df is None or getattr(df, "empty", True):
        return {}
    out: dict[str, tuple] = {}
    for sym in symbols:
        try:
            part = df.xs(sym, level="symbol") if isinstance(df.index, pd.MultiIndex) else df
        except KeyError:
            continue
        if part is None or part.empty:
            continue
        part = part.sort_index()
        stamps = [float(t.timestamp()) for t in part.index]
        out[str(sym).upper()] = (
            stamps,
            [float(v) for v in part["high"]],
            [float(v) for v in part["low"]],
            [float(v) for v in part["close"]],
        )
    return out


def fill_prices(rows: list[dict]) -> None:
    """Fill a missing price from the latest trade. Leaves None when it cannot."""
    need = [r["symbol"] for r in rows if not r.get("price")]
    if not need:
        return
    import alpaca_api
    from tools import bars

    client = bars.client()
    if client is None:
        return
    try:
        from config import load_config
        cfg = load_config()
    except Exception:  # noqa: BLE001
        cfg = {}
    for sym in need:
        px = alpaca_api.get_latest_trade_price(client, sym, cfg)
        if px and px > 0:
            for r in rows:
                if r["symbol"] == sym:
                    r["price"] = float(px)


def run_check(
    positions: list[dict],
    bars_by_symbol: dict[str, tuple] | None,
    *,
    lookback: int,
    now: float | None,
    lengths: dict,
    eps: float = DIRECTION_EPS,
) -> list[dict]:
    rows = []
    for pos in positions:
        sym = pos["symbol"]
        bars = None if bars_by_symbol is None else bars_by_symbol.get(sym)
        rows.append(evaluate_symbol(
            sym, bars, pos.get("price"),
            lookback=lookback, eps=eps, now=now, **lengths,
        ))
    return rows


def format_report(rows: list[dict], *, lookback: int, now_et: datetime) -> str:
    clock = now_et.strftime("%H:%M ET")
    lines = [
        f"sell double-check  {clock}  lookback {lookback}m",
        (
            "sell yes = both %R lines have turned down at all, or no lookback. "
            "sell no = up or sideways."
        ),
    ]
    if not rows:
        lines.append("no open positions")
        return "\n".join(lines)
    for r in rows:
        fast = _pair(r.get("fast_then"), r.get("fast_now"), r.get("fast_trend"))
        slow = _pair(r.get("slow_then"), r.get("slow_now"), r.get("slow_trend"))
        px = r.get("price")
        px_s = f"{px:.2f}" if isinstance(px, (int, float)) else "n/a"
        lines.append(
            f"{r['symbol']:<6} sell {r['sell']:<3} {r['trend']:<12} "
            f"fast {fast:<22} slow {slow:<22} px {px_s}  {r['reason']}"
        )
    return "\n".join(lines)


def _pair(then, now, trend) -> str:
    if then is None or now is None:
        return "n/a"
    return f"{then:.1f}->{now:.1f} {trend}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Read-only sell double-check from percent-R lookback plus the live reading.")
    p.add_argument("--lookback", type=int, default=LOOKBACK_MIN,
                   help="minutes of %%R history, ending at the live reading")
    p.add_argument("--json", action="store_true")
    p.add_argument("--symbol", action="append", default=[],
                   help="limit the report to these open symbols")
    args = p.parse_args(argv)
    lookback = max(2, int(args.lookback))

    import ai_positions as ap
    try:
        import alpaca_trader
        broker = alpaca_trader.get_positions_detail()
    except Exception:  # noqa: BLE001
        broker = None
    try:
        state = ap._load_state()
    except Exception:  # noqa: BLE001
        state = {}
    positions = iter_open(state, broker)
    if args.symbol:
        want = {s.upper() for s in args.symbol}
        positions = [r for r in positions if r["symbol"] in want]
    fill_prices(positions)

    now_et = datetime.now(ET)
    now = now_et.timestamp()
    bars_by = None
    if positions:
        try:
            from config import load_config
            lengths = lengths_from_cfg(load_config())
        except Exception:  # noqa: BLE001
            lengths = lengths_from_cfg({})
        bars_by = fetch_session_bars([r["symbol"] for r in positions], now_et)
    else:
        lengths = lengths_from_cfg({})
    rows = run_check(
        positions, bars_by, lookback=lookback, now=now, lengths=lengths)
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        print(format_report(rows, lookback=lookback, now_et=now_et))
    return 1 if any(r["sell"] == "yes" for r in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
