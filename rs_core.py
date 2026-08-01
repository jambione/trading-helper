#!/usr/bin/env python3
"""
rs_core.py — Relative-strength math. Pure functions, zero I/O.

This is the calculation half of the RS screener (rs_screener.py drives it,
rs_cache.py stores the bars, rs_fetch.py pulls them). Nothing here touches the
network, the clock, the filesystem or the config file, so every rule below is
testable against a synthetic frame — see tests/test_rs_core.py.

THE FORMULA
    rs_raw = 0.40·(P₀/P₆₃) + 0.20·(P₀/P₁₂₆) + 0.20·(P₀/P₁₈₉) + 0.20·(P₀/P₂₅₂)

Pₙ is the split-adjusted close n BENCHMARK sessions back — not n rows back, see
"same session" below. These are trailing windows all ending today, which is the
IBD form. rs_form="quarters" switches to the non-overlapping reading
(P₀/P₆₃, P₆₃/P₁₂₆, P₁₂₆/P₁₈₉, P₁₈₉/P₂₅₂), which is a genuinely different
statistic and different ranking — which is why every row carries rs_form.

rs_rating is then the cross-sectional percentile of rs_raw mapped to 1-99.

Four rules the rest of this module exists to enforce:

1. PRICE RETURNS, NOT TOTAL RETURNS. The bars are Adjustment.SPLIT, so dividends
   are excluded. A 4%-yielder's real 12-month total return is ~4pp above what we
   report. IBD measures price performance too, so this is the right choice, but
   it must be named. (Adjustment.ALL is not the answer: every dividend restates
   all prior bars, so an ALL cache needs repair on every ex-div date across the
   whole universe.)

2. THE PERCENTILE IS ONLY MEANINGFUL OVER THE POPULATION IT RANKED. Rank the
   full universe, then filter — never the reverse, which would make "RS 90" mean
   "top 10% of names that already passed a strength screen". stamp_ratings puts
   the population on every row so the number stays auditable. The same critique
   is already written down for medians in stocktwits_trending.py:502-505.

3. BOTH LEGS OF EVERY RATIO COME OFF THE SAME SESSION. A gappy symbol's
   closes.iloc[-63] is not 63 sessions ago, it is 63 *rows* ago, which silently
   compares the stock's 61st prior session to SPY's 63rd. align_frame_to_calendar
   reindexes onto the benchmark's own index first, and that is the only reason
   the positional access in anchor_close is safe. Coverage is necessary but NOT
   sufficient: a name that IPO'd 200 sessions ago has 79% coverage and simply has
   no P₂₅₂, so each anchor is required individually and the weights are NEVER
   renormalised over the survivors.

4. NEVER INVENT A VALUE. Short history yields None, never 0.0 and never 1.0.
   Watch signals.calc_rvol in particular — it returns a constant 1.0 Series below
   20 bars, a manufactured "unremarkable" that trailing_stats converts to None.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from signals import atr as _atr
from signals import calc_rvol as _calc_rvol
from signals import sma as _sma

# The IBD weighting. Keys are sessions back; values must sum to 1.0 so that a
# flat series scores exactly 1.0 and the number reads as "×the market".
RS_WEIGHTS: dict[int, float] = {63: 0.4, 126: 0.2, 189: 0.2, 252: 0.2}

# Plain return windows published alongside rs_raw for transparency.
RETURN_WINDOWS: dict[str, int] = {"1m": 21, "3m": 63, "6m": 126, "12m": 252}

RS_FORMS = ("trailing", "quarters")

_SMA_WINDOWS = (50, 200)
_VOL_WINDOW = 50
_ADR_WINDOW = 20
_RVOL_MIN_SESSIONS = 20   # signals.calc_rvol's own floor
_ATR_PERIOD = 14


# ── Alignment ─────────────────────────────────────────────────────────────────

def align_frame_to_calendar(
    bars: pd.DataFrame,
    calendar: pd.DatetimeIndex,
    ffill_limit: int = 2,
) -> tuple[pd.DataFrame, float]:
    """Reindex `bars` onto the benchmark's session index. Returns (frame, coverage).

    `coverage` is the fraction of `calendar` sessions that carried a REAL close,
    measured BEFORE any filling — otherwise the fill would launder the very gaps
    the caller is trying to detect.

    close/high/low are forward-filled up to `ffill_limit` sessions, because a
    price genuinely persists across a day with no print. `volume` is NOT filled:
    carrying volume forward manufactures liquidity that never traded, and it
    feeds avg_vol_50d and the liquidity filter.

    Sessions before the symbol's first real bar are left NaN rather than
    back-filled, so a recent IPO reads as "no P₂₅₂" instead of a flat year.

    The returned frame carries a `real_bar` boolean column recording which
    sessions were genuinely traded. Downstream code needs it: after the fill,
    a NaN check can no longer tell a real close from a carried one, and
    reporting a carried date as the stock's last print would be a quiet lie.
    """
    columns = ["close", "high", "low", "volume", "real_bar"]
    if bars is None or bars.empty or calendar is None or len(calendar) == 0:
        empty = pd.DataFrame(columns=columns, index=calendar)
        empty["real_bar"] = False
        return empty, 0.0

    frame = bars[[c for c in ("close", "high", "low", "volume") if c in bars.columns]].copy()
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    frame = frame.reindex(calendar)

    real = frame["close"].notna() if "close" in frame.columns else pd.Series(False, index=calendar)
    frame["real_bar"] = real.fillna(False).astype(bool)
    coverage = float(frame["real_bar"].sum()) / float(len(calendar))

    if ffill_limit and ffill_limit > 0:
        for col in ("close", "high", "low"):
            if col in frame.columns:
                frame[col] = frame[col].ffill(limit=int(ffill_limit))
    # Anything before the first real close stays NaN — ffill cannot reach back.
    return frame, coverage


# ── Returns ───────────────────────────────────────────────────────────────────

def anchor_close(closes: pd.Series, sessions_back: int) -> float | None:
    """The close `sessions_back` benchmark sessions before the last one.

    None when the slot is off the front of the series or holds no value. The
    positional access is only correct because `closes` is already aligned to the
    benchmark calendar — calling this on a raw, gappy series is the misalignment
    bug this module exists to prevent.
    """
    if closes is None or len(closes) == 0:
        return None
    idx = len(closes) - 1 - int(sessions_back)
    if idx < 0:
        return None
    value = closes.iloc[idx]
    if value is None or not np.isfinite(value):
        return None
    return float(value)


def period_return(closes: pd.Series, sessions_back: int) -> float | None:
    """P₀/Pₙ − 1 as a decimal fraction, or None when either end is absent."""
    p0 = anchor_close(closes, 0)
    pn = anchor_close(closes, sessions_back)
    if p0 is None or pn is None or pn == 0:
        return None
    return (p0 / pn) - 1.0


def rs_raw(closes: pd.Series, form: str = "trailing",
           weights: dict[int, float] | None = None) -> float | None:
    """The weighted relative-strength score, or None if ANY anchor is missing.

    The None is deliberate and load-bearing. Renormalising the weights over the
    three surviving terms would produce a 3-term blend — a different statistic —
    and ranking it against 4-term scores is the same category of error as
    ranking a filtered subset (see the module docstring, rule 2).
    """
    if form not in RS_FORMS:
        raise ValueError(f"unknown rs_form {form!r}; expected one of {RS_FORMS}")
    w = weights or RS_WEIGHTS
    anchors = sorted(w)                       # e.g. [63, 126, 189, 252]

    closes_by_anchor: dict[int, float] = {}
    for n in (0, *anchors):
        value = anchor_close(closes, n)
        if value is None or value == 0:
            return None
        closes_by_anchor[n] = value

    total = 0.0
    if form == "trailing":
        for n in anchors:
            total += w[n] * (closes_by_anchor[0] / closes_by_anchor[n])
    else:                                     # "quarters" — non-overlapping
        prev = 0
        for n in anchors:
            total += w[n] * (closes_by_anchor[prev] / closes_by_anchor[n])
            prev = n
    return float(total)


def rs_vs_benchmark(stock_ret: float | None, bench_ret: float | None) -> float | None:
    """(1 + r_stock) / (1 + r_benchmark), or None when either leg is absent.

    None rather than 1.0 on a missing leg: 1.0 reads as "performed exactly in
    line with the market", which is a claim we cannot make when we could not
    measure one side of it.
    """
    if stock_ret is None or bench_ret is None:
        return None
    denom = 1.0 + bench_ret
    if denom == 0:
        return None
    return (1.0 + stock_ret) / denom


# ── Percentile ────────────────────────────────────────────────────────────────

def rank_percentiles(raw: dict[str, float]) -> dict[str, float]:
    """{symbol → ascending percent rank in (0, 1]}, ties averaged.

    Published alongside the integer rating so the un-bucketed number stays
    visible — the difference between RS 89 and RS 90 is often a rounding edge.

    Raises on a None value rather than ranking it. A None coerced to 0.0 would
    sort below every real score and deflate everyone above it.
    """
    if not raw:
        return {}
    for sym, value in raw.items():
        if value is None or not np.isfinite(value):
            raise ValueError(f"rank_percentiles got a non-finite score for {sym!r}: {value!r}")
    series = pd.Series(raw, dtype="float64")
    ranked = series.rank(method="average", pct=True)
    return {str(sym): float(pct) for sym, pct in ranked.items()}


# The rank percentile arrives as a float, so 99 · (13/99) is 13.000000000002 and
# a naive ceil promotes an exact bucket boundary to the next bucket. Nudge down
# by far less than the smallest real gap between two distinct percentiles
# (99/N, i.e. ~0.01 even at N=10,000) and far more than float64 noise at this
# magnitude (~1e-12).
_CEIL_EPSILON = 1e-9


def percentile_ratings(raw: dict[str, float]) -> dict[str, int]:
    """{symbol → 1..99}. rating = clip(ceil(99 · rank_pct), 1, 99).

    99 buckets over the population, so each rating holds about N/99 names —
    not N/100. Ties share a rating because they share a percentile.
    """
    return {sym: max(1, min(99, int(math.ceil(99.0 * pct - _CEIL_EPSILON))))
            for sym, pct in rank_percentiles(raw).items()}


# ── Trailing statistics ───────────────────────────────────────────────────────

def _real_closes(aligned: pd.DataFrame | None, close: pd.Series) -> pd.Series:
    """The subset of `close` on sessions that genuinely traded.

    Falls back to dropna() when the frame carries no `real_bar` column, so a
    caller passing a hand-built frame still gets sensible behaviour.
    """
    if aligned is not None and "real_bar" in getattr(aligned, "columns", []):
        mask = aligned["real_bar"].fillna(False).astype(bool)
        return close[mask].dropna()
    return close.dropna()


def _last_finite(series: pd.Series | None) -> float | None:
    if series is None or len(series) == 0:
        return None
    value = series.iloc[-1]
    if value is None or not np.isfinite(value):
        return None
    return float(value)


def trailing_stats(aligned: pd.DataFrame) -> dict:
    """SMAs, average volume, RVOL, ADR and ATR off a calendar-aligned frame.

    Every value is None — never 0.0, never 1.0 — when its window is not fully
    covered. Delegates to signals.py so these definitions cannot drift from the
    live engine's, with one correction: signals.calc_rvol returns a constant 1.0
    Series below 20 bars, and a manufactured "perfectly average volume" is
    exactly the plausible wrong number this codebase refuses to print.

    Every window is measured over sessions that genuinely traded, never over
    forward-filled ones: "the average of the last 50 closes" should mean 50
    real closes, and averaging a carried price in would count one print twice.
    Volume is never filled in the first place, so a halted name reports the
    average of what actually traded rather than a diluted one.
    """
    out: dict[str, float | None] = {
        "sma50": None, "sma200": None,
        "avg_vol_50d": None, "avg_dollar_vol_50d": None,
        "rvol": None, "adr_pct": None, "atr14": None,
    }
    if aligned is None or aligned.empty or "close" not in aligned.columns:
        return out

    close = pd.to_numeric(aligned["close"], errors="coerce")
    real_close = _real_closes(aligned, close)

    for window in _SMA_WINDOWS:
        if len(real_close) >= window:
            out[f"sma{window}"] = _last_finite(_sma(real_close, window))

    if "volume" in aligned.columns:
        volume = pd.to_numeric(aligned["volume"], errors="coerce").dropna()
        if len(volume) >= _VOL_WINDOW:
            avg_vol = float(volume.tail(_VOL_WINDOW).mean())
            out["avg_vol_50d"] = avg_vol if np.isfinite(avg_vol) else None
            paired = pd.concat([close, pd.to_numeric(aligned["volume"], errors="coerce")],
                               axis=1).dropna()
            if len(paired) >= _VOL_WINDOW:
                dollar = (paired.iloc[:, 0] * paired.iloc[:, 1]).tail(_VOL_WINDOW).mean()
                out["avg_dollar_vol_50d"] = float(dollar) if np.isfinite(dollar) else None
        # RVOL needs signals.calc_rvol's own 20-bar floor honoured explicitly.
        if len(volume) >= _RVOL_MIN_SESSIONS:
            frame = pd.DataFrame({"volume": volume})
            out["rvol"] = _last_finite(_calc_rvol(frame))

    if {"high", "low"} <= set(aligned.columns):
        hl = aligned[["high", "low"]].apply(pd.to_numeric, errors="coerce").dropna()
        hl = hl[hl["low"] > 0]
        if len(hl) >= _ADR_WINDOW:
            adr = ((hl["high"] / hl["low"]) - 1.0).tail(_ADR_WINDOW).mean() * 100.0
            out["adr_pct"] = float(adr) if np.isfinite(adr) else None
        if {"close"} <= set(aligned.columns):
            ohlc = aligned[["high", "low", "close"]].apply(pd.to_numeric, errors="coerce").dropna()
            if len(ohlc) > _ATR_PERIOD:
                out["atr14"] = _last_finite(_atr(ohlc, _ATR_PERIOD))
    return out


# ── Row assembly ──────────────────────────────────────────────────────────────

def build_row(symbol: str, aligned: pd.DataFrame, coverage: float,
              bench_returns: dict[str, float | None], form: str = "trailing") -> dict:
    """One output row, minus the population-dependent fields stamp_ratings adds.

    `insufficient` names every quantity we could not compute, so a reader can
    tell "this stock is not above its 200-day average" from "this stock has not
    existed for 200 days". Absence is reported, not silently dropped.
    """
    if form not in RS_FORMS:
        raise ValueError(f"unknown rs_form {form!r}; expected one of {RS_FORMS}")

    close = (pd.to_numeric(aligned["close"], errors="coerce")
             if aligned is not None and "close" in aligned.columns
             else pd.Series(dtype="float64"))
    # Real bars only — after the forward fill a notna() check would count
    # carried prices as prints and overstate both p0_date and the session count.
    real = _real_closes(aligned, close)

    insufficient: list[str] = []
    returns: dict[str, float | None] = {}
    for label, sessions in RETURN_WINDOWS.items():
        value = period_return(close, sessions)
        returns[f"ret_{label}"] = value
        if value is None:
            insufficient.append(f"ret_{label}")

    score = rs_raw(close, form=form)
    if score is None:
        insufficient.append("rs_raw")

    stats = trailing_stats(aligned)
    for key, value in stats.items():
        if value is None:
            insufficient.append(key)

    price = anchor_close(close, 0)
    sma50, sma200 = stats["sma50"], stats["sma200"]

    row: dict = {
        "ticker": symbol,
        "rs_rating": None,          # stamp_ratings fills these three
        "rs_raw": score,
        "rs_percentile": None,
        "rs_form": form,
        "population": None,
        "as_of": None,
        "p0_date": (real.index[-1].date().isoformat() if len(real) else None),
        "price": price,
        **returns,
        **{f"rs_vs_spy_{label}": rs_vs_benchmark(returns[f"ret_{label}"],
                                                 bench_returns.get(f"ret_{label}"))
           for label in RETURN_WINDOWS},
        **stats,
        "above_sma50": (None if (sma50 is None or price is None) else bool(price > sma50)),
        "above_sma200": (None if (sma200 is None or price is None) else bool(price > sma200)),
        "sessions_available": int(len(real)),
        "coverage": round(float(coverage), 4),
        "insufficient": insufficient,
        "rejects": [],
    }
    return row


def stamp_ratings(rows: list[dict], ratings: dict[str, int],
                  percentiles: dict[str, float], population: int,
                  as_of: str) -> list[dict]:
    """Write rs_rating / rs_percentile / population / as_of onto every row.

    `population` goes on each row, not just the file header, because a row is
    routinely lifted out of context — into the dashboard, into a log line, into
    a screenshot — and an RS rating without its population is not interpretable.

    A symbol with no rating gets None, never 0. Rating 0 does not exist on the
    1-99 scale, so a 0 would read as "the weakest possible name" rather than
    "not rated".
    """
    for row in rows:
        symbol = row.get("ticker")
        row["rs_rating"] = ratings.get(symbol)
        pct = percentiles.get(symbol)
        row["rs_percentile"] = None if pct is None else round(float(pct), 4)
        row["population"] = int(population)
        row["as_of"] = as_of
    return rows


# ── Filters ───────────────────────────────────────────────────────────────────

def passes_filters(row: dict, cfg: dict) -> tuple[bool, list[str]]:
    """(kept?, names of the filters it failed).

    A filter whose input is None REJECTS and names itself. This deliberately
    inverts swing_screener.score_candidate:463, where a degraded field passes —
    there a None means Finnhub was gated and the stock should not be punished
    for the provider's paywall. Here a None sma200 means the stock has fewer
    than 200 sessions, which is a fact about the stock: it genuinely is not
    above a 200-day average that does not exist.
    """
    rejects: list[str] = []

    def _num(key, default):
        try:
            return float(cfg.get(key, default))
        except (TypeError, ValueError):
            return float(default)

    rating = row.get("rs_rating")
    min_rating = _num("rs_min_rs_rating", 80)
    if rating is None:
        rejects.append("rs_rating: not rated")
    elif rating < min_rating:
        rejects.append(f"rs_rating {rating} < {min_rating:g}")

    price = row.get("price")
    min_price = _num("rs_min_price", 10.0)
    if price is None:
        rejects.append("price: unknown")
    elif price < min_price:
        rejects.append(f"price {price:.2f} < {min_price:g}")

    avg_vol = row.get("avg_vol_50d")
    min_vol = _num("rs_min_avg_vol_50d", 500_000.0)
    if avg_vol is None:
        rejects.append("avg_vol_50d: unknown")
    elif avg_vol < min_vol:
        rejects.append(f"avg_vol_50d {avg_vol:,.0f} < {min_vol:,.0f}")

    if cfg.get("rs_require_above_sma50", True):
        if row.get("above_sma50") is None:
            rejects.append("above_sma50: no sma50")
        elif not row["above_sma50"]:
            rejects.append("below sma50")

    if cfg.get("rs_require_above_sma200", False):
        if row.get("above_sma200") is None:
            rejects.append("above_sma200: no sma200")
        elif not row["above_sma200"]:
            rejects.append("below sma200")

    if cfg.get("rs_use_rvol_filter", False):
        rvol, min_rvol = row.get("rvol"), _num("rs_min_rvol", 1.5)
        if rvol is None:
            rejects.append("rvol: unknown")
        elif rvol < min_rvol:
            rejects.append(f"rvol {rvol:.2f} < {min_rvol:g}")

    if cfg.get("rs_use_adr_filter", False):
        adr, min_adr = row.get("adr_pct"), _num("rs_min_adr_pct", 3.0)
        if adr is None:
            rejects.append("adr_pct: unknown")
        elif adr < min_adr:
            rejects.append(f"adr_pct {adr:.2f} < {min_adr:g}")

    return (not rejects), rejects


def rank_and_cap(rows: list[dict], limit: int) -> list[dict]:
    """Sort strongest-first and take the top `limit`.

    The sort key is total — rs_rating, then 3-month return, then dollar volume,
    then ticker — so a tie can never reorder between two runs over identical
    data. Churn in a ranked list reads as new information when it is not.
    """
    def _key(row: dict) -> tuple:
        return (
            -(row.get("rs_rating") or 0),
            -(row.get("ret_3m") if row.get("ret_3m") is not None else -1e9),
            -(row.get("avg_dollar_vol_50d") or 0.0),
            str(row.get("ticker") or ""),
        )
    ordered = sorted(rows, key=_key)
    return ordered[: int(limit)] if limit and limit > 0 else ordered


# ── Serialisation ─────────────────────────────────────────────────────────────

def jsonable(obj):
    """Recursively replace NaN/Inf/pd.NA with None so json.dump emits valid JSON.

    json.dump happily writes bare `NaN` and `Infinity`, which are not JSON and
    which throw in the browser's JSON.parse. Any NaN reaching this point is
    already a bug by rule 4, but the dashboard must not be the thing that breaks.
    """
    if isinstance(obj, dict):
        return {k: jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, (float, np.floating)):
        value = float(obj)
        return value if math.isfinite(value) else None
    if obj is pd.NA or (obj is not None and obj is pd.NaT):
        return None
    return obj


def to_frame(rows: list[dict]) -> pd.DataFrame:
    """Convenience for notebook/ad-hoc use. The canonical shape is list[dict] —
    it is what the JSON file needs and what every other module here passes."""
    return pd.DataFrame(rows)
