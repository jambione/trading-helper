#!/usr/bin/env python3
"""Never-green profile (operator 11/06): which logged entry features go with never-green, and do they also go with PROFIT?

A feature that predicts the never-green label but not net bp is a label artifact (e.g. quiet tape: more never-greens, small losses),
not a lever. Every row shows both. EXPLORATORY: many features, one sample; a link here is a hypothesis for a pre-registered forward
test, never a gate. Features stamped from the outcome (extension_class, mfe/mae, peak) are excluded.

For each numeric feature with enough rows (>= MIN_N), split into terciles by value (cuts from the data, outcome-blind) and report:
  AUC of never-green on the feature (0.5 = none; < 0.5 = never-greens have LOWER values), per date half,
  never-green rate and mean net bp (exit vs entry less 3 bp) in the low / mid / high tercile.
A row is flagged 'LABEL ONLY' when its never-green spread is >= 15 points between terciles but the net-bp spread is under 3 bp
or points the other way (the busiest tercile loses more).

    .venv/bin/python tools/ng_profile.py              # table to stdout
    .venv/bin/python tools/ng_profile.py --brief      # section for docs/SCOREBOARD.md
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COST, MIN_N, NG_BP = 3.0, 150, 5.0
CLEAN_SINCE = "2026-10-15"   # rvol was paced ~2x too high until the 10/14 after-close lag fix (movers-rvol-was-inflated) and the floors were re-matched
SKIP = {"ask", "bid", "bars_age_sec", "wr_bar_age_sec", "ob_bars", "rs_bars", "criteria", "extension_class", "git_version",
        "square_since", "last_bar_age_sec"}


def load(path=None, since="2000-01-01"):
    rows = []
    for line in open(path or os.path.join(ROOT, "ai_reports/outcomes.jsonl")):
        try:
            o = json.loads(line)
        except ValueError:
            continue
        e, x, t = o.get("entry_price"), o.get("exit_price"), o.get("entry_time")
        f = o.get("features") or {}
        if not (e and x and t and f):
            continue
        d = time.strftime("%Y-%m-%d", time.localtime(t))
        if d < since:
            continue
        pk = o.get("peak_price") or e
        rows.append({"d": d, "ng": (pk - e) / e * 1e4 < NG_BP, "bp": (x - e) / e * 1e4 - COST, "f": f,
                     "src": f.get("source") or o.get("duel_source")})
    return rows


def auc(pos, neg):
    """P(a never-green value > a rest value), ties half. Rank-sum, O(n log n)."""
    if not pos or not neg:
        return float("nan")
    allv = sorted([(v, 1) for v in pos] + [(v, 0) for v in neg])
    i, rank_sum = 0, 0.0
    while i < len(allv):
        j = i
        while j < len(allv) and allv[j][0] == allv[i][0]:
            j += 1
        avg = (i + j + 1) / 2.0
        rank_sum += avg * sum(1 for k in range(i, j) if allv[k][1] == 1)
        i = j
    n1, n0 = len(pos), len(neg)
    return (rank_sum - n1 * (n1 + 1) / 2.0) / (n1 * n0)


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v


def profile(rows, min_n=MIN_N):
    keys = Counter(k for r in rows for k, v in r["f"].items() if k not in SKIP and _num(v))
    days = sorted({r["d"] for r in rows})
    mid = days[len(days) // 2] if days else ""
    out = []
    for k, n in keys.items():
        rs = [r for r in rows if _num(r["f"].get(k))]
        if len(rs) < min_n:
            continue
        pos = [r["f"][k] for r in rs if r["ng"]]
        neg = [r["f"][k] for r in rs if not r["ng"]]
        if len(pos) < 30 or len(neg) < 30:
            continue
        v = sorted(r["f"][k] for r in rs)
        c1, c2 = v[len(v) // 3], v[(2 * len(v)) // 3]
        if c1 == c2:
            continue
        terc = {"low": [r for r in rs if r["f"][k] <= c1], "mid": [r for r in rs if c1 < r["f"][k] <= c2], "high": [r for r in rs if r["f"][k] > c2]}
        if not all(terc.values()):
            continue
        ngr = {t: sum(r["ng"] for r in g) / len(g) for t, g in terc.items()}
        net = {t: sum(r["bp"] for r in g) / len(g) for t, g in terc.items()}
        halves = []
        for h in (lambda d: d < mid, lambda d: d >= mid):
            p = [r["f"][k] for r in rs if h(r["d"]) and r["ng"]]
            q = [r["f"][k] for r in rs if h(r["d"]) and not r["ng"]]
            halves.append(auc(p, q) if len(p) >= 20 and len(q) >= 20 else float("nan"))
        a = auc(pos, neg)
        ng_spread = max(ngr.values()) - min(ngr.values())
        hi_t, lo_t = max(ngr, key=ngr.get), min(ngr, key=ngr.get)
        net_gain_of_low_ng = net[lo_t] - net[hi_t]            # net of the tercile with FEWEST never-greens minus the one with MOST
        label_only = ng_spread >= 0.15 and net_gain_of_low_ng < 3.0
        out.append({"k": k, "n": len(rs), "auc": a, "h1": halves[0], "h2": halves[1], "ngr": ngr, "net": net, "cuts": (c1, c2),
                    "label_only": label_only, "ng_spread": ng_spread, "net_gain": net_gain_of_low_ng,
                    "first": min(r["d"] for r in rs)})
    out.sort(key=lambda x: -abs(x["auc"] - 0.5))
    return out


def sources(rows):
    s = defaultdict(lambda: [0, 0, 0.0])
    for r in rows:
        if r["src"]:
            a = s[r["src"]]; a[0] += r["ng"]; a[1] += 1; a[2] += r["bp"]
    return {k: (v[0] / v[1], v[1], v[2] / v[1]) for k, v in s.items() if v[1] >= 20}


def table(res, top=10):
    lines = ["| feature | n | since | AUC (NG) | half 1 / 2 | NG rate low / mid / high third | net bp low / mid / high | verdict |",
             "|---|---|---|---|---|---|---|---|"]
    for x in res[:top]:
        pct = " / ".join(f"{100 * x['ngr'][t]:.0f}%" for t in ("low", "mid", "high"))
        bps = " / ".join(f"{x['net'][t]:+.1f}" for t in ("low", "mid", "high"))
        verdict = "LABEL ONLY (more never-greens, not more loss)" if x["label_only"] else ("also moves net" if abs(x["net_gain"]) >= 3 else "weak")
        lines.append(f"| {x['k']} | {x['n']} | {x['first']} | {x['auc']:.2f} | {x['h1']:.2f} / {x['h2']:.2f} | {pct} | {bps} | {verdict} |")
    return lines


def brief(top=10, since=CLEAN_SINCE):
    try:
        rows = load(since=since)
    except OSError as e:
        return ["## Never-green profile", "", f"(unavailable: {e})"]
    res = profile(rows)
    src = sources(rows)
    out = ["## Never-green profile (exploratory: which entry features go with never-green, and do they go with profit?)", "",
           f"{len(rows)} entries with logged features since {min(r['d'] for r in rows) if rows else '-'} (clean era: volume ratios were paced ~2x high before 10/15, so older rows mix two scales; use --all for lifetime). AUC < 0.5 = never-greens have LOWER values. "
           "A link that predicts the label but not net bp is a label artifact, not a lever. Hypotheses only; nothing here is a gate.", ""]
    out += table(res, top)
    if src:
        out += ["", "| source | entries | never-green | net bp/trade |", "|---|---|---|---|"]
        out += [f"| {k} | {n} | {100 * g:.0f}% | {b:+.1f} |" for k, (g, n, b) in sorted(src.items(), key=lambda kv: -kv[1][1])]
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--brief", action="store_true")
    ap.add_argument("--all", action="store_true", help="lifetime (mixes the inflated-rvol era)")
    a = ap.parse_args(argv)
    print("\n".join(brief(since="2000-01-01" if a.all else CLEAN_SINCE)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
