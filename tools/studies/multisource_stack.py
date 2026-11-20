#!/usr/bin/env python3
"""multisource_stack - on past Stocktwits trending entries (attention), do layers from OTHER sources - crowd sentiment
(Stocktwits), news (Alpaca), the market/sector (ETFs) - pick the entries that rise over the next 30 minutes, alone
and stacked? Operator 11/20. Prereg + amended_1: docs/studies/multisource_stack_prereg.json (builds on
docs/studies/trending_two_step_prereg.json amended_1).

    .venv/bin/python tools/studies/multisource_stack.py inputs   # after 16:05 ET: bars, daily bars, news (shared key)
    .venv/bin/python tools/studies/multisource_stack.py power    # ON/OFF counts + projected MDE (no outcomes)
    .venv/bin/python tools/studies/multisource_stack.py score    # outcomes, matching, controls, verdicts

Sentiment comes from ai_reports/sentiment_backfill/cache.json (tools/studies/sentiment_backfill.py).
"""
from __future__ import annotations

import hashlib
import json
import math
import pickle
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))

OUT = ROOT / "ai_reports" / "multisource_stack"
SENT = ROOT / "ai_reports" / "sentiment_backfill" / "cache.json"
SI_DIR = ROOT / "data" / "finra_short_interest"
ET = ZoneInfo("America/New_York")
SPLIT = "2026-10-01"
HOLD, MIN_PX = 30, 2.0
ETFS = ("XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC", "SMH", "XBI", "IBIT", "KRE")
CELLS = {"K1": ("S",), "K2": ("N",), "K3": ("R",), "K4": ("S", "N"), "K5": ("S", "R"), "K6": ("S", "N", "R")}
T_CRIT, MDE_MAX, S_LINE, S_MIN_TAGGED = 2.64, 60.0, 0.70, 5
HOLIDAYS = ["2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03",
            "2026-09-07", "2026-11-26", "2026-12-25"]
BEARISH = re.compile(r"\b(offering|dilut\w*|pricing of|registered direct|shelf|S-1|S-3|warrant|reverse split|"
                     r"going concern|delist\w*|downgrade[sd]?|cuts? (?:price )?target)\b", re.I)
ATM = re.compile(r"\bATM\b")                         # case-sensitive (amended_1 #6); 'halt' excluded


def hkey(*p):
    return int(hashlib.sha256("|".join(str(x) for x in p).encode()).hexdigest(), 16)


# ---------------------------------------------------------------- layers (pure)

def s_layer(sent: dict | None):
    """-> True/False, or None when not covered (invalid backfill or < 5 tagged). Pure."""
    if not sent or not sent.get("valid") or (sent.get("tagged") or 0) < S_MIN_TAGGED or sent.get("bull_share") is None:
        return None
    return sent["bull_share"] >= S_LINE


def n_layer(headlines: list[dict], ts: float):
    """headlines: [{created_at_ts, headline, n_symbols}] -> ON if >= 1 headline in [ts-24h, ts) after dropping
    multi-symbol (>= 5) recaps, and none bearish. Pure."""
    h = [x for x in headlines if ts - 86400 <= x["t"] < ts and x.get("n_symbols", 1) < 5]
    if not h:
        return False
    return not any(BEARISH.search(x["headline"] or "") or ATM.search(x["headline"] or "") for x in h)


def ret15(closes_by_min: dict, minute: int):
    """15-minute return to the last completed bar before `minute` (minutes since 09:30). Pure."""
    c1 = closes_by_min.get(minute - 1)
    c0 = closes_by_min.get(minute - 16)
    return (c1 / c0 - 1) if c1 and c0 else None


def pick_etf(daily: dict, sym: str, day: str):
    """The ETF whose daily returns correlate best with sym over the 60 sessions before `day` (>= 0.3, else SPY; < 40
    sessions -> SPY). daily: {sym: {day: close}}. Pure."""
    s = daily.get(sym) or {}
    days = sorted(d for d in s if d < day)[-61:]
    if len(days) < 41:
        return "SPY"
    r = np.diff(np.log([s[d] for d in days]))
    best, bc = "SPY", 0.3
    for e in ETFS:
        ed = daily.get(e) or {}
        if not all(d in ed for d in days):
            continue
        re_ = np.diff(np.log([ed[d] for d in days]))
        c = float(np.corrcoef(r, re_)[0, 1])
        if np.isfinite(c) and c >= bc:
            best, bc = e, c
    return best


def finra_published(settle: str) -> str:
    """Settlement date + 8 business days (exchange holidays). Pure."""
    return str(np.busday_offset(np.datetime64(settle), 8, roll="forward", holidays=HOLIDAYS))


# ---------------------------------------------------------------- events and inputs

def events():
    from sentiment_backfill import events as ev
    return ev()


def minute_of(ts: float):
    d = datetime.fromtimestamp(ts, ET)
    return (d.hour - 9) * 60 + d.minute - 30


def market_hours_now():
    hm = datetime.now(ET).strftime("%H%M")
    return "0900" <= hm < "1605"


def inputs():
    """Fetch 1-minute SIP bars for each event day (event names + ETFs + SPY), daily bars, and news. After 16:05 ET."""
    if market_hours_now():
        sys.exit("market hours - rerun after 16:05 ET (shared data key)")
    from alpaca.data.historical import StockHistoricalDataClient, NewsClient
    from alpaca.data.requests import StockBarsRequest, NewsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import DataFeed, Adjustment
    k = json.load(open(ROOT / "secrets_data.json"))
    cl = StockHistoricalDataClient(k["alpaca_data_api_key"], k["alpaca_data_secret_key"])
    nc = NewsClient(k["alpaca_data_api_key"], k["alpaca_data_secret_key"])
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / "inputs.pkl"
    inp = pickle.load(open(p, "rb")) if p.exists() else {"bars": {}, "daily": {}, "news": {}, "failed": []}
    ev = events()
    by_day = defaultdict(set)
    for ts, day, sym, et in ev:
        by_day[day].add(sym)

    def call(fn, *a):
        for attempt in range(6):
            try:
                return fn(*a)
            except Exception as e:                  # noqa: BLE001
                msg = str(e)
                time.sleep(60 if "429" in msg or "too many" in msg.lower() else 5 * (attempt + 1))
        return None
    for n, (day, syms) in enumerate(sorted(by_day.items())):
        if day in inp["bars"]:
            continue
        s0 = datetime.fromisoformat(day + "T09:30").replace(tzinfo=ET)
        req = StockBarsRequest(symbol_or_symbols=sorted(syms | set(ETFS) | {"SPY"}), timeframe=TimeFrame.Minute,
                               start=s0, end=s0 + timedelta(minutes=390), feed=DataFeed.SIP, adjustment=Adjustment.RAW)
        got = call(cl.get_stock_bars, req)
        if got is None:
            inp["failed"].append(("bars", day))
            continue
        day_bars = {}
        for s, rows in (got.data or {}).items():
            day_bars[s] = {(r.timestamp.astimezone(ET).hour - 9) * 60 + r.timestamp.astimezone(ET).minute - 30:
                           (float(r.open), float(r.close)) for r in rows}
        inp["bars"][day] = day_bars
        if n % 10 == 0:
            pickle.dump(inp, open(p, "wb"))
            print("bars", day, len(day_bars), flush=True)
    syms_all = sorted({s for _, _, s, _ in ev} | set(ETFS) | {"SPY"})
    if not inp["daily"]:
        for i in range(0, len(syms_all), 200):
            ch = syms_all[i:i + 200]
            got = call(cl.get_stock_bars, StockBarsRequest(symbol_or_symbols=ch, timeframe=TimeFrame.Day,
                                                           start=datetime(2026, 4, 1, tzinfo=timezone.utc),
                                                           end=datetime(2026, 11, 20, tzinfo=timezone.utc),
                                                           feed=DataFeed.SIP, adjustment=Adjustment.ALL))
            if got is None:
                inp["failed"].append(("daily", i))
                continue
            for s, rows in (got.data or {}).items():
                inp["daily"][s] = {r.timestamp.astimezone(ET).strftime("%Y-%m-%d"): float(r.close) for r in rows}
        pickle.dump(inp, open(p, "wb"))
    for n, (ts, day, sym, et) in enumerate(ev):
        key = f"{day}|{sym}"
        if key in inp["news"]:
            continue
        rows = []
        # no limit: alpaca-py pages internally until exhausted (a 'limit' would silently cap it; review #1)
        got = call(nc.get_news, NewsRequest(symbols=sym, start=datetime.fromtimestamp(ts - 86400, timezone.utc),
                                            end=datetime.fromtimestamp(ts, timezone.utc)))
        if got is None:
            inp["failed"].append(("news", key))
        else:
            for it in (got.data or {}).get("news", []):
                rows.append({"t": it.created_at.timestamp(), "headline": it.headline, "n_symbols": len(it.symbols or [])})
        inp["news"][key] = rows
        if n % 50 == 0:
            pickle.dump(inp, open(p, "wb"))
            print("news", n, len(ev), flush=True)
    pickle.dump(inp, open(p, "wb"))
    print("INPUTS_DONE failed", len(inp["failed"]), flush=True)


# ---------------------------------------------------------------- building rows (pure given inputs)

def monitor_down_days():
    """Days with a >= 30-minute stretch in 09:30-16:00 ET with no journal record of any kind (parent amended_1 #1)."""
    from sentiment_backfill import JOURNAL
    bad = []
    for f in sorted(JOURNAL.glob("2026-*.jsonl")):
        ts = []
        for line in open(f):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            et = str(r.get("et", ""))
            if "09:30:00" <= et <= "16:00:00":
                h, m, s_ = (int(x) for x in et.split(":"))
                ts.append(h * 3600 + m * 60 + s_)
        ts = sorted([9 * 3600 + 1800] + ts + [16 * 3600])
        if max(np.diff(ts)) >= 1800:
            bad.append(f.stem)
    return set(bad)


def build_rows(inp, sent_cache, down=None):
    rows, counts = [], defaultdict(int)
    down = down if down is not None else set()
    for ts, day, sym, et in events():
        counts["events"] += 1
        if day in down:
            counts["monitor_down_day"] += 1
            continue
        b = inp["bars"].get(day, {})
        bars = b.get(sym)
        if not bars:
            counts["no_bars"] += 1
            continue
        m = minute_of(ts)
        ent = next((x for x in (m + 1, m + 2) if x in bars), None)
        if ent is None:
            counts["no_entry_bar"] += 1
            continue
        entry = bars[ent][0]
        if entry < MIN_PX:
            counts["under_2"] += 1
            continue
        closes = {k: v[1] for k, v in bars.items()}
        opens = {k: v[0] for k, v in bars.items()}
        ex = [k for k in closes if ent <= k <= ent + HOLD - 1]
        pre = [k for k in closes if m - 30 <= k <= m - 1]
        gross = (closes[max(ex)] / entry - 1) * 1e4 if ex else None
        pre_ret = (closes[max(pre)] / closes[min(pre)] - 1) * 1e4 if len(pre) >= 2 else None
        spy = {k: v[1] for k, v in b.get("SPY", {}).items()}
        etf = pick_etf(inp["daily"], sym, day)
        e15 = ret15({k: v[1] for k, v in b.get(etf, {}).items()}, m)
        s15 = ret15(spy, m)
        S = s_layer(sent_cache.get(f"{day}|{sym}"))
        N = n_layer(inp["news"].get(f"{day}|{sym}", []), ts) if f"{day}|{sym}" in inp["news"] else None
        R = (s15 is not None and e15 is not None and s15 > 0 and e15 > 0) if s15 is not None and e15 is not None else None
        rows.append({"day": day, "sym": sym, "m": m, "ent": ent, "entry": entry, "gross": gross, "pre": pre_ret,
                     "S": S, "N": N, "R": R, "etf": etf, "closes": closes, "opens": opens})
    return rows, dict(counts)


def cell_on(row, layers):
    vals = [row[x] for x in layers]
    if any(v is None for v in vals):
        return None
    return all(vals)


def power(rows):
    """ON/OFF counts per cell and half; projected MDE = 2.8 x day-clustered SE of matched differences computed on the
    PRE-event 30-minute return (no outcomes; review #4)."""
    out = {}
    for c, layers in CELLS.items():
        res = {}
        pre_rows = [dict(r, gross=r["pre"]) for r in rows]
        for h, sel in (("h1", lambda d: d < SPLIT), ("h2", lambda d: d >= SPLIT)):
            rr = [r for r in rows if sel(r["day"])]
            on = [r for r in rr if cell_on(r, layers) is True]
            off = [r for r in rr if cell_on(r, layers) is False]
            pairs = [(d, x) for d, x, t in matched_diffs([r for r in pre_rows if sel(r["day"])], layers) if x is not None]
            per = defaultdict(list)
            for d, x in pairs:
                per[d].append(x)
            m = [float(np.mean(v)) for v in per.values()]
            mde = 2.8 * float(np.std(m, ddof=1)) / math.sqrt(len(m)) if len(m) >= 3 else None
            res[h] = {"on": len(on), "off": len(off), "uncovered": len(rr) - len(on) - len(off), "matched_days": len(m),
                      "projected_mde_bp": round(mde, 1) if mde else None}
        res["unproven_by_design"] = any((res[h]["projected_mde_bp"] or 999) > MDE_MAX for h in ("h1", "h2"))
        out[c] = res
    for layer in ("S", "N", "R"):
        v = [r[layer] for r in rows if r[layer] is not None]
        out[f"{layer}_on_rate"] = round(float(np.mean(v)), 3) if v else None
    return out


def matched_diffs(rows, layers):
    """Each ON event minus the mean gross of OFF events the same day within +/-30 min (>= 2 peers), else +/-60;
    otherwise UNMATCHED (the same-day fallback without a time-of-day term counted as unmatched; review #5).
    -> list of (day, diff or None, tier). Pure."""
    by_day = defaultdict(list)
    for r in rows:
        if r["gross"] is not None and cell_on(r, layers) is not None:
            by_day[r["day"]].append(r)
    out = []
    for day, rr in by_day.items():
        off = [r for r in rr if cell_on(r, layers) is False]
        for r in rr:
            if cell_on(r, layers) is not True:
                continue
            for tier, w in (("30", 30), ("60", 60)):
                peers = [o["gross"] for o in off if abs(o["m"] - r["m"]) <= w]
                if len(peers) >= 2:
                    out.append((day, r["gross"] - float(np.mean(peers)), tier))
                    break
            else:
                out.append((day, None, "unmatched"))
    return out


def day_t(pairs):
    per = defaultdict(list)
    for d, x in pairs:
        if x is not None and np.isfinite(x):
            per[d].append(x)
    m = [float(np.mean(v)) for v in per.values()]
    if len(m) < 3 or np.std(m, ddof=1) == 0:
        return (float(np.mean(m)) if m else None), None, len(m)
    se = np.std(m, ddof=1) / math.sqrt(len(m))
    return float(np.mean(m)), float(np.mean(m) / se), len(m)


def hold_ret(r, u):
    """Same trade shape as the event: buy opens[u], sell the last close at or before u+29 (>= 10 minutes of window,
    else None). Pure."""
    if u not in r["opens"]:
        return None
    ks = [k for k in r["closes"] if u <= k <= u + HOLD - 1]
    if not ks or max(ks) - u < 9:
        return None
    return (r["closes"][max(ks)] / r["opens"][u] - 1) * 1e4


def later_and_unc(rows, layers):
    """Same-name later (entry minute in exit+2..exit+62, capped at 15:59) and unconditional (a random trending row,
    a random minute 09:35-15:00) - same trade shape as the event (review #2). -> later, unc, unc_g, later_g, dropped."""
    later, unc, unc_g, later_g, dropped = [], [], [], [], 0
    pool = [r for r in rows if r["gross"] is not None]
    for r in rows:
        if cell_on(r, layers) is not True or r["gross"] is None:
            continue
        start = r["ent"] + HOLD + 2
        cand = [u for u in range(start, min(start + 61, 389)) if hold_ret(r, u) is not None]
        if cand:
            u = cand[hkey("ms-later", r["day"], r["sym"], r["m"]) % len(cand)]
            g = hold_ret(r, u)
            later.append((r["day"], r["gross"] - g))
            later_g.append(g)
        else:
            dropped += 1
        if pool:
            o = pool[hkey("ms-unc", r["day"], r["sym"], r["m"]) % len(pool)]
            u = 5 + hkey("ms-unc-m", r["day"], r["sym"]) % (330 - 5)
            g = hold_ret(o, u)
            if g is not None:
                unc.append((r["day"], r["gross"] - g))
                unc_g.append(g)
    return later, unc, unc_g, later_g, dropped


def score_cell(rows, name, layers):
    pairs = matched_diffs(rows, layers)
    tiers = defaultdict(int)
    for _, _, t in pairs:
        tiers[t] += 1
    good = [(d, x) for d, x, t in pairs if x is not None]
    a = [(d, x) for d, x in good if d < SPLIT]
    b = [(d, x) for d, x in good if d >= SPLIT]
    ma, _, _ = day_t(a)
    mb, _, _ = day_t(b)
    mp, tp, nd = day_t(good)
    later, unc, unc_g, later_g, dropped = later_and_unc(rows, layers)
    ml, _, _ = day_t(later)
    mu, _, _ = day_t(unc)
    unm_h = {}
    for h, sel in (("h1", lambda d: d < SPLIT), ("h2", lambda d: d >= SPLIT)):
        ph = [t for d, _, t in pairs if sel(d)]
        unm_h[h] = (sum(1 for t in ph if t == "unmatched") / len(ph)) if ph else 1.0
    unmatched = max(unm_h.values())
    gap_ok = True
    if len(later_g) > 2 and len(unc_g) > 2:
        se = math.sqrt(np.var(later_g, ddof=1) / len(later_g) + np.var(unc_g, ddof=1) / len(unc_g))
        gap_ok = abs(np.mean(later_g) - np.mean(unc_g)) <= 2 * se
    on_g = [r["gross"] for r in rows if cell_on(r, layers) is True and r["gross"] is not None]
    st = {"n_on": len(pairs), "tiers": dict(tiers), "unmatched_share_by_half": {k: round(v, 3) for k, v in unm_h.items()},
          "later_gross_mean": float(np.mean(later_g)) if later_g else None, "later_dropped": dropped,
          "control_gap_ok": bool(gap_ok), "diff_h1": ma, "diff_h2": mb,
          "diff": mp, "t": tp, "days": nd, "later": ml, "unconditional": mu,
          "on_gross_mean": float(np.mean(on_g)) if on_g else None, "unc_gross_mean": float(np.mean(unc_g)) if unc_g else None}
    ok = (ma or -1) > 0 and (mb or -1) > 0 and (tp or 0) >= T_CRIT and (ml or -1) > 0 and (mu or -1) > 0
    st["EDGE"] = bool(ok)
    st["UNPROVEN_reason"] = ("unmatched > 30% in a half" if unmatched > 0.30 else
                             ("later vs unconditional control gap > 2 SE" if not gap_ok else None))
    return st


def load_sent():
    return json.loads(SENT.read_text()).get("events", {}) if SENT.exists() else {}


def run_power():
    inp = pickle.load(open(OUT / "inputs.pkl", "rb"))
    rows, counts = build_rows(inp, load_sent(), monitor_down_days())
    pw = power(rows)
    out = {"counts": counts, "power": pw, "inputs_failed": len(inp["failed"])}
    (OUT / "power.json").write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps(out, indent=1, default=str))


def run_score():
    inp = pickle.load(open(OUT / "inputs.pkl", "rb"))
    sent = load_sent()
    missing_valid = [k for k, v in sent.items() if "valid" not in v]
    assert not missing_valid, f"{len(missing_valid)} backfill entries lack the coverage flag - refetch first"
    down = monitor_down_days()
    rows, counts = build_rows(inp, sent, down)
    pw = power(rows)
    n_ev = max(counts.get("events", 1), 1)
    failed_share = len(inp["failed"]) / n_ev
    vfile = OUT / "validation.json"
    s_valid = json.loads(vfile.read_text()).get("agreement", 0) >= 0.90 if vfile.exists() else None
    out = {"prereg": "docs/studies/multisource_stack_prereg.json (amended_1)", "counts": counts, "power": pw,
           "monitor_down_days": sorted(down), "inputs_failed_share": round(failed_share, 4),
           "sentiment_events": len(sent), "sentiment_valid": sum(1 for v in sent.values() if v.get("valid")),
           "sentiment_live_validation": s_valid, "cells": {}}
    scored = []
    for c, layers in CELLS.items():
        st = score_cell(rows, c, layers)
        if pw[c]["unproven_by_design"] or ("N" in layers and (pw.get("N_on_rate") or 0) > 0.85):
            st["verdict"] = "UNPROVEN-by-design or non-discriminating (information only)"
        elif st["UNPROVEN_reason"]:
            st["verdict"] = "UNPROVEN (" + st["UNPROVEN_reason"] + ")"
        else:
            st["verdict"] = "EDGE (PROFITABLE check next)" if st["EDGE"] else "NO EDGE"
            scored.append(c)
        if "S" in layers and s_valid is not True:
            st["verdict"] += " - S layer pending live validation (amended_1 #3)"
        if c == "K3":
            st["info_R1_on_vs_unconditional"] = {"on_gross_mean": st["on_gross_mean"], "unconditional_gross_mean": st["unc_gross_mean"],
                                                 "note": "same-time matching cancels SPY; SPY-fallback names make R the SPY sign"}
        out["cells"][c] = st
    # canaries (review #7): 20 random-layer seeds (pass rate <= 10% per cell); +60 bp planted on each scored cell
    rates = {}
    for c, layers in CELLS.items():
        hits = 0
        for seed in range(20):
            rng = np.random.default_rng(seed)
            rr = [dict(r) for r in rows]
            for layer in ("S", "N", "R"):
                rate = pw.get(f"{layer}_on_rate") or 0.5
                for r in rr:
                    if r[layer] is not None:
                        r[layer] = bool(rng.random() < rate)
            hits += score_cell(rr, c, layers)["EDGE"]
        rates[c] = hits / 20
    plant = {}
    for c in scored:
        rp = [dict(r) for r in rows]
        for r in rp:
            if cell_on(r, CELLS[c]) is True and r["gross"] is not None:
                r["gross"] += 60.0
        plant[c] = score_cell(rp, c, CELLS[c])["EDGE"]
    out["canary_random_layer_pass_rates"] = rates
    out["canary_planted_found"] = plant
    canary_ok = all(v <= 0.10 for v in rates.values()) and all(plant.values())
    if failed_share > 0.02 or not canary_ok:
        out["VERDICT"] = "NO VERDICT (" + ("fetch failures > 2%" if failed_share > 0.02 else "canary failed") + ")"
    else:
        out["VERDICT"] = {c: v["verdict"] for c, v in out["cells"].items()}
    (OUT / "score.json").write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps({"VERDICT": out["VERDICT"], "canaries": [rates, plant],
                      "cells": {c: {k: v.get(k) for k in ("n_on", "diff_h1", "diff_h2", "t", "later", "unconditional")}
                                for c, v in out["cells"].items()}}, indent=1, default=str))


if __name__ == "__main__":
    {"inputs": inputs, "power": run_power, "score": run_score}[sys.argv[1]]()
