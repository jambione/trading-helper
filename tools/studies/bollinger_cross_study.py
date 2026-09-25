#!/usr/bin/env python3
"""Bollinger Bands at mid_rise −50 crosses: does %B / bandwidth help rank names?

Question (2026-09-25): would Bollinger %B or bandwidth help decide which names
to seat, as a RANKING input only (never a gate)?

Reuses (does not rewrite):
  mid_rise_runway_study  find_crosses / rth_ok / label_path (runner = ≥0.35R
                         or ≥0.6% within 60m before −1R) / load_stop_map
  day_change_fade_study  ret_at (ret30/ret60), hit_dn_before_up ('fade' = −1%
                         before +1% within 60m), avg_daily_volume +
                         expected_fraction (rvol_pace), same population rules
                         (supply names, $20–100, RTH 09:35–15:30, 9/1–9/24)

Features at the cross bar i (bars 0..i; bar i is the completed minute whose
close fires the cross and is the entry price — nothing after i is used):
  pctb_1m, bw_1m   BB(20, 2σ population) on 1m closes c[i-19..i]
                   %B = (c − lower)/(upper − lower); bw = (upper − lower)/mid
  pctb_5m, bw_5m   BB(20, 2) on clock-aligned 5m closes: the completed 5m
                   buckets before the cross's bucket plus the in-progress
                   bucket's close so far (= c[i]), like a live 5m chart.
                   Needs 20 buckets of RTH data (cache is RTH-only), so it
                   only exists for crosses after ~11:05 ET.
  squeeze_1m       bw_1m / median(bw_1m over earlier bars today) (≥10 prior)
  dist_hod_pct     (c / max(high so far today) − 1)·100  ('room below HOD')
  nw_pos           causal (non-repainting) Nadaraya-Watson envelope position:
                   Gaussian-kernel mean of the last 50 closes (h=8, weights
                   only on past/current bars), envelope = ±3 × mean |c − nw|
                   over the last 50 bars; pos = (c − nw)/(3·mae)
  day_chg_pct, rvol_pace (same definitions as day_change_fade_study)

Output: bucket tables per half with n crosses, n symbol-days, top-2
symbol-day share, runner, ret30, ret60, fade; plus symbol-day-weighted ret60
and runner (each symbol-day averaged first). Tercile top-vs-bottom with
per-cross and per-symbol-day t-stats, within day_chg × pace strata, and a
held-out linear rank (fit half 1, score half 2).

Train days ≤ 2026-09-17; test ≥ 2026-09-18.

Run on the mini (read-only; script in /tmp):
  cd ~/repo/trading-helper && PYTHONPATH=/tmp:$PWD:$PWD/tools:$PWD/tools/studies \
    nice -n 15 .venv/bin/python /tmp/bollinger_cross_study.py [--fetch-daily]
--fetch-daily pulls 20-day daily SIP volume (1 multi-symbol request/sec) and
caches it to --avgvol-cache so reruns are offline.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import pickle
import statistics
import sys
from collections import Counter, defaultdict

_HERE = os.path.abspath(__file__)
if _HERE.startswith("/tmp/") or os.path.basename(os.path.dirname(_HERE)) != "studies":
    ROOT = os.environ.get("TH_ROOT") or os.getcwd()
else:
    ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
for p in (ROOT, os.path.join(ROOT, "tools"), os.path.join(ROOT, "tools", "studies")):
    if p not in sys.path:
        sys.path.insert(0, p)

import mid_rise_runway_study as mr  # noqa: E402
import day_change_fade_study as dcf  # noqa: E402

TRAIN_END = "2026-09-17"
TEST_START = "2026-09-18"

PCTB_BUCKETS = [
    ("<0.2", lambda x: x < 0.2),
    ("0.2-0.5", lambda x: 0.2 <= x < 0.5),
    ("0.5-0.8", lambda x: 0.5 <= x < 0.8),
    ("0.8-1.0", lambda x: 0.8 <= x <= 1.0),
    (">1.0", lambda x: x > 1.0),
]


# ---------------------------------------------------------------- features
def bb(closes, n=20, k=2.0):
    if len(closes) < n:
        return None, None
    w = closes[-n:]
    m = sum(w) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in w) / n)
    if m <= 0:
        return None, None
    up, lo = m + k * sd, m - k * sd
    bw = (up - lo) / m
    if up - lo <= 0:
        return None, bw
    return (w[-1] - lo) / (up - lo), bw


def five_min_closes(t, c, i):
    """Clock-aligned 5m closes through bar i (last one = in-progress bucket)."""
    out = []
    cur = None
    for k in range(i + 1):
        b = int(t[k] // 300)
        if b != cur:
            out.append(c[k])
            cur = b
        else:
            out[-1] = c[k]
    return out


def bw1_series(c):
    out = [None] * len(c)
    for k in range(19, len(c)):
        _, out[k] = bb(c[k - 19:k + 1])
    return out


def nw_pos(c, i, lookback=50, h=8.0, mult=3.0):
    if i + 1 < lookback + 10:
        return None
    ws = [math.exp(-(j * j) / (2 * h * h)) for j in range(lookback)]

    def nw_at(k):
        num = den = 0.0
        for j in range(lookback):
            num += c[k - j] * ws[j]
            den += ws[j]
        return num / den

    nws = [nw_at(k) for k in range(i - lookback + 1, i + 1) if k - lookback + 1 >= 0]
    if len(nws) < 10:
        return None
    cs = c[i - len(nws) + 1:i + 1]
    mae = sum(abs(a - b) for a, b in zip(cs, nws)) / len(nws)
    if mae <= 0:
        return None
    return (c[i] - nws[-1]) / (mult * mae)


# ---------------------------------------------------------------- events
def build_events(cache, supply, stops, min_px, max_px):
    events = []
    skipped = Counter()
    for (sym, day), B in cache.items():
        if B is None or not isinstance(B, tuple) or len(B) != 6:
            continue
        if day < "2026-09-01" or day > "2026-09-24":
            continue
        meta_src, t0 = None, None
        if supply:
            if day in supply and sym not in supply[day]:
                skipped["not_in_supply"] += 1
                continue
            if day in supply and sym in supply[day]:
                meta_src = supply[day][sym]["src"]
                t0 = supply[day][sym]["t0"]
        t, o, h, l, c, v = B
        crosses = mr.find_crosses(B)
        if not crosses:
            continue
        bw1 = bw1_series(c)
        for i, fast, slow, slow_rising in crosses:
            ok, dt = mr.rth_ok(t[i])
            if not ok:
                skipped["outside_rth"] += 1
                continue
            px = c[i]
            if not (min_px <= px <= max_px):
                skipped["price_band"] += 1
                continue
            if t0 is not None and t[i] < t0:
                skipped["before_source"] += 1
                continue
            stop_pct = stops.get((sym, day), mr.DEFAULT_STOP_PCT)
            lab = mr.label_path(B, i, px, stop_pct)
            if lab.get("mfe_pct_60") is None:
                skipped["short_path"] += 1
                continue
            pb1, w1 = bb(c[:i + 1])
            pb5, w5 = bb(five_min_closes(t, c, i))
            prior = [x for x in bw1[:i] if x is not None]
            sq = (w1 / statistics.median(prior)) if (w1 is not None and len(prior) >= 10
                                                    and statistics.median(prior) > 0) else None
            hod = max(h[:i + 1])
            events.append({
                "symbol": sym, "day": day, "source": meta_src or "?",
                "ts": t[i], "price": px,
                "day_chg_pct": (px / o[0] - 1) * 100 if o[0] else None,
                "vol_so_far": sum(v[:i + 1]),
                "mins_open": dt.hour * 60 + dt.minute - 570,
                "pctb_1m": pb1, "bw_1m": w1, "pctb_5m": pb5, "bw_5m": w5,
                "squeeze_1m": sq,
                "dist_hod_pct": (px / hod - 1) * 100 if hod else None,
                "nw_pos": nw_pos(c, i),
                "runner": lab["runner"],
                "ret_30": dcf.ret_at(B, i, px, 30),
                "ret_60": dcf.ret_at(B, i, px, 60),
                "fade": dcf.hit_dn_before_up(B, i, px, 1.0, 1.0, 60),
            })
    return events, skipped


# ---------------------------------------------------------------- stats
def ok(x):
    return x is not None and not (isinstance(x, float) and math.isnan(x))


def mean(xs):
    xs = [x for x in xs if ok(x)]
    return statistics.mean(xs) if xs else float("nan")


def welch(a, b):
    a = [x for x in a if ok(x)]
    b = [x for x in b if ok(x)]
    if len(a) < 3 or len(b) < 3:
        return float("nan"), float("nan")
    d = statistics.mean(a) - statistics.mean(b)
    se = math.sqrt(statistics.variance(a) / len(a) + statistics.variance(b) / len(b))
    return d, (d / se if se > 0 else float("nan"))


def by_symday(rows, key):
    g = defaultdict(list)
    for e in rows:
        if ok(e.get(key)):
            g[(e["symbol"], e["day"])].append(e[key])
    return [statistics.mean(v) for v in g.values()]


def summary(rows):
    n = len(rows)
    sd = Counter((e["symbol"], e["day"]) for e in rows)
    top2 = sum(c for _, c in sd.most_common(2))
    fades = [e["fade"] for e in rows if e.get("fade") is not None]
    return {
        "n": n, "symdays": len(sd),
        "top2_share": (top2 / n) if n else float("nan"),
        "top": ",".join(f"{s}{d[5:]}x{c}" for (s, d), c in sd.most_common(2)),
        "runner": mean([e["runner"] for e in rows]),
        "ret30": mean([e["ret_30"] for e in rows]),
        "ret60": mean([e["ret_60"] for e in rows]),
        "fade": (sum(fades) / len(fades)) if fades else float("nan"),
        "sd_runner": mean(by_symday(rows, "runner")),
        "sd_ret60": mean(by_symday(rows, "ret_60")),
    }


HDR = ("| Bucket | Half | Crosses | Sym-days | Top-2 share | Runner | ret30 | ret60 "
       "| Fade | SD runner | SD ret60 |\n|---|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|")


def row_md(name, half, s):
    flag = " ⚠" if s["n"] and (s["top2_share"] >= 0.4 or s["symdays"] < 8) else ""
    return (f"| {name} | {half} | {s['n']} | {s['symdays']} | {s['top2_share']:.0%}{flag} "
            f"| {s['runner']:.1%} | {s['ret30']:+.3f}% | {s['ret60']:+.3f}% | {s['fade']:.1%} "
            f"| {s['sd_runner']:.1%} | {s['sd_ret60']:+.3f}% |")


def bucket_table(events, key, specs, halves):
    lines = [f"\n### {key}\n", HDR]
    for name, pred in [("ALL", None)] + list(specs):
        for hn, days in halves:
            sub = [e for e in events if e["day"] in days and ok(e.get(key))
                   and (pred is None or pred(e[key]))]
            lines.append(row_md(name, hn, summary(sub)))
    miss = sum(1 for e in events if not ok(e.get(key)))
    lines.append(f"\n(missing {key}: {miss} of {len(events)})")
    return "\n".join(lines)


def cuts_of(vals, n=3):
    s = sorted(x for x in vals if ok(x))
    if len(s) < 3 * n:
        return None
    return [s[int(len(s) * k / n)] for k in range(1, n)]


def terc(x, cuts):
    if not ok(x) or cuts is None:
        return None
    return sum(1 for q in cuts if x >= q)


def tercile_specs(cuts, fmt="{:.3f}"):
    a, b = cuts
    return [(f"T1 <{fmt.format(a)}", lambda x, a=a: x < a),
            (f"T2", lambda x, a=a, b=b: a <= x < b),
            (f"T3 ≥{fmt.format(b)}", lambda x, b=b: x >= b)]


def top_vs_bottom(events, key, halves, train_days):
    cuts = cuts_of([e[key] for e in events if e["day"] in train_days and ok(e.get(key))])
    out = {"key": key, "cuts": cuts, "halves": {}}
    for hn, days in halves:
        sub = [e for e in events if e["day"] in days and ok(e.get(key))]
        top = [e for e in sub if terc(e[key], cuts) == 2]
        bot = [e for e in sub if terc(e[key], cuts) == 0]
        r = {}
        for lab in ("ret_60", "runner", "fade"):
            a = [e[lab] for e in top if e.get(lab) is not None]
            b = [e[lab] for e in bot if e.get(lab) is not None]
            r[lab] = welch(a, b)
            r[lab + "_sd"] = welch(by_symday([e for e in top if e.get(lab) is not None], lab),
                                   by_symday([e for e in bot if e.get(lab) is not None], lab))
        r["n_top"], r["n_bot"] = len(top), len(bot)
        r["sd_top"] = len({(e["symbol"], e["day"]) for e in top})
        r["sd_bot"] = len({(e["symbol"], e["day"]) for e in bot})
        out["halves"][hn] = r
    return out


def tvb_md(res_list):
    lines = ["| Feature | Half | n top/bot (sym-days) | ret60 T3−T1 (t) | SD ret60 T3−T1 (t) "
             "| runner T3−T1 (t) | SD runner (t) | fade T3−T1 (t) |",
             "|---|---|---|--:|--:|--:|--:|--:|"]
    for res in res_list:
        for hn, r in res["halves"].items():
            f = lambda k, pct=False: (f"{r[k][0]:+.1%} ({r[k][1]:+.1f})" if pct
                                      else f"{r[k][0]:+.3f}% ({r[k][1]:+.1f})")
            lines.append(f"| {res['key']} | {hn} | {r['n_top']}/{r['n_bot']} "
                         f"({r['sd_top']}/{r['sd_bot']}) | {f('ret_60')} | {f('ret_60_sd')} "
                         f"| {f('runner', True)} | {f('runner_sd', True)} | {f('fade', True)} |")
        hs = list(res["halves"].values())
        sig = []
        for k in ("ret_60", "ret_60_sd", "runner", "runner_sd", "fade"):
            a, b = hs[0][k][0], hs[1][k][0]
            if ok(a) and ok(b):
                sig.append(f"{k}:{'same' if (a > 0) == (b > 0) else 'FLIP'}")
        lines.append(f"| {res['key']} | sign | {' '.join(sig)} | | | | | |")
    return "\n".join(lines)


def within_strata(events, key, halves, train_days):
    """T3−T1 of `key` inside day_chg tercile × pace tercile cells (train cuts)."""
    tr = [e for e in events if e["day"] in train_days]
    cd = cuts_of([e["day_chg_pct"] for e in tr])
    cp = cuts_of([e["rvol_pace"] for e in tr])
    ck = cuts_of([e[key] for e in tr if ok(e.get(key))])
    lines = []
    for hn, days in halves:
        cells = defaultdict(lambda: ([], []))
        for e in events:
            if e["day"] not in days:
                continue
            tk = terc(e.get(key), ck)
            s = (terc(e.get("day_chg_pct"), cd),
                 terc(e.get("rvol_pace"), cp) if cp else 0)
            if tk is None or s[0] is None or s[1] is None or e.get("ret_60") is None:
                continue
            if tk == 2:
                cells[s][0].append(e)
            elif tk == 0:
                cells[s][1].append(e)
        num = den = 0.0
        num_sd = den_sd = 0.0
        pos = tot = 0
        for s, (top, bot) in cells.items():
            if len(top) < 5 or len(bot) < 5:
                continue
            w = min(len(top), len(bot))
            d = mean([e["ret_60"] for e in top]) - mean([e["ret_60"] for e in bot])
            dsd = mean(by_symday(top, "ret_60")) - mean(by_symday(bot, "ret_60"))
            num += w * d; den += w
            num_sd += w * dsd; den_sd += w
            pos += d > 0; tot += 1
        lines.append(f"| {key} | {hn} | {tot} | {pos}/{tot} | "
                     f"{(num / den if den else float('nan')):+.3f}% | "
                     f"{(num_sd / den_sd if den_sd else float('nan')):+.3f}% |")
    return lines


def spearman(x, y):
    def rank(a):
        idx = sorted(range(len(a)), key=lambda k: a[k])
        r = [0.0] * len(a)
        i = 0
        while i < len(a):
            j = i
            while j + 1 < len(a) and a[idx[j + 1]] == a[idx[i]]:
                j += 1
            for k in range(i, j + 1):
                r[idx[k]] = (i + j) / 2.0
            i = j + 1
        return r
    if len(x) < 10:
        return float("nan")
    rx, ry = rank(x), rank(y)
    mx, my = statistics.mean(rx), statistics.mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return num / den if den else float("nan")


def held_out(events, train_days, test_days, feats_base, extra_sets, target="ret_60"):
    """OLS on train (clipped, standardized features), score on test."""
    import numpy as np

    def mat(rows, feats, stats=None):
        X = np.array([[float(e[f]) for f in feats] for e in rows])
        if stats is None:
            lo = np.percentile(X, 1, axis=0); hi = np.percentile(X, 99, axis=0)
            Xc = np.clip(X, lo, hi)
            mu = Xc.mean(0); sd = Xc.std(0); sd[sd == 0] = 1
            stats = (lo, hi, mu, sd)
        lo, hi, mu, sd = stats
        return (np.clip(X, lo, hi) - mu) / sd, stats

    lines = ["| Model | n train/test | test Spearman(pred, ret60) | test top−bottom third ret60 "
             "| SD-weighted top−bottom | test top-third runner | bottom-third runner |",
             "|---|---|--:|--:|--:|--:|--:|"]
    for name, feats in [("day_chg + pace", feats_base)] + [
            (f"+ {'+'.join(x)}", feats_base + x) for x in extra_sets]:
        need = feats + [target]
        tr = [e for e in events if e["day"] in train_days and all(ok(e.get(f)) for f in need)]
        te = [e for e in events if e["day"] in test_days and all(ok(e.get(f)) for f in need)]
        if len(tr) < 50 or len(te) < 50:
            lines.append(f"| {name} | {len(tr)}/{len(te)} | thin | | | | |")
            continue
        X, st = mat(tr, feats)
        y = np.clip(np.array([e[target] for e in tr]), -5, 5)
        X1 = np.c_[np.ones(len(X)), X]
        beta = np.linalg.lstsq(X1, y, rcond=None)[0]
        Xt, _ = mat(te, feats, st)
        pred = np.c_[np.ones(len(Xt)), Xt] @ beta
        yt = [e[target] for e in te]
        rho = spearman(list(pred), yt)
        q1, q2 = np.percentile(pred, [33.33, 66.67])
        top = [e for e, p in zip(te, pred) if p >= q2]
        bot = [e for e, p in zip(te, pred) if p < q1]
        d = mean([e[target] for e in top]) - mean([e[target] for e in bot])
        dsd = mean(by_symday(top, target)) - mean(by_symday(bot, target))
        lines.append(f"| {name} (β={', '.join(f'{b:+.3f}' for b in beta[1:])}) "
                     f"| {len(tr)}/{len(te)} | {rho:+.3f} | {d:+.3f}% | {dsd:+.3f}% "
                     f"| {mean([e['runner'] for e in top]):.1%} "
                     f"| {mean([e['runner'] for e in bot]):.1%} |")
    return "\n".join(lines)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-price", type=float, default=20.0)
    ap.add_argument("--max-price", type=float, default=100.0)
    ap.add_argument("--fetch-daily", action="store_true")
    ap.add_argument("--avgvol-cache", default="/tmp/bb_avgvol.json")
    ap.add_argument("--out", default="/tmp/bollinger_cross_study.md")
    args = ap.parse_args()

    cache_path = dcf._first_path(dcf.CACHE_CANDIDATES)
    supply_path = dcf._first_path(dcf.SUPPLY_CANDIDATES)
    if not cache_path:
        raise SystemExit("no runway_bars_cache.pkl")
    print(f"cache={cache_path} supply={supply_path}", file=sys.stderr)
    with open(cache_path, "rb") as f:
        cache = pickle.load(f)
    supply = json.load(open(supply_path)) if supply_path else {}
    stops = mr.load_stop_map()

    events, skipped = build_events(cache, supply, stops, args.min_price, args.max_price)
    days = sorted({e["day"] for e in events})
    train_days = {d for d in days if d <= TRAIN_END}
    test_days = {d for d in days if d >= TEST_START}
    halves = [("train", train_days), ("test", test_days)]
    print(f"events={len(events)} skipped={dict(skipped)}", file=sys.stderr)

    # rvol_pace (cached)
    av = {}
    if os.path.exists(args.avgvol_cache):
        av = {tuple(k.split("|")): v for k, v in json.load(open(args.avgvol_cache)).items()}
    if args.fetch_daily and not av:
        av = dcf.avg_daily_volume({e["symbol"] for e in events}, days, sleep_s=1.0)
        json.dump({f"{s}|{d}": v for (s, d), v in av.items()}, open(args.avgvol_cache, "w"))
    for e in events:
        a = av.get((e["symbol"], e["day"]))
        e["rvol_pace"] = None
        if a and a > 0:
            fr = dcf.expected_fraction(e["mins_open"])
            if fr > 0:
                e["rvol_pace"] = e["vol_so_far"] / (a * fr)

    # squeeze relative to own day, and bw relative
    out = []
    P = out.append
    P(f"# Bollinger cross study output\n")
    P(f"events={len(events)}  train days={sorted(train_days)}  test days={sorted(test_days)}")
    P(f"skipped={dict(skipped)}")
    P(f"rvol_pace available: {sum(1 for e in events if ok(e.get('rvol_pace')))}")
    for k in ("pctb_1m", "bw_1m", "pctb_5m", "bw_5m", "squeeze_1m", "nw_pos", "dist_hod_pct"):
        P(f"coverage {k}: {sum(1 for e in events if ok(e.get(k)))}")

    # correlations
    P("\n## Correlations (Spearman, all crosses)\n")
    P("| Pair | train | test |\n|---|--:|--:|")
    pairs = [("pctb_1m", "dist_hod_pct"), ("pctb_5m", "dist_hod_pct"), ("pctb_1m", "pctb_5m"),
             ("bw_1m", "bw_5m"), ("bw_1m", "day_chg_pct"), ("bw_1m", "rvol_pace"),
             ("pctb_1m", "day_chg_pct"), ("pctb_1m", "rvol_pace"), ("nw_pos", "pctb_1m")]
    for a, b in pairs:
        vals = []
        for _hn, ds in halves:
            rows = [e for e in events if e["day"] in ds and ok(e.get(a)) and ok(e.get(b))]
            vals.append(spearman([e[a] for e in rows], [e[b] for e in rows]))
        P(f"| {a} vs {b} | {vals[0]:+.2f} | {vals[1]:+.2f} |")

    P("\n## Buckets\n")
    P("SD runner / SD ret60 = average within each symbol-day first, then across "
      "symbol-days. ⚠ = top-2 symbol-days ≥40% of crosses or <8 symbol-days.")
    P(bucket_table(events, "pctb_1m", PCTB_BUCKETS, halves))
    P(bucket_table(events, "pctb_5m", PCTB_BUCKETS, halves))
    tr = [e for e in events if e["day"] in train_days]
    for key in ("bw_1m", "bw_5m", "squeeze_1m", "nw_pos", "dist_hod_pct"):
        cuts = cuts_of([e[key] for e in tr if ok(e.get(key))])
        if cuts:
            P(bucket_table(events, key, tercile_specs(cuts, "{:.4f}"), halves)
              + f"\n(tercile cuts from train: {cuts})")

    P("\n## Tercile top vs bottom (train cuts applied to both halves)\n")
    feats = ["pctb_1m", "pctb_5m", "bw_1m", "bw_5m", "squeeze_1m", "nw_pos",
             "dist_hod_pct", "day_chg_pct", "rvol_pace"]
    P(tvb_md([top_vs_bottom(events, k, halves, train_days) for k in feats]))

    P("\n## Within day_chg × pace strata (T3−T1 ret60, cells with ≥5 each side)\n")
    P("| Feature | Half | cells | cells T3>T1 | weighted ret60 diff | SD-weighted diff |\n"
      "|---|---|--:|--:|--:|--:|")
    for k in ("pctb_1m", "pctb_5m", "bw_1m", "bw_5m", "squeeze_1m", "dist_hod_pct"):
        for line in within_strata(events, k, halves, train_days):
            P(line)

    P("\n## Per-day consistency: symbol-day-weighted ret60 T3−T1 (train cuts), days with ≥3 sym-days each side\n")
    P("| Feature | days T3>T1 / days | median daily diff |\n|---|--:|--:|")
    for k in ("pctb_1m", "pctb_5m", "bw_1m", "bw_5m", "squeeze_1m", "dist_hod_pct"):
        ck = cuts_of([e[k] for e in tr if ok(e.get(k))])
        diffs = []
        for d in days:
            rows = [e for e in events if e["day"] == d and ok(e.get(k)) and e.get("ret_60") is not None]
            top = [e for e in rows if terc(e[k], ck) == 2]
            bot = [e for e in rows if terc(e[k], ck) == 0]
            if len({(e["symbol"]) for e in top}) < 3 or len({(e["symbol"]) for e in bot}) < 3:
                continue
            diffs.append(mean(by_symday(top, "ret_60")) - mean(by_symday(bot, "ret_60")))
        if diffs:
            P(f"| {k} | {sum(1 for x in diffs if x > 0)}/{len(diffs)} | {statistics.median(diffs):+.3f}% |")

    P("\n## Held-out linear rank: fit on train, score on test (target ret60, clipped ±5%)\n")
    base = ["day_chg_pct", "rvol_pace"]
    P(held_out(events, train_days, test_days, base,
               [["pctb_1m"], ["bw_1m"], ["pctb_1m", "bw_1m"], ["pctb_5m", "bw_5m"],
                ["squeeze_1m"], ["dist_hod_pct"]]))
    P("\nReverse direction (fit test, score train):\n")
    P(held_out(events, test_days, train_days, base,
               [["pctb_1m"], ["bw_1m"], ["pctb_1m", "bw_1m"], ["pctb_5m", "bw_5m"],
                ["squeeze_1m"], ["dist_hod_pct"]]))

    text = "\n".join(out)
    with open(args.out, "w") as f:
        f.write(text)
    print(text)


if __name__ == "__main__":
    main()
