#!/usr/bin/env python3
"""Post-earnings drift, exactly per docs/studies/earnings_drift_prereg.json (7ed491c / 218199a).

Phases (on the mini, AFTER HOURS ONLY: the news fetch shares the desk's Alpaca budget):
  symbols   eligible names from the lh_cache panel + prior tickers (Alpaca name changes)  -> WORK/symbols.json
  news      Benzinga headlines per symbol (+ prior tickers), paged, cached, resumable      -> WORK/news/SYM.json
  events    parse EPS headlines into events (rules below)                                  -> WORK/events.json
  minutes   SIP 1-min 15:45 closes for D0s and control candidates (cached)                 -> WORK/m1545.json
  score     controls, returns, statistics, verdict                                        -> WORK/result.json, report.md

Rules (from the pre-registration):
  universe   panel names with RAW close >= $10 and ADV20 >= $50M, ADV20 = mean(close x volume) over the 20 sessions
             ending D0-1 (shifted), point-in-time; D0 in 2019-01-02..2026-09-18
  headline   not starting CORRECTION/UPDATE; matches \\bQ[1-4]\\b.*\\bEPS\\b and 'Estimate'; no Sees|Guidance|Outlook|
             Raises|Lowers before 'EPS'; SURPRISE = first Beats|Misses|In-Line|Inline inside the EPS clause ('EPS' up
             to the first comma), else unclassified; first qualifying headline per symbol per 10 calendar days (by
             created_at); headlines within 5 min with different surprise words: the one with 'Adj.' wins, else
             unclassified
  D0         ET time < 09:30 -> that session (next session if not a session); >= 16:00 -> next session; RTH -> same
  R0         SIP 1-min close at 15:45 ET on D0 (bar starting 15:44; else the last bar starting 15:35-15:44) / RAW close(D0-1) - 1
  trade      buy close(D0), sell close(D0+5); excess = r - beta x r_SPY; beta = OLS daily vs SPY over the 252 sessions
             ending D0-1 (min 150); cost 10 bp (30 bp sensitivity) charged to the PRIMARY trade only
  PRIMARY    EPS Beats AND R0 > 0
  CONTROL    same symbol, a random session (seed 41) within +/-60 sessions of D0, not within 10 sessions of any of that
             symbol's events, with its own 15:45 return > 0; same entry/hold/beta; candidates tried in a seeded order
  statistic  diff = PRIMARY net excess (cost c) - CONTROL gross excess (the control is a comparison, not a trade);
             SE (a) clustered by ISO week of D0, (b) calendar-time: daily mean of open PRIMARY daily excess and of open
             CONTROL daily excess, Newey-West lag 5, SE of the difference = sqrt(se1^2 + se2^2); the smaller |t| is used
  pass       diff >= +20 bp at 10 bp with t >= 2 in BOTH halves (H1 2019-2022, H2 2023-2026-09-18), >= 300 events per
             half, diff > 0 at 30 bp, > 0 after dropping the top 3 weeks and the top 5 names; power: MDE = 2.84 x SE
             (week-clustered) <= 30 bp per half, else UNDERPOWERED
"""
from __future__ import annotations

import collections
import json
import math
import os
import random
import re
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

REPO = os.environ.get("REPO") or os.path.expanduser("~/repo/trading-helper")
sys.path.insert(0, REPO)
WORK = os.environ.get("WORK") or os.path.join(REPO, "ai_reports", "earnings_drift")
PANEL = os.path.expanduser("~/lh_cache/panel.npz")
ET = ZoneInfo("America/New_York")
D_LO, D_HI = "2019-01-02", "2026-09-18"
H1_END = "2022-12-30"
HOLD, MIN_PX, MIN_ADV = 5, 10.0, 50e6
NEWS_START, NEWS_END = "2018-12-15", "2026-09-20"
RPM = float(os.environ.get("ALPACA_RPM", "150"))
RX_HEAD = re.compile(r"\bQ[1-4]\b.*\bEPS\b", re.I)
RX_EXCL = re.compile(r"\b(Sees|Guidance|Outlook|Raises|Lowers)\b", re.I)
RX_SURP = re.compile(r"\b(Beats|Misses|In-Line|Inline)\b", re.I)


def P_(*a):
    print(*a, flush=True)


def jload(p, d=None):
    try:
        return json.load(open(p))
    except Exception:  # noqa: BLE001
        return d


def jsave(p, obj):
    tmp = p + ".tmp"
    json.dump(obj, open(tmp, "w"))
    os.replace(tmp, p)


def headers():
    from config import load_config
    c = load_config() or {}
    return {"APCA-API-KEY-ID": c.get("api_key"), "APCA-API-SECRET-KEY": c.get("secret_key")}


_last = [0.0]


def get(url, params, H):
    for att in range(6):
        w = _last[0] + 60.0 / RPM - time.monotonic()
        if w > 0:
            time.sleep(w)
        _last[0] = time.monotonic()
        try:
            r = requests.get(url, params=params, headers=H, timeout=60)
            if r.status_code == 429:
                time.sleep(15 * (att + 1))
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            P_(f"  get fail try {att}: {str(e)[:100]}")
            time.sleep(5 * (att + 1))
    return None


# ------------------------------------------------------------------ panel
def panel():
    z = np.load(PANEL, allow_pickle=False)
    P = {k: z[k] for k in ("c", "v", "rc", "spy_c", "syms", "status")}
    P["dates"] = [str(d)[:10] for d in z["dates"]]
    dv = P["c"] * P["v"]
    P["adv_prev"] = pd.DataFrame(dv).rolling(20, min_periods=15).mean().shift(1).values   # 20 sessions ending t-1
    P["ix"] = {d: i for i, d in enumerate(P["dates"])}
    P["col"] = {str(s): j for j, s in enumerate(P["syms"])}
    return P


def eligible(P, t, j):
    rc = P["rc"][t - 1, j] if t >= 1 else np.nan
    adv = P["adv_prev"][t, j]
    return bool(np.isfinite(rc) and rc >= MIN_PX and np.isfinite(adv) and adv >= MIN_ADV)


# ------------------------------------------------------------------ phases
def phase_symbols():
    P = panel()
    lo, hi = P["ix"].get(D_LO), P["ix"].get(D_HI)
    rc, adv = P["rc"], P["adv_prev"]
    ok = (np.roll(rc, 1, 0) >= MIN_PX) & (adv >= MIN_ADV)
    ok[0] = False
    syms = sorted(str(P["syms"][j]) for j in np.where(ok[lo:hi + 1].any(0))[0])
    H = headers()
    prior = {}
    for i in range(0, len(syms), 100):
        js = get("https://data.alpaca.markets/v1/corporate-actions",
                 {"symbols": ",".join(syms[i:i + 100]), "types": "name_change", "start": "2016-01-01", "end": NEWS_END,
                  "limit": 1000}, H)
        for nc in ((js or {}).get("corporate_actions") or {}).get("name_changes", []) or []:
            if nc.get("new_symbol") in syms:
                prior.setdefault(nc["new_symbol"], []).append({"old": nc["old_symbol"], "until": nc["process_date"]})
    os.makedirs(WORK, exist_ok=True)
    jsave(os.path.join(WORK, "symbols.json"), {"symbols": syms, "prior": prior})
    P_(f"symbols {len(syms)}; with prior tickers {len(prior)}")


def phase_news():
    S = jload(os.path.join(WORK, "symbols.json"))
    H = headers()
    D = os.path.join(WORK, "news")
    os.makedirs(D, exist_ok=True)
    fails = jload(os.path.join(WORK, "news_fails.json"), {})
    todo = []
    for s in S["symbols"]:
        renames = sorted(S["prior"].get(s, []), key=lambda x: x["until"])
        own_from = renames[-1]["until"] if renames else NEWS_START          # the ticker is ours only after the rename
        todo.append((s, s, own_from, NEWS_END))
        prev = NEWS_START
        for r in renames:
            todo.append((s, r["old"], prev, r["until"]))
            prev = r["until"]
    n = 0
    for owner, tick, a, b in todo:
        fp = os.path.join(D, f"{owner}__{tick}.json")
        if os.path.exists(fp) or a >= b:
            continue
        rows, token, ok = [], None, True
        while True:
            params = {"symbols": tick, "start": a, "end": b, "limit": 50, "sort": "asc", "include_content": "false"}
            if token:
                params["page_token"] = token
            js = get("https://data.alpaca.markets/v1beta1/news", params, H)
            if js is None:
                ok = False
                break
            for x in js.get("news", []):
                rows.append({"h": x.get("headline", ""), "c": x.get("created_at"), "id": x.get("id")})
            token = js.get("next_page_token")
            if not token:
                break
        if not ok:
            fails[f"{owner}__{tick}"] = time.time()
            jsave(os.path.join(WORK, "news_fails.json"), fails)
            continue
        fails.pop(f"{owner}__{tick}", None)
        jsave(fp, rows)
        n += 1
        if n % 25 == 0:
            P_(f"  news {n} files; fails {len(fails)}")
    jsave(os.path.join(WORK, "news_fails.json"), fails)
    P_(f"news done: new files {n}, failed {len(fails)} of {len(todo)} fetch units")


def surprise(h):
    m = re.search(r"\bEPS\b", h, re.I)
    if not m or RX_EXCL.search(h[:m.start()]):
        return None
    clause = h[m.start():].split(",")[0]
    s = RX_SURP.search(clause)
    if not s:
        return "unclassified"
    w = s.group(1).lower()
    return "beat" if w == "beats" else "miss" if w == "misses" else "inline"


def phase_events():
    P = panel()
    S = jload(os.path.join(WORK, "symbols.json"))
    sessions = P["dates"]
    sset = set(sessions)
    ev, cnt = [], collections.Counter()
    for s in S["symbols"]:
        arts = []
        for fn in os.listdir(os.path.join(WORK, "news")):
            if fn.startswith(s + "__"):
                arts += jload(os.path.join(WORK, "news", fn), [])
        cand = []
        for a in arts:
            h = a.get("h") or ""
            if h.upper().startswith(("CORRECTION", "UPDATE")) or not RX_HEAD.search(h) or "estimate" not in h.lower():
                continue
            sp = surprise(h)
            if sp is None:
                continue
            t = datetime.fromisoformat(a["c"].replace("Z", "+00:00")).astimezone(ET)
            cand.append((t, h, sp))
        cand.sort()
        last = None
        i = 0
        while i < len(cand):
            t0, h0, sp0 = cand[i]
            if last is not None and (t0 - last).days < 10:
                cnt["dedup_10d"] += 1
                i += 1
                continue
            burst = [c for c in cand[i:] if (c[0] - t0).total_seconds() <= 300]
            words = {c[2] for c in burst}
            sp = sp0
            if len(words) > 1:
                adj = [c for c in burst if "adj." in c[1].lower()]
                sp = adj[0][2] if adj and len({c[2] for c in adj}) == 1 else "unclassified"
                cnt["burst_conflict"] += 1
            last = t0
            i += len(burst)
            d = t0.strftime("%Y-%m-%d")
            mins = t0.hour * 60 + t0.minute
            if mins >= 16 * 60 or d not in sset:
                d = next((x for x in sessions if x > d), None) if mins >= 16 * 60 else next((x for x in sessions if x >= d), None)
            elif mins < 9 * 60 + 30:
                pass
            if d is None or not (D_LO <= d <= D_HI):
                cnt["outside_period"] += 1
                continue
            t = P["ix"][d]
            j = P["col"].get(s)
            if j is None or not eligible(P, t, j):
                cnt["not_eligible"] += 1
                continue
            ev.append({"sym": s, "d0": d, "t0": t, "surprise": sp, "headline": h0, "ts": t0.isoformat()})
            cnt[f"surprise_{sp}"] += 1
    jsave(os.path.join(WORK, "events.json"), {"events": ev, "counts": dict(cnt)})
    P_(f"events {len(ev)}; {dict(cnt)}")


def m1545(cache, H, sym, day):
    k = f"{sym}|{day}"
    if k in cache:
        return cache[k]
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
    js = get("https://data.alpaca.markets/v2/stocks/bars",
             {"symbols": sym, "timeframe": "1Min", "start": d.replace(hour=15, minute=35).astimezone(timezone.utc).isoformat(),
              "end": d.replace(hour=15, minute=45).astimezone(timezone.utc).isoformat(), "feed": "sip", "adjustment": "raw",
              "limit": 100}, H)
    if js is None:
        return None                                   # not cached: retried on the next run
    bars = (js.get("bars") or {}).get(sym) or []
    best = None
    for b in bars:
        tl = datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(ET)
        if tl.hour * 60 + tl.minute <= 15 * 60 + 44:
            best = float(b["c"])
    cache[k] = best
    return best


def excess_at(c, spy, spy_r, dates, j, t, hold):
    """Beta-adjusted excess of column j bought at close t, sold at close t+hold (bp), or None.
    Skeptic F3 (2026-10-06): every close t..t+hold must be finite, else a NaN mid-hold poisoned the daily series
    (and with it the calendar-time mean) while the endpoint return still looked fine."""
    T = len(dates)
    if t + hold >= T or t < 160:
        return None
    path = c[t:t + hold + 1, j]
    if not (np.isfinite(path).all() and path[0] > 0 and np.isfinite(spy[t:t + hold + 1]).all()):
        return None
    a, b = path[0], path[-1]
    sr = c[max(0, t - 252):t, j]
    rr = sr[1:] / sr[:-1] - 1
    mr = spy_r[max(0, t - 252) + 1:t]
    ok = np.isfinite(rr) & np.isfinite(mr)
    if ok.sum() < 150:
        return None
    beta = float(np.polyfit(mr[ok], rr[ok], 1)[0])
    r_s = b / a - 1
    r_m = spy[t + hold] / spy[t] - 1
    daily = [(c[t + k, j] / c[t + k - 1, j] - 1) - beta * spy_r[t + k] for k in range(1, hold + 1)]
    return {"beta": beta, "x": (r_s - beta * r_m) * 1e4, "raw_vs_spy": (r_s - r_m) * 1e4,
            "daily": [d * 1e4 for d in daily], "dates": [dates[t + k] for k in range(1, hold + 1)]}


def phase_minutes_and_score():
    P = panel()
    E = jload(os.path.join(WORK, "events.json"))
    H = headers()
    cp = os.path.join(WORK, "m1545.json")
    cache = jload(cp, {})
    rc, c, spy = P["rc"], P["c"], P["spy_c"]
    T = len(P["dates"])
    spy_r = np.r_[np.nan, spy[1:] / spy[:-1] - 1]
    ev_by = collections.defaultdict(list)
    for e in E["events"]:
        ev_by[e["sym"]].append(e["t0"])
    rng_master = random.Random(41)

    def r0(sym, t):
        j = P["col"][sym]
        m = m1545(cache, H, sym, P["dates"][t])
        prev = rc[t - 1, j]
        return None if (m is None or not np.isfinite(prev) or prev <= 0) else m / prev - 1

    def excess(sym, t, hold):
        return excess_at(c, spy, spy_r, P["dates"], P["col"][sym], t, hold)

    rows, cnt = [], collections.Counter()
    for n, e in enumerate(E["events"]):
        rr = r0(e["sym"], e["t0"])
        if rr is None:
            cnt["no_r0"] += 1
            continue
        x5 = excess(e["sym"], e["t0"], HOLD)
        if x5 is None:
            cnt["no_return"] += 1
            continue
        row = {**e, "r0": rr, "x5": x5["x"], "raw5": x5["raw_vs_spy"], "daily": x5["daily"], "ddates": x5["dates"]}
        for hk, hh in (("x1", 1), ("x20", 20)):
            xx = excess(e["sym"], e["t0"], hh)
            row[hk] = None if xx is None else xx["x"]
        row["primary"] = (e["surprise"] == "beat" and rr > 0)
        if row["primary"]:
            rng = random.Random(rng_master.random())
            near = set()
            for t_e in ev_by[e["sym"]]:
                near |= set(range(t_e - 10, t_e + 11))
            cands = [t for t in range(e["t0"] - 60, e["t0"] + 61) if t not in near and 160 <= t < T - HOLD - 1]
            rng.shuffle(cands)
            ctl = None
            for t in cands[:12]:
                cr = r0(e["sym"], t)
                if cr is not None and cr > 0:
                    cx = excess(e["sym"], t, HOLD)
                    if cx is not None:
                        ctl = {"t": t, "x5": cx["x"], "daily": cx["daily"], "ddates": cx["dates"]}
                        break
            if ctl is None:
                cnt["no_control"] += 1
            row["ctl"] = ctl
        rows.append(row)
        if n % 200 == 0:
            jsave(cp, cache)
            P_(f"  scored {n}/{len(E['events'])}")
    jsave(cp, cache)
    score(P, rows, cnt, E.get("counts", {}))


def week_t(vals, weeks):
    m = statistics.fmean(vals)
    by = collections.defaultdict(float)
    for v, w in zip(vals, weeks):
        by[w] += v - m
    G, N = len(by), len(vals)
    se = math.sqrt(sum(s * s for s in by.values()) * G / (G - 1)) / N if G > 1 else float("nan")
    return m, se


def nw_mean_se(series, L=5):
    x = np.asarray(series, float)
    n = len(x)
    if n < 10:
        return float("nan"), float("nan")
    m = x.mean()
    u = x - m
    s = (u @ u) / n
    for l in range(1, L + 1):
        s += 2 * (1 - l / (L + 1)) * (u[l:] @ u[:-l]) / n
    return float(m), float(math.sqrt(max(s, 0) / n))


def t_used(t_week, t_cal):
    """The t the pass rule reads: the signed minimum of the two, NaN if either is NaN (a NaN fails t >= 2).
    Skeptic F2 (2026-10-06): the old min(|t|) x sign(mean) let t_week=+3, t_cal=-2.5 read as +2.5, a pass."""
    if not (t_week == t_week and t_cal == t_cal):
        return float("nan")
    return min(t_week, t_cal)


def cal_series(rows, key_daily, key_dates, cost_bp=0.0):
    by = collections.defaultdict(list)
    for r in rows:
        dl, dd = r[key_daily], r[key_dates]
        for k, (v, d) in enumerate(zip(dl, dd)):
            by[d].append(v - (cost_bp if k == 0 else 0.0))       # cost booked on the first day of the hold
    return [statistics.fmean(by[d]) for d in sorted(by)]


def score(P, rows, cnt, ecnt):
    status = {str(s): str(st) for s, st in zip(P["syms"], P["status"])}
    res = {"prereg": "docs/studies/earnings_drift_prereg.json (7ed491c, 218199a)", "event_counts": ecnt, "score_counts": dict(cnt)}
    halves = {"H1": lambda d: d <= H1_END, "H2": lambda d: d > H1_END}
    out = {}
    for hn, f in halves.items():
        pr = [r for r in rows if r["primary"] and r.get("ctl") and f(r["d0"])]
        if not pr:
            out[hn] = {"n": 0}
            continue
        wk = [datetime.strptime(r["d0"], "%Y-%m-%d").strftime("%G-%V") for r in pr]
        o = {"n": len(pr)}
        for cost in (10.0, 30.0):
            diff = [(r["x5"] - cost) - r["ctl"]["x5"] for r in pr]
            m, se = week_t(diff, wk)
            pm, pse = nw_mean_se(cal_series(pr, "daily", "ddates", cost))
            cm, cse = nw_mean_se(cal_series([{"daily": r["ctl"]["daily"], "ddates": r["ctl"]["ddates"]} for r in pr], "daily", "ddates"))
            t_week = m / se if se and se == se else float("nan")
            t_cal = (pm - cm) / math.sqrt(pse ** 2 + cse ** 2) if pse == pse and cse == cse else float("nan")
            o[f"cost{int(cost)}"] = {"diff_bp": round(m, 2), "se_week": round(se, 2), "t_week": round(t_week, 2),
                                     "cal_daily_diff_bp": round(pm - cm, 3), "t_cal": round(t_cal, 2),
                                     "t_used": round(t_used(t_week, t_cal), 2),
                                     "mde_bp": round(2.84 * se, 2)}
        diff10 = [(r["x5"] - 10) - r["ctl"]["x5"] for r in pr]
        wsum = collections.defaultdict(float)
        nsum = collections.defaultdict(float)
        for v, w, r in zip(diff10, wk, pr):
            wsum[w] += v
            nsum[r["sym"]] += v
        tw = set(sorted(wsum, key=lambda w: -wsum[w])[:3])
        tn = set(sorted(nsum, key=lambda s: -nsum[s])[:5])
        o["drop_top3_weeks_mean"] = round(statistics.fmean([v for v, w in zip(diff10, wk) if w not in tw]), 2)
        o["drop_top5_names_mean"] = round(statistics.fmean([v for v, r in zip(diff10, pr) if r["sym"] not in tn]), 2)
        o["primary_vs_spy_net10_mean"] = round(statistics.fmean([r["x5"] - 10 for r in pr]), 2)
        out[hn] = o
    res["primary"] = out
    pw = all(out[h].get("cost10", {}).get("mde_bp", 1e9) <= 30 for h in out)
    crit = {}
    for h in ("H1", "H2"):
        o = out[h]
        if o.get("n", 0) == 0:
            crit[h] = {"n>=300": False}
            continue
        c10, c30 = o["cost10"], o["cost30"]
        crit[h] = {"n>=300": o["n"] >= 300, "diff>=20": c10["diff_bp"] >= 20, "t>=2": c10["t_used"] >= 2,
                   "cost30>0": c30["diff_bp"] > 0, "drop_weeks>0": o["drop_top3_weeks_mean"] > 0,
                   "drop_names>0": o["drop_top5_names_mean"] > 0}
    res["power_ok"] = pw
    res["criteria"] = crit
    res["verdict"] = ("UNDERPOWERED: no verdict" if not pw else
                      "PASS" if all(all(v.values()) for v in crit.values()) else "FAIL")
    info = {}

    def grp(name, sel, key="x5"):
        for hn, f in halves.items():
            xs = [r[key] for r in rows if sel(r) and r.get(key) is not None and f(r["d0"])]
            if len(xs) > 5:
                wk = [datetime.strptime(r["d0"], "%Y-%m-%d").strftime("%G-%V") for r in rows
                      if sel(r) and r.get(key) is not None and f(r["d0"])]
                m, se = week_t(xs, wk)
                info[f"{name} {hn}"] = {"n": len(xs), "mean_bp_gross": round(m, 2), "t_week": round(m / se, 2) if se == se and se else None}
    grp("Misses & R0<0 (5d)", lambda r: r["surprise"] == "miss" and r["r0"] < 0)
    grp("all Beats (5d)", lambda r: r["surprise"] == "beat")
    grp("R0>0 any surprise (5d)", lambda r: r["r0"] > 0)
    grp("PRIMARY 20d", lambda r: r["primary"], "x20")
    grp("PRIMARY 1d", lambda r: r["primary"], "x1")
    grp("PRIMARY raw vs SPY 5d", lambda r: r["primary"], "raw5")
    grp("PRIMARY H1-style, active names only (5d)", lambda r: r["primary"] and status.get(r["sym"], "").lower() == "active")
    grp("PRIMARY inactive names only (5d)", lambda r: r["primary"] and status.get(r["sym"], "").lower() != "active")
    yrs = collections.Counter(r["d0"][:4] for r in rows)
    info["events_per_year"] = dict(sorted(yrs.items()))
    info["unclassified_per_year"] = dict(sorted(collections.Counter(r["d0"][:4] for r in rows if r["surprise"] == "unclassified").items()))
    res["information"] = info
    jsave(os.path.join(WORK, "result.json"), res)
    L = ["# Earnings drift (prereg docs/studies/earnings_drift_prereg.json)", "", f"**VERDICT: {res['verdict']}**", "",
         "```", json.dumps({k: v for k, v in res.items() if k != "information"}, indent=1), "```", "", "## Information", "```",
         json.dumps(info, indent=1), "```"]
    open(os.path.join(WORK, "report.md"), "w").write("\n".join(L) + "\n")
    P_("\n".join(L))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    {"symbols": phase_symbols, "news": phase_news, "events": phase_events,
     "score": phase_minutes_and_score}.get(cmd, lambda: P_(__doc__))()
