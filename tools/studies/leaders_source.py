#!/usr/bin/env python3
"""leaders_source - would seating the day's leaders (top-30 liquid $20-$400 gainers with a catalyst) that the book does
not seat add buy signals at no worse quality? Pass bar: docs/studies/leaders_source_prereg.json. Read-only. Mini.

    .venv/bin/python tools/studies/leaders_source.py signals   # -> ai_reports/leaders_source/signals.json
    .venv/bin/python tools/studies/quotes_parallel.py leaders_source all 3
    .venv/bin/python tools/studies/leaders_source.py score     # -> result.json
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))
import movers_cap as MC  # noqa: E402

ET = ZoneInfo("America/New_York")
OUT = ROOT / "ai_reports" / "leaders_source"
TOP_N, MIN_PREV_DV, PACE, GAP, MIN_ARTICLES = 30, 50e6, 2.0, 2.0, 200
GATE, CAP_N = 15.0, 1500


def top_mask(pct, band, n=TOP_N):
    """(nsym, 390) % change + band -> bool mask of the top-n names per minute among band names. Pure."""
    x = np.where(band & np.isfinite(pct), pct, -np.inf)
    out = np.zeros(x.shape, dtype=bool)
    k = min(n, x.shape[0])
    for t in range(x.shape[1]):
        col = x[:, t]
        idx = np.argpartition(-col, k - 1)[:k] if x.shape[0] > k else np.arange(x.shape[0])
        idx = idx[np.isfinite(col[idx])]
        out[idx, t] = True
    return out


def catalyst_at(t, story_m, cumv_t, prev_v, gap_pct):
    """List of catalysts true at minute t. Pure."""
    tags = []
    if story_m is not None and story_m <= t:
        tags.append("news")
    if prev_v > 0 and cumv_t / (prev_v * max((t + 1) / 390, 0.05)) >= PACE:
        tags.append("vol")
    if gap_pct is not None and gap_pct >= GAP:
        tags.append("gap")
    return tags


def signals():
    from relative_strength_scan import load_day
    from evening_family import WH_SIP, ALL_ETF, minute_grid, presquare_bars, first_true
    import news_source as ns
    etf = set(ALL_ETF)
    files = sorted(WH_SIP.glob("20??-??-??.npz"))
    out, days = {"lead": [], "lead_all": []}, []
    prev = None
    for fp in files:
        day = load_day(fp)
        kept = MC.kept_symbols(fp.stem)
        if prev is not None and kept is not None and day["idx"].get("SPY") is not None:
            days.append(fp.stem)
            npath = ns.NEWS / f"{fp.stem}.json"
            stories = {}
            if npath.exists():
                arts = json.loads(npath.read_text())
                if len(arts) >= MIN_ARTICLES:
                    stories = ns.first_story_minutes(arts, fp.stem)
            names = []
            pdv = np.nansum(prev["c"] * prev["v"], axis=1)
            for s, i in day["idx"].items():
                j = prev["idx"].get(s)
                if j is None or s in etf or not s.isalpha() or pdv[j] < MIN_PREV_DV:
                    continue
                pc = ns.last_close(prev["c"][j])
                if pc and pc > 0:
                    names.append((s, i, j, pc, float(np.nansum(prev["v"][j]))))
            if names:
                ii = [x[1] for x in names]
                C = day["c"][ii]
                pcs = np.array([x[3] for x in names])[:, None]
                pct = (C / pcs - 1) * 100
                band = np.isfinite(C) & (C >= 20) & (C <= 400)
                top = top_mask(pct, band)
                for k, (s, i, j, pc, pv) in enumerate(names):
                    if not top[k].any():
                        continue
                    o, h, l, c, v = (day[x][i] for x in "ohlcv")
                    cumv = np.nancumsum(np.nan_to_num(v))
                    gap = (o[0] / pc - 1) * 100 if np.isfinite(o[0]) else None
                    cc, hh, ll, off = minute_grid(prev["c"][j], prev["h"][j], prev["l"][j], c, h, l)
                    trig = MC.refractory(t for t in first_true(presquare_bars(cc, hh, ll, off, 21)) if MC.T_FIRST <= t <= MC.T_LAST)
                    for t in trig:
                        if not top[k, t] or not np.isfinite(c[t]) or (c[t] / pc - 1) * 100 < 1.5:
                            continue
                        tags = catalyst_at(t, stories.get(s), float(cumv[t]), pv, gap)
                        if not tags:
                            continue
                        g = MC.ten_min_gross(o, c, t)
                        if g is None:
                            continue
                        rec = {"day": fp.stem, "sym": s, "t": int(t), "gross": round(float(g), 2), "px": round(float(c[t]), 2), "tags": tags}
                        out["lead_all"].append(rec)
                        if s not in kept:
                            out["lead"].append(rec)
            print(fp.stem, {k: len(v) for k, v in out.items()}, flush=True)
        prev = day
    out["days"] = days
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "signals.json").write_text(json.dumps(out))
    print({k: len(v) for k, v in out.items() if k != "days"}, "days", len(days))


def _h(*parts):
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest(), 16)


def lead_sample(sig):
    return sorted(sig["lead"], key=lambda r: _h("leaders", r["day"], r["sym"], r["t"]))[:CAP_N]


def plan():
    sig = json.loads((OUT / "signals.json").read_text())
    return sorted({("ls", r["day"], r["sym"], r["t"], 0.0) for r in lead_sample(sig)})


def gated(rows, cache, gate):
    import news_source as ns
    out = []
    for r in rows:
        sp = cache.get(ns.qkey(r["day"], r["sym"], r["t"]))
        if sp is not None and sp <= gate:
            out.append({**r, "spread": float(sp), "net": r["gross"] - MC.COST, "net_own": r["gross"] - float(sp)})
    return out


def score():
    import news_source as ns
    sig = json.loads((OUT / "signals.json").read_text())
    base_sig = json.loads((MC.OUT / "signals.json").read_text())
    cache = json.load(open(ns.QUOTES))
    lead = lead_sample(sig)
    base = MC.plan_sets(base_sig)["base"]
    days = [d for d in sig["days"] if d in set(base_sig["days"])]
    res = {"days": len(days), "counts": {"lead": len(sig["lead"]), "lead_all": len(sig["lead_all"]), "lead_sampled": len(lead)}}
    for gate in (GATE, 6.0):
        gl, gb = gated(lead, cache, gate), gated(base, cache, gate)
        sl, sb = MC.summ(gl), MC.summ(gb)
        lead_rate = len(sig["lead"]) / max(1, len(days)) / MC.SPAN * (len(gl) / max(1, len(lead)))
        base_rate = len(base_sig["base"]) / max(1, len(base_sig["days"])) / MC.SPAN * (len(gb) / max(1, len(base)))
        tag = f"gate_{gate:g}bp"
        res[tag] = {"lead": sl, "base": sb, "welch_t": MC.welch_t(sl, sb),
                    "opens_per_10m": {"lead": round(lead_rate, 3), "base": round(base_rate, 3)},
                    "lead_le_100": MC.summ([r for r in gl if r["px"] <= 100]), "lead_gt_100": MC.summ([r for r in gl if r["px"] > 100]),
                    "lead_own_spread": MC.summ(gl, "net_own"),
                    "by_tag": {t: MC.summ([r for r in gl if t in r["tags"]]) for t in ("news", "vol", "gap")}}
        if gate == GATE:
            res["verdict"] = MC.judge(sl, sb, rate=lead_rate / base_rate if base_rate > 0 else 0.0)
    res["generated"] = datetime.now(ET).isoformat(timespec="minutes")
    (OUT / "result.json").write_text(json.dumps(res, indent=1))
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    {"signals": signals, "score": score}[sys.argv[1]]()
