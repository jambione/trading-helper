#!/usr/bin/env python3
"""Which names are worth watching? Name quality from seed-time features.

POPULATION  every symbol-day the seed pipeline saw, admitted or refused:
            refusals from ai_reports/admit_ledger/<day>.jsonl, admissions from
            admit_funnel kept_symbols in events.jsonl. t0 = first time seen,
            floored at 09:35 ET (needs 5 bars of history).

LABEL       name quality for the rest of the day, independent of our timing:
            from t0 to 15:30, every 5th minute, scored from that minute's
            close with runway_study.score_path:
              up1  share of minutes that go +1% before -1%
              r30  mean 30-minute return

FEATURES    all from SIP 1m bars completed before t0, same for every name:
            price, move since open, prior 15m return, range position,
            1m volatility, dollar volume so far, strength vs SPY since open,
            minutes since open; plus source and admitted/refused.

TEST        tercile top-vs-bottom on up1 and r30, t-stat, same sign in both
            date halves. Then a held-out check: rank names by the features
            that were stable in the FIRST half only, and measure top vs
            bottom third on the SECOND half.
"""
from __future__ import annotations

import collections
import glob
import json
import math
import os
import statistics
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)
import bars  # noqa: E402
import runway_study as rs  # noqa: E402

DAYS = sys.argv[1:] or ["2026-09-16", "2026-09-17", "2026-09-18",
                        "2026-09-21", "2026-09-22"]
T_MIN = 9 * 60 + 36  # 6 completed bars; at 09:35 index_at gives 5
T_MAX = 15 * 60


def first_seen() -> dict:
    """{(sym, day): {"t0", "sources", "admitted", "reasons"}}"""
    seen: dict = {}

    def note(sym, day, ts, src, admitted, reason):
        if not rs.SYM_RE.match(sym):
            return
        k = (sym, day)
        r = seen.setdefault(k, {"t0": ts, "sources": set(), "admitted": False,
                                "reasons": collections.Counter()})
        r["t0"] = min(r["t0"], ts)
        if src:
            r["sources"].add(src)
        if admitted:
            r["admitted"] = True
        if reason:
            r["reasons"][reason] += 1

    for day in DAYS:
        p = os.path.join(ROOT, "ai_reports", "admit_ledger", f"{day}.jsonl")
        n = 0
        for line in open(p):
            try:
                e = json.loads(line)
            except Exception:
                continue
            n += 1
            note(str(e.get("symbol") or ""), day, float(e.get("ts") or 0),
                 e.get("source"), False, e.get("reason"))
        print(f"  ledger {day}: {n} rows", file=sys.stderr)
    t0 = time.mktime(time.strptime(DAYS[0], "%Y-%m-%d"))
    days = set(DAYS)
    for line in open(os.path.join(ROOT, "ai_reports", "events.jsonl")):
        if '"admit_funnel"' not in line:
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        ts = float(e.get("ts") or 0)
        if ts < t0:
            continue
        day = bars.day_of(ts)
        if day not in days:
            continue
        for s in e.get("kept_symbols") or []:
            note(str(s), day, ts, None, True, None)
    return seen


def features(B, i: int, spy) -> dict | None:
    t, o, h, l, c, v = B
    k = i - 1
    if k < 5:
        return None
    f = {"price": c[k], "move_open": (c[k] / o[0] - 1) * 100,
         "ret_15m": (c[k] / c[max(0, k - 15)] - 1) * 100,
         "mins_open": bars.et_minutes(t[i]) - 570}
    hi, lo = max(h[:k + 1]), min(l[:k + 1])
    if hi > lo:
        f["range_pos"] = (c[k] - lo) / (hi - lo) * 100
    rets = [(c[m] / c[m - 1] - 1) * 100 for m in range(max(1, k - 14), k + 1) if c[m - 1]]
    if len(rets) >= 5:
        f["vol_1m"] = statistics.stdev(rets)
    f["dollars_m"] = sum(c[m] * v[m] for m in range(k + 1)) / 1e6
    if spy:
        j = bars.index_at(spy[0], t[k])
        if j >= 0:
            f["rs_spy"] = f["move_open"] - (spy[4][j] / spy[1][0] - 1) * 100
    return f


def label(B, i: int) -> dict | None:
    t = B[0]
    up, r30 = [], []
    for m in range(i, len(t), 5):
        if bars.et_minutes(t[m]) > 15 * 60 + 30:
            break
        s = rs.score_path(B, m, B[4][m])
        if not s:
            continue
        if s.get("tp_1.0") is not None:
            up.append(s["tp_1.0"])
        if s.get("ret_30") is not None:
            r30.append(s["ret_30"])
    if len(r30) < 6:
        return None
    return {"up1": rs.mean(up) if up else None, "r30": rs.mean(r30), "n_min": len(r30)}


def terciles(rows, feat, tgt, halves):
    pts = [(r["f"][feat], r[tgt], r["day"]) for r in rows
           if feat in r["f"] and r.get(tgt) is not None]
    if len(pts) < 60:
        return None
    xs = sorted(p[0] for p in pts)
    a, b = rs.q(xs, 1 / 3), rs.q(xs, 2 / 3)
    lo = [p for p in pts if p[0] <= a]
    hi = [p for p in pts if p[0] >= b]

    def t_of(days):
        return rs.tstat([p[1] for p in hi if p[2] in days], [p[1] for p in lo if p[2] in days])
    return {"n": len(pts), "a": a, "b": b,
            "lo": rs.mean([p[1] for p in lo]), "hi": rs.mean([p[1] for p in hi]),
            "t": rs.tstat([p[1] for p in hi], [p[1] for p in lo]),
            "t1": t_of(halves[0]), "t2": t_of(halves[1])}


def main():
    seen = first_seen()
    print(f"symbol-days seen: {len(seen)}", file=sys.stderr)
    cache = rs._load_cache()
    want: dict = collections.defaultdict(set)
    for (s, d) in seen:
        want[d].add(s)
    for d in DAYS:
        want[d].add("SPY")
    for d, syms in want.items():
        syms = sorted(syms)
        for i in range(0, len(syms), 100):
            rs.fetch_days({d: set(syms[i:i + 100])}, cache)

    rows = []
    for (s, d), info in seen.items():
        B = cache.get((s, d))
        spy = cache.get(("SPY", d))
        if not B or s == "SPY":
            continue
        t0 = info["t0"]
        m0 = bars.et_minutes(t0)
        if m0 < T_MIN:
            t0 = t0 - (m0 - T_MIN) * 60
        if bars.et_minutes(t0) > T_MAX:
            continue
        i = bars.index_at(B[0], t0)
        if i < 6:
            continue
        f = features(B, i, spy)
        lab = label(B, i)
        if not f or not lab:
            continue
        rows.append({"sym": s, "day": d, "f": f, **lab,
                     "admitted": info["admitted"],
                     "source": sorted(info["sources"])[0] if info["sources"] else "book",
                     "top_reason": (info["reasons"].most_common(1)[0][0]
                                    if info["reasons"] else "admitted")})
    print(f"scored symbol-days: {len(rows)}", file=sys.stderr)
    h1, h2 = set(DAYS[:len(DAYS) // 2 + 1]), set(DAYS[len(DAYS) // 2 + 1:])

    def summary(label_, rs_):
        u = [r["up1"] for r in rs_ if r.get("up1") is not None]
        r3 = [r["r30"] for r in rs_]
        if len(r3) < 15:
            return
        se = statistics.stdev(r3) / math.sqrt(len(r3))
        print(f"  {label_:<34} n={len(r3):>4}  up1 {rs.mean(u):.1%}  r30 {rs.mean(r3):+.3f}% ±{se:.3f}")

    print(f"\n== POPULATION ({', '.join(DAYS)}; halves {sorted(h1)} | {sorted(h2)}) ==")
    summary("all names seen", rows)
    summary("admitted to the book", [r for r in rows if r["admitted"]])
    summary("refused only", [r for r in rows if not r["admitted"]])
    print("\n== BY SOURCE ==")
    by = collections.defaultdict(list)
    for r in rows:
        by[r["source"]].append(r)
    for k, v in sorted(by.items(), key=lambda x: -len(x[1])):
        summary(k, v)
    print("\n== BY MAIN REFUSAL REASON (refused names) ==")
    by = collections.defaultdict(list)
    for r in rows:
        if not r["admitted"]:
            by[r["top_reason"]].append(r)
    for k, v in sorted(by.items(), key=lambda x: -len(x[1]))[:10]:
        summary(k, v)

    feats = sorted({k for r in rows for k in r["f"]})
    stable = {}
    for tgt in ("up1", "r30"):
        print(f"\n== FEATURES vs {tgt}: bottom third / top third, t (half1, half2) ==")
        out = []
        for fn in feats:
            d = terciles(rows, fn, tgt, (h1, h2))
            if d:
                out.append((fn, d))
        out.sort(key=lambda x: -abs(x[1]["t"] or 0))
        for fn, d in out:
            ok = (d["t"] is not None and abs(d["t"]) >= 2 and d["t1"] is not None
                  and d["t2"] is not None and (d["t1"] > 0) == (d["t2"] > 0) == (d["t"] > 0))
            if ok:
                stable.setdefault(fn, []).append((tgt, 1 if d["t"] > 0 else -1))
            fmtv = (lambda x: f"{x:.1%}") if tgt == "up1" else (lambda x: f"{x:+.3f}%")
            print(f"  {fn:<12} n={d['n']:>4}  lo {fmtv(d['lo']):>8}  hi {fmtv(d['hi']):>8}"
                  f"  t {d['t'] or 0:+5.1f} ({d['t1'] or 0:+.1f}, {d['t2'] or 0:+.1f})"
                  f"  cuts {d['a']:.3g}|{d['b']:.3g}{'  STABLE' if ok else ''}")

    # Held-out: fit directions on half 1 only, test on half 2.
    print("\n== HELD-OUT: directions fit on half 1, scored on half 2 ==")
    train = [r for r in rows if r["day"] in h1]
    test = [r for r in rows if r["day"] in h2]
    dirs = {}
    for fn in feats:
        d = terciles(train, fn, "up1", (h1, set()))
        if d and d["t"] is not None and abs(d["t"]) >= 2:
            dirs[fn] = 1 if d["t"] > 0 else -1
    print(f"  features used ({len(dirs)}): {dirs}")
    if dirs and test:
        # rank-average score within the test set
        ranks = {}
        for fn in dirs:
            vals = sorted((r["f"][fn], idx) for idx, r in enumerate(test) if fn in r["f"])
            for pos, (_, idx) in enumerate(vals):
                ranks.setdefault(idx, []).append(dirs[fn] * (pos / max(1, len(vals) - 1) - 0.5))
        scored = [(rs.mean(v), test[idx]) for idx, v in ranks.items() if v]
        scored.sort(key=lambda x: x[0])
        n3 = len(scored) // 3
        summary("test: bottom third by score", [r for _, r in scored[:n3]])
        summary("test: middle third", [r for _, r in scored[n3:2 * n3]])
        summary("test: top third by score", [r for _, r in scored[2 * n3:]])
        summary("test: admitted (current book)", [r for r in test if r["admitted"]])


if __name__ == "__main__":
    main()
