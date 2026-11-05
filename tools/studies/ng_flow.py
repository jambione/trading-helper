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
COST_BP = 3.0            # the scoreboard convention (round-trip allowance); payoff is ALSO reported at 0 bp
FWD_MAX_S = 1800         # forward tape is fetched to max(T+300, exit_time), capped here
MIN_DAYS_HALF = 12
SHARE_OK = (0.25, 0.42)
MAX_DROP = 0.10
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
            xt = float(o.get("exit_time") or 0.0)
            out.append({"key": f"{d}_{s}_{int(t)}", "d": d, "sym": s, "t": float(t), "e": float(e), "xt": xt,
                        "bp_gross": (x - e) / e * 1e4, "bp": (x - e) / e * 1e4 - COST_BP,
                        "ng_peak": (pk - e) / e * 1e4 < NG_BP})
    return out


def fetch_forward(cl, sym, t, t_end, sleep=time.sleep):
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockTradesRequest
    utc = lambda x: datetime.fromtimestamp(x, timezone.utc)
    for attempt in range(5):
        try:
            tr = cl.get_stock_trades(StockTradesRequest(symbol_or_symbols=sym, start=utc(t), end=utc(t_end),
                                                        feed=DataFeed.SIP, limit=None)).data.get(sym) or []
            sleep(P.REQ_GAP_S)
            return [(P._ts(x), float(x.price), P._codes(x)) for x in tr]
        except Exception as ex:  # noqa: BLE001
            print(f"  fwd {sym} {int(t)} try {attempt}: {type(ex).__name__}", flush=True)
            sleep(10 * (attempt + 1))
    raise RuntimeError(f"forward fetch failed {sym} {int(t)}")


def fwd_end(en):
    """Forward tape window end: the hold (exit_time) but at least FWD_S, capped at FWD_MAX_S."""
    return en["t"] + min(FWD_MAX_S, max(FWD_S, (en["xt"] - en["t"]) if en["xt"] > en["t"] else FWD_S))


def ng_from_forward(fwd, t, e, horizon=FWD_S):
    """True/False, or None when the post-entry tape is empty. horizon = seconds after T (the fixed 300 s label, or the
    hold: exit_time - T)."""
    kept = [(ts, px) for ts, px, codes in fwd if t < ts <= t + horizon and codes and all(c in P.KEEP_CODES for c in codes)]
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
            fwd = fetch_forward(cl, en["sym"], en["t"], fwd_end(en))
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


def _u(key):
    import hashlib
    return int(hashlib.sha256(("ng-flow-tie|" + key).encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


def build_rows():
    rows, drops = [], {"no_file": 0, "no_ofi": 0, "no_label": 0}
    dropped = {"A": 0, "B": 0}
    total = {"A": 0, "B": 0}
    for en in entries():
        half = "A" if en["d"] < SPLIT else "B"
        total[half] += 1
        path = os.path.join(OUT, en["key"] + ".pkl")
        if not os.path.exists(path):
            drops["no_file"] += 1; dropped[half] += 1; continue
        d = pickle.load(open(path, "rb"))
        f = P.features(d["tick"], en["t"])
        if f["ofi60"] is None:
            drops["no_ofi"] += 1; dropped[half] += 1; continue
        hold = max(1.0, en["xt"] - en["t"]) if en["xt"] > en["t"] else FWD_S
        ng_hold = ng_from_forward(d["fwd"], en["t"], en["e"], horizon=min(FWD_MAX_S, hold))
        ng_300 = ng_from_forward(d["fwd"], en["t"], en["e"], horizon=FWD_S)
        if ng_hold is None:
            drops["no_label"] += 1; dropped[half] += 1; continue
        qs = P.clean_quotes(d["tick"]["quotes"])
        q = P.last_quote(qs, en["t"], P.READ_MAX_AGE)
        spread_bp = ((q[2] - q[1]) / ((q[2] + q[1]) / 2) * 1e4) if q else None
        pre = [px for ts, px, sz, codes, _t in d["tick"]["trades"] if en["t"] - 60 <= ts < en["t"] and codes and all(c in P.KEEP_CODES for c in codes)]
        rng60 = ((max(pre) - min(pre)) / en["e"] * 1e4) if len(pre) >= 2 else None
        rows.append({**en, "ng": ng_hold, "ng300": ng_300, "ofi": f["ofi60"], "qi": f["qi"], "micro": f["micro"],
                     "spread_bp": spread_bp, "rng60": rng60, "u": _u(en["key"]), "half": half})
    drops["share_A"] = dropped["A"] / max(1, total["A"]); drops["share_B"] = dropped["B"] / max(1, total["B"])
    drops["share_all"] = (dropped["A"] + dropped["B"]) / max(1, total["A"] + total["B"])
    return rows, drops


def score(rows, drops=None):
    a = [r for r in rows if r["half"] == "A"]
    days_a = {r["d"] for r in a}; days_b = {r["d"] for r in rows if r["half"] == "B"}
    if len(a) < 150:
        return {"verdict": "NO_DATA", "n_A": len(a)}
    if drops and drops.get("share_all", 0) > MAX_DROP:
        return {"verdict": "UNPROVEN_DROPS", "drops": drops}
    order = sorted(a, key=lambda r: (r["ofi"], r["u"]))
    cut = (order[len(a) // 3]["ofi"], order[len(a) // 3]["u"])          # fit on A only, ties broken by the seeded u
    weak = lambda r: (r["ofi"], r["u"]) <= cut
    res = {"cut_from_A": [round(cut[0], 4), round(cut[1], 4)], "ng_rate_all": sum(r["ng"] for r in rows) / len(rows)}
    halves = {"ALL": rows, "A": a, "B": [r for r in rows if r["half"] == "B"]}
    under = share_bad = False
    for lab, sub in halves.items():
        w = [r for r in sub if weak(r)]; k = [r for r in sub if not weak(r)]
        if not w or not k:
            res[lab] = None; under = True; continue
        share = len(w) / len(sub)
        if not (SHARE_OK[0] <= share <= SHARE_OK[1]):
            share_bad = True
        if lab != "ALL" and len(w) < MIN_GROUP:
            under = True
        res[lab] = {"n": len(sub), "n_weak": len(w), "share": share,
                    "ng_weak": sum(r["ng"] for r in w) / len(w), "ng_rest": sum(r["ng"] for r in k) / len(k),
                    "z": zdiff(sum(r["ng"] for r in w), len(w), sum(r["ng"] for r in k), len(k)),
                    "net_kept": sum(r["bp"] for r in k) / len(k), "net_all": sum(r["bp"] for r in sub) / len(sub),
                    "net_kept_0bp": sum(r["bp_gross"] for r in k) / len(k), "net_all_0bp": sum(r["bp_gross"] for r in sub) / len(sub),
                    "ng300_weak": sum(r["ng300"] for r in w if r["ng300"] is not None) / max(1, sum(1 for r in w if r["ng300"] is not None)),
                    "ng300_rest": sum(r["ng300"] for r in k if r["ng300"] is not None) / max(1, sum(1 for r in k if r["ng300"] is not None)),
                    "peak_ng_weak": sum(r["ng_peak"] for r in w) / len(w), "peak_ng_rest": sum(r["ng_peak"] for r in k) / len(k)}
    # confounds: the weak-minus-rest never-green gap must hold inside spread and 60 s range terciles (>= 2 of 3 positive each)
    strata = {}
    for name in ("spread_bp", "rng60"):
        vals = sorted(r[name] for r in rows if r[name] is not None)
        if len(vals) < 90:
            strata[name] = None; continue
        t1, t2 = vals[len(vals) // 3], vals[2 * len(vals) // 3]
        diffs = []
        for lo, hi in ((-1, t1), (t1, t2), (t2, 1e18)):
            g = [r for r in rows if r[name] is not None and lo < r[name] <= hi]
            w = [r for r in g if weak(r)]; k = [r for r in g if not weak(r)]
            if len(w) >= 15 and len(k) >= 15:
                diffs.append(sum(r["ng"] for r in w) / len(w) - sum(r["ng"] for r in k) / len(k))
        strata[name] = [round(x, 3) for x in diffs]
    res["strata_ng_diff"] = strata
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
    res["days"] = {"A": len(days_a), "B": len(days_b)}
    al, ha, hb = res.get("ALL"), res.get("A"), res.get("B")
    if not (al and ha and hb):
        res["verdict"] = "NO_DATA"
    elif share_bad:
        res["verdict"] = "ABORT_SHARE"
    elif under or len(days_a) < MIN_DAYS_HALF or len(days_b) < MIN_DAYS_HALF:
        res["verdict"] = "UNDERPOWERED"
    else:
        gap_ok = al["ng_weak"] - al["ng_rest"] >= 0.05
        ng_ok = (gap_ok and al["z"] >= Z_BAR and ha["ng_weak"] > ha["ng_rest"] and hb["ng_weak"] > hb["ng_rest"]
                 and res["boot"]["ng_diff_lo5"] > 0
                 and all(v is not None and sum(x > 0 for x in v) >= 2 for v in strata.values()))
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
        res = score(rows, drops)
        print(json.dumps(res, indent=1, default=str))
        json.dump(res, open(os.path.join(OUT, "result.json"), "w"), indent=1, default=str)
        return 0
    print(__doc__); return 2


if __name__ == "__main__":
    sys.exit(main())
