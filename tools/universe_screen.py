#!/usr/bin/env python3
"""Does ANY constructible universe hold playable moves, or only ours?

Every screen in this lab so far — `drift_screen`, `gate_screen`,
`catalyst_screen`, 42 gate cells and 14 gates — is anchored on
`shadow.jsonl`. That is a universe of one: the names the desk already
chose. Permuting filters inside it has been measured out. What has never
been measured is whether some *other* rule for building a watchlist
produces tape worth trading.

That is this screen. It builds candidate universes from causes rather
than from indicators, resolves each point-in-time, and asks two separate
questions of every one:

  DRIFT      does the tape have direction? (drift_screen's gates,
             unchanged and not weakened — session is the unit)
  PLAYABLE   is the direction big enough to pay for the round trip?

The second is new, and it is the one the operator actually asked for.
A watchlist should hold moves that are worth arming into, not moves that
merely drift. On this book 1R = 5% of price and the measured round trip
(give + spread) is 0.158 R = **0.79% of price**. A universe whose median
favorable excursion is 0.4% is not tradeable no matter how clean its
drift statistic looks — the cost eats it.

**The bar is pre-registered here so results cannot move it:**

    median MFE >= 2x that name's OWN round trip
    median MFE / median MAE >= 1.2
    >= 70% of sessions green

**Cost is per-name (8/23).** The first version charged every universe a
flat 0.79%, which is the desk's own measured round trip and quietly
assumes every watchlist costs what ours does. It does not. Quotes are in
whole cents, so one tick is 0.50% of a $2 stock and 0.02% of a $50 one —
a hundredfold structural difference set by nothing but which names are on
the list, and the single largest lever this desk actually controls.

Each name-day is charged `give + spread`, where the give is the ratchet's
0.50% and the spread is estimated by Roll (1984) from bid-ask bounce in
the minute closes, floored at one tick. The recorded quotes are NOT used
as the cost: `spread_r` covers 56 symbols over 3 days with a p90 of 5.96 R
(29.8% of price), which is a stale or locked book rather than a wide one.
They are used only to validate the estimate, capped at 1.0 R. `--cost-model
fixed` reproduces the pre-8/23 numbers.

Note the bar is a *multiple*, not an absolute — a cheaper universe cannot
pass by having its threshold lowered underneath it.

Both verdicts must pass for a universe to be worth arming into. DRIFT
alone is permission to look; PLAYABLE alone is a big range with no
direction, which is precisely what a ratchet cannot harvest.

Universes (each carries a per-name-day eligibility instant; sampling
starts there, never before — the same discipline as --eligible-within):

    setup           the operator's stage-1 conjunction (setup_rules.py):
                    up >=10%, RVOL >=5, catalyst <24h, $2-20, and shares
                    outstanding <10M. Fires on ~5% of name-days, which is
                    exactly why every MARGINAL gate this lab tested read
                    as the null — a 25-sample effect does not move a
                    493-sample average. Anchored at the first instant all
                    five legs held.
    desk            shadow.jsonl, from admit_ts. The incumbent.
    desk_px:LO-HI   the same universe sliced by median price, e.g.
                    desk_px:0-10, desk_px:10-50, desk_px:50-. This is the
                    cost lever made visible: same desk, same seeds, but
                    several-fold different friction.
    rejects         names the gate turned DOWN, from first rejection.
                    The control: if this beats `desk`, the gate is
                    subtracting value rather than adding it.
    rejects:REASON  one rejection reason (e.g. rejects:not_uptrend)
    burst           signal_shadow.jsonl mention_burst, from signal_at.
                    The one rate-shaped trigger the desk already owns.
    catalyst        a headline within --news-age minutes, from the
                    headline. Needs the Alpaca news cache.
    early_rvol      RVOL >= --rvol before 10:00 ET, from that reading.
                    Volume confirming BEFORE extension, not after.
    gap_hold        opened >= --gap% over the prior close and still above
                    its opening 5-minute low 30 minutes later. Pure bar
                    structure — computable for any symbol, no log needed.
    liquid          megacap control. Expect no drift; if it shows some,
                    the measurement is broken, not the megacaps.

Read-only. Writes only its own screen JSON. Usage (mini, venv):

    .venv/bin/python tools/universe_screen.py
    .venv/bin/python tools/universe_screen.py --universes desk,rejects,gap_hold
    .venv/bin/python tools/universe_screen.py --horizons 15,30 --days 20

Lab-only honest denominator (does not touch live desk / bot_config arms):

    .venv/bin/python tools/universe_screen.py \\
        --universes setup,early_rvol,desk --max-shares-m 10 \\
        --horizons 15,30,60,120 --days 40 --cost-report triple \\
        --out benchmarks/universe_cost/honest_YYYY-MM-DD.json

``--cost-report triple`` prints payX under fixed_079 (flat 0.79%), live_iex
(live give% + Roll/tick or shadow spread_r), and live_sip (live give% +
SIP NBBO RT% = 100*(ask-bid)/mid, quote age < 60s). Stale/missing SIP
quotes are unpriceable and excluded from SIP medians.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import drift_screen as DS  # noqa: E402
from ai_paths import resolve_report_dir  # noqa: E402

SCREEN_DIR = Path(ROOT) / "ai_reports" / "screens"

# Measured 2026-08: 1R = 5% of price, give + spread = 0.158 R round trip.
R_PCT_OF_PRICE = 5.0
COST_PCT = 0.158 * R_PCT_OF_PRICE          # 0.79% of price — the FIXED model
GIVE_PCT = 0.10 * R_PCT_OF_PRICE           # legacy measured-model give (0.10R)
TICK_USD = 0.01                            # the irreducible minimum spread

# Pre-registered playability bar. Stated before any universe was run, and
# unchanged when the cost model became per-name: the multiple is the bar,
# not the absolute MFE, precisely so a cheaper universe cannot pass by
# having its threshold lowered underneath it.
PLAYABLE_MULT = 2.0                        # median MFE >= 2x the round trip
PLAYABLE_MIN_MFE_PCT = PLAYABLE_MULT * COST_PCT   # fixed model only
PLAYABLE_MIN_RATIO = 1.2
PLAYABLE_MIN_GREEN = 0.70

ET_OFFSET_H = 4                            # August is EDT
MIN_ROLL_BARS = 30                         # below this Roll is noise

# Triple-cost lab report (--cost-report triple). Live give is read from
# config at runtime; measured/fixed paths above stay on GIVE_PCT so old
# screens reproduce. SIP quotes older than this at the sample stamp are
# unpriceable and excluded from SIP medians (no fictional floor).
SIP_QUOTE_MAX_AGE_SEC = 60.0
SIP_COV_FLOOR = 0.50                       # below this, verdict_sip=UNPRICEABLE
TRIPLE_THIN_N = 30
TRIPLE_THIN_SESSIONS = 5
COST_CACHE_DIR = Path(ROOT) / "benchmarks" / "universe_cost"


def _et_hm(ts: float) -> tuple[int, int]:
    d = datetime.fromtimestamp(float(ts), timezone.utc) - timedelta(hours=ET_OFFSET_H)
    return d.hour, d.minute


def _clamp_to_rth(ts: float, day: str) -> float:
    """Push a pre-market instant to the open; leave RTH instants alone.

    A headline at 07:12 makes a name eligible, but the desk cannot act on
    it until 09:30 — it places market orders and pre-market takes limits
    only. Sampling from 07:12 would credit the universe with a move it
    could never have traded.
    """
    h, m = _et_hm(ts)
    if (h, m) >= (9, 30):
        return float(ts)
    try:
        d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return float(ts)
    return (d + timedelta(hours=9 + ET_OFFSET_H, minutes=30)).timestamp()


# ------------------------------------------------------------------ loaders

def _iter_log(name: str, days: int):
    path = Path(resolve_report_dir()) / name
    if not path.exists():
        return
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days + 5)).timestamp()
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            ts = r.get("ts")
            try:
                ts = float(ts)
            except (TypeError, ValueError):
                continue
            if ts < cutoff:
                continue
            yield ts, r


def _earliest(pairs) -> dict[str, dict[str, float]]:
    """[(day, sym, ts)] -> day -> {sym: earliest ts}."""
    out: dict[str, dict[str, float]] = defaultdict(dict)
    for day, sym, ts in pairs:
        prev = out[day].get(sym)
        if prev is None or ts < prev:
            out[day][sym] = ts
    return dict(out)


def load_rejects(days: int, reason: str | None = None) -> dict[str, dict[str, float]]:
    """Names the entry gate turned down, from the first time it said no."""
    rows = []
    for ts, r in _iter_log("rejects.jsonl", days):
        sym = r.get("symbol") or r.get("ticker")
        if not sym:
            continue
        if reason and str(r.get("reason") or "") != reason:
            continue
        rows.append((DS._day_of(ts), str(sym).upper(), ts))
    return _earliest(rows)


def load_burst(days: int, signal: str = "mention_burst") -> dict[str, dict[str, float]]:
    """signal_shadow.jsonl, anchored at signal_at — a RATE trigger."""
    rows = []
    for ts, r in _iter_log("signal_shadow.jsonl", days):
        if str(r.get("signal") or "") != signal:
            continue
        sym = r.get("ticker") or r.get("symbol")
        if not sym:
            continue
        at = r.get("signal_at") or ts
        try:
            at = float(at)
        except (TypeError, ValueError):
            at = ts
        day = DS._day_of(ts)
        rows.append((day, str(sym).upper(), _clamp_to_rth(at, day)))
    return _earliest(rows)


def load_early_rvol(days: int, floor: float,
                    before_et: tuple[int, int] = (10, 0)) -> dict[str, dict[str, float]]:
    """RVOL over *floor* observed BEFORE 10:00 ET, from that observation.

    The point is volume confirming a move while it is still early, which
    is the opposite of the desk's current behaviour — its RVOL readings
    are cumulative and peak long after the move.
    """
    rows = []
    for name in ("shadow.jsonl", "rejects.jsonl"):
        for ts, r in _iter_log(name, days):
            sym = r.get("symbol") or r.get("ticker")
            if not sym:
                continue
            try:
                rv = float(r.get("rvol")) if r.get("rvol") is not None else None
            except (TypeError, ValueError):
                rv = None
            # Garbage guard: shadow carries values up to 3144, which is not
            # a relative volume. Anything averaging these has been eating it.
            if rv is None or rv < floor or rv > 100.0:
                continue
            if _et_hm(ts) >= before_et:
                continue
            day = DS._day_of(ts)
            rows.append((day, str(sym).upper(), _clamp_to_rth(ts, day)))
    return _earliest(rows)


def load_catalyst(days: int, syms: list[str], max_age_min: float,
                  refresh: bool = False) -> dict[str, dict[str, float]]:
    """A headline within *max_age_min* of the open, anchored at the headline."""
    import catalyst_screen as CS
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days + 5)
    news = CS.fetch_news(syms, start, end, refresh)
    rows = []
    for sym, items in news.items():
        for n in items or []:
            ts = float(n["ts"])
            if ts < start.timestamp():
                continue
            day = DS._day_of(ts)
            anchor = _clamp_to_rth(ts, day)
            # The headline only counts if it is still fresh when the desk
            # could act on it. A 04:00 print clamped to 09:30 is 5.5 hours
            # stale by the open and is not a catalyst for that session.
            if (anchor - ts) / 60.0 > max_age_min:
                continue
            rows.append((day, str(sym).upper(), anchor))
    return _earliest(rows)


def load_setup(days: int, max_shares_m: float) -> dict[str, dict[str, float]]:
    """The operator's stage-1 conjunction, from the first instant it held.

    Anchored at the FIRST shadow row where all five legs were true at once,
    which is the earliest moment the desk could have acted on it. Rows are
    scanned forward and the first qualifying instant wins, so nothing here
    is knowable later than it was live.

    One honest lookahead: the share count is today's reading applied to a
    past session. Outstanding moves on offerings, so a name that issued
    stock mid-window is scored on its post-issuance count. That direction
    is conservative for admission — the count only grows — but it is a
    lookahead and it is why this universe is evidence for a forward test
    rather than a result on its own.
    """
    import setup_rules
    try:
        import float_feed
    except ImportError:
        return {}
    news = {}
    try:
        news_path = Path(ROOT) / "ai_reports" / "news_cache.json"
        news = json.loads(news_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        news = {}
    out: dict[str, dict[str, float]] = defaultdict(dict)
    for ts, r in _iter_log("shadow.jsonl", days):
        sym = r.get("symbol")
        if not sym:
            continue
        sym = str(sym).upper()
        day = DS._day_of(ts)
        if sym in out.get(day, {}):
            continue                      # already qualified earlier today
        items = news.get(sym) or []
        n24 = sum(1 for n in items
                  if n.get("ts") is not None
                  and ts - 24 * 3600 <= n["ts"] < ts)
        legs = setup_rules.evaluate(
            pct_change=r.get("pct_change"),
            rvol=r.get("rvol"),
            price=r.get("price"),
            # FLOAT, not shares outstanding. float_feed cached
            # shareOutstanding under the name float_cache.json until
            # 2026-08-28; this screen's low-float dimension was therefore
            # measuring the wrong quantity. AREN: 47.6M outstanding against a
            # 13.12M float. The distinction is the whole point of the cut —
            # outstanding includes stock that cannot trade.
            shares_out_m=float_feed.float_shares(sym),
            news_n_24h=n24,
            max_shares_out_m=max_shares_m)
        if legs["ok"]:
            out[day][sym] = _clamp_to_rth(ts, day)
    return dict(out)


def build_gap_hold(bars: dict[str, list[dict]], gap_pct: float,
                   hold_min: int = 30) -> dict[str, dict[str, float]]:
    """Opened up and still holding, confirmed 30 minutes in.

    Structure rather than indicator: the name gapped over the prior close
    and has not given back its opening range. Eligibility is stamped at
    the CONFIRMATION instant, so nothing here is knowable early.
    """
    out: dict[str, dict[str, float]] = defaultdict(dict)
    for sym, rows in bars.items():
        byday: dict[str, list[dict]] = defaultdict(list)
        for b in rows:
            byday[b["day"]].append(b)
        days = sorted(byday)
        for i in range(1, len(days)):
            prev, day = byday[days[i - 1]], byday[days[i]]
            pclose = prev[-1]["c"] if prev else None
            rth = [b for b in day if DS._in_rth(b)]
            if not pclose or pclose <= 0 or len(rth) < hold_min + 5:
                continue
            open_px = rth[0]["o"]
            if open_px <= 0 or 100.0 * (open_px - pclose) / pclose < gap_pct:
                continue
            or_low = min(b["l"] for b in rth[:5])
            window = rth[5:hold_min]
            if not window or min(b["l"] for b in window) < or_low:
                continue
            out[days[i]][sym] = float(rth[min(hold_min, len(rth) - 1)]["t"])
    return dict(out)


# ------------------------------------------------------------------ cost

def tick_spread_pct(price: float) -> float:
    """The spread a name cannot go below, as a percent of its own price.

    Quotes are in whole cents, so a $2 stock cannot be tighter than 0.50%
    while a $200 stock cannot be wider than 0.005% for the same one tick.
    This is a hundredfold structural difference in the cost of trading,
    fixed by nothing except which names are on the list.
    """
    if price <= 0:
        return float("inf")
    return 100.0 * TICK_USD / price


def roll_spread_pct(bars: list[dict], day: str) -> float | None:
    """Effective spread from bid-ask bounce (Roll 1984), % of price.

    Crossing the spread makes consecutive price changes negatively
    correlated: a print at the bid followed by one at the ask reverses
    without any information arriving. Roll inverts that into a spread
    estimate, S = 2*sqrt(-cov(dP_t, dP_t-1)).

    Chosen over the recorded quotes because those are unusable here — 56
    symbols on 3 days, and a p90 of 5.96 R (29.8% of price), which is a
    stale or locked book rather than a spread. Estimating from bars the
    desk already has beats averaging garbage. Returns None when the
    covariance is non-negative (drift dominating the bounce), so the
    caller falls back rather than inventing a number.

    Two known biases, both pinned in the tests. Roll assumes trade
    direction is i.i.d.; systematic alternation inflates it up to twofold,
    and a trending minute makes it undefined rather than small. So it is
    an estimate with a floor under it and a validation beside it, not a
    measurement.
    """
    path = [b for b in bars if b["day"] == day and DS._in_rth(b)]
    if len(path) < MIN_ROLL_BARS:
        return None
    rets = []
    for i in range(1, len(path)):
        prev, cur = path[i - 1]["c"], path[i]["c"]
        if prev > 0:
            rets.append((cur - prev) / prev)
    if len(rets) < MIN_ROLL_BARS:
        return None
    m = statistics.fmean(rets)
    cov = statistics.fmean(
        (rets[i] - m) * (rets[i - 1] - m) for i in range(1, len(rets)))
    if cov >= 0:
        return None
    return 100.0 * 2.0 * (-cov) ** 0.5


def name_cost_pct(bars: list[dict], day: str, model: str) -> tuple[float, str]:
    """Round-trip cost for one name-day, as a percent of price.

    give + spread, where the give is the ratchet's (a strategy constant)
    and the spread is the name's own (a property of what we chose to
    trade). Returns the source too, because a screen that silently
    substitutes a floor for a measurement is the thing this lab keeps
    getting burned by.
    """
    if model == "fixed":
        return COST_PCT, "fixed"
    path = [b for b in bars if b["day"] == day and DS._in_rth(b)]
    if not path:
        return COST_PCT, "fixed"
    price = statistics.median(b["c"] for b in path if b["c"] > 0)
    floor = tick_spread_pct(price)
    roll = roll_spread_pct(bars, day)
    if roll is None:
        return GIVE_PCT + floor, "tick"
    return GIVE_PCT + max(roll, floor), "roll"


def load_quoted_spreads(days: int) -> dict[tuple, float]:
    """(symbol, day) -> median quoted round trip %, for VALIDATION only.

    Sanity-capped at 1.0 R. Above that the row is a broken book, not a
    wide one, and letting it into a median is how a cost model becomes
    fiction.
    """
    acc: dict[tuple, list[float]] = defaultdict(list)
    for ts, r in _iter_log("shadow.jsonl", days):
        sym, sr = r.get("symbol"), r.get("spread_r")
        if not sym or sr is None:
            continue
        try:
            sr = float(sr)
        except (TypeError, ValueError):
            continue
        if not (0.0 < sr < 1.0):
            continue
        acc[(str(sym).upper(), DS._day_of(ts))].append(sr * R_PCT_OF_PRICE)
    return {k: statistics.median(v) for k, v in acc.items()}


def validate_cost(bars: dict[str, list[dict]], quoted: dict[tuple, float],
                  model: str) -> None:
    """Does the estimate agree with the quotes we do trust?"""
    pairs = []
    for (sym, day), q in quoted.items():
        b = bars.get(sym)
        if not b:
            continue
        est, src = name_cost_pct(b, day, model)
        if src == "fixed":
            continue
        pairs.append((est - GIVE_PCT, q))
    print("\n=== COST MODEL VALIDATION ===")
    if len(pairs) < 10:
        print(f"  only {len(pairs)} name-days have a trustworthy quote — "
              "the estimate stands unvalidated. Treat costs as indicative.")
        return
    est = [p[0] for p in pairs]
    obs = [p[1] for p in pairs]
    ratio = statistics.median(e / o for e, o in pairs if o > 0)
    print(f"  n={len(pairs)} name-days with a sane quoted spread (<1.0 R)")
    print(f"  estimated spread  median {statistics.median(est):.3f}% of price")
    print(f"  quoted spread     median {statistics.median(obs):.3f}% of price")
    print(f"  median ratio est/quoted = {ratio:.2f}  "
          f"({'estimate runs high' if ratio > 1.3 else 'estimate runs low' if ratio < 0.77 else 'agrees within 30%'})")
    if ratio < 0.77:
        print(f"  => costs below are a LOWER BOUND, so payX is an UPPER "
              f"bound. Divide payX by ~{1 / ratio:.1f} for the quoted-spread "
              f"reading. A universe that fails here fails harder in reality.")


# --------------------------------------------------------- triple cost lab

def live_give_pct(cfg: dict | None = None,
                  override: float | None = None) -> float:
    """Live trail give as a percent of price.

    ``give_r × R_PCT_OF_PRICE``, capped by ``ai_local_trail_give_max_pct``
    when that ceiling is > 0. Current live (give_r=0.2, max_pct=1.0) → 1.0.
    ``override`` (CLI ``--give-pct``) wins when set so sensitivity runs do
    not require editing bot_config.
    """
    if override is not None:
        return float(override)
    cfg = cfg if isinstance(cfg, dict) else {}
    try:
        give_r = float(cfg.get("ai_local_trail_give_r", 0.10) or 0.10)
    except (TypeError, ValueError):
        give_r = 0.10
    give = give_r * R_PCT_OF_PRICE
    try:
        max_pct = float(cfg.get("ai_local_trail_give_max_pct", 0.0) or 0.0)
    except (TypeError, ValueError):
        max_pct = 0.0
    if max_pct > 0:
        give = min(give, max_pct)
    return give


def fee_pct_from_bps(fee_bps: float) -> float:
    """Flat fee in percent of price. 10 bps → 0.10%. Default 0 invents nothing."""
    try:
        bps = float(fee_bps or 0.0)
    except (TypeError, ValueError):
        bps = 0.0
    return max(0.0, bps) / 100.0


def sip_rt_spread_pct(bid: float, ask: float) -> float | None:
    """Round-trip spread as percent of mid from a SIP NBBO.

    Definition used everywhere in the triple report (and in payX):

        RT% = 100 * (ask - bid) / mid

    That is one full touch (enter at ask, exit at bid) = 2 × half-spread%.
    Missing or crossed books return None so the caller can mark the
    name-day unpriceable rather than floor into fiction.
    """
    try:
        b, a = float(bid), float(ask)
    except (TypeError, ValueError):
        return None
    if b <= 0 or a <= 0 or a < b:
        return None
    mid = 0.5 * (a + b)
    if mid <= 0:
        return None
    return 100.0 * (a - b) / mid


def quote_is_fresh(sample_ts: float, quote_ts: float,
                   max_age_sec: float = SIP_QUOTE_MAX_AGE_SEC) -> bool:
    """True only when the quote is at or before the sample and younger than max_age.

    age = sample_ts − quote_ts. Future quotes and age ≥ max_age are
    unpriceable — excluded from SIP medians, never floored.
    """
    try:
        age = float(sample_ts) - float(quote_ts)
    except (TypeError, ValueError):
        return False
    return 0.0 <= age < float(max_age_sec)


def iex_spread_pct(bars: list[dict], day: str, symbol: str,
                   quoted: dict[tuple, float] | None = None
                   ) -> tuple[float, str]:
    """IEX-era spread term for the live_iex cost model.

    Prefer a sane shadow ``spread_r`` (desk IEX quotes) when present;
    otherwise Roll floored at one tick; otherwise the tick alone.
    """
    quoted = quoted or {}
    q = quoted.get((symbol, day))
    if q is not None and q > 0:
        return float(q), "shadow"
    path = [b for b in bars if b["day"] == day and DS._in_rth(b) and b["c"] > 0]
    if not path:
        # No RTH tape to estimate from — charge the legacy fixed spread
        # term so the row stays in the IEX median rather than vanishing.
        return max(0.0, COST_PCT - GIVE_PCT), "no_rth"
    price = statistics.median(b["c"] for b in path)
    floor = tick_spread_pct(price)
    roll = roll_spread_pct(bars, day)
    if roll is None:
        return floor, "tick"
    return max(roll, floor), "roll"


class SipQuoteCache:
    """Point-in-time SIP NBBO cache under benchmarks/universe_cost/.

    Research-only: ``feed=sip`` via ``alpaca_api.research_feed_rest`` /
    ``research_bar_end``. Never touches live entry feeds.
    """

    def __init__(self, cache_dir: Path | None = None):
        self.dir = Path(cache_dir or COST_CACHE_DIR)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._mem: dict[tuple[str, str], list[dict]] = {}
        self._client = None
        self._client_failed = False
        self.fetch_errors = 0
        self.fetch_ok = 0

    def _disk_path(self, symbol: str, day: str) -> Path:
        return self.dir / f"sip_quotes_{day}_{symbol.upper()}.json"

    def _load_disk(self, symbol: str, day: str) -> list[dict] | None:
        path = self._disk_path(symbol, day)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        rows = raw.get("quotes") if isinstance(raw, dict) else raw
        if not isinstance(rows, list):
            return None
        out = []
        for r in rows:
            try:
                out.append({
                    "ts": float(r["ts"]),
                    "bid": float(r["bid"]),
                    "ask": float(r["ask"]),
                })
            except (KeyError, TypeError, ValueError):
                continue
        out.sort(key=lambda x: x["ts"])
        return out

    def _save_disk(self, symbol: str, day: str, rows: list[dict]) -> None:
        path = self._disk_path(symbol, day)
        try:
            path.write_text(json.dumps({
                "symbol": symbol.upper(), "day": day, "feed": "sip",
                "quotes": rows,
            }), encoding="utf-8")
        except OSError:
            pass

    def _client_or_none(self):
        if self._client_failed:
            return None
        if self._client is not None:
            return self._client
        try:
            import alpaca_api as aa
            from config import load_config
            cfg = load_config()
            self._client = aa.connect_data_client(cfg)
            if self._client is None:
                self._client_failed = True
        except Exception:
            self._client_failed = True
            self._client = None
        return self._client

    def _fetch_day(self, symbol: str, day: str) -> list[dict]:
        """Fetch RTH SIP quotes for one name-day; empty list on failure."""
        client = self._client_or_none()
        if client is None:
            self.fetch_errors += 1
            return []
        try:
            import alpaca_api as aa
            from alpaca.data.requests import StockQuotesRequest
            from alpaca.data.enums import DataFeed
            start = datetime.strptime(day, "%Y-%m-%d").replace(
                tzinfo=timezone.utc) + timedelta(hours=9 + ET_OFFSET_H,
                                                 minutes=25)
            end_req = start + timedelta(hours=7)   # through ~16:25 ET
            end = aa.research_bar_end("sip", requested_end=end_req)
            if end <= start:
                self.fetch_errors += 1
                return []
            kw = {"feed": DataFeed.SIP}
            try:
                from alpaca.common.enums import Sort as _Sort
                kw["sort"] = _Sort.ASC
            except Exception:
                pass
            req = StockQuotesRequest(
                symbol_or_symbols=symbol.upper(),
                start=start, end=end, limit=10000, **kw)
            raw = client.get_stock_quotes(req)
            data = getattr(raw, "data", None) or {}
            quotes = data.get(symbol.upper()) or data.get(symbol) or []
            rows = []
            for q in quotes:
                ts = getattr(q, "timestamp", None)
                if ts is None:
                    continue
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                bid = float(getattr(q, "bid_price", 0) or 0)
                ask = float(getattr(q, "ask_price", 0) or 0)
                if bid <= 0 or ask <= 0:
                    continue
                rows.append({"ts": ts.timestamp(), "bid": bid, "ask": ask})
            rows.sort(key=lambda x: x["ts"])
            self.fetch_ok += 1
            return rows
        except Exception:
            self.fetch_errors += 1
            return []

    def quotes_for(self, symbol: str, day: str) -> list[dict]:
        key = (symbol.upper(), day)
        if key in self._mem:
            return self._mem[key]
        rows = self._load_disk(symbol, day)
        if rows is None:
            rows = self._fetch_day(symbol, day)
            if rows:
                self._save_disk(symbol, day, rows)
            else:
                # Cache the miss so we do not hammer a failing name-day.
                self._save_disk(symbol, day, [])
        self._mem[key] = rows or []
        return self._mem[key]

    def quote_at(self, symbol: str, day: str, sample_ts: float,
                 max_age_sec: float = SIP_QUOTE_MAX_AGE_SEC
                 ) -> dict | None:
        """Latest SIP quote at or before sample_ts with age < max_age_sec."""
        rows = self.quotes_for(symbol, day)
        if not rows:
            return None
        # Binary search for last quote_ts <= sample_ts.
        lo, hi = 0, len(rows) - 1
        best = None
        while lo <= hi:
            mid = (lo + hi) // 2
            if rows[mid]["ts"] <= sample_ts:
                best = rows[mid]
                lo = mid + 1
            else:
                hi = mid - 1
        if best is None:
            return None
        if not quote_is_fresh(sample_ts, best["ts"], max_age_sec):
            return None
        return best


def sip_spread_for_name_day(cache: SipQuoteCache, symbol: str, day: str,
                            sample_ts: float,
                            max_age_sec: float = SIP_QUOTE_MAX_AGE_SEC
                            ) -> tuple[float | None, str]:
    """SIP RT spread% at sample_ts, or (None, reason) when unpriceable."""
    if not sample_ts:
        return None, "no_sample_ts"
    q = cache.quote_at(symbol, day, float(sample_ts), max_age_sec)
    if q is None:
        # Distinguish miss vs stale when we have any quotes that day.
        rows = cache.quotes_for(symbol, day)
        if not rows:
            return None, "no_quotes"
        # Nearest at-or-before, even if stale — for the reason label only.
        prior = [r for r in rows if r["ts"] <= sample_ts]
        if not prior:
            return None, "no_prior_quote"
        age = float(sample_ts) - prior[-1]["ts"]
        if age >= max_age_sec:
            return None, "stale"
        return None, "unusable"
    rt = sip_rt_spread_pct(q["bid"], q["ask"])
    if rt is None:
        return None, "bad_book"
    return rt, "sip"


def triple_costs_for_name_day(
    bars: list[dict], day: str, symbol: str, sample_ts: float,
    give_live: float, fee_pct: float,
    quoted: dict[tuple, float] | None,
    sip_cache: SipQuoteCache | None,
) -> dict:
    """Per-name-day costs for fixed_079 / live_iex / live_sip.

    ``live_sip`` is None when unpriceable (stale/missing SIP quote). Those
    rows are excluded from SIP medians — never substituted with a floor.
    """
    iex_sp, iex_src = iex_spread_pct(bars, day, symbol, quoted)
    fixed = COST_PCT + fee_pct
    live_iex = give_live + iex_sp + fee_pct
    sip_sp, sip_src = (None, "no_cache")
    if sip_cache is not None:
        sip_sp, sip_src = sip_spread_for_name_day(
            sip_cache, symbol, day, sample_ts)
    live_sip = (give_live + sip_sp + fee_pct) if sip_sp is not None else None
    return {
        "fixed_079": fixed,
        "live_iex": live_iex,
        "live_sip": live_sip,
        "iex_spread_src": iex_src,
        "sip_src": sip_src,
        "give_live_pct": give_live,
        "fee_pct": fee_pct,
        "iex_spread_pct": iex_sp,
        "sip_spread_pct": sip_sp,
    }


def playability_from_costs(rows: list[dict], score: dict,
                           cost_key: str = "cost") -> dict:
    """Playability using ``cost_key`` on each row (skips rows with None)."""
    if not rows:
        return {"verdict": "EMPTY"}
    priced = [r for r in rows if r.get(cost_key) is not None]
    if not priced:
        return {"verdict": "UNPRICEABLE", "pay_x": None,
                "median_cost_pct": None, "coverage": 0.0,
                "why": f"no rows with {cost_key}"}
    med_mfe = statistics.median(r["mfe"] for r in priced)
    # Prefer the session-level score's M/A and green (same samples' days).
    ratio = score.get("mfe_over_mae") or 0.0
    green = (score["sessions_green"] / score["sessions"]) if score.get(
        "sessions") else 0.0
    costs = [float(r[cost_key]) for r in priced]
    med_cost = statistics.median(costs)
    bar = PLAYABLE_MULT * med_cost
    clears = sum(1 for r in priced if r["mfe"] >= r[cost_key]) / len(priced)
    fails = []
    if med_mfe < bar:
        fails.append(f"medMFE {med_mfe:.2f}% < {bar:.2f}%")
    if ratio < PLAYABLE_MIN_RATIO:
        fails.append(f"MFE/MAE {ratio:.2f} < {PLAYABLE_MIN_RATIO}")
    if green < PLAYABLE_MIN_GREEN:
        fails.append(f"green {green:.0%} < {PLAYABLE_MIN_GREEN:.0%}")
    return {
        "verdict": "PLAYABLE" if not fails else "UNPLAYABLE",
        "pay_x": med_mfe / med_cost if med_cost else None,
        "median_cost_pct": med_cost,
        "median_mfe": med_mfe,
        "bar_pct": bar,
        "pct_clearing_cost": clears,
        "n_priced": len(priced),
        "coverage": len(priced) / len(rows) if rows else 0.0,
        "why": "; ".join(fails) or "clears the pre-registered bar",
    }


def verdict_sip(play: dict, score: dict, sip_cov: float,
                cov_floor: float = SIP_COV_FLOOR) -> str:
    """Lab verdict on SIP cost only. Coverage below floor → UNPRICEABLE."""
    if sip_cov < cov_floor:
        return "UNPRICEABLE"
    n = int(score.get("n") or 0)
    sessions = int(score.get("sessions") or 0)
    if n < TRIPLE_THIN_N or sessions < TRIPLE_THIN_SESSIONS:
        return "THIN"
    if play.get("verdict") == "PLAYABLE":
        return "PLAYABLE"
    return "UNPLAYABLE"


def universe_label(name: str, max_shares_m: float) -> str:
    if name == "setup":
        return f"setup≤{max_shares_m:g}M"
    return name


def _fmt_pay(x) -> str:
    if x is None:
        return "  n/a"
    return f"{x:6.2f}"


def run_triple_cost_report(args, names: list[str], horizons: list[int],
                           bars: dict, plans: dict,
                           give_live: float, fee_pct: float) -> int:
    """Side-by-side fixed_079 / live_iex / live_sip payX for lab kill/keep."""
    quoted = load_quoted_spreads(args.days)
    sip_cache = SipQuoteCache(COST_CACHE_DIR)
    cov_floor = float(getattr(args, "sip_cov_floor", SIP_COV_FLOOR) or SIP_COV_FLOOR)

    print("\n=== TRIPLE COST REPORT (lab only) ===")
    print(f"  give_live={give_live:.3f}% of price  fee={fee_pct:.3f}%  "
          f"SIP max age={SIP_QUOTE_MAX_AGE_SEC:.0f}s  "
          f"sip_cov floor={cov_floor:.0%}")
    print("  RT SIP spread% = 100*(ask-bid)/mid (= 2× half-spread); "
          "stale/missing → unpriceable, excluded from SIP medians\n")

    hdr = (f"{'universe':<14}{'horiz':>6}{'n':>6}{'sess':>5}"
           f"{'medMFE':>8}{'M/A':>6}"
           f"{'payX_079':>9}{'payX_iex':>9}{'payX_sip':>9}"
           f"{'sip_cov%':>9}{'verdict_sip':>14}")
    print(hdr)
    print("-" * len(hdr))

    payload: dict = {
        "give_live_pct": give_live,
        "fee_pct": fee_pct,
        "sip_quote_max_age_sec": SIP_QUOTE_MAX_AGE_SEC,
        "sip_cov_floor": cov_floor,
        "cost_models": {
            "fixed_079": "flat 0.79% RT (HANDOFF reproduce)",
            "live_iex": "live give% + Roll/tick or shadow spread_r",
            "live_sip": "live give% + SIP RT 100*(ask-bid)/mid; age<60s",
        },
        "bar": {"mult": PLAYABLE_MULT, "min_ratio": PLAYABLE_MIN_RATIO,
                "min_green": PLAYABLE_MIN_GREEN},
        "results": {},
    }
    summary_lines = [
        "# Honest-cost triple payX (lab only)",
        "",
        f"give_live={give_live:.3f}%  fee={fee_pct:.3f}%  "
        f"SIP age<{SIP_QUOTE_MAX_AGE_SEC:.0f}s  cov_floor={cov_floor:.0%}",
        "",
        "Kill/keep is documentation only — no live desk / seed changes.",
        "",
        "```",
        hdr,
        "-" * len(hdr),
    ]

    for n in names:
        plan = plans.get(n) or {}
        if not plan:
            continue
        label = universe_label(n, args.max_shares_m)
        for hz in horizons:
            stride = args.stride or hz
            rows = []
            sip_priced = 0
            sip_total = 0
            for day, members in plan.items():
                for sym, elig in members.items():
                    b = bars.get(sym)
                    if not b:
                        continue
                    sample_ts = float(elig or 0.0)
                    costs = triple_costs_for_name_day(
                        b, day, sym, sample_ts, give_live, fee_pct,
                        quoted, sip_cache)
                    got = DS.sample_excursions(
                        b, day, hz, stride, sample_ts, True)
                    for r in got:
                        r["cost_fixed_079"] = costs["fixed_079"]
                        r["cost_live_iex"] = costs["live_iex"]
                        r["cost_live_sip"] = costs["live_sip"]
                        r["cost"] = costs["fixed_079"]  # default for score
                    if got:
                        sip_total += 1
                        if costs["live_sip"] is not None:
                            sip_priced += 1
                    rows.extend(got)
            s = DS.score(rows)
            if s["verdict"] == "EMPTY":
                line = (f"{label:<14}{hz:>6}{0:>6}{'':>5}"
                        f"{'':>8}{'':>6}{'':>9}{'':>9}{'':>9}"
                        f"{'':>9}{'EMPTY':>14}")
                print(line)
                summary_lines.append(line)
                payload["results"][f"{n}@{hz}m"] = {
                    "label": label, "drift": s, "empty": True}
                continue
            p079 = playability_from_costs(rows, s, "cost_fixed_079")
            piex = playability_from_costs(rows, s, "cost_live_iex")
            psip = playability_from_costs(rows, s, "cost_live_sip")
            # Coverage is share of name-days (not samples) with a fresh SIP quote.
            sip_cov = (sip_priced / sip_total) if sip_total else 0.0
            # Recompute SIP play using only priced rows' coverage signal.
            if psip.get("coverage") is not None and sip_total:
                # Prefer name-day coverage for the verdict floor.
                pass
            v_sip = verdict_sip(psip, s, sip_cov, cov_floor)
            ratio = s.get("mfe_over_mae") or 0.0
            line = (f"{label:<14}{hz:>6}{s['n']:>6}{s['sessions']:>5}"
                    f"{s['median_mfe']:>8.3f}{ratio:>6.2f}"
                    f"{_fmt_pay(p079.get('pay_x'))}"
                    f"{_fmt_pay(piex.get('pay_x'))}"
                    f"{_fmt_pay(psip.get('pay_x'))}"
                    f"{sip_cov:>8.0%}{v_sip:>14}")
            print(line)
            summary_lines.append(line)
            payload["results"][f"{n}@{hz}m"] = {
                "label": label,
                "horizon_min": hz,
                "drift": s,
                "pay_x_079": p079.get("pay_x"),
                "pay_x_iex": piex.get("pay_x"),
                "pay_x_sip": psip.get("pay_x"),
                "sip_cov": sip_cov,
                "sip_name_days_priced": sip_priced,
                "sip_name_days_total": sip_total,
                "verdict_sip": v_sip,
                "playable_079": p079,
                "playable_iex": piex,
                "playable_sip": psip,
            }
            # Lab kill/keep notes for setup @15m (and longer).
            if n == "setup" and hz in (15, 30, 60):
                note = _setup_kill_keep_note(
                    hz, psip.get("pay_x"), sip_cov, cov_floor, s, psip)
                if note:
                    payload["results"][f"{n}@{hz}m"]["lab_note"] = note
        print()

    print(f"SIP quote cache: ok={sip_cache.fetch_ok}  "
          f"errors={sip_cache.fetch_errors}  dir={sip_cache.dir}")
    summary_lines.extend(["```", ""])
    summary_lines.append(
        f"SIP fetch ok={sip_cache.fetch_ok} errors={sip_cache.fetch_errors}")
    # early_rvol shelf note
    for key, cell in payload["results"].items():
        if key.startswith("early_rvol@") and cell.get("verdict_sip") in (
                "PLAYABLE", "THIN"):
            hz = cell.get("horizon_min")
            if hz and hz >= 60:
                summary_lines.append(
                    f"- early_rvol @{hz}m clears SIP bar → "
                    "86s shelf is the wrong harvest window; no live change.")
                break

    day = datetime.now().strftime("%Y-%m-%d")
    out_arg = getattr(args, "out", None) or ""
    if out_arg:
        outp = Path(out_arg)
        if not outp.is_absolute():
            outp = Path(ROOT) / outp
    else:
        outp = COST_CACHE_DIR / f"honest_{day}.json"
    outp.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "day": day,
        "universes": names,
        "horizons": horizons,
        "days": args.days,
        "max_shares_m": args.max_shares_m,
        "lab_only": True,
        **payload,
    }
    outp.write_text(json.dumps(doc, indent=2, default=str), encoding="utf-8")
    md = outp.parent / "summary.md"
    summary_lines.append(f"wrote {outp}")
    md.write_text("\n".join(summary_lines) + "\n", encoding="utf-8")
    print(f"\nwrote {outp}")
    print(f"wrote {md}")
    return 0


def _setup_kill_keep_note(hz: int, pay_sip, sip_cov: float, cov_floor: float,
                          score: dict, play: dict) -> str | None:
    if sip_cov < cov_floor:
        return (f"setup @{hz}m SIP unpriceable "
                f"(cov={sip_cov:.0%} < {cov_floor:.0%}); no lab kill.")
    if pay_sip is None:
        return None
    n = int(score.get("n") or 0)
    if pay_sip < PLAYABLE_MULT and n >= TRIPLE_THIN_N:
        return (f"setup @{hz}m SIP-payX={pay_sip:.2f} < {PLAYABLE_MULT:.0f} "
                f"with n={n} — lab-kill this constructor (document only; "
                "do not change live seeds).")
    if pay_sip >= PLAYABLE_MULT and play.get("verdict") != "PLAYABLE":
        return (f"setup @{hz}m SIP-payX={pay_sip:.2f} ≥ {PLAYABLE_MULT:.0f} "
                "but green/M/A/n still fail — magnitude lead, not PLAYABLE.")
    return None


# ------------------------------------------------------------------ scoring

def playability(rows: list[dict], score: dict) -> dict:
    """Is the drift big enough to pay for the round trip?

    Separate from the drift verdict on purpose. A universe can drift
    cleanly on a move too small to clear the spread, and calling that a
    pass is how a screen produces a tradeable-looking result that loses
    money on contact.
    """
    if not rows:
        return {"verdict": "EMPTY"}
    med_mfe = score["median_mfe"]
    ratio = score.get("mfe_over_mae") or 0.0
    green = (score["sessions_green"] / score["sessions"]) if score["sessions"] else 0.0
    # Each sample is charged its OWN name's round trip. Comparing a median
    # MFE against a pooled median cost would let a universe of cheap names
    # borrow the spread of an expensive one.
    costs = [r.get("cost", COST_PCT) for r in rows]
    med_cost = statistics.median(costs)
    bar = PLAYABLE_MULT * med_cost
    clears = sum(1 for r in rows
                 if r["mfe"] >= r.get("cost", COST_PCT)) / len(rows)
    fails = []
    if med_mfe < bar:
        fails.append(f"medMFE {med_mfe:.2f}% < {bar:.2f}%")
    if ratio < PLAYABLE_MIN_RATIO:
        fails.append(f"MFE/MAE {ratio:.2f} < {PLAYABLE_MIN_RATIO}")
    if green < PLAYABLE_MIN_GREEN:
        fails.append(f"green {green:.0%} < {PLAYABLE_MIN_GREEN:.0%}")
    return {
        "verdict": "PLAYABLE" if not fails else "UNPLAYABLE",
        "pay_x": med_mfe / med_cost if med_cost else None,
        "median_cost_pct": med_cost,
        "bar_pct": bar,
        "mfe_r": med_mfe / R_PCT_OF_PRICE,
        "pct_clearing_cost": clears,
        "why": "; ".join(fails) or "clears the pre-registered bar",
    }


def price_band(plan: dict[str, dict[str, float]], bars: dict[str, list[dict]],
               lo: float, hi: float) -> dict[str, dict[str, float]]:
    """The same universe, sliced by what the names cost.

    The point of the slice: cheap names carry a structurally wider spread
    (one tick is 0.50% of a $2 stock and 0.02% of a $50 one), so a
    universe that looks identical on excursion can differ several-fold on
    what it costs to harvest. That is the one lever this desk controls.
    """
    out: dict[str, dict[str, float]] = defaultdict(dict)
    for day, members in plan.items():
        for sym, elig in members.items():
            b = bars.get(sym)
            if not b:
                continue
            path = [x for x in b if x["day"] == day and DS._in_rth(x)
                    and x["c"] > 0]
            if not path:
                continue
            px = statistics.median(x["c"] for x in path)
            if lo <= px < hi:
                out[day][sym] = elig
    return dict(out)


def resolve(name: str, days: int, args, bars: dict | None = None,
            syms: list[str] | None = None) -> dict[str, dict[str, float]]:
    """One universe -> day -> {symbol: eligibility ts}."""
    if name == "desk":
        return DS.load_shadow_universe(days, "all")
    if name.startswith("desk_px:"):
        lo, _, hi = name.split(":", 1)[1].partition("-")
        return price_band(DS.load_shadow_universe(days, "all"), bars or {},
                          float(lo), float(hi or "inf"))
    if name == "rejects":
        return load_rejects(days)
    if name.startswith("rejects:"):
        return load_rejects(days, name.split(":", 1)[1])
    if name == "burst":
        return load_burst(days)
    if name == "early_rvol":
        return load_early_rvol(days, args.rvol)
    if name == "catalyst":
        return load_catalyst(days, syms or [], args.news_age, args.refresh_news)
    if name == "setup":
        return load_setup(days, args.max_shares_m)
    if name == "gap_hold":
        return build_gap_hold(bars or {}, args.gap)
    if name == "liquid":
        return DS.load_file_universe("liquid", days)
    return {}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--universes",
                    default="setup,desk,desk_px:0-10,desk_px:10-50,desk_px:50-,"
                            "rejects,burst,early_rvol,gap_hold,liquid")
    ap.add_argument("--max-shares-m", type=float, default=10.0,
                    help="setup: shares outstanding cap in millions")
    ap.add_argument("--cost-model", default="measured",
                    choices=("measured", "fixed"),
                    help="measured = give + this name's own estimated spread "
                         "(Roll, floored at one tick). fixed = the flat 0.79%% "
                         "used before 2026-08-23, kept for reproducibility. "
                         "Ignored when --cost-report triple.")
    ap.add_argument("--cost-report", default="none",
                    choices=("none", "triple"),
                    help="triple = side-by-side fixed_079 / live_iex / "
                         "live_sip payX (lab only; writes "
                         "benchmarks/universe_cost/).")
    ap.add_argument("--give-pct", type=float, default=None,
                    help="override live give%% for triple report "
                         "(default = from bot_config trail knobs)")
    ap.add_argument("--fee-bps", type=float, default=0.0,
                    help="optional flat fee in bps added to all three "
                         "triple-report models (default 0)")
    ap.add_argument("--sip-cov-floor", type=float, default=SIP_COV_FLOOR,
                    help="min SIP name-day coverage for a real verdict_sip "
                         "(else UNPRICEABLE)")
    ap.add_argument("--out", default="",
                    help="triple report JSON path "
                         "(default benchmarks/universe_cost/honest_YYYY-MM-DD.json)")
    ap.add_argument("--days", type=int, default=20)
    ap.add_argument("--horizons", default="15,30,60")
    ap.add_argument("--stride", type=int, default=0,
                    help="0 = horizon (non-overlapping, the honest default)")
    ap.add_argument("--limit-symbols", type=int, default=0,
                    help="0 = no cap. Over the cap a seeded sample is taken.")
    ap.add_argument("--rvol", type=float, default=5.0, help="early_rvol floor")
    ap.add_argument("--gap", type=float, default=3.0, help="gap_hold gap %%")
    ap.add_argument("--news-age", type=float, default=60.0,
                    help="catalyst: max headline age at the actionable instant")
    ap.add_argument("--refresh-news", action="store_true")
    args = ap.parse_args()

    horizons = [int(x) for x in args.horizons.split(",") if x.strip()]
    names = [u.strip() for u in args.universes.split(",") if u.strip()]

    # Symbol universe: everything the desk saw OR turned down, which is the
    # widest set with usable history. gap_hold and catalyst are resolved
    # against it rather than against shadow alone.
    pool: set[str] = set()
    for plan in (DS.load_shadow_universe(args.days, "all"), load_rejects(args.days)):
        for d in plan.values():
            pool.update(d)
    for n in names:
        if n == "liquid":
            for d in DS.load_file_universe("liquid", args.days).values():
                pool.update(d)
    syms = DS.select_symbols(sorted(pool), args.limit_symbols)
    if not syms:
        print("no symbols resolved — is shadow.jsonl present?")
        return 0

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days + 5)
    print(f"universe screen  universes={names}  horizons={horizons}min")
    print(f"  symbol pool={len(syms)} (shadow + rejects)  "
          f"window={start.date()}..{end.date()}")

    cfg = {}
    try:
        from config import load_config
        cfg = load_config() or {}
    except Exception:
        cfg = {}
    give_live = live_give_pct(cfg, override=args.give_pct)
    fee_pct = fee_pct_from_bps(args.fee_bps)

    if args.cost_report == "triple":
        print(f"  cost report: triple  give_live={give_live:.3f}%  "
              f"fee={fee_pct:.3f}%")
    else:
        print(f"  cost model: {args.cost_model}", end="")
        if args.cost_model == "measured":
            print(f" — give {GIVE_PCT:.2f}% + per-name spread "
                  f"(Roll, floored at one ${TICK_USD:.2f} tick)")
        else:
            print(f" — flat {COST_PCT:.2f}% for every name")
    print(f"  playable needs medMFE >= {PLAYABLE_MULT:.0f}x that name's own "
          f"round trip, MFE/MAE >= {PLAYABLE_MIN_RATIO}, "
          f"green >= {PLAYABLE_MIN_GREEN:.0%}\n")

    bars = DS.fetch_minutes(syms, start, end)
    if not bars:
        print("  no Alpaca data client — run on the mini with .venv.")
        return 0
    print(f"  bars for {len(bars)}/{len(syms)} symbols")

    plans = {}
    for n in names:
        try:
            plans[n] = resolve(n, args.days, args, bars=bars, syms=syms)
        except Exception as e:  # noqa: BLE001
            print(f"  {n}: could not resolve ({type(e).__name__}: {e})")
            plans[n] = {}
        if not plans[n]:
            print(f"  {n}: empty universe")

    if args.cost_report == "triple":
        return run_triple_cost_report(
            args, names, horizons, bars, plans, give_live, fee_pct)

    validate_cost(bars, load_quoted_spreads(args.days), args.cost_model)
    print()

    hdr = (f"{'universe':<16}{'horiz':>6}{'names':>7}{'n':>7}{'sess':>5}"
           f"{'medPx':>8}{'cost%':>7}{'medMFE':>8}{'M/A':>6}{'sigma':>7}"
           f"{'payX':>6}{'clear':>7}{'green':>7}{'drift':>11}{'play':>12}")
    print(hdr)
    print("-" * len(hdr))
    payload = {}
    cost_src: dict[str, int] = defaultdict(int)
    for n in names:
        plan = plans.get(n) or {}
        if not plan:
            continue
        n_names = sum(len(v) for v in plan.values())
        prices = []
        for day, members in plan.items():
            for sym in members:
                b = bars.get(sym)
                p = [x["c"] for x in (b or []) if x["day"] == day and x["c"] > 0]
                if p:
                    prices.append(statistics.median(p))
        med_px = statistics.median(prices) if prices else float("nan")
        for hz in horizons:
            stride = args.stride or hz
            rows = []
            for day, members in plan.items():
                for sym, elig in members.items():
                    b = bars.get(sym)
                    if not b:
                        continue
                    cost, src = name_cost_pct(b, day, args.cost_model)
                    cost_src[src] += 1
                    got = DS.sample_excursions(
                        b, day, hz, stride, float(elig or 0.0), True)
                    for r in got:
                        r["cost"] = cost
                    rows.extend(got)
            s = DS.score(rows)
            p = playability(rows, s) if s["verdict"] != "EMPTY" else {"verdict": "EMPTY"}
            payload[f"{n}@{hz}m"] = {"drift": s, "playable": p,
                                     "name_days": n_names}
            if s["verdict"] == "EMPTY":
                print(f"{n:<16}{hz:>6}{n_names:>7}{0:>7}   (no samples)")
                continue
            print(f"{n:<16}{hz:>6}{n_names:>7}{s['n']:>7}{s['sessions']:>5}"
                  f"{med_px:>8.2f}{(p.get('median_cost_pct') or 0):>7.3f}"
                  f"{s['median_mfe']:>8.3f}"
                  f"{(s['mfe_over_mae'] or 0):>6.2f}{s['sigma']:>7.2f}"
                  f"{(p.get('pay_x') or 0):>6.2f}"
                  f"{(p.get('pct_clearing_cost') or 0):>7.0%}"
                  f"{s['sessions_green']}/{s['sessions']:<4}"
                  f"{s['verdict']:>11}{p['verdict']:>12}")
        print()

    tot = sum(cost_src.values()) or 1
    print("cost sources: " + ", ".join(
        f"{k} {v / tot:.0%}" for k, v in sorted(cost_src.items())))
    print(f"  'tick' rows are the ${TICK_USD:.2f} FLOOR, not a measurement — "
          "a lower bound where Roll had no bounce to read.")
    print(f"give is {GIVE_PCT:.2f}% of price on every name by construction "
          f"(0.10R, 1R = {R_PCT_OF_PRICE:.0f}% of price), so it does NOT")
    print("vary with price. Where a cheap and an expensive universe differ in")
    print("cost, only the spread term is moving, and it is the smaller one.")
    print("Both verdicts must pass. DRIFT alone is direction too small to")
    print("pay; PLAYABLE alone is range without direction, which is exactly")
    print("what a trailing stop cannot harvest.")
    print("payX = median MFE / that universe's own median round trip. Below")
    print("1.00 the median sample cannot cover its own costs however it is")
    print("traded. clear = share of samples whose MFE beats their own cost.")

    SCREEN_DIR.mkdir(parents=True, exist_ok=True)
    day = datetime.now().strftime("%Y-%m-%d")
    outp = SCREEN_DIR / f"universe_{day}.json"
    outp.write_text(json.dumps({
        "day": day, "universes": names, "horizons": horizons,
        "days": args.days, "stride": args.stride or "horizon",
        "cost_model": args.cost_model,
        "bar": {"mult": PLAYABLE_MULT, "fixed_cost_pct": COST_PCT,
                "give_pct": GIVE_PCT,
                "min_ratio": PLAYABLE_MIN_RATIO, "min_green": PLAYABLE_MIN_GREEN},
        "results": payload,
    }, indent=2, default=str), encoding="utf-8")
    print(f"\nwrote {outp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
