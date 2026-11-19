#!/usr/bin/env python3
"""morning_picks_hold - pick 5 names at 10:00 from the first 30 minutes of tape, hold to 15:55 with a -3% stop
(operator 11/19: pick the name, then hold). Prereg: docs/studies/morning_picks_hold_prereg.json.

    .venv/bin/python tools/studies/morning_picks_hold.py events   # CPU: eligible lists, rankings, outcomes
    .venv/bin/python tools/studies/quotes_parallel.py morning_picks_hold all 5     # 10:00 SIP spreads
    .venv/bin/python tools/studies/morning_picks_hold.py score
"""
from __future__ import annotations

import hashlib
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))

OUT = ROOT / "ai_reports" / "morning_picks_hold"
DECIDE = ("2026-04-28", "2026-09-23")
CONFIRM = ("2026-09-24", "2026-11-18")
T_SIG, T_IN, T_OUT = 29, 30, 385          # 09:59 bar (signal), 10:00 open (entry), 15:55 close (exit)
STOP = 0.03
K, K_INFO = 5, (3, 10)
DEPTH = 15                                  # ranked names kept per ranker (no-quote fallbacks)
N_SPREAD = 20
RANKERS = ("VOL", "RS", "GAP")


def exit_main(o, h, l, c, stop=STOP, t_out=T_OUT):
    """Gross bp: entry open T_IN; -stop fills at the stop (or the bar open if it gapped through); else close t_out. Pure."""
    e = o[T_IN]
    if not (np.isfinite(e) and e > 0):
        return None
    sp = e * (1 - stop) if stop else None
    last = e
    for m in range(T_IN, t_out + 1):
        if sp is not None and np.isfinite(l[m]) and l[m] <= sp:
            fill = min(sp, o[m]) if np.isfinite(o[m]) else sp
            return float((fill / e - 1) * 1e4)
        if np.isfinite(c[m]):
            last = c[m]
    return float((last / e - 1) * 1e4)


def exit_giveback(o, h, l, c, trigger=0.02, keep=0.5):
    """Main rule + once the peak is >= +trigger, exit when price gives back half the peak gain. Pure."""
    e = o[T_IN]
    if not (np.isfinite(e) and e > 0):
        return None
    sp, peak, last = e * (1 - STOP), e, e
    for m in range(T_IN, T_OUT + 1):
        if np.isfinite(l[m]) and l[m] <= sp:
            return float((min(sp, o[m]) if np.isfinite(o[m]) else sp) / e * 1e4 - 1e4)
        if peak >= e * (1 + trigger):
            floor = e + keep * (peak - e)
            if np.isfinite(l[m]) and l[m] <= floor:
                return float(((min(floor, o[m]) if np.isfinite(o[m]) else floor) / e - 1) * 1e4)
        if np.isfinite(h[m]):
            peak = max(peak, h[m])
        if np.isfinite(c[m]):
            last = c[m]
    return float((last / e - 1) * 1e4)


def features(o, h, l, c, v, pc, prev_vol, spy_ret):
    """First-30-minute facts at the 09:59 close. None when unusable. Pure."""
    seg = slice(0, T_SIG + 1)
    o0, px = o[0], c[T_SIG]
    if not (np.isfinite(o0) and o0 > 0 and np.isfinite(px) and px > 0 and pc > 0):
        return None
    vol = float(np.nansum(v[seg]))
    dv = float(np.nansum(c[seg] * v[seg]))
    hi = float(np.nanmax(h[seg])) if np.isfinite(h[seg]).any() else float("nan")
    return {"px": float(px), "pct": float((px / pc - 1) * 100), "dv30": dv,
            "pace": vol / prev_vol if prev_vol > 0 else None,
            "rs": float((px / o0 - 1) * 1e4 - spy_ret), "holding": bool(np.isfinite(hi) and px >= 0.99 * hi),
            "gap": float((o0 / pc - 1) * 100), "unfilled": bool(px >= o0)}


def eligible(f):
    return f is not None and 20 <= f["px"] <= 400 and f["pct"] >= 1.5 and f["dv30"] >= 5e6 and -50 < f["gap"] < 100


def rank(rows, ranker):
    """rows: [{sym, f}] eligible -> symbols best first for one ranker. Pure."""
    if ranker == "VOL":
        sel = [r for r in rows if r["f"]["pace"] is not None]
        key = lambda r: -r["f"]["pace"]  # noqa: E731
    elif ranker == "RS":
        sel = [r for r in rows if r["f"]["holding"]]
        key = lambda r: -r["f"]["rs"]  # noqa: E731
    else:
        sel = [r for r in rows if r["f"]["gap"] >= 2.0 and r["f"]["unfilled"]]
        key = lambda r: -r["f"]["gap"]  # noqa: E731
    return [r["sym"] for r in sorted(sel, key=lambda r: (key(r), r["sym"]))]


def spread_sample(day, syms, n=N_SPREAD):
    return sorted(syms, key=lambda s: hashlib.sha256(f"mph_sp|{day}|{s}".encode()).hexdigest())[:n]


def events():
    from relative_strength_scan import load_day
    from board_structure_family import universe
    from evening_family import ALL_ETF, WH_SIP
    from ticker_filters import is_levered_etp
    import news_source as ns
    files = sorted(WH_SIP.glob("20??-??-??.npz"))
    days, prev = [], None
    for fp in files:
        day = load_day(fp)
        if prev is None or not (DECIDE[0] <= fp.stem <= CONFIRM[1]):
            prev = day
            continue
        si = day["idx"].get("SPY")
        if si is None or not np.isfinite(day["c"][si][T_OUT - 5:T_OUT + 1]).any():   # half day / no SPY
            prev = day
            continue
        spy_ret = float((day["c"][si][T_SIG] / day["o"][si][T_IN - 30] - 1) * 1e4)
        spy_day = exit_main(day["o"][si], day["h"][si], day["l"][si], day["c"][si], stop=0)
        rows = []
        for s in universe(day, prev, set(ALL_ETF)):
            if is_levered_etp(s):
                continue
            i, j = day["idx"][s], prev["idx"].get(s)
            if j is None:
                continue
            pc = ns.last_close(prev["c"][j])
            f = features(*(day[k][i] for k in "ohlcv"), pc, float(np.nansum(prev["v"][j])), spy_ret)
            if not eligible(f):
                continue
            o, h, l, c = (day[k][i] for k in "ohlc")
            g = exit_main(o, h, l, c)
            if g is None:
                continue
            rows.append({"sym": s, "f": f, "gross": round(g, 2),
                         "g_nostop": round(exit_main(o, h, l, c, stop=0), 2),
                         "g_1200": round(exit_main(o, h, l, c, t_out=149), 2),
                         "g_1400": round(exit_main(o, h, l, c, t_out=269), 2),
                         "g_give": round(exit_giveback(o, h, l, c), 2)})
        ranked = {k: rank(rows, k)[:DEPTH] for k in RANKERS}
        days.append({"day": fp.stem, "spy_day": spy_day, "rows": rows, "ranked": ranked,
                     "spread_sample": spread_sample(fp.stem, [r["sym"] for r in rows])})
        print(fp.stem, len(rows), {k: len(v) for k, v in ranked.items()}, flush=True)
        prev = day
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "events.json").write_text(json.dumps(days))
    return days


def plan(which="all"):
    """Quotes needed: each ranker's top 12 (5 picks + no-quote fallbacks) and the 20-name spread sample, per day."""
    out = set()
    for d in json.loads((OUT / "events.json").read_text()):
        syms = set(d["spread_sample"])
        for k in RANKERS:
            syms |= set(d["ranked"][k][:12])
        out |= {("mph", d["day"], s, T_SIG, 0.0) for s in syms}
    return sorted(out)


def day_t(x):
    x = [v for v in x if v is not None]
    if len(x) < 3 or np.std(x, ddof=1) == 0:
        return None
    return round(float(np.mean(x) / (np.std(x, ddof=1) / math.sqrt(len(x)))), 2)


def picks_for(d, ranker, k, quotes):
    """First k ranked names with a quote -> [(row, spread)]; counts skipped no-quote names. Pure."""
    by = {r["sym"]: r for r in d["rows"]}
    out, skipped = [], 0
    for s in d["ranked"][ranker]:
        sp = quotes.get(f"{d['day']}|{s}|{T_SIG}")
        if sp is None:
            skipped += 1
            continue
        out.append((by[s], sp))
        if len(out) == k:
            break
    return out, skipped


def control(d, k, key="gross", draws=200):
    g = [r[key] for r in d["rows"]]
    if len(g) < k:
        return None
    vals = []
    for n in range(draws):
        rs = np.random.default_rng(int(hashlib.sha256(f"mph|{d['day']}|{n}".encode()).hexdigest()[:16], 16))
        vals.append(float(np.mean(rs.choice(g, size=k, replace=False))))
    return float(np.mean(vals))


def summarize(per):
    """per: [{day, net, diff}] -> stats incl. halves and drop-best. Pure."""
    if not per:
        return {"days": 0}
    net = [p["net"] for p in per]
    diff = [p["diff"] for p in per if p["diff"] is not None]
    days = sorted(p["day"] for p in per)
    h = days[len(days) // 2]
    a = [p["net"] for p in per if p["day"] < h]
    b = [p["net"] for p in per if p["day"] >= h]
    drop = sorted(net)[:-1]
    return {"days": len(per), "net": round(float(np.mean(net)), 2), "net_t": day_t(net),
            "diff": round(float(np.mean(diff)), 2) if diff else None, "diff_t": day_t(diff),
            "half1": round(float(np.mean(a)), 2) if a else None, "half2": round(float(np.mean(b)), 2) if b else None,
            "drop_best": round(float(np.mean(drop)), 2) if drop else None,
            "win_days_pct": round(100 * float(np.mean(np.array(net) > 0)), 1)}


def verdict(dec, conf):
    ok = ((dec.get("net") or -99) >= 10 and (dec.get("diff") or -99) >= 10 and (dec.get("diff_t") or -99) >= 2.5
          and (dec.get("half1") or -1) > 0 and (dec.get("half2") or -1) > 0 and (dec.get("drop_best") or -1) > 0
          and (conf.get("net") or -1) > 0 and (conf.get("diff") or -1) > 0)
    why = [f"net {dec.get('net')} (need +10)", f"vs random {dec.get('diff')} t {dec.get('diff_t')} (need +10, t 2.5)",
           f"halves {dec.get('half1')}/{dec.get('half2')}", f"drop best {dec.get('drop_best')}",
           f"confirm net {conf.get('net')} vs random {conf.get('diff')}"]
    return ("PASS" if ok else "FAIL"), why


def score():
    import news_source as ns
    quotes = json.load(open(ns.QUOTES)) if ns.QUOTES.exists() else {}
    days = json.loads((OUT / "events.json").read_text())
    res, skipped = {}, defaultdict(int)
    for ranker in RANKERS:
        for k in (K,) + K_INFO:
            for key in ("gross", "g_nostop", "g_1200", "g_1400", "g_give"):
                if k != K and key != "gross":
                    continue
                per = {"decide": [], "confirm": []}
                for d in days:
                    picks, sk = picks_for(d, ranker, k, quotes)
                    if key == "gross" and k == K:
                        skipped[ranker] += sk
                    sps = [quotes.get(f"{d['day']}|{s}|{T_SIG}") for s in d["spread_sample"]]
                    sps = [x for x in sps if x is not None]
                    if not picks or not sps:
                        continue
                    net = float(np.mean([r[key] - sp for r, sp in picks]))
                    c = control(d, len(picks), key)
                    diff = None if c is None else net - (c - float(np.median(sps)))
                    per["decide" if d["day"] <= DECIDE[1] else "confirm"].append({"day": d["day"], "net": net, "diff": diff})
                tag = f"{ranker}|K{k}|{key}"
                res[tag] = {p: summarize(v) for p, v in per.items()}
    out = {"verdicts": {r: verdict(res[f"{r}|K{K}|gross"]["decide"], res[f"{r}|K{K}|gross"]["confirm"]) for r in RANKERS},
           "main": {r: res[f"{r}|K{K}|gross"] for r in RANKERS}, "info": res, "noquote_skipped": dict(skipped),
           "spy_day_bp": round(float(np.mean([d["spy_day"] for d in days if d["spy_day"] is not None])), 2),
           "days": len(days)}
    (OUT / "score.json").write_text(json.dumps(out, indent=1))
    print(json.dumps({k: out[k] for k in ("verdicts", "main", "noquote_skipped", "spy_day_bp", "days")}, indent=1))
    return out


if __name__ == "__main__":
    {"events": events, "score": score}[sys.argv[1]]()
