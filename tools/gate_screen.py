#!/usr/bin/env python3
"""Grade a candidate entry gate by the drift it admits, not by its P&L.

tools/drift_screen.py asks whether a *universe* drifts. This asks the next
question: does any subset of it drift — the subset some gate selects?

The anchor matters. drift_screen samples arbitrary minutes, which measures
the field. A gate does not fire at arbitrary minutes, so here every sample
is anchored at a moment the gate actually fired, read from shadow.jsonl
(one row per watch poll, carrying the features the desk saw at that
instant). MFE/MAE over the horizon from that instant is then the gate's
own drift, and the same desk_null gates decide the verdict.

This is deliberately backwards from how the desk was tuned. The old sweeps
scored gates by realized P&L, which folds the entry, the 0.10R shelf, the
spread and the slot logic into one number and cannot say which of them
failed. Drift at the fire instant isolates the gate.

``arm_ok`` is included as a gate on purpose: it is what the desk is running
today, so it is the number every candidate has to beat.

Independence follows the rest of the lab: no two accepted samples on one
symbol-day may overlap, the unit is the session, and n never carries a
verdict on its own. No cost is charged — a gate that clears here has earned
a cost test, not an arm.

Read-only. Usage (mini, venv):

    .venv/bin/python tools/gate_screen.py --horizons 15,30,60
    .venv/bin/python tools/gate_screen.py --gates arm_ok,rvol_5,fresh_5m
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import drift_screen as DS  # noqa: E402

SCREEN_DIR = Path(ROOT) / "ai_reports" / "screens"


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


_FLOAT_MEMO: dict[str, float | None] = {}


def _float_m(r: dict) -> float | None:
    """Float in millions for a shadow row's symbol, or None when unreadable.

    Memoised: a gate runs over every watch poll — hundreds of thousands of
    rows — against a few hundred distinct symbols, and load_cache() re-reads
    from disk. None means the cache never had it, which is a different fact
    from a large float and must stay distinguishable here.
    """
    sym = str(r.get("symbol") or "").upper()
    if not sym:
        return None
    if sym not in _FLOAT_MEMO:
        try:
            import float_feed
            _FLOAT_MEMO[sym] = float_feed.float_shares(sym)
        except Exception:  # noqa: BLE001
            _FLOAT_MEMO[sym] = None
    return _FLOAT_MEMO[sym]


def _mins_since_admit(r: dict) -> float | None:
    ts, admit = _f(r.get("ts")), _f(r.get("admit_ts"))
    if ts is None or admit is None or admit <= 0:
        return None
    return (ts - admit) / 60.0


# Candidate gates. Each takes a shadow row and returns True when it fires.
# Keep these cheap and readable — a gate nobody can state in one line is a
# gate nobody can defend after a losing week.
GATES: dict[str, callable] = {
    "all":            lambda r: True,
    "arm_ok":         lambda r: bool(r.get("arm_ok")),
    "cm_rsi_rising":  lambda r: bool(r.get("cm_rsi_rising")),
    "cm_ok":          lambda r: bool(r.get("cm_ok")),
    "pctr_ok":        lambda r: bool(r.get("pctr_ok")),
    "in_zone":        lambda r: bool(r.get("in_zone")),
    "not_sell_sig":   lambda r: not r.get("sell_signal"),
    "rvol_3":         lambda r: (_f(r.get("rvol")) or 0) >= 3.0,
    "rvol_5":         lambda r: (_f(r.get("rvol")) or 0) >= 5.0,
    "rvol_10":        lambda r: (_f(r.get("rvol")) or 0) >= 10.0,
    # Freshness — the lever tools/admission_latency.py points at. If drift
    # survives anywhere it should be right after the name lands.
    "fresh_5m":       lambda r: (_mins_since_admit(r) or 1e9) <= 5.0,
    "fresh_15m":      lambda r: (_mins_since_admit(r) or 1e9) <= 15.0,
    "stale_60m":      lambda r: (_mins_since_admit(r) or -1) >= 60.0,
    # The desk's own arm decision, restricted to fresh names.
    "arm_ok_fresh":   lambda r: (bool(r.get("arm_ok"))
                                 and (_mins_since_admit(r) or 1e9) <= 15.0),
    # Float ceilings. ai_watch_max_float_m cannot be graded by the fill
    # replay — that tool pins fills, so an admission filter is invisible to
    # it — and grading it on the realised record instead confounds the names
    # a ceiling SELECTS with how well the desk happens to exit them. Anchored
    # here at every watch poll, this is the drift the ceiling admits, before
    # execution touches it.
    #
    # The _live variants carry the shipped fail-open: an unreadable float is
    # admitted, because the lookup is a cached profile call and an outage
    # must not empty the book. Kept beside the strict ones on purpose — the
    # gap between a pair is the cost of cache coverage, which on 2026-08-28
    # was 92% of all admissions before a backfill.
    "float_10":       lambda r: (_float_m(r) or 1e9) <= 10.0,
    "float_25":       lambda r: (_float_m(r) or 1e9) <= 25.0,
    "float_50":       lambda r: (_float_m(r) or 1e9) <= 50.0,
    "float_50_live":  lambda r: (_float_m(r) is None
                                 or _float_m(r) <= 50.0),
    "float_over_50":  lambda r: (_float_m(r) or -1) > 50.0,
    # RSI as an ENTRY condition, measured against the desk's own arm verdict
    # rather than in isolation — the question is never "does RSI predict", it
    # is "does adding RSI to what we already do admit better drift".
    #
    # Anchored on arm_ok because the current gate cannot be replayed from
    # shadow: macd_gap, macd_sep_ratio, macd_bull and macd_gap_rising are
    # recorded on ZERO rows, and MACD is the primary lever in both the
    # override and the standard path. arm_ok IS that gate's verdict.
    #
    # The bands come from where the desk actually arms, not from textbook
    # RSI: median CM RSI-2 at an armed moment is 90.7 and 83% are over 70. A
    # 0-50 "oversold" rule is not a filter here, it is a different strategy —
    # it removes 52% of episodes.
    "arm_rsi_rising": lambda r: (bool(r.get("arm_ok"))
                                 and bool(r.get("cm_rsi_rising"))),
    "arm_rsi_le70":   lambda r: (bool(r.get("arm_ok"))
                                 and (_f(r.get("cm_rsi")) or 0) <= 70),
    "arm_rsi_lt80":   lambda r: (bool(r.get("arm_ok"))
                                 and (_f(r.get("cm_rsi")) or 0) < 80),
    "arm_rsi_band":   lambda r: (bool(r.get("arm_ok"))
                                 and 0 <= (_f(r.get("cm_rsi")) or -1) <= 50),
    # The complement: what each rule would THROW AWAY. If the discards drift
    # as well as the keeps, the rule is only costing entries.
    "arm_rsi_cut70":  lambda r: (bool(r.get("arm_ok"))
                                 and (_f(r.get("cm_rsi")) or 0) > 70),
    "arm_rsi_notrise": lambda r: (bool(r.get("arm_ok"))
                                  and not r.get("cm_rsi_rising")),
}


def load_shadow_rows(days: int, source: str | None) -> list[dict]:
    path = Path(DS.resolve_report_dir()) / "shadow.jsonl"
    if not path.exists():
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days + 5)).timestamp()
    out = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            ts = _f(r.get("ts"))
            if ts is None or ts < cutoff:
                continue
            if not (r.get("symbol") or r.get("ticker")):
                continue
            if source and source != "all":
                src = str(r.get("source") or "").lower()
                if source == "research":
                    if src not in DS.RESEARCH_SOURCES:
                        continue
                elif src != source:
                    continue
            r["_sym"] = str(r.get("symbol") or r.get("ticker")).upper()
            r["_day"] = DS._day_of(ts)
            r["_ts"] = ts
            out.append(r)
    return out


def excursion_from(bars: list[dict], day: str, at_ts: float,
                   horizon: int) -> dict | None:
    """MFE / MAE / net over *horizon* minutes from the first bar at/after at_ts."""
    path = [b for b in bars if b["day"] == day and b["t"] >= at_ts
            and DS._in_rth(b)]
    if len(path) < horizon:
        return None
    w = path[:horizon]
    entry = w[0]["o"]
    if entry <= 0:
        return None
    return {
        "day": day,
        "mfe": 100.0 * (max(b["h"] for b in w) - entry) / entry,
        "mae": 100.0 * (entry - min(b["l"] for b in w)) / entry,
        "net": 100.0 * (w[-1]["c"] - entry) / entry,
    }


def samples_for(gate, rows: list[dict], bars: dict[str, list[dict]],
                horizon: int) -> list[dict]:
    """Non-overlapping excursions anchored where *gate* fired."""
    by_key: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        if gate(r):
            by_key[(r["_sym"], r["_day"])].append(r)
    out = []
    span = horizon * 60.0
    for (sym, day), hits in by_key.items():
        b = bars.get(sym)
        if not b:
            continue
        last_t = None
        for r in sorted(hits, key=lambda x: x["_ts"]):
            if last_t is not None and r["_ts"] - last_t < span:
                continue  # windows must not overlap
            e = excursion_from(b, day, r["_ts"], horizon)
            if e:
                out.append(e)
                last_t = r["_ts"]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--days", type=int, default=20)
    ap.add_argument("--source", default="all")
    ap.add_argument("--horizons", default="15,30,60")
    ap.add_argument("--gates", default=",".join(GATES))
    ap.add_argument("--limit-symbols", type=int, default=400,
                    help="0 = no cap; seeded random sample over the cap")
    args = ap.parse_args()

    horizons = [int(x) for x in args.horizons.split(",") if x.strip()]
    names = [g.strip() for g in args.gates.split(",") if g.strip() in GATES]
    if not names:
        print("no known gates requested")
        return 0

    rows = load_shadow_rows(args.days, args.source)
    if not rows:
        print("no shadow rows in that window")
        return 0
    syms = DS.select_symbols([r["_sym"] for r in rows], args.limit_symbols)
    rows = [r for r in rows if r["_sym"] in set(syms)]
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days + 5)
    print(f"gate screen  source={args.source}  shadow rows={len(rows)}  "
          f"symbols={len(syms)}  horizons={horizons}min")
    bars = DS.fetch_minutes(syms, start, end)
    if not bars:
        print("  no Alpaca data client — run on the mini with .venv.")
        return 0
    print(f"  bars {len(bars)}/{len(syms)}\n")

    hdr = (f"{'gate':<16}{'horiz':>6}{'n':>7}{'sess':>6}{'medMFE':>9}"
           f"{'medMAE':>9}{'MFE/MAE':>9}{'MFE-MAE':>9}{'sigma':>7}"
           f"{'medNet':>9}{'green':>7}{'verdict':>13}")
    print(hdr)
    print("-" * len(hdr))
    payload = {}
    for hz in horizons:
        for name in names:
            s = DS.score(samples_for(GATES[name], rows, bars, hz))
            payload[f"{name}@{hz}m"] = s
            if s["verdict"] == "EMPTY":
                print(f"{name:<16}{hz:>6}{0:>7}   (no samples)")
                continue
            print(f"{name:<16}{hz:>6}{s['n']:>7}{s['sessions']:>6}"
                  f"{s['median_mfe']:>9.3f}{s['median_mae']:>9.3f}"
                  f"{(s['mfe_over_mae'] or 0):>9.2f}"
                  f"{s['mean_mfe_minus_mae']:>9.3f}{s['sigma']:>7.2f}"
                  f"{s['median_net']:>9.3f}"
                  f"{s['sessions_green']}/{s['sessions']:<5}{s['verdict']:>13}")
        print()

    print("null: MFE/MAE = 1.00. A gate must beat 'all' AND clear the gates,")
    print("not merely look better than the desk's current arm_ok.")
    SCREEN_DIR.mkdir(parents=True, exist_ok=True)
    day = datetime.now().strftime("%Y-%m-%d")
    outp = SCREEN_DIR / f"gate_{day}.json"
    outp.write_text(json.dumps({"day": day, "source": args.source,
                                "horizons": horizons, "results": payload},
                               indent=2, default=str), encoding="utf-8")
    print(f"wrote {outp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
