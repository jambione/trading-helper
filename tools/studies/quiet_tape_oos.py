#!/usr/bin/env python3
"""Weekend item 1: held-out re-test of the 9/30 quiet-tape rule. Rules fixed in docs/studies/quiet_tape_oos_prereg.json. Read-only; uses the
bar cache of tools/studies/sip_features.py fetch (SIP and IEX 1-minute bars 04:00-16:00).   usage: quiet_tape_oos.py [report]"""
from __future__ import annotations

import math
import pickle
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))
ET = ZoneInfo("America/New_York")
CACHE = ROOT / "ai_reports" / "sip_features"
SINCE, UNTIL = "2026-10-01", "2026-11-05"
CUT, MIN_SESSION_MIN, MIN_QUIET, T_BAR, MIN_PAYOFF = 0.25, 10, 30, -1.65, 1.0


def ratio(bars, fill_ts):
    """Last-5-closed-minutes volume per minute over the session's per-minute pace (09:30 to the last closed bar). None if not computable. Pure."""
    d = datetime.fromtimestamp(fill_ts, ET)
    open_ts = d.replace(hour=9, minute=30, second=0, microsecond=0).timestamp()
    cut = fill_ts - (fill_ts % 60)
    sess = [b for b in bars if open_ts <= b[0] < cut]
    if not sess:
        return None
    minutes = (cut - open_ts) / 60.0
    if minutes < MIN_SESSION_MIN:
        return None
    pace = sum(b[5] for b in sess) / minutes                       # missing minutes count as zero volume
    last5 = sum(b[5] for b in sess if b[0] >= cut - 300) / 5.0
    return None if pace <= 0 else last5 / pace


def tstat(v):
    n = len(v)
    if n < 3:
        return float("nan")
    m = sum(v) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in v) / (n - 1))
    return m / (sd / math.sqrt(n)) if sd else float("nan")


def evaluate(rows, cut=CUT):
    """rows: [{d, bp, ng, ratio}]. Returns dict with the prereg's four conditions. Pure."""
    q = [r for r in rows if r["ratio"] is not None and r["ratio"] < cut]
    k = [r for r in rows if r["ratio"] is not None and r["ratio"] >= cut]
    days = sorted({r["d"] for r in rows if r["ratio"] is not None})
    diffs = []
    for d in days:
        a = [r["bp"] for r in q if r["d"] == d]
        b = [r["bp"] for r in k if r["d"] == d]
        if a and b:
            diffs.append((d, sum(a) / len(a) - sum(b) / len(b)))
    mid = days[len(days) // 2] if days else ""
    h1 = [x for d, x in diffs if d < mid]
    h2 = [x for d, x in diffs if d >= mid]
    allm = sum(r["bp"] for r in q + k) / max(1, len(q + k))
    keptm = sum(r["bp"] for r in k) / max(1, len(k))
    t = tstat([x for _, x in diffs])
    mean = sum(x for _, x in diffs) / max(1, len(diffs))
    ok = (mean < 0 and t <= T_BAR and h1 and h2 and sum(h1) / len(h1) < 0 and sum(h2) / len(h2) < 0 and len(q) >= MIN_QUIET and keptm - allm >= MIN_PAYOFF)
    return {"n_quiet": len(q), "n_rest": len(k), "share": len(q) / max(1, len(q) + len(k)), "days": len(diffs), "mean_diff": mean, "t": t,
            "h1": (sum(h1) / len(h1)) if h1 else None, "h2": (sum(h2) / len(h2)) if h2 else None, "kept_minus_all": keptm - allm,
            "ng_quiet": sum(r["ng"] for r in q) / max(1, len(q)), "ng_rest": sum(r["ng"] for r in k) / max(1, len(k)), "pass": bool(ok)}


def rows_for(feed):
    from early_path import trades_since
    tr, _ = trades_since(SINCE)
    cache, rows = {}, []
    for t in tr:
        if t["day"] > UNTIL:
            continue
        key = (t["day"], feed)
        if key not in cache:
            f = CACHE / f"{t['day']}_{feed}.pkl"
            cache[key] = pickle.load(open(f, "rb")) if f.exists() else None
        c = cache[key]
        if c is None:
            continue
        rows.append({"d": t["day"], "bp": t["net_bp"] - 3.0, "ng": t["never_green"], "ratio": ratio(c.get(t["sym"], []), t["fill_ts"])})
    return rows


def report():
    out = [f"# Quiet-tape held-out re-test (prereg docs/studies/quiet_tape_oos_prereg.json), written {datetime.now(ET):%Y-%m-%d %H:%M} ET", ""]
    for feed in ("sip", "iex"):
        rows = rows_for(feed)
        if not rows:
            out.append(f"{feed.upper()}: no cached bars (run tools/studies/sip_features.py fetch)")
            continue
        ev = evaluate(rows)
        tag = "PRIMARY" if feed == "sip" else "information only"
        out += [f"## {feed.upper()} bars ({tag})", "",
                f"fills with a ratio: {ev['n_quiet'] + ev['n_rest']} over {ev['days']} days with both groups; QUIET (< {CUT}) {ev['n_quiet']} ({100 * ev['share']:.0f}%), rest {ev['n_rest']}",
                f"mean day difference (quiet minus rest) {ev['mean_diff']:+.1f} bp, t {ev['t']:+.2f} (bar <= {T_BAR}); halves {ev['h1'] if ev['h1'] is None else round(ev['h1'], 1)} / {ev['h2'] if ev['h2'] is None else round(ev['h2'], 1)}",
                f"never-green: quiet {100 * ev['ng_quiet']:.0f}% vs rest {100 * ev['ng_rest']:.0f}%; payoff (non-quiet mean minus all) {ev['kept_minus_all']:+.2f} bp (bar >= +{MIN_PAYOFF})",
                f"**VERDICT ({feed.upper()}): {'PASS (skeptic review next; live use needs real-time SIP)' if ev['pass'] else 'FAIL / not shown'}**", ""]
        if feed == "sip":
            out.append("Information: other cutoffs, never scored as primary:")
            for c in (0.15, 0.35, 0.5):
                e = evaluate(rows, c)
                out.append(f"- cutoff {c}: quiet n {e['n_quiet']} ({100 * e['share']:.0f}%), day diff {e['mean_diff']:+.1f} bp, t {e['t']:+.2f}")
            out.append("")
    (ROOT / "docs" / "QUIET_TAPE_OOS.md").write_text("\n".join(out) + "\n")
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(report())
