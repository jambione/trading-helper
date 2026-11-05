#!/usr/bin/env python3
"""Never-green vs order flow at entry. Spec: docs/studies/ng_flow_prereg.json. Run ON THE MINI after the close, nice 19.

Reuses tools/studies/order_flow_pilot (its SIP trade/quote fetch, Lee-Ready signing and OFI60 feature, amended_3 rules) on the
desk's OWN entries instead of the pilot's historical PRE onsets, and scores a different target: never-green.

  features  ofi60 = signed trade-flow imbalance over [T-60 s, T) (primary); qi, micro (information only). T = entry_time.
  label     never_green = no kept SIP trade in (T, T+300 s] prints at or above entry_price x 1.0005 (fixed horizon, SIP).
            The desk's peak_price label is reported as a cross-check only (hold-length dependent).
  groups    weak flow = ofi60 <= the 1/3 quantile of half A (fit on A only, applied to B).
usage: ng_flow.py fetch | score
"""
from __future__ import annotations

import glob
import json
import math
import os
import pickle
import random
import re
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import order_flow_pilot as P  # noqa: E402

ROOT = P.E.ROOT
ET = ZoneInfo("America/New_York")
FIRST, LAST, SPLIT = "2026-09-24", "2026-10-30", "2026-10-14"
OUT = os.path.expanduser("~/ng_flow")
FWD_S, NG_BP, MIN_PX = 300, 5.0, 20.0
MIN_GROUP = 80
Z_BAR = 1.64
BOOT = 2000


def entries():
    out, seen = [], set()
    for f in sorted(glob.glob(os.path.join(ROOT, "ai_reports/tapes/2026-*/outcomes.jsonl"))):
        d = f.split("/")[-2]
        if len(d) != 10 or not (FIRST <= d <= LAST):
            continue
        for line in open(f):
            try:
                o = json.loads(line)
            except ValueError:
                continue
            e, x, t, s = o.get("entry_price"), o.get("exit_price"), o.get("entry_time"), str(o.get("symbol") or "")
            if not (isinstance(e, (int, float)) and e >= MIN_PX and x and t) or o.get("entry_path") != "watch" \
                    or not re.fullmatch(r"[A-Z]{1,5}", s):
                continue
            k = (s, round(t))
            if k in seen:
                continue
            seen.add(k)
            pk = o.get("peak_price") or e
            out.append({"key": f"{d}_{s}_{int(t)}", "d": d, "sym": s, "t": float(t), "e": float(e),
                        "bp": (x - e) / e * 1e4 - 3.0, "ng_peak": (pk - e) / e * 1e4 < NG_BP})
    return out


def fetch_forward(cl, sym, t, sleep=time.sleep):
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockTradesRequest
    utc = lambda x: datetime.fromtimestamp(x, timezone.utc)
    for attempt in range(5):
        try:
            tr = cl.get_stock_trades(StockTradesRequest(symbol_or_symbols=sym, start=utc(t), end=utc(t + FWD_S),
                                                        feed=DataFeed.SIP, limit=None)).data.get(sym) or []
            sleep(P.REQ_GAP_S)
            return [(P._ts(x), float(x.price), P._codes(x)) for x in tr]
        except Exception as ex:  # noqa: BLE001
            print(f"  fwd {sym} {int(t)} try {attempt}: {type(ex).__name__}", flush=True)
            sleep(10 * (attempt + 1))
    raise RuntimeError(f"forward fetch failed {sym} {int(t)}")


def ng_from_forward(fwd, t, e):
    """True/False, or None when the post-entry tape is empty (no kept trade at all)."""
    kept = [(ts, px) for ts, px, codes in fwd if t < ts <= t + FWD_S and codes and all(c in P.KEEP_CODES for c in codes)]
    if not kept:
        return None
    return max(px for _ts, px in kept) < e * (1 + NG_BP / 1e4)


def fetch():
    os.makedirs(OUT, exist_ok=True)
    cl = P.data_client()
    es = entries()
    print(f"{len(es)} entries", flush=True)
    done = fail = 0
    for i, en in enumerate(es):
        path = os.path.join(OUT, en["key"] + ".pkl")
        if os.path.exists(path):
            continue
        try:
            tr, qu = P.fetch_entry(cl, en["sym"], en["t"], limit=None)
            fwd = fetch_forward(cl, en["sym"], en["t"])
        except Exception as ex:  # noqa: BLE001
            fail += 1
            print(f"FAIL {en['key']} {ex}", flush=True)
            if fail > 20 and fail > 0.05 * (done + fail):
                print("ABORT: too many failures"); return 2
            continue
        pickle.dump({"tick": P.compact(tr, qu), "fwd": fwd}, open(path, "wb"))
        done += 1
        if done % 50 == 0:
            print(f"  fetched {done} ({i + 1}/{len(es)})", flush=True)
    print(f"fetch done: {done} new, {fail} failed")
    return 0


def zdiff(k1, n1, k2, n2):
    if min(n1, n2) < 10:
        return float("nan")
    p = (k1 + k2) / (n1 + n2); se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    return (k1 / n1 - k2 / n2) / se if se else float("nan")


def build_rows():
    rows, drops = [], {"no_file": 0, "no_ofi": 0, "no_label": 0}
    for en in entries():
        path = os.path.join(OUT, en["key"] + ".pkl")
        if not os.path.exists(path):
            drops["no_file"] += 1; continue
        d = pickle.load(open(path, "rb"))
        f = P.features(d["tick"], en["t"])
        if f["ofi60"] is None:
            drops["no_ofi"] += 1; continue
        ng = ng_from_forward(d["fwd"], en["t"], en["e"])
        if ng is None:
            drops["no_label"] += 1; continue
        rows.append({**en, "ng": ng, "ofi": f["ofi60"], "qi": f["qi"], "micro": f["micro"]})
    return rows, drops


def score(rows):
    a = [r for r in rows if r["d"] < SPLIT]
    if len(a) < 150:
        return {"verdict": "NO_DATA", "n_A": len(a)}
    cut = sorted(r["ofi"] for r in a)[len(a) // 3]
    weak = lambda r: r["ofi"] <= cut
    res = {"cut_from_A": round(cut, 4), "ng_rate_all": sum(r["ng"] for r in rows) / len(rows)}
    halves = {"ALL": rows, "A": a, "B": [r for r in rows if r["d"] >= SPLIT]}
    under = False
    for lab, sub in halves.items():
        w = [r for r in sub if weak(r)]; k = [r for r in sub if not weak(r)]
        if not w or not k:
            res[lab] = None; under = True; continue
        if lab != "ALL" and len(w) < MIN_GROUP:
            under = True
        res[lab] = {"n": len(sub), "n_weak": len(w), "share": len(w) / len(sub),
                    "ng_weak": sum(r["ng"] for r in w) / len(w), "ng_rest": sum(r["ng"] for r in k) / len(k),
                    "z": zdiff(sum(r["ng"] for r in w), len(w), sum(r["ng"] for r in k), len(k)),
                    "net_kept": sum(r["bp"] for r in k) / len(k), "net_all": sum(r["bp"] for r in sub) / len(sub),
                    "peak_ng_weak": sum(r["ng_peak"] for r in w) / len(w), "peak_ng_rest": sum(r["ng_peak"] for r in k) / len(k)}
    days = {}
    for r in rows:
        days.setdefault(r["d"], []).append(r)
    names = list(days); rng = random.Random(20261105); pay, ngd = [], []
    for _ in range(BOOT):
        s = [r for d in (rng.choice(names) for _ in names) for r in days[d]]
        w = [r for r in s if weak(r)]; k = [r for r in s if not weak(r)]
        if not w or not k:
            continue
        pay.append(sum(r["bp"] for r in k) / len(k) - sum(r["bp"] for r in s) / len(s))
        ngd.append(sum(r["ng"] for r in w) / len(w) - sum(r["ng"] for r in k) / len(k))
    q5 = lambda v: sorted(v)[int(len(v) * 0.05)] if v else float("nan")
    res["boot"] = {"payoff_lo5": q5(pay), "ng_diff_lo5": q5(ngd)}
    al, ha, hb = res.get("ALL"), res.get("A"), res.get("B")
    if not (al and ha and hb):
        res["verdict"] = "NO_DATA"
    elif under:
        res["verdict"] = "UNDERPOWERED"
    else:
        ng_ok = al["z"] >= Z_BAR and ha["ng_weak"] > ha["ng_rest"] and hb["ng_weak"] > hb["ng_rest"] and res["boot"]["ng_diff_lo5"] > 0
        pay_ok = all(x["net_kept"] > x["net_all"] for x in (al, ha, hb)) and res["boot"]["payoff_lo5"] > 0
        res["verdict"] = "PASS_NG_AND_PAYOFF" if ng_ok and pay_ok else "PASS_NG_ONLY" if ng_ok else "FAIL"
    return res


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "fetch":
        return fetch()
    if cmd == "score":
        rows, drops = build_rows()
        print("drops", drops, "scored", len(rows), "days", len({r["d"] for r in rows}))
        if len(rows) < 300:
            print("ABORT: fewer than 300 scored entries"); return 2
        res = score(rows)
        print(json.dumps(res, indent=1, default=str))
        json.dump(res, open(os.path.join(OUT, "result.json"), "w"), indent=1, default=str)
        return 0
    print(__doc__); return 2


if __name__ == "__main__":
    sys.exit(main())
