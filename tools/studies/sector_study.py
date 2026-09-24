#!/usr/bin/env python3
"""Does sector strength separate good entries? (2026-09-14..23)

Industry per symbol from Finnhub /stock/profile2 (finnhubIndustry), cached in
ai_reports/industry_cache.json, mapped to a sector ETF. At each event time:
  sec_rs      sector ETF move since the open minus SPY's, %
  sec_rs15    sector ETF 15-minute return minus SPY's, %  (sector turning now)
  stk_vs_sec  the stock's move since the open minus its sector ETF's, %
Events: one-arm (mid_rise) and a random minute (~control), book-admitted
names >= $20, after admission, 09:40-15:30 — same population as
tools/entry_screen.py. up1 = +1% before -1%; r30 = 30-minute return.
"""
from __future__ import annotations

import json
import math
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)
import bars  # noqa: E402
import entry_screen as es  # noqa: E402
import runway_study as rs  # noqa: E402

DAYS = ["2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18",
        "2026-09-21", "2026-09-22", "2026-09-23"]
H1 = set(DAYS[:4])
CACHE = os.path.join(ROOT, "ai_reports", "industry_cache.json")
ETFS = ("SMH", "XLK", "XLC", "XLV", "XLF", "XLE", "XLU", "XLRE", "XLY", "XLP",
        "XLI", "XLB")
MAP = [  # (keywords, etf) — first match wins
    (("semicond",), "SMH"),
    (("biotech", "pharma", "health", "life science", "medical"), "XLV"),
    (("bank", "financ", "insur", "capital market"), "XLF"),
    (("oil", "gas", "energy", "coal"), "XLE"),
    (("utilit",), "XLU"),
    (("real estate", "reit"), "XLRE"),
    (("commun", "media", "telecom", "entertainment"), "XLC"),
    (("tech", "software", "internet", "it services", "electronic"), "XLK"),
    (("food", "beverage", "tobacco", "household", "personal"), "XLP"),
    (("retail", "consumer", "hotel", "restaur", "leisure", "auto", "apparel",
      "textile", "diversified consumer", "distributors"), "XLY"),
    (("aero", "defense", "airline", "machin", "industrial", "building", "construct",
      "road", "rail", "logistic", "transport", "electrical", "commercial",
      "professional", "trading compan", "marine"), "XLI"),
    (("chemical", "metal", "mining", "packag", "paper", "material"), "XLB"),
]


def industries(syms: list[str]) -> dict:
    try:
        cache = json.load(open(CACHE))
    except Exception:
        cache = {}
    import urllib.parse
    import urllib.request
    from config import load_config
    key = load_config().get("finnhub_key")
    todo = [s for s in syms if s not in cache]
    print(f"industry lookups needed: {len(todo)}", file=sys.stderr)
    for n, s in enumerate(todo):
        url = "https://finnhub.io/api/v1/stock/profile2?" + urllib.parse.urlencode(
            {"symbol": s, "token": key})
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                cache[s] = (json.loads(r.read() or b"{}") or {}).get("finnhubIndustry") or ""
        except Exception as e:  # noqa: BLE001
            if "429" in str(e):
                time.sleep(30)
            cache[s] = cache.get(s, None)
        if n % 25 == 0:
            json.dump(cache, open(CACHE, "w"))
            print(f"  {n}/{len(todo)}", file=sys.stderr)
        time.sleep(1.1)   # free tier: 60/min
    json.dump(cache, open(CACHE, "w"))
    return cache


def etf_for(ind: str | None) -> str | None:
    t = str(ind or "").lower()
    for kws, etf in MAP:
        if any(k in t for k in kws):
            return etf
    return None


def move_since_open(B, k):
    t, o, h, l, c, _v = B
    first = next((i for i, ts in enumerate(t) if bars.et_minutes(ts) >= 570), None)
    if first is None or k < first:
        return None
    return (c[k] / o[first] - 1) * 100


def main():
    from config import load_config
    cfg = load_config()
    first: dict = {}
    want = es.admitted(DAYS, first)
    syms = sorted({s for v in want.values() for s in v})
    ind = industries(syms)
    for d in DAYS:
        want[d] |= set(ETFS) | {"SPY"}
    ext = es.fetch_ext(want)
    rows = []
    mapped = sum(1 for s in syms if etf_for(ind.get(s)))
    print(f"symbols {len(syms)}, mapped to a sector ETF {mapped}", file=sys.stderr)
    for d in DAYS:
        spy = ext.get(("SPY", d))
        if spy is None:
            continue
        SB = es.to_B(spy)
        eB = {e: es.to_B(ext[(e, d)]) for e in ETFS if ext.get((e, d)) is not None}
        for s in want[d]:
            if s in ETFS or s == "SPY":
                continue
            etf = etf_for(ind.get(s))
            df = ext.get((s, d))
            if etf not in eB or df is None or len(df) < 150:
                continue
            ind_ = es.indicators(df, cfg, spy)
            B = es.to_B(df)
            E = eB[etf]
            t_on = first.get((s, d))
            last = -1e18
            for i in range(20, len(B[0]) - 1):
                m = ind_["mins"][i]
                if m < 9 * 60 + 40 or m > 15 * 60 + 30 or B[4][i] < 20:
                    continue
                if t_on is None or ind_["ts"][i] < t_on:
                    continue
                kinds = []
                if i % 5 == 0:
                    kinds.append("~control")
                if "mid_rise" in es.fire(ind_, i) and ind_["ts"][i] - last >= es.COOL:
                    last = ind_["ts"][i]
                    kinds.append("mid_rise")
                if not kinds:
                    continue
                ke, ks = bars.index_at(E[0], B[0][i]), bars.index_at(SB[0], B[0][i])
                if ke < 15 or ks < 15:
                    continue
                me, msp, mst = move_since_open(E, ke), move_since_open(SB, ks), move_since_open(B, i)
                if None in (me, msp, mst):
                    continue
                f = {"sec_rs": me - msp,
                     "sec_rs15": ((E[4][ke] / E[4][ke - 15]) - (SB[4][ks] / SB[4][ks - 15])) * 100,
                     "stk_vs_sec": mst - me, "etf": etf}
                sc = rs.score_path(B, i, B[4][i])
                if sc:
                    for k in kinds:
                        rows.append((k, d, s, f, sc))

    ctrl = [r for r in rows if r[0] == "~control"]
    base = rs.mean([r[4]["tp_1.0"] for r in ctrl if r[4].get("tp_1.0") is not None])
    print(f"SECTOR STUDY {DAYS[0]}..{DAYS[-1]}  baseline random minute up1 {base:.1%} "
          f"(n={len(ctrl)})\n")

    def cell(xs):
        u = [r[4]["tp_1.0"] for r in xs if r[4].get("tp_1.0") is not None]
        r3 = [r[4]["ret_30"] for r in xs if r[4].get("ret_30") is not None]
        if len(u) < 15:
            return f"{'n<15':>24}"
        return f"{rs.mean(u):>6.1%} {rs.mean(r3):>+7.3f} n={len(u):<5}"

    def z(xs):
        u = [r[4]["tp_1.0"] for r in xs if r[4].get("tp_1.0") is not None]
        if len(u) < 15:
            return float("nan")
        return (rs.mean(u) - base) / math.sqrt(base * (1 - base) / len(u))

    splits = [
        ("sector leading > +0.5%", lambda f: f["sec_rs"] > 0.5),
        ("sector in line", lambda f: -0.5 <= f["sec_rs"] <= 0.5),
        ("sector lagging < -0.5%", lambda f: f["sec_rs"] < -0.5),
        ("sector rising now (15m)", lambda f: f["sec_rs15"] > 0.1),
        ("sector falling now (15m)", lambda f: f["sec_rs15"] < -0.1),
        ("stock beating sector", lambda f: f["stk_vs_sec"] > 0.5),
        ("stock lagging sector", lambda f: f["stk_vs_sec"] < -0.5),
        ("leading + rising now", lambda f: f["sec_rs"] > 0.5 and f["sec_rs15"] > 0.1),
        ("leading + stock beats sector", lambda f: f["sec_rs"] > 0.5 and f["stk_vs_sec"] > 0.5),
    ]
    print(f"{'bucket':<30}{'kind':<10}{'ALL up1  r30':>24}{'half 1':>24}{'half 2':>24}{'z':>7}")
    for name, pred in splits:
        for kind in ("~control", "mid_rise"):
            sel = [r for r in rows if r[0] == kind and pred(r[3])]
            a = [r for r in sel if r[1] in H1]
            b = [r for r in sel if r[1] not in H1]
            print(f"{name:<30}{kind:<10}{cell(sel):>24}{cell(a):>24}{cell(b):>24}{z(sel):>+7.1f}")
        print()


if __name__ == "__main__":
    main()
