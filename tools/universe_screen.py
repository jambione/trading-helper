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
GIVE_PCT = 0.10 * R_PCT_OF_PRICE           # what the ratchet surrenders
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
    if name == "gap_hold":
        return build_gap_hold(bars or {}, args.gap)
    if name == "liquid":
        return DS.load_file_universe("liquid", days)
    return {}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--universes",
                    default="desk,desk_px:0-10,desk_px:10-50,desk_px:50-,"
                            "rejects,burst,early_rvol,gap_hold,liquid")
    ap.add_argument("--cost-model", default="measured",
                    choices=("measured", "fixed"),
                    help="measured = give + this name's own estimated spread "
                         "(Roll, floored at one tick). fixed = the flat 0.79%% "
                         "used before 2026-08-23, kept for reproducibility.")
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

    validate_cost(bars, load_quoted_spreads(args.days), args.cost_model)
    print()

    plans = {}
    for n in names:
        try:
            plans[n] = resolve(n, args.days, args, bars=bars, syms=syms)
        except Exception as e:  # noqa: BLE001
            print(f"  {n}: could not resolve ({type(e).__name__}: {e})")
            plans[n] = {}
        if not plans[n]:
            print(f"  {n}: empty universe")

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
        ets = sorted(_et_hm(t) for d in plan.values() for t in d.values() if t)
        med_et = ets[len(ets) // 2] if ets else (0, 0)
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
