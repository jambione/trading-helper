#!/usr/bin/env python3
"""day_review.py — the nightly end-of-day review (operator 10/7: "how we did today and what to do tomorrow").

READ ONLY: it reads the desk's own logs and the nightly study outputs, makes no data requests and places nothing.
It never recommends a config change on its own evidence: a change is named only when a pre-registered gate has
decided, and otherwise the "tomorrow" section says what the data is pointing at and which gate decides it.

Inputs (all on the mini, all optional except outcomes):
  ai_reports/outcomes.jsonl            closed round trips (entry on DAY)
  ai_reports/shadow.jsonl              every book check (price, source, ob_* S/R fields)
  ai_reports/tight_shadow/summary.jsonl  G5 line for DAY
  /tmp/nightly/tt_costing-DAY.txt      tight exit replay, costed (tight_trail_replay_prereg.json)
  ~/session_snapshots/DAY/fidelity.json  replay vs live
  WORK/forward outputs of bro_sr_wr.py (paths printed by that tool) — tails of /tmp/nightly/bro_fwd-*.log

Writes ai_reports/day_review/DAY.md and prints it.
USAGE (mini):  .venv/bin/python tools/studies/day_review.py [DAY]
"""
from __future__ import annotations

import bisect
import json
import math
import os
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
ROOT = os.environ.get("REPO") or os.getcwd()
REP = os.path.join(ROOT, "ai_reports")

# Dated decision gates (docs/PLAN_2026-10-06.md). The review names a change only on or after a gate's date,
# and only as "read the gate", never as a verdict of its own.
GATES = [
    ("G2", "2026-10-15", "cost arms (entry/exit order styles)", "cost_arm prereg"),
    ("G3/G4", "2026-10-19", "name gates", "PLAN_2026-10-06.md"),
    ("G5", "2026-10-20", "tight shadow list", "tight_shadow_prereg.json"),
    ("G8", "2026-10-21", "10:30 start (plus 15:00-15:50 info cell by source)", "slot_1000_skip_prereg.json"),
    ("T1", "2026-10-22", "tight vs movers live", "tight_source_live_prereg.json"),
    ("TT", "2026-10-22", "tight exit replay (arm_pct / stop_1pct / lob)", "tight_trail_replay_prereg.json"),
]


def _load_jsonl(path, keep=None):
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if keep is None or keep(r):
                out.append(r)
    return out


def _day_bounds(day: date):
    t0 = datetime(day.year, day.month, day.day, tzinfo=ET).timestamp()
    return t0, t0 + 86400


def _mean_t(xs):
    n = len(xs)
    if n == 0:
        return None, None
    m = sum(xs) / n
    if n < 2:
        return m, None
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))
    return m, (m / (sd / math.sqrt(n)) if sd > 0 else None)


def _f(v, nd=1):
    return "—" if v is None else f"{v:.{nd}f}"


def trades(day):
    t0, t1 = _day_bounds(day)
    rows = []
    for r in _load_jsonl(os.path.join(REP, "outcomes.jsonl"),
                         lambda r: t0 <= float(r.get("entry_time") or 0) < t1):
        try:
            e, x = float(r["entry_price"]), float(r["exit_price"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (e > 0 and x > 0):
            continue
        src = r.get("duel_source") or (r.get("features") or {}).get("source") or r.get("source") or "?"
        stop = r.get("stop_price")
        rps = (e - float(stop)) if stop not in (None, 0) else None
        mfe_pct = (float(r["mfe_r"]) * rps / e * 100) if (rps and r.get("mfe_r") is not None) else None
        rows.append(dict(
            sym=r.get("symbol"), src=str(src).lower(), t=datetime.fromtimestamp(float(r["entry_time"]), ET),
            bp=(x / e - 1) * 1e4, usd=float(r.get("realized_pl_usd") or 0), hold=float(r.get("hold_sec") or 0),
            why=r.get("close_reason") or r.get("closing_reason"), qty=float(r.get("total_qty") or 0), px=e,
            mfe_pct=mfe_pct, spread_r=r.get("spread_r") if r.get("spread_r") is not None
            else (r.get("features") or {}).get("spread_r")))
    return rows


def book_sizes(day):
    """Distinct names on the book per 30-min bucket (from shadow.jsonl checks), and the per-symbol price series."""
    t0, t1 = _day_bounds(day)
    buckets = defaultdict(set)
    series = defaultdict(list)
    brk = []
    for r in _load_jsonl(os.path.join(REP, "shadow.jsonl"), lambda r: t0 <= float(r.get("ts") or 0) < t1):
        ts = float(r["ts"])
        t = datetime.fromtimestamp(ts, ET)
        if not (9 * 60 + 30 <= t.hour * 60 + t.minute < 16 * 60):
            continue
        sym = r.get("symbol")
        buckets[(t.hour, t.minute // 30 * 30)].add((sym, str(r.get("source") or "?").lower()))
        px = r.get("price")
        if px:
            series[sym].append((ts, float(px)))
        if r.get("ob_brk_dist_pct") is not None and px:
            brk.append((ts, sym, float(r["ob_brk_dist_pct"]), float(px), str(r.get("source") or "?").lower(),
                        r.get("spread_r")))
    for s in series.values():
        s.sort()
    return buckets, series, brk


def _px_at(series, ts, tol=120.0):
    if not series:
        return None
    keys = [p[0] for p in series]
    i = bisect.bisect_left(keys, ts)
    if i < len(series) and series[i][0] - ts <= tol:
        return series[i][1]
    return None


def breakouts(series, brk):
    """First breakout reading per name per 15-min episode; gross move 15 min later (no cost, no control)."""
    seen = {}
    out = []
    for ts, sym, d, px, src, sp in sorted(brk):
        if sym in seen and ts - seen[sym] < 900:
            continue
        seen[sym] = ts
        later = _px_at(series.get(sym), ts + 900)
        if later is None:
            continue
        band = "chase >=0.30" if d >= 0.30 else ("clean 0.10-0.30" if d >= 0.10 else "poke <0.10")
        out.append(dict(sym=sym, src=src, band=band, gross=(later / px - 1) * 1e4,
                        t=datetime.fromtimestamp(ts, ET)))
    return out


def _read(path, tail=None):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    return lines[-tail:] if tail else lines


def main():
    day = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else datetime.now(ET).date()
    D = day.isoformat()
    L = [f"# Day review {D}", "",
         "_Read-only nightly review (tools/studies/day_review.py). One day is information, not evidence: "
         "nothing here changes config; changes come only from the dated gates below._", ""]

    tr = trades(day)
    L += ["## 1. Results by source", ""]
    if not tr:
        L += ["No closed round trips with an entry on this day.", ""]
    else:
        L += ["| Source | Trades | Avg bp/trade | t | Win / loss / flat | P&L | Median hold | $/trade |",
              "|---|---|---|---|---|---|---|---|"]
        by = defaultdict(list)
        for r in tr:
            by[r["src"]].append(r)
        for s in sorted(by, key=lambda k: -len(by[k])) + ["ALL"]:
            v = tr if s == "ALL" else by[s]
            m, t = _mean_t([x["bp"] for x in v])
            w = sum(x["bp"] > 0 for x in v)
            lo = sum(x["bp"] < 0 for x in v)
            hold = sorted(x["hold"] for x in v)[len(v) // 2]
            notional = sum(x["qty"] * x["px"] for x in v) / len(v)
            L.append(f"| {'**ALL**' if s == 'ALL' else s} | {len(v)} | {_f(m)} | {_f(t, 2)} | {w} / {lo} / {len(v) - w - lo} "
                     f"| ${sum(x['usd'] for x in v):.2f} | {hold:.0f}s | ${notional:.0f} |")
        L.append("")

        # The operator's four pillars.
        L += ["## 2. Four pillars", ""]
        hrs = defaultdict(int)
        for r in tr:
            hrs[r["t"].hour] += 1
        mfe = [r["mfe_pct"] for r in tr if r["mfe_pct"] is not None]
        green = sum(1 for x in mfe if x > 0)
        cap = [r["bp"] / 100 / r["mfe_pct"] for r in tr if r["mfe_pct"] and r["mfe_pct"] > 0.05]
        L += [f"- **Opportunities (opens):** {len(tr)} trades; by hour " +
              ", ".join(f"{h}:00 {n}" for h, n in sorted(hrs.items())),
              f"- **Entry quality:** {green} of {len(mfe)} trades ever went green "
              f"({(100 * green / len(mfe)) if mfe else 0:.0f}%)",
              f"- **Runway:** median best move after entry {_f(sorted(mfe)[len(mfe) // 2] if mfe else None, 2)}%",
              f"- **Capture:** median realized / best move {_f(sorted(cap)[len(cap) // 2] * 100 if cap else None, 0)}% "
              f"(trades whose best move was over 0.05%)", ""]

        L += ["## 3. Time of day (entry hour)", "", "| Hour | Trades | Avg bp | tight avg bp |", "|---|---|---|---|"]
        hb = defaultdict(list)
        for r in tr:
            hb[r["t"].hour].append(r)
        for h in sorted(hb):
            v = hb[h]
            m, _ = _mean_t([x["bp"] for x in v])
            mt, _ = _mean_t([x["bp"] for x in v if x["src"] == "tight"])
            L.append(f"| {h}:00 | {len(v)} | {_f(m)} | {_f(mt)} |")
        L += ["", "_G8 information cell (decided ~10/21): 15:00-15:50 vs rest, split by source._", ""]

        L += ["## 4. Exits", ""]
        ex = Counter(r["why"] for r in tr)
        L.append("- By reason: " + ", ".join(f"{k} {n}" for k, n in ex.most_common()))
        for k in ex:
            v = [x["bp"] for x in tr if x["why"] == k]
            L.append(f"  - {k}: avg {_f(sum(v) / len(v))} bp")
        worst = sorted(tr, key=lambda r: r["bp"])[:5]
        best = sorted(tr, key=lambda r: -r["bp"])[:5]
        fmt = lambda r: f"{r['sym']} {r['t']:%H:%M} {r['bp']:+.0f} bp ({r['why']}, {r['hold']:.0f}s)"  # noqa: E731
        L += ["- Worst: " + "; ".join(fmt(r) for r in worst), "- Best: " + "; ".join(fmt(r) for r in best), ""]
        tt = _read(f"/tmp/nightly/tt_costing-{D}.txt")
        L += ["**Tight exit replay (base / arm_pct / stop_1pct / lob), costed, all trades in the replay:**", ""]
        L += (["```"] + tt + ["```"]) if tt else ["_not available (replay not run or failed: /tmp/nightly/tt_replay-DAY.log)_"]
        L += ["", "_Scored on tight trades only at the gate (tight_trail_replay_prereg.json); one day is direction only._", ""]

    buckets, series, brk = book_sizes(day)
    L += ["## 5. Book size (distinct names checked per 30 min)", ""]
    if buckets:
        L.append("| Time | Names | tight | movers | other |")
        L.append("|---|---|---|---|---|")
        for k in sorted(buckets):
            v = buckets[k]
            n_t = sum(1 for _, s in v if s == "tight")
            n_m = sum(1 for _, s in v if s == "movers")
            L.append(f"| {k[0]:02d}:{k[1]:02d} | {len(v)} | {n_t} | {n_m} | {len(v) - n_t - n_m} |")
    else:
        L.append("_no shadow rows_")
    L.append("")

    L += ["## 6. S/R breakouts on the book (information only)", "",
          "_First breakout reading per name per 15 min; gross move 15 min later from the desk's own checks. "
          "No cost and no control: a lead for the breakout prereg, not a result._", ""]
    bo = breakouts(series, brk)
    if bo:
        L += ["| Band | n | Avg gross 15m bp | Up share |", "|---|---|---|---|"]
        bb = defaultdict(list)
        for r in bo:
            bb[r["band"]].append(r["gross"])
        for b in sorted(bb):
            v = bb[b]
            L.append(f"| {b} | {len(v)} | {sum(v) / len(v):+.1f} | {100 * sum(x > 0 for x in v) / len(v):.0f}% |")
    else:
        L.append("_no breakout readings_")
    L.append("")

    L += ["## 7. Study outputs", ""]
    ts_line = [r for r in _load_jsonl(os.path.join(REP, "tight_shadow", "summary.jsonl")) if r.get("day") == D]
    if ts_line:
        s = ts_line[-1]
        sh, dk = s.get("shadow") or {}, s.get("desk_book") or {}
        L.append(f"- **G5 tight shadow:** shadow n {sh.get('n')} net15 {sh.get('net15')} bp vs desk book n {dk.get('n')} "
                 f"net15 {dk.get('net15')} bp (coverage {s.get('minute_coverage')})")
    else:
        L.append("- G5 tight shadow: not run for this day")
    fid = os.path.expanduser(f"~/session_snapshots/{D}/fidelity.json")
    if os.path.exists(fid):
        try:
            fj = json.load(open(fid))
            keys = {k: fj.get(k) for k in ("live_buys", "replay_opens", "matched", "recall", "precision") if k in fj}
            L.append(f"- **Replay fidelity:** {keys or 'see ' + fid}")
        except Exception:  # noqa: BLE001
            L.append(f"- Replay fidelity: unreadable ({fid})")
    else:
        L.append("- Replay fidelity: not written")
    obl = _read(f"/tmp/nightly/ob_fills-{D}.log", tail=6)
    L.append("- **Order-block held-out record:** " + ("appended (see ai_reports/order_block_gate/)" if obl else "not run"))
    for src in ("bro", "alerts"):
        bl = _read(f"/tmp/nightly/{src}_fwd-{D}.log", tail=8)
        L.append(f"- **{src} forward:** " + ("" if bl else "not run"))
        if bl:
            L += ["```"] + bl + ["```"]
    L.append("")

    L += ["## 8. Tomorrow", ""]
    due = [g for g in GATES if g[1] <= D]
    if due:
        L += ["**Gates due — read their pre-registered verdicts before changing anything:**", ""]
        L += [f"- {g[0]} ({g[2]}), due {g[1]}: {g[3]}" for g in due]
    else:
        L.append("**No gate is due: hold settings.** What today's data points at (to be decided by the gates):")
    L.append("")
    if tr:
        notes = []
        tight = [r for r in tr if r["src"] == "tight"]
        late = [r["bp"] for r in tight if r["t"].hour >= 15]
        if len(late) >= 3:
            m, _ = _mean_t(late)
            notes.append(f"- Late session (15:00+) tight trades: n {len(late)}, avg {m:+.1f} bp -> G8 info cell (~10/21).")
        dead = [r for r in tr if r["why"] == "dead_trade"]
        if dead:
            notes.append(f"- dead_trade exits: {len(dead)}, avg {sum(r['bp'] for r in dead) / len(dead):+.1f} bp -> "
                         "lob / arm_pct variants in the tight exit replay (TT).")
        never = [r for r in tr if r["mfe_pct"] is not None and r["mfe_pct"] <= 0]
        if never:
            notes.append(f"- {len(never)} trades never went green (entries are still the coin flip; no entry gate has "
                         "beaten a random minute).")
        L += notes or ["- nothing stood out"]
    L += ["", "| Gate | Due | Question | Prereg |", "|---|---|---|---|"] + [f"| {g[0]} | {g[1]} | {g[2]} | {g[3]} |"
                                                                       for g in GATES]
    text = "\n".join(L) + "\n"
    os.makedirs(os.path.join(REP, "day_review"), exist_ok=True)
    with open(os.path.join(REP, "day_review", f"{D}.md"), "w", encoding="utf-8") as f:
        f.write(text)
    print(text)


if __name__ == "__main__":
    main()
