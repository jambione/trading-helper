#!/usr/bin/env python3
"""Are the engine %R and the chart %R the same line?

The chart line is the TradingView %R Trend Exhaustion port: fast %R(21)
EMA(7) and slow %R(112) EMA(3), counted in minutes (gaps of 15 minutes or
less filled flat). The engine line is what
``strategy_three_indicator.compute_indicators`` computes and what
``evaluate_state`` publishes on the wire. Those two have to be the same
series. The desk's clock-window calculator (``live_exhaustion_pair``) is a
different reading. It is printed in its own column and is never treated as
the chart.

The arm scored here is the live rule: empty-square only (both lines at or
above −35, tighter than 15, fast line rising, two polls), and a full square
does not open. Exits are the trail, not leave-overbought.

Read-only. Exit 0 when the engine line matches the chart line. Exit 1 when
it does not. Exit 2 when there was nothing to measure.

    .venv/bin/python tools/pctr_line_probe.py --wire
    .venv/bin/python tools/pctr_line_probe.py AAPL
    .venv/bin/python tools/pctr_line_probe.py AAPL --feed sip
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import signals  # noqa: E402
import strategy_three_indicator as three_ind  # noqa: E402

# Live empty-square entry. Trail exit is the edge mode: leave-overbought stays off.
RULE = {
    "ai_watch_arm_mode": "last",
    "ai_watch_presquare_only": True,
    "ai_watch_exh_square_arm": True,
    "ai_watch_exh_presquare_arm": True,
    "ai_watch_square_require_rising": True,
    "ai_watch_exh_oversold_arm": False,
    "ai_watch_exh_heating_with_square": False,
    "ai_watch_exh_mid_rise_arm": False,
    "ai_watch_tv_exh_rsi": False,
    "ai_watch_require_live_pctr": True,
    "ai_watch_require_exh_rising": True,
    "ai_watch_require_exhaustion_data": True,
    "ai_watch_exhaustion_rules": True,
    "ai_watch_exhaustion_live": True,
    "ai_edge_mode": "continuation",
    "ai_exit_left_overbought": False,
    "rte_threshold": 20,
    "rte_confluence_max": 15.0,
    "ai_watch_exh_pre_thr": 35.0,
    "ai_watch_square_min_count": 2,
    "ai_watch_square_max_age_sec": 60.0,
    "rte_fast_length": 21,
    "rte_fast_ewm_span": 7,
    "rte_slow_native_length": 112,
    "rte_slow_ewm_span": 3,
    "rte_minute_grid": True,
    "rte_direction_eps": 0.05,
    "trend_lookback": 2,
    "rte_slow_timeframe": None,
}

# Engine publish rounds to 1 decimal. A larger gap is a different line.
PUBLISH_ATOL = 0.051
SERIES_ATOL = 1e-8
# Desk clock-window vs the chart. Printed, and it fails the probe only when
# that fallback would open a different empty-square trade.
ARM_NOTE = "empty-square only, trail exit"

WIRE_ATOL = 0.15
PROBE_SYMBOL = "PCTRPROBE"


def _finite(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(x):
        return None
    return x


def _fmt(v, nd=1) -> str:
    x = _finite(v)
    if x is None:
        return "—"
    return f"{x:.{nd}f}"


def prepare_frame(df: pd.DataFrame) -> pd.DataFrame:
    """OHLC plus a ``time`` column so the minute grid can run.

    A DatetimeIndex with no ``time`` column used to make the engine fall
    through to row-counted %R. The chart counts minutes. The probe gives
    both paths the same clock.
    """
    out = df.copy()
    for col in ("high", "low", "close"):
        if col not in out.columns:
            raise ValueError(f"bars need a {col} column")
    if "time" not in out.columns:
        idx = out.index
        if isinstance(idx, pd.DatetimeIndex):
            clock = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
            out["time"] = [t.isoformat() for t in clock]
        else:
            start = pd.Timestamp("2026-01-02T14:30:00Z")
            out["time"] = [
                (start + pd.Timedelta(minutes=i)).isoformat() for i in range(len(out))
            ]
    return out


def _direction(series: pd.Series, lookback: int) -> tuple[bool, bool]:
    """(rising, falling) over ``lookback`` bars. Same test as the engine."""
    lookback = max(1, int(lookback))
    if len(series) <= lookback:
        return False, False
    now = _finite(series.iloc[-1])
    then = _finite(series.iloc[-1 - lookback])
    if now is None or then is None:
        return False, False
    return now > then, now < then


def _row_span_min(df: pd.DataFrame, length: int) -> float | None:
    if "time" not in df.columns or len(df) < 2:
        return None
    t = pd.to_datetime(df["time"], utc=True, format="mixed")
    if t.isna().any() or len(t) < 2:
        return None
    tail = t.iloc[-min(length, len(t)):]
    return float((tail.iloc[-1] - tail.iloc[0]).total_seconds()) / 60.0


def chart_line(df: pd.DataFrame, cfg: dict | None = None) -> dict:
    """Minute-grid dual %R. This is the chart line."""
    cfg = {**RULE, **(cfg or {})}
    frame = prepare_frame(df)
    out = signals.compute_percent_r_exhaustion(frame, cfg)
    look = int(cfg.get("trend_lookback", 2) or 2)
    fast_up, fast_dn = _direction(out["s_percentR"], look)
    slow_up, slow_dn = _direction(out["l_percentR"], look)
    last = out.iloc[-1]
    fast = _finite(last.get("s_percentR"))
    slow = _finite(last.get("l_percentR"))
    gap = None if fast is None or slow is None else abs(fast - slow)
    return {
        "fast": fast,
        "slow": slow,
        "gap": gap,
        "rising": fast_up,
        "falling": fast_dn,
        "slow_rising": slow_up,
        "slow_falling": slow_dn,
        "ob": bool(last.get("overbought")) if fast is not None and slow is not None else False,
        "tight": bool(last.get("rte_tight")) if fast is not None and slow is not None else False,
        "bars": int(len(frame)),
        "fast_row_span_min": _row_span_min(frame, int(cfg["rte_fast_length"])),
        "slow_row_span_min": _row_span_min(frame, int(cfg["rte_slow_native_length"])),
        "series_fast": out["s_percentR"],
        "series_slow": out["l_percentR"],
        "frame": frame,
    }


def engine_line(df: pd.DataFrame, cfg: dict | None = None) -> dict:
    """What the signal engine computes and what it would publish."""
    cfg = {**RULE, **(cfg or {})}
    frame = prepare_frame(df)
    params = three_ind.params(
        rte_threshold=cfg["rte_threshold"],
        rte_fast_length=cfg["rte_fast_length"],
        rte_slow_native_length=cfg["rte_slow_native_length"],
        rte_slow_timeframe=cfg.get("rte_slow_timeframe"),
        rte_fast_ewm_span=cfg["rte_fast_ewm_span"],
        rte_slow_ewm_span=cfg["rte_slow_ewm_span"],
        rte_minute_grid=cfg["rte_minute_grid"],
        rte_confluence_max=cfg["rte_confluence_max"],
        trend_lookback=cfg["trend_lookback"],
    )
    ind = three_ind.compute_indicators(frame, params)
    arrays = three_ind.to_arrays(ind)
    published = three_ind.evaluate_state(arrays, len(frame) - 1, params)
    last_fast = _finite(ind["s_percentR"].iloc[-1])
    last_slow = _finite(ind["l_percentR"].iloc[-1])
    return {
        "fast": last_fast,
        "slow": last_slow,
        "gap": None if last_fast is None or last_slow is None else abs(last_fast - last_slow),
        "published_fast": _finite(published.get("pctr")),
        "published_slow": _finite(published.get("pctr_slow")),
        "rising": bool(published.get("pctr_rising")),
        "falling": bool(published.get("pctr_falling")),
        "slow_rising": bool(published.get("pctr_slow_rising")),
        "slow_falling": bool(published.get("pctr_slow_falling")),
        "series_fast": ind["s_percentR"],
        "series_slow": ind["l_percentR"],
    }


def _ohlc_rows(df: pd.DataFrame) -> list[tuple[float, float, float]]:
    frame = prepare_frame(df)
    rows = []
    for h, lo, c in zip(frame["high"], frame["low"], frame["close"]):
        fh, fl, fc = _finite(h), _finite(lo), _finite(c)
        if fh is None or fl is None or fc is None:
            continue
        rows.append((fh, fl, fc))
    return rows


def desk_local_line(df: pd.DataFrame, cfg: dict | None = None) -> dict:
    """Clock-window calculator the desk uses when it is not copying the engine.

    A short window comes back ``clock_range``: raw position-in-range, no
    EMA. That number is not the chart line.
    """
    import ai_entry_watch as ew

    cfg = {**RULE, **(cfg or {})}
    rows = _ohlc_rows(df)
    if not rows:
        return {"fast": None, "slow": None, "src": None, "slow_src": None}
    price = rows[-1][2]
    eps = float(cfg["rte_direction_eps"])
    fast = ew._live_percent_r_line(
        rows, price, int(cfg["rte_fast_length"]), float(cfg["rte_fast_ewm_span"]),
        eps, min_range=min(6, int(cfg["rte_fast_length"])),
    )
    slow = ew._live_percent_r_line(
        rows, price, int(cfg["rte_slow_native_length"]), float(cfg["rte_slow_ewm_span"]),
        eps, min_range=int(cfg["rte_slow_native_length"]),
    )
    return {
        "fast": None if fast is None else fast[0],
        "slow": None if slow is None else slow[0],
        "rising": False if fast is None else bool(fast[1]),
        "falling": False if fast is None else bool(fast[2]),
        "src": None if fast is None else fast[3],
        "slow_src": None if slow is None else slow[3],
        "bars": len(rows),
    }


def _clear_streaks(sym: str) -> None:
    import ai_entry_watch as ew

    sym = str(sym or "").upper()
    for store in (
        ew._SQUARE_STREAK, ew._PRESQUARE_STREAK, ew._OS_STREAK,
        ew._OS_QUALIFIED, ew._OS_LEFT,
    ):
        store.pop(sym, None)


def arm_decision(
    *,
    fast,
    slow,
    rising: bool,
    falling: bool = False,
    slow_rising: bool = False,
    slow_falling: bool = False,
    src: str = "live",
    cfg: dict | None = None,
    symbol: str = PROBE_SYMBOL,
) -> tuple[bool, str, str]:
    """(opens, reason, seat) under the empty-square rule.

    Two polls, so the two-poll confirm is included. A full square that the
    exhaustion gate would call ``overbought`` comes back ``not_presquare``.
    """
    import ai_entry_watch as ew

    cfg = {**RULE, **(cfg or {})}
    sym = str(symbol or PROBE_SYMBOL).upper()
    _clear_streaks(sym)
    ind = {
        "pctr": _finite(fast),
        "pctr_slow": _finite(slow),
        "pctr_rising": bool(rising),
        "pctr_falling": bool(falling),
        "pctr_slow_rising": bool(slow_rising),
        "pctr_slow_falling": bool(slow_falling),
        "pctr_src": src or "live",
    }
    rec = {"symbol": sym, "indicator": ind}
    seat, _gap = ew.classify_exh_seat(rec, cfg)
    why = ""
    ok = False
    t0 = 1_700_000_000.0
    try:
        for k in range(2):
            ok, why = ew.exhaustion_allows_buy(rec, cfg, now=t0 + k)
        if ok and why != "presquare" and bool(cfg.get("ai_watch_presquare_only", True)):
            return False, "not_presquare", seat
        return bool(ok), str(why), seat
    finally:
        _clear_streaks(sym)


def _series_gap(a: pd.Series, b: pd.Series) -> float | None:
    if len(a) != len(b) or len(a) == 0:
        return None
    av = a.to_numpy(dtype=float)
    bv = b.to_numpy(dtype=float)
    if not np.allclose(av, bv, equal_nan=True, atol=SERIES_ATOL, rtol=0):
        mask = np.isfinite(av) & np.isfinite(bv)
        if not mask.any():
            return None
        return float(np.nanmax(np.abs(av[mask] - bv[mask])))
    return 0.0


def compare(df: pd.DataFrame, cfg: dict | None = None) -> dict:
    """Engine series and publish against the chart line.

    ``ok`` is true only when the series match, the wire is the chart value
    rounded to 0.1, and both lines would arm the same way.
    """
    cfg = {**RULE, **(cfg or {})}
    chart = chart_line(df, cfg)
    engine = engine_line(df, cfg)
    local = desk_local_line(df, cfg)
    mismatches: list[str] = []

    fast_gap = _series_gap(chart["series_fast"], engine["series_fast"])
    slow_gap = _series_gap(chart["series_slow"], engine["series_slow"])
    if fast_gap is None or fast_gap > SERIES_ATOL:
        mismatches.append(
            f"fast series differs (max {fast_gap if fast_gap is not None else 'n/a'})"
        )
    if slow_gap is None or slow_gap > SERIES_ATOL:
        mismatches.append(
            f"slow series differs (max {slow_gap if slow_gap is not None else 'n/a'})"
        )

    if chart["fast"] is None or engine["published_fast"] is None:
        mismatches.append("fast line missing on the chart or the engine wire")
    elif abs(engine["published_fast"] - round(chart["fast"], 1)) > PUBLISH_ATOL:
        mismatches.append(
            f"engine fast {_fmt(engine['published_fast'])} != "
            f"chart {_fmt(round(chart['fast'], 1))}"
        )
    if chart["slow"] is None or engine["published_slow"] is None:
        mismatches.append("slow line missing on the chart or the engine wire")
    elif abs(engine["published_slow"] - round(chart["slow"], 1)) > PUBLISH_ATOL:
        mismatches.append(
            f"engine slow {_fmt(engine['published_slow'])} != "
            f"chart {_fmt(round(chart['slow'], 1))}"
        )

    if chart["fast"] is not None and engine["published_fast"] is not None:
        if bool(engine["rising"]) != bool(chart["rising"]):
            mismatches.append(
                f"fast rising engine={bool(engine['rising'])} chart={bool(chart['rising'])}"
            )
        if bool(engine["falling"]) != bool(chart["falling"]):
            mismatches.append(
                f"fast falling engine={bool(engine['falling'])} chart={bool(chart['falling'])}"
            )

    arm_chart = arm_decision(
        fast=chart["fast"], slow=chart["slow"], rising=chart["rising"],
        falling=chart["falling"], slow_rising=chart["slow_rising"],
        slow_falling=chart["slow_falling"], src="live", cfg=cfg,
    )
    arm_engine = arm_decision(
        fast=engine["published_fast"], slow=engine["published_slow"],
        rising=engine["rising"], falling=engine["falling"],
        slow_rising=engine["slow_rising"], slow_falling=engine["slow_falling"],
        src="live", cfg=cfg, symbol=PROBE_SYMBOL + "E",
    )
    if arm_chart[:2] != arm_engine[:2] or arm_chart[2] != arm_engine[2]:
        mismatches.append(
            f"arm chart {arm_chart[1]}/{arm_chart[2]} != "
            f"engine {arm_engine[1]}/{arm_engine[2]}"
        )

    notes: list[str] = []
    if local.get("src") == "clock_range":
        notes.append("desk local fast is clock_range, not a 21-minute line")
    elif local.get("fast") is not None and chart["fast"] is not None:
        delta = abs(float(local["fast"]) - float(chart["fast"]))
        if delta > 1.0:
            notes.append(
                f"desk local fast {_fmt(local['fast'])} vs chart {_fmt(chart['fast'])} "
                f"({delta:.1f} apart)"
            )
    if local.get("slow") is None and chart["slow"] is not None:
        notes.append("desk local has no slow line")
    elif (
        local.get("slow") is not None and chart["slow"] is not None
        and abs(float(local["slow"]) - float(chart["slow"])) > 1.0
    ):
        notes.append(
            f"desk local slow {_fmt(local['slow'])} vs chart {_fmt(chart['slow'])}"
        )

    span = chart.get("fast_row_span_min")
    if span is not None and span > 21 * 1.25:
        notes.append(
            f"last 21 prints span {span:.0f} min — row %R is not the minute chart"
        )

    return {
        "ok": not mismatches,
        "mismatches": mismatches,
        "notes": notes,
        "chart": {k: v for k, v in chart.items() if not str(k).startswith("series") and k != "frame"},
        "engine": {k: v for k, v in engine.items() if not str(k).startswith("series")},
        "desk_local": local,
        "arm_chart": {"open": arm_chart[0], "why": arm_chart[1], "seat": arm_chart[2]},
        "arm_engine": {"open": arm_engine[0], "why": arm_engine[1], "seat": arm_engine[2]},
    }


def _pick(row: dict, key: str):
    ind = row.get("indicator") if isinstance(row.get("indicator"), dict) else {}
    if key in ind and ind.get(key) is not None:
        return ind.get(key)
    return row.get(key)


def wire_mismatches(engine: dict, book: dict) -> list[str]:
    """The book column against the engine wire. Both are point readings."""
    out: list[str] = []
    src = str(_pick(book, "pctr_src") or "").strip().lower()
    if src in ("clock_range", "sparse_window", "sparse"):
        out.append(f"book pctr_src={src}")
    for key, label in (("pctr", "fast"), ("pctr_slow", "slow")):
        ev = _finite(_pick(engine, key))
        bv = _finite(_pick(book, key))
        if ev is None and bv is None:
            continue
        if ev is None or bv is None:
            out.append(f"{label} missing on {'engine' if ev is None else 'book'}")
            continue
        if abs(ev - bv) > WIRE_ATOL:
            out.append(f"{label} engine {_fmt(ev)} book {_fmt(bv)}")
    er = _pick(engine, "pctr_rising")
    br = _pick(book, "pctr_rising")
    if er is not None and br is not None and bool(er) != bool(br):
        out.append(f"rising engine={bool(er)} book={bool(br)}")
    return out


def _load_json(path: str) -> dict | None:
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def compare_wire(root: str | None = None) -> tuple[list[str], list[str]]:
    """(mismatch lines, note lines) from signal_state vs the watch book."""
    root = root or ROOT
    state = _load_json(os.path.join(root, "signal_state.json"))
    book = _load_json(os.path.join(root, "ai_reports", "entry_watch_state.json"))
    notes: list[str] = []
    lines: list[str] = []
    if not state:
        return ["signal_state.json missing or empty"], notes
    if not book:
        return ["entry_watch_state.json missing or empty"], notes
    tickers = state.get("tickers") if isinstance(state.get("tickers"), dict) else {}
    if not tickers:
        notes.append("engine wire has no tickers")
    seated = 0
    for key, rec in book.items():
        if not isinstance(rec, dict):
            continue
        sym = str(rec.get("symbol") or key or "").upper().strip()
        if not sym or sym.startswith("_"):
            continue
        status = str(rec.get("status") or "").lower().strip()
        if status in ("closed", "expired", "cancelled", "dropped"):
            continue
        seated += 1
        eng = tickers.get(sym)
        if not isinstance(eng, dict):
            if _finite(_pick(rec, "pctr")) is not None:
                lines.append(f"{sym}  book has %R, engine wire has no row")
            continue
        bad = wire_mismatches(eng, rec)
        if bad:
            lines.append(f"{sym}  " + "; ".join(bad))
        else:
            lines.append(
                f"{sym}  MATCH  fast {_fmt(_pick(eng, 'pctr'))}  "
                f"slow {_fmt(_pick(eng, 'pctr_slow'))}"
            )
    if seated == 0:
        notes.append("watch book has no open seats")
    return lines, notes


def fetch_ohlc(symbol: str, feed: str = "iex") -> tuple[pd.DataFrame | None, str | None]:
    """(frame, error). Error is set when the client or the bars are missing."""
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import alpaca_api
    import bars as bars_mod

    client = bars_mod.client()
    if client is None:
        return None, "no Alpaca data client"
    df = alpaca_api.fetch_bars(client, symbol.upper(), {
        "bar_timeframe": "1Min",
        "bar_count": 400,
        "alpaca_bar_feed": feed,
    })
    if df is None or df.empty:
        return None, f"no 1-minute bars ({feed})"
    return df, None


def format_compare(symbol: str, result: dict) -> str:
    chart = result["chart"]
    engine = result["engine"]
    local = result["desk_local"]
    arm = result["arm_chart"]
    mark = "MATCH" if result["ok"] else "DIFFER"
    lines = [
        f"{symbol}  {mark}  {ARM_NOTE}",
        (
            f"  chart  fast {_fmt(chart.get('fast'))}  slow {_fmt(chart.get('slow'))}  "
            f"gap {_fmt(chart.get('gap'))}  rising {bool(chart.get('rising'))}  "
            f"bars {chart.get('bars')}  "
            f"21-print span {_fmt(chart.get('fast_row_span_min'), 0)}m"
        ),
        (
            f"  engine fast {_fmt(engine.get('published_fast'))}  "
            f"slow {_fmt(engine.get('published_slow'))}  "
            f"rising {bool(engine.get('rising'))}"
        ),
        (
            f"  desk local fast {_fmt(local.get('fast'))}  slow {_fmt(local.get('slow'))}  "
            f"src {local.get('src') or '—'}"
        ),
        (
            f"  arm {arm.get('why')}  seat {arm.get('seat')}  "
            f"open {bool(arm.get('open'))}"
        ),
    ]
    for note in result.get("notes") or []:
        lines.append(f"  note  {note}")
    for bad in result.get("mismatches") or []:
        lines.append(f"  differ  {bad}")
    return "\n".join(lines)


def _wire_ticker(symbol: str) -> dict | None:
    state = _load_json(os.path.join(ROOT, "signal_state.json"))
    tickers = (state or {}).get("tickers") if isinstance(state, dict) else None
    if not isinstance(tickers, dict):
        return None
    row = tickers.get(str(symbol or "").upper())
    return row if isinstance(row, dict) else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Compare the engine %R line to the chart %R line.")
    ap.add_argument("symbol", nargs="?", help="recompute both lines from recent 1-minute bars")
    ap.add_argument("--wire", action="store_true", help="compare signal_state.json to the watch book")
    ap.add_argument("--feed", default="iex", choices=("iex", "sip"))
    args = ap.parse_args(argv)

    if args.symbol and not args.wire:
        df, err = fetch_ohlc(args.symbol, args.feed)
        if err or df is None or df.empty:
            print(f"{args.symbol.upper()}  {err or 'no 1-minute bars'}", file=sys.stderr)
            return 2
        result = compare(df)
        print(format_compare(args.symbol.upper(), result))
        live = _wire_ticker(args.symbol)
        if live is not None:
            # REST bars are not the engine's Finnhub frame. Show the wire
            # beside the recompute; a gap here is a different tape, and the
            # formula check above is what says the two lines are one series.
            print(
                f"  wire   fast {_fmt(_pick(live, 'pctr'))}  "
                f"slow {_fmt(_pick(live, 'pctr_slow'))}  "
                f"src {_pick(live, 'bars_src') or '—'}"
            )
        return 0 if result["ok"] else 1

    lines, notes = compare_wire()
    print(f"wire  {ARM_NOTE}")
    for note in notes:
        print(f"  note  {note}")
    if not lines:
        print("  nothing on the book to compare")
        return 2
    if lines[0].startswith("signal_state") or lines[0].startswith("entry_watch"):
        for line in lines:
            print(f"  {line}")
        return 2
    failed = 0
    for line in lines:
        print(f"  {line}")
        if "MATCH" not in line.split():
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
