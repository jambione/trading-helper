#!/usr/bin/env python3
"""Retail order imbalance (Boehmer, Jones, Zhang & Zhang) as an intraday signal. Spec:
docs/studies/retail_imbalance_prereg.json (amended; common C1-C4).

Each warehouse day 2026-07-01 .. 10-15: 150 names drawn per day (seed 'rimb-names') from that day's warehouse names.
SIP trades 09:30-11:59:59 (data-only account); FINRA TRF prints (exchange 'D') with regular conditions only; sub-penny
fraction frac = round((price * 100) % 1, 4): BUY if 0.6 < frac < 1.0, SELL if 0 < frac < 0.4. RIMB = (buy - sell) /
(buy + sell) shares (>= 50 classified trades). LONG = the top quintile (>= 10 eligible): 12:00 open -> last close
<= 15:59 (warehouse bars). LIFT = long mean minus all-eligible mean; day-equal; day-clustered t.

Budget probe (amended (3), 2026-10-23): 1.2 pages per name-morning on 5 seeded name-mornings -> ~13.5k requests,
under the 6-hour cap, so no cut.

  --fetch   per day, per sampled name: aggregated morning counts (no prices kept), resumable
  (default) count step (outcome-blind; placebo MDE per C1); --score after a result-skeptic READY
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eod_reversal as E  # noqa: E402

ROOT = E.ROOT
OUT_DIR = os.path.join(ROOT, "ai_reports", "retail_imbalance")
ET = ZoneInfo("America/New_York")
FIRST, LAST, HALF_A_END = "2026-07-01", "2026-10-15", "2026-08-22"
N_NAMES, MIN_CLASSIFIED, MIN_ELIGIBLE, QUINT = 150, 50, 10, 0.20
M_ENTRY, M_EXIT = 720, 959
MIN_PX, COST, COSTS = 10.0, 0.0005, (0.0003, 0.0005, 0.0010, 0.0020)
LIFT_BAR, T_PASS, T_BATCH, T_PLACEBO = 0.0005, 2.0, 2.84, 2.0
FLOOR_TRADES, FLOOR_DAYS = 300, 20
REGULAR = {" ", "@", "I"}


def classify(price: float) -> int:
    frac = round((price * 100) % 1, 4)
    if 0.6 < frac < 1.0:
        return 1
    if 0 < frac < 0.4:
        return -1
    return 0


def sample(day: str, names: list[str]) -> list[str]:
    rng = random.Random(int(hashlib.sha256(f"rimb-names|{day}".encode()).hexdigest()[:16], 16))
    return sorted(rng.sample(sorted(names), min(N_NAMES, len(names))))


def fetch() -> None:
    import indicator_confirm_hist as H
    import requests
    s = json.load(open(os.path.join(ROOT, "secrets_data.json")))
    hdr = {"APCA-API-KEY-ID": s["alpaca_data_api_key"], "APCA-API-SECRET-KEY": s["alpaca_data_secret_key"]}
    os.makedirs(os.path.join(OUT_DIR, "agg"), exist_ok=True)
    failed = []
    for day in [d for d in H.warehouse_days() if FIRST <= d <= LAST]:
        p = os.path.join(OUT_DIR, "agg", f"{day}.json")
        if os.path.exists(p):
            continue
        a = datetime.fromisoformat(day).replace(hour=9, minute=30, tzinfo=ET).isoformat()
        b = datetime.fromisoformat(day).replace(hour=12, tzinfo=ET).isoformat()
        agg = {}
        for sym in sample(day, list(H.load_raw("sip", day))):
            c = Counter()
            tok, ok = None, True
            for _ in range(200):
                prm = {"start": a, "end": b, "limit": 10000, "feed": "sip"}
                if tok:
                    prm["page_token"] = tok
                for attempt in range(8):
                    try:
                        r = requests.get(f"https://data.alpaca.markets/v2/stocks/{sym}/trades", headers=hdr,
                                         params=prm, timeout=60)
                    except Exception:  # noqa: BLE001
                        time.sleep(10)
                        continue
                    if r.status_code == 429:
                        time.sleep(30)
                        continue
                    break
                else:
                    ok = False
                    break
                if r.status_code != 200:
                    ok = False
                    break
                j = r.json()
                for t in j.get("trades") or []:
                    if t.get("x") != "D":
                        continue
                    c["trf"] += 1
                    if not set(t.get("c") or [" "]) <= REGULAR:
                        continue
                    k = classify(float(t["p"]))
                    if k:
                        c["subpenny"] += 1
                        c["buy_sh" if k > 0 else "sell_sh"] += int(t["s"])
                        c["classified"] += 1
                tok = j.get("next_page_token")
                time.sleep(0.4)
                if not tok:
                    break
            if tok:
                ok = False                                  # 200-page cap: the morning would be truncated
            if not ok:
                failed.append(f"{day} {sym}")
                continue
            agg[sym] = dict(c)
        if any(f.startswith(f"{day} ") for f in failed):
            continue                                        # never write a day with a failed name (C4)
        json.dump(agg, open(p + ".tmp", "w"))
        os.replace(p + ".tmp", p)
        print(day, len(agg), flush=True)
    json.dump({"failed": failed}, open(os.path.join(OUT_DIR, "fetch_log.json"), "w"))
    print("failed:", failed)


def day_rows(day: str, prev_day: str | None) -> dict:
    import indicator_confirm_hist as H
    agg = json.load(open(os.path.join(OUT_DIR, "agg", f"{day}.json")))
    raw = H.load_raw("sip", day)
    prev = H.load_raw("sip", prev_day) if prev_day else {}
    st, elig = Counter(), []
    for sym, c in agg.items():
        if c.get("classified", 0) < MIN_CLASSIFIED:
            st["too_few_classified"] += 1
            continue
        r = raw.get(sym)
        ent = E.bar_at(r, M_ENTRY) if r is not None else None
        if ent is None:
            st["skip_no_1200_bar"] += 1
            continue
        if ent[1] < MIN_PX:
            st["skip_below_10"] += 1
            continue
        tot = c.get("buy_sh", 0) + c.get("sell_sh", 0)
        rimb = (c.get("buy_sh", 0) - c.get("sell_sh", 0)) / tot if tot else 0.0
        pc = E.last_close_at_or_before(prev[sym], 959) if sym in prev else None
        mc = E.last_close_at_or_before(r, 719)
        chg = (mc / pc - 1) if pc and mc else None
        elig.append({"s": sym, "rimb": rimb, "lh": E.last_close_at_or_before(r, M_EXIT) / float(ent[1]) - 1,
                     "chg": chg})
    sub = sum(c.get("subpenny", 0) for c in agg.values())
    trf = sum(c.get("trf", 0) for c in agg.values())
    return {"day": day, "elig": elig, "stats": dict(st), "subpenny_share": sub / trf if trf else None}


def summarize(d: dict) -> dict | None:
    el = d["elig"]
    if len(el) < MIN_ELIGIBLE:
        return None
    n = max(1, int(len(el) * QUINT))
    top = sorted(el, key=lambda r: (-r["rimb"], r["s"]))[:n]
    bot = sorted(el, key=lambda r: (r["rimb"], r["s"]))[:n]
    allm = statistics.mean(r["lh"] for r in el)
    rng = random.Random(int(hashlib.sha256(f"retail_imbalance-placebo|{d['day']}".encode()).hexdigest()[:16], 16))
    pl = rng.sample(sorted(el, key=lambda r: r["s"]), n)
    hot = [r for r in top if r["chg"] is not None and abs(r["chg"]) > 0.05]
    return {"day": d["day"], "lift": statistics.mean(r["lh"] for r in top) - allm,
            "los": statistics.mean(r["lh"] for r in top), "k": n,
            "placebo_lift": statistics.mean(r["lh"] for r in pl) - allm,
            "bottom_lift": statistics.mean(r["lh"] for r in bot) - allm,
            "hot_lift": (statistics.mean(r["lh"] for r in hot) - allm) if hot else None,
            "subpenny_share": d["subpenny_share"]}


def module_shas() -> dict:
    d = os.path.dirname(os.path.abspath(__file__))
    return {m: hashlib.sha256(open(os.path.join(d, f"{m}.py"), "rb").read()).hexdigest()
            for m in ("retail_imbalance", "eod_reversal")}


def write(name, obj):
    os.makedirs(OUT_DIR, exist_ok=True)
    p = os.path.join(OUT_DIR, name)
    json.dump(obj, open(p + ".tmp", "w"), indent=1, default=str)
    os.replace(p + ".tmp", p)
    return p


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--score", action="store_true")
    ap.add_argument("--skeptic-ready", default="")
    ap.add_argument("--prereg-commit", default="")
    a = ap.parse_args()
    if a.fetch:
        return fetch()
    if json.load(open(os.path.join(OUT_DIR, "fetch_log.json"))).get("failed"):
        raise SystemExit("fetch has failures (C4)")
    import indicator_confirm_hist as H
    days = H.warehouse_days()
    summ, st, shares = [], Counter(), {}
    for i, d in enumerate(days):
        if not (FIRST <= d <= LAST):
            continue
        p_ = os.path.join(OUT_DIR, "agg", f"{d}.json")
        if not os.path.exists(p_) or set(json.load(open(p_))) != set(sample(d, list(H.load_raw("sip", d)))):
            raise SystemExit(f"{d}: day file missing or incomplete (C4, skeptic r1 #1)")
        dr = day_rows(d, days[i - 1] if i else None)
        st.update(dr["stats"])
        shares[d] = dr["subpenny_share"]
        s = summarize(dr)
        if s:
            summ.append(s)
        else:
            st["day_skipped_few_names"] += 1
    by_month = defaultdict(list)
    for d, v in shares.items():
        if v is not None:
            by_month[d[:7]].append(v)
    month_share = {m: statistics.mean(v) for m, v in sorted(by_month.items())}
    halves = {"A": [d for d in summ if d["day"] <= HALF_A_END], "B": [d for d in summ if d["day"] > HALF_A_END]}
    if not a.score:
        cnt = {"stats": dict(st), "module_sha256": module_shas(), "prereg_commit": a.prereg_commit,
               "subpenny_share_by_month": month_share,
               "subpenny_share_moved_2x": (max(month_share.values()) > 2 * min(month_share.values()))
               if month_share else None}
        for h, ds in halves.items():
            pl = [d["placebo_lift"] for d in ds]
            sd = statistics.stdev(pl) if len(pl) >= 2 else None
            mde = (T_PLACEBO + 0.84) * sd / math.sqrt(len(pl)) if sd else None
            n = sum(d["k"] for d in ds)
            cnt[h] = {"days": len(ds), "long_trades": n, "mde": mde,
                      "floors_met": n >= FLOOR_TRADES and len(ds) >= FLOOR_DAYS and mde is not None and mde <= LIFT_BAR}
        print(json.dumps(cnt, indent=1, default=str))
        print(write("count.json", cnt))
        return
    if not a.skeptic_ready:
        raise SystemExit("--score needs --skeptic-ready")
    cnt = json.load(open(os.path.join(OUT_DIR, "count.json")))
    if cnt.get("module_sha256") != module_shas() or not cnt.get("prereg_commit"):
        raise SystemExit("count.json from different code or without --prereg-commit")
    if not all(cnt[h]["floors_met"] for h in ("A", "B")):
        raise SystemExit("UNDERPOWERED at the count step: no outcome is read")
    res, hv, ss = {"halves": {}, "info": {}}, [], []
    for h, ds in halves.items():
        s = E.stats([d["lift"] for d in ds])
        net = E.stats([d["los"] - COST for d in ds])
        v = ("FAIL" if s["t"] is not None and s["t"] <= -2 else
             "PASS" if s["mean"] >= LIFT_BAR and (s["t"] or 0) >= T_PASS and net["mean"] > 0 else "NOT_PASSED")
        hv.append(v)
        ss.append(s)
        res["halves"][h] = {"verdict": v, **s, "net_mean": net["mean"], "net_t": net["t"],
                            "net_by_cost": {f"{round(c * 1e4)}bp": E.stats([d["los"] - c for d in ds])["mean"]
                                            for c in COSTS}}
        res["info"][h] = {k: E.stats([d[k] for d in ds])["mean"] for k in ("bottom_lift", "hot_lift", "placebo_lift")}
        res["info"][h]["lift_by_month"] = {m: E.stats([d["lift"] for d in ds if d["day"][:7] == m])["mean"]
                                           for m in sorted({d["day"][:7] for d in ds})}
    res["verdict"] = ("FAIL" if "FAIL" in hv else
                      ("PASS (batch-robust)" if all((x["t"] or 0) >= T_BATCH for x in ss)
                       else "PASS (single-test), not batch-robust") if all(x == "PASS" for x in hv) else
                      "FAIL" if all(x["ub"] is not None and x["ub"] < LIFT_BAR for x in ss) else "UNDERPOWERED")
    res["declared"] = ["next-day open->close cell not built (information, dropped before scoring)",
                       "the $10 floor reads the split-adjusted warehouse 12:00 open",
                       "days fetched before the page-cap guard (2026-10-23 first days) had no cap check; the probe's "
                       "maximum was 2 of 200 pages",
                       "high-attention = |prior close -> 11:59 close| > 5% within the top quintile"]
    res["skeptic_ready"], res["module_sha256"] = a.skeptic_ready, module_shas()
    write("result.json", res)
    print(json.dumps(res, indent=1, default=str))


if __name__ == "__main__":
    main()
