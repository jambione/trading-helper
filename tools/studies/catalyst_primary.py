#!/usr/bin/env python3
"""catalyst_primary - news / volume pump picks the name, the desk's %R trigger times the entry, exit sized to the name.
Pass bar: docs/studies/catalyst_primary_prereg.json. Read-only. Run on the mini.

    .venv/bin/python tools/studies/catalyst_primary.py events   # -> ai_reports/catalyst_primary/events.jsonl
    .venv/bin/python tools/studies/catalyst_primary.py quotes   # real SIP spread at each entry (data account, cached)
    .venv/bin/python tools/studies/catalyst_primary.py score    # -> result.json
"""
from __future__ import annotations

import hashlib
import json
import math
import sys
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))

ET = ZoneInfo("America/New_York")
OUT = ROOT / "ai_reports" / "catalyst_primary"
SPLIT = "2026-09-24"
T_FIRST, T_LAST, HOLD = 15, 330, 60
NEWS_WIN, VOL_PACE, MIN_ARTICLES, CAP = 30, 3.0, 200, 4000
GROUPS = ("NEWS", "VOL", "BOTH")


def vol_exit(o, h, l, c, t, hold=HOLD):
    """Gross bp: enter open t+1, stop entry-2ATR, target entry+4ATR (stop first), else close of minute t+hold. Pure."""
    if t < 15 or t + hold >= len(c):
        return None
    e = o[t + 1]
    rng = h[t - 14:t + 1] - l[t - 14:t + 1]
    rng = rng[np.isfinite(rng)]
    if not (np.isfinite(e) and e > 0) or len(rng) < 10:
        return None
    atr = float(np.mean(rng))
    if atr <= 0:
        return None
    stop, tgt = e - 2 * atr, e + 4 * atr
    for m in range(t + 1, t + hold + 1):
        if np.isfinite(l[m]) and l[m] <= stop:
            return (stop / e - 1) * 1e4
        if np.isfinite(h[m]) and h[m] >= tgt:
            return (tgt / e - 1) * 1e4
    last = c[t + 1:t + hold + 1]
    last = last[np.isfinite(last)]
    return (last[-1] / e - 1) * 1e4 if len(last) else None


def vol_pace(v, t, avg_daily):
    """Volume of closed minutes t-14..t over the 15-minute share of the prior-5-day mean daily volume. Pure."""
    if not avg_daily or avg_daily <= 0 or t < 14:
        return None
    w = v[t - 14:t + 1]
    return float(np.nansum(w)) / (avg_daily * 15 / 390)


def active(group, t, story_m, pace):
    news = story_m is not None and 0 <= t - story_m <= NEWS_WIN
    vol = pace is not None and pace >= VOL_PACE
    return {"NEWS": news, "VOL": vol, "BOTH": news and vol}[group]


def events():
    from relative_strength_scan import load_day
    from board_structure_family import universe
    from evening_family import WH_SIP, ALL_ETF, minute_grid, presquare_bars, first_true
    from holistic_entry import bracket, COST
    import news_source as ns
    hist = defaultdict(lambda: deque(maxlen=5))
    rows, prev, news_days = [], None, {}
    for fp in sorted(WH_SIP.glob("20??-??-??.npz")):
        day = load_day(fp)
        stories = None
        npath = ns.NEWS / f"{fp.stem}.json"
        if npath.exists():
            arts = json.loads(npath.read_text())
            if len(arts) >= MIN_ARTICLES:
                stories = ns.first_story_minutes(arts, fp.stem)
        news_days[fp.stem] = stories is not None
        if prev is not None and day["idx"].get("SPY") is not None:
            for s in universe(day, prev, set(ALL_ETF)):
                i, j = day["idx"][s], prev["idx"].get(s)
                if j is None or len(hist[s]) < 5:
                    continue
                pc = ns.last_close(prev["c"][j])
                if not pc > 0:
                    continue
                o, h, l, c, v = (day[k][i] for k in "ohlcv")
                avg = float(np.mean(hist[s]))
                story = stories.get(s) if stories is not None else None
                cc, hh, ll, off = minute_grid(prev["c"][j], prev["h"][j], prev["l"][j], c, h, l)
                trig = set(first_true(presquare_bars(cc, hh, ll, off, 21)))
                done = set()
                for t in range(T_FIRST, T_LAST + 1):
                    if not np.isfinite(c[t]) or not (20.0 <= c[t] <= 400.0) or (c[t] / pc - 1) * 100 < 1.5:
                        continue
                    pace = vol_pace(v, t, avg)
                    for g in GROUPS:
                        if g in GROUPS and stories is None and g != "VOL":
                            continue
                        if not active(g, t, story, pace):
                            continue
                        for mode in ("wr", "nowr"):
                            key = (g, mode)
                            if key in done or (mode == "wr" and t not in trig):
                                continue
                            gross = vol_exit(o, h, l, c, t)
                            if gross is None:
                                continue
                            done.add(key)
                            b = bracket(o, h, l, c, t)
                            rows.append({"day": fp.stem, "sym": s, "t": t, "group": g, "mode": mode,
                                         "gross": round(gross, 2), "bracket_gross": None if b is None else round(b + COST, 2),
                                         "pace": None if pace is None else round(pace, 2)})
            print(fp.stem, len(rows), flush=True)
        for s, ii in day["idx"].items():
            dv = float(np.nansum(day["v"][ii]))
            if dv > 0:
                hist[s].append(dv)
        prev = day
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / "events.jsonl", "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    (OUT / "news_days.json").write_text(json.dumps(news_days))
    print("events", len(rows))


def _h(*parts):
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest(), 16)


def sample(rows):
    """Cap each (group, mode, period) at CAP by the lowest sha256('catalyst'|day|sym|t). Pure."""
    by = defaultdict(list)
    for r in rows:
        by[(r["group"], r["mode"], r["day"] < SPLIT)].append(r)
    out = []
    for v in by.values():
        out += sorted(v, key=lambda r: _h("catalyst", r["day"], r["sym"], r["t"]))[:CAP]
    return out


def load_events():
    return [json.loads(x) for x in open(OUT / "events.jsonl")]


def quotes():
    import news_source as ns
    rows = sample(load_events())
    plan = sorted({("cat", r["day"], r["sym"], r["t"], 0.0) for r in rows}, key=lambda p: (p[1] >= SPLIT, p[1], p[2], p[3]))
    print("unique entries to quote", len(plan), flush=True)
    ns.fetch_quotes(plan)


def stats(rows):
    if not rows:
        return {"n": 0}
    per = defaultdict(list)
    for r in rows:
        per[r["day"]].append(r["net"])
    m = [np.mean(v) for v in per.values()]
    sd = float(np.std(m, ddof=1)) if len(m) > 2 else 0.0
    days = sorted(per)
    h1 = set(days[: len(days) // 2])
    a = [r["net"] for r in rows if r["day"] in h1]
    b = [r["net"] for r in rows if r["day"] not in h1]
    return {"n": len(rows), "days": len(days), "mean_net": round(float(np.mean([r["net"] for r in rows])), 2),
            "mean_gross": round(float(np.mean([r["gross"] for r in rows])), 2),
            "median_spread": round(float(np.median([r["spread"] for r in rows])), 2),
            "win_pct": round(100 * float(np.mean([r["net"] > 0 for r in rows])), 1),
            "t_day": round(float(np.mean(m) / (sd / math.sqrt(len(m)))), 2) if sd > 0 else None,
            "half1": round(float(np.mean(a)), 2) if a else None, "half2": round(float(np.mean(b)), 2) if b else None}


def verdict(st):
    if st.get("n", 0) < 300:
        return "UNDERPOWERED", [f"n {st.get('n', 0)} (need 300)"]
    f = []
    if st["mean_net"] < 2.0:
        f.append(f"edge {st['mean_net']} bp (need +2.0)")
    if (st.get("t_day") or 0) < 2.4:
        f.append(f"t {st.get('t_day')} (need 2.4)")
    if not ((st.get("half1") or -1) > 0 and (st.get("half2") or -1) > 0):
        f.append(f"halves {st.get('half1')} / {st.get('half2')}")
    return ("PASS" if not f else "FAIL"), f


def score():
    import news_source as ns
    cache = json.load(open(ns.QUOTES))
    rows, missing = [], defaultdict(int)
    for r in sample(load_events()):
        sp = cache.get(ns.qkey(r["day"], r["sym"], r["t"]))
        if sp is None:
            missing[(r["group"], r["mode"], r["day"] < SPLIT)] += 1
            continue
        rows.append({**r, "spread": float(sp), "net": r["gross"] - float(sp)})
    res = {"verdicts": {}, "decide": {}, "info": {}}
    for g in GROUPS:
        for mode in ("wr", "nowr"):
            pre = [r for r in rows if r["group"] == g and r["mode"] == mode and r["day"] < SPLIT]
            post = [r for r in rows if r["group"] == g and r["mode"] == mode and r["day"] >= SPLIT]
            res["decide" if mode == "wr" else "info"][f"{g}|{mode}|pre"] = stats(pre)
            res["info"][f"{g}|{mode}|post"] = stats(post)
            if mode == "wr":
                res["verdicts"][g] = verdict(res["decide"][f"{g}|{mode}|pre"])
                br = [{**r, "gross": r["bracket_gross"], "net": r["bracket_gross"] - r["spread"]}
                      for r in pre if r["bracket_gross"] is not None]
                res["info"][f"{g}|wr|pre|desk_bracket"] = stats(br)
    res["no_quote_dropped"] = {f"{k[0]}|{k[1]}|{'pre' if k[2] else 'post'}": v for k, v in missing.items()}
    res["generated"] = datetime.now(ET).isoformat(timespec="minutes")
    (OUT / "result.json").write_text(json.dumps(res, indent=1))
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    {"events": events, "quotes": quotes, "score": score}[sys.argv[1]]()
