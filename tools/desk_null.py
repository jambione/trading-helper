#!/usr/bin/env python3
"""Shared nulls for every entry claim on this desk.

The 2026-08-20 measurement stack proved two things at once: the live scalp has
no edge, and the WITHIN dart that said so was loaded with hindsight (it could
buy the morning run-up that put the name on the list). This module is the
replacement kernel so the next thesis is graded against controls that answer
the question we actually mean.

  ELIGIBLE-WITHIN  same name, same day, only after the name is on the
                   watchlist. Kills the pre-list run-up. The admission bar
                   itself is excluded; overlapping forward windows are
                   accepted — a ±30m clock match with a 30m horizon cannot
                   also be non-overlapping.
  LEGACY-WITHIN    the old dart (any RTH bar, |Δt| > horizon). Kept so a
                   re-run can reproduce the handoff numbers and show how
                   much hindsight inflated them.
  OUTSIDE          never-watched names, same instant, price-matched.
  OUTSIDE-VOL      same, also matched on 15m realized vol. Skip rather than
                   silently fall back when the band is empty.
  BENCH            IWM (small-cap) over the same horizon. Residual =
                   name − bench. Until this is off zero, "watched names
                   drift" is not a name-level claim.
  HAIRCUT          round-trip spread, default 20 bps. Cancels in a paired
                   same-cost comparison; it is the bar versus *cash*.

A slice PASSES gate 1 only when n≥30, median net-of-haircut > 0, AND
eligible-within paired median > 0 at ≥2σ. Anything else is FAIL or
UNDERPOWERED. Passing the screen is not a config change.

Read-only. Imported by admission_null, entry_rule_screen, thesis_screen.
"""
from __future__ import annotations

import json
import math
import re
import statistics
from collections import defaultdict
from pathlib import Path

import bars
import shadow_report as sr

ROOT = Path(__file__).resolve().parent.parent

RTH_START_MIN = 9 * 60 + 35
RTH_END_MIN = 15 * 60 + 30
RTH_OPEN_MIN = 9 * 60 + 30
WITHIN_DRAWS = 8
ELIGIBLE_EXCLUDE_SEC = 60.0
ACROSS_TOL_SEC = 90.0
OUTSIDE_POOL = 150
OUTSIDE_MIN_BARS = 200
OUTSIDE_MIN_PRICE = 5.0
OUTSIDE_BAND = (0.5, 2.0)
VOL_BAND = (0.5, 2.0)
VOL_LOOKBACK = 900.0
HAIRCUT_PCT = 0.20
BENCH = "IWM"
MIN_N = 30
MIN_SIGMA = 2.0
# Session-level gate. Admissions inside one afternoon share that afternoon's
# drift, so pooling them and dividing by sqrt(n) counts one market observation
# many times. The late 14:00-15:30 slice passed at a pooled 2.3-3.0σ on n=136
# while 101 of 147 of those admissions came from a single session (2026-08-14);
# by session it was 4/5 positive, p=0.156 — not a finding.
#
# MIN_SESSIONS is not a taste: with D sessions the best possible one-sided sign
# test is 1/2^D, so below D=5 even a perfect record cannot reach p<=0.05. Fewer
# than that is UNDERPOWERED, never PASS.
MIN_SESSIONS = 5
SESSION_P = 0.05
# One session may not BE the sample. A slice can clear the sign test and still
# be one afternoon wearing four scraps as a disguise.
MAX_DAY_SHARE = 0.50
UNIVERSE = ROOT / "valid_tickers.txt"
CHASE_PCT = 2.0
FRESH_PCT = 1.0

TOD_BINS = (
    ("open_drive", RTH_START_MIN, 10 * 60),
    ("morning", 10 * 60, 12 * 60),
    ("midday", 12 * 60, 14 * 60),
    ("late", 14 * 60, RTH_END_MIN + 1),
)

RESEARCH_SOURCES = frozenset({"research", "grok", "claude", "xai"})
_RESEARCH_FILE = re.compile(r"(?:grok|claude)_research_(\d{8})_\d+\.md$", re.IGNORECASE)

# Re-exports so existing imports keep working after the move.
_day = bars.day_of
_et_minutes = bars.et_minutes
_index_at = bars.index_at


def load_universe() -> list[str]:
    try:
        return [ln.strip().upper() for ln in UNIVERSE.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError:
        return []


def build_outside_pool(day: str, watched: set[str], rng, feed: str) -> dict:
    """symbol -> (stamps, closes) for tradeable names the desk never watched."""
    universe = [s for s in load_universe() if s not in watched]
    if not universe:
        return {}
    pick = rng.sample(universe, min(OUTSIDE_POOL, len(universe)))
    bars.fetch_many(pick, day, feed)
    pool = {}
    for s in pick:
        stamps, closes = bars.fetch(s, day, feed)
        if not stamps or len(stamps) < OUTSIDE_MIN_BARS:
            continue
        if closes[0] < OUTSIDE_MIN_PRICE:
            continue
        pool[s] = (stamps, closes)
    return pool


def first_watch_map(rows) -> dict[tuple[str, str], float]:
    """(symbol, day) -> earliest shadow tick that session."""
    out: dict[tuple[str, str], float] = {}
    for r in rows:
        ts = r.get("ts")
        if not ts:
            continue
        key = (str(r.get("symbol") or "").upper(), bars.day_of(ts))
        t = float(ts)
        if not key[0]:
            continue
        if key not in out or t < out[key]:
            out[key] = t
    return out


def collect_admissions(rows) -> list[tuple]:
    """One (t0, sym, day, first_row) per episode whose first tick is RTH."""
    out = []
    for (sym, _adm), series in sr.by_episode(rows).items():
        if not series:
            continue
        t0 = float(series[0].get("ts") or 0)
        if not t0 or not (RTH_START_MIN <= bars.et_minutes(t0) <= RTH_END_MIN):
            continue
        out.append((t0, str(sym).upper(), bars.day_of(t0), series[0]))
    return out


def watched_times(rows) -> dict[str, set]:
    """day -> {(ts, symbol)} for the ACROSS control."""
    out: dict[str, set] = defaultdict(set)
    for r in rows:
        ts = r.get("ts")
        if not ts:
            continue
        out[bars.day_of(ts)].add((float(ts), str(r.get("symbol") or "").upper()))
    return out


def tod_bucket(ts: float) -> str:
    m = bars.et_minutes(ts)
    for name, lo, hi in TOD_BINS:
        if lo <= m < hi:
            return name
    return "other"


def minutes_since_open(ts: float) -> int:
    return bars.et_minutes(ts) - RTH_OPEN_MIN


def legacy_within_stamps(stamps, t0: float, horizon: float) -> list[float]:
    """Any RTH bar whose forward window does not overlap t0. Hindsight-loaded."""
    return [s for s in stamps
            if RTH_START_MIN <= bars.et_minutes(s) <= RTH_END_MIN
            and abs(s - t0) > horizon]


def eligible_stamps(stamps, t0: float, first_watch: float,
                    exclude_sec: float = ELIGIBLE_EXCLUDE_SEC) -> list[float]:
    """RTH bars at/after the name joined the list, not the admission bar.

    Bars before first_watch are the run-up the dart used to beat us with.
    The admission bar is dropped so the control is a different instant, not
    a re-score of the same one. Overlap of forward windows is allowed.
    """
    floor = first_watch if first_watch else t0
    out = []
    for s in stamps:
        if s < floor:
            continue
        if abs(s - t0) < exclude_sec:
            continue
        if not (RTH_START_MIN <= bars.et_minutes(s) <= RTH_END_MIN):
            continue
        out.append(s)
    return out


def median_forward(stamps, closes, sample_times, horizon: float, rng, n_draws: int = WITHIN_DRAWS):
    if not sample_times:
        return None
    pick = (sample_times if len(sample_times) <= n_draws
            else rng.sample(sample_times, n_draws))
    draws = []
    for s in pick:
        v = bars.forward_return(stamps, closes, s, horizon)
        if v is not None:
            draws.append(v)
    return statistics.median(draws) if draws else None


def _outside_forwards(pool, t0, horizon, p0, vol0, want_vol: bool) -> list[float]:
    others = []
    for o_st, o_cl in pool.values():
        i = bars.index_at(o_st, t0)
        if i < 0:
            continue
        if p0:
            ratio = o_cl[i] / p0
            if not (OUTSIDE_BAND[0] <= ratio <= OUTSIDE_BAND[1]):
                continue
        if want_vol:
            if vol0 is None or vol0 <= 0:
                return []
            ov = bars.realized_vol(o_st, o_cl, t0, VOL_LOOKBACK)
            if ov is None or ov <= 0:
                continue
            vr = ov / vol0
            if not (VOL_BAND[0] <= vr <= VOL_BAND[1]):
                continue
        v = bars.forward_return(o_st, o_cl, t0, horizon)
        if v is not None:
            others.append(v)
    return others


def score_one(t0: float, sym: str, day: str, horizon: float, ctx) -> dict | None:
    """Forward return at t0 plus every control. None when the window is missing."""
    horizon = capped_horizon(t0, horizon, getattr(ctx, "flatten_min", None))
    if horizon is None:
        return None
    stamps, closes = bars.fetch(sym, day, ctx.feed)
    if not stamps:
        return None
    fwd = bars.forward_return(stamps, closes, t0, horizon)
    if fwd is None:
        return None
    fw = ctx.first_watch.get((sym, day), t0)
    within = median_forward(
        stamps, closes, legacy_within_stamps(stamps, t0, horizon),
        horizon, ctx.rng, ctx.draws)
    eligible = median_forward(
        stamps, closes, eligible_stamps(stamps, t0, fw),
        horizon, ctx.rng, ctx.draws)
    i = bars.index_at(stamps, t0)
    p0 = closes[i] if i >= 0 else None
    vol0 = bars.realized_vol(stamps, closes, t0, VOL_LOOKBACK)
    pool = ctx.pools.get(day) or {}
    price_out = _outside_forwards(pool, t0, horizon, p0, vol0, want_vol=False)
    vol_out = _outside_forwards(pool, t0, horizon, p0, vol0, want_vol=True)
    bench_st, bench_cl = bars.fetch(ctx.bench, day, ctx.feed)
    bench_fwd = (bars.forward_return(bench_st, bench_cl, t0, horizon)
                 if bench_st else None)
    residual = (fwd - bench_fwd) if bench_fwd is not None else None
    open_move = bars.move_since_open(stamps, closes, t0)
    return {
        "t0": t0, "sym": sym, "day": day,
        "fwd": fwd,
        "net": fwd - ctx.haircut,
        "within": within,
        "eligible": eligible,
        "outside": statistics.median(price_out) if len(price_out) >= 3 else None,
        "outside_vol": statistics.median(vol_out) if len(vol_out) >= 3 else None,
        "bench": bench_fwd,
        "residual": residual,
        "open_move": open_move,
        "vol": vol0,
        "price": p0,
        "tod": tod_bucket(t0),
        "minutes_since_open": minutes_since_open(t0),
    }


def capped_horizon(t0: float, horizon: float, flatten_min: int | None) -> float | None:
    """Shorten the window so it does not run past the desk's EOD flatten.

    A 60m forward from 15:10 includes 16:10 — tape the live book never holds.
    None when t0 is already past flatten (or inside a 5-minute stub).
    """
    if flatten_min is None:
        return horizon
    remain_min = flatten_min - bars.et_minutes(t0)
    if remain_min < 5:
        return None
    return min(horizon, remain_min * 60.0)


class NullContext:
    def __init__(self, feed, rng, haircut, bench, first_watch, pools,
                 draws=WITHIN_DRAWS, flatten_min=None):
        self.feed = feed
        self.rng = rng
        self.haircut = haircut
        self.bench = bench
        self.first_watch = first_watch
        self.pools = pools
        self.draws = draws
        self.flatten_min = flatten_min


def require_bars_client() -> str | None:
    """None if 1m bars are reachable; otherwise a one-line reason to print.

    The MacBook clone has no Alpaca keys. Shadow + SIP live on the mini.
    Call this before prepare_context so an empty run is not a fake FAIL.
    """
    if bars.client() is None:
        return ("no Alpaca data client — these screens run on the mini "
                "(config/secrets.json api_key + the live shadow log)")
    return None


def prepare_context(rows, days, feed: str, rng, haircut: float = HAIRCUT_PCT,
                    bench: str = BENCH, build_outside: bool = True,
                    flatten_min: int | None = None) -> NullContext:
    first_watch = first_watch_map(rows)
    watched_by_day: dict[str, set] = defaultdict(set)
    for r in rows:
        if r.get("ts"):
            watched_by_day[bars.day_of(r["ts"])].add(str(r.get("symbol") or "").upper())
    pools = {}
    if build_outside:
        for day in days:
            pools[day] = build_outside_pool(day, watched_by_day[day], rng, feed)
            print(f"  {day}: {len(pools[day])} tradeable outside names "
                  f"(of {OUTSIDE_POOL} drawn)")
    for day in days:
        bars.fetch(bench, day, feed)
        bars.fetch_many(sorted(watched_by_day.get(day) or []), day, feed)
    return NullContext(feed, rng, haircut, bench, first_watch, pools,
                       flatten_min=flatten_min)


def score_moments(moments, horizon: float, ctx) -> list[dict]:
    """moments are (t0, sym, day) or (t0, sym, day, row). Drops unscoreable."""
    out = []
    for m in moments:
        t0, sym, day = m[0], m[1], m[2]
        s = score_one(t0, sym, day, horizon, ctx)
        if s is not None:
            out.append(s)
    return out


def paired_stats(diffs: list[float]) -> dict:
    if not diffs:
        return {"n": 0, "median": None, "mean": None, "beat": None, "sigma": None}
    n = len(diffs)
    beat = 100.0 * sum(1 for v in diffs if v > 0) / n
    se = 50.0 / (n ** 0.5)
    sigma = abs(beat - 50.0) / se if se else 0.0
    return {
        "n": n,
        "median": statistics.median(diffs),
        "mean": statistics.fmean(diffs),
        "beat": beat,
        "sigma": sigma,
    }


def sign_test_p(positive: int, sessions: int) -> float | None:
    """One-sided binomial p for *positive* of *sessions* under a coin flip."""
    if sessions <= 0:
        return None
    return sum(math.comb(sessions, i)
               for i in range(positive, sessions + 1)) / (2 ** sessions)


def session_stats(scores, key: str = "eligible") -> dict:
    """Per-session medians of the paired diff, and a sign test across them.

    The pooled sigma in paired_stats treats every admission as an independent
    draw. They are not: names admitted in the same afternoon share that
    afternoon's drift, so a pooled n counts one market observation once per
    name that happened to be on the list. Taking the SESSION as the unit of
    independence is the conservative reading, and on five sessions it is also
    the only honest one.

    Returns sessions, positive, p (one-sided sign test), max_share (largest
    session's fraction of the paired sample), and the per-day medians.
    """
    by_day: dict[str, list[float]] = {}
    for s in scores:
        v = s.get(key)
        if v is None:
            continue
        by_day.setdefault(str(s.get("day")), []).append(s["fwd"] - v)
    if not by_day:
        return {"sessions": 0, "positive": 0, "p": None,
                "max_share": None, "medians": {}}
    total = sum(len(v) for v in by_day.values())
    medians = {d: statistics.median(v) for d, v in by_day.items()}
    positive = sum(1 for m in medians.values() if m > 0)
    return {
        "sessions": len(medians),
        "positive": positive,
        "p": sign_test_p(positive, len(medians)),
        "max_share": max(len(v) for v in by_day.values()) / total if total else None,
        "medians": medians,
        "counts": {d: len(v) for d, v in by_day.items()},
    }


def format_sessions(st: dict) -> str:
    if not st["sessions"]:
        return "  sessions                                   n=0"
    p = st["p"]
    share = st["max_share"] or 0.0
    return (f"  by session (unit of independence)          "
            f"{st['positive']}/{st['sessions']} positive  "
            f"p={p:.3f}  biggest session {100 * share:.0f}% of sample")


def format_stat(label: str, vals: list[float]) -> str:
    if not vals:
        return f"  {label:<42} n=0"
    up = 100.0 * sum(1 for v in vals if v > 0) / len(vals)
    return (f"  {label:<42} n={len(vals):<5} "
            f"median {statistics.median(vals):+.3f}%  "
            f"mean {statistics.fmean(vals):+.3f}%  up {up:.0f}%")


def format_paired(label: str, diffs: list[float]) -> str:
    st = paired_stats(diffs)
    if st["n"] == 0:
        return f"  {label:<42} n=0"
    return (f"  {label:<42} n={st['n']:<5} "
            f"median {st['median']:+.3f}%  "
            f"mean {st['mean']:+.3f}%  "
            f"beat {st['beat']:.0f}%  ({st['sigma']:.1f}σ)")


_stat = format_stat
_paired = format_paired


def diffs(scores, key: str) -> list[float]:
    """score['fwd'] - score[key] for rows where key is present."""
    out = []
    for s in scores:
        v = s.get(key)
        if v is not None:
            out.append(s["fwd"] - v)
    return out


def verdict(scores, min_n: int = MIN_N, min_sigma: float = MIN_SIGMA,
            min_sessions: int = MIN_SESSIONS, session_p: float = SESSION_P,
            max_day_share: float = MAX_DAY_SHARE) -> str:
    """Gate 1: EMPTY / UNDERPOWERED / PASS / FAIL.

    PASS needs all four: enough moments, a median that beats cash after the
    haircut, a positive eligible-within paired median at min_sigma, AND
    session-level support — because the pooled sigma alone will happily
    certify one good afternoon (see session_stats).

    UNDERPOWERED, not FAIL, when there are simply too few moments or too few
    sessions to reach a verdict. The distinction matters: FAIL means the
    evidence is against, UNDERPOWERED means keep collecting.
    """
    if not scores:
        return "EMPTY"
    if len(scores) < min_n:
        return "UNDERPOWERED"
    net_med = statistics.median(s["net"] for s in scores)
    elig = diffs(scores, "eligible")
    st = paired_stats(elig)
    timing = (st["n"] >= min_n and st["median"] is not None
              and st["median"] > 0 and st["sigma"] >= min_sigma)
    # Evidence AGAINST is a verdict; decide it before asking about sessions.
    # A slice the eligible-within dart beats has failed on its own terms, and
    # calling that "keep collecting" would launder a negative into a maybe.
    if not (timing and net_med > 0):
        return "FAIL"
    # Pooled timing looks good — now ask whether it is one afternoon.
    sess = session_stats(scores, "eligible")
    if sess["sessions"] < min_sessions:
        return "UNDERPOWERED"
    clustered = (sess["p"] is not None and sess["p"] <= session_p
                 and (sess["max_share"] or 1.0) <= max_day_share)
    return "PASS" if clustered else "FAIL"


def diagnose(scores) -> str:
    """One-line reason the slice is not a trade, or why it passed."""
    v = verdict(scores)
    if v == "EMPTY":
        return "nothing scored"
    n = len(scores)
    net_med = statistics.median(s["net"] for s in scores)
    elig = paired_stats(diffs(scores, "eligible"))
    sess = session_stats(scores, "eligible")
    bits = [f"n={n}", f"net median {net_med:+.3f}%"]
    if elig["n"]:
        bits.append(f"vs eligible {elig['median']:+.3f}% {elig['sigma']:.1f}σ")
    if sess["sessions"]:
        bits.append(f"{sess['positive']}/{sess['sessions']} sessions "
                    f"p={sess['p']:.3f}")
    if v == "PASS":
        return ("beats cash after haircut, beats eligible-within at ≥2σ, and "
                "holds across sessions — " + ", ".join(bits))
    if v == "UNDERPOWERED":
        if sess["sessions"] and sess["sessions"] < MIN_SESSIONS:
            return (f"only {sess['sessions']} sessions; below {MIN_SESSIONS} "
                    f"even a perfect record cannot reach p≤{SESSION_P} — keep "
                    "collecting — " + ", ".join(bits))
        return "too few paired moments for a verdict — " + ", ".join(bits)
    reasons = []
    if net_med <= 0:
        reasons.append("median does not clear the spread vs cash")
    if not (elig["n"] >= MIN_N and elig["median"] is not None
            and elig["median"] > 0 and elig["sigma"] >= MIN_SIGMA):
        reasons.append("no timing edge vs eligible-within")
    if sess["p"] is not None and sess["p"] > SESSION_P:
        reasons.append(f"does not hold across sessions (p={sess['p']:.3f})")
    if (sess["max_share"] or 0) > MAX_DAY_SHARE:
        reasons.append(f"one session is {100 * sess['max_share']:.0f}% of the "
                       "sample — that is an afternoon, not an edge")
    return ("; ".join(reasons) or "failed") + " — " + ", ".join(bits)


# ── research tagging ──────────────────────────────────────────────────────


def _catalyst_re():
    try:
        from research_quality import _CATALYST_WORDS
        return _CATALYST_WORDS
    except ImportError:
        return re.compile(
            r"\b(earnings|catalyst|FDA|approval|contract|launch|guidance|"
            r"buyback|M&A|acquisition|tariff|rate.?cut|AI|datacenter)\b",
            re.IGNORECASE,
        )


def _parse_rows(text: str) -> list[dict]:
    try:
        from research_quality import _local_parse_rows
        return _local_parse_rows(text)
    except ImportError:
        return []


def _blank_day():
    return {"symbols": set(), "catalyst": set(), "champion": None, "reasons": {}}


def load_research_by_day(dirs=None) -> dict[str, dict]:
    """ET-day -> {symbols, catalyst, champion, reasons}.

    Reads dated grok/claude research markdown plus the live suggestion
    JSON snapshots. A symbol is 'research' for that day if it appeared in
    any of those. Catalyst is a text proxy (reason/summary hits the
    catalyst lexicon) — not a news timestamp. Event-time from bars is
    open_move (chase vs fresh), not this tag.
    """
    cat = _catalyst_re()
    out: dict[str, dict] = {}
    search = [Path(d) for d in (dirs or [ROOT / "claude_reports", ROOT / "ai_reports"])]
    for folder in search:
        if not folder.is_dir():
            continue
        for path in folder.glob("*_research_*.md"):
            m = _RESEARCH_FILE.search(path.name)
            if not m:
                continue
            raw = m.group(1)
            day = f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            rec = out.setdefault(day, _blank_day())
            rows = _parse_rows(text)
            best_sym, best_sc = rec.get("champion"), -1e9
            for r in rows:
                sym = str(r.get("symbol") or "").upper()
                if not sym:
                    continue
                rec["symbols"].add(sym)
                blob = " ".join(str(r.get(k) or "") for k in ("reason", "summary", "invalidation"))
                rec["reasons"][sym] = blob
                if cat.search(blob):
                    rec["catalyst"].add(sym)
                sc = r.get("score")
                try:
                    scf = float(sc) if sc is not None else None
                except (TypeError, ValueError):
                    scf = None
                if scf is not None and scf > best_sc:
                    best_sc, best_sym = scf, sym
            if best_sym:
                rec["champion"] = best_sym
    if dirs is not None:
        return out
    for snap in (ROOT / "grok_suggestions.json", ROOT / "claude_suggestions.json"):
        if not snap.is_file():
            continue
        try:
            data = json.loads(snap.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        ts = data.get("updated") or data.get("last_ok")
        if not ts:
            continue
        day = bars.day_of(ts)
        rec = out.setdefault(day, _blank_day())
        rows = data.get("rows") or data.get("suggestions") or []
        best_sym, best_sc = rec.get("champion"), -1e9
        for r in rows:
            if not isinstance(r, dict):
                continue
            sym = str(r.get("symbol") or r.get("ticker") or "").upper()
            if not sym:
                continue
            rec["symbols"].add(sym)
            blob = " ".join(str(r.get(k) or "") for k in ("reason", "summary", "invalidation"))
            rec["reasons"][sym] = blob
            if cat.search(blob):
                rec["catalyst"].add(sym)
            sc = r.get("trending_score", r.get("score"))
            try:
                scf = float(sc) if sc is not None else None
            except (TypeError, ValueError):
                scf = None
            rank = r.get("rank")
            if rank == 1:
                rec["champion"] = sym
            elif scf is not None and scf > best_sc and rec.get("champion") is None:
                best_sc, best_sym = scf, sym
                rec["champion"] = best_sym
    return out


def tag_admission(first_row: dict | None, score: dict, research: dict) -> dict:
    """Labels that split the book into theses. Pure function of logged fields."""
    day = score["day"]
    sym = score["sym"]
    info = research.get(day) or {}
    src = str((first_row or {}).get("source") or "").lower()
    in_list = sym in (info.get("symbols") or set())
    pipe = src in RESEARCH_SOURCES
    research_hit = bool(in_list or pipe)
    exh = (first_row or {}).get("exhaustion")
    rvol = (first_row or {}).get("rvol")
    try:
        rvol_f = None if rvol is None else float(rvol)
    except (TypeError, ValueError):
        rvol_f = None
    open_move = score.get("open_move")
    return {
        "tod": score["tod"],
        "research": research_hit,
        "scanner": not research_hit,
        "champion": info.get("champion") == sym,
        "catalyst": sym in (info.get("catalyst") or set()),
        "feature_ok": exh is not None,
        "rvol": rvol_f,
        "source": src,
        "chase": open_move is not None and open_move >= CHASE_PCT,
        "fresh": open_move is not None and abs(open_move) < FRESH_PCT,
        "open_move": open_move,
        "minutes_since_open": score.get("minutes_since_open"),
    }


def print_scorecard(scores, haircut: float, title: str = "") -> str:
    """Print the standard card. Returns the verdict string."""
    if title:
        print(f"\n=== {title}")
    if not scores:
        print("  EMPTY RUN — nothing scored; no verdict")
        return "EMPTY"
    print(format_stat("fwd (gross)", [s["fwd"] for s in scores]))
    print(format_stat(f"fwd − {haircut:.2f}% haircut (vs cash)",
                      [s["net"] for s in scores]))
    print(format_stat("IWM same instant",
                      [s["bench"] for s in scores if s.get("bench") is not None]))
    print(format_stat("residual name − IWM",
                      [s["residual"] for s in scores if s.get("residual") is not None]))
    print(format_paired("vs eligible-within (honest timing)", diffs(scores, "eligible")))
    print(format_paired("vs legacy-within (hindsight)", diffs(scores, "within")))
    print(format_paired("vs outside price-matched", diffs(scores, "outside")))
    print(format_paired("vs outside vol+price", diffs(scores, "outside_vol")))
    print(format_sessions(session_stats(scores, "eligible")))
    v = verdict(scores)
    print(f"  verdict {v} — {diagnose(scores)}")
    return v
