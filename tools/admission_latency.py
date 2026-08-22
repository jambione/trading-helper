#!/usr/bin/env python3
"""How much of the move is already over when a name reaches the watchlist?

tools/drift_screen.py measured the watched universe as a driftless walk from
``admit_ts`` forward, while the same names screened as strong drift when the
sampling was allowed to start earlier in the session. Both readings are
right, and the gap between them is this: the run happens before admission.

A gate filters what it is shown. If admission arrives after the move, no
gate recovers it — that is a latency problem, not a signal problem, and it
has to be measured before an entry-gate search is worth running.

Per admitted name-day this reports:

    pct_at_admit    where the name stood vs the prior close when admitted
    before          prior close -> admit price, the run already banked
    after           admit price -> the best print remaining that session
    captured        before / (before + after), the share of the day's
                    up-move that happened before the desk could act
    latency_min     first minute the name cleared ai_watch_min_pct_change
                    (ai_watch_trending_min_pct_change for trending)
                    until admit_ts — how long the threshold itself costs

``captured`` is the number that matters. At 0.50 the desk arrives halfway
up. Near 1.00 it is buying the top of a move that is already finished, and
the admission threshold — not the entry rule — is what needs changing.

Read-only. Usage (mini, venv):

    .venv/bin/python tools/admission_latency.py
    .venv/bin/python tools/admission_latency.py --days 20 --source momentum
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

SCREEN_DIR = Path(ROOT) / "ai_reports" / "screens"
ET_OFFSET_H = 4  # August is EDT; matches drift_screen's RTH window


def _cfg() -> dict:
    try:
        from config import load_config
        return load_config()
    except Exception:
        try:
            return json.loads(
                (Path(ROOT) / "config" / "bot_config.json").read_text("utf-8"))
        except Exception:
            return {}


def fetch_daily(symbols: list[str], start: str, end: str) -> dict[str, list[dict]]:
    import h4_screen
    return h4_screen._fetch_daily(symbols, start, end)


def prior_close(daily: list[dict], day: str) -> float | None:
    """Close of the last session strictly before *day*."""
    prev = None
    for row in daily:
        if str(row.get("date")) >= day:
            break
        prev = row
    if not prev:
        return None
    try:
        c = float(prev.get("close") or 0)
    except (TypeError, ValueError):
        return None
    return c if c > 0 else None


def threshold_for(source: str, cfg: dict) -> float:
    if source == "trending":
        return float(cfg.get("ai_watch_trending_min_pct_change", 15.0) or 15.0)
    return float(cfg.get("ai_watch_min_pct_change", 50.0) or 50.0)


def analyse_day(bars: list[dict], day: str, admit_ts: float,
                pclose: float, threshold: float) -> dict | None:
    """One admitted name-day. None when the tape cannot support the question."""
    path = [b for b in bars if b["day"] == day and DS._in_rth(b)]
    if len(path) < 5 or pclose <= 0:
        return None
    at = [b for b in path if b["t"] >= admit_ts]
    if not at:
        return None
    admit_px = at[0]["o"]
    if admit_px <= 0:
        return None

    before = 100.0 * (admit_px - pclose) / pclose
    best_after = max(b["h"] for b in at)
    after = 100.0 * (best_after - admit_px) / admit_px

    # Share of the day's up-move already spent. Computed in price space:
    # ``before`` is a percentage of the prior close and ``after`` one of the
    # admit price, so summing the two percentages mixes denominators and
    # flatters the desk. Only meaningful when the name actually rose into
    # admission; a name admitted below the prior close missed no run.
    captured = None
    if before > 0:
        span = max(best_after, admit_px) - pclose
        captured = ((admit_px - pclose) / span) if span > 0 else None

    # When did the name first clear the admission threshold?
    cross_t = None
    for b in path:
        if 100.0 * (b["h"] - pclose) / pclose >= threshold:
            cross_t = b["t"]
            break
    latency = ((admit_ts - cross_t) / 60.0) if cross_t else None

    admit_et = datetime.fromtimestamp(admit_ts, timezone.utc) - timedelta(hours=ET_OFFSET_H)
    return {
        "day": day,
        "pct_at_admit": before,
        "before": before,
        "after": after,
        "captured": captured,
        "latency_min": latency,
        "admit_et": admit_et.strftime("%H:%M"),
    }


def _pct(vals: list[float], q: float) -> float:
    s = sorted(vals)
    return s[min(int(q * len(s)), len(s) - 1)]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--days", type=int, default=20)
    ap.add_argument("--source", default="all",
                    help="all|momentum|trending|research")
    ap.add_argument("--limit-symbols", type=int, default=150)
    args = ap.parse_args()

    cfg = _cfg()
    sources = (["momentum", "trending", "research"]
               if args.source == "all" else [args.source])
    plans = {s: DS.load_shadow_universe(args.days, s) for s in sources}
    syms = sorted({sym for p in plans.values() for d in p.values() for sym in d})
    syms = syms[:args.limit_symbols]
    if not syms:
        print("no admitted names in shadow.jsonl for that window")
        return 0

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days + 10)
    print(f"admission latency  sources={sources}  symbols={len(syms)}  "
          f"window={start.date()}..{end.date()}")
    for s in sources:
        print(f"  threshold {s}: +{threshold_for(s, cfg):g}% vs prior close")

    minutes = DS.fetch_minutes(syms, start, end)
    if not minutes:
        print("  no Alpaca data client — run on the mini with .venv.")
        return 0
    daily = fetch_daily(syms, start.date().isoformat(), end.date().isoformat())
    print(f"  minute bars {len(minutes)}/{len(syms)}, daily {len(daily)}/{len(syms)}\n")

    rows_by_source: dict[str, list[dict]] = defaultdict(list)
    for src, plan in plans.items():
        thr = threshold_for(src, cfg)
        for day, members in plan.items():
            for sym, admit in members.items():
                b, d = minutes.get(sym), daily.get(sym)
                if not b or not d:
                    continue
                pc = prior_close(d, day)
                if not pc:
                    continue
                r = analyse_day(b, day, admit, pc, thr)
                if r:
                    r["symbol"] = sym
                    rows_by_source[src].append(r)

    hdr = (f"{'source':<12}{'n':>5}{'medAdmit':>10}{'pct@admit':>11}"
           f"{'before':>9}{'after':>9}{'captured':>10}{'cap p25':>9}"
           f"{'cap p75':>9}{'latency':>9}")
    print(hdr)
    print("-" * len(hdr))
    payload = {}
    for src in sources:
        rows = rows_by_source.get(src) or []
        if not rows:
            print(f"{src:<12}{0:>5}   (nothing resolved)")
            continue
        cap = [r["captured"] for r in rows if r["captured"] is not None]
        lat = [r["latency_min"] for r in rows if r["latency_min"] is not None]
        times = sorted(r["admit_et"] for r in rows)
        summary = {
            "n": len(rows),
            "median_admit_et": times[len(times) // 2],
            "median_pct_at_admit": statistics.median(r["pct_at_admit"] for r in rows),
            "median_before": statistics.median(r["before"] for r in rows),
            "median_after": statistics.median(r["after"] for r in rows),
            "median_captured": statistics.median(cap) if cap else None,
            "captured_p25": _pct(cap, 0.25) if cap else None,
            "captured_p75": _pct(cap, 0.75) if cap else None,
            "median_latency_min": statistics.median(lat) if lat else None,
            "n_with_runup": len(cap),
        }
        payload[src] = summary
        print(f"{src:<12}{summary['n']:>5}{summary['median_admit_et']:>10}"
              f"{summary['median_pct_at_admit']:>10.1f}%"
              f"{summary['median_before']:>8.1f}%{summary['median_after']:>8.1f}%"
              f"{(summary['median_captured'] or 0):>10.2f}"
              f"{(summary['captured_p25'] or 0):>9.2f}"
              f"{(summary['captured_p75'] or 0):>9.2f}"
              + (f"{summary['median_latency_min']:>8.0f}m"
                 if summary['median_latency_min'] is not None else f"{'n/a':>9}"))

    print("\ncaptured = share of the day's up-move banked BEFORE the desk could act.")
    print("1.00 means the whole move happened before admission; 0.50 is arriving halfway.")
    SCREEN_DIR.mkdir(parents=True, exist_ok=True)
    day = datetime.now().strftime("%Y-%m-%d")
    outp = SCREEN_DIR / f"admission_latency_{day}.json"
    outp.write_text(json.dumps({"day": day, "days": args.days,
                                "results": payload}, indent=2, default=str),
                    encoding="utf-8")
    print(f"wrote {outp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
