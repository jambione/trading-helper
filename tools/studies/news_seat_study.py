#!/usr/bin/env python3
"""News-triggered seating. Spec: docs/studies/news_seat_prereg.json.

A fresh single-symbol Benzinga headline (via Alpaca news) lands during the
session; if the price confirms it (+0.20% from the minute before the headline
to the minute before entry), buy at the SIP open two minutes after the
headline's minute and hold 10 minutes.

  --fetch-news   headlines 04:00-15:30 ET per weekday (mini: Alpaca keys)
  --fetch-bars   SIP minute bars for event symbols + daily bars (mini)
  (default)      inventory only, no returns
  --score        after the prereg has a result-skeptic READY

The live desk shares the Alpaca data budget: fetches run at <= 20 req/min,
back off on 429 and pause from 09:15 to 16:15 ET.
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import random
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import early_entry_honest as E  # noqa: E402

ROOT = E.ROOT
sys.path.insert(0, ROOT)
OUT_DIR = os.path.join(ROOT, "ai_reports", "news_seat")
NEWS_DIR = os.path.join(OUT_DIR, "news")
BARS_DIR = os.path.join(OUT_DIR, "bars")
DAILY = os.path.join(OUT_DIR, "daily.pkl")

FIRST_DAY, LAST_DAY = "2026-07-01", "2026-10-13"
WIN_START, WIN_END = 9 * 60 + 45, 15 * 60 + 20          # created_at in [09:45, 15:20)
FRESH_MIN = 60
ENTRY_LAG = 2
REACT_MIN = 0.0020
PX_MIN, ADV_MIN, ADV_N = 10.0, 10e6, 20
HOLD = 10
COST = 0.0010
MIN_EVENTS, MIN_DAYS = 100, 20
SYM_RE = re.compile(r"^[A-Z]{1,5}$")
REACTION_RE = re.compile(r"why is|shares are trading|moving|stocks moving|trading (?:higher|lower)", re.I)
ANALYST = re.compile(r"initiat|reiterat|maintain|price target|upgrade|downgrade|rating", re.I)
PAUSE = (9 * 60 + 15, 16 * 60 + 15)
MIN_GAP_SEC = 3.0


# ── pure ─────────────────────────────────────────────────────────────────────

def eligible_events(news: list[dict]) -> list[dict]:
    """news: [{id, ts, symbols, headline}] for one day, any order.
    Returns eligible events with t0 (ET minute). Freshness uses every article,
    premarket included."""
    ordered = sorted(news, key=lambda x: (x["ts"], str(x.get("id"))))
    prior_ts: dict[str, list[float]] = defaultdict(list)   # earlier articles in (ts, id) order
    out = []
    for n in ordered:
        fresh = {s: not any(n["ts"] - FRESH_MIN * 60 <= ts <= n["ts"] for ts in prior_ts[s])
                 for s in n["symbols"]}
        for s in n["symbols"]:
            prior_ts[s].append(n["ts"])
        if len(n["symbols"]) != 1 or not SYM_RE.match(n["symbols"][0]):
            continue
        t0 = E._et_min(n["ts"])
        if not (WIN_START <= t0 < WIN_END):
            continue
        s = n["symbols"][0]
        # A same-second second article sees the first as prior: one event, not two.
        if not fresh[s]:
            continue
        out.append({"sym": s, "ts": n["ts"], "t0": t0, "headline": n.get("headline", ""), "id": n.get("id")})
    return out


def iex_close_at(iex: dict, minute: int, back: int = 15) -> float | None:
    """Last IEX close at or before `minute`, within `back` minutes (IEX is thin)."""
    for m in range(minute, minute - back - 1, -1):
        if m in iex:
            return iex[m][1]
    return None


def evaluate(ev: dict, sip: dict, lag: int = ENTRY_LAG, hold: int = HOLD,
             iex: dict | None = None) -> dict | None:
    """sip / iex: {minute: (open, close)}. None when a needed SIP bar is missing.

    react (SIP) is information: the live desk only has delayed SIP. react_iex is
    what the desk could compute live: last IEX close at or before t0-1 vs at or
    before e-1, the e-1 print strictly after the pre print."""
    t0 = ev["t0"]
    e = t0 + lag
    pre = sip.get(t0 - 1)
    react_bar = sip.get(e - 1)
    if pre is None or react_bar is None or not pre[1]:
        return None
    g = E.markout(sip, e, hold)
    if g is None:
        return None
    r_iex = None
    if iex is not None:
        a = iex_close_at(iex, t0 - 1)
        b = iex.get(e - 1) or iex.get(t0)       # a print after the headline minute began
        if a and b:
            r_iex = b[1] / a - 1
    return {"pre_px": pre[1], "react": react_bar[1] / pre[1] - 1, "react_iex": r_iex,
            "gross": g, "entry_min": e}


def runway(sip: dict, e: int, span: int = 30) -> dict | None:
    a = sip.get(e)
    if a is None or not a[0]:
        return None
    path = [sip[m][1] / a[0] - 1 for m in range(e, e + span) if m in sip and m < E.RTH_END]
    if not path:
        return None
    hit = {}
    for up in (0.005, 0.01):
        hit[up] = None
        for x in path:
            if x <= -0.005:
                hit[up] = False
                break
            if x >= up:
                hit[up] = True
                break
        if hit[up] is None:
            hit[up] = False
    return {"best": max(path), "up05": hit[0.005], "up1": hit[0.01]}


def adv_ok(daily_rows: list, day: str) -> bool | None:
    prior = [r for r in daily_rows if r[0] < day][-ADV_N:]
    if len(prior) < ADV_N:
        return None
    return statistics.mean(c * v for _d, c, v in prior) >= ADV_MIN


def half_verdict(net, t, n, days, net20=None) -> str:
    """net / t at 10 bp; net20 = day-equal net at 20 bp, which must also be > 0."""
    if n < MIN_EVENTS or days < MIN_DAYS or net is None or t is None:
        return "UNDERPOWERED"
    ok20 = net20 is None or net20 > 0
    return "PASS" if net > 0 and t >= 2.0 and ok20 else "FAIL"


# ── fetching (mini) ──────────────────────────────────────────────────────────

_last_req = [0.0]


def _guard() -> None:
    """Pause during the session; keep >= MIN_GAP_SEC between requests."""
    while True:
        m = E._et_min(time.time())
        if not (PAUSE[0] <= m < PAUSE[1]):
            break
        print(f"  paused for the session ({m // 60:02d}:{m % 60:02d} ET)", file=sys.stderr, flush=True)
        time.sleep(300)
    wait = MIN_GAP_SEC - (time.time() - _last_req[0])
    if wait > 0:
        time.sleep(wait)
    _last_req[0] = time.time()


def _headers() -> dict:
    sec = json.load(open(os.path.join(ROOT, "config", "secrets.json")))
    return {"APCA-API-KEY-ID": sec["api_key"], "APCA-API-SECRET-KEY": sec["secret_key"]}


def fetch_news_day(day: str, headers: dict) -> list[dict] | None:
    import requests
    d = datetime.fromisoformat(day).replace(tzinfo=E.ET)
    start = d.replace(hour=4).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = d.replace(hour=15, minute=30).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out, tok, pages = [], None, 0
    while pages < 400:
        p = {"start": start, "end": end, "limit": 50, "sort": "asc", "include_content": "false"}
        if tok:
            p["page_token"] = tok
        for attempt in range(10):
            _guard()
            try:
                r = requests.get("https://data.alpaca.markets/v1beta1/news", headers=headers, params=p, timeout=30)
            except Exception as e:  # noqa: BLE001
                print(f"  {day} news error {str(e)[:60]}", file=sys.stderr)
                time.sleep(10)
                continue
            if r.status_code == 429:
                time.sleep(30)
                continue
            if r.status_code != 200:
                print(f"  {day} news HTTP {r.status_code}", file=sys.stderr)
                return None
            break
        else:
            return None
        j = r.json()
        for n in j.get("news", []):
            out.append({"id": n.get("id"), "ts": datetime.fromisoformat(n["created_at"].replace("Z", "+00:00")).timestamp(),
                        "symbols": [str(s).upper() for s in n.get("symbols") or []], "headline": n.get("headline", "")})
        tok = j.get("next_page_token")
        pages += 1
        if not tok:
            return out
    print(f"  {day}: page cap hit", file=sys.stderr)
    return None


def weekdays(a: str, b: str) -> list[str]:
    d, e, out = date.fromisoformat(a), date.fromisoformat(b), []
    while d <= e:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def do_fetch_news() -> None:
    os.makedirs(NEWS_DIR, exist_ok=True)
    h = _headers()
    for day in weekdays(FIRST_DAY, LAST_DAY):
        p = os.path.join(NEWS_DIR, f"{day}.json")
        if os.path.exists(p):
            continue
        rows = fetch_news_day(day, h)
        if rows is None:
            print(f"{day}: FAILED (not cached; rerun resumes)", flush=True)
            continue
        json.dump(rows, open(p, "w"))
        print(f"{day}: {len(rows)} articles", flush=True)


def load_news(day: str) -> list[dict] | None:
    p = os.path.join(NEWS_DIR, f"{day}.json")
    return json.load(open(p)) if os.path.exists(p) else None


def do_fetch_bars() -> None:
    import bars
    import board_names_study as BN
    os.makedirs(BARS_DIR, exist_ok=True)
    cl = bars.client()
    if cl is None:
        raise SystemExit("no Alpaca data client; run on the mini")
    days = [d for d in weekdays(FIRST_DAY, LAST_DAY) if load_news(d) is not None]
    allsyms = set()
    for day in days:
        evs = eligible_events(load_news(day))
        syms = sorted({e["sym"] for e in evs})
        allsyms |= set(syms)
        p = os.path.join(BARS_DIR, f"{day}.pkl")
        if os.path.exists(p) or not syms:
            continue
        errs: list = []
        _guard()
        sip = E._fetch_feed(cl, syms + ["SPY"], day, "sip", errs)
        _guard()
        iex = E._fetch_feed(cl, syms, day, "iex", errs)
        if errs:
            print(f"{day}: fetch errors {errs} - NOT cached; rerun resumes", flush=True)
            continue
        pickle.dump({"sip": sip, "iex": iex, "want": syms}, open(p, "wb"))
        print(f"{day}: sip {len(sip)}/{len(syms) + 1}, iex {len(iex)}/{len(syms)}", flush=True)
    if not os.path.exists(DAILY):
        errs = []
        _guard()
        daily = BN.fetch_daily(sorted(allsyms), "2026-05-15", (date.fromisoformat(LAST_DAY) + timedelta(days=1)).isoformat(), errs)
        if errs:
            print(f"daily: fetch errors {errs} - NOT cached; rerun", flush=True)
            return
        pickle.dump(daily, open(DAILY, "wb"))
        print(f"daily: {len(daily)}/{len(allsyms)}")


# ── scoring ──────────────────────────────────────────────────────────────────

def build(days: list[str], daily: dict) -> tuple[list[dict], Counter]:
    rows, drops = [], Counter()
    for day in days:
        news = load_news(day)
        bp = os.path.join(BARS_DIR, f"{day}.pkl")
        if news is None or not os.path.exists(bp):
            drops["day_missing"] += 1
            continue
        cache = pickle.load(open(bp, "rb"))
        sips = {s: E.sip_map(f) for s, f in cache["sip"].items() if f is not None}
        iexs = {s: E.sip_map(f) for s, f in cache.get("iex", {}).items() if f is not None}
        spy = sips.get("SPY")
        for ev in eligible_events(news):
            drops["eligible"] += 1
            sip = sips.get(ev["sym"])
            if sip is None:
                drops["no_bars"] += 1
                continue
            r = evaluate(ev, sip, iex=iexs.get(ev["sym"], {}))
            if r is None:
                drops["no_bar"] += 1
                continue
            if r["pre_px"] < PX_MIN:
                drops["under_10"] += 1
                continue
            ok = adv_ok(daily.get(ev["sym"], []), day)
            if ok is None:
                drops["adv_unknown"] += 1
                continue
            if not ok:
                drops["adv_low"] += 1
                continue
            r.update(ev)
            r["day"] = day
            r["sip"] = sip
            r["spy"] = spy
            rows.append(r)
    return rows, drops


def _bp(x):
    return "—" if x is None else f"{x * 1e4:+.1f}"


def _t(x):
    return "—" if x is None else f"{x:+.2f}"


def de(vals_by_day: dict):
    return E.day_equal({d: v for d, v in vals_by_day.items() if v})


def report(rows: list[dict], drops: Counter) -> str:
    # Graded: the IEX reaction (what the desk can compute live). SIP react is information.
    up = [r for r in rows if r["react_iex"] is not None and r["react_iex"] >= REACT_MIN]
    up_sip = [r for r in rows if r["react"] >= REACT_MIN]
    days = sorted({r["day"] for r in up})
    cut = days[(len(days) - 1) // 2] if days else None
    L = [f"# News seat — {FIRST_DAY} .. {LAST_DAY}", "",
         f"eligible liquid events {len(rows)}; NEWS_UP (IEX reaction) {len(up)} on {len(days)} sessions; half A ends {cut}.",
         f"drops: {dict(drops)}", "",
         "| half | events | sessions | gross | net (10 bp) | t(net) | net (20 bp) | verdict |", "|---|---:|---:|---:|---:|---:|---:|---|"]
    verdicts = []
    for h in "AB":
        part = [r for r in up if (r["day"] <= cut) == (h == "A")]
        by = defaultdict(list)
        for r in part:
            by[r["day"]].append(r["gross"] - COST)
        mu, t, n, nd = de(by)
        g, _tg, _n, _d = de({d: [x + COST for x in v] for d, v in by.items()})
        m20, _t20, _n20, _d20 = de({d: [x - COST for x in v] for d, v in by.items()})
        v = half_verdict(mu, t, n, nd, m20)
        verdicts.append(v)
        L.append(f"| {h} | {n} | {nd} | {_bp(g)} | {_bp(mu)} | {_t(t)} | {_bp(m20)} | {v} |")
    final = "PASS" if all(v == "PASS" for v in verdicts) else ("FAIL" if "FAIL" in verdicts else "UNDERPOWERED")
    L += ["", f"**NEWS_UP (IEX reaction): {final}**", "", "### Information only", ""]

    def line(label, part, fn=lambda r: r["gross"]):
        by = defaultdict(list)
        for r in part:
            x = fn(r)
            if x is not None:
                by[r["day"]].append(x)
        mu, t, n, nd = de(by)
        L.append(f"- {label}: {_bp(mu)} bp (t {_t(t)}, n {n}, {nd} sessions)")

    line("NEWS_UP on the SIP reaction (not computable live: SIP is delayed)", up_sip)
    line("all fresh liquid headlines, gross 10m", rows)
    down = [r for r in rows if r["react"] <= -REACT_MIN]
    line("NEWS_DOWN long, gross 10m", down)
    line("NEWS_DOWN short (sign flipped), gross 10m", down, lambda r: -r["gross"])
    line("NEWS_UP net at 20 bp", up, lambda r: r["gross"] - 2 * COST)
    line("NEWS_UP pre-headline drift (t0-11 -> t0-1)", up,
         lambda r: (r["pre_px"] / r["sip"][r["t0"] - 11][1] - 1) if r["t0"] - 11 in r["sip"] else None)
    line("NEWS_UP reaction itself (t0-1 -> e-1)", up, lambda r: r["react"])
    for lag, note in ((1, " (unrealistic: as little as 1 s after publication)"), (5, "")):
        def f(r, lag=lag):
            x = evaluate(r, r["sip"], lag=lag)
            return None if x is None or x["react"] < REACT_MIN else x["gross"]
        line(f"NEWS_UP(SIP) with entry at t0+{lag}{note}", rows, f)
    line("NEWS_UP 30-minute hold", up, lambda r: E.markout(r["sip"], r["entry_min"], 30))
    line("NEWS_UP lift vs later same-name minute", up, lambda r: (
        None if (k := E.control_minute(r["sym"], r["day"], "NEWS", r["sip"], set(), r["entry_min"])) is None
        else r["gross"] - E.markout(r["sip"], k)))
    by_day_syms = defaultdict(list)
    for r in rows:
        by_day_syms[r["day"]].append(r)

    def cross(r):
        others = [o for o in by_day_syms[r["day"]] if o["sym"] != r["sym"]]
        rng = random.Random(f"{r['sym']}|{r['day']}|{r['entry_min']}|news-cross")
        rng.shuffle(others)
        for o in others:
            x = E.markout(o["sip"], r["entry_min"])
            if x is not None:
                return r["gross"] - x
        return None
    line("NEWS_UP lift vs another same-day NEWS symbol at the same minute (news-names-only control)", up, cross)
    line("NEWS_UP minus SPY same-minute 10m markout (beta check)", up, lambda r: (
        None if r["spy"] is None or (x := E.markout(r["spy"], r["entry_min"])) is None else r["gross"] - x))
    line("NEWS_UP reaction articles (written after the move)", [r for r in up if REACTION_RE.search(r["headline"])])
    line("NEWS_UP non-reaction articles", [r for r in up if not REACTION_RE.search(r["headline"])])
    rw = [runway(r["sip"], r["entry_min"]) for r in up]
    rw = [x for x in rw if x]
    if rw:
        L.append(f"- NEWS_UP runway (30 min): best close median {_bp(statistics.median(x['best'] for x in rw))} bp; "
                 f"+0.5% before -0.5% {sum(x['up05'] for x in rw) / len(rw):.0%}; +1% before -0.5% "
                 f"{sum(x['up1'] for x in rw) / len(rw):.0%} (n {len(rw)})")
    line("NEWS_UP analyst-action headlines", [r for r in up if ANALYST.search(r["headline"])])
    line("NEWS_UP other headlines", [r for r in up if not ANALYST.search(r["headline"])])
    try:
        import news_feed as NF
        line("NEWS_UP bullish-regex headlines", [r for r in up if NF.BULLISH.search(r["headline"])])
        line("NEWS_UP bearish-regex headlines", [r for r in up if NF.BEARISH.search(r["headline"])])
    except Exception:  # noqa: BLE001
        pass
    line("NEWS_UP price >= $30", [r for r in up if r["pre_px"] >= 30])
    for hr in range(9, 16):
        line(f"NEWS_UP headline hour {hr}", [r for r in up if r["t0"] // 60 == hr])
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fetch-news", action="store_true")
    ap.add_argument("--fetch-bars", action="store_true")
    ap.add_argument("--score", action="store_true")
    a = ap.parse_args()
    if a.fetch_news:
        do_fetch_news()
        return
    if a.fetch_bars:
        do_fetch_bars()
        return
    days = [d for d in weekdays(FIRST_DAY, LAST_DAY) if load_news(d) is not None]
    n_ev = {d: len(eligible_events(load_news(d))) for d in days}
    have_bars = [d for d in days if os.path.exists(os.path.join(BARS_DIR, f"{d}.pkl"))]
    print(f"news days {len(days)}, bar days {len(have_bars)}, eligible headline events {sum(n_ev.values())} "
          f"(median {statistics.median(n_ev.values()) if n_ev else 0}/day)")
    allwd = weekdays(FIRST_DAY, LAST_DAY)
    print("weekdays with no news file:", [d for d in allwd if d not in days])
    print("news days with events but no bar file:", [d for d in days if n_ev[d] and d not in have_bars])
    if not a.score:
        print("(inventory only; --score after the prereg has a result-skeptic READY)")
        return
    daily = pickle.load(open(DAILY, "rb")) if os.path.exists(DAILY) else {}
    rows, drops = build(days, daily)
    lost = drops["no_bars"] + drops["adv_unknown"]
    if drops["eligible"] and lost > 0.05 * drops["eligible"]:
        raise SystemExit(f"ABORT: {lost}/{drops['eligible']} eligible events lack bars or ADV (> 5%); refetch first. {dict(drops)}")
    md = report(rows, drops)
    print(md)
    with open(os.path.join(OUT_DIR, "result.md"), "w") as f:
        f.write(md + "\n")


if __name__ == "__main__":
    main()
