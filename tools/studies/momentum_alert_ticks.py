#!/usr/bin/env python3
"""Can a 30-60 second scalp on a fresh momentum alert beat the spread? (operator 2026-10-15; INFORMATION ONLY)

Tick replay on historical SIP trades + quotes (free once 15 min old) for every recorded momentum alert
(first alert of the day, first hot/burst flag, second alert; ai_reports/sessions/DAY/discord.jsonl.gz).

Entry: 5 s after the alert (OCR + order delay), paying the ASK of the latest quote at that moment (no
quote within 30 s = skipped, counted). Exits, fixed before any result:
  hold30   sell at the bid 30 s after entry
  hold60   sell at the bid 60 s after entry
  trail05  sell at the bid when a trade prints 0.5% under the running high since entry, or at 180 s
  leash15  hard stop 1% under entry; sell at the bid after 15 s with no new high; or at 180 s
Control: the same rules at a random time 15-75 minutes later on the same name.
Net = exit bid / entry ask - 1 (the whole spread is inside). Day-clustered means, medians, share > 0.

A positive cell is a hypothesis for a pre-registered forward test (and the SIP month), not evidence.
USAGE (mini, after 16:15 ET): .venv/bin/python tools/studies/momentum_alert_ticks.py
"""
from __future__ import annotations

import bisect
import os
import pickle
import random
import statistics as st
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ROOT = os.getcwd()
sys.path[:0] = [os.path.join(ROOT, "tools", "studies"), os.path.join(ROOT, "tools"), ROOT]
import momentum_alert_markouts as M  # noqa: E402

ET = ZoneInfo("America/New_York")
DELAY, WINDOW, QUOTE_MAX_AGE = 5.0, 200.0, 30.0
RULES = ("hold30", "hold60", "trail05", "leash15")
CACHE = os.path.join(ROOT, "ai_reports", "momentum_alerts", "ticks")


# ── pure ─────────────────────────────────────────────────────────────────────

def quote_at(qts: list, quotes: list, t: float):
    """(bid, ask) of the latest quote at or before t, or None when older than QUOTE_MAX_AGE."""
    i = bisect.bisect_right(qts, t) - 1
    if i < 0 or t - qts[i] > QUOTE_MAX_AGE:
        return None
    b, a = quotes[i]
    return (b, a) if b > 0 and a >= b else None


def exit_time(rule: str, t0: float, entry: float, trades: list) -> float:
    """When the rule sells (the sale is at the bid then). trades = [(ts, price)] after t0, sorted."""
    if rule == "hold30":
        return t0 + 30
    if rule == "hold60":
        return t0 + 60
    cap = t0 + 180
    high, last_high_t = entry, t0
    for ts, p in trades:
        if ts > cap:
            break
        if rule == "trail05":
            high = max(high, p)
            if p <= high * 0.995:
                return ts
        else:  # leash15
            if p <= entry * 0.99:
                return ts
            if p > high:
                high, last_high_t = p, ts
            elif ts - last_high_t >= 15:
                return ts
    return cap


def scalp(trades: list, qts: list, quotes: list, t_alert: float) -> dict | None:
    """{rule: net return} for one alert, or None when there is no usable quote at entry."""
    t0 = t_alert + DELAY
    q = quote_at(qts, quotes, t0)
    if q is None:
        return None
    entry = q[1]
    after = [(ts, p) for ts, p in trades if ts > t0]
    out = {}
    for r in RULES:
        tx = exit_time(r, t0, entry, after)
        qx = quote_at(qts, quotes, tx)
        out[r] = (qx[0] / entry - 1) if qx else None
    out["spread_bp"] = (q[1] - q[0]) / ((q[0] + q[1]) / 2) * 1e4
    return out


# ── data ─────────────────────────────────────────────────────────────────────

def fetch_window(cl, sym: str, t: float) -> tuple[list, list, list]:
    """(trades [(ts, px)], quote times, quotes [(bid, ask)]) for [t - 40 s, t + WINDOW]. Cached."""
    os.makedirs(CACHE, exist_ok=True)
    p = os.path.join(CACHE, f"{sym}_{int(t)}.pkl")
    if os.path.exists(p):
        return pickle.load(open(p, "rb"))
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockQuotesRequest, StockTradesRequest
    start = datetime.fromtimestamp(t - 40, timezone.utc)
    end = datetime.fromtimestamp(t + WINDOW, timezone.utc)
    trades, qts, quotes = [], [], []
    for attempt in range(4):
        try:
            tr = cl.get_stock_trades(StockTradesRequest(symbol_or_symbols=sym, start=start, end=end,
                                                        feed=DataFeed.SIP, limit=20000)).data.get(sym) or []
            qu = cl.get_stock_quotes(StockQuotesRequest(symbol_or_symbols=sym, start=start, end=end,
                                                        feed=DataFeed.SIP, limit=20000)).data.get(sym) or []
            trades = [(x.timestamp.timestamp(), float(x.price)) for x in tr if x.price]
            for x in qu:
                qts.append(x.timestamp.timestamp())
                quotes.append((float(x.bid_price or 0), float(x.ask_price or 0)))
            break
        except Exception as e:  # noqa: BLE001
            print(f"  {sym} {int(t)} try {attempt}: {str(e)[:80]}", flush=True)
            time.sleep(5 * (attempt + 1))
    else:
        return [], [], []
    trades.sort()
    order = sorted(range(len(qts)), key=lambda i: qts[i])
    qts, quotes = [qts[i] for i in order], [quotes[i] for i in order]
    pickle.dump((trades, qts, quotes), open(p, "wb"))
    time.sleep(0.35)
    return trades, qts, quotes


def main() -> None:
    import bars as B
    cl = B.client()
    ev = M.events()
    res = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))     # key -> rule -> day -> [net bp]
    ctl = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    flat = defaultdict(lambda: defaultdict(list))
    spreads = defaultdict(list)
    skips = Counter()
    for day, evs in sorted(ev.items()):
        for kind, s, ts in evs:
            sess = "rth" if 9 * 60 + 30 <= M.et_min(ts) < 16 * 60 else "pre/post"
            got = scalp(*fetch_window(cl, s, ts), ts)
            if got is None:
                skips["no_quote_at_entry"] += 1
                continue
            tc = ts + random.Random(f"{day}|{s}|{ts}|ctl").randint(900, 4500)
            got_c = scalp(*fetch_window(cl, s, tc), tc)
            for key in (kind, f"{kind}|{sess}"):
                spreads[key].append(got["spread_bp"])
                for r in RULES:
                    if got[r] is not None:
                        res[key][r][day].append(got[r] * 1e4)
                        flat[key][r].append(got[r] * 1e4)
                    if got_c and got_c.get(r) is not None:
                        ctl[key][r][day].append(got_c[r] * 1e4)

    def dmean(by):
        means = [st.fmean(v) for v in by.values() if v]
        if len(means) < 3:
            return None, None
        mu, sd = st.fmean(means), st.stdev(means)
        return mu, (mu / (sd / len(means) ** 0.5) if sd > 0 else None)

    lines = ["# Momentum alerts: 30-180 s scalps on SIP ticks (entry ask 5 s after the alert, exit bid; INFORMATION ONLY)",
             "", f"skips {dict(skips)}", ""]
    for key in sorted(res):
        sp = st.median(spreads[key]) if spreads[key] else 0
        lines += [f"## {key} (median spread at entry {sp:.0f} bp)", "",
                  "| exit | n | net day-mean (t) | net median | share > 0 | control net day-mean |", "|---|---:|---:|---:|---:|---:|"]
        for r in RULES:
            v = flat[key][r]
            if not v:
                continue
            mu, t = dmean(res[key][r])
            cm, _ = dmean(ctl[key][r])
            lines.append(f"| {r} | {len(v)} | {mu if mu is None else round(mu)} ({t if t is None else round(t, 2)}) | "
                         f"{st.median(v):+.0f} | {sum(x > 0 for x in v) / len(v):.0%} | {cm if cm is None else round(cm)} |")
        lines.append("")
    out = "\n".join(lines)
    print(out)
    with open(os.path.join(ROOT, "ai_reports", "momentum_alerts", "ticks_result.md"), "w") as fh:
        fh.write(out + "\n")


if __name__ == "__main__":
    main()
