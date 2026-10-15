#!/usr/bin/env python3
"""What happens to a momentum-list name right after its alert? (operator 2026-10-15; INFORMATION ONLY)

The 10/15 morning look used the dashboard's recorded prices, which stop when a name leaves the list, so its
10-30 minute numbers were survivorship-biased. This uses SIP 1-minute bars for every alerted name-day instead.

Events (ai_reports/sessions/DAY/discord.jsonl.gz): first alert of the day per name, first hot (burst) flag,
second alert. Entry = SIP open of the minute after the alert (we cannot act inside the alert's minute).
Markouts to the SIP close at +1, +2, +5, +10, +30, +60 minutes. Cost = the full SIP quoted spread at the
alert (replay_costing.entry_spread_bp; one round trip). Control = the same name at a random minute 15-75 min
later, same horizons. Day-clustered means; medians and share up shown because these names are skewed.

A positive cell here is a hypothesis for a pre-registered forward test, not evidence.
USAGE (mini, after 16:15 ET): .venv/bin/python tools/studies/momentum_alert_markouts.py
"""
from __future__ import annotations

import glob
import gzip
import json
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
ET = ZoneInfo("America/New_York")
H = (1, 2, 5, 10, 30, 60)
CACHE = os.path.join(ROOT, "ai_reports", "momentum_alerts")


def et_min(ts: float) -> int:
    d = datetime.fromtimestamp(ts, ET)
    return d.hour * 60 + d.minute


def events() -> dict:
    out = defaultdict(list)          # day -> [(kind, sym, ts)]
    for f in sorted(glob.glob(os.path.join(ROOT, "ai_reports", "sessions", "2026-*", "discord.jsonl.gz"))):
        day = f.split(os.sep)[-2]
        rows = sorted((json.loads(x) for x in gzip.open(f) if x.strip()), key=lambda r: r["ts"])
        seen, seen_b, cnt = set(), set(), Counter()
        for a in rows:
            s = str(a.get("symbol") or "").upper()
            if not s.isalpha() or len(s) > 5:
                continue
            cnt[s] += 1
            if s not in seen:
                out[day].append(("first_alert", s, a["ts"]))
                seen.add(s)
            if a.get("burst") and s not in seen_b:
                out[day].append(("hot_first", s, a["ts"]))
                seen_b.add(s)
            if cnt[s] == 2:
                out[day].append(("second_alert", s, a["ts"]))
    return out


def fetch_bars(day: str, syms: list[str]) -> dict:
    p = os.path.join(CACHE, f"{day}.pkl")
    if os.path.exists(p):
        return pickle.load(open(p, "rb"))
    import bars as B
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    cl = B.client()
    d0 = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
    out, failed = {}, False
    for i in range(0, len(syms), 100):
        chunk = syms[i:i + 100]
        for attempt in range(4):
            try:
                df = cl.get_stock_bars(StockBarsRequest(symbol_or_symbols=chunk, timeframe=TimeFrame.Minute,
                                                        start=d0.replace(hour=4).astimezone(timezone.utc),
                                                        end=d0.replace(hour=20).astimezone(timezone.utc),
                                                        feed=DataFeed.SIP)).df
                break
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                if "invalid symbol" in msg:
                    bad = msg.split("invalid symbol:")[-1].strip().strip('"}').split()[0]
                    chunk = [c for c in chunk if c != bad.upper()]
                    continue
                print(f"  {day} bars try {attempt}: {msg[:80]}", flush=True)
                time.sleep(5 * (attempt + 1))
        else:
            failed = True                    # every try failed: use the bars, but never cache a partial day
            continue
        if df is None or df.empty:
            continue
        for s in df.index.get_level_values("symbol").unique():
            sub = df.xs(s, level="symbol")
            out[str(s)] = {et_min(t.timestamp()): (float(r.open), float(r.close)) for t, r in sub.iterrows()}
        time.sleep(0.4)
    if not failed:
        os.makedirs(CACHE, exist_ok=True)
        pickle.dump(out, open(p, "wb"))
    return out


def close_at_or_before(b: dict, m: int, back: int = 15):
    for k in range(m, m - back - 1, -1):
        if k in b:
            return b[k][1]
    return None


def markouts(b: dict, entry_min: int) -> dict | None:
    if entry_min not in b:
        return None
    p0 = b[entry_min][0]
    out = {}
    for h in H:
        c = close_at_or_before(b, entry_min + h - 1)
        out[h] = (c / p0 - 1) if c else None
    return out


def main() -> None:
    import replay_costing as RC
    cache, box = RC.load_cache(), {}
    ev = events()
    res = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))   # key -> h -> day -> [net bp]
    ctl = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    gross = defaultdict(lambda: defaultdict(list))
    spreads = defaultdict(list)
    skips = Counter()
    for day, evs in sorted(ev.items()):
        b_all = fetch_bars(day, sorted({s for _k, s, _t in evs}))
        for kind, s, ts in evs:
            b = b_all.get(s)
            if not b:
                skips["no_bars"] += 1
                continue
            m0 = et_min(ts) + 1
            mk = markouts(b, m0)
            if not mk:
                skips["no_entry_bar"] += 1
                continue
            sp = RC.entry_spread_bp(cache, s, ts, box)
            if sp is None:
                skips["no_nbbo"] += 1
                continue
            sess = "rth" if 9 * 60 + 30 <= m0 < 16 * 60 else "pre/post"
            km = m0 + random.Random(f"{day}|{s}|{ts}").randint(15, 75)
            mc = markouts(b, km) if km in b else None
            for key in (kind, f"{kind}|{sess}"):
                spreads[key].append(sp)
                for h in H:
                    if mk[h] is not None:
                        res[key][h][day].append(mk[h] * 1e4 - sp)
                        gross[key][h].append(mk[h] * 1e4)
                    if mc and mc[h] is not None:
                        ctl[key][h][day].append(mc[h] * 1e4 - sp)
    RC.save_cache(cache)

    def dmean(by):
        means = [st.fmean(v) for v in by.values() if v]
        if len(means) < 3:
            return None, None, len(means)
        mu, sd = st.fmean(means), st.stdev(means)
        return mu, (mu / (sd / len(means) ** 0.5) if sd > 0 else None), len(means)

    lines = ["# Momentum alerts: what happens after the alert (SIP bars, net of the full spread; INFORMATION ONLY)", "",
             f"skips {dict(skips)}", ""]
    for key in sorted(res):
        sp = st.median(spreads[key]) if spreads[key] else None
        lines.append(f"## {key} (median spread {sp:.0f} bp)" if sp is not None else f"## {key}")
        lines.append("| hold | n | net, day-mean (t) | gross median | share up (gross) | control net, day-mean |")
        lines.append("|---:|---:|---:|---:|---:|---:|")
        for h in H:
            mu, t, nd = dmean(res[key][h])
            cm, _ct, _ = dmean(ctl[key][h])
            g = gross[key][h]
            if not g:
                continue
            lines.append(f"| {h}m | {len(g)} | {mu if mu is None else round(mu)} ({t if t is None else round(t, 2)}) "
                         f"| {st.median(g):+.0f} | {sum(x > 0 for x in g) / len(g):.0%} | {cm if cm is None else round(cm)} |")
        lines.append("")
    out = "\n".join(lines)
    print(out)
    os.makedirs(os.path.join(ROOT, "docs", "studies"), exist_ok=True)
    with open(os.path.join(CACHE, "result.md"), "w") as fh:
        fh.write(out + "\n")


if __name__ == "__main__":
    main()
