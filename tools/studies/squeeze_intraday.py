#!/usr/bin/env python3
"""squeeze_intraday - do intraday breakouts in heavily shorted liquid names pay more than other breakouts?
(operator 11/19 'run the short squeeze test tonight'). Prereg: docs/studies/squeeze_intraday_prereg.json.

    .venv/bin/python tools/studies/squeeze_intraday.py events    # CPU: FINRA groups + triggers + gross outcomes
    .venv/bin/python tools/studies/squeeze_intraday.py count     # outcome-blind count step (power)
    .venv/bin/python tools/studies/quotes_parallel.py squeeze_intraday all 4     # after 16:05 only
    .venv/bin/python tools/studies/squeeze_intraday.py score
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

OUT = ROOT / "ai_reports" / "squeeze_intraday"
DECIDE = ("2026-04-28", "2026-09-23")
CONFIRM = ("2026-09-24", "2026-11-18")
T_LO, T_HI = 30, 330
PUB_LAG = 10
HOLIDAYS_2026 = ["2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19",
                 "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25"]
NORMAL_CAP = 3000
EDGE = 10.0


def calendar(a="2026-01-01", b="2026-12-31"):
    days = np.arange(np.datetime64(a), np.datetime64(b) + 1)
    ok = np.is_busday(days, holidays=HOLIDAYS_2026)
    return [str(d) for d in days[ok]]


def report_for(day: str, settles: list[str], cal: list[str], lag: int = PUB_LAG) -> str | None:
    """Latest settlement (YYYY-MM-DD) whose publication (settle + lag sessions) is strictly before day. Pure."""
    import bisect
    best = None
    for s in sorted(settles):
        i = bisect.bisect_left(cal, s) + lag
        if i < len(cal) and cal[i] < day:
            best = s
    return best


def groups(dtc: dict[str, float]) -> dict[str, tuple[str, int]]:
    """{sym: dtc} -> {sym: (HIGH|MID|NORMAL, decile 1-10)}: top decile HIGH, below the 70th percentile NORMAL. Pure."""
    syms = sorted(s for s, v in dtc.items() if v is not None and np.isfinite(v))
    if len(syms) < 10:
        return {}
    v = np.array([dtc[s] for s in syms])
    p70, p90 = np.quantile(v, 0.70), np.quantile(v, 0.90)
    out = {}
    for s, x in zip(syms, v):
        dec = int(min(10, 1 + np.searchsorted(np.quantile(v, np.linspace(0.1, 0.9, 9)), x, side="right")))
        out[s] = ("HIGH" if x >= p90 else ("NORMAL" if x < p70 else "MID"), dec)
    return out


def breakout(o, h, l, c, v, pc, prev_vol):
    """First bar t in 30..330: close above every earlier high, >= +3% vs prior close, cumulative volume >= 1.5x the
    prior day's volume x the elapsed share of the session, price $20-$400. Pure."""
    if not (pc > 0 and prev_vol > 0):
        return None
    hh = np.where(np.isfinite(h), h, -np.inf)
    run_max = np.maximum.accumulate(hh)
    cv = np.cumsum(np.where(np.isfinite(v), v, 0.0))
    for t in range(T_LO, T_HI + 1):
        px = c[t]
        if not np.isfinite(px) or not (20 <= px <= 400):
            continue
        if px > run_max[t - 1] and (px / pc - 1) >= 0.03 and cv[t] >= 1.5 * prev_vol * (t + 1) / 390:
            return t
    return None


def events():
    from relative_strength_scan import load_day
    from board_structure_family import universe
    from evening_family import ALL_ETF, WH_SIP
    from holistic_entry import bracket, COST
    from ticker_filters import is_levered_etp
    from low_heat_rising import hold_exit
    import short_squeeze as SQ
    import news_source as ns
    si = SQ.load_reports_raw()
    settles = sorted(f"{d[:4]}-{d[4:6]}-{d[6:]}" for d in si)
    cal = calendar()
    rows, prev, rep_used = [], None, {}
    for fp in sorted(WH_SIP.glob("20??-??-??.npz")):
        day = load_day(fp)
        if prev is None or not (DECIDE[0] <= fp.stem <= CONFIRM[1]) or day["idx"].get("SPY") is None:
            prev = day
            continue
        rep = report_for(fp.stem, settles, cal)
        if rep is None:
            prev = day
            continue
        rep_used[fp.stem] = rep
        r_rows = si[rep.replace("-", "")]
        uni = [s for s in universe(day, prev, set(ALL_ETF)) if not is_levered_etp(s)]
        dtc, pcs = {}, {}
        for s in uni:
            j = prev["idx"].get(s)
            if j is None:
                continue
            pc = ns.last_close(prev["c"][j])
            rr = r_rows.get(s)
            if rr is None or rr.get("flagged") or not (20 <= pc <= 400):
                continue
            dtc[s], pcs[s] = rr.get("dtc"), pc
        g = groups(dtc)
        for s, (grp, dec) in g.items():
            i, j = day["idx"][s], prev["idx"][s]
            o, h, l, c, v = (day[k][i] for k in "ohlcv")
            t = breakout(o, h, l, c, v, pcs[s], float(np.nansum(prev["v"][j])))
            if t is None:
                continue
            b, hx = bracket(o, h, l, c, t), hold_exit(o, l, c, t)
            if b is None or hx is None:
                continue
            g30 = None
            if t + 30 < 390 and np.isfinite(c[t + 30]) and np.isfinite(o[t + 1]) and o[t + 1] > 0:
                g30 = round(float((c[t + 30] / o[t + 1] - 1) * 1e4), 2)
            rows.append({"day": fp.stem, "sym": s, "t": int(t), "group": grp, "decile": dec, "dtc": dtc[s],
                         "si_chg": r_rows[s].get("chg"), "report": rep, "hold": round(hx, 2),
                         "desk": round(float(b) + COST, 2), "g30": g30})
        print(fp.stem, rep, len(g), len(rows), flush=True)
        prev = day
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "events.json").write_text(json.dumps(rows))
    (OUT / "reports_used.json").write_text(json.dumps(rep_used, indent=1))


def cluster_se(vals, days):
    """Day-clustered SE of a mean. Pure."""
    n = len(vals)
    if n < 3:
        return None
    x = np.array(vals, float)
    m = x.mean()
    s = defaultdict(float)
    for xi, d in zip(x, days):
        s[d] += xi - m
    g = len(s)
    if g < 3:
        return None
    return float(math.sqrt(sum(v * v for v in s.values()) * g / (g - 1)) / n)


def diff_stats(hi, no, key="net"):
    """HIGH-minus-NORMAL mean, day-clustered SE and t. Pure."""
    if len(hi) < 3 or len(no) < 3:
        return {"diff": None, "se": None, "t": None}
    mh, mn = np.mean([r[key] for r in hi]), np.mean([r[key] for r in no])
    sh, sn = cluster_se([r[key] for r in hi], [r["day"] for r in hi]), cluster_se([r[key] for r in no], [r["day"] for r in no])
    if sh is None or sn is None:
        return {"diff": round(float(mh - mn), 2), "se": None, "t": None}
    se = math.sqrt(sh * sh + sn * sn)
    return {"diff": round(float(mh - mn), 2), "se": round(se, 2), "t": round(float((mh - mn) / se), 2) if se > 0 else None}


def sample(rows):
    """Every HIGH row; NORMAL capped per period by the lowest sha256('sqz'|day|sym); MID not quoted. Pure."""
    out, by = [], defaultdict(list)
    for r in rows:
        if r["group"] == "HIGH":
            out.append(r)
        elif r["group"] == "NORMAL":
            by[r["day"] <= DECIDE[1]].append(r)
    for v in by.values():
        out += sorted(v, key=lambda r: hashlib.sha256(f"sqz|{r['day']}|{r['sym']}".encode()).hexdigest())[:NORMAL_CAP]
    return out


def count():
    """Count step (amended_1): counts per group/period and a placebo smallest detectable effect - group labels are
    shuffled WITHIN each day over the pooled events and the day-clustered SE of the label difference is taken. Only that
    SE is printed; no HIGH-vs-NORMAL comparison is computed or shown."""
    rows = json.loads((OUT / "events.json").read_text())
    per = lambda r: "decide" if r["day"] <= DECIDE[1] else "confirm"  # noqa: E731
    cnt = defaultdict(int)
    for r in sample(rows):
        cnt[f"{r['group']}|{per(r)}"] += 1
    dec = [r for r in sample(rows) if per(r) == "decide"]
    rng = np.random.default_rng(int(hashlib.sha256(b"sqz-placebo").hexdigest()[:16], 16))
    ses = []
    for _ in range(200):
        by = defaultdict(list)
        for r in dec:
            by[r["day"]].append(r)
        hi, no = [], []
        for d, v in by.items():
            labels = [r["group"] for r in v]
            rng.shuffle(labels)
            for r, g in zip(v, labels):
                (hi if g == "HIGH" else no).append({"day": d, "net": r["hold"]})
        s = diff_stats(hi, no)["se"]
        if s:
            ses.append(s)
    se = float(np.median(ses)) if ses else None
    mde = round(2.8 * se, 1) if se else None
    status = "UNDERPOWERED" if (cnt.get("HIGH|decide", 0) < 300 or mde is None or mde > 20) else "POWERED"
    out = {"counts": dict(cnt), "all_events": len(rows), "placebo_se": se, "mde_bp": mde, "status": status}
    (OUT / "count.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))
    return out


def plan(which="all"):
    return sorted({("sqz", r["day"], r["sym"], r["t"], 0.0) for r in sample(json.loads((OUT / "events.json").read_text()))})


def score():
    import news_source as ns
    c = json.loads((OUT / "count.json").read_text())
    if c["status"] != "POWERED":
        print("UNDERPOWERED - not scored", c)
        return c
    q = json.load(open(ns.QUOTES)) if ns.QUOTES.exists() else {}
    rows = sample(json.loads((OUT / "events.json").read_text()))
    traded, noq = defaultdict(list), defaultdict(int)
    for r in rows:
        per = "decide" if r["day"] <= DECIDE[1] else "confirm"
        sp = q.get(ns.qkey(r["day"], r["sym"], r["t"]))
        if sp is None:
            noq[f"{r['group']}|{per}"] += 1
            continue
        traded[(r["group"], per)].append({**r, "net": r["hold"] - sp, "net_desk": r["desk"] - sp,
                                          "net30": None if r["g30"] is None else r["g30"] - sp, "spread": sp})
    res = {}
    for per in ("decide", "confirm"):
        hi, no = traded[("HIGH", per)], traded[("NORMAL", per)]
        res[per] = {"HIGH_net": round(float(np.mean([r["net"] for r in hi])), 2) if hi else None,
                    "HIGH_t": None, "n_high": len(hi), "n_normal": len(no), **diff_stats(hi, no)}
        se = cluster_se([r["net"] for r in hi], [r["day"] for r in hi])
        if se:
            res[per]["HIGH_t"] = round(res[per]["HIGH_net"] / se, 2)
        res[per]["NORMAL_net"] = round(float(np.mean([r["net"] for r in no])), 2) if no else None
    dec_hi, dec_no = traded[("HIGH", "decide")], traded[("NORMAL", "decide")]
    days = sorted({r["day"] for r in dec_hi + dec_no})
    h = days[len(days) // 2] if days else ""
    halves = []
    for a, b in ((lambda d: d < h), (lambda d: d >= h)):
        hh, nn = [r for r in dec_hi if a(r["day"])], [r for r in dec_no if a(r["day"])]
        halves.append({"HIGH_net": round(float(np.mean([r["net"] for r in hh])), 2) if hh else None, **diff_stats(hh, nn)})
    contrib = defaultdict(float)
    for r in dec_hi:
        contrib[r["sym"]] += r["net"]
    top5 = set(sorted(contrib, key=lambda s: -contrib[s])[:5])
    drop = diff_stats([r for r in dec_hi if r["sym"] not in top5], dec_no)
    d, cf = res["decide"], res["confirm"]
    ok = ((d["HIGH_net"] or -99) >= EDGE and (d["diff"] or -99) >= EDGE and (d["t"] or -99) >= 2.5
          and all((x["HIGH_net"] or -1) > 0 and (x["diff"] or -1) > 0 for x in halves)
          and (drop["diff"] or -1) > 0 and (cf["HIGH_net"] or -1) > 0 and (cf["diff"] or -1) > 0)
    info = {}
    for key in ("net_desk", "net30"):
        hh = [r for r in dec_hi if r[key] is not None]
        nn = [r for r in dec_no if r[key] is not None]
        info[key] = {"HIGH": round(float(np.mean([r[key] for r in hh])), 2) if hh else None, **diff_stats(hh, nn, key)}
    allrows = json.loads((OUT / "events.json").read_text())
    info["gross_hold_by_decile_decide"] = {k: round(float(np.mean(v)), 2) for k, v in sorted(
        {dk: [r["hold"] for r in allrows if r["decile"] == dk and r["day"] <= DECIDE[1]] for dk in range(1, 11)}.items()) if v}
    rising = [r for r in dec_hi if (r["si_chg"] or 0) > 0]
    falling = [r for r in dec_hi if (r["si_chg"] or 0) <= 0]
    info["HIGH_si_rising_vs_falling"] = [round(float(np.mean([r["net"] for r in x])), 2) if x else None for x in (rising, falling)]
    info["median_spread_HIGH"] = round(float(np.median([r["spread"] for r in dec_hi])), 2) if dec_hi else None
    out = {"VERDICT": ["PASS" if ok else "FAIL",
                       f"HIGH net {d['HIGH_net']} (need +{EDGE}); HIGH-NORMAL {d['diff']} t {d['t']} (need +{EDGE}, 2.5); "
                       f"halves {halves}; drop top-5 names {drop['diff']}; confirm HIGH {cf['HIGH_net']} diff {cf['diff']}"],
           "decide": d, "confirm": cf, "halves": halves, "drop_top5": drop, "info": info, "noquote": dict(noq), "count": c}
    (OUT / "score.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))
    return out


if __name__ == "__main__":
    {"events": events, "count": count, "score": score}[sys.argv[1]]()
