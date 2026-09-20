#!/usr/bin/env python3
"""Phase B historical SIP square replay — Gate 1 buy/no-buy for Algo Trader Plus.

Answers computability only (not edge): on names Phase B admits, does delayed
historical SIP give ≥ ``rte_slow_native_length`` (112) 1‑min bars by 09:20 ET
so the dual-%R square can exist, and how often would square / pre-square have
been true?

Frozen pass bars (do not retune after seeing results) — see
``docs/SIP_HISTORICAL_REPLAY_CLI_BRIEF.md`` / Gate 1 in ``docs/SIP_FROM_250_PLAN.md``.

Admit kinds (seat/admit, not refuse):
  ``admit`` — Phase B seated a candidate
  ``dry_armed`` / ``dry_shadow`` — dry-run seats that also occupied a seat
  ``seed`` — forward-compat if ledger ever logs seed as a kind

Usage::

    .venv/bin/python tools/phase_b_sip_replay.py \\
      --ledger-dir ai_reports/phase_b_ledger \\
      --days 15 \\
      --feed-sip sip --feed-iex iex \\
      --out ai_reports/phase_b_sip_replay/

Offline summary from precomputed day JSONs::

    .venv/bin/python tools/phase_b_sip_replay.py \\
      --fixture-dir tests/fixtures/phase_b_sip_replay/summary_go \\
      --out /tmp/phase_b_sip_replay_go

WP1 counterfactual (re-filter an existing replay with prior-day $-vol)::

    .venv/bin/python tools/phase_b_sip_replay.py \\
      --rescore-dir ai_reports/phase_b_sip_replay \\
      --min-prior-dollar-vol 2000000 \\
      --out ai_reports/phase_b_sip_replay_liq2m/
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

ET = ZoneInfo("America/New_York")
UTC = timezone.utc

# ── Frozen Gate 1 bars (do not retune on the scoring window) ─────────────────
SLOW_LEN_DEFAULT = 112
WINDOW_START = (4, 0)   # inclusive ET
WINDOW_END = (9, 20)    # exclusive ET — bars with ts < 09:20
FETCH_END = (9, 30)     # fetch through open for context; count still < 09:20

GO_SIP_CLEAR_MIN = 0.60
GO_MEDIAN_BARS_ON_CLEAR = 150
GO_IEX_SIP_RATIO_MAX = 0.50   # IEX clear / SIP clear must be ≪ 1
NOGO_SIP_CLEAR_MAX = 0.40
NOGO_IEX_NEAR_SIP_RATIO = 0.80  # SIP ≈ IEX → choke is not the feed

# Sample floor — below this, rates are noise (Claude review 2026-09-20).
# Two in-sample days must not drive a $99 subscribe decision.
MIN_SESSIONS_FOR_VERDICT = 5
MIN_PAIRS_FOR_VERDICT = 50

ADMIT_KINDS = frozenset({"admit", "dry_armed", "dry_shadow", "seed"})

# Desk defaults when config is unavailable (match config.DEFAULT_CONFIG).
DEFAULT_RTE = {
    "rte_threshold": 20,
    "rte_fast_length": 21,
    "rte_fast_ewm_span": 7,
    "rte_slow_native_length": SLOW_LEN_DEFAULT,
    "rte_slow_ewm_span": 3,
    "rte_slow_timeframe": "",
    "rte_confluence_max": 15.0,
    "rte_min_boxes": 2,
    "ai_watch_exh_pre_thr": 35.0,
}


# ── Pure helpers (unit-tested) ───────────────────────────────────────────────

def load_jsonl(path: Path | str) -> list[dict]:
    rows: list[dict] = []
    p = Path(path)
    if not p.exists():
        return rows
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _day_bound(day: str, hour: int, minute: int) -> datetime:
    return datetime.fromisoformat(day).replace(
        hour=hour, minute=minute, second=0, microsecond=0, tzinfo=ET,
    )


def window_bounds(day: str) -> tuple[datetime, datetime]:
    """[04:00, 09:20) ET for the given calendar day."""
    return (
        _day_bound(day, WINDOW_START[0], WINDOW_START[1]),
        _day_bound(day, WINDOW_END[0], WINDOW_END[1]),
    )


def count_bars_in_window(index_ts, day: str) -> int:
    """Count bar timestamps in [04:00, 09:20) ET.

    ``index_ts`` is an iterable of timezone-aware or naive-UTC datetimes,
    pandas Timestamps, or unix floats.
    """
    t0, t1 = window_bounds(day)
    t0u, t1u = t0.astimezone(UTC), t1.astimezone(UTC)
    n = 0
    for ts in index_ts:
        dt = _as_utc_dt(ts)
        if dt is None:
            continue
        if t0u <= dt < t1u:
            n += 1
    return n


def clear_112(bar_count: int, slow_len: int = SLOW_LEN_DEFAULT) -> bool:
    return int(bar_count) >= int(slow_len)


def _as_utc_dt(ts) -> datetime | None:
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(float(ts), tz=UTC)
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            return ts.replace(tzinfo=UTC)
        return ts.astimezone(UTC)
    # pandas Timestamp
    try:
        import pandas as pd
        if isinstance(ts, pd.Timestamp):
            if ts.tzinfo is None:
                return ts.to_pydatetime().replace(tzinfo=UTC)
            return ts.to_pydatetime().astimezone(UTC)
    except Exception:
        pass
    try:
        s = str(ts).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except Exception:
        return None


def rte_cfg(overrides: dict | None = None) -> dict:
    cfg = dict(DEFAULT_RTE)
    if overrides:
        cfg.update(overrides)
    return cfg


def square_and_pre_square_minutes(
    df,
    day: str,
    cfg: dict | None = None,
) -> dict[str, Any]:
    """Walk SIP (or any) 1‑min OHLCV bars; tally square / pre_square in window.

    Uses ``signals.compute_percent_r_exhaustion`` with desk ``rte_*`` knobs.
    Square = dual OB (both ≥ −rte_threshold) and tight (|fast−slow| ≤
    rte_confluence_max). Pre-square = both ≥ −ai_watch_exh_pre_thr and tight.
    """
    import pandas as pd
    import signals

    c = rte_cfg(cfg)
    thr = float(c.get("rte_threshold", 20) or 20)
    pre_thr = float(c.get("ai_watch_exh_pre_thr", 35) or 35)
    confluence = float(c.get("rte_confluence_max", 15) or 15)
    slow_len = int(c.get("rte_slow_native_length", SLOW_LEN_DEFAULT) or SLOW_LEN_DEFAULT)

    empty = {
        "sip_bars_0415_0920": 0,
        "clear_112": False,
        "square_minutes": 0,
        "pre_square_minutes": 0,
        "slow_len": slow_len,
    }
    if df is None or getattr(df, "empty", True):
        return empty

    work = df.copy()
    if not isinstance(work.index, pd.DatetimeIndex):
        if "ts" in work.columns:
            work = work.set_index(pd.to_datetime(work["ts"], utc=True))
        else:
            return empty
    if work.index.tz is None:
        work.index = work.index.tz_localize(UTC)
    else:
        work.index = work.index.tz_convert(UTC)
    work = work.sort_index()
    for col in ("open", "high", "low", "close"):
        if col not in work.columns:
            return empty
        work[col] = pd.to_numeric(work[col], errors="coerce")
    work = work.dropna(subset=["high", "low", "close"])
    if work.empty:
        return empty

    t0, t1 = window_bounds(day)
    t0u, t1u = t0.astimezone(UTC), t1.astimezone(UTC)
    in_win = work.loc[(work.index >= t0u) & (work.index < t1u)]
    n_bars = int(len(in_win))

    scored = signals.compute_percent_r_exhaustion(work, c)
    # Restrict tallies to the premarket window after indicators see full series.
    win = scored.loc[(scored.index >= t0u) & (scored.index < t1u)]
    if win.empty:
        return {
            "sip_bars_0415_0920": n_bars,
            "clear_112": clear_112(n_bars, slow_len),
            "square_minutes": 0,
            "pre_square_minutes": 0,
            "slow_len": slow_len,
        }

    fast = win["s_percentR"]
    slow = win["l_percentR"]
    gap = (fast - slow).abs()
    tight = gap <= confluence
    # Desk square: rte_tight already = dual OB + tight; recompute explicitly.
    square = (fast >= -thr) & (slow >= -thr) & tight
    pre_square = (fast >= -pre_thr) & (slow >= -pre_thr) & tight

    return {
        "sip_bars_0415_0920": n_bars,
        "clear_112": clear_112(n_bars, slow_len),
        "square_minutes": int(square.fillna(False).sum()),
        "pre_square_minutes": int(pre_square.fillna(False).sum()),
        "slow_len": slow_len,
    }


def collect_admits_from_rows(
    rows: list[dict],
    *,
    max_symbols: int | None = None,
) -> list[dict[str, Any]]:
    """Dedupe admitted symbols from ledger rows. Returns list of {symbol,source,kind}."""
    seen: dict[str, dict[str, Any]] = {}
    for r in rows:
        kind = str(r.get("kind") or "").strip().lower()
        if kind not in ADMIT_KINDS:
            continue
        sym = str(r.get("symbol") or "").upper().strip()
        if not sym:
            continue
        if sym in seen:
            continue
        seen[sym] = {
            "symbol": sym,
            "source": str(r.get("source") or "") or None,
            "kind": kind,
            "universe": "phase_b_admit",
        }
        if max_symbols is not None and len(seen) >= int(max_symbols):
            break
    return list(seen.values())


def collect_fallback_symbols(
    day: str,
    boards: list[str],
    report_dir: Path,
    *,
    exclude: set[str] | None = None,
    max_symbols: int | None = None,
) -> list[dict[str, Any]]:
    """Soft-seed momentum∪research names seen 04:00–09:20 ET that day.

    Labeled ``universe=fallback_*`` so they are never mixed into admit rates.
    research ← ``seed_rank.jsonl`` slots in window.
    momentum ← ``seed_rank.jsonl`` is research-only; momentum falls back to
    movers snapshot if present (``movers_stocks.json``) — best-effort, may be
    empty historically.
    """
    exclude = exclude or set()
    out: dict[str, dict[str, Any]] = {}
    t0, t1 = window_bounds(day)
    t0u, t1u = t0.timestamp(), t1.timestamp()
    want = {b.strip().lower() for b in boards if b.strip()}

    if "research" in want:
        path = report_dir / "seed_rank.jsonl"
        for r in load_jsonl(path):
            ts = r.get("ts")
            try:
                tsf = float(ts)
            except (TypeError, ValueError):
                continue
            if not (t0u <= tsf < t1u):
                continue
            for sym in r.get("symbols") or []:
                s = str(sym).upper().strip()
                if not s or s in exclude or s in out:
                    continue
                out[s] = {
                    "symbol": s,
                    "source": str(r.get("source") or "research"),
                    "kind": "fallback",
                    "universe": "fallback_research",
                }
                if max_symbols is not None and len(out) >= int(max_symbols):
                    return list(out.values())

    if "momentum" in want:
        # Best-effort: current movers file is not day-keyed; only use when
        # its mtime falls on ``day`` (same ET calendar). Otherwise skip.
        for name in ("movers_stocks.json", "momentum_watch.json"):
            path = ROOT / name
            if not path.exists():
                path = report_dir / name
            if not path.exists():
                continue
            try:
                mday = datetime.fromtimestamp(path.stat().st_mtime, tz=ET).strftime(
                    "%Y-%m-%d"
                )
            except OSError:
                continue
            if mday != day:
                continue
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            rows = raw if isinstance(raw, list) else (
                raw.get("stocks") or raw.get("rows") or raw.get("symbols") or []
            )
            for item in rows:
                if isinstance(item, str):
                    s = item.upper().strip()
                elif isinstance(item, dict):
                    s = str(item.get("symbol") or item.get("ticker") or "").upper().strip()
                else:
                    continue
                if not s or s in exclude or s in out:
                    continue
                out[s] = {
                    "symbol": s,
                    "source": "momentum",
                    "kind": "fallback",
                    "universe": "fallback_momentum",
                }
                if max_symbols is not None and len(out) >= int(max_symbols):
                    return list(out.values())

    return list(out.values())


def list_ledger_days(ledger_dir: Path, days: int | None = None) -> list[str]:
    """ET day stems that have a ledger file, newest last; keep last N if set."""
    if not ledger_dir.exists():
        return []
    stems = sorted(p.stem for p in ledger_dir.glob("*.jsonl") if len(p.stem) == 10)
    if days is not None and days > 0:
        stems = stems[-int(days):]
    return stems


def parse_days_arg(raw: str | None, ledger_dir: Path, default_n: int = 15) -> list[str]:
    """``--days`` may be an int (last N ledger days) or comma list of YYYY-MM-DD."""
    if raw is None or str(raw).strip() == "":
        return list_ledger_days(ledger_dir, default_n)
    s = str(raw).strip()
    if s.isdigit():
        return list_ledger_days(ledger_dir, int(s))
    return [d.strip() for d in s.split(",") if d.strip()]


def summarize_pairs(pairs: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate (day, symbol) rows into Gate 1 rates + go/no-go/later.

    Admit-set metrics use ``universe == phase_b_admit`` only. Fallback rows are
    reported separately and do not drive the verdict.
    """
    admits = [p for p in pairs if p.get("universe", "phase_b_admit") == "phase_b_admit"]
    fallbacks = [p for p in pairs if str(p.get("universe") or "").startswith("fallback")]

    def _rates(rows: list[dict]) -> dict[str, Any]:
        n = len(rows)
        if n == 0:
            return {
                "n_pairs": 0,
                "sip_clear_n": 0,
                "iex_clear_n": 0,
                "sip_clear_rate": None,
                "iex_clear_rate": None,
                "median_sip_bars": None,
                "median_sip_bars_on_clear": None,
                "median_iex_bars": None,
                "square_minutes_sum": 0,
                "pre_square_minutes_sum": 0,
                "pairs_with_square": 0,
            }
        sip_clear = [r for r in rows if r.get("sip_clear_112")]
        iex_clear = [r for r in rows if r.get("iex_clear_112")]
        sip_bars = [int(r.get("sip_bars_0415_0920") or 0) for r in rows]
        iex_bars = [int(r.get("iex_bars_0415_0920") or 0) for r in rows]
        sip_on_clear = [int(r.get("sip_bars_0415_0920") or 0) for r in sip_clear]
        return {
            "n_pairs": n,
            "sip_clear_n": len(sip_clear),
            "iex_clear_n": len(iex_clear),
            "sip_clear_rate": len(sip_clear) / n,
            "iex_clear_rate": len(iex_clear) / n,
            "median_sip_bars": float(statistics.median(sip_bars)) if sip_bars else None,
            "median_sip_bars_on_clear": (
                float(statistics.median(sip_on_clear)) if sip_on_clear else None
            ),
            "median_iex_bars": float(statistics.median(iex_bars)) if iex_bars else None,
            "square_minutes_sum": sum(int(r.get("square_minutes") or 0) for r in rows),
            "pre_square_minutes_sum": sum(
                int(r.get("pre_square_minutes") or 0) for r in rows
            ),
            "pairs_with_square": sum(
                1 for r in rows if int(r.get("square_minutes") or 0) > 0
            ),
        }

    admit_m = _rates(admits)
    fb_m = _rates(fallbacks)
    n_days = len({p.get("day") for p in admits if p.get("day")})
    admit_m["n_days"] = n_days
    verdict = verdict_from_metrics(admit_m)
    return {
        "frozen_bars": {
            "go_sip_clear_min": GO_SIP_CLEAR_MIN,
            "go_median_bars_on_clear": GO_MEDIAN_BARS_ON_CLEAR,
            "go_iex_sip_ratio_max": GO_IEX_SIP_RATIO_MAX,
            "nogo_sip_clear_max": NOGO_SIP_CLEAR_MAX,
            "nogo_iex_near_sip_ratio": NOGO_IEX_NEAR_SIP_RATIO,
            "min_sessions": MIN_SESSIONS_FOR_VERDICT,
            "min_pairs": MIN_PAIRS_FOR_VERDICT,
            "slow_len": SLOW_LEN_DEFAULT,
            "window_et": "[04:00, 09:20)",
        },
        "admit": admit_m,
        "fallback": fb_m,
        "verdict": verdict,
        "n_days": n_days,
        "days": sorted({p.get("day") for p in admits if p.get("day")}),
    }


def verdict_from_metrics(m: dict[str, Any]) -> dict[str, Any]:
    """Written go / no-go / later / thin against frozen bars. Deterministic."""
    n = int(m.get("n_pairs") or 0)
    n_days = int(m.get("n_days") or 0)
    reasons: list[str] = []
    if n <= 0:
        return {
            "decision": "later",
            "label": "NO DECISION",
            "reasons": ["no_admit_pairs"],
        }
    if n_days < MIN_SESSIONS_FOR_VERDICT or n < MIN_PAIRS_FOR_VERDICT:
        return {
            "decision": "thin",
            "label": "THIN — NO DECISION",
            "reasons": [
                f"sample too small for a subscribe call: "
                f"n_days={n_days} (need ≥{MIN_SESSIONS_FOR_VERDICT}), "
                f"n_pairs={n} (need ≥{MIN_PAIRS_FOR_VERDICT})"
            ],
        }

    sip_rate = m.get("sip_clear_rate")
    iex_rate = m.get("iex_clear_rate")
    med_clear = m.get("median_sip_bars_on_clear")
    sip_rate_f = float(sip_rate) if sip_rate is not None else 0.0
    iex_rate_f = float(iex_rate) if iex_rate is not None else 0.0
    ratio = (iex_rate_f / sip_rate_f) if sip_rate_f > 0 else (
        1.0 if iex_rate_f > 0 else 0.0
    )

    if sip_rate_f < NOGO_SIP_CLEAR_MAX:
        reasons.append(
            f"sip_clear_rate={sip_rate_f:.1%} < {NOGO_SIP_CLEAR_MAX:.0%} "
            "(universe too thin; fix admits before paying)"
        )
        return {"decision": "no-go", "label": "NO-GO", "reasons": reasons}

    if sip_rate_f > 0 and ratio >= NOGO_IEX_NEAR_SIP_RATIO:
        reasons.append(
            f"iex/sip clear ratio={ratio:.2f} ≥ {NOGO_IEX_NEAR_SIP_RATIO:.2f} "
            "(SIP ≈ IEX; missing_print likely elsewhere — do not buy SIP yet)"
        )
        return {"decision": "later", "label": "LATER", "reasons": reasons}

    go_ok = True
    if sip_rate_f < GO_SIP_CLEAR_MIN:
        go_ok = False
        reasons.append(
            f"sip_clear_rate={sip_rate_f:.1%} < go bar {GO_SIP_CLEAR_MIN:.0%}"
        )
    if med_clear is None or float(med_clear) < GO_MEDIAN_BARS_ON_CLEAR:
        go_ok = False
        reasons.append(
            f"median_sip_bars_on_clear={med_clear} < go bar {GO_MEDIAN_BARS_ON_CLEAR}"
        )
    if ratio >= GO_IEX_SIP_RATIO_MAX:
        go_ok = False
        reasons.append(
            f"iex/sip clear ratio={ratio:.2f} not ≪ "
            f"(need < {GO_IEX_SIP_RATIO_MAX:.2f})"
        )

    if go_ok:
        reasons.append(
            f"sip_clear_rate={sip_rate_f:.1%} ≥ {GO_SIP_CLEAR_MIN:.0%}; "
            f"median_sip_bars_on_clear={med_clear} ≥ {GO_MEDIAN_BARS_ON_CLEAR}; "
            f"iex/sip={ratio:.2f} < {GO_IEX_SIP_RATIO_MAX:.2f}"
        )
        return {"decision": "go", "label": "GO", "reasons": reasons}

    if not reasons:
        reasons.append("between go and no-go bars")
    return {"decision": "later", "label": "LATER", "reasons": reasons}


def render_summary_md(summary: dict[str, Any]) -> str:
    v = summary.get("verdict") or {}
    a = summary.get("admit") or {}
    fb = summary.get("frozen_bars") or {}
    lines = [
        "# Phase B historical SIP square replay",
        "",
        f"**Verdict:** **{v.get('label', '?')}** (`{v.get('decision', '?')}`)",
        "",
        "## Reasons",
    ]
    for r in v.get("reasons") or []:
        lines.append(f"- {r}")
    lines += [
        "",
        "## Frozen bars",
        f"- Window ET: `{fb.get('window_et')}`",
        f"- Slow length: `{fb.get('slow_len')}`",
        f"- Go: SIP clear ≥ {fb.get('go_sip_clear_min'):.0%}, "
        f"median SIP bars on clear ≥ {fb.get('go_median_bars_on_clear')}, "
        f"IEX/SIP clear ratio < {fb.get('go_iex_sip_ratio_max')}",
        f"- No-go: SIP clear < {fb.get('nogo_sip_clear_max'):.0%} "
        f"or IEX/SIP ≥ {fb.get('nogo_iex_near_sip_ratio')} (≈)",
        f"- Sample floor: ≥{fb.get('min_sessions', MIN_SESSIONS_FOR_VERDICT)} sessions "
        f"and ≥{fb.get('min_pairs', MIN_PAIRS_FOR_VERDICT)} pairs (else THIN)",
        "",
        "## Admit set",
        f"- Days: {', '.join(summary.get('days') or []) or '(none)'}",
        f"- (day, symbol) pairs: **{a.get('n_pairs', 0)}**",
        f"- SIP clear-112: **{a.get('sip_clear_n', 0)}** / {a.get('n_pairs', 0)} "
        f"({_pct(a.get('sip_clear_rate'))})",
        f"- IEX clear-112: **{a.get('iex_clear_n', 0)}** / {a.get('n_pairs', 0)} "
        f"({_pct(a.get('iex_clear_rate'))})",
        f"- Median SIP bars (all): {_num(a.get('median_sip_bars'))}",
        f"- Median SIP bars (on clear): **{_num(a.get('median_sip_bars_on_clear'))}**",
        f"- Median IEX bars (all): {_num(a.get('median_iex_bars'))}",
        f"- Square minutes (sum): {a.get('square_minutes_sum', 0)} "
        f"across {a.get('pairs_with_square', 0)} pairs with square>0",
        f"- Pre-square minutes (sum): {a.get('pre_square_minutes_sum', 0)}",
        "",
        "_Computability study only — not expectancy / MFE. Do not subscribe from "
        "this PR._",
        "",
    ]
    fb_m = summary.get("fallback") or {}
    if int(fb_m.get("n_pairs") or 0) > 0:
        lines += [
            "## Universe fallback (not in verdict)",
            f"- Pairs: {fb_m.get('n_pairs')}",
            f"- SIP clear: {_pct(fb_m.get('sip_clear_rate'))}",
            f"- IEX clear: {_pct(fb_m.get('iex_clear_rate'))}",
            "",
        ]
    return "\n".join(lines)


def _pct(v) -> str:
    if v is None:
        return "n/a"
    return f"{100.0 * float(v):.1f}%"


def _num(v) -> str:
    if v is None:
        return "n/a"
    return f"{float(v):.1f}"


# ── Bar fetch (reuse Alpaca research helpers; no new client) ─────────────────

def _data_client():
    import bars
    return bars.client()


def fetch_premarket_ohlcv(
    symbol: str,
    day: str,
    feed: str,
    *,
    client=None,
) -> Any:
    """1‑min OHLCV [04:00, 09:30] ET for ``day`` on research feed ``sip``|``iex``.

    Delayed SIP is fine (``alpaca_api.research_bar_end``). Returns a DataFrame
    or None.
    """
    import pandas as pd
    import alpaca_api as aa
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    from alpaca.data.enums import DataFeed

    cl = client if client is not None else _data_client()
    if cl is None:
        return None

    feed_name = aa.normalize_research_feed(feed)
    start = _day_bound(day, WINDOW_START[0], WINDOW_START[1]).astimezone(UTC)
    end_req = _day_bound(day, FETCH_END[0], FETCH_END[1]).astimezone(UTC)
    end = aa.research_bar_end(feed_name, requested_end=end_req)
    if end <= start:
        return None

    try:
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame(1, TimeFrameUnit.Minute),
            start=start,
            end=end,
            limit=10000,
            extended_hours=True,
            feed=DataFeed.SIP if feed_name == "sip" else DataFeed.IEX,
        )
        df = cl.get_stock_bars(req).df
    except Exception:
        return None

    if df is None or df.empty:
        return None
    if isinstance(df.index, pd.MultiIndex):
        try:
            df = df.xs(symbol, level="symbol")
        except KeyError:
            return None
    df = df.sort_index()
    cols = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
    out = df[cols].copy()
    for c in ("open", "high", "low", "close"):
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")
    return out.dropna(subset=["close"])


def score_symbol_day(
    symbol: str,
    day: str,
    *,
    feed_sip: str = "sip",
    feed_iex: str = "iex",
    cfg: dict | None = None,
    client=None,
    sip_df=None,
    iex_df=None,
    universe: str = "phase_b_admit",
    source: str | None = None,
    kind: str | None = None,
) -> dict[str, Any]:
    """Fetch (or accept) SIP+IEX bars and produce one (day, symbol) metric row."""
    c = rte_cfg(cfg)
    slow_len = int(c.get("rte_slow_native_length", SLOW_LEN_DEFAULT) or SLOW_LEN_DEFAULT)

    if sip_df is None:
        sip_df = fetch_premarket_ohlcv(symbol, day, feed_sip, client=client)
    if iex_df is None:
        iex_df = fetch_premarket_ohlcv(symbol, day, feed_iex, client=client)

    sip_metrics = square_and_pre_square_minutes(sip_df, day, c)
    iex_n = 0
    if iex_df is not None and not getattr(iex_df, "empty", True):
        iex_n = count_bars_in_window(iex_df.index, day)

    return {
        "day": day,
        "symbol": symbol,
        "universe": universe,
        "source": source,
        "kind": kind,
        "sip_bars_0415_0920": int(sip_metrics["sip_bars_0415_0920"]),
        "iex_bars_0415_0920": int(iex_n),
        "sip_clear_112": bool(sip_metrics["clear_112"]),
        "iex_clear_112": clear_112(iex_n, slow_len),
        "square_minutes": int(sip_metrics["square_minutes"]),
        "pre_square_minutes": int(sip_metrics["pre_square_minutes"]),
        "slow_len": slow_len,
    }


def load_fixture_day_results(fixture_dir: Path) -> list[dict[str, Any]]:
    """Load precomputed per-day JSON (``YYYY-MM-DD.json`` with ``symbols`` list)."""
    pairs: list[dict[str, Any]] = []
    for path in sorted(fixture_dir.glob("*.json")):
        if path.name.startswith("summary"):
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        day = raw.get("day") or path.stem
        for row in raw.get("symbols") or raw.get("pairs") or []:
            if not isinstance(row, dict):
                continue
            rec = dict(row)
            rec.setdefault("day", day)
            rec.setdefault("universe", "phase_b_admit")
            pairs.append(rec)
    return pairs


def filter_pairs_by_prior_dollar_vol(
    pairs: list[dict[str, Any]],
    min_prior_dollar_vol: float,
    *,
    sleep_s: float = 0.05,
    fetch: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Keep pairs whose prior RTH-day close×volume ≥ floor (fail closed on None).

    Uses ``phase_b.prior_day_dollar_vol`` (delayed SIP daily bars) when
    ``fetch`` and the row lacks ``prior_dollar_vol``. Annotates kept rows.
    Returns (kept, dig_stats).
    """
    floor = float(min_prior_dollar_vol or 0.0)
    if floor <= 0:
        return list(pairs), {"min_prior_dollar_vol": 0, "n_in": len(pairs), "n_out": len(pairs)}

    pb = None
    if fetch:
        import phase_b as pb  # noqa: F811

    kept: list[dict[str, Any]] = []
    n_unknown = 0
    n_below = 0
    for row in pairs:
        if str(row.get("universe") or "phase_b_admit") != "phase_b_admit":
            kept.append(row)
            continue
        sym = str(row.get("symbol") or "").upper().strip()
        day = str(row.get("day") or "")
        dvol = row.get("prior_dollar_vol")
        if dvol is None and row.get("dollar_volume") is not None:
            dvol = row.get("dollar_volume")
        if dvol is None and fetch and pb is not None:
            dvol = pb.prior_day_dollar_vol(sym, day=day)
            if sleep_s > 0:
                time.sleep(sleep_s)
        try:
            dvol_f = float(dvol) if dvol is not None else None
        except (TypeError, ValueError):
            dvol_f = None
        if dvol_f is None:
            n_unknown += 1
            continue
        if dvol_f < floor:
            n_below += 1
            continue
        rec = dict(row)
        rec["prior_dollar_vol"] = round(dvol_f, 2)
        kept.append(rec)

    dig = {
        "min_prior_dollar_vol": floor,
        "n_in": len(pairs),
        "n_out": len(kept),
        "n_dropped_unknown": n_unknown,
        "n_dropped_below": n_below,
    }
    return kept, dig


def stamp_provenance(summary: dict[str, Any], *, out_dir: Path | None = None) -> dict[str, Any]:
    """Make a capped smoke indistinguishable from a real Gate 1 run."""
    import subprocess
    sha = "unknown"
    try:
        sha = (
            subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True
            ).strip()
            or "unknown"
        )
    except Exception:
        pass
    config_fp = None
    try:
        import learn_stamps as ls
        config_fp = ls.config_fingerprint()
    except Exception:
        config_fp = None
    liq = summary.get("liquidity_filter") or {}
    cap = None
    if isinstance(liq, dict) and liq.get("min_prior_dollar_vol") is not None:
        try:
            cap = float(liq["min_prior_dollar_vol"])
        except (TypeError, ValueError):
            cap = None
    summary["provenance"] = {
        "git_sha": sha,
        "generated_at": datetime.now(tz=ET).isoformat(),
        "config_fp": config_fp,
        "min_prior_dollar_vol_cap": cap,
        "n_days": summary.get("n_days"),
        "n_pairs": (summary.get("admit") or {}).get("n_pairs"),
        "out_dir": str(out_dir) if out_dir else None,
        "verdict_decision": (summary.get("verdict") or {}).get("decision"),
    }
    return summary


def write_outputs(
    out_dir: Path,
    by_day: dict[str, list[dict]],
    summary: dict[str, Any],
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for day, rows in sorted(by_day.items()):
        payload = {
            "day": day,
            "n_symbols": len(rows),
            "symbols": rows,
        }
        (out_dir / f"{day}.json").write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8",
        )
    stamp_provenance(summary, out_dir=out_dir)
    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8",
    )
    (out_dir / "summary.md").write_text(
        render_summary_md(summary), encoding="utf-8",
    )


def run_replay(
    *,
    ledger_dir: Path,
    days: list[str],
    out_dir: Path,
    feed_sip: str = "sip",
    feed_iex: str = "iex",
    max_symbols_per_day: int | None = None,
    universe_fallback: list[str] | None = None,
    report_dir: Path | None = None,
    cfg: dict | None = None,
    client=None,
    sleep_s: float = 0.15,
    min_prior_dollar_vol: float = 0.0,
) -> dict[str, Any]:
    report_dir = report_dir or (ROOT / "ai_reports")
    c = rte_cfg(cfg)
    all_pairs: list[dict] = []
    by_day: dict[str, list[dict]] = {}

    for day in days:
        ledger_path = ledger_dir / f"{day}.jsonl"
        rows = load_jsonl(ledger_path) if ledger_path.exists() else []
        admits = collect_admits_from_rows(rows, max_symbols=max_symbols_per_day)
        symbols = list(admits)
        if universe_fallback and (
            not admits or (max_symbols_per_day and len(admits) < max_symbols_per_day)
        ):
            room = None
            if max_symbols_per_day is not None:
                room = max(0, int(max_symbols_per_day) - len(admits))
            if room is None or room > 0:
                fb = collect_fallback_symbols(
                    day,
                    universe_fallback,
                    report_dir,
                    exclude={a["symbol"] for a in admits},
                    max_symbols=room,
                )
                symbols.extend(fb)

        day_rows: list[dict] = []
        for meta in symbols:
            rec = score_symbol_day(
                meta["symbol"],
                day,
                feed_sip=feed_sip,
                feed_iex=feed_iex,
                cfg=c,
                client=client,
                universe=meta.get("universe") or "phase_b_admit",
                source=meta.get("source"),
                kind=meta.get("kind"),
            )
            day_rows.append(rec)
            all_pairs.append(rec)
            if sleep_s > 0:
                time.sleep(sleep_s)
        by_day[day] = day_rows
        print(
            f"[sip-replay] {day}: {len(day_rows)} symbols "
            f"(admits={sum(1 for r in day_rows if r.get('universe') == 'phase_b_admit')})",
            flush=True,
        )

    dig = None
    if float(min_prior_dollar_vol or 0.0) > 0:
        all_pairs, dig = filter_pairs_by_prior_dollar_vol(
            all_pairs, float(min_prior_dollar_vol), sleep_s=sleep_s, fetch=True,
        )
        by_day = {}
        for p in all_pairs:
            by_day.setdefault(str(p.get("day")), []).append(p)

    summary = summarize_pairs(all_pairs)
    summary["feeds"] = {"sip": feed_sip, "iex": feed_iex}
    summary["admit_kinds"] = sorted(ADMIT_KINDS)
    if dig:
        summary["liquidity_filter"] = dig
    write_outputs(out_dir, by_day, summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Phase B historical SIP vs IEX clear-112 / square replay (Gate 1)",
    )
    ap.add_argument(
        "--ledger-dir",
        default=str(ROOT / "ai_reports" / "phase_b_ledger"),
        help="Directory of YYYY-MM-DD.jsonl Phase B ledgers",
    )
    ap.add_argument(
        "--days",
        default="15",
        help="Last N ledger days (int) or comma-separated YYYY-MM-DD list",
    )
    ap.add_argument("--feed-sip", default="sip", choices=("sip", "iex"))
    ap.add_argument("--feed-iex", default="iex", choices=("sip", "iex"))
    ap.add_argument(
        "--out",
        default=str(ROOT / "ai_reports" / "phase_b_sip_replay"),
        help="Output directory for per-day JSON + summary.md/json",
    )
    ap.add_argument(
        "--max-symbols-per-day",
        type=int,
        default=None,
        help="Optional API budget cap per day",
    )
    ap.add_argument(
        "--universe-fallback",
        default="",
        help="Comma list e.g. momentum,research when ledger admits are thin",
    )
    ap.add_argument(
        "--fixture-dir",
        default="",
        help="Skip Alpaca: load precomputed day JSONs and write summary only",
    )
    ap.add_argument(
        "--rescore-dir",
        default="",
        help="Re-filter an existing phase_b_sip_replay day-JSON dir (WP1 counterfactual)",
    )
    ap.add_argument(
        "--min-prior-dollar-vol",
        type=float,
        default=0.0,
        help="Liquidity floor for admit pairs (prior RTH-day close×volume). "
             "0=off (shipped default until out-of-sample Gate 1). 2e6 was an in-sample fit on 2026-09-17/18 only — do not treat as GO.",
    )
    ap.add_argument(
        "--report-dir",
        default=str(ROOT / "ai_reports"),
        help="ai_reports root for seed_rank / movers fallback",
    )
    ap.add_argument("--sleep", type=float, default=0.15, help="Pause between symbol fetches")
    args = ap.parse_args(argv)

    out_dir = Path(args.out)
    min_dvol = float(args.min_prior_dollar_vol or 0.0)

    if args.fixture_dir or args.rescore_dir:
        src = Path(args.rescore_dir or args.fixture_dir)
        pairs = load_fixture_day_results(src)
        dig = None
        if min_dvol > 0:
            # fixture-dir: annotated vols only (no network). rescore-dir: fetch.
            pairs, dig = filter_pairs_by_prior_dollar_vol(
                pairs,
                min_dvol,
                sleep_s=0.0 if args.fixture_dir else float(args.sleep),
                fetch=bool(args.rescore_dir),
            )
        by_day: dict[str, list[dict]] = {}
        for p in pairs:
            by_day.setdefault(str(p.get("day")), []).append(p)
        summary = summarize_pairs(pairs)
        summary["feeds"] = {"sip": args.feed_sip, "iex": args.feed_iex}
        summary["admit_kinds"] = sorted(ADMIT_KINDS)
        if args.fixture_dir:
            summary["fixture_dir"] = str(args.fixture_dir)
        if args.rescore_dir:
            summary["rescore_dir"] = str(args.rescore_dir)
        if dig:
            summary["liquidity_filter"] = dig
        write_outputs(out_dir, by_day, summary)
        print(render_summary_md(summary))
        if dig:
            print(
                f"[liq] in={dig['n_in']} out={dig['n_out']} "
                f"below={dig['n_dropped_below']} unknown={dig['n_dropped_unknown']}",
                flush=True,
            )
        decision = (summary.get("verdict") or {}).get("decision")
        return 0 if decision == "go" else 1

    ledger_dir = Path(args.ledger_dir)
    days = parse_days_arg(args.days, ledger_dir)
    if not days:
        print(f"No ledger days under {ledger_dir}", file=sys.stderr)
        return 2

    fb = [x.strip() for x in str(args.universe_fallback).split(",") if x.strip()]
    try:
        from config import load_config
        cfg = load_config() or {}
    except Exception:
        cfg = {}

    summary = run_replay(
        ledger_dir=ledger_dir,
        days=days,
        out_dir=out_dir,
        feed_sip=args.feed_sip,
        feed_iex=args.feed_iex,
        max_symbols_per_day=args.max_symbols_per_day,
        universe_fallback=fb or None,
        report_dir=Path(args.report_dir),
        cfg=cfg,
        sleep_s=float(args.sleep),
        min_prior_dollar_vol=min_dvol,
    )
    print(render_summary_md(summary))
    print(f"Wrote {out_dir / 'summary.md'}", flush=True)
    decision = (summary.get("verdict") or {}).get("decision")
    return 0 if decision == "go" else 1


if __name__ == "__main__":
    raise SystemExit(main())
