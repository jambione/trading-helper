#!/usr/bin/env python3
"""Buy-stop entries filtered by buyer-initiated order flow, at quoted cost; arm A = %R turn-down exit, arm B = the
desk's live exit stack. Spec: docs/studies/buystop_flow_prereg.json (main + amended).

Phases (each resumable, data-only account for every fetch):
  --sample   seeded, day-stratified sample of batch 2 T2 trades >= $20 (no outcomes read); re-derives each trade's stop
  --probe    request/time budget on 10 sampled trades -> the largest N per half that fits 6 hours
  --fetch    per trade: SIP trades [k-60 s, k+60 s) (trigger T + flow), quotes [T-120 s, T+1 s], exit quotes
  --cuts     G1 (OFI60) top-tercile cut from HALF A features only (outcome-blind), then arm-B prints + IEX bars for
             G1-IN trades
  (default)  count step (outcome-blind)       --score  verdicts (refuses without READY / floors / same code)
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import pickle
import random
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
import batch2 as B2  # noqa: E402
import wr_trend_trade as W  # noqa: E402

ROOT = W.ROOT
OUT = os.path.join(ROOT, "ai_reports", "buystop_flow")
MIN_PX, N_MAX, BUDGET_H, REQ_PER_MIN = 20.0, 8000, 6.0, 140
READ_AGE, T_ARM = 30.0, 2.24
MDE_BAR, FLOOR_TRADES, FLOOR_DAYS = 0.0003, 300, 20


def jload(name, default=None):
    p = os.path.join(OUT, name)
    return json.load(open(p)) if os.path.exists(p) else default


def jwrite(name, obj):
    os.makedirs(OUT, exist_ok=True)
    p = os.path.join(OUT, name)
    json.dump(obj, open(p + ".tmp", "w"), default=str)
    os.replace(p + ".tmp", p)
    return p


def seeded(*parts) -> random.Random:
    return random.Random(int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:16], 16))


def et_epoch(day: str, minute: int, sec: float = 0.0) -> float:
    from zoneinfo import ZoneInfo
    d = datetime.fromisoformat(day).replace(tzinfo=ZoneInfo("America/New_York"))
    return d.replace(hour=minute // 60, minute=minute % 60).timestamp() + sec


# ── sample ───────────────────────────────────────────────────────────────────

def t2_with_stops(fast, slow, sipf, iexf) -> list[tuple]:
    """batch2.run_rule's T2 branch, also returning the stop level at the fill (same state machine)."""
    out, state = [], {"free_from": 0}
    t = W.DEC_LO - 30
    while t <= W.DEC_HI + 10:
        before = state.get("stop")
        hit = B2.t2_step(fast, slow, iexf, sipf, t, state)
        if hit is None:
            t += 1
            continue
        e, px = hit
        u = W.exit_minute(fast, slow, e + 1)
        x_min = W.M_LAST + 1 if (u is None or u + 1 > W.M_LAST) else u + 1
        out.append((e, px, x_min, before))
        state = {"free_from": x_min}
        t = x_min
    return out


def build_sample(n_half: int) -> dict:
    import indicator_confirm_hist as H
    rows = json.load(open(os.path.join(ROOT, "ai_reports", "batch2", "rows.json")))["rows"]
    t2 = [r for r in rows if r["test"] == "T2" and r["px"] >= MIN_PX]
    by_day = defaultdict(list)
    for r in t2:
        by_day[r["day"]].append(r)
    days = H.warehouse_days()
    out, st = [], Counter()
    for h in ("A", "B"):
        hd = sorted(d for d in by_day if (d <= W.HALF_A_END) == (h == "A"))
        per_day = max(1, n_half // max(1, len(hd)))
        for d in hd:
            pick = sorted(by_day[d], key=lambda r: (r["sym"], r["entry_min"]))
            seeded("bsf-sample", d).shuffle(pick)
            pick = pick[:per_day]
            if not pick:
                continue
            prev = days[days.index(d) - 1]
            sip, iex, iexp = H.load_raw("sip", d), H.load_raw("iex", d), H.load_raw("iex", prev)
            for r in pick:
                s = r["sym"]
                if s not in iex or s not in iexp or s not in sip:
                    st["missing_bars"] += 1
                    continue
                W.REAL.clear()
                fast, slow = W.lines(iexp[s], iex[s])
                sipf = {int(x[0]): (float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])) for x in sip[s]}
                iexf = {int(x[0]): (float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])) for x in iex[s]}
                match = [x for x in t2_with_stops(fast, slow, sipf, iexf) if x[0] == r["entry_min"]]
                if not match or abs(match[0][1] - r["px"]) > 1e-6 or match[0][3] is None:
                    st["stop_not_rederived"] += 1
                    continue
                e, px, x_min, stop = match[0]
                out.append({"day": d, "sym": s, "half": h, "k": e, "px_bar": px, "stop": stop, "exit_min": x_min,
                            "bar_gross": r["gross"], "ctrl_d": r.get("d")})
    return {"trades": out, "stats": dict(st), "n_half": n_half}


# ── fetch ────────────────────────────────────────────────────────────────────

def client():
    from alpaca.data.historical import StockHistoricalDataClient
    s = json.load(open(os.path.join(ROOT, "secrets_data.json")))
    return StockHistoricalDataClient(s["alpaca_data_api_key"], s["alpaca_data_secret_key"])


def _req(fn, *a, **k):
    for attempt in range(6):
        try:
            return fn(*a, **k)
        except Exception as e:  # noqa: BLE001
            print(f"  retry {attempt + 1}: {str(e)[:80]}", flush=True)
            time.sleep(10 * (attempt + 1))
    raise RuntimeError("fetch failed")


def trades_between(cl, sym, a, b):
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockTradesRequest
    utc = lambda x: datetime.fromtimestamp(x, timezone.utc)  # noqa: E731
    got = _req(cl.get_stock_trades, StockTradesRequest(symbol_or_symbols=sym, start=utc(a), end=utc(b),
                                                       feed=DataFeed.SIP)).data.get(sym) or []
    return [(x.timestamp.timestamp(), float(x.price), float(x.size), list(x.conditions or []), x.tape) for x in got]


def quotes_between(cl, sym, a, b):
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockQuotesRequest
    utc = lambda x: datetime.fromtimestamp(x, timezone.utc)  # noqa: E731
    got = _req(cl.get_stock_quotes, StockQuotesRequest(symbol_or_symbols=sym, start=utc(a), end=utc(b),
                                                       feed=DataFeed.SIP)).data.get(sym) or []
    return [(x.timestamp.timestamp(), float(x.bid_price or 0), float(x.ask_price or 0), float(x.bid_size or 0),
             float(x.ask_size or 0)) for x in got]


def trigger_time(trades: list, k_start: float, stop: float):
    import order_flow_pilot as OF
    for ts, px, size, codes, _tape in sorted(trades, key=lambda x: x[0]):
        if k_start <= ts < k_start + 60 and codes and all(c in OF.KEEP_CODES for c in codes) and px >= stop + 0.01:
            return ts
    return None


def fetch_one(cl, t: dict) -> dict:
    import order_flow_pilot as OF
    k0 = et_epoch(t["day"], t["k"])
    tr = trades_between(cl, t["sym"], k0 - 60, k0 + 60)
    time.sleep(60 / REQ_PER_MIN)
    T = trigger_time(tr, k0, t["stop"])
    if T is None:
        return {"status": "no_trigger_in_tape"}
    qs = quotes_between(cl, t["sym"], T - 120, T + 1)
    time.sleep(60 / REQ_PER_MIN)
    x_ts = et_epoch(t["day"], 15 * 60 + 59, 59) if t["exit_min"] > W.M_LAST else et_epoch(t["day"], t["exit_min"])
    xq = quotes_between(cl, t["sym"], x_ts - 30, x_ts + 1)
    time.sleep(60 / REQ_PER_MIN)
    tick = {"trades": tr, "quotes": qs}
    f = OF.features(tick, T)
    q_in = OF.last_quote(OF.clean_quotes(qs), T + 1, READ_AGE)
    q_out = OF.last_quote(OF.clean_quotes(xq), x_ts + 1, READ_AGE)
    return {"status": "ok", "T": T, "x_ts": x_ts, "ofi60": f["ofi60"], "qi": f["qi"], "n_signed": f["n_signed"],
            "ask_in": q_in[2] if q_in else None, "bid_in": q_in[1] if q_in else None,
            "bid_out": q_out[1] if q_out else None, "ask_out": q_out[2] if q_out else None}


# ── arm B (desk exits) ───────────────────────────────────────────────────────

def desk_r_ratio() -> float:
    import exit_capture as XC
    fills, _ = XC.load_fills()
    return statistics.median(f["risk"] / f["entry"] for f in fills)


def arm_b(t: dict, f: dict, secs: list, b1m: list, cfg: dict, ring: int, r_ratio: float, bid_fn) -> dict | None:
    import exit_capture as XC
    entry = f["ask_in"]
    pos = {"sym": t["sym"], "day": t["day"], "t0": f["T"], "entry": entry, "risk": entry * r_ratio}
    xs, trig, why, held = XC.walk(pos, secs, cfg, ring, b1m)
    b = bid_fn(xs)
    px = XC.exit_price(trig, b, why)
    return None if px is None else {"net": px / entry - 1, "why": why, "exit_s": xs}


# ── statistics ───────────────────────────────────────────────────────────────

def dstat(rows, key):
    return W.dstat(rows, key)


def module_shas():
    d = os.path.dirname(os.path.abspath(__file__))
    return {m: hashlib.sha256(open(os.path.join(d, f"{m}.py"), "rb").read()).hexdigest()
            for m in ("buystop_flow", "batch2", "wr_trend_trade", "order_flow_pilot", "exit_capture")}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for f_ in ("--sample", "--probe", "--fetch", "--cuts", "--score"):
        ap.add_argument(f_, action="store_true")
    ap.add_argument("--n-half", type=int, default=N_MAX)
    ap.add_argument("--skeptic-ready", default="")
    ap.add_argument("--prereg-commit", default="")
    a = ap.parse_args()
    if a.sample:
        smp = build_sample(a.n_half)
        print(len(smp["trades"]), smp["stats"])
        jwrite("sample.json", {**smp, "module_sha256": module_shas()})
        return
    smp = jload("sample.json")
    if a.probe:
        cl = client()
        t0, n = time.time(), 0
        for t in smp["trades"][::max(1, len(smp["trades"]) // 10)][:10]:
            fetch_one(cl, t)
            n += 1
        per = (time.time() - t0) / n
        sec_b = 0.0
        import leash_replay as L
        for t in smp["trades"][:3]:
            s0 = time.time()
            L.fetch_seconds(cl, t["sym"], et_epoch(t["day"], t["k"]) - 5, et_epoch(t["day"], t["k"]) + L.MAX_HOLD + 60)
            sec_b += (time.time() - s0) / 3
        n_fit = int(BUDGET_H * 3600 / (per + sec_b / 3))         # arm B on ~1/3 (G1-IN)
        out = {"sec_per_trade_A": per, "sec_per_in_trade_B": sec_b, "max_total_trades": n_fit,
               "n_half": min(N_MAX, n_fit // 2)}
        print(out)
        jwrite("probe.json", out)
        return
    if a.fetch:
        cl, done = client(), jload("fetched.json", {})
        failed = []
        for i, t in enumerate(smp["trades"]):
            key = f"{t['day']}|{t['sym']}|{t['k']}"
            if key in done:
                continue
            try:
                done[key] = fetch_one(cl, t)
            except RuntimeError:
                failed.append(key)
            if i % 100 == 0:
                jwrite("fetched.json", done)
                print(f"  {i}/{len(smp['trades'])}", flush=True)
        jwrite("fetched.json", done)
        jwrite("fetch_log.json", {"failed": failed})
        print("failed", len(failed))
        return
    fetched = jload("fetched.json", {})
    if jload("fetch_log.json", {}).get("failed"):
        raise SystemExit("fetch failures (C4): rerun --fetch")
    import order_flow_pilot as OF
    rows, st = [], Counter()
    for t in smp["trades"]:
        f = fetched.get(f"{t['day']}|{t['sym']}|{t['k']}")
        if f is None:
            st["not_fetched"] += 1
            continue
        if f["status"] != "ok":
            st[f["status"]] += 1
            continue
        rows.append({**t, **f, "m": t["k"]})
    if any(v for k, v in st.items() if k == "not_fetched"):
        raise SystemExit(f"{st['not_fetched']} sampled trades not fetched (C4)")
    cut = OF.top_cut([(r["ofi60"], OF.tie_u(r)) for r in rows if r["half"] == "A" and r["ofi60"] is not None])
    for r in rows:
        r["g1"] = None if r["ofi60"] is None else ("IN" if (r["ofi60"], OF.tie_u(r)) >= tuple(cut) else "OUT")
        ok = r["ask_in"] and r["bid_out"]
        r["net_A"] = (r["bid_out"] / r["ask_in"] - 1) if ok else None
    if a.cuts:
        import exit_capture as XC
        import leash_replay as L
        cl, cache = client(), jload("armB_cache.pkl.json", {})
        store = os.path.join(OUT, "armB.pkl")
        data = pickle.load(open(store, "rb")) if os.path.exists(store) else {}
        for r in rows:
            if r["g1"] != "IN" or r["net_A"] is None:
                continue
            key = f"{r['day']}|{r['sym']}|{r['k']}"
            if key in data:
                continue
            secs = L.fetch_seconds(cl, r["sym"], r["T"] - 5, r["T"] + L.MAX_HOLD + 60)
            b1m = XC.fetch_bars(cl, r["sym"], r["day"])
            data[key] = {"secs": secs, "b1m": b1m}
            if len(data) % 50 == 0:
                pickle.dump(data, open(store, "wb"))
        pickle.dump(data, open(store, "wb"))
        print("arm B paths", len(data))
        return
    half_rows = lambda h, g=None: [r for r in rows if r["half"] == h and (g is None or r["g1"] == g)  # noqa: E731
                                   and r["net_A"] is not None]
    if not a.score:
        cnt = {"stats": dict(st), "cut": cut, "module_sha256": module_shas(), "prereg_commit": a.prereg_commit}
        for h in ("A", "B"):
            inn = half_rows(h, "IN")
            ctrl = [r for r in inn if r.get("ctrl_d") is not None]
            per = defaultdict(list)
            for r in ctrl:
                per[r["day"]].append(r["ctrl_d"])
            xs = [statistics.mean(v) for v in per.values()]
            mde = (2.0 + 0.84) * statistics.stdev(xs) / math.sqrt(len(xs)) if len(xs) >= 2 else None
            nd = len({r["day"] for r in inn})
            cnt[h] = {"in_trades": len(inn), "out_trades": len(half_rows(h, "OUT")),
                      "missing_flow": sum(1 for r in rows if r["half"] == h and r["g1"] is None), "days": nd,
                      "mde": mde, "floors_met": len(inn) >= FLOOR_TRADES and nd >= FLOOR_DAYS
                      and mde is not None and mde <= MDE_BAR}
        print(json.dumps(cnt, indent=1, default=str))
        print(jwrite("count.json", cnt))
        return
    if not a.skeptic_ready:
        raise SystemExit("--score needs --skeptic-ready")
    cnt = jload("count.json")
    if cnt.get("module_sha256") != module_shas() or not cnt.get("prereg_commit"):
        raise SystemExit("count.json from different code or without --prereg-commit")
    if not all(cnt[h]["floors_met"] for h in ("A", "B")):
        raise SystemExit("UNDERPOWERED at the count step: no outcome is read")
    import exit_capture as XC
    import leash_replay as L
    cfg = copy.deepcopy(L.load_config())
    cfg.pop("_min_hold", None)
    ring = max(2, int(cfg.get("ai_local_trail_print_ring", 3) or 3))
    r_ratio = desk_r_ratio()
    data = pickle.load(open(os.path.join(OUT, "armB.pkl"), "rb"))
    cl = client()
    for r in rows:
        key = f"{r['day']}|{r['sym']}|{r['k']}"
        if r["g1"] == "IN" and r["net_A"] is not None and key in data:
            res = arm_b(r, r, data[key]["secs"], data[key]["b1m"], cfg, ring, r_ratio,
                        lambda xs, s=r["sym"]: XC.fetch_bid(cl, s, int(xs)))
            r["net_B"] = res["net"] if res else None
    out = {"arms": {}, "info": {}}
    for arm in ("A", "B"):
        key = f"net_{arm}"
        hv, ubs = [], []
        halves = {}
        for h in ("A", "B"):
            inn = [r for r in half_rows(h, "IN") if r.get(key) is not None]
            s = dstat(inn, lambda r, k=key: r[k])
            if arm == "A":
                o = dstat(half_rows(h, "OUT"), lambda r: r["net_A"])
                diff = (s["mean"] - o["mean"]) if s["mean"] is not None and o["mean"] is not None else None
            else:
                diff = 0.0
            dd = W.drop_days(inn, lambda r, k=key: r[k])
            dn = W.drop_names(inn, lambda r, k=key: r[k])
            if (s["t"] or 0) <= -2:
                v = "FAIL"
            elif s["mean"] > 0 and (s["t"] or 0) >= T_ARM and diff is not None and diff >= 0 and dd >= 0 and dn >= 0:
                v = "PASS"
            else:
                v = "NOT_PASSED"
            hv.append(v)
            ubs.append(s["ub"])
            halves[h] = {"verdict": v, "net": s, "in_minus_out": diff, "drop3_days": dd, "drop5_names": dn,
                         "n": len(inn)}
        verdict = ("FAIL" if "FAIL" in hv else "PASS" if all(x == "PASS" for x in hv) else
                   "FAIL" if all(u is not None and u < 0 for u in ubs) else "UNDERPOWERED")
        out["arms"][arm] = {"verdict": verdict, "halves": halves}
    out["verdict"] = ("PASS" if any(v["verdict"] == "PASS" for v in out["arms"].values()) else
                      "FAIL" if all(v["verdict"] == "FAIL" for v in out["arms"].values()) else "UNDERPOWERED")
    for h in ("A", "B"):
        allr = [r for r in rows if r["half"] == h and r["net_A"] is not None]
        out["info"][h] = {"all_sample_net_A": dstat(allr, lambda r: r["net_A"])["mean"],
                          "out_net_A": dstat(half_rows(h, "OUT"), lambda r: r["net_A"])["mean"],
                          "in_bar_gross": dstat(half_rows(h, "IN"), lambda r: r["bar_gross"])["mean"],
                          "out_bar_gross": dstat(half_rows(h, "OUT"), lambda r: r["bar_gross"])["mean"],
                          "entry_spread_bp_median": statistics.median(
                              (r["ask_in"] - r["bid_in"]) / ((r["ask_in"] + r["bid_in"]) / 2) * 1e4
                              for r in allr if r["ask_in"] and r["bid_in"]) if allr else None}
    out["declared"] = ["arm B runs the desk exit stack under the CURRENT config file (as exit_capture), not each "
                       "day's historical config", "R = entry x the desk's median R/entry (exit_capture fills)"]
    out["skeptic_ready"], out["module_sha256"] = a.skeptic_ready, module_shas()
    jwrite("result.json", out)
    print(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    main()
