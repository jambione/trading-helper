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
        rows, page = [], None
        while True:
            kw = {"page_token": page} if page else {}
            got = call(nc.get_news, NewsRequest(symbols=sym, start=datetime.fromtimestamp(ts - 86400, timezone.utc),
                                                end=datetime.fromtimestamp(ts, timezone.utc), limit=50, **kw))
            if got is None:
                inp["failed"].append(("news", key))
                break
            items = (got.data or {}).get("news", []) if hasattr(got, "data") else []
            for it in items:
                rows.append({"t": it.created_at.timestamp(), "headline": it.headline, "n_symbols": len(it.symbols or [])})
            page = getattr(got, "next_page_token", None)
            if not page:
                break
        inp["news"][key] = rows
        if n % 50 == 0:
            pickle.dump(inp, open(p, "wb"))
            print("news", n, len(ev), flush=True)
    pickle.dump(inp, open(p, "wb"))
    print("INPUTS_DONE failed", len(inp["failed"]), flush=True)


# ---------------------------------------------------------------- building rows (pure given inputs)

def build_rows(inp, sent_cache):
    rows, counts = [], defaultdict(int)
    for ts, day, sym, et in events():
        counts["events"] += 1
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
                     "S": S, "N": N, "R": R, "etf": etf, "closes": closes})
    return rows, dict(counts)


def cell_on(row, layers):
    vals = [row[x] for x in layers]
    if any(v is None for v in vals):
        return None
    return all(vals)


def power(rows):
    """ON/OFF counts per cell and half, projected MDE from the pre-event 30-minute return SD (no outcomes)."""
    out = {}
    for c, layers in CELLS.items():
        res = {}
        for h, sel in (("h1", lambda r: r["day"] < SPLIT), ("h2", lambda r: r["day"] >= SPLIT)):
            rr = [r for r in rows if sel(r)]
            on = [r for r in rr if cell_on(r, layers) is True]
            off = [r for r in rr if cell_on(r, layers) is False]
            pre = [r["pre"] for r in on + off if r["pre"] is not None]
            sd = float(np.std(pre, ddof=1)) if len(pre) > 2 else None
            mde = 2.8 * sd * math.sqrt(1 / max(len(on), 1) + 1 / max(len(off), 1)) * 1.2 if sd else None
            res[h] = {"on": len(on), "off": len(off), "uncovered": len(rr) - len(on) - len(off),
                      "pre_sd_bp": round(sd, 1) if sd else None, "projected_mde_bp": round(mde, 1) if mde else None}
        res["unproven_by_design"] = any((res[h]["projected_mde_bp"] or 999) > MDE_MAX for h in ("h1", "h2"))
        out[c] = res
    for layer in ("S", "N", "R"):
        v = [r[layer] for r in rows if r[layer] is not None]
        out[f"{layer}_on_rate"] = round(float(np.mean(v)), 3) if v else None
    return out


def matched_diffs(rows, layers):
    """Each ON event minus the mean gross of OFF events the same day within +/-30 min (>= 2 peers), else +/-60, else
    the same day's OFF mean (flagged). -> list of (day, diff, tier). Pure."""
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
                if off:
                    out.append((day, r["gross"] - float(np.mean([o["gross"] for o in off])), "day"))
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


def later_and_unc(rows, layers):
    """Same-name later (entry at exit+2..exit+62 minutes, same 30-min hold) and unconditional (a random RTH minute
    09:35-15:00 on the same trending name-days) differences for ON events. Pure."""
    later, unc, unc_g = [], [], []
    pool = [r for r in rows if r["gross"] is not None]
    for r in rows:
        if cell_on(r, layers) is not True or r["gross"] is None:
            continue
        c = r["closes"]
        start = r["ent"] + HOLD + 2
        cand = [u for u in range(start, min(start + 60, 359)) if u in c and u + HOLD - 1 in c]
        if cand:
            u = cand[hkey("ms-later", r["day"], r["sym"], r["m"]) % len(cand)]
            later.append((r["day"], r["gross"] - (c[u + HOLD - 1] / c[u] - 1) * 1e4))
        if pool:
            o = pool[hkey("ms-unc", r["day"], r["sym"], r["m"]) % len(pool)]
            u = 5 + hkey("ms-unc-m", r["day"], r["sym"]) % (330 - 5)
            if u in o["closes"] and u + HOLD - 1 in o["closes"]:
                g = (o["closes"][u + HOLD - 1] / o["closes"][u] - 1) * 1e4
                unc.append((r["day"], r["gross"] - g))
                unc_g.append(g)
    return later, unc, unc_g


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
    later, unc, unc_g = later_and_unc(rows, layers)
    ml, _, _ = day_t(later)
    mu, _, _ = day_t(unc)
    unmatched = tiers["unmatched"] / max(len(pairs), 1)
    on_g = [r["gross"] for r in rows if cell_on(r, layers) is True and r["gross"] is not None]
    st = {"n_on": len(pairs), "tiers": dict(tiers), "unmatched_share": round(unmatched, 3), "diff_h1": ma, "diff_h2": mb,
          "diff": mp, "t": tp, "days": nd, "later": ml, "unconditional": mu,
          "on_gross_mean": float(np.mean(on_g)) if on_g else None, "unc_gross_mean": float(np.mean(unc_g)) if unc_g else None}
    ok = (ma or -1) > 0 and (mb or -1) > 0 and (tp or 0) >= T_CRIT and (ml or -1) > 0 and (mu or -1) > 0 and unmatched <= 0.30
    st["EDGE"] = bool(ok)
    return st


def load_sent():
    return json.loads(SENT.read_text()).get("events", {}) if SENT.exists() else {}


def run_power():
    inp = pickle.load(open(OUT / "inputs.pkl", "rb"))
    rows, counts = build_rows(inp, load_sent())
    pw = power(rows)
    out = {"counts": counts, "power": pw, "inputs_failed": len(inp["failed"])}
    (OUT / "power.json").write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps(out, indent=1, default=str))


def run_score():
    inp = pickle.load(open(OUT / "inputs.pkl", "rb"))
    sent = load_sent()
    rows, counts = build_rows(inp, sent)
    pw = power(rows)
    out = {"prereg": "docs/studies/multisource_stack_prereg.json (amended_1)", "counts": counts, "power": pw,
           "sentiment_events": len(sent), "sentiment_valid": sum(1 for v in sent.values() if v.get("valid")), "cells": {}}
    for c, layers in CELLS.items():
        if pw[c]["unproven_by_design"] or ("N" in layers and (pw.get("N_on_rate") or 0) > 0.85):
            out["cells"][c] = {"verdict": "UNPROVEN-by-design or non-discriminating (not scored for EDGE)",
                               "info": score_cell(rows, c, layers)}
            continue
        st = score_cell(rows, c, layers)
        st["verdict"] = "EDGE (PROFITABLE check next)" if st["EDGE"] else "NO EDGE"
        out["cells"][c] = st
    # canaries: random layers at the same ON rates; planted +60 bp on K6 ON events
    rng = np.random.default_rng(20261120)
    rr = [dict(r) for r in rows]
    for layer in ("S", "N", "R"):
        rate = pw.get(f"{layer}_on_rate") or 0.5
        for r in rr:
            if r[layer] is not None:
                r[layer] = bool(rng.random() < rate)
    out["canary_random_layers_edge"] = {c: score_cell(rr, c, l)["EDGE"] for c, l in CELLS.items()}
    rp = [dict(r) for r in rows]
    for r in rp:
        if cell_on(r, CELLS["K6"]) is True and r["gross"] is not None:
            r["gross"] += 60.0
    out["canary_planted_K6_edge"] = score_cell(rp, "K6", CELLS["K6"])["EDGE"]
    (OUT / "score.json").write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps({c: (v.get("verdict"), v.get("diff_h1") or (v.get("info") or {}).get("diff_h1"),
                          v.get("diff_h2") or (v.get("info") or {}).get("diff_h2"), v.get("t") or (v.get("info") or {}).get("t"))
                      for c, v in out["cells"].items()}, indent=1, default=str))
    print("canaries", out["canary_random_layers_edge"], out["canary_planted_K6_edge"])


if __name__ == "__main__":
    {"inputs": inputs, "power": run_power, "score": run_score}[sys.argv[1]]()
