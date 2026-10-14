#!/usr/bin/env python3
"""Sector lead-lag. Spec: docs/studies/sector_laglead_prereg.json.

When a sector ETF moves up on its own (vs SPY), buy its members that have not
moved yet, at the next minute's SIP open, hold 10 minutes.

  --fetch    industries (Finnhub, cached), daily bars, IEX + SIP minute bars for
             the universe and the ETFs. Mini only, after 16:15 ET, throttled.
  (default)  inventory only, no returns
  --score    after the prereg has a result-skeptic READY

Board, days and scoring helpers come from early_entry_honest.py.
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import random
import statistics
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import early_entry_honest as E  # noqa: E402

ROOT = E.ROOT
sys.path.insert(0, ROOT)
OUT_DIR = os.path.join(ROOT, "ai_reports", "sector_laglead")
INDUSTRY = os.path.join(ROOT, "ai_reports", "industry_cache.json")
DAILY = os.path.join(OUT_DIR, "daily.pkl")

ETFS = ("SMH", "XLK", "XLC", "XLV", "XLF", "XLE", "XLU", "XLRE", "XLY", "XLP", "XLI", "XLB")
SPY = "SPY"
WIN_START, WIN_END = 9 * 60 + 50, 15 * 60 + 20
LOOK = 5
ETF_MIN, REL_MIN = 0.0025, 0.0010
LAG_MAX, LAG_MIN = 0.0005, -0.0050
LEADER_MIN = 0.0050
REFRACT = 30
HOLD = 10
COST = 0.0010
PX_MIN, ADV_MIN, ADV_N = 10.0, 20e6, 20
MIN_BOARD_DAYS = 3
MIN_FIRES, MIN_DAYS = 100, 10
SPLIT_END_A = "2026-09-24"
MAX_MISS = 0.05


# ── pure ─────────────────────────────────────────────────────────────────────

def etf_for(industry: str | None) -> str | None:
    """tools/studies/sector_study.MAP, first keyword match wins."""
    if not industry:
        return None
    import sector_study as SS
    low = industry.lower()
    for keys, etf in SS.MAP:
        if any(k in low for k in keys):
            return etf
    return None


def close_at(series: dict, minute: int, back: int = 15) -> float | None:
    """Last close at or before `minute` within `back` minutes. series: {minute: (open, close)}."""
    for m in range(minute, minute - back - 1, -1):
        if m in series:
            return series[m][1]
    return None


def ret5(series: dict, m: int) -> float | None:
    a, b = close_at(series, m - LOOK), close_at(series, m)
    if not a or b is None:
        return None
    return b / a - 1


def sector_moves(etf: dict, spy: dict, m: int, etf_min: float = ETF_MIN) -> bool:
    r, s = ret5(etf, m), ret5(spy, m)
    eps = 1e-9            # thresholds are inclusive; float noise must not flip an on-the-bar minute
    return r is not None and s is not None and r + eps >= etf_min and (r - s) + eps >= REL_MIN


def is_laggard(stock: dict, m: int) -> bool:
    r = ret5(stock, m)
    return r is not None and LAG_MIN - 1e-9 <= r <= LAG_MAX + 1e-9


def fires(minutes: list[int], etf: dict, spy: dict, members: dict[str, dict],
          etf_min: float = ETF_MIN) -> list[tuple[str, int]]:
    """(symbol, signal minute) for laggards at qualifying minutes, 30-min refractory per symbol."""
    last: dict[str, int] = {}
    out = []
    for m in minutes:
        if not (WIN_START <= m <= WIN_END) or not sector_moves(etf, spy, m, etf_min):
            continue
        for sym in sorted(members):
            if sym in last and m - last[sym] < REFRACT:
                continue
            if is_laggard(members[sym], m):
                out.append((sym, m))
                last[sym] = m
    return out


def adv_ok(rows: list, day: str) -> tuple[bool | None, float | None]:
    prior = [r for r in rows if r[0] < day]
    if len(prior) < ADV_N:
        return None, None
    pc = prior[-1][1]
    return statistics.mean(c * v for _d, c, v in prior[-ADV_N:]) >= ADV_MIN, pc


def half_verdict(net, t, net20, n, days) -> str:
    if n < MIN_FIRES or days < MIN_DAYS or net is None or t is None:
        return "UNDERPOWERED"
    return "PASS" if (net > 0 and t >= 2.0 and net20 is not None and net20 > 0) else "FAIL"


# ── data (mini, after 16:15) ─────────────────────────────────────────────────

def universe(datas: list[dict]) -> list[str]:
    seen = Counter()
    for d in datas:
        for s in d["board"]:
            seen[s] += 1
    return sorted(s for s, n in seen.items() if n >= MIN_BOARD_DAYS)


def _wait_after_close() -> None:
    while True:
        m = E._et_min(time.time())
        if m >= 16 * 60 + 15 or m < 3 * 60 + 30:
            return
        print("  waiting for 16:15 ET (the desk shares the data budget)", file=sys.stderr, flush=True)
        time.sleep(300)


def fetch_industries(syms: list[str]) -> dict:
    try:
        cache = json.load(open(INDUSTRY))
    except Exception:  # noqa: BLE001
        cache = {}
    from config import load_config
    key = load_config().get("finnhub_key")
    todo = [s for s in syms if s not in cache]
    print(f"industry lookups: {len(todo)}", flush=True)
    for i, s in enumerate(todo):
        _wait_after_close()
        url = "https://finnhub.io/api/v1/stock/profile2?" + urllib.parse.urlencode({"symbol": s, "token": key})
        for attempt in range(4):
            try:
                with urllib.request.urlopen(url, timeout=10) as r:
                    cache[s] = (json.loads(r.read() or b"{}") or {}).get("finnhubIndustry") or ""
                break
            except Exception as e:  # noqa: BLE001
                if "429" in str(e):
                    time.sleep(30)
                    continue
                print(f"  {s}: {str(e)[:60]}", file=sys.stderr)
                break
        time.sleep(2.0)                      # 30/min, half the free Finnhub limit
        if i % 50 == 0:
            json.dump(cache, open(INDUSTRY, "w"))
    json.dump(cache, open(INDUSTRY, "w"))
    return cache


def do_fetch(datas: list[dict]) -> None:
    import bars
    import board_names_study as BN
    os.makedirs(OUT_DIR, exist_ok=True)
    _wait_after_close()
    syms = universe(datas)
    fetch_industries(syms)
    want = sorted(set(syms) | set(ETFS) | {SPY})
    if not os.path.exists(DAILY):
        errs: list = []
        days = sorted(d["day"] for d in datas)
        daily = BN.fetch_daily(want, "2026-07-15", (datetime.fromisoformat(days[-1]) + timedelta(days=1)).strftime("%Y-%m-%d"), errs)
        if errs:
            raise SystemExit(f"daily fetch errors {errs}; not cached, rerun")
        pickle.dump(daily, open(DAILY, "wb"))
    cl = bars.client()
    for d in datas:
        p = os.path.join(OUT_DIR, f"{d['day']}.pkl")
        if os.path.exists(p):
            continue
        _wait_after_close()
        errs = []
        sip = E._fetch_feed(cl, want, d["day"], "sip", errs)
        iex = E._fetch_feed(cl, want, d["day"], "iex", errs)
        if errs:
            print(f"{d['day']}: fetch errors {errs} - not cached", flush=True)
            continue
        pickle.dump({"sip": sip, "iex": iex}, open(p, "wb"))
        print(f"{d['day']}: sip {len(sip)}/{len(want)} iex {len(iex)}/{len(want)}", flush=True)


# ── scoring ──────────────────────────────────────────────────────────────────

def build(datas: list[dict]) -> tuple[list[dict], Counter]:
    industry = json.load(open(INDUSTRY)) if os.path.exists(INDUSTRY) else {}
    daily = pickle.load(open(DAILY, "rb")) if os.path.exists(DAILY) else {}
    syms = universe(datas)
    sector = {s: etf_for(industry.get(s)) for s in syms}
    drops = Counter(unmapped=sum(1 for s in syms if not sector[s]))
    rows = []
    for d in datas:
        day = d["day"]
        p = os.path.join(OUT_DIR, f"{day}.pkl")
        if not os.path.exists(p):
            drops["day_missing"] += 1
            continue
        cache = pickle.load(open(p, "rb"))
        sip = {s: E.sip_map(f) for s, f in cache["sip"].items() if f is not None}
        iex = {s: E.sip_map(f) for s, f in cache["iex"].items() if f is not None}
        if SPY not in iex or SPY not in sip:
            drops["no_spy"] += 1
            continue
        members_by_etf = defaultdict(dict)
        for s in syms:
            etf = sector[s]
            if not etf:
                continue
            ok, pc = adv_ok(daily.get(s, []), day)
            drops["name_days"] += 1
            if ok is None:
                drops["adv_unknown"] += 1
                continue
            if not ok or (pc is not None and pc < PX_MIN):
                drops["thin_or_cheap"] += 1
                continue
            if s not in iex or s not in sip:
                drops["no_bars"] += 1
                continue
            members_by_etf[etf][s] = iex[s]
        minutes = list(range(WIN_START, WIN_END + 1))
        for etf, members in members_by_etf.items():
            if etf not in iex or etf not in sip:
                drops["no_etf_bars"] += 1
                continue
            for sym, m in fires(minutes, iex[etf], iex[SPY], members):
                g = E.markout(sip[sym], m + 1)
                if g is None:
                    drops["no_exit_bar"] += 1
                    continue
                rows.append({"day": day, "sym": sym, "etf": etf, "m": m, "gross": g, "sip": sip[sym],
                             "etf_sip": sip[etf], "members": members, "sips": sip,
                             "px": close_at(sip[sym], m)})
    return rows, drops


def _bp(x):
    return "—" if x is None else f"{x * 1e4:+.1f}"


def _t(x):
    return "—" if x is None else f"{x:+.2f}"


def report(rows: list[dict], drops: Counter) -> str:
    L = ["# Sector lead-lag", "", f"fires {len(rows)}; drops {dict(drops)}", "",
         "| half | fires | sessions | gross | net (10 bp) | t | net (20 bp) | verdict |",
         "|---|---:|---:|---:|---:|---:|---:|---|"]
    verdicts = []
    for h in "AB":
        part = [r for r in rows if (r["day"] <= SPLIT_END_A) == (h == "A")]
        by = defaultdict(list)
        for r in part:
            by[r["day"]].append(r["gross"])
        g = E.day_equal(by)[0]
        n10 = E.day_equal({d: [x - COST for x in v] for d, v in by.items()})
        n20 = E.day_equal({d: [x - 2 * COST for x in v] for d, v in by.items()})[0]
        v = half_verdict(n10[0], n10[1], n20, n10[2], n10[3])
        verdicts.append(v)
        L.append(f"| {h} | {n10[2]} | {n10[3]} | {_bp(g)} | {_bp(n10[0])} | {_t(n10[1])} | {_bp(n20)} | {v} |")
    final = "PASS" if all(v == "PASS" for v in verdicts) else ("FAIL" if "FAIL" in verdicts else "UNDERPOWERED")
    L += ["", f"**Sector lead-lag: {final}**", "", "### Information only", ""]

    def line(label, part, fn):
        by = defaultdict(list)
        for r in part:
            x = fn(r)
            if x is not None:
                by[r["day"]].append(x)
        mu, t, n, nd = E.day_equal(by)
        L.append(f"- {label}: {_bp(mu)} bp (t {_t(t)}, n {n}, {nd} sessions)")

    line("lift vs a later same-name minute", rows, lambda r: (
        None if (k := E.control_minute(r["sym"], r["day"], "LAG", r["sip"], set(), r["m"] + 1)) is None
        else r["gross"] - E.markout(r["sip"], k)))

    def vs_leaders(r):
        lead = [s for s, ser in r["members"].items() if s != r["sym"] and (x := ret5(ser, r["m"])) is not None
                and x >= LEADER_MIN and s in r["sips"]]
        rng = random.Random(f"{r['sym']}|{r['day']}|{r['m']}|leader")
        rng.shuffle(lead)
        for s in lead:
            x = E.markout(r["sips"][s], r["m"] + 1)
            if x is not None:
                return r["gross"] - x
        return None
    line("lift vs a same-sector LEADER at the same minute", rows, vs_leaders)
    line("lift vs the sector ETF's next 10 minutes (beta)", rows, lambda r: (
        None if (x := E.markout(r["etf_sip"], r["m"] + 1)) is None else r["gross"] - x))
    line("30-minute markout", rows, lambda r: E.markout(r["sip"], r["m"] + 1, 30))
    line("price >= $30", [r for r in rows if r["px"] and r["px"] >= 30], lambda r: r["gross"])
    for etf in ETFS:
        part = [r for r in rows if r["etf"] == etf]
        if part:
            line(f"sector {etf}", part, lambda r: r["gross"])
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--score", action="store_true")
    a = ap.parse_args()
    datas, _excluded = E.load_days()
    if a.fetch:
        do_fetch(datas)
        return
    syms = universe(datas)
    industry = json.load(open(INDUSTRY)) if os.path.exists(INDUSTRY) else {}
    mapped = sum(1 for s in syms if etf_for(industry.get(s)))
    have = sum(1 for d in datas if os.path.exists(os.path.join(OUT_DIR, f"{d['day']}.pkl")))
    print(f"universe {len(syms)} names (board on >= {MIN_BOARD_DAYS} days), industry known "
          f"{sum(1 for s in syms if s in industry)}, mapped to an ETF {mapped}; bar days {have}/{len(datas)}")
    if not a.score:
        print("(inventory only; --score after the prereg has a result-skeptic READY)")
        return
    rows, drops = build(datas)
    lost = drops["adv_unknown"] + drops["no_bars"]
    if drops["name_days"] and lost > MAX_MISS * drops["name_days"]:
        raise SystemExit(f"ABORT: {lost}/{drops['name_days']} name-days lack bars or ADV (> 5%). {dict(drops)}")
    if drops["day_missing"]:
        raise SystemExit(f"ABORT: {drops['day_missing']} days without bars; rerun --fetch")
    md = report(rows, drops)
    print(md)
    with open(os.path.join(OUT_DIR, "result.md"), "w") as f:
        f.write(md + "\n")


if __name__ == "__main__":
    main()
