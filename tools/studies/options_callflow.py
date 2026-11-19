#!/usr/bin/env python3
"""options_callflow - do intraday bursts of short-dated call premium predict the stock? Options primary, %R secondary.
Pass bar: docs/studies/options_callflow_intraday_prereg.json. Read-only. Run on the mini (data account).

    .venv/bin/python tools/studies/options_callflow.py plan            # universe + days -> plan.json
    .venv/bin/python tools/studies/options_callflow.py fetch [i n]     # option minute premium per stock-day (worker i of n)
    .venv/bin/python tools/studies/options_callflow.py events          # signals + stock outcomes -> events.jsonl
    .venv/bin/python tools/studies/options_callflow.py quotes          # entry spreads (shared cache)
    .venv/bin/python tools/studies/options_callflow.py score           # -> result.json
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))

ET = ZoneInfo("America/New_York")
OUT = ROOT / "ai_reports" / "options_callflow"
FLOW = OUT / "flow"
N_NAMES, N_DAYS, BASE_DAYS, DTE_MAX, STRIKE_BAND = 40, 80, 5, 14, 0.06
MULT, RATIO, MIN_PREM, REFRACT, T_FIRST, T_LAST, EXIT_M = 5.0, 3.0, 50_000.0, 30, 15, 330, 385
LOOSE_WIN, GATE, T_CRIT = 15, 6.0, 2.5
PAPER, DATA = "https://paper-api.alpaca.markets", "https://data.alpaca.markets"


def premium_series(bars_by_contract, types):
    """{occ: [bar]} + {occ: 'call'|'put'} -> (C[390], P[390]) premium $ per minute since 09:30. Pure."""
    C, P = np.zeros(390), np.zeros(390)
    for occ, bars in bars_by_contract.items():
        tgt = C if types.get(occ) == "call" else P
        for b in bars:
            t = datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(ET)
            m = t.hour * 60 + t.minute - 570
            if 0 <= m < 390:
                tgt[m] += float(b.get("v") or 0) * float(b.get("vw") or b.get("c") or 0) * 100
    return C, P


def signals(C, P, base_c, mult=MULT, ratio=RATIO, min_prem=MIN_PREM):
    """Minutes t (15..330) where the 15-minute call premium bursts; refractory 30. Pure."""
    out, last = [], -10 ** 9
    if not base_c or base_c <= 0:
        return out
    for t in range(T_FIRST, T_LAST + 1):
        c15, p15 = float(C[t - 14:t + 1].sum()), float(P[t - 14:t + 1].sum())
        if c15 >= mult * base_c and c15 >= ratio * p15 and c15 >= min_prem and t - last >= REFRACT:
            out.append(t)
            last = t
    return out


def utc_iso(day, hh, mm):
    """ET wall clock on `day` -> ISO UTC 'Z' string (handles DST). Pure."""
    from datetime import timezone
    t = datetime.fromisoformat(day).replace(hour=hh, minute=mm, tzinfo=ET).astimezone(timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _headers():
    k = json.load(open(os.environ.get("TH_DATA_SECRETS") or ROOT / "secrets_data.json"))
    return {"APCA-API-KEY-ID": k["alpaca_data_api_key"], "APCA-API-SECRET-KEY": k["alpaca_data_secret_key"]}


def plan():
    from relative_strength_scan import load_day
    from evening_family import WH_SIP, ALL_ETF
    import news_source as ns
    files = sorted(WH_SIP.glob("20??-??-??.npz"))
    files = [f for f in files if f.stem <= "2026-11-17"]
    days = [f.stem for f in files[-N_DAYS:]]
    first = files.index(next(f for f in files if f.stem == days[0]))
    dv = defaultdict(list)
    for f in files[max(0, first - 20):first]:
        d = load_day(f)
        tot = np.nansum(d["c"] * d["v"], axis=1)
        for s, i in d["idx"].items():
            dv[s].append(float(tot[i]))
    prev = load_day(files[first - 1])
    ok = []
    for s, v in dv.items():
        j = prev["idx"].get(s)
        if s in set(ALL_ETF) or j is None or not s.isalpha():
            continue
        pc = ns.last_close(prev["c"][j])
        if pc and 20 <= pc <= 400:
            ok.append((float(np.median(v)), s))
    names = [s for _, s in sorted(ok, reverse=True)[:N_NAMES]]
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "plan.json").write_text(json.dumps({"names": names, "days": days}, indent=1))
    print(len(names), names[:12], days[0], days[-1])


def _prev_close(day, sym, cache={}):
    from relative_strength_scan import load_day
    from evening_family import WH_SIP
    import news_source as ns
    if day not in cache:
        files = sorted(WH_SIP.glob("20??-??-??.npz"))
        k = [f.stem for f in files].index(day)
        cache.clear()
        cache[day] = load_day(files[k - 1])
    d = cache[day]
    j = d["idx"].get(sym)
    return ns.last_close(d["c"][j]) if j is not None else None


def fetch():
    import requests
    p = json.loads((OUT / "plan.json").read_text())
    i, n = (int(sys.argv[2]), int(sys.argv[3])) if len(sys.argv) > 3 else (0, 1)
    H = _headers()
    FLOW.mkdir(parents=True, exist_ok=True)
    jobs = [(d, s) for d in p["days"] for s in p["names"]]
    jobs = [j for k, j in enumerate(jobs) if k % n == i]

    def get(url, params):
        for attempt in range(6):
            try:
                r = requests.get(url, headers=H, params=params, timeout=40)
                if r.status_code == 429:
                    time.sleep(8 * (attempt + 1))
                    continue
                if r.status_code == 200:
                    return r.json()
            except requests.RequestException:
                pass
            time.sleep(5 * (attempt + 1))
        return None
    for d, s in jobs:
        out = FLOW / f"{d}_{s}.json"
        if out.exists():
            continue
        pc = _prev_close(d, s)
        if not pc:
            continue
        dd = date.fromisoformat(d)
        cs = []
        params = {"underlying_symbols": s, "expiration_date_gte": d, "expiration_date_lte": (dd + timedelta(days=DTE_MAX)).isoformat(),
                  "strike_price_gte": f"{pc * (1 - STRIKE_BAND):.2f}", "strike_price_lte": f"{pc * (1 + STRIKE_BAND):.2f}", "limit": 1000}
        for status in ("inactive", "active"):
            j = get(f"{PAPER}/v2/options/contracts", {**params, "status": status})
            if j:
                cs += j.get("option_contracts", [])
        types = {c["symbol"]: c["type"] for c in cs}
        bars = defaultdict(list)
        syms = sorted(types)
        for k in range(0, len(syms), 60):
            tok = None
            while True:
                params = {"symbols": ",".join(syms[k:k + 60]), "timeframe": "1Min", "limit": 10000,
                          "start": utc_iso(d, 9, 30), "end": utc_iso(d, 16, 0)}
                if tok:
                    params["page_token"] = tok
                j = get(f"{DATA}/v1beta1/options/bars", params)
                if not j:
                    break
                for occ, b in (j.get("bars") or {}).items():
                    bars[occ] += b
                tok = j.get("next_page_token")
                if not tok:
                    break
        C, P = premium_series(bars, types)
        out.write_text(json.dumps({"contracts": len(types), "C": [round(x, 1) for x in C], "P": [round(x, 1) for x in P]}))
        print(d, s, len(types), int(C.sum()), int(P.sum()), flush=True)


def stock_day(day, sym, cache={}):
    from relative_strength_scan import load_day
    from evening_family import WH_SIP
    if day not in cache:
        cache.clear()
        cache[day] = load_day(WH_SIP / f"{day}.npz")
    d = cache[day]
    i = d["idx"].get(sym)
    return None if i is None else tuple(d[k][i] for k in "ohlc")


def close_exit(o, c, t):
    e = o[t + 1]
    x = c[EXIT_M] if np.isfinite(c[EXIT_M]) else np.nan
    if not (np.isfinite(e) and e > 0 and np.isfinite(x)):
        return None
    return (x / e - 1) * 1e4


def _h(*parts):
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest(), 16)


def events():
    from catalyst_primary import vol_exit, loose_ok
    from bar_signal_family import wr_smoothed
    from evening_family import minute_grid
    from relative_strength_scan import load_day
    from evening_family import WH_SIP
    p = json.loads((OUT / "plan.json").read_text())
    days = p["days"]
    hist_c, hist_p = defaultdict(list), defaultdict(list)
    rows = []
    files = sorted(WH_SIP.glob("20??-??-??.npz"))
    stems = [f.stem for f in files]
    for di, d in enumerate(days):
        prevd = load_day(files[stems.index(d) - 1])
        for s in p["names"]:
            fp = FLOW / f"{d}_{s}.json"
            if not fp.exists():
                continue
            f = json.loads(fp.read_text())
            C, P = np.array(f["C"]), np.array(f["P"])
            bc = np.mean(hist_c[s][-BASE_DAYS:]) / 26 if len(hist_c[s]) >= BASE_DAYS else None
            bp = np.mean(hist_p[s][-BASE_DAYS:]) / 26 if len(hist_p[s]) >= BASE_DAYS else None
            hist_c[s].append(float(C.sum()))
            hist_p[s].append(float(P.sum()))
            if di < BASE_DAYS or not bc:
                continue
            sd = stock_day(d, s)
            j = prevd["idx"].get(s)
            if sd is None or j is None:
                continue
            o, h, l, c = sd
            cc, hh, ll, off = minute_grid(prevd["c"][j], prevd["h"][j], prevd["l"][j], c, h, l)
            fast = wr_smoothed(hh, ll, cc, n=21, span=7)
            kinds = [("call", t) for t in signals(C, P, bc)] + [("put", t) for t in signals(P, C, bp)]
            for kind, t in kinds:
                entries = {"now": t}
                lo = next((u for u in range(t, min(t + LOOSE_WIN, T_LAST) + 1) if loose_ok(fast, u + off)), None)
                if lo is not None:
                    entries["loose"] = lo
                for mode, te in entries.items():
                    a, cl = vol_exit(o, h, l, c, te), close_exit(o, c, te)
                    if a is None or cl is None:
                        continue
                    rows.append({"day": d, "sym": s, "t": te, "sig_t": t, "kind": kind, "mode": mode,
                                 "atr": round(float(a), 2), "close": round(float(cl), 2)})
                if kind == "call":
                    rng = (_h("ocf", d, s) % (T_LAST - T_FIRST)) + T_FIRST
                    a, cl = vol_exit(o, h, l, c, rng), close_exit(o, c, rng)
                    if a is not None and cl is not None:
                        rows.append({"day": d, "sym": s, "t": rng, "sig_t": t, "kind": "control", "mode": "now",
                                     "atr": round(float(a), 2), "close": round(float(cl), 2)})
        print(d, len(rows), flush=True)
    with open(OUT / "events.jsonl", "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    print("events", len(rows))


def load_events():
    return [json.loads(x) for x in open(OUT / "events.jsonl")]


def quotes():
    import news_source as ns
    plan_ = sorted({("ocf", r["day"], r["sym"], r["t"], 0.0) for r in load_events()})
    print("entries to quote", len(plan_), flush=True)
    ns.fetch_quotes(plan_)


def stats(rows, key):
    if not rows:
        return {"n": 0}
    per = defaultdict(list)
    for r in rows:
        per[r["day"]].append(r[key])
    m = [np.mean(v) for v in per.values()]
    sd = float(np.std(m, ddof=1)) if len(m) > 2 else 0.0
    days = sorted(per)
    h1 = set(days[: len(days) // 2])
    a = [r[key] for r in rows if r["day"] in h1]
    b = [r[key] for r in rows if r["day"] not in h1]
    return {"n": len(rows), "days": len(days), "mean_net": round(float(np.mean([r[key] for r in rows])), 2),
            "win_pct": round(100 * float(np.mean([r[key] > 0 for r in rows])), 1),
            "t_day": round(float(np.mean(m) / (sd / math.sqrt(len(m)))), 2) if sd > 0 else None,
            "half1": round(float(np.mean(a)), 2) if a else None, "half2": round(float(np.mean(b)), 2) if b else None}


def verdict(st):
    if st.get("n", 0) < 200:
        return "UNDERPOWERED", [f"n {st.get('n', 0)} (need 200)"]
    f = []
    if st["mean_net"] < 2.0:
        f.append(f"edge {st['mean_net']} bp (need +2.0)")
    if (st.get("t_day") or 0) < T_CRIT:
        f.append(f"t {st.get('t_day')} (need {T_CRIT})")
    if not ((st.get("half1") or -1) > 0 and (st.get("half2") or -1) > 0):
        f.append(f"halves {st.get('half1')} / {st.get('half2')}")
    return ("PASS" if not f else "FAIL"), f


def score():
    import news_source as ns
    cache = json.load(open(ns.QUOTES))
    rows, wide, missing = [], 0, 0
    for r in load_events():
        sp = cache.get(ns.qkey(r["day"], r["sym"], r["t"]))
        if sp is None:
            missing += 1
            continue
        if sp > GATE:
            wide += 1
            continue
        rows.append({**r, "spread": float(sp), "net_atr": r["atr"] - sp, "net_close": r["close"] - sp})
    res = {"verdicts": {}, "decide": {}, "info": {}, "wide": wide, "no_quote": missing}
    for mode in ("now", "loose"):
        for x in ("atr", "close"):
            sel = [r for r in rows if r["kind"] == "call" and r["mode"] == mode]
            st = stats(sel, f"net_{x}")
            res["decide"][f"{mode}|{x}"] = st
            res["verdicts"][f"{mode}|{x}"] = verdict(st)
    for x in ("atr", "close"):
        ctl = stats([r for r in rows if r["kind"] == "control"], f"net_{x}")
        res["info"][f"control|{x}"] = ctl
        res["info"][f"put_burst_long|{x}"] = stats([r for r in rows if r["kind"] == "put" and r["mode"] == "now"], f"net_{x}")
        if ctl.get("n") and res["decide"][f"now|{x}"].get("n"):
            res["info"][f"lift_now_vs_control|{x}"] = round(res["decide"][f"now|{x}"]["mean_net"] - ctl["mean_net"], 2)
    calls = [r for r in rows if r["kind"] == "call" and r["mode"] == "now"]
    nd = max(1, len({r["day"] for r in load_events()}))
    res["info"]["call_signals_per_day"] = round(len(calls) / nd, 2)
    res["info"]["opens_per_10m"] = round(len(calls) / nd / ((T_LAST - T_FIRST + 1) / 10), 3)
    for name, a, b in (("09:45-10:30", 15, 60), ("10:30-12:00", 60, 150), ("12:00-14:00", 150, 270), ("14:00-15:00", 270, 331)):
        res["info"][f"now|close|{name}"] = stats([r for r in calls if a <= r["t"] < b], "net_close")
    res["generated"] = datetime.now(ET).isoformat(timespec="minutes")
    (OUT / "result.json").write_text(json.dumps(res, indent=1))
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    {"plan": plan, "fetch": fetch, "events": events, "quotes": quotes, "score": score}[sys.argv[1]]()
