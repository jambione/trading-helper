"""Stocktwits free trending poll for the momentum monitor.

Public JSON (no key required today):
  GET https://api.stocktwits.com/api/2/trending/symbols.json

Mirrors https://stocktwits.com/sentiment columns where possible:
  rank, symbol, last, %chg, volume, 52w high/low (+ score/watchers).

Live last / %chg / session volume come from Alpaca snapshots (free with your
keys). The 52w bounds come from Stocktwits fundamentals on the same payload.

Two provenance rules run through this module, because the panel's numbers come
from two feeds that are not interchangeable:

1. A snapshot bar describes whichever session Alpaca last built a bar for, not
   necessarily today. Every figure derived from one is gated on the bar's own
   timestamp. See `snapshot_fields`.
2. The free Alpaca plan is IEX-only — a few percent of the consolidated tape.
   So Alpaca volume and Stocktwits' `avg_vol_consolidated` may never be mixed
   into one ratio or one median. Relative volume divides IEX by IEX, where the
   feed's share cancels out.
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

TRENDING_URL = "https://api.stocktwits.com/api/2/trending/symbols.json"
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36 "
    "BrasfieldMomentum/1.0"
)

ROOT = Path(__file__).resolve().parent

# `tools.morning_funnel` (relative volume) lives under the repo root. Put it on
# the path here rather than relying on whoever imported this module.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Own the timezone rather than inheriting the host's. Every "is this bar today?"
# question below is answered in ET; naive local time would answer it correctly
# on an ET desk and silently wrongly anywhere else.
ET = ZoneInfo("America/New_York")

# Completed sessions needed before an average daily volume means anything. A
# fresh listing or a name halted all week yields a denominator that is noise,
# and a wrong RVOL is worse than a blank cell.
MIN_AVG_SESSIONS = 5


def _f(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _aware(dt):
    """Timezone-aware datetime, or None. Naive input is read as UTC, which is
    what every Alpaca timestamp is when the SDK drops the tzinfo."""
    if not isinstance(dt, datetime):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _ts_unix(dt) -> float | None:
    dt = _aware(dt)
    return dt.timestamp() if dt is not None else None


def _bar_et_date(bar) -> date | None:
    """The ET calendar date a bar covers, or None when it can't be determined.

    None is a real answer here and must not collapse to "today" — the whole
    point of asking is that assuming today is the bug.
    """
    dt = _aware(getattr(bar, "timestamp", None))
    return dt.astimezone(ET).date() if dt is not None else None


def et_session_date(now: float | None = None) -> date:
    now = time.time() if now is None else now
    return datetime.fromtimestamp(now, ET).date()


def mins_since_open(now: float | None = None) -> float:
    """Minutes since 9:30 ET — negative before the open. Matches the argument
    `morning_funnel.expected_fraction` expects."""
    dt = datetime.fromtimestamp(time.time() if now is None else now, ET)
    open_dt = dt.replace(hour=9, minute=30, second=0, microsecond=0)
    return (dt - open_dt).total_seconds() / 60.0


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
        # Market cap is deliberately not parsed. It never changed a decision on
        # this desk, and Stocktwits' units were only "typically" $millions —
        # a scale guess with nothing downstream to justify the risk.
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
            "high_52w": _f(fund.get("HighPriceLast52Weeks")),
            "low_52w": _f(fund.get("LowPriceLast52Weeks")),
            # Stocktwits' average daily volume is CONSOLIDATED tape. Kept under
            # a name that says so, because the obvious use — dividing it into
            # Alpaca's IEX session volume for a relative-volume figure — is
            # wrong by more than an order of magnitude. See `row_rvol`.
            "avg_vol_consolidated": _f(fund.get("AverageDailyVolumeLastMonth")
                                       or fund.get("AverageDailyVolumeLast3Months")),
            "exchange": s.get("exchange") or "",
            # filled by Alpaca enrich
            "price": None,
            "pct_change": None,
            "vol_session": None,
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


def snapshot_fields(snap, now: float, today_et: date) -> dict[str, Any]:
    """Row fields from one Alpaca snapshot. Pure — no network, no wall clock.

    Which day each figure describes is decided by the bar's own timestamp,
    never by the field name. `daily_bar` means "the most recent daily bar",
    which is today's only once the day has printed. Before that — every
    morning until the first pre-market trade, and all weekend — `daily_bar` IS
    the last completed session and `previous_daily_bar` is the one before it.

    Assuming otherwise breaks two figures at once:

      • volume: yesterday's completed total is shown as today's session volume.
      • %chg:   measured against the close *before* the last session, so the
                last session's whole move is reported as if it were today's.

    The second is the damaging one. `stocktwits_look_min_abs_chg` gates the
    LOOK badge on |%chg|, so an inherited move manufactures LOOK badges that
    ring the speaker and land in the journal as evidence — and it does so
    precisely during the pre-market hours two of the three daily tranches sit
    in.

    Fields with nothing solid behind them are omitted rather than zeroed, so a
    caller can tell "no data" from "no movement".
    """
    out: dict[str, Any] = {}
    daily = getattr(snap, "daily_bar", None)
    prev = getattr(snap, "previous_daily_bar", None)
    latest = getattr(snap, "latest_trade", None)

    # ── price, and how old the print behind it is ────────────────────────
    px = _f(getattr(latest, "price", None)) if latest is not None else None
    if px is not None and px > 0:
        obs = _ts_unix(getattr(latest, "timestamp", None))
        if obs:
            out["price_ts"] = obs
            out["price_age_sec"] = max(0.0, float(now) - obs)
    else:
        # No trade in the snapshot: the bar close is the best price available,
        # and its age is the age of the session it closed, not of a print.
        px = _f(getattr(daily, "close", None)) if daily is not None else None
        px = px if (px is not None and px > 0) else None
    out["price"] = px

    d_date = _bar_et_date(daily)
    p_date = _bar_et_date(prev)
    if d_date is not None:
        out["vol_bar_date"] = d_date.isoformat()

    # Deliberately no volume from the daily bar. Measured on IEX, the daily bar
    # excludes pre-market entirely (AAPL 2026-07-24: bar 1,772,167 vs 1,771,144
    # RTH minutes plus 14,059 pre-market minutes it does not contain), so before
    # 9:30 there is no today-dated bar to read and the column would sit blank
    # through two of the three daily tranches. `vol_session` is summed from
    # minute bars instead — see `refresh_volume`. The bar dates above are still
    # what decides the %chg baseline, and are correct for it.

    # ── %chg baseline: the last close strictly before today ──────────────
    base, base_date = None, None
    if d_date == today_et:
        base = _f(getattr(prev, "close", None)) if prev is not None else None
        base_date = p_date
    elif d_date is not None and d_date < today_et:
        base = _f(getattr(daily, "close", None))
        base_date = d_date
    # d_date is None, or somehow ahead of today: no baseline can be trusted,
    # so no %chg is published. Blank beats a plausible wrong number.

    if base is not None and base > 0 and px is not None:
        out["prev_close"] = base
        out["pct_change"] = (px - base) / base * 100.0
        if base_date is not None:
            out["pct_basis_date"] = base_date.isoformat()

    # Does `pct_change` describe today at all? Only if the print behind `price`
    # is today's. Before the first trade of the day px IS the last close and
    # `base` is that same close, so pct_change is 0.00% by construction — a
    # true statement ("unmoved since Friday") that reads on screen as a flat
    # market. The renderer needs to tell that apart from a real 0.00%.
    print_date = _bar_et_date(latest) if latest is not None else None
    out["pct_is_today"] = print_date == today_et

    # The last completed session's move — what stocktwits.com shows in its
    # %Change column pre-market. Its own field, NEVER merged into pct_change:
    # `apply_look_highlights` gates the LOOK badge on |pct_change|, and feeding
    # it an inherited move is exactly the defect described above. Computable
    # only when today has not printed, because that is the case where `daily`
    # is the last completed session and `prev` is the one before it.
    if d_date is not None and d_date < today_et and prev is not None:
        p_close = _f(getattr(prev, "close", None))
        d_close = _f(getattr(daily, "close", None))
        if p_close and p_close > 0 and d_close is not None:
            out["pct_change_prev"] = (d_close - p_close) / p_close * 100.0
            out["prev_session_date"] = d_date.isoformat()
    return out


def _alpaca_client():
    """Alpaca data client, or None when keys are absent."""
    _load_env()
    api = os.getenv("ALPACA_API_KEY", "")
    sec = os.getenv("ALPACA_SECRET_KEY", "")
    if not api or not sec:
        return None
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        return StockHistoricalDataClient(api, sec)
    except Exception:
        return None


def _feed_arg() -> dict:
    """IEX — the free plan's only feed. Kept in one place because the RVOL
    numerator and denominator are only comparable while it is the same for
    both requests."""
    try:
        from alpaca.data.enums import DataFeed
        return {"feed": DataFeed.IEX}
    except Exception:
        return {}


def enrich_with_alpaca(rows: list[dict[str, Any]],
                       now: float | None = None,
                       avg_by_sym: dict | None = None,
                       time_adjusted: bool = True,
                       client=None) -> list[dict[str, Any]]:
    """Attach last, price age and %chg from an Alpaca snapshot.

    Volume and RVOL do not come from here — see `refresh_volume`, which sums
    them off minute bars. `avg_by_sym` only re-derives RVOL for rows whose
    volume a previous pass already established.
    """
    if not rows:
        return rows
    now = time.time() if now is None else now
    client = _alpaca_client() if client is None else client
    if client is None:
        return rows
    syms = [r["symbol"] for r in rows if r.get("symbol") and not r.get("is_crypto")]
    if not syms:
        return rows
    try:
        from alpaca.data.requests import StockSnapshotRequest
        snaps = client.get_stock_snapshot(
            StockSnapshotRequest(symbol_or_symbols=syms, **_feed_arg()))
    except Exception:
        return rows

    today_et = et_session_date(now)
    by = {r["symbol"]: r for r in rows}
    items = snaps.items() if hasattr(snaps, "items") else []
    for sym, snap in items:
        row = by.get(str(sym).upper())
        if row is None:
            continue
        try:
            row.update(snapshot_fields(snap, now, today_et))
        except Exception:
            continue
        row["rvol"], row["rvol_raw"] = row_rvol(
            row, (avg_by_sym or {}).get(row["symbol"]), now,
            time_adjusted=time_adjusted)
    return rows


def row_rvol(row: dict, avg_vol: float | None, now: float,
             time_adjusted: bool = True) -> tuple:
    """(rvol, rvol_raw) for one enriched row, or (None, None).

    Both sides are sums of Alpaca IEX MINUTE bars — today's so far over the
    average of completed sessions — so three things line up at once: the feed's
    share of the tape cancels, the aggregation granularity matches (a daily bar
    carries odd-lot prints no minute bar does), and pre-market volume is
    included, which is the only reason this reads at 7:00 rather than 9:30.

    Same helper the dashboard and the funnel use, so the RVOL in this panel and
    the one in the momentum table are the same measurement.
    """
    vol = row.get("vol_session")
    if vol is None or avg_vol is None:
        return None, None
    try:
        # Lazy: pulls pandas, which the desk otherwise never needs at startup.
        import tools.morning_funnel as mf
        return mf.rvol_pair(vol, avg_vol, mins_since_open(now),
                            time_adjusted=time_adjusted)
    except Exception:
        return None, None


def fmt_vol(v: float | None) -> str:
    if v is None:
        return "—"
    if v >= 1e9:
        return f"{v / 1e9:.2f}B"
    if v >= 1e6:
        return f"{v / 1e6:.2f}M"
    if v >= 1e3:
        return f"{v / 1e3:.1f}K"
    return f"{v:.0f}"


def range_pct(price: float | None, hi: float | None,
              lo: float | None) -> float | None:
    """Where last sits in the 52w range: 0 = low, 1 = high."""
    if price is None or hi is None or lo is None:
        return None
    span = float(hi) - float(lo)
    if span <= 0:
        return None
    return (float(price) - float(lo)) / span


def price_age(row: dict, now: float) -> float | None:
    """Seconds since the print behind `row["price"]`, or None.

    Derived from the print's absolute timestamp on every read rather than
    frozen when the row was built. A stamped-once age stops advancing the
    moment a quote poll fails to parse, so a price that is now minutes old
    keeps displaying the age it had when it was seconds old — which is the
    exact failure mode `price_ts` on the momentum feed already had.
    """
    ts = _f(row.get("price_ts"))
    if ts:
        return max(0.0, float(now) - ts)
    return _f(row.get("price_age_sec"))


def range_bar(price: float | None, lo: float | None,
              hi: float | None, width: int = 11) -> str:
    """Where price sits in the 52w range, as one track: `─────●─────`.

    Replaces a bare 52w high and 52w low pair, which made the reader do the
    division. The position is the thing that matters — it is already what
    decides EXT vs WASH — and two absolute prices convey it poorly.

    A price outside the range is the interesting case and gets its own glyph
    rather than being clamped silently: momentum names print new 52w highs
    constantly, and `●` pinned at the right end would read the same as merely
    being near the old high.

      ●  inside the range
      ▶  above the 52w high   (a new high right now)
      ◀  below the 52w low    (a new low right now)

    Returns "" when the range cannot be placed — no price, missing bounds, or
    a degenerate span. A track with the marker parked somewhere plausible
    would be a drawing of missing data.
    """
    w = max(3, int(width))
    p, lo_f, hi_f = _f(price), _f(lo), _f(hi)
    if p is None or lo_f is None or hi_f is None or hi_f - lo_f <= 0:
        return ""
    if p > hi_f:
        return "─" * (w - 1) + "[bold green]▶[/bold green]"
    if p < lo_f:
        return "[bold red]◀[/bold red]" + "─" * (w - 1)
    frac = (p - lo_f) / (hi_f - lo_f)
    i = min(w - 1, max(0, round(frac * (w - 1))))
    colour = "green" if frac >= 0.70 else "red" if frac <= 0.30 else "white"
    return (f"[dim]{'─' * i}[/dim][{colour}]●[/{colour}]"
            f"[dim]{'─' * (w - 1 - i)}[/dim]")


def range_cell(price: float | None, lo: float | None, hi: float | None,
               width: int = 11) -> str:
    """The 52w track, or the bare bounds when the marker cannot be placed.

    The bounds come from Stocktwits and need no quote feed; the marker needs a
    current price and does. Falling all the way back to "—" would throw away
    data that is present and was visible before this column replaced the
    separate 52w Hi / 52w Lo pair.
    """
    bar = range_bar(price, lo, hi, width)
    if bar:
        return bar
    lo_f, hi_f = _f(lo), _f(hi)
    if lo_f is None or hi_f is None:
        return ""
    return f"[dim]{lo_f:.2f}–{hi_f:.2f}[/dim]"


def _row_vol(r: dict) -> float | None:
    """Today's session volume for this row, or None.

    Deliberately does NOT fall back to `avg_vol`. That field is Stocktwits'
    consolidated 30-day average — a different feed and a different quantity
    from an IEX session total. Substituting it put both into one median and
    then compared each against it, so a heavily traded name could fail the
    volume gate while a quiet one passed on the strength of its own average.
    A row with no session volume has no volume evidence, and says so.
    """
    return _f(r.get("vol_session"))


def apply_look_highlights(
    rows: list[dict[str, Any]],
    *,
    min_abs_chg: float = 3.0,
    max_looks: int = 2,
    near_high: float = 0.70,
    near_low: float = 0.30,
    heat_top_n: int = 3,
    min_rvol: float | None = None,
) -> list[dict[str, Any]]:
    """
    Mark rows with look=True when worth focusing (desk eyes, not auto-trade).

    Requires ALL of:
      • heat: score ≥ panel median OR top heat_top_n by score
      • move: |%chg| ≥ min_abs_chg
      • volume: session volume ≥ panel median, and — when relative volume is
        known — rvol ≥ min_rvol
      • range: green day + range_pct ≥ near_high  (EXT)
            OR red day  + range_pct ≤ near_low   (WASH)

    At most max_looks rows, ranked by Score × |%Chg| × log10(1+Vol).

    The median on its own is a purely relative bar: half of any panel clears
    it, including a panel where nothing is trading. `min_rvol` adds an absolute
    one, and is skipped rather than assumed for rows whose relative volume
    could not be computed.
    """
    import math
    import statistics

    for r in rows:
        r["look"] = False
        r["look_reason"] = ""
        r["look_priority"] = 0.0
        r["range_pct"] = range_pct(r.get("price"), r.get("high_52w"), r.get("low_52w"))

    if not rows:
        return rows

    scores = [float(r["trending_score"]) for r in rows
              if r.get("trending_score") is not None]
    vols = [v for r in rows if (v := _row_vol(r)) is not None]
    med_score = statistics.median(scores) if scores else None
    med_vol = statistics.median(vols) if vols else None

    by_score = sorted(
        [r for r in rows if r.get("trending_score") is not None],
        key=lambda r: float(r["trending_score"]),
        reverse=True,
    )
    top_heat = {r["symbol"] for r in by_score[: max(1, int(heat_top_n))]}

    candidates: list[tuple[float, dict, str]] = []
    for r in rows:
        sc = _f(r.get("trending_score"))
        chg = _f(r.get("pct_change"))
        vol = _row_vol(r)
        rp = r.get("range_pct")

        heat = (
            r["symbol"] in top_heat
            or (sc is not None and med_score is not None and sc >= med_score)
        )
        move = chg is not None and abs(chg) >= float(min_abs_chg)
        vol_ok = vol is not None and (
            (med_vol is not None and vol >= med_vol) or (med_vol is None and vol > 0)
        )
        # Unknown rvol neither passes nor blocks — the median test stands alone.
        rv = _f(r.get("rvol"))
        if vol_ok and min_rvol is not None and rv is not None:
            vol_ok = rv >= float(min_rvol)
        if not (heat and move and vol_ok) or rp is None or chg is None:
            continue

        if chg > 0 and rp >= float(near_high):
            reason = "EXT"    # extension / near highs, green
        elif chg < 0 and rp <= float(near_low):
            reason = "WASH"   # washout / near lows, red
        else:
            continue

        pri = (sc or 0.0) * abs(chg) * math.log10(1.0 + max(0.0, vol or 0.0))
        candidates.append((pri, r, reason))

    candidates.sort(key=lambda x: -x[0])
    for pri, r, reason in candidates[: max(0, int(max_looks))]:
        r["look"] = True
        r["look_reason"] = reason
        r["look_priority"] = pri
    return rows


class StocktwitsTrending:
    """Throttled cache of Stocktwits trending (default poll ~60s)."""

    def __init__(self, poll_interval: float = 60.0, stocks_only: bool = True,
                 max_price: float | None = 30.0,
                 enrich_quotes: bool = True,
                 look_min_abs_chg: float = 3.0,
                 look_max: int = 2,
                 look_near_high: float = 0.70,
                 look_near_low: float = 0.30,
                 look_min_rvol: float | None = None,
                 quote_interval: float = 15.0,
                 volume_interval: float = 60.0,
                 avg_days: int = 10,
                 rvol_time_adjusted: bool = True):
        self.poll_interval = max(15.0, float(poll_interval))
        # Quotes refresh on their own, faster clock. The trending *ranking*
        # barely moves in a minute and the endpoint is unauthenticated and
        # public, so polling it harder would be rude and pointless; the prices
        # hanging off it go stale in seconds and are what the desk reads.
        self.quote_interval = max(2.0, float(quote_interval))
        # Today's minute bars for the whole panel are a far bigger payload than
        # a snapshot, and volume does not move meaningfully in 15s.
        self.volume_interval = max(10.0, float(volume_interval))
        self.stocks_only = bool(stocks_only)
        self.max_price = float(max_price) if max_price is not None else None
        self.enrich_quotes = bool(enrich_quotes)
        self.look_min_abs_chg = float(look_min_abs_chg)
        self.look_max = int(look_max)
        self.look_near_high = float(look_near_high)
        self.look_near_low = float(look_near_low)
        self.look_min_rvol = (float(look_min_rvol)
                              if look_min_rvol is not None else None)
        self.avg_days = int(avg_days)
        self.rvol_time_adjusted = bool(rvol_time_adjusted)
        self.rows: list[dict[str, Any]] = []
        self.by_symbol: dict[str, dict[str, Any]] = {}
        self.last_ok: float = 0.0
        self.last_attempt: float = 0.0
        self.last_quote_ok: float = 0.0
        self.last_quote_attempt: float = 0.0
        self.error: str = ""
        self.quotes_error: str = ""
        self._seen: set[str] = set()
        self._prev_look: dict[str, bool] = {}
        self._seeded = False          # suppress alerts on the first poll
        self.last_volume_ok: float = 0.0
        self.last_volume_attempt: float = 0.0

    # ── volume + RVOL, off minute bars, on their own slower clock ────────
    def refresh_volume(self, now: float | None = None, client=None) -> bool:
        """Sum today's minute bars per symbol and derive RVOL.

        Separate from the quote clock because volume does not need 15s
        resolution and today's minute bars are a much larger payload than a
        snapshot. Reuses `morning_funnel`, so this panel's RVOL is the same
        measurement as the momentum table's rather than a parallel one.
        """
        now = time.time() if now is None else now
        if not self.rows:
            return False
        if self.last_volume_attempt and \
                (now - self.last_volume_attempt) < self.volume_interval:
            return False
        self.last_volume_attempt = now

        client = _alpaca_client() if client is None else client
        if client is None:
            return False
        syms = [r["symbol"] for r in self.rows
                if r.get("symbol") and not r.get("is_crypto")]
        if not syms:
            return False
        try:
            # Lazy: pulls pandas, which the desk otherwise never needs.
            import tools.morning_funnel as mf
            cfg = {"funnel_avg_days": self.avg_days}
            now_et = datetime.fromtimestamp(now, ET)
            avg = mf.avg_session_volumes(client, syms, cfg, now_et)
            minutes = mf.fetch_minutes_today(client, syms, cfg, now_et)
        except Exception:
            return False

        for r in self.rows:
            sym = r.get("symbol")
            df = minutes.get(sym) if sym else None
            try:
                vol = float(df["volume"].sum()) if df is not None \
                    and not df.empty else None
            except Exception:
                vol = None
            # None, not 0 — "no bars yet" and "traded nothing" both mean there
            # is no volume evidence, and a 0 would divide into a real RVOL.
            r["vol_session"] = vol if (vol or 0) > 0 else None
            r["rvol"], r["rvol_raw"] = row_rvol(
                r, avg.get(sym), now, time_adjusted=self.rvol_time_adjusted)
        self.by_symbol = {r["symbol"]: r for r in self.rows}
        self.last_volume_ok = now
        return True

    def refresh(self, now: float | None = None) -> bool:
        """Re-poll the Stocktwits trending list (and re-quote it)."""
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
        self.rows = rows
        self.by_symbol = {r["symbol"]: r for r in rows}
        self.last_ok = now
        self.error = ""
        if self.enrich_quotes:
            self._quote(now)
            self.refresh_volume(now)
        return True

    def refresh_quotes(self, now: float | None = None) -> bool:
        """Re-quote the rows already held, without re-polling Stocktwits."""
        now = now if now is not None else time.time()
        if not self.rows or not self.enrich_quotes:
            return False
        if self.last_quote_attempt and \
                (now - self.last_quote_attempt) < self.quote_interval:
            return False
        # Stamp before delegating. Leaving it to _quote would make the throttle
        # depend on the work succeeding, so a request that failed early would
        # be retried on the very next render pass — ten times a second.
        self.last_quote_attempt = now
        return self._quote(now)

    def _quote(self, now: float) -> bool:
        self.last_quote_attempt = now
        client = _alpaca_client()
        if client is None:
            # Say so rather than rendering a silent wall of dashes. Without
            # keys the panel is Stocktwits-only — no price, no %chg, no volume,
            # no RVOL — and nothing on screen explained why.
            self.quotes_error = "no Alpaca keys (signal_engine.env)"
            return False
        self.quotes_error = ""
        enrich_with_alpaca(self.rows, now=now,
                           time_adjusted=self.rvol_time_adjusted,
                           client=client)
        self.by_symbol = {r["symbol"]: r for r in self.rows}
        self.last_quote_ok = now
        return True

    def quote_age(self, now: float | None = None) -> float | None:
        """Seconds since quotes last refreshed, or None before the first."""
        if not self.last_quote_ok:
            return None
        return max(0.0, (time.time() if now is None else now) - self.last_quote_ok)

    def rank_of(self, symbol: str) -> int | None:
        r = self.by_symbol.get((symbol or "").upper())
        if not r:
            return None
        return r.get("rank")

    def display_rows(self, price_by_sym: dict[str, float | None] | None = None,
                     limit: int = 12,
                     on_change: Callable[[str, str, str], None] | None = None,
                     now: float | None = None
                     ) -> list[dict[str, Any]]:
        """
        Rows for the monitor panel.

        Prefer Alpaca-enriched price on the row; fall back to price_by_sym
        from the momentum feed. Apply max_price when a price is known.
        Then apply LOOK highlights (heat + move + vol + 52w range).

        `on_change(kind, symbol, detail)` fires on rising edges — a symbol
        entering the panel for the first time ("st_new") or its LOOK badge
        turning on ("st_look") — same shape as Feed's new/burst/buy alerts.
        Suppressed on the first call so the initial fill doesn't fire a
        notification per row.
        """
        price_by_sym = price_by_sym or {}
        now = time.time() if now is None else now
        out = []
        for r in self.rows:
            sym = r["symbol"]
            # Re-age in place, so `by_symbol` — which the journal reads on an
            # alert edge — advances with the clock too, not only the copy the
            # panel renders.
            age = price_age(r, now)
            if age is not None:
                r["price_age_sec"] = age
            px = r.get("price")
            row = {**r}
            if px is None and sym in price_by_sym:
                px = price_by_sym.get(sym)
                # A borrowed price is not the print the snapshot timed. Carrying
                # that age over would date a different number entirely.
                row.pop("price_ts", None)
                row.pop("price_age_sec", None)
            if px is not None and self.max_price is not None and px >= self.max_price:
                continue
            row.update(price=px, price_known=px is not None)
            out.append(row)
            if len(out) >= limit:
                break
        out = apply_look_highlights(
            out,
            min_abs_chg=self.look_min_abs_chg,
            max_looks=self.look_max,
            near_high=self.look_near_high,
            near_low=self.look_near_low,
            min_rvol=self.look_min_rvol,
        )
        for r in out:
            sym = r["symbol"]
            is_new = sym not in self._seen
            if is_new:
                self._seen.add(sym)
            was_look = self._prev_look.get(sym, False)
            look = bool(r.get("look"))
            if self._seeded and on_change is not None:
                if is_new:
                    on_change("st_new", sym, f"#{r.get('rank') or '?'} trending")
                if look and not was_look:
                    on_change("st_look", sym, r.get("look_reason") or "")
            self._prev_look[sym] = look
        self._seeded = True
        return out
