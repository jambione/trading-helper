#!/usr/bin/env python3
"""book_daily_report - the 16:10 ET daily read of the two pillars (operator 11/19). READ-ONLY.

(1) Names: was the book seated with the day's leaders (top-30 liquid $20-$400 gainers with a catalyst)? What blocked
    the ones it missed? (2) Opens and timing: how many opens vs the 30-40 a day goal, how many 10-minute windows had one,
    and how each open was timed. Ends with the next blocker to test. Reads ai_reports/book_coverage/<day>.jsonl (the
    watcher's per-minute rows) and the paper account's fills; writes ai_reports/book_coverage/report_<day>.md.

    .venv/bin/python tools/book_daily_report.py [--day YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import book_coverage_watch as W  # noqa: E402

ET = ZoneInfo("America/New_York")
GOAL_LO, GOAL_HI, STRETCH = 30, 40, 50


def names_section(rows: list[dict]) -> dict:
    """Per-minute watcher rows -> day averages, blockers by unique name, most-missed leaders. Pure."""
    cov = [r for r in rows if r.get("leaders")]
    pur = [r["purity_pct"] for r in rows if r.get("purity_pct") is not None]
    by_reason: dict[str, set] = defaultdict(set)
    missed = Counter()
    pct = {}
    for r in cov:
        for m in r.get("missing") or []:
            by_reason[m["why"]].add(m["symbol"])
            missed[m["symbol"]] += 1
            pct[m["symbol"]] = max(pct.get(m["symbol"], -99), m["pct"])
    flagged = Counter(s for r in rows for s in (r.get("flagged") or []))
    return {"scans": len(cov), "coverage_avg": round(statistics.mean(r["coverage_pct"] for r in cov), 1) if cov else None,
            "purity_avg": round(statistics.mean(pur), 1) if pur else None,
            "blockers": sorted(((k, len(v), sorted(v)[:8]) for k, v in by_reason.items()), key=lambda x: -x[1]),
            "most_missed": [(s, n, pct[s]) for s, n in missed.most_common(10)],
            "flagged": flagged.most_common(10)}


def opens_section(iv: list[dict], day_start: float, day_end: float) -> dict:
    """Day's position intervals -> total opens, 10-minute windows with >= 1 open, time with >= 1 / >= 2 open. Pure."""
    windows = int((day_end - day_start) // 600)
    hit = sum(1 for k in range(windows) if any(day_start + 600 * k <= x["t_open"] < day_start + 600 * (k + 1) for x in iv))
    s = W.opens_summary(iv, day_end, day_start)
    return {"opens": len(iv), "windows": windows, "windows_with_open": hit,
            "windows_pct": round(100 * hit / windows, 1) if windows else None,
            "share_ge1_pct": s["share_ge1_pct"], "share_ge2_pct": s["share_ge2_pct"]}


def timing_section(cards: list[dict]) -> dict:
    """Timing cards -> share bought in the top 10% of the day's range, median minutes after the low, green-in-3 rate. Pure."""
    rp = [c["range_pos"] for c in cards if c.get("range_pos") is not None]
    lo = [c["min_since_low"] for c in cards if c.get("min_since_low") is not None]
    g = [c["green_3m"] for c in cards if "green_3m" in c]
    res = [c["result_bp"] for c in cards if c.get("result_bp") is not None]
    return {"n": len(cards), "top10_range_pct": round(100 * sum(x >= 90 for x in rp) / len(rp), 1) if rp else None,
            "median_range_pos": statistics.median(rp) if rp else None,
            "median_min_after_low": statistics.median(lo) if lo else None,
            "green_3m_pct": round(100 * sum(g) / len(g), 1) if g else None,
            "mean_result_bp": round(statistics.mean(res), 1) if res else None}


def next_blocker(names: dict) -> str:
    if not names["blockers"]:
        return "no blocker recorded"
    reason, n, syms = names["blockers"][0]
    return f"`{reason}` kept {n} leaders off the book ({', '.join(syms)}) - test that blocker next"


def render(day, names, opens, timing) -> str:
    goal = ("MET" if opens["opens"] >= GOAL_LO else "below") + f" (goal {GOAL_LO}-{GOAL_HI}, stretch {GOAL_HI}-{STRETCH})"
    L = [f"# Book report {day}", "",
         "## Opens", f"- **{opens['opens']} opens** - {goal}",
         f"- 10-minute windows with an open: {opens['windows_with_open']} of {opens['windows']} ({opens['windows_pct']}%) - standard >= 1 per 10 min",
         f"- time with >= 1 position open: {opens['share_ge1_pct']}%, >= 2: {opens['share_ge2_pct']}%", "",
         "## Names", f"- leaders covered by the book (avg over {names['scans']} scans): {names['coverage_avg']}%",
         f"- seated names that were leaders with a catalyst (avg): {names['purity_avg']}%",
         "- blockers (unique leaders kept off, by main refusal reason):"]
    L += [f"  - `{r}`: {n} - {', '.join(s)}" for r, n, s in names["blockers"][:8]] or ["  - none"]
    L += ["- most-missed leaders (scans missing, best day %):"]
    L += [f"  - {s}: {n} scans, up to +{p}%" for s, n, p in names["most_missed"]] or ["  - none"]
    if names["flagged"]:
        L += ["- seated names flagged (not a leader or no catalyst): " + ", ".join(f"{s} ({n})" for s, n in names["flagged"])]
    L += ["", "## Timing of opens", f"- opens with a timing card: {timing['n']}",
          f"- bought in the top 10% of the day's range: {timing['top10_range_pct']}% (median range position {timing['median_range_pos']})",
          f"- median minutes after the day's low: {timing['median_min_after_low']}",
          f"- went green within 3 minutes: {timing['green_3m_pct']}%",
          f"- mean result per open: {timing['mean_result_bp']} bp (broker fills, the spread already paid)",
          "", "## Next", f"- {next_blocker(names)}"]
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default=datetime.now(ET).date().isoformat())
    a = ap.parse_args(argv)
    day = a.day
    p = W.OUT / f"{day}.jsonl"
    rows = [json.loads(x) for x in open(p)] if p.exists() else []
    iv = W.position_intervals(W.today_fills(day))
    ds = datetime.fromisoformat(day).replace(hour=9, minute=30, tzinfo=ET).timestamp()
    de = datetime.fromisoformat(day).replace(hour=16, minute=0, tzinfo=ET).timestamp()
    headers = W._keys()
    cards = [W.timing_card(x, W.day_bars(headers, x["symbol"], day)) for x in iv]
    md = render(day, names_section(rows), opens_section(iv, ds, de), timing_section(cards))
    W.OUT.mkdir(parents=True, exist_ok=True)
    (W.OUT / f"report_{day}.md").write_text(md)
    (W.OUT / f"report_{day}_opens.json").write_text(json.dumps(cards, indent=1))
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
